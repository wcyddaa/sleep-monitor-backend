# -*- coding: utf-8 -*-
r"""
clear_all_device_data.py — 一次性清数据脚本（2026-10-09）

为什么删：
    之前的 sleep_records / sleep_frames / device_commands 都是基于旧架构
    （固件走 /sleep/ingest 上传 vital_signs + audio_segments 摘要）写入的脏数据；
    1) 时间戳错乱（boot 秒当 epoch 秒被写入）导致报告里 8:06 / 12:55 / 20:54 这种乱时间
    2) analysis.model / report 文本里可能含"CNN 毫米波雷达 / qwenplus"等旧提示词产物
    清空后让设备用**新架构**（端侧 30s 帧 + epoch 秒 + 干净提示词）重新生成数据。

⚠️ 行为：
    删三张表（sleep_records / sleep_frames / device_commands）的全部行
    保留 devices（固件重新开机仍按 device_id 找回自己）
    保留 users（用户账号不丢）

⚠️ 执行顺序：
    无前置依赖，直接跑。

用法：
    cd C:\code\1\projects\sleep-monitor-backend
    $env:TIDB_PASSWORD = "<TiDB root 密码>"
    python deploy\migrations\clear_all_device_data.py

    # 只看不改：
    python deploy\migrations\clear_all_device_data.py --dry-run
"""
import argparse
import os
import sys
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

# 要清空的表（按外键依赖顺序：先清依赖方，再清被依赖方）
# sleep_frames 引用 devices；sleep_records / device_commands 也引用 devices；devices 不动
CLEAR_TABLES = [
    "device_commands",
    "sleep_records",
    "sleep_frames",
]


def connect():
    return pymysql.connect(
        host=HOST, port=PORT, user=USER, password=PWD,
        database=DB, charset="utf8mb4", autocommit=True,
    )


def count_rows(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只检查不执行")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()

    # 1) 先统计各表当前行数（让用户看清要删多少）
    print("=== 清空前行数 ===")
    snapshot = {}
    for t in CLEAR_TABLES:
        n = count_rows(cur, t)
        snapshot[t] = n
        print(f"  {t:<22} {n:>6} 行")
    # devices/users 不动,顺便打印让用户安心
    print("=== 保留表（不动） ===")
    for t in ("devices", "users"):
        n = count_rows(cur, t)
        print(f"  {t:<22} {n:>6} 行")

    if args.dry_run:
        print("\n--dry-run：未执行 DELETE。")
        conn.close()
        return 0

    # 2) 关 FK（TiDB 也要保险起见）
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        print("\n已临时关闭 FK 检查（FOREIGN_KEY_CHECKS=0）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 关 FK 失败，继续执行（个别 TiDB 版本可能不支持）：{e}")

    # 3) 逐表 DELETE + 复查 0 行
    failed = []
    for t in CLEAR_TABLES:
        try:
            cur.execute(f"DELETE FROM {t}")
            print(f"  已 DELETE FROM {t}")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ DELETE FROM {t} 失败: {e}", file=sys.stderr)
            failed.append(t)
            continue
        n_after = count_rows(cur, t)
        if n_after != 0:
            print(f"  ❌ 复查失败：{t} 还剩 {n_after} 行", file=sys.stderr)
            failed.append(t)
        else:
            print(f"  ✅ 复查通过：{t} = 0 行")

    # 4) 恢复 FK
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        print("\n已恢复 FK 检查（FOREIGN_KEY_CHECKS=1）")
    except Exception:  # noqa: BLE001
        pass

    # 5) 打印最终保留表行数
    print("\n=== 清空后保留表行数 ===")
    for t in ("devices", "users"):
        n = count_rows(cur, t)
        print(f"  {t:<22} {n:>6} 行")

    conn.close()
    if failed:
        print(f"\n❌ {len(failed)} 张表未清空: {failed}", file=sys.stderr)
        return 1
    print("\n✅ 全部清空完成。设备下次 30s 帧 / 重新分析将按新架构写入干净数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
