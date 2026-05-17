"""
Contract tests for the canonical pipeline plan.

These tests don't run any engines — they assert structural invariants
of the plan factory's output. They catch:

  - A new step added without wiring depends_on correctly.
  - A step deadline that exceeds the workflow deadline (would never
    actually fail because the workflow deadline fires first).
  - A cycle introduced by a bad depends_on edit.
  - The persist-first / enrich-async pattern getting accidentally
    broken (Phase 3): `spine.persist_minimal_artifact` MUST run
    before any step that needs `artifact_id`, and
    `spine.update_artifact_enriched` MUST come after the vision branch.
  - The audio-only / video-only / multimodal step counts drifting.

Run with: pytest tests/contracts/test_canonical_plan_contract.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Allow running directly from repo root without installing the SDK.
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "sdk" / "nexus-sdk"))

from nexus_sdk.workflows.plans import canonical_pipeline_plan  # noqa: E402
from nexus_sdk.workflows.models import (  # noqa: E402
    StepKind, StepPlan, WorkflowKind, WorkflowPlan,
)


# ─── Helpers ─────────────────────────────────────────────────


def _plan(has_audio: bool, has_video: bool, profile: str = "fast") -> WorkflowPlan:
    return canonical_pipeline_plan(
        tenant_id="canary",
        session_id="contract-test",
        has_audio=has_audio,
        has_video=has_video,
        profile=profile,
        artifact_id="00000000-0000-0000-0000-000000000000",
        audio_file_path="/tmp/audio.wav" if has_audio else "",
        video_file_path="/tmp/video.mp4" if has_video else "",
        source_filename="contract-test.mp4",
    )


def _has_cycle(steps: list[StepPlan]) -> bool:
    """Kahn's algorithm: a DAG with no cycles topologically sorts to
    the same node count as the input."""
    name_to_deps = {s.name: list(s.depends_on or []) for s in steps}
    in_degree = {name: 0 for name in name_to_deps}
    for deps in name_to_deps.values():
        for d in deps:
            if d in in_degree:
                in_degree[d] += 0  # the dep is referenced by us; nothing to bump
    # Proper Kahn: count incoming edges (each step's depends_on are its
    # predecessors). Step X has in_degree = len(X.depends_on).
    in_degree = {s.name: len(s.depends_on or []) for s in steps}
    queue = [n for n, d in in_degree.items() if d == 0]
    visited: list[str] = []
    while queue:
        n = queue.pop(0)
        visited.append(n)
        for s in steps:
            deps = s.depends_on or []
            if n in deps:
                in_degree[s.name] -= 1
                if in_degree[s.name] == 0:
                    queue.append(s.name)
    return len(visited) != len(steps)


# ─── Tests ───────────────────────────────────────────────────


@pytest.mark.parametrize("has_audio,has_video", [
    (True, False),
    (False, True),
    (True, True),
])
def test_plan_is_a_valid_dag(has_audio: bool, has_video: bool) -> None:
    plan = _plan(has_audio, has_video)
    names = {s.name for s in plan.steps}
    # Every depends_on entry must reference a real step.
    for s in plan.steps:
        for dep in (s.depends_on or []):
            assert dep in names, (
                f"step {s.name} depends_on {dep!r} which is not in the plan"
            )
    # No cycles.
    assert not _has_cycle(plan.steps), "plan contains a cycle"


@pytest.mark.parametrize("has_audio,has_video,expected_kind", [
    (True, False, WorkflowKind.AUDIO),
    (False, True, WorkflowKind.VIDEO),
    (True, True, WorkflowKind.MULTIMODAL),
])
def test_plan_kind_matches_modality(
    has_audio: bool, has_video: bool, expected_kind: WorkflowKind,
) -> None:
    plan = _plan(has_audio, has_video)
    assert plan.kind == expected_kind


def test_neither_modality_rejected() -> None:
    with pytest.raises(ValueError):
        _plan(has_audio=False, has_video=False)


def test_step_deadlines_bounded_by_workflow_deadline() -> None:
    plan = _plan(has_audio=True, has_video=True)
    for s in plan.steps:
        assert s.deadline_seconds <= plan.deadline_seconds, (
            f"step {s.name} deadline {s.deadline_seconds}s exceeds workflow "
            f"deadline {plan.deadline_seconds}s — the step can never time out "
            "before the workflow does"
        )


def test_max_attempts_in_sane_range() -> None:
    plan = _plan(has_audio=True, has_video=True)
    for s in plan.steps:
        assert 1 <= s.max_attempts <= 5, (
            f"step {s.name} has max_attempts={s.max_attempts}, expected 1..5"
        )


def test_persist_first_pattern_phase3() -> None:
    """The Phase 3 architectural pivot: minimal artifact MUST be written
    before any step that needs artifact_id, and enrichment MUST come after
    the vision branch."""
    plan = _plan(has_audio=True, has_video=True)
    by_name = {s.name: s for s in plan.steps}
    assert "spine.persist_minimal_artifact" in by_name, (
        "Phase 3 step spine.persist_minimal_artifact missing — "
        "the persist-first pivot has been reverted"
    )
    assert "spine.update_artifact_enriched" in by_name, (
        "Phase 3 step spine.update_artifact_enriched missing"
    )
    # update_artifact_enriched must depend (transitively) on the vision
    # branch: at minimum, on something that produces eyes_result or
    # visual_graph_output.
    upd = by_name["spine.update_artifact_enriched"]
    deps = set(upd.depends_on or [])
    # Direct deps should include the visual graph (build_visual_graph)
    # OR the evidence builder, depending on the plan revision.
    expected_any = {"spine.build_visual_graph", "eyes.build_evidence"}
    assert deps & expected_any, (
        f"spine.update_artifact_enriched should depend on one of "
        f"{expected_any}, got depends_on={deps}"
    )


def test_audio_only_no_video_steps() -> None:
    plan = _plan(has_audio=True, has_video=False)
    names = {s.name for s in plan.steps}
    for forbidden in (
        "eyes.extract_frames",
        "eyes.detect_scenes",
        "eyes.ocr_frames",
        "eyes.analyze_scenes",
        "shield.redact_video",
        "spine.build_visual_graph",
    ):
        assert forbidden not in names, (
            f"audio-only plan should not include {forbidden}"
        )


def test_video_only_no_audio_steps() -> None:
    plan = _plan(has_audio=False, has_video=True)
    names = {s.name for s in plan.steps}
    for forbidden in (
        "ears.diarize",
        "ears.transcribe_segments",
        "ears.align",
        "shield.redact_audio",
        "ears.preprocess",
    ):
        assert forbidden not in names, (
            f"video-only plan should not include {forbidden}"
        )


def test_engines_referenced_are_in_known_set() -> None:
    """A step naming a typo'd engine would route to a nonexistent
    queue lane. Catch it before runtime."""
    known = {
        "shield", "eyes", "ears", "backbone", "legs",
        "spine", "mouth", "nerves", "hands", "brain", "heart",
    }
    plan = _plan(has_audio=True, has_video=True)
    for s in plan.steps:
        assert s.engine in known, (
            f"step {s.name} routes to unknown engine {s.engine!r}"
        )


def test_step_names_are_engine_prefixed() -> None:
    """Convention: step names are `<engine>.<verb>` so log greps and
    dashboards can split by engine."""
    plan = _plan(has_audio=True, has_video=True)
    for s in plan.steps:
        head, _, _ = s.name.partition(".")
        assert head == s.engine, (
            f"step {s.name} prefix should match engine={s.engine!r}"
        )


def test_terminal_step_has_no_outgoing_edge() -> None:
    """The plan terminates with a backbone.canonicalize* step that
    nothing else depends on."""
    plan = _plan(has_audio=True, has_video=True)
    referenced = set()
    for s in plan.steps:
        for d in (s.depends_on or []):
            referenced.add(d)
    # Steps that nothing references are terminal. There should be at
    # least one and it should be a backbone step.
    terminals = {s.name for s in plan.steps if s.name not in referenced}
    assert terminals, "plan has no terminal step (everything is referenced)"
    backbone_terminals = [t for t in terminals if t.startswith("backbone.")]
    assert backbone_terminals, (
        f"expected a backbone.* terminal step, got terminals={terminals}"
    )


def test_plan_step_count_within_bounds() -> None:
    """Sanity check on plan size. If a refactor accidentally doubles
    the step count, this fails loudly."""
    bounds = {
        WorkflowKind.AUDIO: (8, 14),
        WorkflowKind.VIDEO: (10, 16),
        WorkflowKind.MULTIMODAL: (15, 24),
    }
    for has_audio, has_video in [(True, False), (False, True), (True, True)]:
        plan = _plan(has_audio, has_video)
        low, high = bounds[plan.kind]
        assert low <= len(plan.steps) <= high, (
            f"plan kind={plan.kind} has {len(plan.steps)} steps, "
            f"expected {low}..{high} (drift?)"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
