"""MySQL RAG 检索：从数据库里捞「相似历史」喂给 LLM。

查询方式（按场景切换）：
  - 报告生成（report_graph）：按"得分区间 + 鼾声分钟"在近 30 天同 device 查 5 条
  - 对话问答（chat_graph）  ：按关键词在 report 字段 LIKE 查 Top-K
  - 数据分析（data_graph）  ：按睡眠分期+呼吸暂停风险查 Top-K

返回结构：
  {
    "items": [
      {"id": 12, "date": "2025-10-05", "score": 0.86, "summary": "...", "report": "..."},
      ...
    ],
    "context": "【历史相似案例 (N 条)】\n1) 2025-10-05 评分 86 (良好) ...\n2) ..."
  }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SleepRecord

logger = logging.getLogger(__name__)


def _to_dt(ts) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    return None


async def _base_query(
    db: AsyncSession,
    user_id: int,
    *,
    days: int = 30,
    exclude_id: int | None = None,
    limit: int = 5,
):
    """近 days 天的同用户睡眠记录，按 start_time 倒序。"""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    stmt = (
        select(SleepRecord)
        .where(SleepRecord.user_id == user_id)
        .where(SleepRecord.start_time >= cutoff)
    )
    if exclude_id is not None:
        stmt = stmt.where(SleepRecord.id != exclude_id)
    stmt = stmt.order_by(SleepRecord.start_time.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _fmt_items(items: list[SleepRecord]) -> list[dict[str, Any]]:
    out = []
    for r in items:
        out.append({
            "id": r.id,
            "date": r.start_time.strftime("%Y-%m-%d") if r.start_time else None,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "score": r.sleep_quality_score,
            "avg_heart_rate": r.avg_heart_rate,
            "avg_respiratory_rate": r.avg_respiratory_rate,
            "apnea_risk": r.apnea_risk,
            "summary": (r.report or "").split("\n")[0][:200] if r.report else "",
            "report_excerpt": (r.report or "")[:600] if r.report else "",
        })
    return out


def _build_context(items: list[dict[str, Any]], header: str = "历史相似案例") -> str:
    if not items:
        return ""
    lines = [f"【{header} (共 {len(items)} 条)】"]
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}) 日期 {it['date']}  评分 {it.get('score') or 'N/A'}  "
            f"HR {it.get('avg_heart_rate') or 'N/A'}  RR {it.get('avg_respiratory_rate') or 'N/A'}  "
            f"apnea_risk {it.get('apnea_risk') or 'N/A'}"
        )
        if it.get("summary"):
            lines.append(f"   摘要：{it['summary']}")
    return "\n".join(lines)


async def retrieve_similar_reports(
    db: AsyncSession,
    user_id: int,
    *,
    exclude_id: int | None = None,
    days: int = 30,
    limit: int = 5,
    score_band: tuple[float, float] | None = None,
    snore_min_threshold: float | None = None,
) -> dict[str, Any]:
    """为 report_graph 检索：按时间 + 得分区间 + 鼾声分钟过滤。"""
    items_db = await _base_query(
        db, user_id, days=days, exclude_id=exclude_id, limit=limit * 3
    )
    # 客户端轻过滤（数据库字段有限，先全量拉再在 Python 端过滤）
    items_filt: list[SleepRecord] = []
    for r in items_db:
        if score_band is not None and r.sleep_quality_score is not None:
            lo, hi = score_band
            if not (lo <= r.sleep_quality_score <= hi):
                continue
        if snore_min_threshold is not None:
            # 鼾声分钟数从 analysis.feats.snore_minutes 取
            feats = (r.analysis or {}).get("feats", {}) or {}
            snore_min = feats.get("snore_minutes")
            if snore_min is not None and snore_min < snore_min_threshold:
                continue
        items_filt.append(r)
        if len(items_filt) >= limit:
            break

    items = _fmt_items(items_filt)
    return {"items": items, "context": _build_context(items)}


async def retrieve_by_keyword(
    db: AsyncSession,
    user_id: int,
    keyword: str,
    *,
    days: int = 90,
    limit: int = 5,
) -> dict[str, Any]:
    """为 chat_graph 检索：报告全文 LIKE 关键词。"""
    if not keyword or not keyword.strip():
        return {"items": [], "context": ""}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    pat = f"%{keyword.strip()}%"
    stmt = (
        select(SleepRecord)
        .where(SleepRecord.user_id == user_id)
        .where(SleepRecord.start_time >= cutoff)
        .where(
            or_(
                SleepRecord.report.like(pat),
                SleepRecord.analysis.like(pat),
            )
        )
        .order_by(SleepRecord.start_time.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    items_db = list(result.scalars().all())
    items = _fmt_items(items_db)
    return {"items": items, "context": _build_context(items, header="历史相关报告")}


async def retrieve_by_metrics(
    db: AsyncSession,
    user_id: int,
    *,
    apnea_risk: str | None = None,
    score_band: tuple[float, float] | None = None,
    days: int = 30,
    limit: int = 5,
) -> dict[str, Any]:
    """为 data_graph 检索：按风险 + 得分区间。"""
    items_db = await _base_query(db, user_id, days=days, limit=limit * 2)
    items_filt: list[SleepRecord] = []
    for r in items_db:
        if apnea_risk is not None and r.apnea_risk and r.apnea_risk != apnea_risk:
            continue
        if score_band is not None and r.sleep_quality_score is not None:
            lo, hi = score_band
            if not (lo <= r.sleep_quality_score <= hi):
                continue
        items_filt.append(r)
        if len(items_filt) >= limit:
            break
    items = _fmt_items(items_filt)
    return {"items": items, "context": _build_context(items, header="历史相似指标案例")}
