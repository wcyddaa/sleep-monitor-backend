"""ChatAgent：基于用户睡眠数据的对话式问答。

图结构：
  START
    └─► retrieve     (从 MySQL 拉相关历史报告，写入 state.rag_context)
        └─► build_context   (拼装 system + RAG 上下文 + 历史 + 本轮消息)
            └─► answer      (LLM 答复)
                └─► END
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.llm import get_chat_model
from app.agents.state import ChatAgentState
from app.database import AsyncSessionLocal
from app.rag import retrieve_by_keyword

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是用户的私人睡眠顾问，长期服务一位固定用户。"
    "请基于下方「历史相关报告」+ 用户最近睡眠数据 + 历史对话，给出专业、克制、可执行的回答。\n"
    "\n"
    "【硬约束】\n"
    "  1) 严禁捏造数据：所有数字/事实必须来自 RAG 上下文或 sleep_context；\n"
    "     引用历史报告时给「日期 + 评分」便于用户核对。\n"
    "  2) 严禁下医学诊断：能用「可能」「倾向」「建议关注」就别用「确诊」「是病」。\n"
    "  3) 高风险（apnea_risk=high / 鼾声超 60 分钟 / 心率持续 >100）必须建议线下就医。\n"
    "  4) 回答用中文，简明扼要（<=200 字优先），不要写「我是 AI」之类元信息。\n"
)


async def retrieve(state: ChatAgentState) -> dict[str, Any]:
    """从 MySQL 按关键词在近 90 天报告里检索 Top-5，写入 state.rag_context。"""
    user_id = state.get("user_id")
    message = (state.get("message") or "").strip()
    if not user_id or not message:
        return {"rag_context": ""}
    try:
        async with AsyncSessionLocal() as db:
            rag = await retrieve_by_keyword(db, int(user_id), message, days=90, limit=5)
    except Exception as e:  # noqa: BLE001
        logger.warning("ChatAgent RAG 检索失败: %s", e)
        return {"rag_context": ""}
    return {"rag_context": rag.get("context", "")}


def build_context(state: ChatAgentState) -> dict[str, Any]:
    history = state.get("history") or []
    sleep_ctx = state.get("sleep_context")
    rag_ctx = state.get("rag_context") or ""

    msgs: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]

    if rag_ctx:
        msgs.append(SystemMessage(content=rag_ctx))

    if sleep_ctx:
        ctx_text = (
            "用户最近睡眠数据：\n"
            f"{json.dumps(sleep_ctx, ensure_ascii=False, indent=2)}"
        )
        msgs.append(SystemMessage(content=ctx_text))

    for m in history[-10:]:  # 限制历史条数
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
        elif role == "system":
            msgs.append(SystemMessage(content=content))

    msgs.append(HumanMessage(content=state.get("message", "")))
    return {"messages": msgs, "context_summary": (rag_ctx or "") + ("\n" + ctx_text if sleep_ctx else "")}


def answer(state: ChatAgentState) -> dict[str, Any]:
    llm = get_chat_model()
    msgs = state.get("messages") or []
    try:
        resp = llm.invoke(msgs)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:  # noqa: BLE001
        return {"reply": f"抱歉，回答生成失败：{e}", "citations": []}
    # citations 暴露给前端：含历史报告 → 把 RAG 命中的 record id 列表带上
    citations: list[str] = []
    if state.get("rag_context"):
        citations.append("historical_reports")
    if state.get("sleep_context"):
        citations.append("sleep_record")
    return {"reply": text, "citations": citations}


def build_chat_graph():
    g = StateGraph(ChatAgentState)
    g.add_node("retrieve", retrieve)
    g.add_node("build_context", build_context)
    g.add_node("answer", answer)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "build_context")
    g.add_edge("build_context", "answer")
    g.add_edge("answer", END)
    return g.compile()


chat_graph = build_chat_graph()
