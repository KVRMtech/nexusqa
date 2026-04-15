"""
Nexus Standard Models — Shared request/response types for all engines.

Every engine uses these base models. This ensures:
- Consistent API contracts across all engines
- Traceability (every request has a tenant_id, session_id, trace_id)
- Standard error responses
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────────────

class EngineStatus(str, Enum):
    """Health status of an engine."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class JobStatus(str, Enum):
    """Status of an async processing job."""
    QUEUED = "queued"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Confidence(str, Enum):
    """Confidence level for extracted data."""
    HIGH = "high"        # >90% — auto-accept
    MEDIUM = "medium"    # 70-90% — flag for review
    LOW = "low"          # <70% — require human approval


# ─── Base Request / Response ──────────────────────────────────────

class NexusRequest(BaseModel):
    """Base request model. Every engine request includes these fields."""
    
    tenant_id: str = Field(..., description="Client/tenant identifier")
    session_id: Optional[str] = Field(default=None, description="KT session this relates to")
    trace_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Distributed tracing ID"
    )
    requested_by: Optional[str] = Field(default=None, description="User who made the request")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Request timestamp"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class NexusResponse(BaseModel):
    """Base response model. Every engine response includes these fields."""
    
    success: bool = Field(..., description="Whether the operation succeeded")
    trace_id: str = Field(..., description="Matching trace ID from request")
    engine: str = Field(..., description="Which engine processed this")
    engine_version: str = Field(default="0.1.0", description="Engine version")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Response timestamp"
    )
    processing_time_ms: float = Field(default=0, description="Processing time in milliseconds")
    data: Optional[Any] = Field(default=None, description="Response payload")
    error: Optional[str] = Field(default=None, description="Error message if failed")


class JobResponse(BaseModel):
    """Response for async jobs (long-running operations)."""
    
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique job ID")
    status: JobStatus = Field(default=JobStatus.PENDING)
    trace_id: str = Field(..., description="Matching trace ID")
    engine: str = Field(...)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    estimated_duration_seconds: Optional[int] = Field(default=None)
    progress_percent: float = Field(default=0.0)
    result: Optional[Any] = Field(default=None)
    error: Optional[str] = Field(default=None)


# ─── Knowledge Models ─────────────────────────────────────────────

class SourceReference(BaseModel):
    """Traces any piece of knowledge back to its source."""
    
    session_id: Optional[str] = Field(default=None, description="KT session ID")
    timestamp_start: Optional[str] = Field(default=None, description="Start time in session HH:MM:SS.mmm")
    timestamp_end: Optional[str] = Field(default=None, description="End time in session")
    speaker_id: Optional[str] = Field(default=None, description="Who said/showed this")
    speaker_name: Optional[str] = Field(default=None, description="Speaker display name")
    document_id: Optional[str] = Field(default=None, description="Source document if from a file")
    jira_ticket_id: Optional[str] = Field(default=None, description="Related Jira ticket")
    page_number: Optional[int] = Field(default=None, description="Page number in document")
    confidence: Confidence = Field(default=Confidence.MEDIUM)


class BusinessRule(BaseModel):
    """A formal business rule extracted from knowledge sessions or documents."""
    
    rule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    product: Optional[str] = Field(default=None, description="Product or service name")
    jurisdiction: Optional[str] = Field(default=None, description="Jurisdiction or regulatory region")
    category: str = Field(..., description="Rule category (domain-specific, e.g. rating, compliance)")
    rule_text: str = Field(..., description="Human-readable rule statement")
    formal_logic: Optional[str] = Field(default=None, description="Machine-readable rule logic")
    conditions: list[str] = Field(default_factory=list, description="Conditions for this rule")
    exceptions: list[str] = Field(default_factory=list, description="Known exceptions")
    source: SourceReference = Field(default_factory=SourceReference)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: Optional[datetime] = Field(default=None)
    valid_to: Optional[datetime] = Field(default=None)
    superseded_by: Optional[str] = Field(default=None, description="ID of rule that replaced this one")
    tags: list[str] = Field(default_factory=list)


class TestCase(BaseModel):
    """A test case generated from business rules by the reasoning engine."""
    
    test_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    title: str
    description: str
    preconditions: list[str] = Field(default_factory=list)
    steps: list["TestStep"] = Field(default_factory=list)
    expected_results: list[str] = Field(default_factory=list)
    target_systems: list[str] = Field(default_factory=list, description="web, api, mainframe, db")
    validates_rules: list[str] = Field(default_factory=list, description="Rule IDs this test validates")
    source: SourceReference = Field(default_factory=SourceReference)
    priority: str = Field(default="medium", description="critical, high, medium, low")
    tags: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_by: Optional[str] = Field(default=None, description="Human who approved this test")
    approved_at: Optional[datetime] = Field(default=None)


class TestStep(BaseModel):
    """A single step within a test case."""
    
    step_number: int
    action: str = Field(..., description="What to do")
    target_system: str = Field(..., description="web, api, mainframe, db")
    target_element: Optional[str] = Field(default=None, description="CSS selector, API path, CICS txn, etc.")
    input_data: Optional[dict[str, Any]] = Field(default=None)
    expected_output: Optional[str] = Field(default=None)
    verification: Optional[str] = Field(default=None, description="How to verify this step succeeded")


