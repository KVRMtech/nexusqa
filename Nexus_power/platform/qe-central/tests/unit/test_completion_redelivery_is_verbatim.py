"""F10 / C2, qe-central's leg — WHAT THE REAPER ACTUALLY PUTS ON THE WIRE.

WHAT WAS ALREADY PROVEN, AND WHERE IT STOPS.  ``test_greenwash_recovery``
covers the DECISION to recover: a stale row with a durable completion is
reconciled rather than reaped, one without is reaped, a failure degrades to the
old behaviour.  Every one of those tests replaces
``completion_recovery.redeliver_completion`` with a stub — which is correct for
testing the decision, and means the function that performs the recovery is
itself exercised by exactly one test (the unsigned refusal).

That leaves its three load-bearing claims unmeasured, and each is a claim the
module's own docstring makes in prose:

  * **the ack is written only on acceptance** — the ack's absence is the SOLE
    definition of an orphan, so writing it optimistically silently retires a
    crawl that was never ingested;
  * **a 4xx deliberately does NOT ack** — a body this service cannot route must
    stay visible on the volume for an operator instead of being marked done;
  * **the body is delivered VERBATIM** — "if the file says the crawl failed, a
    failure is what gets delivered".  This is the honesty guarantee of the whole
    recovery path: a recovery that edited, composed or upgraded a completion
    would manufacture successful crawls out of failed ones, which is a worse
    outcome than the lost callback it exists to fix.

The transport is scripted rather than mocked at the library boundary so the
request that would really have gone out — url, body bytes, headers — is the
thing asserted.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.controlplane import completion_recovery

CRAWL_ID = "f10rec"


class ScriptedAsyncClient:
    """Stand in for ``httpx.AsyncClient`` and record the request verbatim.

    ``redeliver_completion`` constructs its own client inside an ``async with``,
    so the class itself is what gets patched; instances capture every POST.
    """

    posted: list = []

    def __init__(self, *_a, **_k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def post(self, url, *, content=None, headers=None):
        type(self).posted.append({"url": url, "content": content,
                                  "headers": dict(headers or {})})
        outcome = type(self).outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(int(outcome), request=httpx.Request("POST", url))


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A scratch storage root with a signable fleet secret configured."""
    monkeypatch.setattr(completion_recovery.phase1_settings, "crawl_storage_root",
                        str(tmp_path), raising=False)
    monkeypatch.setattr(completion_recovery.phase1_settings, "explorer_token",
                        "f10-fleet-secret", raising=False)
    ScriptedAsyncClient.posted = []
    ScriptedAsyncClient.outcome = 200
    monkeypatch.setattr(completion_recovery.httpx, "AsyncClient",
                        ScriptedAsyncClient, raising=False)
    return tmp_path


def _orphan(volume, body: dict) -> dict:
    directory = volume / CRAWL_ID
    directory.mkdir(exist_ok=True)
    (directory / completion_recovery.COMPLETION_FILENAME).write_text(
        json.dumps(body), encoding="utf-8")
    return body


def _acked(volume) -> bool:
    return (volume / CRAWL_ID / completion_recovery.ACK_FILENAME).is_file()


# ─── the ack is the definition of "no longer an orphan" ─────────────────────

@pytest.mark.asyncio
async def test_an_accepted_redelivery_writes_the_ack(volume):
    body = _orphan(volume, {"crawl_id": CRAWL_ID, "tenant_id": "t1",
                            "exploration_id": "e1", "stop_reason": "completed"})
    assert completion_recovery.read_orphaned_completion(CRAWL_ID) == body

    assert await completion_recovery.redeliver_completion(CRAWL_ID, body) is True

    assert _acked(volume), "an accepted recovery left the crawl on the orphan list"
    assert completion_recovery.read_orphaned_completion(CRAWL_ID) is None, (
        "the recovered crawl is still reported as an orphan and will be "
        "re-delivered on every future sweep, forever")


