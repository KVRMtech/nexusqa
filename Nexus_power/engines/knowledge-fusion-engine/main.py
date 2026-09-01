"""Knowledge Fusion Engine — service entrypoint.

Wires the components in ``app/`` together into a FastAPI service:

    EventBus ─ subscribe ──► substrate enqueue ─► indexing_jobs queue
                                                    │
                                                    ▼
                                       SubstrateWorker leases
                                                    │
                                                    ▼
                                       Indexer.index_artifact()
                                                    │
                                                    ▼
                                       transcript_segments + Backbone
                                                    │
                                                    ▼
                                       EventBus.publish substrate.*

Exposes:
    GET  /health           — liveness + readiness summary
    GET  /stats            — queue depth by status
    POST /api/v1/fusion/enqueue  — admin-only manual enqueue
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import structlog
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

# ─── Path setup for SDK imports ────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk"))
for p in (_THIS_DIR, _SDK_PATH):
    if p not in sys.path:
        sys.path.insert(0, p)

from nexus_sdk.auth import (  # noqa: E402
    NexusUser,
    get_current_user,
    init_auth,
)
from nexus_sdk.events import EventBus  # noqa: E402

from app.atlas import (  # noqa: E402
    AtlasBuilder,
    AtlasRepository,
    CrossModalAligner,
    HeuristicLayerClassifier,
)
from app.backbone_client import BackboneClient  # noqa: E402
from app.canonical_reader import CanonicalReader  # noqa: E402
from app.cards import (  # noqa: E402
    CardRepository,
    CardSynthesizer,
)
from app.chunker import ChunkerConfig, TranscriptChunker  # noqa: E402
from app.config import FusionConfig, load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.events import SubstrateEvents  # noqa: E402
from app.indexer import Indexer  # noqa: E402
from app.jobs import JobStore  # noqa: E402
from app.worker import SubstrateWorker  # noqa: E402

logger = structlog.get_logger()


# ─── Module-level singletons (wired in lifespan) ───────────────


class _Services:
    config: Optional[FusionConfig] = None
    db: Optional[Database] = None
    bus: Optional[EventBus] = None
    backbone: Optional[BackboneClient] = None
    store: Optional[JobStore] = None
    worker: Optional[SubstrateWorker] = None
    events: Optional[SubstrateEvents] = None
    consumer_task: Optional[asyncio.Task[None]] = None


_services = _Services()


# ─── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(application: FastAPI):
    config = load_config()
    _services.config = config

    # Auth — used by the admin enqueue endpoint and BackboneClient
    init_auth(jwt_secret=config.jwt_secret)

    # Database
    db = Database(config.postgres_url)
    await db.connect()
    _services.db = db

    # Event bus
    bus = EventBus(
        redis_host=config.redis_host,
        redis_port=config.redis_port,
        redis_password=config.redis_password or "",
        consumer_group="knowledge-fusion",
        consumer_name=f"fusion-{uuid.uuid4().hex[:8]}",
    )
    await bus.connect()
    _services.bus = bus

    # Backbone client
    backbone = BackboneClient(
        base_url=config.backbone_url,
        jwt_secret=config.jwt_secret,
        jwt_algorithm=config.jwt_algorithm,
        service_user_id=config.service_account_user_id,
        service_role=config.service_account_role,
        token_ttl_seconds=config.service_account_token_ttl_seconds,
        timeout_seconds=config.http_timeout_seconds,
    )
    _services.backbone = backbone

    # Indexing pipeline
    reader = CanonicalReader(db)
    chunker = TranscriptChunker(
        ChunkerConfig(
            target_chars=config.chunk_target_chars,
            min_chars=config.chunk_min_chars,
            overlap_chars=config.chunk_overlap_chars,
            max_chunks=config.max_segments_per_artifact,
        )
    )

    # Phase 3 — card synthesizer. Folds newly indexed segments into the
    # knowledge_cards graph after each artifact is embedded. Synthesizer
    # is owned here so a single instance amortises the role-weight cache.
    card_repo = CardRepository(db)
    synthesizer = CardSynthesizer(repo=card_repo, backbone=backbone)

    # Phase 5 — atlas builder. Projects newly indexed segments into the
    # product atlas, runs cross-modal alignment, and refreshes layer
    # stats per touched product.
    atlas_repo = AtlasRepository(db)
    atlas_builder = AtlasBuilder(
        repo=atlas_repo,
        backbone=backbone,
        classifier=HeuristicLayerClassifier(),
        aligner=CrossModalAligner(backbone),
    )

    indexer = Indexer(
        db=db,
        reader=reader,
        backbone=backbone,
        chunker=chunker,
        synthesizer=synthesizer,
        atlas_builder=atlas_builder,
    )

    # Queue + worker
    worker_id = config.worker_id or f"fusion-{uuid.uuid4().hex[:12]}"
    store = JobStore(
        db,
        worker_id=worker_id,
        lease_seconds=config.worker_lease_seconds,
        backoff_base_seconds=config.worker_backoff_base_seconds,
        backoff_max_seconds=config.worker_backoff_max_seconds,
    )
    _services.store = store

    events = SubstrateEvents(bus, enqueue=_make_enqueue_callable(store))
    _services.events = events

    worker = SubstrateWorker(
        store=store,
        indexer=indexer,
        event_publisher=events,
        concurrency=config.worker_concurrency,
        poll_interval_seconds=config.worker_poll_interval_seconds,
        worker_id=worker_id,
    )
    _services.worker = worker

    # Subscribe and start the consumer in the background.
    await events.subscribe()
    _services.consumer_task = asyncio.create_task(
        bus.start_consuming(), name="fusion-event-consumer"
    )
    await worker.start()

    logger.info(
        "fusion.started",
        port=config.port,
        worker_id=worker_id,
        concurrency=config.worker_concurrency,
        backbone=config.backbone_url,
    )

    try:
        yield
    finally:
        # Shutdown order: stop accepting (bus stop) → drain worker
        # → close clients → disconnect DB/bus.
        if _services.consumer_task is not None:
            await bus.stop_consuming()
            _services.consumer_task.cancel()
            try:
                await _services.consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if _services.worker is not None:
            await _services.worker.stop()
        if _services.backbone is not None:
            await _services.backbone.aclose()
        if _services.bus is not None:
            await _services.bus.disconnect()
        if _services.db is not None:
            await _services.db.disconnect()
        logger.info("fusion.stopped")


def _make_enqueue_callable(store: JobStore):
    async def _enqueue(
        *,
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        trace_id: str,
    ) -> None:
        await store.enqueue(
            tenant_id=tenant_id,
            session_id=session_id,
            artifact_id=artifact_id,
            trace_id=trace_id or None,
        )

    return _enqueue


# ─── Application ──────────────────────────────────────────────


app = FastAPI(
    title="Nexus Knowledge Fusion Engine",
    description="Phase 1 substrate builder — transcript chunking, indexing, "
                "Backbone node creation",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Health ───────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    """Liveness + readiness in a single payload."""
    db_status = (
        await _services.db.health() if _services.db is not None else "uninitialised"
    )
    bus_ready = _services.bus is not None and _services.bus._client is not None  # noqa: SLF001
    worker_running = (
        _services.worker is not None and _services.worker.is_running
    )
    backbone_status = (
        await _services.backbone.health()
        if _services.backbone is not None
        else "uninitialised"
    )
    snapshot = (
        _services.worker.snapshot() if _services.worker is not None else {}
    )
    healthy = (
        db_status == "healthy"
        and bus_ready
        and worker_running
        and backbone_status == "healthy"
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "service": "knowledge-fusion",
        "version": (
            _services.config.engine_version if _services.config else "unknown"
        ),
        "database": db_status,
        "event_bus": "connected" if bus_ready else "disconnected",
        "backbone": backbone_status,
        "worker": "running" if worker_running else "stopped",
        "worker_snapshot": snapshot,
    }


@app.get("/stats")
async def stats() -> dict:
    if _services.store is None:
        raise HTTPException(503, "store_not_initialised")
    return await _services.store.stats()


# ─── Admin enqueue ────────────────────────────────────────────


class EnqueueRequest(BaseModel):
    tenant_id: Optional[str] = Field(
        default=None,
        description="If omitted, the caller's tenant_id is used.",
    )
    session_id: str = Field(min_length=1, max_length=64)
    artifact_id: str = Field(min_length=1, max_length=64)
    trace_id: Optional[str] = Field(default=None, max_length=64)


@app.post("/api/v1/fusion/enqueue")
async def enqueue_artifact(
    body: EnqueueRequest,
    user: NexusUser = Depends(get_current_user),
) -> dict:
    """Manually enqueue an artifact for indexing. Admin only."""
    if user.role not in ("admin", "api"):
        raise HTTPException(403, "admin_or_api_required")
    if _services.store is None:
        raise HTTPException(503, "store_not_initialised")

    tenant_id = body.tenant_id or user.tenant_id
    if user.role == "admin" and tenant_id != user.tenant_id:
        # Cross-tenant enqueues require an api-role service account.
        raise HTTPException(403, "cross_tenant_requires_api_role")

    job = await _services.store.enqueue(
        tenant_id=tenant_id,
        session_id=body.session_id,
        artifact_id=body.artifact_id,
        trace_id=body.trace_id,
    )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "attempts": job.attempts,
    }


# ─── Entry point ──────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    uvicorn.run(
        "main:app", host=cfg.host, port=cfg.port, log_level="info"
    )
