"""R4 — EGRESS WORKER RACE (one tenant rewrites another's egress fence).

ATTACK
======
The dispatch order was:

    write THIS worker's squid allowlist  →  POST /explore  →  learn it was busy

So while worker W was crawling for tenant A, tenant B's dispatch reached step 1
and OVERWROTE W's ``allowed_domains.txt``.  B's own dispatch then failed with
409 — but A's browser, already running, was now fenced by B's allowlist.  The
attacker does not need to win a race for their own crawl to start; they only
need to reach the file, and the file write happened unconditionally.

FIX
===
Reserve the slot FIRST.  ``JobManager.reserve`` is the single atomic claim, it
records the owning tenant, and the allowlist write is unreachable unless the
caller holds it.

EXPECTED
========
Under simultaneous contention exactly ONE tenant holds W; the loser never
reaches the fence; ownership never changes; and the loser cannot cancel, read,
or release the winner's crawl.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.main import JOB_CREATED, JOB_FINISHED, JOB_RUNNING, JobManager

A, B = "tenant-a", "tenant-b"
CRAWL_A, CRAWL_B = "a" * 32, "b" * 32


# ── sequential: the basic invariant ────────────────────────────────────────

def test_r4_a_busy_worker_refuses_a_second_tenant(jobs):
    jobs.reserve(CRAWL_A, A)
    with pytest.raises(HTTPException) as exc:
        jobs.reserve(CRAWL_B, B)
    assert exc.value.status_code == 409
    assert jobs.owner(CRAWL_A) == A


def test_r4_ownership_is_recorded_at_reservation(jobs):
    jobs.reserve(CRAWL_A, A)
    assert jobs.owner(CRAWL_A) == A
    assert jobs.state(CRAWL_A) == JOB_CREATED


def test_r4_a_reservation_requires_a_tenant(jobs):
    """An unowned slot could be released or driven by anybody."""
    with pytest.raises(HTTPException) as exc:
        jobs.reserve(CRAWL_A, "")
    assert exc.value.status_code == 400


def test_r4_a_second_tenant_cannot_hijack_a_reserved_crawl_id(jobs):
    """Dispatching with someone else's crawl id must not inherit their slot."""
    jobs.reserve(CRAWL_A, A)
    with pytest.raises(HTTPException) as exc:
        jobs.accept(CRAWL_A, B)
    assert exc.value.status_code == 403


def test_r4_a_second_tenant_cannot_release_the_holders_slot(jobs):
    """Releasing someone else's reservation is the race, one step removed."""
    jobs.reserve(CRAWL_A, A)
    with pytest.raises(HTTPException) as exc:
        jobs.release(CRAWL_A, B)
    assert exc.value.status_code == 403
    assert jobs.owner(CRAWL_A) == A


def test_r4_the_owner_may_release_an_unused_reservation(jobs):
    """POSITIVE half: a dispatch that aborts hands the worker back."""
    jobs.reserve(CRAWL_A, A)
    jobs.release(CRAWL_A, A)
    assert jobs.busy is False
    jobs.reserve(CRAWL_B, B)          # the worker is genuinely free again
    assert jobs.owner(CRAWL_B) == B


def test_r4_a_running_crawl_cannot_be_released_even_by_its_owner(jobs):
    """Release is for an UNUSED reservation. Freeing a live slot would let a
    second tenant race into a worker that still has a browser open."""
    jobs.reserve(CRAWL_A, A)
    jobs.activate(_job(CRAWL_A))
    with pytest.raises(HTTPException) as exc:
        jobs.release(CRAWL_A, A)
    assert exc.value.status_code == 409


def test_r4_the_accept_path_is_idempotent_for_the_holder(jobs):
    """Reserve-then-dispatch must not double-claim and 409 against itself."""
    jobs.reserve(CRAWL_A, A)
    jobs.accept(CRAWL_A, A)           # no raise
    assert jobs.owner(CRAWL_A) == A


# ── concurrent: the actual race ────────────────────────────────────────────

