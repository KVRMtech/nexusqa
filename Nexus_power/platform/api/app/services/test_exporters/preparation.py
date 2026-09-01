"""Canonical preparation pipeline shared by every test exporter (P5).

Fetches the visual evidence graph, calls Heart for test cases, applies
P3 lifecycle gates and the 5-gate automation readiness check, groups by
app_instance, and returns an ExportContext.

Raises HTTPException (422 / 502 / 503 / 504) on every failure path so the
caller doesn't have to translate errors per format.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
import jwt as pyjwt
from fastapi import HTTPException

from ...config import PlatformAPIConfig
from ...database import require_db
from ..e2e_lifecycle import fetch_scenario_states_map, is_exportable_state

from .base import ExportContext


_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_PLATFORM_BASE_URL = os.environ.get("PLATFORM_API_URL", "http://localhost:8000")
_HEART_ENGINE_URL = os.environ.get("HEART_ENGINE_URL", "http://localhost:8004")


# ─── 5-gate automation readiness ─────────────────────────────────────────

ACTIONABLE_STEP_TYPES = {
    "click", "fill", "select", "type", "enter", "input", "navigate", "submit",
}
PROOF_CONFIDENCE_THRESHOLD = 0.85


def is_automation_ready(
    steps: list[dict], controls_by_id: dict[str, dict],
) -> tuple[bool, list[str]]:
    """Five-gate readiness check applied to every actionable step.

    Returns (ready, unmet_gates) where unmet_gates is empty when ready=True.
    """
    unmet: list[str] = []
    for step in steps:
        action = (step.get("action") or "").lower()
        is_actionable = any(kw in action for kw in ACTIONABLE_STEP_TYPES)
        if not is_actionable:
            continue

        if not step.get("evidence_scene_id"):
            unmet.append(f"Gate 1: step '{action[:60]}' has no evidence_scene_id")
            continue
        if not step.get("evidence_control_id"):
            unmet.append(f"Gate 2: step '{action[:60]}' has no evidence_control_id")
            continue
        conf = step.get("proof_confidence", 0.0) or 0.0
        if conf < PROOF_CONFIDENCE_THRESHOLD:
            unmet.append(
                f"Gate 3: step '{action[:60]}' proof_confidence={conf:.2f} < {PROOF_CONFIDENCE_THRESHOLD}"
            )
            continue
        ctrl = controls_by_id.get(step.get("evidence_control_id", ""))
        if not ctrl or not ctrl.get("automation_ready"):
            unmet.append(
                f"Gate 4: control {step.get('evidence_control_id', '')[:8]} is not "
                "automation_ready (selector not OCR-backed)"
            )
            continue
        if not step.get("expected_output"):
            unmet.append(f"Gate 5: step '{action[:60]}' has no expected_output")
    return (len(unmet) == 0, unmet)


# ─── Service-to-service token ────────────────────────────────────────────


def _make_service_token(tenant_id: str) -> str:
    now = int(time.time())
    payload = {
        "sub": "platform-export-service",
        "tenant_id": tenant_id,
        "email": "platform-api@internal.nexus",
        "role": "admin",
        "permissions": ["*"],
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, _config.jwt_secret, algorithm=_config.jwt_algorithm)


# ─── Public preparation pipeline ─────────────────────────────────────────


async def prepare_export_context(
    *, artifact_id: str, tenant_id: str,
    enforce_automation_gates: bool = True,
) -> ExportContext:
    """Run the canonical preparation pipeline.

    Steps:
      1. Fetch visual-evidence-graph for the artifact
      2. Fetch test_cases from Heart in visual_strict mode
      3. Assign deterministic scenario_ids (vs_001, ...)
      4. Filter to exportable lifecycle states (P3 gate)
      5. Run 5-gate automation readiness (when enforce_automation_gates=True)
      6. Group by app_instance via the first step's scene
      7. Return ExportContext

    Raises:
      HTTPException(422) for missing-evidence / no-test-cases / no-approved /
                            gate-failed conditions, with structured detail.
      HTTPException(503/504) on Heart engine unreachability.
    """
    svc_token = _make_service_token(tenant_id)

    # 1) Fetch visual evidence graph
    graph: dict = {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_PLATFORM_BASE_URL}/api/v1/artifacts/{artifact_id}/visual-evidence-graph",
                headers={"Authorization": f"Bearer {svc_token}"},
            )
            if resp.status_code == 200:
                graph = resp.json()
    except Exception as exc:
        _logger.warning("export.prepare: graph fetch failed: %s", exc)

    if not graph.get("scenes"):
        raise HTTPException(422, detail={
            "error": "no_visual_evidence",
            "message": (
                "No visual evidence graph available for this artifact. "
                "Run the visual pipeline first."
            ),
            "unmet_gates": ["Visual evidence pipeline has not processed this artifact"],
        })

    # 2) Fetch test cases from Heart
    heart_result: dict = {}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            h_resp = await client.post(
                f"{_HEART_ENGINE_URL}/api/v1/heart/generate-tests-from-visual",
                json={
                    "artifact_id": artifact_id,
                    "session_id": "",
                    "evidence_mode": "visual_strict",
                },
                headers={"Authorization": f"Bearer {svc_token}"},
            )
            if h_resp.status_code == 200:
                heart_result = h_resp.json()
            else:
                _logger.warning(
                    "export.prepare: heart returned %s — %s",
                    h_resp.status_code, h_resp.text[:200],
                )
    except httpx.TimeoutException:
        raise HTTPException(504, "Heart engine timed out during test generation")
    except httpx.ConnectError:
        raise HTTPException(503, "Heart engine is unreachable")
    except Exception as exc:
        _logger.warning("export.prepare: heart call failed: %s", exc)

    test_cases: list[dict] = heart_result.get("test_cases", []) or []
    if not test_cases:
        raise HTTPException(422, detail={
            "error": "no_test_cases",
            "message": "Heart engine returned no test cases for this artifact.",
            "unmet_gates": [
                "No test cases generated — run visual-strict generation first"
            ],
        })

    total_before_state_filter = len(test_cases)

    # 3) Assign deterministic scenario_ids (must match e2e_architect.py's scheme)
    for i, tc in enumerate(test_cases, start=1):
        tc.setdefault("scenario_id", f"vs_{i:03d}")

    # 4) Lifecycle gate
    factory = require_db()
    async with factory() as db:
        states_map = await fetch_scenario_states_map(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )

    approved_cases: list[dict] = []
    skipped_states: dict[str, int] = {}
    for tc in test_cases:
        sid = tc["scenario_id"]
        st = states_map.get(sid, {}).get("state", "draft")
        if is_exportable_state(st):
            approved_cases.append(tc)
        else:
            skipped_states[st] = skipped_states.get(st, 0) + 1

    if not approved_cases:
        raise HTTPException(422, detail={
            "error": "no_approved_scenarios",
            "message": (
                f"None of the {total_before_state_filter} generated scenarios are in an "
                "exportable lifecycle state (approved / automated / live / stable). "
                "Review and approve scenarios in the Test Studio before exporting."
            ),
            "unmet_gates": [
                f"{count} scenario(s) in state '{state}' — not exportable"
                for state, count in sorted(skipped_states.items())
            ],
            "scenario_state_breakdown": skipped_states,
        })

    # Build controls index
    controls_by_scene: dict[str, list[dict]] = graph.get("controls_by_scene", {})
    controls_by_id: dict[str, dict] = {
        c.get("control_id", ""): c
        for clist in controls_by_scene.values()
        for c in clist
    }

    # 5) Automation readiness check (skippable for non-automation exports like
    #    JSON / Gherkin, which can still describe non-automation-ready steps).
    if enforce_automation_gates:
        all_steps = [step for tc in approved_cases for step in tc.get("steps", [])]
        ready, unmet = is_automation_ready(all_steps, controls_by_id)
        if not ready:
            raise HTTPException(422, detail={
                "error": "gate_failed",
                "message": "One or more automation gates are not met.",
                "unmet_gates": unmet,
            })

    # 6) Group by app_instance via the first step's scene
    scenes_list: list[dict] = graph.get("scenes", []) or []
    edges_list: list[dict] = graph.get("edges", []) or []
    scenes_by_id: dict[str, dict] = {s["scene_id"]: s for s in scenes_list}
    edges_by_id: dict[str, dict] = {e["edge_id"]: e for e in edges_list}

    app_instances: list[dict] = graph.get("app_instances", []) or []
    scene_to_inst: dict[str, str] = {
        s["scene_id"]: (s.get("app_instance_id") or "unassigned")
        for s in scenes_list
    }
    inst_name_map: dict[str, str] = {
        inst["instance_id"]: (inst.get("app_name") or inst.get("app_type") or "App")
        for inst in app_instances
    }
    inst_name_map["unassigned"] = "App"

    tc_by_inst: dict[str, list[dict]] = {}
    for tc in approved_cases:
        steps = tc.get("steps", [])
        inst_id = "unassigned"
        for step in steps:
            sid = step.get("evidence_scene_id") or ""
            if sid in scene_to_inst:
                inst_id = scene_to_inst[sid]
                break
        tc_by_inst.setdefault(inst_id, []).append(tc)

    # Phase 2 — pull URL-keyed surfaces off the graph response when
    # the page extractors have populated them.  All three keys are
    # always present (empty when extractors haven't run yet) so the
    # access pattern is unconditional.
    page_visits = graph.get("page_visits", []) or []
    page_actions = graph.get("page_actions", {}) or {}
    form_snapshots = graph.get("form_snapshots", {}) or {}

    return ExportContext(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        test_cases=approved_cases,
        controls_by_id=controls_by_id,
        scenes_by_id=scenes_by_id,
        edges_by_id=edges_by_id,
        tc_by_inst=tc_by_inst,
        inst_name_map=inst_name_map,
        scenarios_skipped_by_state=skipped_states,
        total_scenarios_before_filter=total_before_state_filter,
        page_visits=page_visits,
        page_actions=page_actions,
        form_snapshots=form_snapshots,
    )
