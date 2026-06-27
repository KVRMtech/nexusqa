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
import logging
import os
import uuid
import zipfile

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request
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
from ..services.diff_and_heal import heal_policy
from ..services.diff_and_heal import control_ledger
from ..services.diff_and_heal import action_resolver
from ..services.flywheel import ledger as flywheel_ledger
from ..services.test_factory import fidelity as tf_fidelity
from ..services.test_runs import (
    last_run_summary_by_scenario,
    _status_severity,
    build_latest_run_timeline,
    build_run_timeline_by_id,
    find_run_by_ci_run_id,
    recent_runs,
    scenario_verdict_history,
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
from ..services.test_factory.heal_scheduler import (
    SCHEDULER as _HEAL_SCHEDULER,
    make_budget as _make_heal_budget,
    measure_harness_flake as _measure_harness_flake,
)
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

# ─── T5.4 APPROVER RBAC (additive; the heal approve gate is "theater without RBAC")
# A viewer can already RUN/DIAGNOSE a heal (read paths + the run endpoints under the
# manager gate); APPROVING/persisting a machine-written fix is a higher bar — only an
# approver role may promote a PROPOSED heal to the active source for runs. This is a
# SECOND, stricter check layered on top of _rbac_gate (which already blocks viewers
# from POST): it narrows the approve endpoint specifically to the approver set and
# records WHO approved into the Part-11 evidence chain. Never loosens existing auth.
_APPROVER_ROLES = frozenset({"admin", "approver", "maintainer", "manager"})


def _require_approver(user: dict) -> None:
    if user.get("role", "viewer") not in _APPROVER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Approving a heal requires an approver role (admin/approver/maintainer/manager); "
                   "viewers may run and diagnose but not persist a fix.",
        )


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


@router.post("/api/v1/test-factory/{artifact_id}/generate")
async def generate_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        summary = await factory_service.generate_and_store(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    added_cases = await proposer.reapply_added_cases(artifact_id, tenant_id)
    reapplied = await _reapply_tf_overrides(artifact_id, tenant_id)
    approved_protected = await proposer.reapply_approved(artifact_id, tenant_id)
    return {"success": True, "overrides_reapplied": reapplied, "added_cases": added_cases,
            "approved_protected": approved_protected, **summary}


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
    return {"success": True, **result}


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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            # Fixed timestamp -> the zip bytes are reproducible too, not just the code.
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)

    suffix = f"-{tcid[:8]}" if tcid else (f"-{cat}" if cat else "")
    filename = f"nexus-playwright-{artifact_id[:8]}{suffix}.zip"
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
    manifest = compile_manifest(cases, build_field_meta(visits))
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
    # T4.1 HEADLESS-PARALLEL PROVE (additive, default-off). "" / "headed" =
    # today's behavior EXACTLY (headed run_live on the single shared Xvfb
    # display; the human-watched demo path). "headless" routes the auto-heal
    # verification + confirmation re-runs through the runner's HEADLESS /run
    # (blocking, parallelizable workers>1) so a 12-iteration loop does not
    # monopolize the single live display ~1h. Oracle / correlation-by-run-id /
    # confirmation gate are UNCHANGED — only the transport + headed/workers
    # baked into the bundle change.
    prove_mode: str = ""
    prove_workers: int | None = None
    # T4.2 SLA + FLAKE (additive, default-off). sla_seconds=None/0 => unbounded
    # (today's only bound is the 12-iteration cap). flake_samples=None/0 => no
    # harness-flake pre-pass. When set, sla_seconds stops a runaway heal toward an
    # HONEST needs_human (never a silent hang) and flake_samples re-runs the
    # baseline control N times to record the harness's own flake rate for
    # flake-correction. Neither can ever turn a non-green run green.
    sla_seconds: float | None = None
    flake_samples: int | None = None
    # AGENTIC AUTO-HEAL (additive, default-OFF). When true, a GROUNDABLE failure that
    # the deterministic recipes can't resolve gets one LLM-agent pass BEFORE escalating
    # to a human: the agent reasons over the LIVE page's controls and proposes a grounded
    # rebind/wait. It cannot fabricate a selector (ungrounded picks are dropped) and
    # cannot touch the REFUSE families (real regression / auth / data / variant); the
    # step's own oracle + the 2x confirm still gate green — so it can never green-wash.
    # Off => the loop is byte-identical to today ($0 LLM).
    enable_agentic_heal: bool = False
    agentic_tier: str | None = None              # LLM tier name (default "tier_premium")
    agentic_min_confidence: float | None = None  # min proposal confidence to apply (default 0.7)
    # EVIDENCE: opt-in full-run VIDEO (screenshots are default-on via the runner env).
    # Off => no video (cheap). On => NEXUS_RECORD_VIDEO=1 for this run only — records +
    # uploads a video clip (the proving/clean-run proof). Bounded by a 30 MiB cap.
    enable_video: bool = False


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
        f"- Data overrides: {'nexus.data.json' if has_data else 'none — using the observed values'}\n"
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


def _configured_files(cases, field_meta, base_url: str, data: dict,
                      data_by_test: dict | None = None,
                      browsers=None, headed: bool = False,
                      workers=None, retries=None,
                      edited: dict | None = None,
                      storage_state: str | None = None) -> dict:
    """Parametrized bundle + nexus.config.json (chosen base URL) + nexus.data.json
    + run README. Shared by the download and the server-side runner so both run
    exactly the same thing. Browser projects / headed / workers / retries are
    baked into playwright.config.ts. nexus.data.json is two-tier:
    {"_global": {...defaults}, "<test_id>": {...per-test overrides}} — defaults
    stay the OBSERVED values, so an empty data file runs identically.

    `edited` = {test_id: {"spec_path","script_source"}} overrides the compiled
    spec for any test that has an active edited version (Phase C). Path keying is
    identical to compile_manifest, so the owned source lands where the runner
    expects it; un-edited tests keep the deterministic compiler output."""
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
    files["nexus.data.json"] = json.dumps(nexus_data, indent=2, sort_keys=True) + "\n"
    files["README.md"] = _run_config_readme(
        len(cases), files.get("nexus.config.json", ""), has_data,
    )
    # Inject a captured authenticated session for SERVER runs ONLY (the caller
    # passes storage_state from the artifact's auth profile). The generated config
    # self-detects nexus.auth.json; downloaded bundles never receive one.
    if storage_state and storage_state.strip():
        files["nexus.auth.json"] = storage_state
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

# Transient run status for the live "running -> done" indicator. The durable
# record is the ingested run (triage board); this only drives the UI spinner, so
# loss on restart / across workers is harmless.
_RUNNER_JOBS: dict[str, dict] = {}
_RUNNER_TASKS: set = set()


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


async def _persist_job(run_id: str) -> None:
    """Mirror the current (typically terminal) job state to the durable registry."""
    job = _RUNNER_JOBS.get(run_id)
    if not job:
        return
    await runner_jobs.persist_job(
        tenant_id=job.get("tenant_id", ""), run_id=run_id,
        artifact_id=job.get("artifact_id", ""), kind=job.get("kind", "run"), job=job,
    )


