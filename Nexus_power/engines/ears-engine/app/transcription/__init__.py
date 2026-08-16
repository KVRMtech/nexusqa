"""
Ears Engine — Whisper Transcription Module.

Production-grade speech-to-text using faster-whisper (CTranslate2 backend).
Supports GPU (float16) and CPU (int8) inference with automatic fallback to stub.

Features:
  - Insurance domain vocabulary boosting via initial_prompt
  - Word-level timestamps for precise alignment
  - Built-in Whisper VAD for noise-robust transcription
  - Async-safe (uses asyncio.to_thread for blocking inference)
  - Stub fallback with structured alerting
"""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from typing import Any, Optional

import structlog
from nexus_sdk.events import fire_stub_alert
from nexus_sdk.config import production_guard
from nexus_sdk.media.speech import (
    SpeechProvider,
    TranscriptionConfig,
    TranscriptionOutput,
    TranscriptionSegment as SpeechSegment,
)

logger = structlog.get_logger()


# Default insurance domain vocabulary for prompt boosting
INSURANCE_VOCABULARY = [
    # Life Insurance terms
    "premium", "beneficiary", "annuitant", "policyholder", "underwriting",
    "mortality table", "cash value", "surrender charge", "death benefit",
    "term life", "whole life", "universal life", "variable life",
    "guaranteed issue", "simplified issue", "fully underwritten",
    "face amount", "rider", "waiver of premium", "accelerated death benefit",
    "conversion privilege", "incontestability", "contestability period",
    "suicide clause", "grace period", "lapse", "reinstatement",
    "nonforfeiture", "reduced paid-up", "extended term",
    # P&C terms
    "deductible", "coinsurance", "subrogation", "indemnity",
    "actual cash value", "replacement cost", "occurrence",
    "claims-made", "aggregate limit", "per occurrence limit",
    "combined single limit", "bodily injury", "property damage",
    "personal injury", "advertising injury", "products liability",
    "completed operations", "additional insured", "named insured",
    "certificate of insurance", "declarations page", "endorsement",
    "exclusion", "condition", "insuring agreement",
    # Actuarial / Financial
    "loss ratio", "combined ratio", "expense ratio",
    "incurred but not reported", "IBNR", "loss development factor",
    "credibility", "experience modification", "retrospective rating",
    "prospective rating", "catastrophe load", "reinsurance",
    "treaty reinsurance", "facultative reinsurance", "ceding company",
    # System / Compliance
    "NAIC", "SERFF", "rate filing", "form filing",
    "market conduct", "MIB", "CLUE report", "MVR",
    "state filing", "admitted carrier", "surplus lines",
    "managing general agent", "MGA", "NPN",
    "producer code", "commission schedule", "hierarchy",
]


