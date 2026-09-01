"""
Nexus SDK — Audio Preprocessing Pipeline.

Production-grade audio preparation for speech-to-text:
  1. Format detection and validation
  2. FFmpeg-based transcoding to 16kHz mono WAV (Whisper-optimal)
  3. Audio normalization (peak & loudness)
  4. Voice Activity Detection (Silero VAD) for silence trimming
  5. Chunking for long recordings (>30 min)

All processing is local — no audio leaves the server.

Dependencies:
  - ffmpeg (system binary, must be on PATH)
  - torch (for Silero VAD)
  - torchaudio or soundfile (for audio I/O)

Usage:
    from nexus_sdk.media.audio import AudioPreprocessor, PreprocessConfig

    preprocessor = AudioPreprocessor(PreprocessConfig())
    result = await preprocessor.prepare(
        input_path="/data/upload/recording.mp3",
        output_dir="/data/processed/session-1/",
    )
    # result.output_path → "/data/processed/session-1/recording_16k.wav"
    # result.metadata → AudioMetadata(...)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from nexus_sdk.media.models import AudioMetadata

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────


class PreprocessConfig(BaseModel):
    """Configuration for audio preprocessing pipeline."""

    # Target format for Whisper
    target_sample_rate: int = Field(default=16000, description="Target sample rate for Whisper")
    target_channels: int = Field(default=1, description="Target channel count (mono)")
    target_bit_depth: int = Field(default=16, description="Target bit depth")

    # Normalization
    normalize_audio: bool = Field(default=True, description="Apply loudness normalization")
    target_loudness_lufs: float = Field(
        default=-23.0, description="Target loudness in LUFS (EBU R128)"
    )
    peak_limit_db: float = Field(
        default=-1.0, description="Peak limiter in dB to prevent clipping"
    )

    # Voice Activity Detection
    apply_vad: bool = Field(
        default=False,
        description="Apply VAD to strip silence. Disabled by default because "
                    "Whisper has built-in VAD. Enable for noisy recordings.",
    )
    vad_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Silero VAD speech probability threshold",
    )
    vad_min_speech_duration_ms: int = Field(
        default=250, description="Minimum speech segment duration in ms"
    )
    vad_min_silence_duration_ms: int = Field(
        default=500, description="Minimum silence duration to split segments in ms"
    )

    # Chunking for long recordings
    max_duration_seconds: int = Field(
        default=3600, description="Max single-file duration. Longer files get chunked."
    )
    chunk_duration_seconds: int = Field(
        default=600, description="Chunk size for long recordings (10 minutes)"
    )
    chunk_overlap_seconds: int = Field(
        default=5, description="Overlap between chunks for continuity"
    )

    # FFmpeg
    ffmpeg_path: str = Field(default="ffmpeg", description="Path to ffmpeg binary")
    ffprobe_path: str = Field(default="ffprobe", description="Path to ffprobe binary")


# ─── Result Types ──────────────────────────────────────────────


@dataclass
class PreprocessResult:
    """Result of audio preprocessing."""

    output_path: str
    """Path to the processed WAV file (or first chunk)."""

    metadata: AudioMetadata
    """Technical metadata of the processed audio."""

    chunk_paths: list[str] = field(default_factory=list)
    """If the recording was chunked, paths to all chunks. Otherwise empty."""

    stages_applied: list[str] = field(default_factory=list)
    """Preprocessing stages that were applied."""

    original_duration_seconds: float = 0.0
    """Duration of the original input file."""

    processing_time_seconds: float = 0.0
    """Wall-clock time for preprocessing."""

    warnings: list[str] = field(default_factory=list)
    """Any non-fatal warnings during preprocessing."""


# ─── FFmpeg Probe ──────────────────────────────────────────────


async def probe_audio(
    file_path: str,
    ffprobe_path: str = "ffprobe",
) -> AudioMetadata:
    """
    Probe an audio file using ffprobe to extract technical metadata.

    Args:
        file_path: Path to the audio file.
        ffprobe_path: Path to the ffprobe binary.

    Returns:
        AudioMetadata with format, sample rate, channels, duration, etc.

    Raises:
        FileNotFoundError: If the audio file doesn't exist.
        RuntimeError: If ffprobe fails or isn't installed.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffprobe failed (exit {proc.returncode}): {error_msg}")

        import json
        info = json.loads(stdout.decode("utf-8"))

    except FileNotFoundError:
        raise RuntimeError(
            f"ffprobe not found at '{ffprobe_path}'. "
            "Install FFmpeg: https://ffmpeg.org/download.html"
        )

    # Extract audio stream info
    audio_stream = None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break

    if not audio_stream:
        raise RuntimeError(f"No audio stream found in: {file_path}")

    fmt = info.get("format", {})

    sample_rate = int(audio_stream.get("sample_rate", 0))
    channels = int(audio_stream.get("channels", 1))
    duration = float(fmt.get("duration", 0.0))
    file_size = int(fmt.get("size", 0))
    codec = audio_stream.get("codec_name", "unknown")
    bit_depth = int(audio_stream.get("bits_per_raw_sample", 0) or 0)
    format_name = fmt.get("format_name", path.suffix.lstrip("."))

    return AudioMetadata(
        file_path=str(path),
        original_filename=path.name,
        format=format_name,
        sample_rate=sample_rate,
        channels=channels,
        duration_seconds=round(duration, 3),
        file_size_bytes=file_size,
        bit_depth=bit_depth if bit_depth > 0 else 16,
        codec=codec,
    )