async def _execute_run(run_id: str, files: dict, env: dict) -> None:
    job = _RUNNER_JOBS.get(run_id)
    if job is None:
        return
    try:
        result = await runner_client.run_suite(files, env)
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
    # Fix ladder: (1) a control whose KIND changed so the recorded RECIPE is wrong
    # (native <select> → custom ARIA combobox: open+pick) — re-synthesise the
    # interaction; else (2) a RENAMED control → re-anchor; else (3) the control-kind
    # fill→selectOption fix. All are proved green (each carries a grounded oracle)
    # before anything is saved, and none can override a real regression.
    _interaction = self_heal.build_interaction_candidate(tc, field_meta, step_number)
    _reanchor = None if _interaction else self_heal.resolve_reanchor_for_step(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        baseline_step=_bs, field_meta=field_meta,
    )
    if _interaction:
        candidate, fixmeta = _interaction
        _heal_note = (
            f"Auto-healed: re-synthesised '{fixmeta.get('label', '')}' as a "
            f"{fixmeta.get('interaction', 'custom combobox')} (open + pick, not "
            "selectOption), verified green"
        )
    elif _reanchor:
        candidate, fixmeta = self_heal.build_reanchor_candidate(
            tc, field_meta, step_number, _reanchor)
        _heal_note = (
            f"Auto-healed: re-anchored '{fixmeta.get('label', '')}' to the renamed "
            f"control '{_reanchor['name']}', verified green"
        )
    else:
        candidate, fixmeta = self_heal.build_candidate_for_step(tc, field_meta, step_number)
        _heal_note = "Auto-healed: control-kind fix (.fill -> .selectOption), verified green"

    # ASSERTION-IMMUTABILITY GUARD — a heal candidate must never carry FEWER grounded
    # outcome assertions than the baseline (no weaken-to-go-green). Refuse before we run.
    _g_ok, _g_msg = self_heal.assert_assertions_unchanged(
        compile_case(tc, field_meta, parametrize=True), candidate)
    if not _g_ok:
        return {"run_id": None, "status": "refused", "healed": False, "refused": True,
                "reason": "assertion-immutability guard: " + _g_msg}
    # OUTCOME-GROUNDING honesty — does the healed step carry a GROUNDED outcome oracle, or
    # did it only re-run green? Threaded to the verifier so the heal is labelled
    # 'proven' (grounded outcome held) vs 'outcome_not_grounded', never silently 'proven'.
    _outcome_grounded = self_heal.step_outcome_grounded(candidate, step_number)
    if not _outcome_grounded:
        _heal_note += (" — note: outcome NOT independently grounded (re-ran green, but "
                       "this step carries no recorded-outcome oracle)")

    id_to_path = {s["test_id"]: s["path"]
                  for s in compile_manifest([tc], field_meta).get("scripts", [])}
    spec_path = id_to_path.get(scenario_id, "")
    base_url = (body.base_url or "").strip()
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    # Inject the candidate as a TRANSIENT edited override for just this test — the
    # exact source we'll persist on green. Verify what you save.
    edited = {**edited_map, scenario_id: {"spec_path": spec_path, "script_source": candidate}}
    files = _configured_files(
        [tc], field_meta, base_url, body.data, data_by_test=body.data_by_test,
        browsers=(body.browsers or ["chromium"])[:1], headed=True, workers=1,
        retries=0, edited=edited, storage_state=storage_state,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
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
        "outcome_grounded": _outcome_grounded,
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
    edited = {**edited_map, scenario_id: {"spec_path": spec_path, "script_source": capture_spec}}
    files = _configured_files(
        [tc], field_meta, base_url, body.data, data_by_test=body.data_by_test,
        browsers=["chromium"], headed=False, workers=1, retries=0,
        edited=edited, storage_state=storage_state,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
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


# ── T4.1 PROVE DISPATCH (additive; default = today's HEADED run_live) ──────────
# A heal prove re-run needs exactly two things from the transport: (1) start the
# bundle, (2) know it reached terminal state. The GREEN verdict itself never
# comes from here — it comes DOWNSTREAM from correlating sub_run_id via
# find_run_by_ci_run_id + build_run_timeline_by_id + the _proven_pass closure +
# the confirmation gate. So a headless prove is a pure transport swap:
#
#   headed   (default): runner_client.run_live (202, single Xvfb display) then
#                       _await_run_terminal() polls /run-live/status. BYTE-
#                       IDENTICAL to the pre-T4.1 inline dispatch.
#   headless (opt-in) : runner_client.run_suite (BLOCKS until the real verdict,
#                       parallelizable workers>1, no single-display 409
#                       contention). No _await_run_terminal needed — run_suite
#                       returns only when terminal.
#
# Returns True iff the run was started AND reached a terminal state. The caller
# treats False exactly like the old `if not started` branch (runner busy /
# unreachable). It does NOT decide pass/fail — that stays with the oracle.

def _prove_is_headless(ctx: dict) -> bool:
    return str((ctx or {}).get("prove_mode") or "").strip().lower() == "headless"


def _prove_workers(ctx: dict) -> int:
    """Workers baked into the bundle for a HEADLESS prove (>=1). Headed proves
    are always single-worker on the one shared display (unchanged)."""
    if not _prove_is_headless(ctx):
        return 1
    try:
        w = int((ctx or {}).get("prove_workers") or 0)
    except (TypeError, ValueError):
        w = 0
    return w if w >= 1 else 2


async def _dispatch_prove(files: dict, env: dict, ctx: dict, *, tries: int = 6) -> bool:
    """Start a prove re-run and wait for it to reach a terminal state.

    HEADED (default, prove_mode != 'headless'): byte-identical to the legacy
    inline block — run_live (retry-on-409) then _await_run_terminal().
    HEADLESS (prove_mode == 'headless'): run_suite (blocks; parallel-safe).

    Returns True if the run started and finished; False if the runner was busy /
    unreachable after `tries` attempts (caller stops toward 'runner busy')."""
    if _prove_is_headless(ctx):
        # HEADLESS /run blocks until the real verdict; no single-display 409
        # contention, so a transport error is the only failure mode to retry.
        for _try in range(tries):
            try:
                await runner_client.run_suite(files, env)
                return True
            except Exception:
                await asyncio.sleep(2.0)
                continue
        return False
    # HEADED (unchanged): start on the single live display, then poll terminal.
    started = False
    for _try in range(tries):
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
        return False
    await _await_run_terminal()
    return True


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
    # T4.1 PROVE MODE (default headed = today's watched demo path, unchanged).
    _headless_prove = _prove_is_headless(ctx)
    _prove_headed = not _headless_prove
    _prove_w = _prove_workers(ctx)

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

        overrides: dict[str, dict] = {}        # sid -> {label_norm: signal}
        attempts: dict[tuple, int] = {}        # (sid, step) -> count
        # T5.5: track the LOWEST diagnose confidence + worst cause across the heals
        # we actually applied this run, so the 3-tier policy can demote a marginal
        # suite to APPROVE before persist. Defaults (1.0 / no cause) mean an UNHEALED
        # green suite is unaffected (nothing was applied to second-guess).
        heal_min_confidence: float = 1.0
        heal_worst_cause: str = ""
        _ra_done: set = set()                  # (sid, step) re-anchor attempts — one per step

        # ── PHASE 2: SEED-BEFORE-RUN (proven control ledger → overrides) ──
        # Pre-populate overrides[sid] from fixes a PRIOR scenario already proved green
        # (oracle + 2x confirm) for the SAME control (fingerprint = page + normalized
        # label + KIND). Iteration 1 then honors them, so a control shared with an
        # earlier-healed scenario passes immediately instead of re-healing from scratch
        # — this is what kills the "Scenario 2 re-heals 80% of shared steps" waste.
        # ADDITIVE + FAIL-OPEN: table absent / DB error / no match → overrides stays
        # empty → byte-identical to today. NEVER GREEN-WASH: a seed is ONLY an override;
        # the step's own grounded oracle + the 2x confirm still decide green, and a
        # seeded step that FAILS the first prove has its seed CLEARED below, so a stale
        # seed is never worse than from-scratch. KIND PRE-GATE: refuse interaction/
        # control_kind seeds on a blank-kind step (the one case the fingerprint can't
        # bind kind on) so a renamed-but-repurposed homonym can't be mis-seeded.
        _seeded_steps: set = set()         # (sid, step_number) seeded via step channels
        _seeded_labels: dict = {}          # sid -> {label_norm} seeded via control_kind
        _seed_fp: dict = {}                # (sid, step_number, fix_kind) -> control_fp  (P3 invalidation)
        _seed_fp_label: dict = {}          # (sid, label_norm) -> control_fp  (control_kind, P3)
        _fuzzy_seeds: set = set()          # (sid, step_number, 'reanchor') seeded via P4 fuzzy (NOT in _seed_fp)
        _fuzzy_on = bool(os.getenv("NEXUS_LEDGER_FUZZY_REANCHOR")) and \
            os.getenv("NEXUS_LEDGER_APP_SCOPE", "1") != "0"   # default-off; needs app scope

        def _fuzzy_reanchor_pick(_obs, _cands):
            """P4 v2 GATES 0-4 — return a reanchor payload to fuzzy-seed for this step's
            live control, or None. Cross-recording label drift means the EXACT fingerprint
            misses; we re-bind to a reanchor PROVEN on a same-app, same-page control whose
            RECORDED label is highly similar to this step's live label. NEVER green-wash:
            the seeded step also compiles a strict (non-swallowed) committed-value oracle,
            so a wrong pick fails RED. GATE 0 text+value · 1 page · 2 kind family · 3 name
            similarity >= threshold · 4 unambiguous single target."""
            _v = (_obs.get("value") or "").strip()
            _verb = (_obs.get("verb") or "").strip().lower()
            # GATE 0: only a value-bearing text input — guarantees the strict committed-
            # value oracle fires (a bare click/link has no value oracle to catch a mis-bind).
            if _verb != "type" or not _v or _v.lower() in ("true", "false", "yes", "no", "on", "off"):
                return None
            _lp = control_ledger.page_key(_obs.get("url") or _obs.get("next_url") or "")
            _ll = action_resolver._norm(_obs.get("label") or "")
            if not _ll:
                return None
            _live_kind = action_resolver._norm(_obs.get("kind") or "")
            _scored = []
            for (_clabel, _cpage, _cpl) in _cands:
                if _cpage != _lp:                                   # GATE 1: same page only
                    continue
                _ckind = action_resolver._norm(_cpl.get("kind") or "")
                if _ckind and _live_kind and _live_kind != _ckind \
                        and _live_kind not in action_resolver._ROLE_CANDIDATES.get(_ckind, ()):
                    continue                                        # GATE 2: kind family
                _score = action_resolver._similarity(_clabel, _ll)  # GATE 3: name similarity
                if _score >= control_ledger.FUZZY_REANCHOR_THRESHOLD:
                    _scored.append((_score, _cpl))
            if not _scored:
                return None
            # GATE 4: refuse a coin-flip — only seed when every passing candidate agrees on
            # the SAME target name (a single unambiguous re-bind); else heal from scratch.
            if len({(_p.get("name") or "") for (_s, _p) in _scored}) > 1:
                return None
            return dict(max(_scored, key=lambda x: x[0])[1])

        try:
            async with tenant_scoped_session(tenant_id) as _seed_session:
                for _sid in selected:
                    _tc = case_by_id.get(_sid)
                    if _tc is None:
                        continue
                    _step_fps = []         # (step, observed, fp) for THIS scenario
                    _all_fps: set = set()
                    _app_scope = ""        # P4: app-level host scope (first step with a host)
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
                    # ── P4 v1: APP-SCOPED cross-recording reuse (dual-key, additive) ──
                    # The exact per-recording read above always WINS; this widens the scope
                    # to the whole app (same host) ONLY for controls this recording has not
                    # itself proven, so a fix proven in ANOTHER recording of the SAME app is
                    # reused. control_fingerprint is already host-agnostic, so an UNCHANGED
                    # control yields a byte-identical fp across recordings — EXACT match, NO
                    # fuzzy, no new green-wash surface. Default-on; fail-open when no host.
                    _proven_app: dict = {}
                    if _app_scope and os.getenv("NEXUS_LEDGER_APP_SCOPE", "1") != "0":
                        _unmatched = {_x for _x in _all_fps if _x not in _proven}
                        if _unmatched:
                            _proven_app = await control_ledger.get_proven_fixes_by_app(
                                _seed_session, tenant_id=tenant_id, app_fingerprint=_app_scope,
                                control_fps=_unmatched)
                    # ── P4 v2: app-wide reanchor candidates for the FUZZY fallback (flag-gated,
                    # default-off; EMPTY unless NEXUS_LEDGER_FUZZY_REANCHOR is set). Fetched once
                    # per scenario; matched per-step by accessible-name similarity below.
                    _fuzzy_cands: list = []
                    if _fuzzy_on and _app_scope:
                        _app_all = await control_ledger.get_proven_fixes_by_app(
                            _seed_session, tenant_id=tenant_id, app_fingerprint=_app_scope)
                        for _flist in _app_all.values():
                            for _f in _flist:
                                _pl = _f.get("payload") or {}
                                if _f.get("fix_kind") == "reanchor" and _pl.get("name"):
                                    _fuzzy_cands.append(
                                        (action_resolver._norm(_f.get("label") or ""),
                                         _f.get("page_path") or "", _pl))
                    if not _proven and not _proven_app and not _fuzzy_cands:
                        continue
                    _ov = overrides.setdefault(_sid, {})
                    _count = 0
                    for (_st, _obs, _fp) in _step_fps:
                        # exact per-recording fix WINS; app-scope (cross-recording, same
                        # EXACT fingerprint) only fills gaps the current recording lacks.
                        _fixes = _proven.get(_fp) or _proven_app.get(_fp)
                        if not _fixes:
                            # ── P4 v2: FUZZY reanchor fallback (flag-gated, default-off) — only
                            # when EXACT + by-app both miss (label drifted across recordings).
                            # Re-bind to a similar same-app control's PROVEN reanchor, with a
                            # strict committed-value oracle so a wrong pick fails RED. Tracked in
                            # _fuzzy_seeds (NOT _seed_fp) so it can NEVER invalidate the source.
                            if _fuzzy_on and _fuzzy_cands:
                                _fz = _fuzzy_reanchor_pick(_obs, _fuzzy_cands)
                                _stn = getattr(_st, "step_number", None)
                                if _fz is not None and _stn is not None:
                                    _ov.setdefault("__reanchors__", {})[_stn] = {**_fz, "strict_oracle": True}
                                    _seeded_steps.add((_sid, _stn))
                                    _fuzzy_seeds.add((_sid, _stn, "reanchor"))
                                    _count += 1
                                    trace(event="fuzzy_seed_applied", scenario_id=_sid, step=_stn,
                                          live_label=(_obs.get("label") or "")[:80], target=_fz.get("name"))
                            continue
                        _stn = getattr(_st, "step_number", None)
                        _live_kind = self_heal._norm(_obs.get("kind") or "")
                        for _fix in _fixes:
                            _kind = _fix.get("fix_kind") or ""
                            _payload = dict(_fix.get("payload") or {})
                            if not _payload:
                                continue
                            # KIND PRE-GATE — no kind to bind on => refuse (never green-wash a homonym).
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
                                _seed_fp_label[(_sid, _ln)] = _fp; _count += 1
                            elif _kind == "interaction":
                                if _stn is None:
                                    continue
                                _ov.setdefault("__interactions__", {})[_stn] = _payload
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "interaction")] = _fp; _count += 1
                            elif _kind == "wait":
                                if _stn is None:
                                    continue
                                _ov.setdefault("__waits__", {})[_stn] = _payload
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "wait")] = _fp; _count += 1
                            elif _kind == "reanchor":   # deployed compile_case_with_overrides consumes __reanchors__
                                if _stn is None or not _payload.get("name"):
                                    continue
                                _ov.setdefault("__reanchors__", {})[_stn] = _payload
                                _seeded_steps.add((_sid, _stn))
                                _seed_fp[(_sid, _stn, "reanchor")] = _fp; _count += 1
                    if not _ov:
                        overrides.pop(_sid, None)   # nothing usable → stay empty (from-scratch)
                    if _count:
                        trace(event="ledger_seeded", scenario_id=_sid, count=_count)
        except Exception as _seed_exc:   # fully fail-open — seeding never affects the run
            overrides.clear(); _seeded_steps.clear(); _seeded_labels.clear()
            _seed_fp.clear(); _seed_fp_label.clear(); _fuzzy_seeds.clear()
            trace(event="ledger_seed_failed", error=str(_seed_exc)[:200])
        # ── END PHASE 2 SEED ──

        async def _reanchor_capture(_sid, _step, _bs):
            """LIVE-PAGE RE-ANCHOR (locator drift / renamed control): run a one-off
            HEADLESS heal-capture for this scenario (the gated afterEach posts the
            failure-state a11y controls to heal_capture_store), then resolve a
            Similo-style re-anchor for the failing LABELED control against the LIVE
            page. Best-effort; returns {name, role, confidence, rationale} or None.
            The step's own grounded oracle + the 2x confirmation gate still decide
            green on the re-prove, so a wrong re-anchor fails RED (never green-wash)."""
            try:
                _tc = case_by_id.get(_sid)
                if _tc is None:
                    return None
                # Compile the capture WITH the fixes already accumulated for the earlier
                # steps (overrides[_sid]) so the sub-run REACHES this deep failing step's
                # page before failing — otherwise it dies at the first un-healed step and
                # the gated afterEach posts the WRONG page, making every deep re-anchor
                # search the wrong nodes and refuse. Empty overrides => byte-identical to
                # the prior baseline-capture behavior.
                _spec = self_heal.compile_case_with_overrides(
                    _tc, field_meta, overrides.get(_sid, {}), heal_capture=True)
                _files = _configured_files(
                    [_tc], field_meta, base_url, data, data_by_test={},
                    browsers=["chromium"], headed=False, workers=1, retries=0,
                    edited={_sid: {"spec_path": spec_path_by_sid.get(_sid, ""),
                                   "script_source": _spec}},
                    storage_state=storage_state,
                )
                _env = {
                    "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                    "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": uuid.uuid4().hex,
                    "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
                    "NEXUS_HEAL_CAPTURE": "1",
                    "NEXUS_HEAL_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/heal-capture",
                }
                await runner_client.run_suite(_files, _env)
                _cap = await heal_capture_store.aget(
                    tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=_sid)
                if not _cap or not _cap.get("nodes"):
                    return None
                return self_heal.resolve_reanchor_for_step(
                    tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=_sid,
                    baseline_step=_bs, field_meta=field_meta, cap=_cap)
            except Exception:
                return None

        async def _capture_live_cap(_sid):
            """Live a11y snapshot of the failure-state page (the SAME headless heal-capture
            re-anchor uses) — returns the raw capture {nodes,...} or None. Reused by the
            agentic heal so the agent sees the page's REAL controls (it can't invent one)."""
            try:
                _tc = case_by_id.get(_sid)
                if _tc is None:
                    return None
                # Same deep-step fix as _reanchor_capture: compile WITH accumulated
                # overrides[_sid] so the agent sees the REAL controls of the actual
                # failing page, not whatever page the un-healed baseline died on.
                _spec = self_heal.compile_case_with_overrides(
                    _tc, field_meta, overrides.get(_sid, {}), heal_capture=True)
                _files = _configured_files(
                    [_tc], field_meta, base_url, data, data_by_test={},
                    browsers=["chromium"], headed=False, workers=1, retries=0,
                    edited={_sid: {"spec_path": spec_path_by_sid.get(_sid, ""),
                                   "script_source": _spec}},
                    storage_state=storage_state,
                )
                _env = {
                    "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                    "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": uuid.uuid4().hex,
                    "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
                    "NEXUS_HEAL_CAPTURE": "1",
                    "NEXUS_HEAL_ENDPOINT": f"{_INGEST_BASE}/api/v1/test-runs/heal-capture",
                }
                await runner_client.run_suite(_files, _env)
                return await heal_capture_store.aget(
                    tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=_sid)
            except Exception:
                return None

        async def _try_agentic(_sid, _step, _bs, _observed, _diag, _f):
            """AGENTIC fallback (gated, default-off): before escalating a GROUNDABLE failure
            to a human, let the LLM agent reason about it against the LIVE page and propose a
            grounded fix (rebind {name,kind} / wait). The agent never declares green and cannot
            fabricate a selector (ungrounded picks are dropped at validation); the step's own
            orthogonal oracle + the 2x confirm still gate green. REFUSE families (real
            regression / auth / data / variant) are EXCLUDED — those stay honest escalations.
            Returns True when a grounded fix was applied (caller should `continue` to re-prove)."""
            if not (ctx or {}).get("enable_agentic_heal", False):
                return False
            from ..services.test_factory import agentic_heal
            if (_diag or {}).get("cause") in agentic_heal.REFUSE_CAUSES:
                return False
            _akey = (_sid, _step, "agentic")
            if _akey in _ra_done:
                return False
            _ra_done.add(_akey)
            _cap = await _capture_live_cap(_sid)
            if not _cap or not _cap.get("nodes"):
                trace(event="agentic_no_capture", scenario_id=_sid, step=_step)
                return False
            _tc = case_by_id.get(_sid)
            _recorded = {}
            for _st in (getattr(_tc, "steps", None) or []):
                _o = self_heal._observed(_st) or {}
                _recorded[getattr(_st, "step_number", None)] = {
                    "label": _o.get("label", ""), "kind": _o.get("kind", ""),
                    "value": _o.get("value", ""),
                    "expected": getattr(_st, "expected", "") or getattr(_st, "expected_result", "")}
            _res = await agentic_heal.propose(
                failing=[{"step_number": _step, "error_message": _f.get("error_message", "")}],
                recorded_by_step=_recorded, nodes=_cap.get("nodes"),
                tier_name=(ctx or {}).get("agentic_tier", "tier_premium"),
                min_confidence=float((ctx or {}).get("agentic_min_confidence", 0.7) or 0.7))
            _applied = _res.get("applied") or []
            if not _applied:
                trace(event="agentic_no_fix", scenario_id=_sid, step=_step, ok=bool(_res.get("ok")),
                      error=str(_res.get("error", ""))[:160], refused=len(_res.get("refused") or []))
                return False
            _sid_ov = overrides.setdefault(_sid, {})
            _did = False
            for _fx in _applied:
                _ch = _fx.get("channel"); _pl = _fx.get("payload") or {}; _stn = _fx.get("step_number")
                if _ch == "reanchors" and _pl.get("name"):
                    _sid_ov.setdefault("__reanchors__", {})[_stn] = _pl
                    # A re-anchor fixes the NAME; any accumulated interaction recipe fixes
                    # the KIND/choreography — they COMPOSE (the compiler applies the
                    # reanchor to `observed` first, then runs the recipe on the renamed
                    # control, e.g. step-22 conditional_text + reanchor). We deliberately
                    # keep both so a renamed boolean/combobox heals WITH its grounded
                    # commit-oracle rather than degrading to an un-grounded click.
                    _did = True
                    trace(event="heal_applied", scenario_id=_sid, step=_stn,
                          label=_observed.get("label", ""),
                          fix="agentic:rebind:" + str(_pl.get("name", "")), attempt=1)
                elif _ch == "waits":
                    from ..services.script_factory.wait_scope_resolver import build_wait_scope_for as _bwsf
                    _wsobs = self_heal._observed(self_heal._baseline_step(_tc, _stn)) or {}
                    _ws = _bwsf(_wsobs, error_message=_f.get("error_message", ""))
                    if _ws:
                        _sid_ov.setdefault("__waits__", {})[_stn] = _ws
                        _did = True
                        trace(event="heal_applied", scenario_id=_sid, step=_stn,
                              label=_observed.get("label", ""), fix="agentic:wait", attempt=1)
            return _did

        if _headless_prove:
            trace(event="prove_mode", mode="headless", workers=_prove_w)

        # T4.2 PER-HEAL WALL-CLOCK SLA (additive, default-off). ctx['sla_seconds']
        # absent/0 => unbounded => the only bound stays _AUTO_HEAL_MAX_ITERS (today's
        # behavior, byte-identical). When set, a blown budget STOPS toward a human
        # honestly — never a silent hang, never a green-wash (timing out is strictly
        # more conservative than the oracle).
        _budget = _make_heal_budget(ctx)

        # T4.2 HARNESS-FLAKE PRE-PASS (additive, default-off). When ctx['flake_samples']
        # > 0, re-run the BASELINE (un-healed, no overrides) bundle N times and record
        # how often it reproduces green. This is the flake-correction denominator for
        # any published "proven green": it measures the harness's own jitter on a
        # control the caller already believes green; it can only ever under-count green,
        # so it cannot make a real failure look green.
        try:
            _flake_n = int(ctx.get("flake_samples") or 0)
        except (TypeError, ValueError):
            _flake_n = 0
        if _flake_n > 0:
            _ctrl_files = _configured_files(
                sel_cases, field_meta, base_url, data, data_by_test={},
                browsers=["chromium"], headed=_prove_headed, workers=_prove_w, retries=0,
                edited=dict(base_edited), storage_state=storage_state,
            )
            _ctrl_env = {
                "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_BASE_URL": base_url,
                "NEXUS_ENV": "nexus-runner",
            }

            async def _flake_dispatch(_f, _e):
                return await _dispatch_prove(_f, _e, ctx)

            async def _flake_correlate(_rid):
                # SAME proven-green verdict the loop uses, by the SAME run-id
                # correlation primitive. None => could not correlate (NOT green).
                _tl = None
                for _ in range(12):
                    await asyncio.sleep(1.5)
                    async with tenant_scoped_session(tenant_id) as session:
                        _real = await find_run_by_ci_run_id(
                            session, artifact_id=artifact_id, tenant_id=tenant_id, ci_run_id=_rid,
                        )
                        if _real is None:
                            continue
                        _tl = await build_run_timeline_by_id(
                            session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=_real,
                        )
                    break
                if _tl is None:
                    return None
                _m = {sc.get("scenario_id"): sc for sc in (_tl.get("scenarios") or [])}
                _FST = {"failed", "broken", "timed_out"}
                def _pp(_sid):
                    _sc = _m.get(_sid)
                    _steps = (_sc or {}).get("steps") or []
                    return bool(_sc) and bool(_steps) \
                        and not any((st.get("status") in _FST) for st in _steps) \
                        and any((st.get("status") == "passed") for st in _steps)
                return (not self_heal.first_failures(_tl, selected)) \
                    and all(_pp(_sid) for _sid in selected)

            _flake = await _measure_harness_flake(
                files=_ctrl_files, env_base=_ctrl_env, selected=selected,
                samples=_flake_n, dispatch=_flake_dispatch,
                correlate_green=_flake_correlate, new_run_id=lambda: uuid.uuid4().hex,
            )
            job.update(harness_flake=_flake)
            trace(event="harness_flake", samples=_flake["samples"],
                  green=_flake["green"], flake_rate=_flake["flake_rate"])

        for iteration in range(1, _AUTO_HEAL_MAX_ITERS + 1):
            # T4.2 SLA: honest needs_human on a blown wall-clock budget (never a
            # silent hang). Checked at the top of each iteration BEFORE more work.
            if _budget.exceeded():
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"per-heal SLA budget exhausted "
                                        f"({_budget.budget_seconds:.0f}s) after "
                                        f"{iteration - 1} iteration(s) — stopping toward a human "
                                        "rather than running indefinitely"))
                trace(event="stop_sla_budget", iteration=iteration,
                      elapsed=round(_budget.elapsed(), 1))
                await _persist_job(run_id)
                return
            # Build candidate specs for any scenario with accumulated corrections.
            edited = dict(base_edited)
            candidate_specs: dict[str, str] = {}
            for sid in selected:
                tc = case_by_id.get(sid)
                if tc is None:
                    continue
                ov = overrides.get(sid)
                if ov:
                    spec = self_heal.compile_case_with_overrides(tc, field_meta, ov)
                    # ASSERTION-IMMUTABILITY GUARD: a heal candidate may never carry FEWER
                    # grounded outcome assertions than the baseline (no weaken-to-go-green).
                    _ok, _msg = self_heal.assert_assertions_unchanged(
                        compile_case(tc, field_meta, parametrize=True), spec)
                    if not _ok:
                        job.update(status="failed", terminal_state="needs_human",
                                   stop_reason=f"heal refused for the test case: {_msg}")
                        trace(event="stop_assertion_guard", scenario_id=sid)
                        return
                    candidate_specs[sid] = spec
                    edited[sid] = {"spec_path": spec_path_by_sid.get(sid, ""), "script_source": spec}

            files = _configured_files(
                sel_cases, field_meta, base_url, data, data_by_test={},
                browsers=["chromium"], headed=_prove_headed, workers=_prove_w, retries=0,
                edited=edited, storage_state=storage_state,
            )
            sub_run_id = uuid.uuid4().hex
            env = {
                "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": sub_run_id,
                "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
            }
            # T4.1: dispatch via run_suite (headless, parallel) or run_live
            # (headed, single display). Either way the run is terminal when this
            # returns; the GREEN verdict comes downstream from the oracle below.
            _ran = await _dispatch_prove(files, env, ctx)
            if not _ran:
                job.update(status="error", terminal_state="error",
                           stop_reason="runner busy or unreachable — could not start the re-run")
                return
            trace(iteration=iteration, event="run_started", scripts=len(sel_cases),
                  prove_mode=("headless" if _headless_prove else "headed"))
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
            # ── PHASE 2 STALE-SEED CLEAR ── On the FIRST prove, any SEEDED step/label that
            # still FAILED carried a STALE memo (the app changed since it was proven). Drop
            # that seed and re-prove so the loop heals it FRESH — a stale seed is thus never
            # worse than from-scratch, and never silently rides along into a later iteration.
            if iteration == 1 and (_seeded_steps or _seeded_labels):
                _failed_pairs = {(f.get("scenario_id"), f.get("step_number")) for f in failures}
                _cleared = 0
                _stale_marks: list = []   # (control_fp, fix_kind) to quarantine (P3)
                _CHAN_KIND = {"__interactions__": "interaction", "__waits__": "wait", "__reanchors__": "reanchor"}
                for (_csid, _cstn) in list(_seeded_steps):
                    if (_csid, _cstn) in _failed_pairs:
                        _cov = overrides.get(_csid) or {}
                        for _chan in ("__interactions__", "__waits__", "__reanchors__"):
                            if _cstn in (_cov.get(_chan) or {}):
                                _cov[_chan].pop(_cstn, None); _cleared += 1
                                if (_csid, _cstn, _CHAN_KIND[_chan]) in _fuzzy_seeds:
                                    # P4 fuzzy seed: cleared like any stale seed (heal fresh),
                                    # but NEVER marked stale — a B-side fuzzy mis-match must not
                                    # invalidate the source recording's correct exact fix.
                                    trace(event="fuzzy_seed_cleared", scenario_id=_csid, step=_cstn)
                                    continue
                                _cfp = _seed_fp.get((_csid, _cstn, _CHAN_KIND[_chan]))
                                if _cfp:
                                    _stale_marks.append((_cfp, _CHAN_KIND[_chan]))
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
                                _clabels.discard(_cln); _cleared += 1
                                _cfp = _seed_fp_label.get((_csid, _cln))
                                if _cfp:
                                    _stale_marks.append((_cfp, "control_kind"))
                if _cleared:
                    # P3: PERSIST the staleness so a permanently-changed control is
                    # QUARANTINED (stops being re-seeded every run), not merely dropped
                    # for this run. Fail-open: invalidation is an optimization that only
                    # ever REMOVES a seed — it can never gate a run or green-wash.
                    if _stale_marks:
                        try:
                            async with tenant_scoped_session(tenant_id) as _stsess:
                                for (_sfp, _skind) in _stale_marks:
                                    await control_ledger.mark_seed_stale(
                                        _stsess, tenant_id=tenant_id, app_key=artifact_id,
                                        control_fp=_sfp, fix_kind=_skind, invalidated_by_run=run_id)
                                await _stsess.commit()
                        except Exception as _stexc:
                            trace(event="ledger_mark_stale_failed", error=str(_stexc)[:200])
                    trace(event="ledger_seed_cleared_stale", cleared=_cleared, marked=len(_stale_marks))
                    continue   # re-prove from the cleaned overrides; the loop heals the rest fresh
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

            # CONFIRMATION GATE — never freeze a Clean Run V1 (or write a positive
            # flywheel label) on ONE lucky/flaky green. Require a 2nd INDEPENDENT green
            # re-run of the SAME candidate; a green-then-red is treated as NOT proven and
            # escalated. "Proven" = reproduced, not a single happy run.
            if all_green:
                _csub = uuid.uuid4().hex
                _cenv = {**env, "NEXUS_RUN_ID": _csub}
                # T4.1: confirmation re-run uses the SAME dispatch (headless or
                # headed) — an INDEPENDENT 2nd run of the SAME candidate. The
                # confirmation oracle (_cpp / first_failures) is unchanged.
                _cstarted = await _dispatch_prove(files, _cenv, ctx)
                _ctl = None
                if _cstarted:
                    for _ccorr in range(12):
                        await asyncio.sleep(1.5)
                        async with tenant_scoped_session(tenant_id) as session:
                            _crid = await find_run_by_ci_run_id(
                                session, artifact_id=artifact_id, tenant_id=tenant_id, ci_run_id=_csub,
                            )
                            if _crid is None:
                                continue
                            _ctl = await build_run_timeline_by_id(
                                session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=_crid,
                            )
                        break
                _cby = {sc.get("scenario_id"): sc for sc in ((_ctl or {}).get("scenarios") or [])}
                def _cpp(_sid, _m=_cby):
                    _sc = _m.get(_sid)
                    _steps = (_sc or {}).get("steps") or []
                    return bool(_sc) and bool(_steps) \
                        and not any((st.get("status") in _FAILST) for st in _steps) \
                        and any((st.get("status") == "passed") for st in _steps)
                _confirmed = (_ctl is not None) \
                    and (not self_heal.first_failures(_ctl, selected)) \
                    and all(_cpp(_sid) for _sid in selected)
                if not _confirmed:
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason="the heal passed once but did NOT reproduce on a "
                                           "confirmation re-run (likely a flaky/transient green) — "
                                           "not freezing it as proven; needs a human")
                    trace(event="stop_unconfirmed_green", iteration=iteration)
                    return

            if all_green:
                # METAMORPHIC ACCEPTANCE (T1.3): before freezing a Clean Run, roll up whether
                # each EXECUTED step asserts a recorded OUTCOME (grounded) or is
                # outcome_not_grounded. A grounded step that fails its oracle already broke
                # all_green upstream; this surfaces the steps that assert NOTHING so an
                # all-green-but-HOLLOW suite (no step proves any recorded outcome) is VISIBLE,
                # never silently frozen as 'proven'. Stamp the job either way; refuse to
                # persist a fully-hollow green.
                _steps_by_sid = {}
                for _sid in selected:
                    _sco = _by_id.get(_sid) or {}
                    _steps_by_sid[_sid] = [st.get("step_number") for st in (_sco.get("steps") or [])
                                           if st.get("status") != "skipped" and st.get("step_number") is not None]
                _specs_g = {}
                for _sid in selected:
                    _tcg = case_by_id.get(_sid)
                    if _tcg is None:
                        continue
                    _specs_g[_sid] = candidate_specs.get(_sid) or compile_case(_tcg, field_meta, parametrize=True)
                grounding = self_heal.suite_outcome_grounding(_specs_g, _steps_by_sid)
                job.update(outcome_grounding=grounding)
                trace(event="suite_outcome_grounding", grounded=grounding["grounded"],
                      outcome_not_grounded=grounding["outcome_not_grounded"], hollow=grounding["hollow"])
                if grounding["hollow"]:
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=("suite ran green but NO step asserts the recorded business "
                                            "OUTCOME (every step is outcome_not_grounded) — refusing to freeze "
                                            "a hollow Clean Run; enrich the recorded outcomes or confirm manually"))
                    trace(event="stop_hollow_suite")
                    return
                # ── SPA SAME-URL NAVIGATION green-wash gate (Layer #4) ───────────
                # A SUBMIT/Next step can assert toHaveURL(recorded next page) yet, in a
                # single-page app, the URL never changes — so the assertion is trivially
                # true and the step proves NOTHING about the transition. Detect such
                # steps (grounded in observed url/next_url + the ABSENCE of any content
                # oracle), restricted to steps that actually EXECUTED in this green run,
                # and REFUSE to freeze — escalate honestly so a human adds a destination
                # content Expected Result. No auto-fix; never green-wash. Returns [] for
                # ordinary multi-URL flows → byte-identical to today.
                try:
                    from ..services.test_factory import recording_quality as _recq2
                    _gw = []
                    for _sid in selected:
                        _tcg2 = case_by_id.get(_sid)
                        if _tcg2 is None:
                            continue
                        _exec = set(_steps_by_sid.get(_sid) or [])
                        for _f in _recq2.detect_same_url_nav_greenwash(getattr(_tcg2, "steps", None) or []):
                            if not _exec or _f.get("step_number") in _exec:
                                _gw.append((_sid, _f))
                except Exception:
                    _gw = []
                if _gw:
                    _sid0, _f0 = _gw[0]
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=(f"step {_f0.get('step_number')}: asserts it navigated to "
                                            f"'{_f0.get('path')}', but that is the SAME URL the page is already on "
                                            "(a single-page-app view change) and the recording captured no "
                                            "destination content to verify — the check would pass WITHOUT proving "
                                            "the transition. Add an Expected Result naming an element that appears "
                                            "on the destination view, or confirm the step."),
                               stop_diag={"same_url_greenwash": [f for _, f in _gw]})
                    trace(event="stop_same_url_greenwash", count=len(_gw),
                          steps=[f.get("step_number") for _, f in _gw])
                    await _persist_job(run_id)
                    return
                # T5.5 3-TIER HEAL POLICY (additive, default-off). Consulted AFTER every
                # existing gate has already passed (2 confirmed greens + non-hollow +
                # no refuse-cause). It can ONLY make persistence STRICTER: a marginal
                # heal (low diagnose confidence, or proven-but-not-outcome-grounded)
                # that today auto-persists as PROPOSED now STOPS at needs_human pending
                # an explicit human approve. With the flag off it returns AUTO => this
                # block is a no-op and the persist below is byte-identical to today.
                # 'grounded' here means at least one executed step asserts a recorded
                # outcome (the suite is not hollow — already enforced above).
                _outcome_grounded_suite = bool(candidate_specs) and grounding["grounded"] > 0
                _tier = heal_policy.evaluate_heal_tier(
                    confidence=heal_min_confidence,
                    outcome_grounded=_outcome_grounded_suite,
                    cause=heal_worst_cause, confirmed_green=True,
                )
                job.update(heal_policy_tier=_tier["tier"], heal_policy_reason=_tier["reason"],
                           heal_min_confidence=round(heal_min_confidence, 2))
                trace(event="heal_policy", tier=_tier["tier"],
                      confidence=round(heal_min_confidence, 2), reason=_tier["reason"])
                if not _tier["may_auto_persist"]:
                    # Proven green but the policy demands a human before it becomes the
                    # active source. Persist NOTHING; the candidate is reproducible via
                    # the trace + can be re-run + approved manually.
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=("heal policy: " + _tier["reason"]))
                    await _persist_job(run_id)
                    return
                # FULL GREEN (confirmed on 2 independent re-runs) → persist Clean Run - V1.
                healed = [{"test_case_id": sid, "spec_path": spec_path_by_sid.get(sid, ""),
                           "script_source": src} for sid, src in candidate_specs.items()]
                version_no = None
                if healed:
                    async with tenant_scoped_session(tenant_id) as session:
                        rows = await script_versions.batch_save_clean_run_version(
                            session, artifact_id=artifact_id, tenant_id=tenant_id,
                            healed=healed, clean_run_session_id=run_id, n_healed=len(healed),
                        )
                        # Part-11 evidence per healed test (FAIL-CLOSED, atomic).
                        _vmap = {getattr(r, 'test_case_id', ''): getattr(r, 'version_no', 0) for r in (rows or [])}
                        for _h in healed:
                            await heal_evidence.record_heal_event(
                                session, tenant_id=tenant_id, artifact_id=artifact_id,
                                event_type="heal_persisted", actor="nexus-autoheal",
                                scenario_id=(_h.get('test_case_id') or ''),
                                fix_kind="control_kind_fix", verified_green=True,
                                version_no=_vmap.get(_h.get('test_case_id'), 0), run_id=run_id,
                                reason_for_change=f"Clean Run - V1 (auto-healed {len(healed)} step(s), verified green)",
                            )
                        await session.commit()
                        version_no = rows[0].version_no if rows else None
                # ── PHASE 1: memo oracle-PROVEN heals to the app-level control ledger ──
                # Best-effort, fail-open, ADDITIVE. Runs in a SEPARATE tenant-scoped session
                # AFTER the Clean Run V1 commit above has durably succeeded, so a ledger error
                # (e.g. the table is absent pre-migration) can NEVER poison the version
                # transaction or alter the run outcome. Each healed step is fingerprinted from
                # its OWN baseline `observed` (the ORIGINAL recorded label/kind/url — reanchors
                # never mutate it), so step context is preserved and a reanchor keys off the
                # ORIGINAL name (a future scenario starting from that name will match). Reuse
                # (Phase 2) is always re-gated by the step's own oracle, so a memoed entry can
                # never make a wrong test green. app_key = artifact_id (Phase-1 reuse scope).
                _RESERVED_OV = {"__interactions__", "__reanchors__", "__waits__", "__force_open_shadow__"}
                try:
                    async with tenant_scoped_session(tenant_id) as _lsession:
                        for _lsid in (overrides or {}):
                            _lsov = overrides.get(_lsid) or {}
                            _ltc = case_by_id.get(_lsid)
                            if _ltc is None:
                                trace(event="ledger_skip", scenario_id=_lsid, reason="case_not_found")
                                continue
                            _ck_labels = {k for k in _lsov if k not in _RESERVED_OV}
                            _l_ints = _lsov.get("__interactions__") or {}
                            _l_reanchors = _lsov.get("__reanchors__") or {}
                            _l_waits = _lsov.get("__waits__") or {}
                            # (1) step-number-keyed channels → fingerprint each from its OWN baseline observed.
                            for _lstn in (set(_l_ints) | set(_l_reanchors) | set(_l_waits)):
                                _lbs = self_heal._baseline_step(_ltc, _lstn)
                                if _lbs is None:
                                    trace(event="ledger_skip", scenario_id=_lsid, step=_lstn, reason="step_not_found")
                                    continue
                                _lobs = self_heal._observed(_lbs) or {}
                                _lpage = control_ledger.page_key(_lobs.get("url") or _lobs.get("next_url") or "")
                                _lapp = control_ledger.app_key_from_url(_lobs.get("url") or _lobs.get("next_url"))  # P4 app scope
                                _lfp = control_ledger.control_fingerprint(_lobs, page_path=_lpage)
                                if not _lfp:
                                    trace(event="ledger_skip", scenario_id=_lsid, step=_lstn, reason="ungroundable_label")
                                    continue
                                _llabel = _lobs.get("label") or ""
                                if _lstn in _l_reanchors:   # key off the ORIGINAL observed; payload carries the new name
                                    await control_ledger.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id, control_fp=_lfp,
                                        fix_kind="reanchor", payload=dict(_l_reanchors[_lstn] or {}),
                                        label=_llabel, page_path=_lpage, proven_by_run=run_id, app_fingerprint=_lapp)
                                if _lstn in _l_ints:
                                    await control_ledger.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id, control_fp=_lfp,
                                        fix_kind="interaction", payload=dict(_l_ints[_lstn] or {}),
                                        label=_llabel, page_path=_lpage, proven_by_run=run_id, app_fingerprint=_lapp)
                                if _lstn in _l_waits:
                                    await control_ledger.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id, control_fp=_lfp,
                                        fix_kind="wait", payload=dict(_l_waits[_lstn] or {}),
                                        label=_llabel, page_path=_lpage, proven_by_run=run_id, app_fingerprint=_lapp)
                            # (2) control-kind (label-keyed) fixes → scan baseline steps + look up the
                            #     override BY each step's OWN normalized label (preserves step context).
                            if _ck_labels:
                                for _lst in (getattr(_ltc, "steps", None) or []):
                                    _lobs = self_heal._observed(_lst) or {}
                                    _lln = self_heal._norm(_lobs.get("label") or "")
                                    if not _lln or _lln not in _ck_labels:
                                        continue
                                    _lpage = control_ledger.page_key(_lobs.get("url") or _lobs.get("next_url") or "")
                                    _lapp = control_ledger.app_key_from_url(_lobs.get("url") or _lobs.get("next_url"))  # P4 app scope
                                    _lfp = control_ledger.control_fingerprint(_lobs, page_path=_lpage)
                                    if not _lfp:
                                        trace(event="ledger_skip", scenario_id=_lsid,
                                              step=getattr(_lst, "step_number", None), reason="ungroundable_label")
                                        continue
                                    await control_ledger.record_proven_fix(
                                        _lsession, tenant_id=tenant_id, app_key=artifact_id, control_fp=_lfp,
                                        fix_kind="control_kind", payload=dict(_lsov.get(_lln) or {}),
                                        label=(_lobs.get("label") or ""), page_path=_lpage, proven_by_run=run_id,
                                        app_fingerprint=_lapp)
                        await _lsession.commit()
                    trace(event="ledger_memoized", healed_count=len(healed))
                except Exception as _lexc:   # fully fail-open — never affect Clean Run V1
                    trace(event="ledger_write_failed", error=str(_lexc)[:200])
                # ── END PHASE 1 ──
                job.update(status="passed", terminal_state="clean_run_v1",
                           clean_run_version=version_no, healed_count=len(healed))
                trace(event="clean_run_v1", healed_count=len(healed), version_no=version_no)
                return

            # No FAILING step, yet all_green was False — a selected test didn't prove a
            # clean pass: it was missing from the results, had zero steps, or was SKIPPED
            # outright (e.g. a mid-flow UNPROVEN step compiles to test.skip(), which
            # Playwright applies to the WHOLE test → no failures AND no proven pass).
            # Escalate honestly instead of crashing on failures[0] ('list index out of range').
            if not failures:
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason="the re-run produced no failing steps, but a selected test "
                                       "could not be confirmed green — it was skipped or missing in "
                                       "the results (commonly a mid-flow UNPROVEN step that skips the "
                                       "whole test). Needs a human: confirm or repair that step.")
                trace(event="stop_no_failures_no_green")
                await _persist_job(run_id)
                return

            # Take the first failing step and decide.
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
            # T5.5: remember the weakest grounded confidence + cause among applied
            # heals (consumed by the 3-tier policy at persist). Recorded BEFORE the
            # branch decisions; a non-auto-fixable cause stops the loop anyway.
            try:
                _dc = float(diag.get("confidence") or 0.0)
            except (TypeError, ValueError):
                _dc = 0.0
            if _dc < heal_min_confidence:
                heal_min_confidence = _dc
                heal_worst_cause = diag.get("cause", "") or ""
            # ── REAL-REGRESSION → AUTO-AUTHORED DEFECT (V0: the wedge) ───────────
            # Before ANY heal routing: is this a REAL APPLICATION BUG? Two grounded
            # signals say so — the recorded outcome was CONTRADICTED (diagnose →
            # REAL_REGRESSION), or a SERVER/NETWORK error fired in the failing step's
            # window (network_oracle; a 5xx is near-dispositive and the cheapest real-
            # bug signal). Either way we DO NOT heal — we auto-author a structured,
            # replayable DEFECT (recorded repro + precise failure point + expected/
            # actual + evidence) and escalate honestly. The defect + its markdown ride
            # on the run job, so the existing "Copy defect"/"Download .md" surface shows
            # an AUTO-authored bug instead of a hand-written one. No competitor ships
            # this: heals prove an element resolves, never that the step behaved right.
            try:
                from ..services.test_factory import network_oracle as _netq, defect_report as _defq
                _net = _netq.detect(f, observed)
            except Exception:
                _netq = None
                _defq = None
                _net = None
            _is_real_bug = (diag.get("cause") == "REAL_REGRESSION") or (
                _netq is not None and _netq.is_real_bug_signal(_net))
            if _is_real_bug and _defq is not None:
                _defect = _defq.build_defect(
                    tc=tc, failing_step_number=step, diag=diag, network=_net,
                    error_message=f.get("error_message", ""), base_url=base_url,
                    scenario_id=sid, baseline_screenshot=(getattr(bs, "screenshot", "") or None),
                    part11_ref=f"{run_id}:{sid}:{step}")
                _dmd = _defq.defect_to_markdown(_defect)
                # Best-effort Part-11 ledger entry — the immutable record that we found a
                # real bug and REFUSED to heal it (never blocks the escalation).
                try:
                    async with tenant_scoped_session(tenant_id) as _s:
                        await heal_evidence.record_heal_event(
                            _s, tenant_id=tenant_id, artifact_id=artifact_id,
                            event_type="real_regression_filed", actor="nexus-autoheal",
                            scenario_id=sid, fix_kind="none", verified_green=False,
                            version_no=0, run_id=run_id,
                            reason_for_change=("Auto-authored defect (REFUSED to heal a real "
                                               "regression): " + _defect.get("title", ""))[:480])
                        await _s.commit()
                except Exception:
                    pass
                _headline = ((_net or {}).get("detail") or diag.get("recommended_action")
                             or diag.get("cause_label", "Real regression"))
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"step {step}: REAL regression — {_headline}. Auto-authored a "
                                        "defect with repro steps + evidence (Copy defect / Download .md)."),
                           stop_diag={**diag, "network": _net},
                           defect=_defect, defect_markdown=_dmd)
                trace(event="stop_real_regression_defect", scenario_id=sid, step=step,
                      network=bool(_net), severity=_defect.get("severity"))
                await _persist_job(run_id)
                return
            # ── RECORDING-QUALITY classifier (Layer #3) ──────────────────────────
            # Before ANY heal routing: is this failure a RECORDING artifact (a
            # double-captured / duplicate step) rather than app drift? Churning
            # control-kind / re-anchor fixes on such a step is futile and ends in a
            # vague "needs a human". Recognise it up front and escalate with the
            # PRECISE, grounded reason + a concrete suggested fix. Grounded purely in
            # the recording's own steps (labels/verbs/urls); conservative (only a
            # back-to-back same-label same-page re-capture of a PASSED step fires);
            # ESCALATES only — never skips a step or flips one green.
            try:
                from ..services.test_factory import recording_quality as _recq
                _passed_steps = [st.get("step_number")
                                 for st in ((_by_id.get(sid) or {}).get("steps") or [])
                                 if st.get("status") == "passed" and st.get("step_number") is not None]
                _rq = _recq.classify_recording_quality(
                    baseline_step=bs,
                    scenario_steps=(getattr(tc, "steps", None) or []),
                    passed_step_numbers=_passed_steps,
                )
            except Exception:
                _recq = None
                _rq = None
            if _rq:
                _ms = None
                try:
                    _ms = _recq.scenario_missing_submit(getattr(tc, "steps", None) or [])
                except Exception:
                    _ms = None
                _msg = f"step {step}: recording-quality — {_rq['rationale']} {_rq['suggestion']}"
                if _ms:
                    _msg += f"  (Also: {_ms['rationale']} {_ms['suggestion']})"
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=_msg, stop_diag={**diag, "recording_quality": _rq})
                trace(event="stop_recording_quality", scenario_id=sid, step=step,
                      kind=_rq["kind"], of_step=_rq.get("of_step"))
                await _persist_job(run_id)
                return
            # ── L5 WAIT/SCOPE channel (timing/materialize/portal/frame) ───────────
            # DEFAULT-ON (timing is the #1 cause of UI-test failure, ~45%; research:
            # a condition-based waitFor FULLY fixes ~55% of async-wait flakiness where
            # fixed sleeps never do). The recipes are CONDITION-based — waitFor /
            # scroll-until-materialize / frame-by-url, never a fixed sleep — and fire
            # ONLY on a GROUNDED signal (build_wait_scope_for returns None otherwise →
            # no recipe → byte-identical). REAL_REGRESSION never reaches here as auto-
            # fixable, and the preamble only WAITS/SCOPES then THROWS RED on a
            # genuinely-absent control — the step's own outcome oracle still gates
            # green, so this can never turn a real defect green. Opt OUT explicitly
            # with ctx['enable_wait_scope_heal']=False.
            if (diag["cause"] != "WRONG_CONTROL_KIND"
                    and (ctx or {}).get("enable_wait_scope_heal", True)
                    and diag["cause"] in ("LOCATOR_NOT_FOUND", "NEEDS_REVIEW", "FLAKE")):
                from ..services.script_factory.wait_scope_resolver import build_wait_scope_for
                _wkey = (sid, step)
                _ws_sid_ov = overrides.setdefault(sid, {})
                _ws_chan = _ws_sid_ov.setdefault("__waits__", {})
                _ws_attempts = attempts.get(_wkey, 0)
                if step not in _ws_chan and _ws_attempts <= max_attempts:
                    _ws = build_wait_scope_for(
                        observed,
                        error_message=f.get("error_message", ""),
                        baseline_ms=(f.get("baseline_ms") or (bs.observed.get("latency_ms") if bs is not None and isinstance(getattr(bs, "observed", None), dict) else None)),
                        frame_url=f.get("frame_url", "") or "",
                    )
                    if _ws:
                        attempts[_wkey] = _ws_attempts + 1
                        _ws_chan[step] = _ws
                        trace(event="heal_applied", scenario_id=sid, step=step,
                              label=observed.get("label", ""),
                              fix="wait_scope:" + _ws.get("kind", ""), attempt=attempts[_wkey])
                        continue  # re-compile with __waits__ + re-prove (oracle unchanged)
                    # no grounded recipe -> fall through to the honest needs_human return.

            # ── ANY-UI SCOPE channel (closed shadow / canvas — Layer #5) ──────────
            # When the LIVE failure says the control sits in a CLOSED shadow root or on
            # a NON-DOM surface (canvas/WebGL/Flutter), neither the DOM recipes nor the
            # wait/scope recipes (open <iframe> + portal are handled above) can reach
            # it. Classify it from the GROUNDED live error and act honestly:
            #   • closed shadow  → an OPT-IN shim (force shadow roots open; test-env-only
            #     page-init preamble, no oracle weakened). Auto-applied ONLY when
            #     explicitly enabled (ctx['enable_closed_shadow_shim'], default OFF);
            #     otherwise escalate with the precise reason + the actionable opt-in.
            #   • canvas / no-DOM → REFUSE: there is no DOM/AX grounding to heal against,
            #     so escalate honestly. The visual-propose tier stays inert (no blind
            #     coordinate) unless a GPU/VLM grounding node is provisioned.
            # Fires ONLY on a grounded hard-UI signal (detect_any_ui returns None
            # otherwise) → no behaviour change for ordinary DOM controls.
            try:
                from ..services.script_factory.any_ui_resolver import detect_any_ui
                _aui = detect_any_ui(observed, error_message=f.get("error_message", ""))
            except Exception:
                _aui = None
            if _aui and _aui.get("kind") == "open_shadow_shim":
                _akey = (sid, step, "aui")
                if (ctx or {}).get("enable_closed_shadow_shim", False) \
                        and attempts.get(_akey, 0) <= max_attempts \
                        and "__force_open_shadow__" not in overrides.get(sid, {}):
                    attempts[_akey] = attempts.get(_akey, 0) + 1
                    overrides.setdefault(sid, {})["__force_open_shadow__"] = True
                    trace(event="heal_applied", scenario_id=sid, step=step,
                          label=observed.get("label", ""), fix="any_ui:open_shadow_shim",
                          attempt=attempts[_akey])
                    continue  # re-compile with the open-shadow preamble + re-prove (oracle gates)
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"step {step}: the control '{observed.get('label','')}' sits in a CLOSED "
                                        "shadow root that Playwright cannot pierce. Enable the closed-shadow shim "
                                        "(a test-env open-mode page preamble — nothing the app asserts changes, no "
                                        "oracle weakened) to heal it, or confirm/repair the step."),
                           stop_diag={**diag, "any_ui": _aui})
                trace(event="stop_closed_shadow", scenario_id=sid, step=step)
                await _persist_job(run_id)
                return
            if _aui and _aui.get("kind") == "visual_propose":
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=(f"step {step}: the control '{observed.get('label','')}' sits on a NON-DOM "
                                        "surface (canvas/WebGL/Flutter) with no DOM/accessibility grounding — "
                                        "auto-heal cannot ground a locator and will NOT guess a coordinate. Needs "
                                        "a human (visual confirmation)."),
                           stop_diag={**diag, "any_ui": _aui})
                trace(event="stop_non_dom_surface", scenario_id=sid, step=step)
                await _persist_job(run_id)
                return

            if diag["cause"] != "WRONG_CONTROL_KIND":
                # AGENTIC fallback (gated, default-off): before we escalate a GROUNDABLE
                # failure to a human ("Analyze & fix"), let the LLM agent reason about it
                # against the LIVE page and propose a grounded rebind/wait. It cannot
                # fabricate a selector and cannot touch the REFUSE families (handled inside
                # _try_agentic); the step's own oracle + 2x confirm still gate green. On a
                # grounded fix → re-prove; otherwise fall through to the honest escalate.
                if await _try_agentic(sid, step, bs, observed, diag, f):
                    continue
                # State / precondition / regression / A-B-variant families are REFUSE-and-
                # escalate by design (auto_fixable=False): healing them would green-wash a
                # broken session, absent data, a real defect, or one experiment bucket.
                # diagnose() already produced a PRECISE, grounded recommended_action
                # ("restore the login session", "seed the data/fixture", "pin the variant",
                # "file a defect, do NOT heal"). Surface THAT as the headline so the human
                # gets the actionable next step, not a bare "needs a human". (Layer #6 —
                # state/precondition + regression honesty; never green-wash, never vague.)
                _rec = (diag.get("recommended_action") or "").strip()
                _msg = (f"step {step}: {diag['cause_label']} — {_rec}" if _rec
                        else f"step {step}: {diag['cause_label']} — needs a human "
                             "(not an auto-fixable control-kind issue)")
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=_msg, stop_diag=diag)
                trace(event="stop_needs_human", scenario_id=sid, step=step, cause=diag["cause"])
                return

            key = (sid, step)
            attempts[key] = attempts.get(key, 0) + 1
            sid_ov = overrides.setdefault(sid, {})
            _intr = diag.get("interaction")
            if _intr:
                # INTERACTION re-synthesis (the control changed KIND — e.g. a native
                # <select> became a custom ARIA combobox). Thread the recipe under the
                # reserved key; compile_case_with_overrides routes it to the compiler's
                # additive `interactions` channel (open+pick + committed-value oracle).
                _ints = sid_ov.setdefault("__interactions__", {})
                if step in _ints or attempts[key] > max_attempts:
                    # FALLBACK before giving up: the control-KIND interaction didn't fix
                    # it — the control may have been RENAMED, not re-kinded (diagnose can
                    # mis-route a renamed labeled field to the interaction path, as with a
                    # field whose accessible name drifted by a word). Capture the LIVE
                    # controls and try a grounded re-anchor (Similo-style): auto-apply a
                    # confident GENUINE rename + re-prove (the step's own oracle + 2x
                    # confirm still gate green → never green-wash); human-gate a
                    # mid-confidence rename; otherwise escalate as before.
                    if observed.get("label") and (sid, step) not in _ra_done:
                        _ra_done.add((sid, step))
                        _reanchor = await _reanchor_capture(sid, step, bs)
                        _rl = (observed.get("label") or "").strip().lower()
                        _nm = str((_reanchor or {}).get("name") or "").strip()
                        if _reanchor and _nm and _nm.lower() != _rl:
                            try:
                                _rc = float(_reanchor.get("confidence") or 0.0)
                            except (TypeError, ValueError):
                                _rc = 0.0
                            if _rc >= 0.85:
                                sid_ov.setdefault("__reanchors__", {})[step] = {"name": _nm}
                                trace(event="heal_applied", scenario_id=sid, step=step,
                                      label=observed.get("label", ""),
                                      fix="reanchor:" + _nm, attempt=1)
                                continue  # re-compile with __reanchors__ + re-prove (oracle gates)
                            job.update(status="failed", terminal_state="needs_human",
                                       stop_reason=(f"step {step}: likely renamed control — recorded "
                                                    f"'{observed.get('label','')}' best matches the live control "
                                                    f"'{_nm}' at {int(_rc * 100)}% confidence. Confirm to re-anchor "
                                                    "(or fix the step)."),
                                       stop_diag=diag)
                            trace(event="stop_reanchor_confirm", scenario_id=sid, step=step,
                                  recorded=observed.get("label", ""), live=_nm, confidence=round(_rc, 2))
                            await _persist_job(run_id)
                            return
                    # AGENTIC fallback on the control-kind-exhausted path (gated, default-
                    # off): the deterministic interaction re-synthesis + Similo re-anchor
                    # did not close it. Before escalating to a human, let the LLM agent
                    # reason against the LIVE page (now reachable — the capture compiles
                    # WITH overrides) and propose a grounded rebind/wait. It cannot
                    # fabricate a selector and cannot touch the REFUSE families; the step's
                    # own oracle + 2x confirm still gate green. Off => byte-identical.
                    if await _try_agentic(sid, step, bs, observed, diag, f):
                        continue
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=(f"step {step}: the interaction re-synthesis did not make it pass on "
                                            "the re-run — needs a human"),
                               stop_diag=diag)
                    trace(event="stop_no_progress", scenario_id=sid, step=step)
                    return
                _ints[step] = _intr
                trace(event="heal_applied", scenario_id=sid, step=step,
                      label=observed.get("label", ""),
                      fix="interaction:" + _intr.get("kind", ""), attempt=attempts[key])
            else:
                ov_entry = self_heal.select_override_for_step(tc, field_meta, step)
                if ov_entry is None or ov_entry[0] in sid_ov or attempts[key] > max_attempts:
                    # AGENTIC fallback before escalating the select-kind-exhausted path too
                    # (gated, default-off; same oracle + 2x confirm gate; REFUSE families
                    # excluded). Off => byte-identical.
                    if await _try_agentic(sid, step, bs, observed, diag, f):
                        continue
                    job.update(status="failed", terminal_state="needs_human",
                               stop_reason=(f"step {step}: the control-kind fix did not make it pass on the re-run — "
                                            "likely an environment/bot-block on this target, or it needs a human"),
                               stop_diag=diag)
                    trace(event="stop_no_progress", scenario_id=sid, step=step)
                    return
                label_norm, sig = ov_entry
                sid_ov[label_norm] = sig
                trace(event="heal_applied", scenario_id=sid, step=step,
                      label=observed.get("label", ""), fix=".fill -> .selectOption", attempt=attempts[key])

        job.update(status="failed", terminal_state="needs_human",
                   stop_reason="reached the auto-heal iteration limit without a full green")
    except Exception as exc:
        import traceback as _tb
        job.update(status="error", terminal_state="error", stop_reason=f"auto-heal error: {exc}",
                   error_traceback=_tb.format_exc()[-3000:])
    await _persist_job(run_id)  # durable terminal auto-heal outcome (survives restart)


