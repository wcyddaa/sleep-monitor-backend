"""单帧 30s 结构化数据的 AI 分析。

与 report_graph 的区别：
- report_graph：针对一条「睡眠记录」（整晚 raw_data + analysis），生成五段式报告并存库
- 本模块：只针对**一帧 30 秒**的端侧结构化数据（stage/hr/rr/motion/snore_sec），
  调 LLM 生成一段简短分析，**不落库、不产报告章节**

输入是固件 EdgeAggregator 每 30s 上行的一帧（后端 sleep_frames 表一行）。
三开关 OFF 的通道，字段为 -1，提示词里按「未启用」呈现，禁止编造。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm import get_chat_model

logger = logging.getLogger(__name__)

FRAME_SYSTEM_PROMPT = (
    "你是一位睡眠健康助手。用户设备每 30 秒上传一帧端侧模型推理结果，"
    "现在需要你针对**这一帧（30 秒窗口）**给出简短、专业的解读。用中文。\n"
    "\n"
    "【结构】严格按以下三段输出，用【】标记：\n"
    "  【本窗小结】1-2 句概括这 30 秒的睡眠状态（分期 + 有无体动 + 有无鼾声）。\n"
    "  【指标解读】逐一解读已启用的指标（睡眠分期、心率、呼吸频率、体动、鼾声秒数），"
    "每条给出「数值 + 是否在常见范围」；字段为 -1 表示该通道未启用，写「未启用」不要编造。\n"
    "  【提示】0-2 条针对本窗的轻量提示；无异常就写「本窗无明显异常」。\n"
    "\n"
    "【硬约束】\n"
    "  1) 数据只有 30 秒，不要推导整晚结论、不要给长期诊断。\n"
    "  2) 严禁捏造：所有数字必须来自给定数据；-1 一律写「未启用」。\n"
    "  3) 严禁下医学诊断，用「可能」「倾向」「建议关注」。\n"
    "  4) 不要提及任何具体模型/算法/硬件名称（如 CNN、GPT、毫米波雷达、BCG 等）。\n"
    "  5) 不要写「我是 AI」「本报告由模型生成」之类元信息。\n"
)

# 三开关位（与固件 edge_aggregator 的 flags 定义一致）
FLAG_SNORE_OFF = 0x01
FLAG_STAGE_OFF = 0x02
FLAG_BCG_OFF = 0x04

_STAGE_LABEL = {0: "清醒", 1: "浅睡", 2: "深睡"}


def _stage_text(v: int) -> str:
    if v is None or v < 0:
        return "未启用"
    return _STAGE_LABEL.get(v, f"未知({v})")


def _num_text(v: int, unit: str) -> str:
    if v is None or v < 0:
        return "未启用"
    return f"{v}{unit}"


def build_frame_payload(frame: Any) -> dict[str, Any]:
    """把 ORM SleepFrame 转成给 LLM 的紧凑结构化 payload。"""
    flags = int(getattr(frame, "flags", 0) or 0)
    return {
        "窗口开始": int(getattr(frame, "window_start_ts", 0) or 0),
        "窗口结束": int(getattr(frame, "window_end_ts", 0) or 0),
        "睡眠分期": _stage_text(int(getattr(frame, "stage", -1))),
        "心率": _num_text(int(getattr(frame, "heart_rate", -1)), " bpm"),
        "呼吸频率": _num_text(int(getattr(frame, "resp_rate", -1)), " 次/分"),
        "体动": (
            "未启用"
            if int(getattr(frame, "motion", -1)) < 0
            else ("有体动" if int(getattr(frame, "motion", 0)) == 1 else "静止")
        ),
        "鼾声秒数": _num_text(int(getattr(frame, "snore_seconds", -1)), " 秒/30秒"),
        "通道启用": {
            "鼾声检测": not (flags & FLAG_SNORE_OFF),
            "睡眠分期": not (flags & FLAG_STAGE_OFF),
            "心率呼吸体动": not (flags & FLAG_BCG_OFF),
        },
    }


async def analyze_frame(frame: Any) -> str:
    """对这一帧调 LLM，返回分析文本。失败时抛异常由上层兜底。"""
    payload = build_frame_payload(frame)
    llm = get_chat_model()
    msgs = [
        SystemMessage(content=FRAME_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "本帧（30 秒窗口）端侧结构化数据：\n"
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
                "请按 system 提示词的三段结构给出本窗分析："
            )
        ),
    ]
    resp = await llm.ainvoke(msgs)
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return text.strip()


def fallback_frame_analysis(frame: Any) -> str:
    """LLM 不可用时的规则兜底，保证前端不空白。"""
    payload = build_frame_payload(frame)
    return (
        f"【本窗小结】本 30 秒窗口：睡眠分期 {payload['睡眠分期']}，"
        f"体动 {payload['体动']}。\n"
        f"【指标解读】心率 {payload['心率']}，呼吸频率 {payload['呼吸频率']}，"
        f"鼾声 {payload['鼾声秒数']}。\n"
        "【提示】（AI 服务暂不可用，以上为原始结构化数据回显。）"
    )
