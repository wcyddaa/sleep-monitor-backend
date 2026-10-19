#!/usr/bin/env python3
"""华为云 IoTDA 设备侧 MQTT 上报模拟脚本（密钥鉴权）。

模拟一台睡眠监测雷达，通过 MQTT(S) 接入华为云 IoTDA，
把一条 SleepDataIngest 结构的 JSON 上报到：
    $oc/devices/{device_id}/sys/messages/up

用法示例（1883 明文）：
    python scripts/mqtt_device_sim.py \
        --broker dab1a1f2c6.st1.iotda-device.cn-north-4.myhuaweicloud.com \
        --device-id 你的IoTDA设备ID \
        --secret 你的设备密钥 \
        --report-device-id bedroom-01

用法示例（8883 TLS）：
    python scripts/mqtt_device_sim.py \
        --broker dab1a1f2c6.st1.iotda-device.cn-north-4.myhuaweicloud.com \
        --port 8883 --tls --ca ca.pem \
        --device-id 你的IoTDA设备ID --secret 你的设备密钥

参数来源（华为云 IoTDA 控制台）：
  - broker / port : 总览 -> 接入信息（MQTT=1883，MQTTS=8883）
  - device-id     : 设备 -> 设备详情 里的 deviceId（平台分配）
  - secret        : 注册设备时获得的密钥
  - CA 证书       : 「总览 -> 接入信息 -> 证书资源」下载对应 region 的证书

注意：--device-id 是「华为云平台设备 ID」，只用于 MQTT 鉴权和 topic；
      --report-device-id 是上报 JSON 里的 device_id，必须与后端 devices 表
      注册过的设备标识一致（例如 bedroom-01）。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def _utc_hour_ts() -> str:
    """返回华为云鉴权用时间戳，格式 YYYYMMDDHH（UTC）。"""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H")


def build_client_id(device_id: str, ts: str) -> str:
    """一机一密 clientId = {deviceId}_0_1_{时间戳}。

    四段：设备ID ｜ 设备身份标识类型(固定0) ｜ 密码签名类型(1=HMACSHA256校验时间戳) ｜ 时间戳
    """
    return f"{device_id}_0_1_{ts}"


def build_password(secret: str, ts: str) -> str:
    """Password = HMAC-SHA256(key=时间戳, msg=secret)，hex 小写 64 位。"""
    return hmac.new(ts.encode(), secret.encode(), hashlib.sha256).hexdigest()


def build_sample_payload(device_id: str) -> dict:
    """构造一条符合后端 SleepDataIngest 结构的睡眠上报 JSON。"""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=22, minute=0, second=0, microsecond=0)
    end = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return {
        "device_id": device_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "vital_signs": [
            {
                "timestamp": start.isoformat(),
                "heart_rate": 65.0,
                "respiratory_rate": 15.0,
                "bed_status": "in_bed",
                "turnover_count": 1,
            },
            {
                "timestamp": start.replace(hour=23).isoformat(),
                "heart_rate": 58.0,
                "respiratory_rate": 13.0,
                "bed_status": "in_bed",
                "turnover_count": 0,
            },
        ],
        "audio_segments": [
            {
                "start": start.replace(hour=2).isoformat(),
                "end": start.replace(hour=2, minute=5).isoformat(),
                "snore_detected": True,
                "intensity": 0.7,
            }
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="华为云 IoTDA 设备侧 MQTT 上报模拟")
    p.add_argument("--broker", default="6330a71a21.st1.iotda-device.cn-north-4.myhuaweicloud.com", help="IoTDA 接入地址（域名）")
    p.add_argument("--port", type=int, default=1883, help="1883=MQTT，8883=MQTTS")
    p.add_argument("--device-id", default="690ae11fe41f2979315ed4e3_stm32f407-esp8266", help="华为云平台设备 deviceId")
    p.add_argument("--secret", default="123456zxc", help="设备密钥")
    p.add_argument("--report-device-id", default="bedroom-01", help="上报 JSON 里的 device_id（需与后端 devices 表一致）")
    p.add_argument("--topic", default=None, help="默认 $oc/devices/{device_id}/sys/messages/up")
    p.add_argument("--tls", action="store_true", help="8883 时开启 TLS")
    p.add_argument("--ca", default=None, help="TLS CA 证书路径")
    p.add_argument("--loop", type=int, default=1, help="连续上报次数，0=无限循环")
    p.add_argument("--interval", type=float, default=30.0, help="循环上报间隔秒数")
    args = p.parse_args()

    report_device_id = args.report_device_id or args.device_id
    topic = args.topic or f"$oc/devices/{args.device_id}/sys/messages/up"

    ts = _utc_hour_ts()
    client_id = build_client_id(args.device_id, ts)
    password = build_password(args.secret, ts)

    # paho-mqtt 2.x 需要显式声明 CallbackAPIVersion
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.username_pw_set(args.device_id, password)

    if args.tls:
        client.tls_set(ca_certs=args.ca)

    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()

    payload = build_sample_payload(report_device_id)
    body = json.dumps(payload, ensure_ascii=False)

    print(f"[connect] broker={args.broker}:{args.port} client_id={client_id}")
    print(f"[publish] topic={topic}")
    print(f"[publish] payload={body}")

    count = 0
    while True:
        info = client.publish(topic, body, qos=1)
        info.wait_for_publish(timeout=10)
        count += 1
        print(f"[sent] 第 {count} 条上报完成")
        if args.loop > 0 and count >= args.loop:
            break
        time.sleep(args.interval)

    client.loop_stop()
    client.disconnect()
    print("[done]")


if __name__ == "__main__":
    main()