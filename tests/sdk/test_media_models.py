"""
SDK Media Models — Comprehensive Unit Tests.

Tests all Pydantic models, validators, computed properties,
and enum values in nexus_sdk.media.models.
"""

import pytest
import sys
import os
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ─────────────────────────────────────────────────────


class TestApplicationType:
    def test_all_values(self):
        from nexus_sdk.media.models import ApplicationType
        expected = {
            "web_ui", "desktop_app", "excel_spreadsheet",
            "mainframe_3270", "pdf_document", "email_client",
            "terminal", "database_ui", "unknown",
        }
        actual = {t.value for t in ApplicationType}
        assert actual == expected

    def test_string_comparison(self):
        from nexus_sdk.media.models import ApplicationType
        assert ApplicationType.WEB_UI == "web_ui"
        assert ApplicationType.MAINFRAME_3270 == "mainframe_3270"


class TestMediaJobStatus:
    def test_all_values(self):
        from nexus_sdk.media.models import MediaJobStatus
        expected = {
            "queued", "preprocessing", "processing",
            "aligning", "completed", "failed", "cancelled",
        }
        actual = {s.value for s in MediaJobStatus}
        assert actual == expected


class TestAudioFormat:
    def test_wav_format(self):
        from nexus_sdk.media.models import AudioFormat
        assert AudioFormat.WAV == "wav"
        assert AudioFormat.MP3 == "mp3"


class TestVideoFormat:
    def test_mp4_format(self):
        from nexus_sdk.media.models import VideoFormat
        assert VideoFormat.MP4 == "mp4"
        assert VideoFormat.WEBM == "webm"


# ─── AudioMetadata ─────────────────────────────────────────────


class TestAudioMetadata:
    def test_create_with_required_fields(self):
        from nexus_sdk.media.models import AudioMetadata
        meta = AudioMetadata(file_path="/data/audio/test.wav")
        assert meta.file_path == "/data/audio/test.wav"
        assert meta.sample_rate == 16000
        assert meta.channels == 1
        assert meta.format == "wav"
        assert meta.normalized is False
        assert meta.resampled_from is None

    def test_full_metadata(self):
        from nexus_sdk.media.models import AudioMetadata
        meta = AudioMetadata(
            file_path="/data/audio/recording.mp3",
            original_filename="interview.mp3",
            format="mp3",
            sample_rate=44100,
            channels=2,
            duration_seconds=300.5,
            file_size_bytes=4_800_000,
            bit_depth=16,
            codec="mp3",
            normalized=True,
            noise_reduced=False,
            resampled_from=44100,
        )
        assert meta.duration_seconds == 300.5
        assert meta.resampled_from == 44100
        assert meta.normalized is True


# ─── SpeakerInfo ───────────────────────────────────────────────


class TestSpeakerInfo:
    def test_create(self):
        from nexus_sdk.media.models import SpeakerInfo
        speaker = SpeakerInfo(
            speaker_id="SPEAKER_00",
            display_name="John SME",
            total_speaking_time=120.5,
            segment_count=15,
            average_confidence=0.85,
        )
        assert speaker.speaker_id == "SPEAKER_00"
        assert speaker.total_speaking_time == 120.5

    def test_defaults(self):
        from nexus_sdk.media.models import SpeakerInfo
        speaker = SpeakerInfo(speaker_id="SPK")
        assert speaker.display_name is None
        assert speaker.total_speaking_time == 0.0
        assert speaker.embedding_id is None


# ─── TranscriptionSegment ─────────────────────────────────────


