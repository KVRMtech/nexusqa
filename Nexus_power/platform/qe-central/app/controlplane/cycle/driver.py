"""QE-Central S5 — the cycle-driver daemon + per-app cycle state machine (§3.5).

WHAT IT IS
==========
The control plane's engine: a lifespan-hosted asyncio daemon (the
``sentinel_daemon`` pattern — qe_agents.py:136-156; hosted via
``asyncio.create_task`` in ``main.lifespan``, NOT ``@app.on_event``) that, on
each env-gated tick, discovers apps with DUE work (an unprocessed change event,
a schedule tick, or a full-crawl floor) and drives ONE regression cycle per app
over the UNCHANGED VKPower factory purely over HTTP with a service JWT.

Honesty invariants enforced structurally here (never by convention):

  * **Nothing runs without a trigger.**  A cycle exists only for a change event,
    a schedule tick, or a human ``POST /cycles`` — the daemon never invents work.
  * **A budget breach is a TERMINAL ``budget_stopped``, never a partial ``done``.**
    The wall-clock + per-unit budget is asserted at EVERY phase boundary; the
    first breach raises :class:`_BudgetStop`, caught once at the top, and the
    cycle ends ``budget_stopped`` with the breach named.
  * **The cost meter can only UNDER-count.**  Browser-seconds come from the run
    record's ``duration_ms`` (``GET …/runs/{run_id}`` — never a SQL read into the
    nexus DB); an uncorrelated run is an ``unmetered_run`` gap flag, never invented
    seconds (:mod:`app.controlplane.cost.meter`).
  * **A skipped case is never a silent green.**  Selection (Stage-1
    :func:`selector.select`) carries every non-selected case forward with an
    age-labelled verdict; an unmappable case fails SAFE to *selected*; a
    ``possible_deletion`` honest gap blocks the all-green coverage verdict.
  * **One ACTIVE cycle per app.**  Enforced by the ``app_cycles`` partial unique
    index (``uq_app_cycles_one_active_per_app``) — the driver is restart-safe
    without advisory locks; a duplicate insert simply 409s / is skipped.

The DB-free CORE (:func:`execute_cycle`) drives an injected :class:`CycleClient`
and persists through injected :class:`CycleHooks`, so the whole state machine is
unit-testable with a fake factory client and no database (the walking-skeleton
test).  :func:`run_cycle` is the thin DB wrapper (loads the app, wires the hooks
to ``app_cycles`` / ``cost_ledger``, acquires an admission lease).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from sqlalchemy import select, text

from ...db import (
    new_id,
    qec_engine,
    tenant_scoped_qec_session,
    utc_now,
)
from ...db.controlplane_models import (
    CYCLE_STATE_BLACKOUT_DEFERRED,
    CYCLE_STATE_BUDGET_STOPPED,
    CYCLE_STATE_CRAWLING,
    CYCLE_STATE_DONE,
    CYCLE_STATE_FAILED,
    CYCLE_STATE_GENERATING,
    CYCLE_STATE_HEALING,
    CYCLE_STATE_PENDING,
    CYCLE_STATE_PROBING,
    CYCLE_STATE_RUNNING,
    CYCLE_STATE_SELECTING,
    CYCLE_STATE_VERIFYING,
    CYCLE_TRIGGER_FULL_FLOOR,
    CYCLE_TRIGGER_MANUAL,
    CYCLE_TRIGGER_SCHEDULE,
    CYCLE_TRIGGER_WEBHOOK_REPO,
    TERMINAL_CYCLE_STATES,
    AppCycleRow,
    ChangeEventRow,
    is_terminal_cycle_state,
)
from ...db.models import ClientAppRow
from ..cost import meter
from ..scheduling.admission import (
    FAIL_CLOSED_REASONS,
    AdmissionLease,
)
from ..scheduling.distributed import AdmissionBackend, get_admission_backend
from ...observability import (
    dec_admission_in_flight,
    inc_admission_in_flight,
    record_admission_decision,
    record_cycle_completed,
    record_cycle_started,
)
from . import fingerprints as fp
from .change_detector import (
    MODE_FULL,
    MODE_INCREMENTAL,
    AppChangeContext,
    ChangeSet,
    RepoDiff,
    detect,
)
from .fingerprints import FingerprintSnapshot, derive_control_fp, normalize_page_key, probe_live_fingerprints
from .selector import BAND_P0, CaseRef, SelectionResult, default_carry_ttl_cycles, select as select_cases

logger = logging.getLogger(__name__)


# ── Cycle modes (the POST /cycles ``mode`` vocabulary) ─────────────────────
MODE_AUTO = "auto"          # incremental unless the detector escalates to full
MODE_PROBE_ONLY = "probe_only"  # probe → detect → select, then STOP (dry-run)
CYCLE_MODES = frozenset({MODE_AUTO, MODE_FULL, MODE_PROBE_ONLY})


# ── Env knobs (all with dev-safe defaults; tuned per deploy) ────────────────
ENV_TICK_SECONDS = "QEC_CYCLE_TICK_SECONDS"
ENV_RUN_POLL_INTERVAL = "QEC_RUN_POLL_INTERVAL_SECONDS"
ENV_RUN_POLL_MAX = "QEC_RUN_POLL_MAX_SECONDS"
ENV_CYCLE_MAX_WALLCLOCK = "QEC_CYCLE_MAX_WALLCLOCK_SECONDS"
ENV_FULL_FLOOR_SECONDS = "QEC_FULL_FLOOR_SECONDS"
ENV_GET_RUN_RETRIES = "QEC_GET_RUN_RETRIES"
ENV_ADMISSION_MAX_WAIT = "QEC_ADMISSION_MAX_WAIT_SECONDS"
ENV_DISCOVER_LIMIT = "QEC_CYCLE_DISCOVER_LIMIT"


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v


# ── Runner-job status vocabulary (mirrors test_factory run job states) ──────
# A run job is RUNNING while its status is one of these; anything else is a
# terminal outcome (test_factory.py:2951 running→passed/failed/timed_out/error).
_RUN_RUNNING_STATES = frozenset({"running", "idle", "pending", ""})
#: A run counts as GREEN only on a positive pass state (clean_run_v1 = healed green).
_RUN_PASSED_STATES = frozenset({"passed", "clean_run_v1"})
#: Per-scenario timeline statuses that mark a scenario as FAILED (needs heal/verify).
_FAILED_SCENARIO_STATES = frozenset({"failed", "timed_out", "error", "broken"})


# ══════════════════════ the injectable factory client ══════════════════════

class CycleClient(Protocol):
    """The VKPower-factory surface the cycle driver drives (design §3.5).

    Every method rides the same audited HTTP path a human uses (service JWT,
    role=manager).  A FAKE implementation is injected by the walking-skeleton
    test so the whole state machine runs with no network and no database.
    """

    async def fetch_journey_graph(self, *, tenant_id: str, artifact_id: str) -> Optional[dict]: ...
    async def get_rtm(self, *, tenant_id: str, artifact_id: str) -> dict: ...
    async def generate(self, *, tenant_id: str, artifact_id: str) -> dict: ...
    async def run_playwright(
        self, *, tenant_id: str, artifact_id: str, test_ids: Sequence[str], base_url: str,
    ) -> dict: ...
    async def poll_run(self, *, tenant_id: str, artifact_id: str, run_id: str) -> dict: ...
    async def auto_heal(
        self, *, tenant_id: str, artifact_id: str, test_ids: Sequence[str], base_url: str,
    ) -> dict: ...
    async def verify(
        self, *, tenant_id: str, artifact_id: str, test_id: str, base_url: str,
    ) -> dict: ...
    async def triage(self, *, tenant_id: str, artifact_id: str) -> dict: ...
    async def get_run(self, *, tenant_id: str, artifact_id: str, run_id: str) -> Optional[dict]: ...


class _TransientFactory(Exception):
    """Internal marker for a TRANSIENT factory failure (transport error /
    502 / 503 / 504) an IDEMPOTENT call MAY retry.  It never escapes
    :class:`HttpCycleClient` — ``_request`` converts an exhausted (or
    non-idempotent) transient to the honest ``None`` the cycle loop tolerates."""


#: Upstream statuses HttpCycleClient's idempotent-GET retry treats as transient
#: (parity with :data:`app.clients.resilience.TRANSIENT_HTTP_STATUSES`; 429 and
#: all other 4xx are deliberately EXCLUDED — a deterministic error is not retried).
_TRANSIENT_FACTORY_STATUSES = frozenset({502, 503, 504})


class HttpCycleClient:
    """The real :class:`CycleClient` — typed httpx over platform-api (service JWT).

    Reuses the Stage-1 ``clients.factory`` seams for ``/generate`` + ``/rtm`` and
    the fingerprint store's ``fetch_journey_graph`` (each already error-typed and
    pinned), and adds the run/heal/verify/triage/runs seams the driver needs.
    Every call mints a short-lived ``role=manager`` service JWT for the tenant.

    On any transport / non-200 the read methods return an EMPTY/None honest
    result (never raise into the cycle loop) EXCEPT ``generate`` which surfaces
    the upstream failure so a broken generate fails the cycle loudly rather than
    running against stale cases.
    """

    def __init__(self, *, timeout_s: float = 120.0) -> None:
        self._timeout_s = float(timeout_s)

    async def fetch_journey_graph(self, *, tenant_id: str, artifact_id: str) -> Optional[dict]:
        return await fp.fetch_journey_graph(tenant_id=tenant_id, artifact_id=artifact_id)

    async def get_rtm(self, *, tenant_id: str, artifact_id: str) -> dict:
        from ...clients import factory
        try:
            return await factory.get_rtm(tenant_id=tenant_id, artifact_id=artifact_id)
        except factory.FactoryClientError as exc:
            logger.warning(
                "qec.driver.rtm_failed",
                extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                       "status_code": exc.status_code, "detail": exc.detail},
            )
            return {"artifact_id": artifact_id, "tests": []}

    async def generate(self, *, tenant_id: str, artifact_id: str) -> dict:
        from ...clients import factory
        return await factory.generate(tenant_id=tenant_id, artifact_id=artifact_id)

    async def _post(
        self, *, tenant_id: str, path: str, body: dict, timeout_s: float, endpoint: str,
    ) -> Optional[dict]:
        # A POST is a state-changing trigger — NON-idempotent, executed once.
        return await self._request(
            method="POST", tenant_id=tenant_id, path=path, json_body=body,
            timeout_s=timeout_s, endpoint=endpoint, idempotent=False,
        )

    async def _get(
        self, *, tenant_id: str, path: str, timeout_s: float, endpoint: str,
    ) -> Optional[dict]:
        # A GET is a safe read — idempotent, so a transient blip is retried.
        return await self._request(
            method="GET", tenant_id=tenant_id, path=path, json_body=None,
            timeout_s=timeout_s, endpoint=endpoint, idempotent=True,
        )

    async def _request(
        self, *, method: str, tenant_id: str, path: str,
        json_body: Optional[dict], timeout_s: float, endpoint: str, idempotent: bool,
    ) -> Optional[dict]:
        """Issue one authenticated factory call (honest ``None`` on any failure).

        Phase-5.5 wiring: PROPAGATES the correlation id to VKPower (one id spans
        QE-Central and the factory), RECORDS the factory-latency metric on every
        attempt (logical ``endpoint`` label, never the artifact-scoped path), and
        — for an IDEMPOTENT call ONLY — retries a TRANSIENT failure (transport
        error / 502 / 503 / 504) with bounded backoff before degrading to
        ``None``.  A non-idempotent call (the run/heal/verify POSTs) is executed
        EXACTLY ONCE and NEVER auto-retried (no double-submit)."""
        import httpx

        from ...clients.resilience import call_with_retries
        from ...config import settings
        from ...observability import outbound_request_id_headers, record_factory_call
        from ...service_token import mint_service_jwt

        try:
            token = mint_service_jwt(tenant_id)
        except ValueError as exc:
            logger.warning("qec.driver.mint_failed", extra={"error": str(exc)[:200]})
            return None
        headers = outbound_request_id_headers({"Authorization": f"Bearer {token}"})

        async def _attempt() -> Optional[dict]:
            started = time.monotonic()
            status: object = "transport_error"
            try:
                async with httpx.AsyncClient(
                    base_url=settings.platform_api_url, timeout=timeout_s,
                ) as client:
                    resp = await client.request(method, path, json=json_body, headers=headers)
            except Exception as exc:  # transport failure — retryable for an idempotent op
                logger.warning(
                    "qec.driver.transport_error",
                    extra={"tenant_id": tenant_id, "path": path, "error": str(exc)[:300]},
                )
                record_factory_call(
                    endpoint=endpoint, method=method, status=status,
                    duration_seconds=time.monotonic() - started,
                )
                raise _TransientFactory(str(exc)[:200]) from exc
            status = resp.status_code
            record_factory_call(
                endpoint=endpoint, method=method, status=status,
                duration_seconds=time.monotonic() - started,
            )
            if resp.status_code != 200:
                logger.warning(
                    "qec.driver.non_200",
                    extra={"tenant_id": tenant_id, "path": path,
                           "status_code": resp.status_code},
                )
                if resp.status_code in _TRANSIENT_FACTORY_STATUSES:
                    raise _TransientFactory(f"upstream {resp.status_code}")
                return None  # a DETERMINISTIC non-200 — honest None, never retried
            try:
                body = resp.json()
            except Exception:
                return None
            return body if isinstance(body, dict) else {"result": body}

        try:
            return await call_with_retries(
                _attempt, idempotent=idempotent, op=endpoint,
                is_retryable=lambda exc: isinstance(exc, _TransientFactory),
            )
        except _TransientFactory:
            # Retries exhausted (idempotent) or a single transient (non-idempotent):
            # degrade to the honest None the cycle loop already tolerates.
            return None

    async def run_playwright(
        self, *, tenant_id: str, artifact_id: str, test_ids: Sequence[str], base_url: str,
    ) -> dict:
        body = {"test_ids": list(test_ids), "base_url": base_url}
        out = await self._post(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/playwright/run",
            body=body, timeout_s=self._timeout_s, endpoint="run",
        )
        return out or {}

    async def poll_run(self, *, tenant_id: str, artifact_id: str, run_id: str) -> dict:
        out = await self._get(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/playwright/run/{run_id}",
            timeout_s=30.0, endpoint="poll",
        )
        return out or {"run_id": run_id, "status": "unknown"}

    async def auto_heal(
        self, *, tenant_id: str, artifact_id: str, test_ids: Sequence[str], base_url: str,
    ) -> dict:
        body = {"test_ids": list(test_ids), "base_url": base_url, "autonomous": True}
        out = await self._post(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/auto-heal/run-config",
            body=body, timeout_s=self._timeout_s, endpoint="heal",
        )
        return out or {}

    async def verify(
        self, *, tenant_id: str, artifact_id: str, test_id: str, base_url: str,
    ) -> dict:
        body = {"base_url": base_url}
        out = await self._post(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/scripts/{test_id}/verify",
            body=body, timeout_s=self._timeout_s, endpoint="verify",
        )
        return out or {}

    async def triage(self, *, tenant_id: str, artifact_id: str) -> dict:
        out = await self._get(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/triage",
            timeout_s=self._timeout_s, endpoint="triage",
        )
        return out or {}

    async def get_run(self, *, tenant_id: str, artifact_id: str, run_id: str) -> Optional[dict]:
        return await self._get(
            tenant_id=tenant_id,
            path=f"/api/v1/test-factory/{artifact_id}/runs/{run_id}",
            timeout_s=30.0, endpoint="get_run",
        )


# ══════════════════════ budget (vendored SlaBudget + units) ═════════════════
# Unit-budget key aliases → canonical meter units.  ``client_apps.budgets`` may
# use either the ``max_<unit>`` design spelling or the bare unit name (the same
# vocabulary the CI budget gate reads); both normalise here — CONFIG-DRIVEN.
_BUDGET_ALIASES = {
    "browser_seconds": meter.UNIT_BROWSER_SECONDS,
    "max_browser_seconds": meter.UNIT_BROWSER_SECONDS,
    "llm_tokens": meter.UNIT_LLM_TOKENS,
    "max_llm_tokens": meter.UNIT_LLM_TOKENS,
    "substrate_rows": meter.UNIT_SUBSTRATE_ROWS,
    "max_substrate_rows": meter.UNIT_SUBSTRATE_ROWS,
    "wallclock": meter.UNIT_WALLCLOCK_SECONDS,
    "wallclock_seconds": meter.UNIT_WALLCLOCK_SECONDS,
    "max_wallclock": meter.UNIT_WALLCLOCK_SECONDS,
    "max_wallclock_seconds": meter.UNIT_WALLCLOCK_SECONDS,
    "runner_runs": meter.UNIT_RUNNER_RUNS,
    "max_runner_runs": meter.UNIT_RUNNER_RUNS,
    "unmetered_run": meter.UNIT_UNMETERED_RUN,
    "max_unmetered_run": meter.UNIT_UNMETERED_RUN,
}


def parse_cycle_budgets(budgets: Mapping | None) -> dict[str, Decimal]:
    """Normalise a ``client_apps.budgets`` mapping into ``{unit: Decimal}``.

    Only ``…_per_cycle`` unit budgets are read here (monthly caps are a fleet-
    level concern, out of the per-cycle guard).  Unknown / non-numeric / negative
    entries are ignored with a warning — a budget that cannot be parsed must never
    silently DISABLE the guard, but it also must never crash the driver.
    """
    out: dict[str, Decimal] = {}
    for raw_key, raw_val in (budgets or {}).items():
        key = str(raw_key).strip().lower()
        if key.endswith("_per_cycle"):
            key = key[: -len("_per_cycle")]
        unit = _BUDGET_ALIASES.get(key)
        if unit is None:
            continue
        try:
            val = Decimal(str(raw_val))
        except Exception:
            logger.warning("qec.driver.budget_unparseable", extra={"key": raw_key})
            continue
        if val < 0:
            continue
        # A tighter budget for the same unit wins (defensive against dup aliases).
        out[unit] = min(out[unit], val) if unit in out else val
    return out


@dataclass(frozen=True)
class BudgetBreach:
    """One budget dimension that was exceeded mid-cycle (the ``budget_stopped``
    cause).  ``phase`` names the state boundary at which the breach was caught."""

    unit: str
    quantity: Decimal
    budget: Decimal
    phase: str

    def as_dict(self) -> dict:
        return {"unit": self.unit, "quantity": str(self.quantity),
                "budget": str(self.budget), "phase": self.phase}


class _BudgetStop(Exception):
    """Raised the instant a budget is breached — caught ONCE at the cycle top so
    the terminal state is ``budget_stopped``, never a partial ``done``."""

    def __init__(self, breach: BudgetBreach) -> None:
        self.breach = breach
        super().__init__(f"budget breach: {breach.as_dict()}")


class CycleBudget:
    """Wall-clock + per-unit budget guard for ONE cycle (heal_scheduler SlaBudget
    shape — heal_scheduler.py:171-206 — extended with the cost units).

    ``assess(metered, phase)`` is called at EVERY phase boundary; the FIRST
    breach raises :class:`_BudgetStop`.  A missing budget dimension is unbounded
    (no false stop); the wall-clock dimension additionally enforces a hard safety
    ceiling (``QEC_CYCLE_MAX_WALLCLOCK_SECONDS``) so a hung factory can never make
    a cycle run forever even when no explicit wall-clock budget is set.
    """

    def __init__(
        self,
        unit_budgets: Mapping[str, Decimal] | None = None,
        *,
        hard_wallclock_ceiling_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budgets = dict(unit_budgets or {})
        self._clock = clock
        self._started = clock()
        explicit = self._budgets.get(meter.UNIT_WALLCLOCK_SECONDS)
        ceiling = (
            hard_wallclock_ceiling_s
            if hard_wallclock_ceiling_s is not None
            else _env_float(ENV_CYCLE_MAX_WALLCLOCK, 1800.0)
        )
        # The effective wall-clock bound is the TIGHTER of the explicit budget and
        # the safety ceiling (both may be set; the smaller wins).
        bounds = [float(ceiling)] if ceiling and ceiling > 0 else []
        if explicit is not None:
            bounds.append(float(explicit))
        self._wallclock_bound_s: float | None = min(bounds) if bounds else None

    def elapsed_seconds(self) -> Decimal:
        return Decimal(str(round(self._clock() - self._started, 6)))

    def remaining_wallclock_s(self) -> float | None:
        if self._wallclock_bound_s is None:
            return None
        return max(0.0, self._wallclock_bound_s - (self._clock() - self._started))

    def assess(self, metered: Mapping[str, Decimal], *, phase: str) -> None:
        """Raise :class:`_BudgetStop` if wall-clock or any metered unit is over."""
        # Wall-clock (live) — the tighter of explicit budget / safety ceiling.
        if self._wallclock_bound_s is not None:
            elapsed = float(self._clock() - self._started)
            if elapsed >= self._wallclock_bound_s:
                raise _BudgetStop(BudgetBreach(
                    meter.UNIT_WALLCLOCK_SECONDS,
                    Decimal(str(round(elapsed, 6))),
                    Decimal(str(self._wallclock_bound_s)), phase,
                ))
        # Metered units (accumulated so far).
        for unit, limit in self._budgets.items():
            if unit == meter.UNIT_WALLCLOCK_SECONDS:
                continue  # handled live above
            qty = Decimal(str(metered.get(unit, 0) or 0))
            if qty > limit:
                raise _BudgetStop(BudgetBreach(unit, qty, limit, phase))


# ══════════════════════ persistence hooks (DB-free core) ═══════════════════

@dataclass
class CycleHooks:
    """The two persistence seams :func:`execute_cycle` calls, injected so the core
    is DB-free and unit-testable.

    ``save_state(state, **fields)`` persists a state transition (``app_cycles``);
    ``record_units(units, source_ref)`` appends cost entries (``cost_ledger``).
    The default no-op hooks make :func:`execute_cycle` runnable with zero I/O.
    """

    save_state: Callable[..., Awaitable[None]]
    record_units: Callable[..., Awaitable[None]]

    @classmethod
    def noop(cls) -> "CycleHooks":
        async def _save(state: str, **fields) -> None:  # noqa: ANN001
            return None

        async def _units(units: Mapping[str, Decimal], *, source_ref: str = "") -> None:
            return None

        return cls(save_state=_save, record_units=_units)


# ══════════════════════ app config + outcome value objects ═════════════════

@dataclass(frozen=True)
class AppConfig:
    """Everything the state machine needs about one app — loaded ONCE by
    :func:`run_cycle` so :func:`execute_cycle` never touches the DB."""

    app_id: str
    tenant_id: str
    base_url: str
    canonical_host: str
    max_rps: float
    latest_artifact_id: str
    baseline_page_fingerprints: Mapping[str, dict]
    repo_binding: Mapping
    budgets: Mapping
    fences: Mapping

    @classmethod
    def from_row(
        cls, row: ClientAppRow, *, baseline_page_fingerprints: Mapping[str, dict] | None,
    ) -> "AppConfig":
        fences = dict(row.fences or {})
        rps = fences.get("max_rps")
        try:
            rps = float(rps) if rps is not None else 0.0
        except (TypeError, ValueError):
            rps = 0.0
        return cls(
            app_id=row.app_id,
            tenant_id=row.tenant_id,
            base_url=row.base_url or "",
            canonical_host=(row.canonical_host or "").strip().lower(),
            max_rps=rps,
            latest_artifact_id=row.latest_artifact_id or "",
            baseline_page_fingerprints=dict(baseline_page_fingerprints or {}),
            repo_binding=dict(row.repo_binding or {}),
            budgets=dict(row.budgets or {}),
            fences=fences,
        )


@dataclass
class CycleOutcome:
    """The result of one cycle — the exact shape persisted to ``app_cycles``."""

    cycle_id: str
    state: str
    selected_scope: dict = field(default_factory=dict)
    honest_gaps: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)
    error: str = ""

    @property
    def is_terminal(self) -> bool:
        return is_terminal_cycle_state(self.state)


# ══════════════════════ case-ref derivation from /rtm ══════════════════════

def _page_keys_from_assertions(rows: list) -> set[str]:
    """Best-effort page_keys a case's steps touch, from its /rtm rows.

    The pinned /rtm shape (test_vkpower_contracts_s4.py) exposes navigation
    assertions as ``emitted_assertions[].code`` (e.g. ``toHaveURL(/\\/home/)``);
    we extract the path literal so a case can be matched against changed pages.
    This is a fail-safe SUPERSET signal — extracting nothing simply makes the
    case *unmappable* (→ force-selected), never falsely 'unchanged'.
    """
    import re

    keys: set[str] = set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        for a in (row.get("emitted_assertions") or []):
            code = str((a or {}).get("code") or "") if isinstance(a, Mapping) else ""
            for m in re.findall(r"toHaveURL\(\s*/([^/)]*)", code):
                pk = normalize_page_key("/" + m.strip("\\^$"))
                if pk:
                    keys.add(pk)
    return keys


def _control_fps_from_rows(rows: list) -> set[str]:
    """Best-effort control fingerprints a case's steps touch, from ``observed_label``
    (mirrors :func:`derive_control_fp`).  Empty when no label is grounded."""
    fps: set[str] = set()
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("observed_label") or "").strip()
        if label:
            control = derive_control_fp(label)
            if control:
                fps.add(control)
    return fps


def _build_case_refs(
    rtm: Mapping,
    *,
    criticality: Mapping[str, str],
    prior_verdicts: Mapping[str, dict],
) -> tuple[list[CaseRef], set[str]]:
    """Project the /rtm matrix onto :class:`CaseRef`s + the UNMAPPABLE test-id set.

    A case is *unmappable* when neither a page_key nor a control_fp could be
    derived from its rows — the driver then FORCE-SELECTS it (never carries it),
    so a case whose change we cannot localise is never a silent green.
    """
    cases: list[CaseRef] = []
    unmappable: set[str] = set()
    for t in (rtm.get("tests") or []):
        if not isinstance(t, Mapping):
            continue
        test_id = str(t.get("test_id") or "").strip()
        if not test_id:
            continue
        rows = list(t.get("rows") or [])
        page_keys = _page_keys_from_assertions(rows)
        control_fps = _control_fps_from_rows(rows)
        if not page_keys and not control_fps:
            unmappable.add(test_id)
        prior = prior_verdicts.get(test_id) or {}
        cases.append(CaseRef(
            test_id=test_id,
            page_keys=tuple(sorted(page_keys)),
            control_fps=tuple(sorted(control_fps)),
            criticality=str(criticality.get(test_id) or "P2"),
            last_verdict_run_id=str(prior.get("run_id") or ""),
            verdict_age_cycles=int(prior.get("age") or 0),
        ))
    return cases, unmappable


# ══════════════════════ the state machine (DB-free core) ═══════════════════

async def execute_cycle(
    *,
    cycle_id: str,
    mode: str,
    trigger: str,
    app: AppConfig,
    client: CycleClient,
    hooks: CycleHooks | None = None,
    criticality: Mapping[str, str] | None = None,
    prior_verdicts: Mapping[str, dict] | None = None,
    repo_shas: tuple[str | None, str | None] = (None, None),
    repo_diff: RepoDiff | None = None,
    budget: CycleBudget | None = None,
    now: datetime | None = None,
) -> CycleOutcome:
    """Drive ONE regression cycle to a terminal state (DB-free, injectable).

    State walk (design §3.5):
    ``probing → selecting → [crawling] → generating → running → healing →
    verifying → done`` with ``budget_stopped`` / ``failed`` / ``blackout_deferred``
    exits.  A budget breach at ANY phase boundary ⇒ ``budget_stopped`` (never a
    partial ``done``).  ``mode=probe_only`` stops after ``selecting`` (a dry-run).

    All I/O is through ``client`` (factory HTTP) and ``hooks`` (persistence), so
    this function is fully unit-testable with a fake client + no database.
    """
    hooks = hooks or CycleHooks.noop()
    criticality = dict(criticality or {})
    prior_verdicts = dict(prior_verdicts or {})
    budget = budget or CycleBudget(parse_cycle_budgets(app.budgets))
    now = now or _utc_now()
    tenant_id, app_id = app.tenant_id, app.app_id
    metered: dict[str, Decimal] = {}

    async def _meter(units: Mapping[str, Decimal], *, source_ref: str = "") -> None:
        """Accumulate cost in-memory (drives the budget) AND persist it."""
        for unit, qty in units.items():
            metered[unit] = metered.get(unit, Decimal("0")) + Decimal(str(qty))
        await hooks.record_units(units, source_ref=source_ref)

    async def _enforce(phase: str) -> None:
        budget.assess(metered, phase=phase)

    outcome = CycleOutcome(cycle_id=cycle_id, state=CYCLE_STATE_PENDING)
    try:
        # ── blackout gate (loud/traffic-generating work is deferred, not run) ──
        if _in_blackout(app.fences, now):
            await hooks.save_state(
                CYCLE_STATE_BLACKOUT_DEFERRED,
                result={"reason": "app is in a blackout window — deferred",
                        "mode": mode, "trigger": trigger},
            )
            outcome.state = CYCLE_STATE_BLACKOUT_DEFERRED
            outcome.result = {"reason": "blackout_deferred", "mode": mode}
            logger.info("qec.cycle.blackout_deferred",
                        extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id})
            return outcome

        # ── 1) PROBING → the change set ───────────────────────────────────────
        await hooks.save_state(CYCLE_STATE_PROBING, started_at=now)
        change_set = await _detect_change_set(
            app=app, mode=mode, client=client, repo_shas=repo_shas, repo_diff=repo_diff,
        )
        await _enforce(CYCLE_STATE_PROBING)

        # ── 2) SELECTING → what runs vs what carries forward ──────────────────
        await hooks.save_state(CYCLE_STATE_SELECTING)
        rtm = await client.get_rtm(tenant_id=tenant_id, artifact_id=app.latest_artifact_id)
        cases, unmappable = _build_case_refs(
            rtm, criticality=criticality, prior_verdicts=prior_verdicts,
        )
        selection = select_cases(cases, change_set, criticality)
        selection = _force_select_unmappable(selection, unmappable, change_set)
        selected_scope = selection.as_dict()
        honest_gaps = change_set.honest_gaps.as_dict()
        await hooks.save_state(
            CYCLE_STATE_SELECTING,
            selected_scope=selected_scope, honest_gaps=honest_gaps,
        )
        outcome.selected_scope = selected_scope
        outcome.honest_gaps = honest_gaps
        await _enforce(CYCLE_STATE_SELECTING)

        base_result: dict = {
            "mode": change_set.mode, "trigger": trigger,
            "change_reason": change_set.reason,
            "selected_count": len(selection.selected_test_ids),
            "carried_count": len(selection.carried_forward),
            "carried_forward": [c.as_dict() for c in selection.carried_forward],
            "honest_gaps": honest_gaps,
            "coverage_verdict": (
                "blocked_on_p0_gaps"
                if change_set.honest_gaps.has_possible_deletion else "ok"
            ),
            "possible_deletion_pages": list(
                change_set.honest_gaps.vanished_pages_possible_deletion
            ),
        }

        # ── probe_only: a dry-run STOPS here (never generates or runs) ─────────
        if mode == MODE_PROBE_ONLY:
            base_result["dry_run"] = True
            await _record_wallclock(budget, _meter)
            base_result["cost"] = {u: str(q) for u, q in metered.items()}
            return await _finish_done(hooks, outcome, base_result, selection, prior_verdicts)

        if not selection.selected_test_ids:
            # Nothing to run (everything unchanged + carried) — an honest DONE with
            # zero runs, the coverage verdict + carried ages carried through.
            base_result["note"] = "no cases selected — all carried forward with age-labelled verdicts"
            await _record_wallclock(budget, _meter)
            base_result["cost"] = {u: str(q) for u, q in metered.items()}
            return await _finish_done(hooks, outcome, base_result, selection, prior_verdicts)

        # ── 3) GENERATING → compile grounded cases from the substrate ─────────
        await hooks.save_state(CYCLE_STATE_GENERATING)
        generate = await client.generate(tenant_id=tenant_id, artifact_id=app.latest_artifact_id)
        base_result["generate"] = _compact_generate(generate)
        gen_rows = _generate_row_count(generate)
        if gen_rows:
            await _meter({meter.UNIT_SUBSTRATE_ROWS: Decimal(gen_rows)}, source_ref="generate")
        await _enforce(CYCLE_STATE_GENERATING)

        # ── 4) RUNNING → run only the selected cases ──────────────────────────
        await hooks.save_state(CYCLE_STATE_RUNNING)
        run_summary, failed_ids = await _run_and_meter(
            client=client, app=app, test_ids=list(selection.selected_test_ids),
            budget=budget, meter_fn=_meter, phase=CYCLE_STATE_RUNNING,
        )
        base_result["run"] = run_summary
        await _enforce(CYCLE_STATE_RUNNING)

        # ── 5) HEALING → auto-heal the failures (never green-washes) ──────────
        heal_summary: dict | None = None
        if failed_ids:
            await hooks.save_state(CYCLE_STATE_HEALING)
            heal_summary = await _heal_and_meter(
                client=client, app=app, test_ids=failed_ids,
                budget=budget, meter_fn=_meter,
            )
            base_result["heal"] = heal_summary
            await _enforce(CYCLE_STATE_HEALING)

        # ── 6) VERIFYING → deterministic rubric + readiness per changed script ─
        await hooks.save_state(CYCLE_STATE_VERIFYING)
        verify_targets = failed_ids or list(selection.selected_test_ids)
        base_result["verify"] = await _verify_scripts(
            client=client, app=app, test_ids=verify_targets,
        )
        await _enforce(CYCLE_STATE_VERIFYING)

        # ── 7) TRIAGE rollup + DONE ───────────────────────────────────────────
        triage = await client.triage(tenant_id=tenant_id, artifact_id=app.latest_artifact_id)
        base_result["triage"] = _compact_triage(triage)
        await _record_wallclock(budget, _meter)
        base_result["cost"] = {u: str(q) for u, q in metered.items()}
        base_result["failed_test_ids"] = failed_ids
        return await _finish_done(
            hooks, outcome, base_result, selection, prior_verdicts, failed_ids=failed_ids,
        )

    except _BudgetStop as stop:
        # A budget breach is a TERMINAL budget_stopped — NEVER a partial done.
        await _record_wallclock(budget, _meter)
        result = {
            "budget_stopped": stop.breach.as_dict(),
            "mode": mode, "trigger": trigger,
            "cost": {u: str(q) for u, q in metered.items()},
            "coverage_verdict": "budget_stopped",
        }
        result.update({k: outcome.result.get(k) for k in ("selected_count",) if outcome.result})
        await hooks.save_state(
            CYCLE_STATE_BUDGET_STOPPED, result=result, finished_at=_utc_now(),
        )
        outcome.state = CYCLE_STATE_BUDGET_STOPPED
        outcome.result = result
        logger.warning(
            "qec.cycle.budget_stopped",
            extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id,
                   "breach": stop.breach.as_dict()},
        )
        return outcome

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # any other failure → honest terminal 'failed'
        message = str(exc)[:2000]
        await hooks.save_state(CYCLE_STATE_FAILED, error=message, finished_at=_utc_now())
        outcome.state = CYCLE_STATE_FAILED
        outcome.error = message
        logger.error(
            "qec.cycle.failed",
            extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id,
                   "error": message[:300]},
        )
        return outcome


# ── phase helpers ───────────────────────────────────────────────────────────

async def _detect_change_set(
    *, app: AppConfig, mode: str, client: CycleClient,
    repo_shas: tuple[str | None, str | None], repo_diff: RepoDiff | None,
) -> ChangeSet:
    """Probe live fingerprints + repo diff → the :class:`ChangeSet`.

    ``mode=full`` short-circuits to :meth:`ChangeSet.full` (a manual/full-floor
    re-run of everything); otherwise the detector unions the fail-safe repo-SHA
    diff and the fail-safe-to-CHANGED fingerprint diff.
    """
    if mode == MODE_FULL:
        return ChangeSet.full(reason="mode=full re-run requested")

    baseline = FingerprintSnapshot.from_page_fingerprints(app.baseline_page_fingerprints)
    live = await probe_live_fingerprints(
        tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id,
        fetcher=client.fetch_journey_graph,
    )
    context = AppChangeContext(
        app_id=app.app_id, tenant_id=app.tenant_id,
        baseline=baseline, repo_bound=bool(app.repo_binding),
    )
    old_sha, new_sha = repo_shas
    return detect(context, old_sha, new_sha, live, repo_diff)


def _force_select_unmappable(
    selection: SelectionResult, unmappable: set[str], change_set: ChangeSet,
) -> SelectionResult:
    """Ensure every UNMAPPABLE case is SELECTED (fail-safe — never carried).

    A case we could not localise to a page/control must run rather than carry a
    possibly-stale green.  Re-homes any unmappable carried case into ``selected``
    with an explicit reason, preserving the honest per-case reasons.
    """
    if not unmappable:
        return selection
    selected = list(selection.selected_test_ids)
    already = set(selected)
    reasons = dict(selection.per_case_reasons)
    kept_carried = []
    for c in selection.carried_forward:
        if c.test_id in unmappable:
            if c.test_id not in already:
                selected.append(c.test_id)
                already.add(c.test_id)
            reasons[c.test_id] = "unmappable case (no derivable page/control) — fail-safe select"
        else:
            kept_carried.append(c)
    for tid in unmappable:
        if tid not in already:
            selected.append(tid)
            already.add(tid)
            reasons.setdefault(tid, "unmappable case (no derivable page/control) — fail-safe select")
    return SelectionResult(
        mode=selection.mode,
        selected_test_ids=tuple(selected),
        carried_forward=tuple(kept_carried),
        selection_reason=selection.selection_reason + (
            f"; {len(unmappable)} unmappable case(s) fail-safe selected" if unmappable else ""
        ),
        per_case_reasons=reasons,
    )


async def _run_and_meter(
    *, client: CycleClient, app: AppConfig, test_ids: list[str],
    budget: CycleBudget, meter_fn: Callable[..., Awaitable[None]], phase: str,
) -> tuple[dict, list[str]]:
    """Dispatch ``/playwright/run(test_ids)``, poll to terminal, meter the run.

    Returns ``({run_id, status, ...}, failed_test_ids)``.  browser_seconds are
    metered from the durable run record's ``duration_ms``; an uncorrelated run
    becomes an ``unmetered_run`` gap flag (the meter can only under-count).
    """
    started = await client.run_playwright(
        tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id,
        test_ids=test_ids, base_url=app.base_url,
    )
    run_id = str(started.get("run_id") or "")
    if not run_id:
        return ({"run_id": "", "status": "error",
                 "note": "runner did not return a run_id"}, list(test_ids))

    job = await _poll_to_terminal(client=client, app=app, run_id=run_id, budget=budget)
    status = str(job.get("status") or "unknown").lower()

    # Meter from the DURABLE run record (duration_ms) — never SQL into nexus.
    run_record = await _correlate_run(client=client, app=app, run_id=run_id)
    await _meter_run(run_record, run_id=run_id, meter_fn=meter_fn)

    failed_ids = _failed_scenarios(run_record, selected=test_ids, run_status=status)
    return (
        {"run_id": run_id, "status": status,
         "scripts": started.get("scripts"), "failed": len(failed_ids)},
        failed_ids,
    )


async def _heal_and_meter(
    *, client: CycleClient, app: AppConfig, test_ids: list[str],
    budget: CycleBudget, meter_fn: Callable[..., Awaitable[None]],
) -> dict:
    """Auto-heal the failing cases (honest: an unprovable step ⇒ ``stop_reason``,
    NO ``clean_run_version`` — never a silent green)."""
    started = await client.auto_heal(
        tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id,
        test_ids=test_ids, base_url=app.base_url,
    )
    run_id = str(started.get("run_id") or "")
    if not run_id:
        return {"run_id": "", "terminal_state": "error",
                "stop_reason": "runner did not return a heal run_id", "clean_run_version": None}
    job = await _poll_to_terminal(client=client, app=app, run_id=run_id, budget=budget)
    run_record = await _correlate_run(client=client, app=app, run_id=run_id)
    await _meter_run(run_record, run_id=run_id, meter_fn=meter_fn)
    return {
        "run_id": run_id,
        "status": str(job.get("status") or "unknown"),
        "terminal_state": job.get("terminal_state"),
        "stop_reason": job.get("stop_reason") or "",
        "clean_run_version": job.get("clean_run_version"),
        "healed_count": job.get("healed_count"),
    }


async def _verify_scripts(
    *, client: CycleClient, app: AppConfig, test_ids: list[str],
) -> list[dict]:
    """Run the deterministic ``/verify`` rubric + readiness probe per script."""
    out: list[dict] = []
    for test_id in test_ids:
        verdict = await client.verify(
            tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id,
            test_id=test_id, base_url=app.base_url,
        )
        readiness = verdict.get("readiness") if isinstance(verdict, Mapping) else None
        out.append({
            "test_id": test_id,
            "decision": verdict.get("decision") if isinstance(verdict, Mapping) else None,
            "certification_level": (
                verdict.get("certification_level") if isinstance(verdict, Mapping) else None
            ),
            "readiness_status": (readiness or {}).get("status") if isinstance(readiness, Mapping) else None,
        })
    return out


async def _poll_to_terminal(
    *, client: CycleClient, app: AppConfig, run_id: str, budget: CycleBudget,
) -> dict:
    """Poll ``/playwright/run/{run_id}`` until terminal, budget-bounded.

    Bounds: the per-run poll ceiling (``QEC_RUN_POLL_MAX_SECONDS``) AND the
    cycle's remaining wall-clock budget — whichever is tighter.  A wall-clock
    exhaustion mid-poll raises :class:`_BudgetStop` (⇒ budget_stopped), never a
    silent hang.
    """
    interval = _env_float(ENV_RUN_POLL_INTERVAL, 5.0)
    poll_ceiling = _env_float(ENV_RUN_POLL_MAX, 900.0)
    deadline = time.monotonic() + poll_ceiling
    while True:
        job = await client.poll_run(
            tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id, run_id=run_id,
        )
        status = str(job.get("status") or "").lower()
        if status not in _RUN_RUNNING_STATES:
            return job
        # Budget guard — abort the whole cycle (budget_stopped) if wall-clock is up.
        budget.assess({}, phase=f"poll:{run_id[:8]}")
        remaining_budget = budget.remaining_wallclock_s()
        now = time.monotonic()
        if now >= deadline or (remaining_budget is not None and remaining_budget <= 0):
            logger.warning(
                "qec.driver.poll_ceiling",
                extra={"run_id": run_id, "app_id": app.app_id, "last_status": status},
            )
            return {**job, "status": job.get("status") or "timed_out"}
        sleep_for = min(interval, max(0.5, deadline - now))
        if remaining_budget is not None:
            sleep_for = min(sleep_for, remaining_budget)
        await asyncio.sleep(max(0.0, sleep_for))


async def _correlate_run(
    *, client: CycleClient, app: AppConfig, run_id: str,
) -> Optional[dict]:
    """Fetch the durable run header (``GET …/runs/{run_id}`` → run_header) for
    cost + failed-scenario detection, retrying briefly for the ingest lag.

    Returns a dict carrying ``run_id`` + ``duration_ms`` (the run_header) plus a
    ``scenarios`` list, or ``None`` when the run never correlated (⇒ the meter
    records an ``unmetered_run`` gap flag — an honest under-count)."""
    retries = _env_int(ENV_GET_RUN_RETRIES, 3)
    for attempt in range(max(1, retries)):
        timeline = await client.get_run(
            tenant_id=app.tenant_id, artifact_id=app.latest_artifact_id, run_id=run_id,
        )
        header = (timeline or {}).get("run_header") if isinstance(timeline, Mapping) else None
        if isinstance(header, Mapping) and header.get("run_id"):
            merged = dict(header)
            merged["scenarios"] = (timeline or {}).get("scenarios") or []
            return merged
        if attempt < retries - 1:
            await asyncio.sleep(min(2.0, 0.5 * (attempt + 1)))
    return None


async def _meter_run(
    run_record: Optional[dict], *, run_id: str, meter_fn: Callable[..., Awaitable[None]],
) -> None:
    """Meter ONE run: browser_seconds + runner_runs when correlated, else a single
    ``unmetered_run`` gap flag (never invented seconds — meter.py contract)."""
    sample = meter.browser_seconds_from_run(run_record)
    if sample.unmetered or sample.browser_seconds is None:
        await meter_fn({meter.UNIT_UNMETERED_RUN: Decimal(1)}, source_ref=run_id)
    else:
        await meter_fn(
            {meter.UNIT_BROWSER_SECONDS: sample.browser_seconds, meter.UNIT_RUNNER_RUNS: Decimal(1)},
            source_ref=run_id,
        )


def _failed_scenarios(
    run_record: Optional[dict], *, selected: list[str], run_status: str,
) -> list[str]:
    """Which selected cases FAILED — from the per-scenario timeline when available.

    Conservative fail-safe when the timeline is unavailable: a non-green run
    status treats EVERY selected case as failed (heal is safe — it refuses to
    green-wash), so a failure is never silently dropped.  A green run with no
    timeline treats nothing as failed."""
    scenarios = (run_record or {}).get("scenarios") if isinstance(run_record, Mapping) else None
    if isinstance(scenarios, list) and scenarios:
        failed = []
        for s in scenarios:
            if not isinstance(s, Mapping):
                continue
            sid = str(s.get("scenario_id") or s.get("test_id") or "")
            status = str(s.get("status") or s.get("verdict") or "").lower()
            if sid and status in _FAILED_SCENARIO_STATES:
                failed.append(sid)
        return [t for t in selected if t in set(failed)] or failed
    # No timeline — fall back to the run status.
    if run_status in _RUN_PASSED_STATES:
        return []
    return list(selected)


async def _finish_done(
    hooks: CycleHooks,
    outcome: CycleOutcome,
    result: dict,
    selection: SelectionResult,
    prior_verdicts: Mapping[str, dict],
    *,
    failed_ids: list[str] | None = None,
) -> CycleOutcome:
    """Persist the terminal DONE state + the carry-forward verdict map for the
    NEXT cycle (selected-green ⇒ age 0; carried ⇒ age+1)."""
    result["verdicts"] = _next_verdicts(
        selection, prior_verdicts, result, failed_ids or [],
    )
    await hooks.save_state(CYCLE_STATE_DONE, result=result, finished_at=_utc_now())
    outcome.state = CYCLE_STATE_DONE
    outcome.result = result
    return outcome


def _next_verdicts(
    selection: SelectionResult, prior_verdicts: Mapping[str, dict],
    result: dict, failed_ids: list[str],
) -> dict:
    """Build the ``{test_id: {run_id, age}}`` carry map the next cycle reads.

    A selected case that RAN green this cycle resets to age 0 with the fresh
    run_id; a carried case keeps its (incremented) age; a failed case carries NO
    green verdict (empty run_id ⇒ next cycle must re-run it)."""
    run_id = str(((result.get("run") or {}) if isinstance(result.get("run"), Mapping) else {}).get("run_id") or "")
    failed = set(failed_ids)
    verdicts: dict[str, dict] = {}
    for tid in selection.selected_test_ids:
        if tid in failed:
            verdicts[tid] = {"run_id": "", "age": 0}
        else:
            verdicts[tid] = {"run_id": run_id, "age": 0}
    for c in selection.carried_forward:
        verdicts[c.test_id] = {"run_id": c.verdict_run_id, "age": c.verdict_age_cycles}
    return verdicts


async def _record_wallclock(
    budget: CycleBudget, meter_fn: Callable[..., Awaitable[None]],
) -> None:
    """Record the cycle's elapsed wall-clock as a metered unit (source of truth
    for the wallclock cost line)."""
    elapsed = budget.elapsed_seconds()
    if elapsed > 0:
        await meter_fn({meter.UNIT_WALLCLOCK_SECONDS: elapsed}, source_ref="cycle_wallclock")


# ── compaction helpers (keep the persisted result JSONB bounded) ────────────

def _compact_generate(generate: Mapping | None) -> dict:
    g = generate or {}
    return {k: g.get(k) for k in ("success", "generated", "demonstrated", "approved_protected")
            if isinstance(g, Mapping) and k in g}


def _generate_row_count(generate: Mapping | None) -> int:
    g = generate or {}
    if not isinstance(g, Mapping):
        return 0
    try:
        return max(0, int(g.get("demonstrated") or g.get("generated") or 0))
    except (TypeError, ValueError):
        return 0


def _compact_triage(triage: Mapping | None) -> dict:
    t = triage or {}
    if not isinstance(t, Mapping):
        return {}
    out = {k: t.get(k) for k in ("summary", "tally", "counts") if k in t}
    scenarios = t.get("scenarios")
    if isinstance(scenarios, list):
        out["scenario_count"] = len(scenarios)
    return out


def _in_blackout(fences: Mapping, now: datetime) -> bool:
    """True when ``now`` (UTC) falls inside any configured blackout window.

    ``fences.blackout_windows`` = ``[{start:'HH:MM', end:'HH:MM', days:[0-6]?}]``
    (UTC, ISO weekday where Monday=0).  A malformed window is IGNORED (fail-open
    on blackout: we would rather run a cycle than wrongly defer it forever) —
    politeness against the customer host is enforced separately by the admission
    token bucket, which never fails open."""
    windows = (fences or {}).get("blackout_windows") or []
    if not isinstance(windows, list):
        return False
    minute_of_day = now.hour * 60 + now.minute
    weekday = now.weekday()
    for w in windows:
        if not isinstance(w, Mapping):
            continue
        days = w.get("days")
        if isinstance(days, list) and days and weekday not in days:
            continue
        start = _hhmm_to_minutes(w.get("start"))
        end = _hhmm_to_minutes(w.get("end"))
        if start is None or end is None:
            continue
        if start <= end:
            if start <= minute_of_day < end:
                return True
        else:  # window wraps past midnight
            if minute_of_day >= start or minute_of_day < end:
                return True
    return False


def _hhmm_to_minutes(value) -> int | None:
    try:
        hh, mm = str(value).split(":", 1)
        h, m = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h * 60 + m
    return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════ the DB wrapper (run_cycle) ═════════════════════════

async def run_cycle(
    *,
    cycle_id: str,
    tenant_id: str,
    app_id: str,
    mode: str,
    trigger: str,
    client: CycleClient | None = None,
    controller: AdmissionBackend | None = None,
    criticality: Mapping[str, str] | None = None,
    repo_shas: tuple[str | None, str | None] = (None, None),
) -> CycleOutcome:
    """Run ONE cycle for an EXISTING ``app_cycles`` row (DB-backed).

    Loads the app config + baseline fingerprints + prior verdicts, acquires an
    admission lease (global/per-tenant caps + per-host token bucket), then drives
    :func:`execute_cycle` with hooks that persist to ``app_cycles`` /
    ``cost_ledger``.  Releases the lease in a ``finally``.  A cycle already in a
    terminal state is a no-op (idempotent re-entry)."""
    client = client or HttpCycleClient()
    if controller is None:
        # Phase-5.5 distributed scale-out seam: resolve the admission backend from
        # config.  ``QEC_ADMISSION_BACKEND=memory`` (the default) returns the
        # process-local ADMISSION singleton — today's behavior, byte for byte;
        # ``redis`` returns the shared, atomic, fail-closed limiter so N replicas
        # enforce ONE customer-facing rate.  An explicitly-passed controller (the
        # test seam) always wins.
        controller = get_admission_backend()

    row = await _load_cycle_row(tenant_id, cycle_id)
    if row is None:
        logger.warning("qec.run_cycle.missing_row",
                       extra={"tenant_id": tenant_id, "cycle_id": cycle_id})
        return CycleOutcome(cycle_id=cycle_id, state=CYCLE_STATE_FAILED, error="cycle row not found")
    if is_terminal_cycle_state(row.state):
        return CycleOutcome(cycle_id=cycle_id, state=row.state, result=dict(row.result or {}))

    app = await _load_app_config(tenant_id, app_id)
    if app is None:
        await _persist_state(tenant_id, cycle_id, CYCLE_STATE_FAILED,
                             error="app not found or has no latest artifact", finished_at=utc_now())
        return CycleOutcome(cycle_id=cycle_id, state=CYCLE_STATE_FAILED, error="app not found")

    prior_verdicts = await _load_prior_verdicts(tenant_id, app_id, exclude_cycle_id=cycle_id)
    hooks = _db_hooks(tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id)

    lease = await _acquire_admission(controller, app, cycle_id=cycle_id, tenant_id=tenant_id)
    if lease is None:
        # Admission deferred (retryable) or fail-closed (already persisted).  A
        # retryable defer leaves the row where it is for the next tick/trigger.
        return CycleOutcome(cycle_id=cycle_id, state=row.state)
    # Phase-5.5 observability: count the START, track live concurrency (in-flight
    # gauge), and time the run to its terminal state.  Every helper is a hard
    # NO-OP when metrics are disabled/absent and NEVER raises into the loop.
    inc_admission_in_flight()
    record_cycle_started(trigger=trigger, mode=mode)
    _started_at = time.monotonic()
    try:
        outcome = await execute_cycle(
            cycle_id=cycle_id, mode=mode, trigger=trigger, app=app, client=client,
            hooks=hooks, criticality=criticality, prior_verdicts=prior_verdicts,
            repo_shas=repo_shas,
        )
        record_cycle_completed(
            terminal_state=outcome.state,
            duration_seconds=time.monotonic() - _started_at,
        )
        return outcome
    finally:
        await controller.release(lease)
        dec_admission_in_flight()


async def _acquire_admission(
    controller: AdmissionBackend, app: AppConfig, *, cycle_id: str, tenant_id: str,
) -> AdmissionLease | None:
    """Acquire an admission lease with a bounded wait.

    A FAIL-CLOSED denial (no canonical_host / no max_rps) marks the cycle FAILED
    with the honest reason — we never crawl a customer host we cannot rate-key.
    A RETRYABLE denial (caps/tokens) returns None so the caller defers."""
    max_wait = _env_float(ENV_ADMISSION_MAX_WAIT, 30.0)
    deadline = time.monotonic() + max_wait
    while True:
        decision = await controller.try_admit(
            tenant_id=tenant_id, canonical_host=app.canonical_host, max_rps=app.max_rps,
        )
        record_admission_decision(admitted=decision.admitted, reason=decision.reason)
        if decision.admitted:
            return decision.lease
        if decision.reason in FAIL_CLOSED_REASONS:
            await _persist_state(
                tenant_id, cycle_id, CYCLE_STATE_FAILED,
                error=f"admission fail-closed: {decision.reason} "
                      f"(host={app.canonical_host!r}, max_rps={app.max_rps})",
                finished_at=utc_now(),
            )
            logger.warning(
                "qec.run_cycle.admission_fail_closed",
                extra={"tenant_id": tenant_id, "cycle_id": cycle_id, "reason": decision.reason},
            )
            return None
        # Retryable (global/tenant cap, host busy, rate-limited) — bounded wait.
        if time.monotonic() >= deadline:
            logger.info(
                "qec.run_cycle.admission_deferred",
                extra={"tenant_id": tenant_id, "cycle_id": cycle_id, "reason": decision.reason},
            )
            return None
        await asyncio.sleep(min(max(0.5, decision.retry_after_seconds or 1.0), 5.0))


# ── DB hooks + loaders ──────────────────────────────────────────────────────

def _db_hooks(*, tenant_id: str, app_id: str, cycle_id: str) -> CycleHooks:
    async def _save(state: str, **fields) -> None:
        await _persist_state(tenant_id, cycle_id, state, **fields)

    async def _units(units: Mapping[str, Decimal], *, source_ref: str = "") -> None:
        await meter.record_cost(
            tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id,
            units=units, source_ref=source_ref,
        )

    return CycleHooks(save_state=_save, record_units=_units)


async def _persist_state(tenant_id: str, cycle_id: str, state: str, **fields) -> None:
    """Update one ``app_cycles`` row's state (+ optional JSONB/ts fields)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(AppCycleRow).where(
                AppCycleRow.cycle_id == cycle_id, AppCycleRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:  # pragma: no cover — created before the driver runs
            logger.error("qec.persist_state.missing", extra={"cycle_id": cycle_id, "state": state})
            return
        row.state = state
        for key in ("selected_scope", "honest_gaps", "result", "error",
                    "started_at", "finished_at"):
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        row.updated_at = utc_now()


