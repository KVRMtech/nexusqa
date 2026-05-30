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

    # Phase 10 evidence anchors — every step can cite the exact scene/control/edge
    evidence_scene_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    evidence_control_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    evidence_edge_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_flow_edges.edge_id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    proof_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_mode: Mapped[str] = mapped_column(String(20), default="multimodal")

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

    # Phase 2: relative HTTP-servable asset path (e.g. "{tenant}/{session}/{job}_frames/frame_00001.png")
    # Eyes sets this; canonical orchestrator writes it to the DB.  Never contains a hostname.
    frame_asset_path: Mapped[str] = mapped_column(String(1000), default="")

    # Phase 2: back-reference to the canonical artifact that owns this frame
    artifact_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

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

    # Phase 3–5 evidence linking (nullable until those phases populate them)
    app_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    segmentation_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_visual_frames_session", "session_id", "frame_index"),
        Index("ix_visual_frames_tenant", "tenant_id", "session_id"),
        Index("ix_visual_frames_video", "video_id"),
        Index("ix_visual_frames_artifact", "artifact_id", "frame_index"),
        Index("ix_visual_frames_app_instance", "app_instance_id"),
        Index("ix_visual_frames_scene", "scene_id"),
    )


# ─── Visual: Scenes (Phase 3) ────────────────────────────────

class VisualSceneRow(Base):
    """
    Stable logical scene — a group of visually similar consecutive frames.

    Scenes are the primary unit for evidence organisation.  App grouping
    (Phase 5) and control extraction (Phase 6) operate on scenes, not
    raw frames.  scene_id is stable across pipeline retries because it
    is derived deterministically (UUID generated once, then written via
    ON CONFLICT DO NOTHING).
    """
    __tablename__ = "visual_scenes"

    scene_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # The frame inside this scene with the highest OCR confidence
    representative_frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
        nullable=True,
    )

    scene_index: Mapped[int] = mapped_column(Integer, default=0)
    screen_name: Mapped[str] = mapped_column(String(500), default="")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    detected_url: Mapped[str] = mapped_column(String(2000), default="")
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)

    # Populated in Phase 5
    app_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Populated in Phase 5b (flow segmenter)
    flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 1.0 if LLaVA ran on representative frame, 0.5 if dHash only
    completeness_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Phase 8 — structured state summary and quality fields
    # scene_state_summary: JSON with screen_type, screen_title, application_label,
    #   domain, page_key, state_signals, state_confidence, state_quality, etc.
    scene_state_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Explicit duration stored alongside quality so UI can decide whether to show it
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # exact | estimated | unknown
    duration_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    # strong | degraded | weak — top-level gate for UI rendering
    scene_quality: Mapped[str] = mapped_column(String(20), default="weak")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_visual_scenes_artifact", "artifact_id"),
        Index("ix_visual_scenes_tenant_session", "tenant_id", "session_id"),
        Index("ix_visual_scenes_app_instance", "app_instance_id"),
        Index("ix_visual_scenes_flow", "flow_id"),
    )


# ─── App Instances (Phase 5) ─────────────────────────────────

class AppInstanceRow(Base):
    """
    Confidence-scored application grouping over scenes.

    Every grouping carries segmentation_basis and segmentation_confidence.
    No field is labeled as absolute truth — naming uses detected_* throughout.
    """
    __tablename__ = "app_instances"

    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    app_type: Mapped[str] = mapped_column(String(50), default="unknown")
    app_name: Mapped[str] = mapped_column(String(500), default="")
    detected_url: Mapped[str] = mapped_column(String(2000), default="")
    window_title: Mapped[str] = mapped_column(String(500), default="")

    # "app_type_change" | "domain_change" | "window_title_change" | "no_boundary"
    segmentation_basis: Mapped[str] = mapped_column(String(50), default="no_boundary")
    segmentation_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    first_scene_index: Mapped[int] = mapped_column(Integer, default=0)
    last_scene_index: Mapped[int] = mapped_column(Integer, default=0)
    scene_count: Mapped[int] = mapped_column(Integer, default=0)

    # Future operator override hook — not used in automated pipeline
    operator_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_app_instances_artifact", "artifact_id"),
        Index("ix_app_instances_tenant_session", "tenant_id", "session_id"),
    )


