"""
Platform API — E2E Test Architect routes.

Generates critical end-to-end test scenarios from multimodal evidence
(visual + transcript) via a two-pass Brain LLM strategy.

Platform owns: artifact lookup, persona/test-strategy cache retrieval,
               multimodal payload assembly, provenance, persistence.
Brain owns:    LLM analysis — variable extraction (pass 1) and
               scenario generation (pass 2).

This route is INDEPENDENT from test_strategy.py and personas.py.
It reads their caches (read-only) but never writes to them.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from typing import Optional

from nexus_sdk.db.models import CanonicalArtifactRow

from ..database import require_db, row_to_dict
from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..services.multimodal import build_e2e_brain_payload
from ..services.e2e_lifecycle import (
    fetch_scenario_states_map,
    merge_states_into_scenarios,
)
from ..services.test_runs import last_run_summary_by_scenario

router = APIRouter(tags=["E2E Architect"])

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_BRAIN_ENGINE_URL = os.environ.get("BRAIN_ENGINE_URL", _config.brain_engine_url)
_HEART_ENGINE_URL = os.environ.get("HEART_ENGINE_URL", "http://localhost:8005")


# ─── Request / Response Models ──────────────────────────────

class GenerateE2EArchitectRequest(BaseModel):
    """Request to generate E2E Architect scenarios from a canonical artifact."""
    artifact_id: str = Field(..., description="Canonical artifact ID (must have persona draft cached)")
    session_id: Optional[str] = Field(None, description="Session context (optional)")
    force_regenerate: bool = Field(False, description="Skip cache and regenerate from scratch")
    evidence_mode: str = Field(
        "multimodal",
        description=(
            "'multimodal' (default): existing Brain LLM path using persona + transcript + visual. "
            "'visual_strict': fetches visual-evidence-graph, calls Heart engine, skips "
            "match_visual_to_workflows() and time_align_scenes() entirely."
        ),
    )
    # P2: which generation strategies to run in visual_strict mode.
    # Default is "all 7" so a single demo recording yields the richest
    # strategically-diverse test suite.
    strategies: Optional[list[str]] = Field(
        None,
        description=(
            "Visual-strict only. Subset of "
            "['happy_path', 'variant', 'negative', 'boundary', "
            "'state_explorer', 'cross_app', 'error_state']. "
            "If unset, all seven strategies are requested. "
            "LLM-driven: happy_path, variant, negative, boundary. "
            "Deterministic (no LLM): state_explorer, cross_app, error_state."
        ),
    )


# ─── Helpers ───────────────────────────────────────────────────


async def _merge_state_into_response(
    response_data: dict, *, artifact_id: str, tenant_id: str,
) -> dict:
    """Attach P3 lifecycle state + P6 last-run summary to each scenario.

    Opens its own DB session so it can be safely called from any return
    path. Also computes by_state and by_last_run counts for the UI's
    filter chips.
    """
    scenarios = (
        response_data.get("e2e_architect", {}).get("critical_combinations") or []
    )
    if not scenarios:
        return response_data

    factory = require_db()
    async with factory() as db:
        states_map = await fetch_scenario_states_map(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        run_summary_map = await last_run_summary_by_scenario(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )

    merge_states_into_scenarios(scenarios, states_map=states_map)

    # P6: attach last-run summary to each scenario. Scenarios that have
    # never run keep last_run_status=None — the UI renders that as
    # "Never run" rather than as a failure.
    for sc in scenarios:
        sid = sc.get("scenario_id")
        run_summary = run_summary_map.get(sid) if sid else None
        if run_summary:
            sc["last_run"] = run_summary
        else:
            sc["last_run"] = None

    by_state: dict[str, int] = {}
    by_last_run: dict[str, int] = {}
    flaky_count = 0
    failing_count = 0
    for sc in scenarios:
        st = sc.get("state") or "draft"
        by_state[st] = by_state.get(st, 0) + 1
        run = sc.get("last_run")
        last_status = (run or {}).get("last_run_status") if run else None
        run_key = last_status or "never_run"
        by_last_run[run_key] = by_last_run.get(run_key, 0) + 1
        if run and run.get("is_flaky"):
            flaky_count += 1
        if last_status == "failed":
            failing_count += 1

    coverage = response_data.get("e2e_architect", {}).get("coverage_analysis", {})
    coverage["by_state"] = by_state
    coverage["by_last_run"] = by_last_run
    coverage["flaky_scenarios"] = flaky_count
    coverage["failing_scenarios"] = failing_count
    return response_data


def _make_service_token(tenant_id: str) -> str:
    """Create a short-lived JWT for service-to-service calls."""
    now = int(time.time())
    payload = {
        "sub": "platform-api-service",
        "tenant_id": tenant_id,
        "email": "platform-api@internal.nexus",
        "role": "admin",
        "permissions": ["*"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, _config.jwt_secret, algorithm=_config.jwt_algorithm)


# ─── Endpoint ──────────────────────────────────────────────────

@router.post("/api/v1/e2e-architect/generate")
async def generate_e2e_architect(
    req: GenerateE2EArchitectRequest,
    user: dict = Depends(get_current_user),
):
    """Generate critical E2E test scenarios from multimodal evidence.

    Platform owns:
      - Artifact + persona/test-strategy cache retrieval (read-only)
      - Multimodal payload assembly via build_e2e_brain_payload()
      - Provenance enrichment
      - Result caching (e2e_architect_cache)

    Brain owns:
      - Two-pass LLM analysis (variable extraction → scenario generation)
      - Evidence-grounded scenario scoring
    """
    start = time.monotonic()
    tenant_id = user["tenant_id"]

    # ── 1. Load artifact from DB (tenant-scoped) ──────────
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == req.artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(404, f"Artifact {req.artifact_id} not found")

        artifact = row_to_dict(row)

        # ── 1b. Load persona draft for lineage check ──
        full_json = artifact.get("full_artifact_json") or {}
        persona_cache = full_json.get("persona_draft_cache")
        persona_generated_at = ""
        if persona_cache and isinstance(persona_cache, dict):
            persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

        # ── 1c. Check for cached E2E Architect (with lineage + mode validation) ──
        if not req.force_regenerate:
            cached = full_json.get("e2e_architect_cache")
            if cached and isinstance(cached, dict) and cached.get("e2e_architect"):
                cached_source = cached.get("provenance", {}).get("source_persona_generated_at", "")
                cached_mode = cached.get("provenance", {}).get("evidence_mode") or "multimodal"
                if cached_mode != req.evidence_mode:
                    _logger.info(
                        "Invalidating cross-mode E2E Architect cache for artifact=%s "
                        "(cached_mode=%s, requested=%s)",
                        req.artifact_id, cached_mode, req.evidence_mode,
                    )
                elif not cached_source and persona_generated_at and req.evidence_mode != "visual_strict":
                    _logger.info(
                        "Invalidating pre-lineage E2E Architect cache for artifact=%s "
                        "(no source_persona_generated_at, persona has %s)",
                        req.artifact_id, persona_generated_at,
                    )
                elif cached_source and persona_generated_at and cached_source != persona_generated_at:
                    _logger.info(
                        "Invalidating stale E2E Architect cache for artifact=%s "
                        "(persona regenerated: cached_source=%s != current=%s)",
                        req.artifact_id, cached_source, persona_generated_at,
                    )
                else:
                    _logger.info(
                        "Returning cached E2E Architect for artifact=%s (mode=%s, generated=%s)",
                        req.artifact_id, cached_mode,
                        cached.get("provenance", {}).get("generated_at", "?"),
                    )
                    elapsed_ms = (time.monotonic() - start) * 1000
                    cached["cached"] = True
                    cached["cache_hit_ms"] = round(elapsed_ms, 2)
                    # P3.4: merge fresh state (state may have changed since cache write)
                    return await _merge_state_into_response(
                        cached, artifact_id=req.artifact_id, tenant_id=tenant_id,
                    )

    # ── 2. Persona prerequisite (multimodal path only) ──
    # visual_strict mode generates tests purely from the visual evidence graph
    # (scenes, controls, flow edges) — no persona/transcript context required.
    full_json = artifact.get("full_artifact_json") or {}
    persona_cache = full_json.get("persona_draft_cache")
    persona_quality = ""
    persona_generated_at = ""

    if req.evidence_mode != "visual_strict":
        if not persona_cache or not isinstance(persona_cache, dict):
            raise HTTPException(
                422,
                "No persona draft found for this artifact. "
                "Generate a Process Oracle persona first before running E2E Architect "
                "in multimodal mode (or pass evidence_mode=visual_strict for visual-only).",
            )

        persona_quality = persona_cache.get("draft_quality", "")
        persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

        if not (persona_cache.get("domain_map") or {}).get("workflows"):
            raise HTTPException(
                422,
                "Persona draft has no workflow steps — insufficient data for E2E Architect.",
            )

        # ── 2b. Quality gate ──
        if persona_quality == "fallback":
            raise HTTPException(
                422,
                "Persona draft quality is 'fallback'. "
                "Regenerate the persona from a higher-quality recording first.",
            )

        # ── 3. Assemble multimodal payload for Brain (only needed in multimodal) ──
        brain_payload = build_e2e_brain_payload(artifact)
        brain_payload["source_persona_generated_at"] = persona_generated_at
        brain_payload["source_persona_quality"] = persona_quality or "full"
    elif persona_cache and isinstance(persona_cache, dict):
        # Record lineage when persona happens to exist, for cache validation.
        persona_quality = persona_cache.get("draft_quality", "")
        persona_generated_at = persona_cache.get("provenance", {}).get("generated_at", "")

    # ── 3. visual_strict mode: bypass Brain entirely ─────────
    if req.evidence_mode == "visual_strict":
        # P2: default to ALL strategies (LLM-driven + deterministic) so a
        # single visual-strict run yields the richest possible test suite.
        visual_strategies = req.strategies or [
            "happy_path",
            "variant",
            "negative",
            "boundary",
            "state_explorer",
            "cross_app",
            "error_state",
        ]
        token = _make_service_token(tenant_id)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                heart_resp = await client.post(
                    f"{_HEART_ENGINE_URL}/api/v1/heart/generate-tests-from-visual",
                    json={
                        "artifact_id": req.artifact_id,
                        "session_id": req.session_id or artifact.get("session_id", ""),
                        "evidence_mode": "visual_strict",
                        "strategies": visual_strategies,
                    },
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException:
            raise HTTPException(504, "Heart engine timed out during visual-strict test generation")
        except httpx.ConnectError:
            raise HTTPException(503, "Heart engine is unreachable")

        if heart_resp.status_code >= 400:
            try:
                detail = heart_resp.json().get("detail", heart_resp.text[:300])
            except Exception:
                detail = heart_resp.text[:300]
            raise HTTPException(heart_resp.status_code if heart_resp.status_code in (503, 504) else 502, detail)

        elapsed_ms = (time.monotonic() - start) * 1000
        heart_result = heart_resp.json()

        # Map Heart's test_cases → E2EArchitectResponse. Every field comes
        # from Heart's enriched output (see _enrich_test_case in
        # heart-engine/main.py) — no stubs, no hardcoded values.
        test_cases = heart_result.get("test_cases", [])
        coverage = heart_result.get("coverage_report", {})
        unproven_steps = heart_result.get("unproven_steps", [])

        # G1/G2: category and priority are derived from each test_case's
        # strategy — they describe what KIND of test it is, not free-floating
        # labels.
        _STRATEGY_CATEGORY = {
            "happy_path":     "observed",
            "variant":        "observed",
            "state_explorer": "observed",
            "cross_app":      "observed",
            "negative":       "inferred_high_risk",
            "error_state":    "inferred_high_risk",
            "boundary":       "boundary_assumption",
        }
        _STRATEGY_PRIORITY = {
            "happy_path":     "P0_critical",
            "negative":       "P1_high",
            "error_state":    "P1_high",
            "cross_app":      "P1_high",
            "state_explorer": "P2_medium",
            "boundary":       "P2_medium",
            "variant":        "P2_medium",
        }

        critical_combinations = []
        for i, tc in enumerate(test_cases):
            raw_steps = tc.get("steps", []) or []
            strategy = tc.get("strategy") or "happy_path"

            normalised_steps = []
            for s_idx, step in enumerate(raw_steps, start=1):
                normalised_steps.append({
                    "step_number": step.get("step_number", s_idx),
                    "action": step.get("action", ""),
                    "input_data": step.get("input_data", "") or "",
                    "expected_behavior": step.get("expected_output") or step.get("expected_behavior", ""),
                    "evidence_scene_id": step.get("evidence_scene_id", ""),
                    "evidence_control_id": step.get("evidence_control_id", ""),
                    "evidence_edge_id": step.get("evidence_edge_id", ""),
                    "proof_confidence": step.get("proof_confidence", 0.0),
                    "target_element": step.get("target_element", ""),
                })

            # Build evidence_sources from the per-step visual citations
            # (the frontend Evidence Inspector reads this field).
            evidence_sources = []
            seen_scenes: set[str] = set()
            for step in normalised_steps:
                scene_id = step["evidence_scene_id"]
                if scene_id and scene_id not in seen_scenes:
                    seen_scenes.add(scene_id)
                    evidence_sources.append({
                        "text": f"Scene {scene_id[:8]}",
                        "source_modality": "visual",
                        "confidence": float(step.get("proof_confidence") or 0.0),
                        "scene_id": scene_id,
                        "control_id": step.get("evidence_control_id", ""),
                        "edge_id": step.get("evidence_edge_id", ""),
                    })

            proven = sum(
                1 for s in normalised_steps
                if s.get("evidence_scene_id") and s.get("evidence_control_id")
            )

            critical_combinations.append({
                "scenario_id": f"vs_{i + 1:03d}",
                "title": tc.get("title", f"Visual Test {i + 1}"),
                # G1/G2: derived from strategy
                "category": _STRATEGY_CATEGORY.get(strategy, "observed"),
                "priority": _STRATEGY_PRIORITY.get(strategy, "P1_high"),
                "rationale": (
                    f"Generated by {strategy} strategy — {proven} of "
                    f"{len(normalised_steps)} steps grounded in scene + control."
                ),
                "evidence_sources": evidence_sources,
                # G3-G7: Pass through Heart's derived values (no stubs)
                "preconditions": tc.get("preconditions") or [],
                "steps": normalised_steps,
                "expected_outcome": tc.get("expected_outcome") or "",
                "data_matrix": tc.get("data_matrix") or [],
                "workflow_steps_covered": tc.get("workflow_steps_covered") or [],
                "risk_areas_addressed": tc.get("risk_areas_addressed") or [],
                "strategy": strategy,
                "visual_proven_steps": proven,
                "visual_total_steps": len(normalised_steps),
            })

        # P2: by-strategy breakdown for UI filter chips
        by_strategy: dict[str, int] = {}
        for sc in critical_combinations:
            key = sc.get("strategy") or "happy_path"
            by_strategy[key] = by_strategy.get(key, 0) + 1

        # Aggregate breakdowns from real per-scenario values (no hardcoding)
        by_category: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        all_workflow_steps: set[int] = set()
        all_risk_areas: set[str] = set()
        for sc in critical_combinations:
            by_category[sc["category"]] = by_category.get(sc["category"], 0) + 1
            by_priority[sc["priority"]] = by_priority.get(sc["priority"], 0) + 1
            for st in sc.get("workflow_steps_covered", []):
                all_workflow_steps.add(int(st))
            for r in sc.get("risk_areas_addressed", []):
                if r:
                    all_risk_areas.add(r)

        visual_strict_response = {
            "success": True,
            "artifact_id": req.artifact_id,
            "session_id": req.session_id or artifact.get("session_id", ""),
            "e2e_architect": {
                "variables": [],
                "decision_points": [],
                "critical_combinations": critical_combinations,
                "coverage_analysis": {
                    "total_scenarios": len(critical_combinations),
                    "scenarios_before_dedup": coverage.get("total_edges", len(critical_combinations)),
                    "duplicates_removed": 0,
                    "by_category": by_category,
                    "by_priority": by_priority,
                    "by_strategy": by_strategy,
                    "workflow_steps_covered": sorted(all_workflow_steps),
                    # Scene-coverage percentage from per-scenario citations
                    "workflow_coverage_pct": coverage.get("automation_ready_pct", 0.0),
                    "variables_tested": 0,
                    "decision_points_found": 0,
                    "pairwise_combinations_generated": sum(
                        len(sc.get("data_matrix") or []) for sc in critical_combinations
                    ),
                    "pairwise_coverage_pct": 0.0,
                    "risk_areas_addressed": sorted(all_risk_areas),
                    # Visual-strict diagnostics
                    "visual_strict_unproven": unproven_steps,
                    "visual_proven_steps": coverage.get("proven_steps", 0),
                    "visual_total_steps": coverage.get("total_steps", 0),
                    "strategies_requested": visual_strategies,
                    "strategies_empty": coverage.get("strategies_empty") or [],
                    "strategy_counts": coverage.get("strategy_counts") or {},
                    "llm_error": coverage.get("llm_error"),
                },
            },
            "provenance": {
                "evidence_mode": "visual_strict",
                "persona_id": None,
                "test_strategy_id": None,
                "strategy_created_at": None,
                "strategy_source": "visual_strict",
                "brain_model": "heart-visual-strict",
                "strategy_version": None,
                "multimodal_frames_used": 0,
                "model_used": "heart-visual-strict",
                "workflow_steps_analysed": coverage.get("total_edges", 0),
                "risks_considered": len(all_risk_areas),
                "generation_time_ms": round(elapsed_ms, 2),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "processing_time_ms": round(elapsed_ms, 2),
            "visual_substrate": {
                "quality": "visual_strict",
                "frame_count": 0,
                "has_ocr": True,
                "recommendation": None,
            },
            "cached": False,
        }
        return await _merge_state_into_response(
            visual_strict_response,
            artifact_id=req.artifact_id,
            tenant_id=tenant_id,
        )

    # ── 3b. Assess visual substrate quality (multimodal path only) ──
    visual_data = full_json.get("visual_analysis") or {}
    vis_frames = visual_data.get("frames") or []
    vis_stages = visual_data.get("pipeline_stages") or []
    if isinstance(vis_stages, str):
        vis_stages = [vis_stages]

    # Determine what visual profile was originally used
    has_multimodal_markers = any("multimodal" in str(s).lower() for s in vis_stages)
    has_deep_markers = any("deep" in str(s).lower() for s in vis_stages)
    frame_count = len(vis_frames)
    has_ocr = any("ocr" in str(s).lower() and "skipped" not in str(s).lower() for s in vis_stages)

    if has_multimodal_markers:
        visual_substrate_quality = "multimodal"
    elif has_deep_markers or frame_count >= 8:
        visual_substrate_quality = "deep"
    elif frame_count > 0:
        visual_substrate_quality = "fast"
    else:
        visual_substrate_quality = "minimal"

    _logger.info(
        "Visual substrate for artifact=%s: quality=%s frames=%d ocr=%s stages=%s",
        req.artifact_id, visual_substrate_quality, frame_count, has_ocr, vis_stages[:5],
    )

    # ── 4. Call Brain engine ──────────────────────────────
    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=900.0) as client:
            resp = await client.post(
                f"{_BRAIN_ENGINE_URL}/api/v1/brain/generate-e2e-architect",
                json=brain_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Brain engine timed out during E2E Architect generation")
    except httpx.ConnectError:
        raise HTTPException(503, "Brain engine is unreachable")
    except httpx.RemoteProtocolError as e:
        _logger.error("Brain disconnected mid-response: %s", e)
        raise HTTPException(502, "Brain engine disconnected during E2E Architect generation")

    if resp.status_code >= 400:
        _logger.error(
            "Brain generate-e2e-architect failed: HTTP %d — %s",
            resp.status_code, resp.text[:300],
        )
        try:
            detail = resp.json().get("detail", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise HTTPException(resp.status_code if resp.status_code in (503, 504) else 502, detail)

    brain_result = resp.json()

    # ── 5. Attach platform provenance ─────────────────────
    elapsed_ms = (time.monotonic() - start) * 1000
    provenance = brain_result.get("provenance", {})
    provenance["platform_processing_ms"] = round(elapsed_ms, 2)
    provenance["source_persona_generated_at"] = persona_generated_at

    response_data = {
        "success": True,
        "artifact_id": req.artifact_id,
        "session_id": req.session_id or artifact.get("session_id", ""),
        "e2e_architect": brain_result.get("e2e_architect", {}),
        "provenance": provenance,
        "processing_time_ms": round(elapsed_ms, 2),
        "visual_substrate": {
            "quality": visual_substrate_quality,
            "frame_count": frame_count,
            "has_ocr": has_ocr,
            "recommendation": (
                "Re-upload with 'Multimodal' processing profile for richer visual evidence"
                if visual_substrate_quality == "fast"
                else None
            ),
        },
    }

    # ── 6. Cache result in artifact ───────────────────────
    try:
        async with factory() as db:
            result = await db.execute(
                select(CanonicalArtifactRow).where(
                    CanonicalArtifactRow.artifact_id == req.artifact_id,
                    CanonicalArtifactRow.tenant_id == tenant_id,
                )
            )
            art_row = result.scalar_one_or_none()
            if art_row:
                existing_json = dict(art_row.full_artifact_json or {})
                existing_json["e2e_architect_cache"] = response_data
                art_row.full_artifact_json = existing_json
                await db.commit()
                _logger.info(
                    "Cached E2E Architect for artifact=%s (%d scenarios, %dms)",
                    req.artifact_id,
                    len((response_data.get("e2e_architect") or {}).get("critical_combinations", [])),
                    round(elapsed_ms),
                )
    except Exception:
        _logger.warning(
            "Failed to cache E2E Architect for artifact=%s (non-fatal)",
            req.artifact_id, exc_info=True,
        )

    response_data["cached"] = False
    # P3.4: merge lifecycle state for each scenario
    return await _merge_state_into_response(
        response_data, artifact_id=req.artifact_id, tenant_id=tenant_id,
    )