class _Fence:
    """Stands in for a worker's squid allowlist file."""

    def __init__(self) -> None:
        self.contents: list[str] = []
        self.writes: list[str] = []

    def write(self, tenant: str, hosts: list[str]) -> None:
        self.contents = list(hosts)
        self.writes.append(tenant)


async def _dispatch(jobs: JobManager, fence: _Fence, tenant: str, crawl_id: str,
                    hosts: list[str], *, hold: asyncio.Event | None = None) -> str:
    """The FIXED ordering: reserve → (only then) fence → dispatch."""
    try:
        jobs.reserve(crawl_id, tenant)
    except HTTPException:
        return "refused"
    await asyncio.sleep(0)            # yield, so a competitor can interleave
    fence.write(tenant, hosts)
    jobs.activate(_job(crawl_id))
    if hold is not None:
        await hold.wait()
    return "dispatched"


def _job(crawl_id: str):
    import types

    crawler = types.SimpleNamespace(
        crawl_id=crawl_id,
        progress=lambda: {"crawl_id": crawl_id, "running": True},
        cancel=lambda: None,
    )
    from app.main import _Job

    return _Job(crawler)


def test_r4_concurrent_dispatch_lets_exactly_one_tenant_fence_the_worker():
    """TWO tenants, TWO crawls, ONE worker, simultaneous dispatch."""
    async def scenario():
        jobs, fence = JobManager(), _Fence()
        results = await asyncio.gather(
            _dispatch(jobs, fence, A, CRAWL_A, ["a.example"]),
            _dispatch(jobs, fence, B, CRAWL_B, ["b.example"]),
        )
        return jobs, fence, results

    jobs, fence, results = asyncio.run(scenario())
    assert sorted(results) == ["dispatched", "refused"]
    # exactly ONE fence write, by the winner, and the file holds only its hosts
    assert len(fence.writes) == 1
    winner = fence.writes[0]
    assert fence.contents == [f"{winner[-1]}.example"]
    assert jobs.owner(CRAWL_A if winner == A else CRAWL_B) == winner


def test_r4_a_busy_workers_fence_is_never_touched_by_the_loser():
    """THE headline scenario from the brief, step by step:

    1. Tenant A reserves worker W.  2. A starts crawling.  3. W is busy.
    4. B attempts dispatch.  5. B attempts to rewrite W's allowlist.
    6. W keeps using A's policy.  7. B changed nothing.
    """
    async def scenario():
        jobs, fence = JobManager(), _Fence()
        hold = asyncio.Event()
        a_task = asyncio.create_task(
            _dispatch(jobs, fence, A, CRAWL_A, ["a.example"], hold=hold))
        await asyncio.sleep(0.01)                       # A is now running
        b_result = await _dispatch(jobs, fence, B, CRAWL_B, ["evil.example"])
        hold.set()
        await a_task
        return jobs, fence, b_result

    jobs, fence, b_result = asyncio.run(scenario())
    assert b_result == "refused"
    assert fence.writes == [A]
    assert fence.contents == ["a.example"]              # A's policy, untouched
    assert "evil.example" not in fence.contents
    assert jobs.owner(CRAWL_A) == A


def test_r4_many_simultaneous_tenants_still_yield_one_winner():
    """Not two tenants — twenty, all at once."""
    async def scenario():
        jobs, fence = JobManager(), _Fence()
        results = await asyncio.gather(*[
            _dispatch(jobs, fence, f"tenant-{i}", f"{i:032d}", [f"h{i}.example"])
            for i in range(20)
        ])
        return fence, results

    fence, results = asyncio.run(scenario())
    assert results.count("dispatched") == 1
    assert results.count("refused") == 19
    assert len(fence.writes) == 1


