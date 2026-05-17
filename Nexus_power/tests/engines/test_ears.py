"""
Ears Engine — Unit tests.

Tests transcript segment alignment and model structures.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "ears-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


class TestTranscriptionSegment:
    """Test the TranscriptionSegment Pydantic model."""

    def test_create_valid(self):
        from main import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPEAKER_00",
            text="Hello, this is a test.",
            start_time=0.0,
            end_time=3.5,
            confidence=0.95,
            language="en",
        )
        assert seg.speaker == "SPEAKER_00"
        assert seg.text == "Hello, this is a test."
        assert seg.start_time == 0.0
        assert seg.end_time == 3.5

    def test_default_values(self):
        from main import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK",
            text="test",
            start_time=0.0,
            end_time=1.0,
            confidence=0.8,
            language="en",
        )
        assert seg.confidence == 0.8


class TestAlignSegments:
    """Test whisper + diarization alignment logic."""

    def test_basic_alignment(self):
        from main import align_segments
        whisper = [
            {"text": "Hello world", "start": 0.0, "end": 2.0, "confidence": 0.9, "language": "en"},
            {"text": "How are you", "start": 3.5, "end": 5.0, "confidence": 0.85, "language": "en"},
        ]
        speakers = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 3.0},
            {"speaker": "SPEAKER_01", "start": 3.0, "end": 6.0},
        ]
        result = align_segments(whisper, speakers)
        assert len(result) == 2
        # First segment [0, 2] → overlap with SPEAKER_00 [0, 3] = 2.0
        assert result[0].speaker == "SPEAKER_00"
        assert result[0].text == "Hello world"
        # Second segment [3.5, 5] → overlap with SPEAKER_01 [3, 6] = 1.5
        assert result[1].speaker == "SPEAKER_01"

    def test_empty_whisper(self):
        from main import align_segments
        result = align_segments([], [{"speaker": "SPK", "start": 0, "end": 5}])
        assert result == []

    def test_empty_speakers(self):
        from main import align_segments
        whisper = [{"text": "hi", "start": 0, "end": 1, "confidence": 0.9, "language": "en"}]
        result = align_segments(whisper, [])
        assert len(result) == 1
        assert result[0].speaker == "UNKNOWN"

    def test_multiple_segments_same_speaker(self):
        from main import align_segments
        whisper = [
            {"text": "Part one", "start": 0.0, "end": 1.0, "confidence": 0.9, "language": "en"},
            {"text": "Part two", "start": 1.0, "end": 2.0, "confidence": 0.9, "language": "en"},
            {"text": "Part three", "start": 2.0, "end": 3.0, "confidence": 0.9, "language": "en"},
        ]
        speakers = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
        ]
        result = align_segments(whisper, speakers)
        assert all(seg.speaker == "SPEAKER_00" for seg in result)

    def test_preserves_confidence(self):
        from main import align_segments
        whisper = [{"text": "test", "start": 0, "end": 1, "confidence": 0.42, "language": "en"}]
        speakers = [{"speaker": "SPK", "start": 0, "end": 2}]
        result = align_segments(whisper, speakers)
        assert result[0].confidence == 0.42


class TestInsuranceVocabulary:
    """Test that insurance-specific vocabulary list exists and is populated."""

    def test_vocabulary_not_empty(self):
        from main import INSURANCE_VOCABULARY
        assert len(INSURANCE_VOCABULARY) > 10

    def test_contains_key_terms(self):
        from main import INSURANCE_VOCABULARY
        vocab_lower = [v.lower() for v in INSURANCE_VOCABULARY]
        for term in ["premium", "beneficiary", "underwriting"]:
            assert any(term in v for v in vocab_lower), f"Missing vocabulary term: {term}"