# ─── Evidence Controls (Phase 6) ─────────────────────────────

class EvidenceControlRow(Base):
    """
    Individual UI control extracted from a scene's representative frame.

    automation_ready is True only when the selector is grounded in OCR-
    confirmed text (selector_source in {"ocr", "hybrid"}).  Vision-only
    controls always have automation_ready=False and playwright_selector="".
    """
    __tablename__ = "evidence_controls"

    control_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    # nullable so rows can be written before frame FK is resolved
    frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="CASCADE"),
        nullable=True,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    element_type: Mapped[str] = mapped_column(String(50), nullable=False)
    label_text: Mapped[str] = mapped_column(String(500), default="")
    value_text: Mapped[str] = mapped_column(String(500), default="")
    action_kind: Mapped[str] = mapped_column(String(50), default="", server_default="")
    display_label: Mapped[str] = mapped_column(String(600), default="", server_default="")
    observed_value: Mapped[str] = mapped_column(String(500), default="", server_default="")
    bounding_box: Mapped[dict] = mapped_column(JSON, default=dict)

    # "ocr" | "vision" | "hybrid" | "unknown"
    selector_source: Mapped[str] = mapped_column(String(20), default="unknown")
    playwright_selector: Mapped[str] = mapped_column(String(1000), default="")
    selector_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    automation_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_evidence_controls_scene", "scene_id"),
        Index("ix_evidence_controls_artifact", "artifact_id"),
    )


# ─── Evidence Steps (Phase C — first-class user actions) ─────

class EvidenceStepRow(Base):
    """One observable user action inside a scene.

    Produced by the triangulated action classifier (Phase B.3) which combines
    OCR diff, cursor click, LLaVA delta description, and audio intent into a
    single step record.  Downstream test_case_steps and automation
    generators consume this table directly — no more parsing
    ``scene_state_summary.frame_actions``.

    The frame anchors (before_frame_id / after_frame_id) let the frontend
    render before/after thumbnails and let the automation generator reason
    about precondition/postcondition.
    """
    __tablename__ = "evidence_steps"

    step_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Canonical action vocabulary — must match control_extractor's verbs so
    # generated test steps can reference both worlds with one taxonomy.
    action_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_label: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    observed_value: Mapped[str] = mapped_column(String(2000), default="", nullable=False)

    trigger_control_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
        nullable=True,
    )
    before_frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
        nullable=True,
    )
    after_frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
        nullable=True,
    )

    start_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 0..1 overall classifier confidence in this step.
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 0..1 fraction of independent signals (OCR / cursor / LLaVA / audio)
    # that agreed.  Used by the frontend to render trust at a glance.
    agreement_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # JSON list of the contributing signals.  Possible values:
    #   ocr_diff, cursor_click, cursor_hover, llava_delta, audio_intent,
    #   ui_element_added, ui_element_removed, frame_captured.
    evidence_signals: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    cursor_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_y: Mapped[int | None] = mapped_column(Integer, nullable=True)

    audio_intent_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    audio_intent_ts_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_evidence_steps_artifact", "artifact_id"),
        Index("ix_evidence_steps_scene_index", "scene_id", "step_index"),
        Index("ix_evidence_steps_tenant_session", "tenant_id", "session_id"),
    )


# ─── Cursor Events (Phase A.3 — per-frame cursor tracking) ───

class CursorEventRow(Base):
    """Per-frame cursor (x, y) and click/drag flags.

    Stored separately from :class:`EvidenceStepRow` because one user action
    can span multiple cursor events (mouse-move → click → release) and
    because cursor data is independently useful for replay, hover detection,
    and gesture analysis.
    """
    __tablename__ = "cursor_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
        nullable=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    frame_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    cursor_x: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor_y: Mapped[int] = mapped_column(Integer, nullable=False)
    velocity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    is_click: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_drag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # "motion" | "template" | "ml" — lets the classifier weight detection
    # methods differently when triangulating.
    detection_method: Mapped[str] = mapped_column(
        String(32), default="motion", nullable=False,
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_cursor_events_artifact", "artifact_id"),
        Index("ix_cursor_events_frame", "frame_id"),
        Index("ix_cursor_events_tenant_session", "tenant_id", "session_id"),
    )


