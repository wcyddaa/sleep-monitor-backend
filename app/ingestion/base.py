"""数据接入统一落地层。

模拟器 / HTTP webhook / AMQP 推送 / 直接 MQTT 四个来源最终都汇聚到这里：
解析原始消息 -> 归一化为 SleepDataIngest -> 入库 -> 分析 -> 生成报告。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Device, SleepRecord, User
from app.schemas import SleepDataIngest
from app.services import SleepService

logger = logging.getLogger(__name__)


def _to_dict(raw: Any) -> dict:
    """把 str/bytes/dict 统一转成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    raise ValueError(f"无法识别的消息类型: {type(raw)}")


def _unwrap_envelope(data: dict) -> dict:
    """剥离华为云推送的常见外壳，取到真正的设备消息体。

    华为云规则引擎 HTTPS 转发、AMQP/MQTT 推送时，业务数据常被包在
    notify_data -> body -> content 里（设备消息上报的 body 形如
    {"topic": "$oc/devices/xxx/sys/messages/up", "content": {...}}，
    content 可能是对象也可能是 JSON 字符串）；若消息本身已是扁平设备
    上报则原样返回。
    """
    if isinstance(data.get("notify_data"), dict):
        data = data["notify_data"]
    if isinstance(data.get("body"), dict):
        data = data["body"]

    content = data.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = None
    if isinstance(content, dict):
        data = content

    return data


def normalize_device_payload(data: dict) -> SleepDataIngest:
    """把设备上报 JSON 归一化为 SleepDataIngest。

    期望的 JSON 结构（与 SleepDataIngest 字段一一对应）：

        {
          "device_id": "bedroom-01",
          "start_time": "2026-09-17T22:00:00+08:00",
          "end_time": "2026-09-18T06:00:00+08:00",
          "vital_signs": [
            {"timestamp": "...", "heart_rate": 65.0, "respiratory_rate": 15.0,
             "bed_status": "in_bed", "turnover_count": 1}
          ],
          "audio_segments": [
            {"start": "...", "end": "...", "snore_detected": true, "intensity": 0.7}
          ]
        }

    若华为云实际字段名不同（如 heartRate / bedState），在此函数里做一次字段映射即可。
    """
    return SleepDataIngest.model_validate(data)


async def process_device_ingest(
    payload: SleepDataIngest, with_analysis: bool = True
) -> SleepRecord:
    """根据 device_id 解析设备与用户，然后落地数据。

    with_analysis=True：继续执行 analyze -> generate_report（内置模拟器使用，需 LLM，耗时 30s+）。
    with_analysis=False：仅落地后立即返回（HTTP webhook 使用，云平台转发有超时限制）。
    """
    async with AsyncSessionLocal() as db:
        device = (
            await db.execute(select(Device).where(Device.device_id == payload.device_id))
        ).scalar_one_or_none()
        if device is None:
            # 设备未注册（A）：华为云转发而来，按 device_id 自动建设备（待归属）
            from app.services import DeviceService
            device, _ = await DeviceService.get_or_create_webhook(db, payload.device_id)

        owner = None
        if device.owner_id is not None:
            owner = (
                await db.execute(select(User).where(User.id == device.owner_id))
            ).scalar_one_or_none()

        # 有数据上报即视为设备在线，更新心跳时间
        from datetime import datetime, timezone
        device.is_online = True
        device.last_seen = datetime.now(tz=timezone.utc)

        record = await SleepService.ingest(db, owner, payload)
        if with_analysis and owner is not None:
            record = await SleepService.analyze(db, record.id, owner)
            record = await SleepService.generate_report(db, record.id, owner)

        logger.info(
            "接入睡眠记录 #%s device=%s owner=%s score=%s risk=%s",
            record.id,
            payload.device_id,
            device.owner_id,
            record.sleep_quality_score,
            record.apnea_risk,
        )
        return record


async def ingest_raw_json(raw: Any, with_analysis: bool = True) -> SleepRecord | None:
    """网络来源统一入口：解析 -> 归一化 -> 落地，异常在此兜底记录。"""
    try:
        data = _unwrap_envelope(_to_dict(raw))
        payload = normalize_device_payload(data)
        return await process_device_ingest(payload, with_analysis=with_analysis)
    except Exception as e:  # noqa: BLE001
        logger.exception("数据接入处理失败: %s", e)
        return None