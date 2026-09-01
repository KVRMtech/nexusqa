"""
Phase 5-A — Manifest schemas for the eyes per-step pipeline.

Each step writes a manifest (JSON) to the artifact store. The next
step reads its manifest and the previous steps' as needed. The
checkpoint carries only the *keys* of these manifests, so
`workflow_state.checkpoint` stays compact regardless of video length.

Design contract (input → output for each step):

    ┌──────────────────────┐
    │ input_artifact_key   │  (video file in artifact store)
    └─────────┬────────────┘
              │
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.extract_frames  (CPU, ~30s)     │
    │   reads:  input_artifact_key         │
    │   writes: FramesManifest             │ → checkpoint.frames_manifest_key
    └─────────┬────────────────────────────┘
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.detect_scenes   (CPU, ~20s)     │
    │   reads:  FramesManifest             │
    │   writes: ScenesManifest             │ → checkpoint.scenes_manifest_key
    └─────────┬────────────────────────────┘
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.ocr_frames      (CPU, ~60-90s)  │
    │   reads:  FramesManifest, Scenes     │
    │   writes: OCRManifest                │ → checkpoint.ocr_manifest_key
    └─────────┬────────────────────────────┘
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.analyze_scenes  (GPU, ~60-180s) │
    │   reads:  Scenes, OCR                │
    │   writes: EnrichedScenesManifest     │ → checkpoint.enriched_scenes_key
    └─────────┬────────────────────────────┘
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.analyze_transitions (GPU, 30-90s)│
    │   reads:  EnrichedScenesManifest     │
    │   writes: TransitionsManifest        │ → checkpoint.transitions_manifest_key
    └─────────┬────────────────────────────┘
              ▼
    ┌──────────────────────────────────────┐
    │ eyes.build_evidence  (CPU, ~30s)     │
    │   reads:  all of the above           │
    │   writes: VisualAnalysisResult       │ → checkpoint.eyes_result_key
    └──────────────────────────────────────┘

Why JSON manifests + artifact keys instead of inlining state in the
checkpoint:
  - A 15-min video produces 100-300 unique frames after dHash dedup
  - A 60-min screen recording can produce 1000+ frames
  - Inlining frame metadata in workflow_state.checkpoint (which is a
    Postgres JSON column) bloats the row to hundreds of KB and slows
    every dispatcher poll
  - Putting the manifest in object storage keeps the row at <2 KB
    regardless of media length

Schema versioning: every manifest has a `schema_version` field. Bump
when fields are removed or semantics change. Additive changes don't
bump.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FrameRecord(BaseModel):
    """One frame extracted by ffmpeg. The hash is dHash-derived; used
    by detect_scenes for similarity grouping."""

    index: int = Field(..., description="Monotonic frame index (0-based)")
    timestamp_ms: int = Field(
        ..., description="Wall-clock timestamp within the source video, in ms"
    )
    artifact_key: str = Field(
        ..., description="Object-storage key for the frame PNG"
    )
    hash: str = Field(..., description="dHash hex string for similarity grouping")
    source_frame_idx: int = Field(
        ..., description="Index in the original video before dedup"
    )
    width: int = 0
    height: int = 0
    size_bytes: int = 0


class FramesManifest(BaseModel):
    """Output of eyes.extract_frames. Lists every unique frame plus
    enough source-video metadata to drive every downstream step."""

    schema_version: int = 1
    job_id: str
    tenant_id: str
    session_id: str
    workflow_id: str
    source_video_artifact_key: str
    duration_seconds: float
    fps: float
    frames: list[FrameRecord]
    pipeline_stages: list[str] = Field(default_factory=list)


class SceneRecord(BaseModel):
    """A grouped set of frames that share visual structure (dHash
    similarity below a threshold). Representative frame is used for
    expensive LLaVA enrichment."""

    scene_id: str = Field(..., description="Stable id within this workflow")
    representative_frame_idx: int
    frame_indices: list[int] = Field(
        ..., description="Indices into FramesManifest.frames"
    )
    start_ms: int
    end_ms: int
    # Populated by ocr_frames after this manifest is written.
    representative_ocr_text: Optional[str] = None
    merged_ocr_text: Optional[str] = None


class ScenesManifest(BaseModel):
    """Output of eyes.detect_scenes."""

    schema_version: int = 1
    job_id: str
    workflow_id: str
    frames_manifest_key: str
    scenes: list[SceneRecord]
    enrichment_scene_ids: list[str] = Field(
        default_factory=list,
        description="Subset of scenes the GPU step should enrich; "
        "smaller than `scenes` for budget reasons on long videos",
    )
    pipeline_stages: list[str] = Field(default_factory=list)


class OCRResult(BaseModel):
    """OCR for one frame. Lines are post-cleanup."""

    frame_idx: int
    text: str = ""
    lines: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class OCRManifest(BaseModel):
    """Output of eyes.ocr_frames. Sparse — only the frames actually
    OCR'd are listed (rep frames + per-frame inside multi-frame
    scenes when configured)."""

    schema_version: int = 1
    job_id: str
    workflow_id: str
    scenes_manifest_key: str
    profile: str = Field(..., description='"fast" | "standard" | "multimodal"')
    results: list[OCRResult]
    skipped_frame_count: int = 0
    pipeline_stages: list[str] = Field(default_factory=list)


class UIElement(BaseModel):
    """A single visible UI element identified by LLaVA + post-processing."""

    element_type: str
    text: str = ""
    bbox: Optional[list[float]] = None
    entity_id: Optional[str] = None
    persistence_count: int = 1
    properties: dict[str, Any] = Field(default_factory=dict)


class EnrichedScene(BaseModel):
    """A scene after the GPU enrichment pass."""

    scene_id: str
    representative_frame_idx: int
    description: str = ""
    ui_elements: list[UIElement] = Field(default_factory=list)
    application_type: Optional[str] = None
    enrichment_model: str = ""


class EnrichedScenesManifest(BaseModel):
    """Output of eyes.analyze_scenes."""

    schema_version: int = 1
    job_id: str
    workflow_id: str
    ocr_manifest_key: str
    enriched: list[EnrichedScene]
    skipped_scene_ids: list[str] = Field(
        default_factory=list,
        description="Scenes with propagated metadata from a neighbour "
        "instead of a dedicated LLaVA call",
    )
    pipeline_stages: list[str] = Field(default_factory=list)


class TransitionRecord(BaseModel):
    """A transition between two adjacent scenes, as classified by the
    transitions LLM pass."""

    from_scene_id: str
    to_scene_id: str
    kind: str = Field(
        default="unknown",
        description="navigate | submit | scroll | unknown | …",
    )
    reason: str = ""
    confidence: float = 0.0


class TransitionsManifest(BaseModel):
    """Output of eyes.analyze_transitions (second GPU pass)."""

    schema_version: int = 1
    job_id: str
    workflow_id: str
    enriched_scenes_key: str
    transitions: list[TransitionRecord]
    pipeline_stages: list[str] = Field(default_factory=list)


# ─── Manifest envelope ─────────────────────────────────────────


class ManifestRef(BaseModel):
    """A typed pointer to a manifest in the artifact store. Stored in
    the workflow checkpoint instead of the manifest payload itself."""

    kind: str = Field(
        ...,
        description="frames | scenes | ocr | enriched | transitions | result",
    )
    artifact_key: str
    schema_version: int = 1
    size_bytes: int = 0


# Manifest names exported in __all__ so the workflow handlers can do
# `from .manifests import *` safely.
__all__ = [
    "FrameRecord",
    "FramesManifest",
    "SceneRecord",
    "ScenesManifest",
    "OCRResult",
    "OCRManifest",
    "UIElement",
    "EnrichedScene",
    "EnrichedScenesManifest",
    "TransitionRecord",
    "TransitionsManifest",
    "ManifestRef",
]