class WhisperTranscriber(SpeechProvider):
    """
    On-prem Whisper v3 Large transcription.

    Uses faster-whisper (CTranslate2 backend) for optimal
    GPU performance. Falls back to CPU int8 if no GPU.
    Stub fallback when model is unavailable (dev environments).

    Implements the SDK SpeechProvider interface so the model backend
    can be swapped (e.g., Azure Speech, Deepgram) without rewriting
    Ears engine business logic.
    """

    def __init__(
        self,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        model_path: str = "./models/whisper-large-v3",
        fast_model_size: str = "medium",
        fast_compute_type: str = "int8",
        fast_model_path: str = "./models/whisper-medium",
        default_processing_profile: str = "fast",
        vad_threshold: float = 0.5,
        load_timeout_seconds: float = 30.0,
        allow_remote_model_bootstrap: bool = False,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.model_path = model_path
        self.fast_model_size = fast_model_size
        self.fast_compute_type = fast_compute_type
        self.fast_model_path = fast_model_path
        self.default_processing_profile = default_processing_profile
        self.vad_threshold = vad_threshold
        self.load_timeout_seconds = load_timeout_seconds
        self.allow_remote_model_bootstrap = allow_remote_model_bootstrap
        self.model = None
        self.active_model_size: str | None = None
        self.active_processing_profile: str | None = None
        self._event_bus = None
        self._stub_fallback_count: int = 0
        self._vocabulary_terms: list[str] = INSURANCE_VOCABULARY
        # P2 Fix: Dual-model cache to avoid hot-swap thrashing.
        # When memory allows, both fast and deep models stay loaded
        # and we switch between them without unload/reload cycles.
        self._model_cache: dict[str, Any] = {}  # profile -> WhisperModel

    async def load_model(self, processing_profile: str | None = None) -> bool:
        """
        Load Whisper model into GPU/CPU memory.

        Uses a dual-model cache: both 'fast' and 'deep' models can stay
        loaded simultaneously, avoiding unload/reload thrashing when
        requests alternate between profiles.

        Returns True if the real model was loaded, False if stubbed.
        """
        normalized_profile = self._normalize_processing_profile(
            processing_profile
        )
        model_size, compute_type, model_path = self._profile_settings(
            normalized_profile
        )

        # Check dual-model cache first — instant switch, no reload
        if normalized_profile in self._model_cache:
            self.model = self._model_cache[normalized_profile]
            self.active_model_size = model_size
            self.active_processing_profile = normalized_profile
            return True

        # Legacy fast-path: if already loaded with same size
        if self.model is not None and self.active_model_size == model_size:
            return True

        if not self.allow_remote_model_bootstrap and not self._has_local_model_artifacts(model_path):
            logger.warning(
                "whisper.local_model_missing_bootstrap_disabled",
                model=model_size,
                model_path=model_path,
                processing_profile=normalized_profile,
            )
            self.model = None
            self.active_model_size = None
            self.active_processing_profile = None
            return False

        try:
            # Load synchronously to ensure model and inference run on the
            # same thread — ctranslate2 deadlocks when the model is loaded
            # in one thread (asyncio.to_thread) and used from another.
            new_model = self._load_model_sync(
                model_size,
                compute_type,
                model_path,
            )
            self.model = new_model
            self._model_cache[normalized_profile] = new_model
            self.active_model_size = model_size
            self.active_processing_profile = normalized_profile
            logger.info(
                "whisper.loaded",
                model=model_size,
                processing_profile=normalized_profile,
                device=self.device,
                compute_type=compute_type,
            )
            return True
        except ImportError:
            logger.warning("whisper.import_error: faster-whisper not installed — using stub")
            self.model = None
            self.active_model_size = None
            self.active_processing_profile = None
            return False
        except Exception as e:
            logger.error("whisper.load_failed: %s — using stub", e)
            self.model = None
            self.active_model_size = None
            self.active_processing_profile = None
            return False
        finally:
            # Production guard: refuse stub mode in production environments
            production_guard(
                "Whisper transcription model (ears-engine)",
                available=(self.model is not None),
            )

    def _load_model_sync(
        self,
        model_size: str,
        compute_type: str,
        model_path: str,
    ):
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        return WhisperModel(
            model_size,
            device=self.device,
            compute_type=compute_type,
            cpu_threads=1,
            num_workers=1,
            download_root=str(Path(model_path).parent),
        )

    def _has_local_model_artifacts(self, model_path: str) -> bool:
        path = Path(model_path)
        required = {"model.bin", "config.json", "tokenizer.json"}

        if path.exists() and path.is_dir():
            present = {child.name for child in path.iterdir() if child.is_file()}
            if any(name in present for name in required):
                return True

        hub_cache_dir = path.parent / f"models--Systran--faster-whisper-{path.name.replace('whisper-', '')}"
        snapshots_dir = hub_cache_dir / "snapshots"
        if not snapshots_dir.exists() or not snapshots_dir.is_dir():
            return False

        for snapshot_dir in snapshots_dir.iterdir():
            if not snapshot_dir.is_dir():
                continue
            snapshot_files = {child.name for child in snapshot_dir.iterdir() if child.is_file()}
            if any(name in snapshot_files for name in required):
                return True
        return False

    @property
    def is_real(self) -> bool:
        """True if real Whisper model is loaded (not stub)."""
        return self.model is not None

    def describe_mode(self) -> str:
        if not self.model:
            return "stub"
        return (
            f"whisper active={self.active_model_size or 'unknown'} "
            f"fast={self.fast_model_size} deep={self.model_size} device={self.device}"
        )

    async def transcribe(
        self,
        audio_path: str,
        language: str = "en",
        processing_profile: str | None = None,
    ) -> list[dict]:
        """
        Transcribe audio file and return segments.

        Returns list of dicts with keys:
            text, start, end, confidence, language, words
        """
        await self.load_model(processing_profile)

        if self.model is None:
            return self._stub_transcribe(audio_path)

        # Run transcription synchronously to avoid ctranslate2 futex
        # deadlocks when inference runs in a thread pool alongside
        # the asyncio event loop and torch (diarizer).
        profile = self._normalize_processing_profile(processing_profile)
        return self._transcribe_sync(audio_path, language, profile)

    def _transcribe_sync(self, audio_path: str, language: str, processing_profile: str = "fast") -> list[dict]:
        """Synchronous transcription — called in a thread."""
        is_fast = processing_profile == "fast"
        # Use beam_size=1 for fast profile to avoid ctranslate2 thread-pool
        # deadlocks on CPU; deep profile can afford beam_size=5.
        beam_size = 1 if is_fast else 5
        # Disable VAD filter on CPU/fast to avoid Silero torch thread contention
        use_vad = not is_fast
        segments_gen, info = self.model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            word_timestamps=True,
            initial_prompt=self._build_vocabulary_prompt(),
            vad_filter=use_vad,
            vad_parameters=dict(
                threshold=self.vad_threshold,
                min_silence_duration_ms=500,
            ),
        )

        segments = []
        for seg in segments_gen:
            words = []
            if seg.words:
                words = [
                    {
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    }
                    for w in seg.words
                ]
            segments.append({
                "text": seg.text.strip(),
                "start": seg.start,
                "end": seg.end,
                "confidence": seg.avg_logprob,
                "language": language,
                "words": words,
            })

        logger.info(
            "whisper.transcribed",
            segments=len(segments),
            language=info.language if hasattr(info, 'language') else language,
            language_probability=getattr(info, 'language_probability', 0.0),
        )
        return segments

    def _normalize_processing_profile(
        self,
        processing_profile: str | None,
    ) -> str:
        profile = (processing_profile or self.default_processing_profile).strip().lower()
        if profile in {"deep", "full"}:
            return "deep"
        return "fast"

    def _profile_settings(
        self,
        processing_profile: str,
    ) -> tuple[str, str, str]:
        if processing_profile == "fast":
            return (
                self.fast_model_size,
                self.fast_compute_type,
                self.fast_model_path,
            )
        return self.model_size, self.compute_type, self.model_path

    def _unload_model(self) -> None:
        if self.model is not None:
            self.model = None
            self._model_cache.clear()
            gc.collect()
            # P3 Fix: Release CUDA memory after unloading model to
            # prevent VRAM fragmentation.
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

    def set_vocabulary(self, terms: list[str]) -> None:
        """
        Set domain vocabulary for prompt boosting.

        Called by plugin system to inject tenant-specific terms.
        """
        self._vocabulary_terms = terms

    def _build_vocabulary_prompt(self) -> str:
        """Build initial prompt with domain vocabulary for better recognition."""
        terms = self._vocabulary_terms[:50]
        return "Domain terminology: " + ", ".join(terms) + "."

    def _stub_transcribe(self, audio_path: str) -> list[dict]:
        """Development stub when Whisper model is not available."""
        self._stub_fallback_count += 1
        logger.warning("whisper.stub_fallback #%d", self._stub_fallback_count)
        fire_stub_alert(
            self._event_bus, "ears", "whisper",
            fallback_count=self._stub_fallback_count,
            reason="faster-whisper model not loaded",
        )
        return [
            {
                "text": "[Stub] This is a development placeholder for transcription.",
                "start": 0.0,
                "end": 5.0,
                "confidence": 0.0,
                "language": "en",
                "words": [],
            },
            {
                "text": "[Stub] Whisper model not loaded. Install faster-whisper for real transcription.",
                "start": 5.0,
                "end": 10.0,
                "confidence": 0.0,
                "language": "en",
                "words": [],
            },
        ]

    # ── SpeechProvider interface implementation ────────────────
    # These methods satisfy the abstract contract from
    # nexus_sdk.media.speech.SpeechProvider, making WhisperTranscriber
    # a first-class pluggable provider on the canonical hot path.

    @property
    def provider_name(self) -> str:
        return "faster-whisper"

    @property
    def is_available(self) -> bool:
        return self.model is not None

    async def unload_model(self) -> None:
        """Release model resources (SpeechProvider contract)."""
        self._unload_model()

    async def transcribe_provider(
        self,
        audio_path: str,
        config: TranscriptionConfig | None = None,
    ) -> TranscriptionOutput:
        """
        SpeechProvider-conformant transcription entry point.

        Delegates to the existing transcribe() pipeline and wraps
        the result in SDK TranscriptionOutput for callers that
        program against the abstract interface.
        """
        cfg = config or TranscriptionConfig()
        language = cfg.language or "en"
        profile = cfg.processing_profile or self.default_processing_profile

        segments_raw = await self.transcribe(
            audio_path, language=language, processing_profile=profile,
        )

        segments = [
            SpeechSegment(
                text=s["text"],
                start=s["start"],
                end=s["end"],
                confidence=s.get("confidence", 0.0),
                words=s.get("words", []),
            )
            for s in segments_raw
        ]
        full_text = " ".join(s.text for s in segments)

        return TranscriptionOutput(
            segments=segments,
            full_text=full_text,
            language=language,
            provider=self.provider_name,
            model=self.active_model_size or "unknown",
        )
