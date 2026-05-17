"""
Phase 5-A canonical-workflow handlers for eyes-engine.

Six real per-step handlers, each one calling the matching `_stage_*`
method on EyesEngine (see ../main.py).

Flow:
  eyes.extract_frames    (CPU)  ffmpeg + dedup → FramesManifest
  eyes.detect_scenes     (CPU)  dHash grouping → ScenesManifest
  eyes.ocr_frames        (CPU)  EasyOCR        → OCRManifest
  eyes.analyze_scenes    (GPU)  LLaVA per scene → EnrichedScenesManifest
  eyes.analyze_transitions (GPU) 2nd LLM pass  → TransitionsManifest
  eyes.build_evidence    (CPU)  ElementTracker + finalize → VisualAnalysisResult

Each step's output is one manifest (JSON in the artifact store) + a
manifest-ref key in the workflow checkpoint. Frames and other binary
data live in the artifact store from the first step onwards, so each
subsequent step can run on any pod.

Large state (frame PNGs, OCR text for thousands of frames) is OFF
the checkpoint: workflow_state.checkpoint stays well under 5 KB
regardless of video length.

The legacy `_process_video_single` REST path in main.py is unchanged.
It produces identical outputs by calling the same private engine
methods (_group_into_scenes, _batch_ocr*, _analyze_scenes, etc.) that
the new `_stage_*` methods wrap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from nexus_sdk.events import NexusEvent
from nexus_sdk.workflows import JobEnvelope, StepResult

from .manifests import (
    FrameRecord, FramesManifest,
    SceneRecord, ScenesManifest,
    OCRResult, OCRManifest,
    EnrichedScene, EnrichedScenesManifest, UIElement,
    TransitionRecord, TransitionsManifest,
)

logger = logging.getLogger(__name__)


_STEP_EXTRACT = "eyes.extract_frames"
_STEP_DETECT = "eyes.detect_scenes"
_STEP_OCR = "eyes.ocr_frames"
_STEP_ANALYZE = "eyes.analyze_scenes"
_STEP_TRANSITIONS = "eyes.analyze_transitions"
_STEP_EVIDENCE = "eyes.build_evidence"


class EyesWorkflowHandlers:
    """Bound to a live EyesEngine; registers six handlers on a worker."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def register(self, worker) -> None:
        worker.register(_STEP_EXTRACT, self._handle_extract_frames)
        worker.register(_STEP_DETECT, self._handle_detect_scenes)
        worker.register(_STEP_OCR, self._handle_ocr_frames)
        worker.register(_STEP_ANALYZE, self._handle_analyze_scenes)
        worker.register(_STEP_TRANSITIONS, self._handle_analyze_transitions)
        worker.register(_STEP_EVIDENCE, self._handle_build_evidence)

    # ─── eyes.extract_frames ────────────────────────────────────

    async def _handle_extract_frames(self, env: JobEnvelope) -> StepResult:
        """ffmpeg + dedup. Uploads each unique frame to the artifact
        store and writes a FramesManifest pointer to the checkpoint."""
        ckpt = dict(env.checkpoint)
        try:
            video_path = await self._materialize_input(env, ckpt)
        except _MissingInputError as err:
            return _fatal(env, str(err), {"checkpoint_keys": sorted(ckpt.keys())})

        job_id = ckpt.get("eyes_job_id") or str(uuid.uuid4())
        ckpt["eyes_job_id"] = job_id
        ckpt["eyes_start_epoch"] = ckpt.get("eyes_start_epoch") or time.time()
        profile = env.params.get("profile", "fast")

        try:
            stage = await self._engine._stage_extract_frames(
                video_path=video_path, job_id=job_id, processing_profile=profile,
            )
        except Exception as e:
            return _fail(env, e, "extract_failed")

        # Upload each unique frame PNG to the artifact store.
        frame_records: list[FrameRecord] = []
        for f in stage["frames"]:
            frame_path = f.get("frame_path") or f.get("path")
            if not frame_path or not Path(frame_path).is_file():
                continue
            key = (
                f"eyes/{env.tenant_id}/{env.session_id}/{env.workflow_id}/"
                f"frames/{Path(frame_path).name}"
            )
            data = await asyncio.to_thread(Path(frame_path).read_bytes)
            await self._engine._artifacts.upload_bytes(key, data, content_type="image/png")
            frame_records.append(FrameRecord(
                index=int(f.get("index", len(frame_records))),
                timestamp_ms=int(float(f.get("timestamp", 0.0)) * 1000),
                artifact_key=key,
                hash=str(f.get("hash", "")),
                source_frame_idx=int(f.get("source_frame_idx", f.get("index", 0))),
                width=int(f.get("width", 0) or 0),
                height=int(f.get("height", 0) or 0),
                size_bytes=len(data),
            ))

        manifest = FramesManifest(
            job_id=job_id,
            tenant_id=env.tenant_id,
            session_id=env.session_id,
            workflow_id=env.workflow_id,
            source_video_artifact_key=ckpt.get("artifact_key") or ckpt.get("input_artifact_key", ""),
            duration_seconds=float(stage["duration_seconds"]),
            fps=float(stage["fps"]),
            frames=frame_records,
            pipeline_stages=stage["stages"],
        )
        frames_key = await self._upload_manifest(env, "frames_manifest.json", manifest)

        ckpt.update({
            "frames_manifest_key": frames_key,
            "frames_dir": stage.get("frames_dir"),
            "total_frames_extracted": stage["total_frames_extracted"],
            "frame_count": len(frame_records),
            "duration_seconds": stage["duration_seconds"],
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_eyes_step": _STEP_EXTRACT,
        })
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── eyes.detect_scenes ─────────────────────────────────────

    async def _handle_detect_scenes(self, env: JobEnvelope) -> StepResult:
        ckpt = dict(env.checkpoint)
        if "frames_manifest_key" not in ckpt:
            return _fatal(
                env, "frames_manifest_key missing; extract_frames must run first",
                {"checkpoint_keys": sorted(ckpt.keys())},
            )

        try:
            fm = await self._download_manifest(env, ckpt["frames_manifest_key"], FramesManifest)
            raw_frames = _frames_manifest_to_legacy(fm)
        except Exception as e:
            return _fail(env, e, "detect_scenes_download_failed")

        profile = env.params.get("profile", "fast")
        try:
            stage = self._engine._stage_detect_scenes(
                raw_frames=raw_frames, processing_profile=profile,
            )
        except Exception as e:
            return _fail(env, e, "detect_scenes_failed")

        scene_records: list[SceneRecord] = []
        for i, scene in enumerate(stage["scenes"]):
            scene_id = scene.get("scene_id") or f"sc-{i}"
            frame_indices = list(scene.get("frame_indices", []))
            start_ms = int(scene.get("start_ms")
                           or (raw_frames[frame_indices[0]]["timestamp"] * 1000
                               if frame_indices else 0))
            end_ms = int(scene.get("end_ms")
                         or (raw_frames[frame_indices[-1]]["timestamp"] * 1000
                             if frame_indices else 0))
            scene_records.append(SceneRecord(
                scene_id=scene_id,
                representative_frame_idx=int(scene["representative_idx"]),
                frame_indices=frame_indices,
                start_ms=start_ms,
                end_ms=end_ms,
            ))

        manifest = ScenesManifest(
            job_id=ckpt.get("eyes_job_id", ""),
            workflow_id=env.workflow_id,
            frames_manifest_key=ckpt["frames_manifest_key"],
            scenes=scene_records,
            enrichment_scene_ids=stage["enrichment_scene_ids"],
            pipeline_stages=stage["stages"],
        )
        scenes_key = await self._upload_manifest(env, "scenes_manifest.json", manifest)

        ckpt.update({
            "scenes_manifest_key": scenes_key,
            "scene_count": len(scene_records),
            "enrichment_scene_count": len(stage["enrichment_scene_ids"]),
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_eyes_step": _STEP_DETECT,
        })
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── eyes.ocr_frames ────────────────────────────────────────

    async def _handle_ocr_frames(self, env: JobEnvelope) -> StepResult:
        ckpt = dict(env.checkpoint)
        for required in ("frames_manifest_key", "scenes_manifest_key"):
            if required not in ckpt:
                return _fatal(
                    env, f"{required} missing; previous steps must run first",
                    {"checkpoint_keys": sorted(ckpt.keys())},
                )

        try:
            fm = await self._download_manifest(env, ckpt["frames_manifest_key"], FramesManifest)
            sm = await self._download_manifest(env, ckpt["scenes_manifest_key"], ScenesManifest)
            # EasyOCR needs file paths, not the placeholder dicts.
            # Materialize frames to local disk first (cheap when frames
            # are already cached from a previous worker on this pod).
            raw_frames = await self._materialize_frames_to_disk(env, fm)
            scenes = _scenes_manifest_to_legacy(sm, raw_frames=raw_frames)
        except Exception as e:
            return _fail(env, e, "ocr_download_failed")

        profile = env.params.get("profile", "fast")
        job_id = ckpt.get("eyes_job_id", "")

        try:
            stage = await self._engine._stage_ocr_frames(
                job_id=job_id, raw_frames=raw_frames, scenes=scenes,
                processing_profile=profile,
            )
        except Exception as e:
            return _fail(env, e, "ocr_failed")

        ocr_results = []
        for idx, (text, lines, conf) in enumerate(stage["ocr_results"]):
            if (text or "").strip():
                # `lines` here is the legacy `text_regions` list from
                # OCR.extract_text — each entry is a dict
                # {text, bbox, confidence}. OCRResult.lines is typed as
                # list[str] (we drop bbox + per-region confidence; the
                # frame-level `confidence` field above is the average).
                # Normalize each entry to its `text` string.
                line_strs: list[str] = []
                for entry in (lines or []):
                    if isinstance(entry, dict):
                        s = str(entry.get("text", "") or "").strip()
                        if s:
                            line_strs.append(s)
                    elif isinstance(entry, str):
                        s = entry.strip()
                        if s:
                            line_strs.append(s)
                ocr_results.append(OCRResult(
                    frame_idx=idx, text=text,
                    lines=line_strs, confidence=float(conf or 0.0),
                ))
        ocr_manifest = OCRManifest(
            job_id=job_id,
            workflow_id=env.workflow_id,
            scenes_manifest_key=ckpt["scenes_manifest_key"],
            profile=profile,
            results=ocr_results,
            skipped_frame_count=int(stage["skipped_frame_count"]),
            pipeline_stages=stage["stages"],
        )
        ocr_key = await self._upload_manifest(env, "ocr_manifest.json", ocr_manifest)

        # Re-write scenes manifest with the OCR fields filled in.
        for sr, scene in zip(sm.scenes, stage["scenes_with_ocr"]):
            rep_ocr = scene.get("representative_ocr")
            if isinstance(rep_ocr, tuple) and rep_ocr:
                sr.representative_ocr_text = str(rep_ocr[0] or "")
            sr.merged_ocr_text = scene.get("merged_ocr_text") or ""
        updated_scenes_key = await self._upload_manifest(
            env, "scenes_manifest_with_ocr.json", sm,
        )

        ckpt.update({
            "ocr_manifest_key": ocr_key,
            "scenes_manifest_key": updated_scenes_key,
            "ocr_frame_count": len(ocr_results),
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_eyes_step": _STEP_OCR,
        })
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── eyes.analyze_scenes ────────────────────────────────────

    async def _handle_analyze_scenes(self, env: JobEnvelope) -> StepResult:
        ckpt = dict(env.checkpoint)
        for required in (
            "frames_manifest_key", "scenes_manifest_key", "ocr_manifest_key",
        ):
            if required not in ckpt:
                return _fatal(
                    env, f"{required} missing; previous steps must run first",
                    {"checkpoint_keys": sorted(ckpt.keys())},
                )

        try:
            fm = await self._download_manifest(env, ckpt["frames_manifest_key"], FramesManifest)
            sm = await self._download_manifest(env, ckpt["scenes_manifest_key"], ScenesManifest)
            om = await self._download_manifest(env, ckpt["ocr_manifest_key"], OCRManifest)
            # Materialize frames to local disk for LLaVA to read.
            raw_frames = await self._materialize_frames_to_disk(env, fm)
            scenes = _scenes_manifest_to_legacy(sm, raw_frames=raw_frames)
            ocr_results = _ocr_manifest_to_legacy(om, frame_count=len(raw_frames))
        except Exception as e:
            return _fail(env, e, "analyze_download_failed")

        profile = env.params.get("profile", "fast")
        job_id = ckpt.get("eyes_job_id", "")

        # Phase 1 hotfix + architect followup — graceful degradation
        # when the vision model (LLaVA / Ollama / Anthropic tier) is
        # unreachable, hangs, or returns malformed output. Two layers:
        #
        #   - Stage-level time budget (~80% of step deadline). The
        #     previous implementation only caught raised exceptions,
        #     so a silent hang (Ollama returns no result, never raises)
        #     burned the full step deadline (600s) and quarantined on
        #     the second retry. asyncio.wait_for turns a hang into
        #     TimeoutError which the same handler treats as degraded.
        #   - Per-scene timeout + circuit breaker live inside
        #     `_stage_analyze_scenes`'s visual_analyzer — see eyes
        #     main.py for those.
        #
        # On either timeout or exception, the handler falls back to
        # OCR-only synthesis and returns success with
        # degraded_stages=["analyze_scenes"], so downstream steps
        # (`spine.update_artifact_enriched`, `backbone.canonicalize`)
        # still run and the artifact lands as `completed_degraded`.
        import os as _os
        _stage_budget_s = float(_os.environ.get(
            "EYES_ANALYZE_SCENES_STAGE_BUDGET_S", "480",
        ))
        degraded_reason: str | None = None
        try:
            stage = await asyncio.wait_for(
                self._engine._stage_analyze_scenes(
                    job_id=job_id,
                    scenes=scenes,
                    raw_frames=raw_frames,
                    ocr_results=ocr_results,
                    total_frames=len(raw_frames),
                    processing_profile=profile,
                    enrichment_scene_ids=list(sm.enrichment_scene_ids),
                ),
                timeout=_stage_budget_s,
            )
        except (asyncio.TimeoutError, Exception) as e:
            is_timeout = isinstance(e, asyncio.TimeoutError)
            if is_timeout:
                logger.warning(
                    "eyes.workflow.analyze_scenes_timeout scene_count=%d budget_s=%.0f",
                    len(scenes), _stage_budget_s,
                )
                degraded_reason = (
                    f"vision_stage_timeout: exceeded {_stage_budget_s:.0f}s budget"
                )
                _reason_label = "stage_timeout"
            else:
                logger.warning(
                    "eyes.workflow.vision_degraded scene_count=%d err=%s",
                    len(scenes), e, exc_info=True,
                )
                degraded_reason = f"vision_model_failed: {type(e).__name__}"
                _reason_label = type(e).__name__
            # Phase 4 — prometheus counter; distinguish timeout vs raise
            # so the dashboard can split "model down" from "model slow".
            try:
                from nexus_sdk.workflows.metrics import get_workflow_metrics
                get_workflow_metrics().record_vision_degraded(reason=_reason_label)
            except Exception:
                pass
            # Build a minimal stage result from OCR alone. Each scene
            # gets a synthesized FrameAnalysis with the OCR text as the
            # description. The pydantic model is strict, so field names
            # MUST match exactly: timestamp_seconds (not timestamp),
            # description (not scene_description), application_type
            # must be the enum (not None — None fails validation and
            # the entire workflow used to quarantine here instead of
            # degrading cleanly).
            from nexus_sdk.media.models import (
                FrameAnalysis as _FrameAnalysis,
                ApplicationType as _AppType,
            )
            synthetic_frames = []
            for scene in scenes:
                rep_idx = int(scene.get("representative_idx", 0))
                rep_ocr_text = (
                    (scene.get("representative_ocr") or ("", [], 0.0))[0]
                    or scene.get("merged_ocr_text", "")
                    or ""
                )
                rep_frame = scene.get("representative_frame") or {}
                synthetic_frames.append(_FrameAnalysis(
                    frame_index=rep_idx,
                    timestamp_seconds=float(
                        rep_frame.get("timestamp", 0.0) or 0.0
                    ),
                    application_type=_AppType.UNKNOWN,
                    extracted_text=rep_ocr_text,
                    description=(
                        f"(visual analysis unavailable — OCR text: {rep_ocr_text[:200]})"
                        if rep_ocr_text else
                        "(visual analysis unavailable — no OCR text on frame)"
                    ),
                    ui_elements=[],
                ))
            stage = {
                "analyzed_frames": synthetic_frames,
                "active_vision_model": "degraded:ocr_only",
                "stages": ["scene_analysis_degraded"],
            }

        # Convert FrameAnalysis objects → EnrichedScene shape, grouped
        # by scene_id (one enriched record per scene, not per frame).
        enriched_by_scene: dict[str, EnrichedScene] = {}
        for scene in scenes:
            scene_id = scene.get("scene_id") or ""
            rep_idx = int(scene.get("representative_idx", 0))
            rep_fa = next(
                (f for f in stage["analyzed_frames"]
                 if int(getattr(f, "frame_index", -1)) == rep_idx),
                None,
            )
            description = ""
            ui_elements: list[UIElement] = []
            app_type = None
            if rep_fa is not None:
                description = (
                    getattr(rep_fa, "scene_description", "")
                    or getattr(rep_fa, "description", "")
                    or ""
                )
                for el in (rep_fa.ui_elements or []):
                    ui_elements.append(_to_ui_element(el))
                app_type = getattr(rep_fa, "application_type", None)
            enriched_by_scene[scene_id] = EnrichedScene(
                scene_id=scene_id,
                representative_frame_idx=rep_idx,
                description=description,
                ui_elements=ui_elements,
                application_type=app_type,
                enrichment_model=stage["active_vision_model"],
            )

        em = EnrichedScenesManifest(
            job_id=job_id,
            workflow_id=env.workflow_id,
            ocr_manifest_key=ckpt["ocr_manifest_key"],
            enriched=list(enriched_by_scene.values()),
            skipped_scene_ids=[
                s.get("scene_id") or ""
                for s in scenes
                if (s.get("scene_id") or "") not in set(sm.enrichment_scene_ids)
            ],
            pipeline_stages=stage["stages"],
        )
        enriched_key = await self._upload_manifest(env, "enriched_scenes.json", em)

        # Stash the analyzed_frames list in artifact storage too —
        # build_evidence needs FrameAnalysis objects, not just enriched.
        analyzed_key = await self._upload_pickled_frames(env, stage["analyzed_frames"])

        ckpt.update({
            "enriched_scenes_key": enriched_key,
            "analyzed_frames_key": analyzed_key,
            "active_vision_model": stage["active_vision_model"],
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_eyes_step": _STEP_ANALYZE,
        })
        # Propagate graceful-degradation marker so spine.persist_artifact
        # can stamp the canonical_artifacts row with `degraded_stages` and
        # the UI can show the user "visual analysis unavailable for this
        # upload" instead of pretending the artifact is complete.
        #
        # Two trigger paths:
        #   1. Outer exception / stage timeout → degraded_reason set by
        #      the try/except above; the whole stage fell back to OCR.
        #   2. Internal circuit-breaker / per-scene failures — the
        #      handler succeeded, but a significant fraction of scenes
        #      used OCR-only because LLaVA timed out per-scene. Read
        #      the telemetry sink the engine fills and decide based on
        #      the degraded ratio. Threshold 25% by default — operators
        #      can tune via env.
        telemetry = stage.get("telemetry") or {}
        ratio_threshold = float(os.environ.get(
            "EYES_ANALYZE_SCENES_DEGRADED_RATIO", "0.25",
        ))
        degraded_ratio = float(telemetry.get("llava_degraded_ratio") or 0.0)
        circuit_opened = bool(telemetry.get("llava_circuit_opened"))
        if not degraded_reason and (
            circuit_opened or degraded_ratio > ratio_threshold
        ):
            degraded_reason = (
                f"vision_partial: circuit_open={circuit_opened} "
                f"degraded_ratio={degraded_ratio:.2f} "
                f"failed={telemetry.get('llava_degraded_count')}/"
                f"{telemetry.get('llava_total_attempted')}"
            )
            # Bookkeeping prom counter so dashboards can split "outer
            # stage failed" from "internal partial degradation".
            try:
                from nexus_sdk.workflows.metrics import get_workflow_metrics
                get_workflow_metrics().record_vision_degraded(
                    reason=(
                        "circuit_breaker_open"
                        if circuit_opened
                        else "partial_scenes"
                    ),
                )
            except Exception:
                pass
        if degraded_reason:
            existing = list(ckpt.get("degraded_stages") or [])
            if "analyze_scenes" not in existing:
                existing.append("analyze_scenes")
            ckpt["degraded_stages"] = existing
            ckpt["degraded_reasons"] = {
                **(ckpt.get("degraded_reasons") or {}),
                "analyze_scenes": degraded_reason,
            }
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── eyes.analyze_transitions ───────────────────────────────

    async def _handle_analyze_transitions(self, env: JobEnvelope) -> StepResult:
        ckpt = dict(env.checkpoint)
        for required in ("scenes_manifest_key", "analyzed_frames_key"):
            if required not in ckpt:
                return _fatal(
                    env, f"{required} missing; previous steps must run first",
                    {"checkpoint_keys": sorted(ckpt.keys())},
                )

        try:
            sm = await self._download_manifest(env, ckpt["scenes_manifest_key"], ScenesManifest)
            scenes = _scenes_manifest_to_legacy(sm)
            analyzed_frames = await self._download_pickled_frames(env, ckpt["analyzed_frames_key"])
        except Exception as e:
            return _fail(env, e, "transitions_download_failed")

        profile = env.params.get("profile", "fast")
        job_id = ckpt.get("eyes_job_id", "")

        try:
            stage = await self._engine._stage_analyze_transitions(
                job_id=job_id, scenes=scenes,
                analyzed_frames=analyzed_frames,
                processing_profile=profile,
            )
        except Exception as e:
            return _fail(env, e, "transitions_failed")

        transition_records: list[TransitionRecord] = []
        for t in stage["transitions"]:
            t_obj = t if isinstance(t, dict) else (t.model_dump() if hasattr(t, "model_dump") else {})
            transition_records.append(TransitionRecord(
                from_scene_id=str(t_obj.get("from_scene_id") or t_obj.get("from") or ""),
                to_scene_id=str(t_obj.get("to_scene_id") or t_obj.get("to") or ""),
                kind=str(t_obj.get("kind") or t_obj.get("transition_type") or "unknown"),
                reason=str(t_obj.get("reason") or t_obj.get("description") or ""),
                confidence=float(t_obj.get("confidence") or 0.0),
            ))
        tm = TransitionsManifest(
            job_id=job_id,
            workflow_id=env.workflow_id,
            enriched_scenes_key=ckpt.get("enriched_scenes_key", ""),
            transitions=transition_records,
            pipeline_stages=stage["stages"],
        )
        transitions_key = await self._upload_manifest(env, "transitions_manifest.json", tm)

        ckpt.update({
            "transitions_manifest_key": transitions_key,
            "transition_count": len(transition_records),
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_eyes_step": _STEP_TRANSITIONS,
        })
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── eyes.build_evidence ────────────────────────────────────

    async def _handle_build_evidence(self, env: JobEnvelope) -> StepResult:
        ckpt = dict(env.checkpoint)
        for required in (
            "analyzed_frames_key", "transitions_manifest_key",
        ):
            if required not in ckpt:
                return _fatal(
                    env, f"{required} missing; previous steps must run first",
                    {"checkpoint_keys": sorted(ckpt.keys())},
                )

        try:
            analyzed_frames = await self._download_pickled_frames(
                env, ckpt["analyzed_frames_key"],
            )
            tm = await self._download_manifest(
                env, ckpt["transitions_manifest_key"], TransitionsManifest,
            )
            transitions = [t.model_dump() for t in tm.transitions]
        except Exception as e:
            return _fail(env, e, "evidence_download_failed")

        profile = env.params.get("profile", "fast")
        job_id = ckpt.get("eyes_job_id", "")
        started_at = float(ckpt.get("eyes_start_epoch") or time.time())
        elapsed = max(0.0, time.time() - started_at)
        accumulated_stages = list(ckpt.get("pipeline_stages") or [])

        try:
            result = await self._engine._stage_build_evidence(
                job_id=job_id,
                session_id=env.session_id,
                tenant_id=env.tenant_id,
                analyzed_frames=analyzed_frames,
                transitions=transitions,
                active_vision_model=ckpt.get("active_vision_model") or "unknown",
                total_frames_extracted=int(ckpt.get("total_frames_extracted", 0)),
                processing_profile=profile,
                accumulated_stages=accumulated_stages,
                elapsed_seconds=elapsed,
            )
        except Exception as e:
            return _fail(env, e, "evidence_build_failed")

        result_payload = result.model_dump(mode="json")
        result_key = await self._upload_json(
            env, "eyes_result.json", result_payload,
        )

        # Persist into job_store so legacy REST clients can fetch the
        # result by job_id.
        try:
            from nexus_sdk.models import JobStatus
            await self._engine.job_store.set_job(job_id, {
                "job_id": job_id,
                "status": JobStatus.COMPLETED.value,
                "tenant_id": env.tenant_id,
                "session_id": env.session_id,
                "type": "video",
                "processing_profile": profile,
                "result": result_payload,
                "workflow_id": env.workflow_id,
            })
        except Exception as exc:
            logger.warning("eyes.job_store_write_failed err=%s", exc)

        # Emit the legacy event so Shield/Backbone downstream stays wired.
        if self._engine.event_bus:
            try:
                await self._engine.event_bus.publish(NexusEvent(
                    event_type="eyes.analysis.completed",
                    tenant_id=env.tenant_id,
                    trace_id=job_id,
                    engine="eyes",
                    session_id=env.session_id,
                    data={
                        "job_id": job_id,
                        "session_id": env.session_id,
                        "frame_count": len(result.frames),
                        "application_types": result.application_types_seen,
                        "pipeline_stages": result.pipeline_stages,
                        "chunked": False,
                        "processing_profile": profile,
                        "workflow_id": env.workflow_id,
                    },
                ))
            except Exception as exc:
                logger.warning("eyes.event_emit_failed err=%s", exc)

        # Canonical-pipeline plans read `eyes_result` inline from the
        # checkpoint (backbone.canonicalize, spine.persist_artifact,
        # spine.build_visual_graph, spine.persist_visual_evidence).
        # `eyes_result_key` stays for any consumer that prefers the
        # stored key — it points at the same JSON in object storage.
        ckpt.update({
            "eyes_result": result_payload,
            "eyes_result_key": result_key,
            "eyes_status": "completed",
            "current_eyes_step": _STEP_EVIDENCE,
            "frame_count": len(result.frames),
            "scene_transition_count": len(result.scene_transitions or []),
            "pipeline_stages": list(result.pipeline_stages),
        })
        return StepResult(
            workflow_id=env.workflow_id, step_name=env.step_name,
            success=True, checkpoint=ckpt,
        )

    # ─── Helpers ───────────────────────────────────────────────

    async def _materialize_input(self, env: JobEnvelope, ckpt: dict) -> str:
        # Canonical-pipeline plans use `video_file_path` (shared mount).
        # Legacy chain plans use `input_file` or `artifact_key`.
        input_file = (
            ckpt.get("input_file")
            or ckpt.get("video_file_path")
        )
        artifact_key = ckpt.get("artifact_key") or ckpt.get("input_artifact_key", "")
        if (not input_file or not Path(input_file).is_file()) and artifact_key:
            input_file = await self._download_from_store(
                env.tenant_id, env.session_id, artifact_key,
            )
        if not input_file or not Path(input_file).is_file():
            raise _MissingInputError("video file not available on this worker")
        return str(input_file)

    async def _download_from_store(
        self, tenant_id: str, session_id: str, artifact_key: str,
    ) -> str:
        artifacts = self._engine._artifacts
        local_dir = Path(self._engine.cfg.frames_storage_path) / tenant_id / session_id
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / Path(artifact_key).name
        if not local_path.is_file():
            data = await artifacts.download_bytes(artifact_key)
            await asyncio.to_thread(local_path.write_bytes, data)
        return str(local_path)

    async def _materialize_frames_to_disk(
        self, env: JobEnvelope, fm: FramesManifest,
    ) -> list[dict]:
        """Pull each frame from the artifact store to local disk so the
        in-process LLaVA + dHash logic that reads files can run."""
        artifacts = self._engine._artifacts
        local_dir = (
            Path(self._engine.cfg.frames_storage_path)
            / env.tenant_id / env.session_id / "wf" / env.workflow_id / "frames"
        )
        local_dir.mkdir(parents=True, exist_ok=True)
        raw: list[dict] = []
        for fr in fm.frames:
            local_path = local_dir / Path(fr.artifact_key).name
            if not local_path.is_file():
                data = await artifacts.download_bytes(fr.artifact_key)
                await asyncio.to_thread(local_path.write_bytes, data)
            raw.append({
                "index": fr.index,
                "timestamp": fr.timestamp_ms / 1000.0,
                "frame_path": str(local_path),
                "hash": fr.hash,
                "source_frame_idx": fr.source_frame_idx,
                "width": fr.width,
                "height": fr.height,
            })
        return raw

    async def _upload_manifest(
        self, env: JobEnvelope, suffix: str, manifest,
    ) -> str:
        key = (
            f"eyes/{env.tenant_id}/{env.session_id}/{env.workflow_id}/"
            f"manifests/{suffix}"
        )
        data = manifest.model_dump_json().encode("utf-8")
        await self._engine._artifacts.upload_bytes(
            key, data, content_type="application/json",
        )
        return key

    async def _upload_json(
        self, env: JobEnvelope, suffix: str, payload: Any,
    ) -> str:
        key = (
            f"eyes/{env.tenant_id}/{env.session_id}/{env.workflow_id}/"
            f"manifests/{suffix}"
        )
        data = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        await self._engine._artifacts.upload_bytes(
            key, data, content_type="application/json",
        )
        return key

    async def _download_manifest(
        self, env: JobEnvelope, key: str, cls,
    ):
        data = await self._engine._artifacts.download_bytes(key)
        return cls.model_validate_json(data.decode("utf-8"))

    async def _upload_pickled_frames(
        self, env: JobEnvelope, analyzed_frames: list,
    ) -> str:
        """FrameAnalysis objects are pydantic. Persist as JSON so the
        next step can reconstitute them. Each scene's UI elements +
        descriptions + frame paths are the load-bearing fields."""
        payload = [
            (fa.model_dump(mode="json") if hasattr(fa, "model_dump") else dict(fa))
            for fa in analyzed_frames
        ]
        return await self._upload_json(env, "analyzed_frames.json", payload)

    async def _download_pickled_frames(
        self, env: JobEnvelope, key: str,
    ) -> list:
        from nexus_sdk.media.models import FrameAnalysis
        data = await self._engine._artifacts.download_bytes(key)
        records = json.loads(data.decode("utf-8"))
        return [FrameAnalysis.model_validate(r) for r in records]