class TestTranscriptionSegment:
    def test_create_valid(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPEAKER_00",
            text="Hello, this is a test.",
            start_time=0.0,
            end_time=3.5,
            confidence=0.95,
        )
        assert seg.speaker == "SPEAKER_00"
        assert seg.text == "Hello, this is a test."
        assert seg.start_time == 0.0
        assert seg.end_time == 3.5
        assert seg.segment_id  # auto-generated UUID

    def test_end_before_start_raises(self):
        from nexus_sdk.media.models import TranscriptionSegment
        with pytest.raises(ValueError, match="end_time.*must be >= start_time"):
            TranscriptionSegment(
                speaker="SPK",
                text="test",
                start_time=5.0,
                end_time=2.0,
            )

    def test_duration_property(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK", text="test", start_time=1.0, end_time=4.5
        )
        assert seg.duration == 3.5

    def test_word_count_property(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK",
            text="Hello world this is five words",
            start_time=0, end_time=5,
        )
        assert seg.word_count == 6

    def test_word_count_empty(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK", text="", start_time=0, end_time=1
        )
        assert seg.word_count == 0

    def test_negative_confidence_allowed(self):
        """Whisper log-probs can be negative."""
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK", text="test",
            start_time=0, end_time=1,
            confidence=-0.5,
        )
        assert seg.confidence == -0.5

    def test_words_field(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK", text="hello world",
            start_time=0, end_time=2,
            words=[
                {"word": "hello", "start": 0.0, "end": 0.8, "probability": 0.99},
                {"word": "world", "start": 0.9, "end": 1.8, "probability": 0.97},
            ],
        )
        assert len(seg.words) == 2
        assert seg.words[0]["word"] == "hello"


# ─── TranscriptionResult ──────────────────────────────────────


class TestTranscriptionResult:
    def _make_segments(self):
        from nexus_sdk.media.models import TranscriptionSegment
        return [
            TranscriptionSegment(
                speaker="SPEAKER_00", text="Hello world",
                start_time=0.0, end_time=2.0, confidence=0.9,
            ),
            TranscriptionSegment(
                speaker="SPEAKER_01", text="How are you today",
                start_time=2.0, end_time=5.0, confidence=0.85,
            ),
            TranscriptionSegment(
                speaker="SPEAKER_00", text="I am fine thanks",
                start_time=5.0, end_time=8.0, confidence=0.88,
            ),
        ]

    def test_create(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=self._make_segments(),
        )
        assert result.job_id == "j-001"
        assert len(result.segments) == 3

    def test_full_text(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=self._make_segments(),
        )
        assert result.full_text == "Hello world How are you today I am fine thanks"

    def test_speaker_names(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=self._make_segments(),
        )
        names = result.speaker_names()
        assert "SPEAKER_00" in names
        assert "SPEAKER_01" in names

    def test_segments_by_speaker(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=self._make_segments(),
        )
        spk0_segs = result.segments_by_speaker("SPEAKER_00")
        assert len(spk0_segs) == 2

    def test_compute_stats(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=self._make_segments(),
        )
        result.compute_stats()
        assert result.segment_count == 3
        assert result.word_count == 10  # 2 + 4 + 4
        assert result.duration_seconds == 8.0
        assert len(result.speakers) == 2

    def test_compute_stats_empty(self):
        from nexus_sdk.media.models import TranscriptionResult
        result = TranscriptionResult(
            job_id="j-001", session_id="s-001", segments=[]
        )
        result.compute_stats()
        assert result.segment_count == 0
        assert result.word_count == 0
        assert len(result.speakers) == 0


# ─── UIElement ─────────────────────────────────────────────────


class TestUIElement:
    def test_create(self):
        from nexus_sdk.media.models import UIElement
        elem = UIElement(
            element_type="button",
            text="Submit",
            bbox=[10.0, 20.0, 100.0, 50.0],
            confidence=0.95,
            properties={"enabled": True},
        )
        assert elem.element_type == "button"
        assert len(elem.bbox) == 4

    def test_empty_bbox_allowed(self):
        from nexus_sdk.media.models import UIElement
        elem = UIElement(element_type="label", text="Name")
        assert elem.bbox == []

    def test_invalid_bbox_length_raises(self):
        from nexus_sdk.media.models import UIElement
        with pytest.raises(ValueError, match="bbox must have exactly 4"):
            UIElement(
                element_type="button",
                text="X",
                bbox=[10, 20, 100],  # only 3 values
            )

    def test_default_confidence(self):
        from nexus_sdk.media.models import UIElement
        elem = UIElement(element_type="label")
        assert elem.confidence == 0.0
        assert elem.properties == {}


# ─── FrameAnalysis ─────────────────────────────────────────────


