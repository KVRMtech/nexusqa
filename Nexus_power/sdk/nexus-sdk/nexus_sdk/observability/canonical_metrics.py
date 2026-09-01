"""Canonical-pipeline self-observability — per-tenant quality metrics.

Wraps :class:`nexus_sdk.observability.metrics.NexusMetrics` with a
domain-specific facade that the canonical processing chain emits to
after every artifact completes.  The metric names + label dimensions
are stable so dashboards and alerts can be written once and stay
correct as the pipeline evolves.

Two layers:

  1. :class:`CanonicalQualityRecorder` — service-side helper that
     accepts a structured :class:`ArtifactQualitySummary` and emits
     the appropriate Prometheus counters / gauges / histograms.

  2. :class:`RollingDriftDetector` — pure-Python in-memory tracker for
     rolling-mean drift alerting.  Spine calls
     ``observe(tenant_id, metric_name, value)`` per artifact; the
     detector reports when the rolling mean has dropped below a
     configurable fraction of its long-term baseline.  No external
     deps — works in any container.

Why both?  Prometheus tracks long-horizon trends with sample-rate
preserved; the rolling drift detector gives sub-minute alerts when
recent artifacts regress sharply (e.g. a model swap dropped average
agreement_score from 0.7 to 0.3 — Prometheus might not page until the
window aggregates, but the in-memory detector fires immediately).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ─── Public dataclasses ──────────────────────────────────────────────────────

@dataclass
class ArtifactQualitySummary:
    """One artifact's per-stage quality metrics, emitted at completion.

    Field semantics:

      * ``avg_confidence`` / ``avg_agreement_score`` — mean across all
        evidence_steps for the artifact.  Hovering near 0 indicates the
        triangulator is starved of corroborating signals.
      * ``signal_coverage`` — per-signal fraction of evidence_steps that
        cited the signal in evidence_signals.  ``audio_intent`` near 0
        with a video that has audio means the audio pipeline degraded.
      * ``cursor_density`` — cursor_events per visible second of the
        recording.  A healthy capture is in [0.3, 2.0]; under 0.1 means
        the cursor tracker found nothing.
      * ``scene_quality_distribution`` — fraction of scenes at each
        quality tier (strong / degraded / weak).  ``weak`` over 0.5 is
        a strong signal that the visual extractor is starved.
    """

    artifact_id: str
    tenant_id: str
    session_id: str = ""
    duration_seconds: float = 0.0

    scene_count: int = 0
    frame_count: int = 0
    evidence_step_count: int = 0
    cursor_event_count: int = 0
    audio_intent_count: int = 0
    automation_ready_control_count: int = 0

    avg_confidence: float = 0.0
    avg_agreement_score: float = 0.0
    cursor_density: float = 0.0

    # Per-signal coverage:  {"audio_intent": 0.45, "cursor": 0.62, ...}
    signal_coverage: dict[str, float] = field(default_factory=dict)
    # Scene-quality distribution: {"strong": 0.6, "degraded": 0.3, "weak": 0.1}
    scene_quality_distribution: dict[str, float] = field(default_factory=dict)
    # Per-stage wall-clock durations.
    stage_durations_seconds: dict[str, float] = field(default_factory=dict)

    # status: "completed" | "failed" | "partial"
    status: str = "completed"


# ─── Prometheus emitter ──────────────────────────────────────────────────────

_METRIC_PREFIX = "nexus_canonical"


class CanonicalQualityRecorder:
    """Emits canonical-pipeline metrics through the shared NexusMetrics.

    Construct lazily inside the spine engine startup hook, after
    :func:`init_metrics` has been called.  ``record(summary)`` is the
    single entry point — it takes a fully-built
    :class:`ArtifactQualitySummary` and updates every relevant metric
    in one call.  All labels include ``tenant_id`` so per-tenant
    dashboards just filter on a single dimension.

    Failures (Prometheus client missing, registry not initialised) are
    swallowed — observability must never crash the data path.
    """

    def __init__(self) -> None:
        self._counter_artifacts = None
        self._gauge_evidence_step_count = None
        self._gauge_cursor_event_count = None
        self._gauge_audio_intent_count = None
        self._gauge_automation_ready = None
        self._gauge_avg_confidence = None
        self._gauge_avg_agreement = None
        self._gauge_cursor_density = None
        self._gauge_signal_coverage = None
        self._gauge_scene_quality = None
        self._histogram_stage_duration = None
        self._initialized = False

    def _ensure_metrics(self) -> bool:
        if self._initialized:
            return True
        try:
            from .metrics import get_metrics
        except Exception:  # pragma: no cover — defensive
            return False
        m = get_metrics()
        if m is None:
            return False
        try:
            self._counter_artifacts = m.custom_counter(
                f"{_METRIC_PREFIX}_artifacts_total",
                "Number of canonical artifacts processed",
                labels=["tenant_id", "status"],
            )
            self._gauge_evidence_step_count = m.custom_gauge(
                f"{_METRIC_PREFIX}_evidence_step_count",
                "Evidence steps emitted per artifact",
                labels=["tenant_id"],
            )
            self._gauge_cursor_event_count = m.custom_gauge(
                f"{_METRIC_PREFIX}_cursor_event_count",
                "Cursor events emitted per artifact",
                labels=["tenant_id"],
            )
            self._gauge_audio_intent_count = m.custom_gauge(
                f"{_METRIC_PREFIX}_audio_intent_count",
                "Audio intents extracted per artifact",
                labels=["tenant_id"],
            )
            self._gauge_automation_ready = m.custom_gauge(
                f"{_METRIC_PREFIX}_automation_ready_controls",
                "Automation-ready controls per artifact",
                labels=["tenant_id"],
            )
            self._gauge_avg_confidence = m.custom_gauge(
                f"{_METRIC_PREFIX}_evidence_step_avg_confidence",
                "Average per-step confidence (0..1)",
                labels=["tenant_id"],
            )
            self._gauge_avg_agreement = m.custom_gauge(
                f"{_METRIC_PREFIX}_evidence_step_avg_agreement",
                "Average per-step agreement_score (0..1)",
                labels=["tenant_id"],
            )
            self._gauge_cursor_density = m.custom_gauge(
                f"{_METRIC_PREFIX}_cursor_density_per_second",
                "Cursor events per second of recording duration",
                labels=["tenant_id"],
            )
            self._gauge_signal_coverage = m.custom_gauge(
                f"{_METRIC_PREFIX}_signal_coverage_fraction",
                "Fraction of evidence_steps citing each signal",
                labels=["tenant_id", "signal"],
            )
            self._gauge_scene_quality = m.custom_gauge(
                f"{_METRIC_PREFIX}_scene_quality_fraction",
                "Fraction of scenes at each quality tier",
                labels=["tenant_id", "tier"],
            )
            self._histogram_stage_duration = m.custom_histogram(
                f"{_METRIC_PREFIX}_pipeline_stage_seconds",
                "Per-stage canonical pipeline wall-clock duration",
                labels=["tenant_id", "stage"],
                buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
            )
            self._initialized = True
            return True
        except Exception:  # pragma: no cover — defensive
            return False

    def record(self, summary: ArtifactQualitySummary) -> None:
        """Emit Prometheus samples for one artifact's quality summary.

        Safe to call even when the metrics subsystem hasn't been
        initialised — the call is silently dropped rather than raising.
        """
        if not self._ensure_metrics():
            return
        try:
            t = summary.tenant_id or "unknown"
            self._counter_artifacts.labels(t, summary.status or "completed").inc()
            self._gauge_evidence_step_count.labels(t).set(float(summary.evidence_step_count))
            self._gauge_cursor_event_count.labels(t).set(float(summary.cursor_event_count))
            self._gauge_audio_intent_count.labels(t).set(float(summary.audio_intent_count))
            self._gauge_automation_ready.labels(t).set(float(summary.automation_ready_control_count))
            self._gauge_avg_confidence.labels(t).set(float(summary.avg_confidence))
            self._gauge_avg_agreement.labels(t).set(float(summary.avg_agreement_score))
            self._gauge_cursor_density.labels(t).set(float(summary.cursor_density))

            for signal_name, fraction in (summary.signal_coverage or {}).items():
                self._gauge_signal_coverage.labels(t, signal_name).set(float(fraction))
            for tier, fraction in (summary.scene_quality_distribution or {}).items():
                self._gauge_scene_quality.labels(t, tier).set(float(fraction))
            for stage, duration in (summary.stage_durations_seconds or {}).items():
                if duration is None:
                    continue
                self._histogram_stage_duration.labels(t, stage).observe(float(duration))
        except Exception:  # pragma: no cover — defensive
            return


# ─── Drift detector ──────────────────────────────────────────────────────────

@dataclass
class DriftAlert:
    """One drift alert — emitted when rolling mean falls below baseline."""

    metric: str
    tenant_id: str
    rolling_mean: float
    baseline_mean: float
    sample_count: int
    severity: str  # "warning" | "critical"


class RollingDriftDetector:
    """In-memory rolling-mean drift detector.

    Maintains a per-(tenant, metric) sliding window of recent
    observations.  Compares the rolling mean against a longer-horizon
    baseline (also kept in memory) and emits a :class:`DriftAlert`
    when the rolling mean drops below the configured fraction of the
    baseline mean.

    Designed for sub-minute reactivity to model swaps and flaky
    upstream services without depending on the Prometheus pipeline.
    Memory bounded by ``window_size`` × ``baseline_size`` per tenant.
    """

    def __init__(
        self,
        *,
        window_size: int = 5,
        baseline_size: int = 50,
        warning_ratio: float = 0.85,
        critical_ratio: float = 0.65,
        min_baseline_samples: int = 10,
    ):
        if window_size < 2:
            raise ValueError("window_size must be at least 2")
        if baseline_size <= window_size:
            raise ValueError("baseline_size must exceed window_size")
        if not 0 < critical_ratio <= warning_ratio <= 1:
            raise ValueError("0 < critical_ratio <= warning_ratio <= 1")
        if min_baseline_samples > baseline_size:
            raise ValueError("min_baseline_samples must be ≤ baseline_size")
        self._window_size = window_size
        self._baseline_size = baseline_size
        self._warning_ratio = warning_ratio
        self._critical_ratio = critical_ratio
        self._min_baseline_samples = min_baseline_samples
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._baselines: dict[tuple[str, str], deque[float]] = {}

    def observe(
        self, *, tenant_id: str, metric: str, value: float,
    ) -> Optional[DriftAlert]:
        """Record a sample.  Returns a :class:`DriftAlert` when the
        rolling window has dropped below the configured baseline ratio.

        The detector requires ``min_baseline_samples`` baseline values
        before it begins reporting drift — early observations only feed
        the baseline.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(v) or math.isinf(v):
            return None

        key = (tenant_id or "unknown", metric)
        win = self._windows.setdefault(key, deque(maxlen=self._window_size))
        base = self._baselines.setdefault(key, deque(maxlen=self._baseline_size))
        win.append(v)
        base.append(v)

        if len(base) < self._min_baseline_samples or len(win) < self._window_size:
            return None
        # Use baseline excluding the rolling window so the recent dip
        # doesn't drag the comparison value down.
        baseline_pool = [
            b for b in list(base)[: -self._window_size]
        ]
        if len(baseline_pool) < self._min_baseline_samples - self._window_size:
            return None
        baseline_mean = statistics.fmean(baseline_pool) if baseline_pool else 0.0
        rolling_mean = statistics.fmean(win)
        if baseline_mean <= 0:
            return None
        ratio = rolling_mean / baseline_mean

        severity: Optional[str] = None
        if ratio <= self._critical_ratio:
            severity = "critical"
        elif ratio <= self._warning_ratio:
            severity = "warning"
        if severity is None:
            return None

        return DriftAlert(
            metric=metric,
            tenant_id=key[0],
            rolling_mean=round(rolling_mean, 4),
            baseline_mean=round(baseline_mean, 4),
            sample_count=len(base),
            severity=severity,
        )


