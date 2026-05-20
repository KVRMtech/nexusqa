"""
Nexus Orchestrator — Generic Chain-Based Workflow Engine.

Step 2: Build an orchestration layer that chains engines together.
Different chains = different products.

This replaces the hardcoded pipeline approach with a flexible,
configurable workflow engine.  Chain definitions specify which
engines to call, in what order, with what data — and the engine
executes them as a DAG with full state persistence, retries,
polling, and resume.

Built-in chains:
    nexus.qa-testing        — Full QA pipeline (all 11 engines)
    nexus.compliance-audit  — Document compliance checking
    nexus.knowledge-capture — KT session knowledge extraction
    nexus.regression-suite  — Re-run tests from existing rules

API surface:
    Chain CRUD        POST/GET/DELETE /api/v1/orchestrator/chains
    File uploads      POST            /api/v1/orchestrator/files/upload
    Workflow control   POST/GET        /api/v1/orchestrator/workflows
    Dashboard          GET             /api/v1/orchestrator/dashboard
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import httpx

from nexus_sdk.auth import AuthService, NexusUser, get_auth_service, get_current_user, init_auth
from nexus_sdk.workflows import canonical_pipeline_plan
from nexus_sdk.workflows.models import WorkflowStatus as PlaneWorkflowStatus

from .workflows.schema import (
    ChainDefinition,
    ChainListItem,
    StageStatus,
    StartWorkflowRequest,
    StartWorkflowResponse,
    WorkflowInstance,
    WorkflowStatus,
    WorkflowSummary,
)
from .workflows.engine import (
    ChainEngine,
    EngineURLResolver,
    FileStore,
    WorkflowStore,
)
from .workflows.registry import ChainRegistry
from .workflows.builtin import load_all_builtin_chains
from .workflow_plane import WorkflowPlane

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class OrchestratorConfig(BaseSettings):
    """Central configuration — reads from environment variables."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    # Product mode: "canonical" (default) or "full" (legacy all-products)
    product_mode: str = Field(default="canonical", alias="NEXUS_PRODUCT_MODE")

    # Engine URLs (internal network) — aliases match docker-compose env vars
    shield_url: str = Field(default="http://localhost:8001", alias="SHIELD_ENGINE_URL")
    ears_url: str = Field(default="http://localhost:8002", alias="EARS_ENGINE_URL")
    eyes_url: str = Field(default="http://localhost:8003", alias="EYES_ENGINE_URL")
    heart_url: str = Field(default="http://localhost:8004", alias="HEART_ENGINE_URL")
    backbone_url: str = Field(default="http://localhost:8005", alias="BACKBONE_ENGINE_URL")
    nerves_url: str = Field(default="http://localhost:8006", alias="NERVES_ENGINE_URL")
    legs_url: str = Field(default="http://localhost:8007", alias="LEGS_ENGINE_URL")
    hands_url: str = Field(default="http://localhost:8008", alias="HANDS_ENGINE_URL")
    spine_url: str = Field(default="http://localhost:8009", alias="SPINE_ENGINE_URL")
    mouth_url: str = Field(default="http://localhost:8010", alias="MOUTH_ENGINE_URL")
    brain_url: str = Field(default="http://localhost:8011", alias="BRAIN_ENGINE_URL")

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT — reads from NEXUS_JWT_SECRET env var
    jwt_secret: str = Field(default="dev-jwt-secret-key-change-in-production", alias="NEXUS_JWT_SECRET")

    # File storage
    upload_path: str = "/data/nexus/orchestrator/uploads"

    # Processing
    default_processing_profile: str = Field(
        default="multimodal", alias="ORCHESTRATOR_DEFAULT_PROCESSING_PROFILE",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8100

    # P1 Fix: Configurable processing lock TTL (seconds).
    # Default extended from 2h to 4h to prevent premature lock
    # expiry for long videos (>2h duration).
    processing_lock_ttl_seconds: int = Field(
        default=14400, alias="CANONICAL_PROCESSING_LOCK_TTL",
    )

    # Per-tenant admission control: maximum concurrent workflows per tenant.
    # 0 = unlimited (no admission control).
    max_concurrent_workflows_per_tenant: int = Field(
        default=10, alias="MAX_CONCURRENT_WORKFLOWS_PER_TENANT",
    )

    # D2 fix: deep profile runs LLaVA per-frame and is GPU-only. CPU
    # deployments quarantine deep workflows ~13 min in. When this flag
    # is false, the orchestrator rejects deep profile submissions at
    # ingress with a clear error message instead of letting them fail
    # silently downstream. Production GPU deployments should set this
    # to true via env.
    deep_profile_enabled: bool = Field(
        default=False, alias="EYES_DEEP_PROFILE_ENABLED",
    )


# ─── Application Setup ────────────────────────────────────────

config = OrchestratorConfig()


# ─── Per-Tenant Admission Control ─────────────────────────────

class TenantAdmissionControl:
    """
    Track in-flight workflows per tenant to prevent resource exhaustion.

    Architect followup #3: This class used to keep an in-memory dict
    keyed by tenant_id. That worked for a single orchestrator pod but
    silently let `replica_count × max_per_tenant` workflows through
    on the production 3-replica deployment — tenant limits were not
    globally enforced.

    The Redis-backed `TenantConcurrencyLimiter` from the SDK
    replaces the dict with an atomic Lua check-and-incr against a
    shared Redis key. The in-memory dict survives as a `_local`
    fallback so when Redis is unreachable the orchestrator
    fail-opens (allow) instead of refusing every upload.
    """

    def __init__(self, max_per_tenant: int = 10):
        self._max = max_per_tenant
        # In-memory snapshot for /admission/status only — actual
        # enforcement happens against Redis when configured.
        self._local: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._redis_limiter = None   # injected post-init by main.py

    def attach_redis_limiter(self, limiter) -> None:
        """Wire the SDK's Redis-backed concurrency limiter. Called
        from main.py once the orchestrator's Redis client is up."""
        self._redis_limiter = limiter

    @property
    def enabled(self) -> bool:
        return self._max > 0

    async def acquire(self, tenant_id: str) -> bool:
        """Try to acquire a workflow slot for the tenant.
        Returns True if allowed, False if limit exceeded.

        Prefers the Redis-backed atomic limiter. Falls back to the
        in-memory dict only when Redis is unavailable (limiter is
        None) — same per-replica drift as before, but at least
        bounded per-replica when the shared limiter is missing.
        """
        if not self.enabled:
            return True
        if self._redis_limiter is not None:
            allowed, _count = await self._redis_limiter.acquire(tenant_id)
            return allowed
        async with self._lock:
            current = self._local.get(tenant_id, 0)
            if current >= self._max:
                return False
            self._local[tenant_id] = current + 1
            return True

    async def release(self, tenant_id: str) -> None:
        """Release a workflow slot when a workflow completes or fails."""
        if not self.enabled:
            return
        if self._redis_limiter is not None:
            await self._redis_limiter.release(tenant_id)
            return
        async with self._lock:
            current = self._local.get(tenant_id, 0)
            if current > 0:
                self._local[tenant_id] = current - 1
            if self._local.get(tenant_id) == 0:
                del self._local[tenant_id]

    def snapshot(self) -> dict[str, int]:
        """In-memory snapshot for /admission/status. Note: when Redis
        is the source of truth, this only reflects local fallback
        accounting — operators should consult Redis directly via
        `nexus:admission:concurrent:*` for the authoritative view."""
        return dict(self._local)


admission = TenantAdmissionControl(
    max_per_tenant=config.max_concurrent_workflows_per_tenant,
)
# Architect P2: queue-saturation guard. Wired by workflow_plane at
# startup once the inventory Redis client is up — None means
# fail-open (no rejection).
queue_saturation_guard = None

app = FastAPI(
    title="Nexus Orchestrator",
    description=(
        "Generic chain-based workflow engine. "
        "Different chains = different products."
    ),
    version="0.2.0",
)

# --- Singletons (initialised on startup) ---
auth = init_auth(jwt_secret=config.jwt_secret, jwt_expiry_hours=24)
registry = ChainRegistry()
workflow_store = WorkflowStore()
file_store = FileStore(base_path=config.upload_path)
http_client: Optional[httpx.AsyncClient] = None
chain_engine: Optional[ChainEngine] = None
workflow_plane: Optional[WorkflowPlane] = None


def _make_service_token(tenant_id: str) -> str:
    """Create a JWT for engine-to-engine authentication."""
    user = NexusUser(
        user_id="orchestrator-service",
        tenant_id=tenant_id,
        email="orchestrator@nexus.internal",
        role="admin",
        permissions=["*"],
    )
    return auth.create_token(user)


def _url_overrides() -> dict[str, str]:
    return {
        "shield": config.shield_url,
        "ears": config.ears_url,
        "eyes": config.eyes_url,
        "heart": config.heart_url,
        "backbone": config.backbone_url,
        "nerves": config.nerves_url,
        "legs": config.legs_url,
        "hands": config.hands_url,
        "spine": config.spine_url,
        "mouth": config.mouth_url,
        "brain": config.brain_url,
    }


async def _update_session_status(
    tenant_id: str,
    session_id: str,
    status: str,
) -> bool:
    """Update session state for cache-hit and pre-workflow edge paths."""
    platform_api_url = os.environ.get("PLATFORM_API_URL", "")
    if not platform_api_url or not session_id:
        return False

    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                f"{platform_api_url}/api/v1/sessions/{session_id}",
                json={"status": status},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                logger.info("Session %s status updated to %s", session_id, status)
                return True
            logger.warning(
                "Session status update failed: tenant=%s session=%s status=%s http=%s body=%s",
                tenant_id,
                session_id,
                status,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning(
            "Session status update error: tenant=%s session=%s status=%s error=%s",
            tenant_id,
            session_id,
            status,
            exc,
        )
    return False


# ─── Lifecycle ─────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global http_client, chain_engine, workflow_plane

    # HTTP client for calling engines (5-minute timeout for ML tasks)
    http_client = httpx.AsyncClient(timeout=300.0)

    # Connect stores to Redis
    await registry.connect(config.redis_url)
    await workflow_store.connect(config.redis_url)
    await file_store.connect(config.redis_url)
    await file_store.recover()

    # Build chain engine
    url_resolver = EngineURLResolver(overrides=_url_overrides())
    for eng, url in url_resolver._urls.items():
        logger.info("  Engine %-10s → %s", eng, url)
    chain_engine = ChainEngine(
        url_resolver=url_resolver,
        workflow_store=workflow_store,
        file_store=file_store,
        http_client=http_client,
        token_factory=_make_service_token,
    )

    # Register built-in chains
    builtins = load_all_builtin_chains()
    await registry.register_builtins(builtins)

    # ── Production dependency enforcement ─────────────────────
    # In non-degraded mode, verify critical services are actually
    # connected (not just silently falling back to in-memory).
    nexus_env = os.environ.get("NEXUS_ENV", "development").lower()
    allow_degraded = os.environ.get("NEXUS_ALLOW_DEGRADED_MODE", "false").lower() == "true"
    if not allow_degraded:
        critical_failures = []
        if workflow_store._redis is None:
            critical_failures.append("Redis (workflow-store): not connected")
        if registry._redis is None:
            critical_failures.append("Redis (chain-registry): not connected")
        if critical_failures:
            msg = (
                f"[PRODUCTION GUARD] Orchestrator cannot start with in-memory "
                f"fallbacks disabled. Failures:\n"
                + "\n".join(f"  - {f}" for f in critical_failures)
                + "\n\nSet NEXUS_ALLOW_DEGRADED_MODE=true for local dev."
            )
            logger.critical(msg)
            raise RuntimeError(msg)
        logger.info("Production dependency check passed: Redis connected for all stores")

    # ── Recover stale 'running' workflows from before restart ──
    await _reconcile_running_workflows()

    # ── Canonical workflow plane (Phase 1) ─────────────────────
    # Best-effort init: if Postgres or Redis is unreachable we log
    # and continue so the legacy chain engine keeps serving traffic.
    # In non-degraded mode the production guard above already
    # blocked startup if Redis was down; the DB connect is the new
    # critical dependency for this code path.
    try:
        workflow_plane = WorkflowPlane(config.redis_url)
        await workflow_plane.start()
        workflow_plane.attach(app)
        logger.info(
            "workflow_plane.attached routes=/api/v1/workflows + /api/v1/admin/dlq",
        )
    except Exception as exc:
        if not allow_degraded:
            logger.critical(
                "workflow_plane.start_failed err=%s — refusing to serve in non-degraded mode",
                exc,
            )
            raise
        workflow_plane = None
        logger.warning(
            "workflow_plane.unavailable err=%s — legacy chain engine remains active",
            exc,
        )

    logger.info(
        "Nexus Orchestrator started — %d built-in chains registered (product_mode=%s)",
        len(builtins),
        config.product_mode,
    )


async def _reconcile_running_workflows():
    """
    Scan Redis for workflows stuck in 'running' state after an orchestrator
    restart.  These lost their in-memory execution loop when the process
    died.  For each one, reset running stages to pending and re-enqueue
    execution as a background task.
    """
    try:
        all_instances = await workflow_store._all_instances()
        stale = [
            inst for inst in all_instances
            if inst.status == WorkflowStatus.RUNNING
        ]
        if not stale:
            logger.info("orchestrator.reconcile: no stale running workflows")
            return

        logger.warning(
            "orchestrator.reconcile: found %d stale running workflow(s), recovering",
            len(stale),
        )

        for inst in stale:
            chain = await registry.get(inst.chain_id)
            if not chain:
                logger.error(
                    "orchestrator.reconcile: chain '%s' not found for workflow %s, marking failed",
                    inst.chain_id, inst.workflow_id,
                )
                inst.status = WorkflowStatus.FAILED
                inst.error = f"Chain '{inst.chain_id}' no longer registered — cannot recover"
                inst.completed_at = datetime.now(timezone.utc).isoformat()
                await workflow_store.save_instance(inst)
                continue

            # Reset any stages still marked 'running' back to 'pending'
            # so the execution loop re-processes them.
            for stage_exec in inst.stages.values():
                if stage_exec.status == StageStatus.RUNNING:
                    stage_exec.status = StageStatus.PENDING
                    stage_exec.error = None

            inst.status = WorkflowStatus.CREATED
            inst.error = None
            await workflow_store.save_instance(inst)

            # Re-enqueue via asyncio.create_task (startup has no BackgroundTasks)
            asyncio.create_task(chain_engine.execute(inst.workflow_id, chain))

            logger.info(
                "orchestrator.reconcile: re-enqueued workflow %s (chain=%s)",
                inst.workflow_id, inst.chain_id,
            )
    except Exception as exc:
        logger.error("orchestrator.reconcile: failed — %s", exc, exc_info=True)


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()
    if workflow_plane is not None:
        try:
            await workflow_plane.aclose()
        except Exception as exc:
            logger.warning("workflow_plane.aclose_failed err=%s", exc)


# ═══════════════════════════════════════════════════════════════
#  CHAIN MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@app.get(
    "/api/v1/orchestrator/chains",
    response_model=list[ChainListItem],
    tags=["Chains"],
)
async def list_chains(
    user: NexusUser = Depends(get_current_user),
):
    """List all chains visible to the current tenant."""
    chains = await registry.list_chains(tenant_id=user.tenant_id)

    # Canonical-only mode: expose only the canonical chain
    if config.product_mode == "canonical":
        chains = [c for c in chains if c.chain_id == "nexus.canonical-processing"]

    return [
        ChainListItem(
            chain_id=c.chain_id,
            name=c.name,
            description=c.description,
            version=c.version,
            tags=c.tags,
            stage_count=len(c.stages),
            tenant_id=c.tenant_id,
        )
        for c in chains
    ]


@app.get(
    "/api/v1/orchestrator/chains/{chain_id}",
    response_model=ChainDefinition,
    tags=["Chains"],
)
async def get_chain(
    chain_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Get the full definition of a chain."""
    chain = await registry.get(chain_id)
    if not chain:
        raise HTTPException(status_code=404, detail="Chain not found")
    if chain.tenant_id and chain.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised for this chain")
    return chain


@app.post(
    "/api/v1/orchestrator/chains",
    response_model=ChainListItem,
    status_code=201,
    tags=["Chains"],
)
async def register_chain(
    chain: ChainDefinition,
    user: NexusUser = Depends(get_current_user),
):
    """Register a new chain definition (tenant-specific)."""
    # Validate structural correctness
    errors = registry.validate_chain(chain)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"validation_errors": errors},
        )

    # Enforce tenant ownership for custom chains
    chain.tenant_id = user.tenant_id
    chain.created_by = user.user_id
    chain.created_at = datetime.now(timezone.utc).isoformat()

    await registry.register(chain)

    return ChainListItem(
        chain_id=chain.chain_id,
        name=chain.name,
        description=chain.description,
        version=chain.version,
        tags=chain.tags,
        stage_count=len(chain.stages),
        tenant_id=chain.tenant_id,
    )


@app.delete(
    "/api/v1/orchestrator/chains/{chain_id}",
    tags=["Chains"],
)
async def delete_chain(
    chain_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Delete a tenant-owned chain (system chains cannot be deleted)."""
    chain = await registry.get(chain_id)
    if not chain:
        raise HTTPException(status_code=404, detail="Chain not found")
    if chain.tenant_id == "":
        raise HTTPException(
            status_code=403, detail="Cannot delete system-level chains"
        )
    if chain.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised")

    await registry.delete(chain_id)
    return {"deleted": True, "chain_id": chain_id}


# ═══════════════════════════════════════════════════════════════
#  FILE UPLOADS
# ═══════════════════════════════════════════════════════════════

# P0 Fix: Magic-byte validation for uploaded media files.
# Prevents processing of disguised non-media files that could
# cause downstream engine failures or security issues.
_AUDIO_MAGIC = [
    (b'RIFF', 0, b'WAVE', 8),   # WAV
    (b'ID3', 0, None, None),     # MP3 (ID3 tag)
    (b'\xff\xfb', 0, None, None),  # MP3 (no tag)
    (b'\xff\xf3', 0, None, None),  # MP3 (no tag)
    (b'\xff\xf2', 0, None, None),  # MP3 (no tag)
    (b'fLaC', 0, None, None),    # FLAC
    (b'OggS', 0, None, None),    # OGG
]
_VIDEO_MAGIC = [
    (b'\x1a\x45\xdf\xa3', 0, None, None),  # WebM / MKV (EBML)
    (b'RIFF', 0, b'AVI ', 8),               # AVI
]
_MP4_FTYP_OFFSET = 4  # MP4/MOV have 'ftyp' at byte 4


def _validate_media_content(
    content: bytes, expected_type: str, filename: str,
) -> None:
    """Validate file content matches expected media type via magic bytes.

    Args:
        content: Raw file bytes (only first 32 bytes are checked).
        expected_type: 'audio' or 'video'.
        filename: Original filename for error messages.

    Raises:
        HTTPException(415) if content doesn't match any known media signature.
    """
    if len(content) < 12:
        raise HTTPException(
            status_code=415,
            detail=f"File '{filename}' is too small to be a valid {expected_type} file.",
        )

    header = content[:32]

    # MP4/MOV/M4A: 'ftyp' at offset 4 (valid for both audio and video)
    if header[4:8] == b'ftyp':
        return

    # Check type-specific magic bytes
    patterns = _AUDIO_MAGIC if expected_type == "audio" else _VIDEO_MAGIC
    # Audio patterns are also valid for video (e.g. WAV can be audio track)
    if expected_type == "video":
        patterns = _VIDEO_MAGIC + _AUDIO_MAGIC

    for magic, offset, magic2, offset2 in patterns:
        if header[offset:offset + len(magic)] == magic:
            if magic2 is None:
                return
            if header[offset2:offset2 + len(magic2)] == magic2:
                return

    raise HTTPException(
        status_code=415,
        detail=(
            f"File '{filename}' does not appear to be a valid {expected_type} file. "
            f"Supported formats: WAV, MP3, FLAC, OGG, MP4, WebM, MKV, AVI, MOV."
        ),
    )


class FileUploadResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    content_type: str


@app.post(
    "/api/v1/orchestrator/files/upload",
    response_model=FileUploadResponse,
    tags=["Files"],
)
async def upload_file(
    file: UploadFile = File(..., description="Audio, video, or document file"),
    tenant_id: str = Form(...),
    user: NexusUser = Depends(get_current_user),
):
    """
    Upload a file for use in workflow input_data.

    Returns a file_id that should be passed in StartWorkflowRequest.input_data,
    e.g. {"audio_file_id": "<returned-file-id>"}
    """
    # Guard: 500 MB max upload to prevent OOM
    MAX_UPLOAD_BYTES = 500 * 1024 * 1024
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum upload size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    # P0 Fix: Validate file is actually a media file via magic bytes
    _validate_media_content(
        content, "audio",  # generic upload — accept any media type
        file.filename or "upload",
    )
    meta = await file_store.store(
        filename=file.filename or "upload",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        tenant_id=tenant_id,
    )
    return FileUploadResponse(
        file_id=meta["file_id"],
        filename=meta["filename"],
        size_bytes=meta["size_bytes"],
        content_type=meta["content_type"],
    )


# ═══════════════════════════════════════════════════════════════
#  WORKFLOW MANAGEMENT
# ═══════════════════════════════════════════════════════════════


async def _extract_audio_from_video(
    video_file_id: str, store: FileStore, tenant_id: str,
) -> Optional[str]:
    """Auto-extract the embedded audio track from an uploaded video file.

    Required because the UI upload flow lets a user drop only a video
    (Zoom recordings package audio + video as separate files; only one
    often makes it into the file picker).  Without this, the
    audio_transcription stage runs against an empty audio_file_id and
    the SME's narration — the highest-confidence signal for action
    intent — is silently lost.

    Strategy: shell out to ffmpeg with ``-vn -acodec copy`` so we re-mux
    the existing AAC stream into an .m4a container without re-encoding.
    That keeps the call sub-second even on multi-minute recordings and
    preserves audio fidelity.  Falls back to re-encoding to WAV when copy
    is not possible (e.g. weird video codec with PCM audio).

    Returns the new audio file_id on success, ``None`` on failure (caller
    proceeds without audio rather than blocking the whole workflow).
    """
    import asyncio
    import subprocess
    import tempfile

    try:
        video_bytes = await store.read(video_file_id)
        if not video_bytes:
            return None

        with tempfile.TemporaryDirectory(prefix="nexus_audio_extract_") as tmpdir:
            video_path = os.path.join(tmpdir, "input.mp4")
            audio_path = os.path.join(tmpdir, "extracted.m4a")
            with open(video_path, "wb") as f:
                f.write(video_bytes)

            async def _run(cmd: list[str]) -> tuple[int, bytes]:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                return proc.returncode, err or b""

            # Fast path: stream copy when the audio codec is container-compatible.
            rc, err = await _run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", video_path,
                "-vn", "-acodec", "copy",
                audio_path,
            ])
            # Fallback: re-encode to AAC.  Triggered when copy fails (e.g. PCM
            # audio in MOV) or yields an unreadable file.
            if rc != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                rc, err = await _run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", video_path,
                    "-vn", "-c:a", "aac", "-b:a", "128k",
                    audio_path,
                ])
            if rc != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                logger.warning(
                    "Auto audio-extract failed: video=%s rc=%d err=%s",
                    video_file_id, rc, err.decode("utf-8", errors="replace")[:300],
                )
                return None

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            meta = await store.store(
                filename="extracted_audio.m4a",
                content=audio_bytes,
                content_type="audio/mp4",
                tenant_id=tenant_id,
            )
            new_id = meta["file_id"]
            logger.info(
                "Auto-extracted audio: video=%s audio=%s bytes=%d",
                video_file_id, new_id, len(audio_bytes),
            )
            return new_id
    except asyncio.TimeoutError:
        logger.warning("Auto audio-extract timed out: video=%s", video_file_id)
        return None
    except FileNotFoundError:
        # ffmpeg not installed — graceful degradation
        logger.warning("Auto audio-extract skipped: ffmpeg not available")
        return None
    except Exception as exc:
        logger.warning("Auto audio-extract error: video=%s err=%s", video_file_id, exc)
        return None


