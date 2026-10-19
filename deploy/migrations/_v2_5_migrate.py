"""v2.5 迁移：wechat_openid 加唯一索引"""
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
conn = pymysql.connect(
    host=_CRED["host"],
    port=_CRED["port"],
    user=_CRED["user"],
    password=_CRED["password"],
    database=_CRED["database"],
    charset="utf8mb4",
)
cur = conn.cursor()

# 检查索引是否存在
cur.execute("""
    SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'users'
      AND INDEX_NAME = 'ix_users_wechat_openid'
""")
exists = cur.fetchone()[0] > 0

if exists:
    print("[v2.5] 索引已存在，跳过")
else:
    try:
        cur.execute("CREATE UNIQUE INDEX ix_users_wechat_openid ON users(wechat_openid)")
        conn.commit()
        print("[v2.5] 唯一索引 ix_users_wechat_openid 创建成功")
    except Exception as e:
        print(f"[v2.5] 索引创建失败（可能已有重复值）: {e}")

conn.close()
