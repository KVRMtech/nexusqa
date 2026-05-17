"""
Phase A — Canonical-pipeline workflow handlers for the spine engine.

These five handlers replace the legacy `nexus.canonical-processing`
chain stages that the orchestrator used to dispatch via HTTP polling:

  - spine.probe_media              (was: media_probe)
  - spine.persist_visual_evidence  (was: persist_visual_evidence)
  - spine.persist_action_evidence  (was: action_evidence_extraction)
  - spine.build_visual_graph       (was: visual_graph_assembly)
  - spine.persist_artifact         (was: artifact_persistence)

Each handler reads the checkpoint slice the legacy stage's
`input_mapping` referenced, POSTs to the existing endpoint (HTTP
loopback to localhost inside the spine pod), then writes the response
back to the checkpoint under a well-known key the downstream step
looks for. The orchestration runs through WorkflowManager's
Redis-Streams DAG dispatcher, which gives:

  - per-step retry granularity (Phase 5-A / Phase 6 benefits)
  - workflow_state observability (dashboards + alerts work)
  - queue-based scale-out via KEDA

HTTP loopback is used because the endpoint functions live inside the
engine's `register_routes(app)` closure and are not callable
directly. Loopback POSTs cost ~0.5ms inside the same container —
cheaper than refactoring the engine. Auth is satisfied by a
short-lived JWT minted with the same NEXUS_JWT_SECRET the rest of the
stack uses.

The handlers are attached to the existing SpineWorkflowHandlers class
by `attach(handlers_cls)` so spine's main.py only changes one line
(the registration). No new file in the import graph; this module is
imported once by workflow_handlers.py.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


_STEP_PROBE_MEDIA = "spine.probe_media"
_STEP_PERSIST_VISUAL_EVIDENCE = "spine.persist_visual_evidence"
_STEP_PERSIST_ACTION_EVIDENCE = "spine.persist_action_evidence"
_STEP_BUILD_VISUAL_GRAPH = "spine.build_visual_graph"
_STEP_PERSIST_ARTIFACT = "spine.persist_artifact"           # legacy / pre-Phase-3
# Phase 3 — persist-first / enrich-async architectural pivot.
_STEP_PERSIST_MINIMAL_ARTIFACT = "spine.persist_minimal_artifact"
_STEP_UPDATE_ARTIFACT_ENRICHED = "spine.update_artifact_enriched"


# ── Loopback helpers ──────────────────────────────────────────


_loopback_base: str | None = None
_loopback_token: str | None = None


def _base() -> str:
    global _loopback_base
    if _loopback_base is None:
        port = os.environ.get("ENGINE_PORT", "8009")
        _loopback_base = "http://localhost:" + port
    return _loopback_base


def _token() -> str:
    """Mint a long-lived JWT for loopback auth. Worker processes are
    long-lived; the token's 24h TTL spans typical pod lifetime."""
    global _loopback_token
    if _loopback_token is not None:
        return _loopback_token
    secret = os.environ.get("NEXUS_JWT_SECRET", "")
    if not secret:
        return ""
    import jwt
    now = int(time.time())
    _loopback_token = jwt.encode(
        {
            "sub": "spine-workflow-handler",
            "email": "spine-workflow-handler@internal",
            "tenant_id": "internal",
            "role": "admin",
            "iat": now,
            "exp": now + 86400,
        },
        secret,
        algorithm="HS256",
    )
    return _loopback_token


async def _post(path: str, payload: dict, timeout: float = 600.0) -> dict:
    import httpx
    headers = {"Content-Type": "application/json"}
    tok = _token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_base() + path, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                "spine loopback " + path + " returned " + str(resp.status_code) +
                ": " + resp.text[:500]
            )
        return resp.json()


# ── StepResult import (deferred — keeps module import cheap) ──


def _step_result_success(env, checkpoint: dict, duration_ms: int = 0):
    from nexus_sdk.workflows import StepResult
    return StepResult(
        workflow_id=env.workflow_id,
        step_name=env.step_name,
        success=True,
        checkpoint=checkpoint,
        duration_ms=duration_ms,
    )


