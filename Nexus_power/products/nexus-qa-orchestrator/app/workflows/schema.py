"""
Nexus Workflow Schema — Chain & Stage definition models.

A Chain is a directed acyclic graph (DAG) of Stages.
Each Stage calls one engine endpoint with configurable
input/output mapping, retries, conditions, and polling.

Different chains = different products:
  - QA Testing chain
  - Compliance Audit chain
  - Knowledge Capture chain
  - Regression Suite chain
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Any
from enum import Enum

from pydantic import BaseModel, Field


# ─── Enums ─────────────────────────────────────────────────────

class StageStatus(str, Enum):
    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    POLLING = "polling"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    RETRYING = "retrying"


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    DEGRADED = "degraded"  # Completed with skipped/failed non-fatal stages
    NEEDS_REVIEW = "needs_review"  # Brain quality gate failed — held for human review
    FAILED = "failed"
    POLICY_BLOCKED = "policy_blocked"  # Hard policy gate rejected the artifact
    CANCELLED = "cancelled"


# ─── Stage Configuration Models ────────────────────────────────

class RetryPolicy(BaseModel):
    """Configures retry behaviour for a stage."""
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=2.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    retry_on_status: list[int] = Field(
        default_factory=lambda: [500, 502, 503, 504],
    )


class PollingConfig(BaseModel):
    """
    Configuration for async engines that return a job ID
    and require polling for the final result (e.g. Ears, Eyes, Legs).
    """
    enabled: bool = False
    job_id_path: str = Field(
        default="job_id",
        description="Dot-path into the initial response to extract the job ID",
    )
    poll_endpoint: str = Field(
        default="",
        description="Endpoint template with {job_id} placeholder",
    )
    poll_interval_seconds: float = Field(default=5.0, ge=1.0)
    max_poll_seconds: float = Field(default=600.0, ge=10.0)
    completion_statuses: list[str] = Field(
        default_factory=lambda: ["completed"],
    )
    failure_statuses: list[str] = Field(
        default_factory=lambda: ["failed", "error"],
    )
    result_path: str = Field(
        default="result",
        description="Dot-path into poll response for the final result",
    )
    status_path: str = Field(
        default="status",
        description="Dot-path into poll response to read the current status",
    )


# ─── Stage Definition ─────────────────────────────────────────

class StageDefinition(BaseModel):
    """
    A single stage in a chain — one HTTP call to one engine.

    Input/output mapping uses $-prefixed context paths:
      $workflow.tenant_id         — workflow-level value
      $stages.<id>.output.<key>   — output from a prior stage
      $workflow.input.<key>       — original workflow input
      $temp.item                  — current for_each iteration item

    String interpolation uses ${path} syntax:
      "Pipeline for session ${workflow.session_id}"
    """

    # ── Identity ───────────────────────────────────────────────
    stage_id: str = Field(..., description="Unique identifier within the chain")
    name: str = Field(..., description="Human-readable stage name")
    description: str = ""

    # ── Engine target ──────────────────────────────────────────
    engine: str = Field(..., description="Engine name (resolves to base URL)")
    endpoint: str = Field(..., description="API path, e.g. /api/v1/shield/redact")
    method: str = Field(default="POST", pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    request_type: str = Field(
        default="json",
        pattern="^(json|multipart)$",
        description="json = JSON body, multipart = file upload",
    )

    # ── Request mapping ────────────────────────────────────────
    input_mapping: dict[str, Any] = Field(
        default_factory=dict,
        description="Maps context paths ($-prefixed) to request body/param fields",
    )
    file_mappings: dict[str, str] = Field(
        default_factory=dict,
        description="For multipart: {form_field_name: $context_path_to_file_id}",
    )
    headers_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Additional headers (values may be $-prefixed)",
    )

    # ── Response handling ──────────────────────────────────────
    output_transform: Optional[str] = Field(
        default=None,
        description=(
            "Python expression to transform the raw response before storing. "
            "The variable 'result' holds the response dict/list. "
            "Example: \"[item['properties'] for item in result]\""
        ),
    )

    # ── Control flow ───────────────────────────────────────────
    condition: Optional[str] = Field(
        default=None,
        description=(
            "Expression evaluated against context. Stage is skipped when false. "
            "Simple: '$workflow.input.audio_file_id'  "
            "Complex: 'len($stages.rule_extraction.output.rules) > 0'"
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Stage IDs that must complete before this stage runs",
    )

    # ── Error handling ─────────────────────────────────────────
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int = Field(default=300, ge=1, le=7200)
    on_failure: str = Field(
        default="fail",
        pattern="^(fail|skip|continue)$",
        description="fail = abort workflow, skip = mark skipped, continue = mark failed but proceed",
    )

    # ── Async polling ──────────────────────────────────────────
    polling: Optional[PollingConfig] = None

    # ── Iteration ──────────────────────────────────────────────
    for_each: Optional[str] = Field(
        default=None,
        description="Context path to a list. Stage executes once per item.",
    )
    for_each_item_key: str = Field(
        default="item",
        description="Context key name for the current iteration item",
    )
    for_each_concurrency: int = Field(
        default=1, ge=1, le=50,
        description="Max parallel iterations for for_each",
    )


# ─── Chain Definition ─────────────────────────────────────────

class ChainDefinition(BaseModel):
    """
    A complete workflow chain definition.
    Different chains = different products.

    The stages form a DAG via their depends_on fields.
    Stages at the same dependency level execute in parallel.
    """
    chain_id: str = Field(..., description="Unique chain identifier (e.g. nexus.qa-testing)")
    name: str = Field(..., description="Human-readable chain name")
    description: str = ""
    version: str = "1.0.0"
    tags: list[str] = Field(default_factory=list)
    stages: list[StageDefinition] = Field(..., min_length=1)

    # Metadata
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    created_by: str = ""
    tenant_id: str = Field(
        default="",
        description="Empty = system-level chain available to all tenants",
    )


# ─── Runtime State Models ─────────────────────────────────────

class StageExecution(BaseModel):
    """Runtime state of a single stage execution."""
    stage_id: str
    status: StageStatus = StageStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    output: Any = None
    error: Optional[str] = None
    retries: int = 0
    duration_ms: float = 0.0
    iteration_results: list[Any] = Field(default_factory=list)
    # P1: Real-time progress detail for async/polling stages
    progress_detail: Optional[dict] = Field(
        default=None,
        description=(
            "Live progress from async engines. Contains: "
            "progress_percent, current_stage, engine_job_id, "
            "last_poll_at, stall_seconds"
        ),
    )


class WorkflowInstance(BaseModel):
    """Runtime state of a complete workflow execution."""
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chain_id: str
    chain_name: str = ""
    tenant_id: str
    session_id: str = ""
    created_by: str = ""
    status: WorkflowStatus = WorkflowStatus.CREATED
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_data: dict = Field(default_factory=dict)
    stages: dict[str, StageExecution] = Field(default_factory=dict)
    timeline: list[dict] = Field(default_factory=list)
    error: Optional[str] = None


# ─── API Request / Response Models ─────────────────────────────

class StartWorkflowRequest(BaseModel):
    chain_id: str = Field(..., description="ID of the chain to execute")
    tenant_id: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    input_data: dict = Field(
        default_factory=dict,
        description="Workflow input: file IDs, SUT config, options, etc.",
    )


class StartWorkflowResponse(BaseModel):
    workflow_id: str
    chain_id: str
    chain_name: str
    status: WorkflowStatus
    session_id: str
    # Early canonical ID — pre-allocated at upload time for immediate tracking
    artifact_id: Optional[str] = None
    # P3: Cache decision metadata
    cache_hit: bool = False
    cached_artifact_id: Optional[str] = None
    cached_artifact_status: Optional[str] = None
    cache_reason: Optional[str] = None


class WorkflowSummary(BaseModel):
    workflow_id: str
    chain_id: str
    chain_name: str
    tenant_id: str
    status: WorkflowStatus
    created_at: str
    stages_completed: int
    stages_total: int
    error: Optional[str] = None


class ChainListItem(BaseModel):
    chain_id: str
    name: str
    description: str
    version: str
    tags: list[str]
    stage_count: int
    tenant_id: str
