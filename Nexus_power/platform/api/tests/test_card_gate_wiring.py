"""F3 + F4 are WIRED, not merely written.

The modules are unit-tested elsewhere (test_card_state, test_login_probe). This
pins that the run path actually consults them, in the right order, and that the
paths which used to bypass the contract no longer do.

Order matters twice over:
  * the F4 refusal must sit BEFORE `acquire_reservation`, or a blocked run leaves a
    member held by a reservation nothing will release;
  * the F3 stamp must sit under `proven`, not under `ok`.
"""
import types

import pytest

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_STORE = open("app/services/test_factory/persona_store.py", encoding="utf-8").read()


def _seg(text: str, start: str, end: str | None = None) -> str:
    s = text[text.index(start):]
    return s[:s.index(end)] if end and end in s else s


# ── F4: the refusal is reached, and reached early ────────────────────────────

def test_the_run_path_consults_the_card_state_derivation():
    assert "from ..services.test_factory import card_state" in _ROUTER
    assert "_persona_auth_resolve(" in _ROUTER
    seg = _seg(_ROUTER, "if persona_id:\n        _auth = await _persona_auth_resolve")
    assert '_state.get("runnable", True)' in seg[:3000]
    assert '"blocked_reason": "credential"' in seg[:3000]


def test_the_block_executes_zero_scripts_and_never_blames_the_app():
    seg = _seg(_ROUTER, '"blocked_reason": "credential"')
    assert '"scripts": 0' in seg[:1500]
    assert "NOT an application failure" in seg[:1500]


def test_the_refusal_happens_BEFORE_a_reservation_is_taken():
    """A block after acquire_reservation leaves the member held by a reservation
    that nothing releases — the run returned, so no completion callback fires."""
    body = _seg(_ROUTER, "if persona_id:\n        _auth = await _persona_auth_resolve")
    assert body.index('"blocked_reason": "credential"') < body.index("acquire_reservation(")


def test_the_state_is_derived_from_the_DECRYPTED_card_not_the_stored_names():
    """The stored slot_names list cannot show that a slot holds an empty value, and
    an empty secret is typed into the field and fails as though the app were broken."""
    seg = _seg(_ROUTER, "async def _persona_auth_resolve(", "async def _persona_auth_bundle(")
    assert "live_slot_values=" in seg
    assert "get_persona_credential(" in seg


def test_the_gate_defaults_ON_and_can_be_turned_off_without_a_rebuild(monkeypatch):
    """Unlike F1's routing flag, this one is ON by default: it can only refuse a run
    that would otherwise have executed logged out and blamed the application."""
    from app.routers import test_factory as tf
    monkeypatch.delenv("NEXUS_CARD_HEALTH_GATE", raising=False)
    assert tf._card_gate_enabled() is True
    for off in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("NEXUS_CARD_HEALTH_GATE", off)
        assert tf._card_gate_enabled() is False, off
    for on in ("1", "true", "yes", "on", ""):
        monkeypatch.setenv("NEXUS_CARD_HEALTH_GATE", on)
        assert tf._card_gate_enabled() is True, on


# ── F3: the stamp follows the proof, not the attempt ─────────────────────────

def test_the_preflight_reads_the_outcome_instead_of_matching_one_substring():
    assert "from ..services.test_factory import login_probe" in _ROUTER
    seg = _seg(_ROUTER, "async def _persona_preflight(", "_RUNNER_JOBS: dict")
    assert "login_probe.read_outcome(output)" in seg
    # the old rule must be gone from this path
    assert '("recipe login OK" in output)' not in seg


def test_ok_and_proven_are_kept_distinct_by_the_preflight():
    """`ok` = the attempt did not fail (a run may proceed). `proven` = the recorded
    landing was reached (a card may be stamped). Collapsing them is the defect."""
    seg = _seg(_ROUTER, "async def _persona_preflight(", "_RUNNER_JOBS: dict")
    assert '"ok": v["outcome"] in (login_probe.PROVEN, login_probe.STEPS_ONLY)' in seg
    assert '"proven": v["proven"]' in seg


def test_the_verify_probe_also_stamps_the_CARD_so_verify_is_a_real_action():
    """F3 asks for a per-card Verify. The recipe probe already takes persona_id +
    environment_id and drives a real login, so it IS that action once it stamps the
    card — one probe path, one doctrine, rather than a second half-parallel one."""
    seg = _seg(_ROUTER, "async def verify_recipe_endpoint", "# ── Persona diff")
    assert "stamp_card_verified(" in seg
    # a proof and a card-attributed failure are both recorded; anything else leaves
    # the card alone rather than overwriting it with a claim in either direction
    assert "verified=True" in seg and "verified=False" in seg
    assert 'if v["proven"]:' in seg
    assert 'elif v["attribution"] == "credential":' in seg


def test_the_verify_probe_proves_the_card_WHERE_the_proof_is_claimed():
    """A card is per (member, ENVIRONMENT) and this endpoint stamps it. Defaulting
    the destination to the crawled origin meant a card could be marked 'proven for
    prod' by a login replayed against whatever host the crawl used — and it would
    carry prod's decrypted secrets there to do it."""
    seg = _seg(_ROUTER, "async def verify_recipe_endpoint", "# ── Persona diff")
    assert "environment_routing.resolve_destination(" in seg
    assert seg.index("environment_routing.resolve_destination(") < seg.index("_configured_files(")
    assert 'cannot establish where to verify this card' in seg


