"""
Phase 12 — DAG primitive tests.

Covers the topological-sort + ready-set algorithms that the dispatcher
rewrite will consume. The dispatcher itself is NOT in this commit.

What's verified:
  - Linear plans (today's shape) still work end-to-end through the
    DAG runner.
  - Multi-fork / multi-join shapes (the multimodal use case) produce
    the right ready-sets.
  - Cycles are rejected.
  - Unknown depends_on names are rejected.
  - ready_steps() is deterministic — same input → same output every
    call. The dispatcher's behavior must not flip-flop.
"""

from __future__ import annotations

import pytest

from nexus_sdk.workflows.models import (
    StepPlan, StepKind, WorkflowPlan, WorkflowKind,
)
from nexus_sdk.workflows.dag import (
    DAGError, validate_dag, topological_order, ready_steps, is_complete,
)


def _plan(steps: list[StepPlan]) -> WorkflowPlan:
    """Helper: build a WorkflowPlan with sensible defaults for tests."""
    return WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t",
        session_id="s",
        steps=steps,
    )


def _step(name: str, depends_on: list[str] | None = None) -> StepPlan:
    # Phase 12 semantics: depends_on=None means "auto-derive linear chain";
    # depends_on=[] means "explicit root." Pass through verbatim so the
    # caller controls which one they get.
    return StepPlan(
        name=name,
        engine="test",
        depends_on=depends_on,
    )


# ─── Linear plans (today's shape) ──────────────────────────────


def test_linear_plan_auto_derives_depends_on():
    """A linear plan with no explicit depends_on still works because
    WorkflowPlan.__post_init__ auto-derives the chain."""
    plan = _plan([_step("a"), _step("b"), _step("c")])
    assert plan.steps[0].depends_on == []
    assert plan.steps[1].depends_on == ["a"]
    assert plan.steps[2].depends_on == ["b"]


def test_linear_plan_ready_sequence():
    """Linear plan: ready set is one step at a time."""
    plan = _plan([_step("a"), _step("b"), _step("c")])
    assert ready_steps(plan, completed=[]) == ["a"]
    assert ready_steps(plan, completed=["a"]) == ["b"]
    assert ready_steps(plan, completed=["a", "b"]) == ["c"]
    assert ready_steps(plan, completed=["a", "b", "c"]) == []
    assert is_complete(plan, completed=["a", "b", "c"]) is True


def test_linear_plan_topological_order_matches_input_order():
    """For a linear plan, topo order equals plan.steps order — the
    dispatcher gets stable behavior."""
    plan = _plan([_step("a"), _step("b"), _step("c"), _step("d")])
    assert topological_order(plan) == ["a", "b", "c", "d"]


# ─── DAG plans (Phase 12 use case) ─────────────────────────────


def test_diamond_dag_fork_and_join():
    """Classic diamond:
        a → b → d
        a → c → d
       After a completes, b AND c become ready in parallel.
       After both b and c complete, d becomes ready."""
    plan = _plan([
        _step("a"),
        _step("b", depends_on=["a"]),
        _step("c", depends_on=["a"]),
        _step("d", depends_on=["b", "c"]),
    ])
    assert ready_steps(plan, completed=[]) == ["a"]
    assert ready_steps(plan, completed=["a"]) == ["b", "c"]
    # Order: step's original index decides ties — b before c.
    assert ready_steps(plan, completed=["a", "b"]) == ["c"]
    assert ready_steps(plan, completed=["a", "c"]) == ["b"]
    assert ready_steps(plan, completed=["a", "b", "c"]) == ["d"]
    assert is_complete(plan, ["a", "b", "c", "d"]) is True


