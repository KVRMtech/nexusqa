"""Phase-B submit ENABLEMENT (make it live). Two defects fixed together in the
crawl dispatch: (1) the operator's approved flow names must flow to the explorer,
and (2) the stored env_attestation must be mapped onto the explorer's STRICT
Attestation shape (else it fails validation and every submit is refused). Both
values come from the app's real stored config — never hardcoded."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.db import utc_now
from app.routers.explorations import _explorer_attestation
from app.security import prod_guard


def _future_iso(h: int = 24) -> str:
    return (utc_now() + timedelta(hours=h)).isoformat()


def _past_iso(h: int = 1) -> str:
    return (utc_now() - timedelta(hours=h)).isoformat()


def _disposable_att(*, env_kind: str = "disposable", expires: str | None = None) -> dict:
    return {
        "env_kind": env_kind,
        "attested_by": "sre@client.example",
        "attested_at": _past_iso(2),
        "expires_at": expires or _future_iso(24),
        "reset_procedure": "terraform destroy && apply",
        "rules_of_engagement": {"signed": True, "signed_by": "ciso@client.example"},
        "preflight": {"passed": True, "at": _past_iso(1)},
    }


def _app(*, env_attestation: dict | None = None, fences: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(app_id="a1", env_attestation=dict(env_attestation or {}),
                           fences=dict(fences or {}), schedule={}, status="active")


# ── prod_guard.submit_approvals — fail-closed, from real config ─────────────────

def test_disposable_env_gets_the_blanket_with_no_fences():
    # Founder/client direction: a Test/disposable env has NO submission limits.
    # A valid disposable attestation ALONE grants the blanket "*"; naming each
    # control one at a time is no longer required.
    assert prod_guard.submit_approvals(_app(env_attestation=_disposable_att())) == ["*"]


def test_disposable_blanket_appends_the_operator_named_list():
    app = _app(env_attestation=_disposable_att(),
               fences={"allow_submit": True, "submit_approvals": ["Get a Quote", "Submit application"]})
    assert prod_guard.submit_approvals(app) == ["*", "Get a Quote", "Submit application"]


def test_disposable_blanket_ignores_the_allow_submit_toggle():
    # allow_submit is a live-env ceremony; on a disposable env the attestation
    # already authorises submission, so the toggle no longer gates it.
    app = _app(env_attestation=_disposable_att(),
               fences={"allow_submit": False, "submit_approvals": ["Place order"]})
    assert prod_guard.submit_approvals(app) == ["*", "Place order"]


def test_staging_env_never_gets_approvals_even_with_allow_submit():
    app = _app(env_attestation=_disposable_att(env_kind="staging"),
               fences={"allow_submit": True, "submit_approvals": ["Continue"]})
    assert prod_guard.submit_approvals(app) == []   # a mutating submit needs DISPOSABLE


def test_expired_attestation_yields_no_approvals():
    app = _app(env_attestation=_disposable_att(expires=_past_iso(1)),
               fences={"allow_submit": True, "submit_approvals": ["Continue"]})
    assert prod_guard.submit_approvals(app) == []


def test_approvals_fall_back_to_env_attestation_list():
    att = _disposable_att()
    att["submit_approvals"] = ["Submit application"]
    app = _app(env_attestation=att, fences={"allow_submit": True})
    assert prod_guard.submit_approvals(app) == ["*", "Submit application"]


# ── _explorer_attestation — STRICT shape, verbatim values ───────────────────────

_EXPLORER_KEYS = {"attested_by", "env_kind", "reset_procedure", "expires_at_ms"}


def test_maps_stored_attestation_to_the_explorer_strict_shape():
    out = _explorer_attestation(_disposable_att())
    assert out is not None
    assert set(out) <= _EXPLORER_KEYS            # nothing the extra='forbid' model rejects
    assert out["attested_by"] == "sre@client.example"
    assert out["env_kind"] == "disposable"
    assert isinstance(out["expires_at_ms"], int) and out["expires_at_ms"] > 0


def test_drops_every_extra_stored_key_that_would_fail_validation():
    att = _disposable_att()
    att["submit_approvals"] = ["x"]
    out = _explorer_attestation(att)
    for k in ("attested_at", "rules_of_engagement", "preflight", "submit_approvals", "expires_at"):
        assert k not in out


def test_none_without_an_attested_by():
    assert _explorer_attestation({"env_kind": "disposable"}) is None
    assert _explorer_attestation(None) is None
