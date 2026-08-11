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

from .dispositions import is_action_label, normalize_label

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

# URL path segments that don't name a flow (skip when reading a flow off a page path).
_GENERIC_SEGMENTS = frozenset({
    "", "step", "steps", "page", "pages", "index", "home", "app", "apps", "main",
    "en", "us", "en-us", "www", "public", "web", "ui",
})

# Dispositions the user must act on (vs. auto-handled) — drives per-flow "to provide".
_ACTIONABLE = {"ASK", "APPROVE"}


def _is_auth(norm: str) -> bool:
    return any(t in norm for t in _AUTH_TOKENS)


def _display_name_for(seg: str) -> str:
    """A readable flow name from a URL segment — just the segment title-cased. Domain
    naming is NOT applied here: domain fields group by hint (with the hint's own name)
    BEFORE the URL fallback, so title-casing the real segment keeps generic pages honest
    ("send money" -> "Send Money") and avoids two pages collapsing to one hint name."""
    return seg.replace("-", " ").replace("_", " ").strip().title() or _FALLBACK_NAME


def _flow_from_url(url: str) -> tuple[str, str] | None:
    """Ground a field's flow in the PAGE it appeared on: the last meaningful path segment
    names the flow (``…/orders/dispatch`` -> ``dispatch`` -> "Dispatch"). Domain-agnostic —
    the URL structure carries the flow on any site. Also reads a hash-route FRAGMENT
    (``app/#/checkout/payment`` -> ``payment``): many SPAs carry the real route there, not
    in the path. Returns None for a URL with no usable segment so the caller can fall back
    to label keywords."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    # Path first (the common case); then the hash fragment for hash-routed SPAs.
    for seg_source in (parts.path, parts.fragment):
        for raw in reversed([s for s in str(seg_source).split("/") if s]):
            seg = normalize_label(raw.split(".")[0])  # drop any file extension
            if not seg or seg.isdigit() or seg in _GENERIC_SEGMENTS:
                continue
            return seg, _display_name_for(seg)
    return None


def _flow_of(norm: str) -> tuple[str, str]:
    """Best (key, display-name) for a non-auth field via domain hints; fallback otherwise."""
    for key, name, kws in _FLOW_HINTS:
        if any(k in norm for k in kws):
            return key, name
    return _FALLBACK_KEY, _FALLBACK_NAME


def _primary_key_from_url(base_url: str, present: set[str]) -> str | None:
    """Elect the onboarded flow from the entry URL. Prefer the page the URL points at
    directly (grounded, same rule as the fields); fall back to a domain-hint match on any
    path segment. Returns a key only if that flow is actually present, so we never elect
    an empty primary."""
    grounded = _flow_from_url(base_url)
    if grounded and grounded[0] in present:
        return grounded[0]
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
    field_urls: Mapping[str, str] | None = None,
    captured_paths: Sequence[str] = (),
) -> dict:
    """Partition manifest ``items`` (each a Disposition ``as_dict``) into flows.

    ``field_urls`` maps a normalized field label to the PAGE URL the crawler saw it on.
    When present, a field's flow is grounded in that page (exact + fully generic); absent
    a URL, it falls back to label keyword hints. Auth fields split off by label regardless.

    Returns ``{"flows": [...data flows, primary first...], "auth": {...}|None,
    "primary_flow": key|None}``. Each flow carries ``items`` (with a ``provided`` bool
    added), ``to_provide`` (actionable-and-not-yet-provided count) and ``total``.
    """
    provided = {normalize_label(x) for x in provided_labels if str(x).strip()}
    urls = {normalize_label(k): v for k, v in (field_urls or {}).items() if str(v).strip()}
    auth_items: list[dict] = []
    buckets: dict[str, dict] = {}

    for it in items:
        label = str(it.get("label") or "")
        norm = normalize_label(label)
        if not norm:
            continue
        # Login/credential fields split off first (so they are never dropped as actions).
        if _is_auth(norm):
            auth_items.append({**it, "provided": norm in provided})
            continue
        disposition = it.get("disposition")
        uncaptured = bool(it.get("uncaptured_options"))
        actionable_or_choice = disposition in _ACTIONABLE or uncaptured
        # UI action controls ("Mark … as done", "Enable …") are not values to provide —
        # drop them. But NEVER drop a real data field: keep anything the user must provide
        # (ASK/APPROVE) or any unread choice, even if its label starts with a verb ("Add
        # rider", "Remove dependent").
        if is_action_label(label) and not actionable_or_choice:
            continue
        enriched = {**it, "provided": norm in provided}
        # PAGE-HONEST: the page a field was captured on is the ground truth of its flow.
        # Group by that page URL FIRST so fields from different pages never merge.
        grounded = _flow_from_url(urls.get(norm, ""))
        if grounded:
            key, name = grounded
        elif actionable_or_choice:
            # No page URL, but the user needs to see this field — a domain hint may NAME
            # its bucket (a boost, never a cross-page move); else the neutral bucket.
            hint_key, hint_name = _flow_of(norm)
            key, name = (hint_key, hint_name) if hint_key != _FALLBACK_KEY else (_FALLBACK_KEY, _FALLBACK_NAME)
        else:
            # A QUIET auto-handled field with no page URL: we don't actually know its flow.
            # A domain keyword must NOT invent a phantom bucket for it (that would leak
            # domain vocab as the grouping MECHANISM — e.g. an auto "Amount" spawning a
            # "Transfer" bucket on a non-banking app). Group it neutrally.
            key, name = _FALLBACK_KEY, _FALLBACK_NAME
        buckets.setdefault(key, {"key": key, "name": name, "items": []})["items"].append(enriched)

    present = set(buckets)
    # The flow keys the crawl ACTUALLY captured a page for (ground truth). When known,
    # this — NOT the bucket set — decides whether the onboarded entry flow was reached:
    # an auto-only field can hint-create a same-named bucket without the page ever being
    # crawled, which must not suppress the "not captured" signal.
    captured_keys = {fk[0] for p in captured_paths if (fk := _flow_from_url(str(p)))}
    entry = _flow_from_url(base_url)
    entry_captured = (
        entry is not None and (entry[0] in captured_keys if captured_paths else entry[0] in present)
    )
    missing_primary = None
    if entry is not None and not entry_captured:
        missing_primary = {
            "key": entry[0],
            "name": entry[1],
            "reason": f"we haven't captured your {entry[1]} form yet — re-crawl to reach it",
        }
    # Never mark a hint-created bucket the "main flow" when its page was never crawled.
    primary_key = None if missing_primary else _primary_key_from_url(base_url, present)

    def _counts(bucket_items: list[dict]) -> tuple[int, int, int]:
        actionable = [x for x in bucket_items if x.get("disposition") in _ACTIONABLE]
        to_provide = sum(1 for x in actionable if not x.get("provided"))
        # Dropdowns whose options weren't captured: a choice we can't offer until a
        # re-crawl. They are NOT actionable (not ASK), so they'd otherwise let an
        # all-dropdown flow read "0 of 0 ready" — a false green. Counted so the flow can
        # never be shown ready while any remain.
        uncaptured = sum(1 for x in bucket_items if x.get("uncaptured_options"))
        return to_provide, len(actionable), uncaptured

    # Order: primary first, then remaining flows by domain-hint priority, fallback last.
    hint_order = {key: i for i, (key, _n, _k) in enumerate(_FLOW_HINTS)}

    def _sort_key(key: str) -> tuple:
        return (0 if key == primary_key else 1, hint_order.get(key, 99), key)

    flows: list[dict] = []
    for key in sorted(buckets, key=_sort_key):
        b = buckets[key]
        to_provide, actionable_total, uncaptured = _counts(b["items"])
        flows.append({
            "key": key,
            "name": b["name"],
            "kind": "flow",
            "primary": key == primary_key,
            "to_provide": to_provide,
            "actionable": actionable_total,
            "uncaptured": uncaptured,
            "total": len(b["items"]),
            "items": b["items"],
        })

    auth = None
    if auth_items:
        a_to_provide, a_actionable, a_uncaptured = _counts(auth_items)
        auth = {
            "key": "auth",
            "name": "Sign in",
            "kind": "auth",
            "satisfied": bool(auth_satisfied),
            # A stored credential satisfies the whole login group; otherwise it's real
            # progress against the login fields.
            "to_provide": 0 if auth_satisfied else a_to_provide,
            "actionable": a_actionable,
            "uncaptured": a_uncaptured,
            "total": len(auth_items),
            "items": auth_items,
        }

    return {
        "flows": flows,
        "auth": auth,
        "primary_flow": primary_key,
        "missing_primary": missing_primary,
    }


def block_cause_for_missing_primary(
    *,
    has_credentials: bool,
    login_flow_present: bool,
    coverage: dict | None,
    can_sign_in: bool = True,
) -> dict | None:
    """Why an onboarded ENTRY flow wasn't captured: a benign non-reach vs an AUTH block.

    ``group_into_flows`` sets ``missing_primary`` purely on "the entry page wasn't in the
    captured routes" — it cannot see WHY. The seed-manifest layer knows two more things:
    the crawl's own coverage (does it report an auth block / an expired session?) and
    whether the app has stored credentials at all. Combine them into an honest cause so
    the app UI never tells a user to "just re-crawl" a login the crawl can never pass.

    Returns the annotation to merge onto ``missing_primary`` (a machine ``blocked`` code,
    a human ``reason`` and a ``remediation``), or ``None`` for a benign non-reach — which
    the caller leaves as the ordinary "didn't reach it — re-crawl" message.

    Precedence: the crawler's explicit ``coverage.auth_blocked`` (authoritative) or an
    ``auth_incomplete`` + ``session_expired`` session death; else a conservative fallback
    — the app has NO credentials AND the crawl saw a login flow — so the truth still
    surfaces on crawls recorded before the crawler flag shipped. Never fires when
    credentials exist and the crawl reported no auth trouble (a real budget/depth miss).
    """
    cov = coverage if isinstance(coverage, dict) else {}
    crawler_blocked = bool(cov.get("auth_blocked"))
    session_expired = (
        bool(cov.get("auth_incomplete"))
        and str(cov.get("auth_reason") or "") == "session_expired"
    )
    # A VERIFIED login that the app refuses to keep across page loads is its own block —
    # it carries neither auth_blocked nor session_expired, so it must be admitted here or
    # it would fall through and be reported as an ordinary "didn't reach it — re-crawl".
    not_persisted = (bool(cov.get("auth_incomplete"))
                     and str(cov.get("auth_reason") or "") == "not_persisted")
    if not (crawler_blocked or session_expired or not_persisted
            or (not has_credentials and login_flow_present)):
        return None
    if not_persisted:
        # The crawl SIGNED IN and the app still demanded a sign-in on the next page
        # load. Both of the usual remedies are already proven correct here, so naming
        # either would send the operator after nothing.
        return {
            "blocked": "auth_not_persisted",
            "reason": ("Blocked: the crawl signed in successfully, but this app drops "
                       "the sign-in on every page load."),
            "remediation": ("This app keeps the signed-in user in the page rather than "
                            "a cookie, so pages behind the login cannot be reached yet. "
                            "Re-recording and new credentials will not change it."),
        }
    if session_expired:
        if not can_sign_in:
            # The app holds ONLY a recorded session. Telling the operator to "re-record"
            # sends them round a loop that cannot end: the next recording captures
            # another session, and an app whose login lives in client-side state can
            # never restore one. The durable fix is a username + password the crawl
            # REPLAYS, so say that instead.
            return {
                "blocked": "auth_session_unusable",
                "reason": ("Blocked: this app has only a recorded session, and the app "
                           "would not accept it."),
                "remediation": ("Add a username and password so the crawl signs itself "
                                "in — re-recording captures another session that can "
                                "fail the same way."),
            }
        return {
            "blocked": "auth_session_expired",
            "reason": "Blocked: the stored login session has expired.",
            "remediation": "Re-record the login, then re-crawl.",
        }
    return {
        "blocked": "auth_no_credentials",
        "reason": "Blocked: this app is behind a login and has no credentials attached.",
        "remediation": "Record a login or attach a member card to this app, then re-crawl.",
    }
