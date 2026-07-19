"""QE-Central Phase-6 SAFETY SPINE — production onboarding guard tests (pure).

Pins the fail-closed foundation that must hold BEFORE any real client app is
crawled/cycled (``app/security/prod_guard.py``).  Pure functions over a fake
``client_apps`` row — no network, no database:

  * a non-live (draft/attested) app is REFUSED a cycle (409);
  * a live app (signed RoE + non-prod attestation + passed preflight) PROCEEDS;
  * an EXPLORE crawl without a valid non-prod attestation is REFUSED;
  * a SUBMIT phase without a disposable-env attestation + per-flow approval is
    REFUSED (422), and is ALLOWED once both are present;
  * the DEFAULT for a real app is fail-closed — a dev/test explicit bypass flag
    is honoured ONLY in a dev/test env and NEVER in staging/production;
  * the Phase-0 inline-bundle harness/synthetic path is NOT gated (structural).
"""
from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.db import utc_now
from app.security import prod_guard
from app.security.prod_guard import (
    ONBOARDING_ATTESTED,
    ONBOARDING_DRAFT,
    ONBOARDING_LIVE,
    OnboardingRefused,
    assert_crawlable,
    attestation_present,
    onboarding_ready,
    onboarding_status,
)


# ── fixtures / builders ─────────────────────────────────────────────────────

def _future_iso(hours: int = 24) -> str:
    return (utc_now() + timedelta(hours=hours)).isoformat()


def _past_iso(hours: int = 1) -> str:
    return (utc_now() - timedelta(hours=hours)).isoformat()


def _signed_roe() -> dict:
    return {"signed": True, "signed_by": "ciso@client.example", "version": "roe-v1"}


def _attestation(env_kind: str = "disposable", *, expires: str | None = None) -> dict:
    return {
        "env_kind": env_kind,
        "attested_by": "sre@client.example",
        "attested_at": _past_iso(2),
        "expires_at": expires if expires is not None else _future_iso(24),
        "reset_procedure": "terraform destroy && apply",
        # Authorization to test the target URL (owns / permitted) — the liability gate.
        "authorization": {"authorized": True, "authorized_by": "ciso@client.example"},
    }


def _app(
    *,
    env_attestation: dict | None = None,
    fences: dict | None = None,
    schedule: dict | None = None,
    status: str = "active",
    app_id: str = "app-1",
) -> SimpleNamespace:
    """A minimal stand-in for a ``ClientAppRow`` (prod_guard reads by attribute)."""
    return SimpleNamespace(
        app_id=app_id,
        env_attestation=dict(env_attestation or {}),
        fences=dict(fences or {}),
        schedule=dict(schedule or {}),
        status=status,
    )


def _live_app(**overrides) -> SimpleNamespace:
    """An onboarding-``live`` app: signed RoE + disposable attestation + preflight."""
    att = _attestation("disposable")
    att["rules_of_engagement"] = _signed_roe()
    att["preflight"] = {"passed": True, "at": _past_iso(1)}
    base = dict(env_attestation=att)
    base.update(overrides)
    return _app(**base)


# ── onboarding state machine ────────────────────────────────────────────────

def test_bare_app_is_draft_and_refused_a_cycle():
    app = _app()
    assert onboarding_status(app) == ONBOARDING_DRAFT
    ok, reasons = onboarding_ready(app)
    assert ok is False
    assert reasons  # every unmet requirement is named
    with pytest.raises(OnboardingRefused) as exc:
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")
    assert exc.value.status_code == 409
    assert exc.value.phase == prod_guard.PHASE_EXPLORE


def test_status_progresses_draft_attested_live():
    # draft: nothing signed/attested.
    assert onboarding_status(_app()) == ONBOARDING_DRAFT

    # attested: signed RoE + valid attestation, but preflight NOT passed.
    att = _attestation("disposable")
    att["rules_of_engagement"] = _signed_roe()
    attested = _app(env_attestation=att)
    assert onboarding_status(attested) == ONBOARDING_ATTESTED
    assert onboarding_ready(attested)[0] is False  # not live yet → not crawlable

    # live: + passed preflight.
    live = _live_app()
    assert onboarding_status(live) == ONBOARDING_LIVE


