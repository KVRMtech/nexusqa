"""
Nexus QA Orchestrator v0.2.0 — End-to-end QA Pipeline.

v0.2.0 — Modular refactor:
  app.config    → OrchestratorConfig
  app.models    → PipelineStage, KTSession, request/response models
  app.store     → RedisSessionStore (Redis + in-memory fallback)
  app.pipeline  → Full pipeline execution + polling helpers

Orchestrates all 10 engines for a complete QA workflow:
  - Upload recordings / documents
  - Transcribe → Shield → Extract rules → Generate tests
  - Generate test data → Execute tests → Generate reports → Notify
"""

from __future__ import annotations

import os
import sys
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, BackgroundTasks

# ─── Path Setup (must run before any nexus_sdk / app imports) ──
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk")
for p in [_THIS_DIR, _SDK_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

from nexus_sdk.auth import AuthService, NexusUser, get_current_user, init_auth

# ─── Modular sub-packages ─────────────────────────────────────
from app.config import OrchestratorConfig
from app.models import (
    PipelineStage,
    KTSession,
    CreateSessionRequest,
    RunPipelineRequest,
    SessionSummary,
)
from app.store import RedisSessionStore
from app.pipeline import run_full_pipeline

# Backward-compat re-exports so existing test imports keep working
from app.pipeline import (  # noqa: F401
    _poll_ears_job,
    _poll_eyes_job,
    _poll_legs_job,
    _log_timeline,
)

logger = logging.getLogger(__name__)

# ─── The QA Orchestrator ──────────────────────────────────────

app = FastAPI(
    title="Nexus QA Orchestrator",
    description="End-to-end QA pipeline for insurance product testing",
    version="0.2.0",
)

config = OrchestratorConfig()
auth = AuthService(jwt_secret=config.jwt_secret)

# Initialize the SDK global auth so get_current_user() dependency works
init_auth(jwt_secret=config.jwt_secret)

# Redis-backed session store (falls back to in-memory)
store = RedisSessionStore()

# HTTP client for calling engines
http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=660.0)
    await store.connect(config.redis_url)


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


# ─── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "qa-orchestrator"}


# ─── Session Management ───────────────────────────────────────

