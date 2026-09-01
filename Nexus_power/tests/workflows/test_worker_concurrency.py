"""
Proof tests for the WorkflowWorker concurrency change.

Goals:
  1. With concurrency=1, the worker runs handlers strictly serially.
  2. With concurrency=N>1, up to N handlers are in flight at once.
  3. Backpressure: when N handlers are already running, the worker
     blocks on sem.acquire() before pulling the next job from the queue.
  4. Drain: on stop(), the worker waits for in-flight tasks before
     tearing down the HTTP client.

A fake JobQueue stands in for Redis Streams so the tests run on a
laptop in <1s. A fake handler exposes an asyncio.Event so each test
can pause / resume work precisely.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from nexus_sdk.workflows import StepKind, WorkerConfig, WorkflowWorker
from nexus_sdk.workflows.models import StepResult


# ─── Fake queue ────────────────────────────────────────────────


class _FakeQueue:
    """Async generator masquerading as JobQueue.consume()."""

    def __init__(self, jobs: list[dict]) -> None:
        self._jobs = list(jobs)
        self.acked: list[str] = []
        self.nacked: list[str] = []
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def consume(self):
        # Yield each canned job, then idle (so the worker blocks on
        # sem.acquire if it has spare capacity but no more jobs).
        for j in self._jobs:
            if self._stop:
                return
            yield j
        # Stay in the generator briefly so in-flight tasks can finish.
        while not self._stop:
            await asyncio.sleep(0.01)

    async def ack(self, msg_id: str) -> None:
        self.acked.append(msg_id)

    async def nack(self, msg_id: str, job: dict) -> None:
        self.nacked.append(msg_id)


def _job(workflow_id: str, step: str = "test.step", attempt: int = 1) -> dict:
    """Build a minimal job envelope dict the worker accepts."""
    return {
        "_msg_id": f"msg-{workflow_id}",
        "payload": {
            "workflow_id": workflow_id,
            "step_name": step,
            "step_index": 0,
            "attempt": attempt,
            "tenant_id": "t",
            "session_id": "s",
            "deadline_at_epoch": time.time() + 3600,
            "heartbeat_seconds": 60,
            "params": {},
            "checkpoint": {},
        },
    }


# ─── Fake step handler ────────────────────────────────────────


class _GatedHandler:
    """Step handler that records when it enters/exits, and blocks on
    an asyncio.Event until the test releases it. Lets us measure
    `peak_in_flight` precisely."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.peak_in_flight = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()
        self._release = asyncio.Event()

    async def handle(self, envelope) -> StepResult:
        async with self._lock:
            self._in_flight += 1
            self.entered += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        await self._release.wait()
        async with self._lock:
            self._in_flight -= 1
            self.exited += 1
        return StepResult(
            workflow_id=envelope.workflow_id,
            step_name=envelope.step_name,
            success=True,
            checkpoint={},
            duration_ms=10,
        )

    def release_all(self) -> None:
        self._release.set()


# ─── Stub the orchestrator callback so tests don't hit the network ──


class _LocalManagerClient:
    """Stands in for an HTTP orchestrator. record_result + heartbeat
    just succeed in-memory. The worker treats us as `manager_client`
    and skips the HTTP path entirely."""

    def __init__(self) -> None:
        self.results: list[StepResult] = []
        self.heartbeats: list[str] = []

    async def record_result(self, result: StepResult) -> None:
        self.results.append(result)

    async def heartbeat(self, workflow_id: str) -> None:
        self.heartbeats.append(workflow_id)


# ─── Tests ────────────────────────────────────────────────────


def _config(concurrency: int) -> WorkerConfig:
    return WorkerConfig(
        engine_name="test",
        kind=StepKind.CPU,
        orchestrator_url="http://unused",
        concurrency=concurrency,
    )


