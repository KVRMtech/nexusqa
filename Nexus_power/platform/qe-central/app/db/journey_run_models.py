"""ORM rows for Runnable Journeys (alembic ``qec_006`` — Release D).

``JourneyCaseRow`` records which factory test cases exercise which journey,
per crawl artifact (re-crawls mint new artifacts; links are re-derived on
every completion). ``kind='journey_e2e'`` marks THE one adopted end-to-end
case — the journey's runnable form — carrying the business-named
``display_name`` overlay. The frozen factory's own case rows are never
touched.

``JourneyRunRow`` is the journey run ledger: dispatch id (transient runner
job), ingested id (durable evidence/report key, resolved via ci_run_id),
honest status, and the folded-back verdict summary. A run verdict NEVER
mutates the crawl-side journey claim — they are different facts, displayed
side by side.

Schema is managed by Alembic — these classes exist for typed queries (and
``QecBase.metadata.create_all`` in DB-backed tests).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JourneyCaseRow(QecBase):
    """One journey ⇄ test-case link for one crawl artifact."""

    __tablename__ = "journey_cases"

    link_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    journey_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The factory's own case name (read-only mirror for display).
    case_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    #: linked | journey_e2e (the ONE adopted end-to-end runnable form).
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="linked")
    #: Business-named overlay ("Verify <business_name> end to end") — F5 law;
    #: the frozen factory's case name is untouched.
    display_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    #: Percent (0-100) of the journey's walked node URLs the case's steps
    #: cover — the deterministic adoption score.
    coverage_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_journey_cases_identity", "tenant_id", "journey_id",
              "artifact_id", "test_case_id", unique=True),
        Index("ix_journey_cases_journey", "tenant_id", "app_id", "journey_id"),
    )


class JourneyRunRow(QecBase):
    """One journey-dispatched execution, honestly statused end to end."""

    __tablename__ = "journey_runs"

    journey_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    journey_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ingested_run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: dispatched | running | passed | failed | timed_out | error | blocked
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="dispatched")
    blocked_reason: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    #: noVNC viewer address while the run is in flight (headed run-live path);
    #: transient — the runner tears the session down shortly after the run.
    live_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    env_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    identity_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    verdict_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_journey_runs_dispatch", "tenant_id", "dispatch_run_id"),
        Index("ix_journey_runs_journey", "tenant_id", "app_id", "journey_id"),
    )


__all__ = ["JourneyCaseRow", "JourneyRunRow"]
