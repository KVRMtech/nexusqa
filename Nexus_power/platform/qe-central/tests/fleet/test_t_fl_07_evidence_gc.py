"""M3.3 / T-FL-07 — disk stays bounded, and the GC never deletes what is needed.

The milestone states four things cleanup must NEVER remove:

  * an ACTIVE crawl
  * evidence needed for AUDIT
  * anything inside CONFIGURED RETENTION
  * an INCOMPLETE INGESTION

Each is proven separately below, and each is proven in the FAIL-CLOSED
direction: an uncertainty must resolve to keep. The final test drives a real
sweep over a real directory tree and shows the disk actually shrinks — because a
GC that is perfectly safe and collects nothing is also a failed GC.
"""
from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.controlplane import evidence_gc as gc

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
RETENTION = 7 * 24 * 3600.0
AUDIT = 30 * 24 * 3600.0


def _classify(**kw):
    base = dict(
        crawl_id="a" * 32, status="completed",
        finished_at=NOW - timedelta(days=30),
        has_completion_record=True, has_ack=True, valid_id=True,
        now=NOW, retention_s=RETENTION, audit_retention_s=AUDIT,
    )
    base.update(kw)
    return gc.classify(**base)


# ══════════════════════════════════════════════════════════════════════════
# THE FOUR "NEVER DELETE" GUARANTEES
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status", sorted(gc.ACTIVE_STATUSES))
def test_never_deletes_an_active_crawl(status):
    """Deleting under a live writer corrupts the crawl that is running."""
    assert _classify(status=status, finished_at=None) == gc.KEEP_ACTIVE, (
        f"a crawl in status {status!r} was eligible for deletion while running")


def test_never_deletes_an_active_crawl_even_if_it_looks_ancient():
    """An old finished_at on an active row must not override liveness.

    Status is the source of truth for "is this being written"; an mtime or a
    stale timestamp is not, because a slow crawl and a dead one look identical
    on disk.
    """
    assert _classify(status="running",
                     finished_at=NOW - timedelta(days=999)) == gc.KEEP_ACTIVE


def test_never_deletes_within_configured_retention():
    assert _classify(finished_at=NOW - timedelta(days=1)) == gc.KEEP_RETENTION
    # …and the boundary: just inside retention is still kept.
    assert _classify(
        finished_at=NOW - timedelta(seconds=RETENTION - 60)) == gc.KEEP_RETENTION


def test_never_deletes_audit_evidence_on_the_normal_clock():
    """A refusal is exactly what someone will later ask you to justify."""
    aged_past_normal_retention = NOW - timedelta(days=10)   # > 7d, < 30d
    for status in sorted(gc.AUDIT_STATUSES):
        assert _classify(status=status,
                         finished_at=aged_past_normal_retention) == gc.KEEP_AUDIT, (
            f"{status!r} evidence was deleted on the ordinary retention clock — "
            "the audit window is longer for exactly this reason")


def test_audit_evidence_is_eventually_collectable():
    """Retained longer, not forever — otherwise the disk is unbounded by design."""
    assert _classify(status="failed",
                     finished_at=NOW - timedelta(days=31)) == gc.DELETE


def test_never_deletes_an_incomplete_ingestion():
    """A completion record with no ACK is the ONLY copy of a crawl the recovery
    path is about to rescue."""
    assert _classify(has_completion_record=True, has_ack=False,
                     finished_at=NOW - timedelta(days=365)) == gc.KEEP_UNINGESTED, (
        "evidence was deleted before qe-central confirmed it ingested it — this "
        "destroys a completed crawl that the reaper was about to recover")


def test_an_acknowledged_ingestion_past_retention_is_collectable():
    assert _classify(has_completion_record=True, has_ack=True,
                     finished_at=NOW - timedelta(days=30)) == gc.DELETE


# ══════════════════════════════════════════════════════════════════════════
# FAIL-CLOSED ON EVERY UNCERTAINTY
# ══════════════════════════════════════════════════════════════════════════

def test_unknown_crawl_is_kept():
    """No row means this scan could not account for it — never delete that."""
    assert _classify(status=None) == gc.KEEP_UNKNOWN


def test_unrecognised_directory_name_is_kept():
    """A path we cannot validate is one we must not walk, let alone remove."""
    assert _classify(valid_id=False) == gc.KEEP_INVALID


def test_terminal_status_without_a_finish_time_is_kept():
    """It cannot be aged, so its deletion cannot be justified."""
    assert _classify(status="completed", finished_at=None) == gc.KEEP_UNKNOWN


def test_an_unrecognised_status_is_kept():
    assert _classify(status="quantum_superposition") == gc.KEEP_UNKNOWN, (
        "an unrecognised status was treated as a licence to delete")


def test_a_naive_finished_at_is_handled_not_crashed():
    """A tz-naive timestamp must not raise inside a sweep."""
    assert _classify(finished_at=datetime(2026, 1, 1)) == gc.DELETE


# ══════════════════════════════════════════════════════════════════════════
# THE SWEEP — the disk actually shrinks, and stays bounded
# ══════════════════════════════════════════════════════════════════════════

