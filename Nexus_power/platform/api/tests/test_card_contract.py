"""A credential card must be able to fill the login it is for.

F2 of MEMBERS_ENVIRONMENTS_DESIGN — the defect the founder spotted: the card form
asks the operator to RETYPE slot names into a free-text box defaulted to
'member_number, password', a vocabulary their app does not use, and never reads the
recipe's slots.

The failure is silent, which is what makes it serious. A mismatched card saves
cleanly and displays normally; at run time the compiled login finds the slot missing,
SKIPS the whole login, the suite executes unauthenticated, and every failure is
attributed to the application under test. A typo becomes "your app is broken".

Enforced at the API rather than the form, because a card is also reachable from a
script, a bulk import or curl.

Pure — no DB, no live stack.
"""
import pytest

from app.services.test_factory.card_contract import (
    CardContractError, check_card, required_slots, slot_fields,
)


def _recipe(slots=("email", "password"), *, declared=None):
    return {
        "steps": [{"action": "goto", "path": "/login"}]
             + [{"action": "fill", "slot": s, "label": s.title()} for s in slots]
             + [{"action": "click", "name": "Sign in"}],
        "slots": [{"name": s, "type": "secret"} for s in (declared or slots)],
    }


# ── what the recipe actually asks for ────────────────────────────────────────

def test_required_slots_come_from_the_STEPS_not_the_declaration():
    """The steps are what the replay executes. A recipe whose steps fill a slot the
    declaration omits would otherwise pass a check against the declaration and still
    skip the login — one of the enumerated silent-failure scenarios."""
    recipe = _recipe(("member_number", "password", "pin"), declared=("member_number", "password"))
    assert required_slots(recipe) == ["member_number", "password", "pin"]


def test_the_declaration_is_the_fallback_when_no_fill_steps_exist():
    assert required_slots({"steps": [], "slots": [{"name": "email"}]}) == ["email"]


def test_order_is_preserved_and_duplicates_collapse():
    recipe = {"steps": [{"action": "fill", "slot": "a"}, {"action": "fill", "slot": "b"},
                        {"action": "fill", "slot": "a"}]}
    assert required_slots(recipe) == ["a", "b"]


def test_no_vocabulary_is_assumed():
    """GENERIC. Whatever the app calls its fields is what a card must supply."""
    for names in (("member_number", "password", "pin"), ("email", "password"),
                  ("policy_no", "password"), ("mobile", "password", "otp"),
                  ("mrn", "password"), ("frequent_flyer", "password")):
        assert required_slots(_recipe(names)) == list(names)
        assert check_card(recipe=_recipe(names),
                          slot_values={n: "v" for n in names})["slot_names"] == list(names)


# ── the card must match ──────────────────────────────────────────────────────

def test_a_matching_card_is_accepted():
    out = check_card(recipe=_recipe(), slot_values={"email": "a@b.c", "password": "s3cret"})
    assert out["slot_names"] == ["email", "password"]


def test_a_MISSING_slot_is_refused_with_what_is_missing():
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(("email", "password", "pin")),
                   slot_values={"email": "a@b.c", "password": "s"})
    assert exc.value.detail["missing"] == ["pin"]
    assert exc.value.detail["required"] == ["email", "password", "pin"]


def test_the_founders_exact_case_a_typo_is_refused_not_stored():
    """'e-mail' instead of 'email' — saves fine today, then silently skips the login."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(), slot_values={"e-mail": "a@b.c", "password": "s"})
    assert exc.value.detail["missing"] == ["email"]
    assert exc.value.detail["unexpected"] == ["e-mail"]


def test_the_hardcoded_default_vocabulary_is_refused_against_a_real_recipe():
    """The form's 'member_number, password' default against an email/password app."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(("email", "password")),
                   slot_values={"member_number": "8891234", "password": "s"})
    assert "email" in exc.value.detail["missing"]
    assert "member_number" in exc.value.detail["unexpected"]


