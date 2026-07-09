"""QE-Central Phase-5 — the certified-invariant executor (submit-tier, fail-closed).

A ``qec_certified_invariants`` row is a HUMAN-authored, e-signed P0 statement (an
underwriting ceiling, a "money never leaves without dual approval", …) — the
NON-enumerable half of coverage (design §3.4).  The product EXECUTES and CERTIFIES
these; it never claims to auto-discover them.  This module turns one such
invariant into a MEASURED, honest certification RUN over the UNCHANGED VKPower
factory (generate → verify), on an ATTESTED disposable environment only.

Two honesty spines, both structural (never by convention):

  1. **FAIL-CLOSED on the environment.**  Executing an invariant runs the
     compiled suite against a live app — for a submit-tier invariant that MUTATES
     the app.  So before ANY factory call the executor requires a *disposable-env
     attestation* (``env_kind == 'disposable'`` AND an unexpired ``expires_at``).
     A missing / wrong-kind / expired attestation ⇒ ``status='blocked'`` and the
     factory is NEVER called — a mutating run can never touch a non-disposable
     (prod/staging) environment.  The attestation gate mirrors the explorer
     SUBMIT-phase guard (``engines/qe-explorer/app/guard.py``) and the design's
     ``client_apps.env_attestation`` fail-closed rule (§3.5).

  2. **A MUST-REFUSE proof is mandatory — never a green-on-nothing.**  A positive
     run that certifies green proves NOTHING on its own (a test that asserts
     nothing is green on any app).  So the executor ALSO runs the same suite
     against a *break-mode* environment that STRIPS the checked server-side
     behavior, and asserts the suite does NOT certify green there.  If the
     break-mode STAYS green that is a **GREEN_WASH** — the oracle is proving
     nothing — surfaced LOUDLY as ``status='error'`` (never ``certified``).

An invariant is ``certified`` (``behaves=True``) ONLY when ALL of:
  * the positive run certifies green on the attested env, AND
  * a real confirmation baseline exists (``certification_level ==
    'CERTIFIED-EVIDENCED'`` — grounded, table-loaded evidence, not a static
    rubric pass), AND
  * the break-mode run is correctly refused (does NOT certify green).

The factory client is INJECTED (:class:`InvariantFactory`) so the whole decision
core is unit-testable with a fake — no network, no database — and the DB loader
is likewise injectable so the fail-closed / green-wash / certify paths are pinned
without a Postgres.

ZERO LLM anywhere; deterministic.  The verdict-reading + decision primitives carry
all the honesty logic and are unit-tested with no I/O.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.gov_models import CertifiedInvariantRow, ScenarioRow
from ..db.models import ClientAppRow

logger = logging.getLogger(__name__)

MODEL_VERSION = "qec-invariant-exec-v1"

# ── Result statuses (the InvariantResult.status vocabulary) ────────────────
STATUS_CERTIFIED = "certified"   # positive green + baseline + break refused
STATUS_REFUSED = "refused"       # an honest precondition for certification is unmet
STATUS_BLOCKED = "blocked"       # fail-closed: no valid disposable-env attestation
STATUS_ERROR = "error"           # GREEN_WASH detected, or an execution failure
INVARIANT_STATUSES = frozenset(
    {STATUS_CERTIFIED, STATUS_REFUSED, STATUS_BLOCKED, STATUS_ERROR}
)

# ── The VKPower /verify verdict vocabulary this executor reads ─────────────
# (test_factory.py:4685-4707 — decision is deterministic; the LLM never scores.)
DECISION_CERTIFIED = "certified"
CERT_EVIDENCED = "CERTIFIED-EVIDENCED"   # grounded, table-loaded → confirmation baseline
CERT_STATIC = "CERTIFIED-STATIC"         # rubric pass, NO grounded evidence → RENDERS
READINESS_READY = "READY"                # live env reachable + preflight ok (else BLOCKED/DEGRADED)

# ── env_attestation vocabulary (client_apps.env_attestation, §3.5) ─────────
ENV_KIND_DISPOSABLE = "disposable"
#: Key (in env_attestation or fences) that configures the MUST-REFUSE break probe:
#: ``{"base_url": "<full break-mode url>"}`` OR ``{"param": "break", "value": "x"}``
#: (appended to the positive base_url).  Config-driven; NEVER hardcoded.
BREAK_PROBE_KEY = "break_probe"

# ── Env knobs (dev-safe defaults; tuned per deploy — never hardcoded) ──────
ENV_MAX_TESTS = "QEC_INVARIANT_MAX_TESTS"
_DEFAULT_MAX_TESTS = 50


def _max_tests() -> int:
    """Ceiling on the number of cases verified per environment (bounded loop)."""
    try:
        v = int(os.getenv(ENV_MAX_TESTS, "") or _DEFAULT_MAX_TESTS)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TESTS
    return v if v > 0 else _DEFAULT_MAX_TESTS


# ══════════════════════ the injectable factory client ══════════════════════

class InvariantFactory(Protocol):
    """The UNCHANGED-VKPower factory surface the executor drives (service JWT).

    Every method rides the same audited HTTP path a human uses.  A FAKE is
    injected by the unit tests so the whole certification decision runs with no
    network and no database.
    """

    async def generate(self, *, tenant_id: str, artifact_id: str) -> Mapping: ...

    async def get_rtm(self, *, tenant_id: str, artifact_id: str) -> Mapping: ...

    async def verify(
        self, *, tenant_id: str, artifact_id: str, test_id: str, base_url: str,
    ) -> Mapping: ...


class HttpInvariantFactory:
    """The real :class:`InvariantFactory` — typed httpx over platform-api.

    Reuses the pinned ``clients.factory`` seams for ``/generate`` + ``/rtm`` and
    adds the per-script ``/verify`` call (with a live ``base_url`` so the verdict
    carries the readiness probe).  Every call mints a short-lived ``role=manager``
    service JWT for the tenant (R-13).  Non-2xx / transport failures raise so the
    executor surfaces an honest ``status='error'`` rather than a silent success.
    """

    def __init__(self, *, timeout_s: float = 120.0) -> None:
        self._timeout_s = float(timeout_s)

    async def generate(self, *, tenant_id: str, artifact_id: str) -> Mapping:
        from ..clients import factory
        return await factory.generate(tenant_id=tenant_id, artifact_id=artifact_id)

    async def get_rtm(self, *, tenant_id: str, artifact_id: str) -> Mapping:
        from ..clients import factory
        return await factory.get_rtm(tenant_id=tenant_id, artifact_id=artifact_id)

    async def verify(
        self, *, tenant_id: str, artifact_id: str, test_id: str, base_url: str,
    ) -> Mapping:
        import httpx

        from ..config import settings
        from ..service_token import mint_service_jwt

        token = mint_service_jwt(tenant_id)
        path = f"/api/v1/test-factory/{artifact_id}/scripts/{test_id}/verify"
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=self._timeout_s,
        ) as client:
            resp = await client.post(
                path, json={"base_url": base_url},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("detail") or "")
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(
                f"/verify failed ({resp.status_code}) for {test_id}: {detail}"[:300]
            )
        body = resp.json()
        return body if isinstance(body, dict) else {"result": body}


# ══════════════════════ value objects ══════════════════════════════════════

@dataclass(frozen=True)
class InvariantContext:
    """The DB facts the executor needs — loaded ONCE (injectable for tests).

    ``scenario_artifact_id`` is the linked scenario's materialized (or source)
    artifact — the substrate the factory ``generate`` + ``verify`` run against.
    ``env_attestation`` / ``fences`` / ``base_url`` come from the ``client_apps``
    row; the executor reads the disposable attestation + break-probe config from
    them (never from a hardcoded value).
    """

    invariant_id: str
    tenant_id: str
    app_id: str
    statement: str
    band: str
    status: str
    requires_disposable_env: bool
    linked_scenario_ids: tuple[str, ...]
    env_attestation: Mapping[str, Any]
    fences: Mapping[str, Any]
    base_url: str
    scenario_artifact_id: str


@dataclass(frozen=True)
class AttestationCheck:
    """Result of the fail-closed disposable-env attestation gate."""

    ok: bool
    required: bool
    env_kind: str
    expired: bool
    expires_at: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "required": self.required,
            "env_kind": self.env_kind,
            "expired": self.expired,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class InvariantResult:
    """The outcome of one certification run.

    ``status`` ∈ certified | refused | blocked | error; ``behaves`` is True ONLY
    when ``status == 'certified'`` (positive green + confirmation baseline +
    break-mode refused).  ``refuse_proof`` carries the FULL evidence of the
    MUST-REFUSE proof (or why it could not be run) so the certification is
    auditable and never a green-on-nothing.
    """

    status: str
    behaves: bool
    refuse_proof: dict = field(default_factory=dict)
    invariant_id: str = ""
    statement: str = ""
    band: str = ""
    detail: str = ""

    @property
    def certified(self) -> bool:
        return self.status == STATUS_CERTIFIED

    def as_dict(self) -> dict:
        return {
            "model_version": MODEL_VERSION,
            "invariant_id": self.invariant_id,
            "status": self.status,
            "behaves": self.behaves,
            "band": self.band,
            "statement": self.statement,
            "detail": self.detail,
            "refuse_proof": dict(self.refuse_proof),
        }


# ══════════════════════ pure helpers: attestation gate ═════════════════════

def _as_aware_utc(dt: Any) -> Optional[datetime]:
    """Coerce an ISO string / naive datetime to a tz-aware UTC datetime, or None."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if isinstance(dt, str):
        raw = dt.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def check_disposable_attestation(
    env_attestation: Mapping[str, Any] | None,
    *,
    now: datetime,
    required: bool,
) -> AttestationCheck:
    """The FAIL-CLOSED disposable-env gate (pure).

    When ``required`` (submit tier), the attestation MUST declare
    ``env_kind == 'disposable'`` AND carry an unexpired ``expires_at``.  Anything
    missing / wrong-kind / unparseable / expired ⇒ ``ok=False`` (the caller then
    refuses to run any mutating factory call).  When NOT required the gate is a
    no-op pass.
    """
    att = dict(env_attestation or {})
    env_kind = str(att.get("env_kind") or "").strip().lower()
    expires = _as_aware_utc(att.get("expires_at"))
    expires_iso = expires.isoformat() if expires else ""

    if not required:
        return AttestationCheck(
            ok=True, required=False, env_kind=env_kind, expired=False,
            expires_at=expires_iso,
            reason="disposable-env attestation not required for this execution",
        )

    if env_kind != ENV_KIND_DISPOSABLE:
        return AttestationCheck(
            ok=False, required=True, env_kind=env_kind, expired=False,
            expires_at=expires_iso,
            reason=(
                "submit-tier execution requires a disposable-env attestation "
                f"(env_kind must be {ENV_KIND_DISPOSABLE!r}, got "
                f"{env_kind or 'unset'!r}) — refusing (fail-closed)"
            ),
        )
    if expires is None:
        return AttestationCheck(
            ok=False, required=True, env_kind=env_kind, expired=False,
            expires_at="",
            reason=(
                "disposable-env attestation has no parseable expires_at — "
                "refusing (fail-closed)"
            ),
        )
    if expires <= now:
        return AttestationCheck(
            ok=False, required=True, env_kind=env_kind, expired=True,
            expires_at=expires_iso,
            reason="disposable-env attestation has EXPIRED — refusing (fail-closed)",
        )
    return AttestationCheck(
        ok=True, required=True, env_kind=env_kind, expired=False,
        expires_at=expires_iso, reason="disposable-env attestation valid",
    )


