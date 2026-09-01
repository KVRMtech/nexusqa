"""
Phase 6 canonical-workflow handlers for ears-engine.

Replaces the Phase 1 monolithic shortcut (`ears.transcribe` ran the entire
pipeline + `ears.diarize` was a passthrough) with four real per-step
handlers that each do honest work:

  1. ears.preprocess          (CPU)  resample/normalize/optional chunking
  2. ears.diarize             (GPU)  pyannote 3.1
  3. ears.transcribe_segments (GPU)  Whisper, fans out over chunks
  4. ears.align               (CPU)  speaker + transcript alignment + result

Each step's output is checkpoint state for the next step. Large outputs
(whisper segments — can be thousands of items on a 60-min call) are
written to the artifact store; the checkpoint carries only the storage
key, so workflow_state rows stay small. Audio files cross pod boundaries
via the same artifact store — every step starts by ensuring its inputs
are on local disk.

The legacy `_run_transcription` REST path in main.py is unchanged. It
duplicates logic with these handlers on purpose; a follow-up PR will
collapse both onto the four `_stage_*` engine methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from nexus_sdk.events import NexusEvent
from nexus_sdk.workflows import JobEnvelope, StepResult

logger = logging.getLogger(__name__)


_STEP_PREPROCESS = "ears.preprocess"
_STEP_DIARIZE = "ears.diarize"
_STEP_TRANSCRIBE = "ears.transcribe_segments"
_STEP_ALIGN = "ears.align"


class EarsWorkflowHandlers:
    """Bound to a live EarsEngine instance; registers handlers on a worker."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def register(self, worker) -> None:
        worker.register(_STEP_PREPROCESS, self._handle_preprocess)
        worker.register(_STEP_DIARIZE, self._handle_diarize)
        worker.register(_STEP_TRANSCRIBE, self._handle_transcribe_segments)
        worker.register(_STEP_ALIGN, self._handle_align)

    # ─── ears.preprocess ────────────────────────────────────────

    async def _handle_preprocess(self, env: JobEnvelope) -> StepResult:
        """Resample to 16 kHz mono, optionally apply VAD, optionally chunk
        long audio. Uploads the processed file(s) to the artifact store
        so the next step can pull them from any pod."""
        ckpt = dict(env.checkpoint)
        try:
            audio_path = await self._materialize_input(env, ckpt)
        except _MissingInputError as err:
            return _fatal(env, str(err), {"checkpoint_keys": sorted(ckpt.keys())})

        try:
            stage = await self._engine._stage_preprocess(
                audio_path=audio_path,
                session_id=env.session_id,
                tenant_id=env.tenant_id,
            )
        except Exception as e:
            return _fail(env, e, "preprocess_failed")

        # Upload processed audio + chunks to the artifact store so the
        # diarize/transcribe steps can run on a different pod.
        processed_key = await self._upload_audio(
            env, Path(stage["processed_path"]), kind="processed",
        )
        chunk_keys: list[str] = []
        for idx, cp in enumerate(stage["chunk_paths"]):
            ck = await self._upload_audio(
                env, Path(cp), kind=f"chunk-{idx:04d}",
            )
            chunk_keys.append(ck)

        ckpt.update({
            "ears_job_id": ckpt.get("ears_job_id") or str(uuid.uuid4()),
            "processed_audio_key": processed_key,
            "chunk_audio_keys": chunk_keys,
            "audio_meta": stage["audio_meta"],
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_ears_step": _STEP_PREPROCESS,
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
        )

    # ─── ears.diarize ───────────────────────────────────────────

    async def _handle_diarize(self, env: JobEnvelope) -> StepResult:
        """Run pyannote on the processed audio. Fast profile can skip and
        emit a single SPEAKER_00 segment for the whole file."""
        ckpt = dict(env.checkpoint)
        if "processed_audio_key" not in ckpt:
            return _fatal(
                env,
                "processed_audio_key missing; ears.preprocess must run first",
                {"checkpoint_keys": sorted(ckpt.keys())},
            )

        try:
            processed_path = await self._download_artifact(
                env, ckpt["processed_audio_key"], kind="processed",
            )
        except Exception as e:
            return _fail(env, e, "diarize_download_failed")

        try:
            stage = await self._engine._stage_diarize(
                processed_path=str(processed_path),
                audio_meta=ckpt.get("audio_meta", {}),
                num_speakers=ckpt.get("num_speakers") or env.params.get("num_speakers"),
                processing_profile=env.params.get("profile", "fast"),
            )
        except Exception as e:
            return _fail(env, e, "diarize_failed")

        ckpt.update({
            "speaker_segments": stage["speaker_segments"],
            "speaker_count": len({
                s.get("speaker") for s in stage["speaker_segments"] if s.get("speaker")
            }),
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_ears_step": _STEP_DIARIZE,
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
        )

    # ─── ears.transcribe_segments ───────────────────────────────

    async def _handle_transcribe_segments(self, env: JobEnvelope) -> StepResult:
        """Run Whisper. If the preprocess step produced chunks, fan out
        across them and dedupe overlap on the way back. Output can be
        thousands of segments — writes to the artifact store, not the
        checkpoint."""
        ckpt = dict(env.checkpoint)
        if "processed_audio_key" not in ckpt:
            return _fatal(
                env,
                "processed_audio_key missing; ears.preprocess must run first",
                {"checkpoint_keys": sorted(ckpt.keys())},
            )

        chunk_keys: list[str] = list(ckpt.get("chunk_audio_keys") or [])
        try:
            processed_path = await self._download_artifact(
                env, ckpt["processed_audio_key"], kind="processed",
            )
            local_chunks: list[str] = []
            for ck in chunk_keys:
                lp = await self._download_artifact(env, ck, kind="chunk")
                local_chunks.append(str(lp))
        except Exception as e:
            return _fail(env, e, "transcribe_download_failed")

        try:
            stage = await self._engine._stage_transcribe(
                processed_path=str(processed_path),
                chunk_paths=local_chunks,
                language=ckpt.get("language", env.params.get("language", "en")),
                processing_profile=env.params.get("profile", "fast"),
            )
        except Exception as e:
            return _fail(env, e, "transcribe_failed")

        # Whisper output can be huge (15-min call ≈ 1k segments × ~200B).
        # Stash in the artifact store; the checkpoint carries only the key.
        segs_key = await self._upload_json(
            env, stage["whisper_segments"], suffix="whisper_segments.json",
        )

        ckpt.update({
            "whisper_segments_key": segs_key,
            "whisper_segment_count": len(stage["whisper_segments"]),
            "active_model_size": stage["active_model_size"],
            "pipeline_stages": _merge_stages(ckpt.get("pipeline_stages"), stage["stages"]),
            "current_ears_step": _STEP_TRANSCRIBE,
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
        )

    # ─── ears.align ─────────────────────────────────────────────

    async def _handle_align(self, env: JobEnvelope) -> StepResult:
        """Combine speaker + whisper segments into the final
        TranscriptionResult. Persists the result and emits the
        ears.transcription.completed event for Shield / Backbone."""
        import time
        ckpt = dict(env.checkpoint)
        if "whisper_segments_key" not in ckpt or "speaker_segments" not in ckpt:
            return _fatal(
                env,
                "missing inputs; ears.transcribe_segments + ears.diarize must run first",
                {"checkpoint_keys": sorted(ckpt.keys())},
            )

        try:
            whisper_segments = await self._download_json(
                env, ckpt["whisper_segments_key"],
            )
        except Exception as e:
            return _fail(env, e, "align_download_failed")

        speaker_segments = list(ckpt.get("speaker_segments") or [])
        job_id = ckpt.get("ears_job_id") or str(uuid.uuid4())
        # elapsed since the preprocess step started; absent that, use 0.
        elapsed = float(ckpt.get("ears_elapsed_s") or 0.0)

        try:
            result = self._engine._stage_align(
                whisper_segments=whisper_segments,
                speaker_segments=speaker_segments,
                audio_meta=ckpt.get("audio_meta", {}),
                language=ckpt.get("language", env.params.get("language", "en")),
                active_model_size=ckpt.get("active_model_size") or "unknown",
                accumulated_stages=list(ckpt.get("pipeline_stages") or []),
                elapsed_seconds=elapsed,
                job_id=job_id,
                session_id=env.session_id,
                tenant_id=env.tenant_id,
            )
        except Exception as e:
            return _fail(env, e, "align_failed")

        result_payload = result.model_dump(mode="json")

        # Emit the same downstream event the legacy path emits so Shield
        # / Backbone wiring keeps working.
        if self._engine.event_bus:
            try:
                await self._engine.event_bus.publish(NexusEvent(
                    event_type="ears.transcription.completed",
                    tenant_id=env.tenant_id,
                    trace_id=job_id,
                    engine="ears",
                    session_id=env.session_id,
                    data={
                        "job_id": job_id,
                        "session_id": env.session_id,
                        "transcript_text": result.full_text,
                        "segment_count": result.segment_count,
                        "speaker_count": len(result.speakers),
                        "duration_seconds": result.duration_seconds,
                        "word_count": result.word_count,
                        "processing_profile": env.params.get("profile", "fast"),
                        "model_size": ckpt.get("active_model_size"),
                        "pipeline_stages": result.pipeline_stages,
                        "workflow_id": env.workflow_id,
                    },
                ))
            except Exception as exc:
                logger.warning(
                    "ears.event_emit_failed", err=str(exc)[:200],
                )

        ckpt.update({
            "ears_result": result_payload,
            "ears_status": "completed",
            "current_ears_step": _STEP_ALIGN,
            "transcript_text": result.full_text,
            "segment_count": result.segment_count,
            "speaker_count": len(result.speakers),
            "language_detected": result.language,
            "pipeline_stages": result.pipeline_stages,
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
        )

    # ─── Helpers ───────────────────────────────────────────────

    async def _materialize_input(
        self, env: JobEnvelope, ckpt: dict,
    ) -> str:
        """Resolve the input audio file path on this pod. Pulls from the
        artifact store if the prior step (shield.redact_audio) ran on a
        different pod and left only a key.

        Canonical-pipeline plans pass `audio_file_path` (a shared
        filesystem path mounted into both orchestrator + engine pods).
        Legacy chain plans pass `input_file` or `artifact_key`."""
        input_file = (
            ckpt.get("input_file")
            or ckpt.get("audio_file_path")
        )
        artifact_key = ckpt.get("artifact_key") or ckpt.get("input_artifact_key", "")
        if (not input_file or not Path(input_file).is_file()) and artifact_key:
            input_file = await self._download_from_store(
                env.tenant_id, env.session_id, artifact_key,
            )
        if not input_file or not Path(input_file).is_file():
            raise _MissingInputError("audio file not available on this worker")
        return str(input_file)

    async def _download_from_store(
        self, tenant_id: str, session_id: str, artifact_key: str,
    ) -> str:
        artifacts = self._engine._artifacts
        local_dir = (
            Path(self._engine.cfg.audio_storage_path) / tenant_id / session_id
        )
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / Path(artifact_key).name
        if not local_path.is_file():
            data = await artifacts.download_bytes(artifact_key)
            await asyncio.to_thread(local_path.write_bytes, data)
        return str(local_path)

    async def _download_artifact(
        self, env: JobEnvelope, key: str, kind: str,
    ) -> Path:
        artifacts = self._engine._artifacts
        local_dir = (
            Path(self._engine.cfg.audio_storage_path)
            / env.tenant_id / env.session_id / "wf" / kind
        )
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / Path(key).name
        if not local_path.is_file():
            data = await artifacts.download_bytes(key)
            await asyncio.to_thread(local_path.write_bytes, data)
        return local_path

    async def _upload_audio(
        self, env: JobEnvelope, local_path: Path, kind: str,
    ) -> str:
        """Upload an audio file to the artifact store; return the key."""
        artifacts = self._engine._artifacts
        key = (
            f"ears/{env.tenant_id}/{env.session_id}/{env.workflow_id}/"
            f"{kind}/{local_path.name}"
        )
        data = await asyncio.to_thread(local_path.read_bytes)
        await artifacts.upload_bytes(key, data)
        return key

    async def _upload_json(
        self, env: JobEnvelope, payload: Any, suffix: str,
    ) -> str:
        """Persist a JSON-serializable payload to the artifact store."""
        artifacts = self._engine._artifacts
        key = (
            f"ears/{env.tenant_id}/{env.session_id}/{env.workflow_id}/"
            f"manifests/{suffix}"
        )
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await artifacts.upload_bytes(key, data)
        return key

    async def _download_json(self, env: JobEnvelope, key: str) -> Any:
        artifacts = self._engine._artifacts
        data = await artifacts.download_bytes(key)
        return json.loads(data.decode("utf-8"))


# ─── Internal helpers ──────────────────────────────────────────


class _MissingInputError(Exception):
    """Step can't run because a required input is unavailable on this pod."""


def _merge_stages(prior: Any, new: list[str]) -> list[str]:
    out = list(prior) if isinstance(prior, list) else []
    out.extend(new or [])
    return out


def _fatal(env: JobEnvelope, msg: str, ctx: dict) -> StepResult:
    return StepResult(
        workflow_id=env.workflow_id,
        step_name=env.step_name,
        success=False,
        error=msg,
        error_context=ctx,
        fatal=True,
    )


def _fail(env: JobEnvelope, exc: Exception, label: str) -> StepResult:
    logger.error(
        "ears.workflow.%s err=%s", label, exc, exc_info=True,
    )
    return StepResult(
        workflow_id=env.workflow_id,
        step_name=env.step_name,
        success=False,
        error=str(exc),
        error_context={"exception_type": type(exc).__name__, "label": label},
    )
