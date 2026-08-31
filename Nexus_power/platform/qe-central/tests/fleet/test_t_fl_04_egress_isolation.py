"""M3.3 / T-FL-04 — PER-WORKER EGRESS ISOLATION, red-teamed.

THE STOP CONDITION THIS FILE GUARDS
===================================
"No concurrency milestone completion without a passing red-team egress test. A
fleet that processes N crawls but leaks tenant traffic is a failed
implementation."

So these tests are adversarial by construction: they use REAL files on disk (not
a mocked writer), REAL concurrency (not a sequential simulation), and they try to
make one tenant's crawl egress through another tenant's fence.

WHAT IS PROVEN
==============
  1. RESERVE-THEN-WRITE (M0.5 T-SEC-03) still holds — a worker that refuses the
     reservation never has its fence written.
  2. Each CRAWL's fence contains ONLY its own destinations (Team A / Phase A:
     the fence is per-crawl — contracts/fleet_egress_fence_v1.json — selected
     by squid via the crawl's proxy login, so what a browser is fenced by is
     its crawl's OWN file, however many crawls share a worker).
  3. Under CONCURRENT dispatch for different tenants, every crawl's fence ends
     up with its own tenant's destinations — no interleaving, no
     last-writer-wins, on separate workers AND on one.
  4. Crawl A's fence is never rewritten by crawl B's dispatch.
  5. A SHARED allowlist path (two workers, one registered path) is still
     detected and REFUSED — defence in depth for a worker whose proxy might
     still be running the legacy shared-file squid.conf.
  6. The fence is written BEFORE any dispatch (i.e. before the browser can make
     a request), and released safely on every failure path.
  7. An unsafe allowlist entry is refused rather than trimmed.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from app.clients.explorer_client import ExplorerDispatchError
from app.controlplane.scheduling import egress_fence
from app.controlplane.scheduling import worker_registry as wr
from app.routers import explorations


# ══════════════════════════════════════════════════════════════════════════
# 1. THE SHARED-FENCE LEAK — configuration alone was enough
# ══════════════════════════════════════════════════════════════════════════

def test_two_workers_sharing_one_allowlist_file_are_detected():
    """A copy-pasted allowlist_path collapses per-worker isolation."""
    workers = [
        {"worker_id": "w1", "url": "http://w1", "allowlist_path": "/eg/shared.txt"},
        {"worker_id": "w2", "url": "http://w2", "allowlist_path": "/eg/shared.txt"},
        {"worker_id": "w3", "url": "http://w3", "allowlist_path": "/eg/w3.txt"},
    ]
    conflicts = wr.fence_conflicts(workers)
    assert "/eg/shared.txt" in conflicts, (
        "two workers sharing one egress allowlist file were NOT detected — "
        "tenant B's dispatch would overwrite tenant A's fence mid-crawl")
    assert sorted(conflicts["/eg/shared.txt"]) == ["w1", "w2"]
    assert "/eg/w3.txt" not in conflicts, "a correctly isolated worker was flagged"


def test_all_conflicted_workers_are_refused_not_just_the_duplicates():
    """FAIL-CLOSED: when N workers share a file, ALL N are dropped.

    Keeping one is not safe — whichever is kept still has its fence rewritten by
    the others' dispatches. Losing capacity is an incident; leaking a tenant's
    traffic is a breach.
    """
    workers = [
        {"worker_id": "w1", "url": "http://w1", "allowlist_path": "/eg/shared.txt"},
        {"worker_id": "w2", "url": "http://w2", "allowlist_path": "/eg/shared.txt"},
        {"worker_id": "ok", "url": "http://w3", "allowlist_path": "/eg/ok.txt"},
    ]
    kept = {w["worker_id"] for w in wr.drop_fence_conflicted(workers)}
    assert kept == {"ok"}, (
        "a worker with a shared egress fence was still offered work — kept=" + str(kept))


def test_a_sound_fence_topology_keeps_every_worker():
    workers = [
        {"worker_id": "w1", "url": "http://w1", "allowlist_path": "/eg/w1.txt"},
        {"worker_id": "w2", "url": "http://w2", "allowlist_path": "/eg/w2.txt"},
    ]
    assert len(wr.drop_fence_conflicted(workers)) == 2, (
        "correctly isolated workers were dropped — this would silently halve "
        "fleet capacity for no reason")


def test_a_worker_with_no_allowlist_path_is_not_treated_as_a_conflict():
    """An empty path is a different defect (caught at dispatch), not a collision."""
    workers = [{"worker_id": "w1", "url": "http://w1", "allowlist_path": ""},
               {"worker_id": "w2", "url": "http://w2", "allowlist_path": ""}]
    assert wr.fence_conflicts(workers) == {}


# ══════════════════════════════════════════════════════════════════════════
# 2. REAL FILES — each worker's fence holds only its own crawl's destinations
# ══════════════════════════════════════════════════════════════════════════

def test_each_crawls_fence_holds_only_its_own_destinations():
    """Two workers, one crawl each — and the file each crawl is fenced by
    (its own per-crawl file, selected by proxy login) holds only its host."""
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "w1" / "aw.txt"
        b = Path(tmp) / "w2" / "aw.txt"
        explorations._write_egress_allowlist(["tenant-a.example"], str(a),
                                             crawl_id="crawl-a")
        explorations._write_egress_allowlist(["tenant-b.example"], str(b),
                                             crawl_id="crawl-b")

        a_body = egress_fence.crawl_fence_path(str(a), "crawl-a").read_text()
        b_body = egress_fence.crawl_fence_path(str(b), "crawl-b").read_text()
        assert "tenant-a.example" in a_body
        assert "tenant-b.example" not in a_body, (
            "EGRESS LEAK: crawl A's fence contains tenant B's destination")
        assert "tenant-b.example" in b_body
        assert "tenant-a.example" not in b_body, (
            "EGRESS LEAK: crawl B's fence contains tenant A's destination")


def test_two_crawls_on_one_worker_keep_independent_fences():
    """THE PROPERTY THE PER-WORKER FILE COULD NEVER HAVE. Two crawls on the
    SAME worker (same allowlist_path): the second dispatch must not rewrite
    the first crawl's live fence, and the generated squid include must carry
    BOTH fences keyed by their own crawl ids."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "w" / "aw.txt"
        explorations._write_egress_allowlist(["tenant-a.example"], str(root),
                                             crawl_id="crawl-a")
        explorations._write_egress_allowlist(["tenant-b.example"], str(root),
                                             crawl_id="crawl-b")

        a_body = egress_fence.crawl_fence_path(str(root), "crawl-a").read_text()
        assert "tenant-a.example" in a_body and "tenant-b.example" not in a_body, (
            "EGRESS LEAK: the second crawl's dispatch rewrote the first "
            "crawl's live fence on a shared worker — the exact T-FL-08 defect")
        conf = (root.parent / egress_fence.ACL_INCLUDE).read_text()
        for cid in ("crawl-a", "crawl-b"):
            assert f"proxy_auth {cid}" in conf, (
                f"{cid} has no proxy-login ACL — squid could not select its fence")


