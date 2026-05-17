"""
Ears Engine — Modular Sub-package Tests.

Tests transcription, diarization and preprocessor modules that were
refactored from the monolithic ears-engine/main.py.

These tests run without GPU models (faster-whisper, pyannote)
and validate the stub paths, data-flow, and alignment logic.
"""

import pytest
import sys
import os
import asyncio
import json
import types
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "ears-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Whisper Transcriber ──────────────────────────────────────


class TestWhisperTranscriber:
    """Test WhisperTranscriber class in stub mode (no GPU)."""

    def test_init_defaults(self):
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        assert t.model_size == "large-v3"
        assert t.fast_model_size == "medium"
        assert t.device == "cuda"
        assert t.model is None  # not loaded yet
        assert t.is_real is False

    def test_init_custom(self):
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber(
            model_size="medium",
            device="cpu",
            compute_type="int8",
            fast_model_size="small",
            fast_compute_type="int8",
            vad_threshold=0.6,
        )
        assert t.model_size == "medium"
        assert t.fast_model_size == "small"
        assert t.device == "cpu"
        assert t.compute_type == "int8"
        assert t.vad_threshold == 0.6

    def test_load_model_stub_fallback(self):
        """Without faster-whisper installed, load_model returns False."""
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        result = asyncio.get_event_loop().run_until_complete(t.load_model())
        assert result is False
        assert t.model is None
        assert t.is_real is False

    def test_stub_transcribe(self):
        """Stub transcribe returns 2 placeholder segments."""
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        result = asyncio.get_event_loop().run_until_complete(
            t.transcribe("/tmp/test.wav")
        )
        assert len(result) == 2
        assert "[Stub]" in result[0]["text"]
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 5.0
        assert result[0]["confidence"] == 0.0
        assert result[0]["language"] == "en"
        assert isinstance(result[0]["words"], list)

    def test_transcription_result_exposes_transcript_text(self):
        from nexus_sdk.media.models import TranscriptionResult, TranscriptionSegment

        result = TranscriptionResult(
            job_id="job-1",
            session_id="session-1",
            segments=[
                TranscriptionSegment(
                    speaker="SPEAKER_00",
                    text="Hello world",
                    start_time=0.0,
                    end_time=1.0,
                ),
                TranscriptionSegment(
                    speaker="SPEAKER_01",
                    text="How are you",
                    start_time=1.5,
                    end_time=2.5,
                ),
            ],
        )

        result.compute_stats()

        assert result.transcript_text == "Hello world How are you"

    def test_stub_increments_fallback_count(self):
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        asyncio.get_event_loop().run_until_complete(t.transcribe("/tmp/a.wav"))
        asyncio.get_event_loop().run_until_complete(t.transcribe("/tmp/b.wav"))
        assert t._stub_fallback_count == 2

    def test_set_vocabulary(self):
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        custom = ["premium", "deductible", "coinsurance"]
        t.set_vocabulary(custom)
        assert t._vocabulary_terms == custom

    def test_build_vocabulary_prompt(self):
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        t.set_vocabulary(["alpha", "beta", "gamma"])
        prompt = t._build_vocabulary_prompt()
        assert prompt.startswith("Domain terminology:")
        assert "alpha" in prompt
        assert "beta" in prompt

    def test_build_vocabulary_prompt_truncates(self):
        """Vocabulary prompt limited to 50 terms."""
        from app.transcription import WhisperTranscriber
        t = WhisperTranscriber()
        long_vocab = [f"term_{i}" for i in range(200)]
        t.set_vocabulary(long_vocab)
        prompt = t._build_vocabulary_prompt()
        # Should only include first 50
        assert "term_49" in prompt
        assert "term_50" not in prompt


