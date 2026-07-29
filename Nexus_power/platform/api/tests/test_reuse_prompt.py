"""Reuse proposal at onboarding — don't make the next tester record (Phase 8).

At onboarding only the base URL is known. The login_type_key is a hash of the form
shape, so it cannot be computed before a form has been seen — which is precisely
when the proposal is worth making. Matching therefore falls back to the DOMAIN the
recipe was recorded against, and the decision is delegated to propose_reuse:

    one recorded login on that host   -> reuse it
    several                           -> ask which (a public login and a member
                                         portal on one host are not the same login)
    none                              -> record

Pure — no DB, no live stack.
"""
from app.services.test_factory import login_fingerprint as lf


def _recipe(key, domain, recipe_id):
    return {"recipe_id": recipe_id, "key": key, "domain": domain,
            "login_type_key": key, "login_domain": domain}


def test_a_known_host_with_one_recorded_login_is_offered_for_reuse():
    lib = [_recipe("lt_aaa", "example.com", "r1")]
    got = lf.propose_reuse(domain="example.com", library=lib)
    assert got["action"] == "reuse"
    assert got["recipe"]["recipe_id"] == "r1"


def test_a_known_host_with_several_logins_asks_which():
    """A public login and a member portal on one host are genuinely different —
    silently picking one would hand the tester the wrong choreography."""
    lib = [_recipe("lt_aaa", "example.com", "r1"),
           _recipe("lt_bbb", "example.com", "r2")]
    got = lf.propose_reuse(domain="example.com", library=lib)
    assert got["action"] == "disambiguate"
    assert {o["recipe_id"] for o in got["options"]} == {"r1", "r2"}


def test_an_unknown_host_records_fresh():
    lib = [_recipe("lt_aaa", "example.com", "r1")]
    assert lf.propose_reuse(domain="other-co.com", library=lib)["action"] == "record"


def test_an_empty_library_records_fresh():
    assert lf.propose_reuse(domain="example.com", library=[])["action"] == "record"
    assert lf.propose_reuse(domain="example.com", library=None)["action"] == "record"


def test_a_recipe_recorded_before_the_domain_column_is_never_proposed():
    """An empty domain must match nothing — proposing it would offer a recipe we
    cannot show was recorded against this host."""
    lib = [_recipe("lt_aaa", "", "legacy")]
    assert lf.propose_reuse(domain="example.com", library=lib)["action"] == "record"


def test_once_a_form_IS_seen_the_exact_key_decides():
    """Post-recording the form shape is known, so matching tightens from the host
    to the exact login type — two different logins on one host stop colliding."""
    fields = [{"name": "member_number"}, {"name": "password"}]
    key = lf.login_type_key(domain="example.com", login_path="/login",
                            fields=fields, submit="Sign in")
    lib = [_recipe(key, "example.com", "r1")]
    got = lf.propose_reuse(domain="example.com", login_path="/login",
                           fields=fields, submit="Sign in", library=lib)
    assert got["action"] == "reuse" and got["recipe"]["recipe_id"] == "r1"

    other = lf.propose_reuse(domain="example.com", login_path="/login",
                             fields=[{"name": "email"}, {"name": "password"}],
                             submit="Sign in", library=lib)
    assert other["action"] == "record"