def test_a_rewrite_replaces_rather_than_appends():
    """A fence that ACCUMULATED destinations would grow to permit every host any
    tenant ever crawled — the slowest, quietest version of the same leak."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "w.txt"
        explorations._write_egress_allowlist(["first.example"], str(f),
                                             crawl_id="c1")
        explorations._write_egress_allowlist(["second.example"], str(f),
                                             crawl_id="c1")
        body = egress_fence.crawl_fence_path(str(f), "c1").read_text()
        assert "second.example" in body
        assert "first.example" not in body, (
            "the allowlist ACCUMULATED across dispatches of one crawl — a "
            "resumed crawl would keep egress permission for every host it "
            "was ever pointed at")


def test_an_empty_allowlist_is_refused_never_written():
    """An empty fence file is an OPEN fence in some squid configurations, and in
    all of them it means the operator's intent was lost. Refuse, never write."""
    from fastapi import HTTPException
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "w.txt"
        with pytest.raises(HTTPException) as exc:
            explorations._write_egress_allowlist([], str(f), crawl_id="c1")
        assert exc.value.status_code == 422
        assert not f.exists(), "an empty allowlist was written to disk"
        assert not egress_fence.crawl_fence_path(str(f), "c1").exists(), (
            "an empty per-crawl fence was written to disk")


