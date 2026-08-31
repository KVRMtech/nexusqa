"""THE FENCE↔CAPACITY PAIRING — guarded in BOTH directions.

HISTORY (short, because the guards are live). While the egress fence writer
was keyed on the WORKER (``_write_egress_allowlist`` took no crawl id),
concurrent crawls on one worker overwrote each other's live fence — the
cross-tenant leak recorded by the strict xfail in
``tests/fleet/test_t_fl_08_concurrency_redteam.py``. This file then pinned the
pairing *writer shared ⇒ admission clamp present* (``FENCE_IS_PER_WORKER``),
and its tripwire told whoever repaired the fence the exact order: make the
writer per-crawl, watch the xfail XPASS, remove the marker, flip the flag.

TEAM A / PHASE A did exactly that (2026-08-31): the writer takes ``crawl_id``
and writes one dstdomain file per crawl; squid selects each fence by the
crawl's PROXY LOGIN (``contracts/fleet_egress_fence_v1.json``); the xfail
XPASSed (``[XPASS(strict)]`` in the run log) and its marker is gone;
``FENCE_IS_PER_WORKER`` is False.

WHAT THIS FILE PINS NOW — the same seam, both directions, so neither half can
drift without the build saying so:

  * writer PER-CRAWL (today) ⇒ the clamp must be OFF: capacity means capacity,
    and the fleet numbers report the registered totals (a clamp left on would
    silently serialise every worker to one crawl and make the queue starve a
    fleet that has room);
  * writer ever SHARED again (a revert) ⇒ the clamp must come BACK before the
    build goes green — the leak must never be reachable while nothing fences
    per crawl.

The squid-side half of the tripwire (the CONSUMER must select fences per
crawl while capacity is unclamped) lives in
``tests/contract/test_egress_fence_per_crawl_tripwire.py``.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from app.controlplane.scheduling import worker_registry as wr


def _worker(capacity, in_flight, **extra):
    base = {
        "worker_id": "w0", "status": "active", "capacity": capacity,
        "in_flight": in_flight,
        "last_heartbeat_at": datetime.now(timezone.utc),
        "tenant_scope": [],
    }
    base.update(extra)
    return base


# ── capacity means capacity ────────────────────────────────────────────────

def test_a_capacity_8_worker_with_one_crawl_in_flight_admits_the_second():
    """THE ONE THAT FLIPPED. Under the per-worker fence this exact admission
    was the leak sequence; under the per-crawl fence it is ordinary scheduling
    and refusing it would starve a fleet that has room."""
    assert wr.has_capacity(_worker(capacity=8, in_flight=1)) is True
    assert wr.has_capacity(_worker(capacity=8, in_flight=8)) is False


def test_the_first_crawl_is_still_admitted():
    """FALSIFICATION CONTROL, kept from the clamped era: an admission gate
    that admits nothing would pass a wrongly-inverted test."""
    assert wr.has_capacity(_worker(capacity=1, in_flight=0)) is True
    assert wr.has_capacity(_worker(capacity=1, in_flight=1)) is False


def test_a_dead_or_zero_capacity_worker_is_unchanged():
    assert wr.has_capacity(_worker(capacity=0, in_flight=0)) is False
    assert wr.has_capacity({"capacity": "junk", "in_flight": 0}) is False


# ── the fleet numbers tell the registered truth ────────────────────────────

def test_fleet_capacity_reports_the_registered_totals():
    """The queue is told the room that ACTUALLY exists — the registered
    capacities, now that admission grants them."""
    now = datetime.now(timezone.utc)
    workers = [_worker(capacity=8, in_flight=0, worker_id="w0"),
               _worker(capacity=8, in_flight=1, worker_id="w1")]
    got = wr.fleet_capacity(workers, now=now, ttl_s=60.0)
    assert got["capacity"] == 16
    assert got["in_flight"] == 1
    assert got["free"] == 15


def test_the_control_with_the_flag_on_the_clamp_still_works(monkeypatch):
    """The REVERT PATH must still function: if the writer ever regresses and
    the flag returns to True, one slot per worker must again be the truth the
    queue is told. A clamp that rotted while unused would make the revert a
    no-op exactly when it mattered."""
    monkeypatch.setattr(wr, "FENCE_IS_PER_WORKER", True)
    now = datetime.now(timezone.utc)
    assert wr.has_capacity(_worker(capacity=8, in_flight=1)) is False
    got = wr.fleet_capacity([_worker(capacity=8, in_flight=1)],
                            now=now, ttl_s=60.0)
    assert got["capacity"] == 1
    assert got["free"] == 0


# ── the tripwire pairing: writer shape ⇔ clamp state ───────────────────────

def test_the_clamp_state_matches_the_writer_shape():
    """THE TRIPWIRE, INVERTED AND KEPT. Whichever way the writer drifts, the
    flag must follow — in the unsafe direction the build fails until it does."""
    from app.routers.explorations import _write_egress_allowlist

    params = list(inspect.signature(_write_egress_allowlist).parameters)
    writer_is_shared = not any(
        "crawl" in p or "exploration" in p for p in params)
    if writer_is_shared:
        assert wr.FENCE_IS_PER_WORKER is True, (
            "THE FENCE WRITER LOST ITS CRAWL ID while the admission clamp is "
            "off — every capacity>1 worker is one overwrite away from the "
            "T-FL-08 cross-tenant egress leak again. Either restore the "
            "per-crawl writer (contracts/fleet_egress_fence_v1.json) or flip "
            "FENCE_IS_PER_WORKER back to True before anything else merges.")
    else:
        assert wr.FENCE_IS_PER_WORKER is False, (
            "the fence writer is per-crawl but the admission clamp is still "
            "on: every worker is silently serialised to one crawl, the fleet "
            "numbers under-report, and queued crawls wait for room that "
            "exists. Flip FENCE_IS_PER_WORKER to False (see the flag's "
            "docstring for the history).")
