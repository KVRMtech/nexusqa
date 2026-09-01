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
                    GuardRule, Phase, classify_request, registrable_domain,
                    same_registrable_domain)
from .walk_persist import WalkReason

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
    #: M1.3 CONTROLLED WALK PERSISTENCE — an :class:`app.walk_persist.WalkAuthorization`
    #: built ONLY from a verified platform provisioning proof, or ``None``.
    #:
    #: ``None`` is the default and is the entire backward-compatibility story:
    #: a crawl without a verified proof behaves byte-identically to the crawl
    #: before this feature existed, because the WALK phase is then unreachable
    #: for mutations and the crawler never enters it.  There is no boolean, env
    #: var or dispatch field that can stand in for this object.
    walk_authorization: Any = None
    #: The audit request id of the most recently AUTHORISED walk mutation, so the
    #: route handler can link the observed response status back to its ledger
    #: entry.  Diagnostic plumbing only — nothing reads it as a permission.
    last_walk_request_id: str = ""
    #: Why walk persistence was NOT granted (an :class:`app.attest.AttestReason`
    #: code), or "" when it was.  Carried into ``crawl_meta`` so a walk that
    #: stopped at a Save Draft can say which check refused it.
    walk_denied_reason: str = ""
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
        decision = classify_request(
            method, url, self.phase, self.refuse_pack, is_login, action_button_name,
            attestation=self.attestation, submit_flow_approved=self.submit_flow_approved,
            now_ms=now_ms, walk_attested=self.walk_attested,
        )
        # M1.3 · the per-step mutation budget + the immutable audit write.
        #
        # ORDER MATTERS, and this order is deliberate: the pure rules decide
        # FIRST, and only a request they would allow is charged to the budget.
        # Charging first would let a refused irreversible verb drain the
        # allowance a legitimate Save Draft needed — a denial-of-progress bug
        # dressed as a safety feature.
        if (self.phase is Phase.WALK
                and (method or "").strip().upper() in MUTATING_METHODS
                and decision.allow):
            return self._charge_walk_mutation(method, url, now_ms=now_ms)
        return decision

    # ── M1.3 walk persistence ────────────────────────────────────────────────

    @property
    def walk_attested(self) -> bool:
        """True ONLY when this crawl holds a verified platform provisioning
        proof.  Read from the authorisation object's own verdict — never from a
        flag anybody outside :mod:`app.attest` can set."""
        auth = self.walk_authorization
        verdict = getattr(auth, "verdict", None)
        return bool(auth is not None and getattr(verdict, "authorized", False))

    def _charge_walk_mutation(self, method: str, url: str, *,
                              now_ms: int) -> GuardDecision:
        """Atomically consume one per-step budget slot and write the audit
        record, or REFUSE.  Any failure — no authorisation, closed window,
        exhausted budget, off-origin target, an audit sink that would not
        write — is a block carrying its own stable rule id."""
        auth = self.walk_authorization
        if auth is None:                        # unreachable via decide(); belt+braces
            return GuardDecision(
                allow=False, reason="WALK mutation refused — no walk authorization",
                rule_id=GuardRule.WALK_NO_ATTESTATION,
                event_kind=EVENT_BLOCKED_METHOD, severity="critical")
        try:
            allowed, why, request_id = auth.authorize_mutation(method, url, now_ms=now_ms)
        except Exception:
            logger.exception("qec.walk.authorize_error — refusing the mutation")
            return GuardDecision(
                allow=False,
                reason="WALK mutation refused — authorization raised (fail-closed)",
                rule_id=GuardRule.WALK_NOT_AUTHORIZED,
                event_kind=EVENT_BLOCKED_METHOD, severity="critical")
        if allowed:
            self.last_walk_request_id = request_id
            return GuardDecision(
                allow=True,
                reason=(f"{(method or '').strip().upper()} allowed in WALK — attested "
                        f"disposable env, step-authorized, within budget; audited as "
                        f"{request_id}"),
                rule_id=GuardRule.WALK_MUTATION_OK)
        rule = (GuardRule.WALK_BUDGET_EXCEEDED
                if why in (WalkReason.BUDGET_EXCEEDED, WalkReason.WINDOW_CLOSED)
                else GuardRule.WALK_NOT_AUTHORIZED)
        return GuardDecision(
            allow=False,
            reason=f"WALK mutation refused — {why}",
            rule_id=rule, event_kind=EVENT_BLOCKED_METHOD, severity="critical")
