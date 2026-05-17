"""
Nexus SDK — GPU Worker Base for durable job processing.

Provides a base class that GPU engines (Ears, Eyes, Heart) extend
to run as dedicated worker pods that consume from the Redis Streams
job queue.

Architecture:
    API Pod (accepts HTTP) ──enqueue──► Redis Stream ──consume──► Worker Pod (GPU)

Each engine supports two modes controlled by ENGINE_MODE env var:
    - "api"    : HTTP server only, enqueues jobs to Redis Streams
    - "worker" : No HTTP server, consumes jobs from Redis Streams + processes on GPU
    - "hybrid" : Both HTTP server and worker loop (single-pod dev mode, default)

Workers:
    - Run the consume loop in the background
    - Honor GPU semaphore for local concurrency
    - Update job status in JobStore as they process
    - Auto-recover abandoned jobs from dead workers
    - Expose /health and /metrics even in worker mode (for K8s probes)

Usage in an engine:
    from nexus_sdk.worker import GPUWorkerMixin

    class EarsEngine(NexusEngine, GPUWorkerMixin):
        async def on_startup(self):
            await self.init_worker_queue()
            if self.is_worker_mode:
                self.start_worker_loop(self._process_job)

        async def _process_job(self, job: dict):
            # Actual GPU processing logic
            ...
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine, Optional

from nexus_sdk.queue import JobQueue

logger = logging.getLogger(__name__)

# Valid engine modes
MODE_API = "api"
MODE_WORKER = "worker"
MODE_HYBRID = "hybrid"


class GPUWorkerMixin:
    """
    Mixin that adds durable job queue support to any NexusEngine.

    Engines that do GPU work should inherit from both NexusEngine
    and GPUWorkerMixin, then call init_worker_queue() in on_startup().
    """

    _job_queue: Optional[JobQueue] = None
    _worker_task: Optional[asyncio.Task] = None
    _engine_mode: str = MODE_HYBRID

    @property
    def engine_mode(self) -> str:
        """Current engine mode: api, worker, or hybrid."""
        return self._engine_mode

    @property
    def is_api_mode(self) -> bool:
        return self._engine_mode in (MODE_API, MODE_HYBRID)

    @property
    def is_worker_mode(self) -> bool:
        return self._engine_mode in (MODE_WORKER, MODE_HYBRID)

    @property
    def job_queue(self) -> Optional[JobQueue]:
        return self._job_queue

    async def init_worker_queue(self) -> bool:
        """
        Initialize the job queue. Call from on_startup().

        Reads ENGINE_MODE from env (default: hybrid).
        Returns True if queue connected successfully.
        """
        self._engine_mode = os.environ.get("ENGINE_MODE", MODE_HYBRID).lower().strip()
        if self._engine_mode not in (MODE_API, MODE_WORKER, MODE_HYBRID):
            logger.warning(
                "GPUWorker: invalid ENGINE_MODE=%s, defaulting to hybrid",
                self._engine_mode,
            )
            self._engine_mode = MODE_HYBRID

        # Access config from the NexusEngine base
        config = getattr(self, "config", None)
        engine_name = getattr(self, "name", "unknown")

        redis_host = getattr(config, "redis_host", "localhost") if config else "localhost"
        redis_port = getattr(config, "redis_port", 6379) if config else 6379
        redis_password = getattr(config, "redis_password", "") if config else ""

        # Read optional GPU concurrency from env or config
        gpu_concurrency = int(os.environ.get("GPU_CONCURRENCY", "1"))

        self._job_queue = JobQueue(
            engine_name=engine_name,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_password=redis_password,
            batch_size=gpu_concurrency,
        )

        connected = await self._job_queue.connect()

        if connected:
            logger.info(
                "GPUWorker[%s]: queue initialized, mode=%s",
                engine_name,
                self._engine_mode,
            )
        else:
            logger.warning(
                "GPUWorker[%s]: queue connection failed, falling back to in-process",
                engine_name,
            )

        return connected

    def start_worker_loop(
        self,
        process_fn: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        gpu_semaphore: Optional[asyncio.Semaphore] = None,
    ) -> None:
        """
        Start the background worker loop that consumes and processes jobs.

        Args:
            process_fn: Async function that processes a single job dict.
                       The job dict contains: job_id, payload, _msg_id, _retry_count.
            gpu_semaphore: Optional semaphore to limit GPU concurrency.
                          If not provided, jobs are processed sequentially.
        """
        if not self._job_queue or not self._job_queue.is_connected:
            logger.warning("GPUWorker: cannot start worker loop, queue not connected")
            return

        if not self.is_worker_mode:
            logger.info("GPUWorker: skipping worker loop, mode=%s", self._engine_mode)
            return

        engine_name = getattr(self, "name", "unknown")

        async def _worker_loop():
            logger.info("GPUWorker[%s]: worker loop started", engine_name)
            job_store = getattr(self, "job_store", None)

            async for job in self._job_queue.consume():
                job_id = job.get("job_id", "unknown")
                msg_id = job.get("_msg_id")

                try:
                    # Update job status to processing
                    if job_store:
                        await job_store.update_job(
                            job_id,
                            status="processing",
                            current_stage="claimed_by_worker",
                            worker=self._job_queue._config.consumer_name,
                        )

                    # Process with GPU semaphore if provided
                    if gpu_semaphore:
                        async with gpu_semaphore:
                            await process_fn(job)
                    else:
                        await process_fn(job)

                    # Acknowledge successful processing
                    await self._job_queue.ack(msg_id)

                except Exception as e:
                    logger.error(
                        "GPUWorker[%s]: job %s failed: %s",
                        engine_name,
                        job_id,
                        e,
                        exc_info=True,
                    )

                    # Update job status to failed
                    if job_store:
                        await job_store.update_job(
                            job_id,
                            status="failed",
                            error=str(e),
                            current_stage="worker_error",
                        )

                    # Nack triggers retry or DLQ
                    await self._job_queue.nack(msg_id, job)

        self._worker_task = asyncio.create_task(_worker_loop())
        logger.info(
            "GPUWorker[%s]: worker loop task created, mode=%s",
            engine_name,
            self._engine_mode,
        )

    async def stop_worker_loop(self) -> None:
        """Stop the worker loop gracefully."""
        if self._job_queue:
            self._job_queue.stop()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._job_queue:
            await self._job_queue.close()
        logger.info("GPUWorker: worker loop stopped")

    async def enqueue_gpu_job(
        self,
        job_id: str,
        payload: dict[str, Any],
        priority: str = "normal",
    ) -> bool:
        """
        Enqueue a job for GPU processing.

        Returns True if enqueued to Redis Streams.
        Returns False if queue unavailable (caller should fall back to BackgroundTasks).
        """
        if not self._job_queue or not self._job_queue.is_connected:
            return False

        try:
            await self._job_queue.enqueue(
                job_id=job_id,
                payload=payload,
                priority=priority,
            )
            return True
        except Exception as e:
            logger.error(
                "GPUWorker: enqueue failed for job=%s: %s",
                job_id,
                e,
            )
            return False

    def register_queue_routes(self, app) -> None:
        """
        Register queue management endpoints on the FastAPI app.

        Adds:
          GET  /api/v1/{engine}/queue/metrics  — queue depth, pending, DLQ count
          GET  /api/v1/{engine}/queue/dlq      — list dead-letter jobs
          POST /api/v1/{engine}/queue/dlq/{id}/retry — retry a DLQ job
        """
        engine_name = getattr(self, "name", "unknown")

        @app.get(f"/api/v1/{engine_name}/queue/metrics")
        async def queue_metrics():
            if not self._job_queue:
                return {"error": "queue not initialized"}
            return await self._job_queue.get_metrics()

        @app.get(f"/api/v1/{engine_name}/queue/dlq")
        async def queue_dlq(count: int = 100):
            if not self._job_queue:
                return {"error": "queue not initialized"}
            return await self._job_queue.get_dlq_jobs(count=count)

        @app.post(f"/api/v1/{engine_name}/queue/dlq/{{dlq_msg_id}}/retry")
        async def retry_dlq_job(dlq_msg_id: str):
            if not self._job_queue:
                return {"error": "queue not initialized"}
            ok = await self._job_queue.retry_dlq_job(dlq_msg_id)
            return {"success": ok, "dlq_msg_id": dlq_msg_id}


# ─── Priority GPU Semaphore ───────────────────────────────────


# Priority values: lower number = higher priority
GPU_PRIORITY_FAST = 0
GPU_PRIORITY_NORMAL = 5
GPU_PRIORITY_DEEP = 10


class PriorityGPUSemaphore:
    """
    Priority-aware GPU concurrency guard.

    Replaces ``asyncio.Semaphore(1)`` in GPU engines so that higher-priority
    requests (e.g. ``processing_profile=fast``) are served before lower-priority
    ones (``deep``).

    Usage::

        sem = PriorityGPUSemaphore(concurrency=1)

        async with sem.acquire(priority=GPU_PRIORITY_FAST):
            await do_gpu_work()

    The semaphore is also usable as a plain ``async with sem:`` (defaults to
    GPU_PRIORITY_NORMAL) so it's a drop-in replacement for asyncio.Semaphore.
    """

    def __init__(self, concurrency: int = 1):
        self._concurrency = concurrency
        self._active = 0
        self._waiters: list[tuple[int, asyncio.Event, int]] = []  # (priority, event, seq)
        self._seq = 0  # tiebreaker for FIFO within same priority
        self._lock = asyncio.Lock()

    class _AcquireContext:
        """Async context manager returned by acquire()."""

        def __init__(self, sem: "PriorityGPUSemaphore", priority: int):
            self._sem = sem
            self._priority = priority

        async def __aenter__(self):
            await self._sem._acquire(self._priority)
            return self

        async def __aexit__(self, *exc):
            await self._sem._release()

    def acquire(self, priority: int = GPU_PRIORITY_NORMAL) -> "_AcquireContext":
        """Return an async context manager that acquires with given priority."""
        return self._AcquireContext(self, priority)

    async def __aenter__(self):
        await self._acquire(GPU_PRIORITY_NORMAL)
        return self

    async def __aexit__(self, *exc):
        await self._release()

    async def _acquire(self, priority: int) -> None:
        async with self._lock:
            if self._active < self._concurrency:
                self._active += 1
                return
            # Must wait — enqueue with priority
            event = asyncio.Event()
            seq = self._seq
            self._seq += 1
            self._waiters.append((priority, event, seq))
            # Keep sorted: lowest priority value = highest priority
            self._waiters.sort(key=lambda w: (w[0], w[2]))

        # Wait outside the lock so other tasks can release
        await event.wait()

    async def _release(self) -> None:
        async with self._lock:
            if self._waiters:
                _, event, _ = self._waiters.pop(0)
                event.set()
            else:
                self._active -= 1

    @property
    def waiting_count(self) -> int:
        """Number of tasks waiting for GPU access."""
        return len(self._waiters)

    @property
    def active_count(self) -> int:
        """Number of tasks currently holding GPU access."""
        return self._active
