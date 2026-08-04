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

import asyncio
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
from ..db.journey_run_models import JourneyCaseRow, JourneyRunRow
from ..db.models import ClientAppRow, QEExplorationRow
from ..services import (
    branch_planner,
    journey_case_linker,
    journey_fold,
    journey_naming,
    journey_runner,
)
from ..services.journey_fold import BRANCH_BLOCKED, BRANCH_DISCOVERED, BRANCH_WALKED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Journeys"])

_MUTATE = require_role("admin", "manager")


class JourneyRename(BaseModel):
    business_name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)


class WalkBranchesIn(BaseModel):
    journey_id: Optional[str] = Field(default=None, max_length=64)


async def _latest_artifact(session, tenant_id: str, app_id: str) -> str:
    """The app's promoted crawl artifact — the one whose cases are current."""
    return str((await session.execute(
        select(ClientAppRow.latest_artifact_id).where(
            ClientAppRow.tenant_id == tenant_id,
            ClientAppRow.app_id == app_id,
        ))).scalar_one_or_none() or "")


def _run_view(run: JourneyRunRow | None) -> dict | None:
    """The RUN-proof line — a distinct fact beside the crawl-proof, never
    merged with it."""
    if run is None:
        return None
    return {
        "journey_run_id": run.journey_run_id,
        "status": run.status,
        "blocked_reason": run.blocked_reason,
        "live_url": run.live_url,
        "dispatch_run_id": run.dispatch_run_id,
        "ingested_run_id": run.ingested_run_id,
        "artifact_id": run.artifact_id,
        "test_case_id": run.test_case_id,
        "env_ref": run.env_ref,
        "verdict_summary": dict(run.verdict_summary or {}),
        "started_at": run.started_at.isoformat(),
        "finished_at": (run.finished_at.isoformat()
                        if run.finished_at else None),
    }