def test_an_UNEXPECTED_slot_is_refused_even_when_all_required_are_present():
    """It means the operator is filling a different login than the one on file."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(),
                   slot_values={"email": "a@b.c", "password": "s", "pin": "1234"})
    assert exc.value.detail["unexpected"] == ["pin"]
    assert exc.value.detail["missing"] == []


def test_an_EMPTY_value_is_not_a_credential():
    """'' would be typed into the field and the login would fail as if the app were
    broken. Blank must be refused, not stored."""
    for blank in ("", "   ", None):
        with pytest.raises(CardContractError) as exc:
            check_card(recipe=_recipe(), slot_values={"email": blank, "password": "s"})
        assert exc.value.detail["missing"] == ["email"]


# ── prerequisites ────────────────────────────────────────────────────────────

def test_a_card_BEFORE_any_recipe_is_refused():
    """Guaranteed-wrong work: with no recipe the slot names can only be a guess, and
    a guessed name silently skips the login."""
    for recipe in (None, {}):
        with pytest.raises(CardContractError) as exc:
            check_card(recipe=recipe, slot_values={"email": "a@b.c"})
        assert exc.value.detail["reason"] == "no_recipe"


def test_a_recipe_that_fills_nothing_cannot_be_driven_by_a_card():
    with pytest.raises(CardContractError) as exc:
        check_card(recipe={"steps": [{"action": "goto", "path": "/login"}], "slots": []},
                   slot_values={"email": "a@b.c"})
    assert exc.value.detail["reason"] == "recipe_has_no_slots"


def test_an_empty_card_against_a_real_recipe_is_refused():
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(), slot_values={})
    assert exc.value.detail["missing"] == ["email", "password"]


def test_the_error_tells_the_operator_what_the_login_asks_for():
    """A refusal that does not say what IS required just moves the guessing."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(("policy_no", "password")), slot_values={"x": "y"})
    note = exc.value.detail["note"]
    assert "policy_no" in note and "password" in note


def test_no_credential_value_appears_in_the_refusal():
    """Errors get logged and surfaced — they must not carry secrets."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(), slot_values={"email": "a@b.c", "wrong": "SUPERSECRET"})
    assert "SUPERSECRET" not in repr(exc.value.detail)


def test_the_string_None_is_not_accepted_as_a_secret():
    """str(None) is the four-letter word 'None' — a plausible-looking secret that
    would be TYPED INTO THE FIELD at replay. It must read as empty."""
    with pytest.raises(CardContractError) as exc:
        check_card(recipe=_recipe(), slot_values={"email": None, "password": None})
    assert exc.value.detail["missing"] == ["email", "password"]


# ── the form the operator sees ───────────────────────────────────────────────

def test_the_card_form_is_described_by_the_recipe_using_the_APPS_OWN_labels():
    """The operator should read their own application's wording, not our slot ids —
    that is what stops the panel from asking for 'member_number' on an app that
    calls it 'Policy #'."""
    recipe = {
        "steps": [{"action": "goto", "path": "/signin"},
                  {"action": "fill", "slot": "ctl00$txtPolicy", "label": "Policy #"},
                  {"action": "fill", "slot": "ctl00$txtPin", "label": "PIN"},
                  {"action": "click", "name": "Continue"}],
        "slots": [{"name": "ctl00$txtPolicy", "type": "secret"},
                  {"name": "ctl00$txtPin", "type": "secret"}],
    }
    assert slot_fields(recipe) == [
        {"name": "ctl00$txtPolicy", "label": "Policy #", "type": "secret"},
        {"name": "ctl00$txtPin", "label": "PIN", "type": "secret"},
    ]


def test_a_field_with_no_observed_label_falls_back_to_its_slot_name():
    recipe = {"steps": [{"action": "fill", "slot": "email"}], "slots": []}
    assert slot_fields(recipe) == [{"name": "email", "label": "email", "type": "secret"}]


def test_an_unknown_type_defaults_to_masked():
    """Masked is the safe default: showing a secret in the clear cannot be undone."""
    recipe = {"steps": [{"action": "fill", "slot": "pin", "label": "PIN"}]}
    assert slot_fields(recipe)[0]["type"] == "secret"


def test_no_recipe_means_no_form_at_all():
    """The panel renders exactly these fields, so an empty list is what stops it
    offering a card before a login has been recorded."""
    assert slot_fields(None) == []
    assert slot_fields({}) == []


def test_the_form_and_the_refusal_agree_on_the_same_slot_set():
    """The UI is a convenience and the API is the contract — they must not drift."""
    recipe = _recipe(("policy_no", "pin", "password"))
    assert [f["name"] for f in slot_fields(recipe)] == required_slots(recipe)
    assert check_card(recipe=recipe,
                      slot_values={f["name"]: "v" for f in slot_fields(recipe)})
