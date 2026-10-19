"""异步 SQLAlchemy 引擎、会话工厂与启动期幂等迁移。

设计：
- Base.metadata.create_all() 只创建不存在的表，**不会**给已存在的表加列。
- 启动期通过 INFORMATION_SCHEMA 判定列/表是否存在，再 ALTER / CREATE。
- 全程用 `IF NOT EXISTS` 等幂等 SQL，重复启动安全。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
    future=True,
    pool_pre_ping=True,   # 取连接前先 ping，失效连接自动重连，避免 MySQL wait_timeout 断连报 2013
    pool_recycle=3600,    # 连接最长复用 1 小时，低于 MySQL 默认 wait_timeout(8h)
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ----------------------------------------------------------------------
# 启动期幂等迁移：给已存在的表补列 + 建新表
# 每加一张表/一列，在这里补一行；不要让运维再去手动跑 SQL。
# SQL 都要是 IF NOT EXISTS 语义，重复跑不报错。
# ----------------------------------------------------------------------
_MIGRATIONS: list[str] = [
    # 注：devices.cfg_json 已废弃（2026-10-08）。
    #     三开关是设备本地状态，走 BLE 读写、不经云端，故不再存 cfg。
    #     旧库里的该列由 deploy/migrations/drop_devices_cfg_json.py 一次性删除
    #     （DROP COLUMN 没有 IF EXISTS 语义，不适合放进每次启动都跑的迁移列表）。

    # 1) sleep_frames（v2.3：30s 结构化数据帧）
    """CREATE TABLE IF NOT EXISTS sleep_frames (
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
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
]


async def _run_migrations() -> None:
    """按顺序跑 _MIGRATIONS 列表里的 SQL，逐条执行；任何一条失败仅 warn 不阻塞。"""
    for i, sql in enumerate(_MIGRATIONS, 1):
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
            logger.info("migration #%d OK: %s...", i, sql[:60].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            logger.warning("migration #%d failed: %s", i, e)


async def init_db() -> None:
    """启动期：建表 + 跑幂等迁移。"""
    from app import models  # noqa: F401  触发 ORM 注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _run_migrations()
    logger.info("init_db: create_all + %d migrations done", len(_MIGRATIONS))


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每次请求一个 Session。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
