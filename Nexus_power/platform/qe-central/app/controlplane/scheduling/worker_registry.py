"""M3.3 / T-FL-02 — the explorer WORKER REGISTRY.

WHAT THIS REPLACES
==================
``QEC_EXPLORER_POOL`` is a STATIC JSON array of ``{url, allowlist_path}`` read
from the environment. It cannot express liveness, capacity, utilisation or
eligibility, so dispatch walked it in a FIXED order and learned a worker was
busy only by being refused (409). Consequences, all of which this module ends:

  * worker[0] absorbed every dispatch attempt — there was no "least loaded";
  * a DEAD worker kept being offered work, because nothing tracked liveness, and
    each attempt paid a full connect timeout before failing over;
  * fleet capacity was unknowable, so the queue (T-FL-01) had no honest signal
    for "the fleet is full" versus "the fleet is gone".

DESIGN: A PURE CORE, A THIN STORE
=================================
Every scheduling DECISION is a pure function over plain dicts
(:func:`eligible_workers`, :func:`choose_worker`, :func:`fleet_capacity`,
:func:`is_stale`) and is tested without a database. The impure half is a small
set of ``async`` functions that read and write ``explorer_workers``.

LIVENESS IS EVIDENCE, NOT ASSUMPTION
====================================
A worker is eligible only while its heartbeat is FRESH. Staleness is measured
against the row's ``last_heartbeat_at``, never against process memory, so the
verdict survives a qe-central restart and is identical on every replica.

FAIL-SAFE DIRECTION. When the registry is EMPTY or unreadable the caller falls
back to the static ``QEC_EXPLORER_POOL`` (see :func:`schedulable_workers`).
This is deliberate and is the ONLY safe direction: an empty registry on a fresh
deploy — before any explorer has had time to register — must not read as "the
fleet has no capacity", which would queue every crawl indefinitely. Falling back
reproduces today's behaviour exactly, so enabling the registry can never be
worse than not having it.

CAPACITY IS A CAP, NOT A HINT. ``in_flight`` is maintained by the same reserve /
release path that already governs the worker's own single-flight lock, and the
DATABASE enforces ``0 <= in_flight <= capacity``. The registry can therefore
never hand out more concurrency than the fleet actually has.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from ...db import qec_engine

logger = logging.getLogger(__name__)

# ── Worker lifecycle states ─────────────────────────────────────────────────
#: Accepting new work.
STATUS_ACTIVE = "active"
#: Finish what you hold, accept nothing new (rolling deploy / scale-down).
STATUS_DRAINING = "draining"
#: Operator-parked.
STATUS_DISABLED = "disabled"
#: RETIRED BY STALENESS (Team A / Phase A): the sweeper marks an ``active``
#: worker ``stale`` once its heartbeat ages past the TTL, so the registry
#: TABLE says what the pure staleness verdict already enforced — an operator
#: SELECTing the table sees the retirement, not a row that looks alive. A
#: worker that resumes heartbeating re-activates itself (every heartbeat
#: carries the worker's own status), and the reaper deletes the row after the
#: retention window.
STATUS_STALE = "stale"

#: Env: how long after its last heartbeat a worker is considered STALE.
ENV_HEARTBEAT_TTL = "QEC_WORKER_HEARTBEAT_TTL_SECONDS"
#: Default TTL. Generous relative to the heartbeat interval (below) so a single
#: missed beat — a GC pause, a slow node — never evicts a healthy worker. Three
#: missed beats do.
_DEFAULT_HEARTBEAT_TTL_S = 90.0

#: Env: how often a worker should heartbeat. Advertised to the worker at
#: registration so the interval lives in ONE place.
ENV_HEARTBEAT_INTERVAL = "QEC_WORKER_HEARTBEAT_INTERVAL_SECONDS"
_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

#: Env: how long a stale worker row is retained before it is reaped. Kept well
#: beyond the TTL so an operator debugging a crash still sees the corpse.
ENV_WORKER_RETENTION = "QEC_WORKER_RETENTION_SECONDS"
_DEFAULT_WORKER_RETENTION_S = 3_600.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def heartbeat_ttl_seconds() -> float:
    return _env_float(ENV_HEARTBEAT_TTL, _DEFAULT_HEARTBEAT_TTL_S)


def heartbeat_interval_seconds() -> float:
    return _env_float(ENV_HEARTBEAT_INTERVAL, _DEFAULT_HEARTBEAT_INTERVAL_S)


def worker_retention_seconds() -> float:
    return _env_float(ENV_WORKER_RETENTION, _DEFAULT_WORKER_RETENTION_S)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ─── PURE CORE — every scheduling decision, no database ─────────────────────


def is_stale(worker: Mapping[str, Any], *, now: datetime, ttl_s: float) -> bool:
    """True when ``worker``'s heartbeat is older than ``ttl_s``.

    A row with NO heartbeat is stale: absence of evidence of life is not
    evidence of life. That is the fail-safe direction — a worker wrongly judged
    stale loses work it would have done, while a dead worker wrongly judged
    alive SILENTLY SWALLOWS crawls that never run.
    """
    hb = worker.get("last_heartbeat_at")
    if not isinstance(hb, datetime):
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb) > timedelta(seconds=max(0.0, ttl_s))


#: THE EGRESS FENCE IS PER-CRAWL (Team A / Phase A) — capacity means capacity.
#:
#: HISTORY, kept because the guards live on. This was ``True`` while
#: ``_write_egress_allowlist`` was keyed on the WORKER and took no crawl id:
#: at capacity > 1, concurrent dispatches to one worker overwrote each other's
#: live fence — a cross-tenant egress leak, recorded by a strict xfail in
#: tests/fleet/test_t_fl_08_concurrency_redteam.py and made unreachable by
#: this clamp (the ARB's accepted posture, ARB_EGRESS_FENCE_DECISION_RECORD).
#:
#: The REAL fix required the CONSUMER to select the fence per request, which
#: the ARB record correctly said per-crawl files alone could not do. That is
#: what landed: the writer takes ``crawl_id`` and writes one dstdomain file
#: per crawl; squid authenticates every request (basic_fake_auth) and the
#: generated ACL pair keys each fence on the crawl's PROXY LOGIN; each browser
#: context authenticates as its own crawl id. Frozen shape:
#: ``contracts/fleet_egress_fence_v1.json``. The xfail XPASSed (2026-08-31,
#: ``[XPASS(strict)]`` in the run log), its marker came off, and this flag
#: flipped in the same change — the exact sequence the pairing test dictated.
#:
#: THE PAIRING SURVIVES, INVERTED: tests/test_a_shared_fence_admits_one_crawl_
#: per_worker.py now fails the build if the writer ever loses its crawl id
#: while this is False (revert ⇒ the clamp must come back), and the per-crawl
#: tripwire in tests/contract/ fails it if squid.conf stops selecting fences
#: per crawl while capacity is unclamped.
FENCE_IS_PER_WORKER = False


def effective_capacity(worker: Mapping[str, Any]) -> int:
    """The capacity ADMISSION may use — the registered value, clamped to 1
    while the egress fence is per-worker (see FENCE_IS_PER_WORKER)."""
    try:
        registered = int(worker.get("capacity") or 0)
    except (TypeError, ValueError):
        return 0
    if registered <= 0:
        return 0
    return 1 if FENCE_IS_PER_WORKER else registered


def has_capacity(worker: Mapping[str, Any]) -> bool:
    """True when the worker can accept at least one more crawl.

    Uses :func:`effective_capacity`, not the registered value, so the clamp
    seam stays in one place: with the per-crawl fence (FENCE_IS_PER_WORKER
    False, today) the two are equal and capacity means capacity; if the fence
    writer ever regresses to a shared file and the flag returns to True,
    admitting a second concurrent crawl onto one worker becomes — again — the
    exact sequence that re-fences a running crawl with another tenant's
    destinations, and this function is where that stops.
    """
    try:
        return int(worker.get("in_flight") or 0) < effective_capacity(worker)
    except (TypeError, ValueError):
        return False


def is_eligible_for_tenant(worker: Mapping[str, Any], tenant_id: str) -> bool:
    """True when ``worker`` may run work for ``tenant_id``.

    An empty ``tenant_affinity`` is a SHARED worker — eligible for everyone, and
    the default. A non-empty affinity is an operator's dedication of hardware to
    one tenant, and it is EXCLUSIVE in both directions: the dedicated worker
    takes only that tenant's work (a second tenant must never land on a pod
    reserved for a customer who paid for isolation).
    """
    affinity = str(worker.get("tenant_affinity") or "").strip()
    return not affinity or affinity == str(tenant_id)


def eligible_workers(
    workers: Iterable[Mapping[str, Any]], *, tenant_id: str,
    now: datetime, ttl_s: float,
) -> list[dict]:
    """Alive, active, tenant-eligible workers WITH free capacity. PURE.

    Order is not significant here — :func:`choose_worker` ranks. Every exclusion
    is one of the four documented reasons, so a "no worker available" answer can
    always be explained (see :func:`explain_unavailable`).
    """
    out: list[dict] = []
    for w in workers:
        if str(w.get("status") or "") != STATUS_ACTIVE:
            continue
        if is_stale(w, now=now, ttl_s=ttl_s):
            continue
        if not is_eligible_for_tenant(w, tenant_id):
            continue
        if not has_capacity(w):
            continue
        out.append(dict(w))
    return out


def choose_worker(
    workers: Iterable[Mapping[str, Any]], *, tenant_id: str,
    now: datetime, ttl_s: float,
) -> dict | None:
    """The LEAST-LOADED eligible worker, or ``None``. PURE and deterministic.

    Ranking, in order:

      1. lowest UTILISATION (``in_flight / capacity``) — a fraction, not a raw
         count, so a big worker with 2/8 used is preferred over a small one with
         1/1 used. Ranking on the raw count would pack small workers first and
         leave large ones idle;
      2. most ABSOLUTE headroom, to break ties toward the worker with more room
         left for a burst;
      3. ``worker_id``, so the choice is deterministic and two replicas deciding
         from the same snapshot make the SAME choice — which keeps the outcome
         reproducible in a test and in an incident.

    A DEDICATED worker is preferred over a shared one for a tenant that has one:
    the operator provisioned it for exactly this, and spending shared capacity
    first would leave that tenant's own hardware idle.
    """
    ranked = eligible_workers(workers, tenant_id=tenant_id, now=now, ttl_s=ttl_s)
    if not ranked:
        return None

    def _key(w: Mapping[str, Any]):
        cap = max(1, int(w.get("capacity") or 1))
        used = int(w.get("in_flight") or 0)
        dedicated = 0 if str(w.get("tenant_affinity") or "").strip() else 1
        return (dedicated, used / cap, -(cap - used), str(w.get("worker_id") or ""))

    return min(ranked, key=_key)


def fleet_capacity(
    workers: Iterable[Mapping[str, Any]], *, tenant_id: str = "",
    now: datetime, ttl_s: float,
) -> dict:
    """Honest capacity totals over the ALIVE, ACTIVE fleet. PURE.

    A stale or draining worker contributes NOTHING — counting it would tell the
    queue there is room that does not exist, and crawls would be admitted into a
    fleet that cannot run them. ``tenant_id`` restricts the count to workers that
    tenant may actually use.
    """
    total = used = free = alive = 0
    for w in workers:
        if str(w.get("status") or "") != STATUS_ACTIVE:
            continue
        if is_stale(w, now=now, ttl_s=ttl_s):
            continue
        if tenant_id and not is_eligible_for_tenant(w, tenant_id):
            continue
        # The CLAMPED capacity, not the registered one: the queue must be told
        # the room that actually exists, and while the fence is per-worker that
        # is one slot per worker (see FENCE_IS_PER_WORKER). Reporting the
        # registered value here while admission clamps below it would tell the
        # queue there is room the scheduler will never grant.
        cap = effective_capacity(w)
        inflight = max(0, int(w.get("in_flight") or 0))
        alive += 1
        total += cap
        used += min(inflight, cap)
        free += max(0, cap - inflight)
    return {"workers_alive": alive, "capacity": total, "in_flight": used,
            "free": free}


def explain_unavailable(
    workers: Iterable[Mapping[str, Any]], *, tenant_id: str,
    now: datetime, ttl_s: float,
) -> str:
    """WHY no worker was available — a real reason, never "unavailable".

    An operator staring at a queued crawl must be able to tell "the fleet is
    busy" (wait, or scale up) from "every worker is dead" (page someone) from
    "this tenant's dedicated worker is parked" (an operator action). Reporting
    one opaque string for three different incidents is what makes a fleet
    un-debuggable at 3am.
    """
    rows = [dict(w) for w in workers]
    if not rows:
        return ("no explorer worker has ever registered — the fleet registry is "
                "empty (are the explorer pods running and reachable?)")
    considered = [w for w in rows if is_eligible_for_tenant(w, tenant_id)]
    if not considered:
        return (f"no worker is eligible for tenant {tenant_id!r} — every "
                "registered worker is dedicated to a different tenant")
    alive = [w for w in considered if not is_stale(w, now=now, ttl_s=ttl_s)]
    if not alive:
        return (f"all {len(considered)} eligible worker(s) are STALE — none has "
                f"heartbeated within {ttl_s:.0f}s (crashed pods, or the "
                "heartbeat path is broken)")
    active = [w for w in alive if str(w.get("status") or "") == STATUS_ACTIVE]
    if not active:
        states = sorted({str(w.get("status") or "?") for w in alive})
        return (f"all {len(alive)} live eligible worker(s) are non-active "
                f"(status: {', '.join(states)}) — draining or operator-disabled")
    cap = fleet_capacity(rows, tenant_id=tenant_id, now=now, ttl_s=ttl_s)
    return (f"the fleet is at capacity: {cap['in_flight']}/{cap['capacity']} "
            f"slots in use across {cap['workers_alive']} live worker(s)")



def fence_conflicts(workers: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Workers that SHARE an egress allowlist file, keyed by path. PURE.

    M3.3 / T-FL-04 — A GUARD THAT DID NOT EXIST.

    Per-worker egress isolation depends on one invariant: each worker's squid
    reads its OWN allowlist file. ``QEC_EXPLORER_POOL``'s docstring states this
    ("each worker MUST have its OWN squid egress allowlist file") but NOTHING
    ever enforced it. Two workers configured with the same ``allowlist_path`` —
    a copy-paste in a Helm values file is all it takes — silently collapses the
    fence: qe-central writes tenant A's allowed hosts, then tenant B's dispatch
    OVERWRITES the same file while A's browser is still running against it, and
    A's crawl is now fenced to B's destinations.

    That is a cross-tenant egress leak produced by configuration alone, with no
    code defect and no error anywhere. It is exactly the failure this milestone's
    stop condition names: "a fleet that processes N crawls but leaks tenant
    traffic is a failed implementation."

    Returns ``{path: [worker_id, ...]}`` for every path claimed by MORE THAN ONE
    worker. Empty ⇒ the fence topology is sound.
    """
    by_path: dict[str, list[str]] = {}
    for w in workers:
        path = str(w.get("allowlist_path") or "").strip()
        if not path:
            continue
        by_path.setdefault(path, []).append(str(w.get("worker_id") or w.get("url") or "?"))
    return {p: ids for p, ids in by_path.items() if len(ids) > 1}


