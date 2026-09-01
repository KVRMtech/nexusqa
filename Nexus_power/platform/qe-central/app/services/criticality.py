"""QE-Central S4 — deterministic criticality registry (P0..P3, fail-up).

A data-driven signal registry that bands a subject (a journey/scenario
projection) into ``P0 | P1 | P2 | P3`` with named evidence — the direct
clone of the ``_TRIAGE_RULES`` marker-table classifier
(``qe_agents.py:161-208``): a list of signal rows, a loop that collects the
markers each row matched, honest evidence, and a FAIL-UP default when no row
matches.  ZERO LLM, deterministic, $0 — matching the triage/auditor doctrine.

Design (§3.4):
  * The registry is DATA: ``pack = {"signals": [{signal_id, band, applies_to,
    matchers, rationale}, ...]}`` persisted in ``qec_criticality_registry``.
    Versions are IMMUTABLE (the verdict_events REGISTRY_VERSION stamping
    pattern) — a change mints a NEW ``registry_version`` and flips ``active``.
  * ``applies_to`` ∈ ``url_path | field_label | button_label | action_verb |
    repo_marker | invariant_link`` — a signal only reads the field-space it
    declares, so a money-path marker never fires on unrelated field text.
  * Banding = the MOST-CRITICAL band among the matched signals.  On NO match
    (or a malformed subject / empty pack) the result FAILS UP to ``P1`` —
    never silently ``P2``/``P3``.  A ``P2``/``P3`` result is only ever emitted
    when a signal EXPLICITLY declares it (a deliberate down-band in a custom
    pack), never by omission.

The seed pack v1 is GENERIC (money / auth / PII / destructive /
multi-page-submit / certified-invariant-link); domain vocabulary
(:data:`DOMAIN_BOOST_PACKS`) is OPTIONAL boost rows a tenant may merge in —
the product works on ANY UI with domain terms as a boost only.

Matching is precision-aware but conservative (fail-up-safe): a marker that
carries a separator (``/transfer``, ``social security``) is a substring test
over the raw field-space; a bare single-token marker (``transfer``) is a
whole-WORD test over the tokenised space, so ``payment`` never matches
``repayment`` yet ``/payment`` still matches ``/payment-history``.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session
from ..db.gov_models import CriticalityRegistryRow

logger = logging.getLogger(__name__)

# ── Bands (P0 most critical) ──────────────────────────────────────────────
BAND_P0 = "P0"
BAND_P1 = "P1"
BAND_P2 = "P2"
BAND_P3 = "P3"
BANDS: tuple[str, ...] = (BAND_P0, BAND_P1, BAND_P2, BAND_P3)
_BAND_ORDER = {BAND_P0: 0, BAND_P1: 1, BAND_P2: 2, BAND_P3: 3}

#: Direction of the honest default — never below P1, never silently P2/P3.
FAIL_UP_BAND = BAND_P1

#: The field-spaces a signal may declare (matches the DB ``applies_to`` domain).
APPLIES_TO = frozenset(
    {"url_path", "field_label", "button_label", "action_verb",
     "repo_marker", "invariant_link"}
)

CLASSIFIER = "criticality-v1-deterministic"

#: Informational version stamped on the built-in seed pack (the per-tenant
#: DB row mints a globally-unique version — see :func:`new_registry_version`).
SEED_REGISTRY_VERSION = "crit-v1"

#: A synthetic ``action_verb`` token :func:`subject_from_journey` emits only
#: when a journey spans ≥2 pages AND commits a submit — lets the pack express
#: "multi-page submission" within the fixed ``applies_to`` vocabulary.
MULTI_PAGE_SUBMIT_TOKEN = "multi_page_submit"


# ── The generic seed pack v1 (data, not code) ─────────────────────────────
SEED_SIGNALS: tuple[dict, ...] = (
    # ── P0: catastrophic if broken ──────────────────────────────────────
    {
        "signal_id": "money_route",
        "band": BAND_P0,
        "applies_to": "url_path",
        "matchers": [
            "/transfer", "/payment", "/pay-", "/checkout", "/billing", "/wire",
            "/withdraw", "/deposit", "/purchase", "/order", "/invoice",
            "/refund", "/payout", "/remit", "/send-money", "/funds",
        ],
        "rationale": "money-movement route — a defect can move real funds",
    },
    {
        "signal_id": "money_control",
        "band": BAND_P0,
        "applies_to": "button_label",
        "matchers": [
            "transfer", "withdraw", "deposit", "checkout", "payment", "payout",
            "remit", "wire", "purchase", "place order", "send money",
            "pay now", "authorize payment", "confirm payment",
        ],
        "rationale": "money-movement control on the page",
    },
    {
        "signal_id": "pii_field",
        "band": BAND_P0,
        "applies_to": "field_label",
        "matchers": [
            "ssn", "social security", "date of birth", "dob", "card number",
            "credit card", "cvv", "cvc", "account number", "routing number",
            "passport", "driver license", "driver's license", "tax id",
            "national id", "bank account",
        ],
        "rationale": "sensitive-PII field — regulated data handling",
    },
    {
        "signal_id": "destructive_control",
        "band": BAND_P0,
        "applies_to": "button_label",
        "matchers": [
            "delete", "remove", "deactivate", "close account", "cancel account",
            "terminate", "permanently delete", "purge", "wipe", "revoke access",
            "disable account",
        ],
        "rationale": "irreversible / destructive control",
    },
    {
        "signal_id": "destructive_route",
        "band": BAND_P0,
        "applies_to": "url_path",
        "matchers": [
            "/delete", "/close-account", "/deactivate", "/terminate",
            "/cancel-account", "/remove", "/purge",
        ],
        "rationale": "irreversible / destructive route",
    },
    {
        "signal_id": "auth_route",
        "band": BAND_P0,
        "applies_to": "url_path",
        "matchers": [
            "/login", "/signin", "/sign-in", "/logout", "/signout", "/sign-out",
            "/password", "/reset-password", "/forgot-password", "/mfa", "/2fa",
            "/otp", "/session", "/oauth", "/sso", "/verify",
        ],
        "rationale": "authentication / session boundary — security-critical",
    },
    {
        "signal_id": "auth_field",
        "band": BAND_P0,
        "applies_to": "field_label",
        "matchers": [
            "password", "current password", "new password", "confirm password",
            "one-time code", "one time passcode", "otp", "verification code",
            "security code", "2fa code",
        ],
        "rationale": "credential / second-factor field",
    },
    {
        "signal_id": "certified_invariant_link",
        "band": BAND_P0,
        "applies_to": "invariant_link",
        "matchers": [],  # presence-based: any linked invariant fires this row
        "rationale": "scenario executes a human-certified P0 invariant",
    },
    # ── P1: important, not inherently catastrophic ──────────────────────
    {
        "signal_id": "multi_page_submit",
        "band": BAND_P1,
        "applies_to": "action_verb",
        "matchers": [MULTI_PAGE_SUBMIT_TOKEN],
        "rationale": "multi-page form submission — a committed cross-page "
                     "state change",
    },
)

#: OPTIONAL domain boosts (product is generic; domain vocab is a boost only).
#: The US financial / life-insurance beachhead is provided as a worked example;
#: a tenant opts in via ``seed_pack(domain='life_financial_insurance')``.
DOMAIN_BOOST_PACKS: dict[str, tuple[dict, ...]] = {
    "life_financial_insurance": (
        {
            "signal_id": "insurance_route",
            "band": BAND_P0,
            "applies_to": "url_path",
            "matchers": [
                "/underwriting", "/quote", "/policy", "/claim", "/beneficiary",
                "/premium", "/annuity", "/enrollment", "/coverage",
            ],
            "rationale": "life/financial-insurance critical route (domain boost)",
        },
        {
            "signal_id": "insurance_field",
            "band": BAND_P0,
            "applies_to": "field_label",
            "matchers": [
                "policy number", "beneficiary", "premium", "coverage amount",
                "death benefit", "annuity", "sum assured",
            ],
            "rationale": "life/financial-insurance sensitive field (domain boost)",
        },
    ),
}


# ── Pack helpers ──────────────────────────────────────────────────────────

def seed_pack(domain: str | None = None) -> dict:
    """Return the built-in seed pack (generic + optional domain boosts).

    Shape matches the ``qec_criticality_registry.pack`` column:
    ``{"registry_version": str, "signals": [ ... ]}``.
    """
    signals: list[dict] = [dict(s) for s in SEED_SIGNALS]
    version = SEED_REGISTRY_VERSION
    if domain:
        boosts = DOMAIN_BOOST_PACKS.get(domain)
        if boosts:
            signals.extend(dict(s) for s in boosts)
            version = f"{SEED_REGISTRY_VERSION}+{domain}"
    return {"registry_version": version, "signals": signals}


def new_registry_version(base: str = SEED_REGISTRY_VERSION) -> str:
    """Mint a globally-unique registry version (the table PK is the version
    alone), so two tenants provisioning the same seed never collide."""
    return f"{base}-{uuid.uuid4().hex[:12]}"


def build_seed_registry(
    *, tenant_id: str, created_by: str, domain: str | None = None,
) -> dict:
    """Build the row payload for a first, ACTIVE seed registry (the router
    inserts it).  ``registry_version`` is unique per :func:`new_registry_version`.
    """
    pack = seed_pack(domain)
    return {
        "registry_version": new_registry_version(pack["registry_version"]),
        "tenant_id": tenant_id,
        "pack": pack,
        "active": True,
        "created_by": (created_by or "")[:200],
    }


def _resolve_pack(
    pack: Any, registry_version: str | None,
) -> tuple[list[dict], str]:
    """Normalise the ``pack`` argument to ``(signals, registry_version)``.

    Accepts ``None`` (built-in seed pack), a ``{"signals": [...]}`` mapping
    (the DB shape), or a bare sequence of signal dicts.
    """
    if pack is None:
        sp = seed_pack()
        return list(sp["signals"]), registry_version or sp["registry_version"]
    if isinstance(pack, Mapping):
        signals = list(pack.get("signals") or [])
        version = (
            registry_version or str(pack.get("registry_version") or "")
            or SEED_REGISTRY_VERSION
        )
        return signals, version
    if isinstance(pack, Sequence):
        return list(pack), registry_version or SEED_REGISTRY_VERSION
    # Unknown pack type → seed pack (never crash, never green-wash).
    sp = seed_pack()
    return list(sp["signals"]), registry_version or sp["registry_version"]


# ── Matching (precision-aware, fail-up-safe) ──────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SEPARATOR_RE = re.compile(r"[^a-z0-9]")


def _space_and_tokens(subject: Mapping[str, Any], applies_to: str) -> tuple[str, set[str]]:
    """Casefolded field-space string + its alphanumeric token set."""
    raw = subject.get(applies_to, "")
    if isinstance(raw, (list, tuple, set)):
        space = " ".join(str(x) for x in raw)
    else:
        space = str(raw or "")
    space = space.casefold()
    return space, set(_TOKEN_RE.findall(space))


def _marker_hit(marker: str, space: str, tokens: set[str]) -> bool:
    """A marker with a separator (``/pay``, ``two words``) is a substring test;
    a bare single token (``transfer``) is a whole-word test."""
    m = (marker or "").strip().casefold()
    if not m:
        return False
    if _SEPARATOR_RE.search(m):
        return m in space
    return m in tokens


def _norm_band(band: Any) -> str:
    """Normalise a signal's declared band; an invalid band FAILS UP to P1
    (never silently P2/P3)."""
    b = str(band or "").strip().upper()
    return b if b in _BAND_ORDER else FAIL_UP_BAND


def signal_matches(signal: Mapping[str, Any], subject: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """(did-match, matched-markers) for one signal against ``subject``.

    ``invariant_link`` is PRESENCE-based: the row fires iff the subject's
    invariant-link space is non-empty (a link IS the signal), regardless of
    the (empty) matcher list.
    """
    applies_to = str(signal.get("applies_to") or "")
    space, tokens = _space_and_tokens(subject, applies_to)
    if applies_to == "invariant_link":
        present = bool(space.strip())
        return present, (["linked-invariant"] if present else [])
    matchers = signal.get("matchers") or []
    matched = [m for m in matchers if _marker_hit(m, space, tokens)]
    return bool(matched), matched


# ── Subject projection ────────────────────────────────────────────────────

def subject_from_journey(
    journey: Mapping[str, Any],
    *,
    invariant_links: Sequence[str] = (),
    repo_markers: Sequence[str] = (),
) -> dict:
    """Project a value-free/locator-free journey into a criticality subject.

    ``journey`` is ``{"kind": str, "pages": [{"host", "path", "verbs",
    "fields", "targets"}, ...]}`` (the shape :mod:`app.services.synthesis`
    emits).  The projection is deterministic and carries NO values/locators —
    only paths, accessible field/target labels, verbs, plus the structural
    ``multi_page_submit`` token when warranted.
    """
    pages = list(journey.get("pages") or [])
    paths, fields, targets, verbs = [], [], [], []
    for p in pages:
        if not isinstance(p, Mapping):
            continue
        if p.get("path"):
            paths.append(str(p["path"]))
        fields.extend(str(f) for f in (p.get("fields") or []))
        targets.extend(str(t) for t in (p.get("targets") or []))
        verbs.extend(str(v) for v in (p.get("verbs") or []))

    has_submit = any(str(v).strip().lower() == "submit" for v in verbs)
    if has_submit and len(pages) >= 2:
        verbs.append(MULTI_PAGE_SUBMIT_TOKEN)

    return {
        "url_path": " ".join(paths),
        "field_label": " ".join(fields),
        "button_label": " ".join(targets),
        "action_verb": " ".join(verbs),
        "repo_marker": " ".join(str(m) for m in repo_markers),
        "invariant_link": " ".join(str(x) for x in invariant_links),
    }


# ── Evaluate (the public contract) ────────────────────────────────────────

def _fail_up(registry_version: str, reason: str = "no criticality signal matched") -> dict:
    """The honest FAIL-UP result — banded P1 with named evidence."""
    return {
        "band": FAIL_UP_BAND,
        "evidence": [{
            "signal_id": "fail_up_default",
            "band": FAIL_UP_BAND,
            "applies_to": "",
            "matched_markers": [],
            "rationale": (
                f"{reason} — FAIL-UP to {FAIL_UP_BAND} "
                f"(never silently P2/P3)"
            ),
            "evidence": reason,
        }],
        "registry_version": registry_version,
        "classifier": CLASSIFIER,
    }


def evaluate(
    subject: Mapping[str, Any],
    *,
    pack: Any = None,
    registry_version: str | None = None,
) -> dict:
    """Band a subject deterministically.

    Args:
        subject: a mapping of field-space → text/list (see
            :func:`subject_from_journey`).  Keys are the :data:`APPLIES_TO`
            vocabulary; missing keys are treated as empty.
        pack: ``None`` (built-in seed pack), a ``{"signals": [...]}`` mapping,
            or a bare sequence of signal dicts.
        registry_version: overrides the version stamped on the result.

    Returns:
        ``{"band": "P0|P1|P2|P3", "evidence": [ hit, ... ], "registry_version":
        str, "classifier": str}``.  ``evidence`` mirrors the ``qe_agents`` hit
        shape and is ordered most-critical-first.  On no match / malformed
        subject / empty pack the band FAILS UP to ``P1``.
    """
    signals, version = _resolve_pack(pack, registry_version)

    if not isinstance(subject, Mapping):
        return _fail_up(version, reason="subject is not a mapping")
    if not signals:
        return _fail_up(version, reason="empty criticality pack")

    hits: list[dict] = []
    for sig in signals:
        if not isinstance(sig, Mapping):
            continue
        try:
            ok, matched = signal_matches(sig, subject)
        except Exception:  # a malformed signal never breaks evaluation
            logger.warning(
                "qec.criticality.signal_eval_failed",
                extra={"signal_id": str(sig.get("signal_id", ""))[:64]},
            )
            continue
        if ok:
            hits.append({
                "signal_id": str(sig.get("signal_id", "")),
                "band": _norm_band(sig.get("band")),
                "applies_to": str(sig.get("applies_to", "")),
                "matched_markers": matched[:8],
                "rationale": str(sig.get("rationale", "")),
                "evidence": f"matched {sig.get('applies_to', '')}: {matched[:3]}",
            })

    if not hits:
        return _fail_up(version)

    hits.sort(key=lambda h: _BAND_ORDER.get(h["band"], _BAND_ORDER[FAIL_UP_BAND]))
    band = hits[0]["band"]
    return {
        "band": band,
        "evidence": hits,
        "registry_version": version,
        "classifier": CLASSIFIER,
    }


# ── DB helper (behind the tenant-scoped qecentral session) ────────────────

async def load_active_pack(tenant_id: str) -> tuple[list[dict], str]:
    """Return ``(signals, registry_version)`` for the tenant's ACTIVE registry
    row, falling back to the built-in seed pack when none is provisioned.

    Never raises for a missing registry — an unprovisioned tenant is banded by
    the generic seed pack (a deterministic, honest default), and the router can
    materialise the seed row lazily via :func:`build_seed_registry`.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(CriticalityRegistryRow)
                .where(
                    CriticalityRegistryRow.tenant_id == tenant_id,
                    CriticalityRegistryRow.active.is_(True),
                )
                .order_by(CriticalityRegistryRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        sp = seed_pack()
        return list(sp["signals"]), sp["registry_version"]
    pack = row.pack or {}
    signals = list(pack.get("signals") or [])
    if not signals:
        # An active-but-empty pack has no honest meaning → seed fallback.
        sp = seed_pack()
        return list(sp["signals"]), sp["registry_version"]
    return signals, row.registry_version


__all__ = [
    "BANDS",
    "BAND_P0", "BAND_P1", "BAND_P2", "BAND_P3",
    "FAIL_UP_BAND",
    "APPLIES_TO",
    "CLASSIFIER",
    "SEED_REGISTRY_VERSION",
    "SEED_SIGNALS",
    "DOMAIN_BOOST_PACKS",
    "seed_pack",
    "new_registry_version",
    "build_seed_registry",
    "signal_matches",
    "subject_from_journey",
    "evaluate",
    "load_active_pack",
]
