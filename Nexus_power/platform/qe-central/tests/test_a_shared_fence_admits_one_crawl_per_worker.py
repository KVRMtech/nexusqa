"""PHASE 6 — WHILE THE EGRESS FENCE IS PER-WORKER, ADMISSION IS ONE PER WORKER.

THE DEFECT THIS MAKES UNREACHABLE, recorded in full by the strict xfail in
``tests/fleet/test_t_fl_08_concurrency_redteam.py``: the fence writer is keyed
on the worker and takes no crawl id, dispatch yields between the write and the
launch, so at capacity > 1 crawl B overwrites crawl A's allowlist inside A's
window and A runs against B's destinations — a cross-tenant egress leak. Latent
only because qec_022's server_default is "1"; nothing refused a larger value.
The Phase 0-4 closure record carries it as accepted-with-findings item 4, owner
seat vacant.

The clamp closes the REACHABLE PATH at the one seam every dispatch crosses —
worker eligibility — while the xfail keeps stating the underlying defect
against the writer itself. Two records, two different subjects, one pairing:
the tripwire below pins that the clamp exists for as long as the writer is
shared.
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


# ── the clamp ──────────────────────────────────────────────────────────────

def test_a_capacity_8_worker_with_one_crawl_in_flight_admits_no_second():
    """THE ONE THAT MATTERS. Capacity 8 is ordinary, documented configuration;
    admitting the second concurrent crawl is the exact sequence that re-fences
    a running crawl with another tenant's destinations."""
    assert wr.has_capacity(_worker(capacity=8, in_flight=1)) is False


def test_the_first_crawl_is_still_admitted():
    """FALSIFICATION CONTROL. A clamp that admits nothing would pass the test
    above and stop the fleet."""
    assert wr.has_capacity(_worker(capacity=8, in_flight=0)) is True
    assert wr.has_capacity(_worker(capacity=1, in_flight=0)) is True


def test_the_clamp_is_the_cause_not_an_accident(monkeypatch):
    """CONTROL FOR THE MECHANISM: with the flag off — the day the fence becomes
    per-crawl — registered capacity means capacity again, byte-for-byte the old
    semantics. This is what makes the clamp a posture, not a regression."""
    monkeypatch.setattr(wr, "FENCE_IS_PER_WORKER", False)
    assert wr.has_capacity(_worker(capacity=8, in_flight=1)) is True
    assert wr.has_capacity(_worker(capacity=8, in_flight=8)) is False


def test_a_dead_or_zero_capacity_worker_is_unchanged():
    assert wr.has_capacity(_worker(capacity=0, in_flight=0)) is False
    assert wr.has_capacity({"capacity": "junk", "in_flight": 0}) is False


# ── the fleet numbers tell the clamped truth ───────────────────────────────

def test_fleet_capacity_reports_the_room_that_actually_exists():
    """The queue must not be told there is room the scheduler will never
    grant: two capacity-8 workers are TWO slots while the fence is shared."""
    now = datetime.now(timezone.utc)
    workers = [_worker(capacity=8, in_flight=0, worker_id="w0"),
               _worker(capacity=8, in_flight=1, worker_id="w1")]
    got = wr.fleet_capacity(workers, now=now, ttl_s=60.0)
    assert got["capacity"] == 2
    assert got["in_flight"] == 1
    assert got["free"] == 1


def test_the_control_with_the_flag_off_the_registered_totals_return(monkeypatch):
    monkeypatch.setattr(wr, "FENCE_IS_PER_WORKER", False)
    now = datetime.now(timezone.utc)
    got = wr.fleet_capacity([_worker(capacity=8, in_flight=1)],
                            now=now, ttl_s=60.0)
    assert got["capacity"] == 8
    assert got["free"] == 7


# ── the pairing: writer shared ⇒ clamp present ─────────────────────────────

def test_the_clamp_stands_for_exactly_as_long_as_the_writer_is_shared():
    """THE TRIPWIRE PAIRING. The strict xfail records the defect against the
    writer; this pins that the admission clamp exists while that writer takes
    no crawl identifier. The day the fence becomes per-crawl, the xfail
    XPASSes, its marker comes off, and THIS test tells whoever did it to flip
    FENCE_IS_PER_WORKER off — so neither record can outlive its subject."""
    from app.routers.explorations import _write_egress_allowlist

    params = list(inspect.signature(_write_egress_allowlist).parameters)
    writer_is_shared = not any(
        "crawl" in p or "exploration" in p for p in params)
    if writer_is_shared:
        assert wr.FENCE_IS_PER_WORKER is True, (
            "the fence writer still takes no crawl id — removing the admission "
            "clamp while it is shared re-opens the cross-tenant egress leak at "
            "any capacity > 1")
    else:
        pytest.fail(
            "the fence writer now takes a per-crawl identifier: remove the "
            "strict xfail in test_t_fl_08, flip FENCE_IS_PER_WORKER to False, "
            "and delete this branch — capacity means capacity again")
