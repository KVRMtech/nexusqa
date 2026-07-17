"""Group Seed-Manifest fields into FLOWS.

A flat list of every ungrounded field reads as noise: the user onboarded an app to test
one thing (a *Transfer*) and instead sees Transfer, Loan and Login fields jumbled with
identical "provide a real value" copy. This groups them so the panel can say, per flow,
"to test **Transfer**, provide these values — N of M ready", and lead with the flow the
app was actually onboarded for.

Design (deliberately layered so it degrades honestly):

1. GENERIC CORE — always runs, no domain knowledge:
   * split AUTH/login fields into their own group, marked *satisfied* when the app already
     holds stored credentials (so it never re-asks for a login the user gave at onboarding);
   * mark each field *provided* when its value is already in the answer key (drives the
     "N of M ready" progress);
   * surface the PRIMARY flow first — inferred from the app's entry URL path, which is
     domain-agnostic (the last meaningful URL segment names the flow on any site).

2. DOMAIN BOOST — optional, only *names* buckets more specifically. Keyword hints
   (transfer / loan / bill / profile / search) give a matched flow a human name; a field
   that matches no hint still lands in one honest "Fields to provide" group. Per the
   product doctrine, domain vocab is a boost, never the mechanism.

The truly accurate source of a field's flow is the PAGE it appeared on. The crawler
currently discards per-field page context (``fields_needing_seed`` is a flat label list),
so until it emits that, this grouping is heuristic — but its worst case is a single flat
group (today's behaviour), never a confident-but-wrong grouping.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .dispositions import normalize_label

# ── Auth / login lexicon ─────────────────────────────────────────────────────
# Whole-normalized-label or token matches that mark a credential/login field. Kept
# tight so profile fields ("email", "name") are NOT swept in unless clearly a login.
_AUTH_TOKENS = (
    "password", "passwd", "passcode", "pass code", "pin", "otp", "one time password",
    "mfa", "2fa", "totp", "verification code", "security code", "remember me", "remember",
    "username", "user name", "user id", "userid", "login", "log in", "sign in", "signin",
)

# ── Domain flow hints (optional boost — order = display priority among non-primary) ──
# Each: (key, display name, keyword fragments matched against the normalized label).
_FLOW_HINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("transfer", "Transfer", (
        "transfer", "from account", "to account", "account", "payee", "recipient",
        "beneficiary", "amount", "routing", "iban", "swift", "send money", "sort code",
    )),
    ("loan", "Loan", (
        "loan", "principal", "apr", "interest rate", "borrow", "emi", "tenure",
        "repayment", "collateral",
    )),
    ("payment", "Bill Pay", ("bill", "biller", "invoice", "due", "pay to", "payment")),
    ("profile", "Profile", (
        "first name", "last name", "full name", "middle name", "phone", "mobile",
        "address", "date of birth", "dob", "city", "state", "zip", "postal", "ssn",
        "gender", "nationality", "occupation",
    )),
    ("search", "Search", ("search", "filter", "query", "keyword")),
    ("contact", "Contact", ("subject", "message", "comment", "feedback", "enquiry")),
)

_FALLBACK_KEY = "other"
_FALLBACK_NAME = "Fields to provide"

# Dispositions the user must act on (vs. auto-handled) — drives per-flow "to provide".
_ACTIONABLE = {"ASK", "APPROVE"}


def _is_auth(norm: str) -> bool:
    return any(t in norm for t in _AUTH_TOKENS)


def _flow_of(norm: str) -> tuple[str, str]:
    """Best (key, display-name) for a non-auth field via domain hints; fallback otherwise."""
    for key, name, kws in _FLOW_HINTS:
        if any(k in norm for k in kws):
            return key, name
    return _FALLBACK_KEY, _FALLBACK_NAME


def _primary_key_from_url(base_url: str, present: set[str]) -> str | None:
    """Infer the onboarded flow from the entry URL's last meaningful path segment.

    Generic: a path segment matching a bucket key (or its hint keywords) wins; e.g.
    ``…/bank/transfer`` -> ``transfer``. Returns a key only if that flow is actually
    present among the fields, so we never elect an empty primary.
    """
    try:
        path = urlsplit(base_url or "").path
    except ValueError:
        path = ""
    segs = [normalize_label(s) for s in path.split("/") if s and not s.isdigit()]
    for seg in reversed(segs):
        for key, _name, kws in _FLOW_HINTS:
            if key in present and (seg == key or seg in kws or any(seg in k or k in seg for k in kws)):
                return key
    return None


def group_into_flows(
    items: Iterable[Mapping],
    *,
    base_url: str = "",
    provided_labels: Sequence[str] = (),
    auth_satisfied: bool = False,
) -> dict:
    """Partition manifest ``items`` (each a Disposition ``as_dict``) into flows.

    Returns ``{"flows": [...data flows, primary first...], "auth": {...}|None,
    "primary_flow": key|None}``. Each flow carries ``items`` (with a ``provided`` bool
    added), ``to_provide`` (actionable-and-not-yet-provided count) and ``total``.
    """
    provided = {normalize_label(x) for x in provided_labels if str(x).strip()}
    auth_items: list[dict] = []
    buckets: dict[str, dict] = {}

    for it in items:
        norm = normalize_label(str(it.get("label") or ""))
        if not norm:
            continue
        enriched = {**it, "provided": norm in provided}
        if _is_auth(norm):
            auth_items.append(enriched)
            continue
        key, name = _flow_of(norm)
        buckets.setdefault(key, {"key": key, "name": name, "items": []})["items"].append(enriched)

    present = set(buckets)
    primary_key = _primary_key_from_url(base_url, present)

    def _counts(bucket_items: list[dict]) -> tuple[int, int]:
        actionable = [x for x in bucket_items if x.get("disposition") in _ACTIONABLE]
        to_provide = sum(1 for x in actionable if not x.get("provided"))
        return to_provide, len(actionable)

    # Order: primary first, then remaining flows by domain-hint priority, fallback last.
    hint_order = {key: i for i, (key, _n, _k) in enumerate(_FLOW_HINTS)}

    def _sort_key(key: str) -> tuple:
        return (0 if key == primary_key else 1, hint_order.get(key, 99), key)

    flows: list[dict] = []
    for key in sorted(buckets, key=_sort_key):
        b = buckets[key]
        to_provide, actionable_total = _counts(b["items"])
        flows.append({
            "key": key,
            "name": b["name"],
            "kind": "flow",
            "primary": key == primary_key,
            "to_provide": to_provide,
            "actionable": actionable_total,
            "total": len(b["items"]),
            "items": b["items"],
        })

    auth = None
    if auth_items:
        a_to_provide, a_actionable = _counts(auth_items)
        auth = {
            "key": "auth",
            "name": "Sign in",
            "kind": "auth",
            "satisfied": bool(auth_satisfied),
            # A stored credential satisfies the whole login group; otherwise it's real
            # progress against the login fields.
            "to_provide": 0 if auth_satisfied else a_to_provide,
            "actionable": a_actionable,
            "total": len(auth_items),
            "items": auth_items,
        }

    return {"flows": flows, "auth": auth, "primary_flow": primary_key}