# ══════════════════════ pure helpers: URL resolution ═══════════════════════

def resolve_positive_base_url(ctx: InvariantContext) -> str:
    """The attested disposable env URL for the POSITIVE run.

    Prefers an explicit ``env_attestation.base_url`` (the disposable target) and
    falls back to the app's ``base_url``.
    """
    att_url = str((ctx.env_attestation or {}).get("base_url") or "").strip()
    return att_url or str(ctx.base_url or "").strip()


def resolve_break_probe(
    env_attestation: Mapping[str, Any] | None,
    fences: Mapping[str, Any] | None,
    positive_base_url: str,
) -> tuple[str, dict]:
    """Resolve the MUST-REFUSE break-mode URL from config (pure).

    Looks for a ``break_probe`` config in ``env_attestation`` first, then
    ``fences``.  Supports either a full ``{"base_url": ...}`` or a
    ``{"param": ..., "value": ...}`` appended to the positive base_url.  Returns
    ``("", {})`` when no break probe is configured — the caller then REFUSES to
    certify (a MUST-REFUSE proof is mandatory).
    """
    cfg: dict = {}
    for src in (env_attestation, fences):
        candidate = (dict(src or {})).get(BREAK_PROBE_KEY)
        if isinstance(candidate, Mapping) and candidate:
            cfg = dict(candidate)
            break
    if not cfg:
        return "", {}

    base = str(cfg.get("base_url") or "").strip()
    if base:
        return base, cfg

    param = str(cfg.get("param") or "").strip()
    value = str(cfg.get("value") or "").strip()
    if param and positive_base_url:
        sep = "&" if "?" in positive_base_url else "?"
        return f"{positive_base_url}{sep}{param}={value}", cfg
    return "", cfg


