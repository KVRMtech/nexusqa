"""Active stale-crawl reaper (Phase 0 WS-B — operational hygiene).

A crawl whose worker crashes, or whose completion callback is lost, leaves its
``qe_explorations`` row in a non-terminal status (``pending`` / ``writing`` /
``running`` / ``dispatched``, and — once Phase 2 lands — ``queued`` / ``claimed``)
forever. Today only a READ-TIME stall valve (routers/apps.py) makes the UI *show*
such a row as "stalled"; the row itself never reaches a terminal state, so it
lingers in the DB and can wedge fleet accounting. This daemon turns that passive
detection into an ACTIVE, leader-gated transition to a first-class ``stalled``
terminal state with an honest reason.

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

from ..db import qec_engine, utc_now

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


def _reason_for(status: str) -> str:
    return _QUEUE_TIMEOUT_REASON if (status or "").lower() in ("queued", "claimed") else _REAP_REASON


async def reap_stale_explorations(now: datetime | None = None) -> int:
    """Terminalize every stale non-terminal exploration fleet-wide. Returns the count.

    Fleet-wide, GUC-less read/write via ``qec_engine`` — the SAME documented posture
    the cycle-driver's ``_scan_fleet`` uses (the least-privilege discovery role is the
    Phase-2/6 backbone seam, tracked separately). Each stale row is transitioned with a
    STATUS-GUARDED UPDATE so a concurrent completion callback is never clobbered.
    """
    now = now or utc_now()
    grace_s = _env_float(ENV_REAPER_GRACE, _DEFAULT_GRACE_S)
    queue_max_wait_s = _env_float(ENV_QUEUE_MAX_WAIT, _DEFAULT_QUEUE_MAX_WAIT_S)
    reaped = 0
    async with qec_engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT exploration_id, tenant_id, status, started_at, created_at, stats "
            "FROM qe_explorations "
            f"WHERE status IN {tuple(ACTIVE_STATUSES)} "
            "ORDER BY updated_at ASC LIMIT 500"
        ))).mappings().all()
        for r in rows:
            if not is_stale(
                status=r["status"], started_at=r["started_at"], created_at=r["created_at"],
                stats=r["stats"], now=now, grace_s=grace_s, queue_max_wait_s=queue_max_wait_s,
            ):
                continue
            result = await conn.execute(text(
                "UPDATE qe_explorations "
                "SET status='stalled', error=:err, finished_at=:now, updated_at=:now "
                "WHERE exploration_id=:eid AND status=:prev"
            ), {
                "err": _reason_for(r["status"]), "now": now,
                "eid": r["exploration_id"], "prev": r["status"],
            })
            if (result.rowcount or 0) > 0:
                reaped += 1
                logger.warning(
                    "qec.reaper.reaped",
                    extra={"exploration_id": r["exploration_id"], "tenant_id": r["tenant_id"],
                           "prev_status": r["status"]},
                )
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