async def _scheduled_run_auto_heal(run_id: str, ctx: dict) -> None:
    """T4.2 admission wrapper around _run_auto_heal: acquire a per-tenant fair
    slot BEFORE the heal body, release it in finally. While queued behind another
    tenant's job, this job stays in status 'running' (the UI already polls it).
    Additive: with the default caps a lone tenant is admitted immediately, so the
    wrapped body runs exactly as before. If admission itself fails we fall back to
    running the body directly (fairness is best-effort, never a hard blocker that
    could drop a heal)."""
    tenant_id = (ctx or {}).get("tenant_id", "")
    acquired = False
    try:
        await _HEAL_SCHEDULER.acquire(tenant_id)
        acquired = True
    except Exception:
        acquired = False
    try:
        await _run_auto_heal(run_id, ctx)
    finally:
        if acquired:
            try:
                await _HEAL_SCHEDULER.release(tenant_id)
            except Exception:
                pass


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
        "storage_state": await _run_storage_state(request, artifact_id, tenant_id),
        # T4.1 (additive, default-off): "" / "headed" keeps the watched single-
        # display run_live demo path EXACTLY; "headless" routes the prove +
        # confirmation re-runs through the runner's parallel HEADLESS /run.
        "prove_mode": (body.prove_mode or "").strip().lower(),
        "prove_workers": body.prove_workers,
        # T4.2 (additive, default-off): per-heal wall-clock SLA budget (0/absent =>
        # unbounded, today's behavior) + harness-flake control samples (0/absent =>
        # no pre-pass).
        "sla_seconds": body.sla_seconds,
        "flake_samples": body.flake_samples,
        # AGENTIC AUTO-HEAL (additive, default-off — see RunConfigRequest).
        "enable_agentic_heal": bool(body.enable_agentic_heal),
        "agentic_tier": (body.agentic_tier or "tier_premium"),
        "agentic_min_confidence": body.agentic_min_confidence,
    }
    if ctx["prove_mode"] == "headless":
        _RUNNER_JOBS[run_id].update(live=False, prove_mode="headless")
    # T4.2 PER-TENANT FAIRNESS: admit through the round-robin scheduler so one
    # tenant can't monopolize the runner / starve others. We acquire INSIDE the
    # task (so the HTTP request still returns immediately, as today) and release
    # in a finally; the job sits in status 'running' while queued. With the
    # default caps a single tenant's job is admitted immediately (no-op), so the
    # demo path is unchanged.
    task = asyncio.create_task(_scheduled_run_auto_heal(run_id, ctx))
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
    nexus.data.json (data overrides — defaults stay the observed values), and a
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
    filename = f"nexus-playwright-run-{artifact_id[:8]}.zip"
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

    base_url = (body.base_url or "").strip()
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    # The Nexus runner container is headless (Xvfb-free); honor browsers/workers/
    # retries but force headless regardless of the requested mode.
    files = _configured_files(
        cases, build_field_meta(visits), base_url, body.data,
        data_by_test=body.data_by_test,
        browsers=body.browsers, headed=False,
        workers=body.workers, retries=body.retries,
        edited=edited_map, storage_state=storage_state,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE,
        "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id,
        "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url,
        "NEXUS_ENV": "nexus-runner",
        # opt-in VIDEO for this run (screenshots are already default-on via the runner env)
        **({"NEXUS_RECORD_VIDEO": "1"} if body.enable_video else {}),
    }
    await _register_job(run_id, {
        "run_id": run_id, "status": "running", "artifact_id": artifact_id,
        "tenant_id": tenant_id, "kind": "run",
        "target": base_url, "scripts": len(cases), "exit_code": None, "output": "",
        "steps_completed": 0, "total_tests": len(cases),
    })
    task = asyncio.create_task(_execute_run(run_id, files, env))
    _RUNNER_TASKS.add(task)
    task.add_done_callback(_RUNNER_TASKS.discard)
    return {"run_id": run_id, "status": "running", "scripts": len(cases), "target": base_url}


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

    base_url = (body.base_url or "").strip()
    storage_state = await _run_storage_state(request, artifact_id, tenant_id)
    files = _configured_files(
        cases, build_field_meta(visits), base_url, body.data,
        data_by_test=body.data_by_test,
        browsers=(body.browsers or ["chromium"])[:1],   # one project for one display
        headed=True,                                     # the live difference
        workers=1,                                       # serialize onto one screen
        retries=body.retries, edited=edited_map, storage_state=storage_state,
    )
    run_id = uuid.uuid4().hex
    env = {
        "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
        "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": run_id,
        "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
        # 🎥 opt-in video on the HEADED/live path too (was only on /playwright/run).
        **({"NEXUS_RECORD_VIDEO": "1"} if body.enable_video else {}),
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
            "target": base_url, "live_url": _LIVE_PATH}


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


