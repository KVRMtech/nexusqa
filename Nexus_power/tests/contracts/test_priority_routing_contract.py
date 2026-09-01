"""
Contract tests for the Phase 6 priority lane routing.

The plan-level / step-level priority drives queue_name(). These tests
pin the exact lane string a step routes to so a typo in the routing
function doesn't silently send premium-tenant work onto the standard
lane (the bug only surfaces when the standard lane saturates and
nobody can explain why).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "sdk" / "nexus-sdk"))

from nexus_sdk.workflows.dispatch import queue_name  # noqa: E402
from nexus_sdk.workflows.models import (  # noqa: E402
    StepKind, StepPlan, WorkflowKind, WorkflowPlan,
    STEP_PRIORITY_STANDARD, STEP_PRIORITY_PRIORITY,
)


def test_standard_priority_yields_base_lane() -> None:
    assert queue_name("eyes", StepKind.GPU) == "eyes.gpu"
    assert queue_name("spine", StepKind.CPU) == "spine.cpu"


def test_priority_yields_priority_suffix() -> None:
    assert queue_name(
        "eyes", StepKind.GPU, priority=STEP_PRIORITY_PRIORITY,
    ) == "eyes.gpu.priority"
    assert queue_name(
        "spine", StepKind.CPU, priority=STEP_PRIORITY_PRIORITY,
    ) == "spine.cpu.priority"


def test_unknown_priority_falls_back_to_standard_lane() -> None:
    """Unknown priority values shouldn't crash the dispatcher — they
    should produce the standard lane name (safe default)."""
    assert queue_name(
        "eyes", StepKind.GPU, priority="garbage",
    ) == "eyes.gpu"


def test_plan_validates_priority_field() -> None:
    base_step = StepPlan(name="x.do", engine="x", kind=StepKind.CPU)
    # Valid values pass.
    WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t1",
        session_id="s1",
        steps=[base_step],
        priority=STEP_PRIORITY_STANDARD,
    )
    WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t1",
        session_id="s1",
        steps=[base_step],
        priority=STEP_PRIORITY_PRIORITY,
    )
    # Invalid values raise at plan construction (early failure).
    with pytest.raises(ValueError):
        WorkflowPlan(
            kind=WorkflowKind.VIDEO,
            tenant_id="t1",
            session_id="s1",
            steps=[base_step],
            priority="urgent",  # not a recognised class
        )


def test_step_priority_overrides_plan_priority() -> None:
    """Effective priority is step.priority (when set) else plan.priority.
    The dispatcher reads this when picking the lane. Validate the
    invariant by hand-rolling the same computation a contract reader
    would expect."""
    steps = [
        StepPlan(name="a.x", engine="a", kind=StepKind.CPU),
        StepPlan(
            name="b.x", engine="b", kind=StepKind.CPU,
            priority=STEP_PRIORITY_STANDARD,  # explicit override down
        ),
    ]
    plan = WorkflowPlan(
        kind=WorkflowKind.VIDEO,
        tenant_id="t1",
        session_id="s1",
        steps=steps,
        priority=STEP_PRIORITY_PRIORITY,  # plan-level: priority
    )
    # Step "a" inherits plan priority => priority lane.
    effective_a = steps[0].priority or plan.priority
    assert queue_name(
        steps[0].engine, steps[0].kind, priority=effective_a,
    ) == "a.cpu.priority"
    # Step "b" overrides => standard lane.
    effective_b = steps[1].priority or plan.priority
    assert queue_name(
        steps[1].engine, steps[1].kind, priority=effective_b,
    ) == "b.cpu"


def test_step_priority_value_validated() -> None:
    bad = StepPlan(
        name="x.do", engine="x", kind=StepKind.CPU,
        priority="urgent",
    )
    with pytest.raises(ValueError):
        WorkflowPlan(
            kind=WorkflowKind.VIDEO,
            tenant_id="t1",
            session_id="s1",
            steps=[bad],
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
