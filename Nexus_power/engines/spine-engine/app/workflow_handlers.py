"""
Phase 1 canonical-workflow handlers for spine-engine.

The orchestrator dispatches JobEnvelopes onto `nexus:queue:spine.cpu`
(parsing is CPU-bound) and `nexus:queue:spine.gpu` (when embeddings
are wired). This module registers a handler per step in the canonical
document plan (`spine.parse`, `spine.chunk_and_embed`).

Phase 1 rollout shortcut: the first dispatched step (`spine.parse`)
runs the existing `_process_document` function end-to-end (parse +
chunk + classify) and stores the document_id + chunk/table summary
in the checkpoint. `spine.chunk_and_embed` is a pass-through —
chunking is already done inside _process_document; the embedding
step is reserved for Phase 1.5 when a dedicated embedding model is
wired (today the chunks land in the engine's in-memory dicts and
are persisted by downstream backbone canonicalization).

Inputs (checkpoint):
  - artifact_key   (str, required) — storage key the upload landed at
  - filename       (str, required) — original filename (for extension)
  - tenant_id      (str)           — also provided via env.tenant_id
  - session_id     (str, optional) — correlation id

Outputs (checkpoint after spine.parse):
  - document_id          (str)  — spine-assigned id
  - spine_job_id         (str)  — internal job tracking id
  - document_type        (str)  — detected DocumentType
  - page_count           (int)
  - chunk_count          (int)
  - table_count          (int)
  - file_hash            (str)  — content-addressed dedup hint
  - processing_time_ms   (float)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from nexus_sdk.workflows import JobEnvelope, StepResult

from . import _canonical_handlers

logger = logging.getLogger(__name__)


_STEP_PARSE = "spine.parse"
_STEP_CHUNK_EMBED = "spine.chunk_and_embed"

# Phase A — new steps that complete the canonical pipeline plan. Each
# is a workflow-plane wrapper around an existing HTTP endpoint the
# legacy chain already used. The endpoint logic is battle-tested; the
# handler reads the right slice of the checkpoint, calls the endpoint
# in-process (HTTP-loopback to localhost), and writes the right slice
# back so the next step in the DAG sees what it needs.
_STEP_PROBE_MEDIA = "spine.probe_media"
_STEP_PERSIST_VISUAL_EVIDENCE = "spine.persist_visual_evidence"
_STEP_PERSIST_ACTION_EVIDENCE = "spine.persist_action_evidence"
_STEP_BUILD_VISUAL_GRAPH = "spine.build_visual_graph"
_STEP_PERSIST_ARTIFACT = "spine.persist_artifact"           # legacy / pre-Phase-3
# Phase 3 — persist-first / enrich-async architectural pivot
_STEP_PERSIST_MINIMAL_ARTIFACT = "spine.persist_minimal_artifact"
_STEP_UPDATE_ARTIFACT_ENRICHED = "spine.update_artifact_enriched"


class SpineWorkflowHandlers:
    """Bound to a live SpineEngine instance; registers handlers on a worker."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def register(self, worker) -> None:
        """Bind every step name in the canonical spine path to its handler."""
        worker.register(_STEP_PARSE, self._handle_parse)
        worker.register(_STEP_CHUNK_EMBED, self._handle_chunk_and_embed)
        # Phase A — canonical-pipeline migration handlers. The handler
        # bodies live in _canonical_handlers.py; attach() binds them as
        # methods on this class (idempotent).
        _canonical_handlers.attach(SpineWorkflowHandlers)
        worker.register(_STEP_PROBE_MEDIA, self._handle_probe_media)
        worker.register(_STEP_PERSIST_VISUAL_EVIDENCE, self._handle_persist_visual_evidence)
        worker.register(_STEP_PERSIST_ACTION_EVIDENCE, self._handle_persist_action_evidence)
        worker.register(_STEP_BUILD_VISUAL_GRAPH, self._handle_build_visual_graph)
        worker.register(_STEP_PERSIST_ARTIFACT, self._handle_persist_artifact)
        # Phase 3 — persist-first / enrich-async handlers.
        worker.register(
            _STEP_PERSIST_MINIMAL_ARTIFACT, self._handle_persist_minimal_artifact,
        )
        worker.register(
            _STEP_UPDATE_ARTIFACT_ENRICHED, self._handle_update_artifact_enriched,
        )

    # ─── spine.parse ────────────────────────────────────────────

    async def _handle_parse(self, env: JobEnvelope) -> StepResult:
        """
        Phase 1 shortcut: download the document from the artifact store
        and run the existing _process_document pipeline (parse + chunk
        + classify) synchronously. Stash the summary in the checkpoint.
        """
        ckpt = dict(env.checkpoint)
        artifact_key = ckpt.get("artifact_key", "")
        filename = ckpt.get("filename", "unknown")

        if not artifact_key:
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error="checkpoint missing artifact_key; spine cannot fetch document",
                error_context={"checkpoint_keys": sorted(ckpt.keys())},
                fatal=True,
            )

        # Validate extension against the engine's supported list before
        # paying the download cost.
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        supported = self._engine.config.supported_formats.split(",")
        if ext not in supported:
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=f"unsupported format {ext!r}; supported={supported}",
                fatal=True,
            )

        try:
            content = await self._engine._artifacts.download_bytes(artifact_key)
        except Exception as e:
            logger.warning("spine.workflow.download_failed key=%s err=%s",
                           artifact_key, e)
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=f"artifact download failed: {e}",
                error_context={"artifact_key": artifact_key},
            )

        max_bytes = self._engine.config.max_document_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=(
                    f"document size {len(content)} bytes exceeds limit "
                    f"{max_bytes} bytes ({self._engine.config.max_document_size_mb}MB)"
                ),
                fatal=True,
            )

        document_id = ckpt.get("document_id") or str(uuid.uuid4())
        job_id = ckpt.get("spine_job_id") or str(uuid.uuid4())
        ckpt["document_id"] = document_id
        ckpt["spine_job_id"] = job_id

        # Defer imports so the SDK can be loaded by tests without
        # pulling the engine's full graph.
        from nexus_sdk.models import JobStatus

        # Mirror the legacy ingest endpoint's documents-dict bookkeeping
        # so any downstream code that reads engine.documents[document_id]
        # (e.g. status endpoints, event emitters) keeps working.
        from datetime import datetime, timezone
        from app.models import DocumentType, ParseStatus  # type: ignore

        self._engine.documents[document_id] = {
            "document_id": document_id,
            "tenant_id": env.tenant_id,
            "session_id": env.session_id,
            "filename": filename,
            "file_size_bytes": len(content),
            "uploaded_by": "workflow",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "status": ParseStatus.PENDING.value,
            "detected_type": DocumentType.UNKNOWN.value,
            "job_id": job_id,
            "artifact_key": artifact_key,
            "workflow_id": env.workflow_id,
        }

        await self._engine.job_store.set_job(
            job_id,
            {
                "job_id": job_id,
                "status": JobStatus.QUEUED.value,
                "tenant_id": env.tenant_id,
                "session_id": env.session_id,
                "type": "document",
                "workflow_id": env.workflow_id,
            },
        )

        start = time.monotonic()
        try:
            # The module-level helper that the legacy BackgroundTasks
            # path also calls. Same code path; no semantic drift between
            # legacy and workflow ingestion.
            from main import _process_document  # type: ignore

            await _process_document(
                engine=self._engine,
                content=content,
                filename=filename,
                document_id=document_id,
                tenant_id=env.tenant_id,
                session_id=env.session_id,
                job_id=job_id,
            )
        except Exception as e:
            logger.error("spine.workflow.parse_failed err=%s", e, exc_info=True)
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=str(e),
                error_context={
                    "exception_type": type(e).__name__,
                    "document_id": document_id,
                },
            )

        duration_ms = (time.monotonic() - start) * 1000

        stored = await self._engine.job_store.get_job(job_id)
        if not stored or stored.get("status") != JobStatus.COMPLETED.value:
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=(stored or {}).get("error") or "spine parse did not complete",
                error_context={
                    "job_id": job_id,
                    "document_id": document_id,
                    "status": (stored or {}).get("status"),
                },
            )

        doc_meta = self._engine.documents.get(document_id, {})
        result = stored.get("result", {}) or {}

        ckpt.update({
            "spine_status": "completed",
            "current_spine_step": _STEP_PARSE,
            "document_type": result.get(
                "document_type", doc_meta.get("detected_type", "unknown"),
            ),
            "page_count": result.get(
                "page_count", doc_meta.get("page_count", 0),
            ),
            "chunk_count": result.get(
                "chunk_count", doc_meta.get("chunk_count", 0),
            ),
            "table_count": result.get(
                "table_count", doc_meta.get("table_count", 0),
            ),
            "file_hash": doc_meta.get("file_hash", ""),
            "processing_time_ms": round(duration_ms, 2),
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
            duration_ms=int(duration_ms),
        )

    # ─── spine.chunk_and_embed ──────────────────────────────────

    async def _handle_chunk_and_embed(self, env: JobEnvelope) -> StepResult:
        """Pass-through under the Phase 1 monolithic shortcut.

        Chunking already ran inside `spine.parse` (the existing pipeline
        chunks during _process_document). Phase 1.5 will:
          - split chunking out of _process_document
          - add a dedicated embedding pass with a GPU model
          - run chunking on spine.cpu and embedding on spine.gpu
        For now this preserves the wire contract so backbone can rely
        on the same checkpoint shape regardless of whether one engine
        step or two emitted it.
        """
        ckpt = dict(env.checkpoint)
        if "document_id" not in ckpt:
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error="document_id missing from checkpoint; spine.parse must run first",
                fatal=True,
            )
        ckpt["current_spine_step"] = _STEP_CHUNK_EMBED
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
        )