# ─── Plain helpers ─────────────────────────────────────────────


class _MissingInputError(Exception):
    """A required input is unavailable on this pod."""


def _merge_stages(prior: Any, new: list[str]) -> list[str]:
    out = list(prior) if isinstance(prior, list) else []
    out.extend(new or [])
    return out


def _fatal(env: JobEnvelope, msg: str, ctx: dict) -> StepResult:
    return StepResult(
        workflow_id=env.workflow_id, step_name=env.step_name,
        success=False, error=msg, error_context=ctx, fatal=True,
    )


def _fail(env: JobEnvelope, exc: Exception, label: str) -> StepResult:
    logger.error("eyes.workflow.%s err=%s", label, exc, exc_info=True)
    return StepResult(
        workflow_id=env.workflow_id, step_name=env.step_name,
        success=False, error=str(exc),
        error_context={"exception_type": type(exc).__name__, "label": label},
    )


def _to_ui_element(el) -> UIElement:
    """Best-effort convert a legacy UIElement → manifest UIElement."""
    if isinstance(el, UIElement):
        return el
    if hasattr(el, "model_dump"):
        d = el.model_dump()
    elif isinstance(el, dict):
        d = el
    else:
        d = {
            "element_type": getattr(el, "element_type", ""),
            "text": getattr(el, "text", ""),
            "bbox": getattr(el, "bbox", None) or getattr(el, "location", None),
        }
    props = d.get("properties") or {}
    return UIElement(
        element_type=str(d.get("element_type") or ""),
        text=str(d.get("text") or ""),
        bbox=d.get("bbox") or d.get("location"),
        entity_id=(props.get("entity_id") if isinstance(props, dict) else None),
        persistence_count=int(
            (props.get("persistence_count") if isinstance(props, dict) else 1) or 1
        ),
        properties=props if isinstance(props, dict) else {},
    )


