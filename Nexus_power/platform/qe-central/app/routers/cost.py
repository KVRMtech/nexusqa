"""QE-Central S5 — the cost API (``/api/v1/qec/…``, design §3.5).

  * ``GET  /apps/{app_id}/cost?window_hours=&group_by=`` — the app's metered cost
    rolled up from the append-only ``cost_ledger`` (RAW UNITS first; ``usd`` only
    when every entry is priced; the ``unmetered_run`` metering-gap count surfaced
    SEPARATELY so it can never be hidden inside a total);
  * ``POST /cost/entries`` — an INTERNAL, service-JWT self-report seam (the
    explorer / synthesis / driver report units they alone can measure).  Guarded
    by the admin|manager gate the service token satisfies; an unknown or negative
    unit is a 422 (a silently-dropped or negated cost would green-wash spend).

The meter can only UNDER-count: this API never invents dollars and never
fabricates ``browser_seconds``.  ZERO LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..controlplane.cost import meter
from ..db import tenant_scoped_qec_session
from ..db.controlplane_models import CostLedgerRow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Cost"])

_MUTATE = require_role("admin", "manager")

#: The aggregation groupings :func:`meter.aggregate_cost` accepts.
_GROUP_BY = frozenset({"cycle_id", "app_id", "unit", "tenant_id", "none"})


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "")


class CostEntry(BaseModel):
    """One internal cost self-report (service-JWT).

    ``units`` maps a cost unit → quantity (the SAME vocabulary the meter + budget
    gate share); ``unit_cost_usd`` is OPTIONAL (raw units publish without dollars).
    An unknown or negative unit is rejected by the meter (422)."""

    app_id: str = Field(default="", max_length=64)
    cycle_id: str = Field(default="", max_length=64)
    units: dict[str, float] = Field(default_factory=dict)
    source_ref: str = Field(default="", max_length=200)
    unit_cost_usd: dict[str, float] | None = None


def _window(window_hours: float | None) -> tuple[datetime | None, datetime | None]:
    """Build a ``(start, end)`` window from a look-back in hours (None = all-time)."""
    if window_hours is None or window_hours <= 0:
        return (None, None)
    return (datetime.now(timezone.utc) - timedelta(hours=float(window_hours)), None)


@router.get("/apps/{app_id}/cost")
async def get_app_cost(
    app_id: str,
    window_hours: float | None = Query(
        default=None, ge=0,
        description="Only consider ledger entries newer than this many hours (default: all).",
    ),
    group_by: str = Query(default="cycle_id", max_length=16),
    user: dict = Depends(require_auth),
) -> dict:
    """The app's metered cost, grouped + windowed (RAW UNITS first)."""
    tenant_id = user["tenant_id"]
    gb = (group_by or "cycle_id").strip().lower()
    if gb not in _GROUP_BY:
        raise HTTPException(status_code=422, detail=f"group_by must be one of {sorted(_GROUP_BY)}")

    start, end = _window(window_hours)
    async with tenant_scoped_qec_session(tenant_id) as session:
        stmt = select(CostLedgerRow).where(
            CostLedgerRow.tenant_id == tenant_id, CostLedgerRow.app_id == app_id,
        )
        if start is not None:
            stmt = stmt.where(CostLedgerRow.created_at >= start)
        rows = (await session.execute(stmt)).scalars().all()

    aggregates = meter.aggregate_cost(
        meter.rows_to_entry_dicts(list(rows)), group_by=gb, window=(start, end),
    )
    groups = {
        key: {
            "units": {u: str(q) for u, q in agg.units.items()},
            "unmetered_runs": agg.unmetered_runs,
            "usd": (str(agg.usd) if agg.usd is not None else None),
            "entry_count": agg.entry_count,
        }
        for key, agg in aggregates.items()
    }
    return {
        "app_id": app_id,
        "group_by": gb,
        "window_hours": window_hours,
        "groups": groups,
        "group_count": len(groups),
    }


@router.post("/cost/entries", status_code=201)
async def record_cost_entry(payload: CostEntry, user: dict = Depends(_MUTATE)) -> dict:
    """Record an internal cost self-report (service-JWT / admin|manager).

    Persists append-only ledger rows through the meter (one row per unit).  An
    unknown or negative unit is a 422 — a dropped/negated cost would green-wash
    spend, and the ledger can only ever accumulate."""
    tenant_id = user["tenant_id"]
    if not payload.units:
        raise HTTPException(status_code=422, detail="units is required (at least one cost unit)")
    try:
        rows = await meter.record_cost(
            tenant_id=tenant_id, app_id=payload.app_id, cycle_id=payload.cycle_id,
            units=payload.units, source_ref=payload.source_ref,
            unit_cost_usd=payload.unit_cost_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info(
        "qec.cost.self_report",
        extra={"tenant_id": tenant_id, "app_id": payload.app_id,
               "cycle_id": payload.cycle_id, "units": list(payload.units.keys()),
               "actor": _actor(user)},
    )
    return {
        "recorded": len(rows),
        "app_id": payload.app_id,
        "cycle_id": payload.cycle_id,
        "units": {r.unit: str(r.quantity) for r in rows},
    }
