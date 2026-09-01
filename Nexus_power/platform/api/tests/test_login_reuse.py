"""Reuse-decision brain (Phase 5) — 'record once, reuse fleet-wide'.

Pins the pure decision that avoids re-recording a login the fleet already has:
observed form -> reuse exact match or record; bare known domain -> reuse the one
type or ask (dotcom vs portal); nothing -> record. No DB — pure functions.
"""
from app.services.test_factory import login_fingerprint as fp

PORTAL = dict(domain="usaa.com", login_path="/portal/login",
              fields=[{"name": "member_number", "type": "text"},
                      {"name": "password", "type": "password"},
                      {"name": "pin", "type": "text"}],
              submit="Continue")
DOTCOM = dict(domain="usaa.com", login_path="/login",
              fields=[{"name": "email", "type": "email"},
                      {"name": "password", "type": "password"}],
              submit="Log on")


def _entry(spec, recipe_id):
    d = fp.login_type_descriptor(**spec)
    d["recipe_id"] = recipe_id
    return d


PORTAL_ENTRY = _entry(PORTAL, "rec_portal")
DOTCOM_ENTRY = _entry(DOTCOM, "rec_dotcom")
LIBRARY = [PORTAL_ENTRY, DOTCOM_ENTRY]


def test_observed_form_reuses_exact_match():
    # A new app whose crawled portal form matches -> reuse, no re-record.
    out = fp.propose_reuse(**PORTAL, library=LIBRARY)
    assert out["action"] == "reuse"
    assert out["recipe"]["recipe_id"] == "rec_portal"


def test_observed_form_no_match_records_fresh():
    novel = dict(domain="newco.com", login_path="/signin",
                 fields=[{"name": "username", "type": "text"},
                         {"name": "password", "type": "password"}],
                 submit="Go")
    out = fp.propose_reuse(**novel, library=LIBRARY)
    assert out["action"] == "record"
    assert out["key"] == fp.login_type_key(**novel)


def test_dotcom_form_is_not_matched_to_portal_on_same_host():
    out = fp.propose_reuse(**DOTCOM, library=LIBRARY)
    assert out["action"] == "reuse"
    assert out["recipe"]["recipe_id"] == "rec_dotcom"  # NOT rec_portal


def test_bare_domain_with_multiple_types_asks():
    # Onboarding a new app on usaa.com with no form yet -> ask dotcom vs portal.
    out = fp.propose_reuse(domain="usaa.com", library=LIBRARY)
    assert out["action"] == "disambiguate"
    assert {o["recipe_id"] for o in out["options"]} == {"rec_portal", "rec_dotcom"}


def test_bare_domain_with_one_type_reuses_it():
    single = [PORTAL_ENTRY]
    out = fp.propose_reuse(domain="usaa.com", library=single)
    assert out["action"] == "reuse"
    assert out["recipe"]["recipe_id"] == "rec_portal"


def test_bare_unknown_domain_records():
    out = fp.propose_reuse(domain="brand-new.com", library=LIBRARY)
    assert out["action"] == "record"


def test_match_helpers():
    assert fp.match_by_key(PORTAL_ENTRY["key"], LIBRARY)["recipe_id"] == "rec_portal"
    assert fp.match_by_key("lt_nope", LIBRARY) is None
    assert {e["recipe_id"] for e in fp.candidates_by_domain("usaa.com", LIBRARY)} == {
        "rec_portal", "rec_dotcom"}
    assert fp.candidates_by_domain("other.com", LIBRARY) == []
