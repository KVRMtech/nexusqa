"""Explorer WORKER POOL foundation (safe concurrency, step 1-3).

Pins the two invariants that keep concurrency safe:
  * the pool DEFAULTS to the single (explorer_url, egress_allowlist_path) worker —
    byte-identical to the pre-pool behavior, and never fail-open on a bad config;
  * each worker writes its OWN egress allowlist file — per-worker isolation, so
    concurrent crawls can never race/clobber a shared fence.
"""
import json

import pytest
from fastapi import HTTPException

from app.clients.config import Phase1Settings
from app.routers.explorations import _write_egress_allowlist


def test_workers_defaults_to_single_worker():
    s = Phase1Settings(explorer_pool="")
    w = s.workers()
    assert len(w) == 1
    assert w[0]["url"] == s.explorer_url
    assert w[0]["allowlist_path"] == s.egress_allowlist_path


def test_workers_parses_pool_with_isolated_allowlists():
    pool = json.dumps([
        {"url": "http://qe-explorer-1:8210", "allowlist_path": "/qec/eg/aw-1.txt"},
        {"url": "http://qe-explorer-2:8210", "allowlist_path": "/qec/eg/aw-2.txt"},
    ])
    w = Phase1Settings(explorer_pool=pool).workers()
    assert [x["url"] for x in w] == ["http://qe-explorer-1:8210", "http://qe-explorer-2:8210"]
    # per-worker egress isolation: every worker has a DISTINCT allowlist file.
    assert len({x["allowlist_path"] for x in w}) == len(w) == 2


def test_workers_drops_incomplete_entries_and_falls_back_on_garbage():
    # an entry without its own allowlist_path is dropped (never share a fence).
    partial = json.dumps([{"url": "http://x:1"}, {"url": "http://y:2", "allowlist_path": "/a"}])
    assert Phase1Settings(explorer_pool=partial).workers() == [{"url": "http://y:2", "allowlist_path": "/a"}]
    # malformed JSON ⇒ single-worker fallback (byte-identical), never fail-open.
    assert len(Phase1Settings(explorer_pool="{not json").workers()) == 1
    # a pool that yields ZERO valid workers also falls back to the single worker.
    assert len(Phase1Settings(explorer_pool=json.dumps([{"url": "http://x:1"}])).workers()) == 1


def test_write_egress_allowlist_is_per_crawl_isolated(tmp_path):
    """Team A / Phase A: the fence is PER-CRAWL now (fleet_egress_fence_v1) —
    two crawls on TWO workers, and two crawls on ONE worker, each read back
    only their own destinations."""
    from app.controlplane.scheduling import egress_fence

    p1, p2 = tmp_path / "w1" / "aw.txt", tmp_path / "w2" / "aw.txt"
    _write_egress_allowlist(["a.example"], str(p1), crawl_id="ca")
    _write_egress_allowlist(["b.example"], str(p2), crawl_id="cb")
    a_body = egress_fence.crawl_fence_path(str(p1), "ca").read_text()
    b_body = egress_fence.crawl_fence_path(str(p2), "cb").read_text()
    assert "a.example" in a_body and "b.example" not in a_body
    assert "b.example" in b_body and "a.example" not in b_body
    # and WITHIN one worker: a second concurrent crawl no longer overwrites.
    _write_egress_allowlist(["c.example"], str(p1), crawl_id="cc")
    assert "a.example" in egress_fence.crawl_fence_path(str(p1), "ca").read_text()
    assert "c.example" in egress_fence.crawl_fence_path(str(p1), "cc").read_text()


def test_write_egress_allowlist_refuses_empty_fail_closed():
    with pytest.raises(HTTPException):
        _write_egress_allowlist([], str("/tmp/qec-should-not-write.txt"),
                                crawl_id="c1")
