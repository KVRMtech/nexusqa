"""The canonical session-substance predicate.

The one rule, in one place — the bug it prevents (dropping a sessionStorage-only
session) was independently reintroduced four times before this module existed, so
its behaviour is pinned here rather than left to each caller's copy.
"""
from nexus_sdk.session import SESSION_SUBSTANCE_KEYS, session_has_substance


def test_cookies_are_substance():
    assert session_has_substance({"cookies": [{"name": "sid"}]}) is True


def test_origins_are_substance():
    assert session_has_substance({"origins": [{"origin": "https://x"}]}) is True


def test_session_storage_only_is_substance():
    # The whole point: no cookies, no origins, sign-in lives in sessionStorage.
    state = {"__nx_session_storage": [{"origin": "https://x", "entries": {"t": "1"}}]}
    assert session_has_substance(state) is True


def test_all_empty_is_not_substance():
    assert session_has_substance({"cookies": [], "origins": [], "__nx_session_storage": []}) is False


def test_null_valued_keys_are_not_substance():
    assert session_has_substance({"cookies": None, "origins": None}) is False


def test_non_dict_inputs_are_not_substance():
    for bad in (None, "nope", 42, [], object()):
        assert session_has_substance(bad) is False


def test_empty_dict_is_not_substance():
    assert session_has_substance({}) is False


def test_the_three_keys_are_the_contract():
    # Guards against a caller's copy diverging on which keys count.
    assert SESSION_SUBSTANCE_KEYS == ("cookies", "origins", "__nx_session_storage")