# ─── UI Dictionary (Phase F.1 — knowledge accumulation) ──────

class UIDictionaryEntryRow(Base):
    """Per-tenant library of recognised UI controls.

    The dictionary is the pipeline's long-term memory: each successful
    extraction lands here and the next recording on the same page reuses
    the prior selector / action_kind / label rather than re-deriving from
    scratch.  Successful Playwright matches at automation time bump
    ``selector_confidence``; failures decrement it.

    Identity is the ``element_signature`` — a deterministic uuid5 over
    (page_key, normalised element_type, normalised label_text).  Two
    captures of the same control on the same page produce the same
    signature so the upsert path always finds the existing row.
    """
    __tablename__ = "ui_dictionary_entries"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    element_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    page_key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    domain: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    element_type: Mapped[str] = mapped_column(String(50), nullable=False)
    label_text: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    display_label: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    action_kind: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    preferred_selector: Mapped[str] = mapped_column(
        String(2000), default="", nullable=False,
    )
    selector_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False,
    )
    # "ocr" | "vision" | "hybrid" | "unknown" — same vocabulary as
    # EvidenceControlRow.selector_source.
    selector_source: Mapped[str] = mapped_column(
        String(20), default="unknown", nullable=False,
    )

    recognition_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    automation_success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    automation_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    bbox_centre_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_centre_y: Mapped[int | None] = mapped_column(Integer, nullable=True)

    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_ui_dictionary_tenant_page", "tenant_id", "page_key"),
        Index("ix_ui_dictionary_tenant_domain", "tenant_id", "domain"),
    )


# ─── Visual Flow Edges (Phase 7) ─────────────────────────────

class VisualFlowEdgeRow(Base):
    """
    Directed transition between two scenes.

    Layer 1 (observed_transition): chronological fact — scene A was followed
    by scene B.  evidence_confidence = 1.0.

    Layer 2 (action_confirmed_transition): a trigger control or action verb
    was identified.  evidence_confidence varies by signal strength.
    """
    __tablename__ = "visual_flow_edges"

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    from_scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    to_scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )

    # "observed_transition" | "action_confirmed_transition"
    edge_type: Mapped[str] = mapped_column(String(40), default="observed_transition")

    # null for Layer 1 edges; populated by Layer 2 when a control is linked
    trigger_control_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
        nullable=True,
    )

    # "transition" | "click" | "navigate" | "type" | "select" | "submit" | "press"
    action_type: Mapped[str] = mapped_column(String(50), default="transition")
    action_value: Mapped[str] = mapped_column(String(500), default="")

    transition_ms: Mapped[int] = mapped_column(Integer, default=0)
    intra_app: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_confidence: Mapped[float] = mapped_column(Float, default=1.0)

    # Populated in Phase 5b (flow segmenter)
    flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Phase 8 — structured action summary and quality fields
    # primary_action_summary: JSON with action_kind, action_label, evidence_sources,
    #   action_quality, action_confidence, fallback_used, etc.
    primary_action_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # strong | degraded | weak — mirrors scene_quality for edges
    action_quality: Mapped[str] = mapped_column(String(20), default="weak")
    # 0.0–1.0 numeric confidence for the action reconstruction
    action_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_visual_flow_edges_artifact", "artifact_id"),
        Index("ix_visual_flow_edges_scenes", "from_scene_id", "to_scene_id"),
        Index("ix_visual_flow_edges_flow", "flow_id"),
    )


# ─── Visual Flows (Phase 5b) ─────────────────────────────────

class VisualFlowRow(Base):
    """
    Business-process flow — a contiguous group of scenes representing
    one testable journey (e.g., \"Guardian Life Insurance Quote\").

    Each flow carries a human-readable flow_label, primary domain, and
    noise flag.  flow_id is deterministic (uuid5) for idempotent upserts.
    """
    __tablename__ = "visual_flows"

    flow_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    flow_index: Mapped[int] = mapped_column(Integer, default=0)
    flow_label: Mapped[str] = mapped_column(String(500), default="")
    domain: Mapped[str] = mapped_column(String(500), default="")
    app_type: Mapped[str] = mapped_column(String(50), default="unknown")

    first_scene_index: Mapped[int] = mapped_column(Integer, default=0)
    last_scene_index: Mapped[int] = mapped_column(Integer, default=0)
    scene_count: Mapped[int] = mapped_column(Integer, default=0)

    is_noise: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # True when user returned to this app mid-session (interleaved multi-app KT)
    is_interleaved: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Number of distinct time-contiguous visit segments (> 1 means user left and came back)
    visit_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_visual_flows_artifact", "artifact_id"),
        Index("ix_visual_flows_tenant_session", "tenant_id", "session_id"),
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


