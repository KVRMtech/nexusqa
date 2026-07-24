"""Attribution Engine v1 (P1.4) — automatic "whose fault was this failure?".

Doctrine (founder-locked, 2026-07-24):
  1. NEVER classify a product limitation as an application defect.
  2. Blame requires POSITIVE evidence; "not yet attributed" beats a guess —
     in BOTH directions (we neither blame the client's app for our script,
     nor excuse our script by hand-waving at the app).

The engine is a DETERMINISTIC ladder over the evidence that already exists at
ingest time: the Playwright error text, the step's stored definition (the
factory case JSON), and whether the run was a CERTIFICATION run against the
attested baseline.  $0, no LLM, no network — safe on every ingested failure.
Every verdict carries: category, tier (confirmed|candidate), blame,
evidence (verbatim quotes from the error), and a human detail sentence.

Categories (the taxonomy the founder specified):
  * product_script_defect — the generated script/oracle is wrong by construction
  * application_defect    — the application demonstrably misbehaved
  * environment           — the target was unreachable / TLS / DNS / conn-reset
  * configuration         — auth/session/base-url routing prevented the test
  * test_data             — required data was missing/rejected
  * unknown               — no provable cause; says so honestly (routed to
                            diagnosis, NEVER silently blamed on the app)

Escaped-defect law (P1.6): every failure class that ever reaches a client
must gain (a) a rung here or a compiler/auditor guard, and (b) a regression
test.  The registry lives in ESCAPED_DEFECT_REGISTRY below and is enforced by
tests/test_escape_guard_registry.py.

Generic across apps: every rung keys on STRUCTURE (error shapes, URL shape,
oracle provenance, grounded-navigation flags) — never a host, never domain
vocabulary.
"""
from __future__ import annotations

import re

from .failure_attribution import (
    ATTR_SCRIPT_DEFECT,
    ATTR_SCRIPT_DEFECT_CANDIDATE,
    classify_step_failure,
)

# ── Category vocabulary ──────────────────────────────────────────────────────
CATEGORY_PRODUCT = "product_script_defect"
CATEGORY_APPLICATION = "application_defect"
CATEGORY_ENVIRONMENT = "environment"
CATEGORY_CONFIG = "configuration"
CATEGORY_DATA = "test_data"
CATEGORY_UNKNOWN = "unknown"

TIER_CONFIRMED = "confirmed"
TIER_CANDIDATE = "candidate"

# ── Escaped-defect registry (P1.6) — class → the guard that now pins it ──────
# Every entry names the regression-test module that proves the guard works.
# tests/test_escape_guard_registry.py asserts each referenced module exists,
# so a rung can never be added here without its test (and vice versa the
# review checklist requires every NEW escape to land as an entry).
ESCAPED_DEFECT_REGISTRY: dict[str, dict] = {
    "url_as_text_oracle": {
        "escaped": "2026-07-24 run 7c89de7e (client-visible)",
        "guards": [
            "compiler._strip_urls (generation)",
            "playwright_auditor.V_URL_TEXT (pre-run block)",
            "attribution_engine rung 1 (post-failure)",
        ],
        "tests": [
            "test_compiler_url_text_oracle.py",
            "test_auditor_url_text_oracle.py",
            "test_failure_attribution.py",
        ],
    },
    "best_effort_text_oracle": {
        "escaped": "same incident class — prose-derived oracle failing a step "
                   "whose grounded oracles passed",
        "guards": [
            "compiler proven-oracle policy (soft, recorded)",
            "attribution_engine rung 6 (post-failure)",
        ],
        "tests": ["test_attribution_engine.py", "test_soft_oracle_recording.py"],
    },
}

# ── Error-shape recognisers (deterministic, quoted as evidence) ──────────────

# Environment: the target was never reachable — nothing app-side executed.
_ENV_RX = re.compile(
    r"(net::ERR_NAME_NOT_RESOLVED|net::ERR_CONNECTION_REFUSED|"
    r"net::ERR_CONNECTION_TIMED_OUT|net::ERR_CONNECTION_RESET|"
    r"net::ERR_CERT_[A-Z_]+|net::ERR_ADDRESS_UNREACHABLE|"
    r"net::ERR_INTERNET_DISCONNECTED|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|"
    r"getaddrinfo|ERR_SSL_PROTOCOL_ERROR)",
    re.IGNORECASE)

# Application: a server-side failure the app itself reported.
_HTTP_5XX_RX = re.compile(
    r"\b(50[0-4])\b[^\n]{0,60}|Internal Server Error|Bad Gateway|"
    r"Service Unavailable|Gateway Timeout",
    re.IGNORECASE)

# Product: a strict-mode violation is OUR locator matching N elements.
_STRICT_MODE_RX = re.compile(
    r"strict mode violation[^\n]*resolved to \d+ elements", re.IGNORECASE)

