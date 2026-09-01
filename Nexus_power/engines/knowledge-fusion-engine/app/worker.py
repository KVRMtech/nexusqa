"""Substrate worker — leases jobs from indexing_jobs, runs the
indexer, completes/fails with retry semantics.

Concurrency: spawn ``worker_concurrency`` workers, each pulling from
the shared queue via ``JobStore.lease_next`` with FOR UPDATE
SKIP LOCKED. No global lock; workers self-coordinate.

Graceful shutdown: ``stop()`` signals all loops to drain after their
current job finishes. In-flight jobs are completed (their leases
held until the indexer returns or fails).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .indexer import Indexer, IndexerOutcome
from .jobs import Job, JobStore

logger = logging.getLogger(__name__)


class SubstrateWorker:
    def __init__(
        self,
        *,
        store: JobStore,
        indexer: Indexer,
        event_publisher,
        concurrency: int,
        poll_interval_seconds: float,
        worker_id: str,
    ) -> None:
        self._store = store
        self._indexer = indexer
        self._publisher = event_publisher
        self._concurrency = max(1, concurrency)
        self._poll_interval = poll_interval_seconds
        self._worker_id = worker_id
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._idle_workers = 0
        self._inflight = 0

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        if self._tasks:
            return
        # Recover anything stuck from a prior crash before workers start.
        recovered = await self._store.release_expired_leases()
        if recovered:
            logger.info(
                "substrate_worker.recovered_expired_leases count=%d",
                recovered,
            )
        for i in range(self._concurrency):
            self._tasks.append(
                asyncio.create_task(self._loop(i), name=f"substrate-{i}")
            )
        logger.info(
            "substrate_worker.started concurrency=%d worker_id=%s",
            self._concurrency,
            self._worker_id,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("substrate_worker.stopped")

    @property
    def is_running(self) -> bool:
        return bool(self._tasks) and not self._stop_event.is_set()

    def snapshot(self) -> dict[str, int]:
        return {
            "concurrency": self._concurrency,
            "idle_workers": self._idle_workers,
            "inflight": self._inflight,
        }

    # ── Worker loop ─────────────────────────────────────────────

    async def _loop(self, slot: int) -> None:
        backoff = self._poll_interval
        while not self._stop_event.is_set():
            try:
                job: Optional[Job] = await self._store.lease_next()
            except Exception as exc:
                logger.exception(
                    "substrate_worker.lease_failed slot=%d err=%s",
                    slot,
                    exc,
                )
                await self._sleep_or_exit(min(backoff * 2, 30.0))
                continue
            if job is None:
                self._idle_workers += 1
                try:
                    await self._sleep_or_exit(self._poll_interval)
                finally:
                    self._idle_workers -= 1
                continue
            backoff = self._poll_interval
            self._inflight += 1
            try:
                await self._process_job(job)
            except Exception as exc:
                logger.exception(
                    "substrate_worker.unhandled_error job=%s err=%s",
                    job.job_id,
                    exc,
                )
                await self._safely_fail(job, repr(exc))
            finally:
                self._inflight -= 1

    async def _process_job(self, job: Job) -> None:
        started = time.monotonic()
        trace_id = job.trace_id or job.job_id

        # Long-running heartbeat: while index_artifact runs, we extend
        # the lease every (lease/3) seconds. If we lose the lease (a
        # crashed-recovery worker grabbed it), we abort to avoid double-
        # writes.
        heartbeat_task = asyncio.create_task(self._heartbeat(job.job_id))
        try:
            result = await self._indexer.index_artifact(
                tenant_id=job.tenant_id,
                session_id=job.session_id,
                artifact_id=job.artifact_id,
                trace_id=trace_id,
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

        elapsed_ms = int((time.monotonic() - started) * 1000)
        await self._handle_result(job, result, elapsed_ms, trace_id)

    async def _heartbeat(self, job_id: str) -> None:
        """Periodically extend lease. Exits silently on cancel."""
        interval = max(5.0, self._store._lease_seconds / 3.0)  # noqa: SLF001
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            try:
                ok = await self._store.heartbeat(job_id)
                if not ok:
                    logger.warning(
                        "substrate_worker.lease_lost job=%s — exiting heartbeat",
                        job_id,
                    )
                    return
            except Exception as exc:
                logger.warning(
                    "substrate_worker.heartbeat_failed job=%s err=%s",
                    job_id,
                    exc,
                )

    async def _handle_result(
        self,
        job: Job,
        result,
        elapsed_ms: int,
        trace_id: str,
    ) -> None:
        if result.outcome in (IndexerOutcome.INDEXED, IndexerOutcome.NOOP):
            await self._store.complete(
                job.job_id,
                result={
                    "outcome": result.outcome.value,
                    "segments_created": result.segments_created,
                    "segments_skipped": result.segments_skipped,
                    "segments_failed": result.segments_failed,
                    "elapsed_ms": elapsed_ms,
                    "detail": result.detail,
                },
            )
            await self._publisher.publish_substrate_indexed(
                tenant_id=job.tenant_id,
                trace_id=trace_id,
                session_id=job.session_id,
                artifact_id=job.artifact_id,
                segments_created=result.segments_created,
                segments_skipped=result.segments_skipped,
                segments_failed=result.segments_failed,
                outcome=result.outcome.value,
            )
        elif result.outcome in (
            IndexerOutcome.SKIP_NO_TRANSCRIPT,
            IndexerOutcome.SKIP_QUALITY_GATE,
        ):
            await self._store.skip(
                job.job_id, reason=result.detail.get("reason") or result.outcome.value
            )
            await self._publisher.publish_substrate_skipped(
                tenant_id=job.tenant_id,
                trace_id=trace_id,
                session_id=job.session_id,
                artifact_id=job.artifact_id,
                reason=result.outcome.value,
                detail=result.detail,
            )
        elif result.outcome == IndexerOutcome.RETRY:
            new_state = await self._store.fail(
                job.job_id,
                error=str(result.detail),
            )
            if new_state == "dead_letter":
                await self._publisher.publish_substrate_failed(
                    tenant_id=job.tenant_id,
                    trace_id=trace_id,
                    session_id=job.session_id,
                    artifact_id=job.artifact_id,
                    error=str(result.detail),
                )

    async def _safely_fail(self, job: Job, error: str) -> None:
        try:
            new_state = await self._store.fail(job.job_id, error=error)
            if new_state == "dead_letter":
                await self._publisher.publish_substrate_failed(
                    tenant_id=job.tenant_id,
                    trace_id=job.trace_id or job.job_id,
                    session_id=job.session_id,
                    artifact_id=job.artifact_id,
                    error=error,
                )
        except Exception:
            logger.exception(
                "substrate_worker.fail_callback_errored job=%s",
                job.job_id,
            )

    async def _sleep_or_exit(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
