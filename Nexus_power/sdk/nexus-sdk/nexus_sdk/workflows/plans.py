"""
Canonical workflow plans.

The platform's published shapes — video, audio, multimodal, document —
are described here as DATA, not code. Each plan is a list of StepPlan
records. Adding a new shape is a PR to this file plus a registration
entry in `CANONICAL_PLANS`. No engine code changes required as long as
the engines already implement the step handlers the plan names.

Per-step `deadline_seconds` are tuned for the median target media
profile (15-minute video at 720p, 60-minute audio at 16 kHz mono).
Overall workflow deadline defaults to 15 minutes (Phase 1 SLO).
"""

from __future__ import annotations

from typing import Any, Callable

from nexus_sdk.workflows.models import StepKind, StepPlan, WorkflowKind, WorkflowPlan


# ─── Audio canonicalization ─────────────────────────────────────


def audio_plan(
    tenant_id: str,
    session_id: str,
    *,
    profile: str = "standard",
    metadata: dict | None = None,
) -> WorkflowPlan:
    """
    Audio canonicalization (Phase 6 — true per-step pipeline):
      1. shield.redact_audio       (CPU; PII pre-scrub)
      2. ears.preprocess           (CPU; resample 16k mono + optional chunking)
      3. ears.diarize              (GPU; pyannote 3.1)
      4. ears.transcribe_segments  (GPU; Whisper, fans out over chunks)
      5. ears.align                (CPU; speaker+text align + finalize)
      6. backbone.canonicalize     (CPU; emit canonical artifacts)
    """
    # Each step's deadline matches measured wall time at the median target
    # (60-min audio, 16 kHz mono, fast profile). Phase 6 split unlocks
    # per-step retry: a Whisper hang now only retries that one step, not
    # the whole pipeline.
    return WorkflowPlan(
        kind=WorkflowKind.AUDIO,
        tenant_id=tenant_id,
        session_id=session_id,
        deadline_seconds=900,
        metadata=metadata or {},
        steps=[
            StepPlan(
                name="shield.redact_audio",
                engine="shield",
                kind=StepKind.CPU,
                deadline_seconds=30,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="ears.preprocess",
                engine="ears",
                kind=StepKind.CPU,
                # ffmpeg resample + VAD; 60-min audio ≈ 30s wall time
                deadline_seconds=120,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="ears.diarize",
                engine="ears",
                kind=StepKind.GPU,
                # pyannote on 60-min audio @ fast ≈ 60s; non-fast ≈ 180s
                deadline_seconds=300,
                max_attempts=2,
                params={"profile": profile},
            ),
            StepPlan(
                name="ears.transcribe_segments",
                engine="ears",
                kind=StepKind.GPU,
                # Whisper medium on 60-min audio @ fast ≈ 360s
                deadline_seconds=600,
                max_attempts=2,
                params={"profile": profile},
            ),
            StepPlan(
                name="ears.align",
                engine="ears",
                kind=StepKind.CPU,
                # Pure CPU alignment of two segment lists; well under 30s
                # even for hour-long audio with thousands of segments
                deadline_seconds=60,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="backbone.canonicalize",
                engine="backbone",
                kind=StepKind.CPU,
                deadline_seconds=60,
                max_attempts=3,
                params={"kind": "audio"},
            ),
        ],
    )


# ─── Video canonicalization ─────────────────────────────────────


