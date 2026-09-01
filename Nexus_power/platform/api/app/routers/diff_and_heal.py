"""Platform API — Demo Diff + Self-Healing routes (P7).

Endpoints:

  POST /api/v1/artifacts/diff
       Body: {baseline_artifact_id, current_artifact_id}
       → Structural diff between two visual evidence graphs +
         which existing scenarios are affected by the change.

  POST /api/v1/e2e-architect/{artifact_id}/scenarios/heal-suggestions
       Body: {scenario_ids?: list, baseline_artifact_id?: str}
       → Per-step heal suggestions. ``baseline_artifact_id`` provides
         baseline control labels for better matching; defaults to using
         only the step's stored ``target_element`` as a fallback.

  POST /api/v1/e2e-architect/{artifact_id}/scenarios/apply-healing
       Body: {suggestions: [...]}  (subset the user chose to apply)
       → Persists the swaps into ``e2e_architect_cache`` and writes
         lifecycle audit entries.  Re-validates every suggestion
         against the live visual graph before applying.

All endpoints are tenant-scoped.  Diff + heal computations are purely
deterministic — no LLM involved.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus_sdk.db.models import CanonicalArtifactRow

from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..database import require_db
from ..services.diff_and_heal import (
    apply_heal_plan_to_scenarios,
    build_heal_plan,
    diff_visual_graphs,
)
from ..services.diff_and_heal.diff import affected_scenarios
from ..services.e2e_lifecycle import transition_scenario

router = APIRouter(tags=["E2E Diff & Heal"])
_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_PLATFORM_BASE_URL = os.environ.get(
    "PLATFORM_API_URL", "http://localhost:8000",
)


# ─── Request models ──────────────────────────────────────────────


class DiffRequest(BaseModel):
    baseline_artifact_id: str = Field(..., min_length=1)
    current_artifact_id: str = Field(..., min_length=1)


class HealSuggestionsRequest(BaseModel):
    scenario_ids: list[str] | None = Field(
        None,
        description=(
            "Optional subset of scenario_ids to scan. When omitted, every "
            "scenario in the artifact's e2e_architect_cache is scanned."
        ),
    )
    baseline_artifact_id: str | None = Field(
        None,
        description=(
            "Optional baseline artifact to look up original control labels "
            "for stale control_ids. When omitted, healing uses the step's "
            "target_element field as the search label."
        ),
    )


class HealSuggestionPayload(BaseModel):
    scenario_id: str
    step_number: int
    old_control_id: str
    new_control_id: str
    new_scene_id: str = ""
    new_label: str = ""
    new_selector: str = ""
    match_kind: str = ""
    match_confidence: float = 0.0


class ApplyHealingRequest(BaseModel):
    suggestions: list[HealSuggestionPayload] = Field(..., min_length=1, max_length=500)


# ─── Helpers ─────────────────────────────────────────────────────


def _make_service_token(tenant_id: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": "platform-diff-and-heal-service",
            "tenant_id": tenant_id,
            "email": "platform-api@internal.nexus",
            "role": "admin",
            "permissions": ["*"],
            "iat": now,
            "exp": now + 3600,
        },
        _config.jwt_secret,
        algorithm=_config.jwt_algorithm,
    )


async def _load_artifact(
    db, *, artifact_id: str, tenant_id: str,
) -> CanonicalArtifactRow:
    result = await db.execute(
        select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"Artifact {artifact_id} not found")
    return row


async def _fetch_visual_graph(
    artifact_id: str, tenant_id: str,
) -> dict:
    """Service-token GET of the visual evidence graph."""
    svc_token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_PLATFORM_BASE_URL}/api/v1/artifacts/{artifact_id}/visual-evidence-graph",
                headers={"Authorization": f"Bearer {svc_token}"},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        _logger.warning("diff_and_heal.graph_fetch_failed artifact=%s: %s", artifact_id, exc)
    return {}


def _scenarios_from_cache(art_row: CanonicalArtifactRow) -> list[dict]:
    """Return the critical_combinations list from the artifact's
    e2e_architect_cache, or an empty list if absent."""
    full_json = art_row.full_artifact_json or {}
    cache = full_json.get("e2e_architect_cache") or {}
    architect = cache.get("e2e_architect") or {}
    return list(architect.get("critical_combinations") or [])


# ─── Endpoints ───────────────────────────────────────────────────


@router.post("/api/v1/artifacts/diff")
async def diff_artifacts(
    req: DiffRequest,
    user: dict = Depends(get_current_user),
):
    """Structural diff between two visual evidence graphs +
    which existing scenarios are affected."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        # Verify tenant owns BOTH artifacts
        await _load_artifact(db, artifact_id=req.baseline_artifact_id, tenant_id=tenant_id)
        current_row = await _load_artifact(
            db, artifact_id=req.current_artifact_id, tenant_id=tenant_id,
        )
        # Pull current scenarios so we can report scenario impact
        current_scenarios = _scenarios_from_cache(current_row)

    # Fetch both graphs in parallel-ish (we could async-gather, but the
    # latency is dominated by DB connect; two sequential GETs is fine).
    baseline_graph = await _fetch_visual_graph(req.baseline_artifact_id, tenant_id)
    current_graph = await _fetch_visual_graph(req.current_artifact_id, tenant_id)

    if not baseline_graph.get("scenes"):
        raise HTTPException(422, detail={
            "error": "baseline_graph_missing",
            "message": (
                f"Baseline artifact {req.baseline_artifact_id} has no visual "
                "evidence graph."
            ),
        })
    if not current_graph.get("scenes"):
        raise HTTPException(422, detail={
            "error": "current_graph_missing",
            "message": (
                f"Current artifact {req.current_artifact_id} has no visual "
                "evidence graph."
            ),
        })

    report = diff_visual_graphs(
        baseline_graph, current_graph,
        baseline_artifact_id=req.baseline_artifact_id,
        current_artifact_id=req.current_artifact_id,
    )
    impacted = affected_scenarios(current_scenarios, report)

    return {
        "success": True,
        "diff": report.to_dict(),
        "affected_scenarios": impacted,
    }


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/scenarios/heal-suggestions",
)
async def heal_suggestions(
    req: HealSuggestionsRequest,
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Build a heal plan for stale evidence_control_ids in this artifact's
    scenarios.  Optional ``baseline_artifact_id`` provides original
    control labels for better matching."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        art_row = await _load_artifact(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        scenarios = _scenarios_from_cache(art_row)
        if req.scenario_ids:
            wanted = set(req.scenario_ids)
            scenarios = [sc for sc in scenarios if sc.get("scenario_id") in wanted]

        baseline_controls_by_id: dict[str, dict] = {}
        if req.baseline_artifact_id:
            # Verify tenant owns the baseline too
            await _load_artifact(
                db,
                artifact_id=req.baseline_artifact_id,
                tenant_id=tenant_id,
            )

    if req.baseline_artifact_id:
        baseline_graph = await _fetch_visual_graph(req.baseline_artifact_id, tenant_id)
        for clist in (baseline_graph.get("controls_by_scene") or {}).values():
            for c in clist:
                baseline_controls_by_id[c["control_id"]] = c

    live_graph = await _fetch_visual_graph(artifact_id, tenant_id)
    if not live_graph.get("scenes"):
        raise HTTPException(422, detail={
            "error": "live_graph_missing",
            "message": "Live visual evidence graph unavailable for this artifact.",
        })

    plan = build_heal_plan(
        scenarios,
        live_graph,
        baseline_controls_by_id=baseline_controls_by_id,
    )
    return {
        "success": True,
        "artifact_id": artifact_id,
        "plan": plan.to_dict(),
    }


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/scenarios/apply-healing",
)
async def apply_healing(
    req: ApplyHealingRequest,
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Persist the user's chosen swaps into the artifact's
    e2e_architect_cache and write lifecycle audit entries."""
    tenant_id = user["tenant_id"]
    user_id = user.get("user_id", "")
    user_email = user.get("email", "")

    # Re-fetch live graph for validation
    live_graph = await _fetch_visual_graph(artifact_id, tenant_id)
    if not live_graph.get("scenes"):
        raise HTTPException(422, detail={
            "error": "live_graph_missing",
            "message": "Live visual evidence graph unavailable; can't validate swaps.",
        })

    factory = require_db()
    async with factory() as db:
        art_row = await _load_artifact(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        existing_json = dict(art_row.full_artifact_json or {})
        cache = dict(existing_json.get("e2e_architect_cache") or {})
        architect = dict(cache.get("e2e_architect") or {})
        scenarios = list(architect.get("critical_combinations") or [])

        result = apply_heal_plan_to_scenarios(
            scenarios,
            [s.model_dump() for s in req.suggestions],
            live_graph=live_graph,
        )

        # Persist mutated scenarios back
        architect["critical_combinations"] = scenarios
        cache["e2e_architect"] = architect
        existing_json["e2e_architect_cache"] = cache
        art_row.full_artifact_json = existing_json
        await db.commit()

        # Append a lifecycle-audit entry per scenario that got at least
        # one applied swap (state stays the same — this is an audit-only
        # transition).
        per_scenario_counts: dict[str, int] = {}
        for entry in result.audit_entries:
            if entry.get("outcome") != "applied":
                continue
            sid = entry.get("scenario_id") or ""
            if sid:
                per_scenario_counts[sid] = per_scenario_counts.get(sid, 0) + 1

        for sid, count in per_scenario_counts.items():
            # Use the scenario's current state for an idempotent self-transition,
            # which appends an audit row without changing the state.
            current_state = next(
                (sc.get("state") for sc in scenarios if sc.get("scenario_id") == sid),
                None,
            )
            if not current_state:
                continue
            try:
                await transition_scenario(
                    db,
                    artifact_id=artifact_id,
                    scenario_id=sid,
                    new_state=current_state,
                    user_id=user_id,
                    user_email=user_email,
                    note=(
                        f"Self-healing applied: swapped {count} stale "
                        "control_id(s) for live-graph replacements."
                    ),
                    tenant_id=tenant_id,
                    session_id="",
                )
            except HTTPException:
                pass  # idempotent self-transition shouldn't fail, but be safe

    return {
        "success": True,
        "artifact_id": artifact_id,
        "result": result.to_dict(),
    }
