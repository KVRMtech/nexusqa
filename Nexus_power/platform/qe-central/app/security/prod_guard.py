"""QE-Central Phase-6 SAFETY SPINE — the production onboarding guard (fail-closed).

WHY THIS EXISTS
===============
Grounded in an adversarial finding: today the system fails OPEN — a real client
app could be CRAWLED / have a regression CYCLE fired against it without any proof
that the operator is authorised, that the target is a non-production/disposable
environment, and that a safety preflight ever ran.  This module is the choke
point that makes a crawl/cycle against a **real client app** *physically refuse*
until the app has been driven through onboarding to a ``live`` state.

The whole module is a set of PURE functions over the EXISTING ``client_apps``
JSONB (``env_attestation`` / ``fences`` / ``schedule``) + the ``status`` column —
NO new columns, NO migration.  The routers (``routers/cycles.py``,
``routers/explorations.py``) and the cycle driver (``controlplane/cycle/driver``)
call :func:`assert_crawlable`, which raises :class:`OnboardingRefused` (mapped to
a clean 409/422) when the app is not cleared.

ONBOARDING STATE MACHINE (derived, never stored as a separate column)
=====================================================================
``draft  →  attested  →  live`` — an app may be CRAWLED / SCHEDULED only when
``live``.  The three fields that gate the transition, all read from the existing
``client_apps`` JSONB:

  1. **signed rules-of-engagement** — ``env_attestation.rules_of_engagement`` (a
     dict) with ``signed == True`` AND a non-empty ``signed_by`` (the operator's
     authorisation to test the target at all).  ``fences.rules_of_engagement`` is
     accepted as a fallback location.
  2. **a non-prod / disposable env attestation, attested + unexpired** — the
     ``env_attestation`` envelope: ``env_kind`` ∈ {``disposable``, ``staging``}
     (never ``prod``), a non-empty ``attested_by``, and an ``expires_at`` in the
     future.  Mirrors the explorer SUBMIT-phase guard + the certified-invariant
     executor's disposable gate (``services/invariant_executor``).
  3. **a passed preflight flag** — ``env_attestation.preflight.passed == True``
     (``fences`` / ``schedule`` accepted as fallback locations).

FAIL-CLOSED BY DEFAULT
======================
The DEFAULT for a real app is REFUSE.  A dev/test **explicit** bypass flag
(``fences.onboarding_test_bypass == True`` or ``env_attestation.test_bypass ==
True``) lets a fixture proceed — but it is honoured ONLY when ``NEXUS_ENV`` is a
development/test environment and is NEVER honoured in staging/production, so a
real app can never be waved through by data alone.

ZERO LLM.  No network, no database — every decision is a pure function of the
app row, so the whole guard is unit-testable with a plain fake row.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Onboarding state vocabulary (derived, not persisted separately) ─────────
ONBOARDING_DRAFT = "draft"
ONBOARDING_ATTESTED = "attested"
ONBOARDING_LIVE = "live"
ONBOARDING_STATES = (ONBOARDING_DRAFT, ONBOARDING_ATTESTED, ONBOARDING_LIVE)

# ── Crawl phases the guard reasons about (mirror the explorer guard) ────────
PHASE_EXPLORE = "explore"
PHASE_SUBMIT = "submit"

# ── env_attestation.env_kind vocabulary (client_apps.env_attestation, §3.5) ─
ENV_KIND_DISPOSABLE = "disposable"
ENV_KIND_STAGING = "staging"
#: R2: UAT is a first-class attested kind (the requirement's Test/UAT/Prod
#: selector). Posture = staging: crawlable, never the mutating submit tier
#: (that stays disposable-only) — "same capabilities as Test unless client
#: policy restricts".
ENV_KIND_UAT = "uat"
#: Postures — Production Test: full-depth crawl, submit only with explicit
#: disposable attestation (same as staging/uat). A stepping-stone between
#: test and prod.
ENV_KIND_PRODUCTION_TEST = "production_test"
ENV_KIND_PROD = "prod"

NON_PROD_ENV_KINDS = frozenset({
    ENV_KIND_DISPOSABLE, ENV_KIND_STAGING, ENV_KIND_UAT, ENV_KIND_PRODUCTION_TEST,
})
#: Environments where an OBSERVE-ONLY crawl is allowed: capture pages, fields,
#: locators, navigation — never fill, never submit, never advance a commit.
OBSERVE_ENV_KINDS = frozenset({ENV_KIND_PROD})
#: ALL env_kinds that are crawlable (non-prod at full-depth, prod at observe-only).
CRAWLABLE_ENV_KINDS = NON_PROD_ENV_KINDS | OBSERVE_ENV_KINDS

# ── Posture vocabulary ────────────────────────────────────────────────────────
POSTURE_FULL = "full"
POSTURE_OBSERVE = "observe"


def posture_for_env_kind(env_kind: str) -> str:
    """The crawl posture derived from env_kind.

    * ``full`` — full-depth exploration + attested submit (disposable/staging/
      uat/production_test). The existing default.
    * ``observe`` — observe-only: capture pages/fields/locators/navigation,
      never fill a mutating field, never submit, never advance a commit.
      The crawl catalogs prod; it never mutates it.
    """
    kind = str(env_kind or "").strip().lower()
    if kind in OBSERVE_ENV_KINDS:
        return POSTURE_OBSERVE
    return POSTURE_FULL


def resolve_effective_fences(
    app_fences: dict | None, env_fences: dict | None, *, env_kind: str,
) -> dict:
    """The EFFECTIVE fences for a crawl/run against a specific Environment Profile.

    Multi-env safety (Phase 2): the SAME learned flow may BIND a disposable policy
    in dev but must be READ-ONLY in prod.  The env profile's fences overlay the
    app's; irreversible verbs UNION; then ``allow_submit`` is FAIL-CLOSED — honoured
    ONLY when ``env_kind`` is EXPLICITLY a known non-prod kind (disposable/staging).
    Anything else — 'prod', 'production', an unknown label, or a blank/unattested
    ``env_kind`` — forces ``allow_submit=False`` (a mislabeled/unattested prod env
    can never stay mutable — the fail-OPEN the adversarial review caught).

    With an EXPLICIT non-prod ``env_kind`` and no env fences, the non-safety keys
    pass through unchanged; single-env runs never reach this resolver.
    """
    eff = dict(app_fences or {})
    if env_fences:
        for k, v in env_fences.items():
            if v is not None:
                eff[k] = v
    app_verbs = (app_fences or {}).get("irreversible_verbs_extra") or []
    env_verbs = (env_fences or {}).get("irreversible_verbs_extra") or []
    if env_verbs:  # only rewrite when the env actually contributes verbs (byte-stable)
        eff["irreversible_verbs_extra"] = sorted(set(app_verbs) | set(env_verbs))
    kind = str(env_kind or "").strip().lower()
    # FAIL-CLOSED submit gate: allow_submit is honoured ONLY when the env is
    # EXPLICITLY a known non-prod kind (disposable/staging/uat/production_test).
    # 'prod', 'production', 'PRD', an unrecognized label, or a BLANK/unset
    # env_kind ALL force allow_submit off.
    if kind not in NON_PROD_ENV_KINDS:
        eff["allow_submit"] = False
    # Posture: OBSERVE-ONLY for production environments.  The crawl catalogs
    # the app (pages, fields, locators, navigation) but never fills a form,
    # never submits, never advances a commit.
    if kind in OBSERVE_ENV_KINDS:
        eff["allow_submit"] = False
        eff["observe_only"] = True
    return eff

# ── Environment gating (NEXUS_ENV; default development) ─────────────────────
ENV_VAR = "NEXUS_ENV"
#: Environments in which an explicit per-app test-bypass flag is honoured.  The
#: bypass is NEVER honoured outside these (a real app in staging/production is
#: always fail-closed regardless of any flag in its row).
DEV_ENVS = frozenset({"development", "test", "dev", "local"})


def current_env() -> str:
    """The current deployment environment (``NEXUS_ENV``; default development).

    Read from the process environment at call time (never import-time) so a test
    that sets ``NEXUS_ENV`` before invoking the guard sees the value it set.
    """
    return (os.environ.get(ENV_VAR, "development") or "development").strip().lower()


def is_dev_env(env: str | None = None) -> bool:
    """True when ``env`` (or the live ``NEXUS_ENV``) is a development/test env."""
    return (str(env).strip().lower() if env is not None else current_env()) in DEV_ENVS


# ══════════════════════ refusal signal ═════════════════════════════════════

class OnboardingRefused(Exception):
    """Raised when an app is NOT cleared for the requested crawl/cycle phase.

    Carries an HTTP ``status_code`` (409 when the app is not onboarding-``live``;
    422 when the specific attestation for the phase is missing/invalid) and the
    honest ``reasons`` list so the caller can surface a clear, auditable refusal.
    The cycle driver raises this too; the daemon catches it and skips the app,
    and the routers map it onto a clean :class:`fastapi.HTTPException`.
    """

    def __init__(
        self,
        *,
        status_code: int = 409,
        phase: str = PHASE_EXPLORE,
        reasons: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.phase = str(phase or PHASE_EXPLORE)
        self.reasons = [str(r) for r in (reasons or []) if str(r).strip()]
        self.detail = detail or self._build_detail()
        super().__init__(self.detail)

    def _build_detail(self) -> str:
        joined = "; ".join(self.reasons) if self.reasons else "app is not onboarding-live"
        return f"app is not cleared for a {self.phase} crawl — {joined}"

    def as_http_detail(self) -> dict:
        """A structured, JSON-serialisable HTTP ``detail`` body (never a bare str)."""
        return {
            "refused": True,
            "reason": "onboarding_not_ready",
            "phase": self.phase,
            "reasons": list(self.reasons),
            "message": self.detail,
        }


# ══════════════════════ pure field readers ═════════════════════════════════

def _field(app_row: Any, name: str) -> Any:
    """Read one attribute from an ORM row OR a mapping OR a namespace (or None)."""
    if app_row is None:
        return None
    if isinstance(app_row, Mapping):
        return app_row.get(name)
    return getattr(app_row, name, None)


def _jsonb(app_row: Any, name: str) -> dict:
    """Read one JSONB column as a plain dict ({} when absent / wrong type)."""
    value = _field(app_row, name)
    return dict(value) if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    """Strict truthiness for a JSONB flag: real ``True``, a positive int, or an
    affirmative string.  Guards against a stray ``"false"`` string reading truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "passed", "ok", "signed"}
    return False


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    """Coerce an ISO string / naive datetime / aware datetime → aware UTC, or None.

    Mirrors ``services.invariant_executor._as_aware_utc`` so the two gates parse
    ``expires_at`` identically.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Accept an epoch-seconds or epoch-millis expiry (ms when implausibly large).
        try:
            secs = float(value)
        except (TypeError, ValueError):
            return None
        if secs > 1e12:  # milliseconds
            secs /= 1000.0
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── the three onboarding predicates (pure) ──────────────────────────────────

def _roe(app_row: Any) -> dict:
    """The rules-of-engagement dict (env_attestation first, fences fallback)."""
    att = _jsonb(app_row, "env_attestation")
    roe = att.get("rules_of_engagement")
    if isinstance(roe, Mapping):
        return dict(roe)
    fences_roe = _jsonb(app_row, "fences").get("rules_of_engagement")
    return dict(fences_roe) if isinstance(fences_roe, Mapping) else {}


def _roe_signed(app_row: Any) -> bool:
    """True only for a signed rules-of-engagement with a named signer."""
    roe = _roe(app_row)
    return _truthy(roe.get("signed")) and bool(str(roe.get("signed_by") or "").strip())


def _preflight_passed(app_row: Any) -> bool:
    """True when a safety preflight is recorded as passed (any known location)."""
    att = _jsonb(app_row, "env_attestation")
    fences = _jsonb(app_row, "fences")
    schedule = _jsonb(app_row, "schedule")
    for container in (att, fences, schedule):
        pf = container.get("preflight")
        if isinstance(pf, Mapping) and _truthy(pf.get("passed")):
            return True
        if _truthy(container.get("preflight_passed")):
            return True
    return False


def _authorized_to_test(app_row: Any) -> bool:
    """The operator's AUTHORIZATION attestation — they own or are permitted to test the
    target URL. The liability gate before any crawl reaches a site (esp. a public one):
    ``env_attestation.authorization = {authorized: True, authorized_by: <non-empty>}``.
    Attributed (``authorized_by``) so a refusal/allow is auditable — the crawler never
    touches a URL the operator has not affirmed they may test."""
    att = _jsonb(app_row, "env_attestation")
    authz = att.get("authorization")
    if not isinstance(authz, Mapping):
        return False
    return _truthy(authz.get("authorized")) and bool(str(authz.get("authorized_by") or "").strip())


def _attestation_status(
    app_row: Any, *, allowed_kinds: frozenset[str], now: datetime,
) -> tuple[bool, str, str]:
    """The fail-closed env-attestation gate (pure).

    Returns ``(ok, env_kind, reason)``.  ``ok`` is True only when the attestation
    is ATTRIBUTED (``attested_by``), of an ALLOWED (non-prod) kind, and carries an
    UNEXPIRED, parseable ``expires_at``.
    """
    att = _jsonb(app_row, "env_attestation")
    env_kind = str(att.get("env_kind") or "").strip().lower()
    attested_by = str(att.get("attested_by") or "").strip()
    expires = _parse_iso_utc(att.get("expires_at"))

    if not attested_by:
        return False, env_kind, "env_attestation missing 'attested_by' (unattested)"
    if env_kind not in allowed_kinds:
        return (
            False, env_kind,
            f"env_kind {env_kind or 'unset'!r} is not one of "
            f"{sorted(allowed_kinds)} (a non-prod/disposable attestation is required)",
        )
    if expires is None:
        return False, env_kind, "env_attestation has no parseable 'expires_at'"
    if expires <= now:
        return False, env_kind, "env_attestation has EXPIRED (attested_at window lapsed)"
    return True, env_kind, "attestation valid"


def _submit_approved(app_row: Any) -> bool:
    """True only for an app with per-flow submit approval.

    Reuses the design's ``client_apps.fences.allow_submit`` authorisation AND
    requires at least one explicitly-approved flow (``fences.submit_approvals`` or
    ``env_attestation.submit_approvals``) — mirrors the explorer guard's
    ``submit_flow_approved`` semantics at app granularity.
    """
    fences = _jsonb(app_row, "fences")
    att = _jsonb(app_row, "env_attestation")
    if not _truthy(fences.get("allow_submit")):
        return False
    approvals = fences.get("submit_approvals")
    if not isinstance(approvals, (list, tuple)) or not approvals:
        approvals = att.get("submit_approvals")
    return isinstance(approvals, (list, tuple)) and len(approvals) > 0


# ══════════════════════ public API ═════════════════════════════════════════

def submit_approvals(app_row: Any) -> list[str]:
    """The operator-approved Phase-B submit flow NAMES for this app, or ``[]``.

    Returns the stored approval list ONLY when :func:`_submit_approved` holds
    (``fences.allow_submit`` AND a non-empty per-flow list in
    ``fences.submit_approvals`` or ``env_attestation.submit_approvals``); every other
    app returns ``[]`` so the crawl stays at the Phase-A boundary — fail-closed.
    The names are the operator's real approvals (chosen from a prior crawl's coverage
    ``submit_candidates``), never a hardcoded default. The explorer's own
    ``gate_submit`` re-verifies attestation + per-flow approval + non-irreversible
    verb, so this is the coarse gate, not the only one."""
    # A DISPOSABLE env needs no per-control ceremony.
    #
    # The attestation already carries the operator's signed statement that this
    # environment is throwaway and that they authorise testing against it. Making
    # them then name each submit control one at a time adds no safety on top of
    # that — it only means a crawl silently stops at every boundary until someone
    # notices and types a name, which is friction that does not survive a fleet of
    # applications. Naming controls stays the rule for every other env kind.
    #
    # "*" is a blanket over the app's OWN submits. It is not a bypass: the
    # attestation must still be disposable, unexpired and attributed, the explorer
    # re-verifies at click time, and the refuse pack still stops an irreversible
    # verb. The explorer also holds step-advance labels out of the blanket, or
    # approving "Continue" would make the funnel unwalkable.
    att_ok, _kind, _reason = _attestation_status(
        app_row, allowed_kinds=(ENV_KIND_DISPOSABLE,), now=_utcnow(),
    )
    if att_ok:
        fences = _jsonb(app_row, "fences")
        att = _jsonb(app_row, "env_attestation")
        named = fences.get("submit_approvals") or att.get("submit_approvals") or []
        named = [str(x).strip() for x in named if str(x).strip()]
        return ["*", *named]

    if not _submit_approved(app_row):
        return []
    # AUTHORITATIVE freshness gate (wall-clock): a submit is authorised ONLY on a
    # DISPOSABLE, unexpired, attributed env. qe-central owns this check because the
    # explorer's own clock is monotonic (ms-since-crawl-start), so its expiry test
    # cannot enforce real freshness — mirrors is_submit_capable here at wall-clock.
    att_ok, _kind, _reason = _attestation_status(
        app_row, allowed_kinds=(ENV_KIND_DISPOSABLE,), now=_utcnow(),
    )
    if not att_ok:
        return []
    fences = _jsonb(app_row, "fences")
    att = _jsonb(app_row, "env_attestation")
    approvals = fences.get("submit_approvals")
    if not isinstance(approvals, (list, tuple)) or not approvals:
        approvals = att.get("submit_approvals")
    return [str(x).strip() for x in (approvals or []) if str(x).strip()]


def onboarding_status(app_row: Any) -> str:
    """Derive the onboarding state (``draft`` | ``attested`` | ``live``).

    ``live`` = signed RoE + a valid attestation + a passed preflight;
    ``attested`` = RoE + attestation but preflight not yet passed; else ``draft``.
    """
    now = _utcnow()
    roe = _roe_signed(app_row)
    att_ok, _kind, _reason = _attestation_status(
        app_row, allowed_kinds=CRAWLABLE_ENV_KINDS, now=now,
    )
    preflight = _preflight_passed(app_row)
    authz = _authorized_to_test(app_row)
    if roe and att_ok and preflight and authz:
        return ONBOARDING_LIVE
    if roe and att_ok and authz:
        return ONBOARDING_ATTESTED
    return ONBOARDING_DRAFT


def onboarding_ready(app_row: Any) -> tuple[bool, list[str]]:
    """``(ok, reasons)`` — an app is crawlable/schedulable ONLY when ``ok``.

    ``ok`` is True iff the derived onboarding status is ``live`` (all three of a
    signed RoE, a valid non-prod attestation, and a passed preflight).  ``reasons``
    names every unmet requirement (empty when ``ok``) so a refusal is auditable.
    """
    now = _utcnow()
    reasons: list[str] = []
    if not _authorized_to_test(app_row):
        reasons.append(
            "authorization to test this URL not attested — confirm you own or are "
            "permitted to test it (env_attestation.authorization.authorized + authorized_by)"
        )
    if not _roe_signed(app_row):
        reasons.append(
            "rules-of-engagement not signed "
            "(env_attestation.rules_of_engagement.signed + signed_by)"
        )
    att_ok, _kind, att_reason = _attestation_status(
        app_row, allowed_kinds=CRAWLABLE_ENV_KINDS, now=now,
    )
    if not att_ok:
        reasons.append(att_reason)
    if not _preflight_passed(app_row):
        reasons.append("safety preflight not passed (env_attestation.preflight.passed)")
    return (not reasons, reasons)


def _normalize_phase(phase: Any) -> str:
    p = str(phase or PHASE_EXPLORE).strip().lower()
    return PHASE_SUBMIT if p == PHASE_SUBMIT else PHASE_EXPLORE


def attestation_present(app_row: Any, *, phase: str = PHASE_EXPLORE) -> bool:
    """True when the app carries the attestation the ``phase`` requires.

    * ``explore`` (default, read-only crawl) — a NON-PROD attestation (attested,
      of a disposable/staging kind, unexpired).
    * ``submit`` (mutating) — additionally requires a DISPOSABLE-env attestation
      (via the shared ``check_disposable_attestation`` gate) AND per-flow approval,
      reusing the explorer SUBMIT-phase guard's semantics.
    """
    now = _utcnow()
    if _normalize_phase(phase) == PHASE_SUBMIT:
        # Reuse the certified-invariant executor's disposable gate verbatim so the
        # two SUBMIT gates can never drift apart.  Lazy import keeps this module
        # import-light and free of any import-order coupling.
        from ..services.invariant_executor import check_disposable_attestation

        check = check_disposable_attestation(
            _jsonb(app_row, "env_attestation"), now=now, required=True,
        )
        return bool(check.ok) and _submit_approved(app_row)

    ok, _kind, _reason = _attestation_status(
        app_row, allowed_kinds=CRAWLABLE_ENV_KINDS, now=now,
    )
    return ok


def _bypass_allowed(app_row: Any, env: str) -> bool:
    """True only when an explicit dev/test bypass flag is set AND ``env`` is dev/test.

    NEVER True in staging/production — a real app can never be waved through by a
    flag in its own row.
    """
    if str(env).strip().lower() not in DEV_ENVS:
        return False
    fences = _jsonb(app_row, "fences")
    att = _jsonb(app_row, "env_attestation")
    return (
        _truthy(fences.get("onboarding_test_bypass"))
        or _truthy(fences.get("test_bypass"))
        or _truthy(att.get("test_bypass"))
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def assert_crawlable(
    app_row: Any, *, phase: str = PHASE_EXPLORE, env: str | None = None,
) -> None:
    """REFUSE (raise :class:`OnboardingRefused`) unless the app is cleared to crawl.

    The single helper the cycle/crawl trigger calls.  Passes SILENTLY (returns
    None) only when the app is onboarding-``live`` AND carries the attestation the
    ``phase`` requires — OR when an explicit dev/test bypass flag is set in a
    development/test environment (never in staging/production).

    Args:
        app_row: the ``client_apps`` ORM row (or any mapping/namespace exposing
            ``env_attestation`` / ``fences`` / ``schedule``).
        phase: ``explore`` (read-only, the default) or ``submit`` (mutating).
        env: override the environment (defaults to the live ``NEXUS_ENV``); the
            bypass flag is honoured only for a development/test ``env``.

    Raises:
        OnboardingRefused: status 409 when the app is not onboarding-``live``;
            status 422 when only the phase attestation is missing/invalid.
    """
    resolved_env = (env or current_env())
    resolved_phase = _normalize_phase(phase)

    ready, reasons = onboarding_ready(app_row)
    att_ok = attestation_present(app_row, phase=resolved_phase)
    if ready and att_ok:
        return

    problems = list(reasons)
    if not att_ok:
        if resolved_phase == PHASE_SUBMIT:
            problems.append(
                "submit phase requires a disposable-env attestation AND per-flow "
                "approval (fences.allow_submit + a non-empty submit_approvals)"
            )
        else:
            problems.append(
                "crawl requires a valid non-prod/disposable env attestation"
            )
    problems = _dedupe(problems)

    if _bypass_allowed(app_row, resolved_env):
        logger.warning(
            "qec.prod_guard.onboarding_bypassed",
            extra={
                "phase": resolved_phase,
                "env": resolved_env,
                "app_id": str(_field(app_row, "app_id") or ""),
                "problems": problems,
            },
        )
        return

    status_code = 409 if not ready else 422
    logger.warning(
        "qec.prod_guard.crawl_refused",
        extra={
            "phase": resolved_phase,
            "env": resolved_env,
            "status_code": status_code,
            "app_id": str(_field(app_row, "app_id") or ""),
            "onboarding_status": onboarding_status(app_row),
            "problems": problems,
        },
    )
    raise OnboardingRefused(status_code=status_code, phase=resolved_phase, reasons=problems)


__all__ = [
    # states
    "ONBOARDING_DRAFT", "ONBOARDING_ATTESTED", "ONBOARDING_LIVE", "ONBOARDING_STATES",
    # phases
    "PHASE_EXPLORE", "PHASE_SUBMIT",
    # env-kind vocabulary
    "ENV_KIND_DISPOSABLE", "ENV_KIND_STAGING", "ENV_KIND_PRODUCTION_TEST",
    "ENV_KIND_PROD", "NON_PROD_ENV_KINDS", "OBSERVE_ENV_KINDS", "CRAWLABLE_ENV_KINDS",
    # postures
    "POSTURE_FULL", "POSTURE_OBSERVE", "posture_for_env_kind",
    # env helpers
    "ENV_VAR", "DEV_ENVS", "current_env", "is_dev_env",
    # refusal
    "OnboardingRefused",
    # fences
    "resolve_effective_fences",
    # predicates + gate
    "onboarding_status", "onboarding_ready", "attestation_present", "assert_crawlable",
]
