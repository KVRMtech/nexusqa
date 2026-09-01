"""Pure helpers from app.dispatches — hashing, dedup key generation."""

from __future__ import annotations

from app.dispatches import compute_dedup_key, compute_text_hash


def test_text_hash_normalises_whitespace_and_case() -> None:
    a = compute_text_hash("  Hello,\tWorld  ")
    b = compute_text_hash("hello, world")
    assert a == b


def test_text_hash_differs_for_different_text() -> None:
    assert compute_text_hash("a") != compute_text_hash("b")


def test_dedup_key_includes_channel() -> None:
    th = compute_text_hash("question?")
    a = compute_dedup_key(channel_id="C1", text_hash=th)
    b = compute_dedup_key(channel_id="C2", text_hash=th)
    assert a != b


def test_dedup_key_falls_back_when_no_channel() -> None:
    th = compute_text_hash("q")
    k = compute_dedup_key(channel_id=None, text_hash=th)
    assert isinstance(k, str) and len(k) == 64