@app.post("/api/v1/qa/sessions", response_model=KTSession)
async def create_session(
    req: CreateSessionRequest,
    user: NexusUser = Depends(get_current_user),
):
    """Create a new KT session."""
    session = KTSession(
        session_id=str(uuid.uuid4()),
        tenant_id=req.tenant_id,
        name=req.name,
        description=req.description,
        created_by=user.user_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await store.save_session(session)
    await store.save_data(session.session_id, {
        "rules": [], "tests": [], "results": [],
        "timeline": [], "documents": [], "test_data": [], "reports": [],
    })
    return session


@app.get("/api/v1/qa/sessions")
async def list_sessions(
    user: NexusUser = Depends(get_current_user),
):
    """List all KT sessions for the current tenant."""
    return await store.list_sessions(user.tenant_id)


@app.get("/api/v1/qa/sessions/{session_id}", response_model=SessionSummary)
async def get_session(
    session_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Get full session details."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    data = await store.get_data(session_id)
    return SessionSummary(
        session=session,
        rules=data.get("rules", []),
        test_cases=data.get("tests", []),
        test_results=data.get("results", []),
        timeline=data.get("timeline", []),
    )


@app.post("/api/v1/qa/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Cancel a processing session — marks it as failed/cancelled."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.pipeline_stage == PipelineStage.COMPLETED:
        raise HTTPException(status_code=400, detail="Session already completed")
    session.pipeline_stage = PipelineStage.FAILED
    session.error = "Cancelled by user"
    await store.save_session(session)
    await _log_timeline(store, session_id, "pipeline_cancelled", "Pipeline cancelled by user")
    return {"session_id": session_id, "status": "cancelled"}


@app.delete("/api/v1/qa/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Permanently delete a session and all its data."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await store.delete_session(session_id)
    return {"session_id": session_id, "status": "deleted"}


# ─── Upload KT Recording ──────────────────────────────────────

@app.post("/api/v1/qa/sessions/{session_id}/upload-audio")
async def upload_audio(
    session_id: str,
    background_tasks: BackgroundTasks,
    audio: UploadFile = File(...),
    user: NexusUser = Depends(get_current_user),
):
    """Upload audio recording for a KT session."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    files = {"audio": (audio.filename, await audio.read(), audio.content_type)}
    data = {"tenant_id": session.tenant_id, "session_id": session_id}

    token = auth.create_token(user)
    response = await http_client.post(
        f"{config.ears_url}/api/v1/ears/transcribe",
        files=files, data=data,
        headers={"Authorization": f"Bearer {token}"},
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Ears engine error")

    result = response.json()
    session.audio_job_id = result.get("job_id")
    session.pipeline_stage = PipelineStage.TRANSCRIBING
    await store.save_session(session)

    await _log_timeline(store, session_id, "audio_uploaded", f"Audio uploaded: {audio.filename}")

    return {"job_id": session.audio_job_id, "status": "transcribing"}


@app.post("/api/v1/qa/sessions/{session_id}/upload-video")
async def upload_video(
    session_id: str,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    user: NexusUser = Depends(get_current_user),
):
    """Upload screen recording for a KT session."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    files = {"video": (video.filename, await video.read(), video.content_type)}
    data = {"tenant_id": session.tenant_id, "session_id": session_id}

    token = auth.create_token(user)
    response = await http_client.post(
        f"{config.eyes_url}/api/v1/eyes/analyze-video",
        files=files, data=data,
        headers={"Authorization": f"Bearer {token}"},
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Eyes engine error")

    result = response.json()
    session.video_job_id = result.get("job_id")
    session.pipeline_stage = PipelineStage.VISUAL_ANALYZING
    await store.save_session(session)

    await _log_timeline(store, session_id, "video_uploaded", f"Screen recording uploaded: {video.filename}")

    return {"job_id": session.video_job_id, "status": "analyzing"}


# ─── Upload Documents ─────────────────────────────────────────

@app.post("/api/v1/qa/sessions/{session_id}/upload-document")
async def upload_document(
    session_id: str,
    document: UploadFile = File(...),
    user: NexusUser = Depends(get_current_user),
):
    """Upload a document for ingestion via Spine engine."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    files = {"file": (document.filename, await document.read(), document.content_type)}
    data = {"tenant_id": session.tenant_id, "session_id": session_id}

    token = auth.create_token(user)
    response = await http_client.post(
        f"{config.spine_url}/api/v1/spine/ingest",
        files=files, data=data,
        headers={"Authorization": f"Bearer {token}"},
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Spine engine error")

    result = response.json()
    session.documents_ingested += 1
    await store.save_session(session)

    sdata = await store.get_data(session_id)
    sdata.setdefault("documents", []).append(result)
    await store.save_data(session_id, sdata)

    await _log_timeline(store, session_id, "document_uploaded", f"Document ingested: {document.filename}")

    return {"status": "ingested", "document": result}


# ─── Generate Reports ─────────────────────────────────────────

@app.post("/api/v1/qa/sessions/{session_id}/generate-report")
async def generate_session_report(
    session_id: str,
    report_type: str = "full_session_report",
    format: str = "html",
    user: NexusUser = Depends(get_current_user),
):
    """Generate a report for this QA session via Mouth engine."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    data = await store.get_data(session_id)
    token = auth.create_token(user)

    response = await http_client.post(
        f"{config.mouth_url}/api/v1/mouth/generate",
        json={
            "tenant_id": session.tenant_id,
            "session_id": session_id,
            "report_type": report_type,
            "format": format,
            "title": session.name,
            "rules": data.get("rules", []),
            "test_cases": data.get("tests", []),
            "test_results": data.get("results", []),
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Mouth engine error")

    result = response.json()
    session.reports_generated += 1
    await store.save_session(session)

    data.setdefault("reports", []).append(result)
    await store.save_data(session_id, data)

    await _log_timeline(store, session_id, "report_generated", f"Report generated: {report_type}")

    return result


# ─── Run Full Pipeline ────────────────────────────────────────

@app.post("/api/v1/qa/sessions/{session_id}/run-pipeline")
async def run_pipeline(
    session_id: str,
    req: RunPipelineRequest,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """Run the complete QA pipeline for a session."""
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    background_tasks.add_task(
        run_full_pipeline,
        session_id=session_id,
        tenant_id=req.tenant_id,
        user=user,
        sut_url=req.sut_url,
        sut_credentials=req.sut_credentials,
        skip_execution=req.skip_test_execution,
        notify=req.notify_on_complete,
        store=store,
        config=config,
        http_client=http_client,
        auth=auth,
    )

    return {"session_id": session_id, "status": "pipeline_started"}


# ─── Dashboard ────────────────────────────────────────────────

@app.get("/api/v1/qa/dashboard/summary")
async def dashboard_summary(
    user: NexusUser = Depends(get_current_user),
):
    """Get dashboard summary for the current tenant."""
    tenant_sessions = await store.list_sessions(user.tenant_id)

    return {
        "total_sessions": len(tenant_sessions),
        "active_pipelines": sum(
            1 for s in tenant_sessions
            if s.pipeline_stage not in (PipelineStage.COMPLETED, PipelineStage.FAILED)
        ),
        "total_rules": sum(s.rules_extracted for s in tenant_sessions),
        "total_tests": sum(s.tests_generated for s in tenant_sessions),
        "total_passed": sum(s.tests_passed for s in tenant_sessions),
        "total_failed": sum(s.tests_failed for s in tenant_sessions),
        "total_documents": sum(s.documents_ingested for s in tenant_sessions),
        "total_test_data_records": sum(s.test_data_records for s in tenant_sessions),
        "total_reports": sum(s.reports_generated for s in tenant_sessions),
        "pass_rate": (
            sum(s.tests_passed for s in tenant_sessions)
            / max(sum(s.tests_executed for s in tenant_sessions), 1)
            * 100
        ),
        "recent_sessions": [
            {
                "session_id": s.session_id,
                "name": s.name,
                "stage": s.pipeline_stage.value,
                "rules": s.rules_extracted,
                "tests": s.tests_generated,
                "passed": s.tests_passed,
                "failed": s.tests_failed,
            }
            for s in sorted(tenant_sessions, key=lambda x: x.created_at, reverse=True)[:10]
        ],
    }


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8092)
