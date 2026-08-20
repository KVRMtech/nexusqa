"""M3.3 / T-FL-07 — bounded evidence disk, without ever deleting what is needed.

WHY A GC IS REQUIRED BY THIS MILESTONE
======================================
Concurrency multiplies evidence. Every crawl writes a manifest, staged PNG
frames and downloaded artifacts under ``{crawl_storage_root}/{crawl_id}/``, and
NOTHING has ever removed them. At one crawl at a time that is a slow leak an
operator eventually notices; at N concurrent crawls it is a full disk, and a
full disk does not fail politely — the manifest write fails mid-crawl, the
completion record cannot be fsynced, and crawls start failing for a reason that
has nothing to do with the applications being crawled.

THE HARD PART IS NOT DELETING — IT IS REFUSING TO
=================================================
The milestone names four things cleanup must never remove, and each is a
different question with a different source of truth:

  1. an ACTIVE crawl — the directory is being WRITTEN right now. Source of
     truth: the exploration row's status, read from the database, never the
     filesystem (an idle mtime means a slow crawl as often as a dead one).

  2. AUDIT — evidence backing a crawl an auditor may still be asked about.
     Source of truth: the crawl's terminal disposition. Anything that
     ``refused`` or ``failed`` is retained on a SEPARATE, longer clock, because
     a refusal is precisely the outcome someone will later ask you to justify.

  3. CONFIGURED RETENTION — the operator's stated window. Nothing inside it is
     eligible, full stop.

  4. INCOMPLETE INGESTION — a completion record with no acknowledgement. This is
     the subtlest: the crawl is finished and its row may look terminal, but
     qe-central has not confirmed it ingested the evidence. Deleting here
     destroys the ONLY copy of a crawl the recovery path was about to rescue.
     Source of truth: ``completion.json`` present and ``completion.ack`` absent
     — exactly the pair ``completion_recovery`` uses to find orphans.

FAIL-CLOSED IN EVERY DIRECTION
==============================
Every uncertainty resolves to KEEP:

  * a directory whose crawl id is not in the database → KEEP (it may be a crawl
    minted by a replica whose row this scan could not read);
  * a crawl id that does not match ``CRAWL_ID_PATTERN`` → KEEP and log (it is
    not ours to delete, and a path we cannot validate is one we must not walk);
  * a status we do not recognise → KEEP;
  * any error reading a directory → KEEP.

Deleting evidence is irreversible and deleting the WRONG evidence destroys the
product's entire value proposition (proof of behaviour). A disk that is 10%
fuller than ideal is an operational nuisance; a deleted refusal is an audit
failure. The asymmetry is deliberate and total.

WHEN THE EVIDENCE IS OBJECT-BACKED
==================================
With ``QEC_EVIDENCE_STORE=s3`` the local directory is a CACHE, not the durable
copy (T-FL-03), so local cleanup is safe far sooner — the manifest is still in
object storage and ``ensure_local`` will re-fetch it on demand. The retention
floor still applies, because re-fetching costs latency an operator may not want
during an active audit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from ..db import qec_engine
from .tenant_scope import fleet_tenant_ids, scope_to_tenant

logger = logging.getLogger(__name__)

#: Env: minimum age before ANY crawl's evidence may be removed.
ENV_RETENTION = "QEC_EVIDENCE_RETENTION_SECONDS"
_DEFAULT_RETENTION_S = 7 * 24 * 3600.0          # one week

#: Env: retention for crawls that FAILED or were REFUSED — the audit clock.
ENV_AUDIT_RETENTION = "QEC_EVIDENCE_AUDIT_RETENTION_SECONDS"
_DEFAULT_AUDIT_RETENTION_S = 30 * 24 * 3600.0   # thirty days

#: Env: sweep interval. 0/unset ⇒ the GC never runs (today's behaviour).
ENV_GC_TICK = "QEC_EVIDENCE_GC_TICK_SECONDS"

#: Env: maximum directories removed per sweep — bounds the I/O burst so a GC
#: pass cannot itself become the incident.
ENV_GC_BATCH = "QEC_EVIDENCE_GC_BATCH"
_DEFAULT_GC_BATCH = 200

#: Statuses that mean the crawl is still running or still being written.
ACTIVE_STATUSES = frozenset({"pending", "queued", "claimed", "dispatched",
                             "running", "writing"})
#: Terminal statuses whose evidence an auditor may still ask about.
AUDIT_STATUSES = frozenset({"failed", "refused"})

COMPLETION_FILENAME = "completion.json"
ACK_FILENAME = "completion.ack"

#: Verdicts — returned rather than booleans so a sweep can REPORT why a
#: directory survived, which is what makes a "why is the disk full" question
#: answerable.
KEEP_ACTIVE = "keep_active_crawl"
KEEP_RETENTION = "keep_within_retention"
KEEP_AUDIT = "keep_audit_retention"
KEEP_UNINGESTED = "keep_incomplete_ingestion"
KEEP_UNKNOWN = "keep_unknown_crawl"
KEEP_INVALID = "keep_unrecognised_directory"
DELETE = "delete"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def retention_seconds() -> float:
    return _env_float(ENV_RETENTION, _DEFAULT_RETENTION_S)


def audit_retention_seconds() -> float:
    return _env_float(ENV_AUDIT_RETENTION, _DEFAULT_AUDIT_RETENTION_S)


def gc_batch() -> int:
    try:
        return max(1, int(os.environ.get(ENV_GC_BATCH, "") or _DEFAULT_GC_BATCH))
    except (TypeError, ValueError):
        return _DEFAULT_GC_BATCH


# ─── PURE CORE — the decision, with no filesystem and no database ───────────


def classify(
    *, crawl_id: str, status: str | None, finished_at: datetime | None,
    has_completion_record: bool, has_ack: bool, valid_id: bool,
    now: datetime, retention_s: float, audit_retention_s: float,
) -> str:
    """Should this crawl's evidence be deleted? PURE. Returns a KEEP_*/DELETE code.

    The order of the checks IS the safety argument, cheapest and most absolute
    first. Every branch that is not a confident DELETE returns a KEEP.
    """
    if not valid_id:
        return KEEP_INVALID
    if status is None:
        # No row: this scan could not see it (RLS scope, a replica's row, a
        # race with the insert). Never delete what you cannot account for.
        return KEEP_UNKNOWN
    normalized = str(status).strip().lower()
    if normalized in ACTIVE_STATUSES:
        return KEEP_ACTIVE
    if has_completion_record and not has_ack:
        # Finished, but qe-central has not confirmed ingestion. This directory
        # is the only copy of a crawl the recovery path is about to rescue.
        return KEEP_UNINGESTED
    if finished_at is None:
        # Terminal status with no finish time — we cannot age it, so we cannot
        # justify deleting it.
        return KEEP_UNKNOWN
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    age = now - finished_at
    if normalized in AUDIT_STATUSES:
        if age < timedelta(seconds=max(0.0, audit_retention_s)):
            return KEEP_AUDIT
        return DELETE
    if age < timedelta(seconds=max(0.0, retention_s)):
        return KEEP_RETENTION
    if normalized in ("completed", "incomplete", "stalled", "cancelled"):
        return DELETE
    # An unrecognised terminal status is not a licence to delete.
    return KEEP_UNKNOWN


def directory_size_bytes(path: Path) -> int:
    """Total bytes under ``path``. Best-effort — a race with a writer counts what
    it can rather than raising into a sweep."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