# ─── E2E Scenario Lifecycle State (P3) ─────────────────────────

# Official lifecycle states for an E2E scenario.
#   draft      — newly generated; not yet reviewed
#   reviewed   — a reviewer has looked but not approved/rejected
#   approved   — green-lit for automation/export
#   rejected   — explicitly rejected (kept for audit, hidden by default)
#   automated  — code generated and committed
#   live       — automated and currently passing in CI
#   stable     — has been live + passing for a sustained period
#   failing    — automated but currently failing
E2E_STATE_DRAFT = "draft"
E2E_STATE_REVIEWED = "reviewed"
E2E_STATE_APPROVED = "approved"
E2E_STATE_REJECTED = "rejected"
E2E_STATE_AUTOMATED = "automated"
E2E_STATE_LIVE = "live"
E2E_STATE_STABLE = "stable"
E2E_STATE_FAILING = "failing"

E2E_LIFECYCLE_STATES = frozenset({
    E2E_STATE_DRAFT,
    E2E_STATE_REVIEWED,
    E2E_STATE_APPROVED,
    E2E_STATE_REJECTED,
    E2E_STATE_AUTOMATED,
    E2E_STATE_LIVE,
    E2E_STATE_STABLE,
    E2E_STATE_FAILING,
})

# States that gate the Playwright export — only these are exported by default.
E2E_EXPORTABLE_STATES = frozenset({
    E2E_STATE_APPROVED,
    E2E_STATE_AUTOMATED,
    E2E_STATE_LIVE,
    E2E_STATE_STABLE,
})


class E2EScenarioStateRow(Base):
    """Per-scenario lifecycle state, comments, and audit log (P3).

    Scenarios themselves live in the canonical artifact's
    ``e2e_architect_cache`` JSON blob; this table is the durable layer that
    survives cache regenerations.  scenario_id is the deterministic ID
    assigned by the E2E architect endpoint (e.g. ``vs_001``).

    State rows are created lazily on first transition.  A scenario without
    a row is implicitly in ``draft`` state.  Rows whose scenario_id no
    longer exists after regeneration are preserved (for audit) but
    ignored by the response merge.
    """
    __tablename__ = "e2e_scenario_state"

    state_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Deterministic scenario_id from the E2E architect output (no FK — scenarios
    # live in the artifact's JSON cache, not a relational table).
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    state: Mapped[str] = mapped_column(String(32), default=E2E_STATE_DRAFT, nullable=False)
    state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    state_changed_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    state_changed_by_email: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    # comments_json: list of {comment_id, user_id, email, body, created_at}
    comments_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # audit_log_json: list of {from_state, to_state, user_id, email, at, note}
    audit_log_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_e2e_scenario_state_artifact_scenario",
            "artifact_id", "scenario_id",
            unique=True,
        ),
        Index("ix_e2e_scenario_state_tenant_state", "tenant_id", "state"),
        Index("ix_e2e_scenario_state_artifact", "artifact_id"),
    )


# ─── E2E Execution Feedback Loop (P6) ──────────────────────────

# Run-level status values
E2E_RUN_STATUS_RUNNING = "running"
E2E_RUN_STATUS_PASSED = "passed"
E2E_RUN_STATUS_FAILED = "failed"
E2E_RUN_STATUS_ERROR = "error"
E2E_RUN_STATUS_CANCELLED = "cancelled"

E2E_RUN_TERMINAL_STATUSES = frozenset({
    E2E_RUN_STATUS_PASSED,
    E2E_RUN_STATUS_FAILED,
    E2E_RUN_STATUS_ERROR,
    E2E_RUN_STATUS_CANCELLED,
})