def test_r4_simultaneous_finish_and_dispatch_do_not_double_book():
    """A worker finishing while another tenant dispatches must hand over
    cleanly — never leave two crawls believing they hold the slot."""
    async def scenario():
        jobs, fence = JobManager(), _Fence()
        jobs.reserve(CRAWL_A, A)
        job = _job(CRAWL_A)
        jobs.activate(job)

        async def finisher():
            await asyncio.sleep(0)
            jobs.finish(CRAWL_A, job)

        results = await asyncio.gather(
            finisher(),
            _dispatch(jobs, fence, B, CRAWL_B, ["b.example"]),
        )
        return jobs, fence, results[1]

    jobs, fence, b_result = asyncio.run(scenario())
    # Either B was refused (A had not finished yet) or B won the freed slot —
    # both are correct. What must NOT happen is both crawls owning the worker.
    if b_result == "dispatched":
        assert jobs.owner(CRAWL_B) == B
        assert jobs.owner(CRAWL_A) == ""      # A is terminal, no longer an owner
        assert fence.writes == [B]
    else:
        assert fence.writes == []


# ── T-SEC-09: terminal jobs are evicted ────────────────────────────────────

def test_t_sec_09_finish_evicts_the_job_from_active_lookup(jobs):
    jobs.reserve(CRAWL_A, A)
    job = _job(CRAWL_A)
    jobs.activate(job)
    assert jobs.get(CRAWL_A) is job and jobs.state(CRAWL_A) == JOB_RUNNING

    jobs.finish(CRAWL_A, job)
    assert jobs.get(CRAWL_A) is None
    assert jobs.state(CRAWL_A) == JOB_FINISHED
    assert jobs.busy is False


def test_t_sec_09_active_state_does_not_grow_with_sequential_crawls(jobs):
    """The regression the brief asks for: run enough crawls to expose the old
    growth pattern.  ``_by_id`` used to gain one entry per crawl, forever."""
    for i in range(500):
        crawl_id = f"{i:032d}"
        jobs.reserve(crawl_id, f"tenant-{i}")
        job = _job(crawl_id)
        jobs.activate(job)
        jobs.finish(crawl_id, job)
        assert jobs.active_count == 0, f"leak after {i + 1} crawls"
    assert jobs.active_count == 0
    # terminal history is retained, but BOUNDED
    assert len(jobs._finished) <= 32


def test_t_sec_09_a_finished_crawl_is_not_resolvable_as_an_active_one(jobs):
    """Authorisation must not be able to reuse stale job state."""
    jobs.reserve(CRAWL_A, A)
    job = _job(CRAWL_A)
    jobs.activate(job)
    jobs.finish(CRAWL_A, job)
    assert jobs.get(CRAWL_A) is None
    assert jobs.is_pending(CRAWL_A) is False
    assert jobs.owner(CRAWL_A) == A          # readable for the status endpoint…
    assert jobs.state(CRAWL_A) == JOB_FINISHED   # …but terminal, never active


def test_t_sec_09_tenant_state_does_not_leak_into_a_later_job(jobs):
    """Sequential crawls by different tenants must not inherit each other's
    ownership — the residue that would let stale credentials act on a new job."""
    jobs.reserve(CRAWL_A, A)
    job_a = _job(CRAWL_A)
    jobs.activate(job_a)
    jobs.finish(CRAWL_A, job_a)

    jobs.reserve(CRAWL_B, B)
    assert jobs.owner(CRAWL_B) == B
    with pytest.raises(HTTPException):
        jobs.assert_owner(CRAWL_B, A)


def test_t_sec_09_the_terminal_view_still_answers_a_status_poll(jobs):
    """Eviction must not break the legitimate 'how did it end?' read."""
    jobs.reserve(CRAWL_A, A)
    job = _job(CRAWL_A)
    jobs.activate(job)
    job.error = "boom"
    jobs.finish(CRAWL_A, job)
    view = jobs.finished_view(CRAWL_A)
    assert view["running"] is False and view["error"] == "boom"
    assert view["lifecycle"] == JOB_FINISHED


def test_t_sec_09_a_crawl_that_never_activated_still_frees_the_slot(jobs):
    jobs.reserve(CRAWL_A, A)
    jobs.finish(CRAWL_A, None)
    assert jobs.busy is False and jobs.active_count == 0
