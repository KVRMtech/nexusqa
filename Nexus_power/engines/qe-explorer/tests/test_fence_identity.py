"""TEAM A / PHASE A — the CONSUMER half of the per-crawl egress fence.

The frozen shape is ``contracts/fleet_egress_fence_v1.json``; qe-central's
producer half is ``platform/qe-central/tests/unit/test_egress_fence_per_crawl.py``.
This side owns two consumers:

  * the SHIPPED squid.conf — asserted as BYTES, because squid is configured by
    this repository file and a drifted conf is a drifted fence (the ARB record
    was explicit: the consumer is where the old design was broken);
  * the browser context's PROXY IDENTITY (app/fence.py) — the username squid
    keys the fence on.

Plus the capacity semantics that make two concurrent crawls on one worker a
configuration instead of a leak: the JobManager admits exactly ``capacity``
crawls, and the health payload reports the same numbers the heartbeat carries.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import fence
from app.config import settings
from app.main import JobManager, app

_SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _contract() -> dict:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / "fleet_egress_fence_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError("contracts/fleet_egress_fence_v1.json not found")


CONTRACT = _contract()


# ── the proxy identity ─────────────────────────────────────────────────────

def test_the_context_proxy_identity_is_the_crawl_id():
    cid = "9c21b4e0a1b2c3d4e5f60718293a4b5c"
    p = fence.proxy_settings(cid, "http://qec-egress-proxy:3128")
    assert p == {
        "server": "http://qec-egress-proxy:3128",
        "username": CONTRACT["proxy_identity"]["username"].format(crawl_id=cid),
        "password": CONTRACT["proxy_identity"]["password"],
    }


def test_the_id_pattern_matches_the_contract_and_the_producer():
    assert fence.CRAWL_ID_RE.pattern == CONTRACT["crawl_id_pattern"], (
        "the explorer's crawl-id pattern drifted from the contract — "
        "qe-central would fence a crawl this side refuses to identify (or "
        "vice versa), and the crawl reaches nothing")


def test_an_unfenceable_crawl_id_is_refused_before_a_browser_exists():
    for bad in ("", "has space", 'x"quote', "a/slash", "-leading", "x" * 51):
        with pytest.raises(fence.FenceIdentityError):
            fence.proxy_settings(bad, "http://p:3128")


def test_no_proxy_configured_means_no_identity_not_a_crash():
    assert fence.proxy_settings("c" * 32, "") is None


def test_the_run_job_context_carries_the_identity():
    """Source-order pin: the per-context proxy is derived from fence.proxy_settings
    and installed on the context kwargs BEFORE new_context — a refactor that
    drops it silently reverts every crawl to the shared launch identity."""
    from app.main import _run_job
    src = inspect.getsource(_run_job)
    proxy_at = src.index("fence.proxy_settings(req.crawl_id")
    ctx_at = src.index("browser.new_context(")
    assert proxy_at < ctx_at, (
        "the crawl's proxy identity is set after the context is created — "
        "the browser would authenticate as nothing and be fenced to nothing")


# ── the shipped squid.conf, as bytes ───────────────────────────────────────

def test_squid_conf_selects_the_fence_per_crawl():
    conf = (_SERVICE_ROOT / "squid.conf").read_text(encoding="utf-8")
    for needle in CONTRACT["squid_conf_must_contain"]:
        assert needle in conf, (
            f"squid.conf lost {needle!r} — the per-crawl fence selection is "
            "not what the proxy would actually enforce")
    for needle in CONTRACT["squid_conf_must_not_contain"]:
        assert needle not in conf, (
            f"squid.conf still carries {needle!r} — the legacy shared "
            "per-worker file would fence every crawl on the worker again "
            "(the exact T-FL-08 leak)")


def test_squid_conf_denies_before_it_allows():
    """The 407 challenge must come BEFORE the per-crawl allows, and deny all
    must come after them — order is enforcement in squid."""
    conf = (_SERVICE_ROOT / "squid.conf").read_text(encoding="utf-8")
    challenge = conf.index("http_access deny !fenced_crawl")
    include = conf.index("include /etc/squid/allowlist/crawls.conf")
    deny_all = conf.index("http_access deny all")
    assert challenge < include < deny_all


# ── capacity semantics (what makes concurrency a configuration) ────────────

def test_default_capacity_is_the_proven_single_flight():
    jobs = JobManager()
    assert jobs.capacity == 1
    jobs.reserve("a" * 32, "t1")
    with pytest.raises(HTTPException) as exc:
        jobs.reserve("b" * 32, "t2")
    assert exc.value.status_code == 409


def test_capacity_two_admits_two_and_refuses_the_third():
    jobs = JobManager(capacity=2)
    jobs.reserve("a" * 32, "t1")
    jobs.reserve("b" * 32, "t2")            # the second crawl is ADMITTED
    with pytest.raises(HTTPException) as exc:
        jobs.reserve("c" * 32, "t3")
    assert exc.value.status_code == 409, "capacity is not a cap"
    assert jobs.active_count == 2
    # ownership survives concurrency: each reservation kept its tenant
    assert jobs.owner("a" * 32) == "t1" and jobs.owner("b" * 32) == "t2"


def test_releasing_one_of_two_frees_exactly_one_slot():
    jobs = JobManager(capacity=2)
    jobs.reserve("a" * 32, "t1")
    jobs.reserve("b" * 32, "t2")
    jobs.release("a" * 32, "t1")
    assert jobs.active_count == 1
    jobs.reserve("c" * 32, "t3")            # the freed slot is reusable
    assert jobs.busy


def test_finish_frees_a_slot_at_any_capacity():
    jobs = JobManager(capacity=2)
    jobs.reserve("a" * 32, "t1")
    jobs.reserve("b" * 32, "t2")
    jobs.finish("a" * 32, None)
    assert jobs.active_count == 1 and not jobs.busy
    assert jobs.state("a" * 32) == "finished"


def test_health_reports_the_same_numbers_the_heartbeat_carries():
    """Driven through the REAL lifespan (refuse pack loaded, announcer task
    started-and-disabled via QEC_FLEET_REGISTER=0 from conftest), so the test
    proves the running service's health payload, not a hand-built app.state."""
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=False) as client:
        jobs = JobManager(capacity=2)
        app.state.jobs = jobs
        jobs.reserve("a" * 32, "t1")
        body = client.get("/health").json()
        assert body["capacity"] == 2
        assert body["in_flight"] == 1
        assert body["busy"] is False
