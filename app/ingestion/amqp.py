"""华为云 IoTDA AMQP 推送消费者（AMQP 1.0，apache qpid-proton）。

接入前在华为云 IoTDA 控制台：
1. 数据转发 -> AMQP 队列：创建队列
2. 数据转发 -> 规则：把设备消息路由到该队列
3. 队列详情 -> 接入信息：拿到 host / port / username / password

依赖：pip install python-qpid-proton
"""
from __future__ import annotations

import asyncio
import logging
import threading

from app.config import settings

logger = logging.getLogger(__name__)


class AmqpIngestor:
    """常驻 AMQP 消费者：收到华为云推送 -> 转交 asyncio 落地。"""

    def __init__(self) -> None:
        self._container = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if not settings.amqp_host:
            logger.error("INGEST_MODE=amqp 但未配置 AMQP_HOST，跳过启动")
            return
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run_blocking, daemon=True, name="amqp-consumer"
        )
        self._thread.start()
        logger.info(
            "AMQP 消费者已启动 host=%s:%s queue=%s",
            settings.amqp_host,
            settings.amqp_port,
            settings.amqp_queue,
        )

    async def stop(self) -> None:
        if self._container is not None:
            self._container.stop()

    @staticmethod
    def _broker_url() -> str:
        scheme = "amqps" if settings.amqp_port == 5671 else "amqp"
        return f"{scheme}://{settings.amqp_host}:{settings.amqp_port}"

    def _run_blocking(self) -> None:
        try:
            from proton.handlers import MessagingHandler
            from proton.reactor import Container
        except ImportError as e:  # noqa: BLE001
            logger.error("缺少 qpid-proton，请 `pip install python-qpid-proton`: %s", e)
            return

        loop = self._loop

        class _Handler(MessagingHandler):
            def __init__(self, queue: str) -> None:
                super().__init__()
                self.queue = queue

            def on_start(self, event) -> None:
                conn = event.container.connect(
                    AmqpIngestor._broker_url(),
                    user=settings.amqp_username,
                    password=settings.amqp_password,
                )
                event.container.create_receiver(conn, self.queue)

            def on_message(self, event) -> None:
                from app.ingestion.base import ingest_raw_json

                body = event.message.body
                if loop is not None:
                    asyncio.run_coroutine_threadsafe(ingest_raw_json(body), loop)

        self._container = Container(_Handler(settings.amqp_queue))
        self._container.run()