"""Tests for the pipeline-run provenance recorder."""
from __future__ import annotations

import pytest

from nexus_sdk.provenance import (
    PipelineRunProvenance,
    capture_run_provenance,
    diff_provenance,
)


def test_capture_returns_serialisable_record(monkeypatch):
    monkeypatch.setenv("NEXUS_ENV", "dev")
    prov = capture_run_provenance(
        artifact_id="art-1",
        chain_id="nexus.canonical-processing",
        chain_version="2",
        processing_profile="multimodal",
        engine_versions={"eyes": "0.2.0", "ears": "0.1.5"},
        model_resolved={"eyes": "claude-sonnet-4-6", "ears": "whisper-large-v3"},
        model_providers={"eyes": "anthropic", "ears": "openai"},
        feature_flags={"EYES_PER_FRAME_LLAVA": True},
    )
    d = prov.to_dict()
    assert d["artifact_id"] == "art-1"
    assert d["chain_id"] == "nexus.canonical-processing"
    assert d["chain_version"] == "2"
    assert d["processing_profile"] == "multimodal"
    assert d["deployment_env"] == "dev"
    assert d["engine_versions"] == {"eyes": "0.2.0", "ears": "0.1.5"}
    assert d["model_resolved"]["eyes"] == "claude-sonnet-4-6"
    assert d["feature_flags"]["EYES_PER_FRAME_LLAVA"] is True
    assert d["captured_at"]
    assert "python" in d["runtime"]


