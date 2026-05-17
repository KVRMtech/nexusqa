"""Clip service helper-function tests (no ffmpeg, no S3 required).

The full ClipService end-to-end test needs a real ffmpeg binary and an
S3 mock (e.g. moto); those land in CI integration tests. Here we
verify the pure-function helpers that are easy to break and quietly
catastrophic when wrong.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

import pytest

from app.clips.s3 import S3StorageConfig
from app.clips.service import (
    ClipRequest,
    _ms_to_ffmpeg_time,
    _sha256_file,
)


# ── _ms_to_ffmpeg_time ─────────────────────────────────────────


@pytest.mark.parametrize(
    "ms,expected",
    [
        (0, "00:00:00.000"),
        (1, "00:00:00.001"),
        (1000, "00:00:01.000"),
        (60_000, "00:01:00.000"),
        (3_600_000, "01:00:00.000"),
        (3_661_500, "01:01:01.500"),
        (90_125, "00:01:30.125"),
    ],
)
def test_ms_to_ffmpeg_time_formats_correctly(ms: int, expected: str) -> None:
    assert _ms_to_ffmpeg_time(ms) == expected


def test_ms_to_ffmpeg_time_clamps_negative() -> None:
    assert _ms_to_ffmpeg_time(-5) == "00:00:00.000"


# ── _sha256_file ──────────────────────────────────────────────


def test_sha256_file_matches_hashlib() -> None:
    payload = b"binary clip body - hash me end-to-end"
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, payload)
        os.close(fd)
        assert _sha256_file(path) == hashlib.sha256(payload).hexdigest()
    finally:
        os.unlink(path)


def test_sha256_file_streams_large_input() -> None:
    """Hashing must work for files larger than one read chunk."""
    chunk = b"x" * (1024 * 1024)  # 1 MB
    payload = chunk * 3
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, payload)
        os.close(fd)
        assert _sha256_file(path) == hashlib.sha256(payload).hexdigest()
    finally:
        os.unlink(path)


# ── S3StorageConfig key generation ──────────────────────────────


def test_s3_key_format_includes_tenant_and_session() -> None:
    cfg = S3StorageConfig(bucket="nexus-clips", prefix="v1/clips/")
    key = cfg.s3_key(
        tenant_id="t47",
        session_id="sess-abc",
        clip_id="deadbeef",
        extension="mp4",
    )
    assert key == "v1/clips/t47/sess-abc/deadbeef.mp4"


def test_s3_key_strips_dot_from_extension() -> None:
    cfg = S3StorageConfig(bucket="nexus-clips")
    key = cfg.s3_key(
        tenant_id="t", session_id="s", clip_id="cid", extension=".m4a"
    )
    assert key.endswith("cid.m4a")


def test_thumbnail_key_uses_jpg() -> None:
    cfg = S3StorageConfig(bucket="nexus-clips")
    key = cfg.thumbnail_key(tenant_id="t", session_id="s", clip_id="cid")
    assert key.endswith("cid.jpg")


# ── ClipRequest window normalisation ──────────────────────────


def test_clip_request_pads_window() -> None:
    req = ClipRequest(
        tenant_id="t",
        session_id="s",
        artifact_id="a",
        segment_id=None,
        kind="video",
        start_ms=10_000,
        end_ms=15_000,
        pad_before_ms=5_000,
        pad_after_ms=5_000,
    )
    start, end = req.normalised_window()
    assert start == 5_000
    assert end == 20_000


def test_clip_request_clamps_negative_start() -> None:
    req = ClipRequest(
        tenant_id="t",
        session_id="s",
        artifact_id="a",
        segment_id=None,
        kind="audio",
        start_ms=1_000,
        end_ms=2_000,
        pad_before_ms=5_000,
        pad_after_ms=0,
    )
    start, end = req.normalised_window()
    assert start == 0
    assert end >= 1
