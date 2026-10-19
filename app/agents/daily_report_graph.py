"""DailyReportAgent：把"按区间报告智能体"的输出简化成一个结构化 JSON，给趋势用。

设计要点：
  - 24h 区间（锚点：每天 12:00 切分）
  - 不调 LLM（on-demand 路径上要快）：用纯统计 + 模板生成
  - 输出：
      {
        "duration_min", "sleep_quality_score", "deep_pct", "light_pct", "rem_pct", "wake_pct",
        "avg_heart_rate", "avg_respiratory_rate", "snore_minutes",
        "apnea_risk", "data_completeness", "summary_text"
      }
  - summary_text：一句中文，描述这一天的睡眠（给前端卡片展示）

不调 LLM 的理由：
  1) on-demand 触发：用户打开 dashboard 时要算 7 天，串行 LLM 调用 1-3 秒/天体验差
  2) 摘要字段是结构化数字 + 简单中文模板足够（用户明确说"简化"）
  3) 报告智能体（report_range_graph）仍按用户指定区间调 LLM，给自然语言深度报告
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_daily_summary(frames: list[dict], day_start_ts: int, day_end_ts: int) -> dict[str, Any]:
    """给 24h 区间帧 → 简化结构化摘要（纯计算，不调 LLM）。

    与 compute_summary (report_range_graph) 字段对齐，但加了 deep/light/rem/wake 占比。
    """
    agg = _aggregate_frames(frames)
    return compute_daily_summary_from_agg(agg, day_start_ts, day_end_ts)


def _aggregate_frames(frames: list[dict]) -> dict[str, Any]:
    """把逐帧列表压成聚合计数（与 SQL GROUP BY 的输出结构保持一致）。"""
    actual = [f for f in frames if f.get("window_start_ts") is not None]
    hr_sum = 0
    hr_cnt = 0
    rr_sum = 0
    rr_cnt = 0
    snore_sum = 0
    deep = light = wake = 0
    for f in actual:
        hr = f.get("heart_rate")
        if isinstance(hr, int) and hr > 0:
            hr_sum += hr
            hr_cnt += 1
        rr = f.get("resp_rate")
        if isinstance(rr, int) and rr > 0:
            rr_sum += rr
            rr_cnt += 1
        sn = f.get("snore_seconds")
        if isinstance(sn, int) and sn >= 0:
            snore_sum += sn
        s = f.get("stage", -1)
        if s == 0:
            wake += 1
        elif s == 1:
            light += 1
        elif s == 2:
            deep += 1
    return {
        "frame_count": len(actual),
        "deep": deep,
        "light": light,
        "wake": wake,
        "hr_sum": hr_sum,
        "hr_cnt": hr_cnt,
        "rr_sum": rr_sum,
        "rr_cnt": rr_cnt,
        "snore_sum": snore_sum,
    }


def compute_daily_summary_from_agg(
    agg: dict[str, Any], day_start_ts: int, day_end_ts: int
) -> dict[str, Any]:
    """★ 从 SQL 聚合结果直接算摘要（不逐帧遍历）。

    TiDB 在东京、FC 在上海，把 2 万帧拉进 Python 太慢；在库侧 GROUP BY
    算完只回 7 行，网络传输量降三四个数量级。
    agg 结构见 _aggregate_frames()。
    """
    import math

    expected = math.ceil((day_end_ts - day_start_ts) / 30)
    frame_count = int(agg.get("frame_count") or 0)
    data_completeness = max(0.0, 1.0 - frame_count / expected) if expected else 1.0

    deep = int(agg.get("deep") or 0)
    light = int(agg.get("light") or 0)
    wake = int(agg.get("wake") or 0)
    rem = 0  # 训练模型没出 REM，保持与原逻辑一致

    total = max(1, deep + light + rem + wake)
    deep_pct = round(deep / total, 3)
    light_pct = round(light / total, 3)
    rem_pct = 0.0
    wake_pct = round(wake / total, 3)

    # 睡眠时长 ≈ (deep+light+rem) * 30s
    valid_sleep_frames = deep + light + rem
    duration_min = round(valid_sleep_frames * 30 / 60, 1)

    hr_cnt = int(agg.get("hr_cnt") or 0)
    rr_cnt = int(agg.get("rr_cnt") or 0)
    avg_hr = round(float(agg.get("hr_sum") or 0) / hr_cnt, 1) if hr_cnt else None
    avg_rr = round(float(agg.get("rr_sum") or 0) / rr_cnt, 1) if rr_cnt else None
    snore_min = round(float(agg.get("snore_sum") or 0) / 60, 1) if frame_count else 0.0

    apnea_risk = "low"
    if snore_min >= 20 or (avg_rr and avg_rr >= 22) or (avg_hr and avg_hr >= 90):
        apnea_risk = "medium"
    if snore_min >= 60 or (avg_rr and avg_rr >= 28) or (avg_hr and avg_hr >= 100):
        apnea_risk = "high"

    # 评分（0-100）
    score = 90
    if avg_hr:
        if avg_hr > 85 or avg_hr < 50:
            score -= 10
    if avg_rr:
        if avg_rr > 22 or avg_rr < 10:
            score -= 15
    score -= min(30, int(snore_min * 0.5))
    if duration_min < 240:  # 不足 4 小时
        score -= 10
    score = max(0, min(100, score))

    summary_text = _build_summary_text(
        duration_min, score, deep_pct, snore_min, apnea_risk, data_completeness
    )

    return {
        "duration_min": duration_min,
        "sleep_quality_score": score,
        "deep_pct": deep_pct,
        "light_pct": light_pct,
        "rem_pct": rem_pct,
        "wake_pct": wake_pct,
        "avg_heart_rate": avg_hr,
        "avg_respiratory_rate": avg_rr,
        "snore_minutes": snore_min,
        "apnea_risk": apnea_risk,
        "data_completeness": round(data_completeness, 3),
        "summary_text": summary_text,
    }


def _build_summary_text(duration_min, score, deep_pct, snore_min, apnea_risk, data_completeness) -> str:
    """一句话中文摘要，给前端卡片展示。"""
    if data_completeness >= 0.7:
        return "该日数据几乎全无，暂无法评估"
    h = int(duration_min // 60)
    m = int(duration_min % 60)
    level = "优秀" if score >= 90 else "良好" if score >= 75 else "一般" if score >= 60 else "欠佳"
    pieces = [f"睡眠{h}小时{m}分，质量{level}（{score}分）"]
    if deep_pct >= 0.20:
        pieces.append(f"深睡占比{int(deep_pct*100)}%达标")
    elif deep_pct > 0:
        pieces.append(f"深睡仅{int(deep_pct*100)}%，偏少")
    if snore_min >= 30:
        pieces.append(f"鼾声{snore_min:.0f}分钟偏多")
    if apnea_risk == "high":
        pieces.append("呼吸暂停风险高，建议就医")
    elif apnea_risk == "medium":
        pieces.append("呼吸暂停风险中等，建议关注")
    if data_completeness >= 0.30:
        pieces.append(f"数据缺失{int(data_completeness*100)}%，评估受限")
    return "，".join(pieces) + "。"
