"""
Phase 14 — GPU batcher unit tests.

These test the coalescing semantics. The runner is mocked; the real
GPU throughput-improvement claim must be validated separately on a
GPU rig (see docs/PHASE14_BATCHING_VALIDATION.md, not yet written).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus_sdk.workflows.gpu_batching import InferenceBatcher


@pytest.mark.asyncio
async def test_single_request_passes_through_after_window():
    """A single submission still works — runner is called with a list
    of one item after the wait window expires."""
    calls: list[list[int]] = []

    async def runner(batch: list[int]) -> list[str]:
        calls.append(list(batch))
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=4, max_wait_ms=50, name="test",
    )
    await batcher.start()
    try:
        result = await batcher.submit(1)
        assert result == "r1"
        assert calls == [[1]]
        assert batcher.total_batches == 1
    finally:
        await batcher.stop(drain=False)


@pytest.mark.asyncio
async def test_multiple_concurrent_requests_form_one_batch():
    """8 simultaneous submissions hit the GPU as a single batch of 8."""
    calls: list[list[int]] = []

    async def runner(batch: list[int]) -> list[str]:
        calls.append(list(batch))
        # Simulate a slow GPU pass.
        await asyncio.sleep(0.05)
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=8, max_wait_ms=100, name="test",
    )
    await batcher.start()
    try:
        results = await asyncio.gather(*[batcher.submit(i) for i in range(8)])
        assert results == [f"r{i}" for i in range(8)]
        assert len(calls) == 1, f"expected 1 batch call, got {len(calls)}"
        assert calls[0] == list(range(8))
    finally:
        await batcher.stop(drain=False)


@pytest.mark.asyncio
async def test_batch_caps_at_max_size():
    """16 concurrent submissions with max_batch_size=8 produce ≥2 batches."""
    calls: list[list[int]] = []

    async def runner(batch: list[int]) -> list[str]:
        calls.append(list(batch))
        await asyncio.sleep(0.02)
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=8, max_wait_ms=100, name="test",
    )
    await batcher.start()
    try:
        results = await asyncio.gather(*[batcher.submit(i) for i in range(16)])
        assert sorted(results) == sorted(f"r{i}" for i in range(16))
        assert all(len(c) <= 8 for c in calls)
        assert sum(len(c) for c in calls) == 16
    finally:
        await batcher.stop(drain=False)


@pytest.mark.asyncio
async def test_runner_exception_propagates_to_all_callers():
    """If the GPU pass raises, every caller in that batch sees the
    exception. Subsequent batches must continue to work."""

    call_num = 0

    async def runner(batch: list[int]) -> list[str]:
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            raise RuntimeError("first batch boom")
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=4, max_wait_ms=50, name="test",
    )
    await batcher.start()
    try:
        # First batch — all should raise.
        first = await asyncio.gather(
            *[batcher.submit(i) for i in range(4)],
            return_exceptions=True,
        )
        assert all(isinstance(e, RuntimeError) for e in first)
        assert all("boom" in str(e) for e in first)

        # Second batch — should succeed; batcher recovered.
        second = await asyncio.gather(
            *[batcher.submit(i + 100) for i in range(4)],
        )
        assert second == [f"r{i + 100}" for i in range(4)]
        assert batcher.total_runner_exceptions == 1
    finally:
        await batcher.stop(drain=False)


@pytest.mark.asyncio
async def test_throughput_win_under_concurrent_load():
    """Rough proof of concept: 8 requests with a 100ms GPU pass and
    8-wide batching complete in ~100ms total, not 8 × 100ms = 800ms.
    Tests the value proposition without needing a real GPU."""

    async def runner(batch: list[int]) -> list[str]:
        # Pretend the GPU forward pass takes 100ms regardless of
        # batch size — typical of small-batch LLaVA/Whisper.
        await asyncio.sleep(0.1)
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=8, max_wait_ms=50, name="test",
    )
    await batcher.start()
    start = time.monotonic()
    try:
        results = await asyncio.gather(*[batcher.submit(i) for i in range(8)])
        elapsed = time.monotonic() - start
    finally:
        await batcher.stop(drain=False)

    assert len(results) == 8
    # 8× serial calls would be 800ms. Batched should finish in ~150ms
    # (50ms wait + 100ms GPU). Generous threshold of 300ms accounts
    # for CI variance.
    assert elapsed < 0.3, (
        f"batched 8 requests took {elapsed*1000:.0f}ms — "
        f"batching not coalescing properly"
    )


@pytest.mark.asyncio
async def test_cancelled_request_drops_before_batch_fires():
    """If a caller cancels their await before the batch fires, the
    request is dropped. The remaining batch still runs."""

    async def runner(batch: list[int]) -> list[str]:
        await asyncio.sleep(0.02)
        return [f"r{x}" for x in batch]

    batcher = InferenceBatcher(
        runner=runner, max_batch_size=4, max_wait_ms=100, name="test",
    )
    await batcher.start()
    try:
        # Submit two; cancel one immediately.
        coro_a = batcher.submit(1)
        task_a = asyncio.create_task(coro_a)
        task_b = asyncio.create_task(batcher.submit(2))
        await asyncio.sleep(0.005)
        task_a.cancel()

        # B should still resolve.
        result_b = await task_b
        assert result_b == "r2"
        with pytest.raises(asyncio.CancelledError):
            await task_a
    finally:
        await batcher.stop(drain=False)