# Configuration: the run was bounced to an auth wall.
_AUTH_WALL_RX = re.compile(
    r"Received string:\s*\"[^\"]*/(login|signin|sign-in|sso|auth)[^\"]*\"",
    re.IGNORECASE)

# Oracle shapes parsed out of a Playwright expect() failure.
_TOHAVEURL_RX = re.compile(r"toHaveURL", re.IGNORECASE)
_TOHAVEVALUE_RX = re.compile(r"toHaveValue", re.IGNORECASE)
_GETBYTEXT_ORACLE_RX = re.compile(
    r"Locator:\s*getByText\(", re.IGNORECASE)

# Action (non-expect) locator timeout — the heal pipeline's jurisdiction.
_ACTION_TIMEOUT_RX = re.compile(
    r"locator\.(click|fill|selectOption|check|uncheck|press|setInputFiles)"
    r"[^\n]*Timeout", re.IGNORECASE)

_URL_SHAPED_RX = re.compile(r"^\s*(?:https?://|www\.)", re.IGNORECASE)


def _quote(err: str, rx: re.Pattern) -> str:
    """Verbatim evidence quote — the matched fragment, bounded."""
    m = rx.search(err)
    return (m.group(0)[:200] if m else "")


def _verdict(category: str, tier: str, blame: str, cause: str,
             detail: str, evidence: list[str]) -> dict:
    return {
        # Back-compat keys (F4 consumers: portal + summaries read these).
        "attribution": (
            ATTR_SCRIPT_DEFECT if category == CATEGORY_PRODUCT and tier == TIER_CONFIRMED
            else ATTR_SCRIPT_DEFECT_CANDIDATE if category == CATEGORY_PRODUCT
            else f"{category}_{tier}"),
        "blame": (
            "product" if category == CATEGORY_PRODUCT and tier == TIER_CONFIRMED
            else "product_probable" if category == CATEGORY_PRODUCT
            else category),
        # Engine v1 vocabulary.
        "category": category,
        "tier": tier,
        "cause": cause,
        "detail": detail,
        "evidence": [e for e in evidence if e],
        "engine": "attribution-engine/v1",
    }