async def _wait_for(predicate, timeout=2.0, poll=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(poll)


@pytest.mark.asyncio
async def test_concurrency_one_runs_serially():
    """concurrency=1 → peak_in_flight must be exactly 1."""
    h = _GatedHandler()
    q = _FakeQueue([_job(f"wf-{i}", step="test.step") for i in range(5)])
    mgr = _LocalManagerClient()
    worker = WorkflowWorker(_config(1), q, manager_client=mgr)
    worker.register("test.step", h.handle)

    runner = asyncio.create_task(worker.run())
    # Let the worker pull one job + park it on _release.
    await _wait_for(lambda: h.entered >= 1)
    assert h.entered == 1, f"expected exactly 1 in flight, saw {h.entered}"
    assert h.peak_in_flight == 1

    # Release the gate so all handlers complete. The worker keeps
    # pulling from the queue (which is still active) until every
    # canned job has been processed.
    h.release_all()
    await _wait_for(lambda: h.exited >= 5, timeout=3)
    assert h.exited == 5
    assert h.peak_in_flight == 1

    # Now stop. The queue's consume loop exits; run() drains (nothing
    # left in-flight) and the HTTP client closes.
    q.stop()
    worker.stop()
    await asyncio.wait_for(runner, timeout=3)


@pytest.mark.asyncio
async def test_concurrency_four_pipelines_four_jobs():
    """concurrency=4 → peak_in_flight must be exactly 4."""
    h = _GatedHandler()
    q = _FakeQueue([_job(f"wf-{i}") for i in range(8)])
    mgr = _LocalManagerClient()
    worker = WorkflowWorker(_config(4), q, manager_client=mgr)
    worker.register("test.step", h.handle)

    runner = asyncio.create_task(worker.run())
    # Wait for the semaphore to be exhausted.
    await _wait_for(lambda: h.entered >= 4)
    assert h.entered == 4, (
        f"with concurrency=4 expected 4 in flight, observed {h.entered}; "
        f"peak={h.peak_in_flight}"
    )
    # Crucial: no MORE than 4 in flight — the semaphore must apply backpressure.
    await asyncio.sleep(0.05)
    assert h.peak_in_flight == 4

    h.release_all()
    await _wait_for(lambda: h.exited >= 8, timeout=3)
    assert h.exited == 8
    assert h.peak_in_flight == 4

    q.stop()
    worker.stop()
    await asyncio.wait_for(runner, timeout=3)


@pytest.mark.asyncio
async def test_drain_on_stop_waits_for_in_flight():
    """If the queue stops while jobs are in flight, run() must drain
    them before returning. Otherwise the orchestrator never gets a
    record_result for those jobs and the workflows stick at running."""
    h = _GatedHandler()
    q = _FakeQueue([_job(f"wf-{i}") for i in range(3)])
    mgr = _LocalManagerClient()
    worker = WorkflowWorker(_config(4), q, manager_client=mgr)
    worker.register("test.step", h.handle)

    runner = asyncio.create_task(worker.run())
    # All 3 jobs should be in flight quickly with concurrency=4.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if h.entered >= 3:
            break
    assert h.entered == 3

    # Stop the queue while handlers are still gated.
    q.stop()
    worker.stop()
    # Now release — run() must drain before completing.
    h.release_all()
    await asyncio.wait_for(runner, timeout=3)
    assert h.exited == 3, "worker exited before draining in-flight handlers"
    assert len(mgr.results) == 3


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_pool():
    """A handler raising an exception must release its semaphore slot
    so other concurrent jobs continue. The current implementation
    relies on sem.release() running in a finally block."""

    bad = _GatedHandler()
    good = _GatedHandler()

    async def bad_handler(envelope):
        bad.entered += 1
        raise RuntimeError("boom")

    q = _FakeQueue([
        _job("bad-1", step="bad"),
        _job("good-1", step="good"),
        _job("good-2", step="good"),
        _job("good-3", step="good"),
    ])
    mgr = _LocalManagerClient()
    worker = WorkflowWorker(_config(2), q, manager_client=mgr)
    worker.register("bad", bad_handler)
    worker.register("good", good.handle)

    runner = asyncio.create_task(worker.run())
    # Let the bad job throw + the good jobs reach the gate.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if good.entered >= 2:
            break

    assert bad.entered == 1, (
        f"bad handler should have run once, ran {bad.entered}"
    )
    # The semaphore release happens regardless — at least 2 goods
    # are in flight, not blocked by the failed slot.
    assert good.entered >= 2, (
        f"semaphore not released after handler exception; "
        f"good.entered={good.entered}"
    )

    good.release_all()
    q.stop()
    worker.stop()
    await asyncio.wait_for(runner, timeout=3)
    assert good.exited == 3
