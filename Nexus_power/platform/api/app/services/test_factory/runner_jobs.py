"""Durable runner-job registry — so a live run / heal / auto-heal survives a
platform-api restart or a second worker.

The in-memory ``_RUNNER_JOBS`` dict stays the fast path; this is the durable
backstop. We mirror a job at CREATE and whenever it reaches a TERMINAL / heal-
decided state, and the status endpoint falls back to this table when the
in-memory entry is gone (restart, or the poll landed on another worker). That
closes the 'green re-run happened but the version was never saved / the status
poll returns unknown' orphaning.

ORM defined in app code (binds the SDK ``Base``); the table is created out-of-band
by an idempotent RLS migration (``scripts/apply_runner_jobs.sql``). Mirrors the
surface_prefs / script_versions pattern. Every helper is best-effort and
tenant-scoped; a missing table (pre-migration) is swallowed so the in-memory path
is unaffected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base
from ...database import tenant_scoped_session

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class E2ERunnerJobRow(Base):
    """One runner job (live run / heal / auto-heal), tenant-scoped. ``job_json``
    holds the same shape the UI polls (status, heal fields, output, etc.)."""

    __tablename__ = "e2e_runner_jobs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="run")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    job_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False,
    )


def _json_safe(job: dict) -> dict:
    """The status endpoint only emits JSON-native values; coerce defensively so a
    stray datetime/None never breaks the JSONB write."""
    out: dict[str, Any] = {}
    for k, v in (job or {}).items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


async def persist_job(
    *, tenant_id: str, run_id: str, artifact_id: str, kind: str, job: dict,
) -> None:
    """Best-effort upsert of the current job state. Never raises — a missing table
    (pre-migration) or any DB error is logged and swallowed so the in-memory path
    is unaffected."""
    if not (tenant_id and run_id):
        return
    try:
        async with tenant_scoped_session(tenant_id) as session:
            stmt = (
                pg_insert(E2ERunnerJobRow)
                .values(
                    run_id=run_id, tenant_id=tenant_id,
                    artifact_id=(artifact_id or "")[:64], kind=(kind or "run")[:32],
                    status=str(job.get("status") or "running")[:32],
                    job_json=_json_safe(job), updated_at=_utc_now(),
                )
                .on_conflict_do_update(
                    index_elements=[E2ERunnerJobRow.run_id],
                    set_={
                        "status": str(job.get("status") or "running")[:32],
                        "job_json": _json_safe(job),
                        "updated_at": _utc_now(),
                    },
                )
            )
            await session.execute(stmt)
    except Exception as exc:  # pre-migration / DB error — durability is optional
        logger.debug("runner_jobs.persist_skipped run=%s err=%s", run_id, exc)


async def get_job(*, tenant_id: str, run_id: str, artifact_id: str = "") -> dict | None:
    """Durable fallback for the status endpoint when the in-memory job is gone
    (restart / other worker). None if absent or pre-migration."""
    if not (tenant_id and run_id):
        return None
    try:
        async with tenant_scoped_session(tenant_id) as session:
            row = (await session.execute(
                select(E2ERunnerJobRow).where(
                    E2ERunnerJobRow.run_id == run_id,
                    E2ERunnerJobRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
    except Exception:
        return None
    if row is None:
        return None
    if artifact_id and row.artifact_id and row.artifact_id != artifact_id:
        return None
    return dict(row.job_json or {})


async def list_heal_jobs(*, tenant_id: str, artifact_id: str) -> list[dict]:
    """All persisted heal / auto-heal jobs for an artifact (tenant-scoped,
    best-effort). Returns each job's ``job_json`` dict; empty list if absent or
    pre-migration. Read-only — used by the Grounded-Oracle scorecard's
    heal-integrity proxy. Opens its own session so a missing table can never
    abort the caller's transaction."""
    if not (tenant_id and artifact_id):
        return []
    try:
        async with tenant_scoped_session(tenant_id) as session:
            rows = (await session.execute(
                select(E2ERunnerJobRow).where(
                    E2ERunnerJobRow.tenant_id == tenant_id,
                    E2ERunnerJobRow.artifact_id == artifact_id,
                    E2ERunnerJobRow.kind.in_(("heal", "auto-heal")),
                )
            )).scalars().all()
    except Exception:  # pre-migration / DB error — proxy degrades to empty
        return []
    # Merge the authoritative `kind` column over job_json so the consumer can
    # always distinguish single-step heal from the auto-heal loop.
    return [{**(r.job_json or {}), "kind": r.kind} for r in rows]


__all__ = ["E2ERunnerJobRow", "persist_job", "get_job", "list_heal_jobs"]