@pytest.mark.asyncio
async def test_a_rejected_redelivery_leaves_the_orphan_visible(volume):
    """A 4xx must NOT ack.

    The body could not be routed by this service. Acking it would mark the crawl
    recovered while nothing was ingested — the evidence would be on the volume,
    the row would never complete, and the one signal that says so (the missing
    ack) would have been erased by the code meant to act on it.
    """
    body = _orphan(volume, {"crawl_id": CRAWL_ID, "tenant_id": "t1",
                            "exploration_id": "e1"})
    ScriptedAsyncClient.outcome = 422

    assert await completion_recovery.redeliver_completion(CRAWL_ID, body) is False

    assert not _acked(volume), (
        "a REFUSED completion was acknowledged — the crawl now looks recovered "
        "and its evidence will never be ingested")
    assert completion_recovery.read_orphaned_completion(CRAWL_ID) == body


@pytest.mark.asyncio
async def test_a_transport_failure_leaves_the_orphan_for_the_next_sweep(volume):
    """The reaper ticks again; an unreachable receiver must cost nothing more
    than one retry later."""
    body = _orphan(volume, {"crawl_id": CRAWL_ID, "tenant_id": "t1",
                            "exploration_id": "e1"})
    ScriptedAsyncClient.outcome = httpx.ConnectError("connection refused")

    assert await completion_recovery.redeliver_completion(CRAWL_ID, body) is False
    assert not _acked(volume)


# ─── the honesty guarantee: nothing is composed, edited or upgraded ─────────

@pytest.mark.asyncio
async def test_a_failed_crawls_completion_is_redelivered_as_a_FAILURE(volume):
    """THE CLAIM THAT MATTERS MOST.

    A recovery path that improved what it found would turn every lost callback
    into a manufactured success. The bytes that go on the wire must be the bytes
    on the volume — so this asserts the delivered payload, not the return value.
    """
    body = _orphan(volume, {
        "crawl_id": CRAWL_ID, "tenant_id": "t1", "exploration_id": "e1",
        "stop_reason": "inventory_failed", "disposition": "failed",
        "error": "the page could not be read", "downgraded": True,
    })

    assert await completion_recovery.redeliver_completion(CRAWL_ID, body) is True

    assert len(ScriptedAsyncClient.posted) == 1
    sent = json.loads(ScriptedAsyncClient.posted[0]["content"].decode("utf-8"))
    assert sent == body, (
        "the re-delivered body is not the recorded one; recovery must never "
        "compose, edit or upgrade a completion: %r" % (sent,))
    assert sent["disposition"] == "failed"
    assert sent["stop_reason"] == "inventory_failed"


@pytest.mark.asyncio
async def test_the_redelivery_goes_through_the_ordinary_ingest_seam(volume):
    """Signed, tokened, and addressed to the SAME route the explorer posts to.

    A quieter second ingest path with a different set of checks is precisely
    what the module refuses to build — every downstream effect of a completion
    (bundle mapping, substrate write, artifact promotion, generation) has to
    happen exactly once, through the route that already guards them.
    """
    body = _orphan(volume, {"crawl_id": CRAWL_ID, "tenant_id": "t1",
                            "exploration_id": "e1"})

    assert await completion_recovery.redeliver_completion(CRAWL_ID, body) is True

    sent = ScriptedAsyncClient.posted[0]
    assert sent["url"].endswith("/internal/crawls/%s/complete" % CRAWL_ID), sent["url"]
    assert sent["headers"].get("X-QEC-Signature"), "the recovery went out UNSIGNED"
    assert sent["headers"].get("X-QEC-Token") == "f10-fleet-secret"
    assert sent["headers"].get("Content-Type") == "application/json"


@pytest.mark.asyncio
async def test_a_crawl_id_that_escapes_the_root_is_never_acked(volume):
    """``mark_acknowledged`` shares the containment guard with the reader.

    The reader is already proven to refuse a traversing id; this is the WRITE
    half, which is the one that would put a file outside the storage root.
    """
    completion_recovery.mark_acknowledged("../escape")
    assert not (volume.parent / completion_recovery.ACK_FILENAME).is_file()
    assert not (volume / "escape").exists()
