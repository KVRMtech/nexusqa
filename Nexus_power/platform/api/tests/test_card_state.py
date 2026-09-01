"""Can this member log into this environment, and do we KNOW it or only hope so?

F3 + F4. One derivation behind three consumers: the dispatch gate that refuses a
run, the badge that claims a card is proven, and the member x environment cell.

The distinction the whole thing turns on:

  * slots CHANGED  -> BLOCK. The card cannot fill the login. Not blocking means the
    compiled globalSetup skips the entire login (compiler.py:2137), the suite runs
    logged out, and every failure is attributed to the application under test.
  * proof EXPIRED  -> runnable, badge withdrawn. The values may still be right;
    what expired is our evidence, not the card.

Collapsing those either blocks work that would have succeeded, or keeps claiming
proof we no longer have.

Pure - no DB, no live stack.
"""
import pytest

from app.services.test_factory import card_state as cs


def _recipe(slots=("email", "password"), version=3):
    return {
        "version": version, "login_type_key": "lt_abc",
        "steps": [{"action": "goto", "path": "/login"}]
             + [{"action": "fill", "slot": s, "label": s.title()} for s in slots]
             + [{"action": "click", "name": "Sign in"}],
        "slots": [{"name": s, "type": "secret"} for s in slots],
    }


def _card(slots=("email", "password"), *, status="verified", version=3,
          epoch="", present=True):
    return {"present": present, "slot_names": list(slots), "verify_status": status,
            "recipe_version": version, "verified_epoch": epoch,
            "last_verified_at": "2026-08-01T00:00:00+00:00"}


# ── the block ────────────────────────────────────────────────────────────────

def test_a_card_whose_slots_no_longer_match_is_BLOCKED():
    """THE F4 DEFECT. A re-record renamed the slot; the card still looks healthy and
    the run would have executed logged out."""
    v = cs.evaluate(recipe=_recipe(("member_id", "password")), card=_card(("member_number", "password")))
    assert v["state"] == cs.STALE_SLOTS
    assert v["runnable"] is False
    assert v["reason"] == "card_slots_do_not_match_recipe"
    assert v["missing"] == ["member_id"]
    assert v["unexpected"] == ["member_number"]


def test_the_block_says_what_to_do_and_never_blames_the_application():
    v = cs.evaluate(recipe=_recipe(("policy_no", "pin")), card=_card(("policy_no",)))
    note = v["detail"]["note"]
    assert "BLOCKED" in note
    assert "logged out" in note and "attribute every failure to the application" in note
    assert "policy_no, pin" in note          # what to re-enter


def test_an_ADDED_slot_blocks():
    """A login that gained a PIN step: the card covers less than the login needs."""
    v = cs.evaluate(recipe=_recipe(("email", "password", "pin")), card=_card(("email", "password")))
    assert v["state"] == cs.STALE_SLOTS
    assert v["missing"] == ["pin"]


def test_a_REMOVED_slot_does_NOT_block():
    """Deliberately asymmetric with the WRITE-time contract, and the asymmetry is the
    point. At provisioning time an unexpected name means the operator is filling a
    different login and there is no cost to saying so. At RUN time the interpreter
    reads only the slots the recipe declares and ignores any other value, so a
    leftover from a previous recording still logs in — blocking it would refuse the
    whole fleet the moment a login DROPPED a field, stopping work that would have
    succeeded. Reported, not refused."""
    v = cs.evaluate(recipe=_recipe(("email",)), card=_card(("email", "password")))
    assert v["runnable"] is True
    assert v["reason"] == "card_has_extra_slots"
    assert v["unexpected"] == ["password"]
    assert v["proven"] is False


def test_no_card_blocks_and_names_what_is_needed():
    v = cs.evaluate(recipe=_recipe(), card=None)
    assert v["state"] == cs.NO_CARD
    assert v["runnable"] is False
    assert v["required"] == ["email", "password"]


def test_a_legacy_form_login_stands_in_for_a_card():
    """Every estate predating credential cards would otherwise be blocked on a
    configuration that works today."""
    v = cs.evaluate(recipe=_recipe(), card=None, legacy_login_available=True)
    assert v["runnable"] is True
    assert v["reason"] == "legacy_form_login"
    assert v["proven"] is False


def test_a_legacy_form_login_runs_even_with_NO_recipe_at_all():
    """THE OVER-BLOCK. persona-0 estates have a stored form login and no recipe —
    that IS their login. Judging the recipe questions first would refuse every one
    of them, on a configuration that works today. The gate defaults ON, so this
    would have stopped real work the moment it shipped."""
    v = cs.evaluate(recipe=None, card=None, legacy_login_available=True)
    assert v["runnable"] is True
    assert v["reason"] == "legacy_form_login"