def attribute_failure(
    error_message: str | None,
    *,
    step_def: dict | None = None,
    is_certification: bool = False,
) -> dict | None:
    """Attribute one failed step.  Returns the verdict dict, or ``None`` when
    nothing can be PROVEN (honest silence — the caller shows a neutral
    "cause under analysis", never implicit app-blame).

    ``step_def`` is the stored factory-case step JSON (action/expected/observed)
    when resolvable at ingest; rungs that need it degrade gracefully without it.
    ``is_certification`` marks a baseline certification run: the baseline is
    attested-good, so an unattributable certification failure still yields an
    honest UNKNOWN verdict (quarantine is decided by the caller on ANY
    certification failure — blame and quarantine are separate decisions).
    """
    err = str(error_message or "")
    if not err.strip():
        return None
    observed = dict((step_def or {}).get("observed") or {})

    # ── Rung 1 — F4 classes: URL-as-text oracle (product by construction) ────
    f4 = classify_step_failure(err)
    if f4 is not None:
        tier = TIER_CONFIRMED if f4["attribution"] == ATTR_SCRIPT_DEFECT else TIER_CANDIDATE
        v = _verdict(CATEGORY_PRODUCT, tier, f4["blame"], f4["cause"], f4["detail"],
                     [_quote(err, re.compile(r"getByText\([^\n)]{0,120}\)?", re.I))])
        return v

    # ── Rung 2 — environment: the target was never reachable ────────────────
    if _ENV_RX.search(err):
        return _verdict(
            CATEGORY_ENVIRONMENT, TIER_CONFIRMED, CATEGORY_ENVIRONMENT,
            "target_unreachable",
            "The target environment could not be reached (network/DNS/TLS). "
            "Neither the application nor the test script executed — fix the "
            "environment and re-run.",
            [_quote(err, _ENV_RX)])

    # ── Rung 3 — product: strict-mode violation is OUR ambiguous locator ─────
    if _STRICT_MODE_RX.search(err):
        return _verdict(
            CATEGORY_PRODUCT, TIER_CONFIRMED, "product",
            "ambiguous_locator",
            "The generated locator matched multiple elements (strict-mode "
            "violation) — a script defect in locator scoping, NOT an "
            "application failure. The auditor's ambiguity dimension flags "
            "these; scope the locator to its row/card/section.",
            [_quote(err, _STRICT_MODE_RX)])

    # ── Rung 4 — application: the app reported a server-side failure ─────────
    if _HTTP_5XX_RX.search(err):
        return _verdict(
            CATEGORY_APPLICATION, TIER_CANDIDATE, CATEGORY_APPLICATION,
            "server_error_observed",
            "A server-side error (5xx) surfaced during this step — evidence "
            "points at the application. Confirm via the network capture; the "
            "auto defect report carries the reproduction.",
            [_quote(err, _HTTP_5XX_RX)])

    # ── Rung 5 — configuration: bounced to an auth wall ──────────────────────
    if _AUTH_WALL_RX.search(err):
        return _verdict(
            CATEGORY_CONFIG, TIER_CANDIDATE, CATEGORY_CONFIG,
            "auth_wall",
            "The run landed on a login/auth page instead of the recorded one — "
            "the test session/credentials were missing or expired "
            "(configuration), not an application failure. Re-capture auth or "
            "bind an Environment Profile with credentials.",
            [_quote(err, _AUTH_WALL_RX)])

    is_expect = "expect(" in err

    # ── Rung 6 — best-effort text oracle failing AFTER grounded oracles ─────
    # The generalised run-7c89de7e class: the failing assertion is a
    # prose-derived getByText token (non-grounded best-effort per
    # provenance.py), on a step whose PROVEN oracles (recorded navigation)
    # precede it in the compiled order — so the action worked, the recorded
    # navigation was verified, and only the low-confidence hint failed.
    if is_expect and _GETBYTEXT_ORACLE_RX.search(err):
        nav_grounded = bool(observed.get("navigation_grounded")) and bool(
            observed.get("next_url"))
        after_url_shaped = bool(_URL_SHAPED_RX.match(str(observed.get("after") or "")))
        if nav_grounded or after_url_shaped:
            return _verdict(
                CATEGORY_PRODUCT, TIER_CANDIDATE, "product_probable",
                "best_effort_text_oracle",
                "The failing assertion is a prose-derived text oracle "
                "(non-grounded best-effort). The step's grounded oracles "
                "(recorded action + navigation) precede it and passed — the "
                "application did what the recording expected; the "
                "low-confidence text hint is the product's. Under the "
                "proven-oracle policy this oracle is non-fatal and recorded "
                "as a warning instead.",
                [_quote(err, _GETBYTEXT_ORACLE_RX)])

    # ── Rung 7 — PROVEN oracle failed: this is the product's core claim ──────
    # A recorded-evidence oracle failing IS the signal the client pays for.
    if is_expect and _TOHAVEURL_RX.search(err) and bool(observed.get("navigation_grounded")):
        return _verdict(
            CATEGORY_APPLICATION, TIER_CANDIDATE, CATEGORY_APPLICATION,
            "grounded_navigation_broken",
            "A navigation the recording PROVED this action causes did not "
            "happen. This is a grounded oracle — evidence points at an "
            "application behavior change (or an intended change needing "
            "re-baseline). The step evidence carries the recorded "
            "destination.",
            [_quote(err, re.compile(r"Expected (pattern|string):[^\n]{0,160}", re.I))])
    if is_expect and _TOHAVEVALUE_RX.search(err):
        prov = str((step_def or {}).get("provenance") or "").lower()
        if prov == "demonstrated":
            return _verdict(
                CATEGORY_APPLICATION, TIER_CANDIDATE, CATEGORY_APPLICATION,
                "demonstrated_value_lost",
                "A field value the recording demonstrated did not hold — a "
                "grounded value oracle failed. Evidence points at an "
                "application change (masking/validation/reset).",
                [_quote(err, re.compile(r"Expected string:[^\n]{0,160}", re.I))])

    # ── Rung 8 — action locator timeout: heal's jurisdiction, honestly open ──
    if _ACTION_TIMEOUT_RX.search(err):
        return _verdict(
            CATEGORY_UNKNOWN, TIER_CANDIDATE, CATEGORY_UNKNOWN,
            "action_locator_timeout",
            "The action's target control was not found in time. Two honest "
            "hypotheses: the application's UI changed (real signal) or the "
            "generated locator drifted (product). Routed to the heal/diagnosis "
            "pipeline — the proven-heal verdict settles the blame; nothing is "
            "claimed until it does.",
            [_quote(err, _ACTION_TIMEOUT_RX)])

    # ── Rung 9 — certification context: still honest, never a guess ─────────
    if is_certification:
        return _verdict(
            CATEGORY_UNKNOWN, TIER_CANDIDATE, CATEGORY_UNKNOWN,
            "failed_on_attested_baseline",
            "This case failed its certification run against the attested "
            "baseline and is quarantined from client verdicts until it "
            "passes. The cause is not yet attributed — it is under "
            "diagnosis; the client's application is NOT painted red for it.",
            [err.splitlines()[0][:200] if err.splitlines() else ""])

    # Nothing provable — honest silence. The caller's UI must render this as
    # "failed — cause under analysis", never as implicit application blame.
    return None
