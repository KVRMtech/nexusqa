"""R2 — env_kind write-validation + the UAT kind (requirements-audit findings).

Before: the _ENV_KINDS vocabulary existed but was never enforced, so a typo
('disposible') stored silently and degraded to prod at read time — refusals
with no clue why. And 'uat' did not exist as a kind (the requirement's
Test/UAT/Prod selector) — it inherited prod posture.
"""
import pytest
from fastapi import HTTPException

from app.routers.apps import _ENV_KINDS, _validated_env_kind
from app.security.prod_guard import NON_PROD_ENV_KINDS, resolve_effective_fences


def test_vocabulary_includes_uat_and_all_kinds():
    assert _ENV_KINDS == {"prod", "staging", "uat", "disposable"}


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
