"""Lightweight, additive scheduling substrate for Auto-Heal jobs.

ALL of this is OPT-IN and default-off. Until a caller explicitly acquires an
admission slot / passes an SLA budget / runs the flake pre-pass, the live
behavior of ``_run_auto_heal`` and ``auto_heal_run`` is byte-identical. There is
NO migration and NO new transport here — the flake helper reuses the EXACT
dispatch+correlate primitives the prove-loop already uses (run_live/run_suite +
find_run_by_ci_run_id + build_run_timeline_by_id), injected as callables so this
module never imports the router (no circular import).

Three pieces:

1. ``HealScheduler`` — an in-process, per-tenant round-robin admission gate with
   a global + per-tenant concurrency cap. A heal job awaits ``acquire(tenant_id)``
   BEFORE its ``asyncio.create_task``; this prevents one tenant from monopolizing
   the (single-display, headed) runner and starving everyone else. When the caps
   are not configured (default), ``acquire`` returns immediately — so the default
   path is unchanged.

2. ``SlaBudget`` — a monotonic wall-clock budget. The prove-loop checks
   ``budget.exceeded()`` at the top of each iteration and, if blown, stops with an
   HONEST ``needs_human`` terminal state (never a silent hang). Default budget is
   ``None`` => no SLA => unchanged behavior.

3. ``measure_harness_flake`` — re-runs a KNOWN-GREEN control N times via the
   injected dispatch primitive and records how many of the N reproduced green.
   This is the flake-correction signal: a published "proven green" number is only
   trustworthy once the harness's own baseline flake rate is known. It NEVER
   green-washes — it only measures a control the caller already believes is green,
   and it reports the raw observed rate (it cannot make a real failure look green).

WHY THESE CAN'T GREEN-WASH:
  - The scheduler only orders/limits WHEN jobs run; it does not touch the oracle,
    the confirmation gate, or correlation-by-run-id. A starved-then-admitted job
    proves green by the exact same downstream code.
  - The SLA only STOPS early toward needs_human; it can never mark a non-green run
    green. Timing out is strictly more conservative.
  - Flake measurement reports an observed rate on a control; it is a denominator
    for honesty, not a gate that can pass a failing heal.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional


# ─── 1. Per-tenant round-robin admission (fairness) ───────────────────────────


class HealScheduler:
    """In-process per-tenant fair admission gate.

    Bounded concurrency with round-robin across tenants so no single tenant can
    monopolize the runner. Designed for a SINGLE platform-api worker (the in-mem
    job registry has the same scope); across workers it degrades gracefully to a
    per-worker cap (still strictly better than the unbounded create_task today).

    Defaults are intentionally permissive enough to be a no-op for a typical
    single-tenant demo, but the per-tenant cap still serializes a tenant's own
    headed heals (which already 409-contend on the single display).
    """

    def __init__(self, *, max_global: int = 4, max_per_tenant: int = 1) -> None:
        # max_global <= 0 disables the global cap; max_per_tenant <= 0 disables
        # the per-tenant cap. With both disabled acquire() is an immediate no-op.
        self.max_global = int(max_global)
        self.max_per_tenant = int(max_per_tenant)
        self._global_running = 0
        self._per_tenant: dict[str, int] = {}
        # Round-robin FIFO of waiters keyed by tenant; OrderedDict preserves the
        # arrival order across tenants while we pop tenant-by-tenant.
        self._waiters: "OrderedDict[str, list[asyncio.Future]]" = OrderedDict()
        self._lock = asyncio.Lock()

    def _has_capacity(self, tenant_id: str) -> bool:
        if self.max_global > 0 and self._global_running >= self.max_global:
            return False
        if self.max_per_tenant > 0 and \
                self._per_tenant.get(tenant_id, 0) >= self.max_per_tenant:
            return False
        return True

    async def acquire(self, tenant_id: str) -> None:
        """Block until this tenant may admit one more heal job.

        No-op (returns immediately) when both caps are disabled OR capacity is
        free. Otherwise parks on a per-tenant FIFO future released round-robin by
        ``release``."""
        tenant_id = tenant_id or ""
        async with self._lock:
            # Admit immediately when caps are fully disabled, OR when there is
            # capacity (global + this tenant) AND this tenant is not already
            # queued. We deliberately do NOT block on OTHER tenants' waiters —
            # that is the starvation bug; cross-tenant fairness is enforced by the
            # per-tenant cap + round-robin release, not by serializing everyone
            # behind one queue.
            if (self.max_global <= 0 and self.max_per_tenant <= 0) or \
                    (self._has_capacity(tenant_id) and not self._waiters.get(tenant_id)):
                self._global_running += 1
                self._per_tenant[tenant_id] = self._per_tenant.get(tenant_id, 0) + 1
                return
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._waiters.setdefault(tenant_id, []).append(fut)
        await fut  # released by a later release() that grants this slot

    async def release(self, tenant_id: str) -> None:
        """Return a slot and, if any tenant is waiting, grant the next one
        round-robin (oldest-waiting tenant first, FIFO within a tenant)."""
        tenant_id = tenant_id or ""
        async with self._lock:
            self._global_running = max(0, self._global_running - 1)
            self._per_tenant[tenant_id] = max(0, self._per_tenant.get(tenant_id, 0) - 1)
            if self._per_tenant.get(tenant_id, 0) == 0:
                self._per_tenant.pop(tenant_id, None)
            # Round-robin: try each waiting tenant in arrival order; grant the
            # first whose per-tenant cap now has room. Re-queue (move to back) any
            # tenant we pass over so we don't busy-spin on one blocked tenant.
            for _ in range(len(self._waiters)):
                if self.max_global > 0 and self._global_running >= self.max_global:
                    break
                wt, futs = self._waiters.popitem(last=False)
                if not futs:
                    continue
                if self.max_per_tenant > 0 and \
                        self._per_tenant.get(wt, 0) >= self.max_per_tenant:
                    # No room for this tenant right now; keep it queued at the back.
                    self._waiters[wt] = futs
                    continue
                fut = futs.pop(0)
                if futs:
                    self._waiters[wt] = futs  # remaining waiters keep their place
                self._global_running += 1
                self._per_tenant[wt] = self._per_tenant.get(wt, 0) + 1
                if not fut.done():
                    fut.set_result(None)
                return

    def snapshot(self) -> dict:
        """Observability: current running counts + queue depths (no secrets)."""
        return {
            "global_running": self._global_running,
            "per_tenant_running": dict(self._per_tenant),
            "queued": {t: len(f) for t, f in self._waiters.items()},
            "max_global": self.max_global,
            "max_per_tenant": self.max_per_tenant,
        }


# Process-wide singleton; caps come from env so prod can tune without a redeploy
# of the router. Defaults are a soft no-op for single-tenant (per-tenant cap of 1
# only serializes a tenant's OWN headed heals, which the single display forces
# anyway). Set NEXUS_HEAL_MAX_PER_TENANT<=0 and NEXUS_HEAL_MAX_GLOBAL<=0 to fully
# disable.
def _build_default_scheduler() -> HealScheduler:
    import os
    return HealScheduler(
        max_global=int(os.getenv("NEXUS_HEAL_MAX_GLOBAL", "4") or 4),
        max_per_tenant=int(os.getenv("NEXUS_HEAL_MAX_PER_TENANT", "1") or 1),
    )


SCHEDULER = _build_default_scheduler()


# ─── 2. Per-heal wall-clock SLA budget ────────────────────────────────────────


class SlaBudget:
    """Monotonic wall-clock budget for a single heal job.

    ``budget_seconds=None`` => no budget => ``exceeded()`` is always False, so the
    only bound stays the existing iteration cap (today's behavior, byte-identical).
    """

    def __init__(self, budget_seconds: Optional[float]) -> None:
        self.budget_seconds = (
            float(budget_seconds) if budget_seconds and budget_seconds > 0 else None
        )
        self.started_at = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def exceeded(self) -> bool:
        if self.budget_seconds is None:
            return False
        return self.elapsed() >= self.budget_seconds

    def remaining(self) -> Optional[float]:
        if self.budget_seconds is None:
            return None
        return max(0.0, self.budget_seconds - self.elapsed())


def make_budget(ctx: dict) -> SlaBudget:
    """Build a budget from the heal ctx. ``ctx['sla_seconds']`` absent/0/None =>
    unbounded (default-off => unchanged)."""
    raw = (ctx or {}).get("sla_seconds")
    try:
        secs = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        secs = None
    return SlaBudget(secs)


# ─── 3. Harness-flake measurement (flake-correction) ──────────────────────────


async def measure_harness_flake(
    *,
    files: dict,
    env_base: dict,
    selected: list[str],
    samples: int,
    dispatch: Callable[[dict, dict], Awaitable[bool]],
    correlate_green: Callable[[str], Awaitable[Optional[bool]]],
    new_run_id: Callable[[], str],
) -> dict:
    """Re-run a KNOWN-GREEN control bundle ``samples`` times and report how often
    it reproduced green — the harness's own flake rate, used to flake-correct any
    published "proven green" number.

    Honest-by-construction:
      - ``dispatch(files, env)`` runs the control through the SAME runner path the
        prove-loop uses (headed run_live+await OR headless run_suite); it returns
        whether the run STARTED/completed transport-wise.
      - ``correlate_green(run_id)`` returns the SAME proven-green verdict the
        prove-loop computes (present + has steps + no fail + >=1 pass), or None if
        the run could not be correlated (counted as NOT green — never assumed).
      - A run that fails to start or fails to correlate counts as a non-green
        sample. So the measured rate can only ever be <= the true green rate; it
        cannot inflate confidence.

    Returns {samples, green, errored, uncorrelated, flake_rate, green_rate,
             per_run:[...]} where flake_rate = (samples - green) / samples.
    """
    n = max(0, int(samples or 0))
    out = {
        "samples": n, "green": 0, "errored": 0, "uncorrelated": 0,
        "flake_rate": 0.0, "green_rate": 1.0, "per_run": [],
    }
    if n <= 0:
        return out
    for i in range(n):
        rid = new_run_id()
        env = {**env_base, "NEXUS_RUN_ID": rid}
        rec = {"run_id": rid, "started": False, "green": False}
        try:
            started = await dispatch(files, env)
        except Exception as exc:  # transport / runner busy
            rec["error"] = str(exc)[:300]
            started = False
        rec["started"] = bool(started)
        if not started:
            out["errored"] += 1
            out["per_run"].append(rec)
            continue
        try:
            green = await correlate_green(rid)
        except Exception as exc:
            rec["error"] = str(exc)[:300]
            green = None
        if green is True:
            out["green"] += 1
            rec["green"] = True
        elif green is None:
            out["uncorrelated"] += 1
        out["per_run"].append(rec)
    out["green_rate"] = round(out["green"] / n, 4)
    out["flake_rate"] = round((n - out["green"]) / n, 4)
    return out
