"""Chunker tests — pure Python, no DB.

Validates the two input paths (structured vs plain text), speaker
boundary preservation, idempotency, and the max-chunks safety net.
"""

from __future__ import annotations

import pytest

from app.chunker import (
    Chunk,
    ChunkerConfig,
    TranscriptChunker,
)


def _make_chunker(target=400, min_chars=80, overlap=80, max_chunks=1000):
    return TranscriptChunker(
        ChunkerConfig(
            target_chars=target,
            min_chars=min_chars,
            overlap_chars=overlap,
            max_chunks=max_chunks,
        )
    )


# ── Empty inputs ────────────────────────────────────────────────


def test_empty_transcript_returns_no_chunks() -> None:
    chunker = _make_chunker()
    assert chunker.chunk_artifact({}) == []
    assert chunker.chunk_artifact({"safe_transcript_text": ""}) == []
    assert (
        chunker.chunk_artifact({"safe_transcript_text": "   \n   "}) == []
    )


# ── Plain text path ─────────────────────────────────────────────


def test_plain_text_produces_chunks_and_stamps_timestamps() -> None:
    chunker = _make_chunker(target=200, min_chars=50, overlap=40)
    text = (
        "First sentence of the transcript. "
        "Second sentence with more content. "
        "Third sentence here. Fourth sentence wraps up the demo. "
        "Fifth and sixth sentences continue the discussion. "
        "Seventh and eighth round things out nicely. "
        "Ninth provides a recap. Tenth is the final note for now. "
    ) * 4
    artifact = {
        "safe_transcript_text": text,
        "duration_seconds": 60.0,
    }
    chunks = chunker.chunk_artifact(artifact)
    assert len(chunks) >= 2
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.start_ms >= 0
        assert c.end_ms >= c.start_ms
        assert c.text.strip()
        assert c.token_count is not None
        assert c.speaker_id is None  # no speakers in plain text
    # Ordinals are strictly increasing.
    assert [c.ordinal for c in chunks] == sorted(c.ordinal for c in chunks)


def test_text_hash_is_stable_for_identical_input() -> None:
    chunker = _make_chunker()
    artifact = {
        "safe_transcript_text": "Stable sentence. Another sentence.",
        "duration_seconds": 10.0,
    }
    a = chunker.chunk_artifact(artifact)
    b = chunker.chunk_artifact(artifact)
    assert [c.text_hash for c in a] == [c.text_hash for c in b]


# ── Structured path ─────────────────────────────────────────────


def test_structured_transcript_path_preserves_speakers() -> None:
    chunker = _make_chunker(target=200, min_chars=40, overlap=40)
    artifact = {
        "duration_seconds": 30.0,
        "full_artifact_json": {
            "transcript": {
                "segments": [
                    {
                        "speaker": "alex",
                        "speaker_role": "ui_lead",
                        "text": "Welcome to the LT5 walkthrough today.",
                        "start_ms": 0,
                        "end_ms": 4000,
                        "confidence": 0.91,
                    },
                    {
                        "speaker": "alex",
                        "speaker_role": "ui_lead",
                        "text": (
                            "We'll cover the quote form, the tobacco section, "
                            "and the rate display in order."
                        ),
                        "start_ms": 4000,
                        "end_ms": 9000,
                        "confidence": 0.93,
                    },
                    {
                        "speaker": "brenda",
                        "speaker_role": "backend",
                        "text": (
                            "From the backend side, the form submits to "
                            "/api/v2/quote/generate."
                        ),
                        "start_ms": 9000,
                        "end_ms": 14000,
                        "confidence": 0.95,
                    },
                ]
            }
        },
    }
    chunks = chunker.chunk_artifact(artifact)
    assert chunks, "must produce at least one chunk per speaker group"
    speakers = {c.speaker_id for c in chunks}
    assert speakers == {"alex", "brenda"}

    alex_chunks = [c for c in chunks if c.speaker_id == "alex"]
    brenda_chunks = [c for c in chunks if c.speaker_id == "brenda"]
    # Alex's window precedes Brenda's.
    assert max(c.end_ms for c in alex_chunks) <= max(
        c.end_ms for c in brenda_chunks
    )
    for c in alex_chunks:
        assert c.speaker_role == "ui_lead"
    for c in brenda_chunks:
        assert c.speaker_role == "backend"


def test_structured_path_accepts_seconds_or_milliseconds() -> None:
    chunker = _make_chunker(target=200, min_chars=40, overlap=40)
    artifact = {
        "duration_seconds": 10.0,
        "full_artifact_json": {
            "transcript": {
                "segments": [
                    {
                        "speaker": "alex",
                        "text": "Seconds-encoded start and end values.",
                        "start": 0.5,
                        "end": 4.5,
                    },
                    {
                        "speaker": "alex",
                        "text": "Milliseconds-encoded same content.",
                        "start_ms": 4500,
                        "end_ms": 8500,
                    },
                ]
            }
        },
    }
    chunks = chunker.chunk_artifact(artifact)
    assert chunks
    assert all(c.start_ms >= 0 for c in chunks)
    assert all(c.end_ms >= c.start_ms for c in chunks)


# ── Limits ─────────────────────────────────────────────────────


def test_max_chunks_caps_output() -> None:
    # tiny target + huge text → many windows; cap at 5.
    chunker = _make_chunker(target=100, min_chars=20, overlap=10, max_chunks=5)
    artifact = {
        "safe_transcript_text": ("Some sentence. " * 2000),
        "duration_seconds": 100.0,
    }
    chunks = chunker.chunk_artifact(artifact)
    assert len(chunks) == 5


# ── Config validation ──────────────────────────────────────────


def test_invalid_chunker_config_rejected() -> None:
    with pytest.raises(ValueError):
        ChunkerConfig(target_chars=10)  # below floor
    with pytest.raises(ValueError):
        ChunkerConfig(overlap_chars=500, target_chars=200)
    with pytest.raises(ValueError):
        ChunkerConfig(min_chars=-1)
    with pytest.raises(ValueError):
        ChunkerConfig(max_chunks=0)
