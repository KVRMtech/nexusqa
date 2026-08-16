"""
Ears Engine — Speaker Diarization Module.

Production-grade speaker diarization with tiered backend selection:
  Tier 1: pyannote.audio 3.1 — gold standard accuracy (⭐⭐⭐⭐⭐)
  Tier 2: SpeechBrain ECAPA-TDNN — fully open fallback (⭐⭐⭐⭐)

Backend auto-selection:
  - pyannote loads first if installed and model/token available
  - SpeechBrain kicks in automatically if pyannote is blocked
  - Production Linux containers → pyannote (bundled models, no token)
  - Windows dev machines → SpeechBrain (no symlinks, no gated models)

Both backends produce identical output format: [{speaker, start, end}]
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import structlog
from nexus_sdk.events import fire_stub_alert
from nexus_sdk.media.models import TranscriptionSegment

from .bundle import prepare_runtime_bundle, verify_bundle

logger = structlog.get_logger()

# Target sample rate for all audio processing
_TARGET_SR = 16000


class SpeakerDiarizer:
    """
    Tiered speaker diarization: pyannote (primary) → SpeechBrain (fallback).
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

        # Pyannote backend state
        self._pyannote_pipeline = None
        self._runtime_bundle_dir: Path | None = None

        # SpeechBrain backend state
        self._encoder = None
        self._vad_model = None
        self._vad_utils = None

        self._backend: str = "none"  # "pyannote", "speechbrain", or "none"
        self._event_bus = None
        self._stub_fallback_count: int = 0
        self._startup_reason: str = "not_initialized"

    async def load_model(self) -> bool:
        """
        Load diarization backend. Tries pyannote first, then SpeechBrain.

        Returns True if any real backend loaded, False if stubbed.
        """
        try:
            loaded = await asyncio.wait_for(
                asyncio.to_thread(self._load_model_sync),
                timeout=self.load_timeout_seconds,
            )
            self._startup_reason = self.mode if loaded else self._startup_reason
            return loaded
        except asyncio.TimeoutError:
            logger.warning(
                "diarizer.load_timeout",
                timeout_seconds=self.load_timeout_seconds,
            )
            self._startup_reason = (
                f"diarizer load timed out after {self.load_timeout_seconds:.1f}s"
            )
            return False

    def _load_model_sync(self) -> bool:
        # ── Tier 1: Try pyannote ──────────────────────────────
        pyannote_ok = self._try_load_pyannote()
        if pyannote_ok:
            return True

        # ── Tier 2: Fall back to SpeechBrain ──────────────────
        speechbrain_ok = self._try_load_speechbrain()
        if speechbrain_ok:
            return True

        # Both failed
        logger.warning("diarizer.all_backends_failed")
        return False

    # ── Pyannote loading ──────────────────────────────────────

    def _try_load_pyannote(self) -> bool:
        """Attempt to load pyannote.audio 3.1 pipeline."""
        try:
            from pyannote.audio import Pipeline  # type: ignore[import-not-found]
        except ImportError:
            logger.info("diarizer.pyannote_not_installed, trying speechbrain")
            return False

        # Check bundle or token availability
        if self.verify_manifest:
            bundle_ok, bundle_reason = verify_bundle(self.model_path)
            if not bundle_ok:
                logger.info(
                    "diarizer.pyannote_bundle_invalid",
                    reason=bundle_reason,
                )
                # Keep WHY. The reason was logged and then dropped, so the
                # engine reported startup_reason="stub" — an operator asking why
                # diarization is stubbed got the symptom instead of the cause
                # ("bundle directory missing"), which is the whole point of the
                # field. load_model() only overwrites this on success.
                self._startup_reason = bundle_reason
                return False

        has_local = self._has_local_model_artifacts()
        if not self.hf_token and not has_local:
            logger.info(
                "diarizer.pyannote_no_token_no_local_model",
                model_path=self.model_path,
            )
            return False

        runtime_temp_dir: Path | None = None
        try:
            import torch

            auth_kwargs = {}
            if self.hf_token:
                auth_kwargs["token"] = self.hf_token

            model_source = self.model_path
            if has_local:
                model_source_path, runtime_temp_dir = prepare_runtime_bundle(
                    self.model_path
                )
                model_source = str(model_source_path)

            pipeline = Pipeline.from_pretrained(model_source, **auth_kwargs)

            use_device = self.device
            if use_device == "cuda" and not torch.cuda.is_available():
                use_device = "cpu"
            if use_device == "cuda":
                pipeline.to(torch.device("cuda"))

            # Clean up previous runtime dir
            if (
                self._runtime_bundle_dir
                and self._runtime_bundle_dir != runtime_temp_dir
            ):
                shutil.rmtree(self._runtime_bundle_dir, ignore_errors=True)

            self._pyannote_pipeline = pipeline
            self._runtime_bundle_dir = runtime_temp_dir
            self._backend = "pyannote"
            self.device = use_device

            logger.info(
                "diarizer.loaded",
                backend="pyannote-3.1",
                device=use_device,
                source="local_bundle" if has_local else "huggingface",
            )
            return True
        except Exception as e:
            if runtime_temp_dir is not None:
                shutil.rmtree(runtime_temp_dir, ignore_errors=True)
            logger.warning(
                "diarizer.pyannote_load_failed",
                error=str(e),
            )
            return False

    def _has_local_model_artifacts(self) -> bool:
        model_path = Path(self.model_path)
        if not model_path.exists() or not model_path.is_dir():
            return False
        present = {child.name for child in model_path.iterdir() if child.is_file()}
        return any(
            name in present
            for name in {"config.yaml", "config.yml", "pytorch_model.bin"}
        )

    # ── SpeechBrain loading ───────────────────────────────────

    def _try_load_speechbrain(self) -> bool:
        """Attempt to load SpeechBrain ECAPA-TDNN + Silero VAD."""
        try:
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy

            use_device = self.device
            if use_device == "cuda" and not torch.cuda.is_available():
                use_device = "cpu"

            self._encoder = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir="./models/speechbrain-ecapa",
                run_opts={"device": use_device},
                local_strategy=LocalStrategy.COPY,
            )

            self._vad_model, vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                trust_repo=True,
            )
            self._vad_utils = vad_utils
            self._backend = "speechbrain"
            self.device = use_device

            logger.info(
                "diarizer.loaded",
                backend="speechbrain-ecapa-tdnn",
                vad="silero",
                device=use_device,
            )
            return True
        except Exception as e:
            logger.warning(
                "diarizer.speechbrain_load_failed",
                error=str(e),
            )
            self._encoder = None
            self._vad_model = None
            return False

    # ── Properties ────────────────────────────────────────────

    @property
    def mode(self) -> str:
        if self._backend == "pyannote":
            return f"pyannote-3.1 device={self.device}"
        if self._backend == "speechbrain":
            return f"speechbrain-ecapa device={self.device}"
        return "stub"

    @property
    def startup_reason(self) -> str:
        if self._startup_reason and self._startup_reason != "not_initialized":
            return self._startup_reason
        return self.mode

    @property
    def is_real(self) -> bool:
        return self._backend in ("pyannote", "speechbrain")

    # ── Diarization dispatch ──────────────────────────────────

    async def diarize(
        self,
        audio_path: str,
        num_speakers: Optional[int] = None,
    ) -> list[dict]:
        if not self.is_real:
            return self._stub_diarize()

        return await asyncio.to_thread(
            self._diarize_sync, audio_path, num_speakers
        )

    def _diarize_sync(
        self, audio_path: str, num_speakers: Optional[int]
    ) -> list[dict]:
        if self._backend == "pyannote":
            return self._diarize_pyannote(audio_path, num_speakers)
        return self._diarize_speechbrain(audio_path, num_speakers)

    # ── Pyannote diarization ──────────────────────────────────

    def _diarize_pyannote(
        self, audio_path: str, num_speakers: Optional[int]
    ) -> list[dict]:
        params = {}
        if num_speakers:
            params["num_speakers"] = num_speakers
        else:
            params["min_speakers"] = self.min_speakers
            params["max_speakers"] = self.max_speakers

        # Pre-load audio to avoid torchcodec dependency
        audio_input = self._build_pyannote_input(audio_path)
        diarization = self._pyannote_pipeline(audio_input, **params)

        # pyannote 3.1 pipelines may return the Annotation directly OR wrap it
        # (``result.speaker_diarization``). Calling ``.itertracks`` on the
        # wrapper raises AttributeError and loses the whole diarization, so
        # unwrap when the direct interface is absent.
        if not hasattr(diarization, "itertracks"):
            inner = getattr(diarization, "speaker_diarization", None)
            if inner is None or not hasattr(inner, "itertracks"):
                raise TypeError(
                    "pyannote pipeline returned "
                    f"{type(diarization).__name__}, which exposes neither "
                    "itertracks() nor a speaker_diarization annotation"
                )
            diarization = inner

        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "speaker": speaker,
                "start": round(turn.start, 3),
                "end": round(turn.end, 3),
            })

        logger.info(
            "diarizer.completed",
            backend="pyannote",
            segments=len(segments),
            speakers=len(set(s["speaker"] for s in segments)),
        )
        return segments

    def _build_pyannote_input(self, audio_path: str):
        """Pre-load audio as a waveform dict to bypass torchcodec.

        Falls back to handing pyannote the PATH if the pre-load fails. The
        pre-load is an optimisation (it avoids a torchcodec dependency); pyannote
        can open the file itself. Without this guard a soundfile/torch error —
        an unusual codec, a truncated upload — propagated out of
        ``_diarize_pyannote`` and failed the whole diarization for a step that
        was only ever meant to save it some work.
        """
        try:
            return self._preload_waveform(audio_path)
        except Exception as exc:
            logger.warning(
                "diarizer.waveform_preload_failed",
                error=str(exc),
                fallback="audio_path",
            )
            return audio_path

    def _preload_waveform(self, audio_path: str) -> dict:
        import soundfile as sf
        import torch

        waveform, sample_rate = sf.read(
            audio_path, always_2d=True, dtype="float32"
        )
        waveform_tensor = torch.from_numpy(waveform.T)
        return {
            "waveform": waveform_tensor,
            "sample_rate": sample_rate,
            "uri": Path(audio_path).stem,
        }

    # ── SpeechBrain diarization ───────────────────────────────

    def _diarize_speechbrain(
        self, audio_path: str, num_speakers: Optional[int]
    ) -> list[dict]:
        import torch
        import soundfile as sf
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import pdist

        # Load and resample audio to 16kHz mono
        waveform, sample_rate = sf.read(
            audio_path, dtype="float32", always_2d=True
        )
        if waveform.shape[1] > 1:
            waveform = waveform.mean(axis=1)
        else:
            waveform = waveform[:, 0]

        if sample_rate != _TARGET_SR:
            ratio = _TARGET_SR / sample_rate
            n_samples = int(len(waveform) * ratio)
            indices = np.linspace(0, len(waveform) - 1, n_samples)
            waveform = np.interp(
                indices, np.arange(len(waveform)), waveform
            ).astype(np.float32)
            sample_rate = _TARGET_SR

        audio_tensor = torch.from_numpy(waveform)

        # Voice Activity Detection via Silero
        get_speech_timestamps = self._vad_utils[0]
        speech_timestamps = get_speech_timestamps(
            audio_tensor,
            self._vad_model,
            sampling_rate=_TARGET_SR,
            min_speech_duration_ms=250,
            min_silence_duration_ms=100,
        )

        if not speech_timestamps:
            logger.info("diarizer.no_speech_detected", audio_path=audio_path)
            return []

        # Merge short segments & extract embeddings
        merged_regions = self._merge_speech_regions(
            speech_timestamps, min_duration_samples=_TARGET_SR
        )
        if not merged_regions:
            return []

        embeddings = []
        regions_with_times = []
        for region in merged_regions:
            start_sample = region["start"]
            end_sample = region["end"]
            segment_audio = audio_tensor[start_sample:end_sample]

            if len(segment_audio) < _TARGET_SR // 4:
                continue

            seg_tensor = segment_audio.unsqueeze(0)
            with torch.no_grad():
                emb = self._encoder.encode_batch(seg_tensor)
            embeddings.append(emb.squeeze().cpu().numpy())
            regions_with_times.append({
                "start": start_sample / _TARGET_SR,
                "end": end_sample / _TARGET_SR,
            })

        if len(embeddings) == 0:
            return []

        if len(embeddings) == 1:
            return [{"speaker": "SPEAKER_00", **regions_with_times[0]}]

        # Agglomerative clustering
        emb_matrix = np.stack(embeddings)
        distances = pdist(emb_matrix, metric="cosine")
        Z = linkage(distances, method="average")

        if num_speakers and num_speakers > 0:
            n_clusters = min(num_speakers, len(embeddings))
            labels = fcluster(Z, t=n_clusters, criterion="maxclust")
        else:
            threshold = 0.40
            labels = fcluster(Z, t=threshold, criterion="distance")
            n_clusters = len(set(labels))
            if n_clusters < self.min_speakers:
                labels = fcluster(Z, t=self.min_speakers, criterion="maxclust")
            elif n_clusters > self.max_speakers:
                labels = fcluster(Z, t=self.max_speakers, criterion="maxclust")

        segments = []
        for i, region in enumerate(regions_with_times):
            speaker_id = int(labels[i]) - 1
            segments.append({
                "speaker": f"SPEAKER_{speaker_id:02d}",
                "start": round(region["start"], 3),
                "end": round(region["end"], 3),
            })

        segments = self._merge_consecutive_speaker_segments(segments)

        logger.info(
            "diarizer.completed",
            backend="speechbrain",
            segments=len(segments),
            speakers=len(set(s["speaker"] for s in segments)),
        )
        return segments

    # ── Shared utilities ──────────────────────────────────────

    @staticmethod
    def _merge_speech_regions(
        timestamps: list[dict],
        min_duration_samples: int,
        gap_samples: int = 4800,
    ) -> list[dict]:
        if not timestamps:
            return []

        merged = [{"start": timestamps[0]["start"], "end": timestamps[0]["end"]}]
        for ts in timestamps[1:]:
            prev = merged[-1]
            if ts["start"] - prev["end"] <= gap_samples:
                prev["end"] = ts["end"]
            else:
                merged.append({"start": ts["start"], "end": ts["end"]})

        split = []
        max_samples = 10 * _TARGET_SR
        for region in merged:
            duration = region["end"] - region["start"]
            if duration <= max_samples:
                if duration >= min_duration_samples // 4:
                    split.append(region)
            else:
                offset = region["start"]
                while offset < region["end"]:
                    chunk_end = min(offset + max_samples, region["end"])
                    if chunk_end - offset >= min_duration_samples // 4:
                        split.append({"start": offset, "end": chunk_end})
                    offset = chunk_end
        return split

    @staticmethod
    def _merge_consecutive_speaker_segments(segments: list[dict]) -> list[dict]:
        if not segments:
            return []
        merged = [segments[0].copy()]
        for seg in segments[1:]:
            if seg["speaker"] == merged[-1]["speaker"]:
                merged[-1]["end"] = seg["end"]
            else:
                merged.append(seg.copy())
        return merged

    def _stub_diarize(self) -> list[dict]:
        self._stub_fallback_count += 1
        logger.warning("diarizer.stub_fallback #%d", self._stub_fallback_count)
        fire_stub_alert(
            self._event_bus,
            "ears",
            "diarizer",
            fallback_count=self._stub_fallback_count,
            reason="no diarization backend loaded",
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