class TestInsuranceVocabulary:
    """Test that insurance-specific vocabulary list exists and is populated."""

    def test_vocabulary_not_empty(self):
        from app.transcription import INSURANCE_VOCABULARY
        assert len(INSURANCE_VOCABULARY) > 10

    def test_contains_key_terms(self):
        from app.transcription import INSURANCE_VOCABULARY
        vocab_lower = [v.lower() for v in INSURANCE_VOCABULARY]
        for term in ["premium", "beneficiary", "underwriting", "deductible"]:
            assert any(term in v for v in vocab_lower), f"Missing: {term}"

    def test_vocab_in_default_transcriber(self):
        from app.transcription import WhisperTranscriber, INSURANCE_VOCABULARY
        t = WhisperTranscriber()
        assert t._vocabulary_terms is INSURANCE_VOCABULARY


# ─── Speaker Diarizer ─────────────────────────────────────────


class TestSpeakerDiarizer:
    """Test SpeakerDiarizer class in stub mode (no GPU)."""

    def test_init_defaults(self):
        from app.diarization import SpeakerDiarizer
        d = SpeakerDiarizer()
        assert d.min_speakers == 1
        assert d.max_speakers == 10
        assert d.pipeline is None
        assert d.is_real is False

    def test_load_model_stub_fallback(self):
        from app.diarization import SpeakerDiarizer
        d = SpeakerDiarizer()
        result = asyncio.get_event_loop().run_until_complete(d.load_model())
        assert result is False
        assert d.is_real is False

    def test_stub_diarize(self):
        """Stub diarize returns 2 speaker segments."""
        from app.diarization import SpeakerDiarizer
        d = SpeakerDiarizer()
        result = asyncio.get_event_loop().run_until_complete(
            d.diarize("/tmp/test.wav")
        )
        assert len(result) == 2
        assert result[0]["speaker"] == "SPEAKER_00"
        assert result[1]["speaker"] == "SPEAKER_01"

    def test_stub_increments_fallback_count(self):
        from app.diarization import SpeakerDiarizer
        d = SpeakerDiarizer()
        asyncio.get_event_loop().run_until_complete(d.diarize("/tmp/a.wav"))
        asyncio.get_event_loop().run_until_complete(d.diarize("/tmp/b.wav"))
        assert d._stub_fallback_count == 2

    def test_manifest_verification_failure_updates_startup_reason(self, tmp_path):
        from app.diarization import SpeakerDiarizer

        d = SpeakerDiarizer(model_path=str(tmp_path / "missing"), verify_manifest=True)
        result = asyncio.get_event_loop().run_until_complete(d.load_model())

        assert result is False
        assert "bundle directory missing" in d.startup_reason

    def test_diarize_preloads_waveform_for_pipeline(self, monkeypatch, tmp_path):
        from app.diarization import SpeakerDiarizer

        audio_path = tmp_path / "sample.wav"
        audio_path.write_bytes(b"fake")

        captured = {}

        class FakeTurn:
            def __init__(self, start, end):
                self.start = start
                self.end = end

        class FakeDiarization:
            def itertracks(self, yield_label=False):
                yield FakeTurn(0.0, 1.25), None, "SPEAKER_00"

        class FakePipeline:
            def __call__(self, payload, **kwargs):
                captured["payload"] = payload
                captured["kwargs"] = kwargs
                return FakeDiarization()

        class FakeWaveform:
            """Mimics a numpy 2-D array with a .T transpose attribute."""
            def __init__(self, data):
                self._data = data
            @property
            def T(self):
                return self._data

        fake_soundfile = types.SimpleNamespace(
            read=lambda path, always_2d=True, dtype="float32": (FakeWaveform([[0.1], [0.2], [0.3]]), 16000)
        )

        class FakeTensor:
            def __init__(self, values):
                self.values = values

        class FakeTorch:
            @staticmethod
            def from_numpy(values):
                return FakeTensor(values)

        monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
        monkeypatch.setitem(sys.modules, "torch", FakeTorch)

        diarizer = SpeakerDiarizer(device="cpu")
        diarizer.pipeline = FakePipeline()

        segments = diarizer._diarize_sync(str(audio_path), num_speakers=2)

        assert segments == [{"speaker": "SPEAKER_00", "start": 0.0, "end": 1.25}]
        assert captured["kwargs"] == {"num_speakers": 2}
        assert captured["payload"]["sample_rate"] == 16000
        assert captured["payload"]["uri"] == "sample"
        assert isinstance(captured["payload"]["waveform"], FakeTensor)

    def test_diarize_falls_back_to_path_when_waveform_preload_fails(self, monkeypatch):
        from app.diarization import SpeakerDiarizer

        captured = {}

        class FakeDiarization:
            def itertracks(self, yield_label=False):
                return iter(())

        class FakePipeline:
            def __call__(self, payload, **kwargs):
                captured["payload"] = payload
                captured["kwargs"] = kwargs
                return FakeDiarization()

        def broken_read(*args, **kwargs):
            raise RuntimeError("boom")

        fake_soundfile = types.SimpleNamespace(read=broken_read)
        monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(from_numpy=lambda values: values))

        diarizer = SpeakerDiarizer(device="cpu")
        diarizer.pipeline = FakePipeline()

        segments = diarizer._diarize_sync("/tmp/example.wav", num_speakers=None)

        assert segments == []
        assert captured["payload"] == "/tmp/example.wav"
        assert captured["kwargs"] == {"min_speakers": 1, "max_speakers": 10}

    def test_diarize_accepts_wrapped_pyannote_output(self, monkeypatch, tmp_path):
        from app.diarization import SpeakerDiarizer

        audio_path = tmp_path / "sample.wav"
        audio_path.write_bytes(b"fake")

        class FakeTurn:
            def __init__(self, start, end):
                self.start = start
                self.end = end

        class FakeAnnotation:
            def itertracks(self, yield_label=False):
                yield FakeTurn(1.0, 2.5), None, "SPEAKER_01"

        class FakeDiarizeOutput:
            def __init__(self):
                self.speaker_diarization = FakeAnnotation()

        class FakePipeline:
            def __call__(self, payload, **kwargs):
                return FakeDiarizeOutput()

        class FakeArray:
            @property
            def T(self):
                return self

        fake_soundfile = types.SimpleNamespace(
            read=lambda path, always_2d=True, dtype="float32": (FakeArray(), 16000)
        )
        monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
        monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(from_numpy=lambda values: values))

        diarizer = SpeakerDiarizer(device="cpu")
        diarizer.pipeline = FakePipeline()

        segments = diarizer._diarize_sync(str(audio_path), num_speakers=None)

        assert segments == [{"speaker": "SPEAKER_01", "start": 1.0, "end": 2.5}]


