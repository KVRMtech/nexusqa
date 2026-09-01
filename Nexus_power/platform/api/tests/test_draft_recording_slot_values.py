"""Recorded sign-in VALUES matched onto the recipe's slots (draft_recording).

Recording captured the choreography and a session but never the values, so it could
replay only that session — a snapshot that expires, and that an app whose login lives in
client-side state can never restore. Live: an admin app recorded twice, crawled three
times, never once authenticated, with "re-record" as the only advice. Matching the values
the operator just used onto the slots the recording named is what makes ONE recording
sign every future crawl in.

Generic by construction: a slot's name IS one of the field's identifiers, so matching on
name/id/label/autocomplete holds for email+password, member#+PIN and multi-step MFA alike.
"""
from app.services.test_factory.draft_recording import match_slot_values

# The carrier-admin login as the recorder named it.
LOGIN = {"slots": [{"name": "email", "type": "plain"},
                   {"name": "password", "type": "secret"},
                   {"name": "mfaCode", "type": "plain"}]}


def test_values_match_by_field_name():
    got = match_slot_values(LOGIN, [
        {"name": "email", "id": "", "label": "Email", "value": "mchen@summitlife.com"},
        {"name": "password", "id": "", "label": "Password", "value": "Password123"},
        {"name": "mfaCode", "id": "", "label": "MFA Verification Code", "value": "123456"},
    ])
    assert got == {"email": "mchen@summitlife.com",
                   "password": "Password123",
                   "mfaCode": "123456"}


def test_matches_by_id_or_label_when_the_slot_was_named_from_those():
    # The recipe picks the most-stable identifier available, so a slot may be named by
    # the field's id or its visible label rather than its `name` attribute.
    got = match_slot_values(
        {"slots": [{"name": "user-id"}, {"name": "Passcode"}]},
        [{"name": "", "id": "user-id", "label": "", "value": "u1"},
         {"name": "", "id": "", "label": "Passcode", "value": "9999"}],
    )
    assert got == {"user-id": "u1", "Passcode": "9999"}


def test_last_observation_wins_so_a_partial_keystroke_never_survives():
    # `input` fires per keystroke; the completed secret is the LAST one observed.
    got = match_slot_values(LOGIN, [
        {"name": "password", "value": "P"},
        {"name": "password", "value": "Pass"},
        {"name": "password", "value": "Password123"},
    ])
    assert got == {"password": "Password123"}


def test_unknown_fields_and_blank_values_are_ignored():
    got = match_slot_values(LOGIN, [
        {"name": "newsletter", "value": "yes"},   # not a login slot
        {"name": "email", "value": ""},           # never entered
        {"name": "password", "value": "pw123456"},
    ])
    assert got == {"password": "pw123456"}


def test_no_slots_or_no_values_is_empty_never_a_guess():
    assert match_slot_values(None, [{"name": "email", "value": "a@b.c"}]) == {}
    assert match_slot_values(LOGIN, []) == {}
    assert match_slot_values(LOGIN, None) == {}
    assert match_slot_values({"slots": []}, [{"name": "email", "value": "a@b.c"}]) == {}
