"""KnowledgeGapRecorder helpers — pure tests for topic-hash stability."""

from __future__ import annotations

from app.gaps import compute_topic_hash


def test_hash_is_stable_across_whitespace_and_case() -> None:
    a = compute_topic_hash("What is the CA tobacco lookback?")
    b = compute_topic_hash("  what   is the  ca   TOBACCO lookback?  ")
    assert a == b


def test_hash_strips_minor_punctuation() -> None:
    a = compute_topic_hash("How do I run a quote, exactly?")
    b = compute_topic_hash("How do I run a quote exactly")
    assert a == b


def test_hash_differs_for_different_topics() -> None:
    a = compute_topic_hash("What is the CA tobacco lookback?")
    b = compute_topic_hash("What is the eligibility for ACA subsidy?")
    assert a != b


def test_hash_handles_empty_string() -> None:
    h1 = compute_topic_hash("")
    h2 = compute_topic_hash("    ")
    # Both normalise to empty; same hash.
    assert h1 == h2
    assert len(h1) == 64
