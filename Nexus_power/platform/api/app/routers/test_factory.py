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
)
from nexus_sdk.security.envelope import EnvelopeBlob

from ..auth import get_current_user
from ..database import tenant_scoped_session
from .integrations import integration_installations
from ..services.test_factory import service as factory_service
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
from ..services.diff_and_heal import self_heal
from ..services.diff_and_heal import heal_capture_store
from ..services.flywheel import ledger as flywheel_ledger
from ..services.test_factory import fidelity as tf_fidelity
from ..services.test_runs import (
    last_run_summary_by_scenario,
    _status_severity,
    build_latest_run_timeline,
    build_run_timeline_by_id,
    find_run_by_ci_run_id,
    recent_runs,
)
from ..services.test_factory import runner_jobs
from ..services.test_factory import auth_profiles
from ..services.test_factory.assistant import answer as assistant_answer
from ..services.test_factory.delivery import (
    EXPORT_MEDIA_TYPES,
    build_csv,
    build_excel,
    build_json,
)
from ..services.test_factory.delivery.connectors import CONNECTORS, build_connector

router = APIRouter(tags=["Test Factory"])
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
    return {"success": True, **summary}


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

        for iteration in range(1, _AUTO_HEAL_MAX_ITERS + 1):
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
                    candidate_specs[sid] = spec
                    edited[sid] = {"spec_path": spec_path_by_sid.get(sid, ""), "script_source": spec}

            files = _configured_files(
                sel_cases, field_meta, base_url, data, data_by_test={},
                browsers=["chromium"], headed=True, workers=1, retries=0, edited=edited,
                storage_state=storage_state,
            )
            sub_run_id = uuid.uuid4().hex
            env = {
                "NEXUS_ENDPOINT": _INGEST_BASE, "NEXUS_TOKEN": token or "",
                "NEXUS_ARTIFACT_ID": artifact_id, "NEXUS_RUN_ID": sub_run_id,
                "NEXUS_BASE_URL": base_url, "NEXUS_ENV": "nexus-runner",
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
            await asyncio.sleep(3.0)  # let the in-run reporter finish ingesting

            async with tenant_scoped_session(tenant_id) as session:
                tl = await build_latest_run_timeline(session, artifact_id=artifact_id, tenant_id=tenant_id)
            failures = self_heal.first_failures(tl, selected)

            if not failures:
                # FULL GREEN → persist Clean Run - V1 for the healed tests (atomic).
                healed = [{"test_case_id": sid, "spec_path": spec_path_by_sid.get(sid, ""),
                           "script_source": src} for sid, src in candidate_specs.items()]
                version_no = None
                if healed:
                    async with tenant_scoped_session(tenant_id) as session:
                        rows = await script_versions.batch_save_clean_run_version(
                            session, artifact_id=artifact_id, tenant_id=tenant_id,
                            healed=healed, clean_run_session_id=run_id, n_healed=len(healed),
                        )
                        await session.commit()
                        version_no = rows[0].version_no if rows else None
                job.update(status="passed", terminal_state="clean_run_v1",
                           clean_run_version=version_no, healed_count=len(healed))
                trace(event="clean_run_v1", healed_count=len(healed), version_no=version_no)
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
            if diag["cause"] != "WRONG_CONTROL_KIND":
                job.update(status="failed", terminal_state="needs_human",
                           stop_reason=f"step {step}: {diag['cause_label']} — needs a human (not an auto-fixable control-kind issue)",
                           stop_diag=diag)
                trace(event="stop_needs_human", scenario_id=sid, step=step, cause=diag["cause"])
                return

            key = (sid, step)
            attempts[key] = attempts.get(key, 0) + 1
            ov_entry = self_heal.select_override_for_step(tc, field_meta, step)
            sid_ov = overrides.setdefault(sid, {})
            if ov_entry is None or ov_entry[0] in sid_ov or attempts[key] > max_attempts:
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
        "storage_state": await _run_storage_state(request, artifact_id, tenant_id),
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

    async with httpx.AsyncClient(timeout=60.0) as http:
        result = await connector.push(cases, http)

    return {"success": result.failed == 0, **result.as_dict()}
