"""
Nexus Ears Engine — Speech-to-Text & Speaker Diarization.

Listens to KT session audio, converts to text,
identifies who said what, and emits redacted transcripts.

Pipeline:
1. Audio upload (WAV/MP3/WebM) or real-time chunked stream
2. Preprocessing (FFmpeg normalize + 16kHz mono + optional VAD)
3. Speaker diarization (Pyannote 3.1) — persistent speaker IDs per tenant
4. Speech-to-text (Whisper v3 Large) with insurance vocabulary boosting
5. Segment alignment (speaker + timestamps + text)
6. Emit to Shield for PII redaction before anything touches Backbone

On-prem: All models run locally. No audio leaves the datacenter.
"""

from __future__ import annotations

import os
import uuid
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Header
from fastapi.responses import JSONResponse
from pydantic import Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse, JobResponse, JobStatus
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent
from nexus_sdk.worker import GPUWorkerMixin, PriorityGPUSemaphore, GPU_PRIORITY_FAST, GPU_PRIORITY_DEEP, GPU_PRIORITY_NORMAL
from nexus_sdk.storage import ArtifactStore, StorageConfig, create_storage
from nexus_sdk.media.models import (
    TranscriptionSegment,
    TranscriptionResult,
    AudioMetadata,
    AudioProcessingJob,
    MediaJobStatus,
)
from nexus_sdk.media.audio import AudioPreprocessor, PreprocessConfig

from app.transcription import WhisperTranscriber, INSURANCE_VOCABULARY
from app.diarization import SpeakerDiarizer, align_segments

# WhisperTranscriber now implements nexus_sdk.media.speech.SpeechProvider,
# making it a pluggable provider on the canonical hot path.
# To swap backends, replace WhisperTranscriber with any SpeechProvider impl.
from nexus_sdk.media.speech import SpeechProvider  # noqa: F401 — type reference

import structlog
logger = structlog.get_logger()


# ─── Configuration ─────────────────────────────────────────────

class EarsConfig(EngineConfig):
    engine_name: str = "ears"
    engine_port: int = 8002
    gpu_concurrency: int = Field(
        default=1,
        validation_alias="EARS_GPU_CONCURRENCY",
        ge=1,
    )

    default_processing_profile: str = Field(
        default="fast",
        validation_alias="EARS_DEFAULT_PROCESSING_PROFILE",
    )
    diarizer_startup_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="EARS_DIARIZER_STARTUP_TIMEOUT_SECONDS",
    )
    transcriber_startup_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="EARS_TRANSCRIBER_STARTUP_TIMEOUT_SECONDS",
    )
    allow_remote_model_bootstrap: bool = Field(
        default=False,
        validation_alias="EARS_ALLOW_REMOTE_MODEL_BOOTSTRAP",
    )
    require_real_diarizer: bool = Field(
        default=False,
        validation_alias="EARS_REQUIRE_REAL_DIARIZER",
    )
    verify_diarizer_manifest: bool = Field(
        default=False,
        validation_alias="PYANNOTE_REQUIRE_MANIFEST",
    )

    # Model paths (on-prem, local GPU)
    whisper_model_size: str = "large-v3"
    whisper_model_path: str = "./models/whisper-large-v3"
    whisper_device: str = "cuda"    # "cuda" or "cpu"
    whisper_compute_type: str = "float16"  # "float16" for GPU, "int8" for CPU
    whisper_fast_model_size: str = "medium"
    whisper_fast_model_path: str = "./models/whisper-medium"
    whisper_fast_compute_type: str = "int8"

    # Speaker diarization
    pyannote_model_path: str = "./models/pyannote-speaker-3.1"
    hf_token: str = ""  # HuggingFace token for gated Pyannote model
    min_speakers: int = 1
    max_speakers: int = 10
    diarization_inference_timeout_seconds: float = Field(
        default=120.0,
        validation_alias="EARS_DIARIZATION_INFERENCE_TIMEOUT_SECONDS",
    )
    skip_diarization_in_fast_mode: bool = Field(
        default=True,
        validation_alias="EARS_SKIP_DIARIZATION_FAST",
    )

    # Audio preprocessing
    normalize_audio: bool = True
    target_loudness_lufs: float = -23.0
    apply_vad: bool = False  # Whisper has built-in VAD

    # Working directory for Whisper / ffmpeg / Pyannote (ephemeral; emptyDir
    # in K8s). The durable copy lives in the StorageBackend below.
    audio_storage_path: str = "./data/audio"
    # When true (cloud backends), remove the local audio copy after the job
    # finishes so the pod is stateless.
    audio_delete_after_upload: bool = Field(
        default=True,
        validation_alias="EARS_AUDIO_DELETE_AFTER_UPLOAD",
    )

    # Chunking for long recordings
    max_segment_seconds: int = 30
    max_audio_duration_seconds: int = 3600  # 1 hour
    chunk_duration_seconds: int = 600       # 10 min chunks


# ─── The Ears Engine ───────────────────────────────────────────

