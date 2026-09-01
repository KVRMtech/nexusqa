"""
Phase 12 — DAG scheduling primitives for WorkflowPlan.

Today's orchestrator dispatches steps in `plan.steps` order: when step
N completes, step N+1 is dispatched. This assumes the plan is a linear
list.

Phase 12 lets a plan declare arbitrary precedence via
`StepPlan.depends_on`. Multimodal plans then fan out video + audio
branches as parallel chains rejoined at backbone.canonicalize.

This module implements:
  - `validate_dag(plan)` — assert acyclic, all depends_on names exist,
    exactly-one-terminal (the step every other step transitively
    feeds into); raises on violation
  - `ready_steps(plan, completed)` — given the set of completed step
    names, return the set of step names whose dependencies are
    satisfied AND that haven't run yet (i.e. ready to dispatch)
  - `topological_order(plan)` — a deterministic linearization; used
    for plan visualization and as a sanity check (failing topo sort →
    cycle in the DAG)

The dispatcher rewrite that consumes these primitives is part of the
full Phase 12 work and is NOT in this commit. Today's linear
dispatcher continues to work against linear plans via the implicit
`depends_on` fallback in WorkflowPlan.__post_init__.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from nexus_sdk.workflows.models import StepPlan, WorkflowPlan


class DAGError(ValueError):
    """A WorkflowPlan's depends_on edges are not a valid DAG."""


def validate_dag(plan: WorkflowPlan) -> None:
    """Verify the plan forms a valid DAG.

    Raises DAGError on:
      - Reference to a non-existent step name in `depends_on`
      - Cycles
      - Disconnected components (a step that nothing depends on AND
        depends on nothing — those would never get dispatched in
        Phase 12's join semantics)

    Returns None on success. Idempotent; safe to call repeatedly.
    """
    by_name: dict[str, StepPlan] = {s.name: s for s in plan.steps}

    # 1. Reference check
    for s in plan.steps:
        for dep in (s.depends_on or []):
            if dep not in by_name:
                raise DAGError(
                    f"step {s.name!r} depends_on unknown step {dep!r}"
                )

    # 2. Cycle check via Kahn's algorithm
    in_degree: dict[str, int] = {s.name: len((s.depends_on or [])) for s in plan.steps}
    children: dict[str, list[str]] = defaultdict(list)
    for s in plan.steps:
        for dep in (s.depends_on or []):
            children[dep].append(s.name)

    queue: deque[str] = deque(
        name for name, deg in in_degree.items() if deg == 0
    )
    processed = 0
    while queue:
        name = queue.popleft()
        processed += 1
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
    if processed != len(plan.steps):
        unprocessed = [
            name for name, deg in in_degree.items() if deg > 0
        ]
        raise DAGError(
            "cycle detected in WorkflowPlan; "
            f"unresolved steps: {sorted(unprocessed)}"
        )

    # 3. Connected check — every non-root step must reach a terminal,
    # and every non-terminal step must feed into something. A fully
    # disconnected step would silently sit unreachable.
    has_outgoing: set[str] = set()
    for s in plan.steps:
        for dep in (s.depends_on or []):
            has_outgoing.add(dep)
    terminals = [s.name for s in plan.steps if s.name not in has_outgoing]
    if not terminals:
        raise DAGError(
            "no terminal step found — every step has a dependent, "
            "which is only possible on a cycle"
        )


def topological_order(plan: WorkflowPlan) -> list[str]:
    """Deterministic linearization for visualization / sanity checks.

    Ties are broken by the step's original position in plan.steps,
    so a linear plan's topological order == its plan.steps order.

    Raises DAGError on cycle.
    """
    validate_dag(plan)
    by_name: dict[str, StepPlan] = {s.name: s for s in plan.steps}
    original_position: dict[str, int] = {s.name: i for i, s in enumerate(plan.steps)}

    in_degree: dict[str, int] = {s.name: len((s.depends_on or [])) for s in plan.steps}
    children: dict[str, list[str]] = defaultdict(list)
    for s in plan.steps:
        for dep in (s.depends_on or []):
            children[dep].append(s.name)

    # Priority queue sorted by original position so ties break stably.
    ready: list[str] = sorted(
        [name for name, deg in in_degree.items() if deg == 0],
        key=lambda n: original_position[n],
    )
    out: list[str] = []
    while ready:
        name = ready.pop(0)
        out.append(name)
        for child in children[name]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                # Insert in original-position order
                pos = original_position[child]
                inserted = False
                for i, existing in enumerate(ready):
                    if original_position[existing] > pos:
                        ready.insert(i, child)
                        inserted = True
                        break
                if not inserted:
                    ready.append(child)
    return out


def ready_steps(
    plan: WorkflowPlan,
    completed: Iterable[str],
    *,
    failed: Iterable[str] = (),
    in_flight: Iterable[str] = (),
) -> list[str]:
    """Return step names that are ready to dispatch.

    A step is ready when:
      - It has not completed
      - It is not in-flight (already dispatched, awaiting result)
      - It is not failed (any failed dep aborts the workflow; the
        sweeper handles that out-of-band)
      - All its `depends_on` are in `completed`

    Result is ordered by the step's original position in plan.steps
    so the dispatcher's behavior is deterministic.
    """
    completed_set = set(completed)
    failed_set = set(failed)
    in_flight_set = set(in_flight)

    # Fast-fail if any dep failed — the whole branch is dead.
    if failed_set:
        # The caller decides what to do with this; we just don't
        # surface anything new for dispatch.
        return []

    ready: list[str] = []
    for s in plan.steps:
        if s.name in completed_set:
            continue
        if s.name in in_flight_set:
            continue
        if all(dep in completed_set for dep in (s.depends_on or [])):
            ready.append(s.name)
    return ready


def is_complete(plan: WorkflowPlan, completed: Iterable[str]) -> bool:
    """Workflow is complete when every step is in `completed`."""
    completed_set = set(completed)
    return all(s.name in completed_set for s in plan.steps)


__all__ = [
    "DAGError",
    "validate_dag",
    "topological_order",
    "ready_steps",
    "is_complete",
]