# ══════════════════════ pure helpers: /verify verdict reading ══════════════

def _norm(value: Any) -> str:
    return str(value or "").strip()


def verify_decision_certified(verdict: Mapping[str, Any]) -> bool:
    """True iff the deterministic rubric decision is ``certified``."""
    return _norm(verdict.get("decision")).lower() == DECISION_CERTIFIED


def verify_readiness_ready(verdict: Mapping[str, Any]) -> bool:
    """True iff the live readiness probe reported ``READY`` (reachable + preflight
    ok).  A missing readiness block (no live probe) is NOT ready — the executor
    always probes, so an absent probe means the run cannot be trusted green."""
    readiness = verdict.get("readiness")
    if not isinstance(readiness, Mapping):
        return False
    return _norm(readiness.get("status")).upper() == READINESS_READY


def verify_has_confirmation_baseline(verdict: Mapping[str, Any]) -> bool:
    """True iff the verdict carries a real confirmation baseline.

    ``certification_level == 'CERTIFIED-EVIDENCED'`` means the case was certified
    against GROUNDED, table-loaded evidence (a captured confirmation baseline) —
    the BEHAVES signal.  ``CERTIFIED-STATIC`` (rubric pass, no evidence) is
    RENDERS-only and returns False here.
    """
    return _norm(verdict.get("certification_level")).upper() == CERT_EVIDENCED


