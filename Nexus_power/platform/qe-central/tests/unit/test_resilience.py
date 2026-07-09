"""QE-Central Phase 5.5 — HTTP retry/backoff unit tests (no network, no clock).

Pins the resilience contract the factory clients rely on:

  * an idempotent call retries transient failures (503 / transport timeout) up to
    the cap, then re-raises the ORIGINAL error unwrapped;
  * a non-idempotent call is executed EXACTLY ONCE and never retried (the
    double-submit guard);
  * a deterministic status (404) is not retried even when idempotent;
  * the backoff sequence is bounded and exponential (injected ``sleep`` records the
    exact delays; injected ``rand`` makes them deterministic);
  * a success (immediate or after transient failures) returns the value; and
  * ``asyncio.CancelledError`` propagates immediately (never retried).

All time is injected — the tests are instant and exact.
"""
from __future__ import annotations

import asyncio

import pytest

from app.clients.factory import FactoryClientError
from app.clients.resilience import (
    TRANSIENT_HTTP_STATUSES,
    call_with_retries,
    default_is_retryable,
    env_max_retries,
)


def run(coro):
    return asyncio.run(coro)


class _Recorder:
    """A flaky async thunk + a recording sleep, for deterministic retry tests."""

    def __init__(self, *, raises=None, results=None):
        # ``raises``: exception raised on EVERY call. ``results``: a list consumed
        # left-to-right, each item either an Exception (raised) or a value (returned).
        self._raises = raises
        self._results = list(results or [])
        self.calls = 0
        self.delays: list[float] = []

    async def fn(self):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


def _const_rand(value: float = 1.0):
    return lambda: value


# ── Classification ──────────────────────────────────────────────────────────
class TestClassification:
    @pytest.mark.parametrize("status", sorted(TRANSIENT_HTTP_STATUSES) + [0])
    def test_transient_statuses_are_retryable(self, status):
        assert default_is_retryable(FactoryClientError(status, "x")) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 501, 429])
    def test_deterministic_statuses_are_not_retryable(self, status):
        # NB: 429 is deliberately NOT retried (backpressure signal, not a blip).
        assert default_is_retryable(FactoryClientError(status, "x")) is False

    def test_cancelled_error_never_retryable(self):
        assert default_is_retryable(asyncio.CancelledError()) is False

    def test_plain_exception_not_retryable(self):
        assert default_is_retryable(ValueError("boom")) is False


# ── Retry firing + cap ──────────────────────────────────────────────────────
class TestRetryFires:
    def test_retries_503_up_to_cap_then_raises(self):
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError) as ei:
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=2,
                    base_delay_s=0.2, max_delay_s=5.0,
                    sleep=rec.sleep, rand=_const_rand(1.0),
                )
            assert ei.value.status_code == 503  # original error preserved, unwrapped

        run(body())
        assert rec.calls == 3            # 1 initial + 2 retries
        assert len(rec.delays) == 2      # one sleep between each retry, none after the last

    def test_retries_transport_timeout(self):
        # The factory surfaces a connect/read timeout as status_code == 0.
        rec = _Recorder(raises=FactoryClientError(0, "transport error: timeout"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=1,
                    base_delay_s=0.2, max_delay_s=5.0,
                    sleep=rec.sleep, rand=_const_rand(1.0),
                )

        run(body())
        assert rec.calls == 2
        assert len(rec.delays) == 1

    def test_zero_max_retries_means_single_attempt(self):
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=0, sleep=rec.sleep,
                )

        run(body())
        assert rec.calls == 1
        assert rec.delays == []


# ── Non-idempotent + non-transient are NOT retried ──────────────────────────
class TestNoRetry:
    def test_non_idempotent_call_is_not_retried(self):
        # A POST that may double-submit must run exactly once even on a 503.
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError) as ei:
                await call_with_retries(
                    rec.fn, idempotent=False, max_retries=5, sleep=rec.sleep,
                )
            assert ei.value.status_code == 503

        run(body())
        assert rec.calls == 1
        assert rec.delays == []

    def test_deterministic_404_not_retried_even_when_idempotent(self):
        rec = _Recorder(raises=FactoryClientError(404, "unknown artifact"))

        async def body():
            with pytest.raises(FactoryClientError) as ei:
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=5, sleep=rec.sleep,
                )
            assert ei.value.status_code == 404

        run(body())
        assert rec.calls == 1
        assert rec.delays == []

    def test_cancelled_error_propagates_immediately(self):
        rec = _Recorder(raises=asyncio.CancelledError())

        async def body():
            with pytest.raises(asyncio.CancelledError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=5, sleep=rec.sleep,
                )

        run(body())
        assert rec.calls == 1
        assert rec.delays == []


