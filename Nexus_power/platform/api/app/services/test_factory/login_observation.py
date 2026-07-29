"""Observed-login derivation — raw runner events -> the login OBSERVATION contract.

The runner's ``login_observer.js`` records a flat, ordered log of what a tester
actually did while logging in by hand in the headed auth-capture browser: which
fields were committed and which controls were pressed, with NO credential values
(identifiers only). This module turns that log into the ``observation`` dict that
``login_recorder.recipe_from_observed_login`` consumes.

The inference lives HERE, in Python, rather than in the runner, for two reasons:
the runner is hot-deployed by ``docker cp`` and must stay small and low-risk, and
this reasoning is exactly the part that deserves unit tests.

What has to be inferred from a flat log:

  * **which fills are credential slots** — every committed field is one; its slot
    name is the most stable identifier the page offered (name > id > autocomplete >
    normalized label). No vocabulary is assumed: ``member_number``, ``email``,
    ``policy_no``, ``pin`` are all just identifiers.
  * **which click is the submit** — the first control pressed *after the last
    field was filled*. Controls pressed *between* fills are rungs of a multi-step
    ladder (member -> Continue -> PIN -> Verify) and must stay in place, in order.
  * **which clicks are an optional interstitial** — controls pressed *after* the
    submit but *before* the landing. That is the "sign this document" pop-up some
    members get and others do not, so those steps are marked optional downstream
    and both populations converge at Home.
  * **the Home signal** — the path the browser settled on once login finished,
    used as the ``assert_home`` oracle (login succeeded because a logged-in STATE
    was reached, never because N steps ran).

Pure — no DB, no I/O, no network.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# Mirrors qe-explorer's ``app/guard.py`` heuristic. The crawler computes the
# domain for a crawl-recorded login and this module computes it for an
# auth-capture-recorded one; both feed the SAME login_type_key, so the two must
# agree exactly. KEEP IN SYNC with guard._MULTI_PART_SUFFIXES / registrable_domain.
_MULTI_PART_SUFFIXES = frozenset({
    # United Kingdom
    "co.uk", "org.uk", "gov.uk", "ac.uk", "ltd.uk", "plc.uk", "me.uk",
    "net.uk", "sch.uk", "nhs.uk", "police.uk", "mod.uk",
    # Australia / New Zealand
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au", "asn.au",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz", "geek.nz",
    # Japan / Korea
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp",
    "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr",
    # India / South Africa
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in", "gov.in",
    "co.za", "org.za", "net.za", "gov.za", "web.za",
    # Brazil / Mexico / LatAm
    "com.br", "net.br", "org.br", "gov.br", "edu.br",
    "com.mx", "org.mx", "gob.mx", "com.ar", "com.co", "com.pe", "com.ve",
    # Asia-Pacific
    "com.sg", "com.hk", "com.cn", "com.tw", "com.my", "com.ph", "com.vn",
    "com.pk", "com.bd", "com.np", "co.th", "co.id", "or.id", "co.il",
    # Europe / Middle East
    "com.tr", "org.tr", "gov.tr", "co.at", "or.at", "com.ua", "com.ru",
    "co.ke", "co.ug", "co.tz", "com.ng", "com.eg", "com.sa", "com.qa",
    "com.kw", "com.bh", "com.lb",
})

# Controls that never advance a login — pressing one must not be mistaken for the
# submit. Matched on the control's visible text, case/punctuation-insensitive.
_NON_ADVANCING = frozenset({
    "cancel", "back", "close", "dismiss", "forgot password", "forgot username",
    "forgot member number", "need help", "help", "register", "sign up",
    "enroll", "create account", "remember me", "show password", "hide password",
    "english", "espanol", "privacy", "terms", "skip",
})

_WS = re.compile(r"[\s_\-]+")


def _looks_like_ip(host: str) -> bool:
    """True for an IPv4 dotted-quad or an IPv6 literal (bracketed or bare)."""
    if host.startswith("[") or host.count(":") >= 2:
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 for a hostname (see the KEEP IN SYNC note above)."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    if host.count(":") == 1 and not host.startswith("["):
        host = host.split(":", 1)[0]
    if _looks_like_ip(host) or "." not in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _norm(text: object) -> str:
    return _WS.sub(" ", str(text or "").strip().lower()).strip()


def _slot_name(event: dict) -> str:
    """The most stable identifier the page offered for a field. This becomes BOTH
    the recipe's slot name and the fingerprint's field name, so a login recorded
    here keys identically to the same login matched at onboarding."""
    for key in ("name", "id", "autocomplete"):
        val = str((event or {}).get(key) or "").strip()
        if val:
            return val
    label = _norm((event or {}).get("label"))
    return _WS.sub("_", label) if label else ""


def _slot_type(event: dict) -> str:
    """``secret`` for a masked input, ``plain`` otherwise. Every slot's value is
    envelope-encrypted in the credential card regardless — this only tells the UI
    whether to mask the box, so a member number renders readable and a password
    does not."""
    return "secret" if str((event or {}).get("type") or "").lower() == "password" else "plain"


def _path_of(url: object) -> str:
    try:
        path = urlsplit(str(url or "")).path or "/"
    except ValueError:
        return "/"
    return path if path.startswith("/") else "/" + path


def _host_of(url: object) -> str:
    try:
        return urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""


def _origin_of(url: object) -> str:
    """scheme://host[:port] — how we tell 'the login happened somewhere else'."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_advancing(click: dict) -> bool:
    """A control that plausibly advances the login (not Cancel / Forgot / a
    language toggle). Conservative: unknown controls DO advance, because missing a
    real rung breaks replay, while an extra rung is visible in the recipe."""
    return _norm((click or {}).get("text")) not in _NON_ADVANCING


