"""HTTP endpoints for the Test Factory (Phase 1 — demonstrated functional E2E).

* ``POST /api/v1/test-factory/{artifact_id}/generate``
    Generate demonstrated test cases from the artifact's Pages & Forms
    evidence and persist them.  Idempotent.
* ``GET  /api/v1/test-factory/{artifact_id}/summary``
    Small aggregate payload for the UI (counts only — never the full set).
* ``GET  /api/v1/test-factory/{artifact_id}/test-cases``
    Server-side paginated listing.
* ``GET  /api/v1/test-factory/{artifact_id}/export``
    Stream the generated suite as Excel/CSV/JSON for download or hand-off to a
    test-management tool, in the standard QA column format.

Additive router.  Reads frozen Pages & Forms data; writes only the additive
``factory_test_cases`` table.  Tenant isolation enforced via
``tenant_scoped_session`` (Postgres RLS).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import logging
import os
import uuid
import zipfile
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request, Body
from fastapi import Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, text

from nexus_sdk.db.models import (
    CanonicalArtifactRow,
    E2ETestRunRow,
    E2ETestRunStepRow,
    E2E_STEP_STATUS_PASSED,
    AuditLogRow,
)
from nexus_sdk.security.envelope import EnvelopeBlob

from ..auth import get_current_user
from ..database import tenant_scoped_session
from .integrations import integration_installations
from ..services.test_factory import service as factory_service
from ..services.test_factory import proposer
from ..services.test_factory.options_extractor import (
    extract_field_options_for_artifact,
)
from ..services.test_factory.anchor_extractor import (
    extract_anchors_for_artifact,
)
from ..services.test_factory.after_extractor import (
    extract_outcomes_for_artifact,
)
from ..services.test_factory.enrich_extractor import enrich_artifact
from ..services.script_factory import build_field_meta, ci_workflow_files, compile_case, compile_manifest, compile_project
from ..services.script_factory import runner_client
from ..services.script_factory import versions as script_versions
from ..services.script_factory.triage import assemble_triage
from ..services.oracle_scorecard import compute_artifact_scorecard
from ..services.test_factory.provenance import build_rtm
from ..services.test_factory.perceptual_diff import diff_screenshots
from ..services.test_factory.semantic_oracle import judge_semantic_match
from ..services.test_factory.run_screenshots import fetch_latest_screenshot
from ..services.storyboard.form_snapshot_extractor import _fetch_frame_bytes
from ..services.diff_and_heal import self_heal
from ..services.diff_and_heal import heal_capture_store
from ..services.diff_and_heal import heal_evidence
from ..services.diff_and_heal import heal_slo as heal_slo_svc
from ..services.flywheel import ledger as flywheel_ledger
from ..services.test_factory import fidelity as tf_fidelity
from ..services.test_runs import (
    last_run_summary_by_scenario,
    product_quarantined_scenarios,
    quarantine_decision,
    uncertified_exploratory_scenarios,
    _status_severity,
    build_latest_run_timeline,
    build_run_timeline_by_id,
    find_run_by_ci_run_id,
    recent_runs,
    VERDICT_PASSED,
    VERDICT_REAL_REGRESSION,
    VERDICT_SELECTOR_DRIFT,
    VERDICT_VISUAL_CHANGE,
    VERDICT_FLAKE,
    VERDICT_NEEDS_REVIEW,
)
# Anti-drift: the flywheel ledger's verdict enums are clamped to EXACTLY the set
# the deterministic reducer emits (sourced from the constants above, not a copy),
# so a client-supplied verdict can never smuggle raw/PII text into the de-identified
# federated ledger. Unknown -> the fixed "unknown" sentinel, never the raw string.
_KNOWN_VERDICTS = frozenset({
    VERDICT_PASSED, VERDICT_REAL_REGRESSION, VERDICT_SELECTOR_DRIFT,
    VERDICT_VISUAL_CHANGE, VERDICT_FLAKE, VERDICT_NEEDS_REVIEW,
})
from ..services.test_factory import runner_jobs
from ..services.test_factory import auth_profiles
from ..services.test_factory import evidence_report
from ..services.test_factory import field_learning
from ..services.test_factory import report_html
from ..services.test_factory import report_export
from ..services.test_factory import evidence_manifest
from ..services.test_factory import report_formats
from ..services.test_factory import persona_store, login_recorder, login_fingerprint
from ..services.test_factory import login_observation
from ..services.test_factory import member_data_resolver
from ..services.test_factory import draft_recording
from ..services.test_factory import environment_routing
from ..services.test_factory import card_contract
from ..services.test_factory import card_state
from ..services.test_factory import session_handoff
from ..services.test_factory import login_probe
from ..services.test_factory import persona_identity
from ..services.test_factory import matrix
from ..services.script_factory.compiler import _data_key as compiler_data_key
from ..services.test_factory import persona_diff
from ..services.test_factory import persona_governance
from ..services.test_factory import persona_scale
from ..services.test_factory import report_store
from ..services.test_factory import evidence_retention
from ..services.test_factory.assistant import answer as assistant_answer
from ..services.test_factory.delivery import (
    EXPORT_MEDIA_TYPES,
    build_csv,
    build_excel,
    build_json,
)
from ..services.test_factory.delivery.connectors import CONNECTORS, build_connector

# ─── RBAC (ADDITIVE 2026-06-21; governance fast-track #1) ────────────────────
# The marketed role-gating was UI-only; every mutating endpoint here ran on bare
# get_current_user, so a viewer could run-against-prod / approve heals / push PII.
# ONE centralized gate: POST/PATCH/PUT/DELETE require admin|manager (the platform
# _PRIVILEGED set); GET reads stay open to viewers; the service-to-service reporter
# ingest (/test-runs/progress) is exempt.
_PRIVILEGED = frozenset({"admin", "manager"})
_RBAC_EXEMPT_SUFFIXES = ("/test-runs/progress",)


async def _rbac_gate(request: Request, user: dict = Depends(get_current_user)) -> None:
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        if any(request.url.path.endswith(s) for s in _RBAC_EXEMPT_SUFFIXES):
            return
        if user.get("role", "viewer") not in _PRIVILEGED:
            raise HTTPException(status_code=403, detail="Requires admin or manager role to modify test cases")
        await _audit_mutation(request, user)


async def _audit_mutation(request: Request, user: dict) -> None:
    """Immutable audit trail (governance #2): one AuditLogRow per AUTHORIZED
    mutation — who (user_id/email), what (method + route), which entity
    (artifact / case), when (created_at). Fail-SAFE: an audit error never
    breaks the mutation it records."""
    try:
        path = request.url.path
        parts = [p for p in path.split("/") if p]
        artifact_id, case_id = "", ""
        if "test-factory" in parts:
            i = parts.index("test-factory")
            if i + 1 < len(parts):
                artifact_id = parts[i + 1]
        if "test-cases" in parts:
            j = parts.index("test-cases")
            if j + 1 < len(parts):
                case_id = parts[j + 1]
        async with tenant_scoped_session(user["tenant_id"]) as session:
            session.add(AuditLogRow(
                tenant_id=user["tenant_id"],
                engine="test_factory",
                action=f"{request.method} {path}",
                entity_type="test_case" if case_id else "test_factory",
                entity_id=case_id or artifact_id,
                user_id=str(user.get("sub") or user.get("user_id") or ""),
                details={"method": request.method, "path": path,
                         "role": user.get("role", ""), "email": user.get("email", "")},
            ))
            await session.commit()
    except Exception:
        pass  # never break a mutation because of an audit-log failure


router = APIRouter(tags=["Test Factory"], dependencies=[Depends(_rbac_gate)])
_logger = logging.getLogger(__name__)

_BUILDERS = {"excel": build_excel, "csv": build_csv, "json": build_json}
_EXTENSIONS = {"excel": "xlsx", "csv": "csv", "json": "json"}


def _env_routing_enabled() -> bool:
    """Let the SELECTED environment decide where a run goes, and judge posture on
    that destination. Default OFF: unset, the address still comes from the request
    exactly as before. Read per call so a live box reverts without a rebuild."""
    return os.getenv("NEXUS_ENV_ROUTING", "").strip().lower() in (
        "1", "true", "yes", "on")


def _card_gate_enabled() -> bool:
    """Refuse a run whose member cannot perform the recorded login (F4).

    Default ON, unlike the other two flags, and deliberately: this gate can only
    refuse a run that would otherwise have executed logged out and attributed every
    failure to the application under test. Turning it off restores that green-wash,
    so the escape hatch exists for an emergency, not as the resting state.
    Read per call, so ``NEXUS_CARD_HEALTH_GATE=0`` takes effect without a rebuild."""
    return os.getenv("NEXUS_CARD_HEALTH_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _member_data_enabled() -> bool:
    """Resolve PROVEN member-derived values to the running member, and block when
    that member has no answer. Read per call (not cached at import) so it can be
    turned off on a live box without a rebuild. Default OFF: unset means the data
    map is untouched and the compiled output is unchanged."""
    return os.getenv("NEXUS_MEMBER_DATA_RESOLVER", "").strip().lower() in (
        "1", "true", "yes", "on")


async def _require_artifact(session, artifact_id: str, tenant_id: str) -> CanonicalArtifactRow:
    art = (
        await session.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return art


class GenerateRequest(BaseModel):
    """Optional generate body (ANSWERS P1).  Every field defaults so the endpoint
    still validates a BODY-LESS POST — today's qe-central caller sends no body, so
    this must never become required or those callers would 400."""
    answer_key: dict | None = None


@router.post("/api/v1/test-factory/{artifact_id}/generate")
async def generate_test_cases(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    body: GenerateRequest | None = Body(None),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    answer_key = body.answer_key if body else None
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        summary = await factory_service.generate_and_store(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
            answer_key=answer_key,
        )
    added_cases = await proposer.reapply_added_cases(artifact_id, tenant_id)
    reapplied = await _reapply_tf_overrides(artifact_id, tenant_id)
    approved_protected = await proposer.reapply_approved(artifact_id, tenant_id)
    # P0.3 — certification-before-client: prove the fresh suite on the baseline
    # (fire-and-forget; generation returns immediately, certification results
    # land via the normal ingest path tagged environment='certification').
    _spawn_certification(request, artifact_id, tenant_id)
    return {"success": True, "overrides_reapplied": reapplied, "added_cases": added_cases,
            "approved_protected": approved_protected, "certification": "dispatched",
            **summary}


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()


class AssistantRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    history: list[dict] = Field(default_factory=list)


@router.post("/api/v1/test-factory/{artifact_id}/assistant")
async def assistant(
    request: Request,
    body: AssistantRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Co-Architect (repointed) — grounded QA copilot over Pages & Forms data."""
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")

    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await assistant_answer(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            message=body.message, history=body.history, router=llm_router,
        )


@router.post("/api/v1/test-factory/{artifact_id}/capture-options")
async def capture_options(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Capture available field options (vision) into form_snapshot_signals, then
    regenerate so grounded combinations materialise.

    Uses the shared LLM router (from the storyboard composer).  Costs one vision
    call per page; only options actually visible in frames are captured.
    """
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    token = _bearer(request)

    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        options = await extract_field_options_for_artifact(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            router=llm_router, auth_token=token,
        )
        await session.commit()
        # commit reset the transaction-local RLS var — re-arm before regenerate.
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        regenerated = await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, "options_capture": options, "regenerated": regenerated}


@router.post("/api/v1/test-factory/{artifact_id}/capture-anchors")
async def capture_anchors(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Capture per-action 'where it sits' anchors (vision) into
    page_actions.evidence_signals, then regenerate so steps gain the anchor.

    Uses the shared LLM router. One vision call per page; only landmarks
    actually visible in frames are recorded — repeated controls become
    unambiguous ('Click Select in the 10:30 AM row').
    """
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    token = _bearer(request)

    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        anchors = await extract_anchors_for_artifact(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            router=llm_router, auth_token=token,
        )
        await session.commit()
        # commit reset the transaction-local RLS var — re-arm before regenerate.
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        regenerated = await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, "anchor_capture": anchors, "regenerated": regenerated}


@router.post("/api/v1/test-factory/{artifact_id}/capture-outcomes")
async def capture_outcomes(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Capture per-action 'what happened after' outcomes (vision) into
    page_actions.evidence_signals, then regenerate so each step's Expected
    Result reflects the observed outcome (results appeared / validation error /
    navigation) — the real source for waits + assertions.
    """
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    token = _bearer(request)

    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        outcomes = await extract_outcomes_for_artifact(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            router=llm_router, auth_token=token,
        )
        await session.commit()
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        regenerated = await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, "outcome_capture": outcomes, "regenerated": regenerated}


@router.post("/api/v1/test-factory/{artifact_id}/enrich")
async def enrich(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """One-click enrichment: run all vision capture passes (available options,
    'where it sits' anchors, 'what happened after' outcomes) then regenerate
    ONCE. Replaces the separate capture buttons so users never have to know the
    internal passes. Each pass only records what is visible in frames.
    """
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    token = _bearer(request)

    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        capture = await enrich_artifact(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            router=llm_router, auth_token=token,
        )
        await session.commit()
        # commit reset the transaction-local RLS var — re-arm before regenerate.
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        regenerated = await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
            validate=True, router=llm_router,  # Phase 2: LLM double-check (membership-gated)
        )
    return {"success": True, "enrichment": capture, "regenerated": regenerated}


_CATEGORIES = {"negative", "boundary", "error_state"}


@router.post("/api/v1/test-factory/{artifact_id}/generate/{category}")
async def generate_category_endpoint(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    category: str = PathParam(..., min_length=1, max_length=40),
    user: dict = Depends(get_current_user),
):
    """On-demand generate a non-demonstrated category (negative|boundary|error_state)."""
    if category not in _CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown category '{category}' (use: {', '.join(sorted(_CATEGORIES))})",
        )
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        result = await factory_service.generate_category(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            category=category, session_id=getattr(art, "session_id", "") or "",
        )
    # Same compensation as POST /generate (180-196): the category pass rebuilds
    # cases from Pages & Forms, so re-apply the user's added cases + step
    # overrides + approved snapshots — otherwise "Generate full suite" silently
    # drops the user's edits on the negative/boundary/error_state passes.
    await proposer.reapply_added_cases(artifact_id, tenant_id)
    await _reapply_tf_overrides(artifact_id, tenant_id)
    await proposer.reapply_approved(artifact_id, tenant_id)
    # P0.3 — the rebuilt suite must re-prove itself on the baseline too.
    _spawn_certification(request, artifact_id, tenant_id)
    return {"success": True, "certification": "dispatched", **result}


@router.get("/api/v1/test-factory/{artifact_id}/summary")
async def get_summary(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await factory_service.summarize(session, artifact_id=artifact_id)


@router.get("/api/v1/test-factory/{artifact_id}/test-cases")
async def list_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(25, ge=1, le=200, description="Test cases per page"),
    status: str = Query("active", pattern="^(active|reserve)$"),
    type: str | None = Query(None, description="filter by category/test_type"),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await factory_service.list_paginated(
            session, artifact_id=artifact_id, page=page, limit=limit,
            status=status, test_type=type,
        )


@router.get("/api/v1/test-factory/{artifact_id}/reserve")
async def get_reserve(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        reserve = await factory_service.get_reserve(session, artifact_id=artifact_id)
    if reserve is None:
        raise HTTPException(
            status_code=404,
            detail="no combination reserve for this artifact — run /generate first",
        )
    return reserve


@router.get("/api/v1/test-factory/{artifact_id}/export")
async def export_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    format: str = Query("excel", description="excel | csv | json"),
    details: bool = Query(False, description="include the 'Observed in Recording' evidence column (role/toggle gated in the UI)"),
    redact: bool = Query(False, description="redact detected PII (SSN/DOB/email/phone/policy) from the export"),
    user: dict = Depends(get_current_user),
):
    fmt = (format or "excel").lower()
    if fmt not in _BUILDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported format '{format}' (use: {', '.join(_BUILDERS)})",
        )

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )

    if not cases:
        raise HTTPException(
            status_code=404,
            detail="no generated test cases for this artifact — run /generate first",
        )

    if redact:
        from ..services.test_factory import redaction as _redaction
        cases, _pii_types = _redaction.redact_cases(cases)
        await _redaction.log_shield(tenant_id, "redact", len(cases), _pii_types,
                                    str(user.get("sub") or user.get("user_id") or ""))
    payload = _BUILDERS[fmt](cases, details)
    filename = f"nexus-testcases-{artifact_id[:8]}.{_EXTENSIONS[fmt]}"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type=EXPORT_MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/api/v1/test-factory/{artifact_id}/playwright")
async def generate_playwright(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    category: str = Query(
        "",
        description="optional single category: functional | combination | negative | "
        "boundary | error_state. Empty = the whole active suite.",
    ),
    test_case_id: str = Query(
        "", description="optional single test case id (takes precedence over category).",
    ),
    user: dict = Depends(get_current_user),
):
    """Deterministically compile the active suite, one category, or one specific
    test case into a runnable Playwright project (zip). Read-only + ZERO LLM:
    grounded in the stored observed evidence, same suite in -> byte-identical
    project out. The buyer owns the emitted code.
    """
    tenant_id = user["tenant_id"]
    cat = (category or "").strip().lower()
    tcid = (test_case_id or "").strip()
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        # Deterministic kind-awareness: captured control type + options per field
        # (read-only over existing signals; no LLM, no pipeline mutation).
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited = await _active_edited_map(session, artifact_id=artifact_id)
    if tcid:
        cases = [c for c in cases if (getattr(c, "test_id", "") or "") == tcid]
    elif cat:
        cases = [c for c in cases if (getattr(c, "type", "") or "").lower() == cat]
    if not cases:
        detail = (
            "test case not found or not active"
            if tcid else
            f"no active '{cat}' test cases — generate that category first"
            if cat else
            "no generated test cases for this artifact — run /generate first"
        )
        raise HTTPException(status_code=404, detail=detail)

    files = compile_project(cases, build_field_meta(visits))
    # Phase C overlay: same substitution as the run path / manifest, so the
    # DOWNLOADED zip carries the user's saved / regenerated / healed scripts
    # rather than a fresh-from-cases recompile that ignores their edits.
    if edited:
        id_to_path = {s["test_id"]: s["path"]
                      for s in compile_manifest(cases, build_field_meta(visits)).get("scripts", [])}
        for tid, ev in edited.items():
            src = (ev or {}).get("script_source")
            if not src:
                continue
            path = id_to_path.get(tid) or (ev or {}).get("spec_path")
            if path and path in files:  # only override specs actually in this bundle
                files[path] = src

    # ── AUDITOR DELIVERY GATE (HONEST-10) ────────────────────────────────
    # Scores every spec ACTUALLY delivered (saved/healed/edited versions
    # included — the override pass above already ran) with the deterministic
    # rubric + API-policy lint. Report ships inside the zip. Default mode
    # 'annotate' leaves existing behavior untouched; NEXUS_AUDITOR_GATE=block
    # turns suite_min < NEXUS_AUDITOR_MIN_SCORE into HTTP 409. $0 — no LLM.
    from ..services.test_factory import playwright_auditor as _pw_gate
    # Load the SAME evidence the on-demand audit endpoint uses — steps-only
    # scoring hard-fails the grounding dimension and reads as 0/repair.
    try:
        async with tenant_scoped_session(tenant_id) as _gate_sess:
            _gv, _gate_actions = await factory_service._load_current_pages_and_actions(
                _gate_sess, artifact_id=artifact_id)
    except Exception:
        _gv, _gate_actions = [], None

    # ── P4/P7/P8: evidence-derived project extras (additive files) ────────
    try:
        files.update(_engine_extra_files(_gv if _gv else visits, _gate_actions, cases))
    except Exception:
        logger.warning("playwright.extras_failed", extra={"artifact_id": artifact_id})

    _audit_by_test: dict = {}
    _audit_min = 10
    _case_by_id = {(getattr(c, "test_id", "") or ""): c for c in cases}
    # Build the test->spec map from the manifest here: the override pass only
    # defines its own map when saved versions exist, so the gate must not
    # depend on that branch having run.
    _gate_map = {
        str(s.get("test_id", "") or ""): str(s.get("path", "") or s.get("spec_path", "") or "")
        for s in compile_manifest(cases, build_field_meta(visits)).get("scripts", [])
    }
    for _tid, _p in _gate_map.items():
        _c = _case_by_id.get(_tid)
        if _c is None or _p not in files:
            continue
        try:
            _rep = _pw_gate.score_spec(
                files[_p], list(getattr(_c, "steps", []) or []), evidence=_gate_actions)
        except Exception as _exc:  # the gate must never break delivery
            _rep = {"overall_score": 0, "decision": "audit_error",
                    "findings": [f"auditor crashed: {str(_exc)[:120]}"],
                    "gaps": [], "dimension_scores": {}}
        try:
            _lint = _pw_gate.lint_spec(files[_p])
        except Exception:
            _lint = []
        _audit_by_test[_tid] = {
            "spec_path": _p,
            "overall_score": _rep.get("overall_score"),
            "decision": _rep.get("decision"),
            "dimension_scores": _rep.get("dimension_scores"),
            "findings": ([x for x in _rep.get("findings") or []][:12]
                         if isinstance(_rep.get("findings"), (list, tuple))
                         else ([] if _rep.get("findings") in (None, 0) else [str(_rep.get("findings"))])),
            "gaps": ([x for x in _rep.get("gaps") or []][:12]
                     if isinstance(_rep.get("gaps"), (list, tuple))
                     else ([] if _rep.get("gaps") in (None, 0) else [str(_rep.get("gaps"))])),
            "lint": _lint[:20],
        }
        try:
            _audit_min = min(_audit_min, int(_rep.get("overall_score") or 0))
        except (TypeError, ValueError):
            _audit_min = 0
    _gate_mode = (os.getenv("NEXUS_AUDITOR_GATE", "annotate") or "annotate").lower()
    try:
        _gate_min = int(os.getenv("NEXUS_AUDITOR_MIN_SCORE", "9") or 9)
    except ValueError:
        _gate_min = 9
    if _audit_by_test:
        files["vkpower-audit-report.json"] = json.dumps({
            "rubric": "HONEST-10 deterministic (playwright_auditor.score_spec) + API-policy lint",
            "gate_mode": _gate_mode,
            "min_score_threshold": _gate_min,
            "suite_min_overall": _audit_min,
            "scripts": _audit_by_test,
            "note": ("delivery-time audit uses the stored page-action evidence; "
                     "POST .../scripts/{test_id}/audit gives the full "
                     "evidence-grounded audit per script"),
        }, indent=2, sort_keys=True)
    # verdict history: every DELIVERED script's audit becomes a timeline event
    # (source=delivery-gate). Best-effort — the zip never fails on bookkeeping.
    try:
        from ..services.test_factory import verdict_events as _ve_gate
        for _tid2, _s2 in _audit_by_test.items():
            await _ve_gate.record_verdict(
                tenant_id=tenant_id, artifact_id=artifact_id, test_id=_tid2,
                version=None, source="delivery-gate",
                actor=str(user.get("email") or user.get("sub") or ""),
                overall=int(_s2.get("overall_score") or 0),
                decision=str(_s2.get("decision") or ""),
                axes=dict(_s2.get("dimension_scores") or {}),
                gaps=int(str((_s2.get("gaps") or ["0"])[0]) if isinstance(_s2.get("gaps"), list) else (_s2.get("gaps") or 0)),
                findings=[str(f)[:200] for f in (_s2.get("findings") or [])],
                lint=list(_s2.get("lint") or []),
            )
    except Exception:
        logger.warning("verdict_events.gate_record_failed", extra={"artifact_id": artifact_id})

    if _gate_mode == "block" and _audit_by_test and _audit_min < _gate_min:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "auditor_gate_blocked",
                "suite_min_overall": _audit_min,
                "threshold": _gate_min,
                "blocked_scripts": {
                    k: {"overall_score": v.get("overall_score"),
                        "findings": v.get("findings", [])[:5]}
                    for k, v in _audit_by_test.items()
                    if int(v.get("overall_score") or 0) < _gate_min
                },
                "hint": ("fix the findings or download with "
                         "NEXUS_AUDITOR_GATE=annotate to inspect the full "
                         "report inside the zip"),
            })
    from ..services.script_factory.runner_client import add_legacy_artifact_aliases
    files = add_legacy_artifact_aliases(files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            # Fixed timestamp -> the zip bytes are reproducible too, not just the code.
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)

    suffix = f"-{tcid[:8]}" if tcid else (f"-{cat}" if cat else "")
    filename = f"vkpower-playwright-{artifact_id[:8]}{suffix}.zip"
    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/api/v1/test-factory/{artifact_id}/playwright/manifest")
async def playwright_manifest(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """List the deterministically-compiled Playwright suite as JSON for the
    Execution view: each script's source + spec path + category + per-step stats,
    the supporting project files, and the exact run commands. Same compilation as
    the zip (/playwright) — read-only, ZERO LLM. Returns empty `scripts` (NOT 404)
    when nothing is generated yet, so the page can show a 'generate first' prompt.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited = await _active_edited_map(session, artifact_id=artifact_id)
    manifest = compile_manifest(cases, build_field_meta(visits))
    # Phase C overlay: surface the ACTIVE edited / regenerated / healed version
    # for any test that has one — the SAME substitution the run path
    # (_configured_files) already applies — so a saved or regenerated script is
    # actually visible here, not the stale fresh-from-cases compile (the bug
    # behind "Save updates Playwright but the script never changes").
    if edited:
        for sc in manifest.get("scripts", []):
            ev = edited.get(sc.get("test_id"))
            if ev and ev.get("script_source"):
                sc["code"] = ev["script_source"]
    manifest["artifact_id"] = artifact_id
    manifest["run"] = {
        "install": "npm install && npx playwright install --with-deps",
        "all": "npx playwright test",
        "headed": "npx playwright test --headed",
        "ui": "npx playwright test --ui",
        "report": "npx playwright show-report",
        "reporter_env": {
            "NEXUS_ENDPOINT": "<your-nexus-host>",
            "NEXUS_TOKEN": "<your-api-jwt>",
            "NEXUS_ARTIFACT_ID": artifact_id,
        },
    }
    return manifest


#: Playwright's screenshot modes, so an unknown value degrades to a sane one
#: rather than silently disabling capture.
_SCREENSHOT_MODES = ("on", "only-on-failure", "off")


class RunConfigRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    base_url: str = ""
    data: dict[str, str] = Field(default_factory=dict)            # global defaults
    data_by_test: dict[str, dict[str, str]] = Field(default_factory=dict)  # per-test overrides
    browsers: list[str] = Field(default_factory=lambda: ["chromium"])
    headed: bool = False
    workers: int | None = None
    retries: int | None = None
    autonomous: bool = False  # AUTOPILOT (Mode B): drive+prove UNPROVEN steps + auto-apply the grounded agentic analyst, no human
    # Multi-env: a RESOLVED Environment Profile from qe-central (base_url, cookies,
    # data_overrides, env_assertion, ...). qe-central owns app_environments + seals
    # per-env creds; platform-api only APPLIES the resolved context. None ⇒ unchanged.
    env_context: dict | None = None
    # Tier-T3 deep diagnostics (spec §2.10): performance timings, the browser
    # accessibility snapshot, console + page errors, optionally the rendered
    # HTML. OFF by default because it costs time and storage on every step; the
    # compiled spec is inert without it, so a run is byte-identical when unset.
    diagnostics: bool = False
    diagnostics_html: bool = False
    # Visual evidence — ON by default for every run, whether you launch ONE
    # test or the whole suite (this is a run-level policy, not a per-test one).
    # A test-evidence product should show what the run SAW, not only the
    # wreckage of a failure. Set false to dial back on a large suite or a
    # storage-constrained deployment; `screenshot_mode` allows the middle
    # ground ('only-on-failure') without turning capture off entirely.
    video: bool = True
    screenshots: bool = True
    screenshot_mode: str = "on"        # on | only-on-failure | off
    # Persona × Environment (P2): run AS a declared member, resolved to a
    # recipe + credential card at dispatch. Empty ⇒ today's behaviour (the
    # artifact's form-login / captured session). environment_id names the
    # registry row for the card + (P4) posture.
    persona_id: str = ""
    environment_id: str = ""
    # Scale (P5): resolve a persona by TRAITS / behavior class instead of a
    # pinned id — "run as any senior-large-family member". Ignored when
    # persona_id is set. {"traits": [...], "behavior_class": "..."}.
    persona_match: dict | None = None
    # Environment governance (P4): does this run intend to MUTATE (submit/write)?
    # Default True — a regression suite mutates, so a production target is
    # default-deny unless the environment carries an explicit write authorization.
    # A read-only verification run sets this False and is allowed on a locked env.
    mutating: bool = True


class SaveVersionRequest(BaseModel):
    test_id: str = Field(..., min_length=1, max_length=64)
    script_source: str = ""
    data: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class RestoreVersionRequest(BaseModel):
    test_id: str = Field(..., min_length=1, max_length=64)
    version_no: int = Field(..., ge=1)


def _run_config_readme(case_count: int, nexus_config_json: str, has_data: bool) -> str:
    try:
        base = json.loads(nexus_config_json or "{}").get("baseURL", "") or "(recorded default)"
    except Exception:
        base = "(recorded default)"
    return (
        "# Nexus — configured run\n\n"
        "Pre-configured to run the selected scripts against your chosen environment "
        "and data — no code edits needed.\n\n"
        f"- Base URL: {base}  (nexus.config.json; override any time with NEXUS_BASE_URL)\n"
        f"- Data overrides: {'vkpower.data.json' if has_data else 'none — using the observed values'}\n"
        f"- Scripts: {case_count}\n\n"
        "## Run\n\n"
        "```bash\nnpm install\nnpx playwright install --with-deps\nnpx playwright test\n```\n\n"
        "Results upload to the Nexus Grounded Triage board if you also set "
        "NEXUS_ENDPOINT, NEXUS_TOKEN and NEXUS_ARTIFACT_ID (see .env.example).\n"
    )


async def _active_edited_map(session, *, artifact_id: str) -> dict:
    """test_id -> {spec_path, script_source} for tests with an active edited
    version (Phase C). Materialized as plain dicts INSIDE the session so the
    values survive after the session/commit closes."""
    active = await script_versions.active_versions_for_artifact(
        session, artifact_id=artifact_id,
    )
    return {
        tid: {"spec_path": row.spec_path, "script_source": row.script_source}
        for tid, row in active.items() if row.script_source
    }


def _env_run_label(env_context: dict | None) -> str:
    """The run's ``environment`` label for the cross-env parity axis — the Environment
    Profile name when a profile is bound, else the default runner label (so today's
    single-env runs are unchanged)."""
    if isinstance(env_context, dict):
        name = str(env_context.get("name") or "").strip()
        if name:
            return name[:100]
    return "nexus-runner"


def _with_env_assertion(tc, env_assertion: dict):
    """Return the case with ``env_assertion`` attached (extra field → compile_case
    reads it). One env per run, so the same HARD env-pin rides every case."""
    try:
        return type(tc)(**{**tc.model_dump(), "env_assertion": dict(env_assertion)})
    except Exception:
        return tc


def _norm_env_cookie(c: dict) -> dict:
    """Normalize an Environment Profile routing cookie into a valid Playwright
    storageState cookie (defaults for the optional fields)."""
    return {
        "name": str(c.get("name", "")), "value": str(c.get("value", "")),
        "domain": str(c.get("domain", "")), "path": str(c.get("path") or "/"),
        "expires": c.get("expires", -1), "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", False)), "sameSite": c.get("sameSite", "Lax"),
    }


def _merge_env_cookies(storage_state: str | None, cookies: list) -> str:
    """Merge env routing cookies into the run's storageState JSON (→ vkpower.auth.json,
    which the config already self-detects). Preserves any captured auth session; the
    env routing cookies (Gloo/canary) are added so the run lands on the target env."""
    try:
        ss = json.loads(storage_state) if storage_state and storage_state.strip() else {}
    except Exception:
        ss = {}
    if not isinstance(ss, dict):
        ss = {}
    existing = ss.get("cookies") if isinstance(ss.get("cookies"), list) else []
    merged: dict = {(c.get("name"), c.get("domain"), c.get("path", "/")): c for c in existing}
    for c in cookies or ():
        if isinstance(c, dict) and c.get("name"):
            nc = _norm_env_cookie(c)
            merged[(nc["name"], nc["domain"], nc["path"])] = nc
    ss["cookies"] = list(merged.values())
    ss.setdefault("origins", ss.get("origins") or [])
    return json.dumps(ss)


def _configured_files(cases, field_meta, base_url: str, data: dict,
                      data_by_test: dict | None = None,
                      browsers=None, headed: bool = False,
                      workers=None, retries=None,
                      edited: dict | None = None,
                      storage_state: str | None = None,
                      env_context: dict | None = None,
                      auth_config: dict | None = None) -> dict:
    """Parametrized bundle + nexus.config.json (chosen base URL) + vkpower.data.json
    + run README. Shared by the download and the server-side runner so both run
    exactly the same thing. Browser projects / headed / workers / retries are
    baked into playwright.config.ts. vkpower.data.json is two-tier:
    {"_global": {...defaults}, "<test_id>": {...per-test overrides}} — defaults
    stay the OBSERVED values, so an empty data file runs identically.

    `edited` = {test_id: {"spec_path","script_source"}} overrides the compiled
    spec for any test that has an active edited version (Phase C). Path keying is
    identical to compile_manifest, so the owned source lands where the runner
    expects it; un-edited tests keep the deterministic compiler output.

    `env_context` (multi-env) = a RESOLVED Environment Profile from qe-central
    ({base_url, cookies, data_overrides, env_assertion, ...}). None ⇒ byte-identical
    to a single-env run. When present it REBINDS the same flow: env base_url,
    data_overrides merged into the run data, the routing cookies folded into the
    run's storageState, and the HARD env-assertion attached to every case (RED if
    the routing landed on the wrong env)."""
    _env_sidecar: dict = {}
    if env_context:
        if env_context.get("base_url"):
            base_url = str(env_context["base_url"]).strip()
        if env_context.get("data_overrides"):
            data = {**(data or {}), **dict(env_context["data_overrides"])}
        _ea = env_context.get("env_assertion")
        if isinstance(_ea, dict) and _ea:
            cases = [_with_env_assertion(tc, _ea) for tc in cases]
        if env_context.get("cookies"):
            storage_state = _merge_env_cookies(storage_state, env_context["cookies"])
        # #8: env routing HEADERS + HTTP basic-auth → Playwright use.extraHTTPHeaders /
        # use.httpCredentials, delivered via the self-detecting vkpower.env.json sidecar
        # the config auto-loads. httpCredentials is a SECRET (basic-auth password), so
        # it rides the SAME server-run-only path as vkpower.auth.json (env_context is
        # never passed on a download/CI-bundle path) and is gitignored in the bundle.
        _hdrs = env_context.get("headers")
        if isinstance(_hdrs, dict) and _hdrs:
            _env_sidecar["extraHTTPHeaders"] = {
                str(k): str(v) for k, v in _hdrs.items() if str(k).strip()
            }
        _creds = env_context.get("http_credentials")
        if isinstance(_creds, dict) and str(_creds.get("username") or "").strip():
            _cred_obj = {
                "username": str(_creds.get("username", "")),
                "password": str(_creds.get("password", "")),
            }
            # SCOPE the basic-auth to the env's ORIGIN. Without `origin` Playwright
            # replays the password to ANY origin that answers 401 — so a cross-origin
            # asset (CDN/SSO/iframe) 401ing would leak the sealed env password. Pin it
            # to the run's effective base_url origin (env base_url wins above).
            try:
                _p = urlsplit(base_url or "")
                if _p.scheme and _p.netloc:
                    _cred_obj["origin"] = f"{_p.scheme}://{_p.netloc}"
            except Exception:
                pass
            _env_sidecar["httpCredentials"] = _cred_obj
    files = compile_project(
        cases, field_meta, parametrize=True, base_url_default=(base_url or "").strip(),
        projects=browsers, headed=bool(headed), workers=workers,
        retries=int(retries or 0),
    )
    if edited:
        id_to_path = {s["test_id"]: s["path"] for s in compile_manifest(cases, field_meta).get("scripts", [])}
        for tid, ev in edited.items():
            src = (ev or {}).get("script_source")
            if not src:
                continue
            path = id_to_path.get(tid) or (ev or {}).get("spec_path")
            if path and path in files:  # only override specs actually in this run
                files[path] = src
    nexus_data: dict = {
        "_global": {str(k): str(v) for k, v in (data or {}).items() if str(k).strip()},
    }
    for tid, fields in (data_by_test or {}).items():
        if not str(tid).strip() or not isinstance(fields, dict):
            continue
        row = {str(k): str(v) for k, v in fields.items() if str(k).strip()}
        if row:
            nexus_data[str(tid)] = row
    has_data = bool(nexus_data["_global"]) or len(nexus_data) > 1
    files["vkpower.data.json"] = json.dumps(nexus_data, indent=2, sort_keys=True) + "\n"
    files["README.md"] = _run_config_readme(
        len(cases), files.get("nexus.config.json", ""), has_data,
    )
    # AUTH PERMANENCE — form-login takes precedence over a captured session. When
    # a form-login profile is bound, overwrite vkpower.auth.config.json with the
    # 'form' config so the runner's globalSetup logs in FRESH each run (creds ride
    # the run ENV, never the bundle). This self-renews the session, so we do NOT
    # inject the (possibly-expired) captured storageState — globalSetup is the sole
    # source of truth; the config self-detects the vkpower.auth.json it writes.
    if auth_config:
        files["vkpower.auth.config.json"] = json.dumps(auth_config, indent=2) + "\n"
    # Inject a captured authenticated session for SERVER runs ONLY (the caller
    # passes storage_state from the artifact's auth profile). The generated config
    # self-detects vkpower.auth.json; downloaded bundles never receive one.
    elif storage_state and storage_state.strip():
        files["vkpower.auth.json"] = storage_state
    # #8 multi-env sidecar — ONLY when the bound Environment Profile carries routing
    # headers / basic-auth. Absent ⇒ no file ⇒ the config's self-detection is a no-op
    # (byte-identical to a single-env run). Written after compile so it never enters
    # compile_project's frozen file set.
    if _env_sidecar:
        files["vkpower.env.json"] = json.dumps(_env_sidecar)
    return files


# Internal endpoint the runner's bundled reporter posts results to (grounded
# triage). platform-api serves /api/v1/* directly on 8091.
_INGEST_BASE = os.getenv("NEXUS_INTERNAL_INGEST_BASE", "http://platform-api:8091")

# noVNC viewer path (runner's websockify served via nginx /live/); RELATIVE so
# the browser opens it on the portal origin (same-origin iframe).
_LIVE_PATH = "/live/vnc.html?autoconnect=1&resize=remote&path=live/websockify"

# Interactive capture stream (runner :6081 via nginx /auth-live). NOT view-only —
# the operator logs in here. RELATIVE so the portal opens it same-origin.
_AUTH_LIVE_PATH = "/auth-live/vnc.html?autoconnect=1&resize=remote&path=auth-live/websockify"


async def _run_storage_state(request, artifact_id: str, tenant_id: str) -> str | None:
    """Decrypt the artifact's auth profile (captured session) for injection into a
    SERVER run. Returns the storageState JSON, or None. Never raises (auth is
    optional — absent → an unauthenticated run, exactly as before)."""
    envelope = getattr(request.app.state, "envelope_service", None)
    async with tenant_scoped_session(tenant_id) as session:
        return await auth_profiles.get_storage_state(
            session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
        )


async def _run_form_login(request, artifact_id: str, tenant_id: str) -> tuple[dict | None, dict]:
    """AUTH PERMANENCE — the artifact's form-login profile split into
    (auth_config, login_env) for a SERVER run, or (None, {}) if none is bound.

    When present, the run's globalSetup logs in FRESH (credentials from the run
    env, form shape from vkpower.auth.config.json), so a run's green reproduces
    without re-importing an expiring session. Never raises — absent/undecryptable
    falls back to the captured-session path exactly as before."""
    envelope = getattr(request.app.state, "envelope_service", None)
    async with tenant_scoped_session(tenant_id) as session:
        cfg = await auth_profiles.get_form_login(
            session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
        )
    if not cfg:
        return None, {}
    return auth_profiles.build_form_login_bundle(cfg)


async def _persona_auth_resolve(request, artifact_id: str, tenant_id: str,
                                persona_id: str, environment_id: str,
                                environment: dict | None = None) -> dict:
    """Resolve (persona, environment) → the login bundle AND a verdict on it.

    ``{auth_config, login_env, state, recipe, has_card}``. ``state`` is
    ``card_state.evaluate`` over the ACTIVE recipe and the DECRYPTED card, so it
    sees what the login will actually be handed — including a slot that exists but
    holds an empty value, which the stored slot-name list cannot show.

    This is the single place both the run gate and the probes learn whether a member
    can log in, so a probe and a run can never disagree about it. Never raises."""
    envelope = getattr(request.app.state, "envelope_service", None)
    out = {"auth_config": None, "login_env": {}, "state": None,
           "recipe": None, "has_card": False}
    if not persona_id:
        cfg, env = await _run_form_login(request, artifact_id, tenant_id)
        out["auth_config"], out["login_env"] = cfg, env
        return out
    try:
        async with tenant_scoped_session(tenant_id) as session:
            recipe = await persona_store.get_recipe(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
            card = await persona_store.get_persona_credential(
                session, envelope=envelope, tenant_id=tenant_id,
                persona_id=persona_id, environment_id=environment_id)
            status = await persona_store.credential_status(
                session, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id)
    except Exception as exc:
        _logger.warning("persona.auth_resolve_failed persona=%s err=%s",
                        persona_id, str(exc)[:200])
        return out

    out["recipe"] = recipe
    out["has_card"] = bool(card)
    # A legacy form-login shape is not a slot-checked card; it predates slots and is
    # judged by its own guard, so it is reported as the legacy stand-in rather than
    # diffed against a recipe it was never provisioned for.
    #
    # Decided by WHERE the card came from, not by what it looks like. Sniffing for a
    # key named 'user'/'password' would classify a perfectly ordinary email+password
    # card as legacy the moment `credential_status` returned nothing — a transient DB
    # error would then skip the slot check entirely. `get_persona_credential` returns
    # a legacy profile for exactly one input, the synthetic persona-0.
    legacy = bool(card) and (str(persona_id).startswith("persona0::") or not recipe)
    out["state"] = card_state.evaluate(
        recipe=recipe, card=status, environment=environment,
        legacy_login_available=legacy,
        live_slot_values=(card if (card and not legacy) else None))

    if recipe and card:
        cfg, env = persona_store.build_persona_bundle(recipe, card)
        out["auth_config"], out["login_env"] = cfg, env
        return out
    if card and ("user" in card or "password" in card):
        cfg, env = auth_profiles.build_form_login_bundle(card)
        out["auth_config"], out["login_env"] = cfg, env
        return out
    return out


async def _persona_auth_bundle(request, artifact_id: str, tenant_id: str,
                               persona_id: str, environment_id: str
                               ) -> tuple[dict | None, dict]:
    """Resolve (persona, environment) → (auth_config, login_env) for a run.

    Prefers a login RECIPE (the general, multi-step path) filled from the
    persona's credential card; falls back to the legacy form-login when the
    persona is the synthetic persona-0. Returns (None, {}) when nothing is
    bound. Shared by the P1 recipe probe and the P2 run dispatch — one resolver,
    so a probe and a real run authenticate identically. Never raises.

    Thin wrapper over ``_persona_auth_resolve`` for callers that only need the
    bundle. Anything that must REFUSE a run should use the resolver and read its
    ``state`` — a bundle alone cannot tell a covering card from one that will make
    the interpreter skip the entire login."""
    r = await _persona_auth_resolve(request, artifact_id, tenant_id,
                                    persona_id, environment_id)
    return r["auth_config"], r["login_env"]


def _probe_diag(where: str, run_id: str, output: str, verdict: dict) -> None:
    """Say WHY a login probe could not read its own outcome.

    UNREADABLE blocks proof and tells the operator nothing — it cannot distinguish
    "the runner produced nothing" from "the login never ran" from "our markers
    changed". Logs OUR OWN emitted markers only, never page content, so no
    credential or application data can travel into a log line."""
    if verdict.get("outcome") != login_probe.UNREADABLE:
        return
    lines = [ln.strip()[:160] for ln in output.splitlines() if "[nexus-auth]" in ln][:5]
    _logger.warning(
        "test_factory.login_probe.unreadable where=%s run=%s output_len=%d auth_lines=%r",
        where, run_id, len(output), lines)


async def _persona_preflight(request, artifact_id: str, tenant_id: str, cases,
                             visits, edited_map, base_url: str,
                             auth_config: dict, login_env: dict, token: str) -> dict:
    """Prove a persona can log in BEFORE running the suite. Runs ONE carrier test
    headless and reads the runner's login log.

    Returns ``{ok, proven, outcome, attribution, detail, note}``. ``ok`` means the
    login attempt did not fail; ``proven`` means the recorded logged-in landing was
    actually REACHED. They are different, and only ``proven`` may stamp a card
    verified — the interpreter prints ``recipe login OK`` for steps that merely
    replayed, and prints ``form login OK`` unconditionally after a click with no
    assertion at all, so treating either as proof marks a card verified when nobody
    logged in. On a permissive test app that mistake is invisible.

    A failure here is ours — a card that does not cover the login, an unrecorded
    interstitial, or recipe drift — never app-blame."""
    carrier = cases[:1]
    if not carrier:
        return {"ok": False, "detail": "no carrier test available for preflight"}
    files = _configured_files(
        carrier, build_field_meta(visits), base_url, {},
        browsers=["chromium"], headed=False, workers=1, retries=0,
        edited=edited_map, storage_state=None, auth_config=auth_config)
    pf_run = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": pf_run,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "persona-preflight",
        "NEXUS_VIDEO": "off", "NEXUS_SCREENSHOT": "off",
        **login_env,
    }
    try:
        res = await runner_client.run_suite(files, env, timeout_ms=90000)
    except Exception as exc:
        return {"ok": False, "proven": False, "outcome": login_probe.NO_ATTEMPT,
                "attribution": "configuration",
                "detail": f"preflight runner error: {exc}",
                "note": "The preflight could not be run, so nothing is claimed about this login."}
    output = str((_RUNNER_JOBS.get(pf_run) or {}).get("output") or "")
    if not output:
        try:
            output = str((res or {}).get("output") or "")
        except Exception:
            output = ""
    v = login_probe.read_outcome(output)
    _probe_diag("preflight", pf_run, output, v)
    # A login that merely REPLAYED is allowed to run — refusing it would strand every
    # recipe recorded without a logged-in checkpoint — but it is not proof, so it can
    # never stamp the card. That split is the whole point of F3.
    return {
        "ok": v["outcome"] in (login_probe.PROVEN, login_probe.STEPS_ONLY),
        "proven": v["proven"], "outcome": v["outcome"],
        "attribution": v["attribution"], "detail": v["detail"], "note": v["note"],
        "step": v["step"], "slots": v["slots"],
    }

# Transient run status for the live "running -> done" indicator. The durable
# record is the ingested run (triage board); this only drives the UI spinner, so
# loss on restart / across workers is harmless.
_RUNNER_JOBS: dict[str, dict] = {}
_RUNNER_TASKS: set = set()


# Certification-run resilience (learned from job a66d0e69, 2026-07-25): a
# 48-case certification hit the 240s default runner cap and DIED SILENTLY —
# INFO logs were suppressed at the deployed level, there was no retry, and the
# reporter only ingests at run end, so the quarantine that was mid-flight never
# landed and the client met the broken case two minutes later.
_CERT_RETRYABLE_STATUSES = frozenset({"error", "timed_out"})
_CERT_MAX_ATTEMPTS = 3
_CERT_RETRY_BACKOFF_S = 30.0

# ── Concurrency budget for BACKGROUND recovery runs (cert + auto-heal + regen) ──
# They share the ONE runner with client-initiated runs. An unbounded fan-out — a
# certification on every regenerate + an auto-heal per failing scenario — thrashed
# the runner: client runs 409'd and clean measurement was impossible (2026-07-25).
# The budget: at most ONE background recovery run holds the runner at a time
# (this lock), acquired with a bounded wait so a wedged job never blocks forever;
# and a per-artifact cert cooldown so rapid regenerates coalesce into one cert.
# Client runs (playwright_run / run_live / heal_step) NEVER take this lock — they
# always get the runner ahead of background recovery.
_RECOVERY_RUNNER_LOCK = asyncio.Lock()
_RECOVERY_LOCK_WAIT_S = 1200.0        # give up the slot rather than queue forever
_CERT_COOLDOWN_S = 120.0
_CERT_LAST_DISPATCH: dict[str, float] = {}   # artifact_id -> loop time of last cert


def _loop_now() -> float:
    try:
        return asyncio.get_event_loop().time()
    except Exception:  # pragma: no cover — no running loop
        return 0.0


def _cert_timeout_ms(case_count: int) -> int:
    """Per-suite certification cap: base 2 min + 30s/case, ceiling 30 min.
    A 48-case suite gets ~26 min — the 240s default is for SMALL ad-hoc runs
    and is exactly what killed certification job a66d0e69."""
    return min(1_800_000, 120_000 + 30_000 * max(0, int(case_count)))


async def _certify_generated_suite(
    *, request: Request, artifact_id: str, tenant_id: str, token: str,
) -> None:
    """P0.3 — certification-before-client ("a test must prove itself on the
    baseline before it may judge the application").

    Runs the freshly generated ACTIVE suite once against the app's own
    baseline, tagged ``environment='certification'``.  Results flow through the
    SAME reporter → ingest path as every run; the summary keeps certification
    runs OUT of client-facing stats, and ``product_quarantined_scenarios`` /
    the exploratory gate turn certification outcomes into run-gate decisions —
    so the first failure of a defective generated script is OURS, never the
    client's.

    Resilient by design: per-suite scaled timeout, up to ``_CERT_MAX_ATTEMPTS``
    attempts on runner error/timeout (a busy or freshly-restarted runner must
    not silently cost the suite its certification), and WARNING-level
    lifecycle logs so the trail is visible under the deployed log level.
    Fire-and-forget: never blocks or fails generation.
    """
    try:
        async with tenant_scoped_session(tenant_id) as session:
            cases = await factory_service.load_active_production_cases(
                session, artifact_id=artifact_id,
            )
            visits, _ = await factory_service._load_current_pages_and_actions(
                session, artifact_id=artifact_id,
            )
            edited_map = await _active_edited_map(session, artifact_id=artifact_id)
        if not cases or not visits:
            _logger.warning(
                "test_factory.certification.skipped artifact=%s reason=no_cases_or_visits",
                artifact_id,
            )
            return
        # Baseline URL — the app's recorded FULL host (url_host, not the
        # registrable canonical_host which yields the dead https://sslip.io).
        base_url = _recorded_origin(visits)
        if not base_url:
            _logger.warning(
                "test_factory.certification.skipped artifact=%s reason=no_recorded_host",
                artifact_id,
            )
            return
        storage_state = await _run_storage_state(request, artifact_id, tenant_id)
        auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
        files = _configured_files(
            cases, build_field_meta(visits), base_url, None,
            browsers=["chromium"], headed=False, workers=2, retries=0,
            edited=edited_map, storage_state=storage_state, auth_config=auth_config,
        )
        timeout_ms = _cert_timeout_ms(len(cases))

        # Concurrency budget: hold the shared recovery-runner lock for the whole
        # attempt loop so cert + auto-heal never contend for the runner. Bounded
        # wait — if another recovery run is wedged, give up rather than pile on.
        try:
            await asyncio.wait_for(_RECOVERY_RUNNER_LOCK.acquire(),
                                   timeout=_RECOVERY_LOCK_WAIT_S)
        except asyncio.TimeoutError:
            _logger.warning(
                "test_factory.certification.skipped artifact=%s reason=recovery_"
                "runner_busy (another recovery run held the slot > %ds)",
                artifact_id, int(_RECOVERY_LOCK_WAIT_S))
            return
        try:
          for attempt in range(1, _CERT_MAX_ATTEMPTS + 1):
            run_id = uuid.uuid4().hex
            env = {
                "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics",
                "NEXUS_TOKEN": token or "",
                "NEXUS_ARTIFACT_ID": artifact_id,
                "NEXUS_RUN_ID": run_id,
                "NEXUS_BASE_URL": base_url,
                "NEXUS_ENV": "certification",
                **login_env,
                # Certification is the run the Trust Block points at — the one a
                # client is asked to believe. It should carry the richest
                # evidence we can produce, not the thinnest.
                "NEXUS_VIDEO": os.getenv("NEXUS_CERT_VIDEO", "on"),
                "NEXUS_SCREENSHOT": os.getenv("NEXUS_CERT_SCREENSHOT", "on"),
            }
            await _register_job(run_id, {
                "run_id": run_id, "status": "running", "artifact_id": artifact_id,
                "tenant_id": tenant_id, "kind": "certification",
                "target": base_url, "scripts": len(cases), "exit_code": None,
                "output": "", "steps_completed": 0, "total_tests": len(cases),
            })
            _logger.warning(
                "test_factory.certification.dispatched artifact=%s run=%s cases=%d "
                "target=%s timeout_ms=%d attempt=%d/%d",
                artifact_id, run_id, len(cases), base_url, timeout_ms,
                attempt, _CERT_MAX_ATTEMPTS,
            )
            await _execute_run(run_id, files, env, timeout_ms=timeout_ms)
            status = str((_RUNNER_JOBS.get(run_id) or {}).get("status") or "error")
            if status not in _CERT_RETRYABLE_STATUSES:
                _logger.warning(
                    "test_factory.certification.completed artifact=%s run=%s status=%s",
                    artifact_id, run_id, status,
                )
                return
            _logger.warning(
                "test_factory.certification.attempt_failed artifact=%s run=%s "
                "status=%s attempt=%d/%d%s",
                artifact_id, run_id, status, attempt, _CERT_MAX_ATTEMPTS,
                " — retrying" if attempt < _CERT_MAX_ATTEMPTS else " — GIVING UP "
                "(suite stays uncertified; exploratory cases remain gated; "
                "re-trigger via POST /certify)",
            )
            if attempt < _CERT_MAX_ATTEMPTS:
                await asyncio.sleep(_CERT_RETRY_BACKOFF_S)
        finally:
            _RECOVERY_RUNNER_LOCK.release()
    except Exception:
        _logger.exception(
            "test_factory.certification.failed artifact=%s (generation unaffected)",
            artifact_id,
        )


def _identity_label(body, persona_id: str) -> str:
    """WHO this run authenticated as, for the report's Trust Block.

    A result you cannot attribute to an identity is weak evidence: "it passed" means
    little without "as whom". ``persona`` alone cannot separate a login recorded
    moments ago from the artifact's long-stored session, and those have very
    different shelf lives — one is a deliberate act by the operator standing there,
    the other may be days old.

    Only ``recorded_session`` is honoured from the request (set server-side by
    Record + Run); everything else is DERIVED, so a caller cannot label a run with an
    identity it does not have.
    """
    if persona_id:
        return "member"
    if str(getattr(body, "identity", "") or "").strip() == "recorded_session":
        return "recorded_session"
    return "stored_session"


def _recorded_origin(visits) -> str:
    """The app ORIGIN (scheme://host) a server-side run must navigate to.

    Uses ``url_host`` — the ACTUAL recorded host — NOT ``canonical_host``, which
    is the registrable/politeness domain (e.g. 'sslip.io' for
    'vkpowerlife.35-186-147-245.sslip.io'). Deriving the base URL from
    canonical_host produced ``https://sslip.io`` → nginx 404, so every
    certification / diagnosis / auto-heal run navigated to a dead host and died
    at the first field (proven 2026-07-25 by a diagnosis run's 404 screenshot).
    Dotless hosts are internal container names (http); public hosts https."""
    host = ""
    for v in (visits or []):
        host = (getattr(v, "url_host", "") or "").strip()
        if host:
            break
    if not host:  # last resort — a capture with no full host recorded
        for v in (visits or []):
            host = (getattr(v, "canonical_host", "") or "").strip()
            if host:
                break
    if not host:
        return ""
    return f"{'http' if '.' not in host else 'https'}://{host}"


def _spawn_certification(request: Request, artifact_id: str, tenant_id: str) -> None:
    """Schedule the post-generation certification run (fire-and-forget).

    Debounced per artifact: rapid regenerates (or an auto-heal + a regenerate)
    within the cooldown coalesce into ONE certification instead of stacking
    certs that fight the runner. POST /certify is the explicit override for a
    deliberate re-certify."""
    now = _loop_now()
    last = _CERT_LAST_DISPATCH.get(artifact_id, 0.0)
    if now - last < _CERT_COOLDOWN_S:
        _logger.warning(
            "test_factory.certification.debounced artifact=%s (a cert was "
            "dispatched %.0fs ago < %.0fs cooldown)",
            artifact_id, now - last, _CERT_COOLDOWN_S)
        return
    _CERT_LAST_DISPATCH[artifact_id] = now
    token = _bearer(request)
    task = asyncio.create_task(_certify_generated_suite(
        request=request, artifact_id=artifact_id, tenant_id=tenant_id, token=token,
    ))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)


# ── Auto-heal driver (V2 reflex arc, founder-approved FULL-AUTO 2026-07-25) ──
# The unattended version of the TrueFix flow a human drives from the Studio:
#   capture re-run (failure-state a11y) → grounded candidate (re-anchor or
#   control-kind) → NEVER-GREEN-WASH check (assertions byte-unchanged) →
#   headless verify run → persist ACTIVE only on green + verdict gate →
#   re-certify the suite. A candidate that cannot be grounded, or does not
#   PROVE green, changes NOTHING — the dossier stays open for a human.
# All driver runs are tagged environment='diagnosis': excluded from client
# stats AND from recovery-orchestrator triggering (no reflex recursion).
_AUTO_HEAL_ATTEMPTED: set = set()   # (tenant, artifact, scenario, step) — one try per process
_AUTO_HEAL_LOCK = asyncio.Lock()    # the runner is shared — one unattended heal at a time
_ENV_DIAGNOSIS = "diagnosis"


async def _auto_heal_scenario(
    *, request: Request, artifact_id: str, tenant_id: str, scenario_id: str,
    step_number: int, cause: str, token: str,
) -> None:
    key = (tenant_id, artifact_id, scenario_id, int(step_number))
    if key in _AUTO_HEAL_ATTEMPTED:
        _logger.warning(
            "test_factory.auto_heal.skipped scenario=%s step=%s reason=already_attempted",
            scenario_id, step_number)
        return
    _AUTO_HEAL_ATTEMPTED.add(key)
    # Share the ONE recovery-runner budget with certification (serialize, never
    # thrash the runner). Bounded wait — if a cert holds the slot too long, drop
    # this heal rather than queue forever; the dossier stays open for the next
    # pass or a human.
    try:
        await asyncio.wait_for(_RECOVERY_RUNNER_LOCK.acquire(),
                               timeout=_RECOVERY_LOCK_WAIT_S)
    except asyncio.TimeoutError:
        _logger.warning(
            "test_factory.auto_heal.skipped scenario=%s reason=recovery_runner_busy",
            scenario_id)
        return
    try:
        try:
            from ..services.agentic import recovery_store

            async with tenant_scoped_session(tenant_id) as session:
                cases = await factory_service.load_active_production_cases(
                    session, artifact_id=artifact_id)
                visits, _ = await factory_service._load_current_pages_and_actions(
                    session, artifact_id=artifact_id)
                edited_map = await _active_edited_map(session, artifact_id=artifact_id)
            tc = next((c for c in cases
                       if (getattr(c, "test_id", "") or "") == scenario_id), None)
            if tc is None or not visits:
                _logger.warning(
                    "test_factory.auto_heal.skipped scenario=%s reason=no_case_or_visits",
                    scenario_id)
                return
            field_meta = build_field_meta(visits)
            base_url = _recorded_origin(visits)   # url_host, never canonical_host
            if not base_url:
                _logger.warning(
                    "test_factory.auto_heal.skipped scenario=%s reason=no_recorded_host",
                    scenario_id)
                return
            storage_state = await _run_storage_state(request, artifact_id, tenant_id)
            auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
            id_to_path = {s["test_id"]: s["path"]
                          for s in compile_manifest([tc], field_meta).get("scripts", [])}
            spec_path = id_to_path.get(scenario_id, "")

            # ── 1) CAPTURE: failure-state a11y snapshot (headless, this case) ──
            capture_spec = compile_case(tc, field_meta, parametrize=True, heal_capture=True)
            cap_run = uuid.uuid4().hex
            cap_files = _configured_files(
                [tc], field_meta, base_url, None,
                browsers=["chromium"], headed=False, workers=1, retries=0,
                edited={**edited_map,
                        scenario_id: {"spec_path": spec_path, "script_source": capture_spec}},
                storage_state=storage_state, auth_config=auth_config,
            )
            await _register_job(cap_run, {
                "run_id": cap_run, "status": "running", "artifact_id": artifact_id,
                "tenant_id": tenant_id, "kind": "diagnosis", "target": base_url,
                "scripts": 1, "exit_code": None, "output": "",
                "steps_completed": 0, "total_tests": 1,
            })
            _logger.warning(
                "test_factory.auto_heal.capture artifact=%s scenario=%s step=%s run=%s",
                artifact_id, scenario_id, step_number, cap_run)
            try:
                _res = await runner_client.run_suite(cap_files, {
                    "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
                    "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": cap_run,
                    "NEXUS_BASE_URL": base_url, "NEXUS_ENV": _ENV_DIAGNOSIS,
                    **login_env,
                    "NEXUS_HEAL_CAPTURE": "1",
                    "NEXUS_HEAL_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/heal-capture",
                }, timeout_ms=180000)
                _j = _RUNNER_JOBS.get(cap_run)
                if _j is not None:
                    _j.update(status=str(_res.get("status") or "done"),
                              exit_code=_res.get("exit_code"))
                await _persist_job(cap_run)
            except Exception as exc:
                _j = _RUNNER_JOBS.get(cap_run)
                if _j is not None:
                    _j.update(status="error", output=f"capture error: {exc}"[-800:])
                await _persist_job(cap_run)
                _logger.warning(
                    "test_factory.auto_heal.capture_failed scenario=%s err=%s",
                    scenario_id, str(exc)[:200])
                return

            # ── 2) CANDIDATE: grounded re-anchor first, else control-kind ─────
            candidate = ""
            fixmeta: dict = {}
            fix_kind = ""
            heal_note = ""
            reanchor = self_heal.resolve_reanchor_for_step(
                tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
                baseline_step=self_heal._baseline_step(tc, step_number),
                field_meta=field_meta,
            )
            try:
                if reanchor:
                    candidate, fixmeta = self_heal.build_reanchor_candidate(
                        tc, field_meta, step_number, reanchor)
                    fix_kind = "reanchor"
                    heal_note = (
                        f"Auto-healed (unattended): re-anchored "
                        f"'{fixmeta.get('label', '')}' to '{reanchor['name']}', "
                        "verified green + re-certified")
                else:
                    candidate, fixmeta = self_heal.build_candidate_for_step(
                        tc, field_meta, step_number)
                    fix_kind = "control_kind_fix"
                    heal_note = ("Auto-healed (unattended): control-kind fix, "
                                 "verified green + re-certified")
            except Exception as exc:
                _logger.warning(
                    "test_factory.auto_heal.no_grounded_fix scenario=%s step=%s err=%s",
                    scenario_id, step_number, str(exc)[:200])
                await _regenerate_combination(
                    request=request, artifact_id=artifact_id, tenant_id=tenant_id,
                    scenario_id=scenario_id, step_number=step_number, cause=cause)
                return
            baseline_spec = compile_case(tc, field_meta, parametrize=True)
            if not candidate or candidate == baseline_spec:
                _logger.warning(
                    "test_factory.auto_heal.no_grounded_fix scenario=%s step=%s "
                    "reason=candidate_empty_or_identical",
                    scenario_id, step_number)
                await _regenerate_combination(
                    request=request, artifact_id=artifact_id, tenant_id=tenant_id,
                    scenario_id=scenario_id, step_number=step_number, cause=cause)
                return
            # NEVER-GREEN-WASH invariant: a heal may move LOCATORS, never oracles.
            ok, why = self_heal.assert_assertions_unchanged(baseline_spec, candidate)
            if not ok:
                _logger.warning(
                    "test_factory.auto_heal.refused scenario=%s step=%s "
                    "reason=assertions_changed detail=%s", scenario_id, step_number, why)
                return

            # ── 3) VERIFY: headless candidate run, correlated by run id ───────
            ver_run = uuid.uuid4().hex
            ver_files = _configured_files(
                [tc], field_meta, base_url, None,
                browsers=["chromium"], headed=False, workers=1, retries=0,
                edited={**edited_map,
                        scenario_id: {"spec_path": spec_path, "script_source": candidate}},
                storage_state=storage_state, auth_config=auth_config,
            )
            await _register_job(ver_run, {
                "run_id": ver_run, "status": "running", "artifact_id": artifact_id,
                "tenant_id": tenant_id, "kind": "auto-heal", "target": base_url,
                "scripts": 1, "exit_code": None, "output": "",
                "steps_completed": 0, "total_tests": 1,
            })
            _logger.warning(
                "test_factory.auto_heal.verify artifact=%s scenario=%s fix=%s run=%s",
                artifact_id, scenario_id, fix_kind, ver_run)
            try:
                _res = await runner_client.run_suite(ver_files, {
                    "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
                    "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": ver_run,
                    "NEXUS_BASE_URL": base_url, "NEXUS_ENV": _ENV_DIAGNOSIS,
                    **login_env,
                }, timeout_ms=180000)
                _j = _RUNNER_JOBS.get(ver_run)
                if _j is not None:
                    _j.update(status=str(_res.get("status") or "done"),
                              exit_code=_res.get("exit_code"))
                await _persist_job(ver_run)
            except Exception as exc:
                _j = _RUNNER_JOBS.get(ver_run)
                if _j is not None:
                    _j.update(status="error", output=f"verify error: {exc}"[-800:])
                await _persist_job(ver_run)
                _logger.warning(
                    "test_factory.auto_heal.verify_failed scenario=%s err=%s",
                    scenario_id, str(exc)[:200])
                return
            ev = None
            for _ in range(10):   # ~15s for the reporter's ingest to land
                await asyncio.sleep(1.5)
                async with tenant_scoped_session(tenant_id) as session:
                    real_run_id = await find_run_by_ci_run_id(
                        session, artifact_id=artifact_id, tenant_id=tenant_id,
                        ci_run_id=ver_run)
                    if real_run_id is None:
                        continue
                    timeline = await build_run_timeline_by_id(
                        session, artifact_id=artifact_id, tenant_id=tenant_id,
                        run_id=real_run_id)
                ev = self_heal.evaluate_heal(timeline, scenario_id, step_number)
                break

            if not ev or not ev.get("healed"):
                reason = (ev or {}).get("reason") or "verification run not correlated"
                _logger.warning(
                    "test_factory.auto_heal.not_proven scenario=%s step=%s reason=%s "
                    "— nothing changed", scenario_id, step_number, str(reason)[:200])
                try:
                    async with tenant_scoped_session(tenant_id) as session:
                        await flywheel_ledger.record_label(
                            session, tenant_id=tenant_id, decision_point=fix_kind,
                            artifact_id=artifact_id, scenario_id=scenario_id,
                            verified_green=False, human_decision_enum="not_promoted",
                            engine_verdict_enum=((ev or {}).get("verdict") or ""),
                            git_commit=os.getenv("NEXUS_GIT_COMMIT", ""))
                        await session.commit()
                except Exception:
                    pass
                await _regenerate_combination(
                    request=request, artifact_id=artifact_id, tenant_id=tenant_id,
                    scenario_id=scenario_id, step_number=step_number, cause=cause)
                return

            # ── 4) PERSIST (full-auto policy) + close the dossier + re-prove ──
            async with tenant_scoped_session(tenant_id) as session:
                row = await script_versions.save_new_version(
                    session, artifact_id=artifact_id, tenant_id=tenant_id,
                    session_id="", test_case_id=scenario_id,
                    spec_path=spec_path, script_source=candidate,
                    data_json={}, author="nexus-auto-heal", note=heal_note,
                    # Founder-approved FULL-AUTO: the fix activates on the
                    # DOUBLE proof — step green + verdict gate here, and the
                    # whole-suite certification dispatched below (the client
                    # gates keep holding until that passes).
                    proposed=False,
                )
                await heal_evidence.record_heal_event(
                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                    event_type="heal_persisted", actor="nexus-auto-heal",
                    scenario_id=scenario_id, step_number=int(step_number),
                    fix_kind=fix_kind,
                    before_locator=str(fixmeta.get("label", "")),
                    after_locator=(reanchor["name"] if reanchor
                                   else str(fixmeta.get("label", ""))),
                    engine_verdict=(ev.get("verdict") or ""), verified_green=True,
                    version_no=getattr(row, "version_no", 0), run_id=ver_run,
                    reason_for_change=heal_note,
                )
                await flywheel_ledger.record_label(
                    session, tenant_id=tenant_id, decision_point=fix_kind,
                    artifact_id=artifact_id, scenario_id=scenario_id,
                    emitted_method_enum=("" if fix_kind == "reanchor" else "selectOption"),
                    verified_green=True, human_decision_enum="auto_approved",
                    engine_verdict_enum=(ev.get("verdict") or ""),
                    git_commit=os.getenv("NEXUS_GIT_COMMIT", ""))
                await recovery_store.record_decision(
                    session, tenant_id=tenant_id,
                    proposal_id=recovery_store._proposal_id(
                        tenant_id, artifact_id, scenario_id, cause),
                    decision="approve", decided_by="nexus-auto-heal",
                    note=heal_note)
                await session.commit()
            _logger.warning(
                "test_factory.auto_heal.HEALED artifact=%s scenario=%s step=%s fix=%s "
                "version=%s — dispatching certification to re-prove the suite",
                artifact_id, scenario_id, step_number, fix_kind,
                getattr(row, "version_no", 0))
            _spawn_certification(request, artifact_id, tenant_id)
        except Exception:
            _logger.exception(
                "test_factory.auto_heal.error artifact=%s scenario=%s (nothing changed)",
                artifact_id, scenario_id)
    finally:
        _RECOVERY_RUNNER_LOCK.release()


async def _regenerate_combination(
    *, request: Request, artifact_id: str, tenant_id: str,
    scenario_id: str, step_number: int, cause: str,
) -> bool:
    """V3 — the truthful repair when heal honestly refuses: a COMBINATION case
    whose option-captured axis targets a field ABSENT from the form state its
    other values produce is REGENERATED without that axis (coverage reduction
    LABELED in name/description/tags), then re-proven through certification.

    Honesty rails live in ``combination_regen.drop_absent_axis``: only
    'available'-provenance steps, only combination cases — a demonstrated step
    failing is a real signal and is never touched. The recovery dossier stays
    OPEN (design-vs-application ambiguity is a human's question); its bundle is
    refreshed with the regeneration record. Returns True if regenerated."""
    key = ("regen", tenant_id, artifact_id, scenario_id, int(step_number))
    if key in _AUTO_HEAL_ATTEMPTED:
        return False
    _AUTO_HEAL_ATTEMPTED.add(key)
    try:
        from datetime import datetime, timezone

        from nexus_sdk.db.models import FactoryTestCaseRow

        from ..services.agentic import recovery_store
        from ..services.test_factory.combination_regen import drop_absent_axis

        async with tenant_scoped_session(tenant_id) as session:
            row = (await session.execute(
                select(FactoryTestCaseRow).where(
                    FactoryTestCaseRow.test_case_id == scenario_id,
                    FactoryTestCaseRow.tenant_id == tenant_id,
                    FactoryTestCaseRow.artifact_id == artifact_id,
                )
            )).scalar_one_or_none()
            if row is None:
                _logger.warning(
                    "test_factory.auto_heal.regen_skipped scenario=%s reason=no_row",
                    scenario_id)
                return False
            result = drop_absent_axis(
                test_case=dict(row.test_case or {}),
                name=str(row.name or ""),
                test_type=str(row.test_type or ""),
                step_number=int(step_number),
                diagnosed_at=datetime.now(timezone.utc).date().isoformat(),
            )
            if result is None:
                _logger.warning(
                    "test_factory.auto_heal.regen_ineligible scenario=%s step=%s "
                    "(non-combination or demonstrated step — dossier stays open "
                    "for human review)", scenario_id, step_number)
                return False
            row.test_case = result.test_case
            row.name = result.name[:500]
            row.step_count = int(result.step_count)
            row.description = str(result.test_case.get("description")
                                  or row.description)[:8000]
            tags = list(row.tags or [])
            if result.tag not in tags:
                tags.append(result.tag)
            row.tags = tags
            row.updated_at = datetime.now(timezone.utc)
            # Refresh the OPEN dossier with the regeneration record (UPSERT on
            # the same (scenario, cause) identity — terminal states respected).
            await recovery_store.persist_scan(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                run_id="", proposals=[{
                    "scenario_id": scenario_id,
                    "step_number": int(step_number),
                    "cause": cause,
                    "kind": "heal_candidate",
                    "suggested_strategy": (
                        f"REGENERATED without axis '{result.dropped_label}'"
                        + (f"='{result.dropped_value}'" if result.dropped_value else "")
                        + " (field absent in this combination's form state); "
                        "re-certifying. OPEN QUESTION for a human: if the field "
                        "SHOULD appear for these values, that is an application "
                        "finding."),
                    "regenerated": True,
                    "auto": True,
                }])
            await session.commit()
        _logger.warning(
            "test_factory.auto_heal.REGENERATED artifact=%s scenario=%s "
            "dropped=%s=%s — dispatching certification to re-prove",
            artifact_id, scenario_id, result.dropped_label, result.dropped_value)
        _spawn_certification(request, artifact_id, tenant_id)
        return True
    except Exception:
        _logger.exception(
            "test_factory.auto_heal.regen_error scenario=%s (nothing changed)",
            scenario_id)
        return False


def _spawn_auto_heal(request: Request, artifact_id: str, tenant_id: str,
                     scenario_id: str, step_number: int, cause: str) -> None:
    """Schedule one unattended heal attempt (fire-and-forget, deduped)."""
    token = _bearer(request)
    task = asyncio.create_task(_auto_heal_scenario(
        request=request, artifact_id=artifact_id, tenant_id=tenant_id,
        scenario_id=scenario_id, step_number=int(step_number),
        cause=cause, token=token,
    ))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)


async def _register_job(run_id: str, job: dict) -> None:
    """Store the job in-memory (fast path) AND mirror it to the durable registry
    (best-effort) so a restart / a second worker can still read its status and
    final heal outcome. The job dict carries 'tenant_id'/'kind' for persistence;
    the status endpoint strips tenant_id from its response."""
    _RUNNER_JOBS[run_id] = job
    await runner_jobs.persist_job(
        tenant_id=job.get("tenant_id", ""), run_id=run_id,
        artifact_id=job.get("artifact_id", ""), kind=job.get("kind", "run"), job=job,
    )
    # Lazy registry hygiene: any durable 'running' row whose process died on a
    # restart would read 'running' forever — sweep them stale on registration.
    _t = asyncio.create_task(runner_jobs.sweep_stale(
        tenant_id=job.get("tenant_id", "")))
    _RUNNER_TASKS.add(_t)
    _t.add_done_callback(_RUNNER_TASKS.discard)


async def _persist_job(run_id: str) -> None:
    """Mirror the current (typically terminal) job state to the durable registry."""
    job = _RUNNER_JOBS.get(run_id)
    if not job:
        return
    await runner_jobs.persist_job(
        tenant_id=job.get("tenant_id", ""), run_id=run_id,
        artifact_id=job.get("artifact_id", ""), kind=job.get("kind", "run"), job=job,
    )


async def _execute_run(run_id: str, files: dict, env: dict,
                       timeout_ms: int | None = None) -> None:
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        return
    try:
        result = await runner_client.run_suite(
            files, env, timeout_ms=timeout_ms or 240000)
        job.update(
            status=result.get("status", "error"),
            exit_code=result.get("exit_code"),
            output=(result.get("output") or "")[-4000:],
        )
    except Exception as exc:  # transport / runner down / timeout
        job.update(status="error", output=f"runner error: {exc}"[-1000:])
    await _persist_job(run_id)  # durable terminal status (survives restart / worker)


async def _poll_live(run_id: str) -> None:
    """Mirror the runner's background LIVE run into _RUNNER_JOBS so the UI's
    existing status poll works unchanged. Reads the runner's single-display
    /run-live/status directly."""
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        return
    for _ in range(260):  # ~650s ceiling, above the runner 10-min hard cap
        await asyncio.sleep(2.5)
        try:
            s = await runner_client.live_status()
        except Exception:
            continue
        job["status"] = s.get("status", "running")
        job["output"] = (s.get("output") or "")[-4000:]
        job["exit_code"] = s.get("exit_code")
        if job["status"] not in ("running", "idle"):
            break
    await _persist_job(run_id)  # durable terminal status (survives restart / worker)


async def _poll_heal(run_id: str, ctx: dict) -> None:
    """After the headed candidate run finishes, verify the failing step went GREEN
    and persist the fix as a new immutable version ONLY then. The verification run
    is correlated by run_id (the reporter sets ci_run_id = NEXUS_RUN_ID), NOT
    'whatever ran last' — so a racing ingest can't make us verify the wrong run.
    No green => nothing is written (the prior active version is untouched —
    auto-rollback by construction). Outcome is mirrored to the durable registry so
    it survives a restart / another worker."""
    await _poll_live(run_id)  # mirror runner status into the job until terminal
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        return
    tenant_id = ctx["tenant_id"]
    artifact_id = ctx["artifact_id"]
    try:
        ev = None
        # Wait for THIS run's ingest to land (the reporter posts at run end),
        # correlating by run_id rather than a fixed sleep against 'newest run'.
        for _ in range(10):  # ~15s budget for the reporter to finish ingesting
            await asyncio.sleep(1.5)
            async with tenant_scoped_session(tenant_id) as session:
                real_run_id = await find_run_by_ci_run_id(
                    session, artifact_id=artifact_id, tenant_id=tenant_id, ci_run_id=run_id,
                )
                if real_run_id is None:
                    continue
                timeline = await build_run_timeline_by_id(
                    session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=real_run_id,
                )
            ev = self_heal.evaluate_heal(timeline, ctx["scenario_id"], ctx["step_number"])
            break
        if ev is None:
            job.update(healed=False, heal_version=None,
                       heal_reason="Could not correlate the verification run (no ingested "
                                   "result for this run id) — nothing was changed.")
        elif ev["healed"]:
            async with tenant_scoped_session(tenant_id) as session:
                row = await script_versions.save_new_version(
                    session, artifact_id=artifact_id, tenant_id=tenant_id,
                    session_id="", test_case_id=ctx["scenario_id"],
                    spec_path=ctx["spec_path"], script_source=ctx["candidate"],
                    data_json={}, author="nexus-truefix",
                    note=ctx.get("heal_note")
                    or "Auto-healed: control-kind fix (.fill -> .selectOption), verified green",
                    proposed=True,  # human-gated: not active until a human approves it
                )
                # Part-11 evidence (FAIL-CLOSED, atomic with the version save): if
                # this raises, the async-with rolls back and the heal is NOT promoted.
                await heal_evidence.record_heal_event(
                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                    event_type="heal_persisted", actor="nexus-truefix",
                    scenario_id=ctx["scenario_id"], step_number=ctx.get("step_number", 0),
                    fix_kind=ctx.get("fix_kind", ""),
                    before_locator=ctx.get("before_locator", ""),
                    after_locator=ctx.get("after_locator", ""),
                    engine_verdict=(ev.get("verdict") or ""), verified_green=True,
                    version_no=getattr(row, "version_no", 0), run_id=run_id,
                    reason_for_change=ctx.get("heal_note", ""),
                )
                # Flywheel (default-OFF) — a fix PROVEN green on a headed re-run is a
                # strong, env-grounded positive label. De-identified; self-gated.
                await flywheel_ledger.record_label(
                    session, tenant_id=tenant_id,
                    decision_point=ctx.get("fix_kind", "control_kind_fix"),
                    artifact_id=artifact_id, scenario_id=ctx["scenario_id"],
                    emitted_method_enum=("" if ctx.get("fix_kind") == "reanchor" else "selectOption"),
                    verified_green=True, human_decision_enum="left_pending",
                    engine_verdict_enum=(ev.get("verdict") or ""),
                    git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
                )
                await session.commit()
            job.update(
                healed=True, heal_version=row.version_no, pending_approval=True,
                heal_reason=(
                    f"Verified green on the headed re-run — saved as PROPOSED v{row.version_no}. "
                    "Approve it to make it the active source for runs."
                ),
            )
        else:
            job.update(healed=False, heal_version=None, heal_reason=ev["reason"])
            # Flywheel (default-OFF) — a fix that was attempted but did NOT prove
            # green is a negative label (what doesn't work). De-identified; gated.
            try:
                async with tenant_scoped_session(tenant_id) as session:
                    await flywheel_ledger.record_label(
                        session, tenant_id=tenant_id,
                        decision_point=ctx.get("fix_kind", "control_kind_fix"),
                        artifact_id=artifact_id, scenario_id=ctx["scenario_id"],
                        verified_green=False, human_decision_enum="not_promoted",
                        engine_verdict_enum=(ev.get("verdict") or ""),
                        git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
                    )
                    await session.commit()
            except Exception:
                pass  # capture is best-effort; never affect the heal flow
    except Exception as exc:  # never let a verify error masquerade as a heal
        job.update(healed=False, heal_version=None, heal_reason=f"verify error: {exc}")
    await _persist_job(run_id)  # durable terminal heal outcome (survives restart)


@router.post("/api/v1/test-factory/{artifact_id}/steps/{scenario_id}/{step_number}/heal")
async def heal_step(
    body: RunConfigRequest,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    step_number: int = PathParam(..., ge=0, le=10000),
    user: dict = Depends(get_current_user),
):
    """Nexus TrueFix — APPLY the control-kind fix to ONE test and PROVE it: recompile
    the case (.fill -> .selectOption) and re-run it HEADED on the runner. The candidate
    spec is persisted as a new immutable ScriptVersionRow ONLY if the step passes AND
    the scenario verdict is not a real regression — otherwise nothing is written
    (reversible via /restore; the baseline recording is never mutated). Returns a
    run_id + live_url immediately; poll /playwright/run/{run_id} for the outcome
    (healed / heal_version / heal_reason). One live run at a time."""
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == scenario_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this scenario")

    field_meta = build_field_meta(visits)
    # Choose the grounded fix (Phase B broadens this beyond control-kind): if a
    # failure-state a11y capture resolves a RENAMED control, re-anchor to it;
    # otherwise the control-kind fix. Both are proved green before anything is
    # saved, and neither can override a real regression (the verdict gate holds).
    _bs = self_heal._baseline_step(tc, step_number)
    _reanchor = self_heal.resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=_bs, field_meta=field_meta,
    )
    if _reanchor:
        candidate, fixmeta = self_heal.build_reanchor_candidate(
            tc, field_meta, step_number, _reanchor)
        _heal_note = (
            f"Auto-healed: re-anchored '{fixmeta.get('label', '')}' to the renamed "
            f"control '{_reanchor['name']}', verified green"
        )
    else:
        candidate, fixmeta = self_heal.build_candidate_for_step(tc, field_meta, step_number)
        _heal_note = "Auto-healed: control-kind fix (.fill -> .selectOption), verified green"
    id_to_path = {s["test_id"]: s["path"]
                  for s in compile_manifest([tc], field_meta).get("scripts", [])}
    spec_path = id_to_path.get(scenario_id, "")
    base_url = (body.base_url or "").strip()
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
    # Inject the candidate as a TRANSIENT edited override for just this test — the
    # exact source we'll persist on green. Verify what you save.
    edited = {**edited_map, scenario_id: {"spec_path": spec_path, "script_source": candidate}}
    files = _configured_files(
        [tc], field_meta, base_url, body.data, data_by_test=body.data_by_test,
        browsers=(body.browsers or ["chromium"])[:1], headed=True, workers=1,
        retries=0, edited=edited, storage_state=storage_state, auth_config=auth_config,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": _env_run_label(body.env_context),
        **login_env,
        # Watching it live and KEEPING the recording are different things — a
        # live viewer is gone the moment you look away.
        "NEXUS_VIDEO": "on" if body.video else "off",
        "NEXUS_SCREENSHOT": (
            body.screenshot_mode
            if (body.screenshots and body.screenshot_mode in _SCREENSHOT_MODES)
            else ("on" if body.screenshots else "off")),
    }
    try:
        await runner_client.run_live(files, env)            # 202; raises on 409
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="a live run is already in progress")
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner unreachable: {exc}")
    await _register_job(run_id, {
        "run_id": run_id, "status": "running", "artifact_id": artifact_id,
        "tenant_id": tenant_id, "kind": "heal",
        "target": base_url, "scripts": 1, "exit_code": None, "output": "",
        "steps_completed": 0, "total_tests": 1, "live": True, "heal": True,
        "healed": None, "heal_version": None, "heal_reason": "verifying the fix…",
    })
    ctx = {
        "tenant_id": tenant_id, "artifact_id": artifact_id, "scenario_id": scenario_id,
        "step_number": step_number, "spec_path": spec_path, "candidate": candidate,
        "heal_note": _heal_note, "fix_kind": ("reanchor" if _reanchor else "control_kind_fix"),
        "before_locator": fixmeta.get("label", ""),
        "after_locator": (_reanchor["name"] if _reanchor else (fixmeta.get("label", "") + " (.fill -> .selectOption)")),
    }
    task = asyncio.create_task(_poll_heal(run_id, ctx))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    return {"run_id": run_id, "status": "running", "live_url": _LIVE_PATH,
            "verifying": True, "fix": fixmeta}


@router.post("/api/v1/test-factory/{artifact_id}/steps/{scenario_id}/{step_number}/capture-failure-state")
async def capture_failure_state(
    body: RunConfigRequest,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    step_number: int = PathParam(..., ge=0, le=10000),
    user: dict = Depends(get_current_user),
):
    """Nexus TrueFix (Phase B) — re-run ONE failing test with a11y capture ON so
    the re-anchor resolver can find a RENAMED control. Recompiles the test with the
    gated afterEach (heal_capture=True) and runs it HEADLESS with
    NEXUS_HEAL_CAPTURE=1; the fixture posts the failure-state accessibility tree to
    the transient store. Saves NOTHING (the saved version is untouched). Blocks
    until the run finishes, then reports whether a confident re-anchor was found:
    {captured, nodes, reanchored, reanchor}. The UI then re-runs Analyze, which
    upgrades the diagnosis to SELECTOR_REANCHOR + offers Apply & re-run."""
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == scenario_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this scenario")

    field_meta = build_field_meta(visits)
    capture_spec = compile_case(tc, field_meta, parametrize=True, heal_capture=True)
    id_to_path = {s["test_id"]: s["path"]
                  for s in compile_manifest([tc], field_meta).get("scripts", [])}
    spec_path = id_to_path.get(scenario_id, "")
    base_url = (body.base_url or "").strip()
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
    edited = {**edited_map, scenario_id: {"spec_path": spec_path, "script_source": capture_spec}}
    files = _configured_files(
        [tc], field_meta, base_url, body.data, data_by_test=body.data_by_test,
        browsers=["chromium"], headed=False, workers=1, retries=0,
        edited=edited, storage_state=storage_state, auth_config=auth_config,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
        **login_env,
        "NEXUS_HEAL_CAPTURE": "1",
        "NEXUS_HEAL_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/heal-capture",
    }
    try:
        # Blocks until the run (and the afterEach's awaited POST) completes. The
        # test is expected to FAIL — that's the failure-state we capture.
        await runner_client.run_suite(files, env)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="a run is already in progress")
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"capture run failed: {exc}")

    cap = heal_capture_store.get(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id)
    nodes = (cap or {}).get("nodes") or []
    reanchor = self_heal.resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=self_heal._baseline_step(tc, step_number), field_meta=field_meta,
    )
    return {
        "captured": bool(nodes), "nodes": len(nodes),
        "reanchored": bool(reanchor), "reanchor": reanchor,
    }


@router.get("/api/v1/test-factory/{artifact_id}/heal-slo")
async def get_heal_slo_artifact(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """P6 — Auto-Heal Reliability SLO for ONE artifact: heal volume, success rate, the
    <1% false-heal SLO target, per-flow churn, heal-storm anomalies (a deploy that broke
    many locators at once → escalate, not absorb), and the tamper-evident chain check.
    Read-only aggregation over the Part-11 heal evidence ledger; no migration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await heal_slo_svc.heal_slo(session, tenant_id=tenant_id, artifact_id=artifact_id)


@router.post("/api/v1/test-factory/reap-stale")
async def reap_stale_jobs(
    max_age_minutes: int = 120,
    user: dict = Depends(get_current_user),
):
    """JOB REAPER: a run whose heal loop died (process restart / crash) stays
    'running' forever (the zombie class). Mark every durable job row still 'running'
    with updated_at older than the cutoff as failed/stale_timeout — an HONEST
    terminal state ('the loop died or hung'), never a fabricated pass/fail verdict
    about the tests themselves. Idempotent; returns the reaped run_ids."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as _sel
    from ..services.test_factory.runner_jobs import E2ERunnerJobRow as _RJ
    tenant_id = user["tenant_id"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(5, int(max_age_minutes)))
    reaped: list = []
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            _sel(_RJ).where(_RJ.tenant_id == tenant_id))).scalars().all()
        for r in rows:
            j = dict(getattr(r, "job_json", None) or {})
            # A LIVE in-memory job that already reached a terminal state must
            # never be reaped (the durable mirror can lag) — sync it instead.
            _mem_live = _RUNNER_JOBS.get(getattr(r, "run_id", ""))
            if _mem_live and (_mem_live.get("terminal_state")
                              or _mem_live.get("status") not in (None, "running")):
                if j.get("status") == "running" and not j.get("terminal_state"):
                    r.job_json = dict(_mem_live)
                continue
            if j.get("status") == "running" and not j.get("terminal_state") \
                    and getattr(r, "updated_at", None) and r.updated_at < cutoff:
                j.update(status="failed", terminal_state="stale_timeout",
                         stop_reason=("reaped: the heal loop died or hung (no heartbeat for "
                                      f">{max_age_minutes}m) — NOT a test verdict; re-run to "
                                      "get a real result"))
                r.job_json = j
                reaped.append(getattr(r, "run_id", ""))
        await session.commit()
    # mirror in-memory zombies (same predicate; in-memory jobs have no timestamp, so
    # only reap those whose durable row was just reaped — never a live loop).
    for _rid in reaped:
        _mem = _RUNNER_JOBS.get(_rid)
        if _mem is not None and _mem.get("status") == "running":
            _mem.update(status="failed", terminal_state="stale_timeout",
                        stop_reason="reaped: the heal loop died or hung — re-run for a real result")
    return {"reaped": [r for r in reaped if r], "count": len([r for r in reaped if r]),
            "max_age_minutes": max_age_minutes}


@router.get("/api/v1/heal-slo")
async def get_heal_slo_tenant(user: dict = Depends(get_current_user)):
    """P6 — tenant-wide Auto-Heal Reliability SLO (across all artifacts) — the
    'this scales safely' reliability dashboard for 100+ tenants / 10k+ tests."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        return await heal_slo_svc.heal_slo(session, tenant_id=tenant_id)


# ── HEAL INTELLIGENCE (N1/N2/N3) — read-only aggregations over grounded evidence ──

def _observations_from_events(rows):
    """Heal-event rows -> calibration observations. rung = fix_kind; green =
    verified_green; confidence from a de-identified score bucket when present (coarse
    until per-rung confidence is logged — the honest current signal)."""
    obs = []
    for r in (rows or []):
        det = r.get("details") or {}
        sb = det.get("score_bucket")
        conf = (float(sb) / 3.0) if isinstance(sb, (int, float)) else 0.8
        obs.append({"rung": r.get("fix_kind") or r.get("event_type") or "unknown",
                    "confidence": min(1.0, max(0.0, conf)),
                    "green": bool(r.get("verified_green"))})
    return obs


def _events_for_benchmark(rows):
    """Adapt heal-event rows to the false-heal benchmark's key shape. The ledger has no
    control_fp column, so the control identity is (scenario_id # step_number) — a
    heal_persisted then a LATER heal_stopped on the SAME (scenario, step) is a genuine
    green->contradiction. cause = engine_verdict / reason; fix_kind carried through."""
    out = []
    for r in (rows or []):
        out.append({
            "event_type": r.get("event_type") or "",
            "control_fp": f"{r.get('scenario_id') or ''}#{r.get('step_number')}",
            "cause": r.get("engine_verdict") or r.get("reason_for_change") or "unknown",
            "fix_kind": r.get("fix_kind") or "unknown",
            "verified_green": bool(r.get("verified_green")),
        })
    return out


@router.get("/api/v1/test-factory/{artifact_id}/journey-graph")
async def get_journey_graph(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """N1 — the app's JOURNEY GRAPH: pages (nodes) + control-attributed transitions
    (edges) built from the recording's grounded steps. Powers multi-hop recovery,
    phantom-by-absence, and deploy-impact diffs. Read-only; no migration."""
    from ..services.diff_and_heal import journey_graph as _jg
    from ..services.diff_and_heal import control_ledger as _cl
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
        visits, _ = await factory_service._load_current_pages_and_actions(session, artifact_id=artifact_id)
    pages = [{"url_path": (getattr(v, "url_path", "") or "")} for v in (visits or [])]
    transitions = []
    for tc in cases:
        prev = ""
        for st in (getattr(tc, "steps", None) or []):
            o = self_heal._observed(st) or {}
            cur = _jg.page_key(o.get("url") or prev or "")
            nxt = _jg.page_key(o.get("next_url")) if o.get("next_url") else ""
            if nxt and nxt != cur:
                transitions.append({
                    "from_page": cur, "to_page": nxt,
                    "control_label": o.get("label") or "",
                    "control_fp": _cl.control_fingerprint(o, page_path=cur),
                    "verb": o.get("verb") or ""})
            prev = nxt or cur
    return _jg.build_journey_graph(pages, transitions)


@router.get("/api/v1/test-factory/{artifact_id}/heal-calibration")
async def get_heal_calibration(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """N2 — per-rung heal CALIBRATION: reliability + ECE + recommended min-confidence
    to gate autonomy at the tenant's false-heal SLO. Turns the threshold question into a
    measurement over the heal-evidence ledger. Read-only."""
    from ..services.diff_and_heal import heal_calibration as _hc
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        chain = await heal_evidence.list_heal_events(session, tenant_id=tenant_id, artifact_id=artifact_id)
    out = _hc.calibrate(_observations_from_events(chain.get("events") or []))
    out["note"] = ("confidence is a coarse de-identified bucket from the heal ledger; it "
                   "sharpens as per-rung confidence is logged. Reliability + refusal are exact.")
    return out


@router.get("/api/v1/test-factory/{artifact_id}/heal-audit")
async def get_heal_audit(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """N3 — AUDIT AS A QUERY: one call answers 'is every green proven and is the evidence
    chain intact?' — the tamper-evident heal chain + a chain-integrity summary + the
    false-heal benchmark over this artifact. The enterprise moat as an endpoint."""
    from ..services.diff_and_heal import false_heal_benchmark as _fhb
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        chain = await heal_evidence.list_heal_events(session, tenant_id=tenant_id, artifact_id=artifact_id)
    events = chain.get("events") or []
    chain_ok_all = all(e.get("chain_ok", True) for e in events)
    first_break = next((e.get("row_hash") for e in events if not e.get("chain_ok", True)), None)
    return {
        "artifact_id": artifact_id,
        "chain_summary": {"total_events": len(events), "chain_ok_all": chain_ok_all,
                          "first_break_row": first_break},
        "benchmark": _fhb.benchmark(_events_for_benchmark(events)),
        "events": events,
    }


@router.get("/api/v1/test-factory/{artifact_id}/trust-slo")
async def get_trust_slo(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """N3 — PUBLISHED TRUST SLO: heal-reliability SLO + false-heal benchmark for this
    artifact in one payload — the 'this maintains safely at scale' number. Read-only."""
    from ..services.diff_and_heal import false_heal_benchmark as _fhb
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        slo = await heal_slo_svc.heal_slo(session, tenant_id=tenant_id, artifact_id=artifact_id)
        chain = await heal_evidence.list_heal_events(session, tenant_id=tenant_id, artifact_id=artifact_id)
    return {"heal_slo": slo, "false_heal_benchmark": _fhb.benchmark(_events_for_benchmark(chain.get("events") or []))}


@router.get("/api/v1/heal-benchmark")
async def get_heal_benchmark_tenant(user: dict = Depends(get_current_user)):
    """The FALSE-HEAL BENCHMARK across all of a tenant's artifacts — the category
    yardstick (false-heal rate < 1%, refusal rate, per-cause). Defined by evidence-chain
    contradiction, so it cannot be gamed by the thing it measures."""
    from ..services.diff_and_heal import false_heal_benchmark as _fhb
    from sqlalchemy import select as _sel
    from nexus_sdk.db.models import HealEventRow as _HER
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            _sel(_HER).where(_HER.tenant_id == tenant_id).order_by(_HER.created_at))).scalars().all()
    evs = [{"event_type": r.event_type, "scenario_id": r.scenario_id, "step_number": r.step_number,
            "engine_verdict": r.engine_verdict, "fix_kind": r.fix_kind,
            "verified_green": r.verified_green} for r in rows]
    return _fhb.benchmark(_events_for_benchmark(evs))


# ─── Auto-Heal Run (P1, iterate-whole-spec) ──────────────────────────────────
#
# Toggle on → on failure auto-diagnose + fix + re-run + continue, and when the
# WHOLE selected suite is green, persist an immutable "Clean Run - V1". Safety is
# structural: the loop attempts ONLY a confirmable control-kind fix, requires
# FULL green before saving (downstream steps are the outcome oracle, so a false
# green is caught by a later step failing), hard-stops toward a human on anything
# else, and writes nothing unless the whole script passes. Convergence: each step
# is attempted at most `max_attempts` times and a corrected label is never
# re-applied (no-progress stop), so the loop is bounded. $0 LLM.

_AUTO_HEAL_MAX_ITERS = 12


async def _await_run_terminal(cycles: int = 200) -> str:
    """Poll the runner's single live display until the current run is terminal."""
    for _ in range(cycles):
        await asyncio.sleep(2.5)
        try:
            s = await runner_client.live_status()
        except Exception:
            continue
        st = s.get("status", "running")
        if st not in ("running", "idle"):
            return st
    return "timed_out"


async def _auto_capture_and_reanchor(
    *, tenant_id: str, artifact_id: str, token: str, scenario_id: str, step_number: int,
    tc, field_meta: dict, base_url: str, data, storage_state, ov, ra, spec_path: str,
    env_context: dict | None = None,
    auth_config: dict | None = None, login_env: dict | None = None,
) -> dict | None:
    """P2-full: re-run ONE scenario HEADLESS with a11y capture ON, then resolve a
    MULTI-SIGNAL (similo) re-anchor for the failing step — automatically, so the live
    locator resolution fires inside the auto-heal loop (not only on a manual click).
    Best-effort: returns the re-anchor dict {name, role, confidence, rationale} or None
    (refuse / capture failed). Saves nothing; the capture is transient. The caller then
    applies the re-anchor and RE-RUNS to prove green — never green-wash."""
    try:
        _ovd = dict(ov or {})
        _ints = _ovd.pop("__interactions__", None) or {}
        _navs = _ovd.pop("__nav_overrides__", None) or {}
        _padv = _ovd.pop("__pre_advance__", None) or {}
        _navr = _ovd.pop("__nav_recover__", None) or {}
        capture_spec = compile_case(
            tc, {**field_meta, **_ovd}, parametrize=True,
            heal_capture=True, reanchors=(ra or {}),
            interactions=_ints, nav_overrides=_navs, pre_advance=_padv,
            nav_recovers=_navr,
        )
        edited = {scenario_id: {"spec_path": spec_path, "script_source": capture_spec}}
        files = _configured_files(
            [tc], field_meta, base_url, data, data_by_test={},
            browsers=["chromium"], headed=False, workers=1, retries=0,
            edited=edited, storage_state=storage_state, env_context=env_context,
            auth_config=auth_config,
        )
        env = {
            "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
            "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": uuid.uuid4().hex,
            "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
            **(login_env or {}),
            "NEXUS_HEAL_CAPTURE": "1",
            "NEXUS_HEAL_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/heal-capture",
        }
        await runner_client.run_suite(files, env)  # blocks; the test FAILS = the capture
    except Exception:
        return None
    return self_heal.resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=self_heal._baseline_step(tc, step_number), field_meta=field_meta,
    )


async def _record_heal_stop(tenant_id: str, artifact_id: str, scenario_id: str,
                            step_number: int, cause: str, reason: str) -> None:
    """Best-effort (FAIL-OPEN) record of an honest heal STOP (refuse / needs-human) into
    the Part-11 ledger, so the reliability SLO counts ATTEMPTS — not only successes — and
    the success rate is real, never flattering. A recording failure never breaks the loop."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            await heal_evidence.record_heal_event(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                event_type="heal_stopped", actor="nexus-autoheal",
                scenario_id=scenario_id, step_number=int(step_number or 0),
                engine_verdict=cause or "", verified_green=False,
                reason_for_change=(reason or "")[:500],
            )
            await session.commit()
    except Exception:
        pass


async def _run_auto_heal(run_id: str, ctx: dict) -> None:
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        return
    tenant_id = ctx["tenant_id"]
    artifact_id = ctx["artifact_id"]
    token = ctx["token"]
    selected = list(ctx["scenario_ids"])
    base_url = (ctx.get("base_url") or "").strip()
    data = ctx.get("data") or {}
    max_attempts = int(ctx.get("max_attempts") or 3)
    storage_state = ctx.get("storage_state")  # captured auth session (or None)
    _fl_auth_config, _fl_login_env = ctx.get("form_login") or (None, {})  # AUTH PERMANENCE
    # Multi-env (#7 heal parity): the resolved Environment Profile. When present the
    # heal re-runs REBIND to that env (base_url + cookies + headers + basic-auth + pin)
    # exactly like the graded run — so a fix is proven against the RIGHT env, never the
    # default. None ⇒ single-env heal, byte-identical to before.
    env_context = ctx.get("env_context")
    # AUTOPILOT (Mode B): when on, UNPROVEN (review/inferred) steps are EXECUTED + ASSERTED
    # (compiled with autonomous_resolve=True) so the agent DRIVES + PROVES them instead of
    # skipping for a human; the agentic analyst is auto-applied; the orthogonal recorded-
    # outcome oracle + the 2x confirm still gate green; heal events are attributed to the
    # autonomous agent. Default OFF => byte-identical to the human Studio path.
    autonomous = bool(ctx.get("autonomous"))
    heal_actor = "autonomous-agent" if autonomous else "nexus-autoheal"

    def trace(**kw):
        job.setdefault("heal_trace", []).append(kw)

    try:
        async with tenant_scoped_session(tenant_id) as session:
            cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
            visits, _ = await factory_service._load_current_pages_and_actions(session, artifact_id=artifact_id)
            base_edited = await _active_edited_map(session, artifact_id=artifact_id)
        field_meta = build_field_meta(visits)
        case_by_id = {(getattr(c, "test_id", "") or ""): c for c in cases}
        sel_cases = [case_by_id[sid] for sid in selected if sid in case_by_id]
        if not sel_cases:
            job.update(status="error", terminal_state="error",
                       stop_reason="no matching active test cases")
            return
        spec_path_by_sid = {
            sid: {s["test_id"]: s["path"]
                  for s in compile_manifest([case_by_id[sid]], field_meta).get("scripts", [])}.get(sid, "")
            for sid in selected if sid in case_by_id
        }

        overrides: dict[str, dict] = {}        # sid -> {label_norm: signal}  (control-kind)
        reanchors: dict[str, dict] = {}        # sid -> {step: {name}}  (P2-full multi-signal re-anchor)
        stabilize: dict[str, dict] = {}        # sid -> {step: True}  (P4 flake-wait synthesis)
        visual: dict[str, dict] = {}           # sid -> {step: {x,y}}  (P5-full opt-in visual coordinate)
        phantom_skips: dict[str, set] = {}     # sid -> {step,...}  (no-op a control-absent exact-duplicate of a passed step)
        heal_shapes: dict[str, dict] = {}      # sid -> de-id shape {recorded_kind,resolved_role,score_bucket,fix_kind} (P7 learning)
        attempts: dict[tuple, int] = {}        # (sid, step[, 'ra'|'stab'|'vis']) -> count

        # ── PROVEN-CONTROL LEDGER: SEED-BEFORE-RUN (heal once, reuse) ──────────────
        # Pre-load fixes PROVEN green for this app's controls (exact fingerprint per
        # recording, then app-scoped exact) into the loop's channels BEFORE iteration 1,
        # so a control healed in an earlier run/scenario passes immediately instead of
        # re-healing from scratch. ADDITIVE + FAIL-OPEN: table absent / DB error / no
        # match => empty => byte-identical. NEVER GREEN-WASH: a seed is ONLY an override
        # — the step's own grounded oracle + the 2x confirm still decide green, and a
        # seeded step that fails iteration 1 has its seed CLEARED + QUARANTINED below.
        _seeded_steps: set = set()
        _seeded_labels: dict = {}
        _seed_fp: dict = {}
        _seed_fp_label: dict = {}
        try:
            from ..services.diff_and_heal import control_ledger
            async with tenant_scoped_session(tenant_id) as _seed_session:
                for _sid in selected:
                    _tc = case_by_id.get(_sid)
                    if _tc is None:
                        continue
                    _step_fps = []
                    _all_fps: set = set()
                    _app_scope = ""
                    for _st in (getattr(_tc, "steps", None) or []):
                        _obs = self_heal._observed(_st) or {}
                        if not _app_scope:
                            _app_scope = control_ledger.app_key_from_url(_obs.get("url") or _obs.get("next_url"))
                        _page = control_ledger.page_key(_obs.get("url") or _obs.get("next_url") or "")
                        _fp = control_ledger.control_fingerprint(_obs, page_path=_page)
                        if not _fp:
                            continue
                        _step_fps.append((_st, _obs, _fp))
                        _all_fps.add(_fp)
                    if not _all_fps:
                        continue
                    _proven = await control_ledger.get_proven_fixes(
                        _seed_session, tenant_id=tenant_id, app_key=artifact_id, control_fps=_all_fps)
                    _proven_app: dict = {}
                    if _app_scope and os.getenv("NEXUS_LEDGER_APP_SCOPE", "1") != "0":
                        _unmatched = {_x for _x in _all_fps if _x not in _proven}
                        if _unmatched:
                            _proven_app = await control_ledger.get_proven_fixes_by_app(
                                _seed_session, tenant_id=tenant_id, app_fingerprint=_app_scope,
                                control_fps=_unmatched)
                    if not _proven and not _proven_app:
                        continue
                    _ov = overrides.setdefault(_sid, {})
                    _count = 0
                    for (_st, _obs, _fp) in _step_fps:
                        _fixes = _proven.get(_fp) or _proven_app.get(_fp)
                        if not _fixes:
                            continue
                        _stn = getattr(_st, "step_number", None)
                        _live_kind = self_heal._norm(_obs.get("kind") or "")
                        for _fix in _fixes:
                            _kind = _fix.get("fix_kind") or ""
                            _payload = dict(_fix.get("payload") or {})
                            if not _payload:
                                continue
                            # KIND PRE-GATE: no kind to bind on => refuse (never seed a homonym).
                            if _kind in ("interaction", "control_kind") and not _live_kind:
                                trace(event="ledger_seed_refused", scenario_id=_sid, step=_stn,
                                      fix_kind=_kind, reason="blank_kind_no_bind")
                                continue
                            if _kind == "control_kind":
                                _ln = self_heal._norm(_obs.get("label") or "")
                                if not _ln:
                                    continue
                                _ov[_ln] = _payload
                                _seeded_labels.setdefault(_sid, set()).add(_ln)
                                _seed_fp_label[(_sid, _ln)] = _fp
                                _count += 1
                            elif _kind == "interaction" and _stn is not None:
                                _ov.setdefault("__interactions__", {})[_stn] = _payload
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "interaction")] = _fp
                                _count += 1
                            elif _kind == "reanchor" and _stn is not None and _payload.get("name"):
                                reanchors.setdefault(_sid, {})[_stn] = _payload
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "reanchor")] = _fp
                                _count += 1
                            elif _kind == "nav" and _stn is not None and _payload.get("url"):
                                _ov.setdefault("__nav_overrides__", {})[_stn] = _payload["url"]
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "nav")] = _fp
                                _count += 1
                            elif _kind == "nav_recover" and _stn is not None:
                                _ov.setdefault("__nav_recover__", {})[_stn] = True
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "nav_recover")] = _fp
                                _count += 1
                            elif _kind == "advance" and _stn is not None:
                                _ov.setdefault("__pre_advance__", {})[_stn] = int(_payload.get("pages") or 3)
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "advance")] = _fp
                                _count += 1
                    if not _ov:
                        overrides.pop(_sid, None)
                    if _count:
                        trace(event="ledger_seeded", scenario_id=_sid, count=_count)
        except Exception as _seed_exc:  # fully fail-open — seeding never affects the run
            overrides.clear(); reanchors.clear()
            _seeded_steps.clear(); _seeded_labels.clear(); _seed_fp.clear(); _seed_fp_label.clear()
            trace(event="ledger_seed_failed", error=str(_seed_exc)[:200])
        # ── END LEDGER SEED ──

        async def _try_agentic(sid, step, observed, f, diag):
            """AUTOPILOT autonomous analyst (gated ctx['enable_agentic_heal'] = autonomous):
            when the deterministic heals are exhausted, the grounded LLM agent reasons over
            the LIVE page like an engineer and proposes a fix (rebind {name,kind} / wait),
            bound VERBATIM to a live control (ungrounded picks dropped at validation), before
            escalating. Auto-applied (no human). NEVER green-wash — the orthogonal recorded-
            outcome oracle + the 2x confirm still decide green, and the REFUSE families
            (real-regression / auth / data / variant) are excluded. Returns True iff a
            grounded fix was applied (caller `continue`s to re-prove)."""
            if not ctx.get("enable_agentic_heal"):
                return False
            try:
                from ..services.test_factory import agentic_heal
            except Exception as _ie:  # module absent / import error -> escalate honestly, never crash
                trace(event="agentic_unavailable", error=str(_ie)[:140])
                return False
            if (diag or {}).get("cause") in agentic_heal.REFUSE_CAUSES:
                return False
            akey = (sid, step, "agentic")
            if attempts.get(akey, 0) >= 1:
                return False
            attempts[akey] = 1
            tc = case_by_id.get(sid)
            # Live controls captured during the reanchor capture (heal_capture_store). If the
            # path didn't capture yet, capture now (ensures the agent reasons over the LIVE page).
            cap = heal_capture_store.get(tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=sid)
            nodes = (cap or {}).get("nodes") or []
            if not nodes:
                await _auto_capture_and_reanchor(
                    tenant_id=tenant_id, artifact_id=artifact_id, token=token,
                    scenario_id=sid, step_number=step, tc=tc, field_meta=field_meta,
                    base_url=base_url, data=data, storage_state=storage_state,
                    ov=overrides.get(sid), ra=reanchors.get(sid),
                    spec_path=spec_path_by_sid.get(sid, ""), env_context=env_context,
                    auth_config=_fl_auth_config, login_env=_fl_login_env)
                cap = heal_capture_store.get(tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=sid)
                nodes = (cap or {}).get("nodes") or []
            if not nodes:
                trace(event="agentic_no_capture", scenario_id=sid, step=step)
                return False
            recorded = {}
            for st in (getattr(tc, "steps", None) or []):
                o = self_heal._observed(st) or {}
                recorded[getattr(st, "step_number", None)] = {
                    "label": o.get("label", ""), "kind": o.get("kind", ""),
                    "value": o.get("value", ""),
                    # FULL-CONTEXT analyst (Phase-3): also surface the recorded verb, the
                    # disambiguating anchor/block, and the recorded next page — all grounded.
                    "verb": o.get("verb", ""), "anchor": o.get("anchor", ""),
                    "next_url": o.get("next_url", ""),
                    "expected": (getattr(st, "expected", "") or getattr(st, "expected_result", "") or "")}
            # Feed the analyst the deterministic DIAGNOSIS + the heals ALREADY TRIED for this
            # step (derived from the attempts ledger) so it builds on the pipeline's work.
            _tried_names = {"ra": "re-anchor", "selfb": "select-content-fallback",
                            "stab": "stabilize-wait", "phantom": "phantom-skip",
                            "regconfirm": "regression-confirm"}
            _tried = [nm for k, nm in _tried_names.items() if attempts.get((sid, step, k), 0) > 0]
            res = await agentic_heal.propose(
                failing=[{"step_number": step, "error_message": f.get("error_message", ""),
                          "cause": (diag or {}).get("cause", ""), "tried": _tried}],
                recorded_by_step=recorded, nodes=nodes,
                tier_name=ctx.get("agentic_tier", "tier_premium"),
                min_confidence=float(ctx.get("agentic_min_confidence", 0.7) or 0.7))
            applied = res.get("applied") or []
            if not applied:
                trace(event="agentic_no_fix", scenario_id=sid, step=step,
                      ok=bool(res.get("ok")), error=str(res.get("error", ""))[:160],
                      refused=len(res.get("refused") or []))
                return False
            did = False
            for fx in applied:
                ch = fx.get("channel"); pl = fx.get("payload") or {}; stn = fx.get("step_number")
                if ch == "reanchors" and pl.get("name"):
                    # thread the LIVE kind too — the compiler kind-locks it (live
                    # evidence beats accumulated overrides), so a rebound RADIO
                    # compiles as check()+toBeChecked, never selectOption.
                    reanchors.setdefault(sid, {})[stn] = {"name": pl["name"],
                                                          "kind": pl.get("kind") or "",
                                                          "frame_selector": ""}
                    if pl.get("kind"):
                        _bo = self_heal._observed(self_heal._baseline_step(tc, stn)) or {}
                        _lbl = " ".join((_bo.get("label", "") or "").strip().lower().split())
                        if _lbl:
                            overrides.setdefault(sid, {})[_lbl] = {
                                "control": pl["kind"], "options": [], "required": False}
                    heal_shapes[sid] = {"recorded_kind": str(observed.get("kind") or ""),
                                        "resolved_role": str(pl.get("kind") or ""),
                                        "score_bucket": 0, "fix_kind": "agentic_rebind"}
                    did = True
                    trace(event="agentic_applied", scenario_id=sid, step=stn,
                          name=pl.get("name"), kind=pl.get("kind", ""), confidence=fx.get("confidence"))
                elif ch == "waits":
                    stabilize.setdefault(sid, {})[stn] = True
                    did = True
                    trace(event="agentic_wait", scenario_id=sid, step=stn)
            return did

        # Iteration budget SCALES with flow length: every heal->prove cycle costs one
        # full re-run, so a 28-step flow with several distinct defects legitimately
        # needs more iterations than a 3-step one. Attempts-gating (each rung fires at
        # most once per step) still guarantees convergence to green or an honest stop;
        # the cap only stops a pathological loop. Never a green-wash lever.
        _max_steps = max((len(getattr(c, "steps", None) or []) for c in sel_cases), default=0)
        _iter_budget = max(_AUTO_HEAL_MAX_ITERS, min(40, 2 * _max_steps + 6))
        for iteration in range(1, _iter_budget + 1):
            # Build candidate specs for any scenario with accumulated corrections.
            edited = dict(base_edited)
            candidate_specs: dict[str, str] = {}
            for sid in selected:
                tc = case_by_id.get(sid)
                if tc is None:
                    continue
                ov = overrides.get(sid)
                ra = reanchors.get(sid)
                stab = stabilize.get(sid)
                vis = visual.get(sid)
                psk = phantom_skips.get(sid)
                if ov or ra or stab or vis or psk or autonomous:
                    # apply control-kind corrections (field_meta) + multi-signal re-anchors
                    # (P2-full) + flake-wait stabilization (P4) + opt-in visual coordinates
                    # (P5-full) accumulated for this scenario. AUTOPILOT: compile with
                    # autonomous_resolve so UNPROVEN steps EXECUTE+ASSERT (driven + proven by
                    # the agent), from iteration 1, for every selected case.
                    _ovd = dict(ov or {})
                    _ints = _ovd.pop("__interactions__", None) or {}
                    _navs = _ovd.pop("__nav_overrides__", None) or {}
                    _padv = _ovd.pop("__pre_advance__", None) or {}
                    _navr = _ovd.pop("__nav_recover__", None) or {}
                    _fos = bool(_ovd.pop("__force_open_shadow__", False))
                    spec = compile_case(
                        tc, {**field_meta, **_ovd}, parametrize=True,
                        reanchors=(ra or {}), stabilize=(stab or {}), visual=(vis or {}),
                        interactions=_ints, nav_overrides=_navs, pre_advance=_padv,
                        nav_recovers=_navr, force_open_shadow=_fos,
                        autonomous_resolve=autonomous, phantom_skips=(psk or set()),
                    )
                    candidate_specs[sid] = spec
                    edited[sid] = {"spec_path": spec_path_by_sid.get(sid, ""), "script_source": spec}

            files = _configured_files(
                sel_cases, field_meta, base_url, data, data_by_test={},
                browsers=["chromium"], headed=True, workers=1, retries=0, edited=edited,
                storage_state=storage_state, env_context=env_context,
                auth_config=_fl_auth_config,
            )
            sub_run_id = uuid.uuid4().hex
            env = {
                "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
                "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": sub_run_id,
                "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
                **_fl_login_env,
            }
            started = False
            for _try in range(6):
                try:
                    await runner_client.run_live(files, env)
                    started = True
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response is not None and exc.response.status_code == 409:
                        await asyncio.sleep(2.0)
                        continue
                    raise
                except Exception:
                    await asyncio.sleep(2.0)
                    continue
            if not started:
                job.update(status="error", terminal_state="error",
                           stop_reason="runner busy or unreachable — could not start the re-run")
                return
            trace(iteration=iteration, event="run_started", scripts=len(sel_cases))
            await _await_run_terminal()
            # PROVE-GREEN gate: correlate to THIS verification sub-run by its run
            # id (never trust 'newest run by time' — a racing ingest could green-
            # wash). Poll for the reporter to land this run; if it never correlates,
            # STOP toward a human rather than counting it green.
            tl = None
            for _corr in range(12):  # ~18s budget for ingest (run is already terminal)
                await asyncio.sleep(1.5)
                async with tenant_scoped_session(tenant_id) as session:
                    real_run_id = await find_run_by_ci_run_id(
                        session, artifact_id=artifact_id, tenant_id=tenant_id, ci_run_id=sub_run_id,
                    )
                    if real_run_id is None:
                        continue
                    tl = await build_run_timeline_by_id(
                        session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=real_run_id,
                    )
                break
            if tl is None:
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason="could not correlate the verification re-run (no ingested "
                                       "result for this run id) — not counting it green; needs a human")
                trace(event="stop_no_verification", iteration=iteration)
                return
            failures = self_heal.first_failures(tl, selected)
            # GREEN requires every selected scenario PRESENT and actually PASSED —
            # not merely 'no failed step' (a missing / zero-step / all-skipped
            # scenario is NOT proof of green and must not freeze a Clean Run V1).
            _by_id = {sc.get("scenario_id"): sc for sc in (tl.get("scenarios") or [])}
            _FAILST = {"failed", "broken", "timed_out"}
            def _proven_pass(_sid):
                _sc = _by_id.get(_sid)
                _steps = (_sc or {}).get("steps") or []
                return bool(_sc) and bool(_steps) \
                    and not any((st.get("status") in _FAILST) for st in _steps) \
                    and any((st.get("status") == "passed") for st in _steps)
            all_green = (not failures) and all(_proven_pass(_sid) for _sid in selected)
            # Legibility: record EXACTLY what this iteration's verification run produced
            # (per-scenario verdict + per-step status) so a stop/crash is never opaque —
            # we can see whether the healed step actually ran/passed/skipped.
            trace(event="iter_result", iteration=iteration, all_green=all_green,
                  n_failures=len(failures),
                  scenarios=[{"sid": (sc.get("scenario_id") or "")[:8],
                              "verdict": sc.get("verdict"),
                              "steps": [{"n": st.get("step_number"), "s": st.get("status")}
                                        for st in (sc.get("steps") or [])]}
                             for sc in (tl.get("scenarios") or [])])

            # ── LEDGER SEED INVALIDATION (stale-memo guard): a seeded step that STILL
            # failed on iteration 1 carried a STALE memo (the app changed since it was
            # proven). Drop the seed + QUARANTINE the ledger row (stops re-seeding every
            # run), then re-prove fresh — a stale seed is never worse than from-scratch.
            # Fail-open; only ever REMOVES seeds — can never gate a run or green-wash.
            if iteration == 1 and (_seeded_steps or _seeded_labels):
                _failed_pairs = {(f.get("scenario_id"), f.get("step_number")) for f in failures}
                _cleared = 0
                _stale_marks: list = []
                for (_csid, _cstn) in list(_seeded_steps):
                    if (_csid, _cstn) in _failed_pairs:
                        _cov = overrides.get(_csid) or {}
                        for _chan, _ck in (("__interactions__", "interaction"),
                                           ("__nav_overrides__", "nav"),
                                           ("__pre_advance__", "advance"),
                                           ("__nav_recover__", "nav_recover")):
                            if _cstn in (_cov.get(_chan) or {}):
                                _cov[_chan].pop(_cstn, None)
                                _cleared += 1
                                _cfp = _seed_fp.get((_csid, _cstn, _ck))
                                if _cfp:
                                    _stale_marks.append((_cfp, _ck))
                        if _cstn in (reanchors.get(_csid) or {}):
                            reanchors[_csid].pop(_cstn, None)
                            _cleared += 1
                            _cfp = _seed_fp.get((_csid, _cstn, "reanchor"))
                            if _cfp:
                                _stale_marks.append((_cfp, "reanchor"))
                        _seeded_steps.discard((_csid, _cstn))
                _failed_by_sid: dict = {}
                for (_csid, _cstn) in _failed_pairs:
                    _failed_by_sid.setdefault(_csid, set()).add(_cstn)
                for _csid, _clabels in list(_seeded_labels.items()):
                    _ctc = case_by_id.get(_csid)
                    _cfsteps = _failed_by_sid.get(_csid) or set()
                    for _cst in (getattr(_ctc, "steps", None) or []):
                        if getattr(_cst, "step_number", None) in _cfsteps:
                            _cln = self_heal._norm((self_heal._observed(_cst) or {}).get("label") or "")
                            if _cln in _clabels:
                                (overrides.get(_csid) or {}).pop(_cln, None)
                                _clabels.discard(_cln)
                                _cleared += 1
                                _cfp = _seed_fp_label.get((_csid, _cln))
                                if _cfp:
                                    _stale_marks.append((_cfp, "control_kind"))
                if _cleared:
                    if _stale_marks:
                        try:
                            from ..services.diff_and_heal import control_ledger as _cl
                            async with tenant_scoped_session(tenant_id) as _stsess:
                                for (_sfp, _skind) in _stale_marks:
                                    await _cl.mark_seed_stale(
                                        _stsess, tenant_id=tenant_id, app_key=artifact_id,
                                        control_fp=_sfp, fix_kind=_skind, invalidated_by_run=run_id)
                                await _stsess.commit()
                        except Exception as _stexc:
                            trace(event="ledger_mark_stale_failed", error=str(_stexc)[:200])
                    trace(event="ledger_seed_cleared_stale", cleared=_cleared, marked=len(_stale_marks))
                    continue  # re-prove from the cleaned overrides; the loop heals fresh

            if all_green:
                # AUDITOR GATE (Phase-0; warning-first, NEXUS_AUDITOR_GATE=enforce to BLOCK):
                # even a green run must pass the deterministic structural audit — a run that
                # passes at runtime but still carries an impossible-transition assertion or a
                # dropped recorded value is a STRUCTURAL green-wash the pass/fail status alone
                # cannot see. Warning-first (attach + trace the score); when enforced, a
                # non-certified audit REFUSES to certify the clean run (never green-wash).
                from ..services.test_factory import playwright_auditor as _pwa
                _enforce_audit = os.getenv("NEXUS_AUDITOR_GATE") == "enforce"
                _audits: dict = {}
                for _asid, _asrc in candidate_specs.items():
                    _atc = case_by_id.get(_asid)
                    _asteps = list(getattr(_atc, "steps", None) or []) if _atc is not None else []
                    try:
                        # Phase-0 auditor restored (efd0269 revert undone): gate()
                        # consumes a score_spec REPORT and reports an HONEST
                        # would_block independent of enforcement.
                        _arep = _pwa.score_spec(_asrc, _asteps, evidence=None)
                        _ag = _pwa.gate(_arep, blocking=_enforce_audit)
                    except Exception:
                        _ag = None
                    if _ag is not None:
                        _audits[_asid] = _ag
                        trace(event="auditor_gate", scenario_id=_asid, decision=_ag.get("decision"),
                              score=_ag.get("overall_score"), would_block=_ag.get("would_block"),
                              findings=(_ag.get("warnings") or [])[:4])
                _blocked = [s for s, g in _audits.items() if g.get("would_block")]
                if _enforce_audit and _blocked:
                    _bf = (_audits[_blocked[0]].get("findings") or ["structural audit failed"])[0]
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=(f"auditor gate BLOCKED the clean run: {len(_blocked)} script(s) "
                                            f"failed the structural audit despite passing at runtime "
                                            f"(e.g. {_bf}). Refusing to certify a structurally-unsound "
                                            f"green — never green-wash."), audit=_audits)
                    trace(event="stop_auditor_gate", blocked=len(_blocked))
                    await _persist_job(run_id)
                    return
                # FULL GREEN → persist Clean Run - V1 for the healed tests (atomic).
                healed = [{"test_case_id": sid, "spec_path": spec_path_by_sid.get(sid, ""),
                           "script_source": src} for sid, src in candidate_specs.items()]
                version_no = None
                if healed:
                    # FAIL-OPEN: the green is already PROVEN (oracle + 2x confirm). A
                    # version-row persistence fault (e.g. the pre-existing dual-identity
                    # defect: a regenerated runtime case id orphaned from
                    # factory_test_cases -> script_versions FK) must NEVER convert a
                    # proven green into job=error. On failure we still record the
                    # Part-11 evidence (scenario_id carries no FK) in a fresh session,
                    # trace the precise fault, and certify the clean run with
                    # version=None — the truth, not a crash.
                    try:
                        async with tenant_scoped_session(tenant_id) as session:
                            rows = await script_versions.batch_save_clean_run_version(
                                session, artifact_id=artifact_id, tenant_id=tenant_id,
                                healed=healed, clean_run_session_id=run_id, n_healed=len(healed),
                            )
                            # Part-11 evidence per healed test (atomic with the versions).
                            _vmap = {getattr(r, 'test_case_id', ''): getattr(r, 'version_no', 0) for r in (rows or [])}
                            for _h in healed:
                                _sid7 = (_h.get('test_case_id') or '')
                                _shp7 = heal_shapes.get(_sid7, {})
                                await heal_evidence.record_heal_event(
                                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                                    event_type="heal_persisted", actor=heal_actor,
                                    scenario_id=_sid7,
                                    fix_kind=(_shp7.get("fix_kind") or "control_kind_fix"),
                                    verified_green=True,
                                    version_no=_vmap.get(_sid7, 0), run_id=run_id,
                                    reason_for_change=f"Clean Run - V1 (auto-healed {len(healed)} step(s), verified green)",
                                    details=_shp7,  # P7: de-identified drift SHAPE for the flywheel
                                )
                            await session.commit()
                            version_no = rows[0].version_no if rows else None
                    except Exception as _vpexc:
                        trace(event="version_persist_failed", error=str(_vpexc)[:300],
                              note=("proven green preserved; version row not written — "
                                    "runtime case id is likely orphaned from factory_test_cases "
                                    "(dual-identity defect); re-generate/approve the case to "
                                    "restore version persistence"))
                        try:
                            async with tenant_scoped_session(tenant_id) as _evs:
                                for _h in healed:
                                    _sid7 = (_h.get('test_case_id') or '')
                                    _shp7 = heal_shapes.get(_sid7, {})
                                    await heal_evidence.record_heal_event(
                                        _evs, tenant_id=tenant_id, artifact_id=artifact_id,
                                        event_type="heal_persisted", actor=heal_actor,
                                        scenario_id=_sid7,
                                        fix_kind=(_shp7.get("fix_kind") or "control_kind_fix"),
                                        verified_green=True,
                                        version_no=0, run_id=run_id,
                                        reason_for_change=("Clean Run - V1 (proven green; version row "
                                                           "NOT persisted: orphaned case id)"),
                                        details=_shp7,
                                    )
                                await _evs.commit()
                        except Exception as _evexc:
                            trace(event="evidence_persist_failed", error=str(_evexc)[:200])
                # ── PROVEN-CONTROL LEDGER: WRITE-ON-GREEN (heal once, reuse): memoize
                # every override that was part of this PROVEN (2x-confirmed) green so
                # future runs of this app SEED it at iteration 1. Keyed off each step's
                # ORIGINAL baseline observed (reanchors never mutate it). Reuse is re-gated
                # by the step's own oracle every run — a memo can never green-wash.
                # Includes the NEW fix kinds: nav (entry-URL correction) + advance
                # (wizard pages). Fully fail-open — never affects the Clean Run.
                try:
                    from ..services.diff_and_heal import control_ledger as _cl
                    _RESV = {"__interactions__", "__nav_overrides__", "__pre_advance__", "__nav_recover__"}
                    async with tenant_scoped_session(tenant_id) as _lsession:
                        for _lsid in set(list(overrides or {}) + list(reanchors or {})):
                            _lsov = overrides.get(_lsid) or {}
                            _ltc = case_by_id.get(_lsid)
                            if _ltc is None:
                                continue
                            _ck_labels = {k for k in _lsov if k not in _RESV}
                            _l_ints = _lsov.get("__interactions__") or {}
                            _l_navs = _lsov.get("__nav_overrides__") or {}
                            _l_advs = _lsov.get("__pre_advance__") or {}
                            _l_navr = _lsov.get("__nav_recover__") or {}
                            _l_reas = reanchors.get(_lsid) or {}
                            for _lstn in (set(_l_ints) | set(_l_navs) | set(_l_advs)
                                          | set(_l_navr) | set(_l_reas)):
                                _lbs = self_heal._baseline_step(_ltc, _lstn)
                                if _lbs is None:
                                    continue
                                _lobs = self_heal._observed(_lbs) or {}
                                _lpage = _cl.page_key(_lobs.get("url") or _lobs.get("next_url") or "")
                                _lapp = _cl.app_key_from_url(_lobs.get("url") or _lobs.get("next_url"))
                                _lfp = _cl.control_fingerprint(_lobs, page_path=_lpage)
                                if not _lfp:
                                    continue
                                _llabel = _lobs.get("label") or ""
                                for _fk, _pl in (("reanchor", _l_reas.get(_lstn)),
                                                 ("interaction", _l_ints.get(_lstn)),
                                                 ("nav", {"url": _l_navs[_lstn]} if _lstn in _l_navs else None),
                                                 ("advance", {"pages": _l_advs[_lstn]} if _lstn in _l_advs else None),
                                                 ("nav_recover", {"on": True} if _lstn in _l_navr else None)):
                                    if not _pl:
                                        continue
                                    await _cl.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id,
                                        control_fp=_lfp, fix_kind=_fk, payload=dict(_pl),
                                        label=_llabel, page_path=_lpage, proven_by_run=run_id,
                                        app_fingerprint=_lapp)
                            if _ck_labels:
                                for _lst in (getattr(_ltc, "steps", None) or []):
                                    _lobs = self_heal._observed(_lst) or {}
                                    _lln = self_heal._norm(_lobs.get("label") or "")
                                    if not _lln or _lln not in _ck_labels:
                                        continue
                                    _lpage = _cl.page_key(_lobs.get("url") or _lobs.get("next_url") or "")
                                    _lapp = _cl.app_key_from_url(_lobs.get("url") or _lobs.get("next_url"))
                                    _lfp = _cl.control_fingerprint(_lobs, page_path=_lpage)
                                    if not _lfp:
                                        continue
                                    await _cl.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id,
                                        control_fp=_lfp, fix_kind="control_kind",
                                        payload=dict(_lsov.get(_lln) or {}),
                                        label=(_lobs.get("label") or ""), page_path=_lpage,
                                        proven_by_run=run_id, app_fingerprint=_lapp)
                        await _lsession.commit()
                    trace(event="ledger_memoized", healed_count=len(healed))
                except Exception as _lexc:  # fully fail-open — never affects Clean Run V1
                    trace(event="ledger_memoize_failed", error=str(_lexc)[:200])

                job.update(status="passed", terminal_state="clean_run_v1",
                           clean_run_version=version_no, healed_count=len(healed))
                trace(event="clean_run_v1", healed_count=len(healed), version_no=version_no)
                return

            # Take the first failing step and decide.
            if not failures:
                # No FAILED step, yet not all-green — e.g. a recompiled candidate fix made
                # the test SKIP (an unproven / re-broken step → test.skip aborts the whole
                # test) or every step was skipped. There is nothing to diagnose; stop
                # HONESTLY rather than crash on failures[0]. A skipped test is NEVER a pass.
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=("the candidate fix made the test SKIP (a step could not "
                                        "be proven) — no failing step to fix and no proven-green "
                                        "result; needs a human"))
                trace(event="stop_skip_no_green", iteration=iteration)
                await _persist_job(run_id)
                return
            f = failures[0]
            sid = f["scenario_id"]
            step = f["step_number"]
            tc = case_by_id.get(sid)
            bs = self_heal._baseline_step(tc, step)
            observed = self_heal._observed(bs) if bs is not None else {}
            diag = self_heal.diagnose(
                error_message=f.get("error_message", ""), status=f.get("status", "failed"),
                observed=observed, field_meta=field_meta, baseline_step=bs,
                is_flaky=False, selector_drifted=False, prior_step_passed=f.get("prior_passed", False),
            )
            # NETWORK ORACLE (R5 wiring — was built, never threaded): attach the
            # best available network signal to the diagnosis. Origin-gated (R7):
            # a third-party 5xx/failure surfaces as External Dependency — the
            # application under test is NOT accused by a foreign origin's error.
            # Advisory only; never breaks diagnosis.
            try:
                from ..services.test_factory import network_oracle as _net_oracle
                _nsig = _net_oracle.detect(
                    f, observed, base_host=_net_oracle._host_of(base_url))
                if _nsig:
                    diag = {**diag, "network": _nsig}
                    if _nsig.get("kind") == "external_dependency":
                        diag = {**diag,
                                "cause_label": ((diag.get("cause_label") or "")
                                                + " — a THIRD-PARTY dependency failed ("
                                                + str(_nsig.get("detail") or "")[:120]
                                                + "), not the application under test"),
                                "recommended_action": (
                                    "A third-party origin failed during this step ("
                                    + str(_nsig.get("url") or "")[:100]
                                    + "). Verify the external service/stub; this signal does "
                                      "NOT prove the application defective. "
                                    + (diag.get("recommended_action") or "")).strip()}
                    trace(event="network_signal", scenario_id=sid, step=step,
                          kind=_nsig.get("kind"))
            except Exception:
                pass
            # P2-full: a locator/selector-class failure → auto-capture the live a11y tree
            # and try a MULTI-SIGNAL (similo) re-anchor automatically, instead of stopping
            # for a human. One capture+reanchor attempt per step; similo only returns a
            # confident, unambiguous, role-compatible NAMED match (else REFUSE), so a wrong
            # match can never silently re-bind. The re-anchor is applied and the loop
            # recompiles + RE-RUNS to PROVE it green — never green-wash.
            if diag["cause"] in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT"):
                ra_key = (sid, step, "ra")
                if attempts.get(ra_key, 0) < 1:
                    attempts[ra_key] = attempts.get(ra_key, 0) + 1
                    trace(event="reanchor_capture", scenario_id=sid, step=step, cause=diag["cause"])
                    reanchor = await _auto_capture_and_reanchor(
                        tenant_id=tenant_id, artifact_id=artifact_id, token=token,
                        scenario_id=sid, step_number=step, tc=tc, field_meta=field_meta,
                        base_url=base_url, data=data, storage_state=storage_state,
                        ov=overrides.get(sid), ra=reanchors.get(sid),
                        spec_path=spec_path_by_sid.get(sid, ""), env_context=env_context,
                    )
                    if reanchor and reanchor.get("name"):
                        # P7: a bounded, k-anon-gated learned prior nudges the surfaced
                        # confidence from past PROVEN heals of this drift SHAPE (fail-open;
                        # NEVER overrides the refuse-floor or the prove-green oracle), and
                        # stashes the de-identified shape so the heal-event records it for
                        # the flywheel. Neutral until >= K_ANON observations accumulate.
                        try:
                            from ..services.diff_and_heal import heal_learning
                            _rawc = float(reanchor.get("confidence") or 0.0)
                            _o7 = (self_heal._observed(self_heal._baseline_step(tc, step))
                                   if tc is not None else {})
                            _shape = {"recorded_kind": str(_o7.get("kind") or ""),
                                      "resolved_role": str(reanchor.get("role") or ""),
                                      "score_bucket": heal_learning.score_bucket(_rawc),
                                      "fix_kind": "reanchor"}
                            async with tenant_scoped_session(tenant_id) as _s7:
                                _nudge = await heal_learning.prior(
                                    _s7, tenant_id=tenant_id, key=heal_learning.pattern_key(**_shape))
                            reanchor["confidence"] = heal_learning.apply_prior(_rawc, _nudge)
                            heal_shapes[sid] = _shape
                        except Exception:
                            pass  # fail-open: learning never breaks a heal
                        reanchors.setdefault(sid, {})[step] = {
                            "name": reanchor["name"],
                            "frame_selector": reanchor.get("frame_selector", ""),
                            # P6 over-qualified disambiguation: thread the block anchor so the
                            # compiler scopes a REPEATED control to its one card/row (absent =>
                            # byte-identical name-only re-anchor). Without this the re-bind name
                            # ("Add to cart") is ambiguous and hits a strict-mode 6-match.
                            "anchor": reanchor.get("anchor", ""),
                            "anchor_kind": reanchor.get("anchor_kind", ""),
                        }
                        trace(event="reanchor_applied", scenario_id=sid, step=step,
                              name=reanchor["name"], confidence=reanchor.get("confidence"),
                              anchor=reanchor.get("anchor", ""),
                              frame=reanchor.get("frame_selector", ""))
                        continue  # recompile with the re-anchor and re-run to PROVE green
                    if reanchor and reanchor.get("login_detected"):
                        # AUTH legibility: the control matched nothing because the run is
                        # UNAUTHENTICATED — an expired/missing session redirected the app to a
                        # login screen, so the recorded control genuinely isn't present.
                        # Reclassify to a SPECIFIC, actionable cause (re-authenticate) instead
                        # of the misleading 'locator not found / renamed control', and so the
                        # select-as-text fallback below does NOT fire (nothing to heal on a
                        # login page). The honest stop surfaces this.
                        diag = {**diag, "cause": "AUTH_NOT_AUTHENTICATED",
                                "cause_label": "Not authenticated — page redirected to a login screen",
                                "recommended_action": (
                                    "the run is NOT authenticated: an expired or missing login "
                                    "session redirected the app to its sign-in screen, so the "
                                    "recorded control isn't present. Re-capture / refresh the auth "
                                    "session (many apps expire a session in minutes) or include the "
                                    "login steps in the recording. This is NOT a renamed/removed "
                                    "control and NOT an environment / bot block."),
                                "evidence": (diag.get("evidence") or []) + [
                                    "the failing page is a login screen"
                                    + (f" ({reanchor.get('login_password_fields')} password field(s))"
                                       if reanchor.get("login_password_fields") else "")
                                    + (f" at {reanchor.get('login_url')}" if reanchor.get("login_url") else "")]}
                        trace(event="auth_required", scenario_id=sid, step=step,
                              url=reanchor.get("login_url", ""))
                        # RE-LOGIN-IN-FLOW (Phase-3, gated NEXUS_RELOGIN_IN_FLOW=1,
                        # DEFAULT OFF): the session EXPIRED mid-run. Decide a grounded
                        # recovery and retry ONCE — re-inject the freshest stored session
                        # (an operator may have refreshed it in the auth-live window) —
                        # else surface an ACTIONABLE plan on the honest stop. NEVER
                        # green-wash: the prove-green re-run still decides; a still-stale
                        # session re-hits login -> the loop stops honestly next pass.
                        if os.getenv("NEXUS_RELOGIN_IN_FLOW") == "1" \
                                and attempts.get((sid, step, "relogin"), 0) < 1:
                            attempts[(sid, step, "relogin")] = 1
                            from ..services.test_factory import relogin as _relogin
                            _steps_view = []
                            try:
                                for _st in (getattr(tc, "steps", None) or []):
                                    _o = self_heal._observed(_st) or {}
                                    _steps_view.append({
                                        "step_number": getattr(_st, "step_number", None)
                                        or (_st.get("step_number") if isinstance(_st, dict) else None),
                                        "kind": _o.get("kind"), "verb": _o.get("verb"),
                                        "label": _o.get("label"), "value": _o.get("value")})
                            except Exception:
                                _steps_view = []
                            _prologue = _relogin.find_login_prologue(_steps_view)
                            _fresh = None
                            try:
                                async with tenant_scoped_session(tenant_id) as _s:
                                    _fresh = await auth_profiles.get_storage_state(
                                        _s, envelope=ctx.get("envelope"),
                                        tenant_id=tenant_id, artifact_id=artifact_id)
                            except Exception:
                                _fresh = None
                            _plan = _relogin.plan_recovery(
                                login_detected=True, profile_present=bool(_fresh),
                                prologue_steps=_prologue)
                            if _plan["action"] == "reinject_profile" and _fresh and _fresh != storage_state:
                                storage_state = _fresh
                                trace(event="relogin_reinject", scenario_id=sid, step=step)
                                continue  # re-run with the refreshed session to PROVE the login cleared
                            diag = {**diag,
                                    "recommended_action": _plan.get("recommended_action")
                                    or diag.get("recommended_action"),
                                    "evidence": (diag.get("evidence") or []) + [_plan.get("reason", "")]}
                            trace(event="relogin_plan", scenario_id=sid, step=step,
                                  action=_plan.get("action"))
                    if reanchor and reanchor.get("canvas_detected"):
                        # P5-FULL (opt-in, env NEXUS_VISUAL_HEAL_ENABLED=1, DEFAULT OFF):
                        # try a VLM visual locate -> a coordinate click, GATED by the
                        # prove-green re-run. A wrong coordinate breaks the scenario flow
                        # (a later step fails RED) -> never green. The VLM is only a
                        # proposer; the existing prove-green gate is the sole authority.
                        if os.getenv("NEXUS_VISUAL_HEAL_ENABLED") == "1" \
                                and attempts.get((sid, step, "vis"), 0) < 1:
                            attempts[(sid, step, "vis")] = 1
                            _cap = heal_capture_store.get(
                                tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=sid)
                            _shot = (_cap or {}).get("shot") or ""
                            _loc = None
                            if _shot:
                                try:
                                    from ..services.diff_and_heal import visual_locate
                                    _o = (self_heal._observed(self_heal._baseline_step(tc, step))
                                          if tc is not None else {})
                                    _loc = await visual_locate.locate(
                                        screenshot_b64=_shot,
                                        description=str(_o.get("label") or _o.get("verb") or ""),
                                        value=str(_o.get("value") or ""),
                                        viewport=(_cap or {}).get("viewport") or {})
                                except Exception:
                                    _loc = None
                            if _loc and _loc.get("x") is not None and _loc.get("y") is not None:
                                visual.setdefault(sid, {})[step] = {"x": _loc["x"], "y": _loc["y"]}
                                trace(event="visual_applied", scenario_id=sid, step=step,
                                      x=_loc.get("x"), y=_loc.get("y"), confidence=_loc.get("confidence"))
                                continue  # recompile with the coordinate + re-run to PROVE green
                            trace(event="visual_refused", scenario_id=sid, step=step)
                        # P5-safe honest diagnosis (opt-in OFF, or visual refused): the
                        # recorded control matched no DOM/a11y signal but the page carries a
                        # <canvas> — a pixel-drawn control. Diagnose HONESTLY, never blind-heal.
                        diag = {**diag, "cause": "CANVAS_NO_DOM",
                                "cause_label": "Canvas / visual control (no DOM handle)",
                                "recommended_action": (
                                    "this control is drawn on a <canvas> with no DOM handle — a "
                                    "visual/coordinate interaction is required (opt-in visual tier) "
                                    "or a human can confirm; this is NOT an environment block"),
                                "evidence": (diag.get("evidence") or []) + [
                                    f"{reanchor.get('canvas_count', 0)} <canvas> element(s) on the "
                                    "failing page; the recorded control matched no DOM/a11y signal."]}
                        trace(event="canvas_detected", scenario_id=sid, step=step,
                              canvas=reanchor.get("canvas_count", 0))
                    trace(event="reanchor_refused", scenario_id=sid, step=step)
                # no confident re-anchor → fall through to the honest diagnosis stop below

            # P4 flake-wait synthesis: a TIMEOUT whose cause isn't a locator / control-kind /
            # real-regression = a timing/async flake (the control IS there, the action just
            # didn't settle). Add a page-settle wait + re-run ONCE to confirm; if it then
            # passes it was a flake (PROVEN green by the re-run), else fall through to the
            # honest stop. Never green-wash — the re-run must actually pass.
            _ferr = (f.get("error_message") or "").lower()
            if "timeout" in _ferr and diag["cause"] in ("NEEDS_REVIEW", "FLAKE"):
                st_key = (sid, step, "stab")
                if attempts.get(st_key, 0) < 1:
                    attempts[st_key] = attempts.get(st_key, 0) + 1
                    stabilize.setdefault(sid, {})[step] = True
                    trace(event="stabilize_applied", scenario_id=sid, step=step)
                    continue  # recompile with the settle-wait and re-run to PROVE green
                trace(event="stabilize_exhausted", scenario_id=sid, step=step)

            # FALLBACK — the no-accessible-name <select> case (e.g. SauceDemo's sort
            # dropdown): a LOCATOR_NOT_FOUND whose re-anchor REFUSED (similo found no
            # accessible-name match) may be a <select> mis-classified as text. Try the
            # SELECT control-kind override ONCE: it recompiles the step as a <select>, which
            # binds via the content-anchored rung (the <select> that CONTAINS the recorded
            # option) for BOTH the action and its committed-value oracle. The prove-green
            # re-run decides — a non-select control just fails RED and falls to the honest
            # stop. Never green-wash; this only retires the "select-as-text" mis-classify.
            if diag["cause"] in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT"):
                _sk = (sid, step, "selfb")
                if attempts.get(_sk, 0) < 1:
                    _ovs = self_heal.select_override_for_step(tc, field_meta, step)
                    if _ovs is not None and _ovs[0] not in overrides.get(sid, {}):
                        attempts[_sk] = 1
                        overrides.setdefault(sid, {})[_ovs[0]] = _ovs[1]
                        heal_shapes[sid] = {"recorded_kind": str(observed.get("kind") or ""),
                                            "resolved_role": "combobox", "score_bucket": 0,
                                            "fix_kind": "select_content_fallback"}
                        trace(event="select_content_fallback", scenario_id=sid, step=step)
                        continue  # recompile as <select> (content rung) + re-run to PROVE green

            # INTERACTION-REVERT: a recipe applied on an earlier pass did NOT fix this
            # step (it is failing again). REVERT it — the recipe early-return otherwise
            # shadows every later fix on this step (fix accumulation is only correct for
            # fixes that compose; an exclusive recipe that failed must get out of the
            # way). Falls through so the remaining rungs act on this same pass.
            _cur_ints = (overrides.get(sid) or {}).get("__interactions__") or {}
            if step in _cur_ints and attempts.get((sid, step, "intr"), 0) >= 1:
                _cur_ints.pop(step, None)
                trace(event="interaction_reverted", scenario_id=sid, step=step)

            # ENTRY-URL NORMALIZATION (grounded in the recording's own page_visits): the
            # run never actually REACHED the recorded page — a malformed recorded entry URL
            # (OCR truncation: apex host / dropped suffix) lands on a login or wrong page
            # and every later step "fails". Ground the fix in recorded evidence: the
            # page_visit on the SAME site whose path stem matches the entry's; if its full
            # URL differs from the compiled entry, drive entry THERE and re-run to PROVE.
            # Never invented (page_visits are the recording's URL evidence); a wrong
            # candidate fails RED downstream — never green-wash. Once per scenario.
            if diag.get("cause") in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT",
                                     "AUTH_NOT_AUTHENTICATED"):
                _nfk = (sid, "navfix")
                _fixes = self_heal.entry_url_candidates(tc, visits, storage_state=storage_state)
                if _fixes and attempts.get(_nfk, 0) < len(_fixes["candidates"]):
                    _ci = attempts.get(_nfk, 0)
                    attempts[_nfk] = _ci + 1
                    _cand = _fixes["candidates"][_ci]
                    # EVIDENCE INVALIDATION: every heal attempted so far reasoned against
                    # the WRONG page (the malformed entry landed elsewhere) — a reanchor
                    # refused on a login screen, a select-fallback applied to a page that
                    # wasn't the recorded one. Reset this scenario's heal state (attempts
                    # + accumulated fixes, keeping ONLY the nav override) so every rung
                    # re-attempts against the corrected page. Bounded: at most one reset
                    # per candidate (<=3) => still convergent; each rung must re-PROVE on
                    # the re-run => never green-wash.
                    overrides[sid] = {"__nav_overrides__": {_fixes["step_number"]: _cand}}
                    reanchors.pop(sid, None)
                    stabilize.pop(sid, None)
                    visual.pop(sid, None)
                    phantom_skips.pop(sid, None)
                    heal_shapes.pop(sid, None)
                    for _k in [k for k in list(attempts)
                               if isinstance(k, tuple) and k and k[0] == sid and k != _nfk]:
                        attempts.pop(_k, None)
                    trace(event="entry_url_normalized", scenario_id=sid, step=step,
                          from_url=_fixes.get("from", ""), to_url=_cand,
                          candidate=f"{_ci + 1}/{len(_fixes['candidates'])}",
                          heal_state_reset=True)
                    continue  # recompile with the corrected entry + re-run to PROVE green

            # WIZARD-ADVANCE recovery (dropped-intermediate-navigation class): the
            # recorded control is ABSENT here because the recording advanced a wizard
            # (e.g. profile -> plan) through a navigation the extraction dropped. Advance
            # via the page's OWN progression control until the label appears — bounded
            # (<=3 pages), self-guarded (label present => zero clicks => inert), non-
            # destructive curated labels only; the step's action + oracle still decide.
            # Advancing changes this step's world -> reset ITS rung attempts (the
            # evidence-invalidation principle, per-step). Never green-wash.
            if diag.get("cause") in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT") and step > 1:
                _ak = (sid, step, "adv")
                if attempts.get(_ak, 0) < 1:
                    attempts[_ak] = 1
                    overrides.setdefault(sid, {}).setdefault("__pre_advance__", {})[step] = 3
                    for _k in [k for k in list(attempts)
                               if isinstance(k, tuple) and len(k) >= 2
                               and k[0] == sid and k[1] == step and k != _ak]:
                        attempts.pop(_k, None)
                    trace(event="wizard_advance_applied", scenario_id=sid, step=step,
                          max_pages=3)
                    continue  # recompile with the advance preamble + re-run to PROVE green

            # CONTROL-KIND INTERACTION re-synthesis (the UACR recipe library): diagnose
            # found the control changed KIND (native <select> -> custom ARIA combobox /
            # range slider / role=switch / accordion / progressively-revealed field) and
            # returned a grounded, runtime-introspecting recipe with its OWN committed-
            # value oracle. Apply it via the compiler's additive `interactions` channel
            # and re-run to PROVE green — a wrong recipe fails RED fast (bounded ~6s),
            # never a green-wash. Once per step.
            # B3 EVIDENCE-SCOPED DISAMBIGUATION (AMBIGUOUS_LOCATOR): a repeated visible
            # name matched N controls. If the recording GROUNDED a disambiguating anchor
            # (observed['anchor'] captured for this control), scope to it and re-run; else
            # we do NOT guess which of the N — the honest stop's recommendation stands.
            # Inert when no anchor was captured (the named capture-side gap); rescues any
            # app whose extraction did ground a neighbor. Once/step.
            if diag.get("cause") == "AMBIGUOUS_LOCATOR" \
                    and attempts.get((sid, step, "disamb"), 0) < 1:
                attempts[(sid, step, "disamb")] = 1
                _anch = (observed.get("anchor") or observed.get("neighbor") or "").strip()
                if _anch:
                    reanchors.setdefault(sid, {})[step] = {
                        "name": observed.get("label") or "", "anchor": _anch,
                        "anchor_kind": observed.get("anchor_kind") or "block",
                        "frame_selector": ""}
                    trace(event="ambiguity_scoped", scenario_id=sid, step=step, anchor=_anch[:60])
                    continue  # recompile scoped to the grounded anchor + re-run to PROVE green
                trace(event="ambiguity_unresolved_no_anchor", scenario_id=sid, step=step)

            # B7 CLOSED-SHADOW last resort (gated NEXUS_FORCE_OPEN_SHADOW=1, DEFAULT OFF):
            # a control genuinely not found + every grounded heal refused MAY sit in a
            # CLOSED shadow root (no distinct runtime signal, so this is opt-in, not
            # auto-detected). Force all roots open before boot and re-run ONCE; the step's
            # own oracle still decides. Applied per-scenario (the addInitScript is global).
            if os.getenv("NEXUS_FORCE_OPEN_SHADOW") == "1" \
                    and diag.get("cause") in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT") \
                    and attempts.get((sid, "force_open_shadow"), 0) < 1:
                attempts[(sid, "force_open_shadow")] = 1
                overrides.setdefault(sid, {})["__force_open_shadow__"] = True
                trace(event="force_open_shadow_applied", scenario_id=sid, step=step)
                continue  # recompile with shadow roots forced open + re-run to PROVE green

            _intr = diag.get("interaction")
            if _intr and attempts.get((sid, step, "intr"), 0) < 1:
                attempts[(sid, step, "intr")] = 1
                overrides.setdefault(sid, {}).setdefault("__interactions__", {})[step] = _intr
                heal_shapes[sid] = {"recorded_kind": str(observed.get("kind") or ""),
                                    "resolved_role": str(_intr.get("kind") or ""),
                                    "score_bucket": 0, "fix_kind": "interaction"}
                trace(event="interaction_applied", scenario_id=sid, step=step,
                      kind=str(_intr.get("kind") or ""), hint=str(_intr.get("hint") or ""))
                continue  # recompile with the interaction recipe + re-run to PROVE green

            # AUTOPILOT autonomous analyst: the deterministic heals are exhausted. Before
            # escalating to a human, let the grounded LLM agent reason over the LIVE page and
            # propose a verbatim-grounded rebind/wait (auto-applied, never green-wash). Gated
            # by ctx['enable_agentic_heal'] (= autonomous); OFF => skipped, byte-identical.
            if await _try_agentic(sid, step, observed, f, diag):
                continue  # recompile with the agentic fix + re-run to PROVE green

            # ADVANCE-REVERT (last resort, one-shot): every rung was tried WITH a
            # wizard-advance applied and the step still fails. A hidden-until-reveal
            # conditional field is invisible to the advance probe (not in the DOM /
            # a11y tree pre-reveal), so the preamble can OVERSHOOT the wizard past the
            # step's real page and poison every rung that ran after it. Remove the
            # advance and reset this step's rung attempts ONCE so each rung re-attempts
            # WITHOUT it — the scenario's earlier (now-passing) steps already position
            # the page. One-shot per step => convergent; never green-wash (every retry
            # must still prove green on the re-run).
            _padv_map = (overrides.get(sid) or {}).get("__pre_advance__") or {}
            if step in _padv_map and attempts.get((sid, step, "advrev"), 0) < 1:
                attempts[(sid, step, "advrev")] = 1
                _padv_map.pop(step, None)
                for _k in [k for k in list(attempts)
                           if isinstance(k, tuple) and len(k) >= 3
                           and k[0] == sid and k[1] == step
                           and k[2] not in ("adv", "advrev")]:
                    attempts.pop(_k, None)
                trace(event="wizard_advance_reverted", scenario_id=sid, step=step)
                continue  # re-run without the advance; rungs re-attempt on the true page

            # PHANTOM-SKIP (gated NEXUS_PHANTOM_SKIP): every grounded heal REFUSED — the control
            # is genuinely absent. If this step is an EXACT duplicate of an EARLIER step that
            # already PASSED this run, it is a fabricated / misplaced generation artifact (e.g. a
            # 'sort' step duplicated onto a page that has no sort control). No-op it (NOT
            # test.skip, which aborts) so the recorded flow CONTINUES. Never green-wash: a proven
            # duplicate is recognized — never a real step — and the phantom asserts nothing.
            if (os.getenv("NEXUS_PHANTOM_SKIP") == "1"
                    and diag["cause"] in ("LOCATOR_NOT_FOUND", "SELECTOR_DRIFT")):
                _phk = (sid, step, "phantom")
                if attempts.get(_phk, 0) < 1 and self_heal.is_phantom_duplicate(tc, step, tl, sid):
                    attempts[_phk] = 1
                    phantom_skips.setdefault(sid, set()).add(step)
                    trace(event="phantom_skip", scenario_id=sid, step=step)
                    continue  # recompile with the phantom step no-op'd + re-run to PROVE green

            # DEFECT-REPRODUCES CHECK (Phase-3): a REAL_REGRESSION is only actionable if it
            # REPRODUCES — a single observation may be a flake. Re-run ONCE to confirm before
            # recommending a defect. If the re-run passes, it was a flake (the loop reaches
            # green); if it fails differently, it re-diagnoses; only a 2nd INDEPENDENT
            # REAL_REGRESSION is CONFIRMED and recommended for filing. Never green-wash on
            # either side: we neither hide a reproduced regression nor report an unreproduced
            # flake as a bug. One extra re-run (attempts-gated → never loops).
            # NAV-RECOVER on a PROVEN transition (dropped causing-click): the recording
            # PROVED this page is reached, but the click that causes it was dropped by
            # extraction — the app never navigates and the hard oracle correctly fails.
            # Perform the missing user action (the app's own link/progression control)
            # and re-run: the hard toHaveURL stays UNTOUCHED, so a genuinely broken
            # navigation still fails RED (recovery never softens an oracle). Once/step.
            if diag.get("cause") == "REAL_REGRESSION" \
                    and (observed.get("verb") or "").strip().lower() == "navigate":
                _nrk = (sid, step, "navrec")
                if attempts.get(_nrk, 0) < 1:
                    attempts[_nrk] = 1
                    overrides.setdefault(sid, {}).setdefault("__nav_recover__", {})[step] = True
                    trace(event="nav_recover_applied", scenario_id=sid, step=step)
                    continue  # re-run: recovery + the SAME hard assertion decide

            if diag["cause"] == "REAL_REGRESSION":
                _rgk = (sid, step, "regconfirm")
                if attempts.get(_rgk, 0) < 1:
                    attempts[_rgk] = 1
                    trace(event="regression_reproduce_check", scenario_id=sid, step=step)
                    continue  # re-run to confirm the regression reproduces before a defect
                diag = {**diag, "recommended_action": (
                    (diag.get("recommended_action") or "")
                    + " (CONFIRMED: the regression reproduced across 2 independent runs — "
                      "not a flake.)")}
                trace(event="regression_confirmed", scenario_id=sid, step=step)
                # AUTO-AUTHORED DEFECT REPORT (R5): a CONFIRMED real regression
                # ships with a filing-ready defect — repro steps with the failing
                # one flagged, expected-vs-actual, flow-mechanical severity, and
                # paste-ready markdown — folded into diag so it persists on
                # stop_diag and reaches every surface that renders the stop.
                # (build_defect existed complete+pure with ZERO call sites —
                # requirements-audit finding.) Additive: never breaks the stop.
                try:
                    from ..services.test_factory.defect_report import (
                        build_defect, defect_to_markdown)
                    _defect = build_defect(
                        tc=tc, failing_step_number=step, diag=diag,
                        network=diag.get("network"),
                        error_message=str(job.get("output") or "")[-2000:],
                        base_url=str(observed.get("url")
                                     or observed.get("next_url") or ""),
                        scenario_id=sid,
                    )
                    diag = {**diag, "defect_report": _defect,
                            "defect_markdown": defect_to_markdown(_defect)}
                    trace(event="defect_report_authored", scenario_id=sid, step=step)
                except Exception:
                    pass  # authoring is additive — never break the honest stop

            if diag["cause"] != "WRONG_CONTROL_KIND":
                # P0 legibility: lead with the GROUNDED cause + the recommended action,
                # not a generic "needs a human". Only frame it as an environment issue
                # when diagnose actually classified it as one.
                _ra = diag.get("recommended_action") or ""
                _isenv = diag.get("cause") in ("FLAKE", "ENVIRONMENT", "ENV_BLOCK")
                # FINAL-STATE CONSISTENCY GUARD: the diagnosis may have been made
                # on an EARLIER iteration (e.g. step-2 ambiguity), while the last
                # re-run died earlier at the network layer (bot-block class:
                # ERR_HTTP2/net::ERR_*/timeout on goto). A stop must never carry
                # a stale in-page diagnosis — or claim "not a bot-block" — when
                # the final failure IS environment-class. Supersede honestly.
                if not _isenv:
                    try:
                        from ..services.test_factory.qe_agents import triage_classify
                        _final_out = str(job.get("output") or "")[-4000:]
                        _tri = triage_classify({"status": "failed", "error": _final_out})
                        if _tri.get("classification") == "environment":
                            _prev_label = diag.get("cause_label") or diag.get("cause") or ""
                            diag = {**diag,
                                    "cause": "ENV_BLOCK",
                                    "cause_label": "Environment/bot-block on the final re-run",
                                    "recommended_action": (
                                        "The LAST re-run failed at the entry/network layer ("
                                        + _tri.get("evidence", "")[:100]
                                        + ") — a datacenter-IP / bot-protection class no locator "
                                          "work can fix. Run against an environment that admits "
                                          "the runner (your own app, the proving ground, or an "
                                          "allow-listed egress). Earlier in-page diagnosis ("
                                        + _prev_label
                                        + ") applies only once the page is reachable — "
                                          "final re-run superseded it."),
                                    "superseded_diagnosis": _prev_label}
                            _ra = diag["recommended_action"]
                            _isenv = True
                            trace(event="stop_diag_superseded_env", scenario_id=sid, step=step)
                    except Exception:
                        pass  # the guard must never break the stop path
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"step {step}: {diag['cause_label']}"
                                        + (f" — {_ra}" if _ra else "")
                                        + ("" if _isenv else " (not an environment/bot-block)")),
                           stop_diag=diag)
                trace(event="stop_needs_human", scenario_id=sid, step=step, cause=diag["cause"])
                await _record_heal_stop(tenant_id, artifact_id, sid, step, diag.get("cause"),
                                        f"{diag.get('cause_label', '')}{(' — ' + _ra) if _ra else ''}")
                return

            key = (sid, step)
            attempts[key] = attempts.get(key, 0) + 1
            ov_entry = self_heal.select_override_for_step(tc, field_meta, step)
            sid_ov = overrides.setdefault(sid, {})
            if ov_entry is None or ov_entry[0] in sid_ov or attempts[key] > max_attempts:
                # P0 legibility: the control-kind fix was applied but the step still fails.
                # Do NOT hardcode "environment/bot-block" — lead with the grounded diagnosis
                # (diag) we just computed. A locator timeout here means the recorded name did
                # not resolve on the live page (a re-bind is needed) — categorically NOT an
                # environment block.
                _cl = diag.get("cause_label") or "could not auto-fix this step"
                _ra = diag.get("recommended_action") or ""
                _isenv = diag.get("cause") in ("FLAKE", "ENVIRONMENT", "ENV_BLOCK")
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"step {step}: {_cl}"
                                        + (f" — {_ra}" if _ra else "")
                                        + " (the control-kind fix alone did not resolve it"
                                        + ("" if _isenv else "; this needs a locator re-bind, "
                                           "not an environment/bot-block") + ")"),
                           stop_diag=diag)
                trace(event="stop_no_progress", scenario_id=sid, step=step, cause=diag.get("cause"))
                await _record_heal_stop(tenant_id, artifact_id, sid, step, diag.get("cause"),
                                        f"{_cl}{(' — ' + _ra) if _ra else ''}")
                return
            label_norm, sig = ov_entry
            sid_ov[label_norm] = sig
            trace(event="heal_applied", scenario_id=sid, step=step,
                  label=observed.get("label", ""), fix=".fill -> .selectOption", attempt=attempts[key])

        job.update(status="failed", terminal_state="needs_human",
                   stop_reason="reached the auto-heal iteration limit without a full green")
    except Exception as exc:
        job.update(status="error", terminal_state="error", stop_reason=f"auto-heal error: {exc}")
    await _persist_job(run_id)  # durable terminal auto-heal outcome (survives restart)


@router.post("/api/v1/test-factory/{artifact_id}/auto-heal/run-config")
async def auto_heal_run(
    body: RunConfigRequest,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Auto-Heal Run — run the selected suite HEADED and, on a failing step, auto
    diagnose + fix (control-kind) + re-run + continue until the whole suite is green,
    then save an immutable "Clean Run - V1". Stops toward a human (writing nothing)
    on any failure that isn't a confirmable control-kind fix. Returns {run_id,
    live_url}; poll /playwright/run/{run_id} for { heal_trace, terminal_state,
    clean_run_version, stop_reason }. One live run at a time. $0 LLM, no migration."""
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
    if tcids:
        sel = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
    elif cats:
        sel = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
    else:
        sel = list(cases)
    selected = [(getattr(c, "test_id", "") or "") for c in sel if getattr(c, "test_id", "")]
    if not selected:
        raise HTTPException(status_code=404, detail="no matching active test cases to run")

    run_id = uuid.uuid4().hex
    await _register_job(run_id, {
        "run_id": run_id, "status": "running", "artifact_id": artifact_id,
        "tenant_id": tenant_id, "kind": "auto-heal",
        "target": (body.base_url or "").strip(), "scripts": len(selected),
        "exit_code": None, "output": "", "steps_completed": 0, "total_tests": len(selected),
        "live": True, "auto_heal": True, "heal_trace": [], "terminal_state": None,
        "clean_run_version": None, "healed_count": 0, "stop_reason": "",
    })
    ctx = {
        "tenant_id": tenant_id, "artifact_id": artifact_id, "token": token,
        "scenario_ids": selected, "base_url": body.base_url, "data": body.data,
        "max_attempts": 3,
        # Multi-env (#7 heal parity): the resolved Environment Profile the heal re-runs
        # rebind to. None (single-env) ⇒ unchanged.
        "env_context": body.env_context,
        "storage_state": await _run_storage_state(request, artifact_id, tenant_id),
        # AUTH PERMANENCE — form-login (config + creds) so the heal loop's re-runs
        # self-login instead of riding an expiring captured cookie.
        "form_login": await _run_form_login(request, artifact_id, tenant_id),
        # AUTOPILOT (Mode B) — fully autonomous: execute+prove UNPROVEN steps + auto-apply
        # the grounded agentic analyst (no human approval). Default off => byte-identical.
        "autonomous": bool(getattr(body, "autonomous", False)),
        "enable_agentic_heal": bool(getattr(body, "autonomous", False)),
        # Re-login-in-flow (Phase-3): the per-tenant envelope so the heal loop can
        # re-fetch a refreshed auth session mid-run on a login redirect.
        "envelope": getattr(request.app.state, "envelope_service", None),
        "agentic_tier": "tier_premium",
        "agentic_min_confidence": 0.7,
    }
    task = asyncio.create_task(_run_auto_heal(run_id, ctx))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    return {"run_id": run_id, "status": "running", "live_url": _LIVE_PATH,
            "auto_heal": True, "scripts": len(selected)}


@router.post("/api/v1/test-factory/{artifact_id}/playwright/run-config")
async def playwright_run_config(
    body: RunConfigRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Compile a CONFIGURED, env+data-driven Playwright bundle for a local run:
    the selected scripts (parametrized), nexus.config.json (chosen base URL),
    vkpower.data.json (data overrides — defaults stay the observed values), and a
    one-command README. Read-only, ZERO LLM; same compiler as the plain zip.
    """
    tenant_id = user["tenant_id"]
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    if tcids:
        cases = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
    elif cats:
        cases = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
    if not cases:
        raise HTTPException(
            status_code=404, detail="no matching active test cases to configure",
        )

    files = _configured_files(
        cases, build_field_meta(visits), body.base_url, body.data,
        data_by_test=body.data_by_test,
        browsers=body.browsers, headed=body.headed,
        workers=body.workers, retries=body.retries,
        edited=edited_map,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)
    filename = f"vkpower-playwright-run-{artifact_id[:8]}.zip"
    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/api/v1/test-factory/{artifact_id}/playwright/ci-bundle")
async def playwright_ci_bundle(
    body: RunConfigRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Same configured bundle as /playwright/run-config, PLUS ready-to-commit CI
    pipelines (GitHub Actions / GitLab CI / Jenkins) that run the suite and report
    to the Grounded Triage board. Read-only, ZERO LLM."""
    tenant_id = user["tenant_id"]
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    if tcids:
        cases = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
    elif cats:
        cases = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
    if not cases:
        raise HTTPException(
            status_code=404, detail="no matching active test cases to configure",
        )

    files = _configured_files(
        cases, build_field_meta(visits), body.base_url, body.data,
        data_by_test=body.data_by_test, browsers=body.browsers, headed=body.headed,
        workers=body.workers, retries=body.retries, edited=edited_map,
    )
    files.update(ci_workflow_files(artifact_id))   # the only new line vs run-config

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)
    filename = f"nexus-ci-bundle-{artifact_id[:8]}.zip"
    return StreamingResponse(
        io.BytesIO(buf.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/api/v1/test-factory/{artifact_id}/playwright/run")
async def playwright_run(
    body: RunConfigRequest,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Execute the configured suite on the Nexus runner (server-side) and report
    results to the Grounded Triage board. Returns a run_id immediately; poll
    /playwright/run/{run_id} for live status. Read-only on the artifact, ZERO LLM.
    """
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    if tcids:
        cases = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
    elif cats:
        cases = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
    if not cases:
        raise HTTPException(status_code=404, detail="no matching active test cases to run")

    # P0.3 — quarantine gate: a case whose latest CERTIFICATION run failed for
    # a product-side (or unproven) reason has not earned the right to judge
    # the client's application — it is excluded from client runs until it
    # re-certifies. Application/environment/config certification failures are
    # NOT quarantined (a grounded regression on the baseline is a real signal;
    # infra outages must not shame the cases).
    #
    # Exploratory gate (fail-CLOSED): combination cases are built over
    # option-captured ('available', never demonstrated) values — they may face
    # the client ONLY after a certification run PROVED them on the baseline.
    # Closes the generate→certify window run 40110431 fell through.
    _exploratory_ids = {
        (getattr(c, "test_id", "") or "") for c in cases
        if str(getattr(c, "type", "") or "").lower() == "combination"
    }
    _exploratory_ids.discard("")
    async with tenant_scoped_session(tenant_id) as session:
        _quarantined = await product_quarantined_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        _ungated = await uncertified_exploratory_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            exploratory_ids=_exploratory_ids,
        )
    excluded_quarantined: list[str] = []
    excluded_uncertified: list[str] = []
    if _quarantined or _ungated:
        excluded_quarantined = [
            (getattr(c, "test_id", "") or "") for c in cases
            if (getattr(c, "test_id", "") or "") in _quarantined
        ]
        excluded_uncertified = [
            (getattr(c, "test_id", "") or "") for c in cases
            if (getattr(c, "test_id", "") or "") in _ungated
            and (getattr(c, "test_id", "") or "") not in _quarantined
        ]
        _blocked = set(excluded_quarantined) | set(excluded_uncertified)
        cases = [
            c for c in cases
            if (getattr(c, "test_id", "") or "") not in _blocked
        ]
        if not cases:
            raise HTTPException(status_code=409, detail={
                "error": "all requested cases are gated",
                "reason": (
                    "Quarantined cases failed certification for a product-side "
                    "(or not-yet-attributed) cause; exploratory combination "
                    "cases must PASS a certification run before facing the "
                    "client. Re-trigger certification (POST …/certify) or run "
                    "the demonstrated flows. This is NOT an application "
                    "failure."
                ),
                "quarantined_test_ids": excluded_quarantined[:50],
                "uncertified_exploratory_test_ids": excluded_uncertified[:50],
            })

    base_url = (body.base_url or "").strip()
    if body.env_context and body.env_context.get("base_url"):
        base_url = str(body.env_context["base_url"]).strip()  # env profile base_url wins (SSRF-guarded)
    # Fall back to the ORIGIN THE CRAWL RECORDED when neither the caller nor a
    # selected environment supplied one. Without it NEXUS_BASE_URL is "", and the
    # compiled spec's relative `page.goto('/portal/...')` has no origin to resolve
    # against — every step fails for a reason that has nothing to do with the app.
    if not base_url:
        base_url = _recorded_origin(visits)
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    # Persona × Environment (P2): a NAMED persona resolves to a recipe + card and
    # the run authenticates AS that member. Absent ⇒ the form-login path exactly
    # as before. Identity is a DECLARED input, never an ambient session.
    persona_id = (body.persona_id or "").strip()
    environment_id = (body.environment_id or body.env_context and
                      str((body.env_context or {}).get("environment_id") or "") or "").strip() \
        if not (body.environment_id or "").strip() else body.environment_id.strip()
    # Scale (P5): resolve a persona by TRAITS / behavior class when no id is
    # pinned — "run as any senior-large-family member". An honest miss (no
    # match) is a 422, never a guessed member.
    if not persona_id and body.persona_match:
        async with tenant_scoped_session(tenant_id) as session:
            _pl = await persona_store.list_personas(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
        sel = persona_scale.select_persona(
            _pl, traits=(body.persona_match or {}).get("traits") or [],
            behavior_class=(body.persona_match or {}).get("behavior_class") or "")
        if not sel.get("persona"):
            raise HTTPException(status_code=422, detail={
                "error": "no persona matches the requested traits/behavior class",
                "persona_match": body.persona_match, "reason": sel.get("reason"),
                "note": "No member is guessed. Define a matching persona or relax "
                        "the request. This is NOT an application failure.",
            })
        persona_id = sel["persona"]["persona_id"]
    run_id = uuid.uuid4().hex
    reservation_id = ""

    # F5 — RUN AS A REAL, CURRENT MEMBER OF THIS APPLICATION.
    # persona_id is a declared input and nothing checked it. A retired member kept
    # running from a pinned CI config; a member of a different application in the
    # same tenant was accepted and its card typed into this app's login; an id that
    # resolved to nothing ran anyway while the report named a member nobody defined.
    # Checked here rather than in the picker, because the picker already hides
    # retired members and every one of these arrives from a caller that never sees it.
    if persona_id:
        async with tenant_scoped_session(tenant_id) as session:
            _prow = await persona_store.get_persona(
                session, tenant_id=tenant_id, persona_id=persona_id)
        _ident = persona_identity.check_persona(
            persona_id=persona_id, persona=_prow, artifact_id=artifact_id)
        if not _ident["allowed"]:
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": "member_identity",
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": _ident["reason"], "detail": _ident["detail"],
                "note": _ident["detail"].get("note", ""),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }
    # ── Environment governance (P4): posture, production default-deny, health ──
    # The ENVIRONMENT is a governed target regardless of persona. An unregistered
    # environment resolves to {} ⇒ a default read_write, non-production target
    # (today's behaviour, byte-identical). A production target is default-deny.
    async with tenant_scoped_session(tenant_id) as session:
        governance_env = await persona_store.get_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id) or {}

    # F1 — THE SELECTED ENVIRONMENT DECIDES THE DESTINATION.
    # environment_id already chose the credential card, the posture, the reservation
    # and the member-data answers; the ADDRESS came from the request body, which the
    # portal defaults to the crawled origin. Posture is therefore enforced from the
    # row named by the LABEL while the traffic goes wherever the request pointed — so
    # selecting 'uat' with a production address passes uat's read_write gate and
    # mutates production, while the report, the parity trend and the certification
    # ledger all record 'uat'. Resolve the destination FIRST, then gate on it.
    env_routing = {"source": "request", "reason": ""}
    if _env_routing_enabled():
        env_routing = environment_routing.resolve_destination(
            environment_id=environment_id, environment=governance_env or None,
            requested_base_url=base_url,
            requested_env_context=body.env_context)
        if not env_routing["allowed"]:
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": "environment",
                "environment_id": environment_id,
                "reason": env_routing["reason"],
                "detail": env_routing["detail"],
                "note": ("The run was BLOCKED before dispatch because its destination "
                         "could not be established honestly. Running would have "
                         "exercised one target while reporting another. This is a "
                         "configuration conflict, NOT an application failure."),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }
        if env_routing["source"] == "environment":
            base_url = env_routing["base_url"]
            body.env_context = env_routing["env_context"]

    gate = persona_governance.gate_dispatch(
        governance_env, mutating_requested=bool(body.mutating))
    posture = gate["posture"]
    if not gate["allowed"]:
        return {
            "run_id": run_id, "status": "blocked",
            "blocked_reason": gate["attribution"],   # environment_policy
            "persona_id": persona_id, "environment_id": environment_id,
            "posture": posture, "detail": " ".join(gate["reasons"]),
            "note": ("The ENVIRONMENT policy refused this run (production is "
                     "default-deny, or the environment is in a known-bad health "
                     "state). This is an environment decision — the application "
                     "under test is NOT implicated."),
            "scripts": 0, "excluded_quarantined": excluded_quarantined,
            "excluded_uncertified_exploratory": excluded_uncertified,
        }
    # ── Behavior-class scoping (P4): out-of-class cases refused, never failed ──
    # A case a structure fork bound to a behavior class (a 5-beneficiary journey a
    # 2-kid member never walks) does not apply to an out-of-class persona. Such a
    # case is SKIPPED with a test_scope attribution — never run-and-failed as if
    # the app broke. Only named personas carry a class; the recording baseline
    # walks its own journeys by construction.
    out_of_scope: list[dict] = []
    if persona_id and not persona_id.startswith("persona0::"):
        async with tenant_scoped_session(tenant_id) as session:
            _p = await persona_store.get_persona(
                session, tenant_id=tenant_id, persona_id=persona_id)
            scopes = await persona_store.get_case_scopes(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
        persona_class = (_p or {}).get("behavior_class", "") if _p else ""
        if scopes:
            # a case's stable scenario id is ProductionTestCase.test_id, which
            # equals FactoryTestCaseRow.test_case_id (the id the scope registry
            # and the UI use) — see service._row_values ("test_case_id": tc.test_id).
            req_by_case = {str(getattr(c, "test_id", "") or ""):
                           scopes.get(str(getattr(c, "test_id", "") or ""), "")
                           for c in cases}
            part = persona_governance.partition_suite(req_by_case, persona_class)
            drop = {o["scenario_id"] for o in part["out_of_scope"]}
            out_of_scope = part["out_of_scope"]
            if drop:
                cases = [c for c in cases
                         if str(getattr(c, "test_id", "") or "") not in drop]
            if not cases:
                return {
                    "run_id": run_id, "status": "blocked",
                    "blocked_reason": persona_governance.ATTR_TEST_SCOPE,
                    "persona_id": persona_id, "environment_id": environment_id,
                    "behavior_class": persona_class, "posture": posture,
                    "detail": "; ".join(o["reason"] for o in out_of_scope[:5]),
                    "note": ("Every requested case belongs to a behavior class this "
                             "persona is not in. The suite does not apply to this "
                             "member — skipped as out-of-scope, NOT failed. The "
                             "application is not implicated."),
                    "scripts": 0, "out_of_scope": out_of_scope,
                    "excluded_quarantined": excluded_quarantined,
                    "excluded_uncertified_exploratory": excluded_uncertified,
                }
        # Scoped certification (R3, OPT-IN): an environment that requires it admits
        # only cases CERTIFIED for THIS (environment, behavior_class) cell — a
        # certification on the baseline (or another env/class) does NOT grant trust
        # here. Default off ⇒ unchanged behaviour.
        if governance_env.get("require_scoped_certification"):
            async with tenant_scoped_session(tenant_id) as session:
                _certified = await persona_store.cell_certified_scenarios(
                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                    environment_id=environment_id, behavior_class=persona_class)
            cases = [c for c in cases
                     if str(getattr(c, "test_id", "") or "") in _certified]
            if not cases:
                return {
                    "run_id": run_id, "status": "blocked",
                    "blocked_reason": "needs_scoped_certification",
                    "persona_id": persona_id, "environment_id": environment_id,
                    "behavior_class": persona_class, "posture": posture,
                    "detail": (f"environment '{environment_id}' requires a scoped "
                               f"certification for the ('{environment_id}', "
                               f"'{persona_class or 'any'}') cell; none of the requested "
                               "cases are certified there yet. Certify on THIS "
                               "environment as THIS member class first — a certification "
                               "on the baseline or another env/class does not grant "
                               "trust here."),
                    "note": ("Environment policy: scoped certification required. This "
                             "is an environment decision, NOT an application failure."),
                    "scripts": 0,
                    "excluded_quarantined": excluded_quarantined,
                    "excluded_uncertified_exploratory": excluded_uncertified,
                }
    if persona_id:
        _auth = await _persona_auth_resolve(
            request, artifact_id, tenant_id, persona_id, environment_id,
            environment=governance_env)
        auth_config, login_env = _auth["auth_config"], _auth["login_env"]

        # F4 — A SLOT CHANGE MUST BLOCK, NOT SKIP.
        # The recorded login can be re-recorded at any time, and slot names come from
        # the app's own form (a framework-regenerated id is enough to change one), so
        # a card provisioned yesterday can stop matching today. Without this, the
        # compiled globalSetup finds the slot's env unset, SKIPS THE ENTIRE LOGIN,
        # the suite executes logged out, and every failure is attributed to the
        # application under test. Refusing costs an afternoon; the alternative costs
        # trust in the verdict.
        #
        # Derived live from (active recipe x decrypted card) rather than read from a
        # stored flag: a card row can be written by a script, a bulk import or a
        # migration that never ran the marker, and enforcement must not depend on
        # every past code path having remembered.
        _state = _auth.get("state") or {}
        if _card_gate_enabled() and _state and not _state.get("runnable", True):
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": "credential",
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": _state.get("reason") or _state.get("state"),
                "detail": _state.get("detail") or {},
                "note": ("This member cannot perform the recorded login for this "
                         "environment, so the run is BLOCKED rather than executed "
                         "logged out. A skipped login would have run the whole suite "
                         "unauthenticated and attributed every failure to the "
                         "application. This is a CREDENTIAL configuration gap, NOT an "
                         "application failure."),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }

        if not auth_config:
            raise HTTPException(status_code=422, detail={
                "error": "persona has no login recipe + credential card for this environment",
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": "store a recipe and the persona's card first — a run "
                          "never fabricates a session. This is NOT an application failure.",
            })
        # Reserve the persona so two runs never mutate the same member at once.
        # P5 ops: a per-tenant live-reservation CAP bounds concurrent persona runs
        # so one tenant cannot exhaust the runner fleet. Default 50, override with
        # NEXUS_PERSONA_RESERVATION_CAP.
        try:
            _resv_cap = int(os.getenv("NEXUS_PERSONA_RESERVATION_CAP", "50"))
        except ValueError:
            _resv_cap = 50
        async with tenant_scoped_session(tenant_id) as session:
            _live = await persona_store.count_live_reservations(session, tenant_id=tenant_id)
            await session.commit()
        if _resv_cap > 0 and _live >= _resv_cap:
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": "tenant_capacity",
                "persona_id": persona_id, "environment_id": environment_id,
                "detail": f"{_live} persona runs already in flight for this tenant "
                          f"(cap {_resv_cap}). Retry when one finishes.",
                "note": ("A per-tenant concurrency cap deferred this run so the "
                         "runner fleet stays fair. This is a capacity decision, "
                         "not an application failure."),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }
        async with tenant_scoped_session(tenant_id) as session:
            reservation_id = await persona_store.acquire_reservation(
                session, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id, run_id=run_id,
                ttl_seconds=int((body.retries or 0) + 1) * 1800)
            await session.commit()
        if not reservation_id:
            raise HTTPException(status_code=409, detail={
                "error": "persona is busy",
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": "another run currently holds this member. Retry shortly "
                          "or pick a different persona. This is NOT an application failure.",
            })
        # Persona preflight — prove the member can log in BEFORE the suite. A dead
        # test member BLOCKS with a test-data cause; the application is never
        # blamed for a rotten credential.
        try:
            pf = await _persona_preflight(
                request, artifact_id, tenant_id, cases, visits, edited_map,
                base_url or _recorded_origin(visits), auth_config, login_env, token)
        except Exception as exc:
            pf = {"ok": False, "detail": f"preflight error: {exc}"}
        # F3 — 'verified' MUST MEAN A REAL LOGIN.
        # Only a preflight that reached the recorded logged-in landing may stamp the
        # card. `pf['ok']` is merely "the attempt did not fail": the interpreter
        # prints 'recipe login OK' for steps that only replayed, and prints
        # 'form login OK' unconditionally after a click with no assertion at all — so
        # stamping on `ok` marked cards verified when nobody had logged in, and on a
        # permissive test app nothing would ever reveal it.
        #
        # A run that is allowed but unproven leaves the card's previous state alone
        # rather than overwriting it with a claim this run cannot support.
        #
        # A FAILURE is only recorded against the card when the card is what failed.
        # Recipe drift, an unrecorded interstitial and a runner outage are not the
        # credential's fault, and marking it 'failed' for them destroys a good proof
        # and sends the operator to re-type a password that was never wrong — the
        # precise misattribution login_probe exists to prevent.
        _blames_card = (pf.get("attribution") == "credential")
        if pf.get("proven") or (not pf.get("ok") and _blames_card):
            async with tenant_scoped_session(tenant_id) as session:
                await persona_store.stamp_card_verified(
                    session, tenant_id=tenant_id, persona_id=persona_id,
                    environment_id=environment_id, verified=bool(pf.get("proven")),
                    data_epoch=str(governance_env.get("data_epoch") or ""),
                    recipe_version=int((_auth.get("recipe") or {}).get("version") or 0))
                await session.commit()
        if not pf.get("ok"):
            async with tenant_scoped_session(tenant_id) as session:
                await persona_store.release_reservation(
                    session, tenant_id=tenant_id, run_id=run_id)
                await session.commit()
            # Attribute the block to what actually went wrong. Reporting an
            # unrecorded interstitial as a credential problem sends the operator to
            # re-type a password that was never wrong.
            _attr = pf.get("attribution") or "test_data"
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": ("credential" if _attr == "credential"
                                   else "test_data"),
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": pf.get("outcome") or "login_not_confirmed",
                "detail": pf.get("detail"),
                "note": pf.get("note") or (
                    "The test MEMBER could not log in — the run is BLOCKED, not "
                    "executed. The application under test is NOT implicated."),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }
    else:
        auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
    # Cardinality-driven repetition (P4/R4): a repeated block runs to THIS
    # persona's PROVEN count (5 beneficiaries vs 2). The plan rides NEXUS_REPETITION
    # into the run so a data-driven repeatable step reads its count from the member
    # instead of a hardcoded number; it is also surfaced in the report.
    repetition: dict[str, int] = {}
    if persona_id:
        async with tenant_scoped_session(tenant_id) as session:
            _counts = await persona_store.get_persona_cardinality(
                session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
        if _counts:
            _cd = [{"block": b, "count_b": n} for b, n in _counts.items()]
            _pc = {f"{persona_id}::{b}": n for b, n in _counts.items()}
            repetition = {p["block"]: p["repeat"]
                          for p in persona_governance.repetition_plan(_cd, _pc, persona_id)}
    # MEMBER DATA — run the suite as the member who is actually running.
    # A crawl-generated case carries the crawl member's values as literals; replayed
    # as someone else those are the wrong person's data, and the compiled oracle
    # asserts them against the value it just typed, so the run goes green having
    # tested nobody. Redirect every PROVEN member-derived field to this member's own
    # answer, and BLOCK when an answer is not known — never fall back to the literal.
    # Default OFF: with the flag unset the data map is untouched and the compiled
    # output is byte-identical to before.
    _member_data_by_test = dict(body.data_by_test or {})
    if persona_id and _member_data_enabled():
        async with tenant_scoped_session(tenant_id) as session:
            _classifications = await persona_store.get_classifications(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
            _answers = await persona_store.get_expected_values(
                session, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id)
        _plan = member_data_resolver.plan_member_data(
            cases, classifications=_classifications, answers=_answers,
            data_key=compiler_data_key)
        if _plan["missing"]:
            async with tenant_scoped_session(tenant_id) as session:
                await persona_store.release_reservation(
                    session, tenant_id=tenant_id, run_id=run_id)
                await session.commit()
            _unknown = [{"value_key": m["value_key"], "field": m.get("label") or "",
                         "scenario_id": m.get("scenario_id"),
                         "step_number": m.get("step_number"),
                         "reason": m.get("reason") or "no_answer_for_this_member"}
                        for m in _plan["missing"][:50]]
            return {
                "run_id": run_id, "status": "blocked",
                "blocked_reason": "member_data",
                "persona_id": persona_id, "environment_id": environment_id,
                "missing_count": len(_plan["missing"]),
                "missing": _unknown,
                "note": ("This suite asserts values that belong to a MEMBER, and this "
                         "member has no recorded answer for them. The run is BLOCKED, "
                         "not executed — running would have asserted another member's "
                         "data and reported green. Record this member's answers, or "
                         "reclassify the values as shared. The application under test "
                         "is NOT implicated."),
                "scripts": 0, "excluded_quarantined": excluded_quarantined,
                "excluded_uncertified_exploratory": excluded_uncertified,
            }
        for _sid, _fields in (_plan["data_by_test"] or {}).items():
            _member_data_by_test[_sid] = {**(_member_data_by_test.get(_sid) or {}), **_fields}
        # A value the page must SHOW cannot be redirected through the data map —
        # the spec asserts it rather than reading it. Rewrite those assertions to
        # this member's truth on a COPY of the suite, so the recorded case is
        # untouched and the change lives only in this run.
        cases, _rewrites = member_data_resolver.apply_member_expectations(
            cases, classifications=_classifications, answers=_answers)
        if _rewrites:
            _logger.warning("member_data.expectations_rewritten persona=%s env=%s count=%d",
                            persona_id, environment_id, len(_rewrites))
    # The Nexus runner container is headless (Xvfb-free); honor browsers/workers/
    # retries but force headless regardless of the requested mode.
    files = _configured_files(
        cases, build_field_meta(visits), base_url, body.data,
        data_by_test=_member_data_by_test,
        browsers=body.browsers, headed=False,
        workers=body.workers, retries=body.retries,
        edited=edited_map, storage_state=storage_state,
        env_context=body.env_context, auth_config=auth_config,
    )
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics",
        "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id,
        "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url,
        # Multi-env: tag the run with its Environment Profile identity so cross-env
        # parity can group by it (else every run collapses to one env). Falls back to
        # the deploy label when no profile is bound.
        "NEXUS_ENV": _env_run_label(body.env_context),
        # AUTH PERMANENCE — form-login credentials (empty dict when no profile is
        # bound). Secrets ride the run env, never the bundle.
        **login_env,
        # Tier-T3 diagnostics: opt-in per run (the fixture is inert otherwise).
        **({"NEXUS_T3_DIAGNOSTICS": "1"} if body.diagnostics else {}),
        **({"NEXUS_T3_HTML": "1"} if (body.diagnostics and body.diagnostics_html) else {}),
        # Visual evidence (default ON) — same policy for one test or the suite.
        "NEXUS_VIDEO": "on" if body.video else "off",
        "NEXUS_SCREENSHOT": (
            body.screenshot_mode
            if (body.screenshots and body.screenshot_mode in _SCREENSHOT_MODES)
            else ("on" if body.screenshots else "off")),
        # Persona identity tag — the run knows AS WHOM it ran, for the report.
        **({"NEXUS_PERSONA": persona_id} if persona_id else {}),
        "NEXUS_IDENTITY": _identity_label(body, persona_id),
        # Environment posture (P4) — the run knows the governance floor it runs
        # under (read_write | read_only | no_submit); surfaced in the report.
        "NEXUS_POSTURE": posture,
        # Cardinality-driven repetition (R4) — {block: count} for this member, so a
        # data-driven repeatable step runs the RIGHT number of times.
        **({"NEXUS_REPETITION": json.dumps(repetition)} if repetition else {}),
    }
    await _register_job(run_id, {
        "run_id": run_id, "status": "running", "artifact_id": artifact_id,
        "tenant_id": tenant_id, "kind": "run", "persona_id": persona_id,
        "environment_id": environment_id,
        "target": base_url, "scripts": len(cases), "exit_code": None, "output": "",
        "steps_completed": 0, "total_tests": len(cases),
    })
    task = asyncio.create_task(_execute_run(run_id, files, env))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    if reservation_id:
        # Release the persona hold when the run task finishes — a member is never
        # wedged past its run (TTL is the backstop for a crashed process).
        def _release(_t, _rid=run_id, _tid=tenant_id):
            async def _do():
                try:
                    async with tenant_scoped_session(_tid) as s:
                        await persona_store.release_reservation(s, tenant_id=_tid, run_id=_rid)
                        await s.commit()
                except Exception as exc:
                    _logger.warning("persona.release_failed run=%s err=%s", _rid, str(exc)[:200])
            rt = asyncio.create_task(_do())
            _RUNNER_TASKS.add(rt)
            rt.add_done_callback(_RUNNER_TASKS.discard)
        task.add_done_callback(_release)
    return {"run_id": run_id, "status": "running", "scripts": len(cases),
            "target": base_url, "persona_id": persona_id,
            "environment_id": environment_id, "posture": posture,
            "out_of_scope": out_of_scope, "repetition_plan": repetition,
            "excluded_quarantined": excluded_quarantined,
            "excluded_uncertified_exploratory": excluded_uncertified}


@router.post("/api/v1/test-factory/{artifact_id}/playwright/run-live")
async def playwright_run_live(
    body: RunConfigRequest,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Run the configured suite HEADED on the Nexus runner under a virtual
    display, streamed to the portal via noVNC. Returns {run_id, live_url}
    immediately; poll /playwright/run/{run_id} for status. The headless /run path
    is unaffected. One live run at a time (runner busy lock); one browser/worker
    so a single display shows a deterministic browser."""
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
    if tcids:
        cases = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
    elif cats:
        cases = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
    if not cases:
        raise HTTPException(status_code=404, detail="no matching active test cases to run")

    # P0.3 — quarantine on the LIVE/HEADED path is SURFACED, not enforced: this
    # is the operator's deliberate diagnostic tool (a human choosing one case to
    # watch), so a quarantined case is flagged with its certification verdict but
    # still allowed to run — the operator is inspecting it ON PURPOSE. Quarantine
    # protects the CLIENT-facing headless verdict (playwright_run excludes there),
    # not the operator's ability to watch a product-side failure reproduce live.
    _exploratory_ids = {
        (getattr(c, "test_id", "") or "") for c in cases
        if str(getattr(c, "type", "") or "").lower() == "combination"
    }
    _exploratory_ids.discard("")
    async with tenant_scoped_session(tenant_id) as session:
        _quarantined = await product_quarantined_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        _ungated = await uncertified_exploratory_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            exploratory_ids=_exploratory_ids,
        )
    quarantine_warning = [
        {
            "test_id": tid,
            "cause": ((_quarantined.get(tid, {}).get("attribution") or {}).get("cause")),
            "category": ((_quarantined.get(tid, {}).get("attribution") or {}).get("category")),
        }
        for tid in ((getattr(c, "test_id", "") or "") for c in cases)
        if tid in _quarantined
    ] + [
        {"test_id": tid, "cause": reason, "category": "uncertified_exploratory"}
        for tid, reason in _ungated.items()
    ]

    # ── MEMBERS & ENVIRONMENTS APPLY HERE TOO (Record + Run, phase 3) ─────────
    # This path used to have NO concept of a member: it always authenticated with
    # the one stored form-login, whatever the "Run as" picker said. So the whole
    # Members & Environments surface had no relationship to the button an operator
    # actually presses to watch a run — and, worse, every governance control the
    # headless path enforces was simply absent here. Two run buttons with two
    # different sets of safety rules is how a decommissioned member or a broken
    # card slips through unnoticed.
    #
    # Same resolution, same refusals, same words as playwright_run.
    run_id = uuid.uuid4().hex
    persona_id = (body.persona_id or "").strip()
    environment_id = (body.environment_id or "").strip() or str(
        (body.env_context or {}).get("environment_id") or "").strip()

    def _blocked(reason_code: str, detail, note: str, blocked_reason: str) -> dict:
        return {
            "run_id": run_id, "status": "blocked", "blocked_reason": blocked_reason,
            "persona_id": persona_id, "environment_id": environment_id,
            "reason": reason_code, "detail": detail, "note": note,
            "scripts": 0, "live_url": "", "quarantine_warning": quarantine_warning,
        }

    governance_env = {}
    _live_posture = ""
    if environment_id:
        async with tenant_scoped_session(tenant_id) as session:
            governance_env = await persona_store.get_environment(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                environment_id=environment_id) or {}

    # F5 — a real, current member of THIS application.
    if persona_id:
        async with tenant_scoped_session(tenant_id) as session:
            _prow = await persona_store.get_persona(
                session, tenant_id=tenant_id, persona_id=persona_id)
        _ident = persona_identity.check_persona(
            persona_id=persona_id, persona=_prow, artifact_id=artifact_id)
        if not _ident["allowed"]:
            return _blocked(_ident["reason"], _ident["detail"],
                            _ident["detail"].get("note", ""), "member_identity")

    # F1 — the selected environment decides where the run goes.
    if _env_routing_enabled() and environment_id:
        _routed = environment_routing.resolve_destination(
            environment_id=environment_id, environment=governance_env or None,
            requested_base_url=(body.base_url or "").strip(),
            requested_env_context=body.env_context)
        if not _routed["allowed"]:
            return _blocked(_routed["reason"], _routed["detail"],
                            "The run was BLOCKED before dispatch because its destination "
                            "could not be established honestly. This is a configuration "
                            "conflict, NOT an application failure.", "environment")
        if _routed["source"] == "environment":
            body.base_url = _routed["base_url"]
            body.env_context = _routed["env_context"]

    # P4 — the environment's posture governs a watched run exactly as it governs a
    # headless one. Watching is not a licence to mutate a locked environment.
    if environment_id:
        _gate = persona_governance.gate_dispatch(
            governance_env, mutating_requested=bool(body.mutating))
        _live_posture = _gate["posture"]
        if not _gate["allowed"]:
            return _blocked("environment_policy", " ".join(_gate["reasons"]),
                            "The ENVIRONMENT policy refused this run. This is an "
                            "environment decision — the application under test is NOT "
                            "implicated.", _gate["attribution"])

    base_url = (body.base_url or "").strip()
    if body.env_context and body.env_context.get("base_url"):
        base_url = str(body.env_context["base_url"]).strip()  # env profile base_url wins (SSRF-guarded)
    # Same fallback as every other dispatch path — run-live was the one that lacked
    # it. The portal's "run this test live" button sends no base_url, so the run
    # started with NEXUS_BASE_URL="" and the headed browser had nowhere to navigate:
    # Playwright launched it, failed the first goto, and relaunched. The noVNC viewer
    # therefore showed a BLANK screen, and the response reported target "" — which is
    # the tell, and was ignored for two rounds of "the live view is blank".
    if not base_url:
        base_url = _recorded_origin(visits)
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    if persona_id:
        _auth = await _persona_auth_resolve(
            request, artifact_id, tenant_id, persona_id, environment_id,
            environment=governance_env)
        auth_config, login_env = _auth["auth_config"], _auth["login_env"]
        # F4 — a card that cannot perform the recorded login BLOCKS, here too. The
        # alternative is the compiled globalSetup skipping the login and the operator
        # WATCHING a suite run logged out, with every failure attributed to the app.
        _state = _auth.get("state") or {}
        if _card_gate_enabled() and _state and not _state.get("runnable", True):
            return _blocked(_state.get("reason") or _state.get("state"),
                            _state.get("detail") or {},
                            "This member cannot perform the recorded login for this "
                            "environment, so the run is BLOCKED rather than watched "
                            "running logged out. This is a CREDENTIAL configuration "
                            "gap, NOT an application failure.", "credential")
        if not auth_config:
            raise HTTPException(status_code=422, detail={
                "error": "persona has no login recipe + credential card for this environment",
                "persona_id": persona_id, "environment_id": environment_id,
                "reason": "store a recipe and the persona's card first — a run never "
                          "fabricates a session. This is NOT an application failure.",
            })
    else:
        auth_config, login_env = await _run_form_login(request, artifact_id, tenant_id)
    files = _configured_files(
        cases, build_field_meta(visits), base_url, body.data,
        data_by_test=body.data_by_test,
        browsers=(body.browsers or ["chromium"])[:1],   # one project for one display
        headed=True,                                     # the live difference
        workers=1,                                       # serialize onto one screen
        retries=body.retries, edited=edited_map, storage_state=storage_state,
        env_context=body.env_context, auth_config=auth_config,
    )
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_DIAGNOSTICS_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/diagnostics", "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": _env_run_label(body.env_context),
        # A WATCHED run must be as attributable as a headless one. This path recorded
        # no identity at all, so the report's Trust Block read "default identity" for
        # every live run — including one deliberately run as a named member.
        **({"NEXUS_PERSONA": persona_id} if persona_id else {}),
        "NEXUS_IDENTITY": _identity_label(body, persona_id),
        **({"NEXUS_POSTURE": _live_posture} if _live_posture else {}),
        **login_env,
    }
    try:
        await runner_client.run_live(files, env)         # 202; raises on 409
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="a live run is already in progress")
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner unreachable: {exc}")
    await _register_job(run_id, {
        "run_id": run_id, "status": "running", "artifact_id": artifact_id,
        "tenant_id": tenant_id, "kind": "live",
        "target": base_url, "scripts": len(cases), "exit_code": None, "output": "",
        "steps_completed": 0, "total_tests": len(cases), "live": True,
    })
    task = asyncio.create_task(_poll_live(run_id))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    return {"run_id": run_id, "status": "running", "scripts": len(cases),
            "target": base_url, "live_url": _LIVE_PATH,
            "persona_id": persona_id, "environment_id": environment_id,
            "quarantine_warning": quarantine_warning}


@router.get("/api/v1/test-factory/{artifact_id}/playwright/run/{run_id}")
async def playwright_run_status(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Live status of a runner job (running -> passed/failed/timed_out/error).
    Transient — the durable record is the ingested run on the triage board."""
    job = _RUNNER_JOBS.get(run_id)
    if job is not None and job.get("artifact_id") == artifact_id:
        return {k: v for k, v in job.items() if k != "tenant_id"}
    # In-memory entry gone (platform-api restart, or the poll landed on another
    # worker) — fall back to the durable registry so the run/heal isn't orphaned.
    durable = await runner_jobs.get_job(
        tenant_id=user["tenant_id"], run_id=run_id, artifact_id=artifact_id,
    )
    if durable is not None:
        return {k: v for k, v in durable.items() if k != "tenant_id"}
    return {"run_id": run_id, "status": "unknown"}


class _ProgressBody(BaseModel):
    run_id: str
    artifact_id: str
    done: int = 0
    total: int = 0
    last_status: str = "running"


@router.post("/api/v1/test-runs/progress")
async def runner_progress(body: _ProgressBody, user: dict = Depends(get_current_user)):
    """Best-effort live-progress ping from the in-run reporter; updates only the
    transient in-memory job counter the UI polls. No DB, no durability."""
    job = _RUNNER_JOBS.get(body.run_id)
    if job and job.get("artifact_id") == body.artifact_id:
        job["steps_completed"] = int(body.done)
        job["total_tests"] = int(body.total) or job.get("total_tests") or job.get("scripts")
    return {"ok": True}


# ─── Authentication profile (capture-once login → encrypted session) ─────────
# Lets a generated test run from a COLD session: the operator logs in ONCE via an
# interactive headed capture (MFA included), we save the Playwright storageState,
# encrypt it at rest (EnvelopeService), and inject it into server runs as
# vkpower.auth.json. The session is NEVER returned to the client or stored plaintext.


class _AuthCaptureBody(BaseModel):
    base_url: str = Field("", max_length=2000)


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/capture")
async def start_auth_capture(
    body: _AuthCaptureBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Start an INTERACTIVE login capture: opens a headed browser on the runner at
    the (supplied or recorded) URL, streamed to the portal via the interactive
    noVNC path. The operator logs in once; then call /auth/save. Returns
    {live_url}. One capture/run at a time."""
    tenant_id = user["tenant_id"]
    url = (body.base_url or "").strip()
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        if not url:
            cases = await factory_service.load_active_production_cases(
                session, artifact_id=artifact_id,
            )
            url = (compile_manifest(cases).get("recorded_base_url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="no URL to open — supply base_url")
    try:
        res = await runner_client.auth_capture_start(url)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="runner busy — a run or capture is already in progress")
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner unreachable: {exc}")
    # The runner mints a per-capture one-time VNC password; the operator's noVNC
    # client needs it to connect (the interactive display is gated on it). The
    # password is hex (URL-safe) and dies with the capture (save/cancel/timeout).
    pw = (res or {}).get("vnc_password", "") if isinstance(res, dict) else ""
    live = _AUTH_LIVE_PATH + (f"&password={pw}" if pw else "")
    return {"status": "capturing", "live_url": live, "url": url}


class _SaveAndRunBody(RunConfigRequest):
    """A run request plus how to run it. Inherits every run option (test_ids,
    categories, retries, browsers, persona/environment) so Record + Run is the SAME
    run as any other — reporting, attribution, auto-heal and certification all fire
    on the ingest path exactly as they do today."""
    watch: bool = Field(True, description="true = headed live run you can watch; false = headless")
    # Set by THIS endpoint, never trusted from the caller — _identity_label only
    # honours the literal "recorded_session" and derives everything else.
    identity: str = Field("", description="server-set; do not supply")


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/save-and-run")
async def save_auth_capture_and_run(
    body: _SaveAndRunBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """RECORD + RUN — store the login just recorded, then immediately run with it.

    The point of doing this in ONE call is that it cannot half-happen. Chained by
    hand, the middle step can appear to succeed while producing nothing usable: the
    recorder always returns a storage state, so an operator who opened the browser
    and closed it still gets a stored "session" that is an empty cookie jar. Running
    with it executes the whole suite LOGGED OUT, every step fails, and the report
    attributes those failures to the application under test.

    So: save, then judge what was saved, and only dispatch when it can actually
    authenticate. A refusal here has run NOTHING and says why.

    Returns the run's own response on success, so the caller drives it exactly like
    any other run.
    """
    saved = await save_auth_capture(request=request, artifact_id=artifact_id, user=user)
    verdict = (saved or {}).get("session") or {}
    if not verdict.get("usable"):
        raise HTTPException(status_code=422, detail={
            "error": "the recorded login did not produce a usable session",
            "reason": verdict.get("reason") or "no_session",
            "cookies": verdict.get("cookies", 0),
            "storage_keys": verdict.get("storage_keys", 0),
            "note": verdict.get("note", ""),
            "recipe": (saved or {}).get("recipe"),
            "ran": False,
        })

    # The run is authenticating with the login just recorded — say so, so the report
    # can distinguish it from the artifact's long-stored session.
    body.identity = "recorded_session"
    run = (await playwright_run_live(body=body, request=request, artifact_id=artifact_id,
                                     user=user)
           if body.watch else
           await playwright_run(body=body, request=request, artifact_id=artifact_id,
                                user=user))
    return {**(run or {}), "recorded_login": True,
            "session": verdict, "recipe": (saved or {}).get("recipe")}


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/save")
async def save_auth_capture(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Pull the captured storageState from the runner, ENCRYPT it, and store it as
    this artifact's auth profile. Never returns the session. Refuses (503) if
    encryption is unavailable rather than store a session in plaintext.

    ALSO derives a reusable LOGIN RECIPE from how the operator actually logged in
    (RECORD ONCE): the runner observed which fields were filled and which controls
    were pressed — identifiers only, never a credential value — so the same login
    replays for any member with their own card. The session is one member; the
    recipe is all of them. Recipe derivation is BEST-EFFORT and strictly
    subordinate: any failure there is reported, never fatal, and never costs the
    operator the session they just captured."""
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        try:
            await runner_client.auth_capture_cancel()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="encryption unavailable — cannot store a session securely")
    try:
        res = await runner_client.auth_capture_save()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    state = res.get("storage_state")
    if not state:
        raise HTTPException(status_code=400, detail="no captured session — start a capture and log in first")
    state_json = json.dumps(state)
    cookie_n = len((state or {}).get("cookies", []) if isinstance(state, dict) else [])
    # Storing ALWAYS succeeds — the recorder returns a state even when the operator
    # never completed the login. Say plainly whether what we just stored can actually
    # authenticate, so a caller chaining a run onto this can refuse rather than run
    # the whole suite logged out and blame the application for every failure.
    session_verdict = session_handoff.assess(state)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        try:
            await auth_profiles.save_profile(
                session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
                storage_state_json=state_json, label=f"captured session ({cookie_n} cookie(s))",
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        recipe_info = await _derive_recipe_from_capture(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            snapshot=res.get("login_observation"))
        await session.commit()
    return {"status": "saved", "recipe": recipe_info, "session": session_verdict}


async def _derive_recipe_from_capture(session, *, tenant_id: str, artifact_id: str,
                                      snapshot: dict | None) -> dict:
    """Turn a recorded login into a persisted, replayable recipe.

    Returns a non-secret summary for the UI: ``{recorded, reason, slots,
    step_count, login_type_key, recipe_id, version}``. NEVER raises — the caller
    has already stored the operator's session and must not lose it because the
    choreography could not be read."""
    if not snapshot:
        return {"recorded": False, "reason": "no_observation_from_runner"}
    try:
        observation = login_observation.observation_from_events(snapshot)
        if observation.get("unusable"):
            return {"recorded": False,
                    "reason": observation.get("reason") or "unusable_observation"}
        derived = login_recorder.recipe_from_observed_login(observation)
        steps, slots = derived.get("steps") or [], derived.get("slots") or []
        if not slots:
            return {"recorded": False, "reason": "no_credential_fields_observed"}
        # Captures are re-run routinely (an expired cookie), and save_recipe is NOT
        # idempotent — it mints max(version)+1 and supersedes the prior active row,
        # clearing the verified_at that certification reads. So an UNCHANGED
        # choreography must not mint a version: re-capturing a session is not a
        # change to how the app is logged into.
        current = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        if current and (current.get("steps") or []) == steps:
            return {"recorded": False, "reason": "unchanged",
                    "slots": [s.get("name") for s in slots],
                    "recipe_id": current.get("recipe_id", ""),
                    "version": current.get("version", 0)}
        stored = await persona_store.save_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            steps=steps, slots=slots, source="login_recording",
            login_type_key=derived.get("login_type_key") or "")
        # F4: a re-record can rename the slots the cards were provisioned for.
        _impact = await persona_store.withdraw_card_proofs(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            required_slots=card_contract.required_slots(
                {"steps": steps, "slots": slots}))
        return {
            "recorded": True,
            "reason": observation.get("reason") or "",
            "slots": [s.get("name") for s in slots],
            "cards_broken": _impact["broken"],
            "cards_unproven": _impact["unproven"],
            # Shown back to the operator: the login page we will replay, and the
            # landing we will assert as "logged in". If they browsed on before
            # saving, the Home here is where they wandered — visible, not silent.
            "login_path": observation.get("login_path") or "",
            "home_path": (observation.get("home") or {}).get("url_pattern") or "",
            "step_count": stored.get("step_count", len(steps)),
            "login_type_key": stored.get("login_type_key", ""),
            "recipe_id": stored.get("recipe_id", ""),
            "version": stored.get("version", 0),
            "truncated": bool((snapshot or {}).get("truncated")),
        }
    except Exception as exc:   # noqa: BLE001 — the session must survive any failure
        _logger.warning("auth_capture.recipe_derivation_failed artifact=%s err=%s",
                        artifact_id, exc)
        return {"recorded": False, "reason": "derivation_failed"}


class _AuthImportBody(BaseModel):
    """Crawler-relayed Playwright ``storageState`` (cookies + origins) to store
    as this artifact's encrypted auth profile. The label is a non-secret note."""

    storage_state: dict = Field(..., description="Playwright storageState JSON")
    label: str | None = Field(None, max_length=200)


class _FormLoginBody(BaseModel):
    """AUTH PERMANENCE — a form-login profile so server-side runs log in FRESH
    each time (self-renewing auth) instead of riding an expiring captured cookie.
    Credentials are ENCRYPTED at rest (same envelope as the session profile) and
    are NEVER written into a bundle — the runner reads them from the run env."""

    user: str = Field(..., min_length=1, max_length=300, description="login username/email")
    password: str = Field(..., min_length=1, max_length=500, description="login password")
    login_path: str = Field("/", max_length=500, description="path to the login form")
    submit_label: str = Field("Sign in", max_length=120, description="submit button accessible name")
    fields: list[dict] | None = Field(
        None, description="[{label, value}] — value 'user'/'password' maps to the creds")
    label: str | None = Field(None, max_length=200, description="non-secret note")


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/form-login")
async def set_form_login(
    body: _FormLoginBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Store a form-login profile (credentials + form shape) so every server-side
    run of this artifact self-authenticates via the runner's globalSetup — the
    green is reproducible without re-importing an expiring session. Same gates as
    /auth/import: OFF unless NEXUS_QEC_AUTH_IMPORT_ENABLED, admin|manager only,
    503 if encryption is unavailable. Secrets are envelope-encrypted, never
    returned."""
    if os.getenv("NEXUS_QEC_AUTH_IMPORT_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="Auth import is disabled for this deployment.")
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Form-login setup requires an admin or manager role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(status_code=503, detail="encryption unavailable — cannot store credentials securely")
    config = {
        "user": body.user, "password": body.password,
        "login_path": body.login_path, "submit_label": body.submit_label,
        "fields": body.fields or [
            {"label": "Email", "value": "user"},
            {"label": "Password", "value": "password"},
        ],
    }
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        try:
            await auth_profiles.save_form_login(
                session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
                config=config, label=(body.label or f"form-login ({body.user})"),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        await session.commit()
    # R2: configuring auth YIELDS a baseline login recipe (no hand-authoring) so a
    # named persona fills the same choreography with its own card. Idempotent +
    # best-effort — a hiccup never fails the auth save.
    baseline = {"materialized": False, "reason": "skipped"}
    try:
        async with tenant_scoped_session(tenant_id) as session:
            baseline = await persona_store.ensure_baseline_from_form_login(
                session, tenant_id=tenant_id, artifact_id=artifact_id, form_login_cfg=config)
            await session.commit()
    except Exception as exc:
        _logger.warning("ensure_baseline_failed artifact=%s err=%s", artifact_id, str(exc)[:200])
    return {"status": "saved", "strategy": "form", "login_path": body.login_path,
            "user_masked": (body.user[:2] + "***") if body.user else "",
            "baseline_recipe": baseline}


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/import")
async def import_auth_profile(
    body: _AuthImportBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Import an externally captured storageState as this artifact's auth
    profile (QE-Central extension E3).

    Same guarantees as /auth/save — the session is ENCRYPTED at rest via the
    EnvelopeService (AAD=artifact_id), never stored plaintext, never returned —
    but the state arrives in the request body (relayed in-memory from the
    contained explorer) instead of being pulled from the interactive runner
    capture. OFF by default: 403 unless NEXUS_QEC_AUTH_IMPORT_ENABLED is
    truthy, so every existing deployment is byte-identical. Requires an
    admin|manager role (router RBAC gate + explicit check). Refuses (503) if
    encryption is unavailable; 422 on an empty/oversize session."""
    if os.getenv("NEXUS_QEC_AUTH_IMPORT_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="Auth import is disabled (QE-Central session handoff is not enabled for this deployment).")
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Auth import requires an admin or manager role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(status_code=503, detail="encryption unavailable — cannot store a session securely")
    state = body.storage_state
    if not state:
        raise HTTPException(status_code=422, detail="empty session")
    state_json = json.dumps(state)
    cookie_n = len(state.get("cookies", []) if isinstance(state, dict) else [])
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        try:
            await auth_profiles.save_profile(
                session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
                storage_state_json=state_json,
                label=(body.label or f"imported session ({cookie_n} cookie(s))"),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        await session.commit()
    return {"status": "saved"}


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/cancel")
async def cancel_auth_capture(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Cancel an in-progress capture (closes the runner browser)."""
    try:
        await runner_client.auth_capture_cancel()
    except Exception:
        pass
    return {"status": "cancelled"}


@router.get("/api/v1/test-factory/{artifact_id}/playwright/auth")
async def auth_status(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Auth status: is a session stored for this artifact, and is a capture running.
    Never returns the session itself."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        profile = await auth_profiles.get_status(session, tenant_id=tenant_id, artifact_id=artifact_id)
    try:
        capturing = bool((await runner_client.auth_capture_status()).get("active"))
    except Exception:
        capturing = False
    encryption_available = getattr(request.app.state, "envelope_service", None) is not None
    return {
        "artifact_id": artifact_id, "profile": profile,
        "capturing": capturing, "encryption_available": encryption_available,
    }


@router.delete("/api/v1/test-factory/{artifact_id}/playwright/auth")
async def clear_auth_profile(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Delete the stored auth profile — runs revert to unauthenticated."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        removed = await auth_profiles.clear_profile(session, tenant_id=tenant_id, artifact_id=artifact_id)
        await session.commit()
    return {"status": "cleared", "removed": removed}


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/versions")
async def list_versions(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Version history for one test, newest first. Empty = never edited."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        versions = await script_versions.list_script_versions(
            session, artifact_id=artifact_id, test_case_id=test_id,
        )
    return {"artifact_id": artifact_id, "test_id": test_id, "versions": versions}


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/versions/{version_no}/approve")
async def approve_script_version(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    version_no: int = PathParam(..., ge=1, le=100000),
    user: dict = Depends(get_current_user),
):
    """Approve a PROPOSED (auto-healed / TrueFix) version → it becomes the active
    source for runs. The human gate: a machine-written fix is never silently
    activated. Idempotent. Returns the approved version's metadata."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        row = await script_versions.approve_version(
            session, artifact_id=artifact_id, test_case_id=test_id, version_no=version_no,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")
        # Flywheel (default-OFF) — a human APPROVING a machine fix (already verified
        # green) is the strongest positive label. De-identified; self-gated.
        _note = (row.note or "").lower()
        await flywheel_ledger.record_label(
            session, tenant_id=tenant_id,
            decision_point=("reanchor" if "re-anchor" in _note or "renamed" in _note else (
                "control_kind_fix" if "control-kind" in _note else "heal_approve")),
            artifact_id=artifact_id, test_case_id=test_id, scenario_id=test_id,
            human_decision_enum="approved", verified_green=True,
            engine_verdict_enum="heal_proposed",
            git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
        )
        await session.commit()
    return {
        "artifact_id": artifact_id, "test_id": test_id, "version_no": version_no,
        "approved": True, "author": row.author, "note": row.note,
    }


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/source")
async def script_source(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    version_no: int | None = Query(default=None, ge=1),
    user: dict = Depends(get_current_user),
):
    """Editor seed: the edited (active, or a specific) source+data if present,
    else the deterministic PARAMETRIZED compiled source for this test (the exact
    spec a run uses), so edits stay env/data-driven. `edited` says which it is."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        if version_no is not None:
            ver = await script_versions.get_version(
                session, artifact_id=artifact_id, test_case_id=test_id, version_no=version_no,
            )
            if ver is None:
                raise HTTPException(status_code=404, detail="version not found")
            return {"edited": True, "version_no": ver.version_no,
                    "spec_path": ver.spec_path, "script_source": ver.script_source,
                    "data": ver.data_json}
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id,
        )
        if active is not None:
            return {"edited": True, "version_no": active.version_no,
                    "spec_path": active.spec_path, "script_source": active.script_source,
                    "data": active.data_json}
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
    field_meta = build_field_meta(visits)
    id_to_path = {s["test_id"]: s["path"] for s in compile_manifest(cases, field_meta).get("scripts", [])}
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None or test_id not in id_to_path:
        raise HTTPException(status_code=404, detail="test not found in compiled suite")
    return {"edited": False, "version_no": 0,
            "spec_path": id_to_path[test_id],
            "script_source": compile_case(tc, field_meta, parametrize=True),
            "data": {}}


@router.post("/api/v1/test-factory/{artifact_id}/scripts/save")
async def save_version(
    body: SaveVersionRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Save edits as a NEW immutable version (vN+1). Runs then use this source."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        field_meta = build_field_meta(visits)
        id_to_path = {
            s["test_id"]: s["path"]
            for s in compile_manifest(cases, field_meta).get("scripts", [])
        }
        if body.test_id not in id_to_path:
            raise HTTPException(status_code=404, detail="test not found in compiled suite")
        # #4 never-green-wash: a manual save MUST NOT silently weaken the grounded
        # oracle and become the active (running) version with no check — the SAME
        # assertion-immutability guard the heal path is forced through. If the saved
        # source drops grounded assertions below the compiled baseline, save it as a
        # PROPOSAL (requires approval) rather than active, and flag it.
        oracle_weakened = False
        weaken_reason = ""
        _tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == body.test_id), None)
        if _tc is not None:
            try:
                _baseline = compile_case(_tc, field_meta, parametrize=True)
                _ok, weaken_reason = self_heal.assert_assertions_unchanged(
                    _baseline, body.script_source or "")
                oracle_weakened = not _ok
            except Exception:
                oracle_weakened = False  # fail-open: a check error never blocks a save
        row = await script_versions.save_new_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=str(user.get("session_id", "") or ""),
            test_case_id=body.test_id, spec_path=id_to_path[body.test_id],
            script_source=body.script_source, data_json=dict(body.data or {}),
            author=str(user.get("email") or user.get("user_id") or ""),
            note=(body.note + (f" [oracle_weakened — saved as proposal: {weaken_reason}]"
                               if oracle_weakened else "")),
            proposed=oracle_weakened,
        )
        # Flywheel (default-OFF) — a human hand-edit of the generated script is a
        # recording->test correction. De-identified: only the fact, never the source.
        await flywheel_ledger.record_label(
            session, tenant_id=tenant_id, decision_point="script_edit",
            artifact_id=artifact_id, test_case_id=body.test_id, scenario_id=body.test_id,
            human_decision_enum="edited", git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
        )
        result = {
            "script_version_id": row.script_version_id,
            "version_no": row.version_no, "spec_path": row.spec_path,
            "oracle_weakened": oracle_weakened,
            "proposed": oracle_weakened,
            "weaken_reason": weaken_reason,
        }
        await session.commit()
    return result


@router.post("/api/v1/test-factory/{artifact_id}/scripts/restore")
async def restore_version(
    body: RestoreVersionRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Restore vN by APPENDING its content as a new highest version (immutable)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        src = await script_versions.get_version(
            session, artifact_id=artifact_id, test_case_id=body.test_id, version_no=body.version_no,
        )
        if src is None:
            raise HTTPException(status_code=404, detail="version not found")
        row = await script_versions.save_new_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=str(user.get("session_id", "") or ""),
            test_case_id=body.test_id, spec_path=src.spec_path,
            script_source=src.script_source, data_json=dict(src.data_json or {}),
            author=str(user.get("email") or user.get("user_id") or ""),
            note=f"restore of v{src.version_no}",
        )
        result = {
            "script_version_id": row.script_version_id,
            "version_no": row.version_no, "restored_from": body.version_no,
        }
        await session.commit()
    return result


@router.get("/api/v1/test-factory/{artifact_id}/runs/summary")
async def runs_summary(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    window: int = Query(10, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    """Per-script run-history sparkline (last N run statuses + durations) + flake
    fingerprint, plus a board-level Run Summary. Read-only, $0 LLM — surfaces the
    existing test_runs engine + run/step tables (no new computation, no migration)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        per_sid = await last_run_summary_by_scenario(
            session, artifact_id=artifact_id, tenant_id=tenant_id, flake_window_runs=window,
        )
        rows = (await session.execute(
            select(
                E2ETestRunStepRow.scenario_id, E2ETestRunStepRow.run_id,
                E2ETestRunStepRow.status, E2ETestRunRow.started_at, E2ETestRunRow.duration_ms,
            )
            .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
            .where(
                E2ETestRunStepRow.artifact_id == artifact_id,
                E2ETestRunStepRow.tenant_id == tenant_id,
                E2ETestRunStepRow.scenario_id != "",
            )
            .order_by(desc(E2ETestRunRow.started_at))
        )).all()
        latest = (await session.execute(
            select(E2ETestRunRow)
            .where(E2ETestRunRow.artifact_id == artifact_id, E2ETestRunRow.tenant_id == tenant_id)
            .order_by(desc(E2ETestRunRow.started_at)).limit(1)
        )).scalars().first()
        board = {
            "passed": 0, "failed": 0, "skipped": 0, "flaky": 0,
            "duration_ms": 0, "total_scripts": 0, "last_run_at": None, "status": "",
        }
        if latest is not None:
            board.update(
                passed=latest.passed_steps, failed=latest.failed_steps,
                skipped=latest.skipped_steps, duration_ms=latest.duration_ms,
                status=latest.status,
                last_run_at=(latest.started_at.isoformat() if latest.started_at else None),
            )

    # Worst step-status per (scenario, run); newest first, capped to the window.
    spark: dict = {}
    for sid, run_id, st, started_at, dur in rows:
        per_run = spark.setdefault(sid, {})
        cur = per_run.get(run_id)
        if cur is None:
            per_run[run_id] = {"status": st, "started_at": started_at, "duration_ms": dur}
        elif _status_severity(st) > _status_severity(cur["status"]):
            cur["status"] = st

    scripts: dict = {}
    for sid, runs in spark.items():
        ordered = sorted(runs.values(), key=lambda r: r["started_at"], reverse=True)[:window]
        s = per_sid.get(sid, {})
        scripts[sid] = {
            "runs": [
                {"status": r["status"], "duration_ms": r["duration_ms"],
                 "at": (r["started_at"].isoformat() if r["started_at"] else None)}
                for r in ordered
            ],
            "flake_rate_pct": s.get("flake_rate_pct", 0.0),
            "is_flaky": bool(s.get("is_flaky")),
            "consecutive_failures": s.get("consecutive_failures", 0),
            "last_run_status": s.get("last_run_status", ""),
            "last_run_at": s.get("last_run_at"),
            # F4 — failure attribution: when the latest failure is PROVABLY a
            # generated-oracle defect, the UI must say "product-side", never
            # painting the client's application red for our oracle.
            "failure_attribution": s.get("failure_attribution"),
            # P0.2 — soft-oracle misses on the latest client run (visible,
            # non-fatal best-effort hints under the proven-oracle policy).
            "soft_oracle_misses": s.get("soft_oracle_misses", 0),
            # P0.3 — latest certification-run state (kept OUT of the client
            # stats above; quarantine is derived server-side from it).
            "certification": s.get("certification"),
        }
    # P0.3 — per-script quarantine flag (server truth mirrored for the UI,
    # via the SAME pure rule the run-gate uses).
    for _sid, v in scripts.items():
        v["quarantined"] = quarantine_decision(v.get("certification"))
    board["total_scripts"] = len(scripts)
    board["flaky"] = sum(1 for v in scripts.values() if v["is_flaky"])
    board["quarantined"] = sum(1 for v in scripts.values() if v.get("quarantined"))
    return {"artifact_id": artifact_id, "board": board, "scripts": scripts}


@router.post("/api/v1/test-factory/{artifact_id}/certify")
async def certify_suite(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """P0.3 — re-trigger certification WITHOUT regenerating. The recovery path
    for a lost certification run (runner busy/restarted/timed out — job
    a66d0e69 was killed mid-flight and the suite silently stayed uncertified).
    Fire-and-forget: results land via the normal reporter → ingest path tagged
    environment='certification'; the run-gates read them automatically."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
    if not cases:
        raise HTTPException(status_code=404, detail="no active cases to certify")
    _spawn_certification(request, artifact_id, tenant_id)
    return {
        "certification": "dispatched",
        "cases": len(cases),
        "timeout_ms": _cert_timeout_ms(len(cases)),
        "attempts": _CERT_MAX_ATTEMPTS,
    }


@router.get("/api/v1/test-factory/{artifact_id}/quality/product-faults")
async def product_fault_metric(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    window_days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
):
    """P2.8 — the north-star quality metric: CLIENT-VISIBLE product-fault
    failures (target: zero). Counts every failed step on a NON-certification
    run whose attribution is product-side, against the certification catches
    (product faults intercepted BEFORE a client run). Read-only, $0 LLM,
    deterministic over the ingested run/step tables.
    """
    from datetime import datetime, timedelta, timezone

    tenant_id = user["tenant_id"]
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        rows = (await session.execute(
            select(
                E2ETestRunStepRow.scenario_id,
                E2ETestRunStepRow.step_number,
                E2ETestRunStepRow.metadata_json,
                E2ETestRunRow.environment,
                E2ETestRunRow.started_at,
                E2ETestRunRow.run_id,
            )
            .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
            .where(
                E2ETestRunStepRow.artifact_id == artifact_id,
                E2ETestRunStepRow.tenant_id == tenant_id,
                E2ETestRunRow.started_at >= since,
            )
            .order_by(desc(E2ETestRunRow.started_at))
        )).all()

    client_visible: list[dict] = []
    caught_in_certification = 0
    caught_in_diagnosis = 0
    by_cause: dict[str, int] = {}
    for sid, step_no, meta, env_name, started_at, run_id in rows:
        attr = (meta or {}).get("failure_attribution")
        if not attr:
            continue
        category = str(attr.get("category") or "")
        if category != "product_script_defect":
            continue
        env_l = str(env_name or "").strip().lower()
        by_cause[attr.get("cause") or "unspecified"] = (
            by_cause.get(attr.get("cause") or "unspecified", 0) + 1
        )
        if env_l == "certification":
            caught_in_certification += 1
        elif env_l == "diagnosis":
            # The auto-heal driver's own capture/verify instruments — the
            # product examining itself, never client-visible.
            caught_in_diagnosis += 1
        else:
            client_visible.append({
                "run_id": run_id,
                "scenario_id": sid,
                "step_number": step_no,
                "cause": attr.get("cause"),
                "tier": attr.get("tier"),
                "at": started_at.isoformat() if started_at else None,
            })

    return {
        "artifact_id": artifact_id,
        "window_days": window_days,
        # THE metric — a client saw a run fail on OUR defect. Target: 0.
        "client_visible_product_faults": len(client_visible),
        # The gates working — product faults intercepted before any client run.
        "caught_in_certification": caught_in_certification,
        "caught_in_diagnosis": caught_in_diagnosis,
        "by_cause": by_cause,
        "recent_client_visible": client_visible[:25],
    }


async def _fidelity_inputs(session, artifact_id: str):
    cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
    visits, _actions = await factory_service._load_current_pages_and_actions(session, artifact_id=artifact_id)
    return cases, build_field_meta(visits)


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/fidelity")
async def script_fidelity(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    deep: int = Query(0, ge=0, le=1),
    user: dict = Depends(get_current_user),
):
    """Fidelity scorecard for ONE script: does the compiled Playwright faithfully
    implement the test case + verify its Expected Results? Deterministic ($0):
    coverage / assertions / drift. deep=1 adds a gpt-4o semantic review."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this script")
    report = tf_fidelity.compute_fidelity(
        tc, field_meta, active_source=(active.script_source if active else None))
    if deep:
        composer = getattr(request.app.state, "storyboard_composer", None)
        llm_router = getattr(composer, "_llm_router", None) if composer else None
        if llm_router is not None:
            spec = compile_case(tc, field_meta, parametrize=True)
            report["llm_review"] = await tf_fidelity.llm_faithfulness_review(
                tc, spec, report, router=llm_router)
    return report


@router.get("/api/v1/test-factory/{artifact_id}/fidelity")
async def suite_fidelity(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Suite-level fidelity: the deterministic scorecard for every active script +
    a rollup. $0 LLM (per-script deep review is on the individual endpoint)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        active_map = await _active_edited_map(session, artifact_id=artifact_id)
    scripts = [
        tf_fidelity.compute_fidelity(
            tc, field_meta,
            active_source=(active_map.get(getattr(tc, "test_id", "") or "") or {}).get("script_source"),
        )
        for tc in cases
    ]
    total = len(scripts)
    rollup = {
        "scripts": total,
        "strong": sum(1 for s in scripts if s["grade"] == "strong"),
        "partial": sum(1 for s in scripts if s["grade"] == "partial"),
        "weak": sum(1 for s in scripts if s["grade"] == "weak"),
        "drifted": sum(1 for s in scripts if s["drift"]),
        "avg_score": round(sum(s["score"] for s in scripts) / total) if total else 0,
    }
    return {"artifact_id": artifact_id, "rollup": rollup, "scripts": scripts}


async def _regenerate_one(session, *, artifact_id, tenant_id, tc, field_meta) -> dict:
    spec = compile_case(tc, field_meta, parametrize=True)
    tid = getattr(tc, "test_id", "") or ""
    id_to_path = {s["test_id"]: s["path"]
                  for s in compile_manifest([tc], field_meta).get("scripts", [])}
    row = await script_versions.save_new_version(
        session, artifact_id=artifact_id, tenant_id=tenant_id, session_id="",
        test_case_id=tid, spec_path=id_to_path.get(tid, ""), script_source=spec,
        data_json={}, author="nexus-regenerate",
        note="Regenerated from the current test case (Pages & Forms + Expected Results)")
    return {"test_id": tid, "version_no": row.version_no, "spec_path": id_to_path.get(tid, "")}


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/regenerate")
async def regenerate_script(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Regenerate ONE script from its current test case and save it as a new
    immutable version (v+1) — reversible via /restore. Owns the compiled spec so
    later edits start from the freshest grounded output."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
        if tc is None:
            raise HTTPException(status_code=404, detail="no active test case for this script")
        out = await _regenerate_one(session, artifact_id=artifact_id, tenant_id=tenant_id,
                                    tc=tc, field_meta=field_meta)
        await session.commit()
    return out


@router.post("/api/v1/test-factory/{artifact_id}/scripts/regenerate-all")
async def regenerate_all(
    body: RunConfigRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Regenerate a GROUP of scripts (selected test_ids/categories, or all) to new
    versions in one pass. Reuses the same per-script regenerate."""
    tenant_id = user["tenant_id"]
    tcids = {t.strip() for t in (body.test_ids or []) if t and t.strip()}
    cats = {c.strip().lower() for c in (body.categories or []) if c and c.strip()}
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        if tcids:
            cases = [c for c in cases if (getattr(c, "test_id", "") or "") in tcids]
        elif cats:
            cases = [c for c in cases if (getattr(c, "type", "") or "").lower() in cats]
        results = [
            await _regenerate_one(session, artifact_id=artifact_id, tenant_id=tenant_id,
                                  tc=tc, field_meta=field_meta)
            for tc in cases
        ]
        await session.commit()
    return {"artifact_id": artifact_id, "regenerated": len(results), "versions": results}


@router.get("/api/v1/test-factory/{artifact_id}/runs/latest")
async def latest_run_timeline(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """THIS RUN: per-scenario, per-step pass/fail timeline for the single most-
    recent run. Header counts come straight off the run row, so they always agree
    with the per-step rollup below — no cross-run mixing (that accumulation lives
    in the History view / triage board). Read-only, $0 LLM, no migration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        timeline = await build_latest_run_timeline(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        # SENTINEL (R5 wiring — was built, never called): auto-diagnose every
        # scenario's first failure and attach the unified diagnosis (cause +
        # PRODUCT/SCRIPT/ENVIRONMENT triage verdict + provenance) to the
        # timeline. $0 deterministic, Governor-gated, additive, fail-open.
        try:
            from ..services.agentic import auto_diagnosis as _sentinel
            _diags = await _sentinel.diagnose_failures(
                session, artifact_id=artifact_id, tenant_id=tenant_id,
                scenario_ids=None, timeline=timeline,
            )
            timeline = _sentinel.attach_to_timeline(timeline, _diags)
        except Exception:
            pass  # rendering aid only — the honest timeline stands on its own
        return timeline


@router.get("/api/v1/test-factory/{artifact_id}/runs")
async def list_runs_for_artifact(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    limit: int = 25,
    user: dict = Depends(get_current_user),
):
    """Recent run headers (newest first) for the run picker, so any prior run can
    be re-inspected — not just the latest. Read-only, $0 LLM, no migration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return {
            "artifact_id": artifact_id,
            "runs": await recent_runs(
                session, artifact_id=artifact_id, tenant_id=tenant_id, limit=limit,
            ),
        }


@router.get("/api/v1/test-factory/{artifact_id}/runs/{run_id}")
async def run_timeline_by_id(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Per-scenario, per-step timeline for ONE historical run (same shape as
    /runs/latest). Declared AFTER /runs/latest and /runs/summary so those literal
    paths win; this matches any other run_id. Read-only, $0 LLM, no migration."""
    if run_id in ("latest", "summary"):  # defensive — literals are matched above
        raise HTTPException(status_code=404, detail="not a run id")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        timeline = await build_run_timeline_by_id(
            session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=run_id,
        )
        # SENTINEL: same additive auto-diagnosis as /runs/latest (fail-open).
        try:
            from ..services.agentic import auto_diagnosis as _sentinel
            _diags = await _sentinel.diagnose_failures(
                session, artifact_id=artifact_id, tenant_id=tenant_id,
                scenario_ids=None, timeline=timeline,
            )
            timeline = _sentinel.attach_to_timeline(timeline, _diags)
        except Exception:
            pass
        return timeline


@router.get("/api/v1/test-factory/{artifact_id}/proven-controls")
async def get_proven_controls(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    include_invalidated: bool = False,
    user: dict = Depends(get_current_user),
):
    """R6 — the LEARNED-CAPABILITY ledger, visible for the first time
    (requirements-audit finding: list_proven_controls had no route or UI, so
    accumulated learning was invisible and unauditable). Every oracle-proven
    heal memoized for this artifact: fix kind, payload, provenance
    (proven_by_run), confirmed_count (rising trust), stale/quarantine
    lifecycle. Read-only, $0 LLM, no migration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        from ..services.diff_and_heal import control_ledger as _ledger
        entries = await _ledger.list_proven_controls(
            session, tenant_id=tenant_id, app_key=artifact_id,
            include_invalidated=include_invalidated,
        )
        return {
            "artifact_id": artifact_id,
            "count": len(entries),
            "entries": entries,
            "note": ("Each entry is an oracle-PROVEN heal reused across runs; "
                     "2 consecutive misfires quarantine it, a green re-prove "
                     "reactivates it — learning with an audit trail, never "
                     "silent memory."),
        }


@router.get("/api/v1/test-factory/{artifact_id}/runs/{run_id}/recovery-scan")
async def recovery_scan(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Recovery Agent v1 (R5) — PROPOSE-ONLY scan of one run: every failing
    scenario classified into the 9-class outcome taxonomy; capability-gap
    findings become human-gated PROPOSAL BUNDLES (diagnosis + failing-repro
    pointer + grounded strategy suggestion); application defects surface their
    auto-authored reports. The agent never applies anything — every proposal
    requires explicit human approval. Read-only, $0 LLM, no migration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        timeline = await build_run_timeline_by_id(
            session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=run_id,
        )
        from ..services.agentic import auto_diagnosis as _sentinel
        from ..services.agentic import recovery_agent as _recovery
        from ..services.agentic import recovery_store as _store
        try:
            diags = await _sentinel.diagnose_failures(
                session, artifact_id=artifact_id, tenant_id=tenant_id,
                scenario_ids=None, timeline=timeline,
            )
        except Exception:
            diags = {}
        scan = _recovery.scan(timeline, diags)
        out = _recovery.scan_to_dict(scan)
        # R5 v2: PERSIST the capability-gap proposals (human-gated) + auto-RESOLVE
        # any approved proposal whose repro scenario now passes. Additive, fail-open.
        try:
            passing = {sc.get("scenario_id") for sc in (timeline.get("scenarios") or [])
                       if all(st.get("status") == "passed" for st in (sc.get("steps") or []))
                       and sc.get("scenario_id")}
            await _store.resolve_if_passing(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                passing_scenario_ids=passing)
            out["persisted"] = await _store.persist_scan(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                run_id=scan.run_id, proposals=scan.proposals)
        except Exception:
            out["persisted"] = 0
        return out


@router.get("/api/v1/test-factory/promotion-candidates")
async def promotion_candidates(
    min_apps: int = 3,
    user: dict = Depends(get_current_user),
):
    """R6 — mine the tenant's proven-control ledger for HEALS that recur across
    distinct apps: each becomes a human-gated candidate to graduate into a
    PERMANENT capability (a UACR recipe benefiting every future client). Pure,
    read-only, $0 LLM — a maintainer reviews and lands the recipe + its
    regression test; the agent never self-modifies the product."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        from ..services.diff_and_heal import control_ledger as _ledger
        from ..services.agentic import promotion_miner as _miner
        # All proven controls for the tenant (app-fingerprint scope = cross-recording).
        entries = await _ledger.list_proven_controls(
            session, tenant_id=tenant_id, include_invalidated=False)
        candidates = _miner.mine_to_dicts(entries, min_apps=max(2, int(min_apps)))
        return {"count": len(candidates), "min_apps": max(2, int(min_apps)),
                "candidates": candidates,
                "note": ("Continuous learning: a heal proven green across multiple "
                         "distinct apps is a signal to make it permanent so it "
                         "applies on the first pass. Human-gated — never auto-landed.")}


@router.get("/api/v1/test-factory/{artifact_id}/recovery-proposals")
async def list_recovery_proposals(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    status: str = "",
    user: dict = Depends(get_current_user),
):
    """R5 v2 — the persisted, human-gated proposal queue for an artifact
    (optionally filtered by status: proposed/approved/rejected/resolved).
    Read-only, $0 LLM."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        from ..services.agentic import recovery_store as _store
        entries = await _store.list_proposals(
            session, tenant_id=tenant_id, artifact_id=artifact_id, status=status.strip())
        return {"artifact_id": artifact_id, "count": len(entries), "proposals": entries,
                "note": ("Propose-only: an APPROVE records intent + attribution; the "
                         "agent never applies the fix. A green run of the repro case "
                         "resolves it.")}


@router.post("/api/v1/test-factory/{artifact_id}/recovery-proposals/{proposal_id}/decision")
async def decide_recovery_proposal(
    payload: dict,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    proposal_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """R5 v2 — record a human APPROVE / REJECT on a capability-gap proposal
    (attributed to the authenticated operator + timestamped). The agent applies
    nothing; this is the auditable human gate."""
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(403, "admin or manager role required to decide a proposal")
    tenant_id = user["tenant_id"]
    decision = str((payload or {}).get("decision") or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be 'approve' or 'reject'")
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        from ..services.agentic import recovery_store as _store
        who = str(user.get("sub") or user.get("email") or "").strip() or "unknown"
        updated = await _store.record_decision(
            session, tenant_id=tenant_id, proposal_id=proposal_id,
            decision=decision, decided_by=who,
            note=str((payload or {}).get("note") or ""))
        if updated is None:
            raise HTTPException(404, "proposal not found")
        return updated


@router.get("/api/v1/test-factory/{artifact_id}/steps/{scenario_id}/{step_number}/analyze")
async def analyze_failed_step(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    step_number: int = PathParam(..., ge=0, le=10000),
    user: dict = Depends(get_current_user),
):
    """Nexus TrueFix — grounded root-cause diagnosis of the latest run's failure
    of one step: names the most likely cause (wrong-control-kind / selector-drift
    / locator-not-found / timing / flake / needs-review) with confidence, grounded
    evidence, a recommended action, and a compiler-derived suggested fix. Read-only,
    $0 LLM, no migration. Apply + closed-loop verify is a later phase."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        diag = await self_heal.analyze_step(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            scenario_id=scenario_id, step_number=step_number,
        )
        # TRIAGE VERDICT (R5 wiring — was built, never exposed here): attach
        # the deterministic PRODUCT / SCRIPT / ENVIRONMENT source + fix/build/
        # flag route to the on-click diagnosis. $0, Governor-gated, additive,
        # fail-open — the grounded diagnosis stands on its own without it.
        try:
            from ..services.agentic import governor as _gov
            from ..services.agentic import triage as _triage
            if isinstance(diag, dict) and diag.get("found") is not False \
                    and _gov.agent_enabled("triage"):
                _t = _triage.triage(diag, error_message=str(diag.get("error_message") or ""))
                diag = {**diag, "triage": _t,
                        "source": _t["source"], "route": _t["route"]}
        except Exception:
            pass
        return diag


@router.get("/api/v1/test-factory/{artifact_id}/triage")
async def get_triage(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Grounded triage board: per-scenario verdict (real-regression / selector-drift
    / visual-change / flake / needs-review) joined to the captured baseline, plus
    the 'need you / don't need you' tally. Read-only, $0 LLM. Empty board when no
    runs have been ingested yet."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await assemble_triage(session, artifact_id=artifact_id, tenant_id=tenant_id)


@router.get("/api/v1/test-factory/{artifact_id}/oracle-scorecard")
async def get_oracle_scorecard(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Grounded Oracle, MEASURED — a per-artifact scorecard that makes the oracle
    visible: VERIFIED (positive proof: passed runs / failed toHaveURL outcome
    assertions) vs ASSUMED (inference: drift / flake / needs-review), a design-
    confidence rollup, and heal INTEGRITY (the engine never green-washes an
    unproven fix; plus an honestly-flagged approved-then-contradicted false-heal
    proxy). Read-only, $0 LLM, no migration. Empty/insufficient denominators are
    surfaced as such, never as a misleading 0."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await compute_artifact_scorecard(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )


@router.get("/api/v1/test-factory/{artifact_id}/env-parity")
async def get_env_parity(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Cross-environment PARITY (multi-env Phase 3) — run the SAME flow against
    dev/test/uat/prod and report where their GROUNDED outputs diverge ("prod premium
    $72 vs UAT $75"). Re-derives verdicts through the SAME frozen classify_failure
    reducer (no new verdict logic); reports a divergence ONLY from PROVEN-grounded
    values or differing verdict labels — anything unverified is 'incomparable', never
    a divergence and never a silent 'match'. Read-only, $0 LLM, no migration."""
    from ..services.env_parity import compute_env_parity

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await compute_env_parity(session, artifact_id=artifact_id, tenant_id=tenant_id)


@router.get("/api/v1/test-factory/{artifact_id}/rtm")
async def get_rtm(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Requirements Traceability Matrix (Phase 2 provenance) — per test, the chain
    requirement (the recorded human behavior) → step → emitted assertion, with each
    assertion's oracle_kind + a grounded flag, and an `unproven` honesty flag for
    steps with no grounded oracle. The audit artifact a regulated buyer asks for.
    Read-only, $0 LLM, no migration; assertion code is byte-identical to the
    compiled spec (anti-drift), so the RTM can never lie about what runs."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id,
        )
        field_meta = build_field_meta(visits)
    return {"artifact_id": artifact_id, "tests": [build_rtm(tc, field_meta) for tc in cases]}


async def _scenario_visual_inputs(
    request: Request, session, *, artifact_id: str, tenant_id: str,
    scenario_id: str, step_number: int,
):
    """(baseline_bytes|None, actual_bytes|None, expected_text) for a scenario's
    failing step — baseline = the RECORDED frame (eyes-engine), actual = the stored
    run screenshot. Best-effort: Nones when a source is unavailable so advisory
    consumers degrade gracefully. Never raises."""
    token = _bearer(request)
    baseline_path, expected = "", ""
    try:
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )
        tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == scenario_id), None)
        bstep = self_heal._baseline_step(tc, step_number) if tc is not None else None
        if bstep is not None:
            baseline_path = getattr(bstep, "screenshot", "") or ""
            expected = (getattr(bstep, "expected_result", "")
                        or getattr(bstep, "expected", "") or "")
    except Exception:
        pass
    actual_bytes = await fetch_latest_screenshot(
        session, tenant_id=tenant_id, artifact_id=artifact_id,
        scenario_id=scenario_id, step_number=step_number,
    )
    baseline_bytes = None
    if baseline_path:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                baseline_bytes = await _fetch_frame_bytes(client, baseline_path, auth_token=token)
        except Exception:
            baseline_bytes = None
    return baseline_bytes, actual_bytes, expected


@router.get("/api/v1/test-factory/{artifact_id}/scenarios/{scenario_id}/visual-diff")
async def scenario_visual_diff(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    step_number: int = Query(default=0, ge=0, le=10000),
    user: dict = Depends(get_current_user),
):
    """ADVISORY perceptual diff (Phase 2): how much the failure-state screenshot
    differs from the recorded baseline frame — deterministic ($0, Pillow), an
    'X% changed' BADGE only. It NEVER feeds or flips a pass/fail verdict. Returns
    {available, changed_ratio, changed_bbox, identical}; available=false (with the
    missing source named) when the baseline frame or actual screenshot isn't there."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        baseline, actual, _ = await _scenario_visual_inputs(
            request, session, artifact_id=artifact_id, tenant_id=tenant_id,
            scenario_id=scenario_id, step_number=step_number,
        )
    if not baseline or not actual:
        missing = [s for s, b in (("baseline frame", baseline),
                                  ("actual screenshot", actual)) if not b]
        return {"available": False, "reason": "missing " + ", ".join(missing)}
    return {"available": True, **diff_screenshots(baseline, actual)}


@router.post("/api/v1/test-factory/{artifact_id}/scenarios/{scenario_id}/semantic-check")
async def scenario_semantic_check(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    step_number: int = Query(default=0, ge=0, le=10000),
    user: dict = Depends(get_current_user),
):
    """ADVISORY VLM semantic check (Phase 2): does the failure-state screen still
    SATISFY the RECORDED expected outcome? Gated — needs the vision router; meant
    for ONE call per human-escalated triage card (never a batch). Returns a
    semantic_match SIGNAL ONLY ('match'|'deviation'|'uncertain') — it NEVER calls
    the verdict reducer and NEVER changes a pass/fail label. router unavailable or
    missing images -> 'uncertain' (no-op)."""
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        baseline, actual, expected = await _scenario_visual_inputs(
            request, session, artifact_id=artifact_id, tenant_id=tenant_id,
            scenario_id=scenario_id, step_number=step_number,
        )
    return await judge_semantic_match(expected, baseline, actual, llm_router)


class TriageFeedbackRequest(BaseModel):
    # Constrained to the canonical verdict vocabulary as defense-in-depth; the
    # handler ALSO clamps server-side (so a non-validating client can't leak).
    verdict: str = Field("", max_length=24)        # the engine verdict being judged
    agrees: bool = True
    corrected_verdict: str = Field("", max_length=24)


@router.post("/api/v1/test-factory/{artifact_id}/triage/{scenario_id}/feedback")
async def triage_feedback(
    body: TriageFeedbackRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Human CONFIRMS or OVERRIDES the engine's grounded verdict — a direct oracle
    correction, the highest-value flywheel label (engine said X; human agreed / said
    Y). Records ONLY de-identified verdict ENUMS clamped to the canonical set, never
    raw text. Flywheel capture is gated default-OFF; additive + tenant-scoped, and
    best-effort so a pre-migration table / DB error can never break the user's
    feedback (authz 404 still propagates)."""
    tenant_id = user["tenant_id"]
    if not flywheel_ledger.capture_enabled():
        return {"recorded": False}
    # Clamp client-supplied verdict strings to the EXACT set the reducer emits —
    # unknown collapses to a fixed sentinel, so no raw/PII text reaches the ledger.
    ev = body.verdict if body.verdict in _KNOWN_VERDICTS else "unknown"
    cv = body.corrected_verdict if body.corrected_verdict in _KNOWN_VERDICTS else "unknown"
    try:
        async with tenant_scoped_session(tenant_id) as session:
            await _require_artifact(session, artifact_id, tenant_id)
            await flywheel_ledger.record_label(
                session, tenant_id=tenant_id, decision_point="triage_feedback",
                artifact_id=artifact_id, scenario_id=scenario_id,
                engine_verdict_enum=ev,
                human_decision_enum=("agreed" if body.agrees else f"overridden_to:{cv}"),
                git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
            )
    except HTTPException:
        raise  # authz / 404 stays a real error
    except Exception:
        return {"recorded": False}  # capture is best-effort — never 500 the user
    return {"recorded": True}


class ValueConflictResolveRequest(BaseModel):
    test_id: str = Field(..., min_length=1, max_length=64)
    choice: str = Field("", max_length=16)         # typed | committed | other


@router.post("/api/v1/test-factory/{artifact_id}/value-conflict/resolve")
async def resolve_value_conflict(
    body: ValueConflictResolveRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Human resolves a value-conflict (typed vs committed vs other) — a labeled
    data correction. Records ONLY the de-identified CHOICE enum, never the value.
    Flywheel capture gated default-OFF; best-effort so a pre-migration table / DB
    error can never break the user's action (authz 404 still propagates)."""
    tenant_id = user["tenant_id"]
    if not flywheel_ledger.capture_enabled():
        return {"recorded": False}
    choice = (body.choice or "").strip().lower()
    choice = choice if choice in ("typed", "committed", "other") else "other"
    try:
        async with tenant_scoped_session(tenant_id) as session:
            await _require_artifact(session, artifact_id, tenant_id)
            await flywheel_ledger.record_label(
                session, tenant_id=tenant_id, decision_point="value_conflict",
                artifact_id=artifact_id, test_case_id=body.test_id, scenario_id=body.test_id,
                human_decision_enum=f"chose_{choice}"[:32],
                git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
            )
    except HTTPException:
        raise  # authz / 404 stays a real error
    except Exception:
        return {"recorded": False}  # capture is best-effort — never 500 the user
    return {"recorded": True}


@router.post("/api/v1/test-factory/{artifact_id}/push/{tool}")
async def push_to_tm_tool(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    tool: str = PathParam(..., min_length=1, max_length=40),
    user: dict = Depends(get_current_user),
):
    """Push the active test suite into a test-management tool.

    Credentials are decrypted at call time from the tenant's connected
    ``integration_installations`` row (envelope-encrypted) — never hardcoded.
    """
    tool = tool.lower()
    if tool not in CONNECTORS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported tool '{tool}' (supported: {', '.join(CONNECTORS)})",
        )
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(status_code=503, detail="envelope_service unavailable")

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        inst = (
            await session.execute(
                select(integration_installations).where(
                    integration_installations.c.tenant_id == tenant_id,
                    integration_installations.c.integration_id == tool,
                    integration_installations.c.status == "connected",
                )
            )
        ).mappings().first()
        if inst is None:
            raise HTTPException(
                status_code=409,
                detail=f"no connected '{tool}' integration for this tenant — install it first",
            )
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )

    if not cases:
        raise HTTPException(
            status_code=404,
            detail="no generated test cases for this artifact — run /generate first",
        )

    blob_bytes = inst.get("encrypted_credentials")
    if not blob_bytes:
        raise HTTPException(
            status_code=409, detail=f"'{tool}' integration has no stored credentials",
        )
    try:
        plaintext = await envelope.decrypt(
            tenant_id,
            EnvelopeBlob.from_bytes(bytes(blob_bytes)),
            expected_aad=tool.encode("utf-8"),
        )
        credentials = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        _logger.warning("test_factory.push.credential_decrypt_failed tool=%s err=%s", tool, exc)
        raise HTTPException(status_code=502, detail=f"could not decrypt '{tool}' credentials")

    try:
        connector = build_connector(tool, credentials, dict(inst.get("config") or {}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Governance #5: redact PII before it leaves the perimeter to an external
    # SaaS (qTest/TestRail/...). Operates on copies; the stored suite is intact.
    from ..services.test_factory import redaction as _redaction
    cases, _pii_types = _redaction.redact_cases(cases)
    await _redaction.log_shield(tenant_id, "redact", len(cases), _pii_types,
                                str(user.get("sub") or user.get("user_id") or ""))

    async with httpx.AsyncClient(timeout=60.0) as http:
        result = await connector.push(cases, http)

    return {"success": result.failed == 0, "pii_redacted_types": sorted(set(_pii_types)), **result.as_dict()}


# ─── Test Factory inline edit via override layer (ADDITIVE 2026-06-21; outside
# the frozen capture pipeline) ────────────────────────────────────────────────
# Test Factory cases are GENERATED (grounded) from page-actions, so a raw row
# edit alone would be overwritten on the next regenerate. We (a) mutate the
# stored FactoryTestCaseRow.test_case JSON so list + Playwright reflect the edit
# immediately, AND (b) record the edit as an OVERRIDE in
# full_artifact_json["test_factory_overrides"]; /generate re-applies overrides so
# edits SURVIVE regeneration. Grounding (evidence_*) is never touched.

def _apply_case_override(tc: dict, step_ov: dict, name_ov) -> dict:
    if name_ov:
        tc["name"] = name_ov
        if "title" in tc:
            tc["title"] = name_ov
    kept = []
    for st in (tc.get("steps") or []):
        d = step_ov.get(str(st.get("step_number")))
        if d and d.get("deleted"):
            continue  # tombstoned step — dropped here AND on every regenerate
        if d:
            # #2b flag-for-review: if Pages & Forms changed this step SINCE the
            # user edited it (the regenerated base differs from the captured
            # baseline), flag it for review rather than silently keeping the stale
            # edit OR overwriting it. The user decides; nothing wins silently.
            base = d.get("baseline") if isinstance(d.get("baseline"), dict) else None
            pf_conflict = False
            if base:
                cur_obs = st.get("observed") or {}
                pf_conflict = (
                    (base.get("action") is not None and base.get("action") != st.get("action"))
                    or (base.get("label") is not None and base.get("label") != cur_obs.get("label"))
                    or (base.get("value") is not None and base.get("value") != cur_obs.get("value"))
                )
            for k, v in d.items():
                if k in ("deleted", "baseline"):
                    continue
                if k == "value":
                    # Editable TEST DATA: the compiler fills from observed.value,
                    # so route the user's value there + data_ref (display). Mark it
                    # user-edited — it is no longer oracle-proven (never green-wash).
                    obs = st.get("observed")
                    if not isinstance(obs, dict):
                        obs = {}
                        st["observed"] = obs
                    obs["value"] = v
                    obs["provenance"] = "user-edited"
                    st["data_ref"] = v
                else:
                    st[k] = v
            if pf_conflict:
                st["confidence"] = "review"   # surfaces in the existing "N to review" badge
                st["pf_conflict"] = True
        kept.append(st)
    tc["steps"] = kept
    return tc


async def _reapply_tf_overrides(artifact_id: str, tenant_id: str) -> int:
    import copy
    from sqlalchemy.orm.attributes import flag_modified
    from nexus_sdk.db.models import FactoryTestCaseRow, CanonicalArtifactRow
    async with tenant_scoped_session(tenant_id) as session:
        art = (await session.execute(select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        ))).scalar_one_or_none()
        overrides = dict((getattr(art, "full_artifact_json", None) or {}).get("test_factory_overrides") or {}) if art else {}
        if not overrides:
            return 0
        rows = (await session.execute(select(FactoryTestCaseRow).where(
            FactoryTestCaseRow.artifact_id == artifact_id,
        ))).scalars().all()
        n = 0
        for row in rows:
            co = overrides.get(row.test_case_id)
            if not co:
                continue
            tc = copy.deepcopy(row.test_case or {})
            _apply_case_override(tc, dict(co.get("steps") or {}), co.get("name"))
            row.test_case = tc
            flag_modified(row, "test_case")
            if co.get("name"):
                row.name = co["name"]
            n += 1
        await session.commit()
        return n


class EditTestCaseStep(BaseModel):
    step_number: int
    action: str | None = None
    expected_result: str | None = None
    verification: str | None = None
    value: str | None = None      # editable TEST DATA -> observed.value (user-edited)
    delete: bool = False          # tombstone this step; survives regenerate


class EditTestCaseRequest(BaseModel):
    title: str | None = None
    steps: list[EditTestCaseStep] = Field(default_factory=list)


@router.patch("/api/v1/test-factory/{artifact_id}/test-cases/{case_id}")
async def edit_test_case(
    req: EditTestCaseRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    case_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Inline-edit a Test Factory case (action / expected result / verification +
    title). Mutates the stored case JSON (list + Playwright reflect it) and
    records an override so the edit survives regeneration. Grounding immutable."""
    import copy
    from sqlalchemy.orm.attributes import flag_modified
    from nexus_sdk.db.models import FactoryTestCaseRow, CanonicalArtifactRow
    tenant_id = user["tenant_id"]
    step_ov: dict = {}
    for patch in req.steps:
        d: dict = {}
        if patch.delete:
            d["deleted"] = True
        if patch.action is not None:
            d["action"] = patch.action[:4000]
        if patch.expected_result is not None:
            d["expected_result"] = patch.expected_result[:4000]
        if patch.verification is not None:
            d["verification"] = patch.verification[:4000]
        if patch.value is not None:
            d["value"] = patch.value[:1000]
        if d:
            step_ov[str(patch.step_number)] = d
    name_ov = req.title.strip()[:500] if (req.title and req.title.strip()) else None

    async with tenant_scoped_session(tenant_id) as session:
        row = (await session.execute(select(FactoryTestCaseRow).where(
            FactoryTestCaseRow.test_case_id == case_id,
            FactoryTestCaseRow.artifact_id == artifact_id,
        ))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Test case {case_id} not found")
        tc = copy.deepcopy(row.test_case or {})
        # #2b: capture the pre-edit baseline of each edited step so a later Pages &
        # Forms change to that same step can be flagged for review on regenerate.
        _orig = {str(s.get("step_number")): s for s in (row.test_case or {}).get("steps", [])}
        for _sn, _d in step_ov.items():
            if _d.get("deleted"):
                continue
            _os = _orig.get(_sn) or {}
            _d["baseline"] = {
                "action": _os.get("action"),
                "label": (_os.get("observed") or {}).get("label"),
                "value": (_os.get("observed") or {}).get("value"),
            }
        _apply_case_override(tc, step_ov, name_ov)
        row.test_case = tc
        flag_modified(row, "test_case")
        if name_ov:
            row.name = name_ov

        art = (await session.execute(select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        ))).scalar_one_or_none()
        if art is not None:
            j = dict(art.full_artifact_json or {})
            ov = dict(j.get("test_factory_overrides") or {})
            existing = dict(ov.get(case_id) or {})
            es = dict(existing.get("steps") or {})
            for sn, d in step_ov.items():           # merge per step; don't clobber
                cur = dict(es.get(sn) or {})         # a prior edit on the same step
                cur.update(d)
                es[sn] = cur
            existing["steps"] = es
            if name_ov:
                existing["name"] = name_ov
            ov[case_id] = existing
            j["test_factory_overrides"] = ov
            art.full_artifact_json = j
            flag_modified(art, "full_artifact_json")
        await session.commit()

    return {
        "success": True,
        "test_case_id": case_id,
        "overridden_steps": len(step_ov),
        "survives_regenerate": True,
    }


# ─── Conversational grounded ADD (ADDITIVE 2026-06-21; outside the frozen
# capture pipeline) ───────────────────────────────────────────────────────────
# User describes a case in plain English -> proposer drafts it using ONLY the
# captured catalog (selectors computed server-side) -> review -> commit.  The
# commit RE-VALIDATES every step against the live catalog, so nothing ungrounded
# can be persisted.  Added cases survive regeneration (v1-human + reapply).

class ProposeAddRequest(BaseModel):
    message: str = ""
    history: list[dict] = []
    name: str = ""
    steps: list[dict] = []


@router.post("/api/v1/test-factory/{artifact_id}/propose-case")
async def propose_test_case(
    request: Request,
    body: ProposeAddRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Draft a NEW grounded test case from a plain-English request. Returns the
    proposed case for review (does NOT persist)."""
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await proposer.propose(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            message=body.message, history=body.history, router=llm_router,
        )


@router.post("/api/v1/test-factory/{artifact_id}/add-case")
async def add_test_case(
    request: Request,
    body: ProposeAddRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Persist a reviewed proposed case. Re-validates every step against the live
    catalog (ungrounded steps are dropped); stored so it survives regeneration."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        result = await proposer.add_case(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
            name=body.name, steps=body.steps, message=body.message,
        )
    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error") or "Could not ground the case")
    return result


# ─── Case review / sign-off lifecycle (ADDITIVE 2026-06-21; governance #3) ─────
# The CASE is the controlled document an auditor samples. We add a real
# draft -> in_review -> approved transition with approver identity + e-signature,
# stored in the case JSON (review block) + an immutable approval SNAPSHOT in the
# artifact json so regenerate cannot destroy approved content (see
# proposer.reapply_approved, wired into /generate). No migration.

class ReviewRequest(BaseModel):
    action: str = ""           # submit | approve | reject | reopen
    signature: str = ""        # typed full name — required to approve
    note: str = ""


@router.post("/api/v1/test-factory/{artifact_id}/test-cases/{case_id}/review")
async def review_test_case(
    req: ReviewRequest,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    case_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Transition a case through its sign-off lifecycle. Approve requires an
    e-signature and records approver identity + timestamp, and snapshots the
    approved content so a later Generate/Enrich cannot overwrite it."""
    import copy
    from datetime import datetime, timezone
    from sqlalchemy.orm.attributes import flag_modified
    from nexus_sdk.db.models import FactoryTestCaseRow, CanonicalArtifactRow

    tenant_id = user["tenant_id"]
    action = (req.action or "").strip().lower()
    if action not in ("submit", "approve", "reject", "reopen"):
        raise HTTPException(status_code=400, detail="action must be submit|approve|reject|reopen")
    actor = str(user.get("sub") or user.get("user_id") or user.get("email") or "")
    actor_email = str(user.get("email") or "")
    now = datetime.now(timezone.utc).isoformat()

    async with tenant_scoped_session(tenant_id) as session:
        row = (await session.execute(select(FactoryTestCaseRow).where(
            FactoryTestCaseRow.test_case_id == case_id,
            FactoryTestCaseRow.artifact_id == artifact_id,
        ))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Test case {case_id} not found")

        tc = copy.deepcopy(row.test_case or {})
        review = dict(tc.get("review") or {})
        prev = str(review.get("state") or "draft")

        if action == "submit":
            review["state"] = "in_review"
        elif action == "approve":
            if not (req.signature or "").strip():
                raise HTTPException(status_code=422, detail="An e-signature (your full name) is required to approve")
            review["state"] = "approved"
            review["approved_by"] = actor
            review["approved_email"] = actor_email
            review["approved_at"] = now
            review["signature"] = req.signature.strip()[:200]
        elif action == "reject":
            review["state"] = "rejected"
            review["rejected_by"] = actor
            review["rejected_at"] = now
        elif action == "reopen":
            review["state"] = "draft"
            review.pop("approved_by", None)
            review.pop("approved_at", None)
            review.pop("signature", None)

        history = list(review.get("history") or [])
        history.append({"action": action, "by": actor, "email": actor_email,
                        "at": now, "from": prev, "note": (req.note or "")[:500]})
        review["history"] = history[-50:]
        tc["review"] = review
        row.test_case = tc
        flag_modified(row, "test_case")

        # immutable approval snapshot (governance #4) — survives regenerate
        art = (await session.execute(select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        ))).scalar_one_or_none()
        if art is not None:
            j = dict(art.full_artifact_json or {})
            appr = dict(j.get("test_factory_approved") or {})
            if action == "approve":
                appr[case_id] = {"test_case": tc, "approved_by": actor,
                                 "approved_email": actor_email, "approved_at": now,
                                 "signature": review["signature"]}
            elif action in ("reject", "reopen"):
                appr.pop(case_id, None)  # unlock — no longer an approved snapshot
            j["test_factory_approved"] = appr
            art.full_artifact_json = j
            flag_modified(art, "full_artifact_json")

        await session.commit()

    return {"success": True, "case_id": case_id, "state": review["state"],
            "approved_by": review.get("approved_by", ""), "approved_at": review.get("approved_at", "")}


# ── Agentic Playwright audit — "Verify with AI" ──────────────────────────────
# Ported onto the VM's working router (the diverged full router pulled in
# prior-session modules the VM lacks, which crash-looped startup). Self-contained
# and uses ONLY names this router already imports; ``pw_auditor`` is imported
# LAZILY inside each handler so a missing/late module can never crash startup.
# Never green-wash: an ungrounded "fix" is demoted to UNPROVEN, never asserted
# green; on any LLM fault the $0 deterministic verdict stands.


def _audit_evidence_text(visits, actions) -> str:
    """A compact, verbatim ground-truth blob the auditor grounds every claim
    against — page URLs, recorded values, and observed outcomes from the recording."""
    by_visit: dict = {}
    for a in (actions or []):
        by_visit.setdefault(getattr(a, "page_visit_id", "") or "", []).append(a)
    lines: list[str] = []
    for v in (visits or []):
        path = getattr(v, "url_path", "") or getattr(v, "location", "") or "?"
        lines.append(f"PAGE {path}")
        for a in by_visit.get(getattr(v, "page_visit_id", "") or "", []):
            verb = (getattr(a, "verb", "") or "").strip()
            label = (getattr(a, "target_label", "") or "").strip()
            val = getattr(a, "value", None)
            outcome = (getattr(a, "after_outcome", "") or "").strip()
            detail = (getattr(a, "after_detail", "") or "").strip()
            line = f"  {verb} '{label}'"
            if val not in (None, ""):
                line += f" = '{val}'"
            if outcome:
                line += f" -> {outcome}: {detail}"
            lines.append(line)
    return "\n".join(lines)


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/audit")
async def audit_script_agentic(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    deep: int = Query(1, ge=0, le=1),
    user: dict = Depends(get_current_user),
):
    """Agentic Playwright audit of ONE script: physical-possibility + grounding,
    scored on the 5 auditor dimensions with a per-STEP verdict (the impossible-
    transition gate). Deterministic ($0) always; deep=1 adds the LLM per-step
    reasoning when a router is available. NEVER green-wash — an ungrounded "fix"
    is demoted to UNPROVEN, never asserted green; on any LLM fault the $0
    deterministic verdict stands (never auto-certify)."""
    from ..services.test_factory import playwright_auditor as pw_auditor
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        visits, actions = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this script")
    spec = (getattr(active, "script_source", None) if active else None) \
        or compile_case(tc, field_meta, parametrize=True)
    steps = list(getattr(tc, "steps", []) or [])

    if deep:
        composer = getattr(request.app.state, "storyboard_composer", None)
        llm_router = getattr(composer, "_llm_router", None) if composer else None
        if llm_router is not None:
            _deep = await pw_auditor.audit(
                spec_text=spec, evidence_text=_audit_evidence_text(visits, actions),
                steps=steps, evidence=actions, router=llm_router)
            # The DECISION is deterministic, never LLM-derived: gaps are
            # coverage info and must not flip a certified score into repair.
            try:
                _deep["decision"] = pw_auditor.score_spec(
                    spec, steps, evidence=actions)["decision"]
                _deep["decision_source"] = "deterministic"
            except Exception:
                pass
            return _deep
    # $0 deterministic verdict (LLM disabled / unavailable) — fully functional.
    return pw_auditor.score_spec(spec, steps, evidence=actions)


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/audit/repair")
async def repair_script_from_audit(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Apply the audit repair: RE-DERIVE the test case from the recording with the
    grounded generator (ungrounded navigation assertions dropped, typed fills
    restored, uncaptured boundaries marked UNPROVEN), recompile this script, and
    save it as a new version (v+1, reversible via history). The repair is always
    COMPILER-emitted from grounded evidence — never an LLM-authored spec, never a
    fabricated green. Returns the before/after diff + the re-scored audit."""
    from ..services.test_factory import playwright_auditor as pw_auditor
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
        before = (getattr(active, "script_source", "") if active else "") or ""
        # Re-derive the cases with the fixed generator, then recompile this script.
        await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "")
        # generate_and_store commits — re-arm the transaction-local RLS scope.
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)})
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        _visits, actions = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
        tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
        if tc is None:
            raise HTTPException(status_code=404, detail="no active test case for this script")
        repaired = compile_case(tc, field_meta, parametrize=True)
        id_to_path = {s["test_id"]: s["path"]
                      for s in compile_manifest([tc], field_meta).get("scripts", [])}
        row = await script_versions.save_new_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id, session_id="",
            test_case_id=test_id, spec_path=id_to_path.get(test_id, ""), script_source=repaired,
            data_json={}, author="nexus-audit-repair",
            note="Repaired from the agentic audit — re-derived from the recording "
                 "(ungrounded assertions dropped, fills grounded, gaps marked UNPROVEN).")
    report = pw_auditor.score_spec(repaired, list(getattr(tc, "steps", []) or []), evidence=actions)
    return {
        "before": before, "after": repaired,
        "version_no": getattr(row, "version_no", None),
        "changed": before.strip() != repaired.strip(),
        "audit": report,
    }


def _engine_extra_files(visits, actions, cases) -> dict:
    """P4/P7/P8 evidence-derived extras for the delivered project. Pure +
    additive: consumes already-loaded rows, returns {path: content}. Any
    failure upstream is caught by the caller — extras can never break the zip."""
    import json as _json

    def _n(v):
        return " ".join(str(v or "").split()).strip()

    trusted = [v for v in (visits or [])
               if str(getattr(v, "source", "") or "").lower() in
               ("ground_truth", "url_regex", "url_scene", "llm_inferred")
               and (getattr(v, "canonical_host", "") or getattr(v, "url_host", ""))]

    by_visit: dict = {}
    for a in (actions or []):
        vid = getattr(a, "page_visit_id", "")
        lbl = _n(getattr(a, "target_label", ""))
        verb = str(getattr(a, "verb", "") or "").lower()
        kind = str(getattr(a, "target_kind", "") or "").lower()
        if vid and lbl and verb in ("type", "select", "click", "check", "fill"):
            by_visit.setdefault(vid, []).append((lbl, kind))

    out: dict = {}

    # ── P4: POM-lite from the video ──────────────────────────────────────
    lines = [
        "// vkpower-pages.ts — page objects SYNTHESIZED FROM THE RECORDING.",
        "// One entry per trusted-tier page visit; controls are the ones the user",
        "// actually touched, with accessibility-first locators. Import and use,",
        "// or treat as living documentation — the specs do not depend on it.",
        "import { Page } from '@playwright/test';",
        "",
        "export const VKPowerPages = {",
    ]
    used_keys: set = set()
    for v in trusted:
        host = getattr(v, "canonical_host", "") or getattr(v, "url_host", "")
        path = getattr(v, "url_path", "") or "/"
        seg = [s for s in path.split("/") if s]
        key = "_".join(seg[-2:]) if seg else "home"
        key = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in key) or "page"
        while key in used_keys:
            key += "_"
        used_keys.add(key)
        controls = by_visit.get(getattr(v, "page_visit_id", ""), [])
        lines.append(f"  {key}: {{")
        lines.append(f"    url: 'https://{host}{path}',")
        lines.append("    controls: (page: Page) => ({")
        seen_ctl: set = set()
        for lbl, kind in controls[:12]:
            ck = "".join(ch if ch.isalnum() else "_" for ch in lbl.lower())[:40] or "control"
            if ck in seen_ctl:
                continue
            seen_ctl.add(ck)
            esc = lbl.replace("'", "\\'")
            if kind in ("button", "link", "radio", "checkbox", "tab"):
                loc = f"page.getByRole('{kind}', {{ name: '{esc}' }})"
            else:
                loc = f"page.getByLabel('{esc}')"
            lines.append(f"      {ck}: {loc},")
        lines.append("    }),")
        lines.append("  },")
    lines.append("};")
    lines.append("")
    out["pages/vkpower-pages.ts"] = "\n".join(lines)

    # ── P7: T2 synthetic data tiers (UNPROVEN, approval-gated, NOT wired) ─
    fields: dict = {}
    for a in (actions or []):
        verb = str(getattr(a, "verb", "") or "").lower()
        lbl = _n(getattr(a, "target_label", ""))
        val = _n(getattr(a, "value", ""))
        if verb in ("type", "select") and lbl and val:
            fields.setdefault(lbl, val)
    synth: dict = {}
    for lbl, val in list(fields.items())[:25]:
        digits = "".join(ch for ch in val if ch.isdigit())
        variants = [
            {"value": "", "class": "empty"},
            {"value": " " + val + " ", "class": "surrounding-whitespace"},
            {"value": val + "X" * max(1, 256 - len(val)), "class": "overflow-length"},
        ]
        if digits and digits == "".join(ch for ch in val if ch.isalnum()):
            variants += [{"value": "0", "class": "numeric-lower-bound"},
                         {"value": "9" * max(2, len(digits) + 1), "class": "numeric-overflow"}]
        synth[lbl] = {
            "observed_value": val,
            "tier": "T2-synthetic-UNPROVEN",
            "requires_approval": True,
            "variants": variants,
        }
    out["data/vkpower.synthetic.json"] = _json.dumps({
        "note": ("Synthetic boundary/invalid candidates DERIVED from observed value "
                 "formats. UNPROVEN by definition — the recording never demonstrated "
                 "them. Governance: requires_approval=true; they are NOT wired into "
                 "any generated case until a human approves."),
        "fields": synth,
    }, indent=2, sort_keys=True)

    # ── P8: advisory a11y lane ────────────────────────────────────────────
    a11y = [
        "// advisory-a11y.spec.ts — ADVISORY accessibility probes (never gating).",
        "// Landmark/heading presence on the recorded trusted pages. Run explicitly:",
        "//   npx playwright test tests/a11y --project=chromium",
        "import { test, expect } from '@playwright/test';",
        "",
        "test.describe('@a11y-advisory landmarks', () => {",
    ]
    for v in trusted[:5]:
        host = getattr(v, "canonical_host", "") or getattr(v, "url_host", "")
        path = getattr(v, "url_path", "") or "/"
        a11y += [
            f"  test('advisory: heading present on {path}', async ({{ page }}) => {{",
            "    test.info().annotations.push({ type: 'advisory', description: 'a11y probe — reports, does not gate' });",
            f"    await page.goto('https://{host}{path}');",
            "    await expect(page.getByRole('heading').first()).toBeVisible({ timeout: 10000 });",
            "  });",
        ]
    a11y += ["});", ""]
    out["tests/a11y/advisory-a11y.spec.ts"] = "\n".join(a11y)

    # ── P8: diagnostics bundle schema v1 ─────────────────────────────────
    out["vkpower-diagnostics.schema.json"] = _json.dumps({
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Nexus run diagnostics bundle",
        "version": "v1",
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "status": {"type": "string", "enum": ["running", "passed", "failed", "stopped"]},
            "terminal_state": {"type": ["string", "null"]},
            "stop_reason": {"type": ["string", "null"],
                            "description": "honest diagnosis — never editorialized"},
            "heal_trace": {"type": "array", "items": {"type": "object", "properties": {
                "event": {"type": "string"}, "rung": {"type": "string"},
                "explanation": {"type": "string"}, "confidence": {"type": "number"}}}},
            "screenshots": {"type": "array", "items": {"type": "string"}},
            "step_results": {"type": "array", "items": {"type": "object", "properties": {
                "step_number": {"type": "integer"}, "verdict": {"type": "string"},
                "evidence_tier": {"type": "string"}}}},
        },
        "required": ["run_id", "status"],
    }, indent=2, sort_keys=True)

    return out


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/verify")
async def verify_script(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    body: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """UNIFIED VERIFICATION v2: deterministic rubric + lint + risk model +
    decision dossier (+ optional readiness probe when base_url is given) +
    governed waivers, appended to the hash-chained verdict history. The
    decision is deterministic — an LLM never scores. $0 by default."""
    import hashlib as _hl
    from ..services.test_factory import playwright_auditor as pw_auditor
    from ..services.test_factory import verdict_events as _ve

    tenant_id = user["tenant_id"]
    actor = str(user.get("email") or user.get("sub") or "")
    async with tenant_scoped_session(tenant_id) as session:
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        visits, actions = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this script")
    spec = (getattr(active, "script_source", None) if active else None) \
        or compile_case(tc, field_meta, parametrize=True)
    steps = list(getattr(tc, "steps", []) or [])

    det = pw_auditor.score_spec(spec, steps, evidence=actions)
    try:
        lint = pw_auditor.lint_spec(spec)
    except Exception:
        lint = []

    # optional READINESS probe (base_url): reachability + live locator preflight.
    preflight_result = None
    readiness = None
    base_url = (str(body.get("base_url") or "")).strip()
    if base_url:
        reasons = []
        reachable = False
        try:
            import httpx as _hx
            async with _hx.AsyncClient(timeout=6.0, verify=False) as _cl:
                _r = await _cl.get(base_url)
                reachable = _r.status_code < 500
                if not reachable:
                    reasons.append(f"environment returned HTTP {_r.status_code}")
        except Exception as _re:
            reasons.append(f"environment unreachable: {str(_re)[:120]}")
        try:
            from .preflight import preflight as _pf, runner_client as _rc
            files = _pf.build_preflight_files(tc, field_meta, base_url=base_url)
            preflight_result = await _rc.run_suite(
                files, {"NEXUS_BASE_URL": base_url}, timeout_ms=120000)
        except Exception as _pe:
            preflight_result = {"error": f"preflight unavailable: {str(_pe)[:160]}"}
            reasons.append("locator preflight unavailable")
        _data_refs = sum(1 for s in steps
                         if (s.get("data_ref") if isinstance(s, dict)
                             else getattr(s, "data_ref", None)))
        readiness = {
            "status": ("BLOCKED" if not reachable else
                       ("DEGRADED" if reasons else "READY")),
            "reachable": reachable,
            "data_ref_steps": _data_refs,
            "reasons": reasons,
        }

    risk_obj = _ve.risk(steps=steps, det=det, lint=lint, preflight=preflight_result)

    # governed waivers: annotate, never delete
    waivers = await _ve.active_waivers(
        tenant_id=tenant_id, artifact_id=artifact_id, test_id=test_id)
    active_findings, waived_findings = _ve.apply_waivers(
        list(det.get("findings") or []), waivers)

    # alternatives considered = the locator rungs per step (anchor bundles)
    try:
        from ..services.script_factory.compiler import _anchor_bundles as _ab
        alternatives = _ab(steps)
    except Exception:
        alternatives = []

    lint_errors = [l for l in lint if l.get("severity") == "error"]
    verdict = {
        "artifact_id": artifact_id,
        "test_id": test_id,
        "version": getattr(active, "version", None) if active else None,
        "registry_version": _ve.REGISTRY_VERSION,
        "overall_score": det.get("overall_score"),
        "decision": det.get("decision"),
        "certification_level": (
            "CERTIFIED-EVIDENCED" if det.get("decision") == "certified" and actions
            else ("CERTIFIED-STATIC" if det.get("decision") == "certified"
                  else str(det.get("decision", "")).upper() or "REPAIR")),
        "dimension_scores": det.get("dimension_scores"),
        "risk": risk_obj,
        "gaps": det.get("gaps"),
        "findings": active_findings,
        "waived_findings": waived_findings,
        "per_step": det.get("per_step", []),
        "lint": lint,
        "lint_errors": len(lint_errors),
        "preflight": preflight_result,
        "readiness": readiness,
        "decision_source": "deterministic",
    }
    rec = await _ve.record_verdict(
        tenant_id=tenant_id, artifact_id=artifact_id, test_id=test_id,
        version=verdict["version"], source="verify", actor=actor,
        overall=int(det.get("overall_score") or 0),
        decision=str(det.get("decision") or ""),
        axes=dict(det.get("dimension_scores") or {}),
        gaps=int(det.get("gaps") or 0),
        findings=[str(f)[:200] for f in active_findings],
        lint=lint,
        preflight=(preflight_result if isinstance(preflight_result, dict) else None),
    )
    verdict["verdict_event"] = rec
    if rec:
        dossier = _ve.build_dossier(
            spec=spec, steps=steps, det=det, lint=lint,
            preflight=preflight_result, risk_obj=risk_obj,
            alternatives=alternatives, actor=actor, source="verify")
        ok = await _ve.save_dossier(
            tenant_id=tenant_id, artifact_id=artifact_id, test_id=test_id,
            verdict_id=rec["verdict_id"], chain_hash=rec["chain_hash"],
            payload=dossier)
        verdict["dossier_saved"] = ok
    return verdict


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/verdicts")
async def script_verdicts(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Verdict history timeline (newest first) + regression-aware trend."""
    from ..services.test_factory import verdict_events as _ve
    tenant_id = user["tenant_id"]
    events = await _ve.list_verdicts(
        tenant_id=tenant_id, artifact_id=artifact_id, test_id=test_id, limit=limit)
    return {"artifact_id": artifact_id, "test_id": test_id,
            "trend": _ve.trend(events), "events": events}


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/dossiers/{verdict_id}")
async def get_decision_dossier(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    verdict_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The reproducible decision record: inputs, rules, evidence, alternatives,
    rationale — hash-chained to the verdict."""
    from ..services.test_factory import verdict_events as _ve
    d = await _ve.get_dossier(tenant_id=user["tenant_id"], verdict_id=verdict_id)
    if d is None or d.get("artifact_id") != artifact_id or d.get("test_id") != test_id:
        raise HTTPException(status_code=404, detail="dossier not found")
    return d


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/waivers")
async def create_verification_waiver(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """Governed exception: waive a finding WITH reason + expiry (max 90 days).
    Waivers annotate findings in verdicts — they never delete them."""
    from datetime import datetime, timedelta, timezone
    from ..services.test_factory import verdict_events as _ve
    match = str(body.get("finding_match") or "").strip()
    reason = str(body.get("reason") or "").strip()
    days = min(90, max(1, int(body.get("days") or 30)))
    if len(match) < 6 or len(reason) < 10:
        raise HTTPException(
            status_code=422,
            detail="finding_match (>=6 chars) and reason (>=10 chars) are required")
    rec = await _ve.create_waiver(
        tenant_id=user["tenant_id"], artifact_id=artifact_id, test_id=test_id,
        finding_match=match, reason=reason,
        actor=str(user.get("email") or user.get("sub") or ""),
        expires_at=datetime.now(timezone.utc) + timedelta(days=days))
    if rec is None:
        raise HTTPException(status_code=503, detail="waiver store unavailable")
    return rec


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/waivers")
async def list_verification_waivers(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    from ..services.test_factory import verdict_events as _ve
    return {"waivers": await _ve.active_waivers(
        tenant_id=user["tenant_id"], artifact_id=artifact_id, test_id=test_id)}


@router.get("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/remediations")
async def list_remediations(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Findings -> safe compiler-channel actions. Every remediation routes
    through the existing audit/repair loop — never freehand code."""
    from ..services.test_factory import playwright_auditor as pw_auditor
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        _v, actions = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this script")
    spec = (getattr(active, "script_source", None) if active else None) \
        or compile_case(tc, field_meta, parametrize=True)
    det = pw_auditor.score_spec(spec, list(getattr(tc, "steps", []) or []), evidence=actions)
    try:
        lint = pw_auditor.lint_spec(spec)
    except Exception:
        lint = []
    items = []
    for f in det.get("findings") or []:
        txt = str(f)
        channel = ("reanchors" if "locator" in txt.lower()
                   else "nav_overrides" if "navigation" in txt.lower()
                   else "interactions" if "filled" in txt.lower() or "value" in txt.lower()
                   else "stabilize")
        items.append({"finding": txt, "channel": channel,
                      "apply_via": f"POST /api/v1/test-factory/{artifact_id}/scripts/{test_id}/audit/repair",
                      "auto_appliable": True})
    for l in lint:
        if l.get("severity") == "error":
            items.append({"finding": f"lint:{l.get('rule')} line {l.get('line')}",
                          "channel": "api-policy",
                          "apply_via": "regenerate (compiler emits policy-clean code)",
                          "auto_appliable": True})
    return {"count": len(items), "remediations": items}


@router.get("/api/v1/verification/calibration")
async def verification_calibration(
    user: dict = Depends(get_current_user),
):
    """Historian v0: per-dimension / per-source verdict distribution from the
    timeline. HONEST NOTE: precision needs labeled outcomes — until runs/humans
    confirm findings, these are fire COUNTS, not precision claims."""
    from sqlalchemy import select as _sel
    from ..services.test_factory.verdict_events import VerdictEventRow
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            _sel(VerdictEventRow.axes, VerdictEventRow.source,
                 VerdictEventRow.decision).where(
                VerdictEventRow.tenant_id == tenant_id).limit(2000)
        )).all()
    dims: dict = {}
    by_source: dict = {}
    for axes, source, decision in rows:
        by_source.setdefault(source, {"count": 0, "certified": 0})
        by_source[source]["count"] += 1
        by_source[source]["certified"] += int(decision == "certified")
        for k, v in (axes or {}).items():
            d = dims.setdefault(k, {"count": 0, "sum": 0, "min": 10})
            d["count"] += 1
            d["sum"] += int(v or 0)
            d["min"] = min(d["min"], int(v or 0))
    for k, d in dims.items():
        d["avg"] = round(d["sum"] / d["count"], 2) if d["count"] else None
        d.pop("sum", None)
    from ..services.test_factory.verdict_events import precision_by_dimension
    measured = await precision_by_dimension(tenant_id=tenant_id)
    return {"events": len(rows), "dimensions": dims, "by_source": by_source,
            "measured_precision": measured,
            "note": ("measured_precision reflects human/run-labeled outcomes "
                     "(POST /verification/findings/label); dimensions without "
                     "labels stay honest counts, never self-reported precision")}


@router.post("/api/v1/verification/import")
async def verify_imported_script(
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """Verify a THIRD-PARTY script (Copilot/human-written). Honest ceiling:
    with no recording evidence, evidence-dependent dimensions are unverifiable
    and the best achievable level is CERTIFIED-STATIC — stated, not hidden."""
    import hashlib as _hl
    from ..services.test_factory import playwright_auditor as pw_auditor
    from ..services.test_factory import verdict_events as _ve
    script = str(body.get("script") or "")
    name = str(body.get("name") or "imported-script")[:120]
    if len(script) < 40:
        raise HTTPException(status_code=422, detail="body.script (>=40 chars) required")
    det = pw_auditor.score_spec(script, [], evidence=None)
    try:
        lint = pw_auditor.lint_spec(script)
    except Exception:
        lint = []
    risk_obj = _ve.risk(steps=[], det=det, lint=lint, preflight=None)
    pseudo_id = _hl.sha256(script.encode("utf-8")).hexdigest()[:32]
    rec = await _ve.record_verdict(
        tenant_id=user["tenant_id"], artifact_id="imported", test_id=pseudo_id,
        version=None, source="import",
        actor=str(user.get("email") or user.get("sub") or ""),
        overall=int(det.get("overall_score") or 0),
        decision=str(det.get("decision") or ""),
        axes=dict(det.get("dimension_scores") or {}),
        gaps=int(det.get("gaps") or 0),
        findings=[str(f)[:200] for f in (det.get("findings") or [])],
        lint=lint)
    return {
        "name": name,
        "script_sha256": pseudo_id,
        "overall_score": det.get("overall_score"),
        "decision": det.get("decision"),
        "certification_level": ("CERTIFIED-STATIC" if det.get("decision") == "certified"
                                 else str(det.get("decision", "")).upper() or "REPAIR"),
        "ceiling_note": ("no recording evidence supplied — grounded-replay, "
                         "navigation-causality and value-fidelity checks are "
                         "UNVERIFIABLE for this asset; CERTIFIED-STATIC is the "
                         "honest maximum. Upload a recording to unlock "
                         "CERTIFIED-EVIDENCED."),
        "unverifiable_dimensions": ["grounded_replay (evidence-dependent parts)",
                                     "navigation_correctness (causality vs recording)"],
        "dimension_scores": det.get("dimension_scores"),
        "risk": risk_obj,
        "lint": lint,
        "verdict_event": rec,
        "decision_source": "deterministic",
    }


@router.post("/api/v1/verification/findings/label")
async def label_finding(
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """HISTORIAN FUEL: record a confirmed/refuted outcome for a finding. As
    labels accumulate, /verification/calibration reports MEASURED precision."""
    from ..services.test_factory import verdict_events as _ve
    outcome = str(body.get("outcome") or "").strip().lower()
    if outcome not in ("confirmed", "refuted"):
        raise HTTPException(status_code=422, detail="outcome must be confirmed|refuted")
    match = str(body.get("finding_match") or "").strip()
    if len(match) < 6:
        raise HTTPException(status_code=422, detail="finding_match (>=6 chars) required")
    rec = await _ve.add_finding_label(
        tenant_id=user["tenant_id"],
        artifact_id=str(body.get("artifact_id") or "")[:64],
        test_id=str(body.get("test_id") or "")[:64],
        dimension=str(body.get("dimension") or "")[:64],
        finding_match=match, outcome=outcome,
        actor=str(user.get("email") or user.get("sub") or ""))
    if rec is None:
        raise HTTPException(status_code=503, detail="label store unavailable")
    return rec


@router.post("/api/v1/test-factory/{artifact_id}/test-cases/{test_id}/requirement")
async def set_requirement_ref(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """INTENT v1: attach a requirement reference (Jira/ALM id) to a test case
    as a governed tag (req:<REF>). Idempotent; multiple refs allowed."""
    from sqlalchemy import text as _text
    ref = str(body.get("requirement_ref") or "").strip()
    if not (2 <= len(ref) <= 60) or any(ch in ref for ch in " '\";"):
        raise HTTPException(status_code=422, detail="requirement_ref: 2-60 chars, no spaces/quotes")
    tag = f"req:{ref}"
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        import json as _json
        res = await session.execute(_text(
            "update factory_test_cases set tags = ("
            " case when tags @> cast(:tagarr as jsonb) then tags"
            " else coalesce(tags, '[]'::jsonb) || cast(:tagarr2 as jsonb) end)"
            " where artifact_id = :a and test_case_id = :t and tenant_id = :ten"
        ), {"tagarr": _json.dumps([tag]), "tagarr2": _json.dumps([tag]),
            "a": artifact_id, "t": test_id, "ten": tenant_id})
        await session.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="test case not found")
    return {"test_id": test_id, "requirement_ref": ref, "tag": tag}


@router.get("/api/v1/test-factory/{artifact_id}/traceability")
async def traceability_report(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """INTENT v1: requirement x case x evidence-grade x latest-verdict coverage
    matrix — the regulated-buyer audit answer, from data that already exists."""
    from sqlalchemy import text as _text
    from ..services.test_factory import verdict_events as _ve
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(_text(
            "select test_case_id, name, coalesce(tags,'[]'::jsonb) as tags "
            "from factory_test_cases where artifact_id=:a and tenant_id=:ten"
        ), {"a": artifact_id, "ten": tenant_id})).all()
    matrix: dict = {}
    unmapped = []
    for tid, name, tags in rows:
        tags = list(tags or [])
        refs = [str(x)[4:] for x in tags if str(x).startswith("req:")]
        grade = next((str(x).split(":", 1)[1] for x in tags
                      if str(x).startswith("evidence-grade:")), None)
        events = await _ve.list_verdicts(
            tenant_id=tenant_id, artifact_id=artifact_id, test_id=tid, limit=1)
        latest = events[0] if events else None
        entry = {"test_id": tid, "name": name, "evidence_grade": grade,
                 "latest_verdict": ({"overall": latest["overall"],
                                     "decision": latest["decision"],
                                     "at": latest["created_at"]} if latest else None)}
        if refs:
            for r in refs:
                matrix.setdefault(r, []).append(entry)
        else:
            unmapped.append(entry)
    return {
        "artifact_id": artifact_id,
        "requirements": [{"requirement_ref": r, "cases": v,
                          "covered": any((c["latest_verdict"] or {}).get("decision") == "certified"
                                          for c in v)}
                         for r, v in sorted(matrix.items())],
        "unmapped_cases": unmapped,
        "note": ("covered = at least one CERTIFIED case verifies the requirement; "
                 "unmapped cases need a requirement_ref for full traceability"),
    }


@router.get("/api/v1/verification/sentinel")
async def sentinel_report(
    stale_days: int = Query(14, ge=1, le=90),
    user: dict = Depends(get_current_user),
):
    """SENTINEL v1: the watcher — regression trends, spec drift since last
    verify, stale scripts, HIGH-risk-yet-certified assets. Poll from cron/CI;
    each alert carries evidence. Read-only, deterministic."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as _sel
    from ..services.test_factory.verdict_events import (
        VerdictEventRow, DecisionDossierRow)
    tenant_id = user["tenant_id"]
    alerts = []
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            _sel(VerdictEventRow).where(
                VerdictEventRow.tenant_id == tenant_id
            ).order_by(VerdictEventRow.created_at.desc()).limit(1000)
        )).scalars().all()
        dossiers = (await session.execute(
            _sel(DecisionDossierRow.test_id, DecisionDossierRow.payload,
                 DecisionDossierRow.created_at).where(
                DecisionDossierRow.tenant_id == tenant_id
            ).order_by(DecisionDossierRow.created_at.desc()).limit(500)
        )).all()
    by_test: dict = {}
    for r in rows:
        by_test.setdefault((r.artifact_id, r.test_id), []).append(r)
    now = datetime.now(timezone.utc)
    for (aid, tid), evs in by_test.items():
        if len(evs) >= 2 and evs[0].overall < evs[1].overall:
            alerts.append({"kind": "regression", "severity": "major",
                           "artifact_id": aid, "test_id": tid,
                           "evidence": f"overall {evs[1].overall} -> {evs[0].overall} "
                                       f"at {evs[0].created_at.isoformat()}"})
        if evs and (now - evs[0].created_at).days >= stale_days:
            alerts.append({"kind": "stale", "severity": "minor",
                           "artifact_id": aid, "test_id": tid,
                           "evidence": f"no verdict in {(now - evs[0].created_at).days}d"})
    spec_hashes: dict = {}
    for tid, payload, created in dossiers:
        h = ((payload or {}).get("inputs") or {}).get("spec_sha256")
        if not h:
            continue
        prev = spec_hashes.get(tid)
        if prev and prev != h:
            alerts.append({"kind": "spec_drift", "severity": "advisory",
                           "test_id": tid,
                           "evidence": f"spec hash changed {prev[:10]} -> {h[:10]} between verifies"})
        spec_hashes.setdefault(tid, h)
    return {"alerts": alerts, "scripts_watched": len(by_test),
            "note": "poll from cron/CI for autonomy; every alert is evidence-linked"}


@router.get("/api/v1/verification/escalations")
async def escalation_inbox(
    user: dict = Depends(get_current_user),
):
    """ESCALATIONS v1: the human queue, derived from live data — regressions,
    waivers expiring within 7 days, non-certified deliveries. Nothing invents
    work; every item cites its source record."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select as _sel
    from ..services.test_factory.verdict_events import VerdictEventRow, WaiverRow
    tenant_id = user["tenant_id"]
    items = []
    now = datetime.now(timezone.utc)
    async with tenant_scoped_session(tenant_id) as session:
        evs = (await session.execute(
            _sel(VerdictEventRow).where(
                VerdictEventRow.tenant_id == tenant_id,
                VerdictEventRow.decision != "certified",
                VerdictEventRow.source == "delivery-gate",
            ).order_by(VerdictEventRow.created_at.desc()).limit(50)
        )).scalars().all()
        wvs = (await session.execute(
            _sel(WaiverRow).where(
                WaiverRow.tenant_id == tenant_id,
                WaiverRow.expires_at > now,
                WaiverRow.expires_at < now + timedelta(days=7),
            )
        )).scalars().all()
    for e in evs:
        items.append({"kind": "non_certified_delivery", "severity": "major",
                      "artifact_id": e.artifact_id, "test_id": e.test_id,
                      "evidence": f"delivered at {e.created_at.isoformat()} with "
                                  f"decision={e.decision} overall={e.overall}"})
    for w in wvs:
        items.append({"kind": "waiver_expiring", "severity": "minor",
                      "artifact_id": w.artifact_id, "test_id": w.test_id,
                      "evidence": f"waiver '{w.finding_match}' by {w.actor} "
                                  f"expires {w.expires_at.isoformat()}"})
    return {"count": len(items), "items": items}


@router.post("/api/v1/verification/sentinel/scan")
async def sentinel_scan_now(
    user: dict = Depends(get_current_user),
):
    """Run one Sentinel cycle NOW (persisting deduped alerts) — the same core
    the autonomous daemon runs on its schedule."""
    from ..services.test_factory.qe_agents import sentinel_scan
    alerts = await sentinel_scan(user["tenant_id"], persist=True)
    return {"alerts_found": len(alerts), "alerts": alerts[:50]}


@router.get("/api/v1/verification/sentinel/alerts")
async def sentinel_alerts(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    from sqlalchemy import select as _sel
    from ..services.test_factory.qe_agents import SentinelAlertRow
    async with tenant_scoped_session(user["tenant_id"]) as session:
        rows = (await session.execute(
            _sel(SentinelAlertRow).where(
                SentinelAlertRow.tenant_id == user["tenant_id"]
            ).order_by(SentinelAlertRow.created_at.desc()).limit(limit)
        )).scalars().all()
    return {"count": len(rows), "alerts": [
        {"alert_id": r.alert_id, "kind": r.kind, "severity": r.severity,
         "artifact_id": r.artifact_id, "test_id": r.test_id,
         "evidence": r.evidence, "created_at": r.created_at.isoformat()}
        for r in rows]}


@router.post("/api/v1/verification/triage/{run_id}")
async def triage_run(
    run_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """TRIAGE: product vs script vs environment vs data classification for a
    runner job — deterministic v1, honest 'unknown' when markers are absent."""
    from sqlalchemy import select as _sel
    from ..services.test_factory.runner_jobs import E2ERunnerJobRow
    from ..services.test_factory.qe_agents import triage_classify
    tenant_id = user["tenant_id"]
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        async with tenant_scoped_session(tenant_id) as session:
            row = (await session.execute(
                _sel(E2ERunnerJobRow).where(
                    E2ERunnerJobRow.run_id == run_id,
                    E2ERunnerJobRow.tenant_id == tenant_id)
            )).scalar()
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        job = dict(getattr(row, "job_json", None) or {})
    return {"run_id": run_id, "triage": triage_classify(job)}


@router.post("/api/v1/test-factory/{artifact_id}/test-cases/{test_id}/intent-check")
async def intent_check(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    body: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    """INTENT LENS: does the demonstrated flow satisfy this requirement?
    LLM judgment is QUOTE-GROUNDED — every quote must be a verbatim substring
    of the case evidence or the verdict demotes to 'unverifiable'. With no LLM
    available the deterministic token-coverage heuristic answers, labeled
    honestly. The agent never grades itself."""
    import json as _json
    from ..services.test_factory.qe_agents import (
        build_intent_evidence, intent_heuristic, validate_intent_quotes)
    req_text = str(body.get("requirement_text") or "").strip()
    req_ref = str(body.get("requirement_ref") or "").strip()
    if len(req_text) < 15:
        raise HTTPException(status_code=422,
                            detail="requirement_text (>=15 chars) required")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        cases, _fm = await _fidelity_inputs(session, artifact_id)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case")
    carry = list(getattr(tc, "data_carry", None) or [])
    evidence_text = build_intent_evidence(tc, carry)

    heuristic = intent_heuristic(req_text, evidence_text)
    result = dict(heuristic)

    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is not None:
        prompt = (
            "You judge whether a DEMONSTRATED test flow satisfies a business "
            "requirement. Use ONLY the evidence lines given. Respond with pure "
            "JSON: {\"verdict\": \"satisfies|partial|unsatisfied\", "
            "\"rationale\": str, \"quotes\": [str, ...]}. Every quote MUST be "
            "copied VERBATIM from an evidence line (exact substring). Never "
            "invent steps.\n\nREQUIREMENT: " + req_text
            + "\n\nEVIDENCE:\n" + evidence_text[:6000]
        )
        try:
            from ..services.llm.providers import CompletionRequest as _CReq
            try:
                _req = _CReq(prompt=prompt, max_tokens=500)
            except TypeError:
                try:
                    _req = _CReq(messages=[{"role": "user", "content": prompt}],
                                 max_tokens=500)
                except TypeError:
                    _req = _CReq(prompt=prompt)
            raw = await llm_router.complete(request=_req, task="analysis")
            txt = (getattr(raw, "text", None) or getattr(raw, "content", None)
                   or (raw if isinstance(raw, str) else str(raw)))
            start, end = txt.find("{"), txt.rfind("}")
            parsed = _json.loads(txt[start:end + 1]) if start >= 0 else {}
            if parsed.get("verdict") in ("satisfies", "partial", "unsatisfied"):
                validated = validate_intent_quotes(parsed, evidence_text)
                validated["heuristic_check"] = {
                    "coverage": heuristic["coverage"],
                    "verdict": heuristic["verdict"]}
                result = validated
        except Exception as _le:
            result["llm_note"] = f"LLM lens unavailable ({str(_le)[:80]}) — heuristic verdict stands"
    return {
        "artifact_id": artifact_id,
        "test_id": test_id,
        "requirement_ref": req_ref or None,
        "requirement_text": req_text,
        "intent": result,
        "evidence_lines": evidence_text.count("\n") + 1,
        "contract": ("quotes verbatim-validated against demonstrated evidence; "
                     "unvalidated LLM output demotes to unverifiable"),
    }


@router.get("/api/v1/verification/precision-report")
async def precision_report(
    user: dict = Depends(get_current_user),
):
    """THE PUBLISHABLE NUMBERS: measured label precision + verdict/gate stats +
    red-team status in one artifact. Anything unmeasured says so explicitly."""
    from sqlalchemy import select as _sel
    from ..services.test_factory.verdict_events import (
        VerdictEventRow, precision_by_dimension)
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        evs = (await session.execute(
            _sel(VerdictEventRow.decision, VerdictEventRow.overall,
                 VerdictEventRow.source).where(
                VerdictEventRow.tenant_id == tenant_id).limit(5000)
        )).all()
    total = len(evs)
    certified = sum(1 for d, _o, _s in evs if d == "certified")
    delivered = [o for d, o, s in evs if s == "delivery-gate"]
    measured = await precision_by_dimension(tenant_id=tenant_id)
    labeled = sum(v.get("labels", 0) for v in measured.values())
    return {
        "report_version": "v1",
        "verdicts_total": total,
        "certified_rate": round(certified / total, 3) if total else None,
        "deliveries_gated": len(delivered),
        "delivery_min_score": min(delivered) if delivered else None,
        "finding_precision_by_dimension": measured,
        "labels_total": labeled,
        "redteam": {"suite": "benchmarks/redteam/run_redteam.py",
                     "status": "run in CI — exit 0 required",
                     "attack_classes": 4},
        "accuracy_benchmark": {"suite": "benchmarks/pages_forms/run_benchmark.py",
                                "note": "headline accuracy counts VERIFIED keys only"},
        "honesty": ("every number here is measured from stored events/labels; "
                    "dimensions without labels report counts, never precision"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Execution Evidence Report — the audit-grade Certificate of Execution
# (QECentral/docs/EXECUTION_EVIDENCE_REPORT_SPEC.md, Phase R1)
# ═══════════════════════════════════════════════════════════════════════════

#: Async + cached (AC-19): report assembly never blocks run completion, and a
#: reviewer refreshing the page does not re-scan every step row. Keyed by
#: (tenant, artifact, run, depth); short TTL so a fresh ingest shows up quickly.
_REPORT_CACHE: dict[tuple, tuple[float, dict]] = {}
_REPORT_CACHE_TTL_S = 45.0
_REPORT_CACHE_MAX = 64


def _report_cache_get(key: tuple) -> dict | None:
    hit = _REPORT_CACHE.get(key)
    if not hit:
        return None
    stamped, payload = hit
    if (_loop_now() - stamped) > _REPORT_CACHE_TTL_S:
        _REPORT_CACHE.pop(key, None)
        return None
    return payload


def _report_cache_put(key: tuple, payload: dict) -> None:
    if len(_REPORT_CACHE) >= _REPORT_CACHE_MAX:
        _REPORT_CACHE.clear()          # tiny cache; wholesale reset is fine
    _REPORT_CACHE[key] = (_loop_now(), payload)


async def _assemble_report(request: Request, artifact_id: str, tenant_id: str,
                           run_id: str | None, include_steps: bool) -> dict:
    key = (tenant_id, artifact_id, run_id or "", bool(include_steps))
    cached = _report_cache_get(key)
    if cached is not None:
        return cached
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        report = await evidence_report.build_report(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            run_id=run_id, include_steps=include_steps,
        )
    _report_cache_put(key, report)
    return report


@router.get("/api/v1/test-factory/{artifact_id}/report")
async def execution_evidence_report(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str | None = Query(None, max_length=64,
                               description="specific run; default = latest non-diagnosis run"),
    include_steps: bool = Query(True, description="include per-step detail"),
    snapshot: bool = Query(False,
                           description="read the FROZEN report for this run instead of recomputing"),
    user: dict = Depends(get_current_user),
):
    """The Execution Evidence Report as JSON: Crawl → Flow → Case → Step with
    the spec's status state machine applied, opened by the Trust Block.

    Read-only, ZERO LLM, deterministic. Every number is a count of stored rows;
    every failure sentence is an Attribution Engine verdict that quotes the
    evidence it matched. Skipped steps are NEVER counted as passes.
    """
    tenant_id = user["tenant_id"]
    rid = (run_id or "").strip() or None
    if snapshot:
        if not rid:
            raise HTTPException(status_code=422,
                                detail="run_id is required to read a frozen snapshot")
        async with tenant_scoped_session(tenant_id) as session:
            await _require_artifact(session, artifact_id, tenant_id)
            frozen = await report_store.get_snapshot(
                session, tenant_id=tenant_id, run_id=rid)
        if frozen is None:
            raise HTTPException(status_code=404,
                                detail="no frozen report for that run (it may predate snapshotting)")
        return frozen
    live = await _assemble_report(request, artifact_id, tenant_id, rid, include_steps)
    return {**live, "source": "live"}


@router.get("/api/v1/test-factory/{artifact_id}/report/runs")
async def execution_report_runs(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    limit: int = Query(25, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Recent runs this report can be pointed at, newest first.

    The report DEFAULTS to the newest run, which is honest but can be a
    one-case ad-hoc run — so a reader needs to see what else exists rather than
    conclude the suite shrank. Each entry carries its own counts, so choosing a
    run is an informed choice and not a hunt for the greenest one.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        rows = (await session.execute(
            select(E2ETestRunRow)
            .where(E2ETestRunRow.artifact_id == artifact_id,
                   E2ETestRunRow.tenant_id == tenant_id)
            .order_by(E2ETestRunRow.started_at.desc())
            .limit(limit)
        )).scalars().all()
    runs = []
    for r in rows:
        total = int(getattr(r, "total_steps", 0) or 0)
        passed = int(getattr(r, "passed_steps", 0) or 0)
        runs.append({
            "run_id": r.run_id,
            "environment": getattr(r, "environment", "") or "",
            "status": getattr(r, "status", "") or "",
            "started_at": (getattr(r, "started_at", None).isoformat()
                           if getattr(r, "started_at", None) else None),
            "total_steps": total,
            "passed_steps": passed,
            "failed_steps": int(getattr(r, "failed_steps", 0) or 0),
            "skipped_steps": int(getattr(r, "skipped_steps", 0) or 0),
            "is_certification": (getattr(r, "environment", "") == "certification"),
        })
    return {
        "artifact_id": artifact_id,
        "runs": runs,
        "default": runs[0]["run_id"] if runs else None,
        "note": ("The report defaults to the newest non-diagnosis run. Pick a "
                 "run explicitly with ?run_id=… — every entry shows its own "
                 "step counts so the choice is informed, not a search for the "
                 "most flattering one."),
    }


@router.get("/api/v1/test-factory/{artifact_id}/report/snapshots")
async def report_snapshots(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """Frozen reports (AC-1) — one per ingested run, each with its own chain root.

    A snapshot is the account of a run AS IT STOOD when the run landed. The live
    report is recomputed from current data, so the two can legitimately differ
    after a regenerate — the API says which one you are reading rather than
    quietly reconciling them.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        rows = await report_store.list_snapshots(
            session, tenant_id=tenant_id, artifact_id=artifact_id, limit=limit)
    return {
        "artifact_id": artifact_id, "snapshots": rows, "count": len(rows),
        "note": ("Each snapshot was frozen when its run was ingested and carries a "
                 "SHA-256 chain root, so a stored report can itself be checked for "
                 "tampering. Read one with ?snapshot=true on the report endpoint."),
    }


@router.get("/api/v1/test-factory/{artifact_id}/report.html")
async def execution_evidence_report_html(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str | None = Query(None, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The same report as ONE self-contained, offline-openable HTML document
    (no CDN, no external assets) — the artifact a reviewer or auditor keeps."""
    report = await _assemble_report(request, artifact_id, user["tenant_id"],
                                    (run_id or "").strip() or None, True)
    return Response(content=report_html.render_html(report),
                    media_type="text/html; charset=utf-8")


# ── Phase R3: audit-grade export + review workflow (spec §2.16-§2.18) ────────

def _export_role_ok(user: dict) -> bool:
    """Exporting evidence is an EGRESS event (screenshots, traces, network
    detail leave the platform), so it needs a write-capable role — a viewer can
    read the report in-product but cannot package it out."""
    return (user.get("role") or "viewer").lower() in ("admin", "manager", "editor")


async def _audit_report_event(*, tenant_id: str, artifact_id: str, actor: str,
                              event_type: str, detail: str,
                              scenario_id: str = "", step_number: int = 0,
                              verdict: str = "", details: dict | None = None) -> None:
    """Append a report-lifecycle event to the tenant's Part-11 hash chain.

    Best-effort by design: an audit-write failure must not corrupt the caller's
    response, but it is logged at WARNING so the gap is VISIBLE in the trail
    rather than invisible."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            await heal_evidence.record_heal_event(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                event_type=event_type, actor=actor or "unknown",
                scenario_id=(scenario_id or "")[:100], step_number=int(step_number or 0),
                engine_verdict=(verdict or "")[:40],
                verified_green=False, reason_for_change=(detail or "")[:500],
                details=dict(details or {}),
            )
            await session.commit()
    except Exception as exc:
        _logger.warning("test_factory.report_audit_failed artifact=%s type=%s err=%s",
                        artifact_id, event_type, str(exc)[:200])


@router.get("/api/v1/test-factory/{artifact_id}/report.zip")
async def execution_evidence_export(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str | None = Query(None, max_length=64),
    mask_all_inputs: bool = Query(False,
                                  description="also mask every recorded input value"),
    user: dict = Depends(get_current_user),
):
    """The complete evidence package: report.html + report.json + verdict.json +
    manifest.json (SHA-256 chain) + an offline verifier script.

    Governed egress: write-role only, credential-shaped values always redacted,
    watermarked with who exported it and when, and the export itself recorded as
    an audit event on the tenant's hash chain.
    """
    if not _export_role_ok(user):
        raise HTTPException(
            status_code=403,
            detail="Exporting evidence requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    rid = (run_id or "").strip() or None
    report = await _assemble_report(request, artifact_id, tenant_id, rid, True)
    html = report_html.render_html(report)
    actor = user.get("email") or user.get("user_id") or "unknown"
    blob, manifest = report_export.build_zip(
        report=report, html=html, exported_by=actor, artifact_id=artifact_id,
        run_id=(report.get("run") or {}).get("run_id", "") or "",
        mask_all_inputs=bool(mask_all_inputs),
    )
    if not manifest.get("signed"):
        # The package still says signed=false honestly, but an operator would
        # not notice a signing key that vanished (e.g. a container recreate
        # dropping a key stored outside a mounted volume). Say so, loudly, in
        # the logs — a silent downgrade from tamper-PROOF to tamper-EVIDENT is
        # exactly the kind of quiet regression this product refuses to allow.
        _logger.warning(
            "test_factory.evidence_export_UNSIGNED artifact=%s key_source=%s — "
            "packages are tamper-EVIDENT only; provision "
            "NEXUS_EVIDENCE_SIGNING_KEY_FILE (see docs/EVIDENCE_SIGNING_AND_RETENTION.md)",
            artifact_id, evidence_manifest.signing_key_source())
    await _audit_report_event(
        tenant_id=tenant_id, artifact_id=artifact_id, actor=actor,
        event_type="evidence_exported",
        detail=(f"chain_root={manifest.get('chain_root')} "
                f"signed={manifest.get('signed')} "
                f"files={manifest.get('file_count')} "
                f"run={(report.get('run') or {}).get('run_id', '')}"))
    fname = f"evidence-{artifact_id[:12]}-{(manifest.get('chain_root') or '')[:8]}.zip"
    return Response(
        content=blob, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"',
                 "X-Nexus-Chain-Root": str(manifest.get("chain_root") or ""),
                 "X-Nexus-Signed": "true" if manifest.get("signed") else "false"})


@router.get("/api/v1/test-factory/{artifact_id}/report/verdict.json")
async def execution_verdict_json(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str | None = Query(None, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Machine-readable verdict for CI gates and dashboards — the SAME truth the
    humans read, carrying what JUnit XML structurally cannot (attribution class,
    evidence class, defect signatures, certification state)."""
    report = await _assemble_report(request, artifact_id, user["tenant_id"],
                                    (run_id or "").strip() or None, True)
    return report_export.build_verdict_json(report)


class _ReviewDispositionBody(BaseModel):
    """§2.18 — Needs Review is a WORKFLOW, not a label. A disposition records
    WHO decided WHAT and why, on the tamper-evident chain."""

    scenario_id: str = Field(..., min_length=1, max_length=64)
    step_number: int = Field(0, ge=0, le=10000)
    disposition: str = Field(..., description="confirm_defect | reclassify | retest | dismiss")
    reason: str = Field(..., min_length=3, max_length=500)
    reclassify_to: str | None = Field(None, max_length=40)
    assignee: str | None = Field(None, max_length=200,
                                 description="who owns this item (a queue, not a label)")
    defect_signature: str | None = Field(None, max_length=64)
    signature_name: str | None = Field(
        None, max_length=200,
        description="typed full name = electronic signature (regulated tenants)")


_ALLOWED_DISPOSITIONS = ("confirm_defect", "reclassify", "retest", "dismiss")

#: E-signature method (spec §6 decision 3), per deployment.
#:   typed_name  — the reviewer types their full name (default; simple, and
#:                 honest about being a single factor);
#:   sso_session — the signature is the IdP-authenticated session itself: we
#:                 record sub / email / issuer / auth-time from the validated
#:                 token, which is a stronger identity claim than free text
#:                 because the reviewer cannot type someone else's name;
#:   both        — require BOTH (closest to a 21 CFR Part 11 two-component
#:                 signature: an authenticated identity plus a deliberate act).
_ESIGN_METHOD = (os.getenv("NEXUS_ESIGN_METHOD", "") or "typed_name").strip().lower()
_ESIGN_METHODS = ("typed_name", "sso_session", "both")


#: Subjects that are OUR OWN machinery, never a signing human. An SSO signature
#: that a service token could satisfy would be a signature in name only.
_SERVICE_SUBJECT_PREFIXES = ("svc-", "platform-", "nexus-", "qe-central")


def _esign_evidence(user: dict, typed_name: str) -> dict:
    """What we can HONESTLY assert about who signed, and how.

    The SSO component requires a REAL identity-provider assertion: an `iss`
    claim, a subject that is not one of our own service principals, and an
    email. Our internally minted tokens have none of that, so they can never
    satisfy an SSO signature — which was the whole point of offering the mode.

    Fails CLOSED: when the configured method cannot be satisfied the disposition
    is recorded as UNSIGNED with the reason, never upgraded to a sign-off just
    because the request happened to carry a valid token.
    """
    method = _ESIGN_METHOD if _ESIGN_METHOD in _ESIGN_METHODS else "typed_name"
    typed = (typed_name or "").strip()
    sub = str(user.get("user_id") or "")
    email = str(user.get("email") or "")
    iss = str(user.get("iss") or "")
    auth_time = user.get("auth_time") or ""

    has_typed = bool(typed)
    is_service = (not sub or sub == "anonymous"
                  or any(sub.lower().startswith(p) for p in _SERVICE_SUBJECT_PREFIXES))
    # A real IdP assertion needs an issuer AND a human subject AND an email.
    has_sso = bool(iss) and not is_service and bool(email)

    if method == "typed_name":
        signed, need = has_typed, "a typed full name"
    elif method == "sso_session":
        signed, need = has_sso, "an identity-provider authenticated session"
    else:
        signed, need = (has_typed and has_sso), "BOTH a typed full name and an IdP session"

    reasons = []
    if not signed:
        if not has_typed and method in ("typed_name", "both"):
            reasons.append("no full name was typed")
        if not has_sso and method in ("sso_session", "both"):
            if not iss:
                reasons.append("the token carries no issuer (iss) — not an IdP session")
            elif is_service:
                reasons.append(f"subject {sub!r} is an internal service principal, not a person")
            elif not email:
                reasons.append("the token carries no email to identify the signer")
    return {
        "method": method,
        "electronically_signed": bool(signed),
        "requires": need,
        "typed_name": typed,
        "sso_subject": sub if has_sso else "",
        "sso_email": email if has_sso else "",
        "sso_issuer": iss if has_sso else "",
        "sso_auth_time": auth_time if has_sso else "",
        "components": [c for c, ok in (("typed_name", has_typed),
                                       ("sso_session", has_sso)) if ok],
        "unsigned_reasons": reasons,
        "note": ("A signature is asserted only when this deployment's configured "
                 "method is actually satisfied. An unmet requirement is recorded "
                 "as UNSIGNED with the reason — never upgraded to a sign-off "
                 "because a request happened to be authenticated. An internally "
                 "minted service token can never satisfy the SSO component."),
    }


@router.post("/api/v1/test-factory/{artifact_id}/report/review")
async def record_review_disposition(
    body: _ReviewDispositionBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Record a human disposition on a Needs-Review (or AI-suggested) finding.

    This closes the D3 loop: the machine SUGGESTS, a named human ASSERTS, and
    the transition is written to the tenant's Part-11 hash chain so the report
    can always answer "who signed off?".
    """
    if not _export_role_ok(user):
        raise HTTPException(
            status_code=403,
            detail="Recording a disposition requires an editor, manager or admin role.")
    if body.disposition not in _ALLOWED_DISPOSITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"disposition must be one of {', '.join(_ALLOWED_DISPOSITIONS)}")
    tenant_id = user["tenant_id"]
    actor = user.get("email") or user.get("user_id") or "unknown"
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
    detail = (f"disposition={body.disposition} scenario={body.scenario_id} "
              f"step={body.step_number} reclassify_to={body.reclassify_to or '-'} "
              f"assignee={body.assignee or '-'} "
              f"signed_by={body.signature_name or '(unsigned)'} :: {body.reason}")
    await _audit_report_event(
        tenant_id=tenant_id, artifact_id=artifact_id, actor=actor,
        event_type="review_disposition", detail=detail,
        scenario_id=body.scenario_id, step_number=body.step_number,
        verdict=body.disposition,
        details={
            "disposition": body.disposition,
            "assignee": (body.assignee or "").strip(),
            "reclassify_to": (body.reclassify_to or "").strip(),
            "defect_signature": (body.defect_signature or "").strip(),
            "reason": body.reason,
            # We record WHETHER it was signed and by what method — never a
            # claim that an unsigned disposition was signed.
            **{"signature": _esign_evidence(user, body.signature_name or "")},
            "electronically_signed": _esign_evidence(
                user, body.signature_name or "")["electronically_signed"],
            "signature_name": (body.signature_name or "").strip(),
        })
    return {"recorded": True, "disposition": body.disposition,
            "scenario_id": body.scenario_id, "step_number": body.step_number,
            "assignee": body.assignee or "", "actor": actor,
            "signature": _esign_evidence(user, body.signature_name or ""),
            "electronically_signed": _esign_evidence(
                user, body.signature_name or "")["electronically_signed"],
            "note": ("Recorded on the tenant's tamper-evident chain. An unsigned "
                     "disposition is still recorded — it is simply marked unsigned, "
                     "never presented as a signed one.")}


@router.get("/api/v1/test-factory/{artifact_id}/report/review-queue")
async def report_review_queue(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    include_resolved: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    """§2.18 — Needs Review as a QUEUE, not a label.

    Every finding that needs a human owner, joined with whatever disposition has
    already been recorded on the tamper-evident chain. An item is OPEN until a
    named human dispositions it, so "who signed off?" is always answerable — and
    an unsigned disposition is reported as unsigned rather than quietly counted
    as a sign-off.
    """
    tenant_id = user["tenant_id"]
    report = await _assemble_report(request, artifact_id, tenant_id, None, True)
    defects = ((report.get("defects") or {}).get("defects") or [])

    # Dispositions live on the audit chain; read them back structurally.
    latest: dict[tuple, dict] = {}
    try:
        async with tenant_scoped_session(tenant_id) as session:
            events = await heal_evidence.list_heal_events(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
        for ev in (events.get("events") or []):
            if ev.get("event_type") != "review_disposition":
                continue
            d = dict(ev.get("details") or {})
            key = (str(ev.get("scenario_id") or ""), int(ev.get("step_number") or 0))
            latest[key] = {          # ordered oldest→newest, so last wins
                "disposition": d.get("disposition") or ev.get("engine_verdict") or "",
                "assignee": d.get("assignee") or "",
                "reclassify_to": d.get("reclassify_to") or "",
                "reason": d.get("reason") or ev.get("reason_for_change") or "",
                "electronically_signed": bool(d.get("electronically_signed")),
                "signature_name": d.get("signature_name") or "",
                "decided_by": ev.get("actor") or "",
                "decided_at": ev.get("created_at"),
                "chain_ok": ev.get("chain_ok"),
            }
    except Exception as exc:
        _logger.warning("test_factory.review_queue_dispositions_failed err=%s", str(exc)[:200])

    items, resolved = [], []
    for d in defects:
        key = (str(d.get("scenario_id") or ""), int(d.get("step_number") or 0))
        disp = latest.get(key)
        item = {
            "defect_signature": d.get("signature"),
            "scenario_id": d.get("scenario_id"),
            "case_name": d.get("case_name"),
            "step_number": d.get("step_number"),
            "class": d.get("display_status"),
            "category": d.get("category"),
            "cause": d.get("cause"),
            "severity": d.get("severity"),
            "priority": d.get("priority"),
            "suggested_component": d.get("suggested_component"),
            "suggested_fix_area": d.get("suggested_fix_area"),
            "assessment_reasons": d.get("assessment_reasons"),
            "blast_radius": d.get("blast_radius"),
            "occurrence_count": d.get("occurrence_count"),
            "lifecycle": d.get("lifecycle"),
            "first_seen": d.get("first_seen"),
            "last_seen": d.get("last_seen"),
            "fingerprint": d.get("fingerprint"),
            "disposition": disp,
            "state": "resolved" if disp else "open",
        }
        (resolved if disp else items).append(item)

    # Worst first: unattributed items ('unset') sit at the top because they are
    # precisely the ones a machine could not decide.
    rank = {"unset": 0, "critical": 1, "high": 2, "medium": 3, "low": 4}
    items.sort(key=lambda i: (rank.get(i.get("severity"), 5),
                              -(i.get("occurrence_count") or 0)))

    return {
        "artifact_id": artifact_id,
        "open": items,
        "open_count": len(items),
        "resolved": resolved if include_resolved else [],
        "resolved_count": len(resolved),
        "dispositions": sorted(_ALLOWED_DISPOSITIONS),
        "note": ("An item stays OPEN until a named human dispositions it. "
                 "Severity 'unset' means the attribution ladder could not prove a "
                 "cause — those are listed first because they are exactly the ones "
                 "a machine must not decide. Dispositions are recorded on the "
                 "tenant's tamper-evident chain; an unsigned one is reported as "
                 "unsigned, never counted as a sign-off."),
    }


@router.post("/api/v1/test-factory/{artifact_id}/evidence/retention")
async def evidence_retention_pass(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    apply: bool = Query(False, description="actually reclaim; default is a dry run"),
    limit: int = Query(500, ge=1, le=5000),
    only_ids: str | None = Query(None, max_length=4000,
                                 description="comma-separated artifact ids for a TARGETED reclaim"),
    user: dict = Depends(get_current_user),
):
    """Reclaim evidence bytes past their retention window (spec §6 decision 1).

    DRY RUN by default: an irreversible sweep must be an explicit act, never the
    accidental result of calling an endpoint. Reclaimed artifacts are
    TOMBSTONED, not deleted — the row survives with the SHA-256, the original
    size and the reclaim date, so an exported copy can still be proven genuine.
    Run/step rows and verdicts are never touched.
    """
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403,
                            detail="Evidence retention requires an admin or manager role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        ids = [i.strip() for i in (only_ids or "").split(",") if i.strip()] or None
        result = await evidence_retention.apply_retention(
            session, tenant_id=tenant_id, dry_run=not apply, limit=limit,
            only_ids=ids)
        if apply:
            await session.commit()
    if apply:
        await _audit_report_event(
            tenant_id=tenant_id, artifact_id=artifact_id,
            actor=user.get("email") or user.get("user_id") or "unknown",
            event_type="evidence_retention",
            detail=(f"reclaimed={result.get('candidates')} "
                    f"bytes={result.get('reclaimable_bytes')} (tombstoned, not deleted)"))
    return result


@router.get("/api/v1/test-factory/{artifact_id}/report/audit-trail")
async def report_audit_trail(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The artifact's audit trail plus a LIVE verification of the hash chain —
    so "immutable" is a checkable claim, not an adjective."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
    # SEPARATE sessions per query on purpose: a failing statement aborts its
    # whole Postgres transaction, so sharing one session made a missing-table
    # error on the first call cascade into a bogus "chain verification
    # unavailable" on the second. Each answer now stands on its own evidence.
    try:
        async with tenant_scoped_session(tenant_id) as session:
            events = await heal_evidence.list_heal_events(
                session, tenant_id=tenant_id, artifact_id=artifact_id)
    except Exception as exc:
        _logger.warning("test_factory.audit_list_failed err=%s", str(exc)[:200])
        events = {"events": [], "error": "audit trail unavailable"}
    try:
        async with tenant_scoped_session(tenant_id) as session:
            chain = await heal_evidence.verify_chain(session, tenant_id=tenant_id)
    except Exception as exc:
        _logger.warning("test_factory.audit_verify_failed err=%s", str(exc)[:200])
        chain = {"verified": None, "error": "chain verification unavailable"}
    return {"artifact_id": artifact_id, "audit": events, "chain_verification": chain,
            "signing_enabled": evidence_manifest.signing_enabled(),
            "signing_key_source": evidence_manifest.signing_key_source(),
            "esign_method": _ESIGN_METHOD,
            "retention_windows_days": evidence_retention.DEFAULT_WINDOWS}


# ── Phase R4: reach formats, analytics, filters (spec §2.13-§2.16) ──────────

_REPORT_FORMATS = ("csv", "junit", "xlsx", "pdf")


async def _filtered_report(request: Request, artifact_id: str, tenant_id: str,
                           run_id: str | None, status: str | None, flow: str | None,
                           test_type: str | None, search: str | None) -> dict:
    report = await _assemble_report(request, artifact_id, tenant_id, run_id, True)
    if not any((status, flow, test_type, search)):
        return report
    statuses = {s.strip() for s in (status or "").split(",") if s.strip()} or None
    return report_formats.filter_report(
        report, statuses=statuses, flow=flow, test_type=test_type, search=search)


@router.get("/api/v1/test-factory/{artifact_id}/report/analytics")
async def execution_report_analytics(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    run_id: str | None = Query(None, max_length=64),
    user: dict = Depends(get_current_user),
):
    """§2.14 dashboard metrics. Rates are over EXECUTED cases only — a case that
    never ran is reported under coverage, never folded into a pass rate. The
    distribution is evidence CLASS (measured), not a model confidence score."""
    report = await _assemble_report(request, artifact_id, user["tenant_id"],
                                    (run_id or "").strip() or None, True)
    return report_formats.build_analytics(report)


@router.get("/api/v1/test-factory/{artifact_id}/report/export.{fmt}")
async def execution_report_format(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    fmt: str = PathParam(..., min_length=2, max_length=8),
    run_id: str | None = Query(None, max_length=64),
    status: str | None = Query(None, description="comma-separated display statuses"),
    flow: str | None = Query(None, max_length=120),
    test_type: str | None = Query(None, max_length=40),
    search: str | None = Query(None, max_length=200),
    user: dict = Depends(get_current_user),
):
    """Render the report as csv | junit | xlsx | pdf, with the §2.13 filters.

    Same governance as the ZIP: these carry evidence out of the platform, so a
    write-capable role is required, credential-shaped values are redacted, and
    the export is recorded on the tenant's audit chain. JUnit is LOSSY by
    construction — the mapping keeps `<failure>` for application defects and
    `<error>` for our own automation faults so a CI gate never mistakes one for
    the other.
    """
    f = (fmt or "").strip().lower()
    if f not in _REPORT_FORMATS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown format '{fmt}' (use: {', '.join(_REPORT_FORMATS)})")
    if not _export_role_ok(user):
        raise HTTPException(
            status_code=403,
            detail="Exporting evidence requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    report = await _filtered_report(request, artifact_id, tenant_id,
                                    (run_id or "").strip() or None,
                                    status, flow, test_type, search)
    # Redact before ANY format leaves the platform — the ZIP is not a special case.
    report, _stats = report_export.redact_report(report)
    actor = user.get("email") or user.get("user_id") or "unknown"
    stem = f"evidence-{artifact_id[:12]}"

    if f == "csv":
        blob, media, name = (report_formats.to_csv(report).encode("utf-8"),
                             "text/csv; charset=utf-8", f"{stem}.csv")
    elif f == "junit":
        blob, media, name = (report_formats.to_junit_xml(report).encode("utf-8"),
                             "application/xml; charset=utf-8", f"{stem}-junit.xml")
    elif f == "xlsx":
        try:
            blob = report_formats.to_xlsx(report)
        except ImportError:
            raise HTTPException(status_code=503,
                                detail="XLSX export unavailable (openpyxl not installed)")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        name = f"{stem}.xlsx"
    else:
        blob, media, name = (report_formats.to_pdf(report), "application/pdf",
                             f"{stem}.pdf")

    await _audit_report_event(
        tenant_id=tenant_id, artifact_id=artifact_id, actor=actor,
        event_type="evidence_exported",
        detail=(f"format={f} bytes={len(blob)} filters=status:{status} "
                f"flow:{flow} type:{test_type} q:{'yes' if search else 'no'}"))
    return Response(content=blob, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ═══════════════════════════════════════════════════════════════════════════
# Persona × Environment Matrix — registries (P0). RUN = Suite × Env × Persona.
# ═══════════════════════════════════════════════════════════════════════════

def _persona_write_ok(user: dict) -> bool:
    return (user.get("role") or "viewer").lower() in ("admin", "manager", "editor")


async def _persona_audit(*, tenant_id: str, artifact_id: str, actor: str,
                         event_type: str, detail: str) -> None:
    try:
        async with tenant_scoped_session(tenant_id) as session:
            await heal_evidence.record_heal_event(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                event_type=event_type, actor=actor or "unknown", scenario_id="",
                step_number=0, engine_verdict="", verified_green=False,
                reason_for_change=(detail or "")[:500])
            await session.commit()
    except Exception as exc:
        _logger.warning("persona.audit_failed artifact=%s type=%s err=%s",
                        artifact_id, event_type, str(exc)[:200])


class _PersonaBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    traits: list[str] = Field(default_factory=list)
    behavior_class: str = Field("", max_length=64)


class _RecipeBody(BaseModel):
    steps: list[dict] = Field(..., min_length=1)
    slots: list[dict] = Field(default_factory=list)
    source: str = Field("recorded", max_length=24)
    # Reuse keys. Without these a recipe stored through this endpoint can never be
    # PROPOSED to the next app on the same host — the fingerprint and the domain are
    # what make "already recorded, just add your credentials" possible.
    login_type_key: str = Field("", max_length=64)
    login_domain: str = Field("", max_length=253)


class _CredentialBody(BaseModel):
    slot_values: dict[str, str] = Field(..., description="slot name -> secret value")


class _ExpectedValuesBody(BaseModel):
    values: dict[str, str] = Field(..., description="value_key -> expected value")


@router.get("/api/v1/test-factory/{artifact_id}/personas")
async def list_personas_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    traits: str | None = Query(None, description="comma-separated trait filter"),
    user: dict = Depends(get_current_user),
):
    """Personas defined for this artifact (+ a synthetic 'default' persona-0 when
    a legacy form-login exists, so every current client already has one)."""
    tenant_id = user["tenant_id"]
    tlist = [t.strip() for t in (traits or "").split(",") if t.strip()] or None
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        personas = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id, traits=tlist)
    return {"artifact_id": artifact_id, "personas": personas, "count": len(personas)}


@router.post("/api/v1/test-factory/{artifact_id}/personas")
async def create_persona_endpoint(
    body: _PersonaBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    if not _persona_write_ok(user):
        raise HTTPException(403, "Defining a persona requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.save_persona(
            session, tenant_id=tenant_id, artifact_id=artifact_id, name=body.name,
            description=body.description, traits=body.traits,
            behavior_class=body.behavior_class)
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="persona_defined", detail=f"name={body.name}")
    return out


class _MemberFromLoginBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    environment_id: str = Field(..., min_length=1, max_length=64)
    slot_values: dict = Field(default_factory=dict)
    behavior_class: str = Field("", max_length=64)
    traits: list[str] = Field(default_factory=list)
    description: str = Field("", max_length=2000)


@router.post("/api/v1/test-factory/{artifact_id}/personas/from-recorded-login")
async def member_from_recorded_login_endpoint(
    body: _MemberFromLoginBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Turn the login just recorded into a REUSABLE member.

    Record + Run authenticates with a captured session. A session is perishable —
    it expires, and nothing can replay it. So the moment the operator closes the
    tab, tomorrow's scheduled run has no way to be that user again. This is the
    bridge: it pairs the recipe the recording already derived (the choreography,
    which does NOT expire) with a card (the values, encrypted) so the same login
    can be performed again, unattended, for as long as the credentials are valid.

    The recording deliberately captures identifiers only, never a credential value
    — so the values cannot be harvested from it and the operator supplies them
    once here. The recipe is what makes that a short, correct form instead of
    hand-authoring: the fields asked for are the fields the application's own
    login asked for.

    The new member is NOT proven. The recording proved a *session*; it did not
    prove that these values, typed into this recipe, log in. Only a replay proves
    that, so the card is stored ``unverified`` and Verify is left to do its job."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Saving a member requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(503, "encryption unavailable — refusing to store credentials in plaintext")
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(422, "a member needs a name")

    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)

        # A card is values FOR a recipe. Without the recipe there are no slots, so
        # any names supplied would be a guess — and a guessed name silently skips
        # the login at run time.
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)

        # save_persona upserts by name, and the upsert does not touch ``status`` —
        # so reusing a RETIRED member's name would attach the card to a member that
        # every dispatch then refuses, after telling the operator it saved fine.
        # Retirement is a decision; silently reviving it would defeat it.
        existing = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id, include_retired=True)
        clash = next((p for p in existing
                      if str(p.get("name") or "").strip().lower() == name.lower()
                      and str(p.get("status") or "active").lower() != "active"), None)
        if clash:
            raise HTTPException(status_code=409, detail={
                "error": "a retired member already uses that name",
                "reason": "name_retired",
                "persona_id": clash.get("persona_id", ""),
                "note": ("A member with this name was retired, and saving over it would "
                         "produce a member that looks fine here but is refused by every "
                         "run. Choose a different name, or un-retire that member."),
            })

        try:
            # Refuse BEFORE creating anything, so a rejected card cannot leave a
            # nameless member behind with no way to log in.
            card_contract.check_card(recipe=recipe, slot_values=body.slot_values)
        except card_contract.CardContractError as exc:
            raise HTTPException(status_code=422,
                                detail={"error": str(exc), **exc.detail})

        p = await persona_store.save_persona(
            session, tenant_id=tenant_id, artifact_id=artifact_id, name=name,
            description=body.description, traits=body.traits,
            behavior_class=body.behavior_class)
        try:
            card = await persona_store.save_persona_credential(
                session, envelope=envelope, tenant_id=tenant_id,
                persona_id=p["persona_id"], environment_id=body.environment_id,
                slot_values=body.slot_values,
                login_type_key=str((recipe or {}).get("login_type_key") or ""),
                recipe_version=int((recipe or {}).get("version") or 0))
        except (ValueError, RuntimeError) as exc:
            # No commit — the member goes with the card it could not be given.
            raise HTTPException(status_code=422, detail=str(exc))
        await session.commit()

    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="member_saved_from_recorded_login",
                         detail=f"name={name} env={body.environment_id}")
    return {
        "status": "saved",
        "persona_id": p["persona_id"],
        "name": name,
        "environment_id": body.environment_id,
        # Names only. The values just travelled one way.
        "slot_names": card.get("slot_names", []),
        "login_type_key": card.get("login_type_key", ""),
        "recipe_version": card.get("recipe_version", 0),
        "verify_status": "unverified",
        "proven": False,
        "note": ("Saved. This member can now be picked under 'Run as' and used by a "
                 "schedule. It is NOT proven yet: the recording proved a session, not "
                 "this card. Press Verify to replay the recorded login with these "
                 "values and find out before a schedule does."),
    }


@router.post("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/retire")
async def retire_persona_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    if not _persona_write_ok(user):
        raise HTTPException(403, "Retiring a persona requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        # F5: retirement has to STOP things, not merely hide the member. Runs in
        # flight are authenticating as an account that has just been decommissioned;
        # letting them finish files evidence under it. And a reservation left held
        # keeps counting against the tenant's concurrency cap until its TTL expires,
        # for a member nobody can select any more.
        in_flight = await persona_store.live_runs_for_persona(
            session, tenant_id=tenant_id, persona_id=persona_id)
        await persona_store.retire_persona(session, tenant_id=tenant_id, persona_id=persona_id)
        released = await persona_store.release_persona_reservations(
            session, tenant_id=tenant_id, persona_id=persona_id)
        await session.commit()
    for rid in in_flight:
        job = _RUNNER_JOBS.get(rid)
        if job is not None:
            job["status"] = "blocked"
            job["blocked_reason"] = "member_identity"
            job["detail"] = ("the member this run authenticates as was retired while "
                             "it was still running; its results are not filed under a "
                             "decommissioned account. This is NOT an application failure.")
            await _persist_job(rid)
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="persona_retired",
                         detail=f"persona={persona_id} in_flight={len(in_flight)} "
                                f"reservations_released={released}")
    return {"retired": True, "persona_id": persona_id,
            "reservations_released": released,
            "runs_blocked": in_flight,
            "note": (("%d run(s) in flight as this member were BLOCKED — their "
                      "results are not filed under a retired account."
                      % len(in_flight)) if in_flight else
                     "No runs were in flight as this member.")}


@router.put("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/credentials/{environment_id}")
async def set_persona_credential_endpoint(
    body: _CredentialBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Store a persona's credential card for one environment (write-only —
    slot NAMES are returned, values never). Secrets are envelope-encrypted.

    The card must be able to FILL the artifact's active login recipe. A card whose
    slots do not match is refused here rather than in the form, because a card is
    also reachable from a script, a bulk import or curl — and a mismatch is silent
    downstream: the compiled login finds the slot missing, skips the whole login,
    the suite runs unauthenticated, and every failure is attributed to the
    application under test."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Storing credentials requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(503, "encryption unavailable — cannot store credentials securely")
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        try:
            card_contract.check_card(recipe=recipe, slot_values=body.slot_values)
        except card_contract.CardContractError as exc:
            raise HTTPException(422, detail={"error": str(exc), **exc.detail})
        try:
            out = await persona_store.save_persona_credential(
                session, envelope=envelope, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id, slot_values=body.slot_values,
                login_type_key=str((recipe or {}).get("login_type_key") or ""),
                recipe_version=int((recipe or {}).get("version") or 0))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc))
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="persona_credential_set",
                         detail=f"persona={persona_id} env={environment_id} slots={out['slot_names']}")
    return {"status": "saved", **out}


@router.get("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/credentials/{environment_id}/status")
async def persona_credential_status_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        st = await persona_store.credential_status(
            session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
    return st


@router.put("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/expected-values/{environment_id}")
async def set_expected_values_endpoint(
    body: _ExpectedValuesBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    if not _persona_write_ok(user):
        raise HTTPException(403, "Setting expected values requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for k, v in body.values.items():
            await persona_store.set_expected_value(
                session, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id, value_key=k, expected_value=v)
        await session.commit()
    return {"status": "saved", "count": len(body.values)}


@router.get("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/expected-values/{environment_id}")
async def get_expected_values_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        vals = await persona_store.get_expected_values(
            session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
    return {"values": vals, "count": len(vals)}


class _CardinalityBody(BaseModel):
    counts: dict[str, int] = Field(..., description="{repeatable_block: count} for this member, e.g. {'beneficiary': 5}")


@router.put("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/cardinality/{environment_id}")
async def set_persona_cardinality_endpoint(
    body: _CardinalityBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Set how many times a repeated block runs for THIS member (5 beneficiaries
    vs 2). One suite serves any family size — the count rides the run and a
    data-driven repeatable step reads it. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Setting cardinality requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.set_persona_cardinality(
            session, tenant_id=tenant_id, persona_id=persona_id,
            environment_id=environment_id, counts=body.counts)
        await session.commit()
    return {"status": "saved", "persona_id": persona_id, "environment_id": environment_id,
            "counts": out}


@router.get("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/repetition-plan/{environment_id}")
async def persona_repetition_plan_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The repeat plan a run applies for this member: {block: count}. Never
    invents a count — an unset block repeats once (the recorded shape)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        counts = await persona_store.get_persona_cardinality(
            session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
    cd = [{"block": b, "count_b": n} for b, n in counts.items()]
    pc = {f"{persona_id}::{b}": n for b, n in counts.items()}
    plan = persona_governance.repetition_plan(cd, pc, persona_id)
    return {"persona_id": persona_id, "environment_id": environment_id,
            "repetition_plan": {p["block"]: p["repeat"] for p in plan}, "detail": plan}


@router.get("/api/v1/test-factory/{artifact_id}/recipes")
async def list_recipes_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        recipes = await persona_store.list_recipes(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    return {"artifact_id": artifact_id, "recipes": recipes, "count": len(recipes)}



# --- FIELD LEARNING (P1/P4/P5) ----------------------------------------------
# The crawl asks a client for what it could not fill. Answering the same question
# on the next crawl means nothing was learned. These endpoints are the memory.


class _FieldAnswerBody(BaseModel):
    """One answer to one residue question, keyed by the field's own signature."""
    signature: str = Field(..., min_length=8, max_length=64)
    value: str = Field(..., min_length=1, max_length=4096)
    semantic_type: str = Field("unknown", max_length=48)
    field_label: str = Field("", max_length=300)
    signature_version: int = Field(1, ge=1, le=99)


class _FieldAnswersBody(BaseModel):
    answers: list[_FieldAnswerBody] = Field(default_factory=list, max_length=200)


class _LearningConsentBody(BaseModel):
    contribute: bool = False
    consume: bool = True


@router.post("/api/v1/test-factory/{artifact_id}/field-memory")
async def remember_field_answers_endpoint(
    body: _FieldAnswersBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Remember what the client just told us, so the next crawl does not ask again.

    Values are envelope-encrypted exactly like a credential card and scoped to this
    tenant — never readable by another client, and never written to the shared
    priors table. What DOES generalise is the shape: that a field with this
    signature is this semantic type. That contribution is opt-in and off by
    default, and is skipped silently when the tenant has not opted in."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Saving test data requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(503, "encryption unavailable - refusing to store test data in plaintext")

    stored, shared, errors = 0, 0, []
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for a in body.answers:
            try:
                await field_learning.remember(
                    session, envelope=envelope, tenant_id=tenant_id,
                    artifact_id=artifact_id, signature=a.signature, value=a.value,
                    semantic_type=a.semantic_type, field_label=a.field_label,
                    signature_version=a.signature_version)
                stored += 1
                # SHAPE ONLY. observe_prior has no parameter a value could travel
                # through, and forces the type into the closed vocabulary.
                if await field_learning.observe_prior(
                        session, tenant_id=tenant_id, signature=a.signature,
                        semantic_type=a.semantic_type,
                        signature_version=a.signature_version):
                    shared += 1
            except (ValueError, RuntimeError) as exc:
                errors.append({"signature": a.signature, "error": str(exc)[:200]})
        await session.commit()
    return {"status": "saved", "remembered": stored, "contributed_shapes": shared,
            "errors": errors,
            "note": ("Remembered for this application only. The next crawl fills these "
                     "without asking. Values are encrypted and never leave this tenant.")}


@router.get("/api/v1/test-factory/{artifact_id}/field-memory")
async def list_field_memory_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """What we remember for this application - labels and outcomes, never values."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        items = await field_learning.list_memories(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    return {"artifact_id": artifact_id, "count": len(items), "memories": items}


@router.delete("/api/v1/test-factory/{artifact_id}/field-memory")
async def forget_field_memory_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    signature: str = Query("", max_length=64),
    user: dict = Depends(get_current_user),
):
    """Forget. A client must be able to make us stop holding their data."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Deleting test data requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        removed = await field_learning.forget(
            session, tenant_id=tenant_id, artifact_id=artifact_id, signature=signature)
        await session.commit()
    return {"status": "forgotten", "removed": removed}


@router.get("/api/v1/test-factory/{artifact_id}/field-resolution")
async def field_resolution_endpoint(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Everything a crawl of this application needs in order not to ask twice.

    Called by the orchestrator immediately before dispatch. Returns the tenant's own
    remembered values (decrypted for this dispatch only) AND the pooled, value-free
    priors. They are separate keys because they carry entirely different risk and
    the caller must never conflate them."""
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        recalled = await field_learning.recall(
            session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id)
        priors = await field_learning.load_priors(session, tenant_id=tenant_id)
        consent = await field_learning.get_consent(session, tenant_id=tenant_id)
    # Refuse to hand out priors that are not value-free rather than distribute them:
    # the check costs nothing and the failure it catches is unrecoverable.
    if not field_learning.priors_are_value_free(priors):
        _logger.error("test_factory.field_priors.not_value_free artifact=%s", artifact_id)
        priors = {}
    return {"artifact_id": artifact_id, "recalled_values": recalled,
            "field_priors": priors, "consent": consent,
            "identity_seed": f"{tenant_id}::{artifact_id}"}


@router.post("/api/v1/test-factory/{artifact_id}/field-outcome")
async def field_outcome_endpoint(
    body: dict,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """P5 - did the APPLICATION accept the value we remembered?

    A remembered value the app rejects is worse than none, because it looks like an
    answer. Once rejected more often than accepted it stops being offered and the
    field returns to the residue ask. The loop closes on the application's verdict,
    never on our optimism."""
    tenant_id = user["tenant_id"]
    outcomes = body.get("outcomes") if isinstance(body, dict) else None
    if not isinstance(outcomes, list):
        raise HTTPException(422, "outcomes must be a list of {signature, accepted}")
    applied = 0
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for o in outcomes[:500]:
            if not isinstance(o, dict):
                continue
            sig = str(o.get("signature") or "").strip()
            if not sig:
                continue
            accepted = bool(o.get("accepted"))
            await field_learning.record_outcome(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                signature=sig, accepted=accepted)
            await field_learning.observe_prior(
                session, tenant_id=tenant_id, signature=sig,
                semantic_type=str(o.get("semantic_type") or "unknown"),
                accepted=accepted)
            applied += 1
        await session.commit()
    return {"status": "recorded", "applied": applied}


@router.get("/api/v1/test-factory/learning-consent")
async def get_learning_consent_endpoint(user: dict = Depends(get_current_user)):
    """Whether this tenant contributes field SHAPES to the shared pool.

    Contributing is off until somebody decides, because a regulated client must be
    able to use the product without their field shapes reaching a shared table, and
    the safe default is the one that applies before anyone has chosen."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        return await field_learning.get_consent(session, tenant_id=tenant_id)


@router.post("/api/v1/test-factory/learning-consent")
async def set_learning_consent_endpoint(
    body: _LearningConsentBody,
    user: dict = Depends(get_current_user),
):
    if not _persona_write_ok(user):
        raise HTTPException(403, "Changing the learning consent requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        out = await field_learning.set_consent(
            session, tenant_id=tenant_id, contribute=body.contribute,
            consume=body.consume,
            decided_by=user.get("email") or user.get("user_id") or "?")
        await session.commit()
    return out


@router.get("/api/v1/test-factory/{artifact_id}/login-contract")
async def login_contract_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The fields a credential card for THIS app must supply — derived from the
    recorded login, never from a vocabulary of ours.

    The card editor renders exactly this, so what an operator is asked to fill is
    what the application's own login form asked for, under the app's own labels.
    The same derivation backs the server-side refusal in PUT …/credentials/{env},
    so the form and the contract cannot drift apart.

    ``fields`` is empty and ``reason`` is set when no card can be provisioned yet."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    if not recipe:
        return {"artifact_id": artifact_id, "has_recipe": False, "fields": [],
                "reason": "no_recipe",
                "note": ("No login has been recorded for this application yet. Record "
                         "the login first — the card's fields come from that recording, "
                         "so there is nothing to fill in until it exists.")}
    fields = card_contract.slot_fields(recipe)
    return {
        "artifact_id": artifact_id, "has_recipe": True,
        "recipe_id": recipe.get("recipe_id", ""), "version": recipe.get("version", 0),
        "login_type_key": recipe.get("login_type_key", ""),
        "login_domain": recipe.get("login_domain", ""),
        "source": recipe.get("source", ""), "fields": fields,
        "reason": "" if fields else "recipe_has_no_slots",
        "note": ("" if fields else
                 "The recorded login fills no fields, so it cannot be driven by a "
                 "credential card. Re-record the login."),
    }


@router.post("/api/v1/test-factory/{artifact_id}/recipes")
async def create_recipe_endpoint(
    body: _RecipeBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Store a login recipe (steps + named slots). Usually produced by the crawl;
    this endpoint also allows an explicit re-record."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Storing a recipe requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    # A hand-authored recipe is the one path that can carry a logged-in checkpoint
    # that checks nothing — and that checkpoint is the only thing that can mark a
    # login proven.
    try:
        card_contract.check_recipe_steps(body.steps)
    except card_contract.CardContractError as exc:
        raise HTTPException(422, detail={"error": str(exc), **exc.detail})
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.save_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            steps=body.steps, slots=body.slots, source=body.source,
            login_type_key=body.login_type_key, login_domain=body.login_domain)
        impact = await persona_store.withdraw_card_proofs(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            required_slots=card_contract.required_slots(
                {"steps": body.steps, "slots": body.slots}))
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="login_recipe_saved",
                         detail=f"version={out['version']} "
                                f"cards_broken={len(impact['broken'])}")
    return {"status": "saved", **out, "cards": impact,
            "note": (("%d member card(s) no longer match this login and are BLOCKED "
                      "until re-provisioned." % len(impact["broken"]))
                     if impact["broken"] else
                     ("Existing cards still fit; their earlier proof was withdrawn "
                      "because it was made against the previous version."
                      if impact["unproven"] else ""))}


class _ObsField(BaseModel):
    slot: str
    label: str | None = None
    type: str | None = None


class _RecordObservationBody(BaseModel):
    domain: str = ""
    login_path: str = "/"
    fields: list[_ObsField] = Field(default_factory=list)
    submit: str = ""
    verify_documents: list[dict] | None = None
    home: dict | None = None


class _ReuseCheckBody(BaseModel):
    domain: str = ""
    login_path: str = "/"
    fields: list[_ObsField] = Field(default_factory=list)
    submit: str = ""


@router.post("/api/v1/test-factory/{artifact_id}/recipes/from-observation")
async def record_recipe_from_observation_endpoint(
    body: _RecordObservationBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Record a login recipe from the crawler's OBSERVED login (Phase 3/5): the
    observed fields/submit + any verify-documents interstitial + the Home landing
    become a replayable recipe (Member -> [verify-doc, optional] -> Home oracle),
    stamped with its login_type_key for fleet reuse. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Recording a recipe requires an editor, manager or admin role.")
    built = login_recorder.recipe_from_observed_login({
        "domain": body.domain, "login_path": body.login_path,
        "fields": [{"slot": f.slot, "label": f.label, "type": f.type} for f in body.fields],
        "submit": body.submit,
        "verify_documents": body.verify_documents, "home": body.home,
    })
    if not built["steps"]:
        raise HTTPException(422, "no login steps observed — nothing to record")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.save_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            steps=built["steps"], slots=built["slots"],
            source="crawl_demonstration", login_type_key=built["login_type_key"],
            login_domain=body.domain)
        impact = await persona_store.withdraw_card_proofs(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            required_slots=card_contract.required_slots(built))
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="login_recipe_recorded",
                         detail=f"version={out['version']} key={built['login_type_key']} "
                                f"cards_broken={len(impact['broken'])}")
    return {"status": "recorded", **out, "cards": impact}


class _DraftStartBody(BaseModel):
    """Where the operator wants to begin recording — normally the app's entry."""
    url: str = Field(..., min_length=1, max_length=2000)


@router.post("/api/v1/test-factory/recordings/start")
async def draft_recording_start(
    body: _DraftStartBody,
    user: dict = Depends(get_current_user),
):
    """Begin a recording BEFORE the application exists (onboarding, Access step).

    Deliberately NOT artifact-scoped: a recipe lives against an artifact, and an
    artifact is only minted by the first crawl, so at this point there is nothing
    to attach to. The recording is derived and handed back; persisting it is the
    caller's business, and happens when the app is created."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Recording a login requires an editor, manager or admin role.")
    try:
        res = await runner_client.auth_capture_start(body.url.strip())
    except httpx.HTTPStatusError as exc:
        # PRESERVE the runner's own status. Collapsing everything into 502 turned
        # "the recorder is busy" (409, which the UI recovers from by closing the
        # stale capture and retrying) into "the recorder is unreachable — try
        # again shortly", which is both wrong and unactionable: retrying a BUSY
        # recorder never clears it. Live-observed as a recorder that looked dead
        # for as long as one abandoned capture stayed open.
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail="runner busy — a run or capture is already in progress")
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    pw = (res or {}).get("vnc_password", "") if isinstance(res, dict) else ""
    return {"status": "recording",
            "live_url": _AUTH_LIVE_PATH + (f"&password={pw}" if pw else "")}


@router.get("/api/v1/test-factory/recordings/observation")
async def draft_recording_observation(user: dict = Depends(get_current_user)):
    """What the recording has captured SO FAR — identifiers only, never a value.
    Lets the operator see the fields being picked up while still logged in."""
    try:
        return await runner_client.auth_capture_observation()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")


@router.post("/api/v1/test-factory/recordings/save")
async def draft_recording_save(user: dict = Depends(get_current_user)):
    """Close the recording and return the DRAFT — a login recipe and the
    environment the browser ended up on. Persists nothing.

    A public flow with no login is a legitimate outcome: the environment is still
    returned, and ``usable`` says plainly that no login was seen."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Recording a login requires an editor, manager or admin role.")
    try:
        res = await runner_client.auth_capture_save()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"runner error: {exc}")
    snapshot = (res or {}).get("login_observation")
    state = (res or {}).get("storage_state")
    draft = draft_recording.derive_draft(
        observation_snapshot=snapshot,
        storage_state=state,
        start_url=str((snapshot or {}).get("start_url") or ""))
    # The sign-in VALUES just used, matched to the slots this recording named. They are
    # what turn ONE recording into a login every FUTURE crawl replays instead of a
    # session that expires. Returned to the caller, which hands them to qe-central for
    # envelope encryption — never persisted here, and never logged (the diagnostic
    # below counts them, and never shows one).
    _slot_values = draft_recording.match_slot_values(
        draft.get("login") or {}, (res or {}).get("login_values") or [])
    if _slot_values:
        draft["slot_values"] = _slot_values

    # Shape-only diagnostics. "No session was captured" is otherwise unfalsifiable
    # from the outside: it could mean the runner returned nothing, or a state with
    # no cookies, or an app that keeps its session somewhere storageState cannot
    # see (sessionStorage). NAMES only — never a cookie value. WARNING level
    # because platform-api suppresses INFO.
    _cookies = (state or {}).get("cookies") if isinstance(state, dict) else None
    _origins = (state or {}).get("origins") if isinstance(state, dict) else None
    _names = [str((c or {}).get("name") or "") for c in (_cookies or [])][:25]
    _ls = sum(len((o or {}).get("localStorage") or []) for o in (_origins or []))
    # sessionStorage is carried separately (Playwright's storageState omits it),
    # and leaving it out of this line made a WORKING capture read as an empty one —
    # the diagnostic was blind to the mechanism that had just carried the session.
    _ss = (state or {}).get("__nx_session_storage") if isinstance(state, dict) else None
    _ss_origins = [str((e or {}).get("origin") or "") for e in (_ss or [])][:20]
    # KEY NAMES only, never values — the same policy as cookie_names above. Without
    # them "we carried a session" and "we carried some UI state" look identical, and
    # a crawl that still meets a login wall is unexplainable from the outside.
    _ss_keys: list[str] = []
    for _e in (_ss or []):
        _ss_keys.extend(list(((_e or {}).get("entries") or {}).keys()))
    _ss_keys = _ss_keys[:25]
    _logger.warning(
        "recordings.save state_type=%s cookies=%d origins=%d localStorage_keys=%d "
        "sessionStorage_origins=%d (%s) sessionStorage_keys=%s cookie_names=%s "
        "events=%d login=%s",
        type(state).__name__, len(_cookies or []), len(_origins or []), _ls,
        len(_ss or []), ",".join(_ss_origins) or "-", ",".join(_ss_keys) or "-",
        ",".join(_names) or "-", len((snapshot or {}).get("events") or []),
        bool(draft.get("login")))

    # Tell the caller WHY there is no session, so the UI can say something true
    # rather than just "none".
    draft["session_detail"] = {
        "cookies": len(_cookies or []),
        "origins": len(_origins or []),
        "local_storage_keys": _ls,
        "session_storage_origins": _ss_origins,
        "session_storage_keys": _ss_keys,
        "cookie_names": _names,
        "reason": ("" if draft.get("session")
                   else "runner_returned_no_state" if not isinstance(state, dict)
                   else "state_empty__app_may_keep_session_in_sessionStorage"),
    }
    return {"status": "recorded", **draft}


@router.post("/api/v1/test-factory/recordings/cancel")
async def draft_recording_cancel(user: dict = Depends(get_current_user)):
    try:
        await runner_client.auth_capture_cancel()
    except Exception:
        pass
    return {"status": "cancelled"}


@router.post("/api/v1/test-factory/recipes/reuse-check")
async def recipe_reuse_check_endpoint(
    body: _ReuseCheckBody,
    user: dict = Depends(get_current_user),
):
    """Reuse proposal (Phase 5): given a login form, compute its login_type_key and
    report whether this tenant ALREADY has a matching recipe (``reuse`` — 'portal
    login already recorded, just enter your member number') or none (``record``).
    Tenant-scoped; spans artifacts (a recipe is reused across a client's apps)."""
    tenant_id = user["tenant_id"]
    domain = str(body.domain or "").strip().lower()

    # NO FORM SEEN YET. At onboarding only the base URL is known, which is exactly
    # when the proposal is worth making — the point is to stop the next tester
    # recording a login somebody already recorded. The key is a hash of the form
    # shape and cannot be computed without one, so match on the domain instead and
    # let propose_reuse decide: one recorded login on that host is a reuse, several
    # are a genuine question (a public login and a member portal are not the same),
    # none is a fresh recording.
    if not body.fields:
        async with tenant_scoped_session(tenant_id) as session:
            candidates = await persona_store.find_recipes_by_domain(
                session, tenant_id=tenant_id, login_domain=domain)
        proposal = login_fingerprint.propose_reuse(
            domain=domain,
            library=[dict(c, domain=c.get("login_domain") or domain,
                          key=c.get("login_type_key")) for c in candidates])
        return {"action": proposal.get("action", "record"),
                "matched_on": "domain", "domain": domain,
                "recipes": candidates,
                "options": proposal.get("options", []),
                "login_type_key": proposal.get("key") or ""}

    key = login_fingerprint.login_type_key(
        domain=domain, login_path=body.login_path,
        fields=[{"name": f.slot, "type": f.type or "text"} for f in body.fields],
        submit=body.submit)
    async with tenant_scoped_session(tenant_id) as session:
        hits = await persona_store.find_recipes_by_login_type(
            session, tenant_id=tenant_id, login_type_key=key)
    return {"action": "reuse" if hits else "record", "matched_on": "form",
            "login_type_key": key, "recipes": hits}


@router.post("/api/v1/test-factory/{artifact_id}/recipes/ensure-baseline")
async def ensure_baseline_recipe_endpoint(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Backfill: derive a baseline login recipe from this artifact's configured
    form-login, so named personas fill the same choreography with their own card
    instead of anyone hand-authoring it. Idempotent (no-op if a recipe already
    exists); honest skip when there is no form-login to derive from. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Materializing a baseline recipe requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cfg = await auth_profiles.get_form_login(
            session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id)
        out = await persona_store.ensure_baseline_from_form_login(
            session, tenant_id=tenant_id, artifact_id=artifact_id, form_login_cfg=cfg)
        await session.commit()
    if out.get("materialized"):
        await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                             actor=user.get("email") or user.get("user_id") or "?",
                             event_type="baseline_recipe_materialized",
                             detail=f"recipe={out.get('recipe_id')} slots={out.get('slots')}")
    return {"artifact_id": artifact_id, **out,
            "note": ("A crawl/auth-setup now yields a login recipe on file. A bare "
                     "captured SESSION (cookies only, no login steps) cannot derive "
                     "one — record a multi-step login demonstration for those.")}


@router.get("/api/v1/test-factory/{artifact_id}/classifications")
async def get_classifications_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cls = await persona_store.get_classifications(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    return {"artifact_id": artifact_id, "classifications": cls, "count": len(cls)}


class _RecipeProbeBody(BaseModel):
    """Verify a login recipe by replaying it with a persona's card."""
    persona_id: str = Field(..., min_length=1, max_length=64)
    environment_id: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field("", max_length=2000)
    test_id: str = Field("", max_length=64, description="a functional test to carry the probe run")


@router.post("/api/v1/test-factory/{artifact_id}/recipes/verify")
async def verify_recipe_endpoint(
    body: _RecipeProbeBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Replay the active login recipe with a member's card and report whether it
    actually logs in — the honest verification probe, and the per-card **Verify**
    action (F3).

    Runs ONE test headless so the recipe drives a real login, then reads the
    runner's own log through ``login_probe``. **Only reaching the recorded
    logged-in landing counts as verified**, and it stamps both the recipe and the
    card. Steps that merely replayed do NOT — the interpreter prints
    ``recipe login OK`` for a sequence that ran without proving anyone got in, and
    prints ``form login OK`` unconditionally after a click with no assertion at all.

    A definite failure (drift, an unrecorded interstitial, a card that does not
    cover the login) is recorded as failed and named for what it is. An inconclusive
    outcome leaves the card's existing state alone rather than overwriting it with a
    claim in either direction. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Verifying a recipe requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    token = _bearer(request)
    auth_config, login_env = await _persona_auth_bundle(
        request, artifact_id, tenant_id, body.persona_id, body.environment_id)
    if not auth_config:
        raise HTTPException(422, {
            "error": "no login recipe + card resolved for this persona/environment",
            "hint": "store a recipe (POST …/recipes) and a credential card "
                    "(PUT …/personas/{id}/credentials/{env}) first",
        })

    # Pick a carrier test (any active functional case) so the run has a page to
    # land on after the recipe login. The probe judges the LOGIN, not the case.
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
        visits, _ = await factory_service._load_current_pages_and_actions(session, artifact_id=artifact_id)
        edited_map = await _active_edited_map(session, artifact_id=artifact_id)
        _probe_env = await persona_store.get_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=body.environment_id) or {}
        _recipe_row = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        # The epoch a proof is stamped against — without it the card would be
        # recorded as proven with no tie to the data snapshot it was proven on.
        _env_epoch = str(_probe_env.get("data_epoch") or "")
    tcid = (body.test_id or "").strip()
    carrier = [c for c in cases if (getattr(c, "test_id", "") or "") == tcid] if tcid else cases[:1]
    if not carrier:
        raise HTTPException(404, "no active test to carry the probe run")

    # THE PROOF MUST HAPPEN WHERE THE PROOF IS CLAIMED.
    # A card is per (member, ENVIRONMENT), and this probe stamps it. Defaulting the
    # destination to the crawled origin meant a card could be marked "proven for
    # uat" by a login performed against whatever host the crawl happened to use —
    # and it would carry that environment's decrypted secrets there to do it. Same
    # class of defect as F1, with the added harm of sending credentials to a host
    # nobody selected.
    _routed = environment_routing.resolve_destination(
        environment_id=body.environment_id, environment=_probe_env or None,
        requested_base_url=(body.base_url or "").strip())
    if not _routed["allowed"]:
        raise HTTPException(422, {
            "error": "cannot establish where to verify this card",
            "reason": _routed["reason"], "detail": _routed["detail"],
            "note": ("A credential card is proven for ONE environment. Verifying it "
                     "somewhere else would prove nothing about that environment and "
                     "would send its credentials to a host that was not selected."),
        })
    base_url = _routed["base_url"] or _recorded_origin(visits)
    files = _configured_files(
        carrier, build_field_meta(visits), base_url, {},
        browsers=["chromium"], headed=False, workers=1, retries=0,
        edited=edited_map, storage_state=None, auth_config=auth_config)
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "recipe-probe",
        **login_env,
    }
    try:
        res = await runner_client.run_suite(files, env, timeout_ms=120000)
    except Exception as exc:
        raise HTTPException(502, f"probe runner error: {exc}")

    output = ""
    j = _RUNNER_JOBS.get(run_id) or {}
    output = str(j.get("output") or "")
    if not output:
        try:
            output = str((res or {}).get("output") or "")
        except Exception:
            output = ""
    v = login_probe.read_outcome(output)
    _probe_diag("verify", run_id, output, v)
    drift = ({"step": v["step"], "action": v["action"]}
             if v["outcome"] == login_probe.DRIFT else None)
    # Only a login that REACHED the recorded landing page counts. Stamping on the
    # weaker 'the steps replayed' signal is what let a recipe be marked verified
    # against a login nobody completed.
    if v["proven"]:
        async with tenant_scoped_session(tenant_id) as session:
            rec = await persona_store.get_recipe(session, tenant_id=tenant_id, artifact_id=artifact_id)
            if rec:
                await persona_store.stamp_recipe_verified(
                    session, tenant_id=tenant_id, recipe_id=rec["recipe_id"],
                    environment_id=body.environment_id)
            await persona_store.stamp_card_verified(
                session, tenant_id=tenant_id, persona_id=body.persona_id,
                environment_id=body.environment_id, verified=True,
                data_epoch=str(_env_epoch or ""),
                recipe_version=int((_recipe_row or {}).get("version") or 0))
            await session.commit()
    elif v["attribution"] == "credential":
        # The CARD is marked failed only when the card is what failed. Recipe drift,
        # an unrecorded interstitial and a runner outage are not the credential's
        # fault; recording them against it would destroy a good proof and point the
        # operator at a password that was never wrong.
        async with tenant_scoped_session(tenant_id) as session:
            await persona_store.stamp_card_verified(
                session, tenant_id=tenant_id, persona_id=body.persona_id,
                environment_id=body.environment_id, verified=False,
                data_epoch=str(_env_epoch or ""))
            await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="recipe_verified",
                         detail=f"persona={body.persona_id} env={body.environment_id} "
                                f"outcome={v['outcome']} proven={v['proven']}")
    return {
        "verified": bool(v["proven"]),
        "outcome": v["outcome"],
        "attribution": v["attribution"],
        "recipe_drift": drift,
        "run_id": run_id,
        "detail": v["detail"],
        "note": v["note"],
    }


# ── Persona diff / honest oracles (P3) ──────────────────────────────────────

def _suite_observed_values(cases) -> dict[str, str]:
    """value_key -> observed/expected text across the active suite. value_key is
    stable (scenario:step:kind) so a classification binds to one assertion."""
    out: dict[str, str] = {}
    for c in cases:
        tc = dict(getattr(c, "test_case", None) or {})
        sid = str(getattr(c, "test_case_id", "") or tc.get("test_id") or "")
        for st in (tc.get("steps") or []):
            n = int(st.get("step_number") or 0)
            obs = st.get("observed") or {}
            for kind, val in (("expected", st.get("expected_result") or st.get("expected")),
                              ("observed_value", obs.get("value")),
                              ("observed_text", obs.get("text"))):
                if val:
                    out[f"{sid}:{n}:{kind}"] = str(val)
    return out


class _ClassifyBody(BaseModel):
    baseline_persona_id: str = Field("", max_length=64,
                                     description="persona whose card identity grounds the echo test; default persona-0")
    environment_id: str = Field("uat", max_length=64)


@router.post("/api/v1/test-factory/{artifact_id}/oracles/classify")
async def classify_oracles_endpoint(
    body: _ClassifyBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Single-crawl oracle classification: tag every suite expected-value that
    echoes the recording member's own identity as member_derived/identity_echo.
    Everything else stays unknown until a two-persona diff proves it. Persisted
    to value_classifications; consumed by the per-persona oracle split and (at
    run time) by the compiler's honest downgrade."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Classifying oracles requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
        # identity values = the baseline persona's card (persona-0 = form-login)
        bp = (body.baseline_persona_id or "").strip() or f"persona0::{artifact_id}"
        card = await persona_store.get_persona_credential(
            session, envelope=envelope, tenant_id=tenant_id,
            persona_id=bp, environment_id=body.environment_id)
    identity_values = set()
    for v in (card or {}).values():
        if v and len(str(v)) >= 3:
            identity_values.add(str(v))
            # also the local-part of an email is a strong identity token
            if "@" in str(v):
                identity_values.add(str(v).split("@", 1)[0])
    observed = _suite_observed_values(cases)
    classified = persona_diff.classify_single_persona(observed, identity_values)
    member_derived = 0
    async with tenant_scoped_session(tenant_id) as session:
        for key, c in classified.items():
            sid, _, _ = (key.split(":", 2) + ["", ""])[:3]
            await persona_store.save_classification(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                value_key=key, class_=c["class"], evidence=c["evidence"],
                scenario_id=sid)
            if c["class"] == persona_diff.CLASS_MEMBER_DERIVED:
                member_derived += 1
        await session.commit()
    return {
        "classified": len(classified),
        "member_derived": member_derived,
        "app_constant_or_unknown": len(classified) - member_derived,
        "note": ("Values echoing the recording member's own identity are tagged "
                 "member_derived (identity_echo). The rest stay unknown until a "
                 "two-persona diff proves app-constancy — never assumed."),
    }


@router.get("/api/v1/test-factory/{artifact_id}/personas/{persona_id}/oracle-split/{environment_id}")
async def persona_oracle_split_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    persona_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """How much of the suite is PROVEN for this persona: app-constant values +
    member-derived values the persona's answer sheet supplies are PROVEN; the
    rest are UNVERIFIED (asserted structurally, never faked). The honest answer
    to 'does this suite hold for THIS member?'."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        classifications = await persona_store.get_classifications(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        answer_sheet = await persona_store.get_expected_values(
            session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
        p = await persona_store.get_persona(session, tenant_id=tenant_id, persona_id=persona_id)
    is_baseline = bool((p or {}).get("is_recording_baseline")) or persona_id.startswith("persona0::")
    split = persona_diff.per_persona_split(classifications, answer_sheet, is_baseline)
    return {"artifact_id": artifact_id, "persona_id": persona_id,
            "environment_id": environment_id, "is_baseline": is_baseline, **split}


class _PersonaDiffBody(BaseModel):
    persona_a: str = Field(..., min_length=1, max_length=64)
    persona_b: str = Field(..., min_length=1, max_length=64)
    environment_id: str = Field("uat", max_length=64)
    observed_a: dict[str, str] = Field(default_factory=dict)
    observed_b: dict[str, str] = Field(default_factory=dict)
    pages_a: list[str] = Field(default_factory=list)
    pages_b: list[str] = Field(default_factory=list)
    repeat_counts_a: dict[str, int] = Field(default_factory=dict)
    repeat_counts_b: dict[str, int] = Field(default_factory=dict)


@router.post("/api/v1/test-factory/{artifact_id}/oracles/persona-diff")
async def persona_diff_endpoint(
    body: _PersonaDiffBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Two-persona diff (the proven classifier). Given the values two personas
    saw on the same journey, classify each: differ ⇒ member_derived (and each
    persona's value is written to its answer sheet); identical ⇒ app_constant;
    time-shaped ⇒ volatile. Also reports the STRUCTURE diff (page forks +
    cardinality) that drives repetition and behavior-class scoping.

    Values may be supplied in the body (from two persona runs' captures) — the
    classification and its evidence are persisted so the run-time oracle and the
    report read the same proven truth."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Running a persona diff requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    value_diff = persona_diff.diff_two_personas(body.observed_a, body.observed_b)
    structure = persona_diff.diff_structure(
        body.pages_a, body.pages_b, body.repeat_counts_a, body.repeat_counts_b)
    md = 0
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for key, c in value_diff.items():
            sid = key.split(":", 1)[0]
            await persona_store.save_classification(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                value_key=key, class_=c["class"], evidence=c["evidence"], scenario_id=sid,
                detail=c.get("detail") or {})
            if c["class"] == persona_diff.CLASS_MEMBER_DERIVED:
                md += 1
                # write each persona's PROVEN value to its own answer sheet
                await persona_store.set_expected_value(
                    session, tenant_id=tenant_id, persona_id=body.persona_a,
                    environment_id=body.environment_id, value_key=key,
                    expected_value=body.observed_a.get(key, ""), source="diff_proven")
                await persona_store.set_expected_value(
                    session, tenant_id=tenant_id, persona_id=body.persona_b,
                    environment_id=body.environment_id, value_key=key,
                    expected_value=body.observed_b.get(key, ""), source="diff_proven")
        await session.commit()
    by_class: dict[str, int] = {}
    for c in value_diff.values():
        by_class[c["class"]] = by_class.get(c["class"], 0) + 1
    return {"value_diff": {"by_class": by_class, "member_derived": md,
                           "keys": len(value_diff)},
            "structure": structure,
            "note": ("Member-derived values were written to each persona's answer "
                     "sheet (diff_proven), so a later run asserts the RIGHT value "
                     "per member. Nothing was assumed — differences are observed.")}


# ── Environment governance (P4) ──────────────────────────────────────────────

class _EnvironmentBody(BaseModel):
    label: str = Field("", max_length=120)
    posture: str = Field("read_write", max_length=16, description="read_write | read_only | no_submit")
    is_production: bool = False
    write_authorized: bool = Field(False, description="explicit, revocable authorization to WRITE to production")
    base_url: str = Field("", max_length=2000)
    data_epoch: str = Field("", max_length=64)
    require_scoped_certification: bool = Field(False,
        description="opt-in: a persona run here needs a certification for its (env, behavior_class) cell")
    # F6 — routing the run must APPLY. NON-SECRET only: an onboarding profile's sealed
    # credentials stay in qe-central and are never mirrored here. Omitted (None) keeps
    # whatever is stored, so a governance-only edit cannot blank a lane selector.
    cookies: list[dict] | None = Field(None, description="non-secret routing cookies [{name,value,domain,path}]")
    headers: dict[str, str] | None = Field(None, description="non-secret routing headers")
    env_assertion: dict | None = Field(None, description="{url_pattern|selector|expect_text} — proof the run landed here")
    source: str = Field("", max_length=24, description="which registry this row came from: studio | onboarding")
    app_env_id: str = Field("", max_length=64, description="the onboarding Environment Profile this mirrors")


@router.put("/api/v1/test-factory/{artifact_id}/environments/{environment_id}")
async def put_environment_endpoint(
    body: _EnvironmentBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Register/update an environment's governance: posture, the production flag,
    and (for production) the explicit write authorization. A run inherits this at
    dispatch — production is default-deny without ``write_authorized``. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Configuring an environment requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.save_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id, label=body.label, posture=body.posture,
            is_production=body.is_production, write_authorized=body.write_authorized,
            base_url=body.base_url, data_epoch=body.data_epoch,
            require_scoped_certification=body.require_scoped_certification,
            cookies=body.cookies, headers=body.headers,
            env_assertion=body.env_assertion,
            source=body.source, app_env_id=body.app_env_id)
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="environment_configured",
                         detail=f"env={environment_id} posture={out['posture']} "
                                f"prod={out['is_production']} write_auth={out['write_authorized']}")
    return {"status": "saved", "environment_id": environment_id,
            "effective_posture": persona_governance.effective_posture({
                "posture": out["posture"], "is_production": out["is_production"]}),
            **out}


@router.get("/api/v1/test-factory/{artifact_id}/environments")
async def list_environments_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        envs = await persona_store.list_environments(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    for e in envs:
        e["effective_posture"] = persona_governance.effective_posture(e)
    return {"artifact_id": artifact_id, "environments": envs, "count": len(envs)}


class _CertRecordItem(BaseModel):
    scenario_id: str = Field(..., min_length=1, max_length=64)
    status: str = Field("certified", max_length=16, description="certified | failed")


class _CertRecordBody(BaseModel):
    environment_id: str = Field(..., min_length=1, max_length=64)
    behavior_class: str = Field("", max_length=64)
    run_id: str = Field("", max_length=64)
    scenarios: list[_CertRecordItem] = Field(..., min_length=1, max_length=2000)


@router.post("/api/v1/test-factory/{artifact_id}/certification/record")
async def record_certification_endpoint(
    body: _CertRecordBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Record a scoped certification: which scenarios are certified for a specific
    (environment, behavior_class) cell. A cell is proven on its OWN — a baseline
    certification does not grant trust on another env/class. Editor+. (The cert
    orchestrator calls this after a per-(env,class) certification run.)"""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Recording a certification requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    n = 0
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for it in body.scenarios:
            await persona_store.record_certification(
                session, tenant_id=tenant_id, artifact_id=artifact_id,
                scenario_id=it.scenario_id, environment_id=body.environment_id,
                behavior_class=body.behavior_class, status=it.status, run_id=body.run_id)
            n += 1
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="scoped_certification_recorded",
                         detail=f"env={body.environment_id} class={body.behavior_class} scenarios={n}")
    return {"status": "recorded", "environment_id": body.environment_id,
            "behavior_class": body.behavior_class, "recorded": n}


@router.get("/api/v1/test-factory/{artifact_id}/certification/matrix")
async def certification_matrix_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """The (environment × behavior_class) proof grid: how much of the suite is
    certified for each cell. A cell with no rows is simply not certified there —
    never assumed from another cell."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        ledger = await persona_store.get_certification_ledger(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    return {"artifact_id": artifact_id,
            "certification_matrix": persona_store.certification_matrix(ledger),
            "cells": len(ledger),
            "note": ("Each (env × class) cell is proven on its own. A baseline "
                     "certification does not grant trust on another environment or "
                     "member class.")}


class _CaseScopeBody(BaseModel):
    required_behavior_class: str = Field(..., max_length=64,
                                         description="behavior class this case belongs to; '' clears the binding")
    evidence: str = Field("editor", max_length=24)


@router.put("/api/v1/test-factory/{artifact_id}/cases/{scenario_id}/behavior-class")
async def bind_case_scope_endpoint(
    body: _CaseScopeBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Bind a case to the behavior class it belongs to. An out-of-class persona is
    then refused at dispatch (test_scope) — the journey does not apply to that
    member, so it is skipped, never run-and-failed. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Scoping a case requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        await persona_store.bind_case_scope(
            session, tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
            required_behavior_class=body.required_behavior_class.strip(),
            evidence=body.evidence)
        await session.commit()
    return {"status": "bound", "scenario_id": scenario_id,
            "required_behavior_class": body.required_behavior_class.strip()}


class _ScopeFromStructureBody(BaseModel):
    behavior_class: str = Field(..., min_length=1, max_length=64)
    forked_scenario_ids: list[str] = Field(..., min_length=1,
                                           description="cases a structure fork proved belong to this class")


@router.post("/api/v1/test-factory/{artifact_id}/scopes/from-structure")
async def scopes_from_structure_endpoint(
    body: _ScopeFromStructureBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Bulk-bind the cases a P3 structure fork identified to the behavior class
    that walks them. Turns a proven fork into enforced scoping in one call."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Scoping cases requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    bc = body.behavior_class.strip()
    bound = 0
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        for sid in body.forked_scenario_ids:
            sid = str(sid).strip()
            if not sid:
                continue
            await persona_store.bind_case_scope(
                session, tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=sid,
                required_behavior_class=bc, evidence="structure_fork")
            bound += 1
        await session.commit()
    return {"status": "bound", "behavior_class": bc, "bound": bound,
            "note": ("These cases now REFUSE an out-of-class persona at dispatch "
                     "(test_scope) — proven-fork scoping, never a false red.")}


def _probe_url_ok(url: str) -> bool:
    """Minimal SSRF guard for the reachability probe: http(s) only, and no
    link-local / cloud-metadata host. base_url is operator-configured, so this is
    a backstop, not the primary control."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in ("169.254.169.254", "metadata", "metadata.google.internal"):
        return False
    return True


class _EnvHealthBody(BaseModel):
    base_url: str = Field("", max_length=2000, description="overrides the stored base_url for this probe")
    persona_id: str = Field("", max_length=64, description="probe login with this persona's card (optional)")


@router.post("/api/v1/test-factory/{artifact_id}/environments/{environment_id}/health")
async def environment_health_endpoint(
    body: _EnvHealthBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Environment health probe — reachability, and (with a persona) a login probe.
    This is NOT a re-crawl: it checks the target is up and the recipe still logs
    in, then stamps the environment. A known-bad environment is refused at
    dispatch (environment_policy), never blamed on the application. Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Probing an environment requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        env_row = await persona_store.get_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id) or {}
    base_url = (body.base_url or env_row.get("base_url") or "").strip()
    status = "unknown"
    detail = ""
    # 1) reachability
    if base_url:
        if not _probe_url_ok(base_url):
            status, detail = "unreachable", "base_url failed the safety guard (scheme/host)"
        else:
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                    r = await client.get(base_url)
                if r.status_code >= 500:
                    status, detail = "unreachable", f"reachability GET returned {r.status_code}"
                else:
                    status, detail = "healthy", f"reachability GET {r.status_code}"
            except Exception as exc:
                status, detail = "unreachable", f"reachability error: {str(exc)[:200]}"
    else:
        detail = "no base_url configured — reachability skipped"
    # 2) optional login probe (reuses the recipe/card probe, honest drift naming)
    if status in ("healthy", "unknown") and body.persona_id.strip():
        try:
            auth_config, login_env = await _persona_auth_bundle(
                request, artifact_id, tenant_id, body.persona_id.strip(), environment_id)
            if auth_config:
                token = _bearer(request)
                async with tenant_scoped_session(tenant_id) as session:
                    cases = await factory_service.load_active_production_cases(session, artifact_id=artifact_id)
                    visits, _ = await factory_service._load_current_pages_and_actions(session, artifact_id=artifact_id)
                    edited_map = await _active_edited_map(session, artifact_id=artifact_id)
                carrier = cases[:1]
                if carrier:
                    probe_url = base_url or _recorded_origin(visits)
                    files = _configured_files(
                        carrier, build_field_meta(visits), probe_url, {},
                        browsers=["chromium"], headed=False, workers=1, retries=0,
                        edited=edited_map, storage_state=None, auth_config=auth_config)
                    run_id = uuid.uuid4().hex
                    penv = {"NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                            "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
                            "NEXUS_BASE_URL": probe_url, "NEXUS_ENV": "env-health-probe",
                            **login_env}
                    try:
                        await runner_client.run_suite(files, penv, timeout_ms=120000)
                    except Exception as exc:
                        _logger.warning("env_health.probe_runner_err env=%s err=%s", environment_id, str(exc)[:200])
                    output = str((_RUNNER_JOBS.get(run_id) or {}).get("output") or "")
                    # Read the same signal the run gate reads. This branch was the
                    # last holder of the old substring test, and health is not a
                    # cosmetic label: gate_dispatch REFUSES a known-bad environment,
                    # so calling an environment 'healthy + login OK' on the strength
                    # of a line printed without anyone logging in re-opened the gate
                    # for exactly the case F3 exists to close.
                    pv = login_probe.read_outcome(output)
                    if pv["proven"]:
                        status, detail = "healthy", "reachable, and a login reached the recorded page"
                    elif pv["outcome"] == login_probe.DRIFT:
                        status, detail = "recipe_drift", pv["detail"]
                    elif pv["outcome"] == login_probe.STEPS_ONLY:
                        # Not a failure of the environment: the login ran, nothing
                        # proved it. Naming the environment bad here would refuse
                        # every run against it.
                        status = "healthy"
                        detail = ("reachable; the login replayed but this recipe has no "
                                  "logged-in checkpoint, so nothing proved it")
                    else:
                        status, detail = "login_failed", pv["detail"] or "login did not confirm"
        except Exception as exc:
            _logger.warning("env_health.login_probe_err env=%s err=%s", environment_id, str(exc)[:200])
    async with tenant_scoped_session(tenant_id) as session:
        await persona_store.stamp_env_health(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id, health_status=status, health_detail=detail)
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="environment_health_probe",
                         detail=f"env={environment_id} status={status}")
    return {"environment_id": environment_id, "health_status": status, "detail": detail,
            "note": ("A health probe checks the environment is up and the recipe "
                     "still logs in — it is NOT a re-crawl. A known-bad environment "
                     "is refused at dispatch, never blamed on the application.")}


# ── Scale (P5) ───────────────────────────────────────────────────────────────

class _PersonaSelectBody(BaseModel):
    traits: list[str] = Field(default_factory=list)
    behavior_class: str = Field("", max_length=64)


@router.post("/api/v1/test-factory/{artifact_id}/personas/select")
async def select_persona_endpoint(
    body: _PersonaSelectBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Resolve a concrete persona from a trait / behavior-class request — the
    preview behind 'run as any senior-large-family member'. Deterministic (the
    most specific match), and an honest miss returns persona=None, never a
    guessed member."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        personas = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    sel = persona_scale.select_persona(personas, traits=body.traits,
                                       behavior_class=body.behavior_class)
    p = sel.get("persona")
    return {"artifact_id": artifact_id,
            "persona_id": (p or {}).get("persona_id"),
            "name": (p or {}).get("name"),
            "ambiguous": sel.get("ambiguous"), "candidates": sel.get("candidates"),
            "reason": sel.get("reason")}


@router.get("/api/v1/test-factory/{artifact_id}/environments/{environment_id}/staleness")
async def environment_staleness_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    ttl_days: int = Query(30, ge=1, le=365),
    user: dict = Depends(get_current_user),
):
    """Which persona cards for this environment need re-verification — because the
    environment's data epoch rolled since they were proven, or the TTL lapsed, or
    they were never verified. The honest due-list before a regression run; a
    stale card is a signal, never a silent pass."""
    from datetime import datetime, timezone
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        env = await persona_store.get_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id) or {}
        cards = await persona_store.all_credential_status(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    by_persona = {c["persona_id"]: c for c in cards
                  if c["environment_id"] == environment_id}
    rep = persona_scale.staleness_report(
        by_persona, env, now=datetime.now(timezone.utc), ttl_days=ttl_days)
    return {"artifact_id": artifact_id, "environment_id": environment_id, **rep}


class _BulkCard(BaseModel):
    persona_name: str = Field(..., min_length=1, max_length=120)
    environment_id: str = Field(..., min_length=1, max_length=64)
    slot_values: dict[str, str] = Field(..., min_length=1)
    behavior_class: str = Field("", max_length=64)
    traits: list[str] = Field(default_factory=list)


class _BulkImportBody(BaseModel):
    cards: list[_BulkCard] = Field(..., min_length=1, max_length=500)


@router.post("/api/v1/test-factory/{artifact_id}/credentials/bulk-import")
async def bulk_import_credentials_endpoint(
    body: _BulkImportBody,
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Provision many members at once: for each row, upsert the persona (by name)
    and its envelope-encrypted card for an environment. One call stands up a whole
    app's test population. Ciphertext only — nothing plaintext is ever stored."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Importing credentials requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(503, "encryption unavailable — refusing to import credentials in plaintext")
    imported = 0
    errors: list[dict] = []
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        # The card contract applies HERE too. Bulk import is exactly the path where a
        # column-mapping mistake produces five hundred cards that all save cleanly and
        # all silently skip the login — the single-card PUT enforcing it while this
        # one did not is how "the API is the contract" quietly stops being true.
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        for i, c in enumerate(body.cards):
            try:
                card_contract.check_card(recipe=recipe, slot_values=c.slot_values)
                p = await persona_store.save_persona(
                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                    name=c.persona_name, behavior_class=c.behavior_class, traits=c.traits)
                await persona_store.save_persona_credential(
                    session, envelope=envelope, tenant_id=tenant_id,
                    persona_id=p["persona_id"], environment_id=c.environment_id,
                    slot_values=c.slot_values,
                    login_type_key=str((recipe or {}).get("login_type_key") or ""),
                    recipe_version=int((recipe or {}).get("version") or 0))
                imported += 1
            except card_contract.CardContractError as exc:
                errors.append({"index": i, "persona_name": c.persona_name,
                               "error": str(exc), **exc.detail})
            except Exception as exc:
                errors.append({"index": i, "persona_name": c.persona_name,
                               "error": str(exc)[:200]})
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="credentials_bulk_imported",
                         detail=f"imported={imported} errors={len(errors)}")
    return {"imported": imported, "errors": errors, "requested": len(body.cards),
            "note": "Personas + envelope-encrypted cards provisioned. No secret is echoed back."}


@router.get("/api/v1/test-factory/{artifact_id}/matrix")
async def member_environment_matrix_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    mutating: bool = Query(True, description="judge posture for a mutating run (a run's default)"),
    user: dict = Depends(get_current_user),
):
    """Which member × environment combinations can actually run — members down,
    environments across, one verdict per cell.

    Every state here comes from the SAME derivation the dispatch gate applies, so a
    cell that reads ready is not refused for that reason and a cell that reads
    blocked would have been. Previously all of it was discovered by dispatching a
    run and reading the refusal, which is a slow way to learn a fact the server
    already knows — and it trains people to treat a BLOCKED run as noise.

    Slot NAMES only; no credential value is reachable from this endpoint."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        personas = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        envs = await persona_store.list_environments(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        cards = await persona_store.all_credential_status(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    # persona-0 is synthetic and runs on the stored form login, not a card.
    legacy = {p["persona_id"] for p in personas if p.get("legacy")
              or str(p.get("persona_id", "")).startswith(persona_identity.PERSONA0_PREFIX)}
    out = matrix.build(personas=personas, environments=envs, cards=cards,
                       recipe=recipe, mutating=mutating, legacy_persona_ids=legacy)
    return {"artifact_id": artifact_id, **out}


@router.get("/api/v1/test-factory/{artifact_id}/credentials/manifest")
async def credentials_manifest_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    export: bool = Query(False, description="mark a deliberate export (audit-logged); a plain view is not"),
    user: dict = Depends(get_current_user),
):
    """The NON-SECRET provisioning grid: every persona×environment card, its slot
    NAMES (never values), verify status + epoch. Export this to see what is
    provisioned and what is missing — secrets never leave the envelope. A
    deliberate ``export=true`` is redacted AND audit-logged (P5 ops).

    Each card also carries the DERIVED state the run gate will apply — ready /
    unproven / stale_slots — computed against the artifact's ACTIVE recipe and the
    environment's current epoch. It is derived on read rather than stored so a card
    orphaned by a re-record shows as broken immediately, without anything having had
    to remember to mark it."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        personas = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        cards = await persona_store.all_credential_status(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        recipe = await persona_store.get_recipe(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        envs = await persona_store.list_environments(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    by_env = {e["environment_id"]: e for e in envs}
    pname = {p["persona_id"]: p["name"] for p in personas}
    for c in cards:
        c["persona_name"] = pname.get(c["persona_id"], "")
        st = card_state.evaluate(recipe=recipe, card=c,
                                 environment=by_env.get(c["environment_id"]))
        c["state"] = st["state"]
        c["runnable"] = st["runnable"]
        c["state_reason"] = st["reason"]
        c["missing_slots"] = st["missing"]
        c["unexpected_slots"] = st["unexpected"]
        c["state_note"] = st["detail"].get("note", "")
    if export:
        await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                             actor=user.get("email") or user.get("user_id") or "?",
                             event_type="credentials_manifest_exported",
                             detail=f"cards={len(cards)} personas={len(personas)}")
    return {"artifact_id": artifact_id, "personas": len(personas),
            "cards": cards, "card_count": len(cards), "exported": bool(export),
            "note": "Slot NAMES only — a credential value never leaves the envelope."}


@router.post("/api/v1/test-factory/{artifact_id}/credentials/rotate")
async def rotate_credentials_endpoint(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Secret rotation (P5 ops): re-encrypt every persona card of this artifact so
    the CURRENT envelope key wraps them (after a key roll). Decrypt-then-encrypt in
    place; no plaintext is returned or logged. Admin/manager only."""
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(403, "Rotating credentials requires an admin or manager role.")
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(503, "encryption unavailable — cannot rotate credentials")
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        out = await persona_store.rotate_cards(
            session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id)
        await session.commit()
    await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                         actor=user.get("email") or user.get("user_id") or "?",
                         event_type="credentials_rotated",
                         detail=f"rotated={out.get('rotated')} failed={out.get('failed')}")
    return {"artifact_id": artifact_id, **out,
            "note": "Cards re-wrapped under the current envelope key. No secret was exposed."}


class _StalenessSweepBody(BaseModel):
    data_epoch: str = Field("", max_length=64,
                            description="the environment's NEW data epoch; defaults to the stored one")


@router.post("/api/v1/test-factory/{artifact_id}/environments/{environment_id}/staleness/sweep")
async def staleness_sweep_endpoint(
    body: _StalenessSweepBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    environment_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Staleness sweep (P5 ops): when an environment's data epoch rolls, flag every
    card verified against an OLDER epoch back to unverified, so the next run must
    re-prove it before it is trusted. Idempotent. An external scheduler calls this
    after a data refresh; re-validation itself happens at the next run's preflight.
    Editor+."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Sweeping staleness requires an editor, manager or admin role.")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        env = await persona_store.get_environment(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id) or {}
        epoch = (body.data_epoch or "").strip() or str(env.get("data_epoch") or "")
        out = await persona_store.flag_stale_cards(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            environment_id=environment_id, current_epoch=epoch)
        await session.commit()
    if out.get("flagged_count"):
        await _persona_audit(tenant_id=tenant_id, artifact_id=artifact_id,
                             actor=user.get("email") or user.get("user_id") or "?",
                             event_type="cards_flagged_stale",
                             detail=f"env={environment_id} epoch={epoch} flagged={out['flagged_count']}")
    return {"artifact_id": artifact_id, **out,
            "note": ("Cards proven against an older epoch are now unverified; the "
                     "next run re-proves them before trusting a member value. A stale "
                     "card is a signal, never a silent pass.")}


@router.get("/api/v1/test-factory/{artifact_id}/personas/ops/summary")
async def persona_ops_summary_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Ops rollup for the persona × environment matrix: personas, environments +
    postures, cards + verification state, recipes. A single glance at the health
    of an app's test population."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        personas = await persona_store.list_personas(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        envs = await persona_store.list_environments(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        cards = await persona_store.all_credential_status(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
        recipes = await persona_store.list_recipes(
            session, tenant_id=tenant_id, artifact_id=artifact_id)
    verified = sum(1 for c in cards if c.get("verify_status") == "verified")
    prod = sum(1 for e in envs if e.get("is_production"))
    return {"artifact_id": artifact_id,
            "personas": len(personas),
            "environments": {"total": len(envs), "production": prod,
                             "postures": {e["environment_id"]:
                                          persona_governance.effective_posture(e) for e in envs}},
            "credentials": {"total": len(cards), "verified": verified,
                            "unverified": len(cards) - verified},
            "recipes": len(recipes)}


class _ImpersonationScaffoldBody(BaseModel):
    admin_login_steps: list[dict] = Field(..., min_length=1,
                                          description="the admin login choreography (recipe steps)")
    impersonate_path: str = Field(..., min_length=1, max_length=2000)
    # No default: the switch-on field is whatever the target app calls it. A
    # default would quietly bake one client's vocabulary into every other's.
    member_slot: str = Field(..., min_length=1, max_length=64,
                             description="the field the app switches identity on, named as the app names it")
    submit_selector: str = Field("", max_length=400)


@router.post("/api/v1/test-factory/{artifact_id}/recipes/impersonation-scaffold")
async def impersonation_scaffold_endpoint(
    body: _ImpersonationScaffoldBody,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Scaffold a recipe for an environment where a run reaches a member by ADMIN
    impersonation (a switch-user endpoint) rather than a member login — the
    env-dependent path some enterprises use. Returns recipe steps + slots ready
    to POST to /recipes; it verifies and runs through the same machinery. No
    secret is embedded."""
    if not _persona_write_ok(user):
        raise HTTPException(403, "Scaffolding a recipe requires an editor, manager or admin role.")
    scaffold = persona_scale.build_impersonation_recipe(
        admin_login_steps=body.admin_login_steps, impersonate_path=body.impersonate_path,
        member_slot=body.member_slot, submit_selector=body.submit_selector)
    return {"artifact_id": artifact_id, **scaffold}
