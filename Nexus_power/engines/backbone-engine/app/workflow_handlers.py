"""
Phase 1 canonical-workflow handlers for backbone-engine.

Backbone is the join point — the LAST step in every canonical plan.
Its role here is to project the upstream checkpoint into the platform's
knowledge graph (Neo4j) and vector store (Milvus / Sentence-Transformers).

Step names from canonical plans:
  - backbone.canonicalize             (audio / video / document plans)
  - backbone.canonicalize_multimodal  (multimodal plan)

What the handlers DO:
  1. Read the upstream checkpoint (ears_result, eyes_result, document
     fields produced by spine).
  2. Create a KT_SESSION knowledge node carrying session-level metadata
     (tenant_id, session_id, profile, frame_count, segment_count,
     speaker_count, language, document_type, etc.).
  3. Index salient text content (transcript text, scene descriptions,
     document chunks) into the vector store so canonical artifacts are
     immediately searchable.
  4. Return the knowledge node id in the checkpoint.

What the handlers do NOT do:
  - Persist the QT_Session DB row — that remains spine's
    `_build_artifact_for_session` / `_persist_artifact_to_db` path,
    triggered via the existing event-bus subscription concurrently
    with this step. (Phase 1.5 will collapse the two paths.)

Failure handling:
  - Neo4j / Milvus connectivity issues return a soft (non-fatal)
    failure so the orchestrator retries instead of quarantining a
    valid upload. The in-memory fallback ensures the engine still
    accepts writes in degraded mode.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from nexus_sdk.workflows import JobEnvelope, StepResult

logger = logging.getLogger(__name__)


_STEP_CANONICALIZE = "backbone.canonicalize"
_STEP_CANONICALIZE_MM = "backbone.canonicalize_multimodal"

# Vector-store payload size cap. Text content from a long video can be
# many KB; trim to keep one embedding-call cheap and predictable.
_MAX_INDEX_CHARS = 8_000


class BackboneWorkflowHandlers:
    """Bound to a live BackboneEngine instance; registers handlers on a worker."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def register(self, worker) -> None:
        worker.register(_STEP_CANONICALIZE, self._handle_canonicalize)
        worker.register(_STEP_CANONICALIZE_MM, self._handle_canonicalize_multimodal)

    # ─── backbone.canonicalize ──────────────────────────────────

    async def _handle_canonicalize(self, env: JobEnvelope) -> StepResult:
        """Single-modality canonicalization (audio, video, OR document).

        `params.kind` is set by the canonical plan ('audio' | 'video' |
        'document') so the handler knows which upstream fields to read.
        """
        kind = env.params.get("kind") or self._guess_kind(env.checkpoint)
        return await self._run(env, kinds=[kind])

    # ─── backbone.canonicalize_multimodal ───────────────────────

    async def _handle_canonicalize_multimodal(self, env: JobEnvelope) -> StepResult:
        """Multimodal canonicalization — combines audio + video metadata
        into a single KT_SESSION node and stores both text indexes."""
        return await self._run(env, kinds=["video", "audio"])

    # ─── Shared body ────────────────────────────────────────────

    async def _run(self, env: JobEnvelope, kinds: list[str]) -> StepResult:
        start = time.monotonic()
        ckpt = dict(env.checkpoint)

        # 1. Build the KT_SESSION node properties from the upstream
        #    checkpoint. Each kind contributes its own slice.
        props: dict[str, Any] = {
            "session_id": env.session_id,
            "tenant_id": env.tenant_id,
            "workflow_id": env.workflow_id,
            "processing_profile": env.params.get("profile", "fast"),
            "kinds": kinds,
        }
        text_indexes: list[tuple[str, dict[str, Any]]] = []

        for kind in kinds:
            slice_props, text_bundle = self._slice_for(kind, ckpt)
            props.update(slice_props)
            if text_bundle is not None:
                text_indexes.append((kind, text_bundle))

        # 2. Create the canonical knowledge node.
        try:
            # Late import: NodeType lives in main.py at module scope
            # alongside the engine class. Workers run in the engine's
            # own pod so sys.path already has it.
            from main import NodeType  # type: ignore

            node_id = await self._engine.graph.create_node(
                node_type=NodeType.KT_SESSION.value,
                properties=props,
                tenant_id=env.tenant_id,
                source={
                    "session_id": env.session_id,
                    "engine": "backbone",
                    "workflow_id": env.workflow_id,
                },
            )
        except Exception as e:
            logger.warning(
                "backbone.workflow.create_node_failed kinds=%s err=%s",
                kinds, e, exc_info=True,
            )
            return StepResult(
                workflow_id=env.workflow_id,
                step_name=env.step_name,
                success=False,
                error=f"knowledge graph write failed: {e}",
                error_context={"kinds": kinds, "exception_type": type(e).__name__},
            )

        # 3. Index salient text into the vector store. Failures here
        #    are soft — the knowledge node still exists.
        indexed = 0
        for kind, bundle in text_indexes:
            text = bundle.get("text") or ""
            if not text:
                continue
            text = text[:_MAX_INDEX_CHARS]
            try:
                self._engine.vectors.store(
                    node_id,
                    text,
                    {
                        "type": "KTSession",
                        "kind": kind,
                        "tenant_id": env.tenant_id,
                        "session_id": env.session_id,
                    },
                )
                indexed += 1
            except Exception as e:
                logger.debug(
                    "backbone.workflow.vector_index_failed kind=%s err=%s",
                    kind, e,
                )

        duration_ms = int((time.monotonic() - start) * 1000)
        ckpt.update({
            "canonical_kt_session_node_id": node_id,
            "canonical_kinds": kinds,
            "canonical_indexed_text_slices": indexed,
            "backbone_status": "completed",
        })
        return StepResult(
            workflow_id=env.workflow_id,
            step_name=env.step_name,
            success=True,
            checkpoint=ckpt,
            duration_ms=duration_ms,
        )

    # ─── Per-kind slicing ───────────────────────────────────────

    def _slice_for(
        self, kind: str, ckpt: dict[str, Any],
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        """Pull kind-specific fields from the checkpoint into KT_SESSION
        properties. Returns (props, text_bundle_or_None)."""
        if kind == "audio":
            return self._slice_audio(ckpt)
        if kind == "video":
            return self._slice_video(ckpt)
        if kind == "document":
            return self._slice_document(ckpt)
        return {}, None

    @staticmethod
    def _slice_audio(ckpt: dict[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        result = ckpt.get("ears_result") or {}
        text = (
            ckpt.get("safe_text")          # preferred: shield-redacted
            or ckpt.get("transcript_text")  # raw transcript
            or result.get("text")
            or ""
        )
        segments = result.get("segments", []) or []
        speakers = {s.get("speaker") for s in segments if s.get("speaker")}
        props = {
            "audio.segment_count": len(segments),
            "audio.speaker_count": len(speakers),
            "audio.language": ckpt.get("language_detected") or result.get("language", ""),
            "audio.duration_seconds": result.get("audio_duration_seconds", 0),
        }
        return props, ({"text": text} if text else None)

    @staticmethod
    def _slice_video(ckpt: dict[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        result = ckpt.get("eyes_result") or {}
        frames = result.get("frames", []) or []
        scene_transitions = result.get("scene_transitions", []) or []
        # Concatenate scene descriptions for the vector index — they're
        # the most semantically dense projection of the video.
        descriptions: list[str] = []
        for fr in frames:
            desc = fr.get("description") or ""
            if desc:
                descriptions.append(desc)
        text = "\n".join(descriptions)
        app_types = result.get("application_types_seen", []) or []
        props = {
            "video.frame_count": len(frames),
            "video.scene_transition_count": len(scene_transitions),
            "video.application_types": list(app_types),
        }
        return props, ({"text": text} if text else None)

    @staticmethod
    def _slice_document(ckpt: dict[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        # Spine writes document_id + summary fields into the checkpoint.
        # Chunks themselves are in engine.document_chunks on the spine
        # pod; we don't pull them across the network here. The
        # KT_SESSION node carries the document_id so downstream code
        # can hop to spine for full chunks.
        props = {
            "document.document_id": ckpt.get("document_id", ""),
            "document.document_type": ckpt.get("document_type", "unknown"),
            "document.page_count": int(ckpt.get("page_count", 0) or 0),
            "document.chunk_count": int(ckpt.get("chunk_count", 0) or 0),
            "document.table_count": int(ckpt.get("table_count", 0) or 0),
        }
        # Use the safe_text if shield wrote one; otherwise no inline
        # text is available (it's in chunks on spine).
        text = ckpt.get("safe_text") or ""
        return props, ({"text": text} if text else None)

    # ─── Kind inference ─────────────────────────────────────────

    @staticmethod
    def _guess_kind(ckpt: dict[str, Any]) -> str:
        """When the plan didn't pass params.kind explicitly, infer it
        from which upstream artifact the checkpoint actually carries."""
        if ckpt.get("ears_result"):
            return "audio"
        if ckpt.get("eyes_result"):
            return "video"
        if ckpt.get("document_id"):
            return "document"
        return "audio"  # safe default; gets the empty-text gate
