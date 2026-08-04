"""Journey Graph API (Release C3/C4/C5).

The surface that answers the only question a business user actually asks —
"did you get all the way through Apply?" — PER PATH, per identity, per env,
per time, with every branch nobody walked shown as a first-class object.

Honesty rules encoded here, not in prose:
  * no percentages anywhere — rollups are counts with expandable path lists;
  * path products are enumerated exactly only up to the configured cap;
    larger spaces are reported as ``"not_enumerated"`` with per-option
    coverage only;
  * ``branch_coverage`` is EARNABLE and derived: true only when every
    enumerated option on the journey's nodes is walked or attributably
    blocked AND at least one completed traversal exists — one ``discovered``
    branch anywhere keeps it false;
  * operator renames win forever (``name_source='operator'``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import (
    JourneyBranchRow,
    JourneyEdgeRow,
    JourneyNodeRow,
    JourneyRow,
    JourneyTraversalRow,
)
from ..db.models import QEExplorationRow
from ..services import branch_planner, journey_fold, journey_naming
from ..services.journey_fold import BRANCH_BLOCKED, BRANCH_DISCOVERED, BRANCH_WALKED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Journeys"])

_MUTATE = require_role("admin", "manager")


class JourneyRename(BaseModel):
    business_name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)


class WalkBranchesIn(BaseModel):
    journey_id: Optional[str] = Field(default=None, max_length=64)


async def _journey_or_404(session, tenant_id: str, app_id: str,
                          journey_id: str) -> JourneyRow:
    row = (await session.execute(
        select(JourneyRow).where(
            JourneyRow.tenant_id == tenant_id,
            JourneyRow.app_id == app_id,
            JourneyRow.journey_id == journey_id,
        ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="journey not found")
    return row


async def _journey_rollup(session, tenant_id: str, app_id: str,
                          journey: JourneyRow) -> dict[str, Any]:
    """Counts + the earnable branch_coverage for one journey. Counts, never
    percentages."""
    traversals = (await session.execute(
        select(JourneyTraversalRow).where(
            JourneyTraversalRow.tenant_id == tenant_id,
            JourneyTraversalRow.app_id == app_id,
            JourneyTraversalRow.journey_id == journey.journey_id,
        ))).scalars().all()
    distinct_paths = {t.path_hash for t in traversals}
    completed_paths = {t.path_hash for t in traversals if t.completed}
    node_fps = sorted({str(fp) for t in traversals for fp in (t.path_fps or [])})
    branches: list[JourneyBranchRow] = []
    if node_fps:
        branches = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.app_id == app_id,
                JourneyBranchRow.node_fp.in_(node_fps),
            ))).scalars().all()
    by_status: dict[str, int] = {}
    for b in branches:
        by_status[b.status] = by_status.get(b.status, 0) + 1
    # EARNABLE, derived, never assertable: one discovered/planned branch
    # anywhere keeps it false; a branch-free journey earns it with one
    # completed path (its one path IS every path).
    all_settled = all(b.status in (BRANCH_WALKED, BRANCH_BLOCKED)
                      for b in branches)
    branch_coverage = bool(completed_paths) and all_settled
    return {
        "journey_id": journey.journey_id,
        "flow_id": journey.flow_id,
        "business_name": journey.business_name,
        "name_source": journey.name_source,
        "description": journey.name_description,
        "entry_title": journey.entry_title,
        "entry_url": journey.entry_url,
        "deepest_steps": journey.deepest_steps,
        "last_proven_at": (journey.last_proven_at.isoformat()
                           if journey.last_proven_at else None),
        "paths_walked": len(distinct_paths),
        "paths_completed": len(completed_paths),
        "branches": {
            "walked": by_status.get(BRANCH_WALKED, 0),
            "discovered": by_status.get(BRANCH_DISCOVERED, 0),
            "planned": by_status.get("planned", 0),
            "blocked": by_status.get(BRANCH_BLOCKED, 0),
        },
        "branch_coverage": branch_coverage,
        "node_fps": node_fps,
    }


def _path_enumeration(branches: list[JourneyBranchRow]) -> dict[str, Any]:
    """The honesty block: the exact path product up to the cap, else
    ``not_enumerated`` — never an extrapolated claim over an uncounted space."""
    per_control: dict[tuple[str, str], int] = {}
    for b in branches:
        key = (b.node_fp, b.control_signature)
        per_control[key] = per_control.get(key, 0) + 1
    product = 1
    for count in per_control.values():
        product *= max(count, 1)
        if product > settings.journey_path_enum_cap:
            return {"enumerated": False,
                    "note": (f"> {settings.journey_path_enum_cap} "
                             "(not enumerated)"),
                    "decision_controls": len(per_control)}
    return {"enumerated": True, "path_product": product,
            "decision_controls": len(per_control)}


@router.get("/apps/{app_id}/journeys")
async def list_journeys(app_id: str, user: dict = Depends(require_auth)) -> dict:
    """The app's journeys, business names first, with per-journey rollups and
    the app-level earnable branch_coverage."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        journeys = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
            ).order_by(JourneyRow.created_at))).scalars().all()
        rollups = []
        for j in journeys:
            r = await _journey_rollup(session, tenant_id, app_id, j)
            r.pop("node_fps", None)
            rollups.append(r)
    return {
        "app_id": app_id,
        "journeys": rollups,
        "journeys_found": len(rollups),
        # App-level claim, earnable only: EVERY journey earned it and at
        # least one exists. One discovered branch anywhere keeps it false.
        "branch_coverage": bool(rollups) and all(
            r["branch_coverage"] for r in rollups),
    }


