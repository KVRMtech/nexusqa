"""M1.7 / T-GW-02 — FAULT INJECTION on the completion callback.

The delivery path is where a finished crawl was being lost.  These tests drive
``app.main._deliver_completion`` and ``_sweep_orphaned_completions`` against a
scripted HTTP client that can fail in each way the real world fails: refused
connections, gateway errors, a rejected signature, and a receiver that has
already seen this crawl.

WHAT EACH FAULT MUST PRODUCE:

    transport error / 5xx  ->  retried, then left as a recoverable ORPHAN
    4xx                    ->  not retried, left as a VISIBLE orphan
    2xx (incl. duplicate)  ->  acknowledged, orphan cleared
    process death          ->  the next process's sweep recovers it

The last one is the whole point, and it is why the durable record is written
BEFORE the first POST rather than after the last one fails.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import completion_manifest, main


class ScriptedHTTP:
    """An ``httpx.AsyncClient`` stand-in that replays a scripted outcome list.

    Each entry is an int status code or an exception INSTANCE to raise.  The
    list is consumed in order and the last entry repeats, so a test can say
    "fail twice then succeed" or "always refuse" without counting attempts.
    """

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.posts: list = []

    async def post(self, url, *, content=None, headers=None):
        self.posts.append({"url": url, "content": content,
                           "signature": (headers or {}).get("X-QEC-Signature", "")})
        outcome = self._outcomes[min(len(self.posts) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(int(outcome), request=httpx.Request("POST", url))


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """Point the explorer's work dir at a scratch volume, and make backoff free.

    The backoff schedule is asserted directly in ``test_greenwash_holes``; here
    it is neutralised so a five-attempt fault-injection test does not spend
    thirty seconds of wall clock proving something about arithmetic.
    """
    monkeypatch.setattr(main.settings, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(main.settings, "callback_url", "http://qe-central:8093",
                        raising=False)
    monkeypatch.setattr(completion_manifest, "backoff_delay", lambda *a, **k: 0.0)
    return tmp_path


def _install(monkeypatch, client) -> None:
    monkeypatch.setattr(main.app.state, "http", client, raising=False)


BODY = {"crawl_id": "c1", "tenant_id": "t1", "exploration_id": "e1",
        "stop_reason": "completed"}


def test_a_successful_delivery_is_acknowledged(volume, monkeypatch):
    client = ScriptedHTTP([200])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    assert asyncio.run(main._deliver_completion("c1", BODY)) is True
    assert len(client.posts) == 1
    assert completion_manifest.is_delivered(str(volume), "c1")
    assert not completion_manifest.is_orphaned(str(volume), "c1")


def test_a_transient_failure_is_retried_and_then_lands(volume, monkeypatch):
    client = ScriptedHTTP([httpx.ConnectError("connection refused"), 502, 200])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    assert asyncio.run(main._deliver_completion("c1", BODY)) is True
    assert len(client.posts) == 3
    assert completion_manifest.is_delivered(str(volume), "c1")
    # Every attempt is in the durable log, so the recovery is OBSERVABLE.
    attempts = completion_manifest.read_attempts(str(volume), "c1")
    assert [a["ok"] for a in attempts] == [False, False, True]


def test_every_attempt_is_signed_afresh(volume, monkeypatch):
    """LOAD-BEARING.  The v2 envelope carries a SINGLE-USE NONCE (T-SEC-06), so
    re-POSTing one signed envelope is a replay and the receiver refuses it.  A
    retry loop that signed once would be a retry loop that could only ever fail
    — the retry would look implemented and never work."""
    client = ScriptedHTTP([503, 503, 200])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    asyncio.run(main._deliver_completion("c1", BODY))

    signatures = [p["signature"] for p in client.posts]
    assert len(signatures) == 3
    assert all(signatures), "every attempt must carry a signature"
    assert len(set(signatures)) == 3, "a reused envelope is a replay the receiver refuses"
    # The BODY is byte-identical across attempts — only the envelope changes.
    assert len({p["content"] for p in client.posts}) == 1


def test_an_exhausted_delivery_leaves_a_recoverable_orphan(volume, monkeypatch):
    client = ScriptedHTTP([httpx.ConnectError("qe-central is down")])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    assert asyncio.run(main._deliver_completion("c1", BODY)) is False
    assert client.posts, "it really tried"
    # THE POINT: the crawl is not lost. Its evidence and its completion record
    # are both on the volume, and it is discoverable as an orphan.
    assert completion_manifest.is_orphaned(str(volume), "c1")
    assert completion_manifest.read_completion(str(volume), "c1") == BODY


def test_a_client_error_is_not_retried(volume, monkeypatch):
    """A bad signature, an unknown crawl or a malformed body fails identically
    forever.  Spending five attempts proving that only delays the honest report."""
    client = ScriptedHTTP([404])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    assert asyncio.run(main._deliver_completion("c1", BODY)) is False
    assert len(client.posts) == 1
    assert completion_manifest.is_orphaned(str(volume), "c1")


def test_a_duplicate_delivery_is_a_successful_delivery(volume, monkeypatch):
    """The receiver is idempotent on terminal status and answers 2xx for a crawl
    it has already landed.  Treating that as a failure would leave a completed
    crawl on the orphan list forever, re-delivered on every single sweep."""
    client = ScriptedHTTP([200])
    _install(monkeypatch, client)
    completion_manifest.write_completion(str(volume), "c1", BODY)

    assert asyncio.run(main._deliver_completion("c1", BODY)) is True
    assert asyncio.run(main._deliver_completion("c1", BODY)) is True
    assert completion_manifest.is_delivered(str(volume), "c1")


# ── the sweeper: recovery that survives the process that owned the delivery ──


def test_the_sweeper_recovers_a_completion_left_by_a_dead_process(volume, monkeypatch):
    """T-GW-02 ACCEPTANCE.

    Simulates the real sequence: a crawl finishes, writes its durable
    completion, and the process dies before the POST lands.  A NEW process
    starts, scans the volume, and delivers it.  No state is carried in memory
    between the two — which is the whole reason this works.
    """
    completion_manifest.write_completion(str(volume), "c1", BODY)
    completion_manifest.record_attempt(str(volume), "c1", attempt=1, ok=False,
                                       error="process died mid-delivery")

    client = ScriptedHTTP([200])
    _install(monkeypatch, client)

    assert asyncio.run(main._sweep_orphaned_completions()) == 1
    assert completion_manifest.is_delivered(str(volume), "c1")
    assert "/crawls/c1/" in client.posts[0]["url"]


def test_the_sweeper_recovers_every_orphan_on_the_volume(volume, monkeypatch):
    for n in range(3):
        completion_manifest.write_completion(
            str(volume), f"c{n}",
            {**BODY, "crawl_id": f"c{n}", "exploration_id": f"e{n}"})
    completion_manifest.mark_delivered(str(volume), "c1")     # one already landed

    client = ScriptedHTTP([200])
    _install(monkeypatch, client)

    assert asyncio.run(main._sweep_orphaned_completions()) == 2
    assert len(client.posts) == 2                              # the acked one is skipped


def test_the_sweeper_is_a_no_op_on_a_healthy_volume(volume, monkeypatch):
    completion_manifest.write_completion(str(volume), "c1", BODY)
    completion_manifest.mark_delivered(str(volume), "c1")

    client = ScriptedHTTP([500])
    _install(monkeypatch, client)

    assert asyncio.run(main._sweep_orphaned_completions()) == 0
    assert client.posts == []


def test_an_unroutable_completion_is_reported_not_retried(volume, monkeypatch):
    """A record missing crawl_id / tenant_id / exploration_id cannot be routed by
    qe-central.  Retrying it into a permanent 404 wastes attempts and buries the
    fact that something on the volume needs a human."""
    completion_manifest.write_completion(str(volume), "c1", {"crawl_id": "c1"})

    client = ScriptedHTTP([200])
    _install(monkeypatch, client)

    assert asyncio.run(main._sweep_orphaned_completions()) == 0
    assert client.posts == []
    assert completion_manifest.is_orphaned(str(volume), "c1"), \
        "it stays visible so an operator can see it"


def test_a_failed_crawls_completion_is_delivered_too(volume, monkeypatch):
    """An orphaned FAILURE is a row that spins in the UI forever.  Delivery is
    about the crawl reaching a terminal state, not about it succeeding."""
    failed = {**BODY, "stop_reason": "inventory_failed", "disposition": "failed"}
    completion_manifest.write_completion(str(volume), "c1", failed)

    client = ScriptedHTTP([200])
    _install(monkeypatch, client)

    assert asyncio.run(main._sweep_orphaned_completions()) == 1
    assert completion_manifest.is_delivered(str(volume), "c1")


# ── the verdict must survive the wire, or qe-central's half is inert ─────────


def _body_for(summary, monkeypatch, volume):
    """Run the real ``_fire_callback`` and return the body it durably recorded."""
    _install(monkeypatch, ScriptedHTTP([200]))
    req = main.ExploreRequest(crawl_id="c1", tenant_id="t1", exploration_id="e1",
                              target_url="https://app.test/home")
    asyncio.run(main._fire_callback(req, summary, ""))
    return completion_manifest.read_completion(str(volume), "c1")


def test_the_adjudicated_verdict_reaches_the_callback(volume, monkeypatch):
    """THE SEAM THAT WAS MISSING.

    The engine can refuse to lie, but it is qe-central that decides what happens
    to the claim — and it decides on this field. A summary that adjudicated
    ``failed`` and sent no ``disposition`` would arrive as an ordinary crawl and
    be written to substrate, which is the green-wash hole reopening one service
    to the left of where it was closed.
    """
    from app.crawler import CrawlSummary

    summary = CrawlSummary(
        crawl_id="c1", stop_reason="inventory_failed", states=0, actions=0,
        screenshots=0, guard_blocks=0, manifest_path="/work/c1/manifest.jsonl",
        disposition="failed", downgraded=True,
        evidence={"states": 0, "inventory_failures": 1},
        coverage={"discovered_rules": []},
    )

    body = _body_for(summary, monkeypatch, volume)

    assert body["disposition"] == "failed"
    assert body["downgraded"] is True
    assert body["evidence"]["inventory_failures"] == 1
    assert body["stop_reason"] == "inventory_failed"


def test_a_crawl_with_no_summary_is_reported_failed(volume, monkeypatch):
    """FAIL-CLOSED on the absent verdict. A crawl that died before or during
    browser launch never reached the adjudicator at all; an absent disposition
    must never be read as an optimistic one."""
    body = _body_for(None, monkeypatch, volume)

    assert body["disposition"] == "failed"
    assert body["stop_reason"] == "error"


def test_discovered_rules_travel_on_the_callback(volume, monkeypatch):
    """T-GW-04's wire: qe-central persists from ``coverage.discovered_rules``, so
    a rule that never left the container is a rule that was never learned."""
    from app import rules
    from app.crawler import CrawlSummary

    proved = rules.discover(url="https://app.test/apply/1/health",
                            blocked_label="Continue", field_label="None of these",
                            proof="the app enabled it when the agent answered")
    summary = CrawlSummary(
        crawl_id="c1", stop_reason="completed", states=3, actions=5, screenshots=3,
        guard_blocks=0, manifest_path="/work/c1/manifest.jsonl",
        disposition="completed",
        coverage={"discovered_rules": [proved.as_dict()],
                  "rule_reuse": {"known": 0, "hits": 0, "misses": 1,
                                 "lookups": 1, "reuse_rate": 0.0}},
    )

    body = _body_for(summary, monkeypatch, volume)

    carried = body["coverage"]["discovered_rules"]
    assert [r["field_label"] for r in carried] == ["None of these"]
    assert body["coverage"]["rule_reuse"]["reuse_rate"] == 0.0


def test_the_durable_record_is_written_before_delivery_is_attempted(volume, monkeypatch):
    """ORDERING IS THE WHOLE RECOVERY STORY. If the record were written after a
    successful POST, the one case it exists for — the POST never landing — would
    be the one case it was never written."""
    from app.crawler import CrawlSummary

    seen: list = []

    class RecordingHTTP(ScriptedHTTP):
        async def post(self, url, *, content=None, headers=None):
            seen.append(completion_manifest.read_completion(str(volume), "c1"))
            raise httpx.ConnectError("qe-central is down")

    _install(monkeypatch, RecordingHTTP([]))
    req = main.ExploreRequest(crawl_id="c1", tenant_id="t1", exploration_id="e1",
                              target_url="https://app.test/home")
    summary = CrawlSummary(crawl_id="c1", stop_reason="completed", states=1,
                           actions=0, screenshots=1, guard_blocks=0,
                           manifest_path="/work/c1/manifest.jsonl",
                           disposition="completed")

    asyncio.run(main._fire_callback(req, summary, ""))

    assert seen, "the delivery was attempted"
    assert seen[0] is not None, "the record must already be durable on attempt 1"
    assert completion_manifest.is_orphaned(str(volume), "c1")

