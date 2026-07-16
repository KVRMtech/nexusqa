"""Deterministic funnel classifier (Phase 4 — the flywheel join key).

Identifies WHAT KIND of flow a journey is — login / checkout / transfer / quote /
apply / account / search — so templates and priors have a stable, GROUNDED key to join
on across apps. Pure, $0, deterministic, evidence-carrying: it reuses the same
route/keyword marker style as the criticality registry, scores the observed tokens, and
returns ``unknown`` (never a forced bucket) when no type clears the threshold.

It is a JOIN KEY only — it never overrides a criticality band, and a template joined on
a wrong funnel still faces the per-control grounded oracle, so a misroute costs at most a
needless heal-from-scratch, never a wrong-green.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

LOGIN = "login"
CHECKOUT = "checkout"
TRANSFER = "transfer"
QUOTE = "quote"
APPLY = "apply"
ACCOUNT = "account"
SEARCH = "search"
UNKNOWN = "unknown"

FUNNEL_TYPES = (LOGIN, CHECKOUT, TRANSFER, QUOTE, APPLY, ACCOUNT, SEARCH)

# Priority breaks ties deterministically (money/identity flows win over generic ones).
_PRIORITY = {TRANSFER: 0, CHECKOUT: 1, LOGIN: 2, QUOTE: 3, APPLY: 4, ACCOUNT: 5, SEARCH: 6}

# route markers (matched as path substrings) + keyword markers (matched over labels/text).
_MARKERS: dict[str, dict[str, tuple[str, ...]]] = {
    LOGIN: {
        "routes": ("/login", "/signin", "/sign-in", "/log-in", "/auth", "/logout", "/signout"),
        "keywords": ("log in", "login", "sign in", "sign-in", "username", "password"),
    },
    CHECKOUT: {
        "routes": ("/checkout", "/cart", "/basket", "/order", "/billing"),
        "keywords": ("checkout", "shopping cart", "add to cart", "place order", "shipping address",
                     "card number", "billing address"),
    },
    TRANSFER: {
        "routes": ("/transfer", "/wire", "/send-money", "/sendmoney", "/pay-", "/payments"),
        "keywords": ("transfer", "wire", "send money", "from account", "to account", "payee",
                     "routing number"),
    },
    QUOTE: {
        "routes": ("/quote", "/get-quote", "/get-a-quote", "/rate", "/estimate", "/rates"),
        "keywords": ("get quote", "get a quote", "quote", "premium", "coverage amount",
                     "term length", "estimate", "annual rate"),
    },
    APPLY: {
        "routes": ("/apply", "/application", "/enroll", "/signup", "/sign-up", "/register", "/onboard"),
        "keywords": ("apply now", "application", "enroll", "sign up", "register", "beneficiary",
                     "create account"),
    },
    ACCOUNT: {
        "routes": ("/account", "/profile", "/settings", "/dashboard", "/preferences"),
        "keywords": ("account settings", "my account", "profile", "preferences", "manage account"),
    },
    SEARCH: {
        "routes": ("/search", "/find", "/results", "/browse"),
        "keywords": ("search", "filter by", "sort by", "results", "no results"),
    },
}

_ROUTE_WEIGHT = 2  # a route marker is stronger evidence than a keyword.
_KEYWORD_WEIGHT = 1
_MIN_SCORE = 2      # one route OR two keywords to clear "unknown".


def _norm(s: str) -> str:
    return str(s or "").strip().lower()


def classify_funnel(
    *,
    routes: Iterable[str] = (),
    labels: Iterable[str] = (),
    texts: Iterable[str] = (),
) -> dict:
    """Classify a journey's funnel type from its routes + field labels + button/link text.

    Returns ``{funnel_type, confidence, evidence, scores}``. ``confidence`` is the
    winner's share of total matched weight (0..1); ``evidence`` lists the matched
    markers. Below ``_MIN_SCORE`` the type is ``unknown`` (honest, never forced).
    """
    route_blob = " ".join(_norm(r) for r in routes)
    text_blob = " ".join(_norm(t) for t in list(labels) + list(texts))

    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for ftype, markers in _MARKERS.items():
        score = 0
        hits: list[str] = []
        for rm in markers["routes"]:
            if rm in route_blob:
                score += _ROUTE_WEIGHT
                hits.append(f"route:{rm}")
        for kw in markers["keywords"]:
            if kw in text_blob or kw in route_blob:
                score += _KEYWORD_WEIGHT
                hits.append(f"kw:{kw}")
        if score:
            scores[ftype] = score
            evidence[ftype] = hits

    if not scores:
        return {"funnel_type": UNKNOWN, "confidence": 0.0, "evidence": [], "scores": {}}

    best = max(scores, key=lambda t: (scores[t], -_PRIORITY[t]))
    if scores[best] < _MIN_SCORE:
        return {"funnel_type": UNKNOWN, "confidence": 0.0, "evidence": [],
                "scores": scores}
    total = sum(scores.values())
    return {
        "funnel_type": best,
        "confidence": round(scores[best] / total, 3) if total else 0.0,
        "evidence": evidence[best],
        "scores": scores,
    }


def classify_journey(journey: Mapping) -> dict:
    """Convenience over a journey dict that carries ``routes`` / ``labels`` / ``texts``
    (any subset). Missing keys are treated as empty — never an error."""
    j = journey if isinstance(journey, Mapping) else {}
    return classify_funnel(
        routes=j.get("routes") or (),
        labels=j.get("labels") or (),
        texts=j.get("texts") or (),
    )
