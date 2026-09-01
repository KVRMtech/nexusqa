"""
SQLAlchemy models backing the workflow tables.

Imported lazily by WorkflowManager. Kept in its own module so a worker
that uses the in-memory dispatch path doesn't pay the SQLAlchemy import.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
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


class WorkflowStateRow(Base):
    """Durable workflow state. One row per workflow."""

    __tablename__ = "workflow_state"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")

    plan: Mapped[dict] = mapped_column(JSON, nullable=False)
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    current_step: Mapped[str | None] = mapped_column(String, nullable=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSON, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    history: Mapped[list["WorkflowStepHistoryRow"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    __table_args__ = (
        # Sweeper scans live workflows by deadline; this composite index
        # serves the "running workflows past deadline" query directly.
        Index("ix_workflow_state_status_deadline", "status", "deadline_at"),
        # Heartbeat staleness scan.
        Index("ix_workflow_state_status_heartbeat", "status", "last_heartbeat"),
    )


class WorkflowStepHistoryRow(Base):
    """Append-only history of every step attempt. Used for DLQ payloads + audit."""

    __tablename__ = "workflow_step_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workflow_state.workflow_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_name: Mapped[str] = mapped_column(String, nullable=False)
    engine: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # started|completed|failed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    workflow: Mapped["WorkflowStateRow"] = relationship(back_populates="history")
