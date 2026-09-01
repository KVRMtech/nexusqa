"""Tests for the canonical pipeline self-observability layer.

Focus on the pure-Python aggregation + drift detection.  Prometheus
emission is exercised separately by the engine integration tests.
"""
from __future__ import annotations

import pytest

from nexus_sdk.observability.canonical_metrics import (
    ArtifactQualitySummary,
    DriftAlert,
    RollingDriftDetector,
    build_quality_summary,
)


# ─── build_quality_summary ──────────────────────────────────────────────────

def _step(
    *, kind: str = "click_cta", conf: float = 0.7, agreement: float = 0.5,
    signals: list[str] | None = None,
) -> dict:
    return {
        "action_kind": kind,
        "confidence": conf,
        "agreement_score": agreement,
        "evidence_signals": signals or ["audio_intent", "cursor"],
    }


def _scene(*, quality: str = "strong") -> dict:
    return {
        "scene_state_summary": {"state_quality": quality},
        "scene_quality": quality,
    }


def test_summary_aggregates_average_confidence_and_agreement():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        evidence_steps=[
            _step(conf=0.6, agreement=0.5),
            _step(conf=0.8, agreement=0.75),
            _step(conf=0.7, agreement=0.5),
        ],
    )
    assert s.evidence_step_count == 3
    assert s.avg_confidence == pytest.approx(0.7, abs=0.01)
    assert s.avg_agreement_score == pytest.approx(0.5833, abs=0.01)


def test_summary_zero_steps_yields_zero_averages():
    s = build_quality_summary(
        artifact_id="a1", tenant_id="t", evidence_steps=[],
    )
    assert s.evidence_step_count == 0
    assert s.avg_confidence == 0.0
    assert s.avg_agreement_score == 0.0


def test_signal_coverage_calculated_from_evidence_signals():
    """Fraction of evidence_steps citing each signal."""
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        evidence_steps=[
            _step(signals=["audio_intent", "cursor"]),
            _step(signals=["cursor"]),
            _step(signals=["audio_intent", "cursor", "ocr_diff"]),
            _step(signals=["ocr_diff"]),
        ],
    )
    assert s.signal_coverage["cursor"] == 0.75
    assert s.signal_coverage["audio_intent"] == 0.5
    assert s.signal_coverage["ocr_diff"] == 0.5


def test_scene_quality_distribution_normalised():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        scenes=[
            _scene(quality="strong"),
            _scene(quality="strong"),
            _scene(quality="degraded"),
            _scene(quality="weak"),
        ],
    )
    assert s.scene_quality_distribution["strong"] == 0.5
    assert s.scene_quality_distribution["degraded"] == 0.25
    assert s.scene_quality_distribution["weak"] == 0.25


def test_unknown_quality_tier_treated_as_weak():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        scenes=[
            {"scene_quality": "MAYBE"},
            _scene(quality="strong"),
        ],
    )
    assert s.scene_quality_distribution["weak"] == 0.5
    assert s.scene_quality_distribution["strong"] == 0.5


def test_cursor_density_per_second():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        duration_seconds=60.0,
        cursor_events=[{}] * 30,
    )
    assert s.cursor_density == 0.5  # 30/60


def test_cursor_density_zero_when_no_duration():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        duration_seconds=0.0,
        cursor_events=[{}] * 10,
    )
    assert s.cursor_density == 0.0


def test_automation_ready_control_count():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        controls=[
            {"automation_ready": True},
            {"automation_ready": False},
            {"automation_ready": True},
        ],
    )
    assert s.automation_ready_control_count == 2


def test_stage_durations_propagated():
    s = build_quality_summary(
        artifact_id="a1",
        tenant_id="t",
        stage_durations_seconds={"audio_transcription": 12.4, "visual_extraction": 38.7},
    )
    assert s.stage_durations_seconds == {
        "audio_transcription": 12.4, "visual_extraction": 38.7,
    }


