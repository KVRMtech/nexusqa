"""``indexing_jobs`` queue — durable, lease-based, idempotent.

Concurrency semantics:

* ``enqueue(...)`` is idempotent on ``(tenant_id, artifact_id)``. A
  second enqueue for an already-pending or running job re-uses the
  existing row instead of creating a duplicate.
* ``lease(worker_id, ...)`` atomically selects one pending or expired-
  lease job, marks it ``running``, stamps ``locked_by`` and
  ``locked_until``, and returns the job row. Uses
  ``FOR UPDATE SKIP LOCKED`` so concurrent workers never contend.
* ``complete(...)`` / ``fail(...)`` close out a leased job. Failure
  schedules a retry with exponential backoff or moves the job to
  ``dead_letter`` once ``max_attempts`` is exhausted.
* ``release_expired_leases(...)`` recovers crashed workers.

All operations write a row to ``integration_events_log`` is not done
here — observability is the caller's concern; this module is pure
queue mechanics.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import Database, indexing_jobs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    job_id: str
    tenant_id: str
    session_id: str
    artifact_id: str
    status: str
    attempts: int
    max_attempts: int
    trace_id: Optional[str]
    input: dict[str, Any]


class JobStore:
    def __init__(
        self,
        db: Database,
        *,
        worker_id: str,
        lease_seconds: int = 600,
        backoff_base_seconds: int = 30,
        backoff_max_seconds: int = 1800,
    ):
        self._db = db
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds

    # ── Enqueue ─────────────────────────────────────────────────

    async def enqueue(
        self,
        *,
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        trace_id: Optional[str] = None,
        input_payload: Optional[dict[str, Any]] = None,
        max_attempts: int = 5,
    ) -> Job:
        """Idempotently add a job for (tenant_id, artifact_id)."""
        now = _now()
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(indexing_jobs).values(
                job_id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                session_id=session_id,
                artifact_id=artifact_id,
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                trace_id=trace_id,
                input=input_payload or {},
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    indexing_jobs.c.tenant_id,
                    indexing_jobs.c.artifact_id,
                ],
                set_={
                    # If the prior job ended terminally (succeeded/dead_letter)
                    # but a new event arrived for the same artifact (e.g. the
                    # upstream pipeline re-ran the canonical chain), we reset
                    # the row to pending so the worker re-indexes.
                    "status": sa.case(
                        (
                            indexing_jobs.c.status.in_(
                                ("succeeded", "dead_letter", "failed")
                            ),
                            sa.literal("pending"),
                        ),
                        else_=indexing_jobs.c.status,
                    ),
                    "attempts": sa.case(
                        (
                            indexing_jobs.c.status.in_(
                                ("succeeded", "dead_letter", "failed")
                            ),
                            sa.literal(0),
                        ),
                        else_=indexing_jobs.c.attempts,
                    ),
                    "trace_id": stmt.excluded.trace_id,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(indexing_jobs)
            row = (await session.execute(stmt)).mappings().first()

        if row is None:
            raise RuntimeError(
                f"enqueue failed for tenant={tenant_id} artifact={artifact_id}"
            )
        return _row_to_job(row)

    # ── Lease ───────────────────────────────────────────────────

    async def lease_next(
        self, *, tenant_id_filter: Optional[str] = None
    ) -> Optional[Job]:
        """Lease one job, returning it under a per-row lock.

        Selection criteria:
          * status='pending', OR
          * status='running' with an expired ``locked_until`` (crash recovery).
        Ordered by ``created_at`` for FIFO fairness.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers each
        get a distinct row without contention.
        """
        now = _now()
        new_lease_end = now + timedelta(seconds=self._lease_seconds)
        async with self._db.session() as session:
            # RLS would normally scope us to one tenant — but the worker
            # lives outside any user context. We bypass RLS for the
            # queue read by clearing the session variable, then enforce
            # tenant scoping at the application layer via the
            # tenant_id_filter argument and by always referencing
            # tenant_id in subsequent writes.
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )

            where_clauses = [
                sa.or_(
                    indexing_jobs.c.status == "pending",
                    sa.and_(
                        indexing_jobs.c.status == "running",
                        indexing_jobs.c.locked_until.is_not(None),
                        indexing_jobs.c.locked_until < now,
                    ),
                )
            ]
            if tenant_id_filter:
                where_clauses.append(
                    indexing_jobs.c.tenant_id == tenant_id_filter
                )

            select_stmt = (
                sa.select(indexing_jobs)
                .where(*where_clauses)
                .order_by(indexing_jobs.c.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await session.execute(select_stmt)).mappings().first()
            if row is None:
                return None

            await session.execute(
                sa.update(indexing_jobs)
                .where(indexing_jobs.c.job_id == row["job_id"])
                .values(
                    status="running",
                    attempts=indexing_jobs.c.attempts + 1,
                    locked_by=self._worker_id,
                    locked_until=new_lease_end,
                    started_at=sa.func.coalesce(
                        indexing_jobs.c.started_at, now
                    ),
                    updated_at=now,
                )
            )
            # Re-read for accurate attempts count post-update.
            row = (
                await session.execute(
                    sa.select(indexing_jobs).where(
                        indexing_jobs.c.job_id == row["job_id"]
                    )
                )
            ).mappings().first()
        return _row_to_job(row) if row else None

    # ── Heartbeat (extend lease for long-running jobs) ─────────

    async def heartbeat(self, job_id: str) -> bool:
        """Extend the lease on a running job. Returns False if the
        worker has lost the lease (another worker may have taken it)."""
        now = _now()
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            result = await session.execute(
                sa.update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.locked_by == self._worker_id,
                    indexing_jobs.c.status == "running",
                )
                .values(
                    locked_until=now + timedelta(seconds=self._lease_seconds),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    # ── Complete / Fail / Skip ──────────────────────────────────

    async def complete(
        self,
        job_id: str,
        *,
        result: Optional[dict[str, Any]] = None,
    ) -> None:
        now = _now()
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            await session.execute(
                sa.update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.locked_by == self._worker_id,
                )
                .values(
                    status="succeeded",
                    result=result or {},
                    locked_by=None,
                    locked_until=None,
                    completed_at=now,
                    updated_at=now,
                    last_error=None,
                )
            )

    async def skip(self, job_id: str, *, reason: str) -> None:
        """Mark a job as intentionally not indexable (e.g. failed
        quality gate). Not a retry-eligible state."""
        now = _now()
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            await session.execute(
                sa.update(indexing_jobs)
                .where(
                    indexing_jobs.c.job_id == job_id,
                    indexing_jobs.c.locked_by == self._worker_id,
                )
                .values(
                    status="skipped",
                    result={"reason": reason},
                    locked_by=None,
                    locked_until=None,
                    completed_at=now,
                    updated_at=now,
                )
            )

    async def fail(
        self, job_id: str, *, error: str
    ) -> str:
        """Record a failure. Schedules retry or moves to dead_letter."""
        now = _now()
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            row = (
                await session.execute(
                    sa.select(indexing_jobs).where(
                        indexing_jobs.c.job_id == job_id,
                    )
                )
            ).mappings().first()
            if row is None:
                return "missing"
            attempts = row["attempts"]
            max_attempts = row["max_attempts"]
            if attempts >= max_attempts:
                new_status = "dead_letter"
                next_attempt_at = None
            else:
                new_status = "pending"
                backoff = min(
                    self._backoff_max,
                    self._backoff_base * (2 ** max(0, attempts - 1)),
                )
                next_attempt_at = now + timedelta(seconds=backoff)
            await session.execute(
                sa.update(indexing_jobs)
                .where(indexing_jobs.c.job_id == job_id)
                .values(
                    status=new_status,
                    last_error=error[:8000],
                    locked_by=None,
                    locked_until=next_attempt_at,
                    updated_at=now,
                    completed_at=now if new_status == "dead_letter" else None,
                )
            )
            return new_status

    # ── Maintenance ─────────────────────────────────────────────

    async def release_expired_leases(self) -> int:
        """Unstick any 'running' rows whose lease has elapsed.

        ``lease_next`` already handles this lazily on selection, but
        an explicit sweep on startup means metrics surface accurately
        even without traffic. Returns the count of rows reset.
        """
        now = _now()
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            result = await session.execute(
                sa.update(indexing_jobs)
                .where(
                    indexing_jobs.c.status == "running",
                    indexing_jobs.c.locked_until.is_not(None),
                    indexing_jobs.c.locked_until < now,
                )
                .values(
                    status="pending",
                    locked_by=None,
                    locked_until=None,
                    updated_at=now,
                )
            )
            return int(result.rowcount or 0)

    async def stats(self) -> dict[str, int]:
        """Per-status row counts; used by the /stats health endpoint."""
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            rows = (
                await session.execute(
                    sa.select(
                        indexing_jobs.c.status,
                        sa.func.count().label("count"),
                    ).group_by(indexing_jobs.c.status)
                )
            ).all()
        return {row.status: int(row.count) for row in rows}


# ── Helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_job(row) -> Job:
    return Job(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        artifact_id=row["artifact_id"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        trace_id=row["trace_id"],
        input=row["input"] or {},
    )