def video_plan(
    tenant_id: str,
    session_id: str,
    *,
    profile: str = "fast",
    metadata: dict | None = None,
) -> WorkflowPlan:
    """
    Video canonicalization (Phase 5-A — 6 real steps + finalize):
      1. shield.redact_video         (CPU)
      2. eyes.extract_frames         (CPU; ffmpeg + dHash dedup)
      3. eyes.detect_scenes          (CPU; similarity grouping)
      4. eyes.ocr_frames             (CPU; EasyOCR batched)
      5. eyes.analyze_scenes         (GPU; LLaVA per rep frame)
      6. eyes.analyze_transitions    (GPU; 2nd LLM pass on adjacencies)
      7. eyes.build_evidence         (CPU; ElementTracker + result)
      8. backbone.canonicalize       (CPU)
    """
    return WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id=tenant_id,
        session_id=session_id,
        # Workflow-level deadline = sum of per-step deadlines plus
        # ~25% safety margin for queue contention.
        deadline_seconds=2400,
        metadata=metadata or {},
        steps=[
            StepPlan(
                name="shield.redact_video",
                engine="shield",
                kind=StepKind.CPU,
                deadline_seconds=60,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.extract_frames",
                engine="eyes",
                kind=StepKind.CPU,
                # Pure ffmpeg + dHash. 15-min @ 720p ≈ 30s wall time.
                deadline_seconds=180,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.detect_scenes",
                engine="eyes",
                kind=StepKind.CPU,
                # In-memory dHash grouping over the frames manifest.
                deadline_seconds=60,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.ocr_frames",
                engine="eyes",
                kind=StepKind.CPU,
                # EasyOCR batched. Fast profile may skip entirely.
                deadline_seconds=300,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.analyze_scenes",
                engine="eyes",
                kind=StepKind.GPU,
                # LLaVA per representative frame.
                # 15-min @ fast ≈ 60-120s; deep ≈ 180-300s.
                deadline_seconds=600,
                max_attempts=2,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.analyze_transitions",
                engine="eyes",
                kind=StepKind.GPU,
                # Second LLM pass. Smaller payload (just adjacent rep frames).
                deadline_seconds=300,
                max_attempts=2,
                params={"profile": profile},
            ),
            StepPlan(
                name="eyes.build_evidence",
                engine="eyes",
                kind=StepKind.CPU,
                # ElementTracker post-process + VisualAnalysisResult.
                deadline_seconds=60,
                max_attempts=3,
                params={"profile": profile},
            ),
            StepPlan(
                name="backbone.canonicalize",
                engine="backbone",
                kind=StepKind.CPU,
                deadline_seconds=120,
                max_attempts=3,
                params={"kind": "video"},
            ),
        ],
    )


# ─── Multimodal (video + audio) ─────────────────────────────────


