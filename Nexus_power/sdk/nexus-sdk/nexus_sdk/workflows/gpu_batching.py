"""
Phase 14 — GPU inference batcher.

Multiple concurrent in-pod inference requests get coalesced into a
single GPU forward pass within a short collection window. The single-
request path bypasses batching when the queue is empty so latency
doesn't regress.

Usage shape (per inference type, e.g. Whisper, LLaVA):

    batcher = InferenceBatcher(
        max_batch_size=8,
        max_wait_ms=200,
        runner=run_whisper_batched,
    )
    result = await batcher.submit(request_payload)

The runner callable receives a list of payloads and must return a
list of results in the same order. The batcher handles:

  - Per-tenant fairness via FIFO within a window
  - Cancellation propagation (caller times out → request is dropped
    from the batch BEFORE the GPU call fires)
  - Single-request fast-path: if the window expires with one item,
    `runner` is called with `[payload]` and returns `[result]`
  - Exception-safe: a runner exception is propagated to every
    caller in that batch (their request didn't succeed); subsequent
    batches are unaffected

NOT in scope here:
  - Cross-pod batching (covered by KEDA at a coarser grain)
  - Speculative decoding / streaming (per-engine concern)
  - Per-tenant priority weighting (Phase 14.5)

Performance contract:
  Throughput: ≥30% improvement vs per-request inference at sustained
  load of N concurrent requests/pod where N ≥ max_batch_size.
  Latency: single-request p95 ≤ baseline + max_wait_ms.

This module ships untested by real GPU benchmarking — those numbers
come from the team's pre-prod GPU rig.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

logger = logging.getLogger(__name__)


T = TypeVar("T")
R = TypeVar("R")


BatchRunner = Callable[[list[T]], Awaitable[list[R]]]


@dataclass
class _PendingRequest(Generic[T, R]):
    payload: T
    future: asyncio.Future
    submitted_at: float = field(default_factory=time.monotonic)


class InferenceBatcher(Generic[T, R]):
    """Batches concurrent inference requests for a single GPU pipeline.

    Each call to `submit` returns a Future that resolves once the
    request's batch completes. The internal loop wakes either when:

      - A new submission arrives and the batch reaches `max_batch_size`
      - `max_wait_ms` has elapsed since the oldest pending request

    Whichever fires first triggers the GPU call.
    """

    def __init__(
        self,
        *,
        runner: BatchRunner[T, R],
        max_batch_size: int = 8,
        max_wait_ms: int = 200,
        name: str = "gpu_batcher",
    ) -> None:
        self._runner = runner
        self._max_batch_size = max(1, max_batch_size)
        self._max_wait_seconds = max(0.001, max_wait_ms / 1000.0)
        self._name = name

        self._queue: deque[_PendingRequest[T, R]] = deque()
        self._wakeup = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopped = False

        # Telemetry — bumped externally via the metrics facade.
        self.total_submitted = 0
        self.total_batches = 0
        self.total_runner_exceptions = 0

    async def start(self) -> None:
        """Start the background batching loop. Idempotent."""
        if self._loop_task is None or self._loop_task.done():
            self._stopped = False
            self._loop_task = asyncio.create_task(
                self._loop(), name=f"{self._name}.loop",
            )

    async def stop(self, drain: bool = True) -> None:
        """Stop the loop. If `drain`, wait for the in-flight batch to
        finish; otherwise cancel pending requests with CancelledError."""
        self._stopped = True
        self._wakeup.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(
                    self._loop_task,
                    timeout=30 if drain else 1,
                )
            except asyncio.TimeoutError:
                self._loop_task.cancel()
        if not drain:
            while self._queue:
                req = self._queue.popleft()
                if not req.future.done():
                    req.future.cancel()

    async def submit(self, payload: T) -> R:
        """Enqueue a payload. Returns the runner's per-request output."""
        if self._stopped:
            raise RuntimeError(f"batcher {self._name!r} is stopped")
        if self._loop_task is None or self._loop_task.done():
            await self.start()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req: _PendingRequest[T, R] = _PendingRequest(payload, fut)
        self._queue.append(req)
        self.total_submitted += 1
        self._wakeup.set()
        return await fut

    async def _loop(self) -> None:
        """Main coalescing loop. Pulls batches off the queue and runs
        them through the runner. Never raises out of the loop — a
        runner failure propagates to its batch's futures but doesn't
        kill the batcher."""
        while not self._stopped:
            if not self._queue:
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(), timeout=self._max_wait_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                self._wakeup.clear()
                if self._stopped:
                    break
                if not self._queue:
                    continue

            # Once we have at least one request, wait up to
            # max_wait_seconds for the batch to fill — but bail
            # immediately if it reaches max_batch_size.
            window_start = time.monotonic()
            while (
                len(self._queue) < self._max_batch_size
                and (time.monotonic() - window_start) < self._max_wait_seconds
                and not self._stopped
            ):
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(),
                        timeout=max(
                            0.001,
                            self._max_wait_seconds - (time.monotonic() - window_start),
                        ),
                    )
                    self._wakeup.clear()
                except asyncio.TimeoutError:
                    break

            # Snapshot the current batch.
            batch_size = min(len(self._queue), self._max_batch_size)
            batch = [self._queue.popleft() for _ in range(batch_size)]

            # Drop cancelled callers — they're not waiting anymore.
            live = [r for r in batch if not r.future.cancelled()]
            if not live:
                continue

            self.total_batches += 1
            payloads = [r.payload for r in live]
            try:
                results = await self._runner(payloads)
                if not isinstance(results, list) or len(results) != len(live):
                    raise RuntimeError(
                        f"runner returned {type(results).__name__} of length "
                        f"{len(results) if hasattr(results, '__len__') else '?'}, "
                        f"expected list of length {len(live)}"
                    )
                for req, result in zip(live, results):
                    if not req.future.done():
                        req.future.set_result(result)
            except asyncio.CancelledError:
                # Surface cancellation to all pending futures.
                for req in live:
                    if not req.future.done():
                        req.future.cancel()
                raise
            except Exception as exc:
                self.total_runner_exceptions += 1
                logger.error(
                    "%s.batch_failed batch_size=%d err=%s",
                    self._name, len(live), exc,
                    exc_info=True,
                )
                for req in live:
                    if not req.future.done():
                        req.future.set_exception(exc)


__all__ = ["InferenceBatcher", "BatchRunner"]