def drop_fence_conflicted(workers: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Every worker whose allowlist file is NOT shared with another worker.

    FAIL-CLOSED, and deliberately asymmetric: when N workers share one file, ALL
    N are dropped, not N-1. There is no safe way to pick a winner — whichever is
    kept still has its fence rewritten by the others' dispatches. Dropping them
    all degrades the fleet's capacity (crawls queue, which is now a first-class
    honest state) instead of running crawls behind a fence that does not fence.

    Losing capacity is an incident. Leaking a tenant's traffic is a breach.
    """
    conflicts = fence_conflicts(workers)
    if not conflicts:
        return [dict(w) for w in workers]
    bad_paths = set(conflicts)
    kept = [dict(w) for w in workers
            if str(w.get("allowlist_path") or "").strip() not in bad_paths]
    logger.error(
        "qec.worker_registry.fence_conflict",
        extra={"conflicts": {p: ids for p, ids in conflicts.items()},
               "dropped": len(list(workers)) - len(kept),
               "detail": ("workers sharing one egress allowlist file cannot be "
                          "isolated from each other and are REFUSED work")})
    return kept


# ─── STORE — the thin impure half ───────────────────────────────────────────

_SELECT_COLS = (
    "worker_id, url, allowlist_path, capacity, in_flight, status, "
    "tenant_affinity, last_heartbeat_at, registered_at, updated_at, meta"
)


async def register_worker(
    *, worker_id: str, url: str, allowlist_path: str = "",
    capacity: int = 1, tenant_affinity: str = "", meta: dict | None = None,
) -> dict:
    """Register (or re-register) a worker. Idempotent by ``worker_id``.

    A restarting pod re-registers as ITSELF, which is why ``worker_id`` must be
    stable (the pod name in K8s). Re-registration RESETS ``in_flight`` to 0: a
    worker that just restarted is, by definition, running nothing — carrying the
    old count forward would permanently strand capacity that no crawl occupies.
    """
    import json
    now = utc_now()
    async with qec_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO explorer_workers "
            "(worker_id, url, allowlist_path, capacity, in_flight, status, "
            " tenant_affinity, last_heartbeat_at, registered_at, updated_at, meta) "
            "VALUES (:wid, :url, :ap, :cap, 0, :st, :aff, :now, :now, :now, "
            "        CAST(:meta AS jsonb)) "
            "ON CONFLICT (worker_id) DO UPDATE SET "
            "  url = EXCLUDED.url, allowlist_path = EXCLUDED.allowlist_path, "
            "  capacity = EXCLUDED.capacity, in_flight = 0, "
            "  status = EXCLUDED.status, "
            "  tenant_affinity = EXCLUDED.tenant_affinity, "
            "  last_heartbeat_at = EXCLUDED.last_heartbeat_at, "
            "  updated_at = EXCLUDED.updated_at, meta = EXCLUDED.meta"
        ), {"wid": worker_id[:64], "url": url[:500], "ap": allowlist_path[:500],
            "cap": max(1, int(capacity)), "st": STATUS_ACTIVE,
            "aff": str(tenant_affinity or "")[:64], "now": now,
            "meta": json.dumps(meta or {})})
    logger.warning(
        "qec.worker_registry.registered",
        extra={"worker_id": worker_id, "capacity": capacity,
               "tenant_affinity": tenant_affinity or "(shared)"})
    return {"worker_id": worker_id, "capacity": capacity,
            "heartbeat_interval_s": heartbeat_interval_seconds(),
            "heartbeat_ttl_s": heartbeat_ttl_seconds()}


async def heartbeat(
    *, worker_id: str, in_flight: int | None = None,
    status: str | None = None, capacity: int | None = None,
) -> bool:
    """Record a heartbeat. Returns False when the worker is not registered.

    A worker may report its own ``in_flight`` — the worker is the authority on
    what it is actually running, and reconciling to it heals any drift left by a
    lost release. The value is CLAMPED to ``[0, capacity]`` so a buggy or
    hostile report cannot violate the capacity invariant (the database check
    constraint is the second line of defence).

    Returning False rather than silently inserting is deliberate: an unknown
    worker heartbeating means it registered against a database that has since
    been reset, and it must RE-REGISTER (declaring capacity and affinity) rather
    than be resurrected with defaults nobody chose.
    """
    now = utc_now()
    sets = ["last_heartbeat_at = :now", "updated_at = :now"]
    params: dict[str, Any] = {"now": now, "wid": worker_id}
    if capacity is not None:
        sets.append("capacity = :cap")
        params["cap"] = max(1, int(capacity))
    if in_flight is not None:
        # Clamp against the row's OWN capacity, in SQL, so the clamp is atomic
        # with the write and cannot race a concurrent capacity change.
        sets.append("in_flight = LEAST(GREATEST(:inf, 0), capacity)")
        params["inf"] = int(in_flight)
    if status is not None and status in (
            STATUS_ACTIVE, STATUS_DRAINING, STATUS_DISABLED):
        sets.append("status = :st")
        params["st"] = status
    async with qec_engine.begin() as conn:
        res = await conn.execute(text(
            "UPDATE explorer_workers SET " + ", ".join(sets)
            + " WHERE worker_id = :wid"), params)
    return bool(res.rowcount)


async def list_workers() -> list[dict]:
    """Every registered worker. Fleet-wide, no tenant GUC — see the migration's
    docstring for why this table is deliberately NOT RLS-scoped."""
    async with qec_engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT " + _SELECT_COLS + " FROM explorer_workers"))).mappings().all()
    return [dict(r) for r in rows]


async def acquire_slot(*, worker_id: str) -> bool:
    """Atomically take one slot on ``worker_id``. False when it is full/ineligible.

    The whole decision is ONE conditional UPDATE. Two qe-central replicas racing
    the same last slot cannot both win: the second sees ``rowcount == 0``,
    because the ``in_flight < capacity`` predicate is evaluated under the row
    lock the UPDATE itself takes. A read-then-write would have a window between
    the check and the write, and at N concurrent dispatches that window is
    exactly where over-subscription happens.
    """
    async with qec_engine.begin() as conn:
        res = await conn.execute(text(
            "UPDATE explorer_workers SET in_flight = in_flight + 1, "
            "  updated_at = now() "
            "WHERE worker_id = :wid AND status = :st AND in_flight < capacity"
        ), {"wid": worker_id, "st": STATUS_ACTIVE})
    return bool(res.rowcount)


async def release_slot(*, worker_id: str) -> bool:
    """Hand one slot back. Never drops below zero (``GREATEST(..., 0)``).

    Idempotency matters here: a dispatch failure path and a completion callback
    can both release the same crawl, and double-releasing must not manufacture
    capacity that does not exist.
    """
    async with qec_engine.begin() as conn:
        res = await conn.execute(text(
            "UPDATE explorer_workers "
            "SET in_flight = GREATEST(in_flight - 1, 0), updated_at = now() "
            "WHERE worker_id = :wid"), {"wid": worker_id})
    return bool(res.rowcount)


#: Env: the registry sweeper's tick. Unset/<=0 ⇒ the sweeper is INERT (logs
#: once and returns), the shipped-off-by-default convention every daemon in
#: this service follows. Staleness itself needs no sweep — it is judged pure,
#: per decision — the sweeper only makes the TABLE tell the same truth.
ENV_REGISTRY_TICK = "QEC_WORKER_REGISTRY_TICK_SECONDS"

#: Contract-pinned validation for a REGISTRATION (fleet_heartbeat_v1.json).
WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CAPACITY_MIN, CAPACITY_MAX = 1, 64


def validate_registration(body: Mapping[str, Any]) -> str:
    """Why this registration must be refused — empty string when it is sound.

    PURE, so the route's refusal set is testable without a database. Refusals
    are fail-closed in the safe direction: a worker that cannot be validly
    described is NOT added to the fleet, and dispatch keeps its static-pool
    fallback exactly as before.
    """
    wid = str(body.get("worker_id") or "")
    if not WORKER_ID_RE.match(wid):
        return f"worker_id {wid!r} does not match {WORKER_ID_RE.pattern}"
    url = str(body.get("url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")) or len(url) > 500:
        return "url must be http(s) and at most 500 chars"
    ap = str(body.get("allowlist_path") or "").strip()
    if not ap or not ap.startswith("/") or len(ap) > 500:
        return ("allowlist_path must be an absolute path - without it "
                "qe-central cannot fence this worker and must not offer it work")
    try:
        cap = int(body.get("capacity"))
    except (TypeError, ValueError):
        return "capacity must be an integer"
    if not (CAPACITY_MIN <= cap <= CAPACITY_MAX):
        return f"capacity must be in [{CAPACITY_MIN}, {CAPACITY_MAX}]"
    aff = str(body.get("tenant_affinity") or "")
    if len(aff) > 64:
        return "tenant_affinity too long"
    return ""


async def retire_stale_workers(*, now: datetime | None = None) -> int:
    """Mark every ``active`` worker whose heartbeat aged past the TTL ``stale``.

    STALENESS RETIRES A ROW — visibly. Scheduling already ignores a stale
    worker (the pure ``is_stale`` check needs no sweep), so this changes no
    dispatch decision; it changes what the TABLE says, which is what an
    operator and the A1 exit measurement read. Guarded on ``status = active``
    so a draining/disabled worker keeps its operator-chosen state, and a
    worker that beats again flips itself back (heartbeats carry status).
    """
    now = now or utc_now()
    cutoff = now - timedelta(seconds=heartbeat_ttl_seconds())
    async with qec_engine.begin() as conn:
        res = await conn.execute(text(
            "UPDATE explorer_workers SET status = :stale, updated_at = :now "
            "WHERE status = :active AND last_heartbeat_at < :cutoff"),
            {"stale": STATUS_STALE, "active": STATUS_ACTIVE,
             "cutoff": cutoff, "now": now})
    n = int(res.rowcount or 0)
    if n:
        logger.warning(
            "qec.worker_registry.retired",
            extra={"workers": n, "ttl_s": heartbeat_ttl_seconds(),
                   "detail": ("no heartbeat within the TTL - retired from "
                              "scheduling; the row is kept until the "
                              "retention window for debugging")})
    return n


async def worker_registry_sweeper_daemon() -> None:
    """Env-gated, leader-hosted sweep: retire at TTL, delete at retention.

    Mirrors the reaper daemon's proven shape exactly: unset tick ⇒ logs once
    and does zero DB work; per-tick try/except so one bad pass never kills the
    loop; CancelledError re-raised for clean shutdown.
    """
    import asyncio

    tick = _env_float(ENV_REGISTRY_TICK, 0.0)
    if tick <= 0:
        logger.info("qec.worker_registry.sweeper_disabled",
                    extra={"reason": f"{ENV_REGISTRY_TICK} not set / <= 0"})
        return
    logger.warning("qec.worker_registry.sweeper_started",
                   extra={"tick_s": tick, "ttl_s": heartbeat_ttl_seconds(),
                          "retention_s": worker_retention_seconds()})
    while True:
        try:
            await retire_stale_workers()
            await reap_stale_workers()
        except asyncio.CancelledError:
            logger.info("qec.worker_registry.sweeper_cancelled")
            raise
        except Exception:  # pragma: no cover — a pass never kills the loop
            logger.warning("qec.worker_registry.sweep_failed", exc_info=True)
        await asyncio.sleep(tick)


async def reap_stale_workers(*, now: datetime | None = None) -> int:
    """Delete worker rows whose heartbeat is older than the retention window.

    Distinct from STALENESS: a stale worker stops receiving work IMMEDIATELY
    (that decision is pure and needs no sweep), but its row is retained for the
    retention window so an operator can still see that it existed and when it
    died. Only after that is the row removed.
    """
    now = now or utc_now()
    cutoff = now - timedelta(seconds=worker_retention_seconds())
    async with qec_engine.begin() as conn:
        res = await conn.execute(text(
            "DELETE FROM explorer_workers WHERE last_heartbeat_at < :cutoff"),
            {"cutoff": cutoff})
    n = int(res.rowcount or 0)
    if n:
        logger.warning("qec.worker_registry.reaped", extra={"workers": n})
    return n


async def schedulable_workers(*, tenant_id: str) -> tuple[list[dict], str]:
    """The workers dispatch may use, and the SOURCE that produced them.

    Returns ``(workers, source)`` where source is ``"registry"`` or
    ``"static_pool"``.

    FALLBACK IS THE SAFE DIRECTION. An empty or unreadable registry falls back
    to the static ``QEC_EXPLORER_POOL``, reproducing today's behaviour exactly.
    The alternative — treating an empty registry as zero capacity — would queue
    every crawl on a fresh deploy, before any explorer had registered, and on
    any transient DB blip. Enabling the registry can therefore never make the
    fleet worse than not having it. The source is RETURNED, not hidden, so the
    caller can record which regime a dispatch actually ran under.
    """
    try:
        rows = await list_workers()
    except Exception as exc:
        logger.warning("qec.worker_registry.read_failed",
                       extra={"error": str(exc)[:200]})
        rows = []
    if rows:
        # T-FL-04 — refuse any worker whose egress fence is shared. See
        # ``drop_fence_conflicted``: capacity loss is an incident, a shared
        # fence is a cross-tenant leak.
        return drop_fence_conflicted(rows), "registry"

    from ...clients.config import phase1_settings
    now = utc_now()
    static = []
    for i, w in enumerate(phase1_settings.workers() or ()):
        static.append({
            "worker_id": f"static-{i}",
            "url": w.get("url", ""),
            "allowlist_path": w.get("allowlist_path", ""),
            # A static worker declares no capacity and cannot heartbeat, so it
            # is modelled exactly as the pre-registry code treated it: one
            # single-flight slot, always "fresh". The worker's own 409 remains
            # the real backstop, as it always was.
            "capacity": 1, "in_flight": 0, "status": STATUS_ACTIVE,
            "tenant_affinity": "", "last_heartbeat_at": now, "meta": {},
        })
    return drop_fence_conflicted(static), "static_pool"
