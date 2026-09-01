"""Acceptance-test harness — compare evidence_steps against ground truth.

The harness implements the public contract used by both the pytest suite
(``test_acceptance.py``) and the CLI smoke runner.  It is framework-free
so a CI job can call :func:`run_acceptance` against any list of fixtures
and report metrics, while pytest can wrap each fixture as a parametrized
case and assert per-fixture thresholds.

Fixture YAML schema::

    name: usaa-life-quote-form-fill
    description: SME demos USAA term life insurance quote form
    video_path: ../recordings/usaa_quote_form.mp4
    artifact_id: <optional — use existing artifact instead of re-processing>
    thresholds:
      action_kind_accuracy: 0.80   # fraction of expected actions whose
                                   # action_kind matches an actual step
      target_match_rate:    0.70   # fraction whose target_label contains
                                   # the expected substring
      value_match_rate:     0.50   # fraction with observed_value match
                                   # (only counted for steps that expect one)
      max_spurious_steps:   3      # extra steps tolerated above expected
    expected_actions:
      - timestamp_ms_min: 0
        timestamp_ms_max: 3000
        action_kind: navigate
        target_label_contains: usaa
      - timestamp_ms_min: 20000
        timestamp_ms_max: 38000
        action_kind: enter_text
        target_label_contains: birthdate
        observed_value_contains: "1990"
      ...

The harness performs a many-to-many soft match (greedy assignment by
timestamp proximity) and computes per-fixture metrics.  Each metric has
a documented threshold so regressions are visible the moment they cross.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ─── Public dataclasses ──────────────────────────────────────────────────────

@dataclass
class ExpectedAction:
    """One expected user action — the ground-truth annotation per fixture."""

    timestamp_ms_min: int
    timestamp_ms_max: int
    action_kind: str
    target_label_contains: Optional[str] = None
    observed_value_contains: Optional[str] = None
    # Free-form notes the annotator wants to attach for diff-readability.
    notes: str = ""

    def matches_timestamp(self, step_ms: int) -> bool:
        return self.timestamp_ms_min <= step_ms <= self.timestamp_ms_max


@dataclass
class AcceptanceThresholds:
    """Minimum metric levels for a fixture to PASS.

    Operators tighten thresholds as the pipeline matures.  Defaults are
    conservative (60% kind match) so an early-stage deployment can still
    PASS while sub-stages are stabilising.
    """

    action_kind_accuracy: float = 0.60
    target_match_rate: float = 0.50
    value_match_rate: float = 0.40
    max_spurious_steps: int = 5


@dataclass
class FixtureManifest:
    """Loaded fixture record."""

    name: str
    description: str
    video_path: Optional[str]
    artifact_id: Optional[str]
    thresholds: AcceptanceThresholds
    expected_actions: list[ExpectedAction] = field(default_factory=list)


@dataclass
class StepMatch:
    """Result of pairing one expected action with one actual step."""

    expected: ExpectedAction
    actual_step: Optional[dict]
    kind_correct: bool = False
    target_correct: bool = False
    value_correct: bool = False
    timestamp_distance_ms: Optional[int] = None


@dataclass
class FixtureResult:
    """Per-fixture metrics + matched/missing/spurious detail."""

    fixture: FixtureManifest
    matches: list[StepMatch] = field(default_factory=list)
    spurious_steps: list[dict] = field(default_factory=list)

    @property
    def total_expected(self) -> int:
        return len(self.fixture.expected_actions)

    @property
    def matched_count(self) -> int:
        return sum(1 for m in self.matches if m.actual_step is not None)

    @property
    def missing_count(self) -> int:
        return self.total_expected - self.matched_count

    @property
    def action_kind_accuracy(self) -> float:
        if not self.matches:
            return 0.0
        return sum(1 for m in self.matches if m.kind_correct) / float(self.total_expected or 1)

    @property
    def target_match_rate(self) -> float:
        applicable = [m for m in self.matches if m.expected.target_label_contains is not None]
        if not applicable:
            return 1.0  # nothing required → trivially satisfied
        return sum(1 for m in applicable if m.target_correct) / float(len(applicable))

    @property
    def value_match_rate(self) -> float:
        applicable = [m for m in self.matches if m.expected.observed_value_contains is not None]
        if not applicable:
            return 1.0
        return sum(1 for m in applicable if m.value_correct) / float(len(applicable))

    @property
    def passed(self) -> bool:
        t = self.fixture.thresholds
        return (
            self.action_kind_accuracy >= t.action_kind_accuracy
            and self.target_match_rate >= t.target_match_rate
            and self.value_match_rate >= t.value_match_rate
            and len(self.spurious_steps) <= t.max_spurious_steps
        )

    def summary_line(self) -> str:
        return (
            f"{self.fixture.name}: "
            f"matched {self.matched_count}/{self.total_expected} · "
            f"kind={self.action_kind_accuracy:.0%} "
            f"target={self.target_match_rate:.0%} "
            f"value={self.value_match_rate:.0%} "
            f"spurious={len(self.spurious_steps)} "
            f"→ {'PASS' if self.passed else 'FAIL'}"
        )


# ─── Loader ──────────────────────────────────────────────────────────────────

def load_fixture(path: str) -> FixtureManifest:
    """Parse a YAML fixture file into a :class:`FixtureManifest`.

    Raises :class:`FileNotFoundError` when the file is missing,
    :class:`ValueError` when required keys are absent.  PyYAML is
    imported lazily so environments that only run the unit-level
    matching tests do not need it.
    """
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover — environment-dependent
        raise ImportError(
            "tests/acceptance requires PyYAML.  Install with: pip install pyyaml"
        ) from exc

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"fixture {path!r}: top-level must be a mapping")

    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError(f"fixture {path!r}: 'name' is required")

    raw_thresholds = data.get("thresholds") or {}
    thresholds = AcceptanceThresholds(
        action_kind_accuracy=float(raw_thresholds.get(
            "action_kind_accuracy", AcceptanceThresholds.action_kind_accuracy,
        )),
        target_match_rate=float(raw_thresholds.get(
            "target_match_rate", AcceptanceThresholds.target_match_rate,
        )),
        value_match_rate=float(raw_thresholds.get(
            "value_match_rate", AcceptanceThresholds.value_match_rate,
        )),
        max_spurious_steps=int(raw_thresholds.get(
            "max_spurious_steps", AcceptanceThresholds.max_spurious_steps,
        )),
    )

    expected_raw = data.get("expected_actions") or []
    if not isinstance(expected_raw, list):
        raise ValueError(f"fixture {path!r}: 'expected_actions' must be a list")
    expected: list[ExpectedAction] = []
    for idx, item in enumerate(expected_raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"fixture {path!r}: expected_actions[{idx}] must be a mapping"
            )
        expected.append(ExpectedAction(
            timestamp_ms_min=int(item.get("timestamp_ms_min", 0)),
            timestamp_ms_max=int(item.get("timestamp_ms_max", 0)),
            action_kind=str(item.get("action_kind") or "").strip().lower(),
            target_label_contains=(
                str(item["target_label_contains"]).strip()
                if item.get("target_label_contains") else None
            ),
            observed_value_contains=(
                str(item["observed_value_contains"]).strip()
                if item.get("observed_value_contains") else None
            ),
            notes=str(item.get("notes") or ""),
        ))

    return FixtureManifest(
        name=name,
        description=str(data.get("description") or ""),
        video_path=(
            os.path.normpath(os.path.join(os.path.dirname(path), data["video_path"]))
            if data.get("video_path") else None
        ),
        artifact_id=(str(data["artifact_id"]).strip() if data.get("artifact_id") else None),
        thresholds=thresholds,
        expected_actions=expected,
    )


def load_all_fixtures(directory: str) -> list[FixtureManifest]:
    """Load every ``*.yaml`` / ``*.yml`` file under ``directory``."""
    fixtures: list[FixtureManifest] = []
    if not os.path.isdir(directory):
        return fixtures
    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)
        if not os.path.isfile(full):
            continue
        if not entry.lower().endswith((".yaml", ".yml")):
            continue
        try:
            fixtures.append(load_fixture(full))
        except Exception as exc:  # pragma: no cover — config error surface
            raise RuntimeError(f"failed to load {full}: {exc}") from exc
    return fixtures


# ─── Matching engine ─────────────────────────────────────────────────────────

# Action-kind compatibility groups — two kinds are "compatible" if they
# describe the same intent at different specificity levels.  E.g. an
# evidence_step classified as "click" still satisfies an expected
# "click_cta" annotation.
_KIND_COMPATIBILITY: dict[str, set[str]] = {
    "click": {"click", "click_cta"},
    "click_cta": {"click", "click_cta"},
    "enter_text": {"enter_text", "type"},
    "type": {"enter_text", "type"},
    "select_option": {"select_option", "select"},
    "select": {"select_option", "select"},
    "submit_form": {"submit_form", "submit"},
    "submit": {"submit_form", "submit"},
    "navigate": {"navigate", "open"},
    "open_overlay": {"open_overlay", "open"},
    "review": {"review", "verify", "inspect"},
    "scroll": {"scroll", "scroll_or_repaint"},
}


def _kinds_compatible(expected: str, actual: str) -> bool:
    e = (expected or "").lower().strip()
    a = (actual or "").lower().strip()
    if not e or not a:
        return False
    if e == a:
        return True
    return a in _KIND_COMPATIBILITY.get(e, set())


def _substring_match(expected_substr: Optional[str], actual_text: str) -> bool:
    if expected_substr is None:
        return True
    if not actual_text:
        return False
    return expected_substr.lower().strip() in actual_text.lower()


def match_actions_to_steps(
    expected_actions: Iterable[ExpectedAction],
    actual_steps: Iterable[dict],
) -> tuple[list[StepMatch], list[dict]]:
    """Greedy match of expected actions against actual steps.

    Algorithm:
      1. Sort expected actions by timestamp_ms_min.
      2. For each expected, find the earliest UNUSED actual step whose
         timestamp falls inside the expected window AND whose action_kind
         is compatible.  Among ties, prefer the closer-to-window-centre.
      3. Steps not matched by any expected are ``spurious_steps``.
      4. Expected actions with no actual match record StepMatch with
         ``actual_step=None`` so the result preserves order.
    """
    expected_list = sorted(
        list(expected_actions),
        key=lambda a: (a.timestamp_ms_min, a.timestamp_ms_max),
    )
    actual_list = sorted(
        list(actual_steps),
        key=lambda s: int(s.get("start_ms") or 0),
    )

    used_indices: set[int] = set()
    matches: list[StepMatch] = []

    for expected in expected_list:
        window_centre = (expected.timestamp_ms_min + expected.timestamp_ms_max) // 2
        candidate: Optional[tuple[int, int, dict]] = None  # (idx, distance, step)
        for idx, step in enumerate(actual_list):
            if idx in used_indices:
                continue
            step_ms = int(step.get("start_ms") or 0)
            if not expected.matches_timestamp(step_ms):
                continue
            if not _kinds_compatible(expected.action_kind, step.get("action_kind", "")):
                continue
            distance = abs(step_ms - window_centre)
            if candidate is None or distance < candidate[1]:
                candidate = (idx, distance, step)

        if candidate is not None:
            used_indices.add(candidate[0])
            step = candidate[2]
            target = str(step.get("target_label") or "")
            value = str(step.get("observed_value") or "")
            matches.append(StepMatch(
                expected=expected,
                actual_step=step,
                kind_correct=True,
                target_correct=_substring_match(expected.target_label_contains, target),
                value_correct=_substring_match(expected.observed_value_contains, value),
                timestamp_distance_ms=candidate[1],
            ))
        else:
            matches.append(StepMatch(expected=expected, actual_step=None))

    spurious = [
        actual_list[i]
        for i in range(len(actual_list))
        if i not in used_indices
    ]
    return matches, spurious


# ─── Top-level runner ────────────────────────────────────────────────────────

def evaluate_fixture(
    fixture: FixtureManifest,
    actual_steps: Iterable[dict],
) -> FixtureResult:
    """Build a :class:`FixtureResult` for one fixture given its actual steps."""
    matches, spurious = match_actions_to_steps(fixture.expected_actions, actual_steps)
    return FixtureResult(
        fixture=fixture,
        matches=matches,
        spurious_steps=spurious,
    )


def run_acceptance(
    fixtures: Iterable[FixtureManifest],
    fetch_steps: "callable[[FixtureManifest], list[dict]]",
) -> list[FixtureResult]:
    """Evaluate every fixture by calling ``fetch_steps(fixture)`` to obtain
    the actual evidence_steps and comparing against expected_actions.

    ``fetch_steps`` is supplied by the caller so the harness stays
    transport-agnostic — production callers query the platform API while
    tests can pass a stubbed dict.  Each fixture's :class:`FixtureResult`
    is returned in the iteration order supplied.
    """
    results: list[FixtureResult] = []
    for fixture in fixtures:
        steps = list(fetch_steps(fixture))
        results.append(evaluate_fixture(fixture, steps))
    return results
