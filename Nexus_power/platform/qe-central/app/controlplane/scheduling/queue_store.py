"""M3.3 / T-FL-01 — the DURABLE CRAWL QUEUE, wired to the dispatch path.

THE DEFECT THIS CLOSES
======================
``routers/explorations._dispatch_explorer`` walked the worker pool and, when
every worker refused, did this::

    if result is None:
        await _mark(..., status="failed", error=detail, ...)
        raise HTTPException(status_code=..., detail=detail)

So a crawl was marked **failed** because the fleet was BUSY. Nothing was wrong
with the crawl, the app, or the credentials — the only fact recorded was that
someone else's crawl got there first. It is the single most damaging thing a
scheduler can do, because "failed" is what a customer reads as "your
application is broken", and it is indistinguishable in the row from a real
failure. Worse, the work was simply LOST: no retry, no record of intent.

``crawl_queue.py`` was written to fix exactly this and had **zero importers** —
a tested pure core that nothing ever called. This module is the missing half.

WHAT IS PURE AND WHAT IS HERE
=============================
Every DECISION stays in :mod:`crawl_queue` — ``plan_admission`` (admit vs
queue), ``fair_drain_order`` (round-robin across tenants), ``queue_positions``.
That core is correct and tested and is NOT rewritten. This module supplies only
what a database is needed for: counting active crawls, persisting the queued
state, and CLAIMING queued rows without two drainers racing the same one.

CLAIMING
========
A claim is one transaction: ``SELECT … FOR UPDATE SKIP LOCKED`` (the SQL the
pure core already pins) followed by a STATUS-GUARDED ``UPDATE`` to ``claimed``
inside the same transaction. ``SKIP LOCKED`` means a second drainer walks past a
row already being claimed instead of blocking on it, and the status guard means
a row that changed underneath us (a cancel, a reap) is never resurrected.

FAIRNESS
========
Reading is two-phase on purpose. Queued rows are read PER TENANT (they live
behind RLS, so there is no other way), then ordered ACROSS tenants by
``fair_drain_order`` BEFORE anything is claimed. Claiming per tenant and
dispatching as we go would serve tenants in enumeration order, and the tenant
that happened to sort first would drain its whole backlog while another tenant's
single crawl waited — the exact starvation the fair order exists to prevent.

TENANT ISOLATION
================
Every read and write below happens inside a transaction scoped to ONE tenant's
``nexus.current_tenant_id`` GUC, so RLS enforces throughout: a drain pass for
tenant A physically cannot claim, read or mutate tenant B's crawl.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from ...db import qec_engine
from ..tenant_scope import fleet_tenant_ids, scope_to_tenant
from .crawl_queue import (
    ADMIT,
    QUEUE,
    STATUS_CLAIMED,
    STATUS_QUEUED,
    fair_drain_order,
    plan_admission,
    queue_positions,
)

logger = logging.getLogger(__name__)

#: Statuses that occupy fleet capacity — a crawl in any of these is either
#: running on a worker or about to be. ``queued`` is deliberately ABSENT: a
#: queued crawl consumes nothing, and counting it would make the queue throttle
#: itself (every queued crawl would make the next one look over-cap forever).
ACTIVE_STATUSES: tuple[str, ...] = ("pending", "dispatched", "running",
                                    "writing", STATUS_CLAIMED)

#: Env: max concurrent crawls for ONE tenant. Unset/0 ⇒ unlimited, which is
#: byte-identical to today for every un-provisioned tenant — the queue must
#: never fail-close a tenant nobody configured.
ENV_TENANT_CAP = "QEC_TENANT_CONCURRENCY_CAP"
#: Env: max concurrent crawls against ONE host, per tenant. Politeness to a
#: customer's domain. Unset/0 ⇒ unlimited.
ENV_HOST_CAP = "QEC_HOST_CONCURRENCY_CAP"
#: Env: how many crawls one drain pass may start. Bounds a burst.
ENV_DRAIN_BATCH = "QEC_QUEUE_DRAIN_BATCH"
#: Env: drain tick interval. 0/unset ⇒ the drainer is DISABLED and behaves
#: exactly as before this milestone.
ENV_DRAIN_TICK = "QEC_QUEUE_DRAIN_TICK_SECONDS"


def _env_int(name: str, default: int | None) -> int | None:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else None


def tenant_cap() -> int | None:
    return _env_int(ENV_TENANT_CAP, None)


def host_cap() -> int | None:
    return _env_int(ENV_HOST_CAP, None)


def drain_batch() -> int:
    return _env_int(ENV_DRAIN_BATCH, 10) or 10


# ─── Counting what is actually in flight ────────────────────────────────────


async def active_counts(tenant_id: str) -> tuple[dict[str, int], dict[str, int]]:
    """``(active_by_tenant, active_by_host)`` for ONE tenant, under its GUC.

    Both counts are necessarily TENANT-SCOPED, because ``qe_explorations`` is
    RLS-protected and a cross-tenant count is not readable (and must not be —
    see T-FL-05). That is also the semantics we want: the host cap is politeness
    toward a customer's own domain, and tenant A's crawls should not be
    throttled because tenant B happens to crawl a host with the same name.
    Fleet-wide saturation is governed separately and correctly by the worker
    registry's capacity, not by guessing at rows this tenant may not read.

    The host is read from the stats blob the dispatch already stamps; a row that
    predates it counts toward the tenant cap only, never toward a wrong host.
    """
    by_tenant: dict[str, int] = {}
    by_host: dict[str, int] = {}
    placeholders = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant_id)
        rows = (await conn.execute(text(
            "SELECT tenant_id, stats FROM qe_explorations "
            f"WHERE status IN ({placeholders})"))).mappings().all()
    for r in rows:
        by_tenant[str(r["tenant_id"])] = by_tenant.get(str(r["tenant_id"]), 0) + 1
        stats = r["stats"] if isinstance(r["stats"], Mapping) else {}
        host = str(stats.get("target_host") or "").strip().lower()
        if host:
            by_host[host] = by_host.get(host, 0) + 1
    return by_tenant, by_host


async def admission_verdict(*, tenant_id: str, host: str) -> tuple[str, str]:
    """ADMIT or QUEUE for a new crawl, via the PURE core. Never rejects.

    Fail-OPEN on a counting error: if the active counts cannot be read we admit,
    because the alternative — queueing on a transient database hiccup — would
    stall the fleet for a reason unrelated to capacity. The worker registry and
    the worker's own single-flight lock remain the real backstops.
    """
    try:
        by_tenant, by_host = await active_counts(tenant_id)
    except Exception as exc:
        logger.warning("qec.queue.active_count_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
        return ADMIT, ""
    return plan_admission(
        host=host, tenant=tenant_id,
        active_by_host=by_host, active_by_tenant=by_tenant,
        host_cap=host_cap(), tenant_cap=tenant_cap(),
    )


# ─── Enqueue ────────────────────────────────────────────────────────────────


async def enqueue(
    *, tenant_id: str, exploration_id: str, reason: str, detail: str = "",
) -> int:
    """Move an exploration row to ``queued``. Returns its 1-based fair position.

    STATUS-GUARDED: only a row still in ``pending`` is queued. A crawl that has
    meanwhile been cancelled, reaped or (in a race) actually dispatched must
    never be dragged back into the queue.

    The queue metadata is MERGED into ``stats`` rather than replacing it — the
    wall budget, the crawl id the reaper's liveness probe reads, and the posture
    were all stamped at mint time, and losing them would leave the reaper unable
    to adjudicate the row it is now responsible for timing out.
    """
    import json
    meta = json.dumps({
        "queued_reason": reason[:80],
        "queued_detail": detail[:500],
    })
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant_id)
        await conn.execute(text(
            "UPDATE qe_explorations "
            "SET status = :queued, "
            "    stats = COALESCE(stats, '{}'::jsonb) "
            "            || CAST(:meta AS jsonb) "
            "            || jsonb_build_object('queued_at', now()::text), "
            "    updated_at = now() "
            "WHERE exploration_id = :eid AND status = 'pending'"
        ), {"queued": STATUS_QUEUED, "eid": exploration_id, "meta": meta})
    positions = await queue_snapshot_positions(tenant_id)
    return int(positions.get(exploration_id, 0))


async def queue_snapshot_positions(tenant_id: str) -> dict[str, int]:
    """1-based positions in the FAIR drain order, fleet-wide.

    Fleet-wide because the position a client is shown must be the order it will
    actually be served in — a per-tenant position would promise a place in a
    line that does not exist.
    """
    rows = await read_queued_fleet()
    return queue_positions(rows)


# ─── Read + claim ───────────────────────────────────────────────────────────


async def read_queued_for_tenant(tenant_id: str, *, limit: int = 200) -> list[dict]:
    """Queued rows for ONE tenant, under its GUC (no lock — phase 1 of the drain)."""
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant_id)
        rows = (await conn.execute(text(
            "SELECT exploration_id, tenant_id, app_id, created_at, stats "
            "FROM qe_explorations WHERE status = :q "
            "ORDER BY created_at ASC LIMIT :lim"
        ), {"q": STATUS_QUEUED, "lim": max(1, limit)})).mappings().all()
    return [dict(r) for r in rows]


async def read_queued_fleet(*, limit_per_tenant: int = 200) -> list[dict]:
    """Every queued crawl in the fleet, gathered per tenant under RLS.

    One tenant's read failure never hides the rest of the queue.
    """
    out: list[dict] = []
    for tenant_id in await fleet_tenant_ids():
        try:
            out.extend(await read_queued_for_tenant(
                tenant_id, limit=limit_per_tenant))
        except Exception as exc:
            logger.warning("qec.queue.read_failed",
                           extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
    return out


async def claim(*, tenant_id: str, exploration_id: str) -> bool:
    """Atomically take ONE queued crawl for dispatch. False if someone else won.

    ``SELECT … FOR UPDATE SKIP LOCKED`` then a status-guarded ``UPDATE`` in the
    SAME transaction. ``SKIP LOCKED`` makes a competing drainer step over this
    row rather than block on it, and the status guard means a row that left
    ``queued`` underneath us is never resurrected.
    """
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant_id)
        locked = (await conn.execute(text(
            "SELECT exploration_id FROM qe_explorations "
            "WHERE exploration_id = :eid AND status = :q "
            "FOR UPDATE SKIP LOCKED"
        ), {"eid": exploration_id, "q": STATUS_QUEUED})).first()
        if locked is None:
            return False
        res = await conn.execute(text(
            "UPDATE qe_explorations SET status = :c, updated_at = now(), "
            "  stats = COALESCE(stats, '{}'::jsonb) "
            "          || jsonb_build_object('claimed_at', now()::text) "
            "WHERE exploration_id = :eid AND status = :q"
        ), {"c": STATUS_CLAIMED, "eid": exploration_id, "q": STATUS_QUEUED})
        return bool(res.rowcount)


async def requeue(*, tenant_id: str, exploration_id: str, reason: str) -> bool:
    """Return a CLAIMED crawl to the queue after a dispatch attempt failed.

    A claim that cannot be dispatched must not evaporate: the crawl goes back to
    ``queued`` and keeps its place in the fair order (``created_at`` is never
    touched), with the attempt counted so a permanently-undispatchable crawl is
    visible rather than silently cycling forever. The reaper's queue-timeout
    remains the backstop that eventually terminalizes it.
    """
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant_id)
        res = await conn.execute(text(
            "UPDATE qe_explorations SET status = :q, updated_at = now(), "
            "  stats = COALESCE(stats, '{}'::jsonb) "
            "          || jsonb_build_object("
            "               'requeue_count', "
            "               COALESCE((stats->>'requeue_count')::int, 0) + 1, "
            "               'last_requeue_reason', CAST(:reason AS text)) "
            "WHERE exploration_id = :eid AND status = :c"
        ), {"q": STATUS_QUEUED, "c": STATUS_CLAIMED,
            "eid": exploration_id, "reason": reason[:300]})
    return bool(res.rowcount)


async def plan_drain(*, free_slots: int) -> list[dict]:
    """The crawls to start now, in FAIR order, bounded by real free capacity.

    ``free_slots`` comes from the worker registry's live capacity, so a drain
    never starts more crawls than the fleet can actually run — the queue would
    otherwise simply move the over-subscription one layer down.
    """
    if free_slots <= 0:
        return []
    queued = await read_queued_fleet()
    if not queued:
        return []
    ordered = fair_drain_order(queued, limit=min(free_slots, drain_batch()))
    logger.info(
        "qec.queue.drain_planned",
        extra={"queued_total": len(queued), "free_slots": free_slots,
               "planned": len(ordered)})
    return ordered


def queue_verdict_is_capacity(status_code: int | None) -> bool:
    """Is a dispatch failure a CAPACITY condition (queue it) or a real error?

    THE DISTINCTION THAT KEEPS THE QUEUE HONEST. Only a busy or unreachable
    fleet is queued:

      * ``409`` — every worker is busy. The crawl is fine; wait.
      * ``502`` — no worker answered. Retryable, and BOUNDED by the reaper's
        queue-timeout, so a fleet that never comes back still terminalizes the
        row with an honest reason instead of holding it forever.

    Everything else — a missing fleet token, a rejected request, a
    misconfiguration — is DETERMINISTIC: it will fail identically on every
    worker and at every future moment. Queueing it would convert an immediate,
    actionable error into an hour of silence followed by a timeout that names
    the wrong cause.
    """
    return status_code in (409, 502)


__all__ = [
    "ACTIVE_STATUSES", "ADMIT", "QUEUE", "STATUS_CLAIMED", "STATUS_QUEUED",
    "active_counts", "admission_verdict", "claim", "drain_batch", "enqueue",
    "host_cap", "plan_drain", "queue_snapshot_positions",
    "queue_verdict_is_capacity", "read_queued_fleet", "read_queued_for_tenant",
    "requeue", "tenant_cap",
]
