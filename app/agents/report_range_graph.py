"""ReportRangeAgent：按用户指定时间区间生成睡眠报告（独立智能体）。

与 ReportAgent 的差异：
  - ReportAgent：消费 1 条 sleep_records（整晚聚合结果）
  - ReportRangeAgent：直接消费 sleep_frames 区间（任意时间窗）
    用户场景："25 号 12:00 到 26 号 12:00 这段时间的睡眠报告"

输出：
  - report        : 自然语言报告（中文）
  - coverage      : { frame_count, expected, missing_ratio, missing_segments:[{start,end}] }
  - degradation   : { mode: full|partial|insufficient, warnings:[...] }
  - summary       : 结构化 JSON 字段（duration_min, sleep_quality_score, stages, apnea_risk, hr, rr, snore）

降级策略：
  1) 数据完整（missing < 5%）     → 正常生成报告
  2) 数据部分缺失（5% - 30%）     → 报告正常生成 + 警告 + 用现有帧估整体均值
  3) 数据严重缺失（30% - 70%）    → 报告降级（标"部分指标因数据缺失未能计算"）+ 仍给估算
  4) 几乎无数据（>= 70%）         → 不给质量分；只说"该区间数据过少，无法评估"
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.llm import get_chat_model
from app.agents.state import TypedDict

logger = logging.getLogger(__name__)


# ===== State =====
class ReportRangeState(TypedDict, total=False):
    # 输入
    user_id: int
    device_id: int
    start_ts: int         # 区间起（epoch 秒，UTC）
    end_ts: int           # 区间止
    # 中间
    frames: list[dict]    # 区间内的 sleep_frames（dict 化）
    coverage: dict        # { frame_count, expected, missing_ratio, missing_segments }
    summary: dict         # 统计特征
    degradation: dict     # { mode, warnings }
    # 输出
    report: str
    error: str | None


# ===== 节点 1：拉帧 + 覆盖率分析 =====
def load_frames(state: ReportRangeState) -> dict[str, Any]:
    """从 DB 拉区间内所有 sleep_frames，统计缺失情况。

    缺失定义：窗口 30s，expected = ceil((end - start) / 30)；
    missing_segments 连续多帧没有时合成一段（便于报告里说"X 点 - Y 点无数据"）。
    """
    from app.database import AsyncSessionLocal
    from sqlalchemy import select
    from app.models import SleepFrame, Device

    device_id = state["device_id"]
    start_ts = state["start_ts"]
    end_ts = state["end_ts"]
    if end_ts <= start_ts:
        return {"error": "end_ts 必须大于 start_ts", "frames": []}

    import math
    expected = math.ceil((end_ts - start_ts) / 30)

    async def _q():
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SleepFrame)
                .where(
                    SleepFrame.device_id == device_id,
                    SleepFrame.window_start_ts >= start_ts,
                    SleepFrame.window_start_ts < end_ts,
                )
                .order_by(SleepFrame.window_start_ts.asc())
            )
            return [f.to_dict() if hasattr(f, "to_dict") else {
                "id": f.id,
                "device_id": f.device_id,
                "window_start_ts": f.window_start_ts,
                "window_end_ts": f.window_end_ts,
                "stage": f.stage,
                "heart_rate": f.heart_rate,
                "resp_rate": f.resp_rate,
                "motion": f.motion,
                "snore_seconds": f.snore_seconds,
                "flags": f.flags,
            } for f in result.scalars().all()]

    # 同步上下文跑异步（FastAPI 同步路由不能 await，但被调用方 SleepService 会 await 此函数）
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        # 已经在 event loop 里了（FastAPI 异步上下文）—— 直接 await
        # 这种情况下 _q 是 coroutine
        coroutine = _q()
    else:
        coroutine = _q()
    # 由于本节点被 graph.ainvoke 调用，coroutine 不会被同步求值，这里改为兼容写法
    # 实际：graph 节点返回 dict 中不直接放 coroutine；改为上层 await 时同步处理
    # 因此这里将 DB 查询留给上层 service 跑（见 SleepService.generate_range_report），
    # 本函数只做纯计算。
    return {}


def compute_coverage(frames: list[dict], start_ts: int, end_ts: int) -> dict:
    """纯函数：给定帧 + 区间 → 覆盖率分析。供 Service 层调用。"""
    import math
    expected = math.ceil((end_ts - start_ts) / 30)
    actual_ts = sorted({f["window_start_ts"] for f in frames})
    frame_count = len(actual_ts)
    missing_ratio = max(0.0, 1.0 - frame_count / expected) if expected else 1.0

    # 找连续缺失段（用于报告里说"X 点 - Y 点无数据"）
    missing_segments: list[dict] = []
    expected_iter = iter(range(start_ts, start_ts + expected * 30, 30))
    cur = next(expected_iter, None)
    seg_start: int | None = None
    for ts in actual_ts:
        while cur is not None and cur < ts:
            if seg_start is None:
                seg_start = cur
            cur = next(expected_iter, None)
        if seg_start is not None and cur is not None and cur == ts:
            missing_segments.append({
                "start_ts": seg_start,
                "end_ts": cur,
            })
            seg_start = None
        cur = next(expected_iter, None)
    # 尾部缺失
    if seg_start is not None:
        end_missing = start_ts + expected * 30
        missing_segments.append({"start_ts": seg_start, "end_ts": end_missing})

    return {
        "frame_count": frame_count,
        "expected": expected,
        "missing_ratio": round(missing_ratio, 3),
        "missing_segments": missing_segments[:20],  # 上限 20 段
    }


# ===== 节点 2：特征汇总 =====
def compute_summary(frames: list[dict]) -> dict:
    """纯函数：给定帧列表 → 汇总统计。"""
    hrs = [f["heart_rate"] for f in frames if isinstance(f.get("heart_rate"), int) and f["heart_rate"] > 0]
    rrs = [f["resp_rate"] for f in frames if isinstance(f.get("resp_rate"), int) and f["resp_rate"] > 0]
    snore_secs = [f["snore_seconds"] for f in frames if isinstance(f.get("snore_seconds"), int) and f["snore_seconds"] >= 0]
    motion_secs = sum(1 for f in frames if f.get("motion") == 1)
    stages = {"deep": 0, "light": 0, "rem": 0, "wake": 0, "unknown": 0}
    for f in frames:
        s = f.get("stage", -1)
        if s == 0:
            stages["wake"] += 1
        elif s == 1:
            stages["light"] += 1
        elif s == 2:
            stages["deep"] += 1
        else:
            stages["unknown"] += 1

    total = max(1, sum(stages[k] for k in ("deep", "light", "rem", "wake")))
    stage_ratio = {k: round(v / total, 3) for k, v in stages.items() if k != "unknown"}
    if stages["unknown"] > 0:
        stage_ratio["unknown"] = round(stages["unknown"] / total, 3)

    # 简单睡眠时长 = (总帧数 - wake - unknown) * 30s；缺数据按比例缩放
    valid_sleep_frames = stages["deep"] + stages["light"] + stages["rem"]
    duration_min = round(valid_sleep_frames * 30 / 60, 1)

    # 平均心率/呼吸
    avg_hr = round(sum(hrs) / len(hrs), 1) if hrs else None
    avg_rr = round(sum(rrs) / len(rrs), 1) if rrs else None
    snore_min = round(sum(snore_secs) / 60, 1) if snore_secs else 0.0

    # 呼吸暂停风险（简单规则）
    apnea_risk = "low"
    if snore_min >= 20 or (avg_rr and avg_rr >= 22) or (avg_hr and avg_hr >= 90):
        apnea_risk = "medium"
    if snore_min >= 60 or (avg_rr and avg_rr >= 28) or (avg_hr and avg_hr >= 100):
        apnea_risk = "high"

    # 睡眠评分（0-100）
    score = 90
    if avg_hr:
        if avg_hr > 85 or avg_hr < 50:
            score -= 10
    if avg_rr:
        if avg_rr > 22 or avg_rr < 10:
            score -= 15
    score -= min(30, int(snore_min * 0.5))
    score = max(0, min(100, score))

    return {
        "frame_count": len(frames),
        "avg_heart_rate": avg_hr,
        "avg_respiratory_rate": avg_rr,
        "snore_minutes": snore_min,
        "motion_seconds": motion_secs * 30,
        "stages": stage_ratio,
        "sleep_duration_min": duration_min,
        "sleep_quality_score": score,
        "apnea_risk": apnea_risk,
    }


# ===== 节点 3：降级判定 =====
def assess_degradation(coverage: dict, summary: dict) -> dict:
    """根据缺失比和 summary 决定降级等级。"""
    ratio = coverage.get("missing_ratio", 1.0)
    warnings: list[str] = []
    if ratio < 0.05:
        mode = "full"
    elif ratio < 0.30:
        mode = "partial"
        warnings.append(f"数据缺失 {int(ratio*100)}%，以下分析基于现有帧估算")
    elif ratio < 0.70:
        mode = "insufficient"
        warnings.append(f"数据严重缺失 {int(ratio*100)}%，仅能给出部分指标")
    else:
        mode = "failed"
        warnings.append(f"数据几乎全无（缺失 {int(ratio*100)}%），无法给出可靠评分")

    # 通道关闭标记（flags bit0=snore_off, bit1=stage_off, bit2=bcg_off）
    seg_list = coverage.get("missing_segments", [])
    if seg_list:
        if len(seg_list) <= 3:
            for seg in seg_list:
                warnings.append(
                    f"{_fmt_ts(seg['start_ts'])} - {_fmt_ts(seg['end_ts'])} 无数据"
                )
        else:
            warnings.append(f"共 {len(seg_list)} 段无数据，首段 {_fmt_ts(seg_list[0]['start_ts'])} - {_fmt_ts(seg_list[0]['end_ts'])}")

    return {"mode": mode, "warnings": warnings}


# ===== 节点 4：LLM 生成报告 =====
SYSTEM_PROMPT = (
    "你是一位温和、严谨的睡眠医学顾问，长期服务一位固定用户。"
    "现在需要为这位用户生成一份按时间段的睡眠报告。\n"
    "\n"
    "【输入】\n"
    "1) 用户指定的时间区间（start_ts / end_ts，epoch 秒）\n"
    "2) 该区间内设备每 30 秒上报的 sleep_frames 帧（已统计成 features）\n"
    "3) 覆盖率 coverage：实际收到多少帧 / 应当收到多少帧 / 缺失段列表\n"
    "4) 降级标记 degradation.mode（full | partial | insufficient | failed）\n"
    "\n"
    "【输出结构】必须严格按以下五段，使用【】标记：\n"
    "  【总评】1-2 句概括：本次报告的评估等级（优/良/中/差），" +
    "以及是否因数据缺失而降级。\n"
    "  【睡眠时间】从数据中算出实际睡眠时长（X 小时 Y 分）；" +
    "如无法确定写「数据不足以判定」。\n"
    "  【关键指标】心率均值/呼吸均值/鼾声分钟数/深睡/浅睡/REM/清醒 占比/翻身次数。" +
    "缺失的写「未提供」不要编。\n"
    "  【数据缺失说明】逐条列出 coverage.missing_segments，" +
    "用「HH:MM-HH:MM（X 帧未上报）」的格式；如无缺失写「全程数据完整」。\n"
    "  【降级策略与建议】基于 degradation.mode 给出：" +
    "full=正常建议；partial=对缺失段做提示，剩余数据可信；" +
    "insufficient=强调评估受限，建议补采；" +
    "failed=建议重新检查设备。\n"
    "\n"
    "【硬约束】\n"
    "  1) 严禁捏造数据：所有数字必须来自输入 JSON；缺失的写「未提供」。\n"
    "  2) 严禁提具体模型/算法/硬件名称（如 CNN、qwenplus、毫米波雷达、压电陶瓷等）；" +
    "    描述能力用「端侧推理」「端侧模型」。\n"
    "  3) 任何医学高风险情况必须建议线下就医。\n"
    "  4) 文末不写「我是 AI」「本报告由模型生成」之类元信息。\n"
    "  5) 报告正文中不要出现「端侧模型」「CNN」之外的任何模型/算法名。\n"
)


def build_prompt_node(state: ReportRangeState) -> dict[str, Any]:
    """把状态拼成 LLM 提示词。"""
    return {"messages": build_range_messages(state)}


def build_range_messages(state: dict) -> list:
    """★ 独立导出：流式接口需要直接拿 messages 去 llm.astream()，
    不必走整张图（图里的 generate_node 是同步 invoke，拿不到增量）。"""
    user_payload = (
        f"用户 ID：{state.get('user_id')}\n"
        f"设备 ID：{state.get('device_id')}\n"
        f"时间区间：{state['start_ts']} → {state['end_ts']} " +
        f"（{_fmt_ts(state['start_ts'])} 至 {_fmt_ts(state['end_ts'])}）\n\n"
        f"覆盖率：{json.dumps(state.get('coverage', {}), ensure_ascii=False)}\n\n"
        f"汇总特征：{json.dumps(state.get('summary', {}), ensure_ascii=False)}\n\n"
        f"降级标记：{json.dumps(state.get('degradation', {}), ensure_ascii=False)}\n\n"
        "请按 system 提示词的五段结构生成报告："
    )
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_payload),
    ]


def generate_node(state: ReportRangeState) -> dict[str, Any]:
    llm = get_chat_model()
    msgs = state.get("messages") or []
    try:
        resp = llm.invoke(msgs)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:  # noqa: BLE001
        return {"report": _fallback_report(state), "error": str(e)}
    return {"report": text}


def polish_node(state: ReportRangeState) -> dict[str, Any]:
    text = (state.get("report") or "").strip()
    if not text:
        text = _fallback_report(state)
    if len(text) > 4000:
        text = text[:3997] + "..."
    return {"report": text}


def _fallback_report(state: ReportRangeState) -> str:
    s = state.get("summary", {}) or {}
    cov = state.get("coverage", {}) or {}
    deg = state.get("degradation", {}) or {}
    return (
        f"【总评】本次报告为{_degrade_text(deg.get('mode'))}，"
        f"缺失比 {int(cov.get('missing_ratio', 1.0) * 100)}%。\n"
        f"【睡眠时间】{'约 ' + str(s.get('sleep_duration_min', 0)) + ' 分钟' if s.get('sleep_duration_min') else '数据不足以判定'}。\n"
        f"【关键指标】心率均值 {s.get('avg_heart_rate') or '未提供'} bpm，"
        f"呼吸均值 {s.get('avg_respiratory_rate') or '未提供'} 次/分，"
        f"鼾声 {s.get('snore_minutes') or 0} 分钟。\n"
        f"【数据缺失说明】共缺失 {len(cov.get('missing_segments', []))} 段，" +
        f"首段 {(_fmt_ts(cov.get('missing_segments', [{}])[0].get('start_ts', 0)) if cov.get('missing_segments') else '无')}。\n"
        f"【降级策略与建议】{('；'.join(deg.get('warnings', [])) or '建议参考数据更完整的时段')}。\n"
        "（说明：LLM 调用失败，已切换为兜底报告。）"
    )


def _degrade_text(mode: str) -> str:
    return {
        "full": "完整评估",
        "partial": "部分降级评估",
        "insufficient": "降级评估（数据严重缺失）",
        "failed": "未给出有效评估（数据几乎全无）",
    }.get(mode or "failed", "未给出有效评估")


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


# ===== 构建图 =====
def build_report_range_graph():
    g = StateGraph(ReportRangeState)
    g.add_node("build_prompt", build_prompt_node)
    g.add_node("generate", generate_node)
    g.add_node("polish", polish_node)
    g.add_edge(START, "build_prompt")
    g.add_edge("build_prompt", "generate")
    g.add_edge("generate", "polish")
    g.add_edge("polish", END)
    return g.compile()


report_range_graph = build_report_range_graph()
