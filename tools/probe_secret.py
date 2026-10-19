# -*- coding: utf-8 -*-
r"""
probe_secret.py - 探测线上后端实际使用的 JWT SECRET_KEY

原理：用候选密钥手工签一个 HS256 JWT（sub=1, admin），打 /api/v1/auth/me：
  - 200 → 该密钥就是线上在用的
  - 401 → 不是

用途：验证「本地 .env 的 SECRET_KEY」和「线上 FC 的 SECRET_KEY」是否一致。
      不一致时，本地起的后端签发的 token 打线上必然 401。

用法：
  python tools\probe_secret.py
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://sleep-m-backend-edgvqrpnhv.cn-shanghai.fcapp.run/api/v1"

# 想探测更多候选就往这里加 (标签, 密钥)
CANDIDATES = [
    ("本地 .env 的值", "change-me-in-production-please-use-a-long-random-string"),
    ("config.py 默认值", "dev-secret-key-please-change-in-production"),
]


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_jwt(secret: str, sub: str = "1") -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": sub, "exp": int(time.time()) + 600, "username": "admin"}
    seg = (
        f"{b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{b64url(sig)}"


def probe(token: str):
    req = urllib.request.Request(
        f"{BASE}/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def main():
    hit = None
    for label, secret in CANDIDATES:
        status, body = probe(make_jwt(secret))
        if status == 200:
            verdict = "✅ 匹配 —— 线上用的就是这个"
            hit = label
        elif status == 401:
            verdict = "❌ 不匹配（401）"
        else:
            verdict = f"⚠️ 异常 status={status} body={body}"
        print(f"[{label}] secret={secret[:24]}...  -> {verdict}")

    print()
    if hit is None:
        print("两个候选都 401 → 线上的 SECRET_KEY 是第三个值（FC 环境变量里另配的）。")
        print("含义：本地 .env 与线上不同步，本地起的后端签发的 token 打线上会被拒。")
    else:
        print(f"结论：线上 SECRET_KEY 来自【{hit}】。")
        print("请确认本地 .env 与 FC 环境变量是否应该保持一致，避免 token 互不认可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