@router.get("/apps/{app_id}/journeys/{journey_id}")
async def get_journey(app_id: str, journey_id: str,
                      user: dict = Depends(require_auth)) -> dict:
    """One journey's full graph: nodes, edges, traversals (evidence-linked),
    branches — walked AND not — plus the path-enumeration honesty block."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = await _journey_or_404(session, tenant_id, app_id, journey_id)
        rollup = await _journey_rollup(session, tenant_id, app_id, journey)
        node_fps = rollup.pop("node_fps")
        nodes = []
        edges = []
        branches: list[JourneyBranchRow] = []
        if node_fps:
            node_rows = (await session.execute(
                select(JourneyNodeRow).where(
                    JourneyNodeRow.tenant_id == tenant_id,
                    JourneyNodeRow.app_id == app_id,
                    JourneyNodeRow.fingerprint.in_(node_fps),
                ))).scalars().all()
            nodes = [{
                "fingerprint": n.fingerprint, "url": n.url, "title": n.title,
                "is_decision": n.is_decision, "is_boundary": n.is_boundary,
                "has_outcome": n.has_outcome, "stale": n.stale,
                "last_seen_at": n.last_seen_at.isoformat(),
            } for n in node_rows]
            edge_rows = (await session.execute(
                select(JourneyEdgeRow).where(
                    JourneyEdgeRow.tenant_id == tenant_id,
                    JourneyEdgeRow.app_id == app_id,
                    JourneyEdgeRow.from_fp.in_(node_fps),
                ))).scalars().all()
            edges = [{
                "from_fp": e.from_fp, "to_fp": e.to_fp,
                "trigger": e.trigger_label_norm,
                "advance_tier": e.advance_tier, "walk_count": e.walk_count,
                "last_walked_at": e.last_walked_at.isoformat(),
            } for e in edge_rows]
            branches = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                    JourneyBranchRow.node_fp.in_(node_fps),
                ))).scalars().all()
        traversal_rows = (await session.execute(
            select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant_id,
                JourneyTraversalRow.app_id == app_id,
                JourneyTraversalRow.journey_id == journey_id,
            ).order_by(JourneyTraversalRow.created_at))).scalars().all()
    return {
        **rollup,
        "nodes": nodes,
        "edges": edges,
        # ``branches`` (from the rollup) stays the COUNTS; the records live
        # under their own key so neither shadows the other.
        "branch_list": [{
            "branch_id": b.branch_id, "node_fp": b.node_fp,
            "control_signature": b.control_signature,
            "control_label": b.control_label_norm,
            "option_label": b.option_label_norm,
            "status": b.status,
            "blocked_reason": b.blocked_reason,
            "walked_in_traversal": b.walked_in_traversal,
        } for b in branches],
        "traversals": [{
            "traversal_id": t.traversal_id,
            "exploration_id": t.exploration_id,
            "terminal": t.terminal, "completed": t.completed,
            "fully_answered": t.fully_answered,
            "path_fps": list(t.path_fps or []),
            "identity_ref": t.identity_ref, "env_ref": t.env_ref,
            "outcome_values": list(t.outcome_values or []),
            "pre_hardening": t.pre_hardening,
            "walked_at": t.created_at.isoformat(),
        } for t in traversal_rows],
        "path_enumeration": _path_enumeration(branches),
    }


@router.patch("/apps/{app_id}/journeys/{journey_id}")
async def rename_journey(app_id: str, journey_id: str, body: JourneyRename,
                         user: dict = Depends(_MUTATE)) -> dict:
    """Operator rename — permanent. The naming agent can never overwrite it."""
    tenant_id = user["tenant_id"]
    if journey_naming.looks_like_url_text(body.business_name):
        raise HTTPException(
            status_code=422,
            detail="a journey name is business prose — it may not contain "
                   "URLs, paths, or technical identifiers")
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = await _journey_or_404(session, tenant_id, app_id, journey_id)
        journey.business_name = body.business_name.strip()[:60]
        journey.name_description = body.description.strip()[:200]
        journey.name_source = "operator"
        journey.named_by = str(user.get("sub") or user.get("email") or "")[:200]
        journey.updated_at = utc_now()
    return {"journey_id": journey_id,
            "business_name": body.business_name.strip()[:60],
            "name_source": "operator"}


@router.post("/apps/{app_id}/journeys/refold")
async def refold_journeys(app_id: str, user: dict = Depends(_MUTATE)) -> dict:
    """Replay: fold this app's COMPLETED historical crawls into the graph.

    The manifest evidence in each exploration row's stats is the source of
    truth; folding is idempotent, so replay is always safe."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(QEExplorationRow).where(
                QEExplorationRow.tenant_id == tenant_id,
                QEExplorationRow.app_id == app_id,
                QEExplorationRow.status == "completed",
            ).order_by(QEExplorationRow.started_at))).scalars().all()
        candidates = [
            (r.exploration_id,
             (r.stats or {}).get("coverage"),
             ((r.stats or {}).get("walk_plan") or {}).get("identity_ref", ""))
            for r in rows if isinstance(r.stats, dict)
        ]
    folded = []
    for exploration_id, coverage, identity_ref in candidates:
        if not coverage:
            continue
        report = await journey_fold.fold_crawl(
            tenant_id=tenant_id, app_id=app_id,
            exploration_id=exploration_id, coverage=coverage,
            identity_ref=identity_ref)
        folded.append({"exploration_id": exploration_id, **report})
    naming = await journey_naming.name_unnamed_journeys(
        tenant_id=tenant_id, app_id=app_id)
    return {"app_id": app_id, "explorations_folded": len(folded),
            "folds": folded, "naming": naming}