# ─── FFmpeg Transcoding ───────────────────────────────────────


async def transcode_to_wav(
    input_path: str,
    output_path: str,
    sample_rate: int = 16000,
    channels: int = 1,
    normalize: bool = True,
    target_loudness_lufs: float = -23.0,
    peak_limit_db: float = -1.0,
    ffmpeg_path: str = "ffmpeg",
) -> str:
    """
    Transcode audio to 16kHz mono WAV with optional loudness normalization.

    Uses a two-pass loudness normalization (EBU R128) when normalize=True.

    Args:
        input_path: Source audio file path.
        output_path: Destination WAV file path.
        sample_rate: Target sample rate (16000 for Whisper).
        channels: Target channels (1 = mono).
        normalize: Apply loudness normalization.
        target_loudness_lufs: Target integrated loudness.
        peak_limit_db: True peak limit.
        ffmpeg_path: Path to ffmpeg binary.

    Returns:
        Path to the output WAV file.

    Raises:
        RuntimeError: If FFmpeg fails.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    if normalize:
        # Two-pass EBU R128 normalization
        # Pass 1: Measure current loudness
        measure_cmd = [
            ffmpeg_path, "-i", input_path,
            "-af", f"loudnorm=I={target_loudness_lufs}:TP={peak_limit_db}:print_format=json",
            "-f", "null",
            "-y",
            "NUL" if os.name == "nt" else "/dev/null",
        ]

        proc = await asyncio.create_subprocess_exec(
            *measure_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await proc.communicate()
        stderr_text = stderr_data.decode("utf-8", errors="replace")

        # Parse loudnorm output from stderr
        loudnorm_params = _parse_loudnorm_output(stderr_text)

        if loudnorm_params:
            # Pass 2: Apply measured normalization
            norm_filter = (
                f"loudnorm=I={target_loudness_lufs}:TP={peak_limit_db}"
                f":measured_I={loudnorm_params.get('input_i', -23)}"
                f":measured_TP={loudnorm_params.get('input_tp', -1)}"
                f":measured_LRA={loudnorm_params.get('input_lra', 7)}"
                f":measured_thresh={loudnorm_params.get('input_thresh', -34)}"
                f":offset={loudnorm_params.get('target_offset', 0)}"
                f":linear=true"
            )
            encode_cmd = [
                ffmpeg_path, "-i", input_path,
                "-af", norm_filter,
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-c:a", "pcm_s16le",
                "-y", output_path,
            ]
        else:
            # Fallback: simple normalization if two-pass parsing failed
            logger.warning("audio.preprocess: loudnorm measurement parse failed, using simple normalization")
            encode_cmd = [
                ffmpeg_path, "-i", input_path,
                "-af", f"loudnorm=I={target_loudness_lufs}:TP={peak_limit_db}",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-c:a", "pcm_s16le",
                "-y", output_path,
            ]
    else:
        # No normalization — just transcode
        encode_cmd = [
            ffmpeg_path, "-i", input_path,
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-c:a", "pcm_s16le",
            "-y", output_path,
        ]

    proc = await asyncio.create_subprocess_exec(
        *encode_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_data = await proc.communicate()

    if proc.returncode != 0:
        error = stderr_data.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg transcoding failed (exit {proc.returncode}): {error}")

    if not os.path.exists(output_path):
        raise RuntimeError(f"FFmpeg did not produce output file: {output_path}")

    logger.info(
        "audio.transcode_complete: %s -> %s (sr=%d, norm=%s)",
        os.path.basename(input_path),
        os.path.basename(output_path),
        sample_rate,
        normalize,
    )

    return output_path


def _parse_loudnorm_output(stderr_text: str) -> Optional[dict[str, float]]:
    """
    Parse the JSON block from FFmpeg loudnorm pass 1 output.

    The output is embedded in FFmpeg's stderr and looks like:
    {
        "input_i" : "-24.00",
        "input_tp" : "-2.00",
        ...
    }
    """
    import json
    import re

    # Find the last JSON block in stderr (loudnorm outputs JSON)
    json_blocks = re.findall(r"\{[^{}]+\}", stderr_text)
    if not json_blocks:
        return None

    # The loudnorm JSON is typically the last block
    for block in reversed(json_blocks):
        try:
            data = json.loads(block)
            if "input_i" in data:
                # Convert string values to float
                return {
                    k: float(v) if isinstance(v, str) else v
                    for k, v in data.items()
                }
        except (json.JSONDecodeError, ValueError):
            continue

    return None


# ─── Audio Chunking ────────────────────────────────────────────


async def chunk_audio(
    input_path: str,
    output_dir: str,
    chunk_duration_seconds: int = 600,
    overlap_seconds: int = 5,
    ffmpeg_path: str = "ffmpeg",
) -> list[str]:
    """
    Split a long audio file into overlapping chunks.

    Each chunk is chunk_duration_seconds long with overlap_seconds
    of overlap with the next chunk (for transcription continuity).

    Args:
        input_path: Source audio (.wav).
        output_dir: Directory to write chunk files.
        chunk_duration_seconds: Duration of each chunk.
        overlap_seconds: Overlap between consecutive chunks.
        ffmpeg_path: Path to ffmpeg binary.

    Returns:
        List of paths to chunk files, in order.
    """
    os.makedirs(output_dir, exist_ok=True)

    meta = await probe_audio(input_path)
    total_duration = meta.duration_seconds

    if total_duration <= chunk_duration_seconds:
        # No chunking needed
        return [input_path]

    chunks = []
    start = 0.0
    chunk_idx = 0
    step = chunk_duration_seconds - overlap_seconds

    while start < total_duration:
        chunk_path = os.path.join(output_dir, f"chunk_{chunk_idx:04d}.wav")
        duration = min(chunk_duration_seconds, total_duration - start)

        cmd = [
            ffmpeg_path,
            "-i", input_path,
            "-ss", str(start),
            "-t", str(duration),
            "-c:a", "pcm_s16le",
            "-y", chunk_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_data = await proc.communicate()

        if proc.returncode != 0:
            error = stderr_data.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg chunking failed at t={start}s: {error}")

        chunks.append(chunk_path)
        chunk_idx += 1
        start += step

    logger.info(
        "audio.chunked: %s -> %d chunks (dur=%s, overlap=%s, total=%s)",
        os.path.basename(input_path),
        len(chunks),
        chunk_duration_seconds,
        overlap_seconds,
        round(total_duration, 1),
    )

    return chunks


# ─── Voice Activity Detection (Silero) ─────────────────────────


class SileroVAD:
    """
    Voice Activity Detection using Silero VAD (torch-based, runs on CPU).

    Detects speech regions in audio, useful for:
    - Stripping leading/trailing silence
    - Skipping non-speech segments
    - Pre-segmenting before transcription

    Note: Whisper has built-in VAD, so this is optional.
    Use it for noisy recordings or when you need segment boundaries.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 500,
    ):
        self.threshold = threshold
        self.min_speech_duration_ms = min_speech_duration_ms
        self.min_silence_duration_ms = min_silence_duration_ms
        self._model = None
        self._utils = None

    def load(self) -> bool:
        """
        Load the Silero VAD model. Returns True if successful.

        Requires torch. Falls back gracefully if unavailable.
        """
        try:
            import torch  # type: ignore[import-not-found]
            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._model = model
            self._utils = utils
            logger.info("silero_vad.loaded")
            return True
        except Exception as e:
            logger.warning("silero_vad.load_failed: %s — VAD disabled", e)
            return False

    def detect_speech(
        self,
        audio_path: str,
        sample_rate: int = 16000,
    ) -> list[dict[str, float]]:
        """
        Detect speech segments in an audio file.

        Args:
            audio_path: Path to a WAV file (16kHz mono recommended).
            sample_rate: Sample rate of the input audio.

        Returns:
            List of speech segments: [{"start": float, "end": float}]
        """
        if self._model is None or self._utils is None:
            logger.warning("silero_vad: model not loaded, returning full duration as speech")
            return [{"start": 0.0, "end": 999999.0}]

        try:
            import torch  # type: ignore[import-not-found]
            (get_speech_timestamps, _, read_audio, *_) = self._utils

            wav = read_audio(audio_path, sampling_rate=sample_rate)

            speech_timestamps = get_speech_timestamps(
                wav,
                self._model,
                threshold=self.threshold,
                sampling_rate=sample_rate,
                min_speech_duration_ms=self.min_speech_duration_ms,
                min_silence_duration_ms=self.min_silence_duration_ms,
                return_seconds=True,
            )

            segments = [
                {"start": round(ts["start"], 3), "end": round(ts["end"], 3)}
                for ts in speech_timestamps
            ]

            logger.info(
                "silero_vad.detected: %d segments, %.1fs total speech",
                len(segments),
                sum(s["end"] - s["start"] for s in segments),
            )

            return segments

        except Exception as e:
            logger.error("silero_vad.detect_failed: %s", e)
            return [{"start": 0.0, "end": 999999.0}]