@router.get("/api/v1/test-factory/{artifact_id}/scenarios/{scenario_id}/verdict-history")
async def scenario_verdict_history_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scenario_id: str = PathParam(..., min_length=1, max_length=128),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    """Per-script GROUNDED VERDICT HISTORY — this scenario's outcome across recent
    runs (newest first): proven-green vs real-regression / selector-drift / flake
    (reusing the frozen classify_failure verdict), duration, the final-frame success
    screenshot, and any heal event that landed on that run. No migration, $0 LLM,
    read-only. Powers the per-script "History" drawer in the Playwright tab."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        history = await scenario_verdict_history(
            session, artifact_id=artifact_id, scenario_id=scenario_id,
            tenant_id=tenant_id, limit=limit,
        )
    return {"artifact_id": artifact_id, "scenario_id": scenario_id, "history": history}


@router.get("/api/v1/test-factory/{artifact_id}/proven-controls")
async def proven_controls_kb(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    scope: str = Query("recording", pattern="^(recording|app|tenant)$"),
    include_invalidated: bool = Query(True),
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(get_current_user),
):
    """Phase 5 — PROVEN CONTROL LEDGER knowledge base (read-only, $0 LLM, no migration).
    Lists the controls whose heals have been oracle-PROVEN green and are reused across
    scenarios AND recordings, with provenance (label, page, fix kind, confidence =
    confirmed_count, cross-recording app scope) and lifecycle (stale_count, quarantined/
    invalidated). scope=recording (this artifact), app (all recordings sharing this app
    host), or tenant. The within-tenant brick of the federated failure->fix flywheel."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        _app_key = artifact_id if scope == "recording" else None
        _app_fp = None
        if scope == "app":
            # derive this artifact's app host from any of its own proven rows
            _seed = await control_ledger.list_proven_controls(
                session, tenant_id=tenant_id, app_key=artifact_id, limit=1)
            _app_fp = (_seed[0].get("app_fingerprint") if _seed else "") or None
            if not _app_fp:
                _app_key = artifact_id   # no host known yet → degrade to this recording
        entries = await control_ledger.list_proven_controls(
            session, tenant_id=tenant_id, app_key=_app_key, app_fingerprint=_app_fp,
            include_invalidated=include_invalidated, limit=limit)
    _active = [e for e in entries if not e.get("invalidated_at")]
    summary = {
        "total": len(entries),
        "active": len(_active),
        "quarantined": len(entries) - len(_active),
        "reused": sum(1 for e in _active if (e.get("confirmed_count") or 0) > 1),
        "by_kind": {},
    }
    for e in entries:
        _k = e.get("fix_kind") or "?"
        summary["by_kind"][_k] = summary["by_kind"].get(_k, 0) + 1
    return {"artifact_id": artifact_id, "scope": scope, "summary": summary, "entries": entries}


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
# nexus.auth.json. The session is NEVER returned to the client or stored plaintext.


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


