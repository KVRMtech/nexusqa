"""The guard context — crawl phase + the AUTH window (M0.3 / T-DE-03 map item 3).

Extracted VERBATIM from :mod:`app.crawler`, and deliberately placed BESIDE
:mod:`app.guard` rather than inside it: ``guard.py`` holds the stateless
refuse-pack rules, while this holds the per-crawl MUTABLE state those rules are
evaluated against (which phase the crawl is in, whether an auth window is open,
which IdP domains were declared).  Merging the two would make a pure rule
engine stateful.

Shared by the crawler and the browser adapter's route handler — every network
request is classified here and ABORTED unless explicitly allowed, so this is
the fail-closed net.  Behaviour is unchanged by the move.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from .auth import AuthWindow
from .guard import (EVENT_BLOCKED_METHOD, MUTATING_METHODS, GuardDecision,
                    Phase, classify_request, registrable_domain,
                    same_registrable_domain)

logger = logging.getLogger("app.crawler")

@dataclass
class GuardContext:
    """Mutable guard state shared between the crawler and the Playwright route
    handler (:mod:`app.main`).  The crawler flips :attr:`phase` as the state
    machine advances; the route handler consults :meth:`decide` for EVERY
    network request so the fail-closed policy tracks the live phase.
    """

    refuse_pack: Any
    login_host: str = ""
    phase: Phase = Phase.EXPLORE
    auth_window: AuthWindow = field(default_factory=lambda: AuthWindow(max_requests=10, window_ms=30_000))
    #: Bounds the mutating-POST burst a single approved Phase-B submit may emit, so
    #: the SUBMIT window authorises the approved flow's POST(s) — NOT unlimited
    #: analytics/autosave/co-located POSTs that happen to fire during the window.
    #: Opened by the crawler at each submit; fail-closed when over budget / past T.
    submit_window: AuthWindow = field(default_factory=lambda: AuthWindow(max_requests=4, window_ms=15_000))
    attestation: Any = None
    submit_flow_approved: bool = False
    #: Federated / SSO login (#7): the DECLARED trusted Identity-Provider domains
    #: (login.microsoftonline.com / okta.com / …) a login flow may redirect to.
    #: Normalized to registrable domains in ``__post_init__``.  Empty ⇒ SSO
    #: cross-domain is refused exactly as before (byte-identical, fail-closed).
    idp_domains: frozenset = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Normalize the declared IdP allowlist to registrable domains ONCE so the
        # per-request check is an exact-set membership (never a suffix/substring
        # trick like 'okta.com.attacker.net').
        object.__setattr__(self, "idp_domains", frozenset(
            rd for rd in (registrable_domain(str(d).strip().lower())
                          for d in (self.idp_domains or ())) if rd
        ))

    def _is_declared_idp(self, host: str) -> bool:
        """True iff ``host``'s registrable domain is in the declared IdP allowlist
        — EXACT registrable-domain membership (never a substring/suffix match), so
        only a domain the operator explicitly declared can pass."""
        if not self.idp_domains or not host:
            return False
        rd = registrable_domain(host)
        return bool(rd) and rd in self.idp_domains

    def decide(self, method: str, url: str, *, now_ms: int,
               action_button_name: str = "") -> GuardDecision:
        """The full per-request decision, adding the caller-enforced AUTH window
        on top of the pure :func:`app.guard.classify_request`."""
        host = urlsplit(url or "").hostname or ""
        is_login = same_registrable_domain(host, self.login_host) if self.login_host else False
        # Federated / SSO login (#7): DURING the AUTH window only, a redirect to a
        # DECLARED IdP registrable domain counts as a login domain so the SSO POST
        # is not blocked as off-domain.  Narrow + fail-closed: AUTH phase only,
        # declared domains only, and still bounded by the ≤N-req/≤T-ms auth window
        # enforced just below (the IdP burst is not an open door).
        if not is_login and self.phase is Phase.AUTH and self._is_declared_idp(host):
            is_login = True
        if self.phase is Phase.AUTH:
            self.auth_window.note(now_ms)
            if (method or "").strip().upper() in MUTATING_METHODS and not self.auth_window.is_open(now_ms):
                return GuardDecision(
                    allow=False,
                    reason="AUTH window closed — login burst exceeded the "
                           "request/time budget",
                    rule_id="guard.auth.window_closed",
                    event_kind=EVENT_BLOCKED_METHOD, severity="critical",
                )
        if self.phase is Phase.SUBMIT:
            # Same caller-side budget as AUTH: an approved submit authorises a small
            # mutating-POST burst, not an open door for every POST the page fires
            # during the goto→refill→click window (analytics/autosave/co-located forms).
            self.submit_window.note(now_ms)
            if (method or "").strip().upper() in MUTATING_METHODS and not self.submit_window.is_open(now_ms):
                return GuardDecision(
                    allow=False,
                    reason="SUBMIT window closed — the approved flow exceeded the "
                           "request/time budget",
                    rule_id="guard.submit.window_closed",
                    event_kind=EVENT_BLOCKED_METHOD, severity="critical",
                )
        return classify_request(
            method, url, self.phase, self.refuse_pack, is_login, action_button_name,
            attestation=self.attestation, submit_flow_approved=self.submit_flow_approved,
            now_ms=now_ms,
        )
