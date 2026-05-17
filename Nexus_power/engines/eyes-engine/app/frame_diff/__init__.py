"""
Eyes Engine — Frame Extraction & Diffing Module.

Extracts meaningful frames from video recordings using OpenCV.
Uses perceptual hashing (dHash) with Hamming distance to skip
visually similar frames, ensuring only significant visual changes
are processed downstream.

Features:
  - OpenCV-based frame extraction with configurable FPS
  - Perceptual hash (dHash) with Hamming distance deduplication
  - Adaptive sampling: slows extraction during stable periods,
    bursts during rapid screen changes
  - Async-safe via asyncio.to_thread
  - Stub fallback for environments without OpenCV
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import structlog
from nexus_sdk.events import fire_stub_alert

logger = structlog.get_logger()

# Adaptive sampling thresholds.
#
# These values are tuned for QA/demo recordings where every UI state matters
# (form fills, click feedback, modal dialogs, single-character text input).
_STABLE_DISTANCE = 0.02       # Below this = "nothing changed"
_BURST_DISTANCE = 0.30        # Above this = rapid change, burst capture
_STABLE_WINDOW = 4            # Consecutive stable frames before slowdown.
_STABLE_INTERVAL_MULT = 1.0   # Multiply frame_interval by this during stable.
                              # Set to 1.0 (no slowdown): when the user is
                              # slowly filling a form, dHash similarity is
                              # high even though the form state IS changing
                              # (each keystroke affects only 1-3% of bits).
                              # Slowing sampling here creates blind windows
                              # where the filled state is never captured.
_BURST_INTERVAL_DIV = 2.0     # Divide frame_interval by this during burst

# Force-keep a frame at most this many seconds apart, regardless of dHash
# similarity. Without this guard, a slow form-fill (each keystroke ~1-3%
# dHash diff, below the extraction threshold) produces a blackout window
# until the next big visual change (URL navigation, modal). 1.5 s gives
# us at least one captured frame every form-fill state without flooding
# the pipeline with near-duplicate frames during truly static periods.
_MAX_GAP_SECONDS = 1.5


class FrameExtractor:
    """
    Extracts meaningful frames from video recordings.

    Uses Hamming distance on dHash to skip visually similar frames,
    with optional adaptive sampling that adjusts extraction rate
    based on screen stability.
    """

    def __init__(
        self,
        frame_diff_threshold: float = 0.03,  # lowered from 0.05 — dHash on 9×8
                                             # downsampled image: text-field changes
                                             # affect only 1-3% of bits so 5% threshold
                                             # was silently dropping all form interactions
        max_fps_extract: float = 2.0,        # raised from 1.0 — 2fps baseline ensures
                                             # at most 0.5 s gap between extracted frames
                                             # before adaptive slow-down applies
        keyframe_only: bool = False,
        adaptive_sampling: bool = True,
    ):
        self.frame_diff_threshold = frame_diff_threshold
        self.max_fps_extract = max_fps_extract
        self.keyframe_only = keyframe_only
        self.adaptive_sampling = adaptive_sampling
        self._event_bus = None
        self._stub_fallback_count: int = 0
        self._cv2_available: Optional[bool] = None

    @staticmethod
    def _hamming_distance(hash_a: str, hash_b: str) -> float:
        """Normalized Hamming distance between two hex-encoded hashes.

        Returns 0.0 for identical hashes, 1.0 for completely different.
        """
        int_a = int(hash_a, 16)
        int_b = int(hash_b, 16)
        xor = int_a ^ int_b
        differing_bits = bin(xor).count("1")
        total_bits = len(hash_a) * 4  # 4 bits per hex char
        return differing_bits / total_bits if total_bits > 0 else 0.0

    async def extract_frames(
        self,
        video_path: str,
        output_dir: str,
    ) -> list[dict]:
        """
        Extract unique frames from video.

        Returns list of {frame_path, timestamp, index, hash, source_frame_idx}.
        """
        os.makedirs(output_dir, exist_ok=True)

        try:
            return await asyncio.to_thread(
                self._extract_with_opencv, video_path, output_dir
            )
        except ImportError:
            return self._stub_extract(video_path, output_dir)

    def _extract_with_opencv(
        self,
        video_path: str,
        output_dir: str,
    ) -> list[dict]:
        """Extract frames using OpenCV with Hamming-distance filtering."""
        import cv2

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        base_interval = max(1, int(fps / self.max_fps_extract))

        frames: list[dict] = []
        prev_hash: Optional[str] = None
        frame_idx = 0
        extracted_idx = 0
        last_extracted_source_frame_idx: Optional[int] = None
        max_gap_frames = max(1, int(fps * _MAX_GAP_SECONDS))

        # Adaptive sampling state
        current_interval = base_interval
        consecutive_stable = 0

        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % current_interval == 0:
                current_hash = self._compute_frame_hash(frame, cv2)

                if prev_hash is None:
                    distance = 1.0  # Force-extract first frame
                else:
                    distance = self._hamming_distance(current_hash, prev_hash)

                is_different = distance > self.frame_diff_threshold
                # Force-keep when the gap since the last extracted frame
                # exceeds _MAX_GAP_SECONDS. This is the guarantee that we
                # never produce a 5-second blackout while the user is
                # actively interacting (form fill, dropdown, typing) —
                # those interactions register sub-threshold dHash diff and
                # the legacy logic dropped them silently.
                force_keep_by_gap = (
                    last_extracted_source_frame_idx is not None
                    and (frame_idx - last_extracted_source_frame_idx) >= max_gap_frames
                )

                if is_different or force_keep_by_gap:
                    timestamp = frame_idx / fps
                    frame_path = os.path.join(
                        output_dir, f"frame_{extracted_idx:05d}.png"
                    )
                    cv2.imwrite(frame_path, frame)

                    frames.append({
                        "frame_path": frame_path,
                        "timestamp": round(timestamp, 3),
                        "index": extracted_idx,
                        "source_frame_idx": frame_idx,
                        "hash": current_hash,
                        "forced_by_gap": bool(force_keep_by_gap and not is_different),
                    })
                    extracted_idx += 1
                    prev_hash = current_hash
                    last_extracted_source_frame_idx = frame_idx
                    consecutive_stable = 0

                    # Adaptive: burst capture on large jumps
                    if self.adaptive_sampling and distance > _BURST_DISTANCE:
                        current_interval = max(
                            1, int(base_interval / _BURST_INTERVAL_DIV)
                        )
                else:
                    consecutive_stable += 1

                # Adaptive: slow down during stable periods
                if self.adaptive_sampling:
                    if consecutive_stable >= _STABLE_WINDOW:
                        current_interval = int(
                            base_interval * _STABLE_INTERVAL_MULT
                        )
                    elif consecutive_stable == 0:
                        pass  # Already handled above for burst
                    else:
                        current_interval = base_interval

            frame_idx += 1

        cap.release()

        duration = frame_idx / fps if fps > 0 else 0
        logger.info(
            "frame_extractor.completed",
            video=os.path.basename(video_path),
            total_source_frames=frame_idx,
            extracted_frames=len(frames),
            video_fps=round(fps, 1),
            duration_seconds=round(duration, 1),
            adaptive=self.adaptive_sampling,
        )

        return frames

    @staticmethod
    def _compute_frame_hash(frame, cv2) -> str:
        """
        Compute a perceptual hash for frame deduplication.

        Uses difference hash (dHash) on a downscaled grayscale image.
        Two visually similar frames will produce the same hash.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Resize to 9x8 for dHash (produces 8x8 = 64-bit hash)
        resized = cv2.resize(gray, (9, 8))

        # dHash: compare adjacent pixels
        diff = resized[:, 1:] > resized[:, :-1]
        # Convert to a hex string
        hash_bits = diff.flatten()
        hash_int = 0
        for bit in hash_bits:
            hash_int = (hash_int << 1) | int(bit)
        return f"{hash_int:016x}"

    def _stub_extract(self, video_path: str, output_dir: str) -> list[dict]:
        """Development stub when OpenCV is not available."""
        self._stub_fallback_count += 1
        logger.warning(
            "frame_extractor.stub_fallback #%d", self._stub_fallback_count
        )
        fire_stub_alert(
            self._event_bus, "eyes", "frame_extractor",
            fallback_count=self._stub_fallback_count,
            reason="OpenCV not installed",
        )
        return [
            {
                "frame_path": f"{output_dir}/frame_00000.png",
                "timestamp": 0.0,
                "index": 0,
                "source_frame_idx": 0,
                "hash": "0000000000000000",
            },
            {
                "frame_path": f"{output_dir}/frame_00001.png",
                "timestamp": 3.0,
                "index": 1,
                "source_frame_idx": 90,
                "hash": "ffffffffffffffff",
            },
            {
                "frame_path": f"{output_dir}/frame_00002.png",
                "timestamp": 7.0,
                "index": 2,
                "source_frame_idx": 210,
                "hash": "00000000ffffffff",
            },
        ]


async def probe_video(video_path: str) -> dict:
    """
    Probe a video file for metadata using OpenCV.

    Returns dict with: duration_seconds, width, height, fps, total_frames, codec.
    """
    try:
        import cv2

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        codec = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])
        duration = total / fps if fps > 0 else 0

        cap.release()

        return {
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "total_frames": total,
            "codec": codec.strip("\x00"),
        }
    except (ImportError, RuntimeError, Exception) as exc:
        logger.warning("probe_video: %s", exc)
        return {
            "duration_seconds": 0.0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "total_frames": 0,
            "codec": "unknown",
        }