# ── Success paths ───────────────────────────────────────────────────────────
class TestSuccess:
    def test_immediate_success_no_sleep(self):
        rec = _Recorder(results=[{"ok": True}])

        async def body():
            out = await call_with_retries(rec.fn, idempotent=True, sleep=rec.sleep)
            assert out == {"ok": True}

        run(body())
        assert rec.calls == 1
        assert rec.delays == []

    def test_success_after_two_transient_failures(self):
        rec = _Recorder(results=[
            FactoryClientError(503, "unavailable"),
            FactoryClientError(0, "transport error"),
            {"artifact_id": "a1", "tests": []},
        ])

        async def body():
            out = await call_with_retries(
                rec.fn, idempotent=True, max_retries=3,
                base_delay_s=0.2, max_delay_s=5.0,
                sleep=rec.sleep, rand=_const_rand(1.0),
            )
            assert out == {"artifact_id": "a1", "tests": []}

        run(body())
        assert rec.calls == 3
        assert len(rec.delays) == 2


# ── Backoff shape: bounded + exponential ────────────────────────────────────
class TestBackoff:
    def test_backoff_is_exponential_and_capped(self):
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=5,
                    base_delay_s=1.0, max_delay_s=4.0,
                    sleep=rec.sleep, rand=_const_rand(1.0),  # rand=1 ⇒ delay == capped
                )

        run(body())
        # attempts 1..5 raise transient → 5 sleeps; capped exponential 1,2,4,4,4.
        assert rec.delays == [1.0, 2.0, 4.0, 4.0, 4.0]
        assert all(d <= 4.0 for d in rec.delays)

    def test_jitter_floor_lower_bounds_the_delay(self):
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=2,
                    base_delay_s=1.0, max_delay_s=10.0,
                    sleep=rec.sleep, rand=_const_rand(0.0),  # rand=0 ⇒ floor (0.5×capped)
                )

        run(body())
        # Equal jitter: [0.5*1, 0.5*2] = [0.5, 1.0]; always ≥ half the capped value.
        assert rec.delays == [0.5, 1.0]

    def test_total_wait_is_bounded_by_cap_times_maxdelay(self):
        rec = _Recorder(raises=FactoryClientError(504, "gateway timeout"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True, max_retries=4,
                    base_delay_s=0.5, max_delay_s=2.0,
                    sleep=rec.sleep, rand=_const_rand(1.0),
                )

        run(body())
        assert sum(rec.delays) <= 4 * 2.0 + 1e-9  # provably bounded


# ── Env default ─────────────────────────────────────────────────────────────
class TestEnvDefault:
    def test_default_max_retries_is_two(self, monkeypatch):
        monkeypatch.delenv("QEC_HTTP_MAX_RETRIES", raising=False)
        assert env_max_retries() == 2

    def test_env_override_respected(self, monkeypatch):
        monkeypatch.setenv("QEC_HTTP_MAX_RETRIES", "4")
        assert env_max_retries() == 4

    def test_bad_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("QEC_HTTP_MAX_RETRIES", "not-a-number")
        assert env_max_retries() == 2

    def test_uses_env_cap_when_max_retries_none(self, monkeypatch):
        monkeypatch.setenv("QEC_HTTP_MAX_RETRIES", "1")
        rec = _Recorder(raises=FactoryClientError(503, "unavailable"))

        async def body():
            with pytest.raises(FactoryClientError):
                await call_with_retries(
                    rec.fn, idempotent=True,  # max_retries omitted → read env (=1)
                    base_delay_s=0.0, max_delay_s=0.0, sleep=rec.sleep,
                )

        run(body())
        assert rec.calls == 2  # 1 initial + 1 env-configured retry
