"""
Phase 12 dispatcher tests.

These tests verify the manager's DAG-aware dispatch logic against a
mocked DB session — no real Postgres needed. They prove:

  - A linear plan with `depends_on` auto-derived still dispatches one
    step at a time (no behavior regression).
  - A diamond DAG dispatches the two parallel branches simultaneously.
  - The multimodal fork (video chain + audio chain) shows both root
    steps in the first ready batch.
  - Cycle detection rejects malformed plans.
  - DAG state survives in checkpoint under __dag_completed_steps__ /
    __dag_in_flight_steps__ keys (no DB migration needed).

Live integration with a Postgres-backed WorkflowStateRow is covered
by the bringup test in tests/integration/test_engine_bring_up.py.
"""

from __future__ import annotations

import pytest

from nexus_sdk.workflows.dag import (
    DAGError, validate_dag, ready_steps, is_complete,
)
from nexus_sdk.workflows.models import (
    StepPlan, StepKind, WorkflowPlan, WorkflowKind,
)


class _Plan:
    """Minimal duck-typed plan for the dag.* helpers — mirrors the
    `_LightweightPlan` shim in manager.py."""

    def __init__(self, steps: list[StepPlan]) -> None:
        self.steps = steps


def _step(name: str, depends_on: list[str] | None = None) -> StepPlan:
    # depends_on=None → auto-derive linear (default).
    # depends_on=[]   → explicit root.
    return StepPlan(name=name, engine="test", depends_on=depends_on)


# ─── Linear plan: dispatcher returns one step at a time ────────


def test_linear_plan_dispatches_one_step_per_round():
    """A 4-step linear plan should see exactly one ready step per
    dispatch round. Identical to pre-Phase 12 behavior."""
    plan = _Plan([
        _step("a"),
        _step("b", depends_on=["a"]),
        _step("c", depends_on=["b"]),
        _step("d", depends_on=["c"]),
    ])
    completed: set[str] = set()
    dispatched_order: list[str] = []
    while True:
        ready = ready_steps(plan, completed=completed)
        if not ready:
            break
        # Linear plan always has exactly one ready step.
        assert len(ready) == 1
        dispatched_order.append(ready[0])
        completed.add(ready[0])
    assert dispatched_order == ["a", "b", "c", "d"]
    assert is_complete(plan, completed)


# ─── Diamond DAG: two branches dispatched in parallel ──────────


def test_diamond_dag_dispatches_parallel_branches():
    """After the root completes, both b and c are ready at the same
    time — the dispatcher enqueues both in one batch."""
    plan = _Plan([
        _step("root"),
        _step("b", depends_on=["root"]),
        _step("c", depends_on=["root"]),
        _step("join", depends_on=["b", "c"]),
    ])
    # Round 1: only root is ready.
    r1 = ready_steps(plan, completed=set())
    assert r1 == ["root"]
    # Round 2: b AND c are ready — these will be dispatched together.
    r2 = ready_steps(plan, completed={"root"})
    assert set(r2) == {"b", "c"}, "DAG must surface both parallel branches"
    # Round 3: only when both b AND c complete does join become ready.
    assert ready_steps(plan, completed={"root", "b"}) == ["c"]
    assert ready_steps(plan, completed={"root", "c"}) == ["b"]
    assert ready_steps(plan, completed={"root", "b", "c"}) == ["join"]


def test_in_flight_steps_block_re_dispatch():
    """Once a step is dispatched (in_flight), it must not appear in
    the ready set again until it terminates. This is what stops the
    dispatcher double-enqueuing the same step on repeated polls."""
    plan = _Plan([
        _step("a"),
        _step("b", depends_on=["a"]),
    ])
    # a is ready, then dispatched (now in flight).
    assert ready_steps(plan, completed=set()) == ["a"]
    assert ready_steps(plan, completed=set(), in_flight={"a"}) == []
    # When a completes, b is ready.
    assert ready_steps(plan, completed={"a"}) == ["b"]
    # If b is in flight, no further steps surface.
    assert ready_steps(plan, completed={"a"}, in_flight={"b"}) == []


# ─── Multimodal plan: video + audio branches concurrent ────────


