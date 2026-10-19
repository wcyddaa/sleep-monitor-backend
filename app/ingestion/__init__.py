"""数据接入层。

四种来源（INGEST_MODE）：
- simulator 内置模拟器，直接生成测试 JSON
- http      华为云规则引擎 HTTPS 转发（webhook 被动接收，无需后台任务）
- amqp      华为云 IoTDA AMQP 队列推送（常驻消费者）
- mqtt      直接订阅 MQTT broker（常驻订阅者）

都在 main.py 的 lifespan 里按 INGEST_MODE 启动对应接入器。
"""
from __future__ import annotations

from app.config import settings
from app.ingestion.base import (
    ingest_raw_json,
    normalize_device_payload,
    process_device_ingest,
)
from app.ingestion.simulator import SimulatorIngestor


def create_ingestor():
    """根据 settings.ingest_mode 返回需要常驻运行的后台接入器；http 模式返回 None。"""
    mode = settings.ingest_mode.strip().lower()
    if mode == "simulator":
        return SimulatorIngestor()
    if mode == "amqp":
        from app.ingestion.amqp import AmqpIngestor

        return AmqpIngestor()
    if mode == "mqtt":
        from app.ingestion.mqtt import MqttIngestor

        return MqttIngestor()
    if mode == "http":
        return None
    raise ValueError(
        f"未知 INGEST_MODE: {settings.ingest_mode}（可选 simulator|http|amqp|mqtt）"
    )


__all__ = [
    "create_ingestor",
    "ingest_raw_json",
    "normalize_device_payload",
    "process_device_ingest",
    "SimulatorIngestor",
]