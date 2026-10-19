"""FastAPI 应用入口。

启动方式：
    uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

或：
    uv run python main.py
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.config import settings
from app.database import init_db
from app.ingestion import create_ingestor
from app.ingestion.webhook import router as ingest_webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


_ingestor = create_ingestor()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动
    await init_db()
    logger.info("数据库已初始化")
    if _ingestor is not None:
        await _ingestor.start()
        logger.info("数据接入器已启动（INGEST_MODE=%s）", settings.ingest_mode)
    yield
    # 关闭
    if _ingestor is not None:
        await _ingestor.stop()


app = FastAPI(
    title="智能睡眠监测系统",
    description=(
        "多模态睡眠监测后端：接收设备（毫米波雷达 + 音频）上报的睡眠数据，"
        "经 LangGraph 完成数据分析、报告生成与 AI 对话问答。"
    ),
    version="0.1.0",
    debug=settings.app_debug,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "认证", "description": "用户注册、登录、当前用户信息。"},
        {"name": "设备", "description": "睡眠监测设备注册与查询。"},
        {"name": "睡眠", "description": "睡眠数据接入、分析、查询。"},
        {"name": "报告", "description": "基于分析结果生成睡眠报告。"},
        {"name": "对话", "description": "AI 对话问答（结合睡眠上下文）。"},
        {"name": "ingest", "description": "设备/云平台数据接入 webhook（免登录）。"},
        {"name": "meta", "description": "健康检查与服务信息。"},
    ],
)

# CORS（开发期全开，生产请按需收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(ingest_webhook_router)


@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "llm": "openai" if settings.use_real_llm else "mock",
    }


@app.get("/", tags=["meta"], summary="服务信息")
async def root() -> dict:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )
