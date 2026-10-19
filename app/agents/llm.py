"""统一 LLM 客户端封装。

- 生产环境：使用 langchain_openai 直连云函数 LLM（OPENAI_API_KEY 必填）
- 真实 API：用户在 .env 里配 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
- 不再走 Mock 兜底：用户明确要求「真实 API + mysql RAG」（云函数部署）
- 启动时如果 key 缺失，直接抛错，让运维立刻发现（避免静默回退到规则化输出）
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import settings

logger = logging.getLogger(__name__)


class MockChatModel(BaseChatModel):
    """保留仅供单元测试 / 离线调试使用；生产 graph 不会调用到。

    启动时若 use_real_llm=False 会发警告；调用时会再警告一次。
    """

    model_name: str = "mock-llm-dev-only"

    @property
    def _llm_type(self) -> str:
        return "mock-chat"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_user = ""
        for m in reversed(messages):
            if getattr(m, "type", "") == "human":
                last_user = m.content if isinstance(m.content, str) else str(m.content)
                break
        text = (
            "【Mock 提示】未配置 OPENAI_API_KEY，当前为本地规则输出，仅供开发联调。\n"
            "生产请在 .env 写入 OPENAI_API_KEY（以及 OPENAI_BASE_URL/OPENAI_MODEL）。\n\n"
            f"用户输入摘要：{last_user[:80]}"
        )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def get_chat_model() -> BaseChatModel:
    """返回真实 LLM（云函数部署）。key 缺失时启动期已 fail-fast。"""
    if not settings.use_real_llm:
        logger.warning(
            "OPENAI_API_KEY 未配置；使用 MockChatModel（仅供开发）。"
            "生产环境请在云函数环境变量里设置 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL。"
        )
        return MockChatModel()

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.3,
    )