def test_multimodal_fork_video_audio_join_backbone():
    """The use case Phase 12 enables: video + audio run in parallel,
    backbone joins both. Mirrors what the new multimodal_plan will
    look like once the dispatcher rewrite lands."""
    plan = _plan([
        _step("shield.redact"),
        # Video branch
        _step("eyes.extract_frames",    depends_on=["shield.redact"]),
        _step("eyes.detect_scenes",     depends_on=["eyes.extract_frames"]),
        _step("eyes.analyze",           depends_on=["eyes.detect_scenes"]),
        # Audio branch — parallel to video, both started from shield.redact
        _step("ears.preprocess",        depends_on=["shield.redact"]),
        _step("ears.diarize",           depends_on=["ears.preprocess"]),
        _step("ears.transcribe",        depends_on=["ears.preprocess"]),
        _step("ears.align",             depends_on=["ears.diarize", "ears.transcribe"]),
        # Join
        _step("backbone.canonicalize",  depends_on=["eyes.analyze", "ears.align"]),
    ])

    # After redact, both branches start.
    assert set(ready_steps(plan, completed=["shield.redact"])) == {
        "eyes.extract_frames", "ears.preprocess",
    }

    # Video branch finished, audio not yet — only audio ready next.
    completed = ["shield.redact", "eyes.extract_frames", "eyes.detect_scenes",
                 "eyes.analyze", "ears.preprocess"]
    assert set(ready_steps(plan, completed=completed)) == {
        "ears.diarize", "ears.transcribe",
    }

    # Audio fan-out joins at align.
    completed += ["ears.diarize", "ears.transcribe"]
    assert ready_steps(plan, completed=completed) == ["ears.align"]

    # Both branches done → backbone joins.
    completed += ["ears.align"]
    assert ready_steps(plan, completed=completed) == ["backbone.canonicalize"]
    assert is_complete(
        plan, completed + ["backbone.canonicalize"]
    ) is True


def test_in_flight_steps_are_not_re_ready():
    """A step that's already dispatched (in_flight) must not be
    surfaced again until it terminates."""
    plan = _plan([
        _step("a"),
        _step("b", depends_on=["a"]),
        _step("c", depends_on=["a"]),
    ])
    # After a completes, b and c are both ready.
    assert ready_steps(plan, completed=["a"]) == ["b", "c"]
    # But once b is dispatched, only c should remain ready.
    assert ready_steps(plan, completed=["a"], in_flight=["b"]) == ["c"]


def test_failed_step_aborts_branch():
    """If a dep failed, no descendant becomes ready. Sweeper handles
    the workflow-level fail-out separately."""
    plan = _plan([
        _step("a"),
        _step("b", depends_on=["a"]),
    ])
    assert ready_steps(plan, completed=[], failed=["a"]) == []


# ─── Error cases ───────────────────────────────────────────────


def test_unknown_depends_on_is_rejected():
    """Static check at plan construction time."""
    with pytest.raises(ValueError, match="unknown step"):
        _plan([
            _step("a"),
            _step("b", depends_on=["nonexistent"]),
        ])


def test_cycle_is_rejected_by_validate_dag():
    """A direct cycle is caught."""
    # Bypass WorkflowPlan auto-derivation by constructing depends_on
    # explicitly so the cycle survives.
    plan = WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t",
        session_id="s",
        steps=[
            StepPlan(name="a", engine="e", depends_on=["b"]),
            StepPlan(name="b", engine="e", depends_on=["a"]),
        ],
    )
    with pytest.raises(DAGError, match="cycle"):
        validate_dag(plan)


def test_longer_cycle_is_rejected():
    """3-node cycle: a → b → c → a"""
    plan = WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t",
        session_id="s",
        steps=[
            StepPlan(name="a", engine="e", depends_on=["c"]),
            StepPlan(name="b", engine="e", depends_on=["a"]),
            StepPlan(name="c", engine="e", depends_on=["b"]),
        ],
    )
    with pytest.raises(DAGError, match="cycle"):
        validate_dag(plan)


def test_topological_order_is_deterministic():
    """Same plan, same input order → same topological output. Critical
    for dispatcher reproducibility."""
    steps = [
        _step("setup"),
        _step("branch_b", depends_on=["setup"]),
        _step("branch_a", depends_on=["setup"]),
        _step("finalize", depends_on=["branch_a", "branch_b"]),
    ]
    plan = _plan(steps)
    order1 = topological_order(plan)
    order2 = topological_order(plan)
    assert order1 == order2
    assert order1[0] == "setup"
    assert order1[-1] == "finalize"
    # Tie-break by original position: branch_b appears before branch_a.
    assert order1.index("branch_b") < order1.index("branch_a")