async def _runnable_view(
    session, *, tenant_id: str, app_id: str, journey_id: str,
    artifact_id: str, paths_completed: int,
) -> dict:
    """{ok, reason, test_case_id, display_name} — the honest runnability
    verdict. A truncated journey never gets a script that pretends to cover
    a path the crawl did not finish."""
    if paths_completed <= 0:
        return {"ok": False, "test_case_id": "", "display_name": "",
                "reason": ("no completed walk yet — re-crawl to prove the "
                           "path before it can be executed")}
    if not artifact_id:
        return {"ok": False, "test_case_id": "", "display_name": "",
                "reason": "no crawl artifact promoted for this app yet"}
    adopted = await journey_case_linker.runnable_case(
        session, tenant_id=tenant_id, app_id=app_id,
        journey_id=journey_id, artifact_id=artifact_id)
    if adopted is None:
        return {"ok": False, "test_case_id": "", "display_name": "",
                "reason": ("no end-to-end case spans this journey's walked "
                           "path on the current crawl artifact — re-crawl "
                           "to regenerate its cases")}
    return {"ok": True, "test_case_id": adopted.test_case_id,
            "display_name": adopted.display_name or adopted.case_name,
            "reason": ""}


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
    """The app's journeys, business names first, with per-journey rollups,
    the earnable branch_coverage, runnability, and the latest RUN proof
    (counts only, never percentages)."""
    tenant_id = user["tenant_id"]
    # Read-time safety net: a restart must not strand 'running' rows forever.
    try:
        await journey_runner.reconcile_stale(tenant_id=tenant_id, app_id=app_id)
    except Exception as exc:
        logger.warning("qec.journeys.reconcile_failed app=%s err=%s",
                       app_id, str(exc)[:160])
    async with tenant_scoped_qec_session(tenant_id) as session:
        artifact_id = await _latest_artifact(session, tenant_id, app_id)
        journeys = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
            ).order_by(JourneyRow.created_at))).scalars().all()
        rollups = []
        run_rollup = {"runnable": 0, "run_green": 0, "run_red": 0,
                      "never_run": 0}
        for j in journeys:
            r = await _journey_rollup(session, tenant_id, app_id, j)
            r.pop("node_fps", None)
            runnable = await _runnable_view(
                session, tenant_id=tenant_id, app_id=app_id,
                journey_id=j.journey_id, artifact_id=artifact_id,
                paths_completed=r["paths_completed"])
            last = await journey_runner.latest_run(
                session, tenant_id=tenant_id, app_id=app_id,
                journey_id=j.journey_id)
            r["runnable"] = runnable
            r["last_run"] = _run_view(last)
            if runnable["ok"]:
                run_rollup["runnable"] += 1
            if last is None:
                run_rollup["never_run"] += 1
            elif last.status == "passed":
                run_rollup["run_green"] += 1
            elif last.status in ("failed", "timed_out", "error", "blocked"):
                run_rollup["run_red"] += 1
            rollups.append(r)
    return {
        "app_id": app_id,
        "artifact_id": artifact_id,
        "journeys": rollups,
        "journeys_found": len(rollups),
        "runs": run_rollup,
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
        # Runnable Journeys (Release D): the case links for the CURRENT
        # artifact, the runnability verdict, and the run ledger.
        artifact_id = await _latest_artifact(session, tenant_id, app_id)
        case_rows = []
        if artifact_id:
            case_rows = (await session.execute(
                select(JourneyCaseRow).where(
                    JourneyCaseRow.tenant_id == tenant_id,
                    JourneyCaseRow.app_id == app_id,
                    JourneyCaseRow.journey_id == journey_id,
                    JourneyCaseRow.artifact_id == artifact_id,
                ).order_by(JourneyCaseRow.kind,  # 'journey_e2e' sorts first
                           JourneyCaseRow.coverage_score.desc()))
            ).scalars().all()
        runnable = await _runnable_view(
            session, tenant_id=tenant_id, app_id=app_id,
            journey_id=journey_id, artifact_id=artifact_id,
            paths_completed=rollup["paths_completed"])
        run_rows = (await session.execute(
            select(JourneyRunRow).where(
                JourneyRunRow.tenant_id == tenant_id,
                JourneyRunRow.app_id == app_id,
                JourneyRunRow.journey_id == journey_id,
            ).order_by(JourneyRunRow.started_at.desc())
            .limit(10))).scalars().all()
    # THE JOURNEY, READ AS A JOURNEY: its longest proven walk rendered as
    # numbered steps — the page reached, and the control that carried the
    # walk onward. Node/edge lists are the graph; THIS is the story.
    node_by_fp = {n["fingerprint"]: n for n in nodes}
    edge_by_pair = {(e["from_fp"], e["to_fp"]): e for e in edges}
    best = None
    for t in traversal_rows:
        if best is None or (
            (t.completed, len(t.path_fps or [])) >
            (best.completed, len(best.path_fps or []))
        ):
            best = t
    steps: list[dict] = []
    if best is not None:
        fps = [str(fp) for fp in (best.path_fps or [])]
        for i, fp in enumerate(fps):
            node = node_by_fp.get(fp, {})
            edge = edge_by_pair.get((fp, fps[i + 1])) if i + 1 < len(fps) else None
            steps.append({
                "step": i + 1,
                "fingerprint": fp,
                "title": node.get("title") or "",
                "url": node.get("url") or "",
                "is_decision": bool(node.get("is_decision")),
                "is_boundary": bool(node.get("is_boundary")),
                "has_outcome": bool(node.get("has_outcome")),
                "advanced_by": (edge or {}).get("trigger") or "",
                "advance_tier": (edge or {}).get("advance_tier") or 0,
            })
    return {
        **rollup,
        "artifact_id": artifact_id,
        "steps": steps,
        "steps_terminal": (best.terminal if best is not None else ""),
        "steps_completed_walk": bool(best.completed) if best is not None else False,
        "cases": [{
            "test_case_id": c.test_case_id,
            "name": c.case_name,
            "display_name": c.display_name or c.case_name,
            "kind": c.kind,
            "coverage_score": c.coverage_score,
        } for c in case_rows],
        "runnable": runnable,
        "runs": [_run_view(r) for r in run_rows],
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
    # Re-derive the journey⇄case links too: an operator who re-folds expects
    # the runnable forms to match what the graph now says.
    async with tenant_scoped_qec_session(tenant_id) as session:
        artifact_id = await _latest_artifact(session, tenant_id, app_id)
    cases = await journey_case_linker.link_app_journeys(
        tenant_id=tenant_id, app_id=app_id, artifact_id=artifact_id)
    return {"app_id": app_id, "explorations_folded": len(folded),
            "folds": folded, "naming": naming, "cases": cases}


async def _dispatch_one_journey(
    session, *, tenant_id: str, app_id: str, journey: JourneyRow,
    artifact_id: str,
) -> dict:
    """Shared dispatch core for run + run-all: resolve runnability, refuse
    a double-dispatch, execute through the existing runner path."""
    rollup = await _journey_rollup(session, tenant_id, app_id, journey)
    runnable = await _runnable_view(
        session, tenant_id=tenant_id, app_id=app_id,
        journey_id=journey.journey_id, artifact_id=artifact_id,
        paths_completed=rollup["paths_completed"])
    if not runnable["ok"]:
        return {"journey_id": journey.journey_id,
                "business_name": journey.business_name,
                "dispatched": False, "reason": runnable["reason"]}
    # The runner is SINGLE-FLIGHT: a concurrent dispatch is refused 409 by
    # nexus-runner, which would land an 'error' row that says nothing about
    # the journey. Refuse up front with the honest reason instead.
    in_flight = (await session.execute(
        select(JourneyRunRow.journey_id).where(
            JourneyRunRow.tenant_id == tenant_id,
            JourneyRunRow.app_id == app_id,
            JourneyRunRow.status.in_(["dispatched", "running"]),
        ).limit(1))).scalar_one_or_none()
    if in_flight:
        same = in_flight == journey.journey_id
        return {"journey_id": journey.journey_id,
                "business_name": journey.business_name,
                "dispatched": False,
                "reason": ("a run for this journey is already in flight"
                           if same else
                           "another journey run is already executing — the "
                           "runner takes one run at a time; try again when "
                           "it finishes")}
    # v1 env note: the run executes against the artifact's own target (the
    # same default a plain Playwright-tab run uses); the bound run-environment
    # NAME rides along as display metadata.
    env_ref = str(((await session.execute(
        select(ClientAppRow.schedule).where(
            ClientAppRow.tenant_id == tenant_id,
            ClientAppRow.app_id == app_id,
        ))).scalar_one_or_none() or {}).get("run_environment") or "")
    result = await journey_runner.dispatch_journey_run(
        tenant_id=tenant_id, app_id=app_id, journey_id=journey.journey_id,
        artifact_id=artifact_id, test_case_id=runnable["test_case_id"],
        env_ref=env_ref)
    return {"journey_id": journey.journey_id,
            "business_name": journey.business_name,
            "dispatched": result["status"] == "running",
            "journey_run_id": result["journey_run_id"],
            "status": result["status"],
            "live_url": result.get("live_url", ""),
            "reason": result["blocked_reason"]}


@router.post("/apps/{app_id}/journeys/{journey_id}/run")
async def run_journey(app_id: str, journey_id: str, response: Response,
                      user: dict = Depends(_MUTATE)) -> dict:
    """Execute the journey's adopted end-to-end case through the EXISTING
    factory → runner path (heal, attribution, evidence, certificates — all
    unchanged). The verdict folds back onto the journey as the RUN proof,
    beside (never merged with) the crawl proof."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = await _journey_or_404(session, tenant_id, app_id, journey_id)
        artifact_id = await _latest_artifact(session, tenant_id, app_id)
        result = await _dispatch_one_journey(
            session, tenant_id=tenant_id, app_id=app_id, journey=journey,
            artifact_id=artifact_id)
    if not result["dispatched"] and "in flight" in (result.get("reason") or ""):
        raise HTTPException(status_code=409, detail=result["reason"])
    if not result["dispatched"] and not result.get("journey_run_id"):
        raise HTTPException(status_code=409, detail=result["reason"])
    response.status_code = 202
    return result


@router.get("/apps/{app_id}/journeys/{journey_id}/run-progress")
async def journey_run_progress(app_id: str, journey_id: str,
                               user: dict = Depends(require_auth)) -> dict:
    """Watchable progress for the journey's latest run: the live viewer
    address while it executes, live step counters from the runner, and the
    honest terminal status once it ends."""
    tenant_id = user["tenant_id"]
    return await journey_runner.live_progress(
        tenant_id=tenant_id, app_id=app_id, journey_id=journey_id)


@router.post("/apps/{app_id}/journeys/run-all")
async def run_all_journeys(app_id: str, response: Response,
                           user: dict = Depends(_MUTATE)) -> dict:
    """Prove all journeys: run every RUNNABLE journey's end-to-end case.

    The runner takes ONE run at a time, so the batch is QUEUED, not fired at
    once — a serial worker dispatches the next journey when the previous one
    finishes. Per-journey honest statuses; no aggregate green."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        artifact_id = await _latest_artifact(session, tenant_id, app_id)
        journeys = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
            ).order_by(JourneyRow.created_at))).scalars().all()
        plan = []
        for journey in journeys:
            rollup = await _journey_rollup(session, tenant_id, app_id, journey)
            runnable = await _runnable_view(
                session, tenant_id=tenant_id, app_id=app_id,
                journey_id=journey.journey_id, artifact_id=artifact_id,
                paths_completed=rollup["paths_completed"])
            plan.append({"journey_id": journey.journey_id,
                         "business_name": journey.business_name,
                         "test_case_id": runnable["test_case_id"],
                         "queued": runnable["ok"],
                         "reason": runnable["reason"]})
    queued = [p for p in plan if p["queued"]]
    if queued:
        asyncio.get_running_loop().create_task(_run_queue(
            tenant_id=tenant_id, app_id=app_id, artifact_id=artifact_id,
            plan=queued))
    response.status_code = 202 if queued else 200
    return {"app_id": app_id, "journeys": len(plan),
            "queued": len(queued), "results": plan}


async def _run_queue(*, tenant_id: str, app_id: str, artifact_id: str,
                     plan: list[dict]) -> None:
    """Serial batch worker: one journey run at a time (the runner is
    single-flight), each fully polled and folded back before the next
    starts. Never raises — a failure on one journey never strands the rest."""
    for item in plan:
        try:
            while await journey_runner.any_run_in_flight(tenant_id=tenant_id):
                await asyncio.sleep(settings.journey_run_poll_s)
            await journey_runner.dispatch_journey_run(
                tenant_id=tenant_id, app_id=app_id,
                journey_id=item["journey_id"], artifact_id=artifact_id,
                test_case_id=item["test_case_id"])
        except Exception as exc:
            logger.warning("qec.journeys.run_queue_failed journey=%s err=%s",
                           item["journey_id"], str(exc)[:200])


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