@router.post("/api/v1/test-factory/{artifact_id}/playwright/auth/save")
async def save_auth_capture(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Pull the captured storageState from the runner, ENCRYPT it, and store it as
    this artifact's auth profile. Never returns the session. Refuses (503) if
    encryption is unavailable rather than store a session in plaintext."""
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
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        try:
            await auth_profiles.save_profile(
                session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id,
                storage_state_json=state_json, label=f"captured session ({cookie_n} cookie(s))",
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
    activated. Idempotent. Returns the approved version's metadata.

    T5.4: gated to an APPROVER role (admin/approver/maintainer/manager) on top of the
    router's POST gate, and the approving human's identity is recorded into the
    tamper-evident Part-11 evidence chain (event_type='heal_approve')."""
    _require_approver(user)
    _approver = str(user.get("sub") or user.get("user_id") or user.get("email") or "")
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        row = await script_versions.approve_version(
            session, artifact_id=artifact_id, test_case_id=test_id, version_no=version_no,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")
        # Part-11 evidence: WHO approved (the human actor), chained + FAIL-CLOSED with
        # the approval. If this raises, the async-with rolls back and the approve is
        # not recorded as having happened (no un-audited promotion).
        await heal_evidence.record_heal_event(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
            event_type="heal_approve", actor=_approver or "unknown",
            scenario_id=test_id, fix_kind="heal_approve", verified_green=True,
            version_no=version_no, run_id="",
            reason_for_change=f"Human approved PROPOSED v{version_no} -> active",
            details={"role": user.get("role", ""), "email": user.get("email", "")},
        )
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


@router.get("/api/v1/test-factory/{artifact_id}/heal-events")
async def heal_events(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Part-11 heal evidence chain for this artifact: the ordered, tamper-evident
    record of every heal decision (capture -> diagnosis -> candidate -> proof ->
    approval), each row chained to the prior via row_hash + per-row chain_ok, the
    approver identity, and the optional detached-signature state. Read-only (GET, so
    open to viewers). T5.3."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await heal_evidence.list_heal_events(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
        )


@router.get("/api/v1/test-factory/heal-events/verify-chain")
async def verify_heal_chain(
    user: dict = Depends(get_current_user),
):
    """T5.3: independently recompute the WHOLE tenant heal-evidence hash chain and
    report the FIRST tamper/break ({ok, count, first_break, signing}). first_break.kind
    ∈ {content_tampered, chain_broken, signature_invalid}. Tenant-wide (not per
    artifact) so a deleted/reordered row across artifacts is caught. Read-only."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        return await heal_evidence.verify_chain(session, tenant_id=tenant_id)


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
        id_to_path = {
            s["test_id"]: s["path"]
            for s in compile_manifest(cases, build_field_meta(visits)).get("scripts", [])
        }
        if body.test_id not in id_to_path:
            raise HTTPException(status_code=404, detail="test not found in compiled suite")
        row = await script_versions.save_new_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=str(user.get("session_id", "") or ""),
            test_case_id=body.test_id, spec_path=id_to_path[body.test_id],
            script_source=body.script_source, data_json=dict(body.data or {}),
            author=str(user.get("email") or user.get("user_id") or ""),
            note=body.note,
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
        }
    board["total_scripts"] = len(scripts)
    board["flaky"] = sum(1 for v in scripts.values() if v["is_flaky"])
    return {"artifact_id": artifact_id, "board": board, "scripts": scripts}


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
        return await build_latest_run_timeline(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )


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
        return await build_run_timeline_by_id(
            session, artifact_id=artifact_id, tenant_id=tenant_id, run_id=run_id,
        )


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
        return await self_heal.analyze_step(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            scenario_id=scenario_id, step_number=step_number,
        )


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

def _selector_for(label: str, kind: str) -> str:
    """Informational resilient-locator hint. The frozen compiler binds the locator
    from observed.label/kind, NOT this field, but we keep it truthful for the UI/
    export. Mirrors generator._locator's shape (role=button|name=X / label=Y)."""
    k = (kind or "").strip().lower()
    if k == "button":
        return f"role=button|name={label}"
    if k == "link":
        return f"role=link|name={label}"
    return f"label={label}"


def _grounded_controls(catalog: dict) -> list[dict]:
    """Flatten the recording's catalog into pickable controls, deduped by label.
    Buttons (captured clicks) + fields (captured inputs) — every one is a REAL
    captured control, so re-pointing a step to any of them stays grounded (you can
    never bind a step to a control the recording never showed). Generic: zero app
    vocabulary, works for any recording/domain."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in (catalog.get("pages") or []):
        pn = p.get("page_name") or p.get("page_key") or ""
        for b in (p.get("buttons") or []):
            lab = (b.get("label") or "").strip()
            if not lab:
                continue
            kind = (b.get("kind") or "button").strip() or "button"
            key = proposer._norm(lab) + "|" + kind
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": lab, "kind": kind, "page": pn})
        for f in (p.get("fields") or []):
            lab = (f.get("label") or "").strip()
            if not lab:
                continue
            key = proposer._norm(lab) + "|field"
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": lab, "kind": "field", "page": pn})
    return out


def _control_is_grounded(control: dict, controls: list[dict]) -> bool:
    """True iff the chosen control's label was actually captured (matched by
    normalized label, kind-agnostic). The anti-green-wash guard: refuse to bind a
    step to a fabricated control."""
    want = proposer._norm((control or {}).get("label") or "")
    if not want:
        return False
    return any(proposer._norm(c["label"]) == want for c in controls)


def _apply_case_override(tc: dict, step_ov: dict, name_ov) -> dict:
    if name_ov:
        tc["name"] = name_ov
        if "title" in tc:
            tc["title"] = name_ov
    for st in (tc.get("steps") or []):
        d = step_ov.get(str(st.get("step_number")))
        if not d:
            continue
        # Grounded RE-POINT: merge a chosen captured control into `observed` — the
        # field the FROZEN compiler actually builds the locator from — recompute the
        # informational selector, and mark provenance honestly. verb/value/url/
        # next_url are left intact (re-pointing a click target doesn't change what
        # was typed or where it navigated). This is what makes Regenerate reflect a
        # step edit instead of only changing the human description text.
        ctrl = d.get("__control__")
        if isinstance(ctrl, dict) and (ctrl.get("label") or "").strip():
            obs = dict(st.get("observed") or {})
            obs["label"] = ctrl["label"]
            if ctrl.get("kind"):
                obs["kind"] = ctrl["kind"]
            if "anchor" in ctrl:
                obs["anchor"] = ctrl.get("anchor")
            if "anchor_kind" in ctrl:
                obs["anchor_kind"] = ctrl.get("anchor_kind")
            # The ORIGINAL control's recorded outcome ('after') was observed for a
            # different element — it no longer applies to the re-pointed control, so
            # drop it rather than assert a stale oracle (which would false-RED). The
            # page-level navigation ('next_url') is kept: it's the recorded page
            # transition, not specific to the clicked element.
            obs.pop("after", None)
            st["observed"] = obs
            st["selector"] = _selector_for(obs.get("label", ""), obs.get("kind", ""))
            st["provenance"] = "user-edited"
        for k, v in d.items():
            if k == "__control__":
                continue
            st[k] = v
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
    # Grounded RE-POINT: re-target this step to a control the recording captured.
    # {label, kind?, anchor?, anchor_kind?}. Validated against the catalog (422 if the
    # control was never captured) — you can never bind to a fabricated control.
    control: dict | None = None


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
    _wants_control = False
    for patch in req.steps:
        d: dict = {}
        if patch.action is not None:
            d["action"] = patch.action[:4000]
        if patch.expected_result is not None:
            d["expected_result"] = patch.expected_result[:4000]
        if patch.verification is not None:
            d["verification"] = patch.verification[:4000]
        if patch.control is not None and (patch.control.get("label") or "").strip():
            d["__control__"] = {k: patch.control.get(k)
                                for k in ("label", "kind", "anchor", "anchor_kind")
                                if patch.control.get(k) is not None}
            _wants_control = True
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
        if _wants_control:
            # GROUNDED-RE-POINT GUARD: a step may only be re-pointed to a control the
            # recording actually captured. Anything else -> 422 (never a fabricated
            # binding). Same catalog the conversational ADD validates against.
            visits, actions = await factory_service._load_current_pages_and_actions(
                session, artifact_id=artifact_id)
            _controls = _grounded_controls(proposer.build_catalog(visits, actions))
            for _sn, _d in step_ov.items():
                _c = _d.get("__control__")
                if _c and not _control_is_grounded(_c, _controls):
                    raise HTTPException(
                        status_code=422,
                        detail=(f"control '{_c.get('label')}' was not captured in this "
                                "recording — a step can only be re-pointed to a control "
                                "the recording actually shows."))
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
            es.update(step_ov)
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
        "repointed_steps": sum(1 for d in step_ov.values() if "__control__" in d),
        "survives_regenerate": True,
    }


@router.get("/api/v1/test-factory/{artifact_id}/test-cases/{case_id}/controls")
async def list_grounded_controls(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    case_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """The grounded menu of controls captured in this recording — the ONLY controls a
    step may be re-pointed to (so an edit can never bind to a fabricated control).
    Deterministic, $0, read-only. Generic: works for any recording/domain."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        visits, actions = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
    return {"controls": _grounded_controls(proposer.build_catalog(visits, actions))}


def _partition_fidelity_gaps(report: dict) -> tuple[list, list]:
    """Split the deterministic fidelity gaps into (auto-fixable-by-regenerate,
    honest-escalations). ONLY drift / stale-strong-assertion gaps are auto-applied —
    and only via the FROZEN compiler (a fresh regenerate). Everything that cannot be
    grounded is escalated with an honest reason; NOTHING is invented or green-washed,
    and no LLM ever writes test code."""
    gaps = report.get("gaps") or []
    drift = bool(report.get("drift"))
    fixed: list[str] = []
    esc: list[dict] = []
    for g in gaps:
        gl = (g or "").lower()
        if "stale" in gl or "drift" in gl:
            fixed.append(g)
        elif "without a strong" in gl:
            # A fresh regenerate materializes the grounded value/URL/state oracles a
            # STALE saved version lacked — but only if it is actually stale; otherwise
            # the script already carries the only honest oracle and we escalate.
            if drift:
                fixed.append(g)
            else:
                esc.append({"gap": g, "reason": "the grounded oracle for these steps is "
                            "already the asserted recorded value/state; a stronger one would "
                            "need a re-capture, not a guess."})
        elif "unproven" in gl:
            esc.append({"gap": g, "reason": "not directly observed in the recording — re-capture "
                        "or confirm the step; there is no grounded oracle to prove it."})
        elif "tolerant not-empty" in gl:
            esc.append({"gap": g, "reason": "real and correct (the value is data-driven); a stricter "
                        "exact-match would be brittle — left as-is, never downgraded."})
        elif "expected result" in gl:
            esc.append({"gap": g, "reason": "needs a human-provided Expected Result — inventing one "
                        "would green-wash."})
        else:
            esc.append({"gap": g, "reason": "no grounded deterministic fix; needs review."})
    return fixed, esc


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/fix-gaps")
async def fix_script_gaps(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Make 'AI Review' ACTIONABLE: apply ONLY deterministic, grounded repairs
    (regenerate to resolve drift / materialize the grounded oracles a stale version
    lacked) and HONESTLY escalate everything that cannot be grounded. NEVER an LLM
    writing test code; NEVER a fabricated assertion; NEVER green-washes — the frozen
    compiler is the only thing that touches the script. Returns what was fixed vs.
    what was left for a human, so the UI can show both truthfully."""
    tenant_id = user["tenant_id"]
    version_no = None
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases, field_meta = await _fidelity_inputs(session, artifact_id)
        tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
        if tc is None:
            raise HTTPException(status_code=404, detail="no active test case for this script")
        active = await script_versions.get_active_version(
            session, artifact_id=artifact_id, test_case_id=test_id)
        report = tf_fidelity.compute_fidelity(
            tc, field_meta, active_source=(active.script_source if active else None))
        fixed, escalations = _partition_fidelity_gaps(report)
        if fixed:
            out = await _regenerate_one(session, artifact_id=artifact_id, tenant_id=tenant_id,
                                        tc=tc, field_meta=field_meta)
            version_no = out.get("version_no")
            await session.commit()
    return {
        "test_id": test_id,
        "fixed": fixed,
        "escalations": escalations,
        "version_no": version_no,
        "drift_resolved": bool(report.get("drift")) and bool(fixed),
        "score": report.get("score"),
        "grade": report.get("grade"),
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
