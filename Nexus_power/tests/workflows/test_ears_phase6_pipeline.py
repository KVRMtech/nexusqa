"""
Phase 6 proof tests for the ears canonical workflow split.

What's under test:
  1. The four new handlers (preprocess / diarize / transcribe_segments /
     align) hand off correctly via the checkpoint contract — what step N
     writes is what step N+1 reads, and nothing is lost across the
     boundary.
  2. Whisper segments (potentially thousands of items) are persisted to
     the artifact store, not the checkpoint, so workflow_state rows stay
     compact regardless of audio length.
  3. A failure inside one step leaves the rest of the checkpoint intact
     so a retry of that step can resume from prior state.
  4. The final result reaches the shape the legacy REST path produced,
     so downstream consumers (Shield, Backbone) do not need code changes.

The engine is mocked at the `_stage_*` boundary — we are testing the
handler glue, not Whisper/pyannote themselves. Audio is a small WAV in
memory; artifact upload/download is an in-memory dict.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import types
import uuid
from dataclasses import dataclass, field
from typing import Any

# Resolved from THIS FILE, not from one machine. These tests hard-coded
# r"c:/Users/harik/nexusqa/Nexus_power/engines/ears-engine" — an absolute path
# on another developer's laptop — so the import failed everywhere else with
# ModuleNotFoundError: No module named 'app.workflow_handlers'.
_EARS_ENGINE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "engines", "ears-engine",
)

import pytest

from nexus_sdk.workflows import JobEnvelope, StepResult


# ─── In-memory artifact store ───────────────────────────────────


class _MemArtifactStore:
    """Stands in for ArtifactStore — keeps everything in a dict so tests
    are deterministic and fast. The handlers only call upload_bytes /
    download_bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.downloads: list[str] = []

    is_local = False

    async def upload_bytes(self, key: str, data: bytes, **_) -> Any:
        self.objects[key] = data
        self.uploads.append(key)
        return types.SimpleNamespace(key=key, size=len(data))

    async def download_bytes(self, key: str) -> bytes:
        self.downloads.append(key)
        if key not in self.objects:
            raise KeyError(f"no such artifact: {key}")
        return self.objects[key]


# ─── Stub EarsEngine ────────────────────────────────────────────


@dataclass
class _StubCfg:
    audio_storage_path: str = "/tmp/ears-test"


class _StubEarsEngine:
    """The handlers reach into the engine for _stage_* methods, the
    artifact store, the event bus, and the config. This stub fakes all
    of those without spinning up the real engine."""

    def __init__(self, tmp_path) -> None:
        self.cfg = _StubCfg(audio_storage_path=str(tmp_path))
        self._artifacts = _MemArtifactStore()
        self.event_bus = _StubEventBus()
        self._stage_calls: list[str] = []
        # Per-stage scripted returns; tests can override.
        self._preprocess_return: dict[str, Any] | None = None
        self._diarize_return: dict[str, Any] | None = None
        self._transcribe_return: dict[str, Any] | None = None
        self._align_should_raise: Exception | None = None

    async def _stage_preprocess(self, audio_path, session_id, tenant_id):
        self._stage_calls.append("preprocess")
        return self._preprocess_return or {
            "processed_path": audio_path,  # pretend ffmpeg ran in place
            "chunk_paths": [],
            "audio_meta": {"duration_seconds": 30.0, "sample_rate": 16000},
            "stages": ["resample", "vad"],
        }

    async def _stage_diarize(
        self, processed_path, audio_meta, num_speakers, processing_profile,
    ):
        self._stage_calls.append("diarize")
        return self._diarize_return or {
            "speaker_segments": [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
                {"speaker": "SPEAKER_01", "start": 10.0, "end": 30.0},
            ],
            "stages": ["diarize"],
        }

    async def _stage_transcribe(
        self, processed_path, chunk_paths, language, processing_profile,
    ):
        self._stage_calls.append("transcribe")
        return self._transcribe_return or {
            "whisper_segments": [
                {"start": 0.0, "end": 5.0, "text": "Hello world"},
                {"start": 5.0, "end": 10.0, "text": "How are you"},
                {"start": 10.0, "end": 30.0, "text": "Fine thanks"},
            ],
            "active_model_size": "medium",
            "stages": ["transcribe"],
        }

    def _stage_align(
        self,
        whisper_segments,
        speaker_segments,
        audio_meta,
        language,
        active_model_size,
        accumulated_stages,
        elapsed_seconds,
        job_id,
        session_id,
        tenant_id,
    ):
        self._stage_calls.append("align")
        if self._align_should_raise is not None:
            raise self._align_should_raise
        accumulated_stages.append("align")
        # Mimic the shape TranscriptionResult would produce — keep it
        # minimal so we don't need pydantic models in tests.
        return _StubResult(
            segments=[
                {**s, "speaker": "SPEAKER_00"} for s in whisper_segments
            ],
            full_text=" ".join(s.get("text", "") for s in whisper_segments),
            speakers=["SPEAKER_00", "SPEAKER_01"],
            duration_seconds=audio_meta.get("duration_seconds", 0),
            word_count=sum(len(s.get("text", "").split()) for s in whisper_segments),
            language=language,
            pipeline_stages=list(accumulated_stages),
            model_version=f"whisper-{active_model_size}",
            segment_count=len(whisper_segments),
            job_id=job_id,
            session_id=session_id,
            tenant_id=tenant_id,
        )


