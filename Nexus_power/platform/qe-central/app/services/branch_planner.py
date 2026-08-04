"""Branch planner — turns the graph's unwalked branches into walk plans.

A plan is the executable answer to "walk the path behind the option nobody
chose": for ONE journey, force at most ONE not-yet-walked option per decision
control along the journey's proven path, as a coherent constrained identity.
Conflicting options on the same control NEVER combine into one plan (one walk
takes one path); options on different controls of the same path DO combine
(each forces independently).

Priorities: journeys whose paths display outcomes first (a different premium
IS a different business path — the highest-value proof), then the largest
discovered backlog.

Status law: ``discovered → planned`` at dispatch; the completion fold
upgrades ``planned → walked`` when the option was genuinely taken; the
reconciler here marks what a planned walk did NOT reach as ``blocked`` with
its attributed reason — surfaced, never silently retried. ``walked`` never
downgrades.

This module is PURE PLANNING + STATUS: it never dispatches. Dispatch
orchestration (flags, caps, the actual crawl) lives at the router layer.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from sqlalchemy import select

from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.fleet_models import TenantProvisioningRow
from ..db.journey_models import (
    JourneyBranchRow,
    JourneyNodeRow,
    JourneyRow,
    JourneyTraversalRow,
)
from .journey_fold import (
    BRANCH_BLOCKED,
    BRANCH_DISCOVERED,
    BRANCH_PLANNED,
    BRANCH_WALKED,
)

logger = logging.getLogger(__name__)


async def autonomy_flags(tenant_id: str) -> dict[str, bool]:
    """The tenant's autonomy posture, FAIL-CLOSED: each capability needs BOTH
    its env-level switch AND the tenant's explicit flag."""
    tenant_branch = tenant_autowalk = False
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (await session.execute(
                select(TenantProvisioningRow.branch_walks_enabled,
                       TenantProvisioningRow.journey_autowalk).where(
                    TenantProvisioningRow.tenant_id == tenant_id,
                ))).one_or_none()
            if row is not None:
                tenant_branch, tenant_autowalk = bool(row[0]), bool(row[1])
    except Exception as exc:
        logger.warning("qec.branch_planner.flags_read_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
    return {
        "branch_walks": settings.branch_walks_enabled and tenant_branch,
        "autowalk": (settings.journey_autowalk_enabled and tenant_autowalk
                     and settings.branch_walks_enabled and tenant_branch),
    }


def _identity_ref(overrides: dict[str, str]) -> str:
    """The constrained-identity marker stamped onto the traversal: the crawl's
    stable synthetic identity, constrained by exactly these planned choices —
    auditable, value-free."""
    basis = "\x1f".join(f"{k}={v}" for k, v in sorted(overrides.items()))
    return "synthetic+planned:" + hashlib.sha256(
        basis.encode("utf-8")).hexdigest()[:12]


async def plan_walks(
    *, tenant_id: str, app_id: str, journey_id: Optional[str] = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Walk plans for this app's ``discovered`` branch backlog.

    One plan per journey per call (a walk takes one path): for each decision
    node on the journey's most recent proven path, at most one not-yet-walked
    option per control. Returns ``[{journey_id, business_name, branch_ids,
    choice_overrides, identity_ref}]``, highest-value first."""
    cap = limit or settings.branch_walks_per_cycle
    plans: list[dict[str, Any]] = []
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey_q = select(JourneyRow).where(
            JourneyRow.tenant_id == tenant_id, JourneyRow.app_id == app_id)
        if journey_id:
            journey_q = journey_q.where(JourneyRow.journey_id == journey_id)
        journeys = (await session.execute(journey_q)).scalars().all()

        scored: list[tuple[int, int, JourneyRow, list[str]]] = []
        for j in journeys:
            traversal = (await session.execute(
                select(JourneyTraversalRow).where(
                    JourneyTraversalRow.tenant_id == tenant_id,
                    JourneyTraversalRow.app_id == app_id,
                    JourneyTraversalRow.journey_id == j.journey_id,
                    JourneyTraversalRow.completed.is_(True),
                ).order_by(JourneyTraversalRow.created_at.desc())
                .limit(1))).scalar_one_or_none()
            if traversal is None:
                continue  # nothing proven to re-walk from — a crawl comes first
            path_fps = [str(fp) for fp in (traversal.path_fps or [])]
            if not path_fps:
                continue
            discovered = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                    JourneyBranchRow.node_fp.in_(path_fps),
                    JourneyBranchRow.status == BRANCH_DISCOVERED,
                ))).scalars().all()
            if not discovered:
                continue
            has_outcome = (await session.execute(
                select(JourneyNodeRow.node_id).where(
                    JourneyNodeRow.tenant_id == tenant_id,
                    JourneyNodeRow.app_id == app_id,
                    JourneyNodeRow.fingerprint.in_(path_fps),
                    JourneyNodeRow.has_outcome.is_(True),
                ).limit(1))).scalar_one_or_none() is not None
            scored.append((1 if has_outcome else 0, len(discovered), j,
                           path_fps))

        scored.sort(key=lambda t: (-t[0], -t[1]))
        for _, _, journey, path_fps in scored[:cap]:
            discovered = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                    JourneyBranchRow.node_fp.in_(path_fps),
                    JourneyBranchRow.status == BRANCH_DISCOVERED,
                ))).scalars().all()
            # Decision nodes closest to the entry first; ONE option per
            # control per plan (conflicting options never combine).
            order = {fp: i for i, fp in enumerate(path_fps)}
            discovered.sort(key=lambda b: (order.get(b.node_fp, 1 << 30),
                                           b.control_signature,
                                           b.option_label_norm))
            overrides: dict[str, str] = {}
            branch_ids: list[str] = []
            for b in discovered:
                if b.control_signature in overrides:
                    continue
                overrides[b.control_signature] = b.option_label_norm
                branch_ids.append(b.branch_id)
            if not overrides:
                continue
            plans.append({
                "journey_id": journey.journey_id,
                "business_name": journey.business_name,
                "branch_ids": branch_ids,
                "choice_overrides": overrides,
                "identity_ref": _identity_ref(overrides),
            })
    return plans


async def mark_planned(*, tenant_id: str, branch_ids: list[str]) -> int:
    """``discovered → planned`` at dispatch (walked/blocked are untouched)."""
    if not branch_ids:
        return 0
    changed = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.branch_id.in_(branch_ids),
                JourneyBranchRow.status == BRANCH_DISCOVERED,
            ))).scalars().all()
        for b in rows:
            b.status = BRANCH_PLANNED
            b.last_status_at = utc_now()
            changed += 1
    return changed


async def reconcile_completion(
    *, tenant_id: str, app_id: str, walk_plan: dict[str, Any],
    terminal_reason: str,
) -> dict[str, int]:
    """After a planned walk's fold: what the walk did NOT upgrade to
    ``walked`` becomes ``blocked`` with its attributed reason — a first-class
    surfaced fact, never a silent retry loop."""
    branch_ids = [str(b) for b in (walk_plan or {}).get("branch_ids") or []]
    if not branch_ids:
        return {"walked": 0, "blocked": 0}
    walked = blocked = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.app_id == app_id,
                JourneyBranchRow.branch_id.in_(branch_ids),
            ))).scalars().all()
        for b in rows:
            if b.status == BRANCH_WALKED:
                walked += 1
                continue
            if b.status == BRANCH_PLANNED:
                b.status = BRANCH_BLOCKED
                b.blocked_reason = (
                    "planned walk completed without reaching this option "
                    f"(walk terminal: {str(terminal_reason or 'unknown')[:60]})"
                )[:400]
                b.last_status_at = utc_now()
                blocked += 1
    logger.warning(
        "qec.branch_planner.reconciled tenant=%s app=%s walked=%d blocked=%d",
        tenant_id, app_id, walked, blocked)
    return {"walked": walked, "blocked": blocked}