class TestPyannoteBundle:
    def _create_bundle(self, tmp_path, *, segmentation_value="deps/pyannote--segmentation-3.0"):
        from app.diarization.bundle import build_manifest, write_manifest

        bundle_dir = tmp_path / "pyannote-speaker-3.1"
        deps_dir = bundle_dir / "deps"
        segmentation_dir = deps_dir / "pyannote--segmentation-3.0"
        embedding_dir = deps_dir / "pyannote--wespeaker-voxceleb-resnet34-LM"
        segmentation_dir.mkdir(parents=True)
        embedding_dir.mkdir(parents=True)

        (bundle_dir / "config.yaml").write_text(
            "\n".join(
                [
                    "version: 3.1.0",
                    "pipeline:",
                    "  params:",
                    f"    embedding: deps/pyannote--wespeaker-voxceleb-resnet34-LM",
                    f"    segmentation: {segmentation_value}",
                    "params:",
                    "  segmentation:",
                    "    min_duration_off: 0.0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "README.md").write_text("bundle", encoding="utf-8")
        (segmentation_dir / "config.yaml").write_text("name: segmentation\n", encoding="utf-8")
        (embedding_dir / "config.yaml").write_text("name: embedding\n", encoding="utf-8")

        manifest = build_manifest(bundle_dir, metadata={"source": "test"})
        write_manifest(bundle_dir, manifest)
        return bundle_dir

    def test_verify_bundle_success(self, tmp_path):
        from app.diarization.bundle import verify_bundle

        bundle_dir = self._create_bundle(tmp_path)
        ok, reason = verify_bundle(bundle_dir)

        assert ok is True
        assert reason == "ok"

    def test_verify_bundle_detects_checksum_mismatch(self, tmp_path):
        from app.diarization.bundle import verify_bundle

        bundle_dir = self._create_bundle(tmp_path)
        (bundle_dir / "README.md").write_text("tampered", encoding="utf-8")

        ok, reason = verify_bundle(bundle_dir)

        assert ok is False
        assert "bundle checksum mismatch" in reason

    def test_verify_bundle_rejects_external_refs(self, tmp_path):
        from app.diarization.bundle import verify_bundle

        bundle_dir = self._create_bundle(tmp_path, segmentation_value="pyannote/segmentation-3.0")
        ok, reason = verify_bundle(bundle_dir)

        assert ok is False
        assert "external model" in reason

    def test_prepare_runtime_bundle_rewrites_local_refs_to_absolute_paths(self, tmp_path):
        from app.diarization.bundle import prepare_runtime_bundle

        bundle_dir = self._create_bundle(tmp_path)
        (bundle_dir / "handler.py").write_text("raise RuntimeError('remote')\n", encoding="utf-8")

        runtime_root, temp_root = prepare_runtime_bundle(bundle_dir)
        try:
            config_text = (runtime_root / "config.yaml").read_text(encoding="utf-8")
            assert str((bundle_dir / "deps" / "pyannote--segmentation-3.0").resolve()).replace("\\", "/") in config_text.replace("\\", "/")
            assert str((bundle_dir / "deps" / "pyannote--wespeaker-voxceleb-resnet34-LM").resolve()).replace("\\", "/") in config_text.replace("\\", "/")
            assert not (runtime_root / "handler.py").exists()
        finally:
            if temp_root is not None:
                import shutil

                shutil.rmtree(temp_root, ignore_errors=True)

    def test_copy_compatibility_assets_mirrors_plda_directory(self, tmp_path, monkeypatch):
        from app.diarization.bundle import copy_compatibility_assets

        compatibility_repo = "pyannote/speaker-diarization-community-1"
        compatibility_root = tmp_path / "fixtures" / "community-1"
        (compatibility_root / "plda").mkdir(parents=True)
        (compatibility_root / "plda" / "xvec_transform.npz").write_text("compat", encoding="utf-8")

        def fake_snapshot_download(*, repo_id, token, local_dir, local_dir_use_symlinks=False, allow_patterns=None):
            assert token == "token"
            target = Path(local_dir)
            target.mkdir(parents=True, exist_ok=True)
            if repo_id == compatibility_repo:
                assert allow_patterns is not None
                assert "plda/**" in allow_patterns
                for child in compatibility_root.iterdir():
                    if child.is_dir():
                        import shutil

                        shutil.copytree(child, target / child.name)
            return str(target)

        fake_hf_module = types.SimpleNamespace(snapshot_download=fake_snapshot_download)
        monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf_module)

        bundle_root = tmp_path / "bundle"
        deps_root = bundle_root / "deps"
        deps_root.mkdir(parents=True)

        mirrored = copy_compatibility_assets(
            bundle_root,
            deps_root,
            repo_id="pyannote/speaker-diarization-3.1",
            hf_token="token",
        )

        assert mirrored == [compatibility_repo]
        assert (bundle_root / "plda" / "xvec_transform.npz").exists()


# ─── Align Segments ───────────────────────────────────────────


class TestAlignSegments:
    """Test whisper + diarization alignment logic (via modular import)."""

    def test_basic_alignment(self):
        from app.diarization import align_segments
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
        assert result[0].speaker == "SPEAKER_00"
        assert result[1].speaker == "SPEAKER_01"

    def test_empty_whisper(self):
        from app.diarization import align_segments
        result = align_segments([], [{"speaker": "SPK", "start": 0, "end": 5}])
        assert result == []

    def test_empty_speakers_gives_unknown(self):
        from app.diarization import align_segments
        whisper = [{"text": "hi", "start": 0, "end": 1, "confidence": 0.9, "language": "en"}]
        result = align_segments(whisper, [])
        assert len(result) == 1
        assert result[0].speaker == "UNKNOWN"

    def test_preserves_confidence(self):
        from app.diarization import align_segments
        whisper = [{"text": "test", "start": 0, "end": 1, "confidence": 0.42, "language": "en"}]
        speakers = [{"speaker": "SPK", "start": 0, "end": 2}]
        result = align_segments(whisper, speakers)
        assert result[0].confidence == 0.42

    def test_preserves_language(self):
        from app.diarization import align_segments
        whisper = [{"text": "bonjour", "start": 0, "end": 1, "confidence": 0.8, "language": "fr"}]
        speakers = [{"speaker": "SPK", "start": 0, "end": 2}]
        result = align_segments(whisper, speakers)
        assert result[0].language == "fr"

    def test_forwards_word_data(self):
        from app.diarization import align_segments
        words = [
            {"word": "hello", "start": 0.0, "end": 0.5, "probability": 0.99},
            {"word": "world", "start": 0.5, "end": 1.0, "probability": 0.97},
        ]
        whisper = [{"text": "hello world", "start": 0, "end": 1, "confidence": 0.9, "language": "en", "words": words}]
        speakers = [{"speaker": "SPK", "start": 0, "end": 2}]
        result = align_segments(whisper, speakers)
        assert len(result[0].words) == 2
        assert result[0].words[0]["word"] == "hello"

    def test_returns_sdk_transcription_segments(self):
        """Aligned results are SDK TranscriptionSegment instances."""
        from app.diarization import align_segments
        from nexus_sdk.media.models import TranscriptionSegment
        whisper = [{"text": "test", "start": 0, "end": 1, "confidence": 0.5, "language": "en"}]
        speakers = [{"speaker": "SPK", "start": 0, "end": 2}]
        result = align_segments(whisper, speakers)
        assert isinstance(result[0], TranscriptionSegment)
        assert result[0].segment_id  # has auto-generated UUID

    def test_overlap_based_speaker_assignment(self):
        """Speaker with most overlap wins."""
        from app.diarization import align_segments
        whisper = [{"text": "crossover", "start": 2.0, "end": 4.0, "confidence": 0.9, "language": "en"}]
        speakers = [
            {"speaker": "A", "start": 0.0, "end": 2.5},   # overlap = 0.5
            {"speaker": "B", "start": 2.5, "end": 5.0},   # overlap = 1.5
        ]
        result = align_segments(whisper, speakers)
        assert result[0].speaker == "B"


# ─── Preprocessor Re-exports ──────────────────────────────────


class TestPreprocessorPackage:
    """Validate preprocessor sub-package exports SDK components."""

    def test_imports(self):
        from app.preprocessor import (
            AudioPreprocessor,
            PreprocessConfig,
            PreprocessResult,
            probe_audio,
        )
        assert AudioPreprocessor is not None
        assert PreprocessConfig is not None

    def test_preprocess_config_defaults(self):
        from app.preprocessor import PreprocessConfig
        cfg = PreprocessConfig()
        assert cfg.target_sample_rate == 16000
        assert cfg.target_channels == 1
        assert cfg.normalize_audio is True

    def test_audio_preprocessor_creation(self):
        from app.preprocessor import AudioPreprocessor, PreprocessConfig
        cfg = PreprocessConfig()
        proc = AudioPreprocessor(cfg)
        assert proc.config is cfg


# ─── Main Module Re-exports ───────────────────────────────────


class TestMainModuleReexports:
    """Verify main.py re-exports for backward compatibility."""

    def test_transcription_segment(self):
        from main import TranscriptionSegment
        seg = TranscriptionSegment(
            speaker="SPK", text="test", start_time=0, end_time=1
        )
        assert seg.speaker == "SPK"

    def test_align_segments(self):
        from main import align_segments
        assert callable(align_segments)

    def test_insurance_vocabulary(self):
        from main import INSURANCE_VOCABULARY
        assert isinstance(INSURANCE_VOCABULARY, list)
        assert len(INSURANCE_VOCABULARY) > 10