def test_an_unwritable_fence_aborts_the_dispatch():
    """Never launch a browser that can only reach a stale/absent allowlist."""
    from fastapi import HTTPException
    # A path whose parent cannot be created (a file used as a directory).
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "blocker"
        blocker.write_text("i am a file, not a directory")
        target = blocker / "sub" / "allow.txt"
        with pytest.raises(HTTPException) as exc:
            explorations._write_egress_allowlist(["x.example"], str(target),
                                                 crawl_id="c1")
        assert exc.value.status_code == 503, (
            "a fence that could not be written did not abort the dispatch")


# ══════════════════════════════════════════════════════════════════════════
# 3. CONCURRENCY — independent fences under simultaneous dispatch
# ══════════════════════════════════════════════════════════════════════════

class _FenceRecorder:
    """A faithful transcription of the dispatch loop, writing REAL files."""

    def __init__(self, tmpdir: str):
        self.tmpdir = tmpdir
        self.events: list[tuple[str, str]] = []
        self.seen_at_dispatch: dict[str, str] = {}

    async def run(self, *, tenant: str, domains: list[str], worker: dict,
                  reserve_ok: bool = True, dispatch_raises=None) -> str:
        self.events.append(("reserve", tenant))
        if not reserve_ok:
            return "busy"
        explorations._write_egress_allowlist(domains, worker["allowlist_path"],
                                             crawl_id=f"cid-{tenant}")
        self.events.append(("fence", tenant))
        # Yield control here: with several of these in flight, any clobbering
        # has a real window to happen before the content is read.
        await asyncio.sleep(0)
        if dispatch_raises is not None:
            self.events.append(("release", tenant))
            raise dispatch_raises
        # What the BROWSER would actually be fenced by at request time — the
        # crawl's OWN file, which is what squid selects by its proxy login.
        self.seen_at_dispatch[tenant] = egress_fence.crawl_fence_path(
            worker["allowlist_path"], f"cid-{tenant}").read_text()
        self.events.append(("dispatch", tenant))
        return "dispatched"


def test_concurrent_workers_maintain_independent_fences():
    """THE concurrency red-team: 8 tenants dispatch at once, each to its OWN
    worker. Every crawl must see ONLY its own destination at dispatch time."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = _FenceRecorder(tmp)
        tenants = [f"tenant-{i}" for i in range(8)]
        workers = [{"worker_id": f"w{i}", "url": f"http://w{i}",
                    "allowlist_path": str(Path(tmp) / f"w{i}.txt")}
                   for i in range(8)]

        async def main():
            await asyncio.gather(*[
                rec.run(tenant=t, domains=[f"{t}.example"], worker=w)
                for t, w in zip(tenants, workers)])
        asyncio.run(main())

        for t in tenants:
            body = rec.seen_at_dispatch[t]
            assert f"{t}.example" in body, f"{t} was not fenced to its own host"
            for other in tenants:
                if other == t:
                    continue
                assert f"{other}.example" not in body, (
                    f"EGRESS LEAK: {t}'s crawl was fenced with {other}'s "
                    "destination — concurrent dispatch clobbered a live fence")

        # …and on disk afterwards, each crawl's fence still holds exactly its
        # own tenant's destination.
        for i, t in enumerate(tenants):
            body = egress_fence.crawl_fence_path(
                str(Path(tmp) / f"w{i}.txt"), f"cid-{t}").read_text()
            assert f"{t}.example" in body
            assert sum(1 for o in tenants if f"{o}.example" in body) == 1, (
                f"crawl cid-{t}'s fence holds more than one tenant's destinations")


def test_crawl_a_cannot_leak_through_crawl_b_fence():
    """Directed red-team: tenant B dispatches repeatedly to ITS worker while
    tenant A's crawl is live on a different worker. A's fence must not move."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = _FenceRecorder(tmp)
        w_a = {"worker_id": "wa", "url": "http://wa",
               "allowlist_path": str(Path(tmp) / "wa.txt")}
        w_b = {"worker_id": "wb", "url": "http://wb",
               "allowlist_path": str(Path(tmp) / "wb.txt")}

        async def main():
            await rec.run(tenant="victim", domains=["victim.example"], worker=w_a)
            # Attacker hammers its own worker while the victim's crawl is live.
            for i in range(20):
                await rec.run(tenant="attacker",
                              domains=[f"attacker-{i}.evil.example"], worker=w_b)
        asyncio.run(main())

        body = egress_fence.crawl_fence_path(
            str(Path(tmp) / "wa.txt"), "cid-victim").read_text()
        assert "victim.example" in body, "the victim's own fence was destroyed"
        assert "evil.example" not in body, (
            "EGRESS LEAK: the attacker's destinations reached the victim's fence")