def test_crawl_refused_without_authorization_attestation():
    # An otherwise-live app (RoE + attestation + preflight) is STILL refused until the
    # operator attests they own / are permitted to test the URL — the liability gate.
    app = _live_app()
    del app.env_attestation["authorization"]
    ok, reasons = onboarding_ready(app)
    assert ok is False
    assert any("authoriz" in r.lower() for r in reasons)
    with pytest.raises(prod_guard.OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")
    # A blank authorized_by (unattributed) does not count.
    app.env_attestation["authorization"] = {"authorized": True, "authorized_by": ""}
    assert onboarding_ready(app)[0] is False


def test_live_app_proceeds_for_explore():
    app = _live_app()
    ok, reasons = onboarding_ready(app)
    assert ok is True and reasons == []
    # No raise — the app is cleared to crawl (assert in every env).
    assert assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production") is None
    assert attestation_present(app, phase=prod_guard.PHASE_EXPLORE) is True


def test_attested_but_no_preflight_is_409_not_live():
    att = _attestation("disposable")
    att["rules_of_engagement"] = _signed_roe()
    app = _app(env_attestation=att)  # attested, preflight missing
    with pytest.raises(OnboardingRefused) as exc:
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")
    assert exc.value.status_code == 409  # not onboarding-live
    assert any("preflight" in r.lower() for r in exc.value.reasons)


# ── EXPLORE attestation gate ────────────────────────────────────────────────

def test_explore_without_attestation_refused():
    # Signed RoE + preflight, but NO attestation envelope at all.
    app = _app(env_attestation={
        "rules_of_engagement": _signed_roe(),
        "preflight": {"passed": True},
    })
    assert attestation_present(app, phase=prod_guard.PHASE_EXPLORE) is False
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")


def test_expired_attestation_refused():
    att = _attestation("disposable", expires=_past_iso(1))
    att["rules_of_engagement"] = _signed_roe()
    att["preflight"] = {"passed": True}
    app = _app(env_attestation=att)
    assert attestation_present(app, phase=prod_guard.PHASE_EXPLORE) is False
    assert onboarding_status(app) != ONBOARDING_LIVE
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")


def test_prod_env_kind_is_never_a_valid_crawl_target():
    # A 'prod' attestation is attested + unexpired but is NOT a non-prod kind.
    att = _attestation("prod")
    att["rules_of_engagement"] = _signed_roe()
    att["preflight"] = {"passed": True}
    app = _app(env_attestation=att)
    assert attestation_present(app, phase=prod_guard.PHASE_EXPLORE) is False
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")


def test_staging_is_a_valid_nonprod_explore_target():
    att = _attestation("staging")
    att["rules_of_engagement"] = _signed_roe()
    att["preflight"] = {"passed": True}
    app = _app(env_attestation=att)
    assert onboarding_status(app) == ONBOARDING_LIVE
    assert attestation_present(app, phase=prod_guard.PHASE_EXPLORE) is True
    assert assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production") is None


# ── SUBMIT attestation gate (disposable + per-flow approval) ────────────────

def test_submit_requires_disposable_env_not_just_staging():
    # A live app on a STAGING env is crawlable for EXPLORE but NOT submit-capable.
    att = _attestation("staging")
    att["rules_of_engagement"] = _signed_roe()
    att["preflight"] = {"passed": True}
    app = _app(env_attestation=att, fences={"allow_submit": True,
                                            "submit_approvals": ["flow-a"]})
    assert attestation_present(app, phase=prod_guard.PHASE_SUBMIT) is False
    with pytest.raises(OnboardingRefused) as exc:
        assert_crawlable(app, phase=prod_guard.PHASE_SUBMIT, env="production")
    # onboarding is LIVE, so the only unmet thing is the submit attestation → 422.
    assert exc.value.status_code == 422
    assert exc.value.phase == prod_guard.PHASE_SUBMIT


def test_submit_without_approval_refused():
    # Disposable + live, but no per-flow approval (fences.allow_submit unset).
    app = _live_app(fences={})
    assert attestation_present(app, phase=prod_guard.PHASE_SUBMIT) is False
    with pytest.raises(OnboardingRefused) as exc:
        assert_crawlable(app, phase=prod_guard.PHASE_SUBMIT, env="production")
    assert exc.value.status_code == 422


def test_submit_allow_submit_true_but_empty_approvals_refused():
    app = _live_app(fences={"allow_submit": True, "submit_approvals": []})
    assert attestation_present(app, phase=prod_guard.PHASE_SUBMIT) is False


def test_submit_with_disposable_and_approval_ok():
    app = _live_app(fences={"allow_submit": True, "submit_approvals": ["flow-a"]})
    assert attestation_present(app, phase=prod_guard.PHASE_SUBMIT) is True
    assert assert_crawlable(app, phase=prod_guard.PHASE_SUBMIT, env="production") is None


# ── dev/test bypass — honoured ONLY in dev/test, NEVER in staging/prod ──────

def test_dev_bypass_flag_allows_in_test_env():
    app = _app(fences={"onboarding_test_bypass": True})  # draft app + bypass flag
    # Explicit bypass in a development/test env lets an existing fixture proceed.
    assert assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="test") is None
    assert assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="development") is None


def test_bypass_flag_is_never_honoured_in_production():
    app = _app(fences={"onboarding_test_bypass": True})
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="production")
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="staging")


def test_default_is_fail_closed_even_in_test_without_bypass():
    # A real (non-live) app with NO bypass flag is refused even in a dev/test env.
    app = _app()
    with pytest.raises(OnboardingRefused):
        assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="test")


def test_bypass_attestation_location_and_string_flag():
    # The bypass flag is accepted in env_attestation too, and as an affirmative str.
    app = _app(env_attestation={"test_bypass": "true"})
    assert assert_crawlable(app, phase=prod_guard.PHASE_EXPLORE, env="dev") is None


