"""QE-Central S5 — the cycle API (``/api/v1/qec/…``, design §3.5).

The human/observability surface over the cycle-driver state machine:

  * ``POST /apps/{app_id}/cycles`` ``{mode: auto|full|probe_only}`` — create ONE
    cycle (409 when the app already has an ACTIVE cycle, enforced by the
    ``app_cycles`` partial unique index) and fire it immediately so it self-drives
    even when the autonomous daemon is off;
  * ``GET  /apps/{app_id}/cycles`` — the app's recent cycles (newest first);
  * ``GET  /cycles/{cycle_id}`` — ONE cycle in full: state, SELECTED vs
    CARRIED-FORWARD cases WITH their verdict ages, the honest gaps, and a live
    cost snapshot rolled up from the append-only ``cost_ledger``.

Every endpoint is tenant-scoped (RLS) and every mutation rides the admin|manager
RBAC gate (platform-api parity).  ZERO LLM.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..auth import require_auth, require_role
from ..controlplane.cost import meter
from ..controlplane.cycle import driver
from ..security import prod_guard
from ..db import row_to_dict, tenant_scoped_qec_session
from ..db.controlplane_models import (
    CYCLE_TRIGGER_MANUAL,
    AppCycleRow,
    is_terminal_cycle_state,
)
from ..db.models import ClientAppRow
from ..fleet import quota
from ..fleet.lifecycle import TenantNotOperational
from ..fleet.provisioning import assert_tenant_operational_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Cycles"])

_MUTATE = require_role("admin", "manager")


class CycleCreate(BaseModel):
    """Start one regression cycle.

    ``mode``: ``auto`` (incremental unless the detector escalates to full),
    ``full`` (re-run every case), or ``probe_only`` (a dry-run that probes +
    selects but never generates/runs)."""

    mode: str = Field(default=driver.MODE_AUTO, max_length=20)


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "")


async def _require_app(session, tenant_id: str, app_id: str) -> ClientAppRow:
    """Fetch one tenant-owned, non-deleted app row or 404 (mirrors ``_require_artifact``)."""
    row = (await session.execute(
        select(ClientAppRow).where(
            ClientAppRow.app_id == app_id, ClientAppRow.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if row is None or row.status == "deleted":
        raise HTTPException(status_code=404, detail="app not found")
    return row


def _cycle_summary(row: AppCycleRow) -> dict:
    """A compact cycle view for the list endpoint (no heavy result JSONB)."""
    scope = dict(row.selected_scope or {})
    gaps = dict(row.honest_gaps or {})
    # Regression Agent: surface ONLY the lightweight review count (int) here so the
    # cycles list shows "N cases need review" without the heavy per-case verdict blob
    # (the full regression_verdicts stay on GET /cycles/{id}).
    _rv = (dict(row.result or {})).get("regression_verdicts") or {}
    _review = sum(1 for v in _rv.values() if isinstance(v, dict) and v.get("needs_review"))
    return {
        "cycle_id": row.cycle_id,
        "app_id": row.app_id,
        "trigger": row.trigger,
        "state": row.state,
        "terminal": is_terminal_cycle_state(row.state),
        "mode": scope.get("mode"),
        "selected_count": len(scope.get("selected_test_ids") or []),
        "carried_count": len(scope.get("carried_forward") or []),
        "regression_review_count": _review,
        "possible_deletion": bool(gaps.get("vanished_pages_possible_deletion")),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


async def _cost_snapshot(tenant_id: str, cycle_id: str) -> dict:
    """Roll up the cycle's ``cost_ledger`` rows into raw units + the gap count.

    RAW UNITS first: ``usd`` is populated only when every entry is priced.  The
    ``unmetered_runs`` gap count is surfaced SEPARATELY so a metering gap is
    visible, never hidden inside a total."""
    rows = await meter.load_cycle_ledger(tenant_id=tenant_id, cycle_id=cycle_id)
    aggregates = meter.aggregate_cost(
        meter.rows_to_entry_dicts(rows), group_by="cycle_id",
    )
    agg = aggregates.get(cycle_id)
    if agg is None:
        return {"units": {}, "unmetered_runs": 0, "usd": None, "entry_count": 0}
    return {
        "units": {u: str(q) for u, q in agg.units.items()},
        "unmetered_runs": agg.unmetered_runs,
        "usd": (str(agg.usd) if agg.usd is not None else None),
        "entry_count": agg.entry_count,
    }


@router.post("/apps/{app_id}/cycles", status_code=202)
async def create_cycle(
    app_id: str, payload: CycleCreate, user: dict = Depends(_MUTATE),
) -> dict:
    """Create + fire ONE cycle (409 when an active cycle already exists)."""
    tenant_id = user["tenant_id"]
    mode = (payload.mode or driver.MODE_AUTO).strip().lower()
    if mode not in driver.CYCLE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of {sorted(driver.CYCLE_MODES)}",
        )

    async with tenant_scoped_qec_session(tenant_id) as session:
        app = await _require_app(session, tenant_id, app_id)
        if app.status != "active":
            raise HTTPException(
                status_code=409,
                detail=f"app is {app.status} — resume it before starting a cycle",
            )
        if not (app.latest_artifact_id or "").strip():
            raise HTTPException(
                status_code=409,
                detail="app has no latest_artifact_id to run a cycle against "
                       "(register a crawl/exploration first)",
            )
        # Phase-6 SAFETY SPINE — fail-closed onboarding gate.  A real app may be
        # cycled ONLY when it is onboarding-'live' (signed rules-of-engagement +
        # a non-prod/disposable attestation + a passed preflight).  INERT in
        # development/test with an explicit per-app bypass flag; fail-closed by
        # default and always fail-closed in staging/production.
        try:
            prod_guard.assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE)
        except prod_guard.OnboardingRefused as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
        # Phase-7 FLEET lifecycle gate — a SUSPENDED / offboarding tenant may not
        # start a cycle (fail-closed).  A tenant with no control record is
        # operational (today's behavior).  Uses the open tenant-scoped session.
        try:
            await assert_tenant_operational_db(session, tenant_id, operation="cycle")
        except TenantNotOperational as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())

    # Phase-7 fleet quota (fail-closed, OPT-IN): refuse a new cycle when the tenant
    # is over its plan's concurrency or monthly-spend cap.  The default plan leaves
    # every cap unlimited, so this resolves the plan and returns immediately (no
    # query) — today's behaviour is unchanged.  The driver re-checks at the shared
    # creation choke point (defense-in-depth for the daemon/webhook paths).
    try:
        await quota.enforce_cycle_quota(tenant_id)
    except quota.QuotaExceeded as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())

    try:
        cycle_id = await driver.create_cycle(
            tenant_id=tenant_id, app_id=app_id, mode=mode, trigger=CYCLE_TRIGGER_MANUAL,
        )
    except IntegrityError:
        # The one-active-cycle-per-app partial unique index rejected the insert.
        raise HTTPException(
            status_code=409,
            detail="an active cycle already exists for this app",
        )
    except prod_guard.OnboardingRefused as exc:
        # Defense-in-depth: the driver re-checks the onboarding gate at the shared
        # creation choke point; map a (racing) refusal to a clean 409/422, never 500.
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
    except quota.QuotaExceeded as exc:
        # Defense-in-depth: the driver re-checks the quota at the shared creation
        # choke point; map a (racing) refusal to a clean 429/409, never 500.
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
    except TenantNotOperational as exc:
        # Defense-in-depth: the driver re-checks the lifecycle gate at the shared
        # creation choke point; map a (racing) suspend refusal to a clean 403.
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())

    driver.launch_cycle(
        cycle_id=cycle_id, tenant_id=tenant_id, app_id=app_id,
        mode=mode, trigger=CYCLE_TRIGGER_MANUAL,
    )
    logger.info(
        "qec.cycles.created",
        extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id,
               "mode": mode, "actor": _actor(user)},
    )
    return {"cycle_id": cycle_id, "app_id": app_id, "mode": mode,
            "trigger": CYCLE_TRIGGER_MANUAL, "state": "pending"}


@router.get("/apps/{app_id}/cycles")
async def list_cycles(
    app_id: str,
    limit: int = Query(default=25, ge=1, le=200),
    user: dict = Depends(require_auth),
) -> dict:
    """List an app's recent cycles (newest first)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        await _require_app(session, tenant_id, app_id)
        rows = (await session.execute(
            select(AppCycleRow)
            .where(AppCycleRow.tenant_id == tenant_id, AppCycleRow.app_id == app_id)
            .order_by(AppCycleRow.created_at.desc())
            .limit(limit)
        )).scalars().all()
    return {"app_id": app_id, "cycles": [_cycle_summary(r) for r in rows], "total": len(rows)}