def observation_from_events(snapshot: dict | None, *, base_url: str = "") -> dict:
    """Derive the login observation from the runner's raw event snapshot.

    Returns the ``login_recorder.recipe_from_observed_login`` contract, plus a
    ``sequence`` carrying the TRUE interleaved order of fills and clicks so a
    multi-step ladder replays as performed, and ``unusable``/``reason`` when the
    log does not contain a login at all (surfaced, never fabricated)."""
    snap = snapshot or {}
    raw = [e for e in (snap.get("events") or []) if isinstance(e, dict)]
    events = sorted(raw, key=lambda e: (e.get("sequence_index") if isinstance(
        e.get("sequence_index"), int) else 0))

    start_url = str(snap.get("start_url") or base_url or "")
    current_url = str(snap.get("current_url") or "")

    # ── pass 1: walk, recording the URL in force when each action happened ──────
    url_at: str = start_url
    fills: list = []          # committed credential fields, in order
    clicks: list = []         # advancing control presses, in order
    navs: list = []           # (event_index, url) for landing detection
    login_url: str = ""       # URL where the FIRST field was committed

    for pos, ev in enumerate(events):
        kind = str(ev.get("kind") or "")
        if kind == "navigate":
            url_at = str(ev.get("url") or url_at)
            navs.append({"pos": pos, "url": url_at})
            continue
        if kind == "fill":
            if not ev.get("filled"):
                continue                       # a cleared/blank field is not a credential
            slot = _slot_name(ev)
            if not slot:
                continue
            if not login_url:
                login_url = url_at
            fills.append({"pos": pos, "slot": slot, "url": url_at,
                          "label": str(ev.get("label") or "").strip() or slot,
                          "type": _slot_type(ev)})
            continue
        if kind == "click":
            if not _is_advancing(ev):
                continue
            text = str(ev.get("text") or "").strip()
            if not text:
                continue                       # an unnameable control cannot be replayed
            clicks.append({"pos": pos, "name": text, "url": url_at,
                           "role": str(ev.get("role") or "").strip()})

    if not fills:
        return {"unusable": True, "reason": "no_credential_fields_observed",
                "domain": registrable_domain(_host_of(login_url or start_url)),
                "login_path": _path_of(login_url or start_url),
                "fields": [], "submit": "", "sequence": []}

    # FEDERATED LOGIN (SSO / OIDC / SAML). The credential fields are entered on the
    # identity provider's origin, not the application's. Keying on where the typing
    # happened would be wrong twice over: the reuse fingerprint would belong to the
    # IdP, so every application behind one provider would collide; and the replayed
    # goto would be the provider's path resolved against the application's base URL,
    # which is simply a wrong address.
    #
    # A login belongs to the APPLICATION being tested. So the recipe enters where
    # the recording entered and lets the application redirect exactly as it did for
    # the operator — which is also the only way the provider receives a valid
    # authorization request.
    app_origin = _origin_of(start_url)
    login_origin = _origin_of(login_url)
    federated = bool(app_origin and login_origin and app_origin != login_origin)
    fingerprint_host = _host_of(start_url) if (federated and start_url) else _host_of(login_url)
    login_path = _path_of(start_url) if federated else _path_of(login_url)

    # ── pass 2: RETRY COLLAPSE ─────────────────────────────────────────────────
    # A mistyped password means the operator filled the same slots twice. Replaying
    # both attempts would refill a field on the page AFTER login and abort the run,
    # so only the last complete attempt is the choreography.
    attempt_start = 0
    seen_slots: set = set()
    for i, f in enumerate(fills):
        if f["slot"] in seen_slots:
            attempt_start = i
            seen_slots = {f["slot"]}
        else:
            seen_slots.add(f["slot"])
    fills = fills[attempt_start:]
    first_fill_pos = fills[0]["pos"]

    # ── pass 3: discard clicks that are not part of THIS login ────────────────
    # Anything pressed before the login page was reached is navigation (the site's
    # "Log in" link), not a rung: the recipe already goes straight to login_path,
    # so replaying it would click a control that is not on that page. Keyed on the
    # URL, not the counter — a genuine on-page rung pressed before any field (a
    # "use my member number" tab) is at login_path and is correctly kept.
    kept: list = []
    for c in clicks:
        if c["pos"] < first_fill_pos:
            on_entry_page = _path_of(c["url"]) == login_path
            if not on_entry_page:
                # Navigation that merely got us to the login page: the recipe
                # already goes straight there, so replaying it would hunt a control
                # that is not on that page.
                continue
            if attempt_start:
                # An earlier, abandoned attempt (a mistyped password). Replaying its
                # submit would fire before the fields of the kept attempt exist.
                continue
        kept.append(c)
    clicks = kept

    # ── pass 4: the submit, and the landing that ENDS the login ────────────────
    total_fills = len(fills)
    last_fill_pos = fills[-1]["pos"]
    submit_i = next((i for i, c in enumerate(clicks) if c["pos"] > last_fill_pos), None)
    submit = clicks[submit_i]["name"] if submit_i is not None else ""
    submit_pos = clicks[submit_i]["pos"] if submit_i is not None else last_fill_pos

    # The landing is where the operator ENDED UP — the last navigation off the
    # login page. It cannot be the FIRST such navigation: a "sign this document"
    # interstitial is itself a different path, so first-match would call the
    # document page Home and throw away the acknowledgement that clears it.
    #
    # KNOWN LIMIT, surfaced not hidden: if the operator browses on after logging
    # in instead of saving straight away, the last navigation is wherever they
    # wandered to. The derived Home is therefore returned to the UI so they can
    # SEE what was captured — and the interstitial steps are optional, so absorbed
    # browsing clicks fail-and-skip rather than breaking another member's run.
    after = [n for n in navs
             if n["pos"] > submit_pos and _path_of(n["url"]) != login_path]
    landing = after[-1] if after else None
    landing_pos = landing["pos"] if landing else None
    home = {"url_pattern": _path_of(landing["url"])} if landing else {}

    # ── pass 5: rebuild the true performed order ──────────────────────────────
    ladder = [c for c in clicks if submit_i is None or c["pos"] <= submit_pos]
    sequence: list = []
    li = 0
    for f in fills:
        while li < len(ladder) and ladder[li]["pos"] < f["pos"]:
            step = {"action": "click", "name": ladder[li]["name"]}
            if ladder[li].get("role") and ladder[li]["role"] not in ("button", ""):
                step["role"] = ladder[li]["role"]
            sequence.append(step)
            li += 1
        sequence.append({"action": "fill", "slot": f["slot"], "label": f["label"]})
    for c in ladder[li:]:
        step = {"action": "click", "name": c["name"]}
        if c.get("role") and c["role"] not in ("button", ""):
            step["role"] = c["role"]
        sequence.append(step)

    # Controls pressed after the submit but BEFORE the landing: the "sign this
    # document" interstitial only some members are shown. Bounded by the landing,
    # so post-login browsing is never mistaken for part of the login.
    interstitial = []
    if submit_i is not None:
        for c in clicks[submit_i + 1:]:
            if landing_pos is not None and c["pos"] > landing_pos:
                break
            step = {"action": "click", "name": c["name"]}
            if c.get("role") and c["role"] not in ("button", ""):
                step["role"] = c["role"]
            interstitial.append(step)

    # Dedupe slots, first occurrence wins, order preserved.
    seen: set = set()
    fields: list = []
    for f in fills:
        if f["slot"] in seen:
            continue
        seen.add(f["slot"])
        fields.append({"slot": f["slot"], "label": f["label"], "type": f["type"]})

    # A login we could not see completed is NOT a usable recipe — replaying it
    # would fill the form and never submit, then "succeed" because the steps ran.
    if not submit:
        return {"unusable": True, "reason": "no_submit_control_observed",
                "domain": registrable_domain(fingerprint_host),
                "login_path": login_path, "fields": fields, "submit": "",
                "sequence": sequence, "federated": federated}

    return {
        "domain": registrable_domain(fingerprint_host),
        "login_path": login_path,
        "federated": federated,
        "fields": fields,
        "submit": submit,
        "verify_documents": interstitial,
        "home": home,
        "sequence": sequence,
        "unusable": False,
        "reason": "",
    }


__all__ = ["observation_from_events", "registrable_domain"]