# ─── Main Preprocessor ────────────────────────────────────────


class AudioPreprocessor:
    """
    Complete audio preprocessing pipeline.

    Orchestrates: probe → transcode → normalize → VAD → chunk.

    Usage:
        preprocessor = AudioPreprocessor(PreprocessConfig())
        result = await preprocessor.prepare(
            input_path="recording.mp3",
            output_dir="/processed/",
        )
    """

    def __init__(self, config: Optional[PreprocessConfig] = None):
        self.config = config or PreprocessConfig()
        self._vad: Optional[SileroVAD] = None
        self._ffmpeg_available: Optional[bool] = None

    async def check_ffmpeg(self) -> bool:
        """Verify that FFmpeg is available on the system."""
        if self._ffmpeg_available is not None:
            return self._ffmpeg_available

        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.ffmpeg_path, "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            self._ffmpeg_available = proc.returncode == 0
            if self._ffmpeg_available:
                version_line = stdout.decode("utf-8", errors="replace").split("\n")[0]
                logger.info("audio.ffmpeg_available: %s", version_line.strip())
            else:
                logger.error("audio.ffmpeg_not_available")
        except FileNotFoundError:
            self._ffmpeg_available = False
            logger.error(
                "audio.ffmpeg_not_found: FFmpeg not on PATH. "
                "Install from https://ffmpeg.org/download.html"
            )

        return self._ffmpeg_available

    def load_vad(self) -> bool:
        """Load the Silero VAD model (optional, for noisy recordings)."""
        if not self.config.apply_vad:
            return False
        self._vad = SileroVAD(
            threshold=self.config.vad_threshold,
            min_speech_duration_ms=self.config.vad_min_speech_duration_ms,
            min_silence_duration_ms=self.config.vad_min_silence_duration_ms,
        )
        return self._vad.load()

    async def prepare(
        self,
        input_path: str,
        output_dir: str,
        session_id: str = "",
    ) -> PreprocessResult:
        """
        Run the full preprocessing pipeline on an audio file.

        Steps:
            1. Probe the input file (format, duration, sample rate)
            2. Transcode to 16kHz mono WAV with normalization
            3. (Optional) Run VAD to detect speech segments
            4. Chunk if duration exceeds max_duration_seconds
            5. Return metadata + processed file paths

        Args:
            input_path: Path to the uploaded audio file.
            output_dir: Directory for processed output files.
            session_id: Session ID for logging context.

        Returns:
            PreprocessResult with output path(s) and metadata.

        Raises:
            FileNotFoundError: If input file doesn't exist.
            RuntimeError: If FFmpeg is not available.
        """
        import time
        t0 = time.monotonic()

        input_p = Path(input_path)
        if not input_p.exists():
            raise FileNotFoundError(f"Audio file not found: {input_path}")

        os.makedirs(output_dir, exist_ok=True)
        stages: list[str] = []
        warnings: list[str] = []

        # Verify FFmpeg
        if not await self.check_ffmpeg():
            raise RuntimeError(
                "FFmpeg is required for audio preprocessing but was not found. "
                "Install FFmpeg and ensure it's on your PATH."
            )

        # Step 1: Probe input
        try:
            input_meta = await probe_audio(
                input_path, ffprobe_path=self.config.ffprobe_path
            )
            stages.append("probe")
        except RuntimeError as e:
            # If ffprobe fails, create minimal metadata from file info
            logger.warning("audio.probe_failed: %s — using file-based metadata", e)
            warnings.append(f"Probe failed: {e}")
            input_meta = AudioMetadata(
                file_path=input_path,
                original_filename=input_p.name,
                format=input_p.suffix.lstrip("."),
                file_size_bytes=input_p.stat().st_size,
            )

        original_duration = input_meta.duration_seconds

        # Step 2: Transcode to 16kHz mono WAV
        stem = input_p.stem
        output_wav = os.path.join(output_dir, f"{stem}_16k.wav")

        needs_transcode = (
            input_meta.sample_rate != self.config.target_sample_rate
            or input_meta.channels != self.config.target_channels
            or input_meta.format not in ("wav", "pcm")
            or self.config.normalize_audio
        )

        if needs_transcode:
            await transcode_to_wav(
                input_path=input_path,
                output_path=output_wav,
                sample_rate=self.config.target_sample_rate,
                channels=self.config.target_channels,
                normalize=self.config.normalize_audio,
                target_loudness_lufs=self.config.target_loudness_lufs,
                peak_limit_db=self.config.peak_limit_db,
                ffmpeg_path=self.config.ffmpeg_path,
            )
            stages.append("transcode")
            if self.config.normalize_audio:
                stages.append("normalize")
        else:
            # Input is already 16kHz mono WAV — just copy
            shutil.copy2(input_path, output_wav)
            stages.append("copy")

        # Step 3: Probe the processed file for accurate metadata
        processed_meta = await probe_audio(output_wav, self.config.ffprobe_path)
        processed_meta.original_filename = input_p.name
        processed_meta.normalized = self.config.normalize_audio and needs_transcode
        if input_meta.sample_rate != self.config.target_sample_rate:
            processed_meta.resampled_from = input_meta.sample_rate

        # Step 4: Chunk if needed
        chunk_paths: list[str] = []
        if processed_meta.duration_seconds > self.config.max_duration_seconds:
            chunk_dir = os.path.join(output_dir, "chunks")
            chunk_paths = await chunk_audio(
                input_path=output_wav,
                output_dir=chunk_dir,
                chunk_duration_seconds=self.config.chunk_duration_seconds,
                overlap_seconds=self.config.chunk_overlap_seconds,
                ffmpeg_path=self.config.ffmpeg_path,
            )
            stages.append("chunk")
            logger.info(
                "audio.chunked_long_recording: session=%s dur=%s chunks=%d",
                session_id,
                processed_meta.duration_seconds,
                len(chunk_paths),
            )

        elapsed = time.monotonic() - t0

        result = PreprocessResult(
            output_path=output_wav,
            metadata=processed_meta,
            chunk_paths=chunk_paths,
            stages_applied=stages,
            original_duration_seconds=original_duration,
            processing_time_seconds=round(elapsed, 3),
            warnings=warnings,
        )

        logger.info(
            "audio.preprocess_complete: session=%s %s -> %s stages=%s dur=%ss elapsed=%ss",
            session_id,
            input_p.name,
            os.path.basename(output_wav),
            stages,
            processed_meta.duration_seconds,
            round(elapsed, 3),
        )

        return result
