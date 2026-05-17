"""
Nexus SDK — Workflow domain models.

These are wire-format types: workers serialise StepResult, the
orchestrator serialises JobEnvelope, and WorkflowPlan is what callers
pass when creating a new workflow.

Everything is metadata-driven. No hardcoded step lists; callers
construct WorkflowPlan from policy at submission time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class WorkflowKind(str, Enum):
    """Canonical pipeline shapes the platform recognises."""

    AUDIO = "audio.canonicalize"
    VIDEO = "video.canonicalize"
    MULTIMODAL = "multimodal.canonicalize"
    DOCUMENT = "document.canonicalize"
    # External callers may register additional kinds via the plugin
    # system; the persistence layer treats unknown kinds as opaque.


class WorkflowStatus(str, Enum):
    PENDING = "pending"        # plan created, no step dispatched yet
    RUNNING = "running"        # at least one step dispatched, not done
    COMPLETED = "completed"    # all steps finished, result captured
    FAILED = "failed"          # terminal failure (max retries OR fatal)
    CANCELLED = "cancelled"    # operator or deadline triggered cancellation
    QUARANTINED = "quarantined"  # in DLQ for human review


class StepKind(str, Enum):
    """Resource class of a step. Drives queue routing (cpu vs gpu)."""

    CPU = "cpu"
    GPU = "gpu"


# Phase 6 — priority lane classes. The dispatcher routes "priority"
# workflows to a separate queue lane that operators can scale
# independently (premium-tenant SLOs). "standard" lanes serve everyone
# else. Adding a new class requires updating known_lanes() and the
# autoscaling targets in Helm.
STEP_PRIORITY_STANDARD = "standard"
STEP_PRIORITY_PRIORITY = "priority"
_VALID_PRIORITIES = {STEP_PRIORITY_STANDARD, STEP_PRIORITY_PRIORITY}


@dataclass
class StepPlan:
    """One step in a WorkflowPlan."""

    name: str                          # machine-readable step identifier
    engine: str                        # which engine handles this step
    kind: StepKind = StepKind.CPU
    deadline_seconds: int = 300        # per-step deadline (vs workflow deadline)
    max_attempts: int = 3
    # Free-form per-step metadata that the worker reads from the envelope.
    # Keep small; large payloads belong in object storage referenced by key.
    params: dict[str, Any] = field(default_factory=dict)
    # Phase 6 — per-step lane priority. None means "inherit plan
    # priority"; explicit "standard" / "priority" overrides the plan
    # default. Routing goes to `{engine}.{kind}` for standard, and
    # `{engine}.{kind}.priority` for priority lanes.
    priority: Optional[str] = None
    # Phase 12 — DAG support.
    #
    #   None  → "auto": WorkflowPlan.__post_init__ derives a linear
    #           chain (this step depends on the immediately preceding
    #           one). The default — keeps every existing linear plan
    #           working without edits.
    #   []    → explicit root: this step has no dependencies and is
    #           ready from t=0. Use for multi-root DAGs like the
    #           multimodal plan's two shield branches.
    #   [...] → explicit dependencies. The dispatcher dispatches this
    #           step only after every listed name reaches `completed`.
    #
    # After __post_init__, depends_on is always a list (never None).
    depends_on: Optional[list[str]] = None


@dataclass
class WorkflowPlan:
    """The full plan handed to WorkflowManager.create()."""

    kind: WorkflowKind
    tenant_id: str
    session_id: str
    steps: list[StepPlan]
    # Workflow-level deadline. The sweeper cancels any workflow that
    # exceeds this regardless of per-step state.
    deadline_seconds: int = 900        # default 15-minute SLO
    # Caller-supplied initial state passed as the first checkpoint.
    initial_state: dict[str, Any] = field(default_factory=dict)
    # Arbitrary correlation metadata (request_id, trace_id, etc.).
    metadata: dict[str, Any] = field(default_factory=dict)
    # Plan-format version. Bumped when the dispatcher-visible shape
    # changes (e.g. Phase 12 introduces DAG semantics). The dispatcher
    # can refuse plans whose version exceeds what it supports.
    #
    # v1 = linear-only (today, pre-Phase 12 dispatcher)
    # v2 = DAG via depends_on (Phase 12 dispatcher)
    #
    # New plans default to v2; the topological scheduler treats a
    # purely linear depends_on chain as equivalent to v1.
    version: int = 2
    # Phase 6 — plan-level priority lane. Routes every step that
    # doesn't override `priority` onto the engine's priority lane.
    # Premium tenants get a dedicated KEDA-scaled worker pool; the
    # standard lane keeps serving everyone else. Validated by
    # __post_init__; invalid values raise.
    priority: str = STEP_PRIORITY_STANDARD

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("WorkflowPlan must have at least one step")
        if self.priority not in _VALID_PRIORITIES:
            raise ValueError(
                f"WorkflowPlan.priority must be one of {_VALID_PRIORITIES}; "
                f"got {self.priority!r}"
            )
        for s in self.steps:
            if s.priority is not None and s.priority not in _VALID_PRIORITIES:
                raise ValueError(
                    f"StepPlan({s.name!r}).priority must be one of "
                    f"{_VALID_PRIORITIES} or None; got {s.priority!r}"
                )
        seen: set[str] = set()
        for s in self.steps:
            if s.name in seen:
                raise ValueError(f"duplicate step name: {s.name}")
            seen.add(s.name)
        # Phase 12 — resolve `depends_on`:
        #   None  → auto-derive linear chain (existing plans keep working)
        #   []    → explicit root (multi-root DAGs like multimodal)
        #   [...] → explicit deps; validate they exist
        for i, s in enumerate(self.steps):
            if s.depends_on is None:
                # Auto-derive: every step except the first depends on its
                # immediate predecessor.
                s.depends_on = [self.steps[i - 1].name] if i > 0 else []
                continue
            # Caller set it explicitly. Validate.
            for dep in s.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"step {s.name!r} depends_on unknown step {dep!r}"
                    )


@dataclass
class JobEnvelope:
    """
    Wire format the orchestrator hands a worker for one step.

    The worker reads `checkpoint` as input, executes its step, and
    returns a StepResult with the new checkpoint. The orchestrator
    persists that checkpoint BEFORE dispatching the next step, so a
    crash between steps is recoverable.
    """

    workflow_id: str
    step_name: str
    step_index: int
    attempt: int
    tenant_id: str
    session_id: str
    deadline_at_epoch: float
    heartbeat_seconds: int
    params: dict[str, Any]
    checkpoint: dict[str, Any]

    def seconds_remaining(self) -> float:
        return max(0.0, self.deadline_at_epoch - time.time())

    def is_expired(self) -> bool:
        return time.time() >= self.deadline_at_epoch

    def to_wire(self) -> dict[str, Any]:
        """Serialise to the dict the queue layer accepts as payload."""
        return {
            "workflow_id": self.workflow_id,
            "step_name": self.step_name,
            "step_index": self.step_index,
            "attempt": self.attempt,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "deadline_at_epoch": self.deadline_at_epoch,
            "heartbeat_seconds": self.heartbeat_seconds,
            "params": self.params,
            "checkpoint": self.checkpoint,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "JobEnvelope":
        return cls(
            workflow_id=payload["workflow_id"],
            step_name=payload["step_name"],
            step_index=int(payload["step_index"]),
            attempt=int(payload["attempt"]),
            tenant_id=payload["tenant_id"],
            session_id=payload["session_id"],
            deadline_at_epoch=float(payload["deadline_at_epoch"]),
            heartbeat_seconds=int(payload.get("heartbeat_seconds", 30)),
            params=dict(payload.get("params", {})),
            checkpoint=dict(payload.get("checkpoint", {})),
        )


@dataclass
class StepResult:
    """Outcome returned by a worker after executing one step."""

    workflow_id: str
    step_name: str
    success: bool
    # When success: new checkpoint state for the next step.
    # When failure: error message + free-form context for the DLQ payload.
    checkpoint: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_context: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    # If a worker discovers the step is unrunnable (bad input, hard
    # validation failure) it sets `fatal=True` so the orchestrator
    # quarantines the workflow without burning retries.
    fatal: bool = False
