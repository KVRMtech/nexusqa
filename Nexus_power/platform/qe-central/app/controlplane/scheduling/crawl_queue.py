"""Durable crawl queue + fair, per-host-concurrent admission (Phase 2 — worker fleet).

Two guarantees, kept honest and pure:

  1. PER-HOST CONCURRENCY, NOT A MUTEX. The existing cycle ``AdmissionController``
     holds a per-host MUTEX (one admitted request per host), so reusing it for crawls
     would serialise "one client, many quote flows on ONE domain" down to a single
     crawl — the very throughput this phase exists to unlock. Crawls instead use a
     per-host CONCURRENCY CAP (default ``>= 1``, configurable), decoupled from the
     crawler's internal page-fetch rate. When a cap is unconfigured the crawl is
     ADMITTED (never fail-closed to a 422 that would reject every un-provisioned
     tenant's crawl — preserving today's behaviour exactly).

  2. FAIR DRAIN, NOT GLOBAL FIFO. When workers free up, the queue drains ROUND-ROBIN
     across tenants (oldest-waiting tenant first each round), so a tenant that floods
     20 crawls cannot starve another tenant's single crawl — proven by an adversarial
     test, not asserted.

A saturated fleet therefore returns an honest ``queued`` (with a real position), never
a dropped crawl or a fabricated "accepted". The DB-backed claim uses
``FOR UPDATE SKIP LOCKED`` on the leader-elected drainer so two ticks never double-claim.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from typing import Any

# ── Durable queue statuses (status is String(20) — NO column migration needed) ──
STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"

# Admission verdicts.
ADMIT = "admit"
QUEUE = "queue"

# The SQL the leader-elected drainer uses to claim the next batch of queued crawls
# without two ticks racing the same row. Selected oldest-first; the caller applies
# fair_drain_order() across the returned rows before dispatching.
CLAIM_QUEUED_SQL = (
    "SELECT exploration_id, tenant_id, app_id, created_at, stats "
    "FROM qe_explorations WHERE status = 'queued' "
    "ORDER BY created_at ASC LIMIT :lim FOR UPDATE SKIP LOCKED"
)


def _cap_admits(active: int, cap: int | None) -> bool:
    """A cap of None/<=0 means UNLIMITED → always admit (skip, never fail-closed)."""
    if cap is None or cap <= 0:
        return True
    return active < cap


def host_admits(active_by_host: Mapping[str, int], host: str, cap: int | None) -> bool:
    """True when starting one more crawl on ``host`` stays within the concurrency cap.

    This is a COUNT-based cap, not a binary mutex: with ``cap=3`` up to three crawls of
    the same host run at once (unlike the cycle mutex that permits exactly one).
    """
    return _cap_admits(int(active_by_host.get(host, 0)), cap)


def tenant_admits(active_by_tenant: Mapping[str, int], tenant: str, cap: int | None) -> bool:
    """True when the tenant is under its concurrent-crawl cap (None/0 ⇒ unlimited)."""
    return _cap_admits(int(active_by_tenant.get(tenant, 0)), cap)


def plan_admission(
    *,
    host: str,
    tenant: str,
    active_by_host: Mapping[str, int],
    active_by_tenant: Mapping[str, int],
    host_cap: int | None,
    tenant_cap: int | None,
) -> tuple[str, str]:
    """Decide ADMIT vs QUEUE for a new crawl. PURE.

    Returns ``(verdict, reason)``. A crawl is QUEUED (never rejected) when it would
    exceed a per-host or per-tenant concurrency cap; otherwise ADMITTED. When both caps
    are unconfigured the crawl is always admitted — byte-identical to today's ungated
    dispatch for un-provisioned tenants.
    """
    if not tenant_admits(active_by_tenant, tenant, tenant_cap):
        return QUEUE, "per_tenant_concurrency_cap"
    if not host_admits(active_by_host, host, host_cap):
        return QUEUE, "per_host_concurrency_cap"
    return ADMIT, ""


def fair_drain_order(queued: Iterable[Mapping[str, Any]], *, limit: int | None = None) -> list[dict]:
    """Order queued crawls ROUND-ROBIN across tenants (fairness, not global FIFO).

    Input rows need ``tenant_id`` and a FIFO sort key (``created_at``; ties broken by
    ``exploration_id``). Within a tenant, order is FIFO. Across tenants, each round emits
    one crawl per tenant, ordering tenants by their current head's age (longest-waiting
    tenant first) — so a tenant flooding the queue cannot monopolise freed workers.
    """
    rows = list(queued)

    def _key(r: Mapping[str, Any]):
        return (r.get("created_at"), str(r.get("exploration_id") or ""))

    rows.sort(key=_key)
    buckets: "OrderedDict[str, deque]" = OrderedDict()
    for r in rows:
        buckets.setdefault(str(r.get("tenant_id") or ""), deque()).append(dict(r))

    result: list[dict] = []
    while buckets and (limit is None or len(result) < limit):
        # Order active tenants by their head item's age (oldest first) — the fairest
        # turn order and fully deterministic.
        order = sorted(buckets.keys(), key=lambda t: _key(buckets[t][0]))
        for tenant in order:
            if limit is not None and len(result) >= limit:
                break
            result.append(buckets[tenant].popleft())
            if not buckets[tenant]:
                del buckets[tenant]
    return result


def queue_positions(queued: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Map ``exploration_id → 1-based position`` in the fair drain order, so the portal
    can show "Queued (position N)" honestly (the order it will actually be served)."""
    ordered = fair_drain_order(queued)
    return {str(r.get("exploration_id")): i + 1 for i, r in enumerate(ordered)}
