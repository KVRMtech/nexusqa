"""
Built-in Chain: Canonical Media Processing Pipeline.

Processes uploaded KT session media (audio + video) through a 7-stage
DAG to produce a unified canonical artifact — the single source of
truth for all downstream consumer chains (QA testing, compliance, etc.).

DAG:
    media_probe ─┬─→ audio_transcription → pii_redaction ──────────────┐
                 └─→ visual_extraction → visual_graph_assembly ────────┤
                                                                       ▼
                                                 artifact_persistence → canonical_quality_gate

Design: audio and visual run in PARALLEL after probe.  The quality gate
is Brain engine verifying minimum coverage before downstream chains start.
"""

from ..schema import (
    ChainDefinition,
    PollingConfig,
    RetryPolicy,
    StageDefinition,
)


def build_canonical_processing_chain() -> ChainDefinition:
    """Build the 7-stage canonical media processing chain."""
    return ChainDefinition(
        chain_id="nexus.canonical-processing",
        name="Canonical Media Processing",
        description=(
            "Process KT session media (audio + video) into a unified "
            "canonical artifact: transcribe → redact PII → extract visual "
            "keyframes → build visual graph → persist artifact → quality gate."
        ),
        version="2.0.0",
        tags=["canonical", "media", "processing", "core"],
        stages=[
            # ── Stage 1: Media Probe ───────────────────────────
            StageDefinition(
                stage_id="media_probe",
                name="Media Probe",
                description=(
                    "Probe uploaded media files for duration, codec, "
                    "resolution, and format metadata via Spine engine. "
                    "Uses shared-filesystem paths — zero copy, zero memory pressure."
                ),
                engine="spine",
                endpoint="/api/v1/spine/probe-media",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "audio_file_path": "$workflow.input.audio_file_path",
                    "video_file_path": "$workflow.input.video_file_path",
                },
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="fail",
            ),

            # ── Stage 2: Audio Transcription (parallel) ────────
            StageDefinition(
                stage_id="audio_transcription",
                name="Audio Transcription",
                description=(
                    "Transcribe KT session audio with speaker diarization "
                    "via Ears engine. Runs in parallel with visual_extraction."
                ),
                engine="ears",
                endpoint="/api/v1/ears/transcribe",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "language": "$workflow.input.language",
                    "num_speakers": "$workflow.input.num_speakers",
                    "processing_profile": "$workflow.input.processing_profile",
                },
                file_mappings={
                    "audio": "$workflow.input.audio_file_id",
                    "video": "$workflow.input.video_file_id",
                },
                depends_on=["media_probe"],
                condition="$workflow.input.audio_file_id or $stages.media_probe.output.audio or $stages.media_probe.output.video.audio_codec",
                timeout_seconds=3600,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="skip",
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/ears/jobs/{job_id}",
                    poll_interval_seconds=5.0,
                    max_poll_seconds=3600.0,
                    completion_statuses=["completed"],
                    failure_statuses=["failed"],
                    result_path="result",
                    status_path="status",
                ),
            ),

            # ── Stage 3: PII Redaction ─────────────────────────
            StageDefinition(
                stage_id="pii_redaction",
                name="PII Redaction",
                description=(
                    "Detect and redact all PII from the transcript "
                    "via Shield engine before downstream consumption."
                ),
                engine="shield",
                endpoint="/api/v1/shield/redact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "text": "$stages.audio_transcription.output.transcript_text",
                },
                depends_on=["audio_transcription"],
                condition="$stages.audio_transcription.output.transcript_text",
                timeout_seconds=60,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="skip",
            ),

            # ── Stage 4: Visual Extraction (parallel) ──────────
            StageDefinition(
                stage_id="visual_extraction",
                name="Visual Extraction",
                description=(
                    "Analyze screen recordings: frame extraction, scene "
                    "grouping, OCR, LLaVA analysis via Eyes engine. "
                    "Runs in parallel with audio_transcription."
                ),
                engine="eyes",
                endpoint="/api/v1/eyes/analyze-video",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "processing_profile": "$workflow.input.processing_profile",
                },
                file_mappings={
                    "video": "$workflow.input.video_file_id",
                },
                depends_on=["media_probe"],
                condition="$workflow.input.video_file_id",
                timeout_seconds=7200,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=15.0),
                on_failure="skip",
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/eyes/jobs/{job_id}",
                    poll_interval_seconds=10.0,
                    max_poll_seconds=7200.0,
                    completion_statuses=["completed"],
                    failure_statuses=["failed"],
                    result_path="result",
                    status_path="status",
                ),
            ),

            # ── Stage 5: Visual Graph Assembly ─────────────────
            StageDefinition(
                stage_id="visual_graph_assembly",
                name="Visual Graph Assembly",
                description=(
                    "Build a screen-flow graph from keyframes, OCR text, "
                    "and scene transitions via Spine engine."
                ),
                engine="spine",
                endpoint="/api/v1/spine/build-visual-graph",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "frames": "$stages.visual_extraction.output.frames",
                    "application_types_seen": "$stages.visual_extraction.output.application_types_seen",
                    "total_frames_extracted": "$stages.visual_extraction.output.total_frames_extracted",
                },
                depends_on=["visual_extraction"],
                condition="$stages.visual_extraction.output.frames",
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=1, backoff_seconds=5.0),
                on_failure="skip",
            ),

            # ── Stage 6: Artifact Persistence ──────────────────
            StageDefinition(
                stage_id="artifact_persistence",
                name="Canonical Artifact Persistence",
                description=(
                    "Merge transcript + visual data into a single canonical "
                    "artifact and persist to PostgreSQL via Spine engine. "
                    "Phase 1.4: Artifact status is the official completion signal."
                ),
                engine="spine",
                endpoint="/api/v1/spine/persist-canonical-artifact",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "workflow_id": "$workflow.workflow_id",
                    "artifact_id": "$workflow.input.artifact_id",
                    "source_type": "$workflow.input.source_type",
                    "source_filename": "$workflow.input.source_filename",
                    "created_by": "$workflow.input.created_by",
                    "processing_profile": "$workflow.input.processing_profile",
                    "media_probe": "$stages.media_probe.output",
                    "safe_transcript": "$stages.pii_redaction.output",
                    "raw_transcript": "$stages.audio_transcription.output",
                    "visual_analysis": "$stages.visual_extraction.output",
                    "visual_graph": "$stages.visual_graph_assembly.output",
                    "media_fingerprint": "$workflow.input.media_fingerprint",
                },
                depends_on=["pii_redaction", "visual_graph_assembly"],
                timeout_seconds=60,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=3.0),
                on_failure="fail",
            ),

            # ── Stage 7: Quality Gate ──────────────────────────
            StageDefinition(
                stage_id="canonical_quality_gate",
                name="Brain Quality Gate",
                description=(
                    "Brain engine verifies the canonical artifact meets "
                    "minimum quality and coverage thresholds before "
                    "downstream consumer chains can use it."
                ),
                engine="brain",
                endpoint="/api/v1/brain/canonical-quality-gate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "artifact_id": "$stages.artifact_persistence.output.artifact_id",
                    "has_transcript": "$stages.audio_transcription.output.transcript_text",
                    "has_visual": "$stages.visual_extraction.output.frames",
                    "scene_count": "$stages.visual_extraction.output.total_frames_analyzed",
                    "duration_seconds": "$stages.media_probe.output.duration_seconds",
                    # Shield returns: safe_text, mapping_id, entities_found, entity_count
                    "entity_count": "$stages.pii_redaction.output.entity_count",
                    "entities_found": "$stages.pii_redaction.output.entities_found",
                    "safe_text": "$stages.pii_redaction.output.safe_text",
                    "mapping_id": "$stages.pii_redaction.output.mapping_id",
                    "raw_transcript": "$stages.audio_transcription.output.transcript_text",
                },
                depends_on=["artifact_persistence"],
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="fail",
            ),
        ],
    )