def _step_result_fail(env, error: str, fatal: bool = False, exc: Exception | None = None):
    from nexus_sdk.workflows import StepResult
    ctx: dict[str, Any] = {}
    if exc is not None:
        ctx["exception_type"] = type(exc).__name__
    return StepResult(
        workflow_id=env.workflow_id,
        step_name=env.step_name,
        success=False,
        error=error,
        error_context=ctx,
        fatal=fatal,
    )


# ── Handler bodies ────────────────────────────────────────────


async def _handle_probe_media(self, env):
    """Probe media files for duration / codec / resolution. Required
    metadata for the canonical_artifact row."""
    ckpt = dict(env.checkpoint)
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "audio_file_path": ckpt.get("audio_file_path"),
        "video_file_path": ckpt.get("video_file_path"),
    }
    try:
        result = await _post("/api/v1/spine/probe-media", payload, timeout=60)
    except Exception as e:
        logger.error(
            "spine.probe_media.failed",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        return _step_result_fail(env, str(e), exc=e)
    ckpt["media_probe_output"] = result
    ckpt["media_duration_seconds"] = float(result.get("duration_seconds") or 0.0)
    ckpt["current_spine_step"] = _STEP_PROBE_MEDIA
    return _step_result_success(env, ckpt)


async def _handle_persist_visual_evidence(self, env):
    """Write visual_frames rows to Postgres so the UI can render scenes.
    Non-fatal on error (matches legacy on_failure='skip')."""
    ckpt = dict(env.checkpoint)
    artifact_id = ckpt.get("artifact_id") or ckpt.get("canonical_artifact_id")
    if not artifact_id:
        return _step_result_fail(
            env,
            "persist_visual_evidence: artifact_id missing — "
            "spine.persist_artifact must run first",
            fatal=True,
        )
    eyes_result = ckpt.get("eyes_result") or {}
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "artifact_id": artifact_id,
        "frames": eyes_result.get("frames", []),
        "scene_transitions": eyes_result.get("scene_transitions", []),
    }
    try:
        result = await _post(
            "/api/v1/spine/persist-visual-frames", payload, timeout=300,
        )
    except Exception as e:
        logger.warning(
            "spine.persist_visual_evidence.skip",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        ckpt["persist_visual_evidence_skipped"] = str(e)
        ckpt["current_spine_step"] = _STEP_PERSIST_VISUAL_EVIDENCE
        return _step_result_success(env, ckpt)
    ckpt["visual_evidence_output"] = result
    ckpt["scenes_persisted"] = int(result.get("scenes_persisted") or 0)
    ckpt["current_spine_step"] = _STEP_PERSIST_VISUAL_EVIDENCE
    return _step_result_success(env, ckpt)


async def _handle_persist_action_evidence(self, env):
    """Multimodal action triangulation: cursor + intent + LLaVA delta +
    OCR diff. Non-fatal on error."""
    ckpt = dict(env.checkpoint)
    artifact_id = ckpt.get("artifact_id") or ckpt.get("canonical_artifact_id")
    if not artifact_id:
        return _step_result_fail(
            env, "action_evidence: artifact_id missing", fatal=True,
        )
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "artifact_id": artifact_id,
        "raw_transcript": ckpt.get("ears_result") or {},
    }
    try:
        result = await _post(
            "/api/v1/spine/persist-action-evidence", payload, timeout=600,
        )
    except Exception as e:
        logger.warning(
            "spine.persist_action_evidence.skip",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        ckpt["persist_action_evidence_skipped"] = str(e)
        ckpt["current_spine_step"] = _STEP_PERSIST_ACTION_EVIDENCE
        return _step_result_success(env, ckpt)
    ckpt["action_evidence_output"] = result
    ckpt["current_spine_step"] = _STEP_PERSIST_ACTION_EVIDENCE
    return _step_result_success(env, ckpt)


async def _handle_build_visual_graph(self, env):
    """Assemble the screen-flow graph. Non-fatal on error."""
    ckpt = dict(env.checkpoint)
    eyes_result = ckpt.get("eyes_result") or {}
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "frames": eyes_result.get("frames", []),
        "application_types_seen": eyes_result.get("application_types_seen", []),
        "total_frames_extracted": int(eyes_result.get("total_frames_extracted") or 0),
    }
    try:
        result = await _post(
            "/api/v1/spine/build-visual-graph", payload, timeout=120,
        )
    except Exception as e:
        logger.warning(
            "spine.build_visual_graph.skip",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        ckpt["build_visual_graph_skipped"] = str(e)
        ckpt["current_spine_step"] = _STEP_BUILD_VISUAL_GRAPH
        return _step_result_success(env, ckpt)
    ckpt["visual_graph_output"] = result
    ckpt["current_spine_step"] = _STEP_BUILD_VISUAL_GRAPH
    return _step_result_success(env, ckpt)


async def _handle_persist_artifact(self, env):
    """Write the canonical_artifacts row. THIS is the step that makes
    the artifact visible in the UI. Fatal on error — there's no point
    finishing the workflow without a row to point at."""
    ckpt = dict(env.checkpoint)
    artifact_id = ckpt.get("artifact_id")
    if not artifact_id:
        return _step_result_fail(
            env,
            "persist_artifact: artifact_id missing in checkpoint",
            fatal=True,
        )
    # Phase 1 — surface graceful-degradation markers so the canonical
    # artifact row records WHICH stages ran in degraded mode (e.g.
    # vision model down → analyze_scenes ran on OCR-only). The UI
    # reads `degraded_stages` from the artifact to show "partial
    # result" badges so customers know what's missing.
    degraded_stages = list(ckpt.get("degraded_stages") or [])
    degraded_reasons = dict(ckpt.get("degraded_reasons") or {})
    # Architect P3: reflect degraded-stage state in the artifact status
    # so the UI can show "Partial" instead of a green "completed" badge
    # when vision/OCR/etc fell back to placeholders.
    artifact_status = "completed_degraded" if degraded_stages else "persisted"
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "workflow_id": env.workflow_id,
        "artifact_id": artifact_id,
        "source_type": ckpt.get("source_type", "upload"),
        "source_filename": ckpt.get("source_filename", "upload"),
        "created_by": ckpt.get("created_by", ""),
        "processing_profile": (
            env.params.get("profile")
            or ckpt.get("processing_profile", "fast")
        ),
        "media_probe": ckpt.get("media_probe_output") or {},
        "safe_transcript": {
            "safe_text": ckpt.get("safe_text", ""),
            "shield_context": ckpt.get("shield_context") or {},
        },
        "raw_transcript": ckpt.get("ears_result") or {},
        "visual_analysis": ckpt.get("eyes_result") or {},
        "visual_graph": ckpt.get("visual_graph_output") or {},
        "media_fingerprint": ckpt.get("media_fingerprint", ""),
        "degraded_stages": degraded_stages,
        "degraded_reasons": degraded_reasons,
        "artifact_status": artifact_status,
    }
    try:
        result = await _post(
            "/api/v1/spine/persist-canonical-artifact", payload, timeout=60,
        )
    except Exception as e:
        logger.error(
            "spine.persist_artifact.failed",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        return _step_result_fail(env, str(e), exc=e)
    ckpt["artifact_persistence_output"] = result
    ckpt["canonical_artifact_id"] = result.get("artifact_id") or artifact_id
    ckpt["current_spine_step"] = _STEP_PERSIST_ARTIFACT
    return _step_result_success(env, ckpt)


# ── Phase 3 handlers: persist-first / enrich-async ─────────────


async def _handle_persist_minimal_artifact(self, env):
    """Phase 3 — write the canonical_artifacts row EARLY with whatever
    data is cheap to produce (probe + transcript + frames manifest).
    Status is `minimal` so the UI can show "Partial · enriching".
    The user sees their artifact in ~1-2 min instead of 25-40 min.

    Mirrors `_handle_persist_artifact` but tolerates empty
    visual_analysis / visual_graph (they get filled in by
    `spine.update_artifact_enriched` later).
    """
    ckpt = dict(env.checkpoint)
    artifact_id = ckpt.get("artifact_id")
    if not artifact_id:
        return _step_result_fail(
            env,
            "persist_minimal_artifact: artifact_id missing in checkpoint",
            fatal=True,
        )
    degraded_stages = list(ckpt.get("degraded_stages") or [])
    degraded_reasons = dict(ckpt.get("degraded_reasons") or {})
    payload = {
        "tenant_id": env.tenant_id,
        "session_id": env.session_id,
        "workflow_id": env.workflow_id,
        "artifact_id": artifact_id,
        "source_type": ckpt.get("source_type", "upload"),
        "source_filename": ckpt.get("source_filename", "upload"),
        "created_by": ckpt.get("created_by", ""),
        "processing_profile": (
            env.params.get("profile")
            or ckpt.get("processing_profile", "fast")
        ),
        "media_probe": ckpt.get("media_probe_output") or {},
        "safe_transcript": {
            "safe_text": ckpt.get("safe_text", ""),
            "shield_context": ckpt.get("shield_context") or {},
        },
        "raw_transcript": ckpt.get("ears_result") or {},
        # NOT YET — visual enrichment hasn't run. Empty placeholders.
        "visual_analysis": {},
        "visual_graph": {},
        "media_fingerprint": ckpt.get("media_fingerprint", ""),
        "degraded_stages": degraded_stages,
        "degraded_reasons": degraded_reasons,
        # Phase 3 marker — tells the spine endpoint to write status=minimal
        # instead of the default `persisted`. The endpoint adds the row
        # to canonical_artifacts but skips the redis fingerprint cache
        # (we only cache fully-enriched artifacts).
        "artifact_status": "minimal",
    }
    try:
        result = await _post(
            "/api/v1/spine/persist-canonical-artifact", payload, timeout=60,
        )
    except Exception as e:
        logger.error(
            "spine.persist_minimal_artifact.failed",
            extra={"err": str(e), "workflow_id": env.workflow_id},
        )
        return _step_result_fail(env, str(e), exc=e)
    ckpt["artifact_persistence_output"] = result
    ckpt["canonical_artifact_id"] = result.get("artifact_id") or artifact_id
    ckpt["minimal_artifact_persisted"] = True
    ckpt["current_spine_step"] = _STEP_PERSIST_MINIMAL_ARTIFACT
    # Phase 4 — prometheus counter for minimal-artifact persistence.
    try:
        from nexus_sdk.workflows.metrics import get_workflow_metrics
        kind = (ckpt.get("processing_profile") or "fast")
        get_workflow_metrics().record_minimal_artifact(kind=kind)
    except Exception:
        pass
    logger.info(
        "spine.persist_minimal_artifact.done wf=%s artifact_id=%s",
        env.workflow_id, artifact_id,
    )
    return _step_result_success(env, ckpt)


async def _handle_update_artifact_enriched(self, env):
    """Phase 3 — UPDATE the minimal artifact with the heavy-enrichment
    data (visual_analysis, visual_graph, scene_count, etc) and flip
    status from `minimal` → `persisted`. Runs after the eyes branch
    finishes (build_evidence + build_visual_graph).

    Failure here does NOT lose the artifact — the minimal row is
    already in DB. Operator can re-trigger enrichment via the spine
    re-enrich endpoint (future work) or just live with a partial
    artifact.
    """
    ckpt = dict(env.checkpoint)
    artifact_id = ckpt.get("artifact_id") or ckpt.get("canonical_artifact_id")
    if not artifact_id:
        return _step_result_fail(
            env,
            "update_artifact_enriched: artifact_id missing",
            fatal=True,
        )
    degraded_stages = list(ckpt.get("degraded_stages") or [])
    degraded_reasons = dict(ckpt.get("degraded_reasons") or {})
    # Architect P3: artifact status reflects degraded-stage state so the
    # UI/API surface can distinguish a fully-enriched artifact from one
    # that completed with partial data. The previous behavior flipped
    # everything to `persisted` once enrichment ran, hiding partial
    # results behind a green badge.
    target_status = "completed_degraded" if degraded_stages else "persisted"
    payload = {
        "tenant_id": env.tenant_id,
        "artifact_id": artifact_id,
        "visual_analysis": ckpt.get("eyes_result") or {},
        "visual_graph": ckpt.get("visual_graph_output") or {},
        "degraded_stages": degraded_stages,
        "degraded_reasons": degraded_reasons,
        "artifact_status": target_status,
    }
    try:
        result = await _post(
            "/api/v1/spine/update-canonical-artifact-enrichment",
            payload, timeout=60,
        )
    except Exception as e:
        # Non-fatal — minimal artifact is already in DB. Log and
        # mark the workflow with degraded_stages so the UI shows
        # "enrichment failed" but keeps the artifact visible.
        logger.warning(
            "spine.update_artifact_enriched.failed (non-fatal — minimal artifact still persisted)",
            extra={"err": str(e), "workflow_id": env.workflow_id, "artifact_id": artifact_id},
        )
        if "enrichment_update" not in degraded_stages:
            degraded_stages.append("enrichment_update")
        ckpt["degraded_stages"] = degraded_stages
        ckpt["degraded_reasons"] = {
            **degraded_reasons,
            "enrichment_update": f"update_failed: {type(e).__name__}",
        }
        ckpt["current_spine_step"] = _STEP_UPDATE_ARTIFACT_ENRICHED
        # Phase 4 — degraded enrichment is still a recorded enrichment
        # attempt; mark degraded=True so dashboards can split the rate.
        try:
            from nexus_sdk.workflows.metrics import get_workflow_metrics
            kind = (ckpt.get("processing_profile") or "fast")
            get_workflow_metrics().record_artifact_enriched(
                kind=kind, degraded=True,
            )
        except Exception:
            pass
        # Return success=True so downstream steps (visual/action
        # evidence, backbone) still run — they handle empty data fine.
        return _step_result_success(env, ckpt)
    ckpt["artifact_enrichment_output"] = result
    ckpt["current_spine_step"] = _STEP_UPDATE_ARTIFACT_ENRICHED
    # Phase 4 — successful enrichment counter.
    try:
        from nexus_sdk.workflows.metrics import get_workflow_metrics
        kind = (ckpt.get("processing_profile") or "fast")
        is_degraded = bool(ckpt.get("degraded_stages"))
        get_workflow_metrics().record_artifact_enriched(
            kind=kind, degraded=is_degraded,
        )
    except Exception:
        pass
    logger.info(
        "spine.update_artifact_enriched.done wf=%s artifact_id=%s",
        env.workflow_id, artifact_id,
    )
    return _step_result_success(env, ckpt)


# ── Attach to the existing SpineWorkflowHandlers class ────────


STEP_NAMES = {
    "probe_media": _STEP_PROBE_MEDIA,
    "persist_visual_evidence": _STEP_PERSIST_VISUAL_EVIDENCE,
    "persist_action_evidence": _STEP_PERSIST_ACTION_EVIDENCE,
    "build_visual_graph": _STEP_BUILD_VISUAL_GRAPH,
    "persist_artifact": _STEP_PERSIST_ARTIFACT,
    "persist_minimal_artifact": _STEP_PERSIST_MINIMAL_ARTIFACT,
    "update_artifact_enriched": _STEP_UPDATE_ARTIFACT_ENRICHED,
}


def attach(handlers_cls) -> None:
    """Bind the new handler methods to SpineWorkflowHandlers. Called
    once from workflow_handlers.py at module load time. Idempotent."""
    if getattr(handlers_cls, "_canonical_handlers_attached", False):
        return
    handlers_cls._handle_probe_media = _handle_probe_media
    handlers_cls._handle_persist_visual_evidence = _handle_persist_visual_evidence
    handlers_cls._handle_persist_action_evidence = _handle_persist_action_evidence
    handlers_cls._handle_build_visual_graph = _handle_build_visual_graph
    handlers_cls._handle_persist_artifact = _handle_persist_artifact
    # Phase 3 handlers.
    handlers_cls._handle_persist_minimal_artifact = _handle_persist_minimal_artifact
    handlers_cls._handle_update_artifact_enriched = _handle_update_artifact_enriched
    handlers_cls._canonical_handlers_attached = True
