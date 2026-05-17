"""
Nexus SDK — Media Processing Module.

Shared models, utilities, and abstractions for audio and video processing
used by the Ears Engine (speech-to-text) and Eyes Engine (visual intelligence).

Sub-modules:
    media.models    — Pydantic models for transcripts, frames, speakers
    media.audio     — Audio preprocessing (FFmpeg, VAD, format conversion)
    media.video     — Video frame extraction and management  (planned)
    media.storage   — File-based media asset management      (planned)
"""

from nexus_sdk.media.models import (
    # Enums
    ApplicationType,
    AudioFormat,
    MediaJobStatus,
    VideoFormat,
    # Audio / Transcription
    AudioMetadata,
    AudioProcessingJob,
    SpeakerInfo,
    TranscriptionResult,
    TranscriptionSegment,
    # Video / Visual
    FrameAnalysis,
    UIElement,
    VideoProcessingJob,
    VisualAnalysisResult,
)

from nexus_sdk.media.audio import (
    AudioPreprocessor,
    PreprocessConfig,
    PreprocessResult,
    SileroVAD,
    chunk_audio,
    probe_audio,
    transcode_to_wav,
)

from nexus_sdk.media.speech import (
    SpeechProvider,
    StubSpeechProvider,
    TranscriptionConfig,
    TranscriptionOutput,
    TranscriptionSegment as SpeechSegment,
)

from nexus_sdk.media.vision import (
    FrameAnalysisContext,
    FrameAnalysisResult,
    OCRProvider,
    OCRResult,
    StubOCRProvider,
    StubVisionProvider,
    VisionProvider,
)

__all__ = [
    # Enums
    "ApplicationType",
    "AudioFormat",
    "MediaJobStatus",
    "VideoFormat",
    # Audio models
    "AudioMetadata",
    "AudioProcessingJob",
    "SpeakerInfo",
    "TranscriptionResult",
    "TranscriptionSegment",
    # Visual models
    "FrameAnalysis",
    "UIElement",
    "VideoProcessingJob",
    "VisualAnalysisResult",
    # Audio preprocessing
    "AudioPreprocessor",
    "PreprocessConfig",
    "PreprocessResult",
    "SileroVAD",
    "chunk_audio",
    "probe_audio",
    "transcode_to_wav",
    # Speech provider abstraction
    "SpeechProvider",
    "StubSpeechProvider",
    "TranscriptionConfig",
    "TranscriptionOutput",
    "SpeechSegment",
    # Vision provider abstraction
    "FrameAnalysisContext",
    "FrameAnalysisResult",
    "OCRProvider",
    "OCRResult",
    "StubOCRProvider",
    "StubVisionProvider",
    "VisionProvider",
]
