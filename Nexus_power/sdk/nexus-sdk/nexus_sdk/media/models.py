"""
Nexus SDK — Shared Media Models.

Canonical Pydantic models for audio transcription and visual analysis,
shared across Ears Engine, Eyes Engine, Heart Engine, and Platform API.

These models are the single source of truth for media data structures.
All engines MUST use these models — not local duplicates.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

__all__ = [
    "ApplicationType",
    "MediaJobStatus",
    "AudioFormat",
    "VideoFormat",
    "AudioMetadata",
    "SpeakerInfo",
    "TranscriptionSegment",
    "TranscriptionResult",
    "UIElement",
    "FrameAnalysis",
    "SceneTransitionAnalysis",
    "VisualAnalysisResult",
    "AudioProcessingJob",
    "VideoProcessingJob",
    "CanonicalMediaArtifact",
]
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ─── Enums ─────────────────────────────────────────────────────


class ApplicationType(str, Enum):
    """Classification of the application shown in a screenshot."""
    WEB_UI = "web_ui"
    DESKTOP_APP = "desktop_app"
    EXCEL_SPREADSHEET = "excel_spreadsheet"
    MAINFRAME_3270 = "mainframe_3270"
    PDF_DOCUMENT = "pdf_document"
    EMAIL_CLIENT = "email_client"
    TERMINAL = "terminal"
    DATABASE_UI = "database_ui"
    UNKNOWN = "unknown"


class MediaJobStatus(str, Enum):
    """Processing status for audio/video jobs."""
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    PROCESSING = "processing"
    ALIGNING = "aligning"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AudioFormat(str, Enum):
    """Supported audio input formats."""
    WAV = "wav"
    MP3 = "mp3"
    WEBM = "webm"
    OGG = "ogg"
    FLAC = "flac"
    M4A = "m4a"
    AAC = "aac"


class VideoFormat(str, Enum):
    """Supported video input formats."""
    MP4 = "mp4"
    WEBM = "webm"
    AVI = "avi"
    MKV = "mkv"
    MOV = "mov"


# ─── Audio / Transcription Models ──────────────────────────────


class AudioMetadata(BaseModel):
    """Technical metadata extracted from an audio file."""

    file_path: str = Field(..., description="Path to the audio file on disk")
    original_filename: str = Field(default="", description="Original uploaded filename")
    format: str = Field(default="wav", description="Audio format (wav, mp3, webm, etc.)")
    sample_rate: int = Field(default=16000, description="Sample rate in Hz")
    channels: int = Field(default=1, description="Number of audio channels")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Audio duration in seconds")
    file_size_bytes: int = Field(default=0, ge=0, description="File size in bytes")
    bit_depth: int = Field(default=16, description="Bit depth (16, 24, 32)")
    codec: str = Field(default="pcm_s16le", description="Audio codec")

    # Preprocessing metadata
    normalized: bool = Field(default=False, description="Whether audio was normalized")
    noise_reduced: bool = Field(default=False, description="Whether noise reduction was applied")
    resampled_from: Optional[int] = Field(
        default=None, description="Original sample rate if resampled"
    )


class SpeakerInfo(BaseModel):
    """Information about a detected speaker."""

    speaker_id: str = Field(..., description="Speaker label (e.g., SPEAKER_00)")
    display_name: Optional[str] = Field(
        default=None, description="Human-readable name if identified"
    )
    total_speaking_time: float = Field(
        default=0.0, ge=0.0, description="Total speaking duration in seconds"
    )
    segment_count: int = Field(
        default=0, ge=0, description="Number of segments attributed to this speaker"
    )
    average_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Average transcription confidence for this speaker"
    )
    embedding_id: Optional[str] = Field(
        default=None, description="Voice embedding reference for speaker re-identification"
    )


class TranscriptionSegment(BaseModel):
    """
    A single time-aligned transcription segment with speaker attribution.

    This is the atomic unit of a transcript — one continuous utterance
    from one speaker with a start/end time and confidence score.
    """

    segment_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique segment identifier",
    )
    speaker: str = Field(..., description="Speaker label (SPEAKER_00, SPEAKER_01, ...)")
    text: str = Field(..., description="Transcribed text content")
    start_time: float = Field(..., ge=0.0, description="Start time in seconds")
    end_time: float = Field(..., ge=0.0, description="End time in seconds")
    confidence: float = Field(
        default=0.0, ge=-100.0, le=1.0,
        description="Transcription confidence (0.0-1.0 or negative log-prob from Whisper)",
    )
    language: str = Field(default="en", description="Detected language code")

    # Word-level timing (optional, from Whisper word_timestamps=True)
    words: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Word-level timestamps: [{word, start, end, probability}]",
    )

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:
        start = info.data.get("start_time", 0.0)
        if v < start:
            raise ValueError(f"end_time ({v}) must be >= start_time ({start})")
        return v

    @property
    def duration(self) -> float:
        """Duration of this segment in seconds."""
        return self.end_time - self.start_time

    @property
    def word_count(self) -> int:
        """Number of words in this segment."""
        return len(self.text.split()) if self.text.strip() else 0


class TranscriptionResult(BaseModel):
    """
    Complete transcription of an audio file / KT session recording.

    Contains all aligned segments, speaker information, and metadata.
    This is the final output of the Ears Engine pipeline.
    """

    job_id: str = Field(..., description="Processing job ID")
    session_id: str = Field(..., description="KT session this transcript belongs to")
    tenant_id: str = Field(default="", description="Tenant that owns this transcript")

    # Content
    segments: list[TranscriptionSegment] = Field(
        default_factory=list, description="Time-aligned transcript segments"
    )
    transcript_text: str = Field(
        default="",
        description="Concatenated transcript text for downstream workflow consumption",
    )
    speakers: list[SpeakerInfo] = Field(
        default_factory=list, description="Detected speakers with statistics"
    )

    # Audio metadata
    audio: Optional[AudioMetadata] = Field(
        default=None, description="Source audio file metadata"
    )
    duration_seconds: float = Field(
        default=0.0, ge=0.0, description="Total audio duration in seconds"
    )

    # Stats
    language: str = Field(default="en", description="Primary detected language")
    word_count: int = Field(default=0, ge=0, description="Total word count")
    segment_count: int = Field(default=0, ge=0, description="Total segment count")

    # Processing metadata
    model_version: str = Field(
        default="whisper-large-v3", description="Whisper model version used"
    )
    processing_time_seconds: float = Field(
        default=0.0, ge=0.0, description="Wall-clock processing time"
    )
    pipeline_stages: list[str] = Field(
        default_factory=list,
        description="Pipeline stages completed (e.g., ['preprocess', 'diarize', 'transcribe', 'align'])",
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def full_text(self) -> str:
        """Concatenated full transcript text."""
        return " ".join(s.text for s in self.segments if s.text.strip())

    def speaker_names(self) -> list[str]:
        """List of unique speaker labels."""
        return list(dict.fromkeys(s.speaker for s in self.segments))

    def segments_by_speaker(self, speaker: str) -> list[TranscriptionSegment]:
        """Filter segments for a specific speaker."""
        return [s for s in self.segments if s.speaker == speaker]

    def compute_stats(self) -> None:
        """Recalculate statistics from segments. Call after modifying segments."""
        self.transcript_text = self.full_text
        self.segment_count = len(self.segments)
        self.word_count = sum(s.word_count for s in self.segments)
        if self.segments:
            self.duration_seconds = max(s.end_time for s in self.segments)

        # Build speaker info
        speaker_map: dict[str, SpeakerInfo] = {}
        for seg in self.segments:
            if seg.speaker not in speaker_map:
                speaker_map[seg.speaker] = SpeakerInfo(speaker_id=seg.speaker)
            info = speaker_map[seg.speaker]
            info.total_speaking_time += seg.duration
            info.segment_count += 1
        # Compute average confidence per speaker
        for spk, info in speaker_map.items():
            segs = self.segments_by_speaker(spk)
            if segs:
                info.average_confidence = sum(
                    max(0, s.confidence) for s in segs
                ) / len(segs)
        self.speakers = list(speaker_map.values())


# ─── Video / Visual Analysis Models ───────────────────────────


class UIElement(BaseModel):
    """A detected UI element in a screenshot/frame."""

    element_type: str = Field(
        ...,
        description="Element type: button, text_field, dropdown, label, table, menu, checkbox, radio, link, image, tab, etc.",
    )
    text: str = Field(default="", description="Text content of the element")
    bbox: list[float] = Field(
        default_factory=list,
        description="Bounding box [x1, y1, x2, y2] in pixels",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Detection confidence"
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional properties (enabled, selected, value, placeholder, etc.)",
    )

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, v: list[float]) -> list[float]:
        if v and len(v) != 4:
            raise ValueError(f"bbox must have exactly 4 values [x1, y1, x2, y2], got {len(v)}")
        return v


class FrameAnalysis(BaseModel):
    """
    Analysis result for a single video frame or screenshot.

    Contains OCR text, detected UI elements, application classification,
    and a natural-language description of the screen content.
    """

    frame_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique frame identifier",
    )
    frame_index: int = Field(..., ge=0, description="Sequential frame number")
    timestamp_seconds: float = Field(
        ..., ge=0.0, description="Timestamp in the source video (seconds)"
    )

    # Classification
    application_type: ApplicationType = Field(
        default=ApplicationType.UNKNOWN,
        description="Detected application type",
    )
    page_title: str = Field(default="", description="Page or dialog title if detected")
    url_or_path: str = Field(default="", description="URL or file path if visible")

    # Content
    ui_elements: list[UIElement] = Field(
        default_factory=list, description="Detected UI elements"
    )
    extracted_text: str = Field(
        default="", description="Full OCR text extracted from frame"
    )
    tables: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Structured data tables detected in the frame",
    )
    state_changes: list[str] = Field(
        default_factory=list,
        description="Changes detected from the previous frame",
    )
    description: str = Field(
        default="",
        description="Natural-language description of what the screen shows",
    )

    @field_validator("ui_elements", "tables", "state_changes", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list:
        """Coerce non-list LLM responses (empty string, None, scalar) to empty list."""
        if v is None or v == "" or v == "null":
            return []
        if not isinstance(v, list):
            return []
        return v

    # Quality metadata
    frame_path: str = Field(default="", description="Absolute path to the extracted frame image on the engine host")
    frame_asset_path: str = Field(
        default="",
        description=(
            "Relative path from frames_storage_path root, e.g. "
            "'{tenant_id}/{session_id}/{job_id}_frames/frame_00001.png'. "
            "Used by gateway/client to construct the HTTP serving URL. "
            "Eyes never embeds a host or port — callers build the absolute URL."
        ),
    )
    ocr_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Average OCR confidence for this frame"
    )
    is_keyframe: bool = Field(
        default=False, description="Whether this is a significant state-change frame"
    )


class SceneTransitionAnalysis(BaseModel):
    """Vision-model analysis of a single step between two consecutive UI states.

    Produced by comparing the **exit** frame of scene *N* (last frame in that
    Eyes scene group) with the **entry** frame of scene *N+1* (first frame).
    FlowBuilder matches edges using
    ``(from_scene chronological_exit_frame_id, to_scene chronological_entry_frame_id)``.
    """

    from_frame_id: str = Field(..., description="Frame id of the pre-action screen")
    to_frame_id: str = Field(..., description="Frame id of the post-action screen")
    action_kind: str = Field(
        default="unknown",
        description=(
            "Canonical verb: click_cta, enter_text, select_option, toggle, "
            "navigate, submit_form, scroll, unknown"
        ),
    )
    action_label: str = Field(default="", description="Short human-readable step label")
    target_element_label: str = Field(
        default="", description="Control or region the user interacted with"
    )
    observed_value: str = Field(
        default="", description="Value entered or selected, when applicable"
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Model confidence 0-1"
    )
    evidence_text: str = Field(
        default="", description="One-sentence justification from the model"
    )


class VisualAnalysisResult(BaseModel):
    """
    Complete visual analysis of a video recording or screenshot batch.

    This is the final output of the Eyes Engine pipeline.
    """

    job_id: str = Field(..., description="Processing job ID")
    session_id: str = Field(..., description="KT session this analysis belongs to")
    tenant_id: str = Field(default="", description="Tenant that owns this analysis")

    # Content
    frames: list[FrameAnalysis] = Field(
        default_factory=list, description="All analyzed frames"
    )
    scene_transitions: list[SceneTransitionAnalysis] = Field(
        default_factory=list,
        description=(
            "Pair-wise transition LLM analysis between consecutive Eyes scenes "
            "(exit frame → entry frame). Empty when EYES_TRANSITION_LLM is off "
            "or the model is unavailable."
        ),
    )

    # Stats
    total_frames_extracted: int = Field(
        default=0, ge=0, description="Total raw frames extracted from video"
    )
    total_frames_analyzed: int = Field(
        default=0, ge=0, description="Frames that passed diff filter and were analyzed"
    )
    duration_seconds: float = Field(
        default=0.0, ge=0.0, description="Video duration in seconds"
    )
    application_types_seen: list[str] = Field(
        default_factory=list,
        description="List of unique application types detected",
    )
    workflow_summary: str = Field(
        default="",
        description="AI-generated summary of the workflow shown in the recording",
    )

    # Processing metadata
    processing_time_seconds: float = Field(
        default=0.0, ge=0.0, description="Wall-clock processing time"
    )
    pipeline_stages: list[str] = Field(
        default_factory=list,
        description="Pipeline stages completed (e.g., ['extract', 'ocr', 'classify', 'analyze'])",
    )
    model_version: str = Field(
        default="", description="Vision model used for analysis (e.g. llava:7b, llama3.2-vision:11b)"
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def compute_stats(self) -> None:
        """Recalculate statistics from frames."""
        self.total_frames_analyzed = len(self.frames)
        if self.frames:
            self.duration_seconds = max(
                f.timestamp_seconds for f in self.frames
            )
            self.application_types_seen = list(
                dict.fromkeys(f.application_type.value for f in self.frames)
            )


# ─── Processing Job Models ────────────────────────────────────


class AudioProcessingJob(BaseModel):
    """Tracks an audio transcription processing job."""

    job_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique job ID",
    )
    tenant_id: str = Field(..., description="Tenant that submitted the job")
    session_id: str = Field(..., description="KT session ID")
    status: MediaJobStatus = Field(default=MediaJobStatus.QUEUED)

    # Input
    audio_path: str = Field(default="", description="Path to the source audio file")
    original_filename: str = Field(default="", description="Original upload filename")
    language: str = Field(default="en", description="Language hint for transcription")
    num_speakers: Optional[int] = Field(
        default=None, description="Expected number of speakers (None = auto-detect)"
    )
    parameters: dict = Field(default_factory=dict, description="Additional processing parameters")

    # Output
    result: Optional[TranscriptionResult] = Field(
        default=None, description="Transcription result when completed"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")

    # Result summary (mirrors DB columns for quick access)
    segment_count: int = Field(default=0, description="Number of transcript segments")
    speaker_count: int = Field(default=0, description="Number of distinct speakers")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Audio duration")
    word_count: int = Field(default=0, description="Total word count")

    # Pipeline
    pipeline_stages: list[str] = Field(default_factory=list, description="Applied pipeline stages")

    # Timing
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    processing_time_seconds: float = Field(default=0.0, ge=0.0)

    # Progress tracking
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    current_stage: str = Field(
        default="queued",
        description="Current pipeline stage: queued, preprocessing, diarizing, transcribing, aligning, completed",
    )

    @property
    def source_file_path(self) -> str:
        """Alias for audio_path — matches DB column name."""
        return self.audio_path


class VideoProcessingJob(BaseModel):
    """Tracks a video visual analysis processing job."""

    job_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique job ID",
    )
    tenant_id: str = Field(..., description="Tenant that submitted the job")
    session_id: str = Field(..., description="KT session ID")
    status: MediaJobStatus = Field(default=MediaJobStatus.QUEUED)

    # Input
    video_path: str = Field(default="", description="Path to the source video file")
    original_filename: str = Field(default="", description="Original upload filename")
    parameters: dict = Field(default_factory=dict, description="Additional processing parameters")

    # Output
    result: Optional[VisualAnalysisResult] = Field(
        default=None, description="Visual analysis result when completed"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")

    # Result summary (mirrors DB columns for quick access)
    frame_count: int = Field(default=0, description="Total frames extracted")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Video duration")

    # Pipeline
    pipeline_stages: list[str] = Field(default_factory=list, description="Applied pipeline stages")

    # Timing
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    processing_time_seconds: float = Field(default=0.0, ge=0.0)

    # Progress tracking
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    current_stage: str = Field(
        default="queued",
        description="Current pipeline stage: queued, extracting, ocr, classifying, analyzing, completed",
    )

    @property
    def source_file_path(self) -> str:
        """Alias for video_path — matches DB column name."""
        return self.video_path


# ─── Canonical Media Artifact ──────────────────────────────────


class CanonicalMediaArtifact(BaseModel):
    """Unified canonical artifact produced by the canonical processing chain.

    Combines transcript, PII-safe text, visual analysis, and visual graph
    into a single immutable artifact that downstream consumer chains
    (QA testing, compliance audit, etc.) consume.
    """

    artifact_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique artifact identifier",
    )
    tenant_id: str = Field(..., description="Owning tenant")
    session_id: str = Field(..., description="KT session this artifact belongs to")

    # Media fingerprint for re-upload deduplication
    media_fingerprint: str = Field(
        default="",
        description="SHA-256 of source media files for re-upload detection",
    )

    # Status
    status: str = Field(
        default="pending",
        description="pending, processing, completed, failed",
    )

    # Probe metadata
    duration_seconds: float = Field(default=0.0, ge=0.0)
    audio_codec: str = Field(default="")
    video_codec: str = Field(default="")
    video_resolution: str = Field(default="")

    # Transcript (PII-safe)
    safe_transcript_text: str = Field(
        default="",
        description="Full transcript after PII redaction",
    )
    raw_transcript_text: str = Field(
        default="",
        description="Original transcript before redaction (if PII skipped)",
    )
    speaker_count: int = Field(default=0)
    word_count: int = Field(default=0)

    # Visual analysis
    scene_count: int = Field(default=0)
    frame_count: int = Field(default=0)
    application_types_seen: list[str] = Field(default_factory=list)
    visual_summary: str = Field(
        default="",
        description="Natural-language summary of the visual workflow",
    )

    # Visual graph
    screen_flow_graph: Optional[dict] = Field(
        default=None,
        description="Directed graph of screen transitions",
    )

    # Quality gate
    brain_quality_score: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Brain engine confidence score (0-1)",
    )
    quality_gate_passed: bool = Field(default=False)

    # Full nested results (for deep consumers)
    transcript_result: Optional[dict] = Field(
        default=None,
        description="Complete TranscriptionResult as dict",
    )
    visual_result: Optional[dict] = Field(
        default=None,
        description="Complete VisualAnalysisResult as dict",
    )

    # Processing metadata
    processing_time_seconds: float = Field(default=0.0, ge=0.0)
    pipeline_stages: list[str] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    completed_at: Optional[datetime] = Field(default=None)
    error: Optional[str] = Field(default=None)
