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
from typing import Optional

from fastapi import Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Header
from pydantic import AliasChoices, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse, JobResponse, JobStatus
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent
from nexus_sdk.media.models import (
    FrameAnalysis,
    VisualAnalysisResult,
    ApplicationType,
    UIElement,
    VideoProcessingJob,
    MediaJobStatus,
)

from app.frame_diff import FrameExtractor, probe_video
from app.vision import OCREngine, ApplicationClassifier, VisualAnalyzer

import structlog
logger = structlog.get_logger()


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
    fast_representative_ocr_only: bool = Field(
        default=True,
        validation_alias="EYES_FAST_REPRESENTATIVE_OCR_ONLY",
    )
    fast_skip_ocr: bool = Field(
        default=True,
        validation_alias="EYES_FAST_SKIP_OCR",
    )

    # Multimodal processing profile (richer visual extraction for E2E analysis)
    multimodal_max_frames: int = Field(
        default=10,
        validation_alias="EYES_MULTIMODAL_MAX_FRAMES",
    )
    multimodal_max_scenes: int = Field(
        default=6,
        validation_alias="EYES_MULTIMODAL_MAX_SCENES",
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
    frame_diff_threshold: float = 0.05   # Hamming distance for frame dedup
    max_fps_extract: float = 1.0          # Base extraction rate (frames/sec)
    keyframe_only: bool = False
    adaptive_sampling: bool = True        # Adjust rate based on screen stability

    # Scene grouping
    scene_boundary_threshold: float = 0.15  # Hamming distance for new scene

    # GPU
    gpu_concurrency: int = 1              # Raise on multi-GPU setups

    # Chunked long-video processing
    chunk_threshold_seconds: float = 600.0   # Chunk videos longer than 10 min
    chunk_duration_seconds: float = 300.0    # 5-minute chunks

    # OCR
    ocr_languages: list[str] = ["en"]
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

    # Storage
    frames_storage_path: str = "./data/frames"


# ─── The Eyes Engine ───────────────────────────────────────────

class EyesEngine(NexusEngine):
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
        )
        self.app_classifier = ApplicationClassifier()
        self.ocr = OCREngine(
            languages=self.cfg.ocr_languages,
            gpu=self.cfg.ocr_gpu,
            model_dir=self.cfg.ocr_model_dir,
            allow_remote_model_bootstrap=self.cfg.ocr_allow_remote_model_bootstrap,
            load_timeout_seconds=self.cfg.ocr_load_timeout_seconds,
        )
        self.visual_analyzer = VisualAnalyzer(
            ollama_base_url=self.cfg.ollama_base_url,
            ollama_model=self.cfg.ollama_model,
            fast_ollama_model=self.cfg.fast_ollama_model,
        )

        # GPU concurrency guard — configurable for multi-GPU
        self._gpu_semaphore: asyncio.Semaphore = asyncio.Semaphore(
            self.cfg.gpu_concurrency
        )

    async def on_startup(self):
        """Load models."""
        self._gpu_semaphore = asyncio.Semaphore(self.cfg.gpu_concurrency)
        os.makedirs(self.cfg.frames_storage_path, exist_ok=True)

        # Wire event bus into components
        self.frame_extractor._event_bus = self.event_bus
        self.ocr._event_bus = self.event_bus

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

            # Save video (truncate filename to avoid Windows MAX_PATH)
            video_dir = (
                Path(self.cfg.frames_storage_path) / tenant_id / session_id
            )
            video_dir.mkdir(parents=True, exist_ok=True)
            ext = Path(video.filename or "video.mp4").suffix or ".mp4"
            video_path = video_dir / f"{job_id}{ext}"

            content = await video.read()
            video_path.write_bytes(content)

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

            # Save screenshot
            temp_dir = (
                Path(self.cfg.frames_storage_path) / tenant_id / "screenshots"
            )
            temp_dir.mkdir(parents=True, exist_ok=True)
            img_path = temp_dir / f"{uuid.uuid4()}_{screenshot.filename}"

            content = await screenshot.read()
            img_path.write_bytes(content)

            # OCR
            async with self._gpu_semaphore:
                extracted_text, text_regions, ocr_conf = await asyncio.to_thread(
                    self.ocr.extract_text, str(img_path)
                )

            # Classify application
            app_type = self.app_classifier.classify(extracted_text)

            # Analyze
            async with self._gpu_semaphore:
                analysis = await self.visual_analyzer.analyze_frame(
                    str(img_path), extracted_text, app_type,
                    processing_profile="deep",
                )

            elapsed_ms = (time.monotonic() - start) * 1000

            frame = FrameAnalysis(
                frame_id=str(uuid.uuid4()),
                frame_index=0,
                timestamp_seconds=0.0,
                application_type=app_type,
                page_title=analysis.get("page_title", ""),
                ui_elements=analysis.get("ui_elements", []),
                extracted_text=extracted_text,
                tables=analysis.get("tables", []),
                description=analysis.get("description", ""),
                frame_path=str(img_path),
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
        raw_frames = self._select_frames_for_profile(
            extracted_frames, processing_profile
        )
        if len(raw_frames) != total_frames_extracted:
            pipeline_stages.append("frame_cap")
            logger.info(
                "eyes.frame_cap_applied",
                job_id=job_id,
                processing_profile=processing_profile,
                extracted_frames=total_frames_extracted,
                selected_frames=len(raw_frames),
            )
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
            )

        # ── Stage 2: Group into Scenes ─────────────────────────
        await self.job_store.update_job(
            job_id,
            current_stage="scene_grouping",
            progress_percent=25.0,
        )
        placeholder_ocr_results = [("", [], 0.0) for _ in raw_frames]
        scenes = self._group_into_scenes(raw_frames, placeholder_ocr_results)
        original_scene_count = len(scenes)
        scenes = self._select_scenes_for_profile(scenes, processing_profile)
        if len(scenes) != original_scene_count:
            pipeline_stages.append("scene_cap")
            logger.info(
                "eyes.scene_cap_applied",
                job_id=job_id,
                processing_profile=processing_profile,
                original_scenes=original_scene_count,
                selected_scenes=len(scenes),
            )
        pipeline_stages.append("scene_group")

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
            # Multimodal always runs OCR on representative frames per scene
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
        )

        # ── Stage 4: Analyze Scenes (1 LLaVA per scene) ───────
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

        return VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=analyzed_frames,
            total_frames_extracted=total_frames_extracted,
            processing_time_seconds=round(time.monotonic() - start, 2),
            pipeline_stages=pipeline_stages,
            model_version=active_vision_model,
        )

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
        """Run OCR on all frames. EasyOCR is CPU-bound — no GPU semaphore."""
        if not frames:
            return []

        worker_count = max(1, min(self.cfg.ocr_max_workers, len(frames)))
        semaphore = asyncio.Semaphore(worker_count)

        logger.info(
            "eyes.ocr_batch_start",
            job_id=job_id,
            frame_count=len(frames),
            worker_count=worker_count,
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
                result = await asyncio.to_thread(
                    self.ocr.extract_text,
                    frame_info["frame_path"],
                )
                elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                logger.info(
                    "eyes.ocr_frame_complete",
                    job_id=job_id,
                    frame_index=frame_idx,
                    elapsed_ms=elapsed_ms,
                    text_chars=len(result[0]),
                    memory_mb=self._memory_usage_mb(),
                )
                return result

        results = await asyncio.gather(
            *(run_single(frame_idx, frame_info) for frame_idx, frame_info in enumerate(frames))
        )
        logger.info(
            "eyes.ocr_batch_complete",
            job_id=job_id,
            frame_count=len(frames),
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
    ) -> list[FrameAnalysis]:
        """One LLaVA call per scene, propagate result to all frames."""
        analyzed_frames: list[FrameAnalysis] = []
        prev_description = ""

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

            # Classify application from merged OCR
            app_type = self.app_classifier.classify(merged_text)
            if scene_idx == 0 and "classify" not in pipeline_stages:
                pipeline_stages.append("classify")

            # ONE LLaVA call for the representative frame
            async with self._gpu_semaphore:
                analysis = await self.visual_analyzer.analyze_frame(
                    rep_frame["frame_path"],
                    merged_text,
                    app_type,
                    prev_description,
                    processing_profile,
                )
            if scene_idx == 0 and "analyze" not in pipeline_stages:
                pipeline_stages.append("analyze")

            scene_description = analysis.get("description", "")
            scene_ui_elements = analysis.get("ui_elements", [])
            scene_tables = analysis.get("tables", [])
            scene_page_title = analysis.get("page_title", "")

            # Propagate analysis to ALL frames in this scene
            for frame_local_idx, global_idx in enumerate(scene["frame_indices"]):
                frame_info = frames[global_idx]
                ocr_text, _, ocr_conf = ocr_results[global_idx]

                is_representative = (global_idx == rep_idx)

                frame_analysis = FrameAnalysis(
                    frame_id=str(uuid.uuid4()),
                    frame_index=global_idx,
                    timestamp_seconds=frame_info["timestamp"],
                    application_type=app_type,
                    page_title=scene_page_title,
                    ui_elements=scene_ui_elements,
                    extracted_text=ocr_text,
                    tables=scene_tables,
                    description=scene_description,
                    frame_path=frame_info["frame_path"],
                    ocr_confidence=ocr_conf,
                    is_keyframe=is_representative,
                )
                analyzed_frames.append(frame_analysis)

            prev_description = scene_description

        return analyzed_frames

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

        # Split video into chunks using ffmpeg
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

        # Process chunks sequentially (GPU semaphore bounds LLaVA inside)
        all_frames: list[FrameAnalysis] = []
        all_stages: set[str] = set()
        total_extracted = 0
        frame_offset = 0

        for chunk_idx, chunk_path in enumerate(chunk_paths):
            progress = 5 + (90 * chunk_idx / len(chunk_paths))
            await self.job_store.update_job(
                job_id,
                current_stage=f"chunk_{chunk_idx+1}/{len(chunk_paths)}",
                progress_percent=round(progress, 1),
            )

            chunk_result = await self._process_video_single(
                job_id=f"{job_id}_chunk{chunk_idx}",
                video_path=chunk_path,
                session_id=session_id,
                tenant_id=tenant_id,
                start=start,
                processing_profile=processing_profile,
            )

            # Adjust frame indices and timestamps for chunk offset
            time_offset = chunk_idx * chunk_duration
            for frame in chunk_result.frames:
                frame.frame_index += frame_offset
                frame.timestamp_seconds += time_offset
                all_frames.append(frame)

            frame_offset += len(chunk_result.frames)
            total_extracted += chunk_result.total_frames_extracted
            all_stages.update(chunk_result.pipeline_stages)

        # Clean up chunk files
        try:
            shutil.rmtree(chunk_dir, ignore_errors=True)
        except Exception:
            pass

        pipeline_stages = sorted(all_stages)
        if "chunk" not in pipeline_stages:
            pipeline_stages.insert(0, "chunk")

        return VisualAnalysisResult(
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
            frames=all_frames,
            total_frames_extracted=total_extracted,
            processing_time_seconds=round(time.monotonic() - start, 2),
            pipeline_stages=pipeline_stages,
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