# Step-level status values
E2E_STEP_STATUS_PASSED = "passed"
E2E_STEP_STATUS_FAILED = "failed"
E2E_STEP_STATUS_SKIPPED = "skipped"
E2E_STEP_STATUS_TIMED_OUT = "timed_out"
E2E_STEP_STATUS_BROKEN = "broken"

E2E_STEP_TERMINAL_STATUSES = frozenset({
    E2E_STEP_STATUS_PASSED,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_SKIPPED,
    E2E_STEP_STATUS_TIMED_OUT,
    E2E_STEP_STATUS_BROKEN,
})


class E2ETestRunRow(Base):
    """One CI execution of the tests generated from a visual_strict artifact.

    Distinct from the legacy :class:`TestRunRow` (table ``test_runs``,
    plural).  This row tracks a single Playwright/Cypress run posted by
    CI to ``POST /api/v1/test-runs/ingest`` and is the source of every
    "Last run" badge in the Test Studio.
    """
    __tablename__ = "test_run"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    ci_run_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    ci_commit_sha: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    ci_pipeline_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    environment: Mapped[str] = mapped_column(String(64), default="ci", nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=E2E_RUN_STATUS_RUNNING, nullable=False)
    total_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_test_run_artifact_started", "artifact_id", "started_at"),
        Index("ix_test_run_tenant_started", "tenant_id", "started_at"),
        Index("ix_test_run_ci_run_id", "tenant_id", "ci_run_id"),
    )


class E2ETestRunStepRow(Base):
    """One executed step inside an E2ETestRunRow.

    ``scenario_id`` is the deterministic scenario id we emit in test names
    (e.g. ``vs_001``), parsed back out of CI's test-name string by the
    ingest webhook.  Drift detection compares ``resolved_selector`` +
    ``resolved_bbox_json`` against the live visual evidence graph at
    serve-time.
    """
    __tablename__ = "test_run_step"

    step_run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("test_run.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=E2E_STEP_STATUS_PASSED, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    expected_selector: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    resolved_selector: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    resolved_bbox_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    evidence_scene_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    evidence_control_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    evidence_edge_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    screenshot_url: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_test_run_step_run", "run_id"),
        Index("ix_test_run_step_scenario", "artifact_id", "scenario_id", "step_number"),
        Index("ix_test_run_step_tenant", "tenant_id"),
    )


# ─── Storyboard Phase 1 — Additive Derivations ────────────────
#
# The following four tables are populated post-pipeline by the
# storyboard composer service.  They never replace pipeline-owned
# tables; they cache derived projections that turn the raw
# visual-evidence graph into a picture-first storyboard the user
# can browse, share and export.
#
# All four follow the v1 conventions used throughout this file:
# tenant_id on every row (for RLS migration 030_storyboard_phase1),
# deterministic uuid5 primary keys (idempotent UPSERT), and version
# columns so re-derivation does not require a schema change.


class AppGroupingRow(Base):
    """
    Deduplicated application instance.

    The pipeline emits one ``AppInstanceRow`` per detected boundary, which
    means OCR-corrupted window titles ("Wivwquardianlife.com",
    "wwwquardianlife.com") get filed as separate apps.  The app deduper
    folds these into one canonical grouping keyed by registered domain
    (or normalized window title for desktop apps), so the storyboard and
    BPMN swimlanes show one lane per real application.
    """

    __tablename__ = "app_groupings"

    grouping_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)

    canonical_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    canonical_domain: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    app_type: Mapped[str] = mapped_column(String(50), default="unknown", nullable=False)
    display_label: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    member_instance_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    total_scene_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_scene_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_scene_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    dedup_basis: Mapped[str] = mapped_column(String(50), default="single_instance", nullable=False)
    dedup_evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    deduper_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_app_groupings_artifact", "artifact_id"),
        Index("ix_app_groupings_tenant_session", "tenant_id", "session_id"),
        Index("ix_app_groupings_canonical_domain", "tenant_id", "canonical_domain"),
    )