def test_a_busy_worker_never_has_its_fence_touched():
    """RESERVE-THEN-WRITE, with a real file: a refused reservation must leave the
    incumbent tenant's fence byte-identical."""
    with tempfile.TemporaryDirectory() as tmp:
        rec = _FenceRecorder(tmp)
        worker = {"worker_id": "w", "url": "http://w",
                  "allowlist_path": str(Path(tmp) / "w.txt")}

        incumbent_fence = egress_fence.crawl_fence_path(
            worker["allowlist_path"], "cid-incumbent")

        async def main():
            await rec.run(tenant="incumbent", domains=["incumbent.example"],
                          worker=worker)
            before = incumbent_fence.read_text()
            outcome = await rec.run(tenant="intruder", domains=["intruder.example"],
                                    worker=worker, reserve_ok=False)
            after = incumbent_fence.read_text()
            return before, after, outcome

        before, after, outcome = asyncio.run(main())
        assert outcome == "busy"
        assert before == after, (
            "EGRESS LEAK: a tenant refused at the reservation still rewrote the "
            "incumbent crawl's fence — this is exactly the M0.5 T-SEC-03 defect")
        assert "intruder.example" not in after
        assert not egress_fence.crawl_fence_path(
            worker["allowlist_path"], "cid-intruder").exists(), (
            "a refused reservation still wrote the intruder's fence file")
        assert ("fence", "intruder") not in rec.events, (
            "the fence was written for a tenant that never held the worker")


# ══════════════════════════════════════════════════════════════════════════
# 4. ORDERING + RELEASE, pinned against the REAL implementation
# ══════════════════════════════════════════════════════════════════════════

def test_reservation_still_precedes_the_fence_write_in_the_real_dispatch():
    """M0.5 T-SEC-03 must survive the M3.3 rewrite of this function."""
    import inspect
    src = inspect.getsource(explorations._dispatch_explorer)
    reserve_at = src.index("reserve_worker(")
    fence_at = src.index("_write_egress_allowlist(allowed_hosts")
    assert reserve_at < fence_at, (
        "M3.3 reordered dispatch so the fence is written before the worker is "
        "reserved — a second tenant can now rewrite a live crawl's fence")


def test_the_fence_is_written_before_any_dispatch_in_the_real_implementation():
    """The fence must exist before the worker can make a single request."""
    import inspect
    src = inspect.getsource(explorations._dispatch_explorer)
    fence_at = src.index("_write_egress_allowlist(allowed_hosts")
    dispatch_at = src.index("explorer_client.dispatch_crawl(")
    assert fence_at < dispatch_at, (
        "the crawl is dispatched before its egress fence is written")


def test_every_dispatch_failure_path_releases_the_worker_and_the_slot():
    import inspect
    src = inspect.getsource(explorations._dispatch_explorer)
    assert src.count("release_worker(") >= 3, (
        "a dispatch failure path does not hand the worker back")
    assert src.count("_release_registry_slot(") >= 2, (
        "a dispatch failure path leaks a registry capacity slot — the fleet "
        "would slowly report less capacity than it has until every worker "
        "looked full and every crawl queued forever")


def test_the_registry_slot_is_taken_only_after_the_worker_agrees():
    """Taking the accounting slot before the worker's own reservation would leak
    a slot on every refusal — and a busy fleet refuses constantly."""
    import inspect
    src = inspect.getsource(explorations._dispatch_explorer)
    reserve_at = src.index("reserve_worker(")
    acquire_at = src.index("worker_registry.acquire_slot(")
    assert reserve_at < acquire_at, (
        "the registry slot is acquired before the worker has agreed to the crawl")


def test_unsafe_allowlist_entries_are_refused_not_trimmed():
    """A partially-honoured fence is not a fence."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        explorations._allowlist_domains(
            "https://good.example", {"allowed_hosts": ["good.example", "*"]})
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail.get("reason") == "unsafe_egress_allowlist", (
        "an unsafe allowlist entry was silently dropped instead of refused")
