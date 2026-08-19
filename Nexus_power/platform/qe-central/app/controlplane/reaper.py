"""Active stale-crawl reaper (Phase 0 WS-B — operational hygiene).

A crawl whose worker crashes, or whose completion callback is lost, leaves its
``qe_explorations`` row in a non-terminal status (``pending`` / ``writing`` /
``running`` / ``dispatched``, and — once Phase 2 lands — ``queued`` / ``claimed``)
forever. Today only a READ-TIME stall valve (routers/apps.py) makes the UI *show*
such a row as "stalled"; the row itself never reaches a terminal state, so it
lingers in the DB and can wedge fleet accounting. This daemon turns that passive
detection into an ACTIVE, leader-gated transition to a first-class ``stalled``
terminal state with an honest reason.

M1.7 / T-GW-02 — THIS DAEMON IS NOW A RECONCILER, NOT ONLY A GRAVEDIGGER.
``stalled`` used to be written for two conditions that need opposite responses:
a crawl whose worker really did die mid-walk, and a crawl that FINISHED
perfectly and lost one HTTP POST. The explorer now writes a durable completion
manifest to the shared volume BEFORE it attempts its callback, so the second
case is answerable from disk — see :func:`orphaned_completion`. A stale row is
reconciled from that record where one exists, and only reaped where none does.

Never-green-wash + no-data-loss:
  * A reaped row is marked ``stalled`` with a truthful reason — never a fabricated
    ``completed``. An empty/failed crawl stays honestly RED.
  * ``stalled`` is deliberately NOT in ``internal._TERMINAL_STATES``, so a genuinely
    late-but-valid completion callback still writes its substrate and supersedes the
    reaped status — reaping can never silently discard a real crawl's evidence.
  * The write is a STATUS-GUARDED conditional UPDATE (``WHERE status = <observed>``),
    so if a callback completed the row between our read and write, the update affects
    zero rows and never clobbers a real terminal state (race-safe, lock-free).

Self-gating: enabled ONLY when ``QEC_REAPER_TICK_SECONDS`` > 0. Unset/<=0 ⇒ logs
"disabled" and does zero DB work, byte-identical to today. Hosted under the SAME
leader election as the cycle daemon (main.lifespan), so exactly one replica reaps
fleet-wide.

Scope note (honest): this terminalizes the exploration ROW so the UI never spins
forever and fleet accounting stays clean. The qe-explorer engine's in-process
single-flight lock is a separate concern — a crashed worker loses its in-memory
lock on restart; a genuinely-hung worker is addressed by the Phase-2 worker-health
registry, not here.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from ..db import qec_engine, substrate_engine, utc_now

logger = logging.getLogger(__name__)

ENV_REAPER_TICK = "QEC_REAPER_TICK_SECONDS"
ENV_REAPER_GRACE = "QEC_REAPER_GRACE_SECONDS"
ENV_QUEUE_MAX_WAIT = "QEC_CRAWL_QUEUE_MAX_WAIT_SECONDS"

#: Non-terminal statuses the reaper may terminalize. ``queued``/``claimed`` are the
#: Phase-2 durable-queue states — included up front so the reaper covers them the
#: moment they exist (harmless until then: no such rows are produced yet).
ACTIVE_STATUSES: tuple[str, ...] = (
    "pending", "writing", "running", "dispatched", "queued", "claimed",
)

#: Default staleness window for an in-flight crawl with no stamped wall budget
#: (older rows) — mirrors the read-time stall valve's 30-min deep-crawl ceiling.
_DEFAULT_WALL_S = 1_800.0
#: Default buffer beyond the wall budget for post-crawl substrate write + generate.
_DEFAULT_GRACE_S = 180.0
#: Default max time a crawl may wait in the Phase-2 queue before it is failed.
_DEFAULT_QUEUE_MAX_WAIT_S = 3_600.0

_REAP_REASON = (
    "stalled: no completion callback within wall budget "
    "(crashed worker or lost callback)"
)
_QUEUE_TIMEOUT_REASON = "queue timeout: no worker became available within the max wait"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _as_mapping(v: Any) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def grace_seconds() -> float:
    """The post-crawl grace buffer (substrate write + generation), env-overridable.

    Public so the READ-TIME stall valve (``routers/apps._latest_crawl``) resolves
    staleness from the same configuration the reaper acts on — the two had
    independent copies of this arithmetic and could disagree about whether the
    same crawl was stalled."""
    return _env_float(ENV_REAPER_GRACE, _DEFAULT_GRACE_S)


def queue_max_wait_seconds() -> float:
    """The Phase-2 queue max wait before a queued crawl is failed (env-overridable)."""
    return _env_float(ENV_QUEUE_MAX_WAIT, _DEFAULT_QUEUE_MAX_WAIT_S)


def stale_after_seconds(status: str, stats: Any, *, grace_s: float, queue_max_wait_s: float) -> float:
    """PURE: how long a row in ``status`` may sit before it is considered stale.

    An in-flight crawl gets its stamped wall budget (``stats.budget_wall_ms``) plus a
    grace buffer, falling back to the 30-min ceiling for older un-stamped rows. A
    queued/claimed row gets the queue max-wait. Deterministic, no I/O.
    """
    st = (status or "").strip().lower()
    s = _as_mapping(stats)
    if st in ("queued", "claimed"):
        return queue_max_wait_s + grace_s
    try:
        wall_ms = float(s.get("budget_wall_ms") or 0)
    except (TypeError, ValueError):
        wall_ms = 0.0
    base = (wall_ms / 1000.0) if wall_ms > 0 else _DEFAULT_WALL_S
    return base + grace_s


def _anchor(started_at: Any, created_at: Any) -> datetime | None:
    """The timestamp staleness is measured from: the crawl's start, or its creation
    for a not-yet-started (pending/queued) row. Naive stamps are treated as UTC."""
    dt = started_at or created_at
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_stale(
    *, status: str, started_at: Any, created_at: Any, stats: Any,
    now: datetime, grace_s: float, queue_max_wait_s: float,
) -> bool:
    """PURE: True when a non-terminal row has sat past its staleness window."""
    anchor = _anchor(started_at, created_at)
    if anchor is None:
        return False  # defensive: never reap a row with no measurable age
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elapsed = (now - anchor).total_seconds()
    return elapsed > stale_after_seconds(
        status, stats, grace_s=grace_s, queue_max_wait_s=queue_max_wait_s,
    )


async def _worker_says_dead(row: Mapping, *, now: datetime, grace_s: float) -> bool:
    """LIVENESS: the explorer itself says it is not running this crawl.

    Waiting out a crawl's whole wall budget is the wrong recovery for a process
    that has already died. The explorer is SINGLE-FLIGHT, so a dead crawl holds
    the slot for the entire fleet — every app's Crawl button stays disabled until
    the row clears. Observed live: a mid-sweep deploy left one app's crawl
    orphaned and a DIFFERENT app could not be crawled at all; with a 45-minute
    wall that is 45 minutes of fleet-wide outage for a job that no longer exists.

    Guarded three ways, because reaping a HEALTHY crawl is far worse than
    recovering slowly:
      * a minimum age of ``grace_s`` — a just-dispatched crawl the worker has not
        registered yet must never be mistaken for a dead one;
      * only a definitive ``dead`` counts (every worker reachable and none has
        the job); ``unknown`` falls back to the existing timeout;
      * any failure here returns False, i.e. the old behaviour exactly.
    """
    try:
        anchor = _anchor(row.get("started_at"), row.get("created_at"))
        if anchor is None:
            return False
        n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if (n - anchor).total_seconds() < max(60.0, grace_s):
            return False                      # too young to judge
        crawl_id = str(_as_mapping(row.get("stats")).get("crawl_id") or "")
        if not crawl_id:
            return False                      # pre-liveness row: timeout only
        from ..clients import explorer_client
        verdict = await explorer_client.crawl_liveness(
            crawl_id, str(row.get("tenant_id") or ""))
        if verdict != "dead":
            return False
        logger.warning(
            "qec.reaper.worker_says_dead",
            extra={"exploration_id": row.get("exploration_id"), "crawl_id": crawl_id},
        )
        return True
    except Exception as exc:                  # never let liveness break the reaper
        logger.warning("qec.reaper.liveness_failed", extra={"error": str(exc)[:200]})
        return False


def _reason_for(status: str) -> str:
    return _QUEUE_TIMEOUT_REASON if (status or "").lower() in ("queued", "claimed") else _REAP_REASON


async def _tenant_ids() -> list[str]:
    """Enumerate tenants from the global registry so the reap can scope PER-TENANT
    under RLS. On an RLS-enforced qec role (non-superuser + FORCE RLS), a GUC-less
    fleet query is filtered to zero rows — so the reaper must set the tenant GUC for
    each tenant to SEE its own crawls. The ``tenants`` registry lives in the substrate
    DB (a global, non-tenant-scoped table); falls back to the platform tenant if it
    cannot be read, so the reaper degrades to platform-scope rather than no-op."""
    try:
        async with substrate_engine.begin() as conn:
            rows = (await conn.execute(text("SELECT tenant_id FROM tenants"))).all()
        ids = [str(r[0]) for r in rows if r[0]]
        return ids or ["__platform__"]
    except Exception as exc:  # pragma: no cover — registry unreadable → platform scope
        logger.warning("qec.reaper.tenant_enum_failed",
                       extra={"error": str(exc)[:200]})
        return ["__platform__"]


#: Reconciled instead of reaped: the crawl DID finish, its evidence is on the
#: volume, and only the notification was lost.
_RECONCILED_REASON = "recovered: durable completion manifest re-delivered by the reaper"


async def _reconcile_from_manifest(row: Mapping) -> bool:
    """Try to COMPLETE a stale row from its durable completion record.

    Returns True when the row was reconciled and must NOT be reaped.

    The file reading and the re-delivery both live in
    :mod:`app.controlplane.completion_recovery`; this is only the decision to
    ask.  Every failure mode returns False, which means "reap it as before" —
    a recovery that cannot run must degrade to the pre-M1.7 behaviour, never
    leave a row un-terminalized and spinning in the UI forever.
    """
    crawl_id = str(_as_mapping(row.get("stats")).get("crawl_id") or "")
    if not crawl_id:
        return False
    try:
        from . import completion_recovery
        body = completion_recovery.read_orphaned_completion(crawl_id)
        if body is None:
            return False
        delivered = await completion_recovery.redeliver_completion(crawl_id, body)
    except Exception as exc:                                # never break the sweep
        logger.warning("qec.reaper.reconcile_failed",
                       extra={"crawl_id": crawl_id, "error": str(exc)[:200]})
        return False
    if delivered:
        logger.warning(
            "qec.reaper.reconciled",
            extra={"exploration_id": row.get("exploration_id"), "crawl_id": crawl_id,
                   "note": _RECONCILED_REASON},
        )
    return bool(delivered)


async def reap_stale_explorations(now: datetime | None = None) -> int:
    """Terminalize every stale non-terminal exploration, PER TENANT under RLS.

    On this deployment the qec role is non-superuser with FORCE RLS, so a fleet-wide
    GUC-less scan sees nothing. The reaper therefore enumerates tenants and reaps each
    under its own ``nexus.current_tenant_id`` GUC — RLS-compliant, no BYPASSRLS needed
    (the dedicated least-privilege discovery role remains the Phase-2/6 backbone seam).
    Each stale row is transitioned with a STATUS-GUARDED UPDATE so a concurrent
    completion callback is never clobbered; one tenant's failure never stops the rest.
    """
    now = now or utc_now()
    grace_s = grace_seconds()
    queue_max_wait_s = queue_max_wait_seconds()
    reaped = 0
    for tenant in await _tenant_ids():
        try:
            async with qec_engine.begin() as conn:
                # Scope this transaction to the tenant so RLS admits its rows.
                await conn.execute(
                    text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
                    {"t": tenant},
                )
                rows = (await conn.execute(text(
                    "SELECT exploration_id, tenant_id, status, started_at, created_at, stats "
                    "FROM qe_explorations "
                    f"WHERE status IN {tuple(ACTIVE_STATUSES)} "
                    "ORDER BY updated_at ASC LIMIT 500"
                ))).mappings().all()
                for r in rows:
                    stale = is_stale(
                        status=r["status"], started_at=r["started_at"],
                        created_at=r["created_at"], stats=r["stats"], now=now,
                        grace_s=grace_s, queue_max_wait_s=queue_max_wait_s,
                    )
                    if not stale and not await _worker_says_dead(
                        r, now=now, grace_s=grace_s,
                    ):
                        continue
                    # ── M1.7 / T-GW-02 · RECONCILE BEFORE YOU BURY ─────────
                    # A crawl that FINISHED and lost its callback is not a
                    # stall, and writing ``stalled`` on it discards a completed
                    # crawl's evidence for want of one HTTP POST. Ask the volume
                    # what actually happened before terminalizing the row.
                    #
                    # Checked here rather than earlier so the cost is paid only
                    # for rows already judged stale — a healthy fleet does no
                    # filesystem work at all.
                    if await _reconcile_from_manifest(r):
                        continue
                    result = await conn.execute(text(
                        "UPDATE qe_explorations "
                        "SET status='stalled', error=:err, finished_at=:now, updated_at=:now "
                        "WHERE exploration_id=:eid AND status=:prev AND tenant_id=:t"
                    ), {
                        "err": _reason_for(r["status"]), "now": now,
                        "eid": r["exploration_id"], "prev": r["status"], "t": tenant,
                    })
                    if (result.rowcount or 0) > 0:
                        reaped += 1
                        logger.warning(
                            "qec.reaper.reaped",
                            extra={"exploration_id": r["exploration_id"], "tenant_id": tenant,
                                   "prev_status": r["status"]},
                        )
        except Exception as exc:  # pragma: no cover — one tenant never stalls the sweep
            logger.warning("qec.reaper.tenant_failed",
                           extra={"tenant_id": tenant, "error": str(exc)[:200]})
    if reaped:
        logger.warning("qec.reaper.tick", extra={"reaped": reaped})
    return reaped


async def stale_crawl_reaper_daemon() -> None:
    """Env-gated, leader-hosted reaper loop (mirrors ``cycle_driver_daemon``).

    Enabled ONLY when ``QEC_REAPER_TICK_SECONDS`` > 0 — otherwise it logs and returns
    immediately (zero DB work, byte-identical to today). Per-tick try/except so one
    failed sweep never kills the loop; re-raises CancelledError for clean shutdown.
    """
    interval = _env_float(ENV_REAPER_TICK, 0.0)
    if interval <= 0:
        logger.info("qec.reaper.disabled", extra={"reason": f"{ENV_REAPER_TICK} not set / <= 0"})
        return
    logger.warning("qec.reaper.started", extra={"interval_s": interval})
    while True:
        try:
            await reap_stale_explorations()
        except asyncio.CancelledError:
            logger.info("qec.reaper.cancelled")
            raise
        except Exception:  # pragma: no cover — a sweep failure never kills the loop
            logger.warning("qec.reaper.tick_failed", exc_info=True)
        await asyncio.sleep(interval)