def test_a_legacy_login_is_never_slot_diffed_against_a_recipe_it_never_saw():
    v = cs.evaluate(recipe=_recipe(("policy_no", "pin")),
                    card={"present": False}, legacy_login_available=True)
    assert v["state"] != cs.STALE_SLOTS
    assert v["runnable"] is True


def test_no_recipe_is_reported_as_a_recipe_problem_not_a_card_one():
    """Saying 'no card' here sends the operator to the wrong screen."""
    v = cs.evaluate(recipe=None, card=_card())
    assert v["state"] == cs.NO_RECIPE
    assert v["runnable"] is False


# ── the live card beats the stored name list ─────────────────────────────────

def test_an_EMPTY_stored_value_is_caught_only_by_the_live_check():
    """The stored slot_names list says the slot exists; it cannot say the value is
    blank. A blank secret gets typed into the field and the login fails as though
    the application were broken."""
    stored = _card(("email", "password"))
    assert cs.evaluate(recipe=_recipe(), card=stored)["runnable"] is True
    live = cs.evaluate(recipe=_recipe(), card=stored,
                       live_slot_values={"email": "a@b.c", "password": "   "})
    assert live["state"] == cs.STALE_SLOTS
    assert live["missing"] == ["password"]


def test_the_live_check_also_catches_a_None_value():
    v = cs.evaluate(recipe=_recipe(), card=_card(),
                    live_slot_values={"email": "a@b.c", "password": None})
    assert v["missing"] == ["password"]


def test_a_live_card_that_covers_is_allowed():
    v = cs.evaluate(recipe=_recipe(), card=_card(),
                    live_slot_values={"email": "a@b.c", "password": "s3cret"})
    assert v["runnable"] is True


def test_no_credential_VALUE_ever_appears_in_the_verdict():
    v = cs.evaluate(recipe=_recipe(), card=_card(),
                    live_slot_values={"email": "a@b.c", "password": "SUPERSECRET"})
    assert "SUPERSECRET" not in repr(v)


# ── the proof ────────────────────────────────────────────────────────────────

def test_a_card_proven_against_the_current_login_is_ready():
    v = cs.evaluate(recipe=_recipe(version=3), card=_card(version=3))
    assert v["state"] == cs.READY
    assert v["proven"] is True and v["runnable"] is True


def test_a_never_proven_card_is_RUNNABLE_because_the_first_run_is_the_proof():
    """Blocking here would make 'verified' unreachable for every new card."""
    v = cs.evaluate(recipe=_recipe(), card=_card(status="unverified"))
    assert v["state"] == cs.UNPROVEN
    assert v["runnable"] is True
    assert v["proven"] is False
    assert v["reason"] == "never_proven"


def test_a_card_proven_against_a_SUPERSEDED_recipe_is_no_longer_proven():
    """F3 rule (b): the login changed, so an older proof is not evidence about it.
    The slots still match, so the run may proceed - only the claim is withdrawn."""
    v = cs.evaluate(recipe=_recipe(version=4), card=_card(version=3))
    assert v["reason"] == "proof_superseded"
    assert v["proven"] is False
    assert v["runnable"] is True


def test_a_card_proven_before_the_contract_existed_cannot_claim_the_current_login():
    """recipe_version 0 = provisioned before cards recorded what they were checked
    against. Bulk-imported cards land exactly here."""
    v = cs.evaluate(recipe=_recipe(), card=_card(version=0))
    assert v["reason"] == "proof_predates_contract"
    assert v["proven"] is False


def test_a_rolled_data_epoch_withdraws_the_proof():
    """F3 rule (c). The member's account may not exist in the new snapshot."""
    v = cs.evaluate(recipe=_recipe(), card=_card(epoch="2026-07"),
                    environment={"data_epoch": "2026-08"})
    assert v["reason"] == "proof_stale_epoch"
    assert v["proven"] is False and v["runnable"] is True


def test_a_BLANK_verified_epoch_is_not_a_free_pass():
    """A card proven while the environment carried no epoch label is not proven
    against the labelled snapshot running now. Exempting blanks is what let every
    pre-P5 card claim freshness forever."""
    v = cs.evaluate(recipe=_recipe(), card=_card(epoch=""),
                    environment={"data_epoch": "2026-08"})
    assert v["proven"] is False
    assert v["reason"] == "proof_stale_epoch"


def test_an_environment_with_no_epoch_does_not_invent_staleness():
    v = cs.evaluate(recipe=_recipe(), card=_card(epoch="2026-07"), environment={})
    assert v["state"] == cs.READY


def test_a_FAILED_proof_is_surfaced_and_not_confused_with_never_tried():
    """The panel rendered 'failed' and 'unverified' in the same grey badge."""
    v = cs.evaluate(recipe=_recipe(), card=_card(status="failed"))
    assert v["reason"] == "proof_failed"
    assert v["proven"] is False and v["runnable"] is True


