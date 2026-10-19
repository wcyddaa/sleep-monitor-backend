"""直接 MQTT 订阅消费者（paho-mqtt）。

后端作为 MQTT 客户端订阅指定 topic（MQTT_BROKER/MQTT_PORT/MQTT_TOPIC），
收到消息 -> 转交 asyncio 落地。依赖：pip install paho-mqtt
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class MqttIngestor:
    """常驻 MQTT 订阅者。"""

    def __init__(self) -> None:
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if not settings.mqtt_broker:
            logger.error("INGEST_MODE=mqtt 但未配置 MQTT_BROKER，跳过启动")
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:  # noqa: BLE001
            logger.error("缺少 paho-mqtt，请 `pip install paho-mqtt`: %s", e)
            return

        self._loop = asyncio.get_running_loop()
        client = mqtt.Client()
        if settings.mqtt_username:
            client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

        def on_connect(client, userdata, flags, rc) -> None:
            if rc == 0:
                logger.info("MQTT 已连接，订阅 %s", settings.mqtt_topic)
                client.subscribe(settings.mqtt_topic)
            else:
                logger.error("MQTT 连接失败 rc=%s", rc)

        def on_message(client, userdata, msg) -> None:
            from app.ingestion.base import ingest_raw_json

            try:
                body = json.loads(msg.payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.warning("MQTT 消息非 JSON（%s），忽略", msg.topic)
                return
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(ingest_raw_json(body), self._loop)

        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(settings.mqtt_broker, settings.mqtt_port, keepalive=60)
        client.loop_start()
        self._client = client
        logger.info(
            "MQTT 消费者已启动 broker=%s:%s topic=%s",
            settings.mqtt_broker,
            settings.mqtt_port,
            settings.mqtt_topic,
        )

    async def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()