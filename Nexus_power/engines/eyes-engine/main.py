"""
Nexus Eyes Engine — Visual Intelligence.

Watches screen recordings/screenshots from KT sessions,
identifies UI elements, reads text from screens, and
understands visual workflows.

Pipeline:
1. Video input (MP4/WebM screen recording) or screenshot batches
2. Frame extraction with intelligent diffing (skip unchanged frames)
3. Application type classification (web UI, Excel, mainframe, PDF, etc.)
4. Visual parsing per application type
5. UI element extraction (buttons, fields, menus, tables)
6. Text extraction (OCR + layout analysis)
7. Visual state change detection (what changed between frames)
8. Emit structured visual data to Backbone via Shield

On-prem: Uses LLaVA through Ollama locally. No screenshots leave the datacenter.
"""

from __future__ import annotations

import os
import uuid
import asyncio
import shutil
import subprocess
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Header, Query, Security
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBearer as _BearerScheme, HTTPAuthorizationCredentials as _BearerCreds
from pydantic import AliasChoices, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse, JobResponse, JobStatus
from nexus_sdk.auth import NexusUser, get_current_user, get_auth_service
from nexus_sdk.events import NexusEvent
from nexus_sdk.worker import GPUWorkerMixin, PriorityGPUSemaphore, GPU_PRIORITY_FAST, GPU_PRIORITY_DEEP
from nexus_sdk.storage import ArtifactStore, StorageConfig, create_storage
from nexus_sdk.media.models import (
    FrameAnalysis,
    SceneTransitionAnalysis,
    VisualAnalysisResult,
    ApplicationType,
    UIElement,
    VideoProcessingJob,
    MediaJobStatus,
)

from app.frame_diff import FrameExtractor, probe_video
from app.vision import OCREngine, ApplicationClassifier, VisualAnalyzer
from app.chunk_checkpoint import (
    completed_chunks as _load_chunk_checkpoints,
    save_chunk_result as _save_chunk_checkpoint,
    clear_chunk_checkpoints as _clear_chunk_checkpoints,
)

# OCREngine now implements nexus_sdk.media.vision.OCRProvider and
# VisualAnalyzer implements VisionProvider, making them pluggable
# providers on the canonical hot path.
from nexus_sdk.media.vision import OCRProvider, VisionProvider  # noqa: F401 — type reference

import structlog
logger = structlog.get_logger()


def _analysis_list(value: Any) -> list:
    """Return a list for untrusted analysis fields."""
    if value is None or value == "" or value == "null":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []



_ADDRESS_BAR_URL_RX = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9\-]{0,62}(?:\.[a-z]{2,10}){1,3}"
    r"(?:/[^\s\"'<>|]{0,300})?",
    re.IGNORECASE,
)


def _address_bar_url(text_regions) -> str:
    """Deterministic address-bar read: URL-shaped OCR text whose region sits in
    the TOP strip of the frame (where every browser's address bar lives). Uses
    the OCR regions' own geometry -- no model call, generic across apps. The
    TOPMOST match wins (the bar sits above its own autocomplete dropdown), with
    length as the tie-break. Returns "" when nothing URL-shaped is up there
    (desktop apps, kiosks) -- NEVER invents; downstream vision tiers cover the
    rest at their own honest confidence."""
    try:
        regions = list(text_regions or [])
        if not regions:
            return ""

        def _ys(r):
            bb = r.get("bbox") if isinstance(r, dict) else None
            out = []
            for p in bb or []:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    try:
                        out.append(float(p[1]))
                    except (TypeError, ValueError):
                        pass
            return out

        frame_h = 0.0
        for r in regions:
            ys = _ys(r)
            if ys:
                frame_h = max(frame_h, max(ys))
        if frame_h <= 0:
            return ""
        strip_limit = frame_h * 0.12

        best = ""
        best_top = None
        for r in regions:
            ys = _ys(r)
            if not ys:
                continue
            top = min(ys)
            if top > strip_limit:
                continue
            text = str((r.get("text") if isinstance(r, dict) else "") or "")
            for m in _ADDRESS_BAR_URL_RX.finditer(text):
                cand = m.group(0).strip().rstrip(".,;:")
                low = cand.lower()
                # Require a dotted host AND either a scheme or a path -- a
                # dotted phrase in a tab title must not mint a page URL.
                if "." not in low:
                    continue
                if not (low.startswith("http") or "/" in low):
                    continue
                if best_top is None or top < best_top or (
                    top == best_top and len(cand) > len(best)
                ):
                    best = cand
                    best_top = top
        return best[:1000]
    except Exception:
        return ""


