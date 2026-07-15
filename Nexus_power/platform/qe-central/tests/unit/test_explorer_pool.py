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


def test_write_egress_allowlist_is_per_path_isolated(tmp_path):
    p1, p2 = tmp_path / "aw1.txt", tmp_path / "aw2.txt"
    _write_egress_allowlist(["a.example"], str(p1))
    _write_egress_allowlist(["b.example"], str(p2))
    # each worker's file holds ONLY its own crawl's hosts — no cross-worker bleed.
    assert "a.example" in p1.read_text() and "b.example" not in p1.read_text()
    assert "b.example" in p2.read_text() and "a.example" not in p2.read_text()


def test_write_egress_allowlist_refuses_empty_fail_closed():
    with pytest.raises(HTTPException):
        _write_egress_allowlist([], str("/tmp/qec-should-not-write.txt"))