async def _compute_media_fingerprint(
    req: StartWorkflowRequest, store: FileStore
) -> Optional[str]:
    """Compute a SHA-256 fingerprint across all uploaded media files.

    Returns the hex digest or None if no files were uploaded.
    """
    input_data = req.input_data or {}
    file_ids: list[str] = []
    for key in ("audio_file_id", "video_file_id", "screen_file_id"):
        fid = input_data.get(key)
        if fid:
            file_ids.append(fid)

    if not file_ids:
        return None

    h = hashlib.sha256()
    for fid in sorted(file_ids):
        content = await store.read(fid)
        if content:
            h.update(content)
    return h.hexdigest()


async def _check_fingerprint_cache(
    tenant_id: str, fingerprint: str, spine_url: str
) -> Optional[dict]:
    """Check if a canonical artifact already exists for this fingerprint.

    Calls the Spine engine's probe endpoint via its Redis-backed cache.
    Returns the cached artifact metadata or None.

    P3: Structured logging for cache decisions. Only returns cache hits
    for terminal (completed) artifacts — in-flight artifacts are not
    eligible for cache reuse. Failures are logged, never swallowed.
    Returns cache_reason in all paths for deterministic auditing.
    """
    try:
        token = _make_service_token(tenant_id)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{spine_url}/api/v1/spine/persist-canonical-artifact",
                json={
                    "tenant_id": tenant_id,
                    "media_fingerprint": fingerprint,
                    # Empty pipeline data signals "cache check only"
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                # Only status="cached" means a real fingerprint match.
                # status="completed"/"no_match" means no existing artifact.
                has_existing = (
                    data.get("status") == "cached"
                    and data.get("artifact_id")
                    and data.get("success", True)
                )
                if has_existing:
                    # P3: Check artifact has required provenance fields
                    cached_artifact_id = data.get("artifact_id", "")
                    if not cached_artifact_id:
                        logger.info(
                            "P3 cache skip: fingerprint=%s cached entry has no artifact_id "
                            "(cache_reason=missing_artifact_id)",
                            fingerprint[:16],
                        )
                        return None

                    # status == "cached" is confirmed by has_existing check
                    # above. The Redis cache in Spine only stores entries for
                    # successfully completed artifacts, so this is trustworthy.
                    logger.info(
                        "P3 cache hit: fingerprint=%s artifact=%s "
                        "(cache_reason=terminal_artifact_exists)",
                        fingerprint[:16], cached_artifact_id,
                    )
                    return {
                        **data,
                        "artifact_status": "completed",
                        "cache_reason": "terminal_artifact_exists",
                    }
                else:
                    logger.info(
                        "P3 cache miss: fingerprint=%s tenant=%s "
                        "(cache_reason=no_existing_artifact)",
                        fingerprint[:16], tenant_id,
                    )
            else:
                logger.warning(
                    "P3 cache check failed: fingerprint=%s HTTP %d — %s "
                    "(cache_reason=spine_error)",
                    fingerprint[:16], resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        # P3: Never swallow cache failures silently
        logger.warning(
            "P3 cache check error: fingerprint=%s tenant=%s error=%s "
            "(cache_reason=exception, proceeding without cache — duplicate processing possible)",
            fingerprint[:16], tenant_id, exc,
        )
    return None


async def _link_cached_artifact_to_session(
    tenant_id: str,
    session_id: str,
    artifact_id: str,
    spine_url: str,
) -> bool:
    """Record a session-level alias for a reused canonical artifact."""
    token = _make_service_token(tenant_id)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{spine_url}/api/v1/spine/artifacts/{artifact_id}/reuse",
                json={
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200 and resp.json().get("success"):
                return True
            logger.warning(
                "Cached artifact reuse link failed: tenant=%s session=%s artifact=%s status=%s body=%s",
                tenant_id,
                session_id,
                artifact_id,
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        logger.warning(
            "Cached artifact reuse link error: tenant=%s session=%s artifact=%s error=%s",
            tenant_id,
            session_id,
            artifact_id,
            exc,
        )
    return False


@app.post(
    "/api/v1/orchestrator/workflows/start",
    response_model=StartWorkflowResponse,
    tags=["Workflows"],
)
async def start_workflow(
    req: StartWorkflowRequest,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """
    Start a new workflow execution.

    The chain_id identifies WHICH chain (product) to run.
    The input_data provides file IDs, SUT config, options, etc.

    Execution is asynchronous — poll GET /workflows/{id} for status.
    """
    chain = await registry.get(req.chain_id)
    if not chain:
        raise HTTPException(status_code=404, detail="Chain not found")
    if chain.tenant_id and chain.tenant_id != req.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised for this chain")

    # ── Canonical-only mode: block non-canonical chains ───────
    if config.product_mode == "canonical" and req.chain_id != "nexus.canonical-processing":
        logger.warning(
            "Blocked non-canonical chain '%s' start (product_mode=canonical, user=%s)",
            req.chain_id, user.user_id,
        )
        raise HTTPException(
            status_code=403,
            detail=f"Chain '{req.chain_id}' is not available in canonical-only mode. "
                   f"Only 'nexus.canonical-processing' is supported.",
        )

    # ── Media fingerprint dedup for canonical processing ──────
    if req.chain_id == "nexus.canonical-processing":
        # Phase 1.5: Enforce session_id for canonical processing
        if not req.session_id or not req.session_id.strip():
            raise HTTPException(
                status_code=400,
                detail="session_id is required for canonical processing. Create a session first.",
            )

        if req.input_data is None:
            req.input_data = {}

        # D1 fix: compute fingerprint over the SOURCE uploads BEFORE
        # auto-extracting audio (extracted bytes vary between ffmpeg
        # runs, breaking dedup for identical re-uploads).
        fingerprint = await _compute_media_fingerprint(req, file_store)

        # Auto-extract audio from video when only video was registered.
        # Mirrors the /process endpoint logic so internal callers benefit too.
        if (
            req.input_data.get("video_file_id")
            and not req.input_data.get("audio_file_id")
        ):
            extracted_audio_id = await _extract_audio_from_video(
                req.input_data["video_file_id"], file_store, req.tenant_id,
            )
            if extracted_audio_id:
                req.input_data["audio_file_id"] = extracted_audio_id
        # Inject provenance fields if not already set (API callers should use /process)
        req.input_data.setdefault("source_type", "api_workflow_start")
        req.input_data.setdefault("source_filename", "unknown")
        req.input_data.setdefault("created_by", user.user_id)
        req.input_data.setdefault("processing_profile", config.default_processing_profile)

        # D2 fix: mirror the /process guard so internal callers also get
        # a clear error rather than a downstream quarantine on CPU.
        _profile = str(req.input_data.get("processing_profile") or "").lower()
        if _profile == "deep" and not config.deep_profile_enabled:
            raise HTTPException(
                status_code=400,
                detail=(
                    "deep profile requires a GPU-enabled deployment. "
                    "Use 'fast' or 'multimodal' instead, or contact your "
                    "admin to enable EYES_DEEP_PROFILE_ENABLED."
                ),
            )
        if fingerprint:
            cached = await _check_fingerprint_cache(
                req.tenant_id, fingerprint, config.spine_url
            )
            if cached:
                await _link_cached_artifact_to_session(
                    tenant_id=req.tenant_id,
                    session_id=req.session_id,
                    artifact_id=cached["artifact_id"],
                    spine_url=config.spine_url,
                )
                await _update_session_status(req.tenant_id, req.session_id, "completed")
                logger.info(
                    "Canonical artifact cache hit: tenant=%s fingerprint=%s artifact=%s reason=%s",
                    req.tenant_id, fingerprint[:16], cached.get("artifact_id"),
                    cached.get("cache_reason"),
                )
                return StartWorkflowResponse(
                    workflow_id=f"cached-{cached['artifact_id']}",
                    chain_id=req.chain_id,
                    chain_name=chain.name,
                    status=WorkflowStatus.COMPLETED,
                    session_id=req.session_id,
                    cache_hit=True,
                    cached_artifact_id=cached.get("artifact_id"),
                    cached_artifact_status=cached.get("artifact_status"),
                    cache_reason=cached.get("cache_reason"),
                )
            # Inject fingerprint into input_data for downstream stages
            if req.input_data is None:
                req.input_data = {}
            req.input_data["media_fingerprint"] = fingerprint

    # ── Per-tenant admission control ──────────────────────────
    if not await admission.acquire(req.tenant_id):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenant '{req.tenant_id}' has reached the maximum of "
                f"{admission._max} concurrent workflows. "
                "Wait for existing workflows to complete before starting new ones."
            ),
        )

    try:
        instance = await chain_engine.start(
            chain=chain,
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            input_data=req.input_data,
            created_by=user.user_id,
        )
    except Exception:
        # Release the slot if workflow creation fails
        await admission.release(req.tenant_id)
        raise

    async def _execute_and_release(workflow_id, chain, tenant_id):
        """Execute workflow then release admission slot."""
        try:
            await chain_engine.execute(workflow_id, chain)
        finally:
            await admission.release(tenant_id)

    # Execute in background
    background_tasks.add_task(
        _execute_and_release, instance.workflow_id, chain, req.tenant_id
    )

    return StartWorkflowResponse(
        workflow_id=instance.workflow_id,
        chain_id=chain.chain_id,
        chain_name=chain.name,
        status=instance.status,
        session_id=instance.session_id,
    )


@app.get(
    "/api/v1/orchestrator/workflows/{workflow_id}",
    response_model=WorkflowInstance,
    tags=["Workflows"],
)
async def get_workflow(
    workflow_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Get the full status of a workflow including all stage states and timeline."""
    instance = await workflow_store.get_instance(workflow_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if instance.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised")
    return instance


@app.get(
    "/api/v1/orchestrator/workflows",
    response_model=list[WorkflowSummary],
    tags=["Workflows"],
)
async def list_workflows(
    limit: int = Query(default=50, ge=1, le=500),
    session_id: Optional[str] = Query(default=None, description="Filter by session ID"),
    user: NexusUser = Depends(get_current_user),
):
    """List all workflows for the current tenant."""
    instances = await workflow_store.list_instances(
        tenant_id=user.tenant_id, limit=limit, session_id=session_id,
    )
    return [
        WorkflowSummary(
            workflow_id=i.workflow_id,
            chain_id=i.chain_id,
            chain_name=i.chain_name,
            tenant_id=i.tenant_id,
            status=i.status,
            created_at=i.created_at,
            stages_completed=sum(
                1
                for s in i.stages.values()
                if s.status.value in ("completed", "skipped")
            ),
            stages_total=len(i.stages),
            error=i.error,
        )
        for i in instances
    ]


@app.post(
    "/api/v1/orchestrator/workflows/{workflow_id}/resume",
    response_model=StartWorkflowResponse,
    tags=["Workflows"],
)
async def resume_workflow(
    workflow_id: str,
    background_tasks: BackgroundTasks,
    user: NexusUser = Depends(get_current_user),
):
    """
    Resume a failed or paused workflow from the last checkpoint.

    Completed stages are skipped; execution continues from the
    first incomplete stage.
    """
    instance = await workflow_store.get_instance(workflow_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if instance.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised")
    if instance.status not in (WorkflowStatus.FAILED, WorkflowStatus.PAUSED, WorkflowStatus.RUNNING):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume workflow in status '{instance.status.value}'",
        )

    chain = await registry.get(instance.chain_id)
    if not chain:
        raise HTTPException(
            status_code=404,
            detail=f"Chain '{instance.chain_id}' no longer exists",
        )

    # Reset failed/running stages to pending so they re-execute
    for stage_exec in instance.stages.values():
        if stage_exec.status.value in ("failed", "running"):
            stage_exec.status = StageStatus.PENDING
            stage_exec.error = None
            stage_exec.retries = 0
    instance.status = WorkflowStatus.CREATED
    instance.error = None
    await workflow_store.save_instance(instance)

    background_tasks.add_task(
        chain_engine.execute, workflow_id, chain
    )

    return StartWorkflowResponse(
        workflow_id=instance.workflow_id,
        chain_id=chain.chain_id,
        chain_name=chain.name,
        status=WorkflowStatus.RUNNING,
        session_id=instance.session_id,
    )


@app.post(
    "/api/v1/orchestrator/workflows/{workflow_id}/cancel",
    tags=["Workflows"],
)
async def cancel_workflow(
    workflow_id: str,
    user: NexusUser = Depends(get_current_user),
):
    """Cancel a running or queued workflow."""
    instance = await workflow_store.get_instance(workflow_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if instance.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Not authorised")

    success = await chain_engine.cancel(workflow_id)
    if not success:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel workflow in status '{instance.status.value}'",
        )
    return {"cancelled": True, "workflow_id": workflow_id}


# ═══════════════════════════════════════════════════════════════
#  CANONICAL PROCESSING (Phase 3 — unified entry point)
# ═══════════════════════════════════════════════════════════════

class CanonicalProcessingRequest(BaseModel):
    """Unified request to upload media and trigger canonical processing.

    This replaces the legacy 3-step flow:
      createQASession → uploadAudio/uploadVideo → runPipeline
    with a single call that handles everything.
    """
    tenant_id: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    audio_file_id: Optional[str] = None
    video_file_id: Optional[str] = None
    document_file_ids: list[str] = Field(default_factory=list)
    language: str = "en"
    num_speakers: Optional[int] = None
    consumer_chain_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional downstream chain to run after canonical processing "
            "completes (e.g. 'nexus.qa-testing'). Omit to run canonical only."
        ),
    )
    consumer_input_data: dict = Field(
        default_factory=dict,
        description="Additional input data forwarded to the consumer chain.",
    )


@app.post(
    "/api/v1/orchestrator/process",
    response_model=StartWorkflowResponse,
    tags=["Canonical Processing"],
)
async def start_canonical_processing(
    background_tasks: BackgroundTasks,
    audio: Optional[UploadFile] = File(default=None, description="Audio recording"),
    video: Optional[UploadFile] = File(default=None, description="Video/screen recording"),
    session_id: str = Form(default=""),
    language: str = Form(default="en"),
    num_speakers: Optional[int] = Form(default=None),
    processing_profile: Optional[str] = Form(default=None, description="Processing profile: 'fast' or 'deep'"),
    consumer_chain_id: Optional[str] = Form(default=None),
    user: NexusUser = Depends(get_current_user),
):
    """
    Upload media and trigger canonical processing in one call.

    Replaces the legacy 3-step flow (create session → upload → run pipeline).
    Files are stored via FileStore, then the canonical-processing chain is started.
    Optionally chains into a consumer workflow (e.g. nexus.qa-testing) when done.

    Phase 1.5: session_id is mandatory — every canonical artifact must be
    traceable to a session. The UI creates the session first, then calls this endpoint.
    """
    tenant_id = user.tenant_id
    if not session_id or not session_id.strip():
        raise HTTPException(
            status_code=400,
            detail="session_id is required. Create a session first via POST /v1/sessions.",
        )

    # D2 fix: deep profile requires GPU (LLaVA per-frame). On CPU
    # deployments it quarantines after step deadlines fire. Reject at
    # ingress with a clear message rather than letting the user wait
    # 13 min for a "Failed" UI state. Production GPU envs flip the
    # EYES_DEEP_PROFILE_ENABLED env flag to true.
    if (processing_profile or "").lower() == "deep" and not config.deep_profile_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                "deep profile requires a GPU-enabled deployment. "
                "Use 'fast' or 'multimodal' instead, or contact your "
                "admin to enable EYES_DEEP_PROFILE_ENABLED."
            ),
        )

    # ── Store uploaded files (500 MB limit per file) ─────────
    MAX_UPLOAD_BYTES = 500 * 1024 * 1024
    audio_file_id: Optional[str] = None
    video_file_id: Optional[str] = None

    if audio:
        content = await audio.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Audio file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            )
        if content:
            _validate_media_content(content, "audio", audio.filename or "audio")
            meta = await file_store.store(
                filename=audio.filename or "audio.wav",
                content=content,
                content_type=audio.content_type or "audio/wav",
                tenant_id=tenant_id,
            )
            audio_file_id = meta["file_id"]

    if video:
        content = await video.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Video file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            )
        if content:
            _validate_media_content(content, "video", video.filename or "video")
            meta = await file_store.store(
                filename=video.filename or "video.mp4",
                content=content,
                content_type=video.content_type or "video/mp4",
                tenant_id=tenant_id,
            )
            video_file_id = meta["file_id"]

    if not audio_file_id and not video_file_id:
        raise HTTPException(
            status_code=400,
            detail="At least one media file (audio or video) is required.",
        )

    # ── Fingerprint dedup ──────────────────────────────────
    # D1 fix: compute fingerprint over the SOURCE uploads only, BEFORE
    # auto-extracting audio. ffmpeg-extracted audio bytes vary between
    # runs (encoder metadata, muxer timestamps) so including them in
    # the fingerprint made dedup fail for byte-identical re-uploads of
    # the same video. Hashing the original uploads keeps the
    # fingerprint deterministic across uploads.
    h = hashlib.sha256()
    for fid in sorted(filter(None, [audio_file_id, video_file_id])):
        file_content = await file_store.read(fid)
        if file_content:
            h.update(file_content)
    fingerprint = h.hexdigest()

    # Auto-extract embedded audio from the video when no separate audio
    # file was provided.  Without this step, audio_transcription runs
    # against an empty audio_file_id and the SME's narration is silently
    # discarded — the most reliable intent signal for downstream action
    # extraction.  Failure is non-fatal: the chain still runs visually.
    # NOTE: must run AFTER fingerprint compute (see comment above).
    if video_file_id and not audio_file_id:
        extracted_audio_id = await _extract_audio_from_video(
            video_file_id, file_store, tenant_id,
        )
        if extracted_audio_id:
            audio_file_id = extracted_audio_id

    cached = await _check_fingerprint_cache(tenant_id, fingerprint, config.spine_url)
    if cached:
        # Both follow-up calls are best-effort, but their failures matter
        # for the user experience:
        #   - link_to_session: without this, the session has no artifact
        #     reference and the UI can't find the cached result.
        #   - update_session_status: without this, the session stays in
        #     'processing' state and the client polls forever.
        # Previously both were fire-and-forget. Now we capture the
        # outcome and surface it in the response so the client can
        # decide whether to fall through to a fresh workflow.
        link_ok = True
        status_ok = True
        try:
            link_ok = await _link_cached_artifact_to_session(
                tenant_id=tenant_id,
                session_id=session_id,
                artifact_id=cached["artifact_id"],
                spine_url=config.spine_url,
            )
        except Exception as exc:
            link_ok = False
            logger.warning(
                "Canonical cache-hit link_to_session failed: tenant=%s artifact=%s err=%s",
                tenant_id, cached.get("artifact_id"), exc,
            )
        try:
            status_ok = await _update_session_status(
                tenant_id, session_id, "completed",
            )
        except Exception as exc:
            status_ok = False
            logger.warning(
                "Canonical cache-hit status update failed: tenant=%s session=%s err=%s",
                tenant_id, session_id, exc,
            )

        if not link_ok or not status_ok:
            # The cache-hit short-circuit is no longer trustworthy —
            # the client would see a "completed" workflow with no
            # discoverable artifact. Fall through to the full chain
            # so the artifact gets re-linked on completion.
            logger.error(
                "Canonical cache-hit degraded: link_ok=%s status_ok=%s — "
                "falling through to fresh workflow for tenant=%s session=%s",
                link_ok, status_ok, tenant_id, session_id,
            )
        else:
            logger.info(
                "Canonical processing cache hit: tenant=%s fp=%s artifact=%s reason=%s",
                tenant_id, fingerprint[:16], cached.get("artifact_id"),
                cached.get("cache_reason"),
            )
            canonical_chain = await registry.get("nexus.canonical-processing")
            return StartWorkflowResponse(
                workflow_id=f"cached-{cached['artifact_id']}",
                chain_id="nexus.canonical-processing",
                chain_name=canonical_chain.name if canonical_chain else "Canonical Media Processing",
                status=WorkflowStatus.COMPLETED,
                session_id=session_id,
                # Expose the cached artifact id under `artifact_id` too so
                # the client's `result.artifact_id` lookup picks it up and
                # the UI can fetch /v1/artifacts/{id} to render the
                # canonical result. Without this, cache-hit responses
                # leave the tracked job with artifact_id=null and the
                # display falls back to "finalizing" indefinitely.
                artifact_id=cached.get("artifact_id"),
                cache_hit=True,
                cached_artifact_id=cached.get("artifact_id"),
                cached_artifact_status=cached.get("artifact_status"),
                cache_reason=cached.get("cache_reason"),
            )

    # ── Start canonical chain ──────────────────────────────
    # Pre-allocate canonical artifact_id immediately for client tracking.
    # This UUID is threaded through the workflow so Stage 6 (artifact_persistence)
    # uses it instead of generating a new one — ensuring the ID returned at
    # upload time matches the final persisted artifact.
    artifact_id = str(uuid.uuid4())
    logger.info(
        "Pre-allocated canonical artifact_id=%s for session=%s fingerprint=%s",
        artifact_id, session_id, fingerprint[:16],
    )

    # P4 Dedup: Acquire fingerprint processing lock to prevent concurrent
    # workflows for the same media. Uses Redis SET NX with TTL.
    fp_lock_key = f"canonical:processing_lock:{tenant_id}:{fingerprint}"
    fp_lock_acquired = False
    try:
        if workflow_store._redis:
            # SET NX with TTL — only succeeds if no active lock exists
            fp_lock_acquired = await workflow_store._redis.set(
                fp_lock_key, session_id, nx=True, ex=config.processing_lock_ttl_seconds,
            )
            if not fp_lock_acquired:
                # Another workflow is actively processing same fingerprint
                existing_session = await workflow_store._redis.get(fp_lock_key)
                # Look up the running workflow so the client can connect
                existing_workflow_id = None
                if existing_session:
                    try:
                        existing_workflows = await workflow_store.list_instances(
                            tenant_id=tenant_id, limit=5, session_id=existing_session,
                        )
                        for ew in existing_workflows:
                            if ew.status in (
                                WorkflowStatus.PENDING, WorkflowStatus.RUNNING,
                            ):
                                existing_workflow_id = ew.workflow_id
                                break
                    except Exception:
                        pass
                logger.warning(
                    "P4 dedup: fingerprint=%s already being processed (lock holder=%s, "
                    "workflow=%s), rejecting duplicate upload from session=%s",
                    fingerprint[:16], existing_session, existing_workflow_id, session_id,
                )
                await _update_session_status(tenant_id, session_id, "cancelled")
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "This media file is already being processed by another workflow. "
                            "Please wait for it to complete or use a different file."
                        ),
                        "existing_workflow_id": existing_workflow_id,
                        "existing_session_id": existing_session,
                    },
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("P4 dedup: fingerprint lock check failed (non-blocking): %s", exc)

    canonical_chain = await registry.get("nexus.canonical-processing")
    if not canonical_chain:
        raise HTTPException(
            status_code=500,
            detail="Canonical processing chain not registered. Contact admin.",
        )

    input_data = {
        "audio_file_id": audio_file_id,
        "video_file_id": video_file_id,
        "language": language,
        "processing_profile": processing_profile or config.default_processing_profile,
        "media_fingerprint": fingerprint,
        "artifact_id": artifact_id,  # Pre-allocated for immediate tracking
        # Provenance fields — flow through to canonical artifact persistence
        "source_type": (
            "audio_video_upload" if audio_file_id and video_file_id
            else "video_upload" if video_file_id
            else "audio_upload"
        ),
        "source_filename": (
            (audio.filename if audio and audio.filename else "")
            + (("+" + video.filename) if video and video.filename else "")
        ).strip("+") or "upload",
        "created_by": user.user_id,
    }
    # Resolve on-disk paths so downstream engines (Spine probe) can access
    # files directly via shared storage — avoids copying bytes over HTTP.
    if audio_file_id:
        audio_meta = file_store.get(audio_file_id)
        if audio_meta:
            input_data["audio_file_path"] = audio_meta["path"]
    if video_file_id:
        video_meta = file_store.get(video_file_id)
        if video_meta:
            input_data["video_file_path"] = video_meta["path"]
    if num_speakers is not None:
        input_data["num_speakers"] = num_speakers

    # ── Queue saturation guard (architect P2) ─────────────────
    # Runs BEFORE tenant admission so a saturated lane doesn't burn
    # through tenants' concurrency budgets only to be quarantined
    # later. Standard-tier uploads bounce; priority-tier uploads
    # bypass (they use dedicated lanes).
    if queue_saturation_guard is not None and queue_saturation_guard.enabled:
        is_priority = (
            input_data.get("processing_profile") == "priority"
            or getattr(user, "priority", False)
        )
        allowed, saturated, retry_after = (
            await queue_saturation_guard.check(is_priority=is_priority)
        )
        if not allowed:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": (
                        "Critical pipeline lanes are saturated. Try again "
                        f"in ~{retry_after}s; current depths above threshold: "
                        f"{saturated}"
                    ),
                    "saturated_lanes": saturated,
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

    # ── Per-tenant admission control (canonical path) ────────
    if not await admission.acquire(tenant_id):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tenant '{tenant_id}' has reached the maximum of "
                f"{admission._max} concurrent workflows. "
                "Wait for existing workflows to complete before starting new ones."
            ),
        )

    # Phase A migration — dispatch via the new Phase 12 workflow plane
    # when it's healthy. The new plane writes workflow_state rows that
    # the UI and metrics dashboards subscribe to; the legacy chain
    # engine writes workflow_instances rows that nothing the client
    # touches reads. Falling back to the legacy chain keeps the
    # service available if the plane fails to start, but the canonical
    # path is now plane-first.
    if workflow_plane is not None:
        has_audio = bool(audio_file_id)
        has_video = bool(video_file_id)
        plan = canonical_pipeline_plan(
            tenant_id=tenant_id,
            session_id=session_id,
            has_audio=has_audio,
            has_video=has_video,
            profile=input_data["processing_profile"],
            artifact_id=artifact_id,
            audio_file_path=input_data.get("audio_file_path", ""),
            video_file_path=input_data.get("video_file_path", ""),
            source_filename=input_data["source_filename"],
            source_type=input_data["source_type"],
            created_by=user.user_id,
            media_fingerprint=fingerprint,
            metadata={
                "audio_file_id": audio_file_id or "",
                "video_file_id": video_file_id or "",
                "language": language,
                "num_speakers": int(num_speakers) if num_speakers else 0,
            },
        )
        try:
            new_workflow_id = await workflow_plane.manager.create(plan)
            await workflow_plane.dispatcher.dispatch_next(new_workflow_id)
        except Exception:
            await admission.release(tenant_id)
            # Best-effort fingerprint lock release on dispatch failure.
            try:
                if workflow_store._redis:
                    await workflow_store._redis.delete(fp_lock_key)
            except Exception:
                pass
            raise

        # Background reaper: poll workflow_state for terminal status, then
        # release admission + fingerprint lock. The plane has its own
        # deadline sweeper that handles stuck workflows; this reaper is
        # purely for tear-down of orchestrator-side resources.
        background_tasks.add_task(
            _release_plane_workflow_resources,
            new_workflow_id, tenant_id, fingerprint, session_id,
        )

        return StartWorkflowResponse(
            workflow_id=new_workflow_id,
            chain_id="canonical_pipeline",
            chain_name="Canonical Pipeline (Phase 12 DAG)",
            status=WorkflowStatus.RUNNING,
            session_id=session_id,
            artifact_id=artifact_id,
        )

    # ── Legacy fallback (plane unavailable) ──────────────────────
    try:
        instance = await chain_engine.start(
            chain=canonical_chain,
            tenant_id=tenant_id,
            session_id=session_id,
            input_data=input_data,
            created_by=user.user_id,
        )
    except Exception:
        await admission.release(tenant_id)
        raise

    # Phase 2.1: Resolve consumer chains to run after canonical completes.
    # In canonical-only mode: no downstream consumers at all.
    if config.product_mode == "canonical":
        if consumer_chain_id:
            logger.warning(
                "Ignoring consumer_chain_id='%s' (product_mode=canonical)",
                consumer_chain_id,
            )
        consumer_chain_ids = []
    elif consumer_chain_id:
        consumer_chain_ids = [consumer_chain_id]
    else:
        consumer_chain_ids = list(DEFAULT_CONSUMER_CHAINS)

    async def _run_canonical_and_release(
        workflow_id, canonical_chain, consumer_chain_ids,
        consumer_input_data, tenant_id, session_id, user_id,
    ):
        """Run canonical + consumers, then release admission slot."""
        try:
            await _run_canonical_then_consumers(
                workflow_id=workflow_id,
                canonical_chain=canonical_chain,
                consumer_chain_ids=consumer_chain_ids,
                consumer_input_data=consumer_input_data,
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
            )
        finally:
            await admission.release(tenant_id)

    # Run canonical chain + consumer chains in background
    background_tasks.add_task(
        _run_canonical_and_release,
        workflow_id=instance.workflow_id,
        canonical_chain=canonical_chain,
        consumer_chain_ids=consumer_chain_ids,
        consumer_input_data={},
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user.user_id,
    )

    return StartWorkflowResponse(
        workflow_id=instance.workflow_id,
        chain_id=canonical_chain.chain_id,
        chain_name=canonical_chain.name,
        status=instance.status,
        session_id=session_id,
        artifact_id=artifact_id,
    )


async def _release_plane_workflow_resources(
    workflow_id: str, tenant_id: str, fingerprint: str,
    session_id: str = "",
) -> None:
    """Poll the new workflow_plane for terminal status, then release
    the admission slot + fingerprint lock and propagate the terminal
    state to the session row. Bounded by the plan's own workflow
    deadline (~45 min worst case) plus 5-min grace; after that the
    plane's sweeper has already marked the workflow cancelled, so
    resources need releasing regardless.

    D5 fix (2026-05-18): also PATCHes the session status when the
    workflow lands terminal. Without this, /process-launched workflows
    completed in the artifact table but the session row stayed at
    `scheduled` indefinitely — UI's "Recent Sessions" panel would
    show "Processing" even though the artifact was 100% complete.
    """
    terminal = {
        PlaneWorkflowStatus.COMPLETED.value,
        PlaneWorkflowStatus.FAILED.value,
        PlaneWorkflowStatus.CANCELLED.value,
        PlaneWorkflowStatus.QUARANTINED.value,
    }
    final_plane_status: Optional[str] = None
    poll_interval_s = 5.0
    max_wait_s = 3300.0  # 55 min — slightly past the multimodal deadline
    elapsed = 0.0
    while elapsed < max_wait_s:
        try:
            wf = await workflow_plane.manager.get(workflow_id)
            if wf is not None and wf.status in terminal:
                final_plane_status = wf.status
                logger.info(
                    "plane.workflow.terminal wf=%s status=%s — releasing resources",
                    workflow_id, wf.status,
                )
                break
        except Exception as exc:
            logger.debug(
                "plane.workflow.poll_failed wf=%s err=%s",
                workflow_id, exc,
            )
        await asyncio.sleep(poll_interval_s)
        elapsed += poll_interval_s
    else:
        logger.warning(
            "plane.workflow.poll_timeout wf=%s — releasing resources anyway",
            workflow_id,
        )

    try:
        await admission.release(tenant_id)
    except Exception as exc:
        logger.warning(
            "plane.workflow.admission_release_failed wf=%s err=%s",
            workflow_id, exc,
        )
    try:
        if workflow_store._redis and fingerprint:
            fp_lock_key = f"canonical:processing_lock:{tenant_id}:{fingerprint}"
            await workflow_store._redis.delete(fp_lock_key)
    except Exception as exc:
        logger.warning(
            "plane.workflow.fp_lock_release_failed wf=%s err=%s",
            workflow_id, exc,
        )

    # D5 fix: flip the session row to match the workflow's terminal
    # state. Quarantined surfaces as "failed" to the user — the
    # operational distinction (retry-eligible vs hard-failed) lives
    # in admin tools, not in the end-user session list.
    if session_id:
        _session_status_map = {
            PlaneWorkflowStatus.COMPLETED.value: "completed",
            PlaneWorkflowStatus.FAILED.value: "failed",
            PlaneWorkflowStatus.CANCELLED.value: "cancelled",
            PlaneWorkflowStatus.QUARANTINED.value: "failed",
        }
        target = _session_status_map.get(final_plane_status or "", "failed")
        try:
            await _update_session_status(tenant_id, session_id, target)
        except Exception as exc:
            logger.warning(
                "plane.workflow.session_status_update_failed wf=%s session=%s target=%s err=%s",
                workflow_id, session_id, target, exc,
            )


# Default consumer chains auto-triggered after canonical processing completes.
# Order matters: rule-extraction feeds test-generation, both feed knowledge-graph,
# then contradiction-detection, and finally report-generation summarises everything.
DEFAULT_CONSUMER_CHAINS: list[str] = [
    "nexus.rule-extraction",
    "nexus.test-generation",
    "nexus.knowledge-graph",
    "nexus.contradiction-detection",
    "nexus.report-generation",
]


async def _run_canonical_then_consumers(
    workflow_id: str,
    canonical_chain: ChainDefinition,
    consumer_chain_ids: list[str],
    consumer_input_data: dict,
    tenant_id: str,
    session_id: str,
    user_id: str,
):
    """Background: run canonical chain, then auto-trigger consumer chains.

    Phase 2.1 — after the canonical-processing chain completes successfully,
    each consumer chain in *consumer_chain_ids* is started sequentially.
    Every consumer receives the canonical artifact_id + workflow_id so it
    can fetch the artifact from Spine rather than re-processing media.

    If *consumer_chain_ids* is empty, nothing runs after canonical.
    If a consumer chain fails, subsequent chains still execute (fail-forward).
    """
    try:
        try:
            await chain_engine.execute(workflow_id, canonical_chain)
        finally:
            # P4: Release fingerprint processing lock on completion (success or failure)
            try:
                _inst = await workflow_store.get_instance(workflow_id)
                if _inst and _inst.input_data:
                    fp = _inst.input_data.get("media_fingerprint", "")
                    if fp and workflow_store._redis:
                        fp_lock_key = f"canonical:processing_lock:{tenant_id}:{fp}"
                        await workflow_store._redis.delete(fp_lock_key)
                        logger.info("P4 dedup: released fingerprint lock for %s", fp[:16])
            except Exception as exc:
                logger.debug("P4 dedup: lock release failed (non-blocking): %s", exc)

        if not consumer_chain_ids:
            return

        # Check if canonical succeeded
        instance = await workflow_store.get_instance(workflow_id)
        if not instance or instance.status != WorkflowStatus.COMPLETED:
            logger.warning(
                "Canonical chain did not complete (status=%s) — skipping %d consumer chain(s)",
                instance.status if instance else "missing",
                len(consumer_chain_ids),
            )
            return

        # Extract artifact_id from the persistence stage output
        artifact_stage = instance.stages.get("artifact_persistence")
        artifact_id = None
        if artifact_stage and artifact_stage.output:
            artifact_id = artifact_stage.output.get("artifact_id")

        # Extract quality gate result for downstream consumers
        qg_stage = instance.stages.get("canonical_quality_gate")
        quality_gate_output = {}
        if qg_stage and qg_stage.output:
            quality_gate_output = {
                "brain_quality_score": qg_stage.output.get("adjusted_score"),
                "quality_gate_passed": qg_stage.output.get("passed", False),
            }

        # Accumulate outputs from consumer chains so later chains can reference
        # earlier consumer results (e.g. test-generation uses rule-extraction output).
        # Keys are sanitised aliases: "nexus.rule-extraction" → "rule_extraction"
        # because WorkflowContext.resolve() splits on '.' — dotted chain IDs would break path resolution.
        consumer_outputs: dict[str, dict] = {}

        for chain_id in consumer_chain_ids:
            consumer_chain = await registry.get(chain_id)
            if not consumer_chain:
                logger.warning("Consumer chain '%s' not registered — skipping", chain_id)
                continue

            consumer_input = {
                **consumer_input_data,
                "canonical_artifact_id": artifact_id,
                "canonical_workflow_id": workflow_id,
                "session_id": session_id,
                **quality_gate_output,
                # Pass prior consumer outputs so chains can depend on each other
                "prior_consumer_outputs": consumer_outputs,
            }

            try:
                consumer_instance = await chain_engine.start(
                    chain=consumer_chain,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    input_data=consumer_input,
                    created_by=user_id,
                )
                await chain_engine.execute(consumer_instance.workflow_id, consumer_chain)

                # Collect outputs for downstream consumers
                completed = await workflow_store.get_instance(consumer_instance.workflow_id)
                if completed and completed.status == WorkflowStatus.COMPLETED:
                    # Sanitise chain_id for use as dict key in $-path resolution.
                    # "nexus.rule-extraction" → "rule_extraction"
                    alias = chain_id.rsplit(".", 1)[-1].replace("-", "_")
                    consumer_outputs[alias] = {
                        s_id: s.output
                        for s_id, s in completed.stages.items()
                        if s.output and s.status == StageStatus.COMPLETED
                    }
                    logger.info(
                        "Consumer chain '%s' completed for canonical workflow %s",
                        chain_id, workflow_id,
                    )
                else:
                    logger.warning(
                        "Consumer chain '%s' did not complete (status=%s) for workflow %s",
                        chain_id,
                        completed.status if completed else "missing",
                        workflow_id,
                    )
            except Exception:
                logger.exception(
                    "Consumer chain '%s' failed for canonical workflow %s — continuing",
                    chain_id, workflow_id,
                )

    except Exception:
        logger.exception("_run_canonical_then_consumers failed for workflow %s", workflow_id)


# ── SSE Workflow Progress Stream ───────────────────────────────

@app.get(
    "/api/v1/orchestrator/workflows/{workflow_id}/stream",
    tags=["Workflows"],
)
async def stream_workflow_progress(
    workflow_id: str,
    token: str = Query(..., description="JWT for SSE auth (EventSource cannot set headers)"),
):
    """Server-Sent Events stream for real-time workflow progress.

    The client opens an EventSource connection and receives:
      - ``progress`` events every 2 seconds with stage-level detail
      - ``complete`` event when the workflow finishes (success or failure)
      - ``error`` event if the workflow is not found

    Authentication is via ``?token=`` query parameter because the browser
    EventSource API does not support custom headers.
    """
    # Authenticate via query-param token (EventSource can't set Authorization header)
    auth = get_auth_service()
    try:
        user = auth.validate_token(token)
    except Exception:
        async def _unauth():
            yield f"event: error\ndata: {json.dumps({'error': 'Invalid or expired token'})}\n\n"
        return StreamingResponse(_unauth(), media_type="text/event-stream")

    # Verify the workflow exists and belongs to this tenant
    instance = await workflow_store.get_instance(workflow_id)
    if not instance:
        async def _not_found():
            yield f"event: error\ndata: {json.dumps({'error': 'Workflow not found'})}\n\n"
        return StreamingResponse(_not_found(), media_type="text/event-stream")

    if instance.tenant_id != user.tenant_id:
        async def _forbidden():
            yield f"event: error\ndata: {json.dumps({'error': 'Not authorised'})}\n\n"
        return StreamingResponse(_forbidden(), media_type="text/event-stream")

    async def _event_stream():
        while True:
            inst = await workflow_store.get_instance(workflow_id)
            if not inst:
                yield f"event: error\ndata: {json.dumps({'error': 'Workflow disappeared'})}\n\n"
                break

            stages_total = len(inst.stages)
            stages_completed = sum(
                1 for s in inst.stages.values()
                if s.status.value in ("completed", "skipped")
            )

            # Compute progress percent from stages
            progress = round(
                (stages_completed / max(stages_total, 1)) * 100, 1
            )

            # Find current running stage
            current_stage = None
            for sid, s in inst.stages.items():
                if s.status.value == "running":
                    current_stage = sid
                    break

            payload = {
                "workflow_id": workflow_id,
                "status": inst.status.value,
                "current_stage": current_stage,
                "progress_percent": progress,
                "stages": [
                    {
                        "stage_id": sid,
                        "status": s.status.value,
                        "duration_ms": s.duration_ms,
                    }
                    for sid, s in inst.stages.items()
                ],
            }

            if inst.error:
                payload["error"] = inst.error

            yield f"event: progress\ndata: {json.dumps(payload)}\n\n"

            if inst.status in (
                WorkflowStatus.COMPLETED,
                WorkflowStatus.FAILED,
                WorkflowStatus.DEGRADED,
                WorkflowStatus.NEEDS_REVIEW,
            ):
                yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
                break

            await asyncio.sleep(2)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
#  ADMISSION CONTROL MONITORING
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/orchestrator/admission/status", tags=["Monitoring"])
async def admission_status(
    user: NexusUser = Depends(get_current_user),
):
    """Return per-tenant in-flight workflow counts."""
    return {
        "enabled": admission.enabled,
        "max_per_tenant": admission._max,
        "tenants": admission.snapshot(),
    }


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════

@app.get("/api/v1/orchestrator/dashboard/summary", tags=["Dashboard"])
async def dashboard_summary(
    user: NexusUser = Depends(get_current_user),
):
    """Get a dashboard summary for the current tenant."""
    workflows = await workflow_store.list_instances(
        tenant_id=user.tenant_id, limit=1000
    )
    chains = await registry.list_chains(tenant_id=user.tenant_id)

    completed = [w for w in workflows if w.status == WorkflowStatus.COMPLETED]
    failed = [w for w in workflows if w.status == WorkflowStatus.FAILED]
    running = [w for w in workflows if w.status == WorkflowStatus.RUNNING]

    # Compute per-chain statistics
    chain_stats: dict[str, dict] = {}
    for w in workflows:
        stats = chain_stats.setdefault(
            w.chain_id,
            {"chain_id": w.chain_id, "chain_name": w.chain_name, "runs": 0, "completed": 0, "failed": 0},
        )
        stats["runs"] += 1
        if w.status == WorkflowStatus.COMPLETED:
            stats["completed"] += 1
        elif w.status == WorkflowStatus.FAILED:
            stats["failed"] += 1

    return {
        "tenant_id": user.tenant_id,
        "total_chains": len(chains),
        "total_workflows": len(workflows),
        "active_workflows": len(running),
        "completed_workflows": len(completed),
        "failed_workflows": len(failed),
        "success_rate": (
            len(completed) / max(len(completed) + len(failed), 1) * 100
        ),
        "chain_stats": list(chain_stats.values()),
        "recent_workflows": [
            WorkflowSummary(
                workflow_id=w.workflow_id,
                chain_id=w.chain_id,
                chain_name=w.chain_name,
                tenant_id=w.tenant_id,
                status=w.status,
                created_at=w.created_at,
                stages_completed=sum(
                    1
                    for s in w.stages.values()
                    if s.status.value in ("completed", "skipped")
                ),
                stages_total=len(w.stages),
                error=w.error,
            ).model_dump()
            for w in workflows[:10]
        ],
    }


# ═══════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "healthy",
        "service": "nexus-orchestrator",
        "version": "0.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health/ready", tags=["Health"])
async def health_ready():
    """Readiness check — verifies Redis, workflow store, and engine connectivity."""
    checks = {}
    overall = "healthy"

    # 1. Redis / workflow store
    try:
        if workflow_store._redis:
            await workflow_store._redis.ping()
            checks["redis"] = {"status": "healthy"}
        else:
            checks["redis"] = {"status": "unhealthy", "message": "no connection"}
            overall = "unhealthy"
    except Exception as e:
        checks["redis"] = {"status": "unhealthy", "message": str(e)}
        overall = "unhealthy"

    # 2. Chain registry
    try:
        chain_count = len(await registry.list_chains(tenant_id=""))
        checks["chain_registry"] = {"status": "healthy", "chains": chain_count}
    except Exception as e:
        checks["chain_registry"] = {"status": "unhealthy", "message": str(e)}
        overall = "unhealthy"

    # 3. Engine connectivity (spot-check a critical subset)
    critical_engines = {"spine": config.spine_url, "brain": config.brain_url}
    for eng_name, eng_url in critical_engines.items():
        try:
            resp = await http_client.get(f"{eng_url}/health", timeout=5.0)
            data = resp.json()
            eng_status = data.get("status", "unknown")
            checks[eng_name] = {"status": eng_status}
            if eng_status not in ("healthy", "degraded"):
                overall = "degraded"
        except Exception as e:
            checks[eng_name] = {"status": "unreachable", "message": str(e)}
            overall = "unhealthy"

    return {
        "status": overall,
        "service": "nexus-orchestrator",
        "version": "0.2.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencies": checks,
    }


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=False,
    )