# ── refusal payload + truthiness hardening ──────────────────────────────────

def test_refusal_http_detail_shape():
    exc = OnboardingRefused(status_code=409, phase="explore", reasons=["a", "b"])
    detail = exc.as_http_detail()
    assert detail["refused"] is True
    assert detail["reason"] == "onboarding_not_ready"
    assert detail["phase"] == "explore"
    assert detail["reasons"] == ["a", "b"]
    assert "message" in detail


def test_stray_false_string_flags_do_not_read_truthy():
    # A signed:"false" RoE and preflight passed:"no" must NOT satisfy the gate.
    att = _attestation("disposable")
    att["rules_of_engagement"] = {"signed": "false", "signed_by": "x"}
    att["preflight"] = {"passed": "no"}
    app = _app(env_attestation=att)
    ok, _reasons = onboarding_ready(app)
    assert ok is False


def test_roe_requires_a_named_signer():
    att = _attestation("disposable")
    att["rules_of_engagement"] = {"signed": True, "signed_by": ""}  # no signer
    att["preflight"] = {"passed": True}
    app = _app(env_attestation=att)
    assert onboarding_status(app) != ONBOARDING_LIVE


# ── structural: the Phase-0 harness/synthetic path is NOT gated ─────────────

def test_phase0_inline_bundle_path_is_not_gated_but_phase1_dispatch_is():
    """The synthetic Phase-0 inline-bundle write never touches the crawl gate;
    only the real-app Phase-1 dispatch does (design: gate the real-app path only)."""
    from app.routers import explorations as ex

    dispatch_src = inspect.getsource(ex._dispatch_explorer)
    inline_src = inspect.getsource(ex._write_inline_bundle)
    assert "assert_crawlable" in dispatch_src, "real-app dispatch must be gated"
    assert "assert_crawlable" not in inline_src, "Phase-0 harness path must NOT be gated"


def test_cycle_router_and_driver_are_wired_to_the_guard():
    from app.routers import cycles as cyc
    from app.controlplane.cycle import driver as drv

    assert "assert_crawlable" in inspect.getsource(cyc.create_cycle)
    assert "assert_crawlable" in inspect.getsource(drv.create_cycle)
    # The daemon skips a non-onboarded app cleanly (never crashes the tick).
    assert "OnboardingRefused" in inspect.getsource(drv._fire_cycle)


# ── resolve_effective_fences (multi-env Phase 2) ───────────────────────────
from app.security.prod_guard import resolve_effective_fences


def test_fences_unset_env_is_byte_identical_to_app():
    app = {"allow_submit": True, "max_rps": 2.0, "blackout_windows": ["x"]}
    assert resolve_effective_fences(app, None, env_kind="disposable") == app
    assert resolve_effective_fences(app, {}, env_kind="disposable") == app


def test_prod_forces_read_only_even_if_profile_claims_submit():
    # a mis-attested prod profile that CLAIMS allow_submit=true is still forced off
    eff = resolve_effective_fences(
        {"allow_submit": True}, {"allow_submit": True}, env_kind="prod")
    assert eff["allow_submit"] is False


def test_disposable_env_may_bind_when_allowed():
    eff = resolve_effective_fences(
        {"allow_submit": False}, {"allow_submit": True}, env_kind="disposable")
    assert eff["allow_submit"] is True   # env overlay enables it in a disposable env


def test_env_overlay_overrides_app_fences():
    eff = resolve_effective_fences(
        {"max_rps": 1.0, "allow_submit": True}, {"max_rps": 5.0}, env_kind="staging")
    assert eff["max_rps"] == 5.0 and eff["allow_submit"] is True


def test_irreversible_verbs_union_across_app_and_env():
    eff = resolve_effective_fences(
        {"irreversible_verbs_extra": ["bind"]},
        {"irreversible_verbs_extra": ["charge", "bind"]}, env_kind="staging")
    assert eff["irreversible_verbs_extra"] == ["bind", "charge"]


def test_prod_override_wins_over_verbs_and_overlay():
    eff = resolve_effective_fences(
        {"allow_submit": True, "max_rps": 1.0},
        {"allow_submit": True, "max_rps": 9.0, "irreversible_verbs_extra": ["bind"]},
        env_kind="prod")
    assert eff["allow_submit"] is False        # prod hard override
    assert eff["max_rps"] == 9.0               # non-safety overlays still apply
    assert eff["irreversible_verbs_extra"] == ["bind"]


def test_prod_override_fail_closed_on_mislabeled_or_blank_env_kind():
    # 'production' (not the literal 'prod'), a blank kind, and unknown labels ALL
    # force allow_submit off — a mislabeled/unattested prod env can never stay mutable.
    for kind in ["production", "PRD", "", "prod-us", "unknown", "PROD"]:
        eff = resolve_effective_fences(
            {"allow_submit": True}, {"allow_submit": True}, env_kind=kind)
        assert eff["allow_submit"] is False, f"env_kind={kind!r} should fail closed"
