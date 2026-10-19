# -*- coding: utf-8 -*-
"""临时脚本：直连 TiDB 清空 sleep_records / sleep_frames / device_commands。
用项目自带 asyncmy 驱动（.venv 里没有 pymysql）。
"""
import asyncio
import os
import sys
import urllib.parse

import asyncmy


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
DB   = _CRED["database"]
PWD  = _CRED["password"]

CLEAR = ["device_commands", "sleep_records", "sleep_frames"]

# TiDB Cloud 强制 TLS；本地无 ca.pem，用 ssl=True 走加密（asyncmy 只认 ssl 参数）
SSL_ARGS = {"ssl": True}


async def main(dry_run: bool):
    conn = await asyncmy.connect(
        host=HOST, port=PORT, user=USER, password=PWD, db=DB,
        charset="utf8mb4", autocommit=True, **SSL_ARGS,
    )
    print("已连接 TiDB")

    async with conn.cursor() as cur:
        await cur.execute("SHOW TABLES")
        tables = [r[0] for r in await cur.fetchall()]
        print("表清单:", tables)

        print("\n=== 清空前 ===")
        for t in tables:
            await cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            n = (await cur.fetchone())[0]
            print(f"  {t:<24} {n:>6} 行")

        if dry_run:
            print("\n--dry-run：未执行")
            conn.close()
            return

        await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for t in CLEAR:
            if t not in tables:
                print(f"  跳过（表不存在）: {t}")
                continue
            await cur.execute(f"DELETE FROM `{t}`")
            await cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            left = (await cur.fetchone())[0]
            print(f"  DELETE {t:<22} -> 剩余 {left} 行 {'OK' if left == 0 else 'FAIL'}")
        await cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        print("\n=== 清空后 ===")
        for t in tables:
            await cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            n = (await cur.fetchone())[0]
            print(f"  {t:<24} {n:>6} 行")

    conn.close()
    print("\n完成")


if __name__ == "__main__":
    # Windows 下 Proactor 事件循环 + SSL 会报 WinError 87，必须换 Selector
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main("--dry-run" in sys.argv))
