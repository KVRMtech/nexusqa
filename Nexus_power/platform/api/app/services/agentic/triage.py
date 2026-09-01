"""Triage — the Source Adjudicator (PRODUCT vs SCRIPT vs ENVIRONMENT) + the fix/build/flag
Verdict. Generic, deterministic, $0.

It maps a diagnosis (its ``cause`` + an optional network signal + the raw error) to ONE
source and ONE action. It only LABELS and ROUTES — it never heals and never files: the
orthogonal oracle + heal_policy still gate any actual heal, and a defect is only filed on
a corroborated real regression. "Verdict" is the ``route`` field (fix / build / flag).
"""
from __future__ import annotations

import re

# Sources
PRODUCT = "product"
SCRIPT = "script"
ENVIRONMENT = "environment"
UNKNOWN = "unknown"

# Routes (the fix-it / build-it / flag-it verdict)
FIX = "fix"        # SCRIPT  -> heal the script / correct the data (oracle-gated)
BUILD = "build"    # PRODUCT -> file a replayable defect (corroborated real regression)
FLAG = "flag"      # ENVIRONMENT -> flag + quarantine; never a bug, never a heal
REVIEW = "review"  # UNKNOWN -> a human looks

# Deterministic cause -> (source, route). Mirrors the self_heal taxonomy + the new
# cross-field cause. Conservative by construction (fail toward review/refuse).
_CAUSE_MAP = {
    "REAL_REGRESSION":           (PRODUCT, BUILD),
    "WRONG_CONTROL_KIND":        (SCRIPT, FIX),
    "SELECTOR_DRIFT":            (SCRIPT, FIX),
    "LOCATOR_NOT_FOUND":         (SCRIPT, FIX),
    "SELECTOR_REANCHOR":         (SCRIPT, FIX),
    "DATA_VALIDITY_CROSS_FIELD": (SCRIPT, FIX),   # bad recorded data on an adaptive form
    "AUTH_PRECONDITION":         (ENVIRONMENT, FLAG),
    "DATA_PRECONDITION_UNMET":   (ENVIRONMENT, FLAG),
    "VARIANT_SUSPECTED":         (ENVIRONMENT, FLAG),
    "FLAKE":                     (ENVIRONMENT, FLAG),
    "NEEDS_REVIEW":              (UNKNOWN, REVIEW),
}

# Transport / infrastructure errors with NO application 5xx -> ENVIRONMENT. A DNS /
# connection / browser-launch failure is never the script's fault and never a product bug.
_NET_INFRA_RE = re.compile(
    r"net::ERR_|ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ERR_NAME_NOT_RESOLVED|"
    r"ERR_CONNECTION|ERR_TIMED_OUT|ERR_SSL|ERR_CERT|ERR_NETWORK|socket hang up|"
    r"browserType\.launch|Target page, context or browser has been closed|"
    r"Target closed|browser has crashed|page crashed",
    re.IGNORECASE,
)

_SOURCE_LABEL = {
    PRODUCT: "Our product (app bug)",
    SCRIPT: "The test (script / data)",
    ENVIRONMENT: "The environment (infra)",
    UNKNOWN: "Needs a human",
}
_ROUTE_LABEL = {
    FIX: "Fix it (heal the script / correct the data)",
    BUILD: "Build it (file a product defect)",
    FLAG: "Flag it (environment — quarantine, not a code defect)",
    REVIEW: "Review (human)",
}


def triage(diag: dict, *, error_message: str = "", network: dict | None = None) -> dict:
    """Return ``{source, route, source_label, route_label, confidence, evidence,
    oracle_gated}`` for a diagnosis.

    Conservative + never-green-wash: PRODUCT only on a contradicted outcome (the diag's
    own REAL_REGRESSION) or a corroborating app-origin 5xx; ENVIRONMENT on
    transport/infra errors (overriding a SCRIPT mislabel — infra is never the script's
    fault); SCRIPT on a heal-able binding/data cause; else REVIEW. The diag's deterministic
    cause leads; signals corroborate."""
    diag = diag or {}
    cause = (diag.get("cause") or "NEEDS_REVIEW").strip().upper()
    err = error_message or diag.get("error_message") or ""
    evidence = list(diag.get("evidence") or [])

    net = network or {}
    # app-origin 5xx in the failing window = near-dispositive PRODUCT (corroborates).
    app_5xx = bool(net.get("is_real_bug_signal")) or bool(net.get("server_error"))
    # transport/infra WITHOUT an app 5xx = ENVIRONMENT (overrides a SCRIPT/REVIEW label).
    infra = bool(_NET_INFRA_RE.search(err)) and not app_5xx

    source, route = _CAUSE_MAP.get(cause, (UNKNOWN, REVIEW))
    if app_5xx and source != PRODUCT:
        source, route = PRODUCT, BUILD
        evidence.append("An application 5xx was observed in the failing step's network window "
                        "— corroborates a real product defect.")
    elif infra:
        source, route = ENVIRONMENT, FLAG
        evidence.append("A transport / infrastructure error with no application 5xx — not a "
                        "code or product defect; flag the environment.")

    return {
        "source": source,
        "route": route,                 # the fix / build / flag VERDICT
        "source_label": _SOURCE_LABEL[source],
        "route_label": _ROUTE_LABEL[route],
        "confidence": float(diag.get("confidence") or 0.0),
        "evidence": evidence,
        # Advisory only: a heal still passes the orthogonal oracle + heal_policy tiers,
        # and a defect is only filed on a corroborated real regression.
        "oracle_gated": True,
    }