# ─── Aggregator ──────────────────────────────────────────────────────────────

def build_quality_summary(
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str = "",
    duration_seconds: float = 0.0,
    scenes: Iterable[dict] = (),
    evidence_steps: Iterable[dict] = (),
    cursor_events: Iterable[dict] = (),
    audio_intents: Iterable[Any] = (),
    controls: Iterable[dict] = (),
    stage_durations_seconds: Optional[dict[str, float]] = None,
    status: str = "completed",
) -> ArtifactQualitySummary:
    """Aggregate per-row evidence into one :class:`ArtifactQualitySummary`.

    Pure transformation — the caller (spine engine's persist endpoint)
    passes whatever lists it has in scope and gets back a structured
    summary ready to feed :class:`CanonicalQualityRecorder.record`.
    """
    scenes_list = list(scenes or [])
    steps_list = list(evidence_steps or [])
    cursors_list = list(cursor_events or [])
    intents_list = list(audio_intents or [])
    controls_list = list(controls or [])

    total = len(steps_list)
    avg_confidence = (
        round(sum(float(s.get("confidence") or 0.0) for s in steps_list) / total, 4)
        if total else 0.0
    )
    avg_agreement = (
        round(sum(float(s.get("agreement_score") or 0.0) for s in steps_list) / total, 4)
        if total else 0.0
    )

    # Per-signal coverage — fraction of evidence_steps citing each.
    signal_coverage: dict[str, float] = {}
    if total:
        signal_counts: dict[str, int] = {}
        for s in steps_list:
            for sig in (s.get("evidence_signals") or []):
                signal_counts[sig] = signal_counts.get(sig, 0) + 1
        signal_coverage = {
            sig: round(count / total, 4) for sig, count in signal_counts.items()
        }

    # Scene quality distribution.
    scene_quality: dict[str, float] = {}
    if scenes_list:
        tier_counts: dict[str, int] = {"strong": 0, "degraded": 0, "weak": 0}
        for sc in scenes_list:
            quality = (
                (sc.get("scene_state_summary") or {}).get("state_quality")
                or sc.get("scene_quality")
                or "weak"
            )
            quality = str(quality).lower()
            if quality not in tier_counts:
                quality = "weak"
            tier_counts[quality] += 1
        n = len(scenes_list)
        scene_quality = {tier: round(c / n, 4) for tier, c in tier_counts.items()}

    cursor_density = 0.0
    if duration_seconds > 0 and cursors_list:
        cursor_density = round(len(cursors_list) / duration_seconds, 4)

    automation_ready = sum(
        1 for c in controls_list if c.get("automation_ready")
    )

    return ArtifactQualitySummary(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id,
        duration_seconds=float(duration_seconds),
        scene_count=len(scenes_list),
        frame_count=int(sum(1 for _ in scenes_list)),  # placeholder when unknown
        evidence_step_count=total,
        cursor_event_count=len(cursors_list),
        audio_intent_count=len(intents_list),
        automation_ready_control_count=automation_ready,
        avg_confidence=avg_confidence,
        avg_agreement_score=avg_agreement,
        cursor_density=cursor_density,
        signal_coverage=signal_coverage,
        scene_quality_distribution=scene_quality,
        stage_durations_seconds=dict(stage_durations_seconds or {}),
        status=status,
    )