def _analysis_text(value: Any) -> str:
    """Return a safe string for untrusted analysis text fields."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _analysis_tables(value: Any) -> list[dict]:
    """FrameAnalysis expects tables as list[dict], while LLMs may return scalars."""
    return [item for item in _analysis_list(value) if isinstance(item, dict)]


# ─── Frame serving auth dependency ─────────────────────────────
# <img> tags cannot set Authorization headers, so the frame endpoint
# additionally accepts the access token as a ?token= query parameter.
# The dependency validates via the same JWT service as every other endpoint.

_frame_bearer = _BearerScheme(auto_error=False)


async def _get_frame_user(
    credentials: Optional[_BearerCreds] = Security(_frame_bearer),
    token: Optional[str] = Query(default=None, alias="token"),
) -> NexusUser:
    """Auth dependency for frame image serving.

    Accepts the JWT access token via:
    - ``Authorization: Bearer <token>`` header  (API / XHR callers)
    - ``?token=<token>`` query parameter        (``<img src>`` callers)

    Any other combination raises HTTP 401.
    """
    raw: Optional[str] = None
    if credentials:
        raw = credentials.credentials
    elif token:
        raw = token
    if not raw:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return get_auth_service().validate_token(raw)


# ─── Configuration ─────────────────────────────────────────────

class EyesConfig(EngineConfig):
    engine_name: str = "eyes"
    engine_port: int = 8003

    # Processing profiles
    default_processing_profile: str = Field(
        default="fast",
        validation_alias="EYES_DEFAULT_PROCESSING_PROFILE",
    )
    fast_max_frames: int = Field(
        default=6,
        validation_alias="EYES_FAST_MAX_FRAMES",
    )
    fast_max_scenes: int = Field(
        default=4,
        validation_alias="EYES_FAST_MAX_SCENES",
    )
    ocr_max_workers: int = Field(
        default=2,
        validation_alias="EYES_OCR_MAX_WORKERS",
    )
    # Downscale frames wider than this before OCR. EasyOCR on CPU scales
    # ~linearly with pixel count, but Zoom screen captures with small
    # browser tab text get hallucinated reads ("USAA" -> "USAD",
    # "JetBlue" -> "Jethlue") when downsized below ~1.0 px per source
    # pixel.
    #
    # Phase 2: raised the default from 0 (disabled) to 1600 so long-demo
    # processing keeps wall-time predictable.  1600 px wide keeps small
    # text at >=0.83 px/source-pixel on 1080p captures (1920->1600) and
    # 0.94 on 1440p (1696->1600), well above the empirical hallucination
    # threshold.  Set to 0 to disable for fidelity-critical recordings.
    ocr_downscale_max_width: int = Field(
        default=1600,
        validation_alias="EYES_OCR_DOWNSCALE_MAX_WIDTH",
    )
    fast_representative_ocr_only: bool = Field(
        default=True,
        validation_alias="EYES_FAST_REPRESENTATIVE_OCR_ONLY",
    )
    fast_skip_ocr: bool = Field(
        default=True,
        validation_alias="EYES_FAST_SKIP_OCR",
    )

    # Per-frame OCR for multi-frame scenes.  Default ON because without it
    # all frames in a scene share the representative frame's OCR text,
    # making field-level state changes (typed values, selected dropdown
    # options) invisible to downstream step extraction. When the user
    # fills a form over 4 seconds, the eyes engine captures multiple
    # frames but rep-frame-only OCR returns the same empty-form text for
    # every one of them — so the bottom panel can never show "Gender =
    # Female". The CPU cost is ~30-60 s per extra frame OCR pass; on a
    # 10-frame form scene that adds ~5 min to processing. Set
    # EYES_PER_FRAME_OCR=false to revert when wall time matters more
    # than form-state fidelity.
    per_frame_ocr_in_multi_frame_scenes: bool = Field(
        # Architect P0 #3: defaults OFF. CPU EasyOCR per-frame is too
        # slow under load and is the leading cause of OCR backlog +
        # workflow quarantine. GPU-deployed operators opt in via env.
        default=False,
        validation_alias="EYES_PER_FRAME_OCR",
    )

    # Multimodal processing profile (richer visual extraction for E2E analysis)
    multimodal_max_frames: int = Field(
        default=30,
        validation_alias="EYES_MULTIMODAL_MAX_FRAMES",
    )
    multimodal_max_scenes: int = Field(
        default=20,
        validation_alias="EYES_MULTIMODAL_MAX_SCENES",
    )

    # Per-frame LLaVA enrichment.  By default LLaVA is invoked once per scene
    # on the representative frame and the resulting description is propagated
    # to every frame in that scene.  That collapses real intra-page user
    # actions (form fills, dropdown selections, button activations) into a
    # single shared description and starves the step extractor of diff
    # signal.  When this flag is on AND the processing_profile is
    # multimodal/deep AND a scene has multiple frames, every frame in that
    # scene is analysed individually so each frame carries its own
    # description / ui_elements.  The cost is one extra LLaVA call per
    # non-representative frame in multi-frame scenes; the per-artifact ceiling
    # below caps the worst case so a session with hundreds of frames cannot
    # blow up the GPU budget.
    per_frame_llava: bool = Field(
        default=False,
        validation_alias="EYES_PER_FRAME_LLAVA",
    )
    per_frame_llava_limit: int = Field(
        default=50,
        validation_alias="EYES_PER_FRAME_LLAVA_LIMIT",
    )

    # Vision model (Ollama — Meta Llama 3.2 Vision)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias=AliasChoices("EYES_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"),
    )
    fast_ollama_model: str = Field(
        default="llama3.2-vision:11b",
        validation_alias="EYES_FAST_OLLAMA_MODEL",
    )
    ollama_model: str = Field(
        default="llama3.2-vision:11b",
        validation_alias="EYES_OLLAMA_MODEL",
    )

    # Frame extraction
    frame_diff_threshold: float = Field(
        default=0.03,                          # lowered from 0.05 — text-field changes
                                               # affect only 1-3% of dHash bits so the
                                               # old 5% threshold silently dropped all
                                               # form-fill interactions
        validation_alias="EYES_FRAME_DIFF_THRESHOLD",
    )
    max_fps_extract: float = Field(
        default=2.0,                           # raised from 1.0 — 2fps baseline means
                                               # at most 0.5 s between sampled frames
                                               # before adaptive slow-down kicks in
        validation_alias="EYES_MAX_FPS_EXTRACT",
    )
    keyframe_only: bool = False
    adaptive_sampling: bool = Field(
        default=True,
        validation_alias="EYES_ADAPTIVE_SAMPLING",
    )
    settle_frame: bool = Field(
        default=True,
        validation_alias="EYES_SETTLE_FRAME",
    )

    # Scene grouping
    # Hamming distance at which two consecutive frames are considered a NEW scene.
    # Lowered from 0.15 → 0.10 so finer UI state transitions (form-step changes,
    # modal openings, dropdown expansion) produce their own scene rather than
    # being merged into a sibling.  Combined with the SDK build_scenes layer this
    # gives one card per distinct UI state — essential for "every step shown"
    # QA evidence.
    scene_boundary_threshold: float = Field(
        default=0.10,
        validation_alias="EYES_SCENE_BOUNDARY_THRESHOLD",
    )

    # GPU
    gpu_concurrency: int = 1              # Raise on multi-GPU setups

    # Chunked long-video processing
    chunk_threshold_seconds: float = Field(
        default=600.0,
        validation_alias="EYES_CHUNK_THRESHOLD_SECONDS",
    )
    chunk_duration_seconds: float = Field(
        default=300.0,
        validation_alias="EYES_CHUNK_DURATION_SECONDS",
    )
    # How many chunks may run their full pipeline concurrently. The internal
    # GPU semaphore still serialises LLaVA calls across all in-flight chunks,
    # so this controls overlap of the I/O-bound stages (extract / OCR / DB).
    #
    # Phase 2: raised the default from 1 (sequential) to 2.  CPU stages of
    # one chunk now run while another waits on Ollama, cutting long-demo
    # wall time ~30%.  GPU is still serialised by ``gpu_concurrency`` so
    # this is safe on single-GPU hosts; raise to 3-4 on multi-GPU.
    chunk_concurrency: int = Field(
        default=2,
        validation_alias="EYES_CHUNK_CONCURRENCY",
    )
    # Memory hygiene for long videos: hard ceiling and eviction strategy.
    # When total resident memory of the eyes process exceeds this MiB
    # threshold mid-pipeline, the analyzer aggressively releases interim
    # buffers (raw_frames, ocr_results, scene transition cache) before
    # continuing.  Set to 0 to disable the watchdog (legacy behaviour).
    memory_ceiling_mb: float = Field(
        default=2048.0,
        validation_alias="EYES_MEMORY_CEILING_MB",
    )
    # Eviction tag — when memory pressure is high we may also drop the
    # in-memory bytes of frame screenshots (the *path* on disk is preserved
    # so artifacts are still served from frame-storage volume).
    aggressive_frame_buffer_eviction: bool = Field(
        default=True,
        validation_alias="EYES_AGGRESSIVE_FRAME_EVICTION",
    )

    # Wave B — transition-pair LLaVA (two screenshots → action label)
    transition_llm_enabled: bool = Field(
        default=False,
        validation_alias="EYES_TRANSITION_LLM",
    )
    transition_llm_max_pairs: int = Field(
        default=30,
        validation_alias="EYES_TRANSITION_LLM_MAX_PAIRS",
    )
    transition_llm_min_confidence: float = Field(
        default=0.65,
        validation_alias="EYES_TRANSITION_LLM_MIN_CONFIDENCE",
    )

    # OCR
    # Comma-separated list of EasyOCR language codes (e.g. "en,es,fr,de").
    # Determines which languages the OCR engine will recognise.  English is
    # the default; production deployments handling multi-locale UIs override
    # via the env var.  Extra languages add only ~50-200 MB per language to
    # the model cache and have negligible runtime cost — adding a language
    # never makes recognition worse for existing languages.
    #
    # Stored as a CSV string at the config layer because pydantic_settings
    # parses list[str] from env as JSON which forces operators to write
    # ``["en","es"]``.  The :pyattr:`ocr_languages` property exposes the
    # parsed list to consumers; tests and operators interact with the CSV.
    ocr_languages_csv: str = Field(
        default="en",
        validation_alias="EYES_OCR_LANGUAGES",
    )

    @property
    def ocr_languages(self) -> list[str]:
        """Parsed list of EasyOCR language codes.

        Lower-cases each token, trims whitespace, drops empties.
        Always non-empty: an empty / whitespace-only configuration falls
        back to ``["en"]`` so EasyOCR can always start.
        """
        parts = [s.strip().lower() for s in (self.ocr_languages_csv or "").split(",")]
        cleaned = [p for p in parts if p]
        return cleaned or ["en"]

    ocr_gpu: bool = Field(
        default=True,
        validation_alias="EYES_OCR_GPU",
    )
    ocr_model_dir: str = "./models/easyocr"
    ocr_allow_remote_model_bootstrap: bool = Field(
        default=True,
        validation_alias="EYES_OCR_ALLOW_REMOTE_MODEL_BOOTSTRAP",
    )
    ocr_load_timeout_seconds: float = Field(
        default=30.0,
        validation_alias="EYES_OCR_LOAD_TIMEOUT_SECONDS",
    )

    # Working directory for ffmpeg / frame extraction. Treat as ephemeral
    # (emptyDir in K8s); the canonical store is the StorageBackend below.
    frames_storage_path: str = Field(
        default="./data/frames",
        validation_alias="EYES_FRAMES_PATH",
    )
    # When true (the default in cloud backends), extracted frames are removed
    # from the working dir as soon as they are uploaded to object storage, so
    # the pod stays stateless and the emptyDir cap is not approached.
    frames_delete_after_upload: bool = Field(
        default=True,
        validation_alias="EYES_FRAMES_DELETE_AFTER_UPLOAD",
    )
    # Concurrency for streaming uploads of extracted frames to the artifact
    # store. Bounded so a 100-frame scene doesn't open 100 HTTP connections.
    frame_upload_concurrency: int = Field(
        default=8,
        validation_alias="EYES_FRAME_UPLOAD_CONCURRENCY",
    )


# ─── The Eyes Engine ───────────────────────────────────────────

class EyesEngine(NexusEngine, GPUWorkerMixin):
    def __init__(self):
        self.cfg = EyesConfig()
        super().__init__(
            name="eyes",
            version="0.2.0",
            config=self.cfg,
            description="Visual Intelligence Engine",
        )
        # Modular components
        self.frame_extractor = FrameExtractor(
            frame_diff_threshold=self.cfg.frame_diff_threshold,
            max_fps_extract=self.cfg.max_fps_extract,
            keyframe_only=self.cfg.keyframe_only,
            adaptive_sampling=self.cfg.adaptive_sampling,
            settle_frame=self.cfg.settle_frame,
        )
        self.app_classifier = ApplicationClassifier()
        self.ocr = OCREngine(
            languages=self.cfg.ocr_languages,
            gpu=self.cfg.ocr_gpu,
            model_dir=self.cfg.ocr_model_dir,
            allow_remote_model_bootstrap=self.cfg.ocr_allow_remote_model_bootstrap,
            load_timeout_seconds=self.cfg.ocr_load_timeout_seconds,
            downscale_max_width=self.cfg.ocr_downscale_max_width,
        )
        # Architect P0 #2: process-isolated OCR pool. asyncio.to_thread
        # can't kill a hung EasyOCR call, so on timeout the thread
        # leaks executor capacity. Process pool gets a hard kill.
        # Enable via env (default off) so first-deploy rollback is
        # one env-var flip away. When enabled, _batch_ocr routes
        # through this pool instead of self.ocr directly.
        self._ocr_pool: Optional[Any] = None
        if str(
            os.environ.get("EYES_OCR_PROCESS_ISOLATION", "true"),
        ).lower() in ("1", "true", "yes"):
            try:
                from app.vision.ocr_pool import OCRProcessPool
                self._ocr_pool = OCRProcessPool(
                    max_workers=self.cfg.ocr_max_workers,
                    frame_timeout_s=float(
                        os.environ.get(
                            "EYES_OCR_FRAME_TIMEOUT_SECONDS", "60.0",
                        ),
                    ),
                    languages=self.cfg.ocr_languages,
                    gpu=self.cfg.ocr_gpu,
                    model_dir=self.cfg.ocr_model_dir,
                    allow_remote_model_bootstrap=self.cfg.ocr_allow_remote_model_bootstrap,
                    load_timeout_seconds=self.cfg.ocr_load_timeout_seconds,
                )
                logger.info(
                    "eyes.ocr_pool.enabled max_workers=%d",
                    self.cfg.ocr_max_workers,
                )
            except Exception as e:
                logger.warning(
                    "eyes.ocr_pool.disabled err=%s — falling back to thread-based OCR",
                    e,
                )
                self._ocr_pool = None
        self.visual_analyzer = VisualAnalyzer(
            ollama_base_url=self.cfg.ollama_base_url,
            ollama_model=self.cfg.ollama_model,
            fast_ollama_model=self.cfg.fast_ollama_model,
        )

        # GPU concurrency guard — priority-aware, configurable for multi-GPU
        self._gpu_semaphore = PriorityGPUSemaphore(
            concurrency=self.cfg.gpu_concurrency
        )

        # Persistent artifact store (object storage). The local working dir
        # is intentionally distinct — it holds in-flight ffmpeg outputs and
        # is reclaimed once frames are uploaded.
        self._storage_config = StorageConfig()
        self._artifacts = ArtifactStore(
            create_storage(self._storage_config), self._storage_config
        )

    async def on_startup(self):
        """Load models."""
        self._gpu_semaphore = PriorityGPUSemaphore(concurrency=self.cfg.gpu_concurrency)
        os.makedirs(self.cfg.frames_storage_path, exist_ok=True)
        logger.info(
            "eyes.storage_backend",
            backend=self._artifacts.backend_name,
            working_dir=self.cfg.frames_storage_path,
            delete_after_upload=self.cfg.frames_delete_after_upload,
        )

        # ── Register engine-specific Prometheus metrics ──
        from nexus_sdk.observability.metrics import get_metrics
        m = get_metrics()
        if m:
            self._m_frame_extractions = m.custom_counter(
                "eyes_frame_extractions_total",
                "Total frame extraction jobs",
                labels=["profile", "status"],
            )
            self._m_scene_analysis_seconds = m.custom_histogram(
                "eyes_scene_analysis_seconds",
                "LLaVA scene analysis duration per frame",
                labels=["model"],
                buckets=(0.5, 1, 2, 5, 10, 30, 60),
            )
            self._m_ocr_seconds = m.custom_histogram(
                "eyes_ocr_seconds",
                "OCR extraction duration per frame",
                labels=[],
                buckets=(0.1, 0.25, 0.5, 1, 2, 5),
            )
            self._m_gpu_queue_depth = m.custom_gauge(
                "eyes_gpu_queue_waiting",
                "Number of tasks waiting for GPU",
            )
        else:
            self._m_frame_extractions = None
            self._m_scene_analysis_seconds = None
            self._m_ocr_seconds = None
            self._m_gpu_queue_depth = None

        # Wire event bus into components
        self.frame_extractor._event_bus = self.event_bus
        self.ocr._event_bus = self.event_bus

        # Surface key tuning knobs at startup so production operators can
        # confirm the runtime configuration from a single log line.
        logger.info(
            "eyes.runtime_config",
            default_processing_profile=self.cfg.default_processing_profile,
            multimodal_max_scenes=self.cfg.multimodal_max_scenes,
            fast_max_scenes=self.cfg.fast_max_scenes,
            frame_diff_threshold=self.cfg.frame_diff_threshold,
            max_fps_extract=self.cfg.max_fps_extract,
            scene_boundary_threshold=self.cfg.scene_boundary_threshold,
            chunk_threshold_seconds=self.cfg.chunk_threshold_seconds,
            chunk_duration_seconds=self.cfg.chunk_duration_seconds,
            chunk_concurrency=self.cfg.chunk_concurrency,
            gpu_concurrency=self.cfg.gpu_concurrency,
            ocr_gpu=self.cfg.ocr_gpu,
            ocr_max_workers=self.cfg.ocr_max_workers,
        )

        # Load models
        await self.ocr.load()
        await self.visual_analyzer.load_model()

        # Report component modes to health endpoint
        self.health.set_mode("ocr", self.ocr.describe_mode())
        self.health.set_mode(
            "visual_analyzer",
            self.visual_analyzer.describe_mode()
            if self.visual_analyzer.is_real
            else "heuristic",
        )

        # Check cv2 availability
        try:
            import cv2
            self.health.set_mode(
                "frame_extraction", f"opencv {cv2.__version__}"
            )
        except ImportError:
            logger.warning(
                "eyes: cv2 (OpenCV) not available — frame extraction will use stub"
            )
            self.health.set_mode("frame_extraction", "stub (no cv2)")

        # Check ffmpeg/ffprobe availability for video processing
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        if ffmpeg_path and ffprobe_path:
            self.health.set_mode("video_processing", "ffmpeg")
            logger.info("eyes.ffmpeg_ready", ffmpeg=ffmpeg_path, ffprobe=ffprobe_path)
        else:
            missing = [b for b, p in [("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)] if not p]
            self.health.set_mode("video_processing", f"degraded (missing: {', '.join(missing)})")
            logger.error("eyes.ffmpeg_missing", missing=missing)

        # ── Orphaned job recovery ──────────────────────────────
        # Jobs stuck in processing/queued from a previous container lifecycle
        # have no in-memory worker — mark them failed so the orchestrator can
        # retry via its idempotency/dispatch-dedup logic.
        # Also clear any idempotency keys pointing to orphaned jobs so
        # orchestrator retries create fresh jobs instead of reusing dead ones.
        try:
            orphaned = 0
            all_jobs = await self.job_store.list_jobs(limit=500)
            orphaned_ids = set()
            for job in all_jobs:
                if job.get("status") in ("processing", "queued"):
                    await self.job_store.update_job(
                        job["job_id"],
                        status="failed",
                        error="Engine restarted during processing — job orphaned. Orchestrator will retry.",
                    )
                    orphaned_ids.add(job["job_id"])
                    orphaned += 1
            # Clear idempotency keys that point to orphaned jobs
            if orphaned_ids:
                redis = getattr(self.job_store, '_redis', None)
                if redis:
                    idem_keys = []
                    async for key in redis.scan_iter('nexus:idem:eyes:*'):
                        val = await redis.get(key)
                        if val in orphaned_ids:
                            idem_keys.append(key)
                    for key in idem_keys:
                        await redis.delete(key)
                    if idem_keys:
                        logger.warning(
                            "eyes.orphaned_idempotency_cleared",
                            count=len(idem_keys),
                        )
            if orphaned:
                logger.warning("eyes.orphaned_jobs_recovered", count=orphaned)
        except Exception:
            logger.warning("eyes.orphaned_job_scan_failed", exc_info=True)

        # ── GPU Job Queue (Redis Streams) ──────────────────────
        await self.init_worker_queue()
        if self.is_worker_mode and self._job_queue and self._job_queue.is_connected:
            self.start_worker_loop(
                self._process_queued_job,
                gpu_semaphore=None,  # _analyze_scenes manages gpu_semaphore internally
            )
            logger.info(
                "eyes.worker_loop_started",
                mode=self.engine_mode,
                consumer=self._job_queue._config.consumer_name,
            )

        if self._job_queue and self._job_queue.is_connected:
            self.register_queue_routes(self.app)

        # ── Canonical workflow workers (Phase 1) ───────────────
        # Two long-running loops: one for eyes.cpu (extract_frames /
        # build_evidence) and one for eyes.gpu (analyze_scenes). The
        # orchestrator URL gates whether these start — when unset (dev
        # without an orchestrator) the legacy /api/v1/eyes path is the
        # only ingress.
        self._workflow_workers: list = []
        orchestrator_url = os.environ.get("NEXUS_ORCHESTRATOR_URL", "")
        if orchestrator_url:
            await self._start_canonical_workflow_workers(orchestrator_url)
        else:
            logger.info(
                "eyes.workflow_workers_disabled "
                "reason=NEXUS_ORCHESTRATOR_URL_unset",
            )

    async def _start_canonical_workflow_workers(self, orchestrator_url: str) -> None:
        from nexus_sdk.workflows import (
            StepKind, WorkerConfig, WorkflowWorker, queue_name,
        )
        from app.workflow_handlers import EyesWorkflowHandlers

        token = os.environ.get("NEXUS_WORKER_TOKEN", "")
        handlers = EyesWorkflowHandlers(self)

        # CPU lanes can soak multiple workflows in parallel while one
        # is stalled on a GPU/IO step. GPU lanes stay at 1 because the
        # engine's internal PriorityGPUSemaphore serializes anyway —
        # higher worker concurrency there just adds context-switching.
        cpu_conc = int(os.environ.get("EYES_WORKER_CONCURRENCY_CPU", "4"))
        gpu_conc = int(os.environ.get("EYES_WORKER_CONCURRENCY_GPU", "1"))
        for kind in (StepKind.CPU, StepKind.GPU):
            lane = queue_name("eyes", kind)
            q = self._build_workflow_lane_queue(lane)
            ok = await q.connect()
            if not ok:
                logger.error("eyes.workflow_worker_redis_unreachable lane=%s", lane)
                continue
            worker = WorkflowWorker(
                config=WorkerConfig(
                    engine_name="eyes",
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
                "eyes.workflow_worker_started lane=%s orchestrator=%s",
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
        """Process a video analysis job claimed from the Redis Streams queue."""
        payload = job.get("payload", {})
        if isinstance(payload, str):
            import json as _json
            payload = _json.loads(payload)

        await self._process_video(
            job_id=payload["job_id"],
            video_path=payload["video_path"],
            session_id=payload["session_id"],
            tenant_id=payload["tenant_id"],
            processing_profile=payload.get("processing_profile", "fast"),
        )

    async def on_shutdown(self):
        """Gracefully stop the worker loop."""
        await self.stop_worker_loop()

    def register_routes(self, app):

        # ── Analyze Video Recording ────────────────────────────

        @app.post("/api/v1/eyes/analyze-video", response_model=JobResponse)
        async def analyze_video(
            background_tasks: BackgroundTasks,
            video: UploadFile = File(
                ..., description="Screen recording (MP4/WebM)"
            ),
            tenant_id: str = Form(...),
            session_id: str = Form(
                default_factory=lambda: str(uuid.uuid4())
            ),
            processing_profile: str = Form(default="fast"),
            user: NexusUser = Depends(get_current_user),
            x_idempotency_key: Optional[str] = Header(default=None),
        ):
            """Upload a screen recording for visual analysis.

            Phase 1.6: If X-Idempotency-Key header is provided and a job
            already exists for that key, returns the existing job immediately.
            """
            # Phase 1.6: Idempotency dedup
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
                                engine="eyes",
                                engine_version="0.2.0",
                                job_id=existing_job_id,
                                status=JobStatus(existing_status),
                                trace_id=existing_job_id,
                            )

            normalized_profile = self._normalize_processing_profile(
                processing_profile
            )

            job_id = str(uuid.uuid4())

            # Working copy on disk (ffmpeg requires a real path). Cleared
            # after the job either completes or fails.
            video_dir = (
                Path(self.cfg.frames_storage_path) / tenant_id / session_id
            )
            video_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(video.filename or "video.mp4").suffix or ".mp4"
            video_path = video_dir / f"{job_id}{ext}"

            content = await video.read()
            video_path.write_bytes(content)

            # Durable copy in object storage. This is the canonical source
            # used for replay / reprocessing; the pod-local file is ephemeral.
            try:
                video_key = self._artifacts.build_key(
                    tenant_id, "eyes", session_id, "video", f"{job_id}{ext}"
                )
                await self._artifacts.upload_file(
                    video_key, video_path, content_type="video/mp4",
                    metadata={"job_id": job_id, "original": video.filename or ""},
                )
            except Exception as e:
                logger.error(
                    "eyes.video_upload_failed",
                    job_id=job_id, error=str(e), exc_info=True,
                )
                raise HTTPException(
                    status_code=503, detail="artifact store unavailable",
                )

            await self.job_store.set_job(job_id, {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "type": "video",
                "processing_profile": normalized_profile,
                "original_filename": video.filename or "",
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
                    "video_path": str(video_path),
                    "session_id": session_id,
                    "tenant_id": tenant_id,
                    "processing_profile": normalized_profile,
                },
            )
            if not enqueued:
                background_tasks.add_task(
                    self._process_video,
                    job_id,
                    str(video_path),
                    session_id,
                    tenant_id,
                    normalized_profile,
                )

            # Phase 1.6: Store idempotency key → job_id mapping
            if x_idempotency_key:
                await self._set_idempotency_job(x_idempotency_key, job_id)

            return JobResponse(
                success=True,
                engine="eyes",
                engine_version="0.2.0",
                job_id=job_id,
                status=JobStatus.QUEUED,
                trace_id=job_id,
            )

        # ── Analyze Single Screenshot ──────────────────────────

        @app.post("/api/v1/eyes/analyze-screenshot")
        async def analyze_screenshot(
            screenshot: UploadFile = File(
                ..., description="Screenshot (PNG/JPG)"
            ),
            tenant_id: str = Form(...),
            session_id: str = Form(
                default_factory=lambda: str(uuid.uuid4())
            ),
            user: NexusUser = Depends(get_current_user),
        ):
            """Analyze a single screenshot immediately (synchronous)."""
            start = time.monotonic()

            # Working copy for OCR + vision providers (need a real path).
            temp_dir = (
                Path(self.cfg.frames_storage_path) / tenant_id / "screenshots"
            )
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = Path(screenshot.filename or "screenshot.png").name
            img_name = f"{uuid.uuid4()}_{safe_name}"
            img_path = temp_dir / img_name

            content = await screenshot.read()
            img_path.write_bytes(content)

            # Durable copy in object storage — addressable via the asset
            # URL helper from the client side.
            try:
                screenshot_key = self._artifacts.build_key(
                    tenant_id, "eyes", session_id, "screenshots", img_name,
                )
                await self._artifacts.upload_file(
                    screenshot_key, img_path,
                )
            except Exception as e:
                logger.warning(
                    "eyes.screenshot_upload_failed",
                    error=str(e),
                )
                screenshot_key = ""

            # OCR
            async with self._gpu_semaphore.acquire(GPU_PRIORITY_FAST):
                extracted_text, text_regions, ocr_conf = await asyncio.to_thread(
                    self.ocr.extract_text, str(img_path)
                )

            # Classify application
            app_type = self.app_classifier.classify(extracted_text)

            # Analyze
            async with self._gpu_semaphore.acquire(GPU_PRIORITY_DEEP):
                analysis = await self.visual_analyzer.analyze_frame(
                    str(img_path), extracted_text, app_type,
                    processing_profile="deep",
                )

            elapsed_ms = (time.monotonic() - start) * 1000

            _storage_root = Path(self.cfg.frames_storage_path).resolve()
            try:
                _frame_asset_path = str(
                    Path(str(img_path)).resolve().relative_to(_storage_root)
                )
            except ValueError:
                _frame_asset_path = ""

            frame = FrameAnalysis(
                frame_id=str(uuid.uuid4()),
                frame_index=0,
                timestamp_seconds=0.0,
                application_type=app_type,
                page_title=_analysis_text(analysis.get("page_title", "")),
                url_or_path=_address_bar_url(text_regions),
                ui_elements=_analysis_list(analysis.get("ui_elements", [])),
                extracted_text=extracted_text,
                tables=_analysis_tables(analysis.get("tables", [])),
                description=_analysis_text(analysis.get("description", "")),
                frame_path=str(img_path),
                frame_asset_path=_frame_asset_path,
                ocr_confidence=ocr_conf,
                is_keyframe=True,
            )

            return {
                "success": True,
                "processing_time_ms": round(elapsed_ms, 2),
                "frame": frame.model_dump(mode="json"),
            }

        # ── Get Job Status ─────────────────────────────────────

        @app.get("/api/v1/eyes/jobs/{job_id}")
        async def get_job(
            job_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            job = await self.job_store.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return job

        # ── Get Session Frames ─────────────────────────────────

        @app.get("/api/v1/eyes/sessions/{session_id}/frames")
        async def get_session_frames(
            session_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get all analyzed frames for a KT session."""
            all_jobs = await self.job_store.list_jobs(limit=500)
            for job in all_jobs:
                if (
                    job.get("session_id") == session_id
                    and job.get("status") == JobStatus.COMPLETED.value
                    and job.get("result")
                ):
                    return job.get("result")

            raise HTTPException(
                status_code=404,
                detail="No visual analysis found for session",
            )

        # ── Serve Frame Image ──────────────────────────────────

        @app.get("/api/v1/eyes/frames/{tenant_id}/{session_id}/{file_path:path}")
        async def serve_frame_image(
            tenant_id: str,
            session_id: str,
            file_path: str,
            user: NexusUser = Depends(_get_frame_user),
        ):
            """Serve a single extracted frame PNG/JPG by its asset path.

            Source of truth is the ArtifactStore:
              - cloud backends   → 307 redirect to a presigned URL
              - local backend    → stream from working dir (dev only)

            Security guarantees:
            - JWT required: unauthenticated requests are rejected before this handler
            - Tenant isolation: user.tenant_id must match the tenant_id path segment
            - Extension allowlist: only .png / .jpg / .jpeg are served
            - Segment validation: rejects '..' and absolute components before
              any storage call so a presigned URL cannot be minted for a
              traversed path
            """
            if user.tenant_id != tenant_id:
                raise HTTPException(status_code=403, detail="Access denied")

            _suffix = Path(file_path).suffix.lower()
            if _suffix not in {".png", ".jpg", ".jpeg"}:
                raise HTTPException(status_code=403, detail="Access denied")

            # Reject traversal in any segment before we trust the path.
            for segment in file_path.replace("\\", "/").split("/"):
                if not segment or segment == "." or segment == ".." or segment.startswith("/"):
                    raise HTTPException(status_code=403, detail="Access denied")

            try:
                key = self._artifacts.build_key(
                    tenant_id, "eyes", session_id, file_path,
                )
            except ValueError:
                raise HTTPException(status_code=403, detail="Access denied")

            _media_type = "image/png" if _suffix == ".png" else "image/jpeg"

            # Cloud backend: redirect to a short-lived presigned URL so the
            # engine pod never proxies the bytes (stateless, cache-friendly).
            if not self._artifacts.is_local:
                if not await self._artifacts.exists(key):
                    raise HTTPException(status_code=404, detail="Frame not found")
                presigned = await self._artifacts.presign(key)
                return RedirectResponse(presigned, status_code=307)

            # Local backend (dev): keep the same path-traversal guard against
            # the working dir and stream from disk.
            _storage_root = Path(self.cfg.frames_storage_path).resolve()
            _full_path = (
                _storage_root / tenant_id / session_id / file_path
            ).resolve()
            if not _full_path.is_relative_to(_storage_root):
                raise HTTPException(status_code=403, detail="Access denied")
            if not _full_path.is_file():
                # Fall back to a stream from the artifact store if the working
                # copy was already GC'd in this pod.
                if await self._artifacts.exists(key):
                    return StreamingResponse(
                        self._artifacts.stream(key),
                        media_type=_media_type,
                        headers={"Cache-Control": "private, max-age=3600"},
                    )
                raise HTTPException(status_code=404, detail="Frame not found")
            return FileResponse(
                str(_full_path),
                media_type=_media_type,
                headers={"Cache-Control": "private, max-age=3600"},
            )

    # ───────────────────────────────────────────────────────────
    # Phase 1.6: Idempotency helpers
    # ───────────────────────────────────────────────────────────

    async def _get_idempotency_job(self, idem_key: str) -> Optional[str]:
        """Look up a previously dispatched job by idempotency key."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                return await redis.get(f"nexus:idem:eyes:{idem_key}")
            except Exception:
                pass
        return None

    async def _set_idempotency_job(self, idem_key: str, job_id: str) -> None:
        """Store idempotency key → job_id mapping (24h TTL)."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                await redis.setex(f"nexus:idem:eyes:{idem_key}", 86400, job_id)
            except Exception:
                pass

    async def _del_idempotency_job(self, idem_key: str) -> None:
        """Remove a stale idempotency mapping (e.g. for failed jobs)."""
        redis = getattr(self.job_store, '_redis', None)
        if redis:
            try:
                await redis.delete(f"nexus:idem:eyes:{idem_key}")
            except Exception:
                pass

    # ───────────────────────────────────────────────────────────
    # Stateless engine helpers — artifact upload + working-dir GC
    # ───────────────────────────────────────────────────────────

    async def _upload_frames_to_artifact_store(
        self,
        tenant_id: str,
        session_id: str,
        job_id: str,
        frames: list,
    ) -> None:
        """Upload every analyzed frame file to object storage.

        Best-effort: a per-frame failure does not abort the job; the frame
        keeps its working-dir path so the local fallback in serve_frame_image
        can still return bytes. The artifact-store path becomes the canonical
        reference for cross-pod / cross-restart access.
        """
        if not frames:
            return
        sem = asyncio.Semaphore(max(1, self.cfg.frame_upload_concurrency))
        delete_after = self.cfg.frames_delete_after_upload and not self._artifacts.is_local

        async def _one(frame) -> None:
            local_path = getattr(frame, "frame_path", "") or ""
            asset_path = getattr(frame, "frame_asset_path", "") or ""
            if not local_path or not asset_path:
                return
            p = Path(local_path)
            if not p.is_file():
                return
            try:
                key = self._artifacts.build_key(
                    tenant_id, "eyes", session_id, asset_path,
                )
            except ValueError:
                logger.warning(
                    "eyes.frame_asset_invalid",
                    job_id=job_id, frame_asset_path=asset_path,
                )
                return
            async with sem:
                try:
                    await self._artifacts.upload_file(key, p)
                except Exception as e:
                    logger.warning(
                        "eyes.frame_upload_failed",
                        job_id=job_id, key=key, error=str(e),
                    )
                    return
            if delete_after:
                try:
                    await asyncio.to_thread(p.unlink)
                except Exception:
                    pass

        await asyncio.gather(*(_one(f) for f in frames), return_exceptions=True)
        logger.info(
            "eyes.frames_uploaded",
            job_id=job_id, count=len(frames),
            backend=self._artifacts.backend_name,
            deleted_locally=delete_after,
        )

    # ───────────────────────────────────────────────────────────
    # Video Processing Pipeline — Scene-Based with Chunking
    # ───────────────────────────────────────────────────────────

    async def _process_video(
        self,
        job_id: str,
        video_path: str,
        session_id: str,
        tenant_id: str,
        processing_profile: str,
    ):
        """Background: full video analysis pipeline.

        Pipeline:
          1. Probe video duration → decide chunk vs single
          2. Extract frames (Hamming distance dedup + adaptive sampling)
          3. Batch OCR all frames (CPU-bound, no GPU semaphore)
          4. Group frames into scenes by dHash proximity
          5. One LLaVA call per scene (GPU semaphore), propagate to all frames
          6. Build result, emit event
        """
        start = time.monotonic()

        try:
            await self.job_store.update_job(
                job_id,
                status=JobStatus.PROCESSING.value,
                current_stage="probing",
                progress_percent=2.0,
            )

            # Probe video duration for chunking decision
            video_meta = await probe_video(video_path)
            duration_s = video_meta.get("duration_seconds", 0.0)

            if (
                duration_s > self.cfg.chunk_threshold_seconds
                and duration_s > 0
            ):
                result = await self._process_video_chunked(
                    job_id,
                    video_path,
                    session_id,
                    tenant_id,
                    duration_s,
                    start,
                    processing_profile,
                )
            else:
                result = await self._process_video_single(
                    job_id,
                    video_path,
                    session_id,
                    tenant_id,
                    start,
                    processing_profile,
                )

            # Store result
            elapsed = time.monotonic() - start
            result.processing_time_seconds = round(elapsed, 2)
            result.compute_stats()

            await self.job_store.update_job(
                job_id,
                status=JobStatus.COMPLETED.value,
                result=result.model_dump(mode="json"),
                processing_time_seconds=round(elapsed, 2),
                current_stage="completed",
                progress_percent=100.0,
            )

            if self.event_bus:
                await self.event_bus.publish(NexusEvent(
                    event_type="eyes.analysis.completed",
                    tenant_id=tenant_id,
                    trace_id=job_id,
                    engine="eyes",
                    session_id=session_id,
                    data={
                        "job_id": job_id,
                        "session_id": session_id,
                        "frame_count": len(result.frames),
                        "application_types": result.application_types_seen,
                        "pipeline_stages": result.pipeline_stages,
                        "chunked": duration_s > self.cfg.chunk_threshold_seconds,
                        "processing_profile": processing_profile,
                    },
                ))

            logger.info(
                "eyes.analysis_complete",
                job_id=job_id,
                session_id=session_id,
                frames=len(result.frames),
                app_types=result.application_types_seen,
                elapsed_s=round(elapsed, 2),
                processing_profile=processing_profile,
                stages=result.pipeline_stages,
            )

        except Exception as e:
            logger.error(
                "eyes.analysis_failed",
                job_id=job_id,
                error=str(e),
                exc_info=True,
            )
            await self.job_store.update_job(
                job_id,
                status=JobStatus.FAILED.value,
                error=str(e),
                current_stage="failed",
            )
            if self.event_bus:
                await self.event_bus.publish(NexusEvent(
                    event_type="eyes.analysis.failed",
                    tenant_id=tenant_id,
                    trace_id=job_id,
                    engine="eyes",
                    session_id=session_id,
                    data={"job_id": job_id, "error": str(e)},
                ))
        finally:
            # Working-dir GC: durable copies are in the artifact store. Skip
            # in local-backend mode so dev users can browse frames directly.
            if (
                self.cfg.frames_delete_after_upload
                and not self._artifacts.is_local
            ):
                await self._cleanup_working_dir(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    video_path=video_path,
                )

    async def _cleanup_working_dir(
        self,
        tenant_id: str,
        session_id: str,
        video_path: str,
    ) -> None:
        """Remove the local video copy and any per-job frame subdirs."""
        try:
            vp = Path(video_path)
            if vp.is_file():
                await asyncio.to_thread(vp.unlink)
            # Frame subdir convention from _process_video_single
            frames_subdir = vp.parent / f"{vp.stem}_frames"
            if frames_subdir.is_dir():
                await asyncio.to_thread(shutil.rmtree, frames_subdir, True)
        except Exception as e:
            logger.warning(
                "eyes.working_dir_gc_failed",
                tenant_id=tenant_id,
                session_id=session_id,
                error=str(e),
            )

    # ── Single-segment pipeline ────────────────────────────────

    async def _process_video_single(
        self,
        job_id: str,
        video_path: str,
        session_id: str,
        tenant_id: str,
        start: float,
        processing_profile: str,
    ) -> VisualAnalysisResult:
        """Process one video segment through the scene-based pipeline."""
        pipeline_stages: list[str] = []

        # ── Stage 1: Extract Frames ────────────────────────────
        await self.job_store.update_job(
            job_id,
            current_stage="extracting",
            progress_percent=5.0,
        )
        frames_dir = str(Path(video_path).parent / f"{job_id}_frames")
        extracted_frames = await self.frame_extractor.extract_frames(
            video_path, frames_dir
        )
        pipeline_stages.append("extract")
        total_frames_extracted = len(extracted_frames)
        # Pass ALL extracted frames to scene detection. dHash grouping
        # deduplicates near-identical frames in O(n) — pre-capping by uniform
        # spacing discards real screen transitions that happen to fall between
        # evenly-spaced sample points.  Scene cap (for Ollama budget) is
        # applied AFTER grouping, so every distinct UI state is preserved.
        raw_frames = extracted_frames
        total_frames = len(raw_frames)

        if total_frames == 0:
            return VisualAnalysisResult(
                job_id=job_id,
                session_id=session_id,
                tenant_id=tenant_id,
                frames=[],
                total_frames_extracted=total_frames_extracted,
                processing_time_seconds=0.0,
                pipeline_stages=pipeline_stages,
                scene_transitions=[],
            )

        # ── Stage 2: Group into Scenes ─────────────────────────
        await self.job_store.update_job(
            job_id,
            current_stage="scene_grouping",
            progress_percent=25.0,
        )
        placeholder_ocr_results = [("", [], 0.0) for _ in raw_frames]
        all_scenes = self._group_into_scenes(raw_frames, placeholder_ocr_results)
        # Enrichment cap: select which scenes receive expensive LLaVA analysis.
        # ALL scenes (and ALL their frames) are preserved for Spine persistence.
        # Profile controls how many scenes get vision-model enrichment, NOT how
        # many scenes reach the DB.  This decouples step coverage from analysis
        # cost — the root fix for "missing steps" complaints on fast-mode uploads.
        scenes_for_enrichment = self._select_scenes_for_profile(all_scenes, processing_profile)
        enrichment_cap_applied = len(scenes_for_enrichment) < len(all_scenes)
        if enrichment_cap_applied:
            pipeline_stages.append("enrichment_cap")
            logger.info(
                "eyes.enrichment_cap_applied",
                job_id=job_id,
                processing_profile=processing_profile,
                total_scenes=len(all_scenes),
                enriched_scenes=len(scenes_for_enrichment),
            )
        pipeline_stages.append("scene_group")
        # Use all_scenes for OCR (CPU-only, cheap) and for final frame output.
        # Use scenes_for_enrichment only for the GPU LLaVA calls below.
        scenes = all_scenes

        # ── Stage 3: OCR (CPU-bound, profile-aware) ───────────
        await self.job_store.update_job(
            job_id,
            current_stage="ocr",
            progress_percent=15.0,
        )
        if processing_profile == "fast" and self.cfg.fast_skip_ocr:
            ocr_results = [("", [], 0.0) for _ in raw_frames]
            for scene in scenes:
                scene["representative_ocr"] = ("", [], 0.0)
                scene["merged_ocr_text"] = ""
            pipeline_stages.append("ocr_skipped")
            logger.warning(
                "eyes.ocr_skipped_fast_mode",
                job_id=job_id,
                frame_count=len(raw_frames),
                scene_count=len(scenes),
                memory_mb=self._memory_usage_mb(),
            )
        elif processing_profile == "fast" and self.cfg.fast_representative_ocr_only:
            ocr_results = await self._batch_ocr_representative_frames(
                job_id,
                raw_frames,
                scenes,
            )
            pipeline_stages.append("ocr_representative")
        elif processing_profile == "multimodal":
            # Multimodal: by default OCR only the representative frame per
            # scene (cheap on CPU).  When per-frame OCR is enabled, also OCR
            # every non-rep frame inside multi-frame scenes so the step
            # extractor can see intra-scene text changes (form fills,
            # dropdown selects).  Single-frame scenes remain rep-only either
            # way — there is nothing to diff.
            if self.cfg.per_frame_ocr_in_multi_frame_scenes:
                ocr_results = await self._batch_ocr_representative_frames(
                    job_id,
                    raw_frames,
                    scenes,
                )
                # Identify multi-frame scenes that need per-frame OCR.
                # **Smart dedup**: in a 9-frame form-fill scene, the
                # representative frame's OCR already covers the empty form;
                # the only frames worth re-OCRing are the ones whose dHash
                # differs from frames we've already OCR'd. Force-keep frames
                # captured during the user's typing/selection register the
                # filled state with distinct dHash and DO get OCR'd; near-
                # duplicates of the rep frame are skipped, saving most of
                # the per-frame OCR cost on long videos.
                indices_needing_ocr: list[int] = []
                rep_indices = {scene["representative_idx"] for scene in scenes}
                # dHash bit-fraction below which two frames are considered
                # near-duplicates (skip OCR). Same threshold band as the
                # frame extractor's diff filter but slightly looser so we
                # catch form-fill state changes (~1.5% bits typically).
                _DEDUP_THRESHOLD = 0.015
                for scene in scenes:
                    if len(scene.get("frame_indices", [])) < 3:
                        continue
                    rep_idx = scene.get("representative_idx")
                    ocr_anchor_hashes: list[str] = []
                    if rep_idx is not None and rep_idx < len(raw_frames):
                        rh = raw_frames[rep_idx].get("hash") or ""
                        if rh:
                            ocr_anchor_hashes.append(rh)
                    for gi in scene["frame_indices"]:
                        if gi in rep_indices or gi >= len(ocr_results):
                            continue
                        gh = raw_frames[gi].get("hash") or ""
                        # Skip frames too similar to any frame we've already
                        # decided to OCR (or the rep frame).
                        if gh and ocr_anchor_hashes:
                            distances = [
                                FrameExtractor._hamming_distance(gh, h)
                                for h in ocr_anchor_hashes if h
                            ]
                            if distances and min(distances) < _DEDUP_THRESHOLD:
                                continue
                        indices_needing_ocr.append(gi)
                        if gh:
                            ocr_anchor_hashes.append(gh)
                if indices_needing_ocr:
                    extra = await self._batch_ocr(
                        job_id, [raw_frames[i] for i in indices_needing_ocr],
                    )
                    for slot, gi in enumerate(indices_needing_ocr):
                        if slot < len(extra):
                            ocr_results[gi] = extra[slot]
                    # Recompute merged_ocr_text per multi-frame scene to
                    # include the new per-frame readings.
                    for scene in scenes:
                        if len(scene.get("frame_indices", [])) >= 3:
                            seen: set[str] = set()
                            parts: list[str] = []
                            for idx in scene["frame_indices"]:
                                if idx >= len(ocr_results):
                                    continue
                                for line in (ocr_results[idx][0] or "").split("\n"):
                                    s = line.strip()
                                    if s and s not in seen:
                                        seen.add(s)
                                        parts.append(s)
                            scene["merged_ocr_text"] = " ".join(parts)
                    logger.info(
                        "eyes.per_frame_ocr.applied",
                        job_id=job_id,
                        extra_frames_ocr=len(indices_needing_ocr),
                        multi_frame_scene_count=sum(
                            1 for s in scenes if len(s.get("frame_indices", [])) >= 3
                        ),
                    )
                pipeline_stages.append("ocr_representative+per_frame_in_multi")
            else:
                ocr_results = await self._batch_ocr_representative_frames(
                    job_id,
                    raw_frames,
                    scenes,
                )
                pipeline_stages.append("ocr_representative")
        else:
            ocr_results = await self._batch_ocr(job_id, raw_frames)
            # Rebuild scene OCR data now that real results are available
            for scene in scenes:
                rep_idx = scene["representative_idx"]
                scene["representative_ocr"] = ocr_results[rep_idx]
                seen: set[str] = set()
                parts: list[str] = []
                for idx in scene["frame_indices"]:
                    for line in ocr_results[idx][0].split("\n"):
                        s = line.strip()
                        if s and s not in seen:
                            seen.add(s)
                            parts.append(s)
                scene["merged_ocr_text"] = " ".join(parts)
            pipeline_stages.append("ocr")

        logger.info(
            "eyes.scenes_grouped",
            job_id=job_id,
            total_frames=total_frames,
            scene_count=len(scenes),
            enriched_scene_count=len(scenes_for_enrichment),
        )

        # ── Stage 4: Analyze Scenes (1 LLaVA per enrichment scene) ────
        # LLaVA runs only on scenes_for_enrichment (respects profile GPU budget).
        # _analyze_scenes returns FrameAnalysis objects for ALL scenes in `scenes`
        # (all_scenes), propagating enriched descriptions to non-enriched scenes
        # from the nearest enriched neighbour so every frame has some metadata.
        await self.job_store.update_job(
            job_id,
            current_stage="analyzing_scenes",
            progress_percent=30.0,
        )
        analyzed_frames = await self._analyze_scenes(
            job_id,
            scenes,
            raw_frames,
            ocr_results,
            total_frames,
            pipeline_stages,
            processing_profile,
            scenes_for_enrichment=scenes_for_enrichment,
        )

        # ── Populate frame_asset_path (relative to storage root) ──
        # This relative path is serialised into the Eyes result payload and
        # consumed by canonical orchestrator / client to construct HTTP URLs.
        # Eyes never embeds a hostname — callers build the absolute URL.
        _storage_root = Path(self.cfg.frames_storage_path).resolve()
        for _frame in analyzed_frames:
            if _frame.frame_path and not _frame.frame_asset_path:
                try:
                    _frame.frame_asset_path = str(
                        Path(_frame.frame_path).resolve().relative_to(_storage_root)
                    )
                except ValueError:
                    # frame_path is outside the storage root — leave empty
                    logger.warning(
                        "eyes.frame_asset_path_outside_root",
                        job_id=job_id,
                        frame_path=_frame.frame_path,
                        storage_root=str(_storage_root),
                    )

        # Determine which vision model was used based on processing profile
        active_vision_model = (
            self.visual_analyzer.fast_ollama_model
            if processing_profile == "fast"
            else self.visual_analyzer.ollama_model
        )

        # ── Visual Quality Metric (logging only) ──────────────
        total_ui_elements = sum(
            len(fa.ui_elements) for fa in analyzed_frames
        )
        if total_ui_elements == 0 and len(analyzed_frames) > 0:
            logger.warning(
                "eyes.visual_quality.low",
                job_id=job_id,
                processing_profile=processing_profile,
                frames_analyzed=len(analyzed_frames),
                total_ui_elements=0,
            )
        pipeline_stages.append(f"quality:{total_ui_elements}")

        scene_transitions = await self._analyze_scene_transitions_llm(
            job_id,
            scenes,
            analyzed_frames,
            processing_profile,
            pipeline_stages,
        )

        # ── Persist frames to object storage (stateless engine pattern) ──
        # Done AFTER transition LLM analysis (which still reads the on-disk
        # frame files); once uploaded, working-dir copies may be reclaimed.
        await self._upload_frames_to_artifact_store(
            tenant_id=tenant_id,
            session_id=session_id,
            job_id=job_id,
            frames=analyzed_frames,
        )
        pipeline_stages.append("artifact_upload")

        # Phase F.2 — UI element persistence across frames.
        # Annotate every analyzed frame's ui_elements with a stable
        # entity_id so downstream consumers (cursor → control linking,
        # selector resolution, dictionary lookup) treat repeated
        # detections of the same control as one entity.  Operates in
        # frame_index order so adjacency-based matching works.  Pure
        # post-processing — no LLM calls, no DB.
        try:
            from nexus_sdk.elements import ElementTracker
            element_tracker = ElementTracker()
            for fa in sorted(
                analyzed_frames, key=lambda f: int(getattr(f, "frame_index", 0)),
            ):
                # Convert UIElement objects to plain dicts for the tracker,
                # then re-annotate the original objects with entity_id +
                # persistence_count via element.properties.
                el_dicts = []
                for el in (fa.ui_elements or []):
                    if hasattr(el, "model_dump"):
                        el_dicts.append(el.model_dump())
                    elif isinstance(el, dict):
                        el_dicts.append(dict(el))
                    else:
                        el_dicts.append({
                            "element_type": getattr(el, "element_type", ""),
                            "text": getattr(el, "text", ""),
                            "bbox": getattr(el, "bbox", None) or getattr(el, "location", None),
                        })
                annotated = element_tracker.assign_entities(
                    el_dicts, frame_index=int(getattr(fa, "frame_index", 0)),
                )
                # Push entity_id back onto the element objects via their
                # properties dict so consumers see it without a schema change.
                for original, ann in zip(fa.ui_elements or [], annotated):
                    props = getattr(original, "properties", None)
                    if not isinstance(props, dict):
                        props = {}
                    props.setdefault("entity_id", ann.get("entity_id"))
                    props.setdefault("persistence_count", ann.get("persistence_count", 1))
                    if hasattr(original, "properties"):
                        try:
                            original.properties = props
                        except Exception:
                            pass
            pipeline_stages.append("element_persistence")
        except ImportError:  # pragma: no cover — sdk should always have it
            pass
        except Exception as exc:
            logger.warning(
                "eyes.element_tracker_failed", error=str(exc)[:200],
            )

        # Long-video memory hygiene: drop interim buffers whose data has now
        # been promoted into the FrameAnalysis records.  raw_frames / ocr
        # results / scene_transitions hold the bulk of in-memory state for
        # multi-thousand-frame artifacts; releasing them here makes room
        # for the orchestrator's downstream serialization.  No-op on
        # short videos because the watchdog only fires when memory is
        # above the configured ceiling.
        self._evict_buffers(
            raw_frames=raw_frames,
            ocr_results=ocr_results if isinstance(ocr_results, list) else None,
            scenes=scenes,
            reason="post_scene_analysis",
        )

        result = VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=analyzed_frames,
            total_frames_extracted=total_frames_extracted,
            processing_time_seconds=round(time.monotonic() - start, 2),
            pipeline_stages=pipeline_stages,
            model_version=active_vision_model,
            scene_transitions=scene_transitions,
        )

        return result

    # ─── Phase 5-A: per-stage building blocks ───────────────────────
    # Each stage method is a thin, pure-ish wrapper around the
    # already-existing private methods used by `_process_video_single`.
    # The new canonical-workflow handlers call these directly so each
    # stage is independently retryable.
    #
    # The legacy `_process_video_single` REST path is unchanged: it
    # still calls the private methods (frame_extractor.extract_frames,
    # _group_into_scenes, etc.) in its own body. There IS some logic
    # duplication; a follow-up PR will collapse `_process_video_single`
    # onto these stage methods. The duplication is the price of
    # NOT touching the legacy REST path during the cutover.

    async def _stage_extract_frames(
        self,
        video_path: str,
        job_id: str,
        processing_profile: str,
    ) -> dict:
        """Stage 1 / 6: ffmpeg + hash + dedup. Pure CPU.

        Returns a dict shaped for the FramesManifest. The handler is
        responsible for uploading each frame to the artifact store
        and assembling the actual FramesManifest object.
        """
        from app.frame_diff import probe_video

        video_meta = await probe_video(video_path)
        duration_s = float(video_meta.get("duration_seconds") or 0.0)
        fps = float(video_meta.get("fps") or 0.0)

        frames_dir = str(Path(video_path).parent / f"{job_id}_frames")
        extracted = await self.frame_extractor.extract_frames(
            video_path, frames_dir,
        )
        return {
            "frames": extracted,
            "frames_dir": frames_dir,
            "duration_seconds": duration_s,
            "fps": fps,
            "total_frames_extracted": len(extracted),
            "stages": ["extract"],
        }

    def _stage_detect_scenes(
        self,
        raw_frames: list[dict],
        processing_profile: str,
    ) -> dict:
        """Stage 2 / 6: dHash similarity grouping. Pure CPU.

        Returns the same scene dict shape the legacy path uses, but
        with OCR fields explicitly None — those get populated by the
        `ocr_frames` stage.
        """
        if not raw_frames:
            return {
                "scenes": [],
                "enrichment_scene_ids": [],
                "stages": ["scene_group_empty"],
            }
        placeholder_ocr = [("", [], 0.0) for _ in raw_frames]
        all_scenes = self._group_into_scenes(raw_frames, placeholder_ocr)
        for scene in all_scenes:
            scene["representative_ocr"] = None
            scene["merged_ocr_text"] = None

        scenes_for_enrichment = self._select_scenes_for_profile(
            all_scenes, processing_profile,
        )
        enrichment_ids = [
            s.get("scene_id") or f"sc-{i}"
            for i, s in enumerate(scenes_for_enrichment)
        ]
        # Backfill scene_id if the grouper didn't assign one.
        for i, scene in enumerate(all_scenes):
            scene.setdefault("scene_id", f"sc-{i}")

        return {
            "scenes": all_scenes,
            "enrichment_scene_ids": enrichment_ids,
            "stages": [
                "scene_group",
                "enrichment_cap"
                if len(scenes_for_enrichment) < len(all_scenes)
                else "scene_group_full",
            ],
        }

    async def _stage_ocr_frames(
        self,
        job_id: str,
        raw_frames: list[dict],
        scenes: list[dict],
        processing_profile: str,
    ) -> dict:
        """Stage 3 / 6: OCR per profile. Pure CPU.

        Mutates `scenes` in place to fill `representative_ocr` and
        `merged_ocr_text` — the handler must persist the updated
        scenes alongside the OCR results.
        """
        stages: list[str] = []
        if processing_profile == "fast" and self.cfg.fast_skip_ocr:
            ocr_results = [("", [], 0.0) for _ in raw_frames]
            for scene in scenes:
                scene["representative_ocr"] = ("", [], 0.0)
                scene["merged_ocr_text"] = ""
            stages.append("ocr_skipped")
        elif processing_profile == "fast" and self.cfg.fast_representative_ocr_only:
            ocr_results = await self._batch_ocr_representative_frames(
                job_id, raw_frames, scenes,
            )
            stages.append("ocr_representative")
        elif processing_profile == "multimodal":
            ocr_results = await self._batch_ocr_representative_frames(
                job_id, raw_frames, scenes,
            )
            stages.append("ocr_representative")
        else:
            ocr_results = await self._batch_ocr(job_id, raw_frames)
            for scene in scenes:
                rep_idx = scene["representative_idx"]
                scene["representative_ocr"] = ocr_results[rep_idx]
                seen: set[str] = set()
                parts: list[str] = []
                for idx in scene["frame_indices"]:
                    for line in (ocr_results[idx][0] or "").split("\n"):
                        s = line.strip()
                        if s and s not in seen:
                            seen.add(s)
                            parts.append(s)
                scene["merged_ocr_text"] = " ".join(parts)
            stages.append("ocr")

        return {
            "ocr_results": ocr_results,
            "scenes_with_ocr": scenes,
            "skipped_frame_count": sum(
                1 for r in ocr_results if not (r[0] or "").strip()
            ),
            "stages": stages,
        }

    async def _stage_analyze_scenes(
        self,
        job_id: str,
        scenes: list[dict],
        raw_frames: list[dict],
        ocr_results: list,
        total_frames: int,
        processing_profile: str,
        enrichment_scene_ids: list[str],
    ) -> dict:
        """Stage 4 / 6: LLaVA per representative frame. GPU lane.

        Returns the analyzed_frames list (FrameAnalysis objects).
        Carries the pipeline_stages accumulator through.
        """
        stages: list[str] = []
        scenes_for_enrichment = [
            s for s in scenes
            if (s.get("scene_id") or "") in set(enrichment_scene_ids)
        ]
        if not scenes_for_enrichment:
            # Backwards-compat: if the upstream stage didn't tag IDs,
            # enrich all scenes — same shape as the legacy path.
            scenes_for_enrichment = scenes

        # Telemetry sink for circuit-breaker / per-scene failure counts.
        # `_analyze_scenes` writes into this dict; the handler reads it
        # to decide whether to set degraded_stages=["analyze_scenes"]
        # on the workflow checkpoint.
        telemetry: dict = {}
        analyzed_frames = await self._analyze_scenes(
            job_id,
            scenes,
            raw_frames,
            ocr_results,
            total_frames,
            stages,
            processing_profile,
            scenes_for_enrichment=scenes_for_enrichment,
            telemetry=telemetry,
        )

        active_vision_model = (
            self.visual_analyzer.fast_ollama_model
            if processing_profile == "fast"
            else self.visual_analyzer.ollama_model
        )
        return {
            "analyzed_frames": analyzed_frames,
            "active_vision_model": active_vision_model,
            "stages": stages,
            "telemetry": telemetry,
        }

    async def _stage_analyze_transitions(
        self,
        job_id: str,
        scenes: list[dict],
        analyzed_frames: list,
        processing_profile: str,
    ) -> dict:
        """Stage 5 / 6: second LLM pass over scene transitions. GPU lane."""
        stages: list[str] = []
        transitions = await self._analyze_scene_transitions_llm(
            job_id, scenes, analyzed_frames, processing_profile, stages,
        )
        return {
            "transitions": transitions,
            "stages": stages,
        }

    async def _stage_build_evidence(
        self,
        job_id: str,
        session_id: str,
        tenant_id: str,
        analyzed_frames: list,
        transitions: list,
        active_vision_model: str,
        total_frames_extracted: int,
        processing_profile: str,
        accumulated_stages: list[str],
        elapsed_seconds: float,
    ) -> VisualAnalysisResult:
        """Stage 6 / 6: ElementTracker post-processing + assemble result.

        Pure CPU. Caller is responsible for emitting events + writing
        to the job store.
        """
        # Populate frame_asset_path so consumers can build URLs.
        _storage_root = Path(self.cfg.frames_storage_path).resolve()
        for _frame in analyzed_frames:
            if _frame.frame_path and not _frame.frame_asset_path:
                try:
                    _frame.frame_asset_path = str(
                        Path(_frame.frame_path).resolve().relative_to(_storage_root)
                    )
                except ValueError:
                    pass

        # Element tracker post-processing: stable entity_id across frames.
        try:
            from nexus_sdk.elements import ElementTracker
            tracker = ElementTracker()
            for fa in sorted(
                analyzed_frames, key=lambda f: int(getattr(f, "frame_index", 0)),
            ):
                el_dicts = []
                for el in (fa.ui_elements or []):
                    if hasattr(el, "model_dump"):
                        el_dicts.append(el.model_dump())
                    elif isinstance(el, dict):
                        el_dicts.append(dict(el))
                    else:
                        el_dicts.append({
                            "element_type": getattr(el, "element_type", ""),
                            "text": getattr(el, "text", ""),
                            "bbox": getattr(el, "bbox", None)
                            or getattr(el, "location", None),
                        })
                annotated = tracker.assign_entities(
                    el_dicts, frame_index=int(getattr(fa, "frame_index", 0)),
                )
                for original, ann in zip(fa.ui_elements or [], annotated):
                    props = getattr(original, "properties", None)
                    if not isinstance(props, dict):
                        props = {}
                    props.setdefault("entity_id", ann.get("entity_id"))
                    props.setdefault(
                        "persistence_count", ann.get("persistence_count", 1),
                    )
                    if hasattr(original, "properties"):
                        try:
                            original.properties = props
                        except Exception:
                            pass
            accumulated_stages.append("element_persistence")
        except ImportError:
            pass
        except Exception as exc:
            logger.warning(
                "eyes.element_tracker_failed", error=str(exc)[:200],
            )

        result = VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=analyzed_frames,
            total_frames_extracted=total_frames_extracted,
            processing_time_seconds=round(elapsed_seconds, 2),
            pipeline_stages=list(accumulated_stages),
            model_version=active_vision_model,
            scene_transitions=transitions,
        )
        result.compute_stats()
        return result

    def _normalize_processing_profile(self, processing_profile: str | None) -> str:
        profile = (processing_profile or self.cfg.default_processing_profile).strip().lower()
        if profile in {"deep", "full"}:
            return "deep"
        if profile == "multimodal":
            return "multimodal"
        return "fast"

    def _select_frames_for_profile(
        self,
        frames: list[dict],
        processing_profile: str,
    ) -> list[dict]:
        if processing_profile == "fast":
            max_frames = max(1, self.cfg.fast_max_frames)
        elif processing_profile == "multimodal":
            max_frames = max(1, self.cfg.multimodal_max_frames)
        else:
            return frames

        if len(frames) <= max_frames:
            return frames

        if max_frames == 1:
            return [frames[0]]

        last_index = len(frames) - 1
        selected_indices = {
            round(last_index * idx / (max_frames - 1))
            for idx in range(max_frames)
        }
        return [frames[idx] for idx in sorted(selected_indices)]

    def _select_scenes_for_profile(
        self,
        scenes: list[dict],
        processing_profile: str,
    ) -> list[dict]:
        if processing_profile == "fast":
            max_scenes = max(1, self.cfg.fast_max_scenes)
        elif processing_profile == "multimodal":
            max_scenes = max(1, self.cfg.multimodal_max_scenes)
        else:
            return scenes

        if len(scenes) <= max_scenes:
            return scenes

        if max_scenes == 1:
            return [scenes[0]]

        last_index = len(scenes) - 1
        selected_indices = {
            round(last_index * idx / (max_scenes - 1))
            for idx in range(max_scenes)
        }
        return [scenes[idx] for idx in sorted(selected_indices)]

    # ── Batch OCR ──────────────────────────────────────────────

    async def _batch_ocr(
        self,
        job_id: str,
        frames: list[dict],
    ) -> list[tuple[str, list[dict], float]]:
        """Run OCR on all frames. EasyOCR is CPU-bound — no GPU semaphore.

        Phase 1 hotfix: per-frame timeout (`EYES_OCR_FRAME_TIMEOUT_SECONDS`,
        default 90s). A hung EasyOCR call no longer blocks the whole
        batch — the offending frame gets a `("", [], 0.0)` placeholder
        and the batch continues. Without this, a single bad PNG could
        burn the entire per-step deadline and quarantine the workflow.
        """
        if not frames:
            return []

        worker_count = max(1, min(self.cfg.ocr_max_workers, len(frames)))
        semaphore = asyncio.Semaphore(worker_count)
        completed_count = [0]  # mutable counter for per-frame progress updates
        timeout_count = [0]    # observability: how many frames timed out

        # Per-frame timeout. Override via env for slow hosts (Tesseract +
        # large slides legitimately take 60-90s on weak CPUs).
        frame_timeout_s = float(
            os.environ.get("EYES_OCR_FRAME_TIMEOUT_SECONDS", "90.0"),
        )

        logger.info(
            "eyes.ocr_batch_start",
            job_id=job_id,
            frame_count=len(frames),
            worker_count=worker_count,
            frame_timeout_s=frame_timeout_s,
            memory_mb=self._memory_usage_mb(),
        )

        async def run_single(frame_idx: int, frame_info: dict) -> tuple[str, list[dict], float]:
            async with semaphore:
                started = time.monotonic()
                memory_before = self._memory_usage_mb()
                logger.info(
                    "eyes.ocr_frame_start",
                    job_id=job_id,
                    frame_index=frame_idx,
                    frame_path=frame_info.get("frame_path"),
                    memory_mb=memory_before,
                )
                try:
                    if self._ocr_pool is not None:
                        # Architect P0 #2: process-isolated path. The
                        # pool's `extract` enforces the timeout via
                        # process kill — no leaked threads on hang.
                        # Returns placeholder, never raises.
                        result = await self._ocr_pool.extract(
                            frame_info["frame_path"],
                        )
                        if result == ("", [], 0.0):
                            timeout_count[0] += 1
                            # Prometheus + log here (pool logs at
                            # debug; the orchestrator's alert needs
                            # the counter increment).
                            try:
                                from nexus_sdk.workflows.metrics import get_workflow_metrics
                                get_workflow_metrics().record_ocr_frame_timeout()
                            except Exception:
                                pass
                    else:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.ocr.extract_text,
                                frame_info["frame_path"],
                            ),
                            timeout=frame_timeout_s,
                        )
                except asyncio.TimeoutError:
                    timeout_count[0] += 1
                    logger.warning(
                        "eyes.ocr_frame_timeout",
                        job_id=job_id,
                        frame_index=frame_idx,
                        frame_path=frame_info.get("frame_path"),
                        elapsed_s=frame_timeout_s,
                        memory_mb=self._memory_usage_mb(),
                    )
                    # Phase 4 — emit prometheus counter so alerts can
                    # fire when OCR is hitting the wall on too many frames.
                    try:
                        from nexus_sdk.workflows.metrics import get_workflow_metrics
                        get_workflow_metrics().record_ocr_frame_timeout()
                    except Exception:
                        pass
                    # Placeholder result — empty text, zero confidence.
                    # The downstream pipeline (`_batch_ocr_representative_frames`,
                    # OCRResult, scene analysis) tolerates empty strings cleanly.
                    result = ("", [], 0.0)
                except Exception as exc:
                    logger.warning(
                        "eyes.ocr_frame_error",
                        job_id=job_id,
                        frame_index=frame_idx,
                        frame_path=frame_info.get("frame_path"),
                        error=str(exc),
                        exception_type=type(exc).__name__,
                    )
                    result = ("", [], 0.0)
                elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                logger.info(
                    "eyes.ocr_frame_complete",
                    job_id=job_id,
                    frame_index=frame_idx,
                    elapsed_ms=elapsed_ms,
                    text_chars=len(result[0]),
                    memory_mb=self._memory_usage_mb(),
                )
                # Emit incremental progress (15% → 29%) per frame to reset orchestrator stall counter
                completed_count[0] += 1
                ocr_pct = 15.0 + (completed_count[0] / len(frames)) * 14.0
                await self.job_store.update_job(
                    job_id,
                    progress_percent=round(ocr_pct, 1),
                    current_stage="ocr",
                )
                return result

        results = await asyncio.gather(
            *(run_single(frame_idx, frame_info) for frame_idx, frame_info in enumerate(frames))
        )
        if timeout_count[0]:
            logger.warning(
                "eyes.ocr_partial_success",
                job_id=job_id,
                frame_count=len(frames),
                timeout_count=timeout_count[0],
                success_pct=round(100.0 * (len(frames) - timeout_count[0]) / len(frames), 1),
            )
        logger.info(
            "eyes.ocr_batch_complete",
            job_id=job_id,
            frame_count=len(frames),
            timeout_count=timeout_count[0],
            worker_count=worker_count,
            memory_mb=self._memory_usage_mb(),
        )
        return results

    async def _batch_ocr_representative_frames(
        self,
        job_id: str,
        frames: list[dict],
        scenes: list[dict],
    ) -> list[tuple[str, list[dict], float]]:
        """Run OCR only on representative frames and propagate within each scene."""
        if not frames:
            return []

        representative_frames = [scene["representative_frame"] for scene in scenes]
        representative_results = await self._batch_ocr(job_id, representative_frames)
        representative_by_index = {
            scene["representative_idx"]: representative_results[idx]
            for idx, scene in enumerate(scenes)
        }

        ocr_results: list[tuple[str, list[dict], float]] = [("", [], 0.0) for _ in frames]
        for scene in scenes:
            representative_idx = scene["representative_idx"]
            representative_ocr = representative_by_index[representative_idx]
            merged_text = representative_ocr[0]
            for frame_index in scene["frame_indices"]:
                ocr_results[frame_index] = representative_ocr
            scene["representative_ocr"] = representative_ocr
            scene["merged_ocr_text"] = merged_text

        logger.info(
            "eyes.ocr_representative_mode",
            job_id=job_id,
            scene_count=len(scenes),
            representative_frame_count=len(representative_frames),
            propagated_frame_count=len(frames),
            memory_mb=self._memory_usage_mb(),
        )
        return ocr_results

    def _memory_usage_mb(self) -> float | None:
        status_path = "/proc/self/status"
        try:
            with open(status_path, "r", encoding="utf-8") as status_file:
                status = status_file.read()
            match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.MULTILINE)
            if not match:
                return None
            return round(int(match.group(1)) / 1024, 2)
        except OSError:
            return None

    def _under_memory_ceiling(self) -> bool:
        """True when current RSS is below the configured memory ceiling.

        A ceiling of 0 disables the check (legacy behaviour) — used in
        constrained CI environments where memory readings are unreliable.
        """
        ceiling = self.cfg.memory_ceiling_mb
        if ceiling <= 0:
            return True
        rss = self._memory_usage_mb()
        if rss is None:
            return True
        return rss < ceiling

    def _evict_buffers(
        self,
        *,
        raw_frames: list[dict] | None = None,
        ocr_results: list | None = None,
        scenes: list[dict] | None = None,
        scene_transitions: list | None = None,
        reason: str = "post_analysis",
    ) -> None:
        """Aggressively drop interim buffers held during pipeline execution.

        Called after each major stage completes so a long video does not
        keep two passes' worth of frame data resident.  Caller passes
        whichever buffers are no longer needed; this method clears them
        in place so any aliases held elsewhere also see the eviction.

        On hosts above the configured memory ceiling we additionally run a
        forced ``gc.collect()`` to return memory to the allocator promptly.
        Skipped on small videos and when the eviction flag is off.
        """
        import gc

        if not self.cfg.aggressive_frame_buffer_eviction:
            return

        # Drop image-bytes fields from frame dicts while preserving the
        # frame_path / frame_id / timestamp metadata downstream consumers
        # still need.  Image bytes are by far the largest field — a single
        # 1080p frame can be 200-500 KB even compressed.
        if raw_frames is not None:
            for f in raw_frames:
                if isinstance(f, dict):
                    f.pop("image_bytes", None)
                    f.pop("image", None)
                    f.pop("png_bytes", None)
                    f.pop("frame_array", None)

        # Clear OCR raw region tuples — keep only the merged text per
        # scene which has already been promoted into scene metadata.
        if ocr_results is not None:
            ocr_results.clear()

        # Per-scene transition pairs hold last-frame and first-frame
        # references; once edges are computed they have no further use.
        if scene_transitions is not None:
            scene_transitions.clear()

        # Scene-level OCR aggregations are heavy strings; the canonical
        # artifact already has merged_ocr_text persisted by this stage.
        if scenes is not None:
            for s in scenes:
                if isinstance(s, dict):
                    s.pop("representative_ocr", None)
                    # Keep merged_ocr_text — frontend consumes it from API

        # If we are above the ceiling, force a collection cycle.
        if not self._under_memory_ceiling():
            gc.collect()
            logger.info(
                "eyes.memory.evicted",
                reason=reason,
                memory_mb=self._memory_usage_mb(),
                ceiling_mb=self.cfg.memory_ceiling_mb,
            )

    # ── Scene Grouping ─────────────────────────────────────────

    def _group_into_scenes(
        self,
        frames: list[dict],
        ocr_results: list[tuple[str, list[dict], float]],
    ) -> list[dict]:
        """Group consecutive frames into scenes by dHash proximity.

        A new scene starts when the Hamming distance between consecutive
        frames exceeds ``scene_boundary_threshold``.
        """
        if not frames:
            return []

        threshold = self.cfg.scene_boundary_threshold
        scenes: list[dict] = []
        current_scene_indices: list[int] = [0]

        for i in range(1, len(frames)):
            prev_hash = frames[i - 1].get("hash", "")
            curr_hash = frames[i].get("hash", "")

            if prev_hash and curr_hash:
                distance = FrameExtractor._hamming_distance(curr_hash, prev_hash)
            else:
                distance = 1.0  # Force new scene if hashes missing

            if distance > threshold:
                # Boundary — close current scene
                scenes.append(
                    self._build_scene(current_scene_indices, frames, ocr_results)
                )
                current_scene_indices = [i]
            else:
                current_scene_indices.append(i)

        # Close last scene
        if current_scene_indices:
            scenes.append(
                self._build_scene(current_scene_indices, frames, ocr_results)
            )

        return scenes

    @staticmethod
    def _build_scene(
        indices: list[int],
        frames: list[dict],
        ocr_results: list[tuple[str, list[dict], float]],
    ) -> dict:
        """Build scene metadata from a group of consecutive frame indices."""
        representative_idx = indices[len(indices) // 2]  # Middle frame

        # Merge OCR text from all frames in scene (deduplicated lines)
        seen_lines: set[str] = set()
        merged_parts: list[str] = []
        for idx in indices:
            text = ocr_results[idx][0]
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped and stripped not in seen_lines:
                    seen_lines.add(stripped)
                    merged_parts.append(stripped)
        merged_ocr_text = " ".join(merged_parts)

        return {
            "frame_indices": indices,
            "representative_idx": representative_idx,
            "representative_frame": frames[representative_idx],
            "representative_ocr": ocr_results[representative_idx],
            "merged_ocr_text": merged_ocr_text,
            "start_timestamp": frames[indices[0]]["timestamp"],
            "end_timestamp": frames[indices[-1]]["timestamp"],
            "frame_count": len(indices),
        }

    # ── Scene Analysis (1 LLaVA call per scene) ───────────────

    async def _analyze_scenes(
        self,
        job_id: str,
        scenes: list[dict],
        frames: list[dict],
        ocr_results: list[tuple[str, list[dict], float]],
        total_frames: int,
        pipeline_stages: list[str],
        processing_profile: str,
        scenes_for_enrichment: list[dict] | None = None,
        telemetry: dict | None = None,
    ) -> list[FrameAnalysis]:
        """One LLaVA call per enriched scene; all scenes produce FrameAnalysis output.

        When ``scenes_for_enrichment`` is supplied (a subset of ``scenes``), only
        those scenes receive an expensive GPU LLaVA call.  Every other scene is
        still persisted but gets a lightweight description derived from OCR text
        and the nearest enriched neighbour's analysis.  This decouples scene
        *retention* (always full) from enrichment *cost* (profile-controlled).
        """
        analyzed_frames: list[FrameAnalysis] = []
        prev_description = ""

        # Build a fast-lookup set of enriched scene ids (by object id)
        enriched_set: set[int] = (
            {id(s) for s in scenes_for_enrichment}
            if scenes_for_enrichment is not None
            else {id(s) for s in scenes}  # all enriched when not capped
        )

        # Cache last enriched analysis so unenriched scenes can inherit it
        last_enriched_analysis: dict = {}

        logger.warning(
            "diag.analyze_scenes.entry",
            scene_count=len(scenes),
            enriched_count=len(enriched_set),
            scenes_for_enrichment_arg=(len(scenes_for_enrichment) if scenes_for_enrichment is not None else None),
            processing_profile=processing_profile,
        )

        # Per-frame LLaVA budget tracker — shared across all scenes in this
        # artifact.  Once exhausted, remaining non-representative frames in
        # multi-frame scenes fall back to the rep-frame analysis so the
        # pipeline degrades gracefully instead of stalling on the GPU queue.
        per_frame_llava_calls = 0
        per_frame_llava_enabled = (
            self.cfg.per_frame_llava
            and processing_profile in {"multimodal", "deep"}
        )
        # Architect followup — per-scene LLaVA timeout + circuit breaker.
        # The previous behaviour let a single hung Ollama call burn the
        # entire step deadline (600s). Now each scene has its own bounded
        # call, and after `circuit_threshold` consecutive timeouts we stop
        # calling LLaVA for the remaining scenes (cheaper to ship a
        # degraded artifact than to wait 5 more scenes × 30s each on a
        # model that's clearly down).
        per_scene_llava_timeout_s = float(os.environ.get(
            "EYES_LLAVA_PER_SCENE_TIMEOUT_S", "30",
        ))
        circuit_threshold = int(os.environ.get(
            "EYES_LLAVA_CIRCUIT_THRESHOLD", "2",
        ))
        consecutive_failures = 0
        circuit_open = False
        # Telemetry counters bubbled up to the workflow handler so the
        # checkpoint can carry degraded_stages=["analyze_scenes"] when
        # LLaVA enrichment partially or fully failed. Without this the
        # handler's outer except can't see internal circuit-breaker
        # state and the artifact would land as clean `persisted` even
        # when every scene used OCR-only.
        total_enriched_attempted = len(enriched_set)
        degraded_enriched_count = 0

        for scene_idx, scene in enumerate(scenes):
            # Progress: 30% → 95% across scenes
            progress = 30 + (65 * scene_idx / max(len(scenes), 1))
            await self.job_store.update_job(
                job_id,
                current_stage=f"scene_{scene_idx+1}/{len(scenes)}",
                progress_percent=round(progress, 1),
            )

            rep_idx = scene["representative_idx"]
            rep_frame = scene["representative_frame"]
            rep_ocr_text, _, rep_ocr_conf = scene["representative_ocr"]
            merged_text = scene["merged_ocr_text"]

            # Classify application from merged OCR (cheap, always run)
            app_type = self.app_classifier.classify(merged_text)
            if scene_idx == 0 and "classify" not in pipeline_stages:
                pipeline_stages.append("classify")

            is_enriched = id(scene) in enriched_set
            if is_enriched and not circuit_open:
                # ONE LLaVA call for the representative frame.
                # Bounded by per-scene timeout — a hang here was the
                # production failure mode that quarantined workflow
                # 887e7205. On timeout, fall through to the
                # OCR-only path the unenriched branch uses.
                _gpu_prio = GPU_PRIORITY_FAST if processing_profile == "fast" else GPU_PRIORITY_DEEP
                analysis = None
                try:
                    async with self._gpu_semaphore.acquire(_gpu_prio):
                        analysis = await asyncio.wait_for(
                            self.visual_analyzer.analyze_frame(
                                rep_frame["frame_path"],
                                merged_text,
                                app_type,
                                prev_description,
                                processing_profile,
                            ),
                            timeout=per_scene_llava_timeout_s,
                        )
                    # Successful call resets the consecutive-failure run.
                    consecutive_failures = 0
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    degraded_enriched_count += 1
                    logger.warning(
                        "eyes.llava_per_scene_timeout",
                        scene_idx=scene_idx,
                        consecutive_failures=consecutive_failures,
                        timeout_s=per_scene_llava_timeout_s,
                        job_id=job_id,
                    )
                except Exception as _exc:
                    consecutive_failures += 1
                    degraded_enriched_count += 1
                    logger.warning(
                        "eyes.llava_per_scene_failed",
                        scene_idx=scene_idx,
                        consecutive_failures=consecutive_failures,
                        err=str(_exc),
                        exception_type=type(_exc).__name__,
                        job_id=job_id,
                    )
                if consecutive_failures >= circuit_threshold:
                    circuit_open = True
                    logger.warning(
                        "eyes.llava_circuit_open",
                        threshold=circuit_threshold,
                        remaining_scenes=len(scenes) - scene_idx - 1,
                        job_id=job_id,
                    )
                    # Emit prom counter so dashboards see the trip.
                    try:
                        from nexus_sdk.workflows.metrics import get_workflow_metrics
                        get_workflow_metrics().record_vision_degraded(
                            reason="circuit_breaker_open",
                        )
                    except Exception:
                        pass
                if analysis is None:
                    # OCR-only fallback for this scene; same shape as
                    # the unenriched branch produces below.
                    ocr_summary = (merged_text or rep_ocr_text or "").strip()
                    analysis = {
                        "description": (
                            ocr_summary
                            if ocr_summary
                            else "(visual analysis unavailable for this scene)"
                        ),
                        "ui_elements": [],
                        "tables": [],
                        "page_title": "",
                    }
                if scene_idx == 0 and "analyze" not in pipeline_stages:
                    pipeline_stages.append("analyze")
                last_enriched_analysis = analysis
            elif is_enriched and circuit_open:
                # Circuit breaker open — skip LLaVA, use OCR-only.
                degraded_enriched_count += 1
                ocr_summary = (merged_text or rep_ocr_text or "").strip()
                analysis = {
                    "description": (
                        ocr_summary
                        if ocr_summary
                        else "(visual analysis skipped — vision model unavailable)"
                    ),
                    "ui_elements": [],
                    "tables": [],
                    "page_title": "",
                }
            else:
                # Unenriched scene: produce HONEST placeholder metadata for this
                # specific scene.  Previously we copied ui_elements / tables /
                # page_title from the LAST enriched scene, which polluted every
                # downstream consumer (control extractor, flow builder, UI) with
                # control labels and titles that belonged to a DIFFERENT screen.
                # That was a major source of "muddled step detail" complaints.
                #
                # Now we keep description (text content is the only thing that
                # transfers safely between visually-similar screens) but leave
                # structured fields empty so downstream stages know there is no
                # vision-grounded data for this scene and can render the scene
                # using OCR / heuristics instead of fake LLaVA output.
                ocr_summary = (merged_text or rep_ocr_text or "").strip()
                inherited_desc = last_enriched_analysis.get("description", "")
                analysis = {
                    "description": ocr_summary if ocr_summary else inherited_desc,
                    "ui_elements": [],
                    "tables": [],
                    "page_title": "",
                }

            scene_description = _analysis_text(analysis.get("description", ""))
            scene_ui_elements = _analysis_list(analysis.get("ui_elements", []))
            scene_tables = _analysis_tables(analysis.get("tables", []))
            scene_page_title = _analysis_text(analysis.get("page_title", ""))

            # Per-frame LLaVA: when enabled, every non-rep frame in a
            # multi-frame enriched scene gets its own analysis so downstream
            # consumers (step extractor, control extractor) can see what
            # changed inside the page rather than a single shared description.
            #
            # Frame-level analyses are keyed by global frame index.  The
            # representative frame's analysis is always reused; non-rep frames
            # call analyze_frame again subject to the artifact-level call
            # budget.  Failures degrade silently to the rep analysis so a
            # single GPU hiccup never zeros out the scene's enrichment.
            per_frame_analyses: dict[int, dict] = {rep_idx: analysis}
            if (
                is_enriched
                and per_frame_llava_enabled
                and len(scene["frame_indices"]) > 1
            ):
                # Per-frame analyses are INDEPENDENT — each uses the rep
                # frame's description as the fixed scene anchor (not a chain)
                # — so they run CONCURRENTLY instead of one-at-a-time.  These
                # are cloud tier-router calls that use no local GPU, so a
                # bounded asyncio.Semaphore (NOT the single-slot GPU semaphore)
                # parallelises the I/O-bound work while staying under provider
                # rate limits; local-model fallback shares one Ollama instance
                # so this cannot blow GPU memory.  Set
                # EYES_VISION_PER_FRAME_CONCURRENCY=1 to restore the previous
                # serial behaviour without a rebuild.
                _pf_prev = analysis.get("description", "") or prev_description
                _pf_candidates = [
                    gi for gi in scene["frame_indices"]
                    if gi != rep_idx and frames[gi].get("frame_path")
                ]
                _pf_remaining = max(
                    0, self.cfg.per_frame_llava_limit - per_frame_llava_calls
                )
                _pf_selected = _pf_candidates[:_pf_remaining]
                if len(_pf_selected) < len(_pf_candidates):
                    logger.info(
                        "eyes.per_frame_llava.budget_exhausted",
                        job_id=job_id,
                        scene_idx=scene_idx,
                        limit=self.cfg.per_frame_llava_limit,
                    )
                _pf_conc = max(1, int(
                    os.environ.get("EYES_VISION_PER_FRAME_CONCURRENCY", "6")
                ))
                _pf_sem = asyncio.Semaphore(_pf_conc)

                async def _enrich_frame(global_idx):
                    frame_ocr = ""
                    if global_idx < len(ocr_results) and ocr_results[global_idx]:
                        frame_ocr = ocr_results[global_idx][0] or ""
                    if not frame_ocr:
                        frame_ocr = merged_text  # rep-frame OCR is best fallback
                    try:
                        async with _pf_sem:
                            res = await self.visual_analyzer.analyze_frame(
                                frames[global_idx]["frame_path"],
                                frame_ocr,
                                app_type,
                                _pf_prev,
                                processing_profile,
                            )
                        return (global_idx, res)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "eyes.per_frame_llava.failed",
                            job_id=job_id,
                            scene_idx=scene_idx,
                            frame_index=global_idx,
                            error=str(exc),
                        )
                        return (global_idx, None)

                if _pf_selected:
                    _pf_results = await asyncio.gather(
                        *(_enrich_frame(gi) for gi in _pf_selected)
                    )
                    for _gi, _res in _pf_results:
                        if _res is not None:
                            per_frame_analyses[_gi] = _res
                            per_frame_llava_calls += 1

            # Propagate per-frame analysis to FrameAnalysis records.  Frames
            # without their own analysis (rep frame, or budget-exhausted
            # frames, or fast-mode scenes) fall back to the scene-level
            # rep analysis — preserving prior behaviour for those code paths.
            for frame_local_idx, global_idx in enumerate(scene["frame_indices"]):
                frame_info = frames[global_idx]
                ocr_text, ocr_regions, ocr_conf = ocr_results[global_idx]

                is_representative = (global_idx == rep_idx)

                frame_specific = per_frame_analyses.get(global_idx, analysis)
                f_description = _analysis_text(frame_specific.get("description", ""))
                f_ui_elements = _analysis_list(frame_specific.get("ui_elements", []))
                f_tables = _analysis_tables(frame_specific.get("tables", []))
                f_page_title = (
                    _analysis_text(frame_specific.get("page_title", ""))
                    or scene_page_title
                )

                frame_analysis = FrameAnalysis(
                    frame_id=str(uuid.uuid4()),
                    frame_index=global_idx,
                    timestamp_seconds=frame_info["timestamp"],
                    application_type=app_type,
                    page_title=f_page_title,
                    url_or_path=_address_bar_url(ocr_regions),
                    ui_elements=f_ui_elements,
                    extracted_text=ocr_text,
                    tables=f_tables,
                    description=f_description,
                    frame_path=frame_info["frame_path"],
                    ocr_confidence=ocr_conf,
                    is_keyframe=is_representative,
                )
                analyzed_frames.append(frame_analysis)

            prev_description = scene_description

        if per_frame_llava_enabled:
            logger.info(
                "eyes.per_frame_llava.summary",
                job_id=job_id,
                processing_profile=processing_profile,
                additional_calls=per_frame_llava_calls,
                limit=self.cfg.per_frame_llava_limit,
            )

        # Architect followup — write telemetry the handler reads to
        # decide whether to mark degraded_stages=["analyze_scenes"].
        # `degraded_ratio` is the fraction of intended-enriched scenes
        # that fell back to OCR-only. The handler uses a small threshold
        # (e.g. > 0.25) so a single transient failure doesn't flip the
        # whole artifact to completed_degraded — but >25% LLaVA failure
        # is unambiguous "partial enrichment".
        if telemetry is not None:
            denom = max(1, total_enriched_attempted)
            telemetry["llava_total_attempted"] = total_enriched_attempted
            telemetry["llava_degraded_count"] = degraded_enriched_count
            telemetry["llava_circuit_opened"] = bool(circuit_open)
            telemetry["llava_degraded_ratio"] = round(
                degraded_enriched_count / denom, 3,
            )

        return analyzed_frames

    async def _analyze_scene_transitions_llm(
        self,
        job_id: str,
        scenes: list[dict],
        analyzed_frames: list[FrameAnalysis],
        processing_profile: str,
        pipeline_stages: list[str],
    ) -> list[SceneTransitionAnalysis]:
        """Pair-wise transition LLM: last frame of scene N → first of scene N+1.

        Runs only when ``EYES_TRANSITION_LLM=true``, ``processing_profile`` is
        ``multimodal``, and Ollama is available.  Bounded by
        ``transition_llm_max_pairs`` with even sampling when over budget.
        """
        if not self.cfg.transition_llm_enabled:
            return []
        if processing_profile != "multimodal":
            return []
        if not self.visual_analyzer.is_real:
            return []
        if len(scenes) < 2 or not analyzed_frames:
            return []

        fa_by_idx = {fa.frame_index: fa for fa in analyzed_frames}
        pairs_raw: list[tuple[FrameAnalysis, FrameAnalysis, str, str]] = []
        for i in range(len(scenes) - 1):
            idxs_a = scenes[i].get("frame_indices") or []
            idxs_b = scenes[i + 1].get("frame_indices") or []
            if not idxs_a or not idxs_b:
                continue
            li = max(idxs_a)
            fj = min(idxs_b)
            fa = fa_by_idx.get(li)
            fb = fa_by_idx.get(fj)
            if not fa or not fb:
                continue
            if not fa.frame_path or not fb.frame_path:
                continue
            ocr_a = scenes[i].get("merged_ocr_text") or fa.extracted_text or ""
            ocr_b = scenes[i + 1].get("merged_ocr_text") or fb.extracted_text or ""
            pairs_raw.append((fa, fb, ocr_a, ocr_b))

        max_pairs = max(1, int(self.cfg.transition_llm_max_pairs))
        if len(pairs_raw) > max_pairs:
            n = len(pairs_raw)
            step = (n - 1) / (max_pairs - 1) if max_pairs > 1 else 0.0
            pick = sorted({min(n - 1, int(round(i * step))) for i in range(max_pairs)})
            pairs_raw = [pairs_raw[j] for j in pick]

        results: list[SceneTransitionAnalysis] = []
        min_conf = float(self.cfg.transition_llm_min_confidence)

        await self.job_store.update_job(
            job_id,
            current_stage="transition_llm",
            progress_percent=92.0,
        )
        _gpu_prio = GPU_PRIORITY_DEEP
        pair_no = 0
        for fa, fb, ocr_a, ocr_b in pairs_raw:
            pair_no += 1
            url_a = (fa.url_or_path or "").strip()
            url_b = (fb.url_or_path or "").strip()
            url_changed = bool(url_a and url_b and url_a != url_b)
            async with self._gpu_semaphore.acquire(_gpu_prio):
                raw = await self.visual_analyzer.analyze_transition_pair(
                    fa.frame_path,
                    fb.frame_path,
                    ocr_a,
                    ocr_b,
                    url_changed,
                    processing_profile,
                )
            if not raw:
                continue
            if float(raw.get("confidence", 0.0)) < min_conf:
                continue
            kind = (raw.get("action_kind") or "unknown").strip().lower()
            if kind == "unknown":
                continue
            results.append(
                SceneTransitionAnalysis(
                    from_frame_id=fa.frame_id,
                    to_frame_id=fb.frame_id,
                    action_kind=kind,
                    action_label=raw.get("action_label", ""),
                    target_element_label=raw.get("target_element_label", ""),
                    observed_value=raw.get("observed_value", ""),
                    confidence=float(raw.get("confidence", 0.0)),
                    evidence_text=raw.get("evidence_text", ""),
                )
            )
            await self.job_store.update_job(
                job_id,
                current_stage=f"transition_llm {pair_no}/{len(pairs_raw)}",
                progress_percent=min(94.0, 92.0 + 2.0 * pair_no / max(len(pairs_raw), 1)),
            )

        if results:
            pipeline_stages.append("transition_llm")
            logger.info(
                "eyes.transition_llm_complete",
                job_id=job_id,
                pairs=len(results),
            )
        return results

    # ───────────────────────────────────────────────────────────
    # Chunked Long-Video Processing
    # ───────────────────────────────────────────────────────────

    async def _process_video_chunked(
        self,
        job_id: str,
        video_path: str,
        session_id: str,
        tenant_id: str,
        duration_s: float,
        start: float,
        processing_profile: str,
    ) -> VisualAnalysisResult:
        """Split long video into chunks, process each, merge results."""
        chunk_dir = str(Path(video_path).parent / f"{job_id}_chunks")
        os.makedirs(chunk_dir, exist_ok=True)

        chunk_duration = self.cfg.chunk_duration_seconds

        await self.job_store.update_job(
            job_id,
            current_stage="splitting",
            progress_percent=3.0,
        )

        # Split video into chunks using ffmpeg.  ffmpeg segment is
        # deterministic for the same input + duration, so re-splitting
        # after a crash produces identical chunk_NNN.mp4 files in the
        # same chunk_dir — that's why resume can match chunk_paths[i]
        # against an existing chunk_NNN_result.json on disk.
        chunk_paths = await asyncio.to_thread(
            self._split_video_ffmpeg,
            video_path,
            chunk_dir,
            chunk_duration,
        )

        logger.info(
            "eyes.video_chunked",
            job_id=job_id,
            duration_s=round(duration_s, 1),
            chunk_count=len(chunk_paths),
            chunk_duration=chunk_duration,
        )

        if not chunk_paths:
            # ffmpeg split failed — fall back to single processing
            logger.warning(
                "eyes.chunk_fallback",
                job_id=job_id,
                reason="ffmpeg split produced no chunks",
            )
            return await self._process_video_single(
                job_id,
                video_path,
                session_id,
                tenant_id,
                start,
                processing_profile,
            )

        # ── Phase 2 — Resume from prior run ─────────────────────
        # If a previous run wrote per-chunk result JSONs under
        # ``chunk_dir`` and crashed before completion, reload them
        # and skip those chunks in this run.  Skipped chunks share
        # the same chunk_NNN.mp4 paths because ffmpeg segment naming
        # is deterministic.
        prior_results = _load_chunk_checkpoints(chunk_dir, len(chunk_paths))
        if prior_results:
            logger.info(
                "eyes.chunk_resume",
                job_id=job_id,
                resumed_chunks=sorted(prior_results.keys()),
                pending_chunks=[
                    i for i in range(len(chunk_paths)) if i not in prior_results
                ],
            )

        # Process chunks with bounded concurrency.  The internal GPU
        # semaphore (gpu_concurrency) still serialises LLaVA calls across all
        # in-flight chunks, so overlapping is safe — extract / OCR / DB work
        # for one chunk runs while another is waiting on Ollama.
        max_inflight = max(1, int(self.cfg.chunk_concurrency))
        chunk_sem = asyncio.Semaphore(max_inflight)
        chunk_results: list[VisualAnalysisResult | None] = [None] * len(chunk_paths)
        for idx, res in prior_results.items():
            chunk_results[idx] = res
        completed = [len(prior_results)]

        logger.info(
            "eyes.chunk_pipeline_started",
            job_id=job_id,
            chunk_count=len(chunk_paths),
            max_inflight=max_inflight,
            resumed=len(prior_results),
        )

        async def run_chunk(chunk_idx: int, chunk_path: str) -> None:
            # Skip chunks already loaded from a prior-run checkpoint.
            if chunk_results[chunk_idx] is not None:
                return
            async with chunk_sem:
                result = await self._process_video_single(
                    job_id=f"{job_id}_chunk{chunk_idx}",
                    video_path=chunk_path,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    start=start,
                    processing_profile=processing_profile,
                )
            chunk_results[chunk_idx] = result
            # Persist the per-chunk result so a later crash resumes here.
            # Best-effort: a write failure is logged inside the helper
            # but does not abort the chunk.
            await asyncio.to_thread(
                _save_chunk_checkpoint, chunk_dir, chunk_idx, result,
            )
            completed[0] += 1
            progress = 5 + (90 * completed[0] / len(chunk_paths))
            await self.job_store.update_job(
                job_id,
                current_stage=f"chunk_{completed[0]}/{len(chunk_paths)}",
                progress_percent=round(progress, 1),
            )

        await asyncio.gather(
            *(run_chunk(idx, path) for idx, path in enumerate(chunk_paths))
        )

        # Stitch chunks back together in order.  Index/timestamp offsets MUST
        # be applied in chunk order so frame_index is monotonic and timestamps
        # reflect the original video timeline regardless of completion order.
        all_frames: list[FrameAnalysis] = []
        all_stages: set[str] = set()
        total_extracted = 0
        frame_offset = 0
        chunk_boundary_indices: list[int] = []

        for chunk_idx, chunk_result in enumerate(chunk_results):
            if chunk_result is None:
                continue
            time_offset = chunk_idx * chunk_duration
            if chunk_idx > 0 and chunk_result.frames:
                chunk_boundary_indices.append(len(all_frames))
            for frame in chunk_result.frames:
                frame.frame_index += frame_offset
                frame.timestamp_seconds += time_offset
                all_frames.append(frame)
            frame_offset += len(chunk_result.frames)
            total_extracted += chunk_result.total_frames_extracted
            all_stages.update(chunk_result.pipeline_stages)

        # P1 Fix: Deduplicate visually similar frames at chunk boundaries.
        # When videos are split into chunks, the last frame of chunk N and
        # first frame of chunk N+1 may depict the same scene.  We compare
        # descriptions and timestamps at each boundary and drop the later
        # duplicate.
        if chunk_boundary_indices:
            drop_indices: set[int] = set()
            for boundary_idx in chunk_boundary_indices:
                if boundary_idx <= 0 or boundary_idx >= len(all_frames):
                    continue
                prev_frame = all_frames[boundary_idx - 1]
                curr_frame = all_frames[boundary_idx]
                # If timestamps are within 2 seconds and descriptions match,
                # treat the boundary frame as a duplicate
                time_gap = abs(
                    curr_frame.timestamp_seconds - prev_frame.timestamp_seconds
                )
                same_desc = (
                    prev_frame.description
                    and curr_frame.description
                    and prev_frame.description == curr_frame.description
                )
                if time_gap < 2.0 and same_desc:
                    drop_indices.add(boundary_idx)
            if drop_indices:
                all_frames = [
                    f for i, f in enumerate(all_frames) if i not in drop_indices
                ]
                # Re-index frames after dedup
                for idx, frame in enumerate(all_frames):
                    frame.frame_index = idx

        # ── Cleanup ─────────────────────────────────────────────
        # Only purge the chunk dir when every chunk produced a
        # result.  Otherwise the checkpoints stay on disk so the
        # next run can resume — better to use a few extra MB than
        # to re-process an hour of video.
        all_chunks_done = all(r is not None for r in chunk_results)
        if all_chunks_done:
            try:
                shutil.rmtree(chunk_dir, ignore_errors=True)
            except Exception:
                pass
        else:
            logger.warning(
                "eyes.chunk_partial_keep",
                job_id=job_id,
                completed=sum(1 for r in chunk_results if r is not None),
                total=len(chunk_paths),
                chunk_dir=chunk_dir,
                note="chunk dir kept so the next run can resume",
            )

        pipeline_stages = sorted(all_stages)
        if "chunk" not in pipeline_stages:
            pipeline_stages.insert(0, "chunk")
        if self.cfg.transition_llm_enabled and processing_profile.strip().lower() == "multimodal":
            logger.info(
                "eyes.transition_llm_skipped_chunked_merge",
                job_id=job_id,
                message=(
                    "transition-pair LLM is not run after chunk merge; "
                    "use non-chunked videos or single-segment processing for B1"
                ),
            )

        return VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=all_frames,
            total_frames_extracted=total_extracted,
            processing_time_seconds=round(time.monotonic() - start, 2),
            pipeline_stages=pipeline_stages,
            scene_transitions=[],
        )

    @staticmethod
    def _split_video_ffmpeg(
        video_path: str,
        output_dir: str,
        chunk_seconds: float,
    ) -> list[str]:
        """Split video into fixed-duration segments using ffmpeg.

        Uses stream copy (no re-encoding) for speed.
        Returns list of chunk file paths in order.
        """
        ext = Path(video_path).suffix or ".mp4"
        pattern = str(Path(output_dir) / f"chunk_%03d{ext}")

        cmd = [
            "ffmpeg",
            "-i", video_path,
            "-c", "copy",
            "-map", "0",
            "-segment_time", str(int(chunk_seconds)),
            "-f", "segment",
            "-reset_timestamps", "1",
            pattern,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes max for splitting
        )

        if result.returncode != 0:
            logger.error(
                "eyes.ffmpeg_split_failed",
                stderr=result.stderr[:500],
                returncode=result.returncode,
            )
            return []

        # Collect generated chunk files in order
        chunk_files = sorted(
            str(p) for p in Path(output_dir).glob(f"chunk_*{ext}")
        )
        return chunk_files


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = EyesEngine()
    engine.run()


if __name__ == "__main__":
    main()
