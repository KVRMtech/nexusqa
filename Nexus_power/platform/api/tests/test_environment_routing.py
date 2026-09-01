"""Environment routing — selecting an environment must decide WHERE the run goes.

F1 of MEMBERS_ENVIRONMENTS_DESIGN, and the most serious defect found in the
member/environment review: `environment_id` picked the credential card, the posture,
the reservation and the member-data answers, while the ADDRESS came from the request
body — which the portal defaults to the crawled origin.

The consequence is not a routing inconvenience. Posture is enforced from the
registered row, so production correctly marked default-deny is consulted only when
its LABEL is selected. Select `uat` with the address still on the production host and
the guard reads uat's posture, allows the run, and a mutating suite executes against
production while every downstream claim says uat.

Pure — no DB, no live stack.
"""
from app.services.test_factory import environment_routing as er


UAT = {"environment_id": "uat", "base_url": "https://uat.example.com",
       "posture": "read_write", "is_production": False}
PROD = {"environment_id": "prod", "base_url": "https://www.example.com",
        "posture": "read_only", "is_production": True, "write_authorized": False}


# ── the environment decides ──────────────────────────────────────────────────

def test_a_registered_environment_sets_the_destination():
    out = er.resolve_destination(environment_id="uat", environment=UAT)
    assert out["allowed"] is True
    assert out["base_url"] == "https://uat.example.com"
    assert out["source"] == "environment"
    assert out["env_context"]["environment_id"] == "uat"


def test_routing_cookies_and_headers_travel_with_it():
    """A cookie-selected lane on a shared host: dropping the cookie lands the run on
    the host's default, which for these estates is production."""
    env = dict(UAT, cookies=[{"name": "x-env", "value": "dev"}],
               headers={"X-Lane": "dev"}, env_assertion={"url_pattern": "/uat/"})
    ctx = er.resolve_destination(environment_id="uat", environment=env)["env_context"]
    assert ctx["cookies"] == [{"name": "x-env", "value": "dev"}]
    assert ctx["headers"] == {"X-Lane": "dev"}
    assert ctx["env_assertion"] == {"url_pattern": "/uat/"}


def test_the_crawled_origin_no_longer_wins_over_the_selected_environment():
    """THE DEFECT, pinned. The request carried the crawled (production) origin while
    the operator selected uat; the run used the request."""
    out = er.resolve_destination(
        environment_id="uat", environment=UAT,
        requested_base_url="")          # portal default cleared by the caller
    assert out["base_url"] == "https://uat.example.com"
    assert "www.example.com" not in out["base_url"]


# ── conflicts are refused, never silently resolved ───────────────────────────

def test_an_address_that_disagrees_with_the_environment_is_REFUSED():
    """Preferring the environment silently would make a pinned address a lie;
    preferring the request silently is the original defect. Refuse instead."""
    out = er.resolve_destination(
        environment_id="uat", environment=UAT,
        requested_base_url="https://www.example.com/portal")
    assert out["allowed"] is False
    assert out["reason"] == "destination_conflicts_with_environment"
    assert out["detail"]["registered_origin"] == "https://uat.example.com"
    assert out["detail"]["requested_origin"] == "https://www.example.com"


def test_the_production_case_this_exists_to_prevent():
    """prod is registered default-deny. Selecting uat while the address points at the
    production host must NOT proceed — that is how a mutating suite reached
    production while the posture gate read uat's read_write."""
    out = er.resolve_destination(
        environment_id="uat", environment=UAT,
        requested_base_url=PROD["base_url"])
    assert out["allowed"] is False


def test_a_matching_address_is_allowed_and_the_environment_still_wins():
    """Same host, different path — not a conflict. The suite may enter deeper than
    the environment's declared base."""
    out = er.resolve_destination(
        environment_id="uat", environment=UAT,
        requested_base_url="https://uat.example.com/portal/apply")
    assert out["allowed"] is True
    assert out["base_url"] == "https://uat.example.com"
    assert out["source"] == "environment"


