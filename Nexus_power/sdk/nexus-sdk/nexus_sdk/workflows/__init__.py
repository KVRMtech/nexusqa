"""
Nexus SDK — Canonical Workflow Layer.

A workflow is an ordered list of steps, each executed by a specific
engine. The orchestrator owns:
  - The plan (which engine runs which step in what order).
  - The checkpoint (the running state passed between steps).
  - The deadline.
  - The retry policy.

A worker owns only the body of ONE step: pull a JobEnvelope, do work,
return a StepResult. Workers are stateless; they may crash mid-step
and the orchestrator will re-dispatch the same step to another worker.

This package provides:
  - models: dataclasses for plans, envelopes, results.
  - manager: WorkflowManager (state CRUD + transitions in Postgres).
  - dispatch: WorkflowDispatcher ties WorkflowManager to JobQueue.
  - worker: WorkflowWorker runs step handlers in engine pods.
  - sweeper: deadline / heartbeat / stuck-workflow detector.
"""

from nexus_sdk.workflows.models import (
    JobEnvelope,
    StepKind,
    StepPlan,
    StepResult,
    WorkflowKind,
    WorkflowPlan,
    WorkflowStatus,
)
from nexus_sdk.workflows.manager import WorkflowManager, WorkflowNotFound
from nexus_sdk.workflows.dispatch import WorkflowDispatcher, queue_name
from nexus_sdk.workflows.worker import WorkerConfig, WorkflowWorker, StepHandler
from nexus_sdk.workflows.plans import (
    audio_plan,
    build_plan,
    canonical_pipeline_plan,
    document_plan,
    multimodal_plan,
    video_plan,
    CANONICAL_PLANS,
)

__all__ = [
    "JobEnvelope",
    "StepKind",
    "StepPlan",
    "StepResult",
    "WorkflowKind",
    "WorkflowPlan",
    "WorkflowStatus",
    "WorkflowManager",
    "WorkflowNotFound",
    "WorkflowDispatcher",
    "queue_name",
    "WorkerConfig",
    "WorkflowWorker",
    "StepHandler",
    "audio_plan",
    "build_plan",
    "canonical_pipeline_plan",
    "document_plan",
    "multimodal_plan",
    "video_plan",
    "CANONICAL_PLANS",
]
