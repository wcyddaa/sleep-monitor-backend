"""测线上后端各接口的真实响应时间，定位 62 秒到底花在哪。

用法：uv run python tools/probe_latency.py
"""
import json
import time

import httpx

BASE = "https://sleep-m-backend-edgvqrpnhv.cn-shanghai.fcapp.run/api/v1"


def timed(client, method, path, **kw):
    t0 = time.time()
    try:
        r = client.request(method, BASE + path, **kw)
        sec = time.time() - t0
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:200]}
        return sec, r.status_code, body
    except Exception as e:
        return time.time() - t0, -1, {"_err": f"{type(e).__name__}: {e}"}


with httpx.Client(timeout=120) as c:
    # 1) 健康检查 / 冷启动探测
    for i in range(2):
        sec, code, body = timed(c, "GET", "/../health")
        print(f"[{i}] GET /health          {sec:6.2f}s  code={code}  {str(body)[:80]}")

    # 2) 登录
    sec, code, body = timed(
        c, "POST", "/auth/login", json={"username": "admin", "password": "1234"}
    )
    print(f"     POST /auth/login      {sec:6.2f}s  code={code}")
    token = None
    if isinstance(body, dict) and body.get("data"):
        token = body["data"].get("access_token")
    print(f"     token={'有' if token else '无'}")

    if not token:
        print("\n登录失败，后续测试跳过。返回：", json.dumps(body, ensure_ascii=False)[:300])
        raise SystemExit

    H = {"Authorization": f"Bearer {token}"}

    # 3) 设备列表
    sec, code, body = timed(c, "GET", "/devices", headers=H)
    print(f"     GET /devices          {sec:6.2f}s  code={code}")
    devices = (body.get("data") or []) if isinstance(body, dict) else []
    if not devices:
        print("没有设备，跳过趋势测试")
        raise SystemExit
    dev_id = devices[0]["id"]
    print(f"     设备 id={dev_id}")

    # 4) 趋势：两次调用，看第二次是否快（能区分冷启动 vs 计算慢）
    for i in range(3):
        sec, code, body = timed(
            c, "GET", "/sleep/trend/weekly", params={"device_id": dev_id, "days": 7}, headers=H
        )
        n = len((body.get("data") or {}).get("items") or []) if isinstance(body, dict) else 0
        print(f"[{i}] GET /sleep/trend/weekly {sec:6.2f}s  code={code}  items={n}")

    # 5) dashboard：验证新代码是否已部署（应该带 items 字段）
    sec, code, body = timed(
        c, "GET", "/sleep/trend/dashboard", params={"device_id": dev_id, "days": 7}, headers=H
    )
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    has_items = "items" in data
    print(
        f"     GET /sleep/trend/dashboard {sec:6.2f}s  code={code}  "
        f"items字段={'有（新代码已部署）' if has_items else '无（后端还是旧代码！）'}"
    )