def multimodal_plan(
    tenant_id: str,
    session_id: str,
    *,
    profile: str = "fast",
    metadata: dict | None = None,
) -> WorkflowPlan:
    """
    Multimodal pipeline runs audio-track and video-track work in the
    same workflow. Because Phase 1 keeps steps sequential, the order is
    chosen so the video work (which is GPU-bound and slower) runs first
    and audio work piggybacks on the now-extracted audio track.

    Phase 2 (planner v2) will fan out audio and video as parallel
    branches via a DAG plan; for now the linear shape gives strict
    determinism and easier debugging.
    """
    # Phase 12 — true DAG. Video and audio branches run IN PARALLEL,
    # both rooted at a shared shield pre-redaction, both joining at
    # backbone.canonicalize_multimodal. Wall-time win: ~max(video,
    # audio) instead of video + audio.
    #
    # Visual:
    #
    #   shield.redact_video ──► eyes.extract_frames ──► ... ──► eyes.build_evidence ──┐
    #                                                                                 ├─► backbone.canonicalize_multimodal
    #   shield.redact_audio ──► ears.preprocess ──► ears.diarize    ┐                ┘
    #                                            └► ears.transcribe ┴► ears.align ───┘
    #
    # The dispatcher reads `depends_on` and schedules accordingly.
    # Linear plans (audio_plan, document_plan, video_plan) get
    # depends_on auto-derived from their step order by WorkflowPlan
    # __post_init__; the same dispatcher handles both.

    steps = [
        # ── Shield pre-redact (parallel) ─────────────────────────
        StepPlan(
            name="shield.redact_video", engine="shield", kind=StepKind.CPU,
            deadline_seconds=60, max_attempts=3, params={"profile": profile},
            depends_on=[],
        ),
        StepPlan(
            name="shield.redact_audio", engine="shield", kind=StepKind.CPU,
            deadline_seconds=30, max_attempts=3, params={"profile": profile},
            depends_on=[],
        ),
        # ── Video branch (Phase 5-A 6-step) ──────────────────────
        StepPlan(
            name="eyes.extract_frames", engine="eyes", kind=StepKind.CPU,
            deadline_seconds=180, max_attempts=3, params={"profile": profile},
            depends_on=["shield.redact_video"],
        ),
        StepPlan(
            name="eyes.detect_scenes", engine="eyes", kind=StepKind.CPU,
            deadline_seconds=60, max_attempts=3, params={"profile": profile},
            depends_on=["eyes.extract_frames"],
        ),
        StepPlan(
            name="eyes.ocr_frames", engine="eyes", kind=StepKind.CPU,
            deadline_seconds=300, max_attempts=3, params={"profile": profile},
            depends_on=["eyes.detect_scenes"],
        ),
        StepPlan(
            name="eyes.analyze_scenes", engine="eyes", kind=StepKind.GPU,
            deadline_seconds=600, max_attempts=2, params={"profile": profile},
            depends_on=["eyes.ocr_frames"],
        ),
        StepPlan(
            name="eyes.analyze_transitions", engine="eyes", kind=StepKind.GPU,
            deadline_seconds=300, max_attempts=2, params={"profile": profile},
            depends_on=["eyes.analyze_scenes"],
        ),
        StepPlan(
            name="eyes.build_evidence", engine="eyes", kind=StepKind.CPU,
            deadline_seconds=60, max_attempts=3, params={"profile": profile},
            depends_on=["eyes.analyze_transitions"],
        ),
        # ── Audio branch (Phase 6 4-step + diarize/transcribe fan-out) ──
        StepPlan(
            name="ears.preprocess", engine="ears", kind=StepKind.CPU,
            deadline_seconds=90, max_attempts=3, params={"profile": profile},
            depends_on=["shield.redact_audio"],
        ),
        StepPlan(
            name="ears.diarize", engine="ears", kind=StepKind.GPU,
            deadline_seconds=180, max_attempts=2, params={"profile": profile},
            depends_on=["ears.preprocess"],
        ),
        StepPlan(
            name="ears.transcribe_segments", engine="ears", kind=StepKind.GPU,
            deadline_seconds=420, max_attempts=2, params={"profile": profile},
            depends_on=["ears.preprocess"],
        ),
        StepPlan(
            name="ears.align", engine="ears", kind=StepKind.CPU,
            deadline_seconds=60, max_attempts=3, params={"profile": profile},
            depends_on=["ears.diarize", "ears.transcribe_segments"],
        ),
        # ── Join: backbone canonicalizes both branches together ──
        StepPlan(
            name="backbone.canonicalize_multimodal",
            engine="backbone", kind=StepKind.CPU,
            deadline_seconds=120, max_attempts=3, params={"kind": "multimodal"},
            depends_on=["eyes.build_evidence", "ears.align"],
        ),
    ]
    return WorkflowPlan(
        kind=WorkflowKind.MULTIMODAL,
        tenant_id=tenant_id,
        session_id=session_id,
        # Wall-time max = video chain (~1560s) since audio chain is shorter
        # and runs in parallel. Plus headroom.
        deadline_seconds=2400,
        metadata=metadata or {},
        steps=steps,
    )


# ─── Document canonicalization ──────────────────────────────────


def document_plan(
    tenant_id: str,
    session_id: str,
    *,
    metadata: dict | None = None,
) -> WorkflowPlan:
    """
    Document ingestion:
      1. shield.redact_text     (CPU)
      2. spine.parse            (CPU; PDF/DOCX/PPTX/CSV)
      3. spine.chunk_and_embed  (CPU + GPU embedding model)
      4. backbone.canonicalize  (CPU)
    """
    return WorkflowPlan(
        kind=WorkflowKind.DOCUMENT,
        tenant_id=tenant_id,
        session_id=session_id,
        deadline_seconds=600,
        metadata=metadata or {},
        steps=[
            StepPlan(
                name="shield.redact_text",
                engine="shield",
                kind=StepKind.CPU,
                deadline_seconds=60,
                max_attempts=3,
            ),
            StepPlan(
                name="spine.parse",
                engine="spine",
                kind=StepKind.CPU,
                deadline_seconds=120,
                max_attempts=3,
            ),
            StepPlan(
                name="spine.chunk_and_embed",
                engine="spine",
                kind=StepKind.GPU,
                deadline_seconds=180,
                max_attempts=2,
            ),
            StepPlan(
                name="backbone.canonicalize",
                engine="backbone",
                kind=StepKind.CPU,
                deadline_seconds=120,
                max_attempts=3,
                params={"kind": "document"},
            ),
        ],
    )


