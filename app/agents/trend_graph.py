"""TrendAgent：长期趋势智能体。

输入：最近 N 天（默认 7）的 DailyReportSummary
输出：
  - dashboard   : 看板数据（昨晚评分、7 晚均分、7 晚均时长、深睡占比均值、鼾声均值、趋势洞察一句话）
  - weekly      : 7 天每日摘要（给 history 页画折线/列表）
  - insights    : 趋势洞察（用 LLM，可选；不调也能返回规则版）

不强制 LLM：dashboard / weekly 是纯统计；insights 默认返回规则版（on-demand 快）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_dashboard(daily_summaries: list[dict], days: int = 7) -> dict[str, Any]:
    """给最近 N 天的 daily summary → 看板数据。

    入参：list of { day_start_ts, summary_json (dict-like), frame_count, data_completeness }
    出参：dashboard 对象
    """
    n = len(daily_summaries)
    if n == 0:
        return {
            "days": days,
            "sample_count": 0,
            "last_night": None,
            "avg_score": None,
            "avg_duration_min": None,
            "avg_deep_pct": None,
            "avg_snore_minutes": None,
            "avg_heart_rate": None,
            "avg_respiratory_rate": None,
            "score_trend": "no_data",
            "insight": "近 7 天暂无数据，等设备上报后即可生成趋势看板。",
            "missing_days": days,
        }

    # 解析每个 summary（DB 存的是 JSON 字符串，service 层会先反序列化）
    parsed: list[dict] = []
    for d in daily_summaries:
        sj = d.get("summary_json")
        if isinstance(sj, str):
            import json
            try:
                sj = json.loads(sj)
            except Exception:
                continue
        if not isinstance(sj, dict):
            continue
        parsed.append({
            "day_start_ts": d.get("day_start_ts"),
            "summary": sj,
            "frame_count": d.get("frame_count", 0),
            "data_completeness": d.get("data_completeness", 0),
        })

    if not parsed:
        return {
            "days": days,
            "sample_count": 0,
            "last_night": None,
            "avg_score": None,
            "avg_duration_min": None,
            "avg_deep_pct": None,
            "avg_snore_minutes": None,
            "avg_heart_rate": None,
            "avg_respiratory_rate": None,
            "score_trend": "no_data",
            "insight": "近 7 天有上报但无有效摘要，请检查设备开关。",
            "missing_days": days,
        }

    # 按时间倒序：最新在前
    parsed.sort(key=lambda x: x["day_start_ts"] or 0, reverse=True)
    last = parsed[0]
    earlier = parsed[1:]

    def _mean(field):
        vs = [p["summary"].get(field) for p in parsed if isinstance(p["summary"].get(field), (int, float))]
        return round(sum(vs) / len(vs), 1) if vs else None

    avg_score = _mean("sleep_quality_score")
    avg_dur = _mean("duration_min")
    avg_deep = _mean("deep_pct")
    avg_snore = _mean("snore_minutes")
    avg_hr = _mean("avg_heart_rate")
    avg_rr = _mean("avg_respiratory_rate")

    # 趋势：对比最近 3 天 vs 之前 4 天的均分
    trend = "stable"
    if avg_score is not None and len(parsed) >= 4:
        recent = [p["summary"].get("sleep_quality_score") for p in parsed[:3]
                  if isinstance(p["summary"].get("sleep_quality_score"), (int, float))]
        prior = [p["summary"].get("sleep_quality_score") for p in parsed[3:]
                 if isinstance(p["summary"].get("sleep_quality_score"), (int, float))]
        if recent and prior:
            r = sum(recent) / len(recent)
            p_ = sum(prior) / len(prior)
            diff = r - p_
            if diff >= 5:
                trend = "up"
            elif diff <= -5:
                trend = "down"
            else:
                trend = "stable"

    # 缺失天数
    missing_days = max(0, days - len(parsed))

    # 一句话洞察（规则版，不调 LLM）
    insight = _build_insight(parsed, avg_score, avg_dur, avg_deep, avg_snore, trend, missing_days)

    return {
        "days": days,
        "sample_count": len(parsed),
        "last_night": {
            "day_start_ts": last["day_start_ts"],
            "summary": last["summary"],
            "summary_text": last["summary"].get("summary_text"),
            "frame_count": last["frame_count"],
            "data_completeness": last["data_completeness"],
        },
        "avg_score": avg_score,
        "avg_duration_min": avg_dur,
        "avg_deep_pct": avg_deep,
        "avg_snore_minutes": avg_snore,
        "avg_heart_rate": avg_hr,
        "avg_respiratory_rate": avg_rr,
        "score_trend": trend,
        "insight": insight,
        "missing_days": missing_days,
    }


def _build_insight(parsed, avg_score, avg_dur, avg_deep, avg_snore, trend, missing_days) -> str:
    parts: list[str] = []
    if avg_score is not None:
        parts.append(f"近 7 晚平均评分 {avg_score} 分")
    if avg_dur is not None:
        h = int(avg_dur // 60)
        m = int(avg_dur % 60)
        parts.append(f"平均睡眠 {h} 小时 {m} 分")
    if avg_deep is not None:
        parts.append(f"深睡占比 {int(avg_deep*100)}%")
    if avg_snore is not None and avg_snore > 0:
        parts.append(f"平均鼾声 {avg_snore:.0f} 分钟")
    if trend == "up":
        parts.append("近期睡眠较之前改善")
    elif trend == "down":
        parts.append("近期睡眠较之前下滑")
    if missing_days > 0:
        parts.append(f"其中 {missing_days} 天数据缺失")
    if not parts:
        return "数据不足以生成趋势洞察"
    return "；".join(parts) + "。"


def compute_weekly(summaries: list[dict], days: int = 7) -> dict[str, Any]:
    """给 history 页：7 天每日一行。

    输入：list of DailyReportSummary（已按 day_start_ts 升序）
    输出：{
      "days": 7,
      "items": [
        { "day_start_ts", "day_label", "score", "duration_min", "deep_pct", "snore_minutes", "apnea_risk", "summary_text", "data_completeness" },
        ...
      ]
    }
    """
    import json
    items: list[dict] = []
    for s in summaries:
        sj = s.get("summary_json")
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                sj = {}
        if not isinstance(sj, dict):
            sj = {}
        items.append({
            "day_start_ts": s.get("day_start_ts"),
            "day_label": _day_label(s.get("day_start_ts")),
            "score": sj.get("sleep_quality_score"),
            "duration_min": sj.get("duration_min"),
            "deep_pct": sj.get("deep_pct"),
            "light_pct": sj.get("light_pct"),
            "rem_pct": sj.get("rem_pct"),
            "wake_pct": sj.get("wake_pct"),
            "avg_heart_rate": sj.get("avg_heart_rate"),
            "avg_respiratory_rate": sj.get("avg_respiratory_rate"),
            "snore_minutes": sj.get("snore_minutes"),
            "apnea_risk": sj.get("apnea_risk"),
            "data_completeness": s.get("data_completeness"),
            "frame_count": s.get("frame_count"),
            "summary_text": sj.get("summary_text"),
        })
    return {"days": days, "items": items}


def _day_label(day_start_ts: int) -> str:
    if not day_start_ts:
        return "-"
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(day_start_ts, tz=timezone.utc)
    return d.strftime("%m-%d")  # 例 "10-26"
