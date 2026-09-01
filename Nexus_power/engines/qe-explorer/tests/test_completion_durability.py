"""F10 / C2 — A CRAWL'S EVIDENCE CAN NEVER VANISH, INCLUDING ON CANCELLATION.

WHAT WAS ALREADY PROVEN NEXT DOOR.  ``test_completion_delivery`` drives the
DELIVERY path — transport errors, 5xx, 4xx, a dead receiver, a dead process —
and proves each one leaves a recoverable orphan.  Every one of those tests
starts from a durable ``completion.json`` that already exists on disk.

WHAT WAS NOT PROVEN, AND IS THE WHOLE OF F10.  The record only exists because
``_fire_callback`` ran, and ``_fire_callback`` was the ONE statement in
``_run_job`` that sat OUTSIDE the ``try/finally``::

    finally:
        jobs.finish(...)
        _record_crawl_terminal(...)

    await _fire_callback(req, summary, error, telemetry)   # <- outside

``asyncio.CancelledError`` does not inherit from ``Exception``, so the
``except Exception`` above it never sees one.  A cancelled job therefore runs
its ``finally`` — freeing the slot, emitting the terminal metric, reporting a
crawl that ended — and then propagates, skipping the callback entirely.  No
``completion.json`` is written at all, so:

  * the explorer's own sweeper cannot recover it — it scans for durable
    completions and there is none;
  * qe-central's reaper cannot reconcile it — it reads the same absent file;
  * the exploration row spins until the reaper calls it ``stalled``.

The crawl's manifest — every page state, every action, every screenshot, and
any CROSSING it recorded — is sitting on the volume, complete, and nothing
will ever point at it again.  That is the evidence vanishing, and cancellation
is not exotic: it is what a rolling deploy, a pod eviction and a container
stop all do to an in-flight crawl.

THE FIX THESE TESTS GATE.  The durable record is written from INSIDE the
terminal ``finally``, and written SYNCHRONOUSLY.  Synchronously is the
load-bearing half: a task that is being cancelled raises ``CancelledError``
again at its next await point, so a ``finally`` that AWAITS the delivery would
be skipped by the very event it exists to survive.  Writing the fact to disk
takes no await; delivering it is a notification that the sweeper can repeat.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import completion_manifest, main
from app.budget import Budget
from app.guard import RefusePack
from app.guard_context import GuardContext
from app.main import AnswerKey, ExploreRequest, JobManager

CRAWL_ID = "f10-cancel"
TENANT = "f10-tenant"


class ScriptedHTTP:
    """An ``httpx.AsyncClient`` stand-in recording every POST it is handed."""

    def __init__(self, outcomes=(200,)) -> None:
        self._outcomes = list(outcomes)
        self.posts: list = []

    async def post(self, url, *, content=None, headers=None):
        self.posts.append({"url": url, "content": content})
        outcome = self._outcomes[min(len(self.posts) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx.Response(int(outcome), request=httpx.Request("POST", url))


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A scratch work dir, free backoff, and a signable fleet secret."""
    monkeypatch.setattr(main.settings, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(main.settings, "callback_url", "http://qe-central:8093",
                        raising=False)
    monkeypatch.setattr(completion_manifest, "backoff_delay", lambda *a, **k: 0.0)
    return tmp_path


def _request(**over) -> ExploreRequest:
    payload = dict(crawl_id=CRAWL_ID, tenant_id=TENANT,
                   exploration_id="f10-exploration",
                   target_url="https://app.f10/apply", env_kind="disposable")
    payload.update(over)
    return ExploreRequest(**payload)


def _run(monkeypatch, *, boom: BaseException, req=None) -> None:
    """Drive the REAL ``_run_job`` to a terminal state through ``boom``.

    The browser is never launched: ``async_playwright`` — the first statement
    inside the guarded block — raises instead.  That is deliberate.  It puts the
    failure at the earliest point a crawl can die, which is the case with the
    LEAST state to fall back on, and it exercises the production function rather
    than a re-typed copy of its shape.
    """
    def _explode(*_a, **_k):
        raise boom

    monkeypatch.setattr("playwright.async_api.async_playwright", _explode)
    pack = RefusePack(version="f10")
    coro = main._run_job(
        req=req or _request(), budget=Budget(rate_per_s=0),
        answer_key=AnswerKey.from_payload({}), credentials=None,
        guard_ctx=GuardContext(refuse_pack=pack),
        config_fingerprint="f10-fp", pack=pack, jobs=JobManager(),
    )
    try:
        asyncio.run(coro)
    except asyncio.CancelledError:
        # The cancellation is expected to propagate — a task that swallowed it
        # would be lying to the loop about having been cancelled. What must NOT
        # be skipped is the durable record, which is what every test asserts.
        pass


# ─── THE HOLE ───────────────────────────────────────────────────────────────

def test_a_cancelled_crawl_still_records_its_completion(volume, monkeypatch):
    """THE F10 CLAIM.  Cancel a crawl and its completion is still on disk.

    Without this, a crawl killed by a rolling deploy leaves a complete manifest
    that nothing will ever read: the two recovery legs — the explorer's sweeper
    and qe-central's reaper — both key on the durable completion record, and it
    was never written.
    """
    _install_http(monkeypatch)
    _run(monkeypatch, boom=asyncio.CancelledError())

    body = completion_manifest.read_completion(str(volume), CRAWL_ID)
    assert body is not None, (
        "THE DEFECT: a cancelled crawl wrote NO durable completion record, so "
        "neither the sweeper nor the reaper can ever recover it and its "
        "manifest is orphaned permanently")
    assert body["crawl_id"] == CRAWL_ID
    assert body["tenant_id"] == TENANT
    assert body["exploration_id"] == "f10-exploration"


def test_a_cancelled_crawls_completion_is_recoverable_by_the_sweeper(volume, monkeypatch):
    """Durable is not the claim — RECOVERABLE is.  A record the sweeper cannot
    route is a file, not a recovery path, so the proof runs the real sweep."""
    _install_http(monkeypatch)
    _run(monkeypatch, boom=asyncio.CancelledError())

    assert completion_manifest.is_orphaned(str(volume), CRAWL_ID), (
        "a cancelled crawl's completion must start life un-acknowledged")
    pending = completion_manifest.pending_completions(str(volume))
    assert [p.crawl_id for p in pending] == [CRAWL_ID]
    assert completion_manifest.completion_body_is_sane(pending[0].body), (
        "the recorded body cannot be routed by qe-central — the sweeper would "
        "log it and give up")

    client = ScriptedHTTP([200])
    monkeypatch.setattr(main.app.state, "http", client, raising=False)
    cleared = asyncio.run(main._sweep_orphaned_completions())
    assert cleared == 1, "the sweeper did not recover the cancelled crawl"
    assert completion_manifest.is_delivered(str(volume), CRAWL_ID)


def test_a_cancelled_crawl_is_reported_failed_not_completed(volume, monkeypatch):
    """The recovered completion must not claim success.

    A cancelled crawl has no summary at all, so its disposition is decided by
    the fail-closed default. If a cancellation could be recovered as
    ``completed`` this fix would have replaced a lost crawl with a false one,
    which is strictly worse."""
    _install_http(monkeypatch)
    _run(monkeypatch, boom=asyncio.CancelledError())

    body = completion_manifest.read_completion(str(volume), CRAWL_ID)
    assert body["disposition"] == "failed", body
    assert body["stop_reason"] != "completed", body


# ─── THE CONTROL: the same guarantee on the paths that already worked ───────

def test_an_ordinary_exception_still_records_its_completion(volume, monkeypatch):
    """The falsification control for the tests above.

    ``except Exception`` DID catch this case before the fix, so this test
    passed both before and after. It is here so that a regression which loses
    the record on EVERY path is distinguishable from one that loses it only on
    cancellation — without it, both failures look identical.
    """
    _install_http(monkeypatch)
    _run(monkeypatch, boom=RuntimeError("browser launch refused"))

    body = completion_manifest.read_completion(str(volume), CRAWL_ID)
    assert body is not None
    assert "browser launch refused" in (body.get("error") or "")
    assert body["disposition"] == "failed"


def test_a_delivery_that_raises_cannot_lose_the_record(volume, monkeypatch):
    """A transport that fails in an unanticipated way must not unwind past the
    durable write. The record is written first, so even an exception escaping
    delivery leaves a recoverable orphan rather than a vanished crawl."""
    client = ScriptedHTTP([RuntimeError("connection pool is closed")])
    monkeypatch.setattr(main.app.state, "http", client, raising=False)
    _run(monkeypatch, boom=RuntimeError("browser launch refused"))

    assert completion_manifest.is_orphaned(str(volume), CRAWL_ID), (
        "the completion record did not survive a failing delivery")
    attempts = completion_manifest.read_attempts(str(volume), CRAWL_ID)
    assert attempts and not any(a["ok"] for a in attempts), (
        "the failed attempts were not logged, so the recovery is invisible")


# ─── callback_pending is reported honestly ──────────────────────────────────

def test_an_undelivered_completion_is_reported_as_pending(volume, monkeypatch):
    """An operator must be able to tell 'finished and landed' from 'finished
    and nobody knows'. Reported from the DURABLE FILES, not from an in-memory
    flag, so the answer survives the process that produced it."""
    client = ScriptedHTTP([500])
    monkeypatch.setattr(main.app.state, "http", client, raising=False)
    _run(monkeypatch, boom=RuntimeError("browser launch refused"))

    assert main._callback_pending(CRAWL_ID) is True

    completion_manifest.mark_delivered(str(volume), CRAWL_ID, status=200)
    assert main._callback_pending(CRAWL_ID) is False


def test_a_crawl_with_no_completion_record_is_not_reported_pending(volume):
    """``pending`` means "recorded and not yet acknowledged". A crawl id that
    reached no terminal state at all has nothing pending, and saying otherwise
    would make every unknown id look like a lost crawl."""
    assert main._callback_pending("never-ran") is False


def _install_http(monkeypatch) -> None:
    monkeypatch.setattr(main.app.state, "http", ScriptedHTTP([200]), raising=False)
