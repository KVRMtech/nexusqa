"""Federated / SSO login (#7) — the DECLARED IdP-domain AUTH exception.

The guard blocks an AUTH-phase login POST that leaves the login's registrable
domain (``AUTH_OFFDOMAIN_POST_BLOCKED``).  Federated login (OIDC/SAML) redirects
the browser to an Identity Provider on a DIFFERENT domain, so that block would
kill every SSO login.  #7 adds a NARROW, fail-closed exception: during the AUTH
window only, a POST to a DECLARED IdP registrable domain is treated as a login
domain.  These tests pin the security envelope:

  * declared IdP  → allowed (AUTH only);
  * undeclared / no declaration → still blocked (byte-identical to before);
  * a look-alike domain (``okta.com.attacker.net``) → refused (exact eTLD+1);
  * the same declared IdP OUTSIDE auth → still blocked (no explore/submit door);
  * the ≤N-req/≤T-ms auth window STILL bounds the IdP burst.
"""
from __future__ import annotations

from app.auth import AuthWindow
from app.crawler import GuardContext, Phase
from app.guard import load_refuse_pack
from app.config import Settings

_PACK = load_refuse_pack(Settings().refuse_pack_path)
_APP = "app.example"
_IDP_POST = "https://acme.okta.com/sso/saml/consume"


def _guard(*, idp=(), phase=Phase.AUTH, window=None) -> GuardContext:
    g = GuardContext(
        refuse_pack=_PACK, login_host=_APP, idp_domains=frozenset(idp),
        auth_window=window or AuthWindow(max_requests=10, window_ms=30_000),
    )
    g.phase = phase
    return g


def test_declared_idp_sso_post_is_allowed_in_auth():
    g = _guard(idp={"okta.com"})
    decision = g.decide("POST", _IDP_POST, now_ms=1)
    assert decision.allow
    assert decision.rule_id == "guard.auth.login_post_ok"


def test_idp_host_is_normalized_to_its_registrable_domain():
    # declaring the full IdP HOST still matches a sibling SSO host on the eTLD+1.
    g = _guard(idp={"login.okta.com"})
    assert g.decide("POST", _IDP_POST, now_ms=1).allow


def test_offdomain_sso_post_blocked_without_declaration():
    # no idp_domains ⇒ the pre-#7 behaviour: an off-domain login POST is refused.
    g = _guard(idp=())
    decision = g.decide("POST", _IDP_POST, now_ms=1)
    assert not decision.allow
    assert decision.rule_id == "guard.auth.offdomain_post_blocked"


def test_lookalike_idp_domain_is_refused():
    # 'okta.com.attacker.net' reduces to 'attacker.net' — NOT the declared eTLD+1.
    g = _guard(idp={"okta.com"})
    decision = g.decide("POST", "https://login.okta.com.attacker.net/steal", now_ms=1)
    assert not decision.allow
    assert decision.rule_id == "guard.auth.offdomain_post_blocked"


def test_declared_idp_post_is_blocked_outside_the_auth_phase():
    # the IdP allowance is AUTH-only — an EXPLORE-phase POST to it is still refused.
    g = _guard(idp={"okta.com"}, phase=Phase.EXPLORE)
    decision = g.decide("POST", _IDP_POST, now_ms=1)
    assert not decision.allow
    assert decision.rule_id == "guard.explore.mutation_blocked"


def test_auth_window_still_bounds_the_idp_burst():
    # even a declared IdP is subject to the ≤N-req/≤T-ms window: the 2nd POST,
    # over the 1-request budget, is refused (no unbounded SSO POST door).
    g = _guard(idp={"okta.com"}, window=AuthWindow(max_requests=1, window_ms=30_000))
    assert g.decide("POST", _IDP_POST, now_ms=1).allow
    second = g.decide("POST", _IDP_POST, now_ms=2)
    assert not second.allow
    assert second.rule_id == "guard.auth.window_closed"


def test_reads_to_the_idp_are_allowed_in_auth():
    # a GET during auth is a read (allowed regardless of domain); the SSO redirect
    # GET reaching the IdP is not a mutation the guard needs to gate.
    g = _guard(idp={"okta.com"})
    assert g.decide("GET", "https://acme.okta.com/authorize?x=1", now_ms=1).allow