def test_a_proof_records_WHICH_login_version_it_proved():
    """Without it, `ready` is unreachable: recipe_version would only ever hold the
    PROVISIONING version, so after any re-record every card is permanently
    proof_superseded and proving it again changes nothing."""
    seg = _seg(_STORE, "async def stamp_card_verified(", "async def all_credential_status(")
    assert "recipe_version" in seg
    # …but never on a FAILURE: a failed attempt says nothing about which login the
    # card belongs to, and re-pointing it would adopt a recipe it never passed.
    assert "if verified and recipe_version is not None:" in seg


def test_a_recipe_cannot_ship_a_checkpoint_that_checks_nothing():
    """'home reached' is the only token that can stamp proven, and the interpreter
    prints it for an assert_home step carrying no signal — it awaits nothing and
    cannot throw. The recorder refuses to emit one; this closes the API door."""
    assert "card_contract.check_recipe_steps(" in _ROUTER
    contract = open("app/services/test_factory/card_contract.py", encoding="utf-8").read()
    assert "signal_less_home_oracle" in contract


def test_the_environment_health_probe_reads_the_same_signal_as_the_gate():
    """Health is not cosmetic — gate_dispatch REFUSES a known-bad environment. This
    branch was the last holder of the old substring test."""
    seg = _seg(_ROUTER, "env-health-probe")
    assert "login_probe.read_outcome(output)" in seg[:2000]
    assert '"recipe login OK" in output' not in seg[:2000]


# ── F4: a new recipe version withdraws what it invalidated ───────────────────

def test_every_recipe_save_path_withdraws_card_proofs():
    """save_recipe supersedes the active version. Each of the three paths that can
    mint version > 1 must tell the cards."""
    assert _ROUTER.count("withdraw_card_proofs(") == 3
    for anchor in ("source=\"login_recording\"",          # hand-recorded in the runner
                   "source=body.source",                   # explicit re-record / qe-central
                   "source=\"crawl_demonstration\""):      # from the crawler's observation
        seg = _seg(_ROUTER, anchor)
        assert "withdraw_card_proofs(" in seg[:1400], anchor


def test_the_withdrawal_diffs_against_the_STEPS_derived_slots():
    """Diffing the declared list would let a hand-edited recipe pass and still skip
    the login — the same reason check_card derives from the steps."""
    assert _ROUTER.count("card_contract.required_slots(") >= 3


def test_the_store_withdraws_the_proof_and_names_who_broke():
    assert "async def withdraw_card_proofs(" in _STORE
    seg = _seg(_STORE, "async def withdraw_card_proofs(", "async def get_recipe(")
    assert 'r.verify_status = "unverified"' in seg
    assert "broken" in seg and "unproven" in seg
    # tenant + artifact scoped, never a cross-tenant sweep
    assert "_artifact_persona_ids(" in seg
    assert "TpPersonaCredentialRow.tenant_id == tenant_id" in seg


# ── the paths that used to bypass the contract ───────────────────────────────

def test_bulk_import_enforces_the_same_card_contract_as_the_single_PUT():
    """Bulk import is exactly where a column-mapping mistake produces 500 cards that
    all save cleanly and all silently skip the login."""
    seg = _seg(_ROUTER, "credentials_bulk_imported")
    seg = _ROUTER[_ROUTER.index("async def bulk_import") if "async def bulk_import" in _ROUTER
                  else _ROUTER.index("imported = 0"):]
    assert "card_contract.check_card(" in seg[:2500]
    assert "login_type_key=" in seg[:2500] and "recipe_version=" in seg[:2500]


def test_the_bundle_declares_every_slot_the_STEPS_fill():
    """The interpreter's missing-credential guard only inspects the slots the config
    declares. A step filling a slot absent from the declaration produced no env key
    AND no warning — the field was filled with '' and the login failed as though the
    application were broken."""
    seg = _seg(_STORE, "def build_persona_bundle(")
    assert "card_contract.required_slots(recipe)" in seg
    # and an unclassified slot is never emitted 'plain', which the guard exempts
    assert '"secret"' in seg


def test_the_manifest_carries_the_state_the_gate_will_apply():
    """Otherwise an orphaned card looks healthy on screen right up until a run is
    blocked."""
    seg = _seg(_ROUTER, "async def credentials_manifest_endpoint")
    assert "card_state.evaluate(" in seg[:3000]
    assert 'c["state"]' in seg[:3000]


# ── one doctrine for staleness, not two ──────────────────────────────────────

def test_a_blank_verified_epoch_is_not_a_free_pass_anywhere():
    """persona_scale and flag_stale_cards both exempted a blank verified_epoch, so
    every card predating the P5 migration claimed freshness forever — and disagreed
    with the gate that actually stops runs."""
    scale = open("app/services/test_factory/persona_scale.py", encoding="utf-8").read()
    assert "if env_epoch and env_epoch != verified_epoch:" in scale
    assert "if env_epoch and verified_epoch and env_epoch != verified_epoch:" not in scale
    seg = _seg(_STORE, "def flag_stale_cards(" if "def flag_stale_cards(" in _STORE
               else 'prev = getattr(r, "verified_epoch"')
    assert 'if r.verify_status == "verified" and epoch and prev != epoch:' in seg
