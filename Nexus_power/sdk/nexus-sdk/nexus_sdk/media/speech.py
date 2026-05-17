"""
Nexus SDK — SpeechProvider Abstraction.

Defines the abstract interface for speech-to-text backends so that
engines like Ears can swap providers (Whisper, Azure Speech, GCP,
Deepgram, etc.) without rewriting business logic.

Usage:
    from nexus_sdk.media.speech import SpeechProvider, TranscriptionConfig

    class WhisperSpeechProvider(SpeechProvider):
        async def transcribe(self, audio_path, config):
            ...
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionConfig:
    """Configuration for a single transcription request."""

    language: Optional[str] = None
    beam_size: int = 5
    word_timestamps: bool = True
    vad_filter: bool = True
    vad_threshold: float = 0.5
    min_silence_duration_ms: int = 500
    vocabulary_boost: list[str] = field(default_factory=list)
    processing_profile: str = "fast"
    max_segment_duration: float = 30.0


@dataclass
class TranscriptionSegment:
    """A single transcription segment with timing info."""

    text: str
    start: float
    end: float
    speaker: Optional[str] = None
    confidence: float = 0.0
    words: list[dict] = field(default_factory=list)


@dataclass
class TranscriptionOutput:
    """Complete output from a speech-to-text operation."""

    segments: list[TranscriptionSegment]
    full_text: str
    language: str = "en"
    language_probability: float = 0.0
    duration_seconds: float = 0.0
    provider: str = "unknown"
    model: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


class SpeechProvider(abc.ABC):
    """
    Abstract interface for speech-to-text providers.

    Implementations must define:
        - transcribe()   : Convert audio file to text
        - load_model()   : Initialize / download the model
        - unload_model() : Free GPU/memory resources
        - is_available   : Whether the provider is ready
        - provider_name  : Human-readable name for metrics
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'faster-whisper', 'azure-speech')."""
        ...

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the model is loaded and ready for inference."""
        ...

    @abc.abstractmethod
    async def load_model(self, profile: str = "fast") -> bool:
        """
        Load the speech model.

        Args:
            profile: Processing profile ('fast', 'deep', etc.)

        Returns:
            True if model loaded successfully.
        """
        ...

    @abc.abstractmethod
    async def unload_model(self) -> None:
        """Release model resources (GPU memory, etc.)."""
        ...

    @abc.abstractmethod
    async def transcribe(
        self,
        audio_path: str,
        config: Optional[TranscriptionConfig] = None,
    ) -> TranscriptionOutput:
        """
        Transcribe an audio file.

        Args:
            audio_path: Path to the audio file (16kHz mono WAV preferred).
            config: Optional transcription configuration.

        Returns:
            TranscriptionOutput with segments and full text.
        """
        ...

    async def health_check(self) -> dict[str, Any]:
        """Return provider health status for monitoring."""
        return {
            "provider": self.provider_name,
            "available": self.is_available,
        }


class StubSpeechProvider(SpeechProvider):
    """
    Stub provider for development/testing when no real model is available.

    Returns synthetic transcription with a warning marker.
    """

    @property
    def provider_name(self) -> str:
        return "stub"

    @property
    def is_available(self) -> bool:
        return True

    async def load_model(self, profile: str = "fast") -> bool:
        logger.warning("StubSpeechProvider: no real model, returning stub transcriptions")
        return True

    async def unload_model(self) -> None:
        pass

    async def transcribe(
        self,
        audio_path: str,
        config: Optional[TranscriptionConfig] = None,
    ) -> TranscriptionOutput:
        return TranscriptionOutput(
            segments=[
                TranscriptionSegment(
                    text="[Stub] Speech provider not configured. Install faster-whisper for real transcription.",
                    start=0.0,
                    end=1.0,
                    confidence=0.0,
                ),
            ],
            full_text="[Stub] Speech provider not configured.",
            provider="stub",
            model="none",
        )
