"""M3.3 / T-FL-01 — the queue DRAINER daemon.

A durable queue that nothing drains is a durable way to lose work. This is the
loop that turns ``queued`` back into a running crawl the moment the fleet has
room.

SHAPE
=====
Mirrors ``stale_crawl_reaper_daemon`` exactly, because that shape is already
proven in this service: env-gated (``QEC_QUEUE_DRAIN_TICK_SECONDS`` unset ⇒ the
daemon logs and returns, and the whole milestone is inert), per-tick
``try/except`` so one bad pass never kills the loop, and ``CancelledError``
re-raised for a clean shutdown.

LEADER-ELECTED
==============
Hosted under the same ``build_leader_election`` wrapper as the cycle driver and
the reaper. N replicas of qe-central must not each drain the queue: they would
each read the same fair order and dispatch the same crawls, and the SKIP LOCKED
claim would be the only thing standing between the fleet and duplicate work.
The claim is correct on its own — but relying on it as the primary defence would
mean every replica burning a full dispatch attempt to lose a race.

ORDER OF OPERATIONS, AND WHY
============================
  1. read live capacity from the worker registry — never from a configured
     number, so a fleet that lost half its pods drains at HALF the rate rather
     than dispatching into workers that are gone;
  2. plan the drain in FAIR order across tenants (the pure core), bounded by
     free slots;
  3. CLAIM each crawl (``FOR UPDATE SKIP LOCKED`` + status guard);
  4. dispatch it;
  5. on failure, REQUEUE it — a claimed crawl that could not be dispatched must
     go back to the queue keeping its place, never evaporate.

Step 3 before step 4 is the important one: claiming first means a crawl is
either queued or claimed at every instant, so a drainer that dies mid-pass
leaves rows the reaper can adjudicate rather than rows nobody owns.
"""
from __future__ import annotations

import asyncio
import logging

from . import queue_store, worker_registry

logger = logging.getLogger(__name__)



async def _publish_fleet_metrics(workers: list, capacity: dict) -> None:
    """Export the gauges KEDA scales on. Never raises into the drain loop.

    ``oldest_wait_s`` matters as much as depth: a queue of 3 that has been
    waiting 40 minutes is a different incident from a queue of 3 draining every
    few seconds, and only the former should page.
    """
    try:
        from datetime import datetime, timezone

        from ...observability import metrics

        queued = await queue_store.read_queued_fleet()
        oldest = 0.0
        now = datetime.now(timezone.utc)
        for row in queued:
            created = row.get("created_at")
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            oldest = max(oldest, (now - created).total_seconds())
        metrics.record_fleet_state(
            queue_depth=len(queued), oldest_wait_s=oldest,
            capacity=int(capacity.get("capacity") or 0),
            in_flight=int(capacity.get("in_flight") or 0),
            workers_alive=int(capacity.get("workers_alive") or 0),
            fence_conflicts=len(worker_registry.fence_conflicts(workers)),
        )
    except Exception:  # pragma: no cover — metrics never break a drain
        logger.debug("qec.queue.metrics_publish_failed", exc_info=True)


#: Env: opt-in to draining against the STATIC pool (no registry). Off by
#: default — see the refusal in ``drain_once``.
ENV_DRAIN_STATIC_POOL = "QEC_QUEUE_DRAIN_STATIC_POOL"

#: Refusal-log pacing: WARN on the first refused tick and every Nth after, so
#: an hour of "registry still empty" is visible without drowning the log.
_REFUSAL_LOG_EVERY = 60
_refused_ticks = 0


def _static_drain_enabled() -> bool:
    import os
    return (os.environ.get(ENV_DRAIN_STATIC_POOL, "") or "").strip() in (
        "1", "true", "yes", "on")


def _log_registry_empty_refusal() -> None:
    global _refused_ticks
    if _refused_ticks % _REFUSAL_LOG_EVERY == 0:
        logger.warning(
            "qec.queue.drain_refused_registry_empty",
            extra={"refused_ticks": _refused_ticks + 1,
                   "detail": ("no explorer worker has REGISTERED (A1 heartbeat "
                              "protocol) - queued crawls stay queued rather "
                              "than dispatching blind into the static pool; "
                              "set QEC_QUEUE_DRAIN_STATIC_POOL=1 to override")})
    _refused_ticks += 1


def _log_registry_populated() -> None:
    global _refused_ticks
    if _refused_ticks:
        logger.warning("qec.queue.drain_registry_populated",
                       extra={"after_refused_ticks": _refused_ticks})
        _refused_ticks = 0


