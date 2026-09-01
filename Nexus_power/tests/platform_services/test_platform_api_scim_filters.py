"""SCIM filter parser — pure unit tests.

Imports happen inside test functions so the ``_isolate_app_module``
autouse fixture in this directory's conftest can swap the ``app``
package to the platform-api copy without leaking the path to other
service tests that import a different ``app``.
"""

from __future__ import annotations

import pytest


def test_empty_filter_matches_everything() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter(None)
    assert f.matches({}) is True
    assert f.matches({"userName": "alice"}) is True
    assert f.clauses == ()


def test_eq_filter() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('userName eq "alice"')
    assert f.matches({"userName": "alice"})
    assert not f.matches({"userName": "bob"})


def test_ne_filter() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('userName ne "alice"')
    assert not f.matches({"userName": "alice"})
    assert f.matches({"userName": "bob"})


def test_co_is_case_insensitive() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('displayName co "JON"')
    assert f.matches({"displayName": "Jonathan Smith"})
    assert not f.matches({"displayName": "Alice"})


def test_sw_is_case_insensitive() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('userName sw "ad"')
    assert f.matches({"userName": "admin"})
    assert not f.matches({"userName": "alice"})


def test_pr_present() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter("title pr")
    assert f.matches({"title": "Manager"})
    assert not f.matches({"title": ""})
    assert not f.matches({})


def test_and_combines_clauses() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('userName eq "alice" and active eq "true"')
    assert f.connective == "and"
    assert f.matches({"userName": "alice", "active": "true"})
    assert not f.matches({"userName": "alice", "active": "false"})
    assert not f.matches({"userName": "bob", "active": "true"})


def test_or_combines_clauses() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('userName eq "alice" or userName eq "bob"')
    assert f.connective == "or"
    assert f.matches({"userName": "alice"})
    assert f.matches({"userName": "bob"})
    assert not f.matches({"userName": "charlie"})


def test_dotted_path_emails_value() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('emails.value eq "alice@example.com"')
    assert f.matches({"emails": [{"value": "alice@example.com"}]})
    assert not f.matches({"emails": [{"value": "bob@example.com"}]})


def test_urn_prefix_stripped() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter(
        'urn:ietf:params:scim:schemas:core:2.0:User:userName eq "alice"'
    )
    assert f.matches({"userName": "alice"})


def test_quoted_value_with_special_chars() -> None:
    from app.scim import parse_scim_filter

    f = parse_scim_filter('displayName eq "O\\"Hara"')
    assert f.matches({"displayName": 'O"Hara'})


def test_mixed_and_or_rejected() -> None:
    from app.scim import SCIMError, parse_scim_filter

    with pytest.raises(SCIMError) as exc:
        parse_scim_filter('a eq "1" and b eq "2" or c eq "3"')
    assert exc.value.scim_type == "invalidFilter"


def test_unknown_operator_rejected() -> None:
    from app.scim import SCIMError, parse_scim_filter

    with pytest.raises(SCIMError):
        parse_scim_filter('userName gt "alice"')


def test_missing_value_rejected() -> None:
    from app.scim import SCIMError, parse_scim_filter

    with pytest.raises(SCIMError):
        parse_scim_filter("userName eq")


def test_unexpected_connective_rejected() -> None:
    from app.scim import SCIMError, parse_scim_filter

    with pytest.raises(SCIMError):
        parse_scim_filter("and userName eq \"alice\"")
