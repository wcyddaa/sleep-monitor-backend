"""一次性脚本：连 TiDB Cloud 跑 v2.3 迁移。"""
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
cfg = {
    "host":     _CRED["host"],
    "port":     _CRED["port"],
    "user":     _CRED["user"],
    "password": _CRED["password"],
    "database": _CRED["database"],         # 项目用的 test 库
    "ssl":      {"ssl": {}},               # TiDB Cloud 系统 trust 即可
    "autocommit": False,
    "charset":  "utf8mb4",
}

# 1) cfg_json 列
CHECK_COL = """
SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'devices'
   AND COLUMN_NAME = 'cfg_json'
"""
ADD_COL = """
ALTER TABLE devices
  ADD COLUMN cfg_json TEXT NULL AFTER last_seen
"""

# 2) sleep_frames 表
CREATE_FRAMES = """
CREATE TABLE IF NOT EXISTS sleep_frames (
  id              INT          NOT NULL AUTO_INCREMENT,
  device_id       INT          NOT NULL,
  window_start_ts BIGINT       NOT NULL,
  window_end_ts   BIGINT       NOT NULL,
  stage           INT          NOT NULL DEFAULT -1,
  heart_rate      INT          NOT NULL DEFAULT -1,
  resp_rate       INT          NOT NULL DEFAULT -1,
  motion          INT          NOT NULL DEFAULT -1,
  snore_seconds   INT          NOT NULL DEFAULT -1,
  flags           INT          NOT NULL DEFAULT 0,
  raw_json        TEXT         NULL,
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_frame_device_start (device_id, window_start_ts),
  KEY ix_frame_device_end (device_id, window_end_ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""

def main():
    print(f"连 {cfg['host']}:{cfg['port']} db={cfg['database']} ...")
    conn = pymysql.connect(**cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE(), VERSION()")
            print("  current_db/version:", cur.fetchone())
            cur.execute(CHECK_COL)
            n = cur.fetchone()[0]
            print(f"  cfg_json 已存在? {bool(n)}")
            if n == 0:
                cur.execute(ADD_COL)
                print("  -> ALTER TABLE devices ADD COLUMN cfg_json OK")
            else:
                print("  -> 跳过 (列已存在)")
            cur.execute(CREATE_FRAMES)
            print("  -> CREATE TABLE IF NOT EXISTS sleep_frames OK")
            cur.execute(
                "SELECT TABLE_NAME, ENGINE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "  AND TABLE_NAME IN ('devices', 'sleep_frames', 'users', 'sleep_records')"
            )
            for row in cur.fetchall():
                print("    ", row)
        conn.commit()
        print("OK done.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