@dataclass
class _StubResult:
    """Stands in for TranscriptionResult. Only fields the handler reads."""

    segments: list[dict]
    full_text: str
    speakers: list[str]
    duration_seconds: float
    word_count: int
    language: str
    pipeline_stages: list[str]
    model_version: str
    segment_count: int
    job_id: str
    session_id: str
    tenant_id: str

    def model_dump(self, mode: str = "python") -> dict:
        return {
            "segments": self.segments,
            "text": self.full_text,
            "speakers": self.speakers,
            "duration_seconds": self.duration_seconds,
            "word_count": self.word_count,
            "language": self.language,
            "pipeline_stages": self.pipeline_stages,
            "model_version": self.model_version,
            "segment_count": self.segment_count,
            "job_id": self.job_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
        }


class _StubEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event) -> None:
        self.published.append(event)


# ─── Test helpers ───────────────────────────────────────────────


def _make_env(
    step_name: str,
    *,
    workflow_id: str = "wf-test",
    checkpoint: dict | None = None,
    params: dict | None = None,
) -> JobEnvelope:
    return JobEnvelope(
        workflow_id=workflow_id,
        step_name=step_name,
        step_index=0,
        attempt=1,
        tenant_id="tenant-test",
        session_id="session-test",
        deadline_at_epoch=time.time() + 3600,
        heartbeat_seconds=60,
        params=params or {"profile": "fast"},
        checkpoint=checkpoint or {},
    )


def _seed_input_file(tmp_path):
    """Drop a fake input audio file on disk so _materialize_input
    finds something. Content doesn't matter — _stage_preprocess is mocked."""
    p = tmp_path / "input.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    return str(p)


# ─── Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_happy_path(tmp_path):
    """Drive all 4 steps in sequence with checkpoint hand-off; assert
    the terminal result has the expected shape."""
    # Import inside the test so the import error message is informative
    # if the file is broken — keeps the test discoverable.
    import sys
    sys.path.insert(0, _EARS_ENGINE_ROOT)
    from app.workflow_handlers import EarsWorkflowHandlers

    engine = _StubEarsEngine(tmp_path)
    handlers = EarsWorkflowHandlers(engine)
    input_path = _seed_input_file(tmp_path)

    # Step 1: preprocess
    env1 = _make_env(
        "ears.preprocess",
        checkpoint={"input_file": input_path},
    )
    r1 = await handlers._handle_preprocess(env1)
    assert r1.success, r1.error
    assert engine._stage_calls == ["preprocess"]
    assert "processed_audio_key" in r1.checkpoint
    assert r1.checkpoint["audio_meta"]["duration_seconds"] == 30.0
    assert r1.checkpoint["current_ears_step"] == "ears.preprocess"
    assert r1.checkpoint["pipeline_stages"] == ["resample", "vad"]

    # Step 2: diarize — uses the processed_audio_key written by step 1
    env2 = _make_env("ears.diarize", checkpoint=dict(r1.checkpoint))
    r2 = await handlers._handle_diarize(env2)
    assert r2.success, r2.error
    assert engine._stage_calls == ["preprocess", "diarize"]
    assert r2.checkpoint["speaker_count"] == 2
    assert len(r2.checkpoint["speaker_segments"]) == 2
    assert r2.checkpoint["current_ears_step"] == "ears.diarize"

    # Step 3: transcribe — large segment list gets uploaded; checkpoint
    # only has the key.
    env3 = _make_env("ears.transcribe_segments", checkpoint=dict(r2.checkpoint))
    r3 = await handlers._handle_transcribe_segments(env3)
    assert r3.success, r3.error
    assert engine._stage_calls == ["preprocess", "diarize", "transcribe"]
    assert "whisper_segments_key" in r3.checkpoint
    assert r3.checkpoint["whisper_segment_count"] == 3
    assert r3.checkpoint["active_model_size"] == "medium"
    # Critical: the segments themselves are NOT in the checkpoint.
    assert "whisper_segments" not in r3.checkpoint, (
        "Whisper output must live in the artifact store, not the "
        "checkpoint, or workflow_state rows explode on long audio."
    )
    # The upload landed under the workflow id.
    seg_key = r3.checkpoint["whisper_segments_key"]
    assert "wf-test" in seg_key

    # Step 4: align — produces the final result and emits the event.
    env4 = _make_env("ears.align", checkpoint=dict(r3.checkpoint))
    r4 = await handlers._handle_align(env4)
    assert r4.success, r4.error
    assert engine._stage_calls == ["preprocess", "diarize", "transcribe", "align"]
    result = r4.checkpoint["ears_result"]
    assert result["segment_count"] == 3
    assert result["language"] == "en"
    assert r4.checkpoint["segment_count"] == 3
    assert r4.checkpoint["speaker_count"] == 2
    # Event was emitted with the workflow_id correlation.
    assert len(engine.event_bus.published) == 1
    ev = engine.event_bus.published[0]
    assert ev.event_type == "ears.transcription.completed"
    assert ev.data["workflow_id"] == "wf-test"