async def _load_cycle_row(tenant_id: str, cycle_id: str) -> AppCycleRow | None:
    async with tenant_scoped_qec_session(tenant_id) as session:
        return (await session.execute(
            select(AppCycleRow).where(
                AppCycleRow.cycle_id == cycle_id, AppCycleRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()


async def _load_app_config(tenant_id: str, app_id: str) -> AppConfig | None:
    """Load the app + its baseline fingerprints into an :class:`AppConfig`."""
    from ...db.controlplane_models import AppFingerprintRow

    async with tenant_scoped_qec_session(tenant_id) as session:
        app_row = (await session.execute(
            select(ClientAppRow).where(
                ClientAppRow.app_id == app_id, ClientAppRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if app_row is None or not (app_row.latest_artifact_id or "").strip():
            return None
        baseline: Mapping[str, dict] = {}
        if app_row.baseline_fingerprint_id:
            fpr = (await session.execute(
                select(AppFingerprintRow).where(
                    AppFingerprintRow.fingerprint_id == app_row.baseline_fingerprint_id,
                    AppFingerprintRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if fpr is not None:
                baseline = dict(fpr.page_fingerprints or {})
        else:
            fpr = (await session.execute(
                select(AppFingerprintRow)
                .where(AppFingerprintRow.tenant_id == tenant_id, AppFingerprintRow.app_id == app_id)
                .order_by(AppFingerprintRow.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if fpr is not None:
                baseline = dict(fpr.page_fingerprints or {})
    return AppConfig.from_row(app_row, baseline_page_fingerprints=baseline)


async def _load_prior_verdicts(
    tenant_id: str, app_id: str, *, exclude_cycle_id: str,
) -> dict[str, dict]:
    """Read the ``verdicts`` carry map from the app's most-recent DONE cycle."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(AppCycleRow)
            .where(
                AppCycleRow.tenant_id == tenant_id, AppCycleRow.app_id == app_id,
                AppCycleRow.state == CYCLE_STATE_DONE, AppCycleRow.cycle_id != exclude_cycle_id,
            )
            .order_by(AppCycleRow.finished_at.desc().nullslast(), AppCycleRow.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    if row is None:
        return {}
    verdicts = (row.result or {}).get("verdicts")
    return dict(verdicts) if isinstance(verdicts, Mapping) else {}


# ══════════════════════ cycle creation (shared by router + daemon) ══════════

async def create_cycle(
    *, tenant_id: str, app_id: str, mode: str, trigger: str,
    change_event_ids: Sequence[str] = (),
) -> str:
    """Create ONE ``app_cycles`` row (state=pending) and bind any draining change
    events to it, in a single tenant-scoped transaction.

    Raises :class:`sqlalchemy.exc.IntegrityError` when an active cycle already
    exists for the app (the partial unique index) — the caller maps that to 409.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: F401  (parity import)

    cycle_id = new_id()
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(AppCycleRow(
            cycle_id=cycle_id, tenant_id=tenant_id, app_id=app_id,
            trigger=trigger, state=CYCLE_STATE_PENDING,
        ))
        await session.flush()  # surfaces the one-active-per-app IntegrityError here
        if change_event_ids:
            await session.execute(
                text(
                    "UPDATE change_events SET processed_cycle_id = :cid "
                    "WHERE tenant_id = :tid AND app_id = :aid "
                    "AND event_id = ANY(:ids) AND processed_cycle_id IS NULL"
                ),
                {"cid": cycle_id, "tid": tenant_id, "aid": app_id,
                 "ids": list(change_event_ids)},
            )
    logger.info(
        "qec.cycle.created",
        extra={"tenant_id": tenant_id, "app_id": app_id, "cycle_id": cycle_id,
               "mode": mode, "trigger": trigger, "bound_events": len(change_event_ids)},
    )
    return cycle_id


# ══════════════════════ the lifespan-hosted daemon ═════════════════════════

_DAEMON_TASKS: set[asyncio.Task] = set()


async def cycle_driver_daemon(*, client: CycleClient | None = None) -> None:
    """The env-gated cycle-driver loop (design §3.5; sentinel_daemon pattern).

    Enabled ONLY when ``QEC_CYCLE_TICK_SECONDS`` > 0 — otherwise it logs and
    returns immediately, so importing/wiring the daemon is inert until an operator
    turns autonomy on.  Each tick discovers DUE work and fires one cycle per app;
    a cycle failure is caught so it NEVER kills the loop (try/except per tick)."""
    interval = _env_float(ENV_TICK_SECONDS, 0.0)
    if interval <= 0:
        logger.info("qec.cycle_daemon.disabled",
                    extra={"reason": f"{ENV_TICK_SECONDS} not set / <= 0"})
        return
    client = client or HttpCycleClient()
    logger.warning("qec.cycle_daemon.started", extra={"interval_s": interval})
    while True:
        try:
            fired = await _tick(client=client)
            logger.info("qec.cycle_daemon.tick", extra={"cycles_fired": fired})
        except asyncio.CancelledError:
            logger.info("qec.cycle_daemon.cancelled")
            raise
        except Exception:  # pragma: no cover — a tick failure never kills the loop
            logger.warning("qec.cycle_daemon.tick_failed", exc_info=True)
        await asyncio.sleep(interval)


async def _tick(*, client: CycleClient) -> int:
    """One daemon tick: discover due work → fire one cycle per app."""
    now = utc_now()
    due = await _discover_due_work(now)
    fired = 0
    for item in due:
        try:
            if await _fire_cycle(item, client=client, now=now):
                fired += 1
        except Exception:  # pragma: no cover — one app's failure never stalls the tick
            logger.warning(
                "qec.cycle_daemon.app_failed",
                extra={"app_id": item.get("app_id"), "tenant_id": item.get("tenant_id")},
                exc_info=True,
            )
    return fired


@dataclass(frozen=True)
class _DueWork:
    tenant_id: str
    app_id: str
    trigger: str
    mode: str
    change_event_ids: tuple[str, ...] = ()
    resume_cycle_id: str = ""


async def _discover_due_work(now: datetime) -> list[dict]:
    """Discover apps with DUE work across the fleet.

    Reads the qecentral DB WITHOUT a tenant GUC (a fleet-wide scan) — honest and
    correct in the dev/superuser posture (open decision #8); a restricted-RLS
    prod deployment needs a dedicated discovery role, a future seam.  A discovery
    failure returns [] (the tick logs + retries) — it never crashes the daemon.

    Due work, in priority order:
      1. RESUME a ``blackout_deferred`` cycle whose window has now lifted;
      2. UNPROCESSED change events (coalesced per app → one incremental cycle);
      3. SCHEDULE ticks (interval / cron) + the full-crawl floor.

    An app that already has an ACTIVE cycle is skipped (the partial unique index
    would 409 the insert anyway)."""
    limit = _env_int(ENV_DISCOVER_LIMIT, 50)
    try:
        active_apps, deferred, changes, apps = await _scan_fleet(limit)
    except Exception:
        logger.warning("qec.cycle_daemon.discover_failed", exc_info=True)
        return []

    out: list[dict] = []
    claimed: set[tuple[str, str]] = set()

    # 1) resume blackout-deferred cycles whose window has lifted.
    for d in deferred:
        key = (d["tenant_id"], d["app_id"])
        if key in claimed:
            continue
        fences = _app_fences(apps, d["app_id"])
        if not _in_blackout(fences, now):
            out.append(_DueWork(
                tenant_id=d["tenant_id"], app_id=d["app_id"],
                trigger=d.get("trigger") or CYCLE_TRIGGER_SCHEDULE, mode=MODE_AUTO,
                resume_cycle_id=d["cycle_id"],
            ).__dict__)
            claimed.add(key)

    # 2) coalesce unprocessed change events per app.
    by_app: dict[tuple[str, str], list[dict]] = {}
    for c in changes:
        by_app.setdefault((c["tenant_id"], c["app_id"]), []).append(c)
    for (tenant_id, app_id), evs in by_app.items():
        key = (tenant_id, app_id)
        if key in claimed or key in active_apps:
            continue
        trigger = (
            CYCLE_TRIGGER_WEBHOOK_REPO
            if any(e.get("source") == "repo_sha" for e in evs) else CYCLE_TRIGGER_SCHEDULE
        )
        out.append(_DueWork(
            tenant_id=tenant_id, app_id=app_id, trigger=trigger, mode=MODE_AUTO,
            change_event_ids=tuple(e["event_id"] for e in evs),
        ).__dict__)
        claimed.add(key)

    # 3) schedule ticks + full-crawl floor.
    for a in apps:
        key = (a["tenant_id"], a["app_id"])
        if key in claimed or key in active_apps:
            continue
        decision = _schedule_decision(a, now)
        if decision is None:
            continue
        trigger, mode = decision
        out.append(_DueWork(
            tenant_id=a["tenant_id"], app_id=a["app_id"], trigger=trigger, mode=mode,
        ).__dict__)
        claimed.add(key)

    return out[:limit]


#: The terminal states as a SQL tuple literal — built from the trusted module
#: constant (NOT user input), byte-identical to the migration's partial-index
#: predicate, so the fleet scan sees exactly the non-terminal (ACTIVE) cycles.
_TERMINAL_SQL_TUPLE = "(" + ", ".join(f"'{s}'" for s in sorted(TERMINAL_CYCLE_STATES)) + ")"


async def _scan_fleet(limit: int) -> tuple[set[tuple[str, str]], list[dict], list[dict], list[dict]]:
    """Fleet-wide (non-tenant-scoped) read of the state the scheduler needs."""
    active_apps: set[tuple[str, str]] = set()
    deferred: list[dict] = []
    changes: list[dict] = []
    apps: list[dict] = []
    async with qec_engine.connect() as conn:
        active_rows = (await conn.execute(text(
            "SELECT tenant_id, app_id, cycle_id, state, trigger FROM app_cycles "
            f"WHERE state NOT IN {_TERMINAL_SQL_TUPLE}"
        ))).mappings().all()
        for r in active_rows:
            active_apps.add((r["tenant_id"], r["app_id"]))
            if r["state"] == CYCLE_STATE_BLACKOUT_DEFERRED:
                deferred.append(dict(r))
        change_rows = (await conn.execute(text(
            "SELECT event_id, tenant_id, app_id, source, created_at FROM change_events "
            "WHERE processed_cycle_id IS NULL ORDER BY created_at ASC LIMIT :lim"
        ), {"lim": max(1, limit) * 20})).mappings().all()
        changes = [dict(r) for r in change_rows]
        app_rows = (await conn.execute(text(
            "SELECT ca.tenant_id, ca.app_id, ca.status, ca.schedule, ca.fences, "
            "ca.latest_artifact_id, "
            "(SELECT max(created_at) FROM app_cycles c "
            " WHERE c.tenant_id = ca.tenant_id AND c.app_id = ca.app_id) AS last_cycle_at, "
            "(SELECT max(created_at) FROM app_cycles c2 "
            " WHERE c2.tenant_id = ca.tenant_id AND c2.app_id = ca.app_id "
            " AND c2.trigger = :full_floor) AS last_full_at "
            "FROM client_apps ca WHERE ca.status = 'active' "
            "AND ca.latest_artifact_id <> '' LIMIT :lim"
        ), {"full_floor": CYCLE_TRIGGER_FULL_FLOOR, "lim": max(1, limit) * 4})).mappings().all()
        apps = [dict(r) for r in app_rows]
    return active_apps, deferred, changes, apps


def _app_fences(apps: list[dict], app_id: str) -> Mapping:
    for a in apps:
        if a["app_id"] == app_id:
            return a.get("fences") or {}
    return {}


def _schedule_decision(app_row: Mapping, now: datetime) -> tuple[str, str] | None:
    """Decide whether an app is DUE by schedule, and with which trigger/mode.

    Returns ``(trigger, mode)`` or ``None``.  Honours a full-crawl floor: when the
    last full cycle is older than ``QEC_FULL_FLOOR_SECONDS`` (a founder honesty
    parameter — bounds the staleness of every 'green'), a FULL cycle is due."""
    schedule = app_row.get("schedule") or {}
    if not isinstance(schedule, Mapping):
        schedule = {}
    last_cycle_at = _coerce_dt(app_row.get("last_cycle_at"))
    last_full_at = _coerce_dt(app_row.get("last_full_at"))

    # Full-crawl floor (independent of the interval/cron schedule).
    floor_s = _env_float(ENV_FULL_FLOOR_SECONDS, 604800.0)  # weekly default
    if floor_s > 0 and (last_full_at is None or _age_seconds(last_full_at, now) >= floor_s):
        # Only fire the floor once there IS a baseline cycle history (never on a
        # brand-new app with zero cycles — that is the first manual/webhook cycle).
        if last_cycle_at is not None:
            return (CYCLE_TRIGGER_FULL_FLOOR, MODE_FULL)

    kind = str(schedule.get("kind") or "manual").strip().lower()
    if kind == "manual":
        return None
    if kind == "interval":
        every = schedule.get("seconds") or schedule.get("interval_seconds")
        try:
            every = float(every)
        except (TypeError, ValueError):
            return None
        if every <= 0:
            return None
        if last_cycle_at is None or _age_seconds(last_cycle_at, now) >= every:
            return (CYCLE_TRIGGER_SCHEDULE, MODE_AUTO)
        return None
    if kind == "cron":
        if _cron_due(str(schedule.get("expr") or schedule.get("cron") or ""), last_cycle_at, now):
            return (CYCLE_TRIGGER_SCHEDULE, MODE_AUTO)
        return None
    return None


def _cron_due(expr: str, last_cycle_at: datetime | None, now: datetime) -> bool:
    """True when a cron tick has elapsed since the last cycle.

    ``croniter`` is import-guarded (design §3.5 new dep): a checkout without it
    installed treats cron schedules as NEVER auto-due (honest — it never invents a
    tick), so the module still loads and the rest of the driver works."""
    if not expr.strip():
        return False
    try:
        from croniter import croniter
    except Exception:
        logger.warning("qec.cycle_daemon.croniter_unavailable",
                       extra={"detail": "croniter not installed — cron schedules inert"})
        return False
    try:
        base = last_cycle_at or (now - _croniter_lookback())
        itr = croniter(expr, base)
        next_fire = itr.get_next(datetime)
        return next_fire <= now
    except Exception as exc:
        logger.warning("qec.cycle_daemon.cron_parse_failed",
                       extra={"expr": expr[:80], "error": str(exc)[:200]})
        return False


def _croniter_lookback():
    from datetime import timedelta
    return timedelta(days=1)


def _coerce_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_seconds(then: datetime, now: datetime) -> float:
    return max(0.0, (now - then).total_seconds())


async def _fire_cycle(item: dict, *, client: CycleClient, now: datetime) -> bool:
    """Create (or resume) + run one cycle for a due-work item.

    Returns True when a cycle was started.  A resume re-drives the existing
    (blackout-deferred) row; otherwise a fresh pending row is created (409 on an
    active cycle ⇒ skipped)."""
    from sqlalchemy.exc import IntegrityError

    tenant_id = item["tenant_id"]
    app_id = item["app_id"]
    mode = item.get("mode") or MODE_AUTO
    trigger = item.get("trigger") or CYCLE_TRIGGER_SCHEDULE
    resume_cycle_id = item.get("resume_cycle_id") or ""

    if resume_cycle_id:
        cycle_id = resume_cycle_id
    else:
        try:
            cycle_id = await create_cycle(
                tenant_id=tenant_id, app_id=app_id, mode=mode, trigger=trigger,
                change_event_ids=item.get("change_event_ids") or (),
            )
        except IntegrityError:
            logger.info("qec.cycle_daemon.active_exists",
                        extra={"tenant_id": tenant_id, "app_id": app_id})
            return False

    task = asyncio.create_task(_run_cycle_guarded(
        cycle_id=cycle_id, tenant_id=tenant_id, app_id=app_id,
        mode=mode, trigger=trigger, client=client,
    ))
    _DAEMON_TASKS.add(task)
    task.add_done_callback(_DAEMON_TASKS.discard)
    return True


async def _run_cycle_guarded(**kwargs) -> None:
    """Run a cycle, swallowing (logging) any error so a create_task never leaks an
    unretrieved exception into the loop."""
    try:
        await run_cycle(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover
        logger.warning("qec.cycle_daemon.run_cycle_failed",
                       extra={"cycle_id": kwargs.get("cycle_id")}, exc_info=True)


def launch_cycle(
    *, cycle_id: str, tenant_id: str, app_id: str, mode: str, trigger: str,
    client: CycleClient | None = None,
) -> asyncio.Task:
    """Fire a cycle as a tracked background task (the ``POST /cycles`` path).

    Lets a human-triggered cycle run IMMEDIATELY and self-drive (admission +
    state machine) even when the autonomous daemon is disabled — mirroring how
    ``playwright_run`` / ``auto_heal_run`` background their runs.  The task is
    tracked so it is not garbage-collected mid-flight."""
    task = asyncio.create_task(_run_cycle_guarded(
        cycle_id=cycle_id, tenant_id=tenant_id, app_id=app_id,
        mode=mode, trigger=trigger, client=client,
    ))
    _DAEMON_TASKS.add(task)
    task.add_done_callback(_DAEMON_TASKS.discard)
    return task


def start_cycle_daemon(application) -> asyncio.Task | None:
    """Start the daemon as a lifespan-hosted task (design §3.5 / main.lifespan).

    Returns the task (stored on ``app.state`` by the caller for clean
    cancellation on shutdown) — or ``None`` when ``QEC_CYCLE_TICK_SECONDS`` is
    unset/<=0, so wiring the daemon is INERT unless autonomy is turned on."""
    interval = _env_float(ENV_TICK_SECONDS, 0.0)
    if interval <= 0:
        logger.info("qec.cycle_daemon.not_started",
                    extra={"reason": f"{ENV_TICK_SECONDS} not set"})
        return None
    task = asyncio.create_task(cycle_driver_daemon())
    _DAEMON_TASKS.add(task)
    task.add_done_callback(_DAEMON_TASKS.discard)
    logger.info("qec.cycle_daemon.task_created", extra={"interval_s": interval})
    return task


async def stop_cycle_daemon(task: asyncio.Task | None) -> None:
    """Cancel the daemon task cleanly on shutdown (idempotent, swallows the
    expected CancelledError)."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown is best-effort
        pass


__all__ = [
    # modes
    "MODE_AUTO", "MODE_FULL", "MODE_INCREMENTAL", "MODE_PROBE_ONLY", "CYCLE_MODES",
    # client
    "CycleClient", "HttpCycleClient",
    # budget
    "CycleBudget", "BudgetBreach", "parse_cycle_budgets",
    # core
    "AppConfig", "CycleOutcome", "CycleHooks", "execute_cycle",
    # db wrappers
    "run_cycle", "create_cycle", "launch_cycle",
    # daemon
    "cycle_driver_daemon", "start_cycle_daemon", "stop_cycle_daemon",
    "ENV_TICK_SECONDS",
]
