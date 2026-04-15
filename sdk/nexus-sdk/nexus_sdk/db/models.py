"""
Nexus ORM Models — SQLAlchemy models for all persistent entities.

These models map directly to PostgreSQL tables.
All multi-tenant data includes a tenant_id column.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus_sdk.db import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ─── Tenants ───────────────────────────────────────────────────

class TenantRow(Base):
    """Client tenant (organization)."""
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(50), default="starter")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    # Relationships
    users: Mapped[list["UserRow"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


# ─── Users ─────────────────────────────────────────────────────

class UserRow(Base):
    """Platform user belonging to a tenant."""
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    tenant: Mapped["TenantRow"] = relationship(back_populates="users")

    __table_args__ = (
        Index("ix_users_tenant_email", "tenant_id", "email"),
    )


# ─── Workflow Instances ────────────────────────────────────────

class WorkflowInstanceRow(Base):
    """Persistent record of a workflow execution."""
    __tablename__ = "workflow_instances"

    workflow_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    chain_id: Mapped[str] = mapped_column(String(100), nullable=False)
    chain_name: Mapped[str] = mapped_column(String(200), default="")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    input_data: Mapped[dict] = mapped_column(JSON, default=dict)
    stages: Mapped[dict] = mapped_column(JSON, default=dict)
    timeline: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_workflow_tenant_status", "tenant_id", "status"),
    )


# ─── Workflow Context Snapshots ────────────────────────────────

class WorkflowContextRow(Base):
    """Persisted workflow context for crash-safe resume."""
    __tablename__ = "workflow_contexts"

    workflow_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


# ─── Audit Log ─────────────────────────────────────────────────

class AuditLogRow(Base):
    """Immutable audit log entry for compliance."""
    __tablename__ = "audit_log"

    log_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), default="")
    entity_id: Mapped[str] = mapped_column(String(100), default="")
    user_id: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_audit_tenant_engine", "tenant_id", "engine"),
        Index("ix_audit_created", "created_at"),
    )


# ─── Report Storage ───────────────────────────────────────────

class ReportRow(Base):
    """Stored report metadata + content."""
    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="")
    format: Mapped[str] = mapped_column(String(10), default="html")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_reports_tenant_type", "tenant_id", "report_type"),
    )


# ─── Shield Audit Log ─────────────────────────────────────────

class ShieldAuditRow(Base):
    """PII detection/redaction audit trail."""
    __tablename__ = "shield_audit"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)  # scan, redact, un-redact
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    pii_types: Mapped[list] = mapped_column(JSON, default=list)
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── KT Sessions ──────────────────────────────────────────────

class SessionRow(Base):
    """Knowledge Transfer session."""
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="scheduled")
    session_type: Mapped[str] = mapped_column(String(50), default="knowledge_transfer")
    sme_name: Mapped[str] = mapped_column(String(200), default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    rules_extracted: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    events: Mapped[list] = mapped_column(JSON, default=list)
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("ix_sessions_tenant_status", "tenant_id", "status"),
    )


# ─── SME Profiles ─────────────────────────────────────────────

class SMEProfileRow(Base):
    """Subject Matter Expert profile."""
    __tablename__ = "sme_profiles"

    speaker_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(100), default="")
    department: Mapped[str] = mapped_column(String(100), default="")
    expertise_domains: Mapped[list] = mapped_column(JSON, default=list)
    total_sessions: Mapped[int] = mapped_column(Integer, default=0)
    total_rules: Mapped[int] = mapped_column(Integer, default=0)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    last_session_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── Contradictions ────────────────────────────────────────────

class ContradictionRow(Base):
    """Tracked contradiction between business rules."""
    __tablename__ = "contradictions"

    contradiction_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_a_id: Mapped[str] = mapped_column(String(64), default="")
    rule_b_id: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="open")
    resolution: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_contradictions_tenant_status", "tenant_id", "status"),
    )


# ─── Guardrails ───────────────────────────────────────────────

class GuardrailPipelineRow(Base):
    """AI confidence guardrail pipeline step."""
    __tablename__ = "guardrail_pipeline"

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(200), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="active")
    threshold: Mapped[float] = mapped_column(Float, default=0.7)
    rules_processed: Mapped[int] = mapped_column(Integer, default=0)
    rules_passed: Mapped[int] = mapped_column(Integer, default=0)
    rules_flagged: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ReviewQueueRow(Base):
    """Items pending human review."""
    __tablename__ = "review_queue"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(50), default="rule")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    assigned_to: Mapped[str] = mapped_column(String(200), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_review_queue_tenant_status", "tenant_id", "status"),
    )


class TrustTrendRow(Base):
    """AI trust/confidence trend data over time."""
    __tablename__ = "trust_trend"

    trend_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rules_total: Mapped[int] = mapped_column(Integer, default=0)
    rules_verified: Mapped[int] = mapped_column(Integer, default=0)
    rules_rejected: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── Traceability ─────────────────────────────────────────────

class TraceRow(Base):
    """Living traceability matrix entry."""
    __tablename__ = "traces"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    requirement_id: Mapped[str] = mapped_column(String(100), default="")
    requirement_text: Mapped[str] = mapped_column(Text, default="")
    rule_id: Mapped[str] = mapped_column(String(100), default="")
    test_case_id: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="linked")
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── Test Suites & Runs ───────────────────────────────────────

class TestSuiteRow(Base):
    """Test suite definition."""
    __tablename__ = "test_suites"

    suite_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    test_count: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="active")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class TestRunRow(Base):
    """Test execution run."""
    __tablename__ = "test_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suite_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    total_tests: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    environment: Mapped[str] = mapped_column(String(100), default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_test_runs_tenant_status", "tenant_id", "status"),
    )


# ─── Data Forge ────────────────────────────────────────────────

class ForgeConfigRow(Base):
    """Test data generation configuration."""
    __tablename__ = "forge_configs"

    config_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_definition: Mapped[dict] = mapped_column(JSON, default=dict)
    record_count: Mapped[int] = mapped_column(Integer, default=100)
    format: Mapped[str] = mapped_column(String(20), default="json")
    status: Mapped[str] = mapped_column(String(30), default="active")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ForgeResultRow(Base):
    """Test data generation result."""
    __tablename__ = "forge_results"

    result_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    config_id: Mapped[str] = mapped_column(String(64), default="")
    records_generated: Mapped[int] = mapped_column(Integer, default=0)
    format: Mapped[str] = mapped_column(String(20), default="json")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── Compliance / Jurisdictions ────────────────────────────────

class JurisdictionRow(Base):
    """Compliance jurisdiction tracking."""
    __tablename__ = "jurisdictions"

    jurisdiction_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(20), default="")
    region: Mapped[str] = mapped_column(String(100), default="")
    rules_count: Mapped[int] = mapped_column(Integer, default=0)
    compliance_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="active")
    last_audit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ─── Test Cases ────────────────────────────────────────────────

class TestCaseRow(Base):
    """
    Persisted test case — production data model matching exact output format.

    Test Case ID pattern: {prefix}-V{version}-{seq}
    Example: E2E-V11-001, BVA-V03-012, NEG-V11-005
    """
    __tablename__ = "test_cases"

    test_case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    suite_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("test_suites.suite_id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    test_type: Mapped[str] = mapped_column(
        String(30), default="e2e",
        comment="e2e, integration, bva, negative, performance, edge, regression",
    )
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(
        String(30), default="draft",
        comment="draft, review, approved, deprecated",
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    target_systems: Mapped[list] = mapped_column(JSON, default=list)
    validates_rules: Mapped[list] = mapped_column(
        JSON, default=list, comment="Business rule IDs this test validates"
    )
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_speaker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_by: Mapped[str] = mapped_column(
        String(50), default="system", comment="system, manual, import"
    )
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    # Relationships
    steps: Mapped[list["TestCaseStepRow"]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan",
        order_by="TestCaseStepRow.step_number",
    )
    preconditions: Mapped[list["TestCasePreconditionRow"]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan",
        order_by="TestCasePreconditionRow.sort_order",
    )
    data_workbook: Mapped[list["DataWorkbookEntryRow"]] = relationship(
        back_populates="test_case", cascade="all, delete-orphan",
        order_by="DataWorkbookEntryRow.sort_order",
    )
    suite: Mapped["TestSuiteRow | None"] = relationship()

    __table_args__ = (
        Index("ix_test_cases_tenant_status", "tenant_id", "status"),
        Index("ix_test_cases_tenant_type", "tenant_id", "test_type"),
        Index("ix_test_cases_suite", "suite_id"),
    )


class TestCaseStepRow(Base):
    """
    Individual step within a test case.

    Steps use (Data.FieldName) syntax for parameterization.
    Example action: "Login to portal using (Data.UserID)"
    """
    __tablename__ = "test_case_steps"

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    test_case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("test_cases.test_case_id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, comment="Action with (Data.X) params")
    expected_result: Mapped[str] = mapped_column(Text, default="", comment="Expected outcome for this step")
    target_system: Mapped[str] = mapped_column(String(50), default="web")
    target_element: Mapped[str] = mapped_column(String(500), default="")
    input_data_refs: Mapped[list] = mapped_column(
        JSON, default=list,
        comment="List of Data.FieldName references used in this step",
    )
    verification: Mapped[str] = mapped_column(Text, default="")
    screenshot_required: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Relationships
    test_case: Mapped["TestCaseRow"] = relationship(back_populates="steps")

    __table_args__ = (
        Index("ix_test_case_steps_case_num", "test_case_id", "step_number", unique=True),
    )


class TestCasePreconditionRow(Base):
    """Precondition for a test case."""
    __tablename__ = "test_case_preconditions"

    precondition_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    test_case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("test_cases.test_case_id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    test_case: Mapped["TestCaseRow"] = relationship(back_populates="preconditions")

    __table_args__ = (
        Index("ix_preconditions_case", "test_case_id"),
    )


class DataWorkbookEntryRow(Base):
    """
    Data Workbook entry — parameterized test data.

    Each entry is a FieldName/FieldValue pair referenced in test steps
    via the (Data.FieldName) syntax.
    """
    __tablename__ = "data_workbook_entries"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    test_case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("test_cases.test_case_id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    field_name: Mapped[str] = mapped_column(String(200), nullable=False)
    field_value: Mapped[str] = mapped_column(Text, default="")
    field_type: Mapped[str] = mapped_column(
        String(30), default="string",
        comment="string, integer, float, boolean, date, email, phone",
    )
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="PII or sensitive data flag"
    )
    generator_hint: Mapped[str] = mapped_column(
        String(100), default="",
        comment="Hint for data generation: faker.email, range(1,100), etc.",
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Relationships
    test_case: Mapped["TestCaseRow"] = relationship(back_populates="data_workbook")

    __table_args__ = (
        Index("ix_data_workbook_case", "test_case_id"),
        Index("ix_data_workbook_field", "test_case_id", "field_name", unique=True),
    )


# ─── Export Tracking ───────────────────────────────────────────

class ExportJobRow(Base):
    """Tracks test case export jobs (Excel, CSV, JSON)."""
    __tablename__ = "export_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    export_type: Mapped[str] = mapped_column(
        String(20), default="excel", comment="excel, csv, json"
    )
    scope: Mapped[str] = mapped_column(
        String(30), default="test_case",
        comment="test_case, suite, all",
    )
    scope_id: Mapped[str] = mapped_column(String(64), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_export_jobs_tenant", "tenant_id", "status"),
    )


# ─── Media: Audio Files ───────────────────────────────────────

class AudioFileRow(Base):
    """
    Uploaded audio file tracking with preprocessing metadata.

    Maps 1:1 with a physical audio file on disk.
    Linked to a KT session and tenant.
    """
    __tablename__ = "audio_files"

    audio_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # File info
    original_filename: Mapped[str] = mapped_column(String(500), default="")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    processed_path: Mapped[str] = mapped_column(
        String(1000), default="", comment="Path to preprocessed 16kHz mono WAV"
    )
    format: Mapped[str] = mapped_column(String(20), default="wav")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # Technical metadata
    sample_rate: Mapped[int] = mapped_column(Integer, default=16000)
    channels: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    bit_depth: Mapped[int] = mapped_column(Integer, default=16)
    codec: Mapped[str] = mapped_column(String(50), default="pcm_s16le")

    # Preprocessing flags
    normalized: Mapped[bool] = mapped_column(Boolean, default=False)
    noise_reduced: Mapped[bool] = mapped_column(Boolean, default=False)
    resampled_from: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Processing state
    preprocess_stages: Mapped[list] = mapped_column(
        JSON, default=list, comment="Stages applied: probe, transcode, normalize, chunk"
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_audio_files_tenant_session", "tenant_id", "session_id"),
    )


# ─── Media: Transcript Segments ───────────────────────────────

class TranscriptSegmentRow(Base):
    """
    Time-aligned transcript segment with speaker attribution.

    Normalized storage for transcript data — one row per utterance.
    Replaces the flat JSON list in sessions.transcript.
    """
    __tablename__ = "transcript_segments"

    segment_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    audio_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("audio_files.audio_id", ondelete="SET NULL"), nullable=True
    )

    # Content
    speaker: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    language: Mapped[str] = mapped_column(String(10), default="en")

    # Word-level timing (optional)
    words_json: Mapped[list] = mapped_column(
        JSON, default=list,
        comment="Word-level timestamps: [{word, start, end, probability}]",
    )

    # Ordering
    segment_index: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_transcript_segments_session", "session_id", "segment_index"),
        Index("ix_transcript_segments_tenant", "tenant_id", "session_id"),
        Index("ix_transcript_segments_speaker", "session_id", "speaker"),
    )


# ─── Media: Video Files ───────────────────────────────────────

class VideoFileRow(Base):
    """
    Uploaded video file (screen recording) tracking.

    Maps 1:1 with a physical video file on disk.
    """
    __tablename__ = "video_files"

    video_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # File info
    original_filename: Mapped[str] = mapped_column(String(500), default="")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="mp4")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # Video metadata
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=30.0)
    codec: Mapped[str] = mapped_column(String(50), default="h264")

    # Processing
    total_frames_extracted: Mapped[int] = mapped_column(Integer, default=0)
    total_frames_analyzed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_video_files_tenant_session", "tenant_id", "session_id"),
    )


# ─── Media: Visual Frames ─────────────────────────────────────

class VisualFrameRow(Base):
    """
    Analyzed frame from a video recording or standalone screenshot.

    Stores OCR text, UI element detections, classification, and
    the natural-language description from the vision model.
    """
    __tablename__ = "visual_frames"

    frame_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    video_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("video_files.video_id", ondelete="SET NULL"), nullable=True
    )

    # Frame position
    frame_index: Mapped[int] = mapped_column(Integer, default=0)
    timestamp_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    frame_path: Mapped[str] = mapped_column(String(1000), default="")

    # Classification
    application_type: Mapped[str] = mapped_column(String(50), default="unknown")
    page_title: Mapped[str] = mapped_column(String(500), default="")
    url_or_path: Mapped[str] = mapped_column(String(1000), default="")

    # Content (JSON for complex structured data)
    ui_elements_json: Mapped[list] = mapped_column(
        JSON, default=list, comment="Detected UI elements [{element_type, text, bbox, confidence}]"
    )
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    tables_json: Mapped[list] = mapped_column(JSON, default=list)
    state_changes_json: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")

    # Quality
    ocr_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_keyframe: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_visual_frames_session", "session_id", "frame_index"),
        Index("ix_visual_frames_tenant", "tenant_id", "session_id"),
        Index("ix_visual_frames_video", "video_id"),
    )


# ─── Media: Processing Jobs ───────────────────────────────────

class MediaProcessingJobRow(Base):
    """
    Unified media processing job tracking.

    Persists job lifecycle in PostgreSQL alongside Redis JobStore.
    Enables querying job history, analytics, and crash recovery.
    """
    __tablename__ = "media_processing_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="audio_transcription, video_analysis, screenshot_analysis",
    )
    status: Mapped[str] = mapped_column(
        String(30), default="queued",
        comment="queued, preprocessing, processing, aligning, completed, failed, cancelled",
    )

    # Input reference
    source_file_path: Mapped[str] = mapped_column(String(1000), default="")
    original_filename: Mapped[str] = mapped_column(String(500), default="")

    # Processing parameters
    language: Mapped[str] = mapped_column(String(10), default="en")
    num_speakers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parameters_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Progress
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0)
    current_stage: Mapped[str] = mapped_column(String(50), default="queued")
    pipeline_stages: Mapped[list] = mapped_column(JSON, default=list)

    # Result summary (full result stays in Redis/event for performance)
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    speaker_count: Mapped[int] = mapped_column(Integer, default=0)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)

    # Error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("ix_media_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_media_jobs_session", "session_id"),
        Index("ix_media_jobs_type", "tenant_id", "job_type"),
    )


# ═══════════════════════════════════════════════════════════════
# QI ENGINEER PORTAL — Phase 7
# Persona-driven, mission-based quality intelligence workflow
# ═══════════════════════════════════════════════════════════════


class PersonaRow(Base):
    """
    AI Persona template.

    System personas are shared across all tenants (tenant_id = '__system__').
    Custom personas are tenant-scoped.
    Each persona defines stage configurations controlling which engines
    are invoked during each mission stage.
    """
    __tablename__ = "personas"

    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="'__system__' for built-in personas, else tenant-scoped",
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    avatar_icon: Mapped[str] = mapped_column(
        String(50), default="brain",
        comment="Lucide icon name for UI rendering",
    )
    system_prompt: Mapped[str] = mapped_column(
        Text, default="",
        comment="LLM system prompt that shapes persona behavior",
    )
    capabilities: Mapped[list] = mapped_column(
        JSON, default=list,
        comment='List of capability strings, e.g. ["rule_extraction","test_generation"]',
    )
    stage_config: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment=(
            "Per-stage engine configuration: "
            '{"1_capture": {"engines": ["ears","eyes","spine","shield"], "auto_advance": false}, ...}'
        ),
    )
    specialty_domains: Mapped[list] = mapped_column(
        JSON, default=list,
        comment='Domains this persona specialises in, e.g. ["compliance","insurance"]',
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    # Relationships
    missions: Mapped[list["MissionRow"]] = relationship(
        back_populates="persona", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_personas_tenant_active", "tenant_id", "is_active"),
        Index("ix_personas_slug", "tenant_id", "slug", unique=True),
    )


class MissionRow(Base):
    """
    A mission is a user's goal-driven work session guided by a persona.

    Missions progress through 5 stages:
      1. Capture   — Gather requirements, domain knowledge, media
      2. Understand — Analyze, extract rules, build knowledge graphs
      3. Strategize — Plan test approach, suggest coverage
      4. Generate   — Create test cases, test data, reports
      5. Validate   — Execute tests, verify compliance, measure confidence

    Status lifecycle: draft → active → paused → completed | failed | cancelled
    """
    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True,
    )
    persona_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("personas.persona_id", ondelete="SET NULL"), nullable=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    objective: Mapped[str] = mapped_column(
        Text, default="",
        comment="Specific mission objective in natural language",
    )
    status: Mapped[str] = mapped_column(
        String(30), default="draft",
        comment="draft, active, paused, completed, failed, cancelled",
    )
    current_stage: Mapped[int] = mapped_column(
        Integer, default=1,
        comment="Current stage number (1-5)",
    )
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    context: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment="Accumulated context passed between stages",
    )
    summary: Mapped[str] = mapped_column(
        Text, default="",
        comment="AI-generated mission summary updated after each stage",
    )
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    persona: Mapped["PersonaRow | None"] = relationship(back_populates="missions")
    stages: Mapped[list["MissionStageRow"]] = relationship(
        back_populates="mission", cascade="all, delete-orphan",
        order_by="MissionStageRow.stage_number",
    )
    artifacts: Mapped[list["MissionArtifactRow"]] = relationship(
        back_populates="mission", cascade="all, delete-orphan",
    )
    messages: Mapped[list["MissionMessageRow"]] = relationship(
        back_populates="mission", cascade="all, delete-orphan",
        order_by="MissionMessageRow.created_at",
    )

    __table_args__ = (
        Index("ix_missions_tenant_status", "tenant_id", "status"),
        Index("ix_missions_tenant_user", "tenant_id", "user_id"),
        Index("ix_missions_persona", "persona_id"),
    )


class MissionStageRow(Base):
    """
    One of 5 stages within a mission.

    Each stage tracks its own status, inputs, outputs, and which engines
    were called. Stages are created when a mission is created (all 5 rows)
    and updated as work proceeds.
    """
    __tablename__ = "mission_stages"

    stage_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    mission_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("missions.mission_id", ondelete="CASCADE"), nullable=False,
    )
    stage_number: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="1=Capture, 2=Understand, 3=Strategize, 4=Generate, 5=Validate",
    )
    stage_type: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="capture, understand, strategize, generate, validate",
    )
    workflow_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Generic orchestrator workflow that executed this stage (provenance & resumability)",
    )
    status: Mapped[str] = mapped_column(
        String(30), default="pending",
        comment="pending, active, completed, skipped, failed",
    )
    inputs: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment="Inputs provided to this stage (file refs, context, etc.)",
    )
    outputs: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment="Outputs produced by this stage (results, summaries)",
    )
    engine_calls: Mapped[list] = mapped_column(
        JSON, default=list,
        comment='Engines invoked: [{"engine":"heart","endpoint":"/extract-rules","status":"ok","duration_ms":1200}]',
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # Relationships
    mission: Mapped["MissionRow"] = relationship(back_populates="stages")
    artifacts: Mapped[list["MissionArtifactRow"]] = relationship(
        back_populates="stage", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_mission_stages_mission", "mission_id", "stage_number", unique=True),
    )


class MissionArtifactRow(Base):
    """
    Artifact produced during a mission stage.

    Types: document, transcript, rules, test_cases, test_data,
           report, graph_snapshot, strategy, execution_results
    """
    __tablename__ = "mission_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    mission_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("missions.mission_id", ondelete="CASCADE"), nullable=False,
    )
    stage_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mission_stages.stage_id", ondelete="CASCADE"), nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="document, transcript, rules, test_cases, test_data, report, graph_snapshot, strategy, execution_results",
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    content_json: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment="Structured artifact content (rules, test cases, etc.)",
    )
    content_text: Mapped[str] = mapped_column(
        Text, default="",
        comment="Plain text or markdown content for documents/reports",
    )
    file_path: Mapped[str] = mapped_column(String(1000), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    item_count: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Number of items (rules extracted, tests generated, etc.)",
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    # Relationships
    mission: Mapped["MissionRow"] = relationship(back_populates="artifacts")
    stage: Mapped["MissionStageRow"] = relationship(back_populates="artifacts")

    __table_args__ = (
        Index("ix_mission_artifacts_mission", "mission_id"),
        Index("ix_mission_artifacts_stage", "stage_id"),
        Index("ix_mission_artifacts_type", "mission_id", "artifact_type"),
    )


class MissionMessageRow(Base):
    """
    Chat message within a mission context.

    Supports the conversational interface where users interact with
    their persona during mission execution. Messages are tagged with
    the stage number they occurred in.
    """
    __tablename__ = "mission_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    mission_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("missions.mission_id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="user, assistant, system",
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    stage_number: Mapped[int] = mapped_column(
        Integer, default=1,
        comment="Stage during which this message was sent",
    )
    content_type: Mapped[str] = mapped_column(
        String(30), default="text",
        comment="text, markdown, json, action",
    )
    action_data: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        comment="Structured data for action messages (stage transitions, file uploads, etc.)",
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    # Relationships
    mission: Mapped["MissionRow"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_mission_messages_mission", "mission_id", "created_at"),
        Index("ix_mission_messages_stage", "mission_id", "stage_number"),
    )


# ─── Canonical Artifact Lifecycle ──────────────────────────────
#
# Official artifact status values (Phase 1.1 contract):
#   pending       — artifact row created, pipeline not started
#   processing    — canonical pipeline actively running
#   completed     — pipeline done, artifact usable
#   failed        — pipeline failed, artifact not usable
#   needs_review  — produced but quality-gate gated
#
# Canonical completion means ALL of:
#   1. artifact exists
#   2. status is terminal (completed | failed | needs_review)
#   3. quality_gate_outcome is set
#   4. provenance (workflow_id, source_type) is attached
#
# Downstream consumers resolve readiness via artifact_id or
# session_id → artifact_id, never from raw upload files.

CANONICAL_STATUS_PENDING = "pending"
CANONICAL_STATUS_PROCESSING = "processing"
CANONICAL_STATUS_COMPLETED = "completed"
CANONICAL_STATUS_FAILED = "failed"
CANONICAL_STATUS_NEEDS_REVIEW = "needs_review"

CANONICAL_TERMINAL_STATUSES = frozenset({
    CANONICAL_STATUS_COMPLETED,
    CANONICAL_STATUS_FAILED,
    CANONICAL_STATUS_NEEDS_REVIEW,
})

# Source types for provenance
SOURCE_TYPE_AUDIO_UPLOAD = "audio_upload"
SOURCE_TYPE_VIDEO_UPLOAD = "video_upload"
SOURCE_TYPE_AUDIO_VIDEO_UPLOAD = "audio_video_upload"
SOURCE_TYPE_LIVE_SESSION = "live_session_finalize"
SOURCE_TYPE_REPLAY_IMPORT = "replay_import"


# ─── Canonical Artifacts ───────────────────────────────────────

class CanonicalArtifactRow(Base):
    """
    Canonical media artifact — the unified output of the canonical
    processing chain.  One row per KT session's processed media.

    The media_fingerprint column enables re-upload deduplication:
    if a user uploads the same media file again, the existing artifact
    is returned instead of reprocessing.

    Lifecycle: pending → processing → persisted → completed | failed | needs_review

    Downstream consumers read artifact_id to get canonical data.
    Workflow state is informational; artifact status is authoritative.
    """
    __tablename__ = "canonical_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    media_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default=CANONICAL_STATUS_PENDING)

    # ── Provenance (Phase 1.1) ─────────────────────────────
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Probe metadata
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Counts
    scene_count: Mapped[int] = mapped_column(Integer, default=0)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)

    # Transcript
    safe_transcript_text: Mapped[str] = mapped_column(Text, default="")

    # Visual
    visual_summary: Mapped[str] = mapped_column(Text, default="")
    application_types_seen: Mapped[list] = mapped_column(JSON, default=list)

    # Quality gate
    brain_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_gate_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # ── Semantic completeness flags (Phase 2.3) ────────────
    has_real_transcript: Mapped[bool] = mapped_column(Boolean, default=False)
    has_visual_semantics: Mapped[bool] = mapped_column(Boolean, default=False)
    semantic_completeness_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Full artifact JSON blob (for downstream consumers)
    full_artifact_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Processing
    processing_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_canonical_artifacts_tenant_session", "tenant_id", "session_id"),
        Index("ix_canonical_artifacts_fingerprint", "media_fingerprint"),
        Index("ix_canonical_artifacts_workflow", "workflow_id"),
    )