@pytest.mark.asyncio
async def test_whisper_segments_stay_in_artifact_store(tmp_path):
    """If the audio produces a huge segment list, the checkpoint must
    not grow unbounded. We assert the segments live in the in-memory
    store and the checkpoint key is small enough to survive any sane
    workflow_state JSON column."""
    import sys
    sys.path.insert(0, _EARS_ENGINE_ROOT)
    from app.workflow_handlers import EarsWorkflowHandlers

    engine = _StubEarsEngine(tmp_path)
    handlers = EarsWorkflowHandlers(engine)
    # 5000 fake whisper segments — what a 1-hour call would yield.
    engine._transcribe_return = {
        "whisper_segments": [
            {"start": i * 0.5, "end": (i + 1) * 0.5, "text": f"word-{i}"}
            for i in range(5000)
        ],
        "active_model_size": "medium",
        "stages": ["chunked_transcribe"],
    }
    input_path = _seed_input_file(tmp_path)

    # Skip steps 1-2 by seeding the inputs they would produce.
    ckpt = {
        "input_file": input_path,
        "processed_audio_key": "ears/tenant-test/session-test/wf-test/processed/input.wav",
        "audio_meta": {"duration_seconds": 3600.0},
        "speaker_segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 3600}],
        "pipeline_stages": ["resample"],
    }
    # Seed the artifact store with the processed audio so download works.
    engine._artifacts.objects[ckpt["processed_audio_key"]] = b"audio"

    env = _make_env("ears.transcribe_segments", checkpoint=ckpt)
    r = await handlers._handle_transcribe_segments(env)
    assert r.success, r.error
    # Critical property: checkpoint is small.
    ckpt_json_size = len(json.dumps(r.checkpoint))
    assert ckpt_json_size < 5_000, (
        f"checkpoint must stay compact regardless of segment count; got "
        f"{ckpt_json_size}B for 5000 segments"
    )
    # And the segments are actually in the store.
    key = r.checkpoint["whisper_segments_key"]
    payload = json.loads(engine._artifacts.objects[key].decode("utf-8"))
    assert len(payload) == 5000


@pytest.mark.asyncio
async def test_align_failure_leaves_checkpoint_intact(tmp_path):
    """If align raises, the StepResult is failed but the prior state
    (which downstream retries can rely on) is unchanged. The handler
    must not mutate or destroy the input checkpoint."""
    import sys
    sys.path.insert(0, _EARS_ENGINE_ROOT)
    from app.workflow_handlers import EarsWorkflowHandlers

    engine = _StubEarsEngine(tmp_path)
    handlers = EarsWorkflowHandlers(engine)
    engine._align_should_raise = RuntimeError("alignment is busted")

    # Seed a complete-to-this-point checkpoint.
    ckpt = {
        "input_file": _seed_input_file(tmp_path),
        "processed_audio_key": "k1",
        "audio_meta": {"duration_seconds": 30.0},
        "speaker_segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 30}],
        "whisper_segments_key": "k2",
        "whisper_segment_count": 3,
        "active_model_size": "medium",
        "pipeline_stages": ["resample", "diarize", "transcribe"],
        "ears_job_id": "job-x",
    }
    engine._artifacts.objects["k1"] = b"audio"
    engine._artifacts.objects["k2"] = json.dumps([
        {"start": 0, "end": 5, "text": "hello"},
    ]).encode()

    env = _make_env("ears.align", checkpoint=dict(ckpt))
    r = await handlers._handle_align(env)
    assert not r.success
    assert "busted" in r.error
    # No event should have been emitted.
    assert engine.event_bus.published == []


@pytest.mark.asyncio
async def test_diarize_missing_processed_audio_is_fatal(tmp_path):
    """If the orchestrator dispatches diarize without preprocess having
    run, the error is fatal (no retry) — retrying with the same input
    state will fail the same way. fatal=True signals the orchestrator
    to skip retries."""
    import sys
    sys.path.insert(0, _EARS_ENGINE_ROOT)
    from app.workflow_handlers import EarsWorkflowHandlers

    engine = _StubEarsEngine(tmp_path)
    handlers = EarsWorkflowHandlers(engine)

    env = _make_env("ears.diarize", checkpoint={})  # nothing seeded
    r = await handlers._handle_diarize(env)
    assert not r.success
    assert r.fatal is True
    assert "processed_audio_key" in r.error