@router.post("/apps/{app_id}/journeys/walk-branches")
async def walk_branches(app_id: str, body: WalkBranchesIn, request: Request,
                        response: Response,
                        user: dict = Depends(_MUTATE)) -> dict:
    """Dispatch planned branch walks for the app's discovered backlog (C4).

    FAIL-CLOSED double gate: the env switch AND the tenant's
    ``branch_walks_enabled`` flag must both be on. Each plan dispatches one
    ordinary E2E crawl whose only difference is the forced enumerated
    choices (``planned`` provenance) — every safety gate unchanged."""
    tenant_id = user["tenant_id"]
    flags = await branch_planner.autonomy_flags(tenant_id)
    if not flags["branch_walks"]:
        raise HTTPException(
            status_code=409,
            detail="branch walks are disabled — enable the tenant flag "
                   "branch_walks_enabled AND the env switch "
                   "QEC_BRANCH_WALKS_ENABLED (both OFF by default)")
    plans = await branch_planner.plan_walks(
        tenant_id=tenant_id, app_id=app_id, journey_id=body.journey_id)
    from .explorations import _dispatch_explorer
    dispatched = []
    for plan in plans:
        await branch_planner.mark_planned(
            tenant_id=tenant_id, branch_ids=plan["branch_ids"])
        try:
            result = await _dispatch_explorer(
                tenant_id=tenant_id, app_id=app_id, request=request,
                response=response, walk_plan=plan)
            dispatched.append({
                "journey_id": plan["journey_id"],
                "business_name": plan["business_name"],
                "branch_ids": plan["branch_ids"],
                "exploration_id": result.get("exploration_id"),
                "crawl_id": result.get("crawl_id"),
            })
        except HTTPException as exc:
            # An undispatchable plan is an honest failure per plan — the
            # branches return to the backlog rather than lying as planned.
            await branch_planner.reconcile_completion(
                tenant_id=tenant_id, app_id=app_id, walk_plan=plan,
                terminal_reason=f"dispatch_failed:{exc.status_code}")
            dispatched.append({
                "journey_id": plan["journey_id"],
                "branch_ids": plan["branch_ids"],
                "error": str(exc.detail)[:300],
            })
    response.status_code = 202 if dispatched else 200
    return {"app_id": app_id, "plans": len(plans), "dispatched": dispatched}
