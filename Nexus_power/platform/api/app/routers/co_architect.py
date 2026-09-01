"""Platform API — Co-Architect routes (P4).

Two endpoints:

  POST /api/v1/e2e-architect/{artifact_id}/co-architect/chat
       Forwards a chat turn to Heart's visual-graph-constrained agent
       and returns the structured response.

  POST /api/v1/e2e-architect/{artifact_id}/co-architect/commit
       Validates a list of agent-proposed scenarios against the live
       visual evidence graph, assigns deterministic ``co_NNN``
       scenario_ids, appends them to the artifact's
       ``e2e_architect_cache`` with ``strategy=co_architect``, and
       creates ``draft`` lifecycle-state rows for them so they show up
       in the Test Studio alongside auto-generated scenarios.

The chat endpoint is stateless — conversation history is sent by the
client every turn. We pick this trade-off so we don't have to commit a
schema migration for chat persistence until/unless usage justifies it.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus_sdk.db.models import CanonicalArtifactRow, E2E_STATE_DRAFT

from ..database import require_db
from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..services.e2e_lifecycle import transition_scenario

router = APIRouter(tags=["E2E Co-Architect"])

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_HEART_ENGINE_URL = os.environ.get(
    "HEART_ENGINE_URL", "http://localhost:8005",
)


# ─── Request models ─────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., min_length=1, max_length=20000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=40)
    propose_scenarios: bool = Field(
        False,
        description=(
            "When True the agent returns structured scenario proposals "
            "(JSON), suitable for the Test Studio's 'Add to Test Plan' "
            "flow. When False it returns plain text."
        ),
    )
    session_id: Optional[str] = None


class ProposedScenarioStep(BaseModel):
    step_number: int
    action: str = Field("", max_length=600)
    input_data: str = Field("", max_length=600)
    expected_output: str = Field("", max_length=2000)
    evidence_scene_id: str = Field(..., min_length=1)
    evidence_control_id: str = Field(..., min_length=1)
    evidence_edge_id: str = Field("")
    proof_confidence: float = Field(0.0, ge=0.0, le=1.0)


class ProposedScenario(BaseModel):
    title: str = Field(..., min_length=1, max_length=400)
    rationale: str = Field("", max_length=2000)
    strategy: str = Field("co_architect", max_length=64)
    steps: list[ProposedScenarioStep] = Field(..., min_length=1, max_length=40)


class CommitProposalsRequest(BaseModel):
    proposed_scenarios: list[ProposedScenario] = Field(
        ..., min_length=1, max_length=20,
    )


# ─── Helpers ────────────────────────────────────────────────────


def _make_service_token(tenant_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": "platform-co-architect-service",
        "tenant_id": tenant_id,
        "email": "platform-api@internal.nexus",
        "role": "admin",
        "permissions": ["*"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, _config.jwt_secret, algorithm=_config.jwt_algorithm)


async def _load_artifact_for_tenant(
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


def _resolve_id_by_prefix(prefix: str, valid_ids: set[str]) -> str | None:
    """The agent often references IDs by their 8-char prefix because that's
    how we render them in the graph context.  Expand back to the full ID
    when there's exactly one match; otherwise return None (caller treats
    that as an ungrounded step and drops it)."""
    prefix = (prefix or "").strip()
    if not prefix:
        return None
    if prefix in valid_ids:
        return prefix
    matches = [vid for vid in valid_ids if vid.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


# ─── Endpoints ──────────────────────────────────────────────────


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/co-architect/chat",
)
async def co_architect_chat(
    req: ChatRequest,
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Forward a chat turn to Heart's Co-Architect endpoint, scoped to
    this tenant's artifact."""
    tenant_id = user["tenant_id"]

    factory = require_db()
    async with factory() as db:
        await _load_artifact_for_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )

    svc_token = _make_service_token(tenant_id)
    payload = {
        "artifact_id": artifact_id,
        "session_id": req.session_id or "",
        "messages": [m.model_dump() for m in req.messages],
        "propose_scenarios": req.propose_scenarios,
        "evidence_mode": "visual_strict",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{_HEART_ENGINE_URL}/api/v1/heart/co-architect/chat",
                json=payload,
                headers={"Authorization": f"Bearer {svc_token}"},
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Heart engine timed out during Co-Architect chat")
    except httpx.ConnectError:
        raise HTTPException(503, "Heart engine is unreachable")

    if resp.status_code >= 400:
        # Bubble up Heart's structured error detail
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        status = resp.status_code if resp.status_code in (502, 503, 504) else 502
        raise HTTPException(status, detail)

    return resp.json()


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/co-architect/commit",
)
async def commit_proposed_scenarios(
    req: CommitProposalsRequest,
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Re-validate agent-proposed scenarios against the live visual graph
    and append them to the artifact's e2e_architect_cache.

    Returns:
        {success, committed: list[{scenario_id, title, step_count}],
         dropped: list[{title, reason}]}
    """
    tenant_id = user["tenant_id"]
    user_id = user.get("user_id", "")
    user_email = user.get("email", "")

    # ── 1. Load artifact + cache + verify tenant
    factory = require_db()
    async with factory() as db:
        art_row = await _load_artifact_for_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        full_json = dict(art_row.full_artifact_json or {})
        cache = full_json.get("e2e_architect_cache") or {}
        if not cache or "e2e_architect" not in cache:
            raise HTTPException(
                422,
                "No e2e_architect_cache present. Run the Test Studio "
                "generation flow first, then commit Co-Architect proposals.",
            )

    # ── 2. Fetch visual graph to validate evidence IDs
    svc_token = _make_service_token(tenant_id)
    graph: dict = {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            g_resp = await client.get(
                f"{os.environ.get('PLATFORM_API_URL', 'http://localhost:8000')}"
                f"/api/v1/artifacts/{artifact_id}/visual-evidence-graph",
                headers={"Authorization": f"Bearer {svc_token}"},
            )
            if g_resp.status_code == 200:
                graph = g_resp.json()
    except Exception as exc:
        _logger.warning("co_architect.commit: graph fetch failed: %s", exc)

    if not (graph.get("scenes") or []):
        raise HTTPException(
            422,
            "Visual evidence graph is unavailable; cannot validate proposed "
            "scenarios.",
        )

    valid_scene_ids = {s["scene_id"] for s in (graph.get("scenes") or [])}
    valid_control_ids = {
        c["control_id"]
        for clist in (graph.get("controls_by_scene") or {}).values()
        for c in clist
    }
    valid_edge_ids = {e["edge_id"] for e in (graph.get("edges") or [])}

    # ── 3. Validate every proposed step, expanding 8-char prefixes back to
    # full UUIDs.  Drop steps that can't be re-grounded.
    architect_block = cache.get("e2e_architect") or {}
    existing_scenarios = list(architect_block.get("critical_combinations") or [])
    # Find the next available co_NNN id
    existing_co_ids = [
        sc.get("scenario_id", "") for sc in existing_scenarios
        if (sc.get("scenario_id") or "").startswith("co_")
    ]
    next_co_num = 1 + max(
        (int(s.split("_")[1]) for s in existing_co_ids if s.split("_")[1].isdigit()),
        default=0,
    )

    committed: list[dict] = []
    dropped: list[dict] = []
    new_scenarios: list[dict] = []

    for proposal in req.proposed_scenarios:
        grounded_steps: list[dict] = []
        ungrounded_count = 0
        for raw_step in proposal.steps:
            full_scene = _resolve_id_by_prefix(
                raw_step.evidence_scene_id, valid_scene_ids,
            )
            full_ctrl = _resolve_id_by_prefix(
                raw_step.evidence_control_id, valid_control_ids,
            )
            full_edge = (
                _resolve_id_by_prefix(raw_step.evidence_edge_id, valid_edge_ids)
                if raw_step.evidence_edge_id else ""
            ) or ""
            if not full_scene or not full_ctrl:
                ungrounded_count += 1
                continue
            grounded_steps.append({
                "step_number": raw_step.step_number,
                "action": raw_step.action,
                "input_data": raw_step.input_data,
                "expected_behavior": raw_step.expected_output,
                "target_element": raw_step.action,
                "evidence_scene_id": full_scene,
                "evidence_control_id": full_ctrl,
                "evidence_edge_id": full_edge,
                "proof_confidence": raw_step.proof_confidence,
            })

        if not grounded_steps:
            dropped.append({
                "title": proposal.title,
                "reason": (
                    f"All {len(proposal.steps)} step(s) failed re-grounding "
                    "against the live visual graph."
                ),
            })
            continue

        scenario_id = f"co_{next_co_num:03d}"
        next_co_num += 1

        evidence_sources: list[dict] = []
        seen_scenes: set[str] = set()
        for step in grounded_steps:
            sid = step["evidence_scene_id"]
            if sid in seen_scenes:
                continue
            seen_scenes.add(sid)
            evidence_sources.append({
                "text": f"Scene {sid[:8]}",
                "source_modality": "visual",
                "confidence": float(step.get("proof_confidence") or 0.0),
                "scene_id": sid,
                "control_id": step["evidence_control_id"],
                "edge_id": step.get("evidence_edge_id") or "",
            })

        new_scenarios.append({
            "scenario_id": scenario_id,
            "title": proposal.title,
            "category": "observed",
            "priority": "P2_medium",
            "rationale": (
                proposal.rationale
                or f"Proposed by Co-Architect — {len(grounded_steps)} of "
                   f"{len(proposal.steps)} step(s) grounded in scene + control."
            ),
            "evidence_sources": evidence_sources,
            "preconditions": [],
            "steps": grounded_steps,
            "expected_outcome": "",
            "data_matrix": [],
            "workflow_steps_covered": [],
            "risk_areas_addressed": [],
            "strategy": "co_architect",
            "visual_proven_steps": len(grounded_steps),
            "visual_total_steps": len(grounded_steps),
        })
        committed.append({
            "scenario_id": scenario_id,
            "title": proposal.title,
            "step_count": len(grounded_steps),
            "dropped_steps": ungrounded_count,
        })

    if not new_scenarios:
        raise HTTPException(422, detail={
            "error": "all_proposals_ungrounded",
            "message": (
                "None of the proposed scenarios could be re-grounded against "
                "the live visual graph. Likely the LLM cited IDs that no "
                "longer exist."
            ),
            "dropped": dropped,
        })

    # ── 4. Persist back to the cache
    async with factory() as db:
        result = await db.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        existing_json = dict(row.full_artifact_json or {})
        existing_cache = dict(existing_json.get("e2e_architect_cache") or {})
        existing_architect = dict(existing_cache.get("e2e_architect") or {})
        existing_scenarios = list(existing_architect.get("critical_combinations") or [])
        existing_scenarios.extend(new_scenarios)
        existing_architect["critical_combinations"] = existing_scenarios

        # Update by_strategy + by_category breakdowns
        coverage = dict(existing_architect.get("coverage_analysis") or {})
        by_strategy = dict(coverage.get("by_strategy") or {})
        by_strategy["co_architect"] = (
            by_strategy.get("co_architect", 0) + len(new_scenarios)
        )
        coverage["by_strategy"] = by_strategy
        by_category = dict(coverage.get("by_category") or {})
        by_category["observed"] = by_category.get("observed", 0) + len(new_scenarios)
        coverage["by_category"] = by_category
        coverage["total_scenarios"] = (
            (coverage.get("total_scenarios") or 0) + len(new_scenarios)
        )
        existing_architect["coverage_analysis"] = coverage

        existing_cache["e2e_architect"] = existing_architect
        existing_json["e2e_architect_cache"] = existing_cache
        row.full_artifact_json = existing_json
        await db.commit()

    # ── 5. Stamp each new scenario into the lifecycle table in draft state
    # so it shows up immediately in the Test Studio with audit metadata.
    async with factory() as db:
        for entry in committed:
            try:
                await transition_scenario(
                    db,
                    artifact_id=artifact_id,
                    scenario_id=entry["scenario_id"],
                    new_state=E2E_STATE_DRAFT,
                    user_id=user_id,
                    user_email=user_email,
                    note=(
                        "Proposed by Co-Architect; awaiting review."
                    ),
                    tenant_id=tenant_id,
                    session_id="",
                )
            except HTTPException:
                # Transitioning a freshly-created row to its own state is
                # idempotent; ignore if validation rejects it.
                pass

    return {
        "success": True,
        "artifact_id": artifact_id,
        "committed": committed,
        "dropped": dropped,
    }


# ─── Inline test-case edit (ADDITIVE 2026-06-21; test-gen stage, outside the
# frozen visual-evidence boundary) ───────────────────────────────────────────
# Edit the human-editable fields of a stored scenario. The grounding each step
# cites (evidence_scene_id / evidence_control_id) is IMMUTABLE here, so a data
# edit can never fabricate evidence or un-ground a step. Persists to the same
# critical_combinations the Co-Architect writes; signals the caller to
# regenerate the Playwright (v+1).
class EditStepPatch(BaseModel):
    step_index: int = Field(..., ge=0)
    action: Optional[str] = Field(None, max_length=600)
    input_data: Optional[str] = Field(None, max_length=600)
    expected_output: Optional[str] = Field(None, max_length=2000)


class EditScenarioRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=400)
    steps: list[EditStepPatch] = Field(default_factory=list, max_length=40)


@router.patch("/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}")
async def edit_scenario(
    artifact_id: str = Path(..., min_length=1, max_length=64),
    scenario_id: str = Path(..., min_length=1, max_length=128),
    req: EditScenarioRequest = Body(...),
    user: dict = Depends(get_current_user),
) -> dict:
    """Inline-edit a test-case scenario's data fields (human edit).

    Only action text / input value / expected result are editable; the
    grounding each step targets stays locked.  Returns the updated scenario
    summary and ``regenerate_playwright=True`` so the caller refreshes the
    runnable script.
    """
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await _load_artifact_for_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        existing_json = dict(row.full_artifact_json or {})
        cache = dict(existing_json.get("e2e_architect_cache") or {})
        architect = dict(cache.get("e2e_architect") or {})
        scenarios = list(architect.get("critical_combinations") or [])

        idx = next(
            (i for i, s in enumerate(scenarios)
             if str(s.get("scenario_id")) == scenario_id),
            None,
        )
        if idx is None:
            raise HTTPException(
                404, f"Scenario {scenario_id} not found in this artifact",
            )
        scenario = dict(scenarios[idx])

        if req.title is not None:
            new_title = req.title.strip()
            if new_title:
                scenario["title"] = new_title

        steps = [dict(s) for s in (scenario.get("steps") or [])]
        for patch in req.steps:
            if patch.step_index >= len(steps):
                raise HTTPException(
                    422,
                    f"step_index {patch.step_index} out of range "
                    f"(0..{max(len(steps) - 1, 0)})",
                )
            step = steps[patch.step_index]
            if patch.action is not None:
                step["action"] = patch.action[:600]
            if patch.input_data is not None:
                step["input_data"] = patch.input_data[:600]
            if patch.expected_output is not None:
                step["expected_output"] = patch.expected_output[:2000]
            # evidence_scene_id / evidence_control_id intentionally NOT touched.
        scenario["steps"] = steps
        scenario["human_edited"] = True

        scenarios[idx] = scenario
        architect["critical_combinations"] = scenarios
        cache["e2e_architect"] = architect
        existing_json["e2e_architect_cache"] = cache
        row.full_artifact_json = existing_json
        await db.commit()

    return {
        "success": True,
        "scenario_id": scenario_id,
        "title": scenario.get("title", ""),
        "step_count": len(scenario.get("steps") or []),
        "human_edited": True,
        "regenerate_playwright": True,
    }
