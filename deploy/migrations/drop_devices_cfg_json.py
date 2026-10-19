# -*- coding: utf-8 -*-
r"""
drop_devices_cfg_json.py — 一次性迁移：删除 devices.cfg_json 列（2026-10-08）

为什么删：
    三开关（snore/stage/bcg）改成**设备本地状态**，走 BLE 0x04/0x05 读写、
    不经云端；后端不再落 cfg，当前配置随每一帧的 raw_json.cfg 字段可见。

⚠️ 执行顺序（重要）：
    请**先部署新后端**，再跑本脚本。
    线上 FC 若还跑着引用 cfg_json 的旧代码，删列后所有 /devices 接口会 500
    （SQLAlchemy 会把 cfg_json 写进 SELECT）。

行为：
    1. 检查列是否存在，不存在则直接退出
    2. 把现有 cfg_json 值备份到 deploy/migrations/backup_devices_cfg_json_<ts>.json
    3. ALTER TABLE devices DROP COLUMN cfg_json
    4. 复查列已消失

用法：
    cd C:\code\1\projects\sleep-monitor-backend
    $env:TIDB_PASSWORD = "<TiDB root 密码>"
    python deploy\migrations\drop_devices_cfg_json.py

    # 只看不改：
    python deploy\migrations\drop_devices_cfg_json.py --dry-run
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.parse

import pymysql


def _tidb_creds():
    """TiDB 凭据：优先环境变量，缺失项回退解析向上逐级查找的 .env；都没有则退出。"""
    env = {
        "TIDB_HOST": os.environ.get("TIDB_HOST"),
        "TIDB_PORT": os.environ.get("TIDB_PORT"),
        "TIDB_USER": os.environ.get("TIDB_USER"),
        "TIDB_PASSWORD": os.environ.get("TIDB_PASSWORD"),
        "TIDB_DATABASE": os.environ.get("TIDB_DATABASE"),
    }
    if any(v is None for v in env.values()):
        _fill_creds_from_env_file(env)
    if not env["TIDB_USER"] or not env["TIDB_PASSWORD"]:
        sys.exit(
            "缺少 TiDB 凭据：请设置环境变量 TIDB_USER / TIDB_PASSWORD"
            "（可选 TIDB_HOST / TIDB_PORT / TIDB_DATABASE），"
            "或在仓库根目录 .env 中提供 DATABASE_URL=mysql+asyncmy://user:pass@host:port/db "
            "或 TIDB_USER= / TIDB_PASSWORD= 等键"
        )
    return {
        "host": env["TIDB_HOST"] or "gateway01.ap-northeast-1.prod.aws.tidbcloud.com",
        "port": int(env["TIDB_PORT"]) if env["TIDB_PORT"] else 4000,
        "user": env["TIDB_USER"],
        "password": env["TIDB_PASSWORD"],
        "database": env["TIDB_DATABASE"] or "test",
    }


def _fill_creds_from_env_file(env):
    """从脚本所在目录向上逐级查找 .env，解析 DATABASE_URL 或 TIDB_* 键，只补缺失项。"""
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        path = os.path.join(d, ".env")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k.startswith("export "):
                        k = k[len("export "):].strip()
                    v = v.strip().strip('"').strip("'")
                    if k == "DATABASE_URL":
                        u = urllib.parse.urlparse(v)
                        if not u.scheme.startswith("mysql"):
                            continue
                        vals = {
                            "TIDB_USER": u.username,
                            "TIDB_PASSWORD": u.password,
                            "TIDB_HOST": u.hostname,
                            "TIDB_PORT": u.port,
                            "TIDB_DATABASE": u.path.lstrip("/"),
                        }
                        for key, val in vals.items():
                            if env[key] is None and val:
                                if key in ("TIDB_USER", "TIDB_PASSWORD"):
                                    val = urllib.parse.unquote(val)
                                env[key] = str(val)
                    elif k in env and env[k] is None and v:
                        env[k] = v
            return
        parent = os.path.dirname(d)
        if parent == d:  # 已到文件系统根
            return
        d = parent


_CRED = _tidb_creds()
HOST = _CRED["host"]
PORT = _CRED["port"]
USER = _CRED["user"]
DB = _CRED["database"]
PWD = _CRED["password"]
TABLE = "devices"
COLUMN = "cfg_json"

BACKUP_DIR = os.path.dirname(os.path.abspath(__file__))


def connect():
    return pymysql.connect(
        host=HOST, port=PORT, user=USER, password=PWD,
        database=DB, charset="utf8mb4", autocommit=True,
    )


def column_exists(cur) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (DB, TABLE, COLUMN),
    )
    return int(cur.fetchone()[0]) > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只检查不执行")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()

    if not column_exists(cur):
        print(f"✅ {TABLE}.{COLUMN} 不存在，无需迁移。")
        return 0

    print(f"发现 {TABLE}.{COLUMN}，准备删除…")

    # 1) 备份现值（万一需要回滚）
    cur.execute(f"SELECT id, device_id, {COLUMN} FROM {TABLE} WHERE {COLUMN} IS NOT NULL")
    rows = cur.fetchall()
    backup = {
        "dropped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "table": TABLE,
        "column": COLUMN,
        "rows": [{"id": r[0], "device_id": r[1], "cfg_json": r[2]} for r in rows],
    }
    if rows:
        path = os.path.join(
            BACKUP_DIR, f"backup_devices_{COLUMN}_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
        print(f"已备份 {len(rows)} 行非空值 -> {path}")
    else:
        print("该列没有非空值，无需备份。")

    if args.dry_run:
        print("--dry-run：未执行 DROP。")
        return 0

    # 2) 删列
    cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
    print(f"已执行：ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")

    # 3) 复查
    if column_exists(cur):
        print("❌ 复查失败：列仍然存在", file=sys.stderr)
        return 1
    print(f"✅ 复查通过：{TABLE}.{COLUMN} 已删除。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
