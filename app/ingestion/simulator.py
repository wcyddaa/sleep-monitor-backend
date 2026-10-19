"""内置模拟器：直接生成测试数据并走统一落地链路，不经过任何网络层。

INGEST_MODE=simulator 时由 main.py 启动。生成数据的各项参数均可在 .env 配置。
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Device, User
from app.schemas import SleepDataIngest

logger = logging.getLogger(__name__)


class SimulatorIngestor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ingest-simulator")
        logger.info("模拟器已启动，间隔 %ss", settings.simulator_interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("模拟器已停止")

    async def _run(self) -> None:
        from app.ingestion.base import process_device_ingest

        await asyncio.sleep(5)  # 给 DB 初始化留时间
        while not self._stop.is_set():
            try:
                async with AsyncSessionLocal() as db:
                    device_id = (
                        await db.execute(
                            select(Device.device_id)
                            .join(User, User.id == Device.owner_id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if device_id is not None:
                    payload = generate_mock_payload(device_id)
                    await process_device_ingest(payload)
            except Exception as e:  # noqa: BLE001
                logger.exception("模拟器 tick 失败: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.simulator_interval_seconds
                )
            except asyncio.TimeoutError:
                continue


def generate_mock_payload(device_id: str) -> SleepDataIngest:
    """生成一段睡眠测试数据。

    结构：start_time/end_time = 睡眠时段；vital_signs = 心率/呼吸/在床/翻身；
    audio_segments = 鼾声片段（时间 + 强度）。
    """
    s = settings
    now = datetime.now(tz=timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(
        hours=random.randint(s.simulator_sleep_hours_min, s.simulator_sleep_hours_max)
    )

    samples: list[dict[str, Any]] = []
    cur = start
    while cur < end:
        samples.append(
            {
                "timestamp": cur,
                "heart_rate": round(
                    random.gauss(s.simulator_heart_rate_mean, s.simulator_heart_rate_std), 1
                ),
                "respiratory_rate": round(
                    random.gauss(s.simulator_resp_rate_mean, s.simulator_resp_rate_std), 1
                ),
                "bed_status": "in_bed" if random.random() > 0.02 else "out_of_bed",
                "turnover_count": random.choice([0, 0, 0, 1, 1, 2]),
            }
        )
        cur += timedelta(minutes=s.simulator_sample_interval_minutes)

    total_minutes = int((end - start).total_seconds() // 60)
    audios: list[dict[str, Any]] = []
    for _ in range(random.randint(0, s.simulator_snore_segments_max)):
        seg_start = start + timedelta(
            minutes=random.randint(30, max(30, total_minutes - 30))
        )
        seg_end = min(seg_start + timedelta(seconds=random.randint(10, 90)), end)
        audios.append(
            {
                "start": seg_start,
                "end": seg_end,
                "snore_detected": True,
                "intensity": round(random.uniform(0.3, 0.85), 2),
            }
        )

    return SleepDataIngest(
        device_id=device_id,
        start_time=start,
        end_time=end,
        vital_signs=samples,
        audio_segments=audios,
    )