def verify_is_certified_green(verdict: Mapping[str, Any]) -> bool:
    """True iff this run certifies GREEN on a live env (decision certified AND
    readiness READY).  Used for the positive run (must be True) AND the break-mode
    run (must be False — a green break-mode is a GREEN_WASH)."""
    if not isinstance(verdict, Mapping):
        return False
    return verify_decision_certified(verdict) and verify_readiness_ready(verdict)


def _verdict_summary(verdict: Mapping[str, Any]) -> dict:
    """A compact, auditable projection of one /verify verdict for the proof."""
    if not isinstance(verdict, Mapping):
        return {"malformed": True}
    readiness = verdict.get("readiness")
    readiness_status = (
        _norm(readiness.get("status")) if isinstance(readiness, Mapping) else ""
    )
    return {
        "test_id": verdict.get("test_id"),
        "decision": verdict.get("decision"),
        "certification_level": verdict.get("certification_level"),
        "readiness_status": readiness_status,
        "certified_green": verify_is_certified_green(verdict),
        "evidenced": verify_has_confirmation_baseline(verdict),
    }


# ══════════════════════ pure decision core (no I/O) ════════════════════════

def decide_certification(
    *,
    invariant_id: str,
    statement: str,
    band: str,
    positive_verdicts: Sequence[Mapping[str, Any]],
    break_verdicts: Sequence[Mapping[str, Any]],
    positive_url: str,
    break_url: str,
) -> InvariantResult:
    """Decide the certification outcome from the two verdict sets (PURE).

    Rules (all three required for ``certified``):
      * POSITIVE certifies green — EVERY case certified-green on the attested env;
      * a confirmation BASELINE exists — ≥1 case ``CERTIFIED-EVIDENCED``;
      * the break-mode is correctly REFUSED — the suite does NOT fully certify
        green under break-mode.

    A break-mode that STAYS fully green is a **GREEN_WASH** ⇒ ``status='error'``
    (surfaced loudly, never ``certified``).  A missing proof (no verdicts on
    either side) ⇒ ``refused`` — never a green-on-nothing.
    """
    proof: dict = {
        "attempted": True,
        "positive_base_url": positive_url,
        "break_base_url": break_url,
        "positive": [_verdict_summary(v) for v in positive_verdicts],
        "break": [_verdict_summary(v) for v in break_verdicts],
    }

    if not positive_verdicts or not break_verdicts:
        proof["reason"] = (
            "the MUST-REFUSE proof did not execute on both environments — "
            "cannot certify"
        )
        return InvariantResult(
            status=STATUS_REFUSED, behaves=False, refuse_proof=proof,
            invariant_id=invariant_id, statement=statement, band=band,
            detail=proof["reason"],
        )

    positive_certified = all(verify_is_certified_green(v) for v in positive_verdicts)
    baseline_present = any(
        verify_has_confirmation_baseline(v) for v in positive_verdicts
    )
    break_fully_green = all(verify_is_certified_green(v) for v in break_verdicts)
    green_wash = break_fully_green
    break_refused = not break_fully_green

    proof.update({
        "positive_certified": positive_certified,
        "baseline_present": baseline_present,
        "break_refused": break_refused,
        "green_wash": green_wash,
    })

    # GREEN_WASH first — the loudest failure (the oracle proves nothing).
    if green_wash:
        proof["reason"] = (
            "GREEN_WASH: the break-mode STRIPPED the checked server-side behavior "
            "yet the suite still certified green — the oracle proves nothing; "
            "refusing to certify"
        )
        return InvariantResult(
            status=STATUS_ERROR, behaves=False, refuse_proof=proof,
            invariant_id=invariant_id, statement=statement, band=band,
            detail=proof["reason"],
        )
    if not positive_certified:
        proof["reason"] = (
            "the positive run did NOT certify green on the attested env — "
            "cannot certify"
        )
        return InvariantResult(
            status=STATUS_REFUSED, behaves=False, refuse_proof=proof,
            invariant_id=invariant_id, statement=statement, band=band,
            detail=proof["reason"],
        )
    if not baseline_present:
        proof["reason"] = (
            "no confirmation baseline (no CERTIFIED-EVIDENCED case) — the suite "
            "RENDERS but does not demonstrably BEHAVE; cannot certify"
        )
        return InvariantResult(
            status=STATUS_REFUSED, behaves=False, refuse_proof=proof,
            invariant_id=invariant_id, statement=statement, band=band,
            detail=proof["reason"],
        )

    proof["reason"] = (
        "positive certified green with a confirmation baseline AND the break-mode "
        "was correctly refused — invariant BEHAVES-certified"
    )
    return InvariantResult(
        status=STATUS_CERTIFIED, behaves=True, refuse_proof=proof,
        invariant_id=invariant_id, statement=statement, band=band,
        detail=proof["reason"],
    )


