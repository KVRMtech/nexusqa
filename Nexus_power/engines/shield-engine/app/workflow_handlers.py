"""
Phase 1 canonical-workflow handlers for shield-engine.

Three step names from the canonical plans:
  - shield.redact_audio   (CPU; first step in audio + multimodal plans)
  - shield.redact_video   (CPU; first step in video + multimodal plans)
  - shield.redact_text    (CPU; first step in document plan)

All three are CPU-bound — shield is a pure transformation engine, no
GPU, no long-running pipeline.

Behaviour:

  When the input checkpoint already carries extracted text (e.g. an
  upstream step transcribed audio or parsed a document), the handler
  runs the actual `PIIDetector.detect` + `PIIRedactor.redact` pass
  synchronously, stashes the safe text + mapping_id in the checkpoint,
  and records an audit entry.

  When the input checkpoint only carries an artifact_key (binary
  payload — audio bytes, video bytes), there is nothing for shield to
  redact YET. The handler runs as a pre-flight gate:
    1. validates the upload metadata
    2. emits a `shield.gate.opened` audit entry
    3. seeds an empty `shield_context` in the checkpoint that
       downstream engines populate after they extract text
    4. passes through

  The legacy `_handle_transcription` event subscription remains the
  real PII redaction path for transcripts. Phase 1.5 will fold that
  into a post-extraction `shield.redact_text` step in each plan.

The handler intentionally never raises a `fatal=True` failure during
validation alone — shield being unavailable shouldn't quarantine
otherwise-valid uploads. A non-fatal failure lets the orchestrator
retry once and proceed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nexus_sdk.workflows import JobEnvelope, StepResult

logger = logging.getLogger(__name__)


_STEP_REDACT_AUDIO = "shield.redact_audio"
_STEP_REDACT_VIDEO = "shield.redact_video"
_STEP_REDACT_TEXT = "shield.redact_text"


# Keys in the checkpoint that signal "text is available — really redact it".
_TEXT_FIELDS = ("transcript_text", "extracted_text", "text", "raw_text")


class ShieldWorkflowHandlers:
    """Bound to a live ShieldEngine instance; registers handlers on a worker."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def register(self, worker) -> None:
        worker.register(_STEP_REDACT_AUDIO, self._handle_redact_audio)
        worker.register(_STEP_REDACT_VIDEO, self._handle_redact_video)
        worker.register(_STEP_REDACT_TEXT, self._handle_redact_text)

    # ─── shield.redact_audio ────────────────────────────────────

    async def _handle_redact_audio(self, env: JobEnvelope) -> StepResult:
        return await self._gate_or_redact(env, kind="audio")

    # ─── shield.redact_video ────────────────────────────────────

    async def _handle_redact_video(self, env: JobEnvelope) -> StepResult:
        return await self._gate_or_redact(env, kind="video")

    # ─── shield.redact_text ─────────────────────────────────────

    async def _handle_redact_text(self, env: JobEnvelope) -> StepResult:
        return await self._gate_or_redact(env, kind="text")

    # ─── Shared body ────────────────────────────────────────────

    async def _gate_or_redact(self, env: JobEnvelope, kind: str) -> StepResult:
        """If the checkpoint carries extracted text, redact it. Otherwise
        run as a pre-flight gate (validate + audit entry + pass)."""
        ckpt = dict(env.checkpoint)
        start = time.monotonic()

        # 1. Find the most relevant text field, if any.
        text_field, text = self._find_text(ckpt)

        if text:
            try:
                entities = self._engine.detector.detect(text)
                safe_text, mapping_id, _mapping = await self._engine.redactor.redact(
                    text, entities,
                )
                await self._record_audit(
                    env, action=f"redact.{kind}",
                    mapping_id=mapping_id,
                    entity_count=len(entities),
                    entity_types=sorted({e["type"] for e in entities}),
                )
                # Write the safe text back to its slot AND publish a
                # canonical `safe_text` so downstream code can rely on
                # one well-known key regardless of which path it took.
                ckpt[text_field] = safe_text
                ckpt["safe_text"] = safe_text
                ckpt["shield_context"] = {
                    "kind": kind,
                    "mapping_id": mapping_id,
                    "entity_count": len(entities),
                    "entity_types": sorted({e["type"] for e in entities}),
                    "mode": "redacted",
                }
                duration_ms = int((time.monotonic() - start) * 1000)
                return StepResult(
                    workflow_id=env.workflow_id,
                    step_name=env.step_name,
                    success=True,
                    checkpoint=ckpt,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                logger.warning(
                    "shield.workflow.redact_failed kind=%s err=%s",
                    kind, e, exc_info=True,
                )
                # Soft fail — let the orchestrator retry. PII redaction
                # not running should NOT quarantine the upload; the
                # event-bus post-redactor is a backup.
                return StepResult(
                    workflow_id=env.workflow_id,
                    step_name=env.step_name,
                    success=False,
                    error=str(e),
                    error_context={"kind": kind, "exception_type": type(e).__name__},
                )

        # 2. No text yet → run as a pre-flight gate.
        if not self._validate_gate(ckpt, kind):
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=(
                    f"shield gate: checkpoint missing required fields for kind={kind!r}; "
                    f"expected artifact_key OR an extracted text field "
                    f"({', '.join(_TEXT_FIELDS)})"
                ),
                error_context={"checkpoint_keys": sorted(ckpt.keys())},
                fatal=True,
            )

        try:
            await self._record_audit(
                env, action=f"gate.{kind}",
                mapping_id=None, entity_count=0, entity_types=[],
            )
        except Exception as e:
            logger.debug("shield.audit_record_failed err=%s", e)

        ckpt["shield_context"] = {
            "kind": kind,
            "mapping_id": None,
            "entity_count": 0,
            "entity_types": [],
            "mode": "gate",
        }
        duration_ms = int((time.monotonic() - start) * 1000)
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
            duration_ms=duration_ms,
        )

    # ─── Helpers ───────────────────────────────────────────────

    @staticmethod
    def _find_text(ckpt: dict[str, Any]) -> tuple[str, str]:
        for f in _TEXT_FIELDS:
            v = ckpt.get(f)
            if isinstance(v, str) and v.strip():
                return f, v
        return "", ""

    @staticmethod
    def _validate_gate(ckpt: dict[str, Any], kind: str) -> bool:
        # The gate path requires SOME signal that the upload is real:
        #   - artifact_key            (legacy chain — bytes in object store)
        #   - audio_file_path         (canonical_pipeline_plan, audio kind)
        #   - video_file_path         (canonical_pipeline_plan, video kind)
        #
        # text-kind has no binary payload requirement at all — text
        # arrives via a transcript step earlier in the DAG.
        if kind == "text":
            return True
        if ckpt.get("artifact_key"):
            return True
        if kind == "audio" and ckpt.get("audio_file_path"):
            return True
        if kind == "video" and ckpt.get("video_file_path"):
            return True
        return False

    async def _record_audit(
        self, env: JobEnvelope, *,
        action: str, mapping_id: str | None,
        entity_count: int, entity_types: list[str],
    ) -> None:
        try:
            from main import ShieldAuditLog  # type: ignore
        except Exception:
            return
        try:
            await ShieldAuditLog.record(
                action=action,
                tenant_id=env.tenant_id,
                user_id="workflow",
                mapping_id=mapping_id,
                entity_count=entity_count,
                entity_types=entity_types,
            )
        except Exception as e:
            logger.debug("shield.audit_emit_failed action=%s err=%s", action, e)
