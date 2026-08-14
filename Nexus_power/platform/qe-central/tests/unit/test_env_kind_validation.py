"""R2 — env_kind write-validation + the UAT kind (requirements-audit findings).

Before: the _ENV_KINDS vocabulary existed but was never enforced, so a typo
('disposible') stored silently and degraded to prod at read time — refusals
with no clue why. And 'uat' did not exist as a kind (the requirement's
Test/UAT/Prod selector) — it inherited prod posture.
"""
import pytest
from fastapi import HTTPException

from app.routers.apps import _ENV_KINDS, _validated_env_kind
from app.security.prod_guard import (
    CRAWLABLE_ENV_KINDS,
    ENV_KIND_PRODUCTION_TEST,
    NON_PROD_ENV_KINDS,
    resolve_effective_fences,
)


def test_the_writable_vocabulary_IS_the_enforceable_one():
    """THE INVARIANT, not a copy of the list.

    This test previously pinned a hand-written literal, and the two vocabularies
    drifted underneath it: ``production_test`` was a first-class crawlable kind
    in prod_guard while the write path 422-rejected it, so a posture the
    enforcer fully understood could never be STORED. Pinning the literal is what
    let that pass — the copy was self-consistent and wrong.

    What must hold is the RELATIONSHIP: anything the enforcer can act on must be
    storable, and nothing else.
    """
    assert _ENV_KINDS == frozenset(CRAWLABLE_ENV_KINDS)


def test_production_test_is_storable_now_that_it_is_enforceable():
    """The concrete case the divergence blocked."""
    assert ENV_KIND_PRODUCTION_TEST in _ENV_KINDS
    att = {"env_kind": ENV_KIND_PRODUCTION_TEST}
    assert _validated_env_kind(att) is att


def test_unknown_kind_is_422_at_write_time():
    with pytest.raises(HTTPException) as exc:
        _validated_env_kind({"env_kind": "disposible"})   # the typo class
    assert exc.value.status_code == 422
    assert "disposible" in str(exc.value.detail)


def test_known_kinds_and_blank_pass_through():
    for kind in ("prod", "staging", "uat", "disposable", "UAT", " Staging "):
        att = {"env_kind": kind}
        assert _validated_env_kind(att) is att
    assert _validated_env_kind({}) == {}
    assert _validated_env_kind(None) is None
    assert _validated_env_kind({"env_kind": ""}) is not None  # blank allowed (degrades honestly)


def test_uat_is_non_prod_but_never_submit_tier():
    assert "uat" in NON_PROD_ENV_KINDS                      # crawlable
    eff = resolve_effective_fences(
        {"allow_submit": True}, {}, env_kind="uat")
    assert eff.get("allow_submit") is True or "allow_submit" not in eff, \
        "uat postures as staging for the fences resolver (non-prod)"
    eff_prod = resolve_effective_fences(
        {"allow_submit": True}, {}, env_kind="prod")
    assert eff_prod.get("allow_submit") is False, "prod always forces submit off"
    eff_typo = resolve_effective_fences(
        {"allow_submit": True}, {}, env_kind="disposible")
    assert eff_typo.get("allow_submit") is False, "unknown kind degrades to prod"