def _make_crawl(root: Path, crawl_id: str, *, mb: int = 1,
                completion=True, ack=True) -> Path:
    d = root / crawl_id
    (d / "frames").mkdir(parents=True, exist_ok=True)
    (d / "frames" / "big.png").write_bytes(b"\0" * (mb * 1024 * 1024))
    (d / "manifest.jsonl").write_text('{"type":"state"}\n')
    if completion:
        (d / gc.COMPLETION_FILENAME).write_text("{}")
    if ack:
        (d / gc.ACK_FILENAME).write_text("200")
    return d


@pytest.mark.asyncio
async def test_sweep_reclaims_collectable_evidence_and_spares_the_rest(monkeypatch):
    """A real sweep over a real tree: the disk shrinks, protected crawls survive."""
    collectable = uuid.uuid4().hex          # completed, old, acked  → DELETE
    active = uuid.uuid4().hex               # running                → KEEP
    uningested = uuid.uuid4().hex           # completion, no ack     → KEEP
    recent = uuid.uuid4().hex               # inside retention       → KEEP
    unknown = uuid.uuid4().hex              # no DB row              → KEEP
    not_a_crawl = "definitely-not-a-crawl-id"  # invalid name        → KEEP

    states = {
        collectable: ("completed", NOW - timedelta(days=30)),
        active: ("running", None),
        uningested: ("completed", NOW - timedelta(days=30)),
        recent: ("completed", NOW - timedelta(hours=2)),
    }

    async def _fake_states():
        return states
    monkeypatch.setattr(gc, "_crawl_states", _fake_states)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_crawl(root, collectable, mb=2)
        _make_crawl(root, active, mb=1, completion=False, ack=False)
        _make_crawl(root, uningested, mb=1, ack=False)
        _make_crawl(root, recent, mb=1)
        _make_crawl(root, unknown, mb=1)
        _make_crawl(root, not_a_crawl, mb=1)

        before = gc.directory_size_bytes(root)
        report = await gc.sweep_once(storage_root=str(root), now=NOW)
        after = gc.directory_size_bytes(root)

        assert report["deleted"] == 1, (
            "expected exactly one collectable crawl, got "
            + str(report["deleted"]) + " — reasons: " + str(report["reasons"]))
        assert after < before, "the sweep reclaimed no disk at all"
        assert report["bytes_reclaimed"] >= 2 * 1024 * 1024

        assert not (root / collectable).exists(), "the collectable crawl survived"
        for protected, why in ((active, "an ACTIVE crawl"),
                               (uningested, "an INCOMPLETE INGESTION"),
                               (recent, "evidence inside CONFIGURED RETENTION"),
                               (unknown, "an UNACCOUNTED-FOR crawl"),
                               (not_a_crawl, "an unrecognised directory")):
            assert (root / protected).exists(), (
                "the GC deleted " + why + " — this is the failure mode that "
                "destroys the product's evidence guarantee")


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(monkeypatch):
    """An operator must be able to see what WOULD go before it goes."""
    cid = uuid.uuid4().hex

    async def _fake_states():
        return {cid: ("completed", NOW - timedelta(days=30))}
    monkeypatch.setattr(gc, "_crawl_states", _fake_states)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_crawl(root, cid, mb=1)
        report = await gc.sweep_once(storage_root=str(root), now=NOW, dry_run=True)
        assert report["deleted"] == 1 and report["dry_run"] is True
        assert (root / cid).exists(), "a dry run deleted real evidence"


@pytest.mark.asyncio
async def test_sweep_is_bounded_per_pass(monkeypatch):
    """A GC pass must not become the incident it exists to prevent."""
    ids = [uuid.uuid4().hex for _ in range(8)]

    async def _fake_states():
        return {c: ("completed", NOW - timedelta(days=30)) for c in ids}
    monkeypatch.setattr(gc, "_crawl_states", _fake_states)
    monkeypatch.setenv(gc.ENV_GC_BATCH, "3")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for c in ids:
            _make_crawl(root, c, mb=1)
        report = await gc.sweep_once(storage_root=str(root), now=NOW)
        assert report["deleted"] == 3, "the batch limit was not honoured"
        assert report["reasons"].get("deferred_batch_limit") == 5
        assert sum(1 for c in ids if (root / c).exists()) == 5


@pytest.mark.asyncio
async def test_sustained_load_keeps_the_disk_bounded(monkeypatch):
    """Rounds of new crawls with GC running: the tree does not grow without
    bound, and the steady state is the protected set — not everything ever
    crawled."""
    live: dict[str, tuple[str, datetime]] = {}

    async def _fake_states():
        return dict(live)
    monkeypatch.setattr(gc, "_crawl_states", _fake_states)
    monkeypatch.setenv(gc.ENV_GC_BATCH, "1000")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sizes = []
        for rnd in range(6):
            # Each round: 5 new crawls finish and age past retention.
            for _ in range(5):
                cid = uuid.uuid4().hex
                _make_crawl(root, cid, mb=1)
                live[cid] = ("completed", NOW - timedelta(days=30))
            await gc.sweep_once(storage_root=str(root), now=NOW)
            sizes.append(gc.directory_size_bytes(root))

        assert max(sizes) <= 1 * 1024 * 1024, (
            "the evidence directory grew under sustained crawl load — sizes per "
            "round: " + str([s // (1024 * 1024) for s in sizes]) + " MiB")
        assert len([p for p in root.iterdir() if p.is_dir()]) == 0, (
            "collectable evidence accumulated across rounds")
