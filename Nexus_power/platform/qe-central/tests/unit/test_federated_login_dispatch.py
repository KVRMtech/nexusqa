"""Federated / SSO login (#7) — the qe-central dispatch side.

The explorer's guard grants the AUTH-phase cross-domain SSO POST, but the browser
still cannot REACH the IdP unless the egress fence (squid allowlist) permits it.
These pin the two dispatch invariants:

  * the declared ``fences.idp_domains`` join the egress allowlist (so the SSO
    redirect can egress), WITHOUT displacing the app's own allowed hosts;
  * the dispatch request relays ``idp_domains`` to the explorer (so its guard can
    treat the IdP POST as a login domain).
"""
from app.clients.explorer_client import ExploreDispatchRequest
from app.routers.explorations import _allowlist_domains, _idp_domains


def test_idp_domains_join_the_egress_allowlist():
    fences = {"allowed_hosts": [".acme-life.example"], "idp_domains": ["okta.com", "login.microsoftonline.com"]}
    allowed = _allowlist_domains("https://app.acme-life.example/", fences)
    assert ".acme-life.example" in allowed            # the app fence is preserved
    assert "okta.com" in allowed                       # SSO IdP can be reached
    assert "login.microsoftonline.com" in allowed


def test_allowlist_without_idp_is_unchanged():
    # no idp_domains ⇒ byte-identical to the pre-#7 allowlist (just the app host).
    assert _allowlist_domains("https://app.acme-life.example/", {}) == ["app.acme-life.example"]


def test_idp_domains_helper_cleans_the_declared_list():
    assert _idp_domains({"idp_domains": [" okta.com ", "", "  "]}) == ["okta.com"]
    assert _idp_domains({}) == []


def test_dispatch_request_relays_idp_domains_to_the_explorer():
    req = ExploreDispatchRequest(
        crawl_id="c1", tenant_id="t1", exploration_id="e1",
        target_url="https://app.acme-life.example/",
        idp_domains=["okta.com"],
    )
    assert req.model_dump()["idp_domains"] == ["okta.com"]