@router.get("/cycles/{cycle_id}")
async def get_cycle(cycle_id: str, user: dict = Depends(require_auth)) -> dict:
    """One cycle in full: state, SELECTED vs CARRIED-FORWARD (with verdict ages),
    honest gaps, and a live cost snapshot."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(AppCycleRow).where(
                AppCycleRow.cycle_id == cycle_id, AppCycleRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="cycle not found")
        scope = dict(row.selected_scope or {})
        gaps = dict(row.honest_gaps or {})
        result = dict(row.result or {})
        base = row_to_dict(row)

    cost = await _cost_snapshot(tenant_id, cycle_id)
    return {
        **base,
        # Surface the selection with explicit selected vs carried-forward (each
        # carried case carries its verdict_run_id + verdict_age_cycles).
        "selected": {
            "mode": scope.get("mode"),
            "selected_test_ids": scope.get("selected_test_ids") or [],
            "carried_forward": scope.get("carried_forward") or [],
            "selection_reason": scope.get("selection_reason") or "",
            "per_case_reasons": scope.get("per_case_reasons") or {},
        },
        "honest_gaps": gaps,
        "coverage_verdict": result.get("coverage_verdict"),
        # Regression Agent (Phase 5): per-case dispositions + the count of cases
        # that need a human review (GENUINE_REGRESSION / HONEST_UNPROVEN).
        "regression_verdicts": result.get("regression_verdicts") or {},
        "regression_review_count": sum(
            1 for v in (result.get("regression_verdicts") or {}).values()
            if isinstance(v, dict) and v.get("needs_review")
        ),
        "cost": cost,
    }