def test_slot_mismatch_outranks_every_proof_question():
    """A card that cannot fill the login is blocked no matter how recently it was
    proven - the proof was about a different login."""
    v = cs.evaluate(recipe=_recipe(("a", "b"), version=9),
                    card=_card(("a",), status="verified", version=9, epoch="x"),
                    environment={"data_epoch": "x"})
    assert v["state"] == cs.STALE_SLOTS


# ── the fingerprint must NOT be the break detector ───────────────────────────

def test_a_DIFFERENT_login_type_key_is_not_treated_as_breakage():
    """A card is per ENVIRONMENT and environments live on different hosts, so a
    different login fingerprint is the normal case across a matrix row. Worse, the
    fingerprint strips dynamic tokens before hashing, so 'member_number_9f3a1c22'
    and 'member_number_7b21ee40' share a key while being different slot names.
    Keying breakage on it would both over- and under-fire."""
    recipe = dict(_recipe(), login_type_key="lt_uat")
    card = dict(_card(), login_type_key="lt_prod")
    assert cs.evaluate(recipe=recipe, card=card)["state"] == cs.READY


# ── generic: no vocabulary is assumed ────────────────────────────────────────

def test_any_applications_own_field_names_work():
    for names in (("member_number", "password", "pin"), ("email", "password"),
                  ("policy_no", "password"), ("mobile", "otp"), ("mrn", "password")):
        assert cs.evaluate(recipe=_recipe(names), card=_card(names))["state"] == cs.READY
        assert cs.evaluate(recipe=_recipe(names),
                           card=_card(names[:-1]))["state"] == cs.STALE_SLOTS


def test_only_ready_and_unproven_are_runnable():
    """Pins the runnable set so a new state cannot accidentally become dispatchable."""
    runnable = set()
    for v in (cs.evaluate(recipe=_recipe(), card=_card()),
              cs.evaluate(recipe=_recipe(), card=_card(status="unverified")),
              cs.evaluate(recipe=_recipe(), card=_card(("x",))),
              cs.evaluate(recipe=_recipe(), card=None),
              cs.evaluate(recipe=None, card=None)):
        if v["runnable"]:
            runnable.add(v["state"])
    assert runnable == {cs.READY, cs.UNPROVEN}


# ── review findings, pinned ──────────────────────────────────────────────────
# Each of these was found by an adversarial review of the first F3/F4 cut and
# survived three independent attempts to refute it. They are the failure modes a
# well-intentioned gate produces on its own.

def test_an_EXTRA_slot_does_not_block_a_login_that_would_succeed():
    """Only a MISSING slot can stop a login. The interpreter reads the slots the
    recipe declares and ignores any other value, so a leftover from a previous
    recording still logs in — blocking on it would refuse the entire fleet the
    moment a login DROPPED a field, stopping work that would have succeeded."""
    v = cs.evaluate(recipe=_recipe(("email",)), card=_card(("email", "password")))
    assert v["runnable"] is True
    assert v["state"] == cs.UNPROVEN
    assert v["reason"] == "card_has_extra_slots"
    assert v["unexpected"] == ["password"]
    assert v["proven"] is False          # still out of step; not claimed as proven


def test_a_MISSING_slot_still_blocks_even_alongside_an_extra_one():
    v = cs.evaluate(recipe=_recipe(("member_id", "pin")), card=_card(("member_number", "pin")))
    assert v["state"] == cs.STALE_SLOTS
    assert v["runnable"] is False
    assert v["missing"] == ["member_id"]


def test_a_proof_recorded_against_the_CURRENT_version_reaches_ready():
    """`ready` has to be reachable. If nothing ever writes the card's recipe_version
    on a successful proof, a re-recorded login leaves every card permanently
    'proof_superseded' — proving it again changes nothing, and the state it needs to
    leave is the state it can never leave."""
    v = cs.evaluate(recipe=_recipe(version=7), card=_card(version=7))
    assert v["state"] == cs.READY and v["proven"] is True


def test_slots_filled_only_by_OPTIONAL_steps_are_not_required():
    """The verify-documents interstitial is shown to SOME members. Demanding a value
    for a screen a member never reaches would refuse exactly the members the
    optional marking exists to accommodate."""
    recipe = {
        "version": 1,
        "steps": [{"action": "fill", "slot": "email", "label": "Email"},
                  {"action": "fill", "slot": "password", "label": "Password"},
                  {"action": "fill", "slot": "doc_ack", "label": "Acknowledge",
                   "optional": True}],
        "slots": [{"name": "email"}, {"name": "password"}, {"name": "doc_ack"}],
    }
    v = cs.evaluate(recipe=recipe, card=_card(("email", "password"), version=1))
    assert v["runnable"] is True
    assert v["state"] != cs.STALE_SLOTS
    # supplying it is fine too — it is not "unexpected"
    v2 = cs.evaluate(recipe=recipe, card=_card(("email", "password", "doc_ack"), version=1))
    assert v2["state"] == cs.READY