# ══════════════════════ DB loader (injectable) ═════════════════════════════

async def load_invariant_context(
    *, tenant_id: str, invariant_id: str,
) -> Optional[InvariantContext]:
    """Load the invariant + its app + the linked scenario's artifact (qecentral DB).

    Reads (all under the tenant RLS GUC) the ``qec_certified_invariants`` row, the
    owning ``client_apps`` row (env_attestation + fences + base_url), and the
    FIRST linked scenario's materialized (or source) artifact.  Returns ``None``
    when the invariant does not exist for this tenant.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        inv = (await session.execute(
            select(CertifiedInvariantRow).where(
                CertifiedInvariantRow.invariant_id == invariant_id,
                CertifiedInvariantRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if inv is None:
            return None

        app = (await session.execute(
            select(ClientAppRow).where(
                ClientAppRow.app_id == inv.app_id,
                ClientAppRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()

        linked = [str(s) for s in (inv.linked_scenario_ids or []) if str(s).strip()]
        scenario_artifact_id = ""
        if linked:
            scen = (await session.execute(
                select(ScenarioRow).where(
                    ScenarioRow.scenario_id == linked[0],
                    ScenarioRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if scen is not None:
                scenario_artifact_id = (
                    scen.materialized_artifact_id or scen.source_artifact_id or ""
                )

    return InvariantContext(
        invariant_id=inv.invariant_id,
        tenant_id=tenant_id,
        app_id=inv.app_id,
        statement=inv.statement or "",
        band=inv.criticality_band or "P0",
        status=inv.status or "",
        requires_disposable_env=bool(inv.requires_disposable_env),
        linked_scenario_ids=tuple(linked),
        env_attestation=dict(app.env_attestation or {}) if app is not None else {},
        fences=dict(app.fences or {}) if app is not None else {},
        base_url=(app.base_url or "") if app is not None else "",
        scenario_artifact_id=scenario_artifact_id,
    )


# ══════════════════════ the orchestrator ═══════════════════════════════════

def _blocked(ctx: InvariantContext, att: AttestationCheck) -> InvariantResult:
    return InvariantResult(
        status=STATUS_BLOCKED, behaves=False,
        refuse_proof={
            "attempted": False,
            "factory_called": False,
            "attestation": att.as_dict(),
            "reason": att.reason,
        },
        invariant_id=ctx.invariant_id, statement=ctx.statement, band=ctx.band,
        detail=att.reason,
    )


def _refused(ctx: InvariantContext, reason: str) -> InvariantResult:
    return InvariantResult(
        status=STATUS_REFUSED, behaves=False,
        refuse_proof={"attempted": False, "factory_called": False, "reason": reason},
        invariant_id=ctx.invariant_id, statement=ctx.statement, band=ctx.band,
        detail=reason,
    )


def _errored(
    invariant_id: str, reason: str, *, ctx: InvariantContext | None = None,
) -> InvariantResult:
    return InvariantResult(
        status=STATUS_ERROR, behaves=False,
        refuse_proof={"attempted": True, "error": reason},
        invariant_id=invariant_id,
        statement=(ctx.statement if ctx else ""),
        band=(ctx.band if ctx else ""),
        detail=reason,
    )


def _test_ids_from_rtm(rtm: Mapping[str, Any]) -> list[str]:
    """Extract ordered, de-duplicated test_ids from a /rtm matrix."""
    out: list[str] = []
    seen: set[str] = set()
    for t in (rtm.get("tests") or []) if isinstance(rtm, Mapping) else []:
        if not isinstance(t, Mapping):
            continue
        tid = _norm(t.get("test_id"))
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


async def execute_invariant(
    tenant_id: str,
    invariant_id: str,
    *,
    factory: InvariantFactory,
    disposable_required: bool = True,
    loader: Callable[..., Awaitable[Optional[InvariantContext]]] | None = None,
    now: datetime | None = None,
) -> InvariantResult:
    """Execute + certify ONE invariant over the UNCHANGED factory (fail-closed).

    Flow (design §3.4 / Phase-5):
      1. Load the invariant context (``loader`` injectable; default = DB).
      2. **FAIL-CLOSED** disposable-env gate — BEFORE any factory call.  Missing /
         wrong-kind / expired attestation ⇒ ``blocked`` and the factory is NEVER
         called (a mutating submit can never touch a non-disposable env).  The
         gate fires when ``disposable_required`` OR the invariant's own
         ``requires_disposable_env`` is set (you cannot bypass an invariant's
         disposable requirement by passing ``disposable_required=False``).
      3. Require a linked, materialized scenario AND a configured break-mode probe
         (a MUST-REFUSE proof is mandatory) — else ``refused``.
      4. ``generate`` the scenario's artifact → resolve its cases via ``/rtm``.
      5. Run ``/verify`` per case against the POSITIVE env, then against the
         BREAK-MODE env.
      6. :func:`decide_certification` — ``certified`` only on positive-green +
         baseline + break-refused; a green break-mode is a loud GREEN_WASH
         (``error``).

    ``factory`` is injected (fake in tests).  Returns an :class:`InvariantResult`;
    this function never raises for an expected failure — it returns a typed,
    honest result instead.
    """
    _loader = loader or load_invariant_context
    now = now or utc_now()

    try:
        ctx = await _loader(tenant_id=tenant_id, invariant_id=invariant_id)
    except Exception as exc:  # loader failure is honest error, never a false green
        logger.error(
            "qec.invariant.load_failed",
            extra={"tenant_id": tenant_id, "invariant_id": invariant_id,
                   "error": str(exc)[:300]},
        )
        return _errored(invariant_id, f"failed to load invariant context: {exc}"[:300])

    if ctx is None:
        return InvariantResult(
            status=STATUS_ERROR, behaves=False,
            refuse_proof={"attempted": False, "reason": "invariant not found"},
            invariant_id=invariant_id, detail="invariant not found for this tenant",
        )

    # ── 2) FAIL-CLOSED disposable-env attestation (BEFORE any factory call) ──
    required = bool(disposable_required) or bool(ctx.requires_disposable_env)
    att = check_disposable_attestation(ctx.env_attestation, now=now, required=required)
    if not att.ok:
        logger.warning(
            "qec.invariant.blocked",
            extra={"tenant_id": tenant_id, "invariant_id": invariant_id,
                   "app_id": ctx.app_id, "reason": att.reason},
        )
        return _blocked(ctx, att)

    # ── 3) A MUST-REFUSE proof is mandatory: linked artifact + break probe ──
    if not ctx.scenario_artifact_id:
        return _refused(
            ctx,
            "invariant has no linked, materialized scenario to execute — "
            "cannot certify",
        )
    positive_url = resolve_positive_base_url(ctx)
    if not positive_url:
        return _refused(ctx, "no positive base_url resolved for the attested env")
    break_url, break_cfg = resolve_break_probe(
        ctx.env_attestation, ctx.fences, positive_url,
    )
    if not break_url:
        return _refused(
            ctx,
            "no break-mode probe configured (env_attestation/fences "
            f"'{BREAK_PROBE_KEY}') — a MUST-REFUSE proof is required to certify",
        )

    # ── 4) generate → resolve the scenario's cases ──────────────────────────
    try:
        generate = await factory.generate(
            tenant_id=tenant_id, artifact_id=ctx.scenario_artifact_id,
        )
    except Exception as exc:
        return _errored(invariant_id, f"factory.generate failed: {exc}"[:300], ctx=ctx)
    try:
        rtm = await factory.get_rtm(
            tenant_id=tenant_id, artifact_id=ctx.scenario_artifact_id,
        )
    except Exception as exc:
        return _errored(invariant_id, f"factory.get_rtm failed: {exc}"[:300], ctx=ctx)

    test_ids = _test_ids_from_rtm(rtm)[: _max_tests()]
    if not test_ids:
        return _refused(
            ctx, "generate produced no cases — nothing to certify",
        )

    # ── 5) verify per case: POSITIVE env, then BREAK-MODE env ────────────────
    try:
        positive_verdicts = [
            await factory.verify(
                tenant_id=tenant_id, artifact_id=ctx.scenario_artifact_id,
                test_id=tid, base_url=positive_url,
            )
            for tid in test_ids
        ]
        break_verdicts = [
            await factory.verify(
                tenant_id=tenant_id, artifact_id=ctx.scenario_artifact_id,
                test_id=tid, base_url=break_url,
            )
            for tid in test_ids
        ]
    except Exception as exc:
        return _errored(invariant_id, f"factory.verify failed: {exc}"[:300], ctx=ctx)

    # ── 6) decide (pure) ────────────────────────────────────────────────────
    result = decide_certification(
        invariant_id=ctx.invariant_id, statement=ctx.statement, band=ctx.band,
        positive_verdicts=positive_verdicts, break_verdicts=break_verdicts,
        positive_url=positive_url, break_url=break_url,
    )
    # Enrich the proof with execution context (never overwrites the decision).
    enriched = dict(result.refuse_proof)
    enriched.update({
        "factory_called": True,
        "artifact_id": ctx.scenario_artifact_id,
        "break_probe": break_cfg,
        "generate": {
            k: generate.get(k)
            for k in ("success", "generated", "demonstrated")
            if isinstance(generate, Mapping) and k in generate
        },
        "test_ids": test_ids,
        "attestation": att.as_dict(),
    })
    result = InvariantResult(
        status=result.status, behaves=result.behaves, refuse_proof=enriched,
        invariant_id=result.invariant_id, statement=result.statement,
        band=result.band, detail=result.detail,
    )

    if result.status == STATUS_ERROR and enriched.get("green_wash"):
        # A green-on-nothing is a DEFECT — surface it LOUDLY.
        logger.error(
            "qec.invariant.green_wash_detected",
            extra={"tenant_id": tenant_id, "invariant_id": invariant_id,
                   "app_id": ctx.app_id, "break_base_url": break_url},
        )
    else:
        logger.info(
            "qec.invariant.executed",
            extra={"tenant_id": tenant_id, "invariant_id": invariant_id,
                   "app_id": ctx.app_id, "status": result.status,
                   "behaves": result.behaves},
        )
    return result


__all__ = [
    "MODEL_VERSION",
    "STATUS_CERTIFIED", "STATUS_REFUSED", "STATUS_BLOCKED", "STATUS_ERROR",
    "INVARIANT_STATUSES",
    "DECISION_CERTIFIED", "CERT_EVIDENCED", "CERT_STATIC", "READINESS_READY",
    "ENV_KIND_DISPOSABLE", "BREAK_PROBE_KEY",
    "InvariantFactory", "HttpInvariantFactory",
    "InvariantContext", "AttestationCheck", "InvariantResult",
    "check_disposable_attestation",
    "resolve_positive_base_url", "resolve_break_probe",
    "verify_decision_certified", "verify_readiness_ready",
    "verify_has_confirmation_baseline", "verify_is_certified_green",
    "decide_certification",
    "load_invariant_context",
    "execute_invariant",
]