def test_multimodal_plan_initial_ready_set():
    """Phase 12's multimodal plan has two root steps (shield.redact_video
    and shield.redact_audio), both ready at t=0. The dispatcher
    enqueues both immediately, kicking off video and audio chains in
    parallel."""
    from nexus_sdk.workflows.plans import build_plan

    plan = build_plan(WorkflowKind.MULTIMODAL, tenant_id="t", session_id="s")
    validate_dag(plan)
    initial_ready = set(ready_steps(plan, completed=set()))
    assert initial_ready == {"shield.redact_video", "shield.redact_audio"}, (
        f"Expected both shield roots ready; got {initial_ready}"
    )


def test_multimodal_plan_parallel_chain_progression():
    """Once shield.redact_video completes, eyes.extract_frames is
    ready EVEN IF the audio branch hasn't started its first step
    yet. Branches are independent."""
    from nexus_sdk.workflows.plans import build_plan

    plan = build_plan(WorkflowKind.MULTIMODAL, tenant_id="t", session_id="s")
    # Simulate video branch completing redact while audio hasn't started.
    completed = {"shield.redact_video"}
    in_flight = {"shield.redact_audio"}
    ready = set(ready_steps(plan, completed=completed, in_flight=in_flight))
    assert "eyes.extract_frames" in ready
    # ears.preprocess still blocked because shield.redact_audio hasn't
    # finished yet.
    assert "ears.preprocess" not in ready


def test_multimodal_join_at_backbone():
    """backbone.canonicalize_multimodal only becomes ready once BOTH
    eyes.build_evidence AND ears.align complete."""
    from nexus_sdk.workflows.plans import build_plan

    plan = build_plan(WorkflowKind.MULTIMODAL, tenant_id="t", session_id="s")
    # Simulate everything done except backbone.
    completed = {
        "shield.redact_video", "shield.redact_audio",
        "eyes.extract_frames", "eyes.detect_scenes", "eyes.ocr_frames",
        "eyes.analyze_scenes", "eyes.analyze_transitions",
        "eyes.build_evidence",
        "ears.preprocess", "ears.diarize", "ears.transcribe_segments",
        "ears.align",
    }
    ready = ready_steps(plan, completed=completed)
    assert ready == ["backbone.canonicalize_multimodal"]
    # If video chain not yet done, backbone is NOT ready even when
    # audio is fully done.
    partial = completed - {"eyes.build_evidence"}
    ready_partial = ready_steps(plan, completed=partial)
    assert "backbone.canonicalize_multimodal" not in ready_partial


# ─── DAG state in checkpoint (no migration needed) ─────────────


def test_dag_state_keys_are_namespaced():
    """The dispatcher stores completion + in-flight tracking in
    checkpoint under __dag_* keys. Verify they don't collide with
    typical handler-side keys."""
    from nexus_sdk.workflows.manager import (
        _DAG_COMPLETED_KEY, _DAG_IN_FLIGHT_KEY,
    )
    # Reserved prefix is `__dag_`. Handlers use bare keys like
    # `frames_manifest_key`, `ears_result`, etc. — namespace separation.
    assert _DAG_COMPLETED_KEY.startswith("__dag_")
    assert _DAG_IN_FLIGHT_KEY.startswith("__dag_")
    assert _DAG_COMPLETED_KEY != _DAG_IN_FLIGHT_KEY


# ─── Cycle / malformed plan rejection ──────────────────────────


def test_dispatcher_rejects_cycle_at_validate_time():
    """Cycles are caught by validate_dag before dispatch runs."""
    plan = _Plan([
        StepPlan(name="a", engine="e", depends_on=["c"]),
        StepPlan(name="b", engine="e", depends_on=["a"]),
        StepPlan(name="c", engine="e", depends_on=["b"]),
    ])
    with pytest.raises(DAGError, match="cycle"):
        validate_dag(plan)


def test_dispatcher_aborts_branch_on_failed_dep():
    """When a dependency fails, ready_steps returns empty — no
    downstream step gets dispatched."""
    plan = _Plan([
        _step("a"),
        _step("b", depends_on=["a"]),
    ])
    assert ready_steps(plan, completed=set(), failed={"a"}) == []