class StoryboardPanelRow(Base):
    """
    A single comic-strip panel in the storyboard.

    Replaces ``visual_scenes`` as the primary unit the storyboard UI
    renders.  Each panel collapses one or more adjacent same-screen
    scenes (so 50 form-fill scenes on the same Quote Form become one
    panel with ``in_scene_action_count = 50``).
    """

    __tablename__ = "storyboard_panels"

    panel_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)

    panel_index: Mapped[int] = mapped_column(Integer, nullable=False)
    first_scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    last_scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    app_grouping_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("app_groupings.grouping_id", ondelete="SET NULL"),
        nullable=True,
    )

    representative_frame_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
        nullable=True,
    )
    representative_frame_asset_path: Mapped[str] = mapped_column(
        String(1000), default="", nullable=False,
    )

    start_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    caption_short: Mapped[str] = mapped_column(Text, default="", nullable=False)
    caption_long: Mapped[str] = mapped_column(Text, default="", nullable=False)

    in_scene_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    panel_quality: Mapped[str] = mapped_column(String(20), default="weak", nullable=False)
    completeness_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False,
    )

    is_noise: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    noise_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    grouper_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    grouper_signals: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_storyboard_panels_artifact_panel", "artifact_id", "panel_index"),
        Index("ix_storyboard_panels_tenant_session", "tenant_id", "session_id"),
        Index("ix_storyboard_panels_app_grouping", "app_grouping_id"),
    )


class SceneCaptionRow(Base):
    """
    Short imperative caption per scene, generated by the caption rewriter.

    The pipeline stores verbose LLaVA descriptions on ``visual_frames``
    ("The interface displays a form with multiple input fields...").
    Those are useful as detector input but not as user-facing captions.
    This table stores 5-8 word imperative captions ("Filled email field",
    "Submitted quote form") plus an optional fuller narrative for the
    drill-down view.

    Captions are keyed by (scene_id, generator_version) so bumping the
    version forces re-generation without dropping existing rows.
    """

    __tablename__ = "scene_captions"

    caption_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    short_caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    narrative_caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    caption_quality: Mapped[str] = mapped_column(String(20), default="weak", nullable=False)

    generator_model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    generator_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    input_signals: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    generation_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_scene_captions_artifact", "artifact_id"),
        Index("ix_scene_captions_tenant", "tenant_id"),
    )


class AnnotatedFrameCacheRow(Base):
    """
    Cached metadata for a server-rendered annotated PNG.

    The frame annotator service composites cursor markers, click points,
    OCR bounding boxes and a short caption banner onto the raw frame
    image and writes the result to the same object store as the raw
    frame, under an ``annotated/v{version}/`` prefix.

    This row records the asset path, dimensions, signal manifest and
    render latency.  Re-rendering is triggered by bumping
    ``annotation_version``; the new asset path includes the version, so
    stale PNGs remain available until garbage-collected.
    """

    __tablename__ = "annotated_frame_cache"

    cache_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    frame_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_frames.frame_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    annotation_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    asset_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), default="image/png", nullable=False)

    width_px: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    annotation_signals: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    render_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_annotated_frame_cache_artifact", "artifact_id"),
        Index("ix_annotated_frame_cache_tenant", "tenant_id"),
    )


class SceneActionRow(Base):
    """
    Multimodal-LLM-extracted semantic user action per scene.

    The canonical pipeline's frame-action extractor (OCR-text-diff)
    produces signals like "text 'TX' appeared on the page" but cannot
    interpret that as "user selected TX from a State dropdown".  That
    semantic interpretation is what this table stores — one row per
    scene with the LLM's structured view of what the user did:

        verb=select, target_label="What state do you live in?",
        target_kind=dropdown, value="TX",
        confidence=0.95, automation_ready=True,
        reasoning="Dropdown showed 'TX' in After frame after being
                   blank in Before frame."

    Actions are keyed by (scene_id, extractor_version) so bumping
    the version forces re-extraction without dropping existing rows.

    Populated by ``platform/api/app/services/storyboard/action_extractor.py``;
    consumed by the visual-flow API response, the 3D Journey bottom
    detail panel, the test exporters (Cypress/Playwright/Gherkin), and
    the quality gate (a new ``action_evidence_coverage`` signal).

    See migration 031_scene_actions for the DDL.  See
    ``memory/action_capture_extraction_limit.md`` for why frame-diff
    extraction is insufficient and this LLM layer is needed.
    """

    __tablename__ = "scene_actions"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Ordinal within (scene_id, extractor_version).  0 for single-action
    # scenes (the common case); 0,1,2,...,N-1 for long-form-fill scenes
    # where the LLM extracted multiple distinct actions.  See migration
    # 033_scene_actions_subaction_index.
    subaction_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Structured action shape (see schemas.SceneAction Pydantic model).
    verb: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    target_label: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    target_kind: Mapped[str] = mapped_column(String(20), default="other", nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    automation_ready: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    reasoning: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    # Reconciliation: which existing pipeline signals corroborated the LLM
    # output.  Keys include ocr_text_match, control_match, cursor_event_match,
    # url_changed, audio_intent_match, sources (list).
    evidence_signals: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Provenance + cost tracking.
    extractor_model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    generation_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_scene_actions_artifact", "artifact_id"),
        Index("ix_scene_actions_tenant", "tenant_id"),
        Index("ix_scene_actions_verb_kind", "tenant_id", "verb", "target_kind"),
    )


