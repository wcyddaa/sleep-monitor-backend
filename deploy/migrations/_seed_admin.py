"""连 TiDB 建 admin 账号（密码=1234）"""
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

# 1. 看现有列
cur.execute("SHOW COLUMNS FROM users")
cols = {r[0] for r in cur.fetchall()}
print("现有列:", cols)

# 2. 必要时补 v2_4 迁移（nickname / avatar_url / wx_unionid）
for col, sql in [
    ("nickname",    "ALTER TABLE users ADD COLUMN nickname VARCHAR(64) NULL AFTER full_name"),
    ("avatar_url",  "ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512) NULL AFTER nickname"),
    ("wx_unionid",  "ALTER TABLE users ADD COLUMN wx_unionid VARCHAR(128) NULL AFTER wechat_openid"),
]:
    if col not in cols:
        print(f"  → 补列 {col}")
        cur.execute(sql)
conn.commit()

# 3. 写入 admin（pbkdf2-sha256 哈希，明文密码 = 1234）
hashed = "$pbkdf2-sha256$29000$oRTCWAsBACAEoLRWKgWAcA$zbvJW9mTp8CFakjbJI6oXWPTM8MCMpbhhX0hZkZpW5M"
cur.execute(
    "INSERT INTO users (username, email, hashed_password, full_name, nickname, is_active, created_at) "
    "VALUES (%s, NULL, %s, %s, %s, 1, NOW(6)) "
    "ON DUPLICATE KEY UPDATE id = id",
    ("admin", hashed, "管理员", "admin"),
)
conn.commit()

cur.execute("SELECT id, username, nickname FROM users WHERE username = 'admin'")
print("admin 用户:", cur.fetchone())
conn.close()
print("完成 ✓")
