"""TEAM A / PHASE A (A2) — a queue with no REGISTERED workers says so.

THE FAILURE THIS PINS. Enabling the drainer (QEC_QUEUE_DRAIN_TICK_SECONDS > 0)
before any explorer has ever registered (A1) would make every tick dispatch
into the STATIC-POOL fallback: workers that declare no capacity, cannot
heartbeat, and may be running an image that never announces itself. A queued
crawl would cycle claim → 409 → requeue forever, the log would say nothing
about WHY, and "the drainer is on" would read as "the queue works".

So ``drain_once`` REFUSES the pass — loudly, with a named reason — while the
scheduling source is not the registry, and starts draining the moment a worker
registers. The reaper's queue-timeout remains the backstop that terminalizes
rows honestly if nothing ever registers.

These tests are PURE (monkeypatched source + plan), so the refusal holds in
every environment, not only where a database happens to be — and each has a
falsification control, because a refusal test that cannot go green when the
condition clears proves only that refusing is easy.
"""
from __future__ import annotations

import pytest

from app.controlplane.scheduling import queue_drainer, worker_registry as wr

pytestmark = pytest.mark.asyncio


def _worker(wid: str = "w0", **extra) -> dict:
    base = {
        "worker_id": wid, "url": f"http://{wid}:8210",
        "allowlist_path": f"/eg/{wid}/aw.txt", "capacity": 1, "in_flight": 0,
        "status": wr.STATUS_ACTIVE, "tenant_affinity": "",
        "last_heartbeat_at": wr.utc_now(),
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _reset_refusal_pacing(monkeypatch):
    """Each test starts at tick zero so the WARNING-pacing assertions hold."""
    monkeypatch.setattr(queue_drainer, "_refused_ticks", 0)
    monkeypatch.delenv(queue_drainer.ENV_DRAIN_STATIC_POOL, raising=False)
    yield


async def test_static_pool_source_refuses_the_pass_and_names_why(monkeypatch, caplog):
    """THE HEADLINE: no registered worker ⇒ the pass is refused, not spun."""
    async def _static(*, tenant_id):
        return [_worker()], "static_pool"

    dispatched: list = []

    async def _never_plan(**kw):  # pragma: no cover — must not be reached
        dispatched.append(kw)
        return []

    monkeypatch.setattr(queue_drainer.worker_registry,
                        "schedulable_workers", _static)
    monkeypatch.setattr(queue_drainer.queue_store, "plan_drain", _never_plan)

    import logging
    with caplog.at_level(logging.WARNING):
        out = await queue_drainer.drain_once()

    assert out.get("refused") == "registry_empty"
    assert out["started"] == 0
    assert not dispatched, (
        "the drainer PLANNED a drain against the static pool — it would have "
        "dispatched blind into workers that never registered")
    assert any("drain_refused_registry_empty" in r.message for r in caplog.records), (
        "the refusal was silent — an operator watching the log cannot tell "
        "'queue idle' from 'no worker has ever registered'")


async def test_registry_source_drains_the_falsification_control(monkeypatch):
    """CONTROL: the same pass with a REGISTERED source must plan a drain —
    otherwise the refusal test above would pass with the drainer broken."""
    async def _registry(*, tenant_id):
        return [_worker()], "registry"

    planned: list = []

    async def _plan(*, free_slots):
        planned.append(free_slots)
        return []                       # nothing queued — the plan ran, that counts

    monkeypatch.setattr(queue_drainer.worker_registry,
                        "schedulable_workers", _registry)
    monkeypatch.setattr(queue_drainer.queue_store, "plan_drain", _plan)

    out = await queue_drainer.drain_once()
    assert out.get("refused") is None
    assert planned == [1], (
        f"a populated registry with one free slot planned {planned!r} — the "
        "drainer refused (or over-planned) work it should have drained")


async def test_the_operator_override_still_drains_the_static_pool(monkeypatch):
    """QEC_QUEUE_DRAIN_STATIC_POOL=1 is the explicit, named escape hatch."""
    async def _static(*, tenant_id):
        return [_worker()], "static_pool"

    planned: list = []

    async def _plan(*, free_slots):
        planned.append(free_slots)
        return []

    monkeypatch.setattr(queue_drainer.worker_registry,
                        "schedulable_workers", _static)
    monkeypatch.setattr(queue_drainer.queue_store, "plan_drain", _plan)
    monkeypatch.setenv(queue_drainer.ENV_DRAIN_STATIC_POOL, "1")

    out = await queue_drainer.drain_once()
    assert out.get("refused") is None and planned == [1], (
        "the documented static-pool override did not drain")


async def test_refusals_are_paced_not_per_tick_spam(monkeypatch, caplog):
    """An hour of empty registry is ~720 ticks; the log must stay legible."""
    async def _static(*, tenant_id):
        return [_worker()], "static_pool"

    monkeypatch.setattr(queue_drainer.worker_registry,
                        "schedulable_workers", _static)

    import logging
    with caplog.at_level(logging.WARNING):
        for _ in range(queue_drainer._REFUSAL_LOG_EVERY + 1):
            await queue_drainer.drain_once()
    warnings = [r for r in caplog.records
                if "drain_refused_registry_empty" in r.message]
    assert 1 <= len(warnings) <= 2, (
        f"{len(warnings)} refusal WARNINGs for {queue_drainer._REFUSAL_LOG_EVERY + 1} "
        "ticks — either silent or log spam; both hide the incident")


async def test_recovery_is_announced(monkeypatch, caplog):
    """The transition empty → populated is one WARNING, so the log records
    WHEN draining actually began."""
    calls = {"n": 0}

    async def _flip(*, tenant_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_worker()], "static_pool"
        return [_worker()], "registry"

    async def _plan(*, free_slots):
        return []

    monkeypatch.setattr(queue_drainer.worker_registry,
                        "schedulable_workers", _flip)
    monkeypatch.setattr(queue_drainer.queue_store, "plan_drain", _plan)

    import logging
    with caplog.at_level(logging.WARNING):
        await queue_drainer.drain_once()
        await queue_drainer.drain_once()
    assert any("drain_registry_populated" in r.message for r in caplog.records), (
        "the registry becoming populated was not announced — nobody can tell "
        "from the log when the queue actually started draining")