class PageVisitRow(Base):
    """
    URL-keyed chronological log of distinct pages the user visited.

    Phase 2.0 — independent of scene boundaries.  The canonical
    pipeline's ``scene_grouper`` merges visually-similar consecutive
    frames; for QA test generation we need every distinct URL captured
    so generated tests can navigate to each one.

    Each row represents one contiguous visit to a single ``location``
    (URL for web, window_title for desktop, screen_name for native).
    A user who visited ``/state`` → ``/gender`` → ``/state`` produces
    three rows because each is a separate contiguous visit; the
    sequence_index preserves order.

    Populated by ``platform/api/app/services/storyboard/page_visit_extractor.py``
    via URL regex over ``visual_frames.extracted_text`` (with native-UI
    fallback to window_title / screen_name).  Consumed by
    ``page_action_extractor`` and ``form_snapshot_extractor`` and by
    the test exporters.

    See migration 034_page_visits for the DDL.
    """

    __tablename__ = "page_visits"

    page_visit_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_new_id,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)

    location: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    url_host: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    url_path: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    url_query: Mapped[str] = mapped_column(String(2000), default="", nullable=False)
    canonical_host: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    first_seen_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frame_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # url_regex | url_scene | window_title | screen_name_ocr | llm_inferred
    source: Mapped[str] = mapped_column(String(40), default="url_regex", nullable=False)
    extraction_confidence: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False,
    )

    primary_scene_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("visual_scenes.scene_id", ondelete="SET NULL"),
        nullable=True,
    )

    form_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    form_snapshot_signals: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False,
    )

    extractor_version: Mapped[str] = mapped_column(
        String(50), default="v1", nullable=False,
    )
    form_snapshot_extractor_version: Mapped[str] = mapped_column(
        String(50), default="", nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_page_visits_artifact_sequence", "artifact_id", "sequence_index"),
        Index("ix_page_visits_tenant_session", "tenant_id", "session_id"),
        Index("ix_page_visits_canonical_host", "tenant_id", "canonical_host"),
    )


class PageActionRow(Base):
    """
    Semantic user actions keyed by page_visit instead of scene.

    Phase 2.1 — schema parity with SceneActionRow but parented by
    ``page_visit_id``.  Lets the action_extractor emit one or more
    rows per URL visited rather than per (merged) scene.  When a
    long-form-fill page (e.g. USAA `/personal-information/birthdate`)
    contained 3 distinct actions, this table holds 3 rows for that
    page_visit_id distinguished by ``subaction_index``.

    Consumed by test exporters (Cypress / Playwright / Gherkin) which
    iterate page_visits in order and emit one test step per
    page_action.

    See migration 035_page_actions for the DDL.
    """

    __tablename__ = "page_actions"

    page_action_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_new_id,
    )
    page_visit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("page_visits.page_visit_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    subaction_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    verb: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    target_label: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    target_kind: Mapped[str] = mapped_column(String(20), default="other", nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    automation_ready: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    reasoning: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    evidence_signals: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    extractor_model: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    extractor_version: Mapped[str] = mapped_column(
        String(50), default="v1", nullable=False,
    )
    generation_latency_ms: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
    )
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ix_page_actions_artifact", "artifact_id"),
        Index("ix_page_actions_tenant", "tenant_id"),
        Index("ix_page_actions_visit_sub", "page_visit_id", "subaction_index"),
        Index("ix_page_actions_verb_kind", "tenant_id", "verb", "target_kind"),
    )