def test_capture_picks_up_explicit_git_commit(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abcdef123456789012")
    prov = capture_run_provenance(
        artifact_id="a", chain_id="c",
    )
    # Truncated to 12 chars
    assert prov.git_commit == "abcdef123456"


def test_capture_picks_up_ci_commit_sha(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("NEXUS_GIT_COMMIT", raising=False)
    monkeypatch.setenv("CI_COMMIT_SHA", "deadbeefcafebabe1234")
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    assert prov.git_commit == "deadbeefcafe"


def test_capture_redacts_secrets_in_tier_env(monkeypatch):
    monkeypatch.setenv("EYES_VISION_TIER1_API_KEY", "sk-ant-real-secret-here")
    monkeypatch.setenv("EYES_VISION_TIER1_PROVIDER", "anthropic")
    monkeypatch.setenv("EYES_VISION_TIER1_MODEL", "claude-sonnet-4-6")
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    fp = prov.env_fingerprint
    assert fp["EYES_VISION_TIER1_PROVIDER"] == "anthropic"
    assert fp["EYES_VISION_TIER1_MODEL"] == "claude-sonnet-4-6"
    # API key value never leaves the boundary.
    assert fp["EYES_VISION_TIER1_API_KEY"].startswith("<set:")
    assert "sk-ant" not in fp["EYES_VISION_TIER1_API_KEY"]


def test_capture_redacts_empty_secret_distinctly(monkeypatch):
    monkeypatch.setenv("EYES_VISION_TIER2_API_KEY", "")
    monkeypatch.setenv("EYES_VISION_TIER2_PROVIDER", "openai")
    monkeypatch.setenv("EYES_VISION_TIER2_MODEL", "gpt-4o")
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    assert prov.env_fingerprint["EYES_VISION_TIER2_API_KEY"] == "<empty>"


def test_capture_pulls_tracked_feature_flags_from_env(monkeypatch):
    monkeypatch.setenv("EYES_PER_FRAME_LLAVA", "true")
    monkeypatch.setenv("EYES_PER_FRAME_OCR", "false")
    monkeypatch.setenv("EYES_TRANSITION_LLM", "1")
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    assert prov.feature_flags["EYES_PER_FRAME_LLAVA"] is True
    assert prov.feature_flags["EYES_PER_FRAME_OCR"] is False
    assert prov.feature_flags["EYES_TRANSITION_LLM"] is True


def test_capture_caller_supplied_flags_override_env(monkeypatch):
    monkeypatch.setenv("EYES_PER_FRAME_LLAVA", "false")
    prov = capture_run_provenance(
        artifact_id="a", chain_id="c",
        feature_flags={"EYES_PER_FRAME_LLAVA": True},
    )
    assert prov.feature_flags["EYES_PER_FRAME_LLAVA"] is True


def test_capture_includes_runtime_python_version():
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    assert "python" in prov.runtime
    assert prov.runtime["python"]


def test_capture_includes_sdk_modules_section():
    prov = capture_run_provenance(artifact_id="a", chain_id="c")
    # SDK modules we know exist
    assert "nexus_sdk" in prov.sdk_modules
    # Sub-modules added in this work
    for expected in ("nexus_sdk.evidence", "nexus_sdk.audio", "nexus_sdk.cursor",
                     "nexus_sdk.dictionary", "nexus_sdk.provenance"):
        assert expected in prov.sdk_modules


# ─── diff_provenance ─────────────────────────────────────────────────────────

def test_diff_returns_empty_for_identical_dicts():
    a = {"x": 1, "nested": {"y": "z"}}
    assert diff_provenance(a, dict(a)) == {}


def test_diff_surfaces_top_level_changes():
    diffs = diff_provenance(
        {"chain_version": "1"}, {"chain_version": "2"},
    )
    assert diffs == {"chain_version": {"a": "1", "b": "2"}}


def test_diff_recurses_into_nested_maps():
    a = {"feature_flags": {"X": True, "Y": False}}
    b = {"feature_flags": {"X": False, "Y": False}}
    diffs = diff_provenance(a, b)
    assert "feature_flags.X" in diffs
    assert diffs["feature_flags.X"] == {"a": True, "b": False}
    assert "feature_flags.Y" not in diffs


def test_diff_handles_missing_keys_on_either_side():
    a = {"only_a": 1}
    b = {"only_b": 2}
    diffs = diff_provenance(a, b)
    assert diffs == {
        "only_a": {"a": 1, "b": None},
        "only_b": {"a": None, "b": 2},
    }


def test_diff_handles_model_resolved_swap():
    """The motivating use case — surfacing a model swap across two runs."""
    a = capture_run_provenance(
        artifact_id="run-a",
        chain_id="c",
        model_resolved={"eyes": "claude-sonnet-4-5"},
        model_providers={"eyes": "anthropic"},
    ).to_dict()
    b = capture_run_provenance(
        artifact_id="run-b",
        chain_id="c",
        model_resolved={"eyes": "claude-sonnet-4-6"},
        model_providers={"eyes": "anthropic"},
    ).to_dict()
    # Strip volatile fields before diffing.
    for k in ("artifact_id", "captured_at"):
        a.pop(k, None)
        b.pop(k, None)
    diffs = diff_provenance(a, b)
    assert "model_resolved.eyes" in diffs
    assert diffs["model_resolved.eyes"]["a"] == "claude-sonnet-4-5"
    assert diffs["model_resolved.eyes"]["b"] == "claude-sonnet-4-6"


def test_to_dict_round_trips_for_equality_check():
    """Calling to_dict twice on identical input yields identical dicts."""
    p1 = capture_run_provenance(
        artifact_id="x", chain_id="c",
        engine_versions={"eyes": "0.1"}, model_resolved={"eyes": "m"},
    )
    p2 = PipelineRunProvenance(**{
        # Manually rebuild with the same fields.
        "artifact_id": p1.artifact_id, "chain_id": p1.chain_id,
        "chain_version": p1.chain_version, "pipeline_version": p1.pipeline_version,
        "sdk_version": p1.sdk_version, "git_commit": p1.git_commit,
        "deployment_env": p1.deployment_env,
        "engine_versions": dict(p1.engine_versions),
        "model_resolved": dict(p1.model_resolved),
        "model_providers": dict(p1.model_providers),
        "sdk_modules": dict(p1.sdk_modules),
        "processing_profile": p1.processing_profile,
        "feature_flags": dict(p1.feature_flags),
        "env_fingerprint": dict(p1.env_fingerprint),
        "runtime": dict(p1.runtime),
        "captured_at": p1.captured_at,
    })
    assert p1.to_dict() == p2.to_dict()