async def drain_once() -> dict:
    """One drain pass. Returns a counts dict for logging/metrics/tests.

    Never raises: a drain failure must not kill the daemon, and a partially
    completed pass is safe because every crawl is either still queued, claimed
    (and adjudicable by the reaper), or dispatched.
    """
    started = requeued = failed = 0
    try:
        workers, source = await worker_registry.schedulable_workers(tenant_id="")
        now = worker_registry.utc_now()
        ttl = worker_registry.heartbeat_ttl_seconds()
        capacity = worker_registry.fleet_capacity(workers, now=now, ttl_s=ttl)
        free = int(capacity.get("free") or 0)
        # ── TEAM A / PHASE A — A QUEUE WITH NO REGISTERED WORKERS SAYS SO ──
        # Draining against the static-pool fallback would dispatch blind: a
        # static worker declares no capacity and cannot heartbeat, so every
        # tick would burn a dispatch attempt against a worker that may be
        # busy, gone, or running an image that never announces itself — and a
        # queued crawl would cycle claim→409→requeue forever while the log
        # said nothing about WHY. Until a worker has REGISTERED (A1), the
        # drainer refuses each pass loudly instead of spinning; the reaper's
        # queue-timeout still terminalizes rows honestly if nothing ever
        # registers. An operator who really wants static-pool draining can say
        # so with QEC_QUEUE_DRAIN_STATIC_POOL=1.
        if source != "registry" and not _static_drain_enabled():
            await _publish_fleet_metrics(workers, capacity)
            _log_registry_empty_refusal()
            return {"started": 0, "requeued": 0, "failed": 0,
                    "free_slots": free, "source": source,
                    "refused": "registry_empty"}
        _log_registry_populated()
        if free <= 0:
            # Publish here too: a fleet at zero free capacity is precisely when
            # the autoscaler most needs the queue depth. Returning early without
            # publishing would freeze the gauge at its last value exactly when
            # it matters most.
            await _publish_fleet_metrics(workers, capacity)
            return {"started": 0, "requeued": 0, "failed": 0, "free_slots": 0,
                    "source": source}

        plan = await queue_store.plan_drain(free_slots=free)
        await _publish_fleet_metrics(workers, capacity)
        if not plan:
            return {"started": 0, "requeued": 0, "failed": 0, "free_slots": free,
                    "source": source}

        # Import here, not at module scope: the router imports this package, so
        # a module-level import would close an import cycle.
        from ...routers.explorations import _dispatch_explorer

        for row in plan:
            tenant_id = str(row.get("tenant_id") or "")
            exploration_id = str(row.get("exploration_id") or "")
            app_id = str(row.get("app_id") or "")
            if not (tenant_id and exploration_id and app_id):
                continue
            if not await queue_store.claim(
                    tenant_id=tenant_id, exploration_id=exploration_id):
                continue          # another drainer won it — not an error
            try:
                await _dispatch_explorer(
                    tenant_id=tenant_id, app_id=app_id, from_queue=True)
                started += 1
                logger.warning(
                    "qec.queue.drained",
                    extra={"exploration_id": exploration_id,
                           "tenant_id": tenant_id, "app_id": app_id})
            except Exception as exc:
                # Back to the queue, keeping its place in the fair order. The
                # reaper's queue-timeout is the backstop that eventually
                # terminalizes a crawl that can never be dispatched, so this
                # cannot cycle forever unnoticed.
                if await queue_store.requeue(
                        tenant_id=tenant_id, exploration_id=exploration_id,
                        reason=str(exc)[:300]):
                    requeued += 1
                else:
                    failed += 1
                logger.warning(
                    "qec.queue.dispatch_failed_requeued",
                    extra={"exploration_id": exploration_id,
                           "tenant_id": tenant_id, "error": str(exc)[:200]})
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("qec.queue.drain_pass_failed", exc_info=True)
    return {"started": started, "requeued": requeued, "failed": failed}


async def crawl_queue_drainer_daemon() -> None:
    """Env-gated, leader-hosted drain loop.

    Enabled ONLY when ``QEC_QUEUE_DRAIN_TICK_SECONDS`` > 0. Unset, it logs once
    and returns, doing zero database work — so this milestone ships inert and is
    turned on deliberately.
    """
    import os
    try:
        interval = float(os.environ.get(queue_store.ENV_DRAIN_TICK, "") or 0.0)
    except (TypeError, ValueError):
        interval = 0.0
    if interval <= 0:
        logger.info(
            "qec.queue.drainer_disabled",
            extra={"reason": f"{queue_store.ENV_DRAIN_TICK} not set / <= 0"})
        return
    logger.warning("qec.queue.drainer_started", extra={"interval_s": interval})
    while True:
        try:
            counts = await drain_once()
            if counts.get("started") or counts.get("requeued"):
                logger.info("qec.queue.drain_tick", extra=counts)
        except asyncio.CancelledError:
            logger.info("qec.queue.drainer_cancelled")
            raise
        except Exception:  # pragma: no cover — a pass never kills the loop
            logger.warning("qec.queue.drain_tick_failed", exc_info=True)
        await asyncio.sleep(interval)
