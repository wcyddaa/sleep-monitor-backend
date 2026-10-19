"""测速：对比「逐行拉回 Python」vs「SQL GROUP BY 聚合」的耗时。

TiDB 在 ap-northeast-1（东京），本地/FC 在上海，跨国传输是瓶颈。
"""
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
conn = pymysql.connect(
    host=_CRED["host"],
    port=_CRED["port"],
    user=_CRED["user"],
    password=_CRED["password"],
    database=_CRED["database"],
    charset="utf8mb4",
)
cur = conn.cursor()

# 找数据量最大的一台设备
cur.execute("""
    SELECT device_id, COUNT(*) AS n
    FROM sleep_frames
    GROUP BY device_id
    ORDER BY n DESC
    LIMIT 3
""")
devices = cur.fetchall()
print("设备帧数 TOP3:", devices)
if not devices:
    print("sleep_frames 表是空的，无法测速")
    raise SystemExit

device_id = devices[0][0]
cur.execute(
    "SELECT MIN(window_start_ts), MAX(window_start_ts) FROM sleep_frames WHERE device_id=%s",
    (device_id,),
)
mn, mx = cur.fetchone()
print(f"\n测试设备 device_id={device_id}, 时间范围 {mn} ~ {mx}")

range_start = mx - 7 * 86400
range_end = mx + 1

# ---- 旧方式：把 7 天所有列（含 raw_json TEXT）拉回来 ----
t0 = time.time()
cur.execute(
    "SELECT * FROM sleep_frames WHERE device_id=%s AND window_start_ts>=%s AND window_start_ts<%s",
    (device_id, range_start, range_end),
)
old_rows = cur.fetchall()
old_sec = time.time() - t0
old_bytes = sum(
    sum(len(str(c)) if c is not None else 0 for c in row) for row in old_rows
)
print(f"\n[旧] SELECT * 逐行拉取: {len(old_rows)} 行, {old_sec:.2f}s, 约 {old_bytes/1024:.0f} KB")

# ---- 新方式：SQL GROUP BY 聚合，只回 N 行 ----
t0 = time.time()
cur.execute(
    """
    SELECT
      floor((window_start_ts - %s) / 86400) AS day_idx,
      count(*) AS frame_count,
      sum(CASE WHEN stage = 2 THEN 1 ELSE 0 END) AS deep,
      sum(CASE WHEN stage = 1 THEN 1 ELSE 0 END) AS light,
      sum(CASE WHEN stage = 0 THEN 1 ELSE 0 END) AS wake,
      sum(CASE WHEN heart_rate > 0 THEN heart_rate ELSE 0 END) AS hr_sum,
      sum(CASE WHEN heart_rate > 0 THEN 1 ELSE 0 END) AS hr_cnt,
      sum(CASE WHEN resp_rate > 0 THEN resp_rate ELSE 0 END) AS rr_sum,
      sum(CASE WHEN resp_rate > 0 THEN 1 ELSE 0 END) AS rr_cnt,
      sum(CASE WHEN snore_seconds >= 0 THEN snore_seconds ELSE 0 END) AS snore_sum
    FROM sleep_frames
    WHERE device_id=%s AND window_start_ts>=%s AND window_start_ts<%s
    GROUP BY day_idx
    """,
    (range_start, device_id, range_start, range_end),
)
new_rows = cur.fetchall()
new_sec = time.time() - t0
print(f"[新] SQL GROUP BY 聚合:  {len(new_rows)} 行, {new_sec:.2f}s")
print("\n聚合结果（每天一行）:")
for r in new_rows:
    print("  day_idx=%s frames=%s deep=%s light=%s wake=%s snore_sum=%s" % r[:6])

if old_sec > 0:
    print(f"\n提速: {old_sec/new_sec:.1f}x  （{old_sec:.2f}s → {new_sec:.2f}s）")
conn.close()
