"""Is the login we just recorded actually a login — and may a run be handed it?

``Record + Run`` chains three steps that used to be separate: record a login, store
it, run the suite with it. Chaining them creates a failure the separate steps never
had — the middle step can succeed *technically* while producing nothing usable, and
the run then starts anyway.

Concretely: the recorder always returns a storage state. If the operator opened the
browser and closed it, or the page uses a login the capture could not see, that state
is an EMPTY cookie jar. Storing it succeeds. Running with it means running LOGGED
OUT — and a logged-out suite fails every step, which the report then attributes to
the application under test. The operator sees a wall of red against their client's
app, caused by a login that never happened.

This module answers one question before a run is allowed to start: **does this
captured state actually carry a session?**

The test is deliberately CRUDE and fail-closed rather than clever. Judging whether a
particular cookie is "the auth cookie" means guessing at names across every app in
the world; guessing wrong in the permissive direction re-opens the exact hole. So the
rule is only: a session must carry SOMETHING a browser could be authenticated by —
at least one cookie, or at least one origin holding local/session storage (token
auth in a single-page app). Nothing at all is not a login, and that judgement cannot
false-positive.

Pure — no DB, no I/O. The caller stores the state and applies the verdict.
"""
from __future__ import annotations

__all__ = ["assess", "USABLE", "EMPTY", "MALFORMED"]

#: The captured state carries something a browser could be authenticated by.
USABLE = "usable"
#: A well-formed state with nothing in it — the recorder ran, the login did not.
EMPTY = "empty_session"
#: Not a storage state at all.
MALFORMED = "malformed"


def assess(storage_state: object) -> dict:
    """Judge a captured Playwright storage state.

    Returns ``{state, usable, cookies, origins, storage_keys, reason, note}``.
    ``note`` is the sentence an operator reads; it never blames the application.
    """
    if not isinstance(storage_state, dict):
        return _out(MALFORMED, 0, 0, 0,
                    note=("The recorder did not return a usable session. Nothing has "
                          "been run — a suite started without a login would fail every "
                          "step and those failures would be attributed to the "
                          "application, which would be wrong."))

    cookies = storage_state.get("cookies")
    cookies = cookies if isinstance(cookies, list) else []
    origins = storage_state.get("origins")
    origins = origins if isinstance(origins, list) else []

    # Count the actual storage entries, not just the origins: an origin entry with an
    # empty list is what a page that set nothing looks like, and counting it as
    # "has storage" would let an empty session through.
    keys = 0
    for o in origins:
        if isinstance(o, dict):
            items = o.get("localStorage")
            if isinstance(items, list):
                keys += len([i for i in items if isinstance(i, dict) and i.get("name")])

    if not cookies and keys == 0:
        return _out(EMPTY, 0, len(origins), 0,
                    note=("No session was captured — the recorded browser held no "
                          "cookies and no stored tokens, which means the login did not "
                          "complete. The run has NOT been started: running now would "
                          "execute the whole suite logged out and report every failure "
                          "against the application under test. Record the login again "
                          "and make sure you reach the logged-in page before saving."))

    return _out(USABLE, len(cookies), len(origins), keys,
                note=("A session was captured (%d cookie(s), %d stored token(s)). The "
                      "run will use it." % (len(cookies), keys)))


def _out(state: str, cookies: int, origins: int, keys: int, *, note: str) -> dict:
    return {
        "state": state,
        "usable": state == USABLE,
        "cookies": cookies,
        "origins": origins,
        "storage_keys": keys,
        "reason": "" if state == USABLE else state,
        "note": note,
    }
