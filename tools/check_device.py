# -*- coding: utf-8 -*-
r"""
check_device.py - 一次性检查 sleep-monitor-backend 数据库

输出 4 段：
  1. users       全表（看登录态 / wechat_openid）
  2. devices     全表（看 owner_id 是否 NULL、is_online、cfg_json 是否已设）
  3. 指定 device_id 详查
  4. sleep_frames 最近 5 条（看 30s 帧是否到）

对应 TROUBLESHOOTING.md 的关卡 3 / 4 / 5 / 6。

用法：
  cd C:\code\1\projects\sleep-monitor-backend
  $env:TIDB_PASSWORD = "<TiDB root 密码>"
  python -m tools.check_device

（凭据优先取 TIDB_USER / TIDB_PASSWORD 等环境变量，缺失项回退解析仓库根目录 .env；
  两者都拿不到时脚本直接退出）
"""
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

# 想详查别的设备就改这里
TARGET_DEVICE_ID = "690ae11fe41f2979315ed4e3_stm32f407-esp8266"


def conn():
    return pymysql.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PWD,
        database=DB,
        charset="utf8mb4",
        autocommit=True,
    )


def main():
    c = conn()
    cur = c.cursor()

    print("=" * 80)
    print("1. users 表（看登录态）")
    print("=" * 80)
    cur.execute(
        "SELECT id, username, email, wechat_openid, is_active, created_at "
        "FROM users ORDER BY id"
    )
    for row in cur.fetchall():
        print(
            f"  id={row[0]:>4}  username={row[1]}  email={row[2]}  "
            f"wechat_openid={row[3]}  active={row[4]}  created_at={row[5]}"
        )

    print()
    print("=" * 80)
    print("2. devices 表（关键：owner_id 是否为 NULL / is_online）")
    print("=" * 80)
    cur.execute(
        "SELECT id, device_id, name, device_type, owner_id, is_online, last_seen, created_at "
        "FROM devices ORDER BY id"
    )
    rows = cur.fetchall()
    if not rows:
        print("  ⚠️  devices 表为空，完全没注册记录")
    for row in rows:
        owner_marker = f"✓(user {row[4]})" if row[4] else "✗ 待归属(NULL)"
        print(
            f"  id={row[0]:>6}  device_id={row[1]}\n"
            f"          name={row[2]}  device_type={row[3]}\n"
            f"          owner={owner_marker}  is_online={row[5]}  last_seen={row[6]}"
        )

    print()
    print("=" * 80)
    print(f"3. 详查 device_id = {TARGET_DEVICE_ID}")
    print("=" * 80)
    cur.execute(
        "SELECT id, device_id, owner_id, is_online, last_seen "
        "FROM devices WHERE device_id = %s",
        (TARGET_DEVICE_ID,),
    )
    rows = cur.fetchall()
    if not rows:
        print("  ⚠️  该 device_id 不在 devices 表里（说明还没上云/没收到 webhook）")
    for row in rows:
        print(f"  id={row[0]}  device_id={row[1]}")
        print(f"  owner_id={row[2]}  is_online={row[3]}  last_seen={row[4]}")

    print()
    print("=" * 80)
    print("4. sleep_frames 表（看最近的 30s 帧是否到）")
    print("=" * 80)
    cur.execute(
        "SELECT id, device_id, window_start_ts, window_end_ts, stage, "
        "heart_rate, resp_rate, motion, snore_seconds, flags, created_at "
        "FROM sleep_frames ORDER BY id DESC LIMIT 5"
    )
    rows = cur.fetchall()
    if not rows:
        print("  ⚠️  sleep_frames 表为空")
        print("      → 若三开关全 OFF，这是【预期行为】（EdgeAggregator 不启动）")
        print("      → 若开关非 OFF，检查 IoTDA 规则引擎是否把 /device/{id}/frame")
        print("        转发到了 POST /api/v1/ingest/huawei/frame")
    for row in rows:
        print(
            f"  id={row[0]:>5}  device_pk={row[1]}  win={row[2]}-{row[3]}  "
            f"stage={row[4]} hr={row[5]} rr={row[6]} motion={row[7]} "
            f"snore_sec={row[8]} flags=0x{row[9]:02x}  created_at={row[10]}"
        )

    c.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"出错: {e}", file=sys.stderr)
        sys.exit(1)
