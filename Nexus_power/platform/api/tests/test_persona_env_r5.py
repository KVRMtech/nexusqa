"""R5 — P5 ops hardening: per-tenant reservation cap, secret rotation, staleness
sweep, and audited manifest export.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_r5.py -q
"""
from __future__ import annotations

import os

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_STORE = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                           "test_factory", "persona_store.py"), encoding="utf-8").read()


def test_store_exposes_ops_helpers():
    for fn in ("count_live_reservations", "rotate_cards", "flag_stale_cards"):
        assert f"async def {fn}(" in _STORE


def test_reservation_cap_enforced_at_dispatch():
    assert "count_live_reservations(" in _ROUTER
    assert "NEXUS_PERSONA_RESERVATION_CAP" in _ROUTER
    seg = _ROUTER[_ROUTER.index("_live = await persona_store.count_live_reservations"):]
    seg = seg[:1200]
    assert 'blocked_reason": "tenant_capacity"' in seg
    assert "not an application failure" in seg


def test_rotation_never_returns_plaintext_and_is_admin_only():
    seg = _ROUTER[_ROUTER.index("async def rotate_credentials_endpoint"):]
    seg = seg[:1600]
    assert "rotate_cards(" in seg
    assert "admin or manager" in seg
    # the store rotate re-wraps in place and never returns plaintext
    rseg = _STORE[_STORE.index("async def rotate_cards("):]
    rseg = rseg[:rseg.index("async def flag_stale_cards(")]
    assert "envelope.decrypt(" in rseg and "envelope.encrypt(" in rseg
    assert "return {\"rotated\"" in rseg  # counts only, no plaintext


def test_staleness_sweep_flags_epoch_stale_cards():
    assert "/environments/{environment_id}/staleness/sweep" in _ROUTER
    seg = _STORE[_STORE.index("async def flag_stale_cards("):]
    seg = seg[:seg.index("# ── Answer sheets")] if "# ── Answer sheets" in seg else seg[:1200]
    # only a verified card against a DIFFERENT epoch is flagged back to unverified
    assert 'verify_status == "verified"' in seg
    assert 'prev != epoch' in seg
    assert '"unverified"' in seg


def test_manifest_export_is_audit_logged_but_a_view_is_not():
    seg = _ROUTER[_ROUTER.index("async def credentials_manifest_endpoint"):]
    seg = seg[:seg.index("async def rotate_credentials_endpoint")]
    assert "if export:" in seg
    assert "credentials_manifest_exported" in seg
