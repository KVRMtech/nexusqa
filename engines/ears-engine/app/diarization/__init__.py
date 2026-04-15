"""
Ears Engine — Speaker Diarization Module.

Production-grade speaker diarization using Pyannote 3.1.
Identifies who is speaking in each segment.

Features:
  - GPU-accelerated (CUDA) or CPU fallback
  - Automatic speaker count detection (min/max range)
  - Async-safe (uses asyncio.to_thread for blocking inference)
  - Stub fallback with structured alerting for dev environments
  - Segment alignment utility for merging with transcription

Note: pyannote.audio is Linux-only for GPU. On Windows,
      it will gracefully fall back to stub mode.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

import structlog
from nexus_sdk.events import fire_stub_alert
from nexus_sdk.media.models import TranscriptionSegment

from .bundle import prepare_runtime_bundle, verify_bundle

logger = structlog.get_logger()


class SpeakerDiarizer:
    """
    On-prem speaker diarization using Pyannote 3.1.

    Identifies who is speaking in each segment.
    """

    def __init__(
        self,
        model_path: str = "./models/pyannote-speaker-3.1",
        hf_token: str = "",
        device: str = "cuda",
        min_speakers: int = 1,
        max_speakers: int = 10,
        load_timeout_seconds: float = 30.0,
        verify_manifest: bool = False,
    ):
        self.model_path = model_path
        self.hf_token = hf_token
        self.device = device
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers
        self.load_timeout_seconds = load_timeout_seconds
        self.verify_manifest = verify_manifest
        self.pipeline = None
        self._runtime_bundle_dir: Path | None = None
        self._event_bus = None
        self._stub_fallback_count: int = 0
        self._startup_reason: str = "not_initialized"

    async def load_model(self) -> bool:
        """
        Load Pyannote diarization pipeline.

        Returns True if loaded, False if stubbed.
        """
        if self.verify_manifest:
            bundle_ok, bundle_reason = verify_bundle(self.model_path)
            if not bundle_ok:
                logger.warning(
                    "diarizer.bundle_verification_failed",
                    model_path=self.model_path,
                    reason=bundle_reason,
                )
                self._startup_reason = bundle_reason
                self.pipeline = None
                return False

        if not self.hf_token and not self._has_local_model_artifacts():
            logger.warning(
                "diarizer.local_model_missing_no_token",
                model_path=self.model_path,
            )
            self._startup_reason = "missing local diarizer artifacts and HF token"
            self.pipeline = None
            return False

        try:
            self.pipeline = await asyncio.wait_for(
                asyncio.to_thread(self._load_model_sync),
                timeout=self.load_timeout_seconds,
            )
            self._startup_reason = self.mode if self.pipeline is not None else self._startup_reason
            return self.pipeline is not None
        except asyncio.TimeoutError:
            logger.warning(
                "diarizer.load_timeout",
                model=self.model_path,
                timeout_seconds=self.load_timeout_seconds,
            )
            self._startup_reason = (
                f"diarizer load timed out after {self.load_timeout_seconds:.1f}s"
            )
            self.pipeline = None
            return False

    def _load_model_sync(self):
        runtime_temp_dir: Path | None = None
        try:
            from pyannote.audio import Pipeline  # type: ignore[import-not-found]

            auth_kwargs = {}
            if self.hf_token:
                auth_kwargs["token"] = self.hf_token

            model_source = self.model_path
            if self._has_local_model_artifacts():
                model_source_path, runtime_temp_dir = prepare_runtime_bundle(self.model_path)
                model_source = str(model_source_path)

            pipeline = Pipeline.from_pretrained(
                model_source,
                **auth_kwargs,
            )
            if self.device == "cuda":
                import torch  # type: ignore[import-not-found]

                pipeline.to(torch.device("cuda"))
            if self._runtime_bundle_dir and self._runtime_bundle_dir != runtime_temp_dir:
                shutil.rmtree(self._runtime_bundle_dir, ignore_errors=True)
            self._runtime_bundle_dir = runtime_temp_dir
            logger.info("diarizer.loaded", model=self.model_path, device=self.device)
            return pipeline
        except ImportError:
            logger.warning("diarizer.import_error: pyannote.audio not installed — using stub")
            self._startup_reason = "pyannote.audio not installed"
            return None
        except Exception as e:
            if runtime_temp_dir is not None:
                shutil.rmtree(runtime_temp_dir, ignore_errors=True)
            logger.error("diarizer.load_failed: %s — using stub", e)
            self._startup_reason = str(e)
            return None

    def _has_local_model_artifacts(self) -> bool:
        model_path = Path(self.model_path)
        if not model_path.exists() or not model_path.is_dir():
            return False
        present = {child.name for child in model_path.iterdir() if child.is_file()}
        return any(name in present for name in {"config.yaml", "config.yml", "pytorch_model.bin"})

    @property
    def mode(self) -> str:
        if not self.is_real:
            return "stub"
        return f"pyannote device={self.device}"

    @property
    def startup_reason(self) -> str:
        if self._startup_reason and self._startup_reason != "not_initialized":
            return self._startup_reason
        if not self.hf_token and not self._has_local_model_artifacts():
            return "missing local diarizer artifacts and HF token"
        return self.mode

    @property
    def is_real(self) -> bool:
        """True if real Pyannote pipeline is loaded."""
        return self.pipeline is not None

    async def diarize(
        self,
        audio_path: str,
        num_speakers: Optional[int] = None,
    ) -> list[dict]:
        """
        Identify speakers in audio.

        Returns list of dicts: {speaker, start, end}
        """
        if self.pipeline is None:
            return self._stub_diarize()

        return await asyncio.to_thread(
            self._diarize_sync, audio_path, num_speakers
        )

    def _diarize_sync(
        self, audio_path: str, num_speakers: Optional[int]
    ) -> list[dict]:
        """Synchronous diarization — called in a thread."""
        params = {}
        if num_speakers:
            params["num_speakers"] = num_speakers
        else:
            params["min_speakers"] = self.min_speakers
            params["max_speakers"] = self.max_speakers

        diarization_input = self._build_pipeline_input(audio_path)
        diarization = self.pipeline(diarization_input, **params)
        annotation = getattr(diarization, "speaker_diarization", diarization)

        segments = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            segments.append({
                "speaker": speaker,
                "start": turn.start,
                "end": turn.end,
            })

        logger.info(
            "diarizer.completed",
            segments=len(segments),
            speakers=len(set(s["speaker"] for s in segments)),
        )
        return segments

    def _build_pipeline_input(self, audio_path: str):
        try:
            import soundfile as sf  # type: ignore[import-not-found]
            import torch  # type: ignore[import-not-found]

            waveform, sample_rate = sf.read(audio_path, always_2d=True, dtype="float32")
            waveform_tensor = torch.from_numpy(waveform.T)
            return {
                "waveform": waveform_tensor,
                "sample_rate": sample_rate,
                "uri": Path(audio_path).stem,
            }
        except Exception as exc:
            logger.warning(
                "diarizer.waveform_preload_failed",
                audio_path=audio_path,
                error=str(exc),
            )
            return audio_path

    def _stub_diarize(self) -> list[dict]:
        """Development stub."""
        self._stub_fallback_count += 1
        logger.warning("diarizer.stub_fallback #%d", self._stub_fallback_count)
        fire_stub_alert(
            self._event_bus, "ears", "diarizer",
            fallback_count=self._stub_fallback_count,
            reason="pyannote pipeline not loaded",
        )
        return [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
            {"speaker": "SPEAKER_01", "start": 5.0, "end": 10.0},
        ]


def align_segments(
    whisper_segments: list[dict],
    speaker_segments: list[dict],
) -> list[TranscriptionSegment]:
    """
    Align Whisper transcription segments with speaker diarization.

    For each Whisper segment, find the speaker segment with maximum
    overlap and assign that speaker label. Uses the shared SDK
    TranscriptionSegment model.

    Args:
        whisper_segments: Output from WhisperTranscriber.transcribe()
        speaker_segments: Output from SpeakerDiarizer.diarize()

    Returns:
        List of TranscriptionSegment with speaker labels assigned.
    """
    aligned = []

    for wseg in whisper_segments:
        w_start = wseg["start"]
        w_end = wseg["end"]

        best_speaker = "UNKNOWN"
        best_overlap = 0.0

        for sseg in speaker_segments:
            s_start = sseg["start"]
            s_end = sseg["end"]

            overlap_start = max(w_start, s_start)
            overlap_end = min(w_end, s_end)
            overlap = max(0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = sseg["speaker"]

        aligned.append(TranscriptionSegment(
            speaker=best_speaker,
            text=wseg["text"],
            start_time=w_start,
            end_time=w_end,
            confidence=wseg.get("confidence", 0.0),
            language=wseg.get("language", "en"),
            words=wseg.get("words", []),
        ))

    return aligned