class EarsEngine(NexusEngine, GPUWorkerMixin):
    def __init__(self):
        self.cfg = EarsConfig()
        super().__init__(
            name="ears",
            version="0.2.0",
            config=self.cfg,
            description="Speech-to-Text & Speaker Diarization Engine",
        )
        # Modular components
        self.preprocessor = AudioPreprocessor(PreprocessConfig(
            normalize_audio=self.cfg.normalize_audio,
            target_loudness_lufs=self.cfg.target_loudness_lufs,
            apply_vad=self.cfg.apply_vad,
            max_duration_seconds=self.cfg.max_audio_duration_seconds,
            chunk_duration_seconds=self.cfg.chunk_duration_seconds,
        ))
        self.transcriber = WhisperTranscriber(
            model_size=self.cfg.whisper_model_size,
            device=self.cfg.whisper_device,
            compute_type=self.cfg.whisper_compute_type,
            model_path=self.cfg.whisper_model_path,
            fast_model_size=self.cfg.whisper_fast_model_size,
            fast_compute_type=self.cfg.whisper_fast_compute_type,
            fast_model_path=self.cfg.whisper_fast_model_path,
            default_processing_profile=self.cfg.default_processing_profile,
            load_timeout_seconds=self.cfg.transcriber_startup_timeout_seconds,
            allow_remote_model_bootstrap=self.cfg.allow_remote_model_bootstrap,
        )
        self.diarizer = SpeakerDiarizer(
            model_path=self.cfg.pyannote_model_path,
            hf_token=self.cfg.hf_token,
            device=self.cfg.whisper_device,
            min_speakers=self.cfg.min_speakers,
            max_speakers=self.cfg.max_speakers,
            load_timeout_seconds=self.cfg.diarizer_startup_timeout_seconds,
            verify_manifest=self.cfg.verify_diarizer_manifest,
        )

        # GPU concurrency guard — priority-aware and configurable.
        self._gpu_semaphore = self._create_gpu_semaphore()

        # Object storage (canonical) + ephemeral working dir (above).
        self._storage_config = StorageConfig()
        self._artifacts = ArtifactStore(
            create_storage(self._storage_config), self._storage_config
        )

    def _create_gpu_semaphore(self) -> PriorityGPUSemaphore:
        raw_concurrency = os.environ.get(
            "GPU_CONCURRENCY",
            str(self.cfg.gpu_concurrency),
        )
        try:
            concurrency = max(1, int(raw_concurrency))
        except (TypeError, ValueError):
            concurrency = 1
        return PriorityGPUSemaphore(concurrency=concurrency)

    async def on_startup(self):
        """Load ML models into memory and configure from plugins."""
        self._gpu_semaphore = self._create_gpu_semaphore()

        # ── Register engine-specific Prometheus metrics ──
        from nexus_sdk.observability.metrics import get_metrics
        m = get_metrics()
        if m:
            self._m_transcriptions = m.custom_counter(
                "ears_transcriptions_total",
                "Total transcription jobs completed",
                labels=["profile", "status"],
            )
            self._m_whisper_seconds = m.custom_histogram(
                "ears_whisper_inference_seconds",
                "Whisper inference duration",
                labels=["profile"],
                buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
            )
            self._m_diarization_seconds = m.custom_histogram(
                "ears_diarization_seconds",
                "Speaker diarization duration",
                labels=[],
                buckets=(0.5, 1, 2, 5, 10, 30, 60),
            )
            self._m_gpu_queue_depth = m.custom_gauge(
                "ears_gpu_queue_waiting",
                "Number of tasks waiting for GPU",
            )
        else:
            self._m_transcriptions = None
            self._m_whisper_seconds = None
            self._m_diarization_seconds = None
            self._m_gpu_queue_depth = None

        # Wire event bus into components
        self.transcriber._event_bus = self.event_bus
        self.diarizer._event_bus = self.event_bus

        # Create working directory; durable artifacts go to object storage.
        os.makedirs(self.cfg.audio_storage_path, exist_ok=True)
        logger.info(
            "ears.storage_backend",
            backend=self._artifacts.backend_name,
            working_dir=self.cfg.audio_storage_path,
            delete_after_upload=self.cfg.audio_delete_after_upload,
        )

        # Load domain vocabulary from plugins
        try:
            vocab_ext = self.plugin_registry.get_merged_vocabulary()
            if vocab_ext and vocab_ext.terms:
                plugin_terms = [t.term for t in vocab_ext.terms]
                boost_words = vocab_ext.boost_words or []
                merged = list(dict.fromkeys(
                    plugin_terms + boost_words + INSURANCE_VOCABULARY
                ))
                self.transcriber.set_vocabulary(merged)
                logger.info(
                    "ears.vocabulary_loaded",
                    plugin_terms=len(plugin_terms),
                    boost_words=len(boost_words),
                    total=len(merged),
                )
        except Exception:
            logger.debug(
                "ears.vocabulary_plugin_unavailable",
                exc_info=True,
            )

        # Check FFmpeg availability
        ffmpeg_ok = await self.preprocessor.check_ffmpeg()

        # Load ML models
        transcriber_loaded = await self.transcriber.load_model(self.cfg.default_processing_profile)
        if not transcriber_loaded:
            logger.warning(
                "ears.transcriber_degraded_startup",
                processing_profile=self.cfg.default_processing_profile,
                timeout_seconds=self.cfg.transcriber_startup_timeout_seconds,
                allow_remote_model_bootstrap=self.cfg.allow_remote_model_bootstrap,
            )
        diarizer_loaded = await self.diarizer.load_model()
        if not diarizer_loaded:
            logger.warning(
                "ears.diarizer_degraded_startup",
                reason=self.diarizer.startup_reason,
                timeout_seconds=self.cfg.diarizer_startup_timeout_seconds,
            )
            if self.cfg.require_real_diarizer:
                raise RuntimeError(
                    f"Required diarizer failed startup: {self.diarizer.startup_reason}"
                )

        # Report component modes to health endpoint
        self.health.set_mode("preprocessor", "ffmpeg" if ffmpeg_ok else "passthrough")
        self.health.set_mode(
            "transcriber",
            self.transcriber.describe_mode()
            if self.transcriber.is_real
            else "stub",
        )
        self.health.set_mode(
            "diarizer", self.diarizer.mode
        )

        # ── Orphaned job recovery ──────────────────────────────
        # Jobs stuck in processing/queued from a previous container lifecycle
        # have no in-memory worker — mark them failed so the orchestrator can
        # retry via its idempotency/dispatch-dedup logic.
        try:
            orphaned = 0
            all_jobs = await self.job_store.list_jobs(limit=500)
            for job in all_jobs:
                if job.get("status") in ("processing", "queued"):
                    await self.job_store.update_job(
                        job["job_id"],
                        status="failed",
                        error="Engine restarted during processing — job orphaned. Orchestrator will retry.",
                        current_stage="failed",
                    )
                    orphaned += 1
            if orphaned:
                logger.warning("ears.orphaned_jobs_recovered", count=orphaned)
        except Exception:
            logger.debug("ears.orphaned_job_scan_failed", exc_info=True)

        # ── GPU Job Queue (Redis Streams) ──────────────────────
        # Enables horizontal scaling: API pods enqueue, worker pods process
        await self.init_worker_queue()
        if self.is_worker_mode and self._job_queue and self._job_queue.is_connected:
            self.start_worker_loop(
                    self._process_queued_job,
            )
            logger.info(
                "ears.worker_loop_started",
                mode=self.engine_mode,
                consumer=self._job_queue._config.consumer_name,
            )

        # Register queue management endpoints
        if self._job_queue and self._job_queue.is_connected:
            self.register_queue_routes(self.app)

        # ── Canonical workflow workers (Phase 1) ───────────────
        # One long-running loop per resource lane. Ears steps are all
        # GPU-bound today (Whisper + Pyannote) but we start a CPU
        # consumer too so the engine can handle any future CPU-only
        # step the canonical plan adds without a redeploy. The
        # orchestrator URL gates whether these start — when unset (dev
        # without an orchestrator) the legacy /api/v1/ears path remains
        # the only ingress.
        self._workflow_workers: list = []
        orchestrator_url = os.environ.get("NEXUS_ORCHESTRATOR_URL", "")
        if orchestrator_url:
            await self._start_canonical_workflow_workers(orchestrator_url)
        else:
            logger.info(
                "ears.workflow_workers_disabled "
                "reason=NEXUS_ORCHESTRATOR_URL_unset",
            )

    async def _start_canonical_workflow_workers(self, orchestrator_url: str) -> None:
        from nexus_sdk.workflows import (
            StepKind, WorkerConfig, WorkflowWorker, queue_name,
        )
        from app.workflow_handlers import EarsWorkflowHandlers

        token = os.environ.get("NEXUS_WORKER_TOKEN", "")
        handlers = EarsWorkflowHandlers(self)

        cpu_conc = int(os.environ.get("EARS_WORKER_CONCURRENCY_CPU", "4"))
        gpu_conc = int(os.environ.get("EARS_WORKER_CONCURRENCY_GPU", "1"))
        for kind in (StepKind.CPU, StepKind.GPU):
            lane = queue_name("ears", kind)
            q = self._build_workflow_lane_queue(lane)
            ok = await q.connect()
            if not ok:
                logger.error("ears.workflow_worker_redis_unreachable lane=%s", lane)
                continue
            worker = WorkflowWorker(
                config=WorkerConfig(
                    engine_name="ears",
                    kind=kind,
                    orchestrator_url=orchestrator_url,
                    auth_token=token,
                    concurrency=cpu_conc if kind == StepKind.CPU else gpu_conc,
                ),
                queue=q,
            )
            handlers.register(worker)
            self._workflow_workers.append(worker)
            asyncio.create_task(
                worker.run(), name=f"workflow_worker.{lane}",
            )
            logger.info(
                "ears.workflow_worker_started lane=%s orchestrator=%s",
                lane, orchestrator_url,
            )

    def _build_workflow_lane_queue(self, lane: str):
        from nexus_sdk.queue import JobQueue

        return JobQueue(
            engine_name=lane,
            redis_host=os.environ.get("REDIS_HOST", "redis"),
            redis_port=int(os.environ.get("REDIS_PORT", "6379")),
            redis_password=os.environ.get("REDIS_PASSWORD", ""),
            redis_db=int(os.environ.get("REDIS_DB", "3")),
        )

    async def _process_queued_job(self, job: dict):
        """Process a job claimed from the Redis Streams queue."""
        payload = job.get("payload", {})
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)

        await self._run_transcription(
            job_id=payload["job_id"],
            audio_path=payload["audio_path"],
            session_id=payload["session_id"],
            tenant_id=payload["tenant_id"],
            language=payload.get("language", "en"),
            num_speakers=payload.get("num_speakers"),
            processing_profile=payload.get("processing_profile", "fast"),
            original_filename=payload.get("original_filename", ""),
        )

    def register_routes(self, app):

        # ── Upload & Transcribe (async job) ────────────────────

        @app.post("/api/v1/ears/transcribe", response_model=JobResponse)
        async def transcribe(
            background_tasks: BackgroundTasks,
            audio: Optional[UploadFile] = File(default=None, description="Audio file (WAV/MP3/WebM)"),
            video: Optional[UploadFile] = File(default=None, description="Video file — audio will be extracted"),
            tenant_id: str = Form(...),
            session_id: str = Form(default_factory=lambda: str(uuid.uuid4())),
            language: str = Form(default="en"),
            num_speakers: Optional[int] = Form(default=None),
            processing_profile: str = Form(default="fast"),
            user: NexusUser = Depends(get_current_user),
            x_idempotency_key: Optional[str] = Header(default=None),
        ):
            """
            Upload audio (or video) and start transcription job.

            If only a video file is provided, the audio track is extracted
            automatically during preprocessing (requires FFmpeg).
            Returns a job_id to poll for results.
            Pipeline: save → preprocess → diarize → transcribe → align → emit.

            Phase 1.6: If X-Idempotency-Key header is provided and a job
            already exists for that key, returns the existing job immediately.
            """
            # Phase 1.6: Idempotency dedup — return existing job if key matches
            if x_idempotency_key:
                existing_job_id = await self._get_idempotency_job(x_idempotency_key)
                if existing_job_id:
                    existing = await self.job_store.get_job(existing_job_id)
                    if existing:
                        existing_status = existing.get("status", "queued")
                        # If the cached job failed, evict the stale mapping
                        # so the retry creates a fresh job instead of
                        # returning the dead one forever.
                        if existing_status == JobStatus.FAILED.value:
                            logger.warning(
                                "Idempotency stale: key=%s job=%s status=failed, evicting",
                                x_idempotency_key[:16], existing_job_id,
                            )
                            await self._del_idempotency_job(x_idempotency_key)
                        else:
                            logger.info(
                                "Idempotency hit: key=%s job=%s",
                                x_idempotency_key[:16], existing_job_id,
                            )
                            return JobResponse(
                                success=True,
                                engine="ears",
                                engine_version="0.2.0",
                                job_id=existing_job_id,
                                status=JobStatus(existing_status),
                                trace_id=existing_job_id,
                            )

            normalized_profile = self._normalize_processing_profile(
                processing_profile
            )

            # Determine source file: prefer audio, fall back to video
            source_file = audio or video
            if source_file is None:
                raise HTTPException(
                    status_code=422,
                    detail="Either 'audio' or 'video' file must be provided",
                )

            job_id = str(uuid.uuid4())

            # Working copy on disk (Whisper / Pyannote / ffmpeg need a path).
            audio_dir = Path(self.cfg.audio_storage_path) / tenant_id / session_id
            audio_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(source_file.filename or "audio.wav").suffix or ".wav"
            audio_path = audio_dir / f"{job_id}{ext}"

            content = await source_file.read()
            audio_path.write_bytes(content)

            # Durable copy in object storage. Required for cross-pod replay
            # and for the canonical orchestrator to re-fetch.
            try:
                audio_key = self._artifacts.build_key(
                    tenant_id, "ears", session_id, "audio", f"{job_id}{ext}",
                )
                await self._artifacts.upload_file(
                    audio_key, audio_path,
                    metadata={
                        "job_id": job_id,
                        "original": source_file.filename or "",
                    },
                )
            except Exception as e:
                logger.error(
                    "ears.audio_upload_failed",
                    job_id=job_id, error=str(e), exc_info=True,
                )
                raise HTTPException(
                    status_code=503, detail="artifact store unavailable",
                )

            # Initialize job (Redis-persisted via SDK JobStore)
            await self.job_store.set_job(job_id, {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "audio_path": str(audio_path),
                "processing_profile": normalized_profile,
                "original_filename": source_file.filename or "",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "result": None,
                "error": None,
                "progress_percent": 0.0,
                "current_stage": "queued",
            })

            # Dispatch: prefer durable Redis Streams queue, fall back to in-process
            enqueued = await self.enqueue_gpu_job(
                job_id=job_id,
                payload={
                    "job_id": job_id,
                    "audio_path": str(audio_path),
                    "session_id": session_id,
                    "tenant_id": tenant_id,
                    "language": language,
                    "num_speakers": num_speakers,
                    "processing_profile": normalized_profile,
                    "original_filename": source_file.filename or "",
                },
            )
            if not enqueued:
                # Fallback: run in-process (single-pod / dev mode)
                background_tasks.add_task(
                    self._run_transcription,
                    job_id=job_id,
                    audio_path=str(audio_path),
                    session_id=session_id,
                    tenant_id=tenant_id,
                    language=language,
                    num_speakers=num_speakers,
                    processing_profile=normalized_profile,
                    original_filename=source_file.filename or "",
                )

            # Phase 1.6: Store idempotency key → job_id mapping
            if x_idempotency_key:
                await self._set_idempotency_job(x_idempotency_key, job_id)

            return JobResponse(
                success=True,
                engine="ears",
                engine_version="0.2.0",
                job_id=job_id,
                status=JobStatus.QUEUED,
                trace_id=job_id,
            )

        # ── Get Job Status ─────────────────────────────────────

        @app.get("/api/v1/ears/jobs/{job_id}")
        async def get_job(
            job_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get the status and result of a transcription job."""
            job = await self.job_store.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return job

        # ── Get Transcript ─────────────────────────────────────

        @app.get("/api/v1/ears/sessions/{session_id}/transcript")
        async def get_transcript(
            session_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get the transcript for a KT session."""
            all_jobs = await self.job_store.list_jobs(limit=500)
            for job in all_jobs:
                if (
                    job.get("session_id") == session_id
                    and job.get("status") == JobStatus.COMPLETED.value
                ):
                    return job.get("result")

            raise HTTPException(
                status_code=404, detail="No transcript found for session"
            )

        # ── List Sessions ──────────────────────────────────────

        @app.get("/api/v1/ears/sessions")
        async def list_sessions(
            user: NexusUser = Depends(get_current_user),
        ):
            """List all transcription sessions for the current tenant."""
            sessions = {}
            all_jobs = await self.job_store.list_jobs(limit=500)
            for job in all_jobs:
                if job.get("tenant_id") == user.tenant_id:
                    sid = job.get("session_id", "")
                    if sid not in sessions:
                        sessions[sid] = {
                            "session_id": sid,
                            "job_count": 0,
                            "latest_status": job.get("status"),
                            "created_at": job.get("created_at"),
                        }
                    sessions[sid]["job_count"] += 1
                    sessions[sid]["latest_status"] = job.get("status")

            return list(sessions.values())

    @staticmethod
    def _deduplicate_overlap_segments(segments: list[dict]) -> list[dict]:
        """Remove duplicate segments from overlapping chunk boundaries.

        When chunks overlap by N seconds, the overlap region is transcribed
        twice. This keeps the first occurrence (from the earlier chunk) and
        drops segments whose start time falls within an already-covered range.
        """
        if not segments:
            return segments
        # Sort by start time
        segments.sort(key=lambda s: s["start"])
        deduped: list[dict] = [segments[0]]
        for seg in segments[1:]:
            prev = deduped[-1]
            # If this segment starts before the previous one ends,
            # it's from the overlap region — skip it.
            if seg["start"] < prev["end"] - 0.1:
                continue
            deduped.append(seg)
        return deduped

    # ── Phase 1.6: Idempotency helpers ─────────────────────────

    async def _get_idempotency_job(self, idem_key: str) -> Optional[str]:
        """Look up a previously dispatched job by idempotency key."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                return await redis.get(f"nexus:idem:ears:{idem_key}")
            except Exception:
                pass
        return None

    async def _set_idempotency_job(self, idem_key: str, job_id: str) -> None:
        """Store idempotency key → job_id mapping (24h TTL)."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                await redis.setex(f"nexus:idem:ears:{idem_key}", 86400, job_id)
            except Exception:
                pass

    async def _del_idempotency_job(self, idem_key: str) -> None:
        """Remove a stale idempotency mapping (e.g. for failed jobs)."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                await redis.delete(f"nexus:idem:ears:{idem_key}")
            except Exception:
                pass

    def _normalize_processing_profile(self, processing_profile: str | None) -> str:
        profile = (processing_profile or self.cfg.default_processing_profile).strip().lower()
        if profile in {"deep", "full"}:
            return "deep"
        return "fast"

    async def _run_transcription(
        self,
        job_id: str,
        audio_path: str,
        session_id: str,
        tenant_id: str,
        language: str,
        num_speakers: Optional[int],
        processing_profile: str,
        original_filename: str = "",
    ):
        """Background task: full transcription pipeline with preprocessing."""
        pipeline_stages: list[str] = []
        start_time = time.monotonic()

        try:
            # ── Stage 1: Preprocessing ─────────────────────────
            await self.job_store.update_job(
                job_id,
                status=JobStatus.PROCESSING.value,
                current_stage="preprocessing",
                progress_percent=5.0,
            )

            processed_path = audio_path
            audio_meta = None

            try:
                processed_dir = str(
                    Path(self.cfg.audio_storage_path) / tenant_id / session_id / "processed"
                )
                preprocess_result = await self.preprocessor.prepare(
                    input_path=audio_path,
                    output_dir=processed_dir,
                    session_id=session_id,
                )
                processed_path = preprocess_result.output_path
                audio_meta = preprocess_result.metadata
                pipeline_stages.extend(preprocess_result.stages_applied)

                await self.job_store.update_job(
                    job_id,
                    current_stage="preprocessing_complete",
                    progress_percent=15.0,
                )

                logger.info(
                    "ears.preprocessed",
                    job_id=job_id,
                    stages=preprocess_result.stages_applied,
                    duration=audio_meta.duration_seconds if audio_meta else 0,
                )
            except RuntimeError as e:
                # Check if this is a video file — cannot proceed without FFmpeg
                video_extensions = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv"}
                if Path(audio_path).suffix.lower() in video_extensions:
                    raise RuntimeError(
                        f"FFmpeg is required to extract audio from video files "
                        f"({Path(audio_path).suffix}), but it is not available: {e}"
                    )
                # Audio-only file — FFmpeg not available, proceed with raw
                logger.warning(
                    "ears.preprocess_skipped: %s — using raw audio", e
                )
                pipeline_stages.append("preprocess_skipped")

            # ── Stage 2: Speaker Diarization ───────────────────
            await self.job_store.update_job(
                job_id,
                current_stage="diarizing",
                progress_percent=20.0,
            )

            # Skip diarization in fast mode — assign all to SPEAKER_00
            _gpu_prio = GPU_PRIORITY_FAST if processing_profile == "fast" else GPU_PRIORITY_DEEP
            diarize_start = time.monotonic()

            if processing_profile == "fast" and self.cfg.skip_diarization_in_fast_mode:
                duration = audio_meta.duration_seconds if audio_meta else 30.0
                speaker_segments = [
                    {"speaker": "SPEAKER_00", "start": 0.0, "end": round(duration, 3)}
                ]
                pipeline_stages.append("diarize_skipped_fast")
                logger.info(
                    "ears.diarization_skipped",
                    job_id=job_id,
                    reason="fast_profile",
                    duration=duration,
                )
            else:
                # Diarize with timeout to prevent indefinite hangs
                _timeout = self.cfg.diarization_inference_timeout_seconds
                try:
                    async with self._gpu_semaphore.acquire(_gpu_prio):
                        speaker_segments = await asyncio.wait_for(
                            self.diarizer.diarize(processed_path, num_speakers),
                            timeout=_timeout,
                        )
                except asyncio.TimeoutError:
                    duration = audio_meta.duration_seconds if audio_meta else 30.0
                    speaker_segments = [
                        {"speaker": "SPEAKER_00", "start": 0.0, "end": round(duration, 3)}
                    ]
                    logger.warning(
                        "ears.diarization_timeout",
                        job_id=job_id,
                        timeout_seconds=_timeout,
                    )
                pipeline_stages.append("diarize")

            diarize_elapsed = round(time.monotonic() - diarize_start, 1)

            await self.job_store.update_job(
                job_id,
                current_stage="diarization_complete",
                progress_percent=45.0,
            )
            logger.info(
                "ears.diarization_complete",
                job_id=job_id,
                speakers=len(set(s.get("speaker", "") for s in speaker_segments)) if speaker_segments else 0,
                segments=len(speaker_segments) if speaker_segments else 0,
                elapsed_s=diarize_elapsed,
            )

            # ── Stage 3: Whisper Transcription ─────────────────
            await self.job_store.update_job(
                job_id,
                current_stage="transcribing",
                progress_percent=50.0,
            )

            # Check for chunk paths from preprocessing
            chunk_paths = getattr(preprocess_result, "chunk_paths", []) if audio_meta else []

            if chunk_paths:
                # ── Chunked transcription fan-out ──────────────
                pipeline_stages.append("chunked_transcribe")
                all_whisper_segments: list[dict] = []
                overlap_sec = getattr(
                    self.preprocessor.config, "chunk_overlap_seconds", 5
                )
                step_sec = self.cfg.chunk_duration_seconds - overlap_sec

                for chunk_idx, chunk_path in enumerate(chunk_paths):
                    chunk_offset = chunk_idx * step_sec
                    pct = 50.0 + (chunk_idx / len(chunk_paths)) * 25.0
                    await self.job_store.update_job(
                        job_id,
                        current_stage=f"transcribing chunk {chunk_idx + 1}/{len(chunk_paths)}",
                        progress_percent=round(pct, 1),
                    )

                    async with self._gpu_semaphore.acquire(_gpu_prio):
                        chunk_segments = await self.transcriber.transcribe(
                            chunk_path,
                            language,
                            processing_profile,
                        )

                    # Offset timestamps to absolute position
                    for seg in chunk_segments:
                        seg["start"] += chunk_offset
                        seg["end"] += chunk_offset
                    all_whisper_segments.extend(chunk_segments)

                # Deduplicate overlapping segments (from chunk overlap)
                whisper_segments = self._deduplicate_overlap_segments(
                    all_whisper_segments
                )
                logger.info(
                    "ears.chunked_transcription_complete",
                    job_id=job_id,
                    chunks=len(chunk_paths),
                    total_segments=len(whisper_segments),
                )
            else:
                # ── Single-file transcription ──────────────────
                async with self._gpu_semaphore.acquire(_gpu_prio):
                    whisper_segments = await self.transcriber.transcribe(
                        processed_path,
                        language,
                        processing_profile,
                    )
                pipeline_stages.append("transcribe")

            # ── Stage 4: Alignment ─────────────────────────────
            await self.job_store.update_job(
                job_id,
                current_stage="aligning",
                progress_percent=85.0,
            )

            aligned = align_segments(whisper_segments, speaker_segments)
            pipeline_stages.append("align")

            await self.job_store.update_job(
                job_id,
                current_stage="finalizing",
                progress_percent=95.0,
            )

            # ── Build Result ───────────────────────────────────
            elapsed = time.monotonic() - start_time

            result = TranscriptionResult(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                segments=aligned,
                audio=audio_meta,
                language=language,
                model_version=f"whisper-{self.transcriber.active_model_size or self.transcriber.model_size}",
                processing_time_seconds=round(elapsed, 2),
                pipeline_stages=pipeline_stages,
            )
            result.compute_stats()

            await self.job_store.update_job(
                job_id,
                status=JobStatus.COMPLETED.value,
                result=result.model_dump(mode="json"),
                processing_time_seconds=round(elapsed, 2),
                current_stage="completed",
                progress_percent=100.0,
            )

            # ── Emit Event for Shield ──────────────────────────
            if self.event_bus:
                await self.event_bus.publish(NexusEvent(
                    event_type="ears.transcription.completed",
                    tenant_id=tenant_id,
                    trace_id=job_id,
                    engine="ears",
                    session_id=session_id,
                    data={
                        "job_id": job_id,
                        "session_id": session_id,
                        "transcript_text": result.full_text,
                        "segment_count": result.segment_count,
                        "speaker_count": len(result.speakers),
                        "duration_seconds": result.duration_seconds,
                        "word_count": result.word_count,
                        "processing_profile": processing_profile,
                        "model_size": self.transcriber.active_model_size,
                        "pipeline_stages": pipeline_stages,
                    },
                ))

            logger.info(
                "ears.transcription_complete",
                job_id=job_id,
                session_id=session_id,
                segments=result.segment_count,
                speakers=len(result.speakers),
                words=result.word_count,
                duration_s=result.duration_seconds,
                elapsed_s=round(elapsed, 2),
                processing_profile=processing_profile,
                model_size=self.transcriber.active_model_size,
                stages=pipeline_stages,
            )

            # ── Record Prometheus metrics ──
            if self._m_transcriptions:
                self._m_transcriptions.labels(
                    service=self.name, profile=processing_profile, status="success",
                ).inc()
            if self._m_whisper_seconds:
                self._m_whisper_seconds.labels(
                    service=self.name, profile=processing_profile,
                ).observe(elapsed)
            if self._m_gpu_queue_depth:
                self._m_gpu_queue_depth.labels(service=self.name).set(
                    self._gpu_semaphore.waiting_count
                )

        except Exception as e:
            logger.error(
                "ears.transcription_failed",
                job_id=job_id,
                error=str(e),
                exc_info=True,
            )
            if self._m_transcriptions:
                self._m_transcriptions.labels(
                    service=self.name, profile=processing_profile, status="failure",
                ).inc()
            await self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED.value,
                error=str(e),
                current_stage="failed",
            )

            if self.event_bus:
                await self.event_bus.publish(NexusEvent(
                    event_type="ears.transcription.failed",
                    tenant_id=tenant_id,
                    trace_id=job_id,
                    engine="ears",
                    session_id=session_id,
                    data={"job_id": job_id, "error": str(e)},
                ))
        finally:
            # Working-dir GC: durable copies are in the artifact store.
            if (
                self.cfg.audio_delete_after_upload
                and not self._artifacts.is_local
            ):
                try:
                    p = Path(audio_path)
                    if p.is_file():
                        await asyncio.to_thread(p.unlink)
                    proc_dir = (
                        Path(self.cfg.audio_storage_path)
                        / tenant_id
                        / session_id
                        / "processed"
                    )
                    if proc_dir.is_dir():
                        import shutil
                        await asyncio.to_thread(shutil.rmtree, str(proc_dir), True)
                except Exception as ce:
                    logger.warning(
                        "ears.working_dir_gc_failed",
                        job_id=job_id, error=str(ce),
                    )

    # ─── Phase 6: per-stage building blocks ──────────────────────
    # Each stage is a pure-ish coroutine: it takes its inputs explicitly
    # and returns its outputs explicitly. The legacy `_run_transcription`
    # wraps job-store updates + event emission around them; the new
    # workflow handlers in app/workflow_handlers.py call each stage as
    # an independent canonical step.
    #
    # Why duplicate logic instead of refactoring `_run_transcription` to
    # call these? Surgery on the legacy REST path during the workflow
    # rollout adds risk for zero new payoff — the REST endpoint will be
    # retired once the workflow plane is the only path. A follow-up
    # cleanup PR collapses the duplication.

    async def _stage_preprocess(
        self,
        audio_path: str,
        session_id: str,
        tenant_id: str,
    ) -> dict:
        """Run preprocessing (resample, normalize, optional VAD, chunking)
        and return checkpoint-shaped output. Raises only when the input is
        a video file and FFmpeg is unavailable — audio-only inputs fall
        back to using the raw file.
        """
        processed_dir = str(
            Path(self.cfg.audio_storage_path) / tenant_id / session_id / "processed"
        )
        try:
            pr = await self.preprocessor.prepare(
                input_path=audio_path,
                output_dir=processed_dir,
                session_id=session_id,
            )
            meta_dict = pr.metadata.model_dump(mode="json") if pr.metadata else {}
            return {
                "processed_path": pr.output_path,
                "chunk_paths": list(getattr(pr, "chunk_paths", []) or []),
                "audio_meta": meta_dict,
                "stages": list(pr.stages_applied or []),
            }
        except RuntimeError as e:
            video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv"}
            if Path(audio_path).suffix.lower() in video_exts:
                raise RuntimeError(
                    f"FFmpeg is required to extract audio from video files "
                    f"({Path(audio_path).suffix}), but it is not available: {e}"
                )
            logger.warning(
                "ears.preprocess_skipped: %s — using raw audio", e
            )
            return {
                "processed_path": audio_path,
                "chunk_paths": [],
                "audio_meta": {},
                "stages": ["preprocess_skipped"],
            }

    async def _stage_diarize(
        self,
        processed_path: str,
        audio_meta: dict,
        num_speakers: Optional[int],
        processing_profile: str,
    ) -> dict:
        """Run speaker diarization. Fast profile may skip via config flag
        and return a single SPEAKER_00 segment covering the whole file."""
        duration = float(audio_meta.get("duration_seconds") or 30.0)
        if processing_profile == "fast" and self.cfg.skip_diarization_in_fast_mode:
            return {
                "speaker_segments": [
                    {"speaker": "SPEAKER_00", "start": 0.0, "end": round(duration, 3)}
                ],
                "stages": ["diarize_skipped_fast"],
            }

        prio = (
            GPU_PRIORITY_FAST if processing_profile == "fast" else GPU_PRIORITY_DEEP
        )
        timeout = self.cfg.diarization_inference_timeout_seconds
        try:
            async with self._gpu_semaphore.acquire(prio):
                segs = await asyncio.wait_for(
                    self.diarizer.diarize(processed_path, num_speakers),
                    timeout=timeout,
                )
            return {
                "speaker_segments": segs,
                "stages": ["diarize"],
            }
        except asyncio.TimeoutError:
            logger.warning(
                "ears.diarization_timeout", timeout_seconds=timeout,
            )
            return {
                "speaker_segments": [
                    {"speaker": "SPEAKER_00", "start": 0.0, "end": round(duration, 3)}
                ],
                "stages": ["diarize_timeout"],
            }

    async def _stage_transcribe(
        self,
        processed_path: str,
        chunk_paths: list[str],
        language: str,
        processing_profile: str,
    ) -> dict:
        """Run Whisper transcription. If `chunk_paths` is non-empty, fan
        out per chunk and dedupe overlap windows on the way out."""
        prio = (
            GPU_PRIORITY_FAST if processing_profile == "fast" else GPU_PRIORITY_DEEP
        )
        if chunk_paths:
            all_segs: list[dict] = []
            overlap_sec = getattr(
                self.preprocessor.config, "chunk_overlap_seconds", 5
            )
            step_sec = self.cfg.chunk_duration_seconds - overlap_sec
            for idx, chunk_path in enumerate(chunk_paths):
                offset = idx * step_sec
                async with self._gpu_semaphore.acquire(prio):
                    chunk_segs = await self.transcriber.transcribe(
                        chunk_path, language, processing_profile,
                    )
                for seg in chunk_segs:
                    seg["start"] += offset
                    seg["end"] += offset
                all_segs.extend(chunk_segs)
            deduped = self._deduplicate_overlap_segments(all_segs)
            return {
                "whisper_segments": deduped,
                "active_model_size": (
                    self.transcriber.active_model_size or self.transcriber.model_size
                ),
                "stages": ["chunked_transcribe"],
            }
        async with self._gpu_semaphore.acquire(prio):
            segs = await self.transcriber.transcribe(
                processed_path, language, processing_profile,
            )
        return {
            "whisper_segments": segs,
            "active_model_size": (
                self.transcriber.active_model_size or self.transcriber.model_size
            ),
            "stages": ["transcribe"],
        }

    def _stage_align(
        self,
        whisper_segments: list[dict],
        speaker_segments: list[dict],
        audio_meta: dict,
        language: str,
        active_model_size: str,
        accumulated_stages: list[str],
        elapsed_seconds: float,
        job_id: str,
        session_id: str,
        tenant_id: str,
    ) -> TranscriptionResult:
        """Align speaker + whisper segments and assemble TranscriptionResult.
        Pure CPU. Caller is responsible for emitting events/metrics."""
        aligned = align_segments(whisper_segments, speaker_segments)
        accumulated_stages.append("align")

        audio = None
        if audio_meta:
            try:
                audio = AudioMetadata.model_validate(audio_meta)
            except Exception:
                audio = None

        result = TranscriptionResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            segments=aligned,
            audio=audio,
            language=language,
            model_version=f"whisper-{active_model_size}",
            processing_time_seconds=round(elapsed_seconds, 2),
            pipeline_stages=list(accumulated_stages),
        )
        result.compute_stats()
        return result

    async def on_shutdown(self):
        """Gracefully stop the worker loop and release resources."""
        await self.stop_worker_loop()


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = EarsEngine()
    engine.run()


if __name__ == "__main__":
    main()