# ─── SWEEP — the impure half ────────────────────────────────────────────────


async def _crawl_states() -> dict[str, tuple[str, datetime | None]]:
    """``crawl_id → (status, finished_at)`` across the fleet, read PER TENANT.

    Per tenant because ``qe_explorations`` is RLS-protected: a GUC-less
    fleet-wide read returns zero rows (T-FL-05), and a GC that believed that
    would conclude every directory belonged to an unknown crawl. That verdict is
    KEEP, so the failure mode would have been a GC that never collected anything
    — safe, but silently useless. Reading correctly is what makes it work.
    """
    out: dict[str, tuple[str, datetime | None]] = {}
    for tenant_id in await fleet_tenant_ids():
        try:
            async with qec_engine.begin() as conn:
                await scope_to_tenant(conn, tenant_id)
                rows = (await conn.execute(text(
                    "SELECT status, finished_at, stats FROM qe_explorations"
                ))).mappings().all()
            for r in rows:
                stats = r["stats"] if isinstance(r["stats"], dict) else {}
                cid = str(stats.get("crawl_id") or "").strip()
                if cid:
                    out[cid] = (str(r["status"]), r["finished_at"])
        except Exception as exc:
            logger.warning("qec.evidence_gc.tenant_read_failed",
                           extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
    return out


async def sweep_once(*, storage_root: str | None = None,
                     now: datetime | None = None, dry_run: bool = False) -> dict:
    """One GC pass. Returns counts + a per-reason breakdown + bytes reclaimed.

    The breakdown is the point: an operator asking "why is the disk still full"
    gets ``{keep_incomplete_ingestion: 412}`` rather than a number, which names
    the actual problem (ingestion is wedged) instead of the symptom.
    """
    from ..clients.config import phase1_settings
    from ..substrate.schema import CRAWL_ID_PATTERN

    now = now or datetime.now(timezone.utc)
    root = Path(storage_root or phase1_settings.crawl_storage_root)
    report = {"scanned": 0, "deleted": 0, "bytes_reclaimed": 0,
              "reasons": {}, "dry_run": dry_run}
    if not root.is_dir():
        return report

    states = await _crawl_states()
    retention_s = retention_seconds()
    audit_s = audit_retention_seconds()
    budget = gc_batch()

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        report["scanned"] += 1
        crawl_id = entry.name
        valid = bool(CRAWL_ID_PATTERN.match(crawl_id))
        status, finished_at = states.get(crawl_id, (None, None))
        verdict = classify(
            crawl_id=crawl_id, status=status, finished_at=finished_at,
            has_completion_record=(entry / COMPLETION_FILENAME).is_file(),
            has_ack=(entry / ACK_FILENAME).is_file(),
            valid_id=valid, now=now,
            retention_s=retention_s, audit_retention_s=audit_s,
        )
        report["reasons"][verdict] = report["reasons"].get(verdict, 0) + 1
        if verdict != DELETE:
            continue
        if report["deleted"] >= budget:
            report["reasons"]["deferred_batch_limit"] = \
                report["reasons"].get("deferred_batch_limit", 0) + 1
            continue
        size = directory_size_bytes(entry)
        if dry_run:
            report["deleted"] += 1
            report["bytes_reclaimed"] += size
            continue
        try:
            shutil.rmtree(entry)
            report["deleted"] += 1
            report["bytes_reclaimed"] += size
            logger.info("qec.evidence_gc.removed",
                        extra={"crawl_id": crawl_id, "bytes": size,
                               "status": status})
        except OSError as exc:
            logger.warning("qec.evidence_gc.remove_failed",
                           extra={"crawl_id": crawl_id, "error": str(exc)[:200]})
    if report["deleted"]:
        logger.warning("qec.evidence_gc.swept", extra=dict(report))
    return report


async def evidence_gc_daemon() -> None:
    """Env-gated, leader-hosted GC loop. Mirrors the reaper's proven shape."""
    interval = _env_float(ENV_GC_TICK, 0.0)
    if interval <= 0:
        logger.info("qec.evidence_gc.disabled",
                    extra={"reason": f"{ENV_GC_TICK} not set / <= 0"})
        return
    logger.warning("qec.evidence_gc.started", extra={"interval_s": interval})
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            logger.info("qec.evidence_gc.cancelled")
            raise
        except Exception:  # pragma: no cover — a sweep never kills the loop
            logger.warning("qec.evidence_gc.tick_failed", exc_info=True)
        await asyncio.sleep(interval)


__all__ = [
    "ACTIVE_STATUSES", "AUDIT_STATUSES", "DELETE", "ENV_AUDIT_RETENTION",
    "ENV_GC_BATCH", "ENV_GC_TICK", "ENV_RETENTION", "KEEP_ACTIVE",
    "KEEP_AUDIT", "KEEP_INVALID", "KEEP_RETENTION", "KEEP_UNINGESTED",
    "KEEP_UNKNOWN", "audit_retention_seconds", "classify",
    "directory_size_bytes", "evidence_gc_daemon", "retention_seconds",
    "sweep_once",
]
