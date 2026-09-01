"""Canonical predicate for a captured browser session.

ONE definition of "does this captured session carry anything worth storing or
injecting into a crawl", shared by every Python service that decides it.

WHY THIS MODULE EXISTS
======================
A Playwright ``storageState`` carries cookies and localStorage. It does NOT carry
sessionStorage — so an app that keeps its whole sign-in there produces a state
with no cookies and no origins, and the naive test ``cookies or origins`` throws
that session away. sessionStorage rides an extra Nexus-namespaced key,
``__nx_session_storage``, and MUST be counted as substance.

That rule was rediscovered and re-fixed independently in four places
(qe-central ``_resolve_session``, platform-api ``derive_draft``, the qe-explorer
injection guard, and the portal panel) — the same bug, fixed four times. This is
the single home for the two Python services that can take the SDK dependency
(qe-central and platform-api, both built FROM nexus-base). The quarantined
qe-explorer keeps a vendored 3-line mirror (it deliberately does not install the
SDK), and the TypeScript portal keeps its own; both are comment-linked here so a
grep for ``session_has_substance`` surfaces every copy.

PURE — no I/O, no logging, no config, no third-party imports. Safe to import from
modules that advertise themselves as side-effect-free.
"""
from __future__ import annotations

__all__ = ["session_has_substance", "SESSION_SUBSTANCE_KEYS"]

#: The keys, any one of which (truthy) means the session carries a real sign-in.
#: ``cookies`` and ``origins`` are Playwright's own; ``__nx_session_storage`` is
#: the Nexus carrier for sessionStorage, which Playwright omits.
SESSION_SUBSTANCE_KEYS = ("cookies", "origins", "__nx_session_storage")


def session_has_substance(state: object) -> bool:
    """True when ``state`` is a captured session worth storing / injecting.

    A dict with at least one truthy substance key (see
    :data:`SESSION_SUBSTANCE_KEYS`). Anything else — ``None``, a non-dict, or an
    all-empty dict — is False. Deliberately tolerant of the input type so callers
    can hand it a raw payload without pre-validating.
    """
    return isinstance(state, dict) and any(
        state.get(k) for k in SESSION_SUBSTANCE_KEYS
    )
