"""R4 (qe-central half) — the DISPATCH ORDERING that makes the fence safe.

The worker enforces the reservation (see
``engines/qe-explorer/tests/security/test_r4_worker_reservation_race.py``).
This file pins the caller's half: qe-central must RESERVE before it writes a
worker's squid allowlist, must hand the slot back when a dispatch aborts, and
must never fall back to fencing a worker it does not hold.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.clients.explorer_client import ExplorerDispatchError
from app.routers import explorations


# ── the ordering, read off the source ──────────────────────────────────────

def test_the_reservation_precedes_the_fence_write_textually():
    """The bug was an ORDER, so the order is what is pinned.

    A reader (or a future refactor) must not be able to move the allowlist write
    above the reservation without this failing."""
    src = inspect.getsource(explorations._dispatch_explorer)
    reserve_at = src.index("reserve_worker(")
    fence_at = src.index("_write_egress_allowlist(allowed_hosts")
    assert reserve_at < fence_at, "the fence is written before the slot is held"


def test_a_failed_dispatch_releases_the_slot():
    src = inspect.getsource(explorations._dispatch_explorer)
    assert src.count("release_worker(") >= 2   # error path AND unexpected path


def test_the_loop_moves_on_without_touching_a_busy_workers_fence():
    """A 409 must ``continue`` from the RESERVATION, not from the dispatch."""
    src = inspect.getsource(explorations._dispatch_explorer)
    head = src[:src.index("_write_egress_allowlist(allowed_hosts")]
    assert "continue" in head, "a busy worker is not skipped before the fence write"


# ── the ordering, exercised ────────────────────────────────────────────────

class _Recorder:
    """Records the sequence of side effects a dispatch performs."""

    def __init__(self, *, reserve_ok: bool, dispatch_raises=None):
        self.events: list[str] = []
        self.reserve_ok = reserve_ok
        self.dispatch_raises = dispatch_raises

    async def reserve_worker(self, *, explorer_url, crawl_id, tenant_id):
        self.events.append(f"reserve:{tenant_id}")
        return self.reserve_ok

    async def release_worker(self, *, explorer_url, crawl_id, tenant_id):
        self.events.append(f"release:{tenant_id}")

    def write_allowlist(self, domains, path):
        self.events.append("fence")

    async def dispatch_crawl(self, request, *, explorer_url=None):
        self.events.append("dispatch")
        if self.dispatch_raises is not None:
            raise self.dispatch_raises
        return type("R", (), {"accepted": True})()


async def _run_loop(rec: _Recorder, workers: list[dict], tenant: str) -> str:
    """A faithful transcription of the dispatch loop's control flow.

    Kept in this file (rather than calling ``_dispatch_explorer``, which needs a
    database, an envelope service and a live app) so the ORDER can be exercised
    directly; the structural tests above pin it to the real implementation."""
    result, last = None, None
    for worker in workers:
        try:
            reserved = await rec.reserve_worker(
                explorer_url=worker["url"], crawl_id="c" * 32, tenant_id=tenant)
        except ExplorerDispatchError as exc:
            last = exc
            if exc.status_code in (409, 502):
                continue
            break
        if not reserved:
            last = ExplorerDispatchError("busy", status_code=409)
            continue
        try:
            rec.write_allowlist(["a.example"], worker["allowlist_path"])
            result = await rec.dispatch_crawl(None, explorer_url=worker["url"])
            last = None
            break
        except ExplorerDispatchError as exc:
            last = exc
            await rec.release_worker(explorer_url=worker["url"],
                                     crawl_id="c" * 32, tenant_id=tenant)
            if exc.status_code in (409, 502):
                continue
            break
    return "dispatched" if result is not None else f"failed:{last}"


WORKERS = [{"url": "http://w1:8210", "allowlist_path": "/eg/w1.txt"}]


def test_a_busy_worker_is_never_fenced():
    """THE property: reservation refused ⇒ the allowlist file is not opened."""
    rec = _Recorder(reserve_ok=False)
    outcome = asyncio.run(_run_loop(rec, WORKERS, "tenant-b"))
    assert outcome.startswith("failed")
    assert "fence" not in rec.events
    assert rec.events == ["reserve:tenant-b"]


def test_a_held_worker_is_fenced_then_dispatched_in_that_order():
    """POSITIVE half — the legitimate path still runs, in the safe order."""
    rec = _Recorder(reserve_ok=True)
    assert asyncio.run(_run_loop(rec, WORKERS, "tenant-a")) == "dispatched"
    assert rec.events == ["reserve:tenant-a", "fence", "dispatch"]


def test_an_aborted_dispatch_hands_the_slot_back():
    """A wedged worker after a failed dispatch is an outage, not a security
    property — but leaving it wedged would push operators toward turning the
    reservation off, which is."""
    rec = _Recorder(reserve_ok=True,
                    dispatch_raises=ExplorerDispatchError("boom", status_code=502))
    outcome = asyncio.run(_run_loop(rec, WORKERS, "tenant-a"))
    assert outcome.startswith("failed")
    assert rec.events == ["reserve:tenant-a", "fence", "dispatch", "release:tenant-a"]


def test_two_workers_only_the_free_one_is_fenced():
    """With a pool, a busy worker is skipped BEFORE its fence is touched."""
    class _TwoWorker(_Recorder):
        def __init__(self):
            super().__init__(reserve_ok=True)
            self.calls = 0

        async def reserve_worker(self, *, explorer_url, crawl_id, tenant_id):
            self.calls += 1
            self.events.append(f"reserve:{explorer_url}")
            return self.calls > 1        # first worker busy, second free

    rec = _TwoWorker()
    workers = [
        {"url": "http://w1:8210", "allowlist_path": "/eg/w1.txt"},
        {"url": "http://w2:8210", "allowlist_path": "/eg/w2.txt"},
    ]
    assert asyncio.run(_run_loop(rec, workers, "tenant-a")) == "dispatched"
    assert rec.events == ["reserve:http://w1:8210", "reserve:http://w2:8210",
                          "fence", "dispatch"]


# ── the client refuses to fence a worker that cannot reserve ───────────────

def test_an_old_worker_image_fails_closed(monkeypatch):
    """A worker without the reservation endpoint answers 404.

    Dispatching anyway would silently restore the race on a mixed-version pool,
    so it is a hard refusal with an operator-actionable message."""
    import httpx

    from app.clients import explorer_client

    class _Resp:
        status_code = 404

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(ExplorerDispatchError) as exc:
        asyncio.run(explorer_client.reserve_worker(
            explorer_url="http://w1:8210", crawl_id="c" * 32, tenant_id="t"))
    assert exc.value.status_code == 503
    assert "reservation" in str(exc.value)


def test_reserving_without_a_fleet_token_is_refused(monkeypatch):
    from app.clients import explorer_client
    from app.clients.config import phase1_settings

    monkeypatch.setattr(phase1_settings, "explorer_token", "")
    with pytest.raises(ExplorerDispatchError) as exc:
        asyncio.run(explorer_client.reserve_worker(
            explorer_url="http://w1:8210", crawl_id="c" * 32, tenant_id="t"))
    assert exc.value.status_code == 503


def test_liveness_without_a_tenant_reports_unknown_not_dead():
    """The reaper must never kill a crawl it could not actually check.

    The worker's status endpoint is owner-scoped now, so a liveness probe with
    no tenant would be refused — and treating a refusal as 'dead' would let the
    reaper terminalise healthy crawls."""
    from app.clients import explorer_client

    assert asyncio.run(explorer_client.crawl_liveness("c" * 32, "")) == "unknown"


def test_the_reaper_passes_the_owning_tenant():
    from app.controlplane import reaper

    src = inspect.getsource(reaper)
    assert 'crawl_liveness(\n            crawl_id, str(row.get("tenant_id") or ""))' in src
