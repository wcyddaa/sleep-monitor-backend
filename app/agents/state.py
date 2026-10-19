"""LangGraph 各 Agent 的状态类型定义。"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


# ================== DataAgent ==================
class DataAgentState(TypedDict, total=False):
    """数据分析 Agent 的状态。

    流：raw_data → 规则特征抽取 → LLM 摘要 → 风险评估 → END
    """
    sleep_record_id: int
    raw_data: dict[str, Any]            # 设备原始数据
    features: dict[str, Any]            # 规则引擎计算的特征
    llm_summary: str                    # LLM 输出的摘要（尝试解析为 JSON）
    analysis: dict[str, Any]            # 最终结构化分析结果
    error: str | None


# ================== ReportAgent ==================
class ReportAgentState(TypedDict, total=False):
    """报告生成 Agent 的状态。

    流：analysis → 加载用户偏好 → 提示词组装 → LLM 生成 → 后处理 → END
    """
    sleep_record_id: int
    user_id: int
    style: str                          # detailed | summary | pediatric
    analysis: dict[str, Any]
    user_profile: dict[str, Any] | None
    messages: list[Any]                 # build_prompt 输出的提示词
    report: str
    error: str | None


# ================== ChatAgent ==================
class ChatAgentState(TypedDict, total=False):
    """对话 Agent 的状态。

    流：用户消息 + 历史 + (可选)睡眠上下文 → 上下文检索 → 提示词组装 → LLM 回复 → END
    """
    user_id: int
    message: str
    history: list[dict[str, Any]]       # [{role, content}]
    sleep_context: dict[str, Any] | None
    messages: list[Any]                 # build_context 输出的提示词
    context_summary: str
    reply: str
    citations: list[str]
