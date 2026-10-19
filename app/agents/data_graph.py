"""DataAgent：把设备原始数据转换为结构化睡眠分析结果。

图结构：
  START
    └─► extract_features  (规则：均值/方差/事件计数)
        └─► llm_summarize  (LLM 输出 JSON 摘要)
            └─► assess_risk  (融合规则 + LLM，给出最终 analysis)
                └─► END
"""
from __future__ import annotations

import json
import re
from statistics import mean, pstdev
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.llm import get_chat_model
from app.agents.state import DataAgentState
from app.config import settings

# ---------- 工具：特征提取 ----------


def _safe_float_list(xs: list[Any]) -> list[float]:
    out: list[float] = []
    for x in xs:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def extract_features(state: DataAgentState) -> dict[str, Any]:
    """基于规则的轻量特征抽取。

    输入 raw_data 形如：
    {
      "vital_signs": [{"heart_rate":72,"respiratory_rate":16,"bed_status":"in_bed","turnover_count":1}, ...],
      "audio_segments": [{"start":..., "end":..., "snore_detected":true, "intensity":0.6}, ...],
      "start_time": ..., "end_time": ...
    }
    """
    raw = state.get("raw_data") or {}
    vitals = raw.get("vital_signs", []) or []
    audios = raw.get("audio_segments", []) or []

    hrs = _safe_float_list([v.get("heart_rate") for v in vitals])
    rrs = _safe_float_list([v.get("respiratory_rate") for v in vitals])
    turnovers = sum(int(v.get("turnover_count", 0) or 0) for v in vitals)

    out_bed_count = sum(1 for v in vitals if v.get("bed_status") == "out_of_bed")
    snore_events = [a for a in audios if a.get("snore_detected")]
    snore_intensity = _safe_float_list([a.get("intensity", 0) for a in audios])
    avg_snore = mean(snore_intensity) if snore_intensity else 0.0

    features: dict[str, Any] = {
        "sample_count": len(vitals),
        "avg_heart_rate": round(mean(hrs), 1) if hrs else None,
        "std_heart_rate": round(pstdev(hrs), 2) if len(hrs) > 1 else 0.0,
        "avg_respiratory_rate": round(mean(rrs), 1) if rrs else None,
        "total_turnovers": turnovers,
        "out_of_bed_samples": out_bed_count,
        "snore_events_count": len(snore_events),
        "avg_snore_intensity": round(avg_snore, 3),
        "audio_segment_count": len(audios),
    }
    return {"features": features}


# ---------- 工具：LLM 摘要 ----------


def _parse_json_safely(text: str) -> dict[str, Any] | None:
    """尽量从 LLM 输出中抠出 JSON。"""
    if not text:
        return None
    # 尝试直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 尝试 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    # 尝试抓首个 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def llm_summarize(state: DataAgentState) -> dict[str, Any]:
    """调用 LLM 产出 JSON 摘要。失败时返回空串，由后续节点兜底。"""
    llm = get_chat_model()
    features = state.get("features") or {}
    prompt = (
        "你是睡眠数据分析专家。下面是已计算好的统计特征，请输出严格的 JSON，"
        "包含字段 sleep_quality_score(0-1)、apnea_risk(low|medium|high)、"
        "sleep_stages(deep/light/rem/wake 占比和≈1)、notes(中文一句话)、"
        "dims(数组，每项 {name:维度名, score:0-100}，建议 4 项如心率稳定性/呼吸节律/体动/鼾声)、"
        "issues(数组，每项 {text:中文, severity:primary|warning|error}，无异常给空数组)、"
        "suggestions(数组，中文改善建议 2-3 条)。\n"
        f"特征: {json.dumps(features, ensure_ascii=False)}"
    )
    try:
        resp = llm.invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as e:  # noqa: BLE001
        return {"llm_summary": "", "error": f"llm_invoke_failed: {e}"}
    return {"llm_summary": text}


# ---------- 工具：风险融合 ----------