def _frames_manifest_to_legacy(fm: FramesManifest) -> list[dict]:
    """Re-shape FramesManifest → the dict-list shape the legacy
    private engine methods (_group_into_scenes etc.) expect."""
    return [
        {
            "index": fr.index,
            "timestamp": fr.timestamp_ms / 1000.0,
            "frame_path": None,  # not on disk yet — analyze step materializes
            "hash": fr.hash,
            "source_frame_idx": fr.source_frame_idx,
            "width": fr.width,
            "height": fr.height,
        }
        for fr in fm.frames
    ]


def _scenes_manifest_to_legacy(
    sm: ScenesManifest,
    raw_frames: list[dict] | None = None,
) -> list[dict]:
    """Rehydrate scenes to the legacy in-memory shape.

    The legacy `_batch_ocr_representative_frames` + LLaVA analyzers
    expect each scene to carry both `representative_idx` AND the
    actual `representative_frame` object (a dict with the frame's
    on-disk path + dHash). Callers that already loaded the frames
    manifest should pass `raw_frames` so we can re-attach that
    object; callers that don't need OCR/LLaVA paths can omit it.
    """
    scenes: list[dict] = []
    for s in sm.scenes:
        rep_idx = int(s.representative_frame_idx)
        scene: dict = {
            "scene_id": s.scene_id,
            "representative_idx": rep_idx,
            "frame_indices": list(s.frame_indices),
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            # Always emit a 3-tuple shape so downstream legacy code
            # (`_analyze_scenes` does `rep_text, _, rep_conf = scene["representative_ocr"]`)
            # can unpack unconditionally. Empty text becomes ("", [], 0.0)
            # rather than None — semantically identical, but unpackable.
            "representative_ocr": (
                s.representative_ocr_text or "", [], 0.0,
            ),
            "merged_ocr_text": s.merged_ocr_text or "",
        }
        if raw_frames and 0 <= rep_idx < len(raw_frames):
            scene["representative_frame"] = raw_frames[rep_idx]
        scenes.append(scene)
    return scenes


def _ocr_manifest_to_legacy(om: OCRManifest, frame_count: int) -> list:
    """The legacy code expects a parallel list aligned to raw_frames
    (one tuple per frame). OCRManifest is sparse — fill gaps with the
    empty tuple."""
    by_idx = {r.frame_idx: (r.text, r.lines, r.confidence) for r in om.results}
    return [by_idx.get(i, ("", [], 0.0)) for i in range(frame_count)]
