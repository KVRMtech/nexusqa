"""Compatibility shim for nexus_sdk.models.

Provides permissive stubs (Pydantic BaseModel with extra='allow') for
the classes the engines reference. These are envelopes, not validated
schemas — domain validation happens inside each engine. The original
models module was removed from the SDK but engines still import from
this path.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict


# ─── Job tracking ─────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str = ""
    status: JobStatus = JobStatus.PENDING
    progress_percent: float = 0.0
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ─── Generic envelopes ────────────────────────────────────────

class NexusRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    tenant_id: Optional[str] = None
    session_id: Optional[str] = None
    workflow_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None


class NexusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: bool = True
    message: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class Confidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    score: float = 0.0
    reason: Optional[str] = None


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="allow")
    source_id: Optional[str] = None
    source_type: Optional[str] = None
    url: Optional[str] = None
    location: Optional[str] = None
    timestamp: Optional[datetime] = None


# ─── Business rules + test cases ─────────────────────────────

class BusinessRule(BaseModel):
    model_config = ConfigDict(extra="allow")
    rule_id: Optional[str] = None
    description: Optional[str] = None
    condition: Optional[str] = None
    expected_result: Optional[str] = None
    domain: Optional[str] = None
    priority: Optional[str] = None
    confidence: Optional[float] = None
    sources: List[Any] = []


class TestStep(BaseModel):
    model_config = ConfigDict(extra="allow")
    step_number: Optional[int] = None
    action: Optional[str] = None
    expected: Optional[str] = None
    expected_result: Optional[str] = None


class TestCase(BaseModel):
    model_config = ConfigDict(extra="allow")
    test_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    steps: List[TestStep] = []
    priority: Optional[str] = None
    type: Optional[str] = None
    business_rule_id: Optional[str] = None


# ─── Production-grade test artifacts ──────────────────────────

class Precondition(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: Optional[str] = None
    setup_action: Optional[str] = None


class DataWorkbookEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    entry_id: Optional[str] = None
    name: Optional[str] = None
    value: Optional[Any] = None
    data_type: Optional[str] = None


class ProductionTestStep(BaseModel):
    model_config = ConfigDict(extra="allow")
    step_number: Optional[int] = None
    action: Optional[str] = None
    selector: Optional[str] = None
    expected: Optional[str] = None
    expected_result: Optional[str] = None
    data_ref: Optional[str] = None


class ProductionTestCase(BaseModel):
    model_config = ConfigDict(extra="allow")
    test_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    steps: List[ProductionTestStep] = []
    preconditions: List[Precondition] = []
    priority: Optional[str] = None
    type: Optional[str] = None
    tags: List[str] = []
    # Case-level Expected Result — the observable outcome the whole flow should
    # reach (grounded in the last recorded page). Surfaced in Test Studio and
    # carried into the compiled spec for traceability.
    expected_outcome: Optional[str] = None


# ─── Test execution results ───────────────────────────────────

class StepResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    step_number: Optional[int] = None
    status: Optional[str] = None
    message: Optional[str] = None
    duration_ms: Optional[float] = None
    screenshot_path: Optional[str] = None


class TestResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    test_id: Optional[str] = None
    status: Optional[str] = None
    duration_ms: Optional[float] = None
    step_results: List[StepResult] = []
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
