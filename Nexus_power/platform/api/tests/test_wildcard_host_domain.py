"""A wildcard-host service is not a customer's domain.

`login_domain` is half of the reuse key that decides whether a recorded login is
PROPOSED to another application. Reducing `vkpowerlife.136-85-106-73.sslip.io` to
`sslip.io` puts every unrelated app on that service under one domain — so one
customer's login recipe could be offered for another customer's application.

For services where anyone may claim any subdomain, the full host IS the identity.
Over-specific keying costs a missed reuse offer; over-general keying makes a wrong
one. Only one of those is safe.

The two implementations (platform-api's login_observation and qe-explorer's guard)
feed the SAME login_type_key and must agree exactly, so they are tested together.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]

from app.services.test_factory.login_observation import registrable_domain as rd_api

# qe-explorer is loaded BY PATH and its directory is deliberately NOT added to
# sys.path. This monorepo has ~20 sibling top-level `app` packages and sys.modules
# has one slot for that name; putting qe-explorer on the path makes `app.services`
# resolve to the wrong service for the rest of the process.
_GUARD = None


def _rd_guard(host):
    global _GUARD
    if _GUARD is None:
        import importlib.util
        path = _REPO / "engines" / "qe-explorer" / "app" / "guard.py"
        spec = importlib.util.spec_from_file_location("qe_explorer_guard_ut", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["qe_explorer_guard_ut"] = mod
        spec.loader.exec_module(mod)
        _GUARD = mod
    return _GUARD.registrable_domain(host)


# ── the defect ───────────────────────────────────────────────────────────────

def test_a_wildcard_host_keeps_its_FULL_host():
    """THE DEFECT. This is the real host this product was demonstrated on."""
    host = "vkpowerlife.136-85-106-73.sslip.io"
    assert rd_api(host) == host
    assert rd_api(host) != "sslip.io"


def test_two_unrelated_apps_on_the_same_service_do_not_share_a_domain():
    a = rd_api("customer-a.10-0-0-1.nip.io")
    b = rd_api("customer-b.10-0-0-2.nip.io")
    assert a != b


def test_two_unrelated_apps_on_the_same_PaaS_do_not_share_a_domain():
    assert rd_api("acme-portal.herokuapp.com") != rd_api("other-corp.herokuapp.com")
    assert rd_api("acme.vercel.app") != rd_api("rival.vercel.app")
    assert rd_api("acme.github.io") != rd_api("rival.github.io")


# ── ordinary domains are unchanged ───────────────────────────────────────────

def test_a_normal_domain_still_reduces_to_eTLD_plus_one():
    assert rd_api("login.acme.com") == "acme.com"
    assert rd_api("www.portal.acme.com") == "acme.com"


def test_multi_part_public_suffixes_still_work():
    assert rd_api("login.acme.co.uk") == "acme.co.uk"
    assert rd_api("secure.bank.com.au") == "bank.com.au"


def test_ip_literals_and_short_hosts_are_untouched():
    for host in ("136.85.106.73", "localhost", "acme.com", "[::1]"):
        assert rd_api(host) == host


def test_a_port_is_stripped_before_anything_else():
    assert rd_api("login.acme.com:8443") == "acme.com"
    assert rd_api("app.1-2-3-4.sslip.io:8080") == "app.1-2-3-4.sslip.io"


# ── the two implementations must not drift ───────────────────────────────────

def test_both_implementations_agree_exactly():
    """They feed the SAME login_type_key. If they disagree, a login recorded by the
    crawler and the same login recorded by auth-capture key differently, and reuse
    silently stops matching."""
    hosts = [
        "vkpowerlife.136-85-106-73.sslip.io", "customer-a.10-0-0-1.nip.io",
        "acme-portal.herokuapp.com", "acme.github.io", "login.acme.com",
        "www.portal.acme.com", "login.acme.co.uk", "secure.bank.com.au",
        "136.85.106.73", "localhost", "acme.com", "login.acme.com:8443",
        "deep.sub.domain.example.org", "a.b.c.d.e.co.jp", "",
    ]
    for h in hosts:
        assert rd_api(h) == _rd_guard(h), h


def test_the_wildcard_sets_are_identical_in_both_files():
    api = open("app/services/test_factory/login_observation.py", encoding="utf-8").read()
    guard = open(str(_REPO / "engines" / "qe-explorer" / "app" / "guard.py"), encoding="utf-8").read()
    def _entries(src):
        blk = src[src.index("_WILDCARD_HOST_SUFFIXES = frozenset({"):]
        blk = blk[:blk.index("})")]
        return sorted(w.strip().strip('",') for w in blk.split() if '"' in w)
    assert _entries(api) == _entries(guard)


def test_nothing_here_reaches_the_network():
    """On-prem: domain resolution must never depend on fetching a suffix list."""
    src = open("app/services/test_factory/login_observation.py", encoding="utf-8").read()
    for forbidden in ("requests", "httpx", "urlopen", "tldextract"):
        assert forbidden not in src, forbidden
