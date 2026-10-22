"""Fab KPIs: availability, scrap and maintenance split by maintenance strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Maintenance, StateHistory, WaferResult

UNAVAILABLE = ("DOWN", "PM", "OFFLINE")


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone=True columns.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def availability(s: AsyncSession, window: timedelta = timedelta(hours=1)) -> Dict[str, Dict]:
    """Share of the window each tool spent in each state."""
    now = datetime.now(timezone.utc)
    start = now - window
    rows = (
        await s.execute(
            select(StateHistory).where((StateHistory.ended_at.is_(None)) | (StateHistory.ended_at > start))
        )
    ).scalars()
    totals: Dict[str, Dict[str, float]] = {}
    for r in rows:
        a = max(_aware(r.started_at), start)
        b = _aware(r.ended_at) if r.ended_at else now
        if b <= a:
            continue
        totals.setdefault(r.equipment_id, {}).setdefault(r.state, 0.0)
        totals[r.equipment_id][r.state] += (b - a).total_seconds()
    out = {}
    for eq, states in totals.items():
        total = sum(states.values()) or 1.0
        down = sum(v for k, v in states.items() if k in UNAVAILABLE)
        out[eq] = {
            "availability": round(100 * (1 - down / total), 1),
            "states": {k: round(100 * v / total, 1) for k, v in states.items()},
        }
    return out


async def strategy_comparison(s: AsyncSession) -> List[Dict]:
    """Wafer outcome and maintenance counts for predictive vs reactive mode."""
    wafer_rows = (
        await s.execute(
            select(
                WaferResult.predictive_mode,
                func.count(),
                func.sum(case((WaferResult.result == "SCRAP", 1), else_=0)),
            ).group_by(WaferResult.predictive_mode)
        )
    ).all()
    # Maintenance is a small table; aggregate in Python so the query stays
    # portable between SQLite and PostgreSQL.
    now = datetime.now(timezone.utc)
    maint: Dict = {}
    for m in (await s.execute(select(Maintenance))).scalars():
        if m.status == "CANCELLED":
            continue
        # A repair's downtime starts at the failure (when the work order was
        # raised); a PM's starts when the technician takes the tool.
        begin = m.requested_at if m.kind == "REPAIR" else m.started_at
        end = _aware(m.finished_at) if m.finished_at else now
        secs = (end - _aware(begin)).total_seconds() if begin else 0.0
        n, total = maint.get((m.predictive_mode, m.kind), (0, 0.0))
        maint[(m.predictive_mode, m.kind)] = (n + 1, total + max(0.0, secs))
    out = []
    for mode in (True, False):
        processed, scrapped = next(((n, sc or 0) for m, n, sc in wafer_rows if m == mode), (0, 0))
        pm_n, pm_s = maint.get((mode, "PM"), (0, 0.0))
        rep_n, rep_s = maint.get((mode, "REPAIR"), (0, 0.0))
        out.append(
            {
                "mode": "predictive" if mode else "reactive",
                "wafers": processed,
                "scrapped": scrapped,
                "scrap_rate": round(100 * scrapped / processed, 2) if processed else 0.0,
                "pm_count": pm_n,
                "repairs": rep_n,
                "planned_down_s": round(pm_s),
                "unplanned_down_s": round(rep_s),
            }
        )
    return out