# ─── RollingDriftDetector ───────────────────────────────────────────────────

def test_drift_detector_returns_none_without_baseline():
    """Below min_baseline_samples → no alerts."""
    d = RollingDriftDetector(window_size=2, baseline_size=10, min_baseline_samples=5)
    for v in [0.7, 0.7, 0.7]:
        assert d.observe(tenant_id="t", metric="conf", value=v) is None


def test_drift_detector_warning_on_moderate_drop():
    d = RollingDriftDetector(
        window_size=3, baseline_size=20, warning_ratio=0.85,
        critical_ratio=0.65, min_baseline_samples=8,
    )
    # Build a stable baseline at 0.8.
    for _ in range(15):
        d.observe(tenant_id="t", metric="conf", value=0.8)
    # Recent window drops to ~0.65 (about 0.81 of 0.8 baseline → warning).
    alert = None
    for v in [0.66, 0.66, 0.66]:
        alert = d.observe(tenant_id="t", metric="conf", value=v) or alert
    assert alert is not None
    assert alert.severity in {"warning", "critical"}
    assert alert.metric == "conf"
    assert alert.tenant_id == "t"


def test_drift_detector_critical_on_severe_drop():
    d = RollingDriftDetector(
        window_size=3, baseline_size=20, warning_ratio=0.85,
        critical_ratio=0.55, min_baseline_samples=8,
    )
    for _ in range(15):
        d.observe(tenant_id="t", metric="conf", value=0.8)
    alert = None
    for v in [0.30, 0.30, 0.30]:
        alert = d.observe(tenant_id="t", metric="conf", value=v) or alert
    assert alert is not None
    assert alert.severity == "critical"


def test_drift_detector_no_alert_when_stable():
    d = RollingDriftDetector(window_size=3, baseline_size=20, min_baseline_samples=8)
    alerts = []
    for _ in range(30):
        a = d.observe(tenant_id="t", metric="conf", value=0.75)
        if a:
            alerts.append(a)
    assert alerts == []


def test_drift_detector_per_metric_per_tenant_isolation():
    """Same tenant, different metric → separate windows."""
    d = RollingDriftDetector(window_size=3, baseline_size=20, min_baseline_samples=8)
    for _ in range(15):
        d.observe(tenant_id="t", metric="conf", value=0.8)
        d.observe(tenant_id="t", metric="agreement", value=0.5)
    # Drop only conf, agreement stays steady.
    a_conf = None
    a_agreement = None
    for _ in range(3):
        a_conf = d.observe(tenant_id="t", metric="conf", value=0.30) or a_conf
        a_agreement = d.observe(tenant_id="t", metric="agreement", value=0.5) or a_agreement
    assert a_conf is not None
    assert a_agreement is None


def test_drift_detector_rejects_invalid_config():
    with pytest.raises(ValueError):
        RollingDriftDetector(window_size=1, baseline_size=10)
    with pytest.raises(ValueError):
        RollingDriftDetector(window_size=10, baseline_size=5)
    with pytest.raises(ValueError):
        RollingDriftDetector(warning_ratio=0.5, critical_ratio=0.6)


def test_drift_detector_handles_nan_and_string_safely():
    d = RollingDriftDetector(window_size=3, baseline_size=10, min_baseline_samples=5)
    assert d.observe(tenant_id="t", metric="m", value=float("nan")) is None
    assert d.observe(tenant_id="t", metric="m", value=float("inf")) is None
    assert d.observe(tenant_id="t", metric="m", value="not-a-number") is None  # type: ignore[arg-type]


# ─── ArtifactQualitySummary status field ────────────────────────────────────

def test_summary_default_status_completed():
    s = build_quality_summary(artifact_id="a", tenant_id="t")
    assert s.status == "completed"


def test_summary_failed_status_propagates():
    s = build_quality_summary(artifact_id="a", tenant_id="t", status="failed")
    assert s.status == "failed"