def _rule_based_assessment(features: dict[str, Any]) -> dict[str, Any]:
    """无 LLM 或 LLM 解析失败时使用的纯规则评估。"""
    hr = features.get("avg_heart_rate") or 70
    rr = features.get("avg_respiratory_rate") or 16
    snore_n = features.get("snore_events_count", 0)
    snore_i = features.get("avg_snore_intensity", 0.0)
    out_n = features.get("out_of_bed_samples", 0)
    samples = max(features.get("sample_count", 1), 1)

    # 简单的呼吸暂停风险评估
    apnea_risk = "low"
    if snore_n >= 5 or snore_i >= 0.7 or rr >= 22 or hr >= 90:
        apnea_risk = "medium"
    if snore_n >= 10 or rr >= 28 or hr >= 100:
        apnea_risk = "high"

    # 睡眠质量评分（0-1）
    score = 0.9
    if hr > 85 or hr < 50:
        score -= 0.1
    if rr > 22 or rr < 10:
        score -= 0.15
    score -= min(0.3, snore_n * 0.03)
    score -= min(0.2, snore_i * 0.2)
    score -= min(0.2, (out_n / samples) * 0.5)
    score = max(0.0, min(1.0, round(score, 3)))

    # ---- 展示字段：维度评分 / 问题 / 建议（LLM 缺失时的规则兜底） ----
    def clamp(x: float) -> float:
        return max(0.0, min(100.0, round(float(x), 1)))

    risk_score = {"low": 92, "medium": 70, "high": 45}.get(apnea_risk, 85)
    dims = [
        {"name": "心率稳定性", "score": clamp(100 - min(40, abs(hr - 68) * 4) - min(20, (features.get("std_heart_rate") or 0) * 8) if hr else 78)},
        {"name": "呼吸节律", "score": clamp(100 - min(40, abs(rr - 15) * 5) if rr else 78)},
        {"name": "在床/体动", "score": clamp(100 - min(30, (out_n / samples) * 60))},
        {"name": "呼吸暂停", "score": float(risk_score)},
    ]

    issues = []
    if snore_n:
        issues.append({"text": f"检出 {snore_n} 段鼾声，平均强度 {snore_i}", "severity": "warning"})
    if apnea_risk == "high":
        issues.append({"text": "呼吸暂停风险偏高，建议关注", "severity": "error"})
    elif apnea_risk == "medium":
        issues.append({"text": "呼吸暂停风险中等", "severity": "warning"})
    if out_n:
        issues.append({"text": f"有 {out_n} 次离床样本，留意睡眠连续性", "severity": "primary"})
    if not issues:
        issues.append({"text": "未发现明显异常", "severity": "primary"})

    suggestions = ["建议保持规律作息与适宜室温，有助于提升深睡比例"]
    if snore_n or snore_i >= 0.5:
        suggestions.insert(0, "建议侧卧或抬高枕头，可减缓鼾声")
    if apnea_risk in ("medium", "high"):
        suggestions.insert(0, "建议结合连续监测观察，必要时咨询睡眠专科医生")

    # 不再写具体模型名到 analysis.model（前端不再展示、后端提示词也不展示）
    # 该字段若前端需要知道"是否用了真 LLM"，可由 settings.use_real_llm 表达

    return {
        "sleep_quality_score": score,
        "apnea_risk": apnea_risk,
        "sleep_stages": {"deep": 0.20, "light": 0.55, "rem": 0.20, "wake": 0.05},
        "notes": "由规则引擎评估（LLM 不可用时的兜底）。",
        "dims": dims,
        "issues": issues,
        "suggestions": suggestions,
        # 留 model 字段但恒为 None，避免 LLM 返回 model=xxx 时被前端展示
        "model": None,
    }


def assess_risk(state: DataAgentState) -> dict[str, Any]:
    """融合 LLM JSON 与规则特征，得到最终 analysis。"""
    features = state.get("features") or {}
    parsed = _parse_json_safely(state.get("llm_summary", "") or "")
    rule_result = _rule_based_assessment(features)

    if parsed:
        # 以 LLM 为主，回填缺失字段
        analysis = {**rule_result, **parsed}
        # 字段类型守卫
        if not isinstance(analysis.get("sleep_stages"), dict):
            analysis["sleep_stages"] = rule_result["sleep_stages"]
        try:
            analysis["sleep_quality_score"] = float(analysis.get("sleep_quality_score", rule_result["sleep_quality_score"]))
        except (TypeError, ValueError):
            analysis["sleep_quality_score"] = rule_result["sleep_quality_score"]
        if analysis.get("apnea_risk") not in {"low", "medium", "high"}:
            analysis["apnea_risk"] = rule_result["apnea_risk"]
    else:
        analysis = rule_result

    # 把特征也合并进 analysis，便于报告 Agent 使用
    analysis["features"] = features

    # 展示字段兜底：LLM 未产出时用规则结果补齐，避免前端空白
    for k in ("dims", "issues", "suggestions", "model"):
        if not analysis.get(k):
            analysis[k] = rule_result.get(k)
    if not analysis.get("issues"):
        analysis["issues"] = rule_result.get("issues") or []

    return {"analysis": analysis}


# ---------- 构建图 ----------

def build_data_graph():
    g = StateGraph(DataAgentState)
    g.add_node("extract_features", extract_features)
    g.add_node("llm_summarize", llm_summarize)
    g.add_node("assess_risk", assess_risk)

    g.add_edge(START, "extract_features")
    g.add_edge("extract_features", "llm_summarize")
    g.add_edge("llm_summarize", "assess_risk")
    g.add_edge("assess_risk", END)
    return g.compile()


data_graph = build_data_graph()
