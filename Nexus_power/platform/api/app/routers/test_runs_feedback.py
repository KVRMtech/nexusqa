"""Platform API — Execution Feedback routes (P6).

Endpoints:

  POST /api/v1/test-runs/ingest
       CI/CD posts the results of an E2E run + per-step details.

  GET  /api/v1/e2e-architect/{artifact_id}/test-runs
       List recent test runs for an artifact, newest first.

  GET  /api/v1/e2e-architect/{artifact_id}/scenarios/runs/summary
       Per-scenario last-run + flake stats. Used by the Test Studio to
       render badges without a round-trip per scenario.

  GET  /api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/last-run
       Full per-scenario detail: last-run steps, drift report,
       root-cause hints (only when failing).

All endpoints are tenant-scoped. The ingest webhook requires the standard
JWT (CI uses a long-lived service token); per-tenant API-key webhook auth
can be layered on later without touching this code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
import jwt as pyjwt
from fastapi import (
    APIRouter, Body, Depends, File, Form, HTTPException, Path, Request, UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    CanonicalArtifactRow,
    E2ETestRunRow,
    E2ETestRunStepRow,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_PASSED,
)

from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..database import require_db, tenant_scoped_session
from ..services.test_runs import (
    detect_drift,
    ingest_run,
    last_run_summary_by_scenario,
    root_cause_hints,
)
from ..services.test_factory.run_screenshots import (
    MAX_SCREENSHOT_BYTES,
    MAX_TRACE_BYTES,
    MAX_VIDEO_BYTES,
    fetch_screenshot,
    store_screenshot,
    store_trace,
    store_video,
)
from ..services.diff_and_heal import heal_capture_store
from ..services.diff_and_heal.action_resolver import flatten_aria

router = APIRouter(tags=["E2E Test Runs"])

_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()
_PLATFORM_BASE_URL = os.environ.get(
    "PLATFORM_API_URL", "http://localhost:8000",
)


# ─── Request models ────────────────────────────────────────────────────


class IngestStep(BaseModel):
    test_name: str = Field("", max_length=600)
    scenario_id: str = Field("", max_length=64)
    step_number: int = 0
    status: str = Field("passed")
    duration_ms: int = 0
    expected_selector: str = Field("", max_length=1000)
    resolved_selector: str = Field("", max_length=1000)
    resolved_bbox: dict | None = None
    evidence_scene_id: str = Field("", max_length=64)
    evidence_control_id: str = Field("", max_length=64)
    evidence_edge_id: str = Field("", max_length=64)
    error_message: str = Field("", max_length=8000)
    screenshot_url: str = Field("", max_length=2000)
    expected_output: str = Field("", max_length=2000)
    metadata: dict = Field(default_factory=dict)


class IngestRequest(BaseModel):
    artifact_id: str = Field(..., min_length=1)
    ci_run_id: str = Field("", max_length=200)
    ci_commit_sha: str = Field("", max_length=64)
    ci_pipeline_url: str = Field("", max_length=1000)
    environment: str = Field("ci", max_length=64)
    status: str = Field("", max_length=20)
    started_at: str | None = None
    completed_at: str | None = None
    steps: list[IngestStep] = Field(..., min_length=1, max_length=2000)
    metadata: dict = Field(default_factory=dict)


# ─── Helpers ───────────────────────────────────────────────────────────


_WRITE_ROLES = frozenset({"admin", "manager"})


def _require_write_role(user: dict) -> None:
    """Gate evidence-mutating POSTs (screenshot / video / heal-capture) to
    privileged roles. Reads stay open to any authenticated tenant member; only
    writes that store uploaded blobs require admin|manager. Service tokens used
    by CI carry role='admin' (see ``_make_service_token``) so the reporter
    upload path is unaffected."""
    if (user.get("role") or "viewer") not in _WRITE_ROLES:
        raise HTTPException(403, "admin or manager role required")


def _make_service_token(tenant_id: str) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": "platform-test-runs-service",
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


async def _verify_artifact_in_tenant(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> None:
    result = await db.execute(
        select(CanonicalArtifactRow.artifact_id).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(404, f"Artifact {artifact_id} not found")


def _row_to_dict(row) -> dict:
    """SQLAlchemy ORM row → JSON-safe dict."""
    out = {}
    for col in row.__table__.columns.keys():
        val = getattr(row, col)
        if hasattr(val, "isoformat"):
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


async def _fetch_visual_graph(
    artifact_id: str, tenant_id: str,
) -> dict:
    """Helper used by the per-scenario endpoint to compute drift + hints
    against the *live* visual graph (not a stale snapshot)."""
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
        _logger.warning("test_runs.graph_fetch_failed: %s", exc)
    return {}


# ─── Endpoints ─────────────────────────────────────────────────────────


# Recovery Orchestrator background tasks — referenced so they are never GC'd.
_RECOVERY_TASKS: set = set()


#: Strong refs for in-flight snapshot tasks (asyncio only holds weak ones,
#: so an un-referenced task can be collected before it finishes).
_SNAPSHOT_TASKS: set = set()


@router.post("/api/v1/test-runs/ingest")
async def ingest_test_run(
    req: IngestRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Ingest a CI run. Authenticated with a standard JWT — CI generates a
    long-lived service token via the existing auth system."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        await _verify_artifact_in_tenant(
            db, artifact_id=req.artifact_id, tenant_id=tenant_id,
        )
        payload = req.model_dump()
        # ingest_run wants 'metadata' on each step; the pydantic dump
        # already produced that. Translate top-level into kwargs:
        summary = await ingest_run(
            db,
            artifact_id=req.artifact_id,
            tenant_id=tenant_id,
            payload=payload,
        )

    # ── AC-1: freeze the report for this run the moment it lands ───────────
    # The report is otherwise assembled on demand, so re-rendering an old run
    # after cases were regenerated need not reproduce the account that
    # described it at the time. Fire-and-forget: a snapshot failure must never
    # break ingest, but it is logged at WARNING so a gap in the record shows.
    try:
        import asyncio as _asyncio

        async def _freeze_report(aid: str, tid: str, rid: str, env: str) -> None:
            try:
                from ..database import tenant_scoped_session
                from ..services.test_factory import evidence_report, report_store
                async with tenant_scoped_session(tid) as sess:
                    report = await evidence_report.build_report(
                        sess, artifact_id=aid, tenant_id=tid, run_id=rid,
                        include_steps=True)
                    report["source"] = "snapshot"
                    root = await report_store.save_snapshot(
                        sess, tenant_id=tid, artifact_id=aid, run_id=rid,
                        environment=env, report=report)
                    await sess.commit()
                _logger.warning(
                    "test_runs.report_snapshot_saved run=%s artifact=%s root=%s",
                    rid, aid, root[:12])
            except Exception as exc:
                _logger.warning(
                    "test_runs.report_snapshot_failed run=%s artifact=%s err=%s",
                    rid, aid, str(exc)[:200])

        _rid = str(summary.get("run_id") or "")
        if _rid:
            _t = _asyncio.create_task(
                _freeze_report(req.artifact_id, tenant_id, _rid,
                               str(req.environment or "")))
            _SNAPSHOT_TASKS.add(_t)
            _t.add_done_callback(_SNAPSHOT_TASKS.discard)
    except Exception as exc:      # scheduling itself must never break ingest
        _logger.warning("test_runs.report_snapshot_spawn_failed err=%s", str(exc)[:200])

    # ── Recovery Orchestrator (the reflex arc, founder-approved FULL-AUTO) ──
    # A failed run triggers automatic diagnosis → routed repair → re-prove the
    # moment it lands, instead of waiting for a human to notice. Fire-and-
    # forget: a recovery error can never break ingest. Certification-run
    # ingests pass NO certification spawner (loop guard — a failing cert must
    # not re-trigger itself; POST /certify is the deliberate retry).
    try:
        env_name = str(req.environment or "").strip().lower()
        # 'diagnosis' runs are the auto-heal driver's OWN instruments (capture /
        # verify) — they must never re-trigger the reflex arc (recursion guard).
        if int(summary.get("failed_steps") or 0) > 0 and env_name != "diagnosis":
            from ..services.test_factory.recovery_orchestrator import run_recovery
            is_cert = env_name == "certification"
            spawn = None
            if not is_cert:
                from .test_factory import _spawn_certification

                def spawn(request=request, aid=req.artifact_id, tid=tenant_id):
                    _spawn_certification(request, aid, tid)

            from .test_factory import _spawn_auto_heal

            def spawn_heal(sid: str, step: int, cause: str,
                           request=request, aid=req.artifact_id, tid=tenant_id):
                _spawn_auto_heal(request, aid, tid, sid, step, cause)

            task = asyncio.create_task(run_recovery(
                artifact_id=req.artifact_id,
                tenant_id=tenant_id,
                run_id=str(summary.get("run_id") or ""),
                is_certification=is_cert,
                session_scope=lambda tid=tenant_id: tenant_scoped_session(tid),
                spawn_certification=spawn,
                spawn_auto_heal=spawn_heal,
            ))
            _RECOVERY_TASKS.add(task)
            task.add_done_callback(_RECOVERY_TASKS.discard)
    except Exception:
        logging.getLogger(__name__).exception(
            "test_runs.recovery_spawn_failed artifact=%s (ingest unaffected)",
            req.artifact_id,
        )
    return summary


@router.post("/api/v1/test-runs/screenshot")
async def upload_run_screenshot(
    file: UploadFile = File(...),
    run_id: str = Form(""),
    artifact_id: str = Form(...),
    scenario_id: str = Form(""),
    step_number: int = Form(0),
    user: dict = Depends(get_current_user),
):
    """Upload one ACTUAL run screenshot (the failure-state frame) from the bundled
    reporter. Stores the bytes tenant-scoped and returns a serve URL to drop into
    the step's screenshot_url. Auth: standard JWT (the reporter sends NEXUS_TOKEN
    as Bearer). Degrades safely pre-migration (503 → reporter logs, skips)."""
    _require_write_role(user)
    tenant_id = user["tenant_id"]
    data = await file.read(MAX_SCREENSHOT_BYTES + 1)
    if not data:
        raise HTTPException(422, "empty screenshot")
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(413, "screenshot too large")
    async with tenant_scoped_session(tenant_id) as session:
        await _verify_artifact_in_tenant(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        try:
            sid = await store_screenshot(
                session,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                run_id=run_id,
                scenario_id=scenario_id,
                step_number=step_number,
                content_type=file.content_type or "image/png",
                image=data,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:  # table missing (pre-migration) / DB error
            _logger.warning("test_runs.screenshot_store_unavailable: %s", exc)
            raise HTTPException(503, "screenshot store unavailable")
    return {"screenshot_id": sid, "url": f"/api/v1/test-runs/screenshot/{sid}"}


@router.post("/api/v1/test-runs/trace")
async def upload_run_trace(
    file: UploadFile = File(...),
    run_id: str = Form(""),
    artifact_id: str = Form(...),
    scenario_id: str = Form(""),
    step_number: int = Form(0),
    user: dict = Depends(get_current_user),
):
    """Upload one Playwright ``trace.zip`` for a FAILED test from the bundled
    reporter (spec §2.8 evidence tier T2).

    One trace carries the DOM snapshots, network log, console log and a
    screencast for that test, and is time-travel replayable — so it satisfies
    "replay from the exact step" without a bespoke video pipeline. Same
    guarantees as the screenshot upload: tenant-scoped, artifact-verified,
    best-effort (503 pre-migration → the reporter skips and the report simply
    shows no trace, never a fabricated one)."""
    _require_write_role(user)
    tenant_id = user["tenant_id"]
    data = await file.read(MAX_TRACE_BYTES + 1)
    if not data:
        raise HTTPException(422, "empty trace")
    if len(data) > MAX_TRACE_BYTES:
        raise HTTPException(413, "trace too large")
    async with tenant_scoped_session(tenant_id) as session:
        await _verify_artifact_in_tenant(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        try:
            tid = await store_trace(
                session,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                run_id=run_id,
                scenario_id=scenario_id,
                step_number=step_number,
                trace=data,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:      # table missing (pre-migration) / DB error
            _logger.warning("test_runs.trace_store_unavailable: %s", exc)
            raise HTTPException(503, "trace store unavailable")
    return {"trace_id": tid, "url": f"/api/v1/test-runs/screenshot/{tid}"}


class HealCaptureRequest(BaseModel):
    artifact_id: str = Field(..., min_length=1)
    run_id: str = Field("", max_length=200)
    scenario_id: str = Field("", max_length=64)
    status: str = Field("", max_length=20)
    aria: dict | None = None


@router.post("/api/v1/test-runs/heal-capture")
async def ingest_heal_capture(
    req: HealCaptureRequest,
    user: dict = Depends(get_current_user),
):
    """Receive a failure-state accessibility snapshot from the gated afterEach
    capture fixture (only sent on a NEXUS_HEAL_CAPTURE=1 re-run that failed). The
    flattened node list is held transiently for the TrueFix re-anchor resolver.
    Auth: standard JWT (the fixture sends NEXUS_TOKEN as Bearer). Best-effort —
    stores nothing harmful and never blocks the heal flow."""
    _require_write_role(user)
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _verify_artifact_in_tenant(
            session, artifact_id=req.artifact_id, tenant_id=tenant_id,
        )
    nodes = flatten_aria(req.aria) if req.aria else []
    # SHARED store (T1.4): write to the process-shared backend so a capture
    # produced by one worker's headed capture-run is visible to whichever worker
    # later runs the resolve — fixes the silent over-escalation under parallel
    # workers. aput falls back to in-memory when the DB is down.
    await heal_capture_store.aput(
        tenant_id=tenant_id, artifact_id=req.artifact_id,
        scenario_id=req.scenario_id, nodes=nodes, status=req.status,
    )
    return {"stored": len(nodes)}


@router.get("/api/v1/test-runs/screenshot/{screenshot_id}")
async def get_run_screenshot(
    request: Request,
    screenshot_id: str = Path(..., min_length=1, max_length=64),
):
    """Serve a stored run screenshot for the baseline-vs-actual <img>. Auth is the
    JWT from the Bearer header OR a ``?token=`` query param (resolved by the auth
    middleware), so a plain <img src> loads. Tenant-scoped — a screenshot only
    resolves within its owning tenant."""
    user = getattr(request.state, "user", None) or {}
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(401, "unauthenticated")
    try:
        async with tenant_scoped_session(tenant_id) as session:
            found = await fetch_screenshot(
                session, tenant_id=tenant_id, screenshot_id=screenshot_id,
            )
    except Exception as exc:  # table missing (pre-migration) / DB error
        _logger.warning("test_runs.screenshot_fetch_unavailable: %s", exc)
        raise HTTPException(404, "screenshot not found")
    if found is None:
        raise HTTPException(404, "screenshot not found")
    content_type, image = found
    return Response(
        content=image,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/api/v1/test-runs/video")
async def upload_run_video(
    file: UploadFile = File(...),
    run_id: str = Form(""),
    artifact_id: str = Form(...),
    scenario_id: str = Form(""),
    step_number: int = Form(0),
    user: dict = Depends(get_current_user),
):
    """Upload one run VIDEO (opt-in — recorded only when NEXUS_RECORD_VIDEO=1) from the
    bundled reporter, typically the proving/clean-run clip. Stored tenant-scoped in the
    shared blob table; returns a serve URL to drop into the step's video_url. Degrades
    safely pre-migration (503 → reporter logs, skips)."""
    _require_write_role(user)
    tenant_id = user["tenant_id"]
    data = await file.read(MAX_VIDEO_BYTES + 1)
    if not data:
        raise HTTPException(422, "empty video")
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "video too large")
    async with tenant_scoped_session(tenant_id) as session:
        await _verify_artifact_in_tenant(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        try:
            vid = await store_video(
                session,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                run_id=run_id,
                scenario_id=scenario_id,
                step_number=step_number,
                content_type=file.content_type or "video/webm",
                video=data,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except Exception as exc:  # table missing (pre-migration) / DB error
            _logger.warning("test_runs.video_store_unavailable: %s", exc)
            raise HTTPException(503, "video store unavailable")
    return {"video_id": vid, "url": f"/api/v1/test-runs/video/{vid}"}


@router.get("/api/v1/test-runs/video/{video_id}")
async def get_run_video(
    request: Request,
    video_id: str = Path(..., min_length=1, max_length=64),
):
    """Serve a stored run video for the <video> player. Auth is the JWT Bearer OR a
    ``?token=`` query param (resolved by the auth middleware), so a plain <video src>
    loads. Tenant-scoped — reuses the shared blob fetch (content_type sets the MIME)."""
    user = getattr(request.state, "user", None) or {}
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(401, "unauthenticated")
    try:
        async with tenant_scoped_session(tenant_id) as session:
            found = await fetch_screenshot(
                session, tenant_id=tenant_id, screenshot_id=video_id,
            )
    except Exception as exc:  # table missing (pre-migration) / DB error
        _logger.warning("test_runs.video_fetch_unavailable: %s", exc)
        raise HTTPException(404, "video not found")
    if found is None:
        raise HTTPException(404, "video not found")
    content_type, blob = found
    return Response(
        content=blob,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/v1/e2e-architect/{artifact_id}/test-runs")
async def list_test_runs(
    artifact_id: str = Path(..., min_length=1),
    limit: int = 20,
    user: dict = Depends(get_current_user),
):
    """Recent runs for an artifact, newest first."""
    tenant_id = user["tenant_id"]
    limit = max(1, min(limit, 200))
    factory = require_db()
    async with factory() as db:
        await _verify_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        result = await db.execute(
            select(E2ETestRunRow)
            .where(
                E2ETestRunRow.artifact_id == artifact_id,
                E2ETestRunRow.tenant_id == tenant_id,
            )
            .order_by(desc(E2ETestRunRow.started_at))
            .limit(limit)
        )
        runs = [_row_to_dict(r) for r in result.scalars().all()]
        return {"artifact_id": artifact_id, "runs": runs}


@router.get("/api/v1/e2e-architect/{artifact_id}/scenarios/runs/summary")
async def scenario_runs_summary(
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Per-scenario last-run + flake summary for every scenario that has
    at least one recorded run.  The Test Studio merges this into its
    scenario list to render last-run badges without a per-card request."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        await _verify_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        summary = await last_run_summary_by_scenario(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        return {"artifact_id": artifact_id, "scenarios": summary}


@router.get(
    "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/last-run",
)
async def scenario_last_run_detail(
    artifact_id: str = Path(..., min_length=1),
    scenario_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Full detail of the most recent run for one scenario, including:
      * per-step records (status, duration, error, resolved selector/bbox)
      * drift report for each failing step (compared to live visual graph)
      * deterministic root-cause hints for failing steps
    """
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        await _verify_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )

        # ── Most recent run for this scenario
        result = await db.execute(
            select(E2ETestRunStepRow, E2ETestRunRow)
            .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
            .where(
                E2ETestRunStepRow.artifact_id == artifact_id,
                E2ETestRunStepRow.scenario_id == scenario_id,
                E2ETestRunStepRow.tenant_id == tenant_id,
            )
            .order_by(desc(E2ETestRunRow.started_at), E2ETestRunStepRow.step_number)
        )
        pairs = result.all()
        if not pairs:
            return {
                "artifact_id": artifact_id,
                "scenario_id": scenario_id,
                "has_runs": False,
            }

        # The first pair's run is the latest; collect all its steps
        latest_run = pairs[0][1]
        latest_steps = [
            step for step, run in pairs if run.run_id == latest_run.run_id
        ]
        latest_steps.sort(key=lambda s: s.step_number)

        # Last passing step lookup: previous run where status==passed
        prior_pairs = [(s, r) for s, r in pairs if r.run_id != latest_run.run_id]
        last_passing_by_step: dict[int, E2ETestRunStepRow] = {}
        for step, _run in prior_pairs:
            if (
                step.status == E2E_STEP_STATUS_PASSED
                and step.step_number not in last_passing_by_step
            ):
                last_passing_by_step[step.step_number] = step

    # ── Fetch live graph for drift + hints (out of the DB block so we
    # don't hold a transaction during HTTP)
    graph = await _fetch_visual_graph(artifact_id, tenant_id)
    scenes_by_id = {s["scene_id"]: s for s in (graph.get("scenes") or [])}
    controls_by_id: dict[str, dict] = {}
    for clist in (graph.get("controls_by_scene") or {}).values():
        for c in clist:
            controls_by_id[c["control_id"]] = c

    step_details = []
    for step in latest_steps:
        last_passing = last_passing_by_step.get(step.step_number)
        live_control = controls_by_id.get(step.evidence_control_id)
        live_scene = scenes_by_id.get(step.evidence_scene_id)
        expected_bbox = None
        if live_control:
            bbox_raw = live_control.get("bounding_box")
            if isinstance(bbox_raw, dict):
                expected_bbox = bbox_raw

        drift = detect_drift(
            expected_selector=step.expected_selector,
            resolved_selector=step.resolved_selector,
            expected_bbox=expected_bbox,
            resolved_bbox=step.resolved_bbox_json or {},
        )
        is_failing = step.status in (E2E_STEP_STATUS_FAILED,)
        hints: list[str] = []
        if is_failing:
            hints = root_cause_hints(
                failing_step=step,
                last_passing_step=last_passing,
                current_control=live_control,
                current_scene=live_scene,
            )

        step_details.append({
            **_row_to_dict(step),
            "drift": {
                "selector_drifted": drift.selector_drifted,
                "bbox_drifted": drift.bbox_drifted,
                "selector_diff": drift.selector_diff,
                "bbox_pixel_distance": drift.bbox_pixel_distance,
            },
            "root_cause_hints": hints,
            "last_passing_at": (
                last_passing.created_at.isoformat()
                if last_passing and last_passing.created_at else None
            ),
        })

    return {
        "artifact_id": artifact_id,
        "scenario_id": scenario_id,
        "has_runs": True,
        "run": _row_to_dict(latest_run),
        "steps": step_details,
    }