class TestFrameAnalysis:
    def test_create(self):
        from nexus_sdk.media.models import FrameAnalysis, ApplicationType
        fa = FrameAnalysis(
            frame_index=0,
            timestamp_seconds=5.5,
            application_type=ApplicationType.WEB_UI,
            page_title="Login Page",
            extracted_text="Username Password",
        )
        assert fa.frame_index == 0
        assert fa.application_type == ApplicationType.WEB_UI
        assert fa.frame_id  # auto-generated

    def test_defaults(self):
        from nexus_sdk.media.models import FrameAnalysis, ApplicationType
        fa = FrameAnalysis(frame_index=0, timestamp_seconds=0.0)
        assert fa.application_type == ApplicationType.UNKNOWN
        assert fa.ui_elements == []
        assert fa.is_keyframe is False


# ─── VisualAnalysisResult ─────────────────────────────────────


class TestVisualAnalysisResult:
    def test_compute_stats(self):
        from nexus_sdk.media.models import (
            VisualAnalysisResult, FrameAnalysis, ApplicationType,
        )
        result = VisualAnalysisResult(
            job_id="j-001",
            session_id="s-001",
            frames=[
                FrameAnalysis(
                    frame_index=0, timestamp_seconds=0.0,
                    application_type=ApplicationType.WEB_UI,
                ),
                FrameAnalysis(
                    frame_index=1, timestamp_seconds=3.0,
                    application_type=ApplicationType.EXCEL_SPREADSHEET,
                ),
                FrameAnalysis(
                    frame_index=2, timestamp_seconds=7.0,
                    application_type=ApplicationType.WEB_UI,
                ),
            ],
        )
        result.compute_stats()
        assert result.total_frames_analyzed == 3
        assert result.duration_seconds == 7.0
        assert "web_ui" in result.application_types_seen
        assert "excel_spreadsheet" in result.application_types_seen


# ─── Processing Jobs ──────────────────────────────────────────


class TestAudioProcessingJob:
    def test_create(self):
        from nexus_sdk.media.models import AudioProcessingJob, MediaJobStatus
        job = AudioProcessingJob(
            tenant_id="t-001",
            session_id="s-001",
            audio_path="/data/test.wav",
        )
        assert job.status == MediaJobStatus.QUEUED
        assert job.progress_percent == 0.0
        assert job.current_stage == "queued"
        assert job.error is None
        assert job.result is None


class TestVideoProcessingJob:
    def test_create(self):
        from nexus_sdk.media.models import VideoProcessingJob, MediaJobStatus
        job = VideoProcessingJob(
            tenant_id="t-001",
            session_id="s-001",
            video_path="/data/test.mp4",
        )
        assert job.status == MediaJobStatus.QUEUED
        assert job.progress_percent == 0.0


# ─── Serialization Round-Trip ──────────────────────────────────


class TestSerialization:
    def test_transcription_segment_roundtrip(self):
        from nexus_sdk.media.models import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPEAKER_00",
            text="Hello world",
            start_time=0.0,
            end_time=2.0,
            confidence=0.9,
        )
        data = seg.model_dump(mode="json")
        restored = TranscriptionSegment(**data)
        assert restored.speaker == seg.speaker
        assert restored.text == seg.text
        assert restored.start_time == seg.start_time

    def test_transcription_result_roundtrip(self):
        from nexus_sdk.media.models import TranscriptionResult, TranscriptionSegment
        result = TranscriptionResult(
            job_id="j-001",
            session_id="s-001",
            segments=[
                TranscriptionSegment(
                    speaker="SPK", text="test",
                    start_time=0, end_time=1, confidence=0.5,
                ),
            ],
        )
        result.compute_stats()
        data = result.model_dump(mode="json")
        restored = TranscriptionResult(**data)
        assert len(restored.segments) == 1
        assert restored.segment_count == 1

    def test_visual_analysis_result_roundtrip(self):
        from nexus_sdk.media.models import (
            VisualAnalysisResult, FrameAnalysis, ApplicationType, UIElement,
        )
        result = VisualAnalysisResult(
            job_id="j-001",
            session_id="s-001",
            frames=[
                FrameAnalysis(
                    frame_index=0,
                    timestamp_seconds=0.0,
                    application_type=ApplicationType.WEB_UI,
                    ui_elements=[
                        UIElement(element_type="button", text="OK"),
                    ],
                    extracted_text="Click OK",
                ),
            ],
        )
        data = result.model_dump(mode="json")
        restored = VisualAnalysisResult(**data)
        assert len(restored.frames) == 1
        assert restored.frames[0].ui_elements[0].text == "OK"
