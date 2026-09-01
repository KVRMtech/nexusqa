"""P5 — attestation server: the accountable human is bound to the authenticated
identity, and the crawl-gate status + unmet reasons are derivable for the app UI."""
from __future__ import annotations

from types import SimpleNamespace

from app.routers.apps import _finalize_attestation
from app.security import prod_guard


# ── _finalize_attestation: bind attested_by / signed_by to user['sub'] ──────────

def test_empty_attestation_is_noop():
    assert _finalize_attestation(None, {"sub": "u@x"}) == {}
    assert _finalize_attestation({}, {"sub": "u@x"}) == {}


def test_blank_attested_by_stamped_from_authenticated_user():
    out = _finalize_attestation({"env_kind": "disposable"}, {"sub": "founder@x"})
    assert out["attested_by"] == "founder@x"


def test_explicit_attested_by_is_preserved():
    out = _finalize_attestation({"attested_by": "someone@else"}, {"sub": "founder@x"})
    assert out["attested_by"] == "someone@else"


def test_roe_signed_by_stamped_when_signed_and_blank():
    out = _finalize_attestation({"rules_of_engagement": {"signed": True}}, {"sub": "founder@x"})
    assert out["rules_of_engagement"]["signed_by"] == "founder@x"
    assert out["rules_of_engagement"]["signed"] is True


def test_roe_signed_by_preserved_when_present():
    out = _finalize_attestation(
        {"rules_of_engagement": {"signed": True, "signed_by": "a@b"}}, {"sub": "founder@x"})
    assert out["rules_of_engagement"]["signed_by"] == "a@b"


def test_unsigned_roe_is_not_stamped():
    out = _finalize_attestation({"rules_of_engagement": {"signed": False}}, {"sub": "founder@x"})
    assert "signed_by" not in (out.get("rules_of_engagement") or {})


def test_no_user_identity_is_noop():
    out = _finalize_attestation({"env_kind": "disposable"}, {})
    assert not out.get("attested_by")


# ── the crawl-gate status/reasons the app UI now surfaces (via _public_view) ────

def test_draft_app_is_not_ready_with_named_reasons():
    row = SimpleNamespace(env_attestation={"env_kind": "disposable"})
    ok, reasons = prod_guard.onboarding_ready(row)
    assert ok is False
    # RoE-not-signed + preflight-not-passed must both be named (auditable refusal).
    assert any("rules-of-engagement" in r.lower() for r in reasons)
    assert any("preflight" in r.lower() for r in reasons)
    assert prod_guard.onboarding_status(row) == "draft"


def test_authorized_by_is_bound_to_jwt_subject_overwriting_client():
    out = _finalize_attestation(
        {"env_kind": "disposable",
         "authorization": {"authorized": True, "authorized_by": "spoofed@evil"}},
        {"sub": "founder@x"},
    )
    assert out["authorization"]["authorized_by"] == "founder@x"


def test_authorization_not_asserted_is_left_untouched():
    out = _finalize_attestation(
        {"env_kind": "disposable", "authorization": {"authorized": False}},
        {"sub": "founder@x"},
    )
    assert not (out.get("authorization") or {}).get("authorized_by")
