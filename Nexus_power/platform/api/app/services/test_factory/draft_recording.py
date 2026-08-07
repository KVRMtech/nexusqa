"""Draft recording — capture a login BEFORE the application exists.

Recording belongs at the start of onboarding, where the operator actually begins.
But every recipe is stored against an artifact, and an artifact is only minted by
the first crawl — so at the Access step there is nothing to attach a recording to.

This module derives what a recording produced WITHOUT persisting any of it: the
login choreography, and the environment the browser ended up talking to. The
wizard holds that draft, it travels with the app when the app is created, and it
becomes a real recipe when the first crawl mints an artifact.

Two things come out of one pass, because the operator does both in one sitting:

  * the LOGIN — steps, named slots, and the reuse fingerprint;
  * the ENVIRONMENT — the origin actually landed on, and the cookies that routed
    the browser there. A routing cookie is how many estates select a box, so it is
    part of "where", not part of "who".

WHAT COMES BACK, AND WHY EACH PART EXISTS. A recording is what finally lets a crawl
past a login, so three things come out and they are kept strictly apart:

  * ``login``       the choreography + named slots. No values, ever — the observer
                    does not read them. This is what OTHER members replay later.
  * ``environment`` the origin landed on and the cookies that ROUTED the browser
                    there. Session cookies are excluded: routing is "where", not
                    "who".
  * ``session``     the storageState — a real logged-in session, which is what gets
                    THIS crawl in. The crawler already accepts one
                    (``explorations._resolve_session``), so nothing new is invented.

The session is returned SEPARATELY and is never folded into ``login`` or
``environment``, so a caller that only wants a description of the login cannot
accidentally end up holding a live one. It travels the same path the onboarding
password already travels — posted once over TLS and encrypted at rest by the
service that stores it — and is never written to disk here.

Pure — no DB, no I/O. The caller talks to the runner and decides what to store.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import login_observation
from . import login_recorder

# Cookie names that carry an authenticated session rather than routing. A draft
# describes a login; it must not smuggle one. Matched case-insensitively as a
# substring, so framework variants are covered without naming every framework.
_SESSION_COOKIE_HINTS = ("session", "auth", "token", "jwt", "sso", "sid",
                         "csrf", "xsrf", "identity", "login")

__all__ = ["derive_draft", "routing_cookies"]


def routing_cookies(storage_state: dict | None) -> list:
    """The cookies that plausibly ROUTED the browser, never the ones that
    authenticated it. Conservative in the safe direction: anything that looks
    like a session is dropped, so a draft can under-describe an environment but
    can never carry a credential."""
    out: list = []
    for raw in ((storage_state or {}).get("cookies") or []):
        cookie = raw or {}
        name = str(cookie.get("name") or "")
        if not name:
            continue
        if any(hint in name.lower() for hint in _SESSION_COOKIE_HINTS):
            continue
        out.append({"name": name,
                    "value": str(cookie.get("value") or ""),
                    "domain": str(cookie.get("domain") or ""),
                    "path": str(cookie.get("path") or "/")})
    return out


def derive_draft(*, observation_snapshot: dict | None,
                 storage_state: dict | None, start_url: str = "") -> dict:
    """Turn one recording into a draft: a login recipe and an environment.

    Returns ``{login, environment, usable, reason}``. ``login`` is None when the
    recording contained no login — a public flow records its environment alone,
    which is a legitimate outcome and not an error."""
    observation = login_observation.observation_from_events(
        observation_snapshot, base_url=start_url)

    landed = str((observation_snapshot or {}).get("current_url") or start_url or "")
    parts = urlsplit(landed)
    base_url = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""

    environment = {
        "base_url": base_url,
        "cookies": routing_cookies(storage_state),
    }
    # Kept apart from `environment` on purpose: one describes where to go, the
    # other IS an authenticated session.
    # `__nx_session_storage` counts: an app that keeps its sign-in in
    # sessionStorage yields no cookies and no origins, and requiring them made a
    # perfectly good recording report an empty session.
    session = storage_state if isinstance(storage_state, dict) and (
        storage_state.get("cookies") or storage_state.get("origins")
        or storage_state.get("__nx_session_storage")) else None

    if observation.get("unusable"):
        return {"login": None, "environment": environment, "session": session,
                "usable": False,
                "reason": observation.get("reason") or "no_login_observed"}

    built = login_recorder.recipe_from_observed_login(observation)
    if not built.get("steps") or not built.get("slots"):
        return {"login": None, "environment": environment, "session": session,
                "usable": False, "reason": "no_credential_fields_observed"}

    return {
        "login": {
            "steps": built["steps"],
            "slots": built["slots"],
            "login_type_key": built["login_type_key"],
            "domain": observation.get("domain") or "",
            "login_path": observation.get("login_path") or "/",
            "home_path": (observation.get("home") or {}).get("url_pattern") or "",
            "federated": bool(observation.get("federated")),
            "slot_names": [s.get("name") for s in built["slots"]],
        },
        "environment": environment,
        "session": session,
        "usable": True,
        "reason": "",
    }
