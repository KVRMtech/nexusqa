"""P0 — Persona × Environment foundation: store logic + bundle + migration.

Pins the properties that must hold before any later phase:
  * a credential card is envelope-encrypted and AAD-bound (never plaintext);
  * build_persona_bundle keeps secrets OUT of the config and in the run env;
  * the reservation model allows exactly one live hold;
  * the migration is append/update-only (no DELETE) with RLS.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p0.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_store as ps

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_SQL = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "apply_persona_env.sql"), encoding="utf-8").read()


# ── build_persona_bundle: the recipe→(config, env) split ─────────────────────

def test_bundle_keeps_secrets_out_of_the_config():
    recipe = {"login_path": "/login",
              "steps": [{"action": "fill", "slot": "member_number"},
                        {"action": "fill", "slot": "pin"}],
              "slots": [{"name": "member_number", "type": "secret"},
                        {"name": "pin", "type": "fixed_code"}]}
    values = {"member_number": "4429871", "pin": "000000"}
    config, env = ps.build_persona_bundle(recipe, values)
    assert config["strategy"] == "recipe"
    blob = str(config)
    assert "4429871" not in blob and "000000" not in blob, "secrets must not enter the bundle"
    assert env["NEXUS_LOGIN_MEMBER_NUMBER"] == "4429871"
    assert env["NEXUS_LOGIN_PIN"] == "000000"
    # slot metadata (non-secret) rides in the config for the interpreter
    names = {s["name"] for s in config["slots"]}
    assert names == {"member_number", "pin"}


def test_bundle_is_none_without_a_recipe():
    assert ps.build_persona_bundle(None, {}) == (None, {})
    assert ps.build_persona_bundle({"steps": []}, {"x": "y"}) == (None, {})


def test_bundle_handles_multi_screen_and_arbitrary_slots():
    """USAA shape: member number → next → password + PIN. No fixed field set."""
    recipe = {"steps": [{"action": "fill", "slot": "member_number"},
                        {"action": "click"},
                        {"action": "fill", "slot": "password"},
                        {"action": "fill", "slot": "pin"}],
              "slots": [{"name": "member_number"}, {"name": "password"}, {"name": "pin"}]}
    config, env = ps.build_persona_bundle(recipe, {"member_number": "m", "password": "p", "pin": "1"})
    assert set(env) == {"NEXUS_LOGIN_MEMBER_NUMBER", "NEXUS_LOGIN_PASSWORD", "NEXUS_LOGIN_PIN"}


def test_card_aad_binds_persona_and_environment():
    a = ps._card_aad("p1", "uat")
    b = ps._card_aad("p1", "cit")
    c = ps._card_aad("p2", "uat")
    assert a != b and a != c, "a card must not be replayable across env or persona"


# ── migration safety ─────────────────────────────────────────────────────────

def test_migration_creates_all_six_tables_idempotently():
    for t in ("tp_login_recipes", "tp_personas", "tp_persona_credentials",
              "tp_persona_expected_values", "tp_value_classifications",
              "tp_persona_reservations"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in _SQL


def test_migration_is_append_or_update_never_deletable_by_the_app():
    assert "GRANT SELECT, INSERT, UPDATE" in _SQL
    for forbidden in ("GRANT DELETE", "GRANT ALL"):
        assert forbidden not in _SQL
    assert "ENABLE ROW LEVEL SECURITY" in _SQL


def test_reservation_has_a_single_live_hold_constraint():
    assert "ux_persona_reservation_live" in _SQL
    assert "WHERE released_at IS NULL" in _SQL


def test_credentials_table_only_ever_holds_ciphertext():
    # the column is BYTEA and the store encrypts before insert
    assert "blob             BYTEA        NOT NULL" in _SQL
    src = open(ps.__file__, encoding="utf-8").read()
    assert "await envelope.encrypt(" in src
    assert "refusing to store credentials in plaintext" in src


# ── endpoints wired + gated ──────────────────────────────────────────────────

def test_registry_endpoints_exist_and_writes_are_role_gated():
    for path in ("/personas", "/recipes",
                 "/credentials/{environment_id}",
                 "/expected-values/{environment_id}", "/classifications"):
        assert path in _ROUTER
    assert "_persona_write_ok(user)" in _ROUTER
    # credential writes return slot NAMES, never the values (the store shapes this)
    src = open(ps.__file__, encoding="utf-8").read()
    assert '"slot_names": slot_names' in src
    # the write endpoint returns the store output, which carries slot_names only
    assert "return {\"status\": \"saved\", **out}" in _ROUTER


def test_credential_endpoint_requires_encryption():
    assert "encryption unavailable — cannot store credentials securely" in _ROUTER


def test_legacy_form_login_is_surfaced_as_persona_zero():
    src = open(ps.__file__, encoding="utf-8").read()
    assert "_legacy_persona0" in src
    assert '"name": "default"' in src
    assert "is_recording_baseline" in src