def test_env_context_base_url_is_treated_as_a_requested_address_too():
    out = er.resolve_destination(
        environment_id="uat", environment=UAT,
        requested_env_context={"base_url": "https://www.example.com"})
    assert out["allowed"] is False
    assert out["reason"] == "destination_conflicts_with_environment"


# ── unknown targets are refused, not defaulted ───────────────────────────────

def test_an_unregistered_environment_is_refused_not_defaulted():
    """Defaulting an unknown id to read_write / non-production is the most permissive
    reading of a target nobody described — and the report would name an environment
    that does not exist."""
    out = er.resolve_destination(environment_id="uat", environment=None)
    assert out["allowed"] is False
    assert out["reason"] == "environment_not_registered"
    assert out["detail"]["environment_id"] == "uat"


def test_a_registered_environment_with_no_base_url_keeps_the_request():
    """Posture still governs; the environment simply never claimed an address. The
    half-described state is surfaced rather than hidden."""
    out = er.resolve_destination(
        environment_id="uat", environment={"environment_id": "uat", "posture": "read_only"},
        requested_base_url="https://uat.example.com")
    assert out["allowed"] is True
    assert out["base_url"] == "https://uat.example.com"
    assert out["source"] == "request"
    assert out["reason"] == "environment_has_no_base_url"


# ── no environment selected: unchanged behaviour ─────────────────────────────

def test_no_environment_selected_is_byte_identical_to_today():
    out = er.resolve_destination(
        environment_id="", environment=None,
        requested_base_url="https://anything.example.com")
    assert out["allowed"] is True
    assert out["base_url"] == "https://anything.example.com"
    assert out["source"] == "request"
    assert out["environment_id"] == ""


def test_no_environment_and_no_address_is_still_allowed():
    """The recorded-origin fallback lives downstream; routing must not block it."""
    out = er.resolve_destination(environment_id="", environment=None)
    assert out["allowed"] is True
    assert out["base_url"] == ""
    assert out["source"] == "none"


# ── origin comparison ────────────────────────────────────────────────────────

def test_origin_comparison_is_host_level_and_scheme_sensitive():
    assert er.same_origin("https://a.example.com/x", "https://a.example.com/y")
    assert not er.same_origin("https://a.example.com", "https://b.example.com")
    assert not er.same_origin("http://a.example.com", "https://a.example.com")
    assert not er.same_origin("https://a.example.com", "https://a.example.com:8443")


def test_a_non_http_or_malformed_url_never_counts_as_a_match():
    for bad in ("", None, "not-a-url", "javascript:alert(1)", "file:///etc/passwd"):
        assert er.origin_of(bad) == ""
        assert not er.same_origin(bad, "https://uat.example.com")


def test_a_malformed_requested_address_does_not_silently_pass_the_conflict_check():
    """An unparseable address contributes no origin, so it cannot 'agree' with the
    environment — the environment simply wins."""
    out = er.resolve_destination(
        environment_id="uat", environment=UAT, requested_base_url="not-a-url")
    assert out["allowed"] is True
    assert out["base_url"] == "https://uat.example.com"


# ── the flag: off means the previous behaviour, exactly ──────────────────────

def test_routing_is_off_unless_explicitly_enabled(monkeypatch):
    """Default OFF. An operator who has not opted in keeps today's behaviour, and a
    live box reverts by unsetting the variable — no rebuild."""
    from app.routers import test_factory as tf

    monkeypatch.delenv("NEXUS_ENV_ROUTING", raising=False)
    assert tf._env_routing_enabled() is False
    for off in ("", "0", "false", "no", "off", "  "):
        monkeypatch.setenv("NEXUS_ENV_ROUTING", off)
        assert tf._env_routing_enabled() is False, off
    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("NEXUS_ENV_ROUTING", on)
        assert tf._env_routing_enabled() is True, on


def test_the_dispatch_path_imports_the_resolver():
    from app.routers.test_factory import environment_routing as wired
    assert callable(wired.resolve_destination)