class TestResult(BaseModel):
    """Result of executing a single test case."""
    
    result_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    test_id: str
    tenant_id: str
    status: str = Field(..., description="pass, fail, error, skip")
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = Field(default=0)
    step_results: list["StepResult"] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list, description="Paths to evidence screenshots")
    logs: list[str] = Field(default_factory=list, description="Execution logs")
    error_message: Optional[str] = Field(default=None)
    defect_id: Optional[str] = Field(default=None, description="Auto-created Jira bug ID if failed")


class StepResult(BaseModel):
    """Result of a single test step execution."""
    
    step_number: int
    status: str  # pass, fail, error
    actual_output: Optional[str] = Field(default=None)
    expected_output: Optional[str] = Field(default=None)
    screenshot: Optional[str] = Field(default=None)
    duration_ms: float = Field(default=0)
    error: Optional[str] = Field(default=None)


# ─── Production Test Case Format ──────────────────────────────────
# These models match the exact Excel output format with Data.X
# parameterization and separated preconditions + data workbook.


class DataWorkbookEntry(BaseModel):
    """Single FieldName/FieldValue pair in a Data Workbook."""

    field_name: str = Field(..., description="Data parameter name (referenced as Data.FieldName in steps)")
    field_value: str = Field(default="", description="Actual value for this parameter")
    field_type: str = Field(
        default="string",
        description="Data type: string, integer, float, boolean, date, email, phone",
    )
    is_sensitive: bool = Field(default=False, description="PII/sensitive data flag")
    generator_hint: str = Field(
        default="",
        description="Hint for data gen: faker.email, range(1,100), enum(A,B,C), etc.",
    )


class ProductionTestStep(BaseModel):
    """
    Production-format test step with step-level expected result.

    Steps use (Data.FieldName) syntax for parameterization.
    Example: "Login to portal using (Data.UserID) and (Data.Password)"
    """

    step_number: int = Field(..., ge=1)
    action: str = Field(..., description="Action with (Data.X) parameter references")
    expected_result: str = Field(default="", description="Expected outcome for this step")
    target_system: str = Field(default="web", description="web, api, mainframe, db, mobile")
    target_element: str = Field(default="", description="CSS selector, API path, screen ID")
    input_data_refs: list[str] = Field(
        default_factory=list,
        description="Data.FieldName references in this step, auto-extracted",
    )
    verification: str = Field(default="", description="How to verify this step passed")
    screenshot_required: bool = Field(default=False)


class Precondition(BaseModel):
    """A single test case precondition."""

    description: str = Field(..., description="Precondition text")
    is_verified: bool = Field(default=False, description="Has this precondition been verified?")


class ProductionTestCase(BaseModel):
    """
    Production-format test case matching the exact Excel output.

    Output columns: Test Case ID | Title | Step Number | Action | Expected Result
    Plus separate Preconditions section and Data Workbook sheet.

    Test Case ID format: {prefix}-V{version:02d}-{seq:03d}
    Example: E2E-V11-001
    """

    test_case_id: str = Field(..., description="Formatted ID like E2E-V11-001")
    tenant_id: str
    title: str = Field(..., description="Descriptive test case title")
    description: str = Field(default="")
    test_type: str = Field(
        default="e2e",
        description="e2e, integration, bva, negative, performance, edge, regression",
    )
    priority: str = Field(default="medium", description="critical, high, medium, low")
    status: str = Field(default="draft", description="draft, review, approved, deprecated")
    version: int = Field(default=1)

    # Core content
    preconditions: list[Precondition] = Field(default_factory=list)
    steps: list[ProductionTestStep] = Field(default_factory=list)
    data_workbook: list[DataWorkbookEntry] = Field(default_factory=list)

    # Traceability
    target_systems: list[str] = Field(default_factory=list)
    validates_rules: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_session_id: Optional[str] = Field(default=None)
    source_speaker_id: Optional[str] = Field(default=None)
    suite_id: Optional[str] = Field(default=None)

    # Metadata
    generated_by: str = Field(default="system", description="system, manual, import")
    approved_by: Optional[str] = Field(default=None)
    approved_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def get_data_refs(self) -> set[str]:
        """Extract all unique (Data.X) references from steps."""
        import re
        refs: set[str] = set()
        for step in self.steps:
            refs.update(re.findall(r"\(Data\.(\w+)\)", step.action))
            refs.update(re.findall(r"\(Data\.(\w+)\)", step.expected_result))
        return refs

    def validate_data_coverage(self) -> list[str]:
        """Check that every (Data.X) ref has a workbook entry and vice versa."""
        errors: list[str] = []
        refs = self.get_data_refs()
        workbook_fields = {e.field_name for e in self.data_workbook}

        missing_data = refs - workbook_fields
        unused_data = workbook_fields - refs

        for m in missing_data:
            errors.append(f"Step references (Data.{m}) but no workbook entry exists")
        for u in unused_data:
            errors.append(f"Workbook entry '{u}' is not referenced in any step")

        return errors