# ─── Canonical pipeline plan (Phase A — full legacy-parity DAG) ──


def canonical_pipeline_plan(
    tenant_id: str,
    session_id: str,
    *,
    has_audio: bool,
    has_video: bool,
    profile: str = "fast",
    artifact_id: str = "",
    audio_file_path: str = "",
    video_file_path: str = "",
    source_filename: str = "",
    source_type: str = "upload",
    created_by: str = "",
    media_fingerprint: str = "",
    metadata: dict | None = None,
) -> WorkflowPlan:
    """
    Phase A migration target — full canonical pipeline with legacy parity.

    Replaces the legacy `nexus.canonical-processing` HTTP-polling chain
    with a Phase 12 DAG that the WorkflowManager dispatches via Redis
    Streams. Every stage the legacy chain emitted has an equivalent
    step here, so callers that previously hit the orchestrator's
    `/process` endpoint get the same artifact state at the end —
    including the `canonical_artifacts` row (written by
    `spine.persist_artifact`) that powers the UI's artifact list.

    The shape adapts to `has_audio` / `has_video`:
      * audio-only    → 11 steps  (kind=AUDIO)
      * video-only    → 12 steps  (kind=VIDEO)
      * multimodal    → 19 steps  (kind=MULTIMODAL)

    Branches join at `spine.persist_artifact`, which writes the
    canonical_artifact row. `spine.persist_visual_evidence` and
    `spine.persist_action_evidence` then fan out from that row;
    `backbone.canonicalize*` is the terminal Neo4j + Milvus emission.

    Initial checkpoint carries the artifact_id + media paths + caller
    identity so each handler can read the slice it needs without an
    extra round-trip to the platform-api.
    """
    if not (has_audio or has_video):
        raise ValueError(
            "canonical_pipeline_plan requires has_audio or has_video"
        )

    if has_audio and has_video:
        kind = WorkflowKind.MULTIMODAL
        backbone_step_name = "backbone.canonicalize_multimodal"
        backbone_kind_param = "multimodal"
    elif has_audio:
        kind = WorkflowKind.AUDIO
        backbone_step_name = "backbone.canonicalize"
        backbone_kind_param = "audio"
    else:
        kind = WorkflowKind.VIDEO
        backbone_step_name = "backbone.canonicalize"
        backbone_kind_param = "video"

    steps: list[StepPlan] = []

    # ── Root: media probe (parallel with shield) ─────────────────
    # probe_media is just `ffprobe` — completes in ~1s for typical
    # uploads. The 180s deadline is headroom for the case where a
    # worker pod gets recreated mid-flight and the new consumer
    # reclaims the original envelope from the consumer-group's
    # pending list. With a 60s deadline that scenario expired on
    # reclaim and burned a retry; 180s absorbs typical orchestrator/
    # engine restart timings (<2min) without affecting healthy runs.
    steps.append(StepPlan(
        name="spine.probe_media",
        engine="spine",
        kind=StepKind.CPU,
        deadline_seconds=180,
        max_attempts=3,
        params={"profile": profile},
        depends_on=[],
    ))

    # ── Root: shield pre-redaction (parallel branches) ───────────
    if has_audio:
        steps.append(StepPlan(
            name="shield.redact_audio",
            engine="shield",
            kind=StepKind.CPU,
            deadline_seconds=30,
            max_attempts=3,
            params={"profile": profile},
            depends_on=[],
        ))
    if has_video:
        steps.append(StepPlan(
            name="shield.redact_video",
            engine="shield",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=[],
        ))

    # ── Audio branch (Phase 6 — 4 ears steps + text redact) ──────
    if has_audio:
        steps.append(StepPlan(
            name="ears.preprocess",
            engine="ears",
            kind=StepKind.CPU,
            deadline_seconds=120,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["shield.redact_audio"],
        ))
        steps.append(StepPlan(
            name="ears.diarize",
            engine="ears",
            kind=StepKind.GPU,
            deadline_seconds=300,
            max_attempts=2,
            params={"profile": profile},
            depends_on=["ears.preprocess"],
        ))
        steps.append(StepPlan(
            name="ears.transcribe_segments",
            engine="ears",
            kind=StepKind.GPU,
            deadline_seconds=600,
            max_attempts=2,
            params={"profile": profile},
            depends_on=["ears.preprocess"],
        ))
        steps.append(StepPlan(
            name="ears.align",
            engine="ears",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["ears.diarize", "ears.transcribe_segments"],
        ))
        # PII redact the now-finalised transcript text.
        steps.append(StepPlan(
            name="shield.redact_text",
            engine="shield",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["ears.align"],
        ))

    # ── Video branch (Phase 5-A — 6 eyes steps) ──────────────────
    if has_video:
        steps.append(StepPlan(
            name="eyes.extract_frames",
            engine="eyes",
            kind=StepKind.CPU,
            deadline_seconds=180,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["shield.redact_video"],
        ))
        steps.append(StepPlan(
            name="eyes.detect_scenes",
            engine="eyes",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["eyes.extract_frames"],
        ))
        steps.append(StepPlan(
            name="eyes.ocr_frames",
            engine="eyes",
            kind=StepKind.CPU,
            # EasyOCR on CPU is 30-100s per representative frame depending
            # on text density. With ~23 rep frames typical for a 1-min
            # screen recording and 2 OCR workers, the worst-case wall
            # time is ~20 min. Set the per-step deadline accordingly so a
            # text-heavy upload doesn't quarantine on a tight deadline.
            deadline_seconds=1200,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["eyes.detect_scenes"],
        ))
        steps.append(StepPlan(
            name="eyes.analyze_scenes",
            engine="eyes",
            kind=StepKind.GPU,
            deadline_seconds=600,
            max_attempts=2,
            params={"profile": profile},
            depends_on=["eyes.ocr_frames"],
        ))
        steps.append(StepPlan(
            name="eyes.analyze_transitions",
            engine="eyes",
            kind=StepKind.GPU,
            deadline_seconds=300,
            max_attempts=2,
            params={"profile": profile},
            depends_on=["eyes.analyze_scenes"],
        ))
        steps.append(StepPlan(
            name="eyes.build_evidence",
            engine="eyes",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["eyes.analyze_transitions"],
        ))
        # Visual flow graph — runs in parallel with persist_artifact's
        # other branches; persist_artifact waits for it so the row has
        # visual_graph data attached.
        steps.append(StepPlan(
            name="spine.build_visual_graph",
            engine="spine",
            kind=StepKind.CPU,
            deadline_seconds=120,
            max_attempts=3,
            params={"profile": profile},
            depends_on=["eyes.build_evidence"],
        ))

    # ── Phase 3: persist-first / enrich-async ─────────────────────
    #
    # Architectural pivot: write the canonical_artifacts row EARLY
    # with whatever data is cheaply available (probe + transcript +
    # basic frames), so the user sees an artifact within a few
    # minutes on CPU instead of the 25-40 min it took to wait for
    # the full eyes branch (OCR + LLaVA + analysis). Heavy visual
    # enrichment then runs async and UPDATES the row when done.
    #
    # Reliability win: if enrichment fails (vision model down, OCR
    # timeout, container restart), the minimal artifact persists.
    # The previous design lost everything on enrichment failure.
    #
    # Status lifecycle on the canonical_artifacts row:
    #   minimal     ← spine.persist_minimal_artifact writes this
    #   persisted   ← spine.update_artifact_enriched updates this
    #   completed   ← backbone canonicalize promotes (existing)
    #
    persist_minimal_deps: list[str] = ["spine.probe_media"]
    if has_audio:
        # Need redacted transcript text to write into the row.
        persist_minimal_deps.append("shield.redact_text")
    if has_video:
        # Just need the basic frame manifest, NOT OCR/LLaVA results.
        persist_minimal_deps.append("eyes.detect_scenes")
    steps.append(StepPlan(
        name="spine.persist_minimal_artifact",
        engine="spine",
        kind=StepKind.CPU,
        deadline_seconds=60,
        max_attempts=3,
        params={"profile": profile},
        depends_on=persist_minimal_deps,
    ))

    # ── Enrichment fold-in (video only — audio is already done) ──
    if has_video:
        update_enriched_deps = [
            "spine.persist_minimal_artifact",
            "eyes.build_evidence",
            "spine.build_visual_graph",
        ]
        steps.append(StepPlan(
            name="spine.update_artifact_enriched",
            engine="spine",
            kind=StepKind.CPU,
            deadline_seconds=60,
            max_attempts=3,
            params={"profile": profile},
            depends_on=update_enriched_deps,
        ))
        enriched_artifact_step = "spine.update_artifact_enriched"
    else:
        # Audio-only path doesn't have a separate enrichment phase —
        # the minimal artifact already has the full audio transcript.
        # Downstream evidence/canonicalize hang off the minimal step.
        enriched_artifact_step = "spine.persist_minimal_artifact"

    # ── Post-persist evidence rows (each branch fan-out) ─────────
    backbone_deps: list[str] = [enriched_artifact_step]
    if has_video:
        steps.append(StepPlan(
            name="spine.persist_visual_evidence",
            engine="spine",
            kind=StepKind.CPU,
            deadline_seconds=300,
            max_attempts=3,
            params={"profile": profile},
            depends_on=[enriched_artifact_step, "eyes.build_evidence"],
        ))
        backbone_deps.append("spine.persist_visual_evidence")
    if has_audio:
        steps.append(StepPlan(
            name="spine.persist_action_evidence",
            engine="spine",
            kind=StepKind.CPU,
            deadline_seconds=600,
            max_attempts=3,
            params={"profile": profile},
            depends_on=[enriched_artifact_step, "ears.align"],
        ))
        backbone_deps.append("spine.persist_action_evidence")

    # ── Terminal: backbone canonicalize (Neo4j + Milvus) ─────────
    steps.append(StepPlan(
        name=backbone_step_name,
        engine="backbone",
        kind=StepKind.CPU,
        deadline_seconds=180,
        max_attempts=3,
        params={"kind": backbone_kind_param},
        depends_on=backbone_deps,
    ))

    # Initial checkpoint — the slice every handler reads. Storing it
    # in initial_state means the first dispatched envelope already
    # carries it; no extra round-trip to platform-api.
    initial_state: dict[str, Any] = {
        "artifact_id": artifact_id,
        "source_type": source_type,
        "source_filename": source_filename,
        "created_by": created_by,
        "media_fingerprint": media_fingerprint,
        "processing_profile": profile,
    }
    if audio_file_path:
        initial_state["audio_file_path"] = audio_file_path
    if video_file_path:
        initial_state["video_file_path"] = video_file_path

    # Wall-time budget. Multimodal is bound by the longer of the two
    # parallel branches. CPU-bound OCR + LLaVA on a typical screen
    # recording (~30 representative frames, text-heavy) needs ~25-30
    # minutes on a Docker Desktop / WSL2 host without a GPU; allow
    # 60 min for multimodal to absorb that without quarantining.
    # Operators on GPU hosts can override per upload by passing a
    # smaller deadline via the orchestrator.
    if kind == WorkflowKind.MULTIMODAL:
        deadline = 3600
    elif kind == WorkflowKind.VIDEO:
        deadline = 3000
    else:
        deadline = 1500

    return WorkflowPlan(
        kind=kind,
        tenant_id=tenant_id,
        session_id=session_id,
        deadline_seconds=deadline,
        initial_state=initial_state,
        metadata=metadata or {},
        steps=steps,
    )


# ─── Registry ───────────────────────────────────────────────────


PlanFactory = Callable[..., WorkflowPlan]

CANONICAL_PLANS: dict[WorkflowKind, PlanFactory] = {
    WorkflowKind.AUDIO: audio_plan,
    WorkflowKind.VIDEO: video_plan,
    WorkflowKind.MULTIMODAL: multimodal_plan,
    WorkflowKind.DOCUMENT: document_plan,
}


def build_plan(
    kind: WorkflowKind | str,
    tenant_id: str,
    session_id: str,
    **kwargs,
) -> WorkflowPlan:
    """Resolve a plan factory by kind. Raises ValueError on unknown kind."""
    if isinstance(kind, str):
        kind = WorkflowKind(kind)
    factory = CANONICAL_PLANS.get(kind)
    if factory is None:
        raise ValueError(f"no canonical plan registered for kind={kind.value}")
    return factory(tenant_id, session_id, **kwargs)
