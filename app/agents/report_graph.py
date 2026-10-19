"""ReportAgent：根据分析结果生成自然语言报告。

图结构：
  START
    └─► retrieve     (从 MySQL 拉近 N 天同用户相似案例，写入 state.rag_context)
        └─► build_prompt   (拼装 system + user 提示词，含 RAG 上下文)
            └─► generate   (真实 LLM 生成)
                └─► polish  (后处理：截断/补签)
                    └─► END
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.llm import get_chat_model
from app.agents.state import ReportAgentState
from app.database import AsyncSessionLocal
from app.rag import retrieve_similar_reports

logger = logging.getLogger(__name__)

# ★ v2 提示词：明确结构 + 引用 RAG 上下文 + 严控编造
SYSTEM_PROMPT_TEMPLATE = (
    "你是一位温和、严谨的睡眠医学顾问，长期服务一位固定用户。"
    "现在需要为这位用户生成一份{style_label}，报告必须用中文。\n"
    "\n"
    "【结构】必须严格按以下五段输出，使用【】标记：\n"
    "  【总评】1-2 句概括本晚睡眠质量评分（0-100）与所在区间（差/一般/良好/优秀）。\n"
    "  【关键指标】列出本晚核心数值：心率均值/最低、呼吸均值、鼾声分钟数、"
    "深睡/浅睡/REM 占比、翻身次数。每条带单位；缺失的写「未提供」不要编。\n"
    "  【对比历史】基于下方「历史相似案例」段，给出与近 30 天同区间的对比（更优/相当/变差）。\n"
    "  【异常事件分析】只分析 analysis 中真实存在的字段，没有就写「无明显异常」。\n"
    "  【个性化建议】3-5 条可执行建议；任何医学高风险情况必须建议线下就医。\n"
    "\n"
    "【硬约束】\n"
    "  1) 严禁捏造数据：所有数字必须来自 analysis 或 RAG 上下文；不存在的写「未提供」。\n"
    "  2) 严禁下医学诊断：能用「可能」「倾向」「建议关注」就别用「确诊」「是病」。\n"
    "  3) 如本晚 apnea_risk=high，必须在文末加一句「建议尽快线下就医做睡眠呼吸监测」。\n"
    "  4) 文末不写「我是 AI」「本报告由模型生成」之类元信息。\n"
    "  5) 严禁在报告正文中提及任何具体模型/算法/硬件名称（如 CNN、qwenplus、"
    "GPT、毫米波雷达、深度学习、压电陶瓷、BCG 等）。描述能力时用「端侧推理」「端侧模型」即可。\n"
)

STYLE_LABELS = {
    "detailed": "详尽版报告（800-1200 字）",
    "summary": "简明版报告（200-400 字）",
    "pediatric": "家长版报告（面向未成年人监护人，语气更温和）",
}


async def retrieve(state: ReportAgentState) -> dict[str, Any]:
    """从 MySQL 拉近 30 天同用户相似案例，注入 state.rag_context。"""
    user_id = state.get("user_id")
    analysis = state.get("analysis") or {}
    score = analysis.get("sleep_quality_score")
    feats = analysis.get("feats", {}) or {}
    snore_min = feats.get("snore_minutes")

    # 得分区间：按当前评分 ±0.1
    score_band = None
    if isinstance(score, (int, float)):
        s = float(score)
        score_band = (max(0.0, s - 0.1), min(1.0, s + 0.1))

    try:
        async with AsyncSessionLocal() as db:
            rag = await retrieve_similar_reports(
                db,
                int(user_id) if user_id else 0,
                exclude_id=int(state.get("sleep_record_id") or 0) or None,
                days=30,
                limit=5,
                score_band=score_band,
                snore_min_threshold=snore_min,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("RAG 检索失败，继续无 RAG 报告: %s", e)
        return {"rag_context": ""}
    return {"rag_context": rag.get("context", "")}


def build_prompt(state: ReportAgentState) -> dict[str, Any]:
    style = state.get("style", "detailed")
    style_label = STYLE_LABELS.get(style, STYLE_LABELS["detailed"])
    system = SYSTEM_PROMPT_TEMPLATE.format(style_label=style_label)

    user_profile = state.get("user_profile") or {}
    profile_line = (
        f"用户信息：姓名={user_profile.get('full_name') or '-'}；"
        f"用户名={user_profile.get('username') or '-'}"
    )
    rag_context = state.get("rag_context") or ""
    analysis = state.get("analysis") or {}

    user_payload = (
        f"{profile_line}\n\n"
        f"{rag_context}\n\n"
        f"本晚睡眠分析数据（JSON）：\n{_fmt(analysis)}\n\n"
        "请按 system 提示词的五段结构生成报告："
    )
    return {"messages": [SystemMessage(content=system), HumanMessage(content=user_payload)]}


def generate(state: ReportAgentState) -> dict[str, Any]:
    llm = get_chat_model()
    msgs = state.get("messages") or []
    try:
        resp = llm.invoke(msgs)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:  # noqa: BLE001
        return {"report": _fallback_report(state.get("analysis") or {}), "error": str(e)}
    return {"report": text}


def polish(state: ReportAgentState) -> dict[str, Any]:
    text = (state.get("report") or "").strip()
    if not text:
        text = _fallback_report(state.get("analysis") or {})
    if len(text) > 4000:
        text = text[:3997] + "..."
    return {"report": text}


def _fmt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _fallback_report(analysis: dict[str, Any]) -> str:
    feats = analysis.get("feats", {}) or {}
    score = analysis.get("sleep_quality_score", "N/A")
    apnea = analysis.get("apnea_risk", "N/A")
    return (
        "【总评】您的整体睡眠质量评分为 "
        f"{score}，呼吸暂停风险为 {apnea}。\n"
        "【关键指标】平均心率 "
        f"{feats.get('avg_heart_rate', 'N/A')} bpm，平均呼吸频率 "
        f"{feats.get('avg_respiratory_rate', 'N/A')} 次/分。\n"
        "【对比历史】未提供（LLM 调用失败，跳过 RAG 对比）。\n"
        "【异常事件分析】"
        f"检测到 {feats.get('snore_events_count', 0)} 次鼾声事件。\n"
        "【个性化建议】保持规律作息，睡前避免咖啡因；如鼾声持续或自评白天嗜睡，建议线下就医。\n"
        "（说明：LLM 调用失败，已切换为兜底报告。）"
    )


def build_report_graph():
    g = StateGraph(ReportAgentState)
    g.add_node("retrieve", retrieve)
    g.add_node("build_prompt", build_prompt)
    g.add_node("generate", generate)
    g.add_node("polish", polish)

    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "build_prompt")
    g.add_edge("build_prompt", "generate")
    g.add_edge("generate", "polish")
    g.add_edge("polish", END)
    return g.compile()


report_graph = build_report_graph()
