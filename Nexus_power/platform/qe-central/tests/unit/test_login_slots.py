"""Recorded-login slot values -> replayable credentials (services/login_slots).

Recording is value-free by construction, so a recording alone could only replay a
SESSION — a snapshot that expires, and that an app whose login lives in client-side
state can NEVER restore (live: a carrier-admin app recorded twice, crawled three times,
never once authenticated). Asking for the values of the slots the recording OBSERVED
closes that gap, and this maps them onto what the crawler replays.

Generic by construction: the slots come from what the app actually asked for, so these
pin email+password, member#+PIN (no masked field at all) and 3-slot MFA with no
app-specific knowledge.
"""
from app.services.login_slots import credentials_from_slot_values

# The carrier-admin login, exactly as the recorder observed it.
MFA_SLOTS = [
    {"name": "email", "type": "plain"},
    {"name": "password", "type": "secret"},
    {"name": "mfaCode", "type": "plain"},
]


def test_three_slot_mfa_login_maps_identifier_secret_and_second_factor():
    got = credentials_from_slot_values(MFA_SLOTS, {
        "email": "mchen@summitlife.com", "password": "Password123", "mfaCode": "123456",
    })
    assert got["username"] == "mchen@summitlife.com"
    assert got["password"] == "Password123"
    # A deterministic code is what a test environment issues — the crawler computes it.
    assert got["mfa"] == {"kind": "otp", "otp": "123456"}


def test_plain_email_password_login():
    got = credentials_from_slot_values(
        [{"name": "Username", "type": "plain"}, {"name": "Password", "type": "secret"}],
        {"Username": "u1", "Password": "p1"},
    )
    assert got == {"username": "u1", "password": "p1"}


def test_passwordless_member_pin_uses_the_pin_as_the_SECRET_not_a_second_factor():
    # U6: neither field is masked, and "PIN" is the PRIMARY secret. Treating it as an
    # OTP would leave the login with no password at all and never sign in.
    got = credentials_from_slot_values(
        [{"name": "Member Number", "type": "plain"}, {"name": "PIN", "type": "plain"}],
        {"Member Number": "1234567", "PIN": "4321"},
    )
    assert got == {"username": "1234567", "password": "4321"}
    assert "mfa" not in got


def test_verification_code_slot_is_a_second_factor_not_the_password():
    got = credentials_from_slot_values(
        [{"name": "Email", "type": "plain"},
         {"name": "Password", "type": "secret"},
         {"name": "Verification code", "type": "plain"}],
        {"Email": "a@b.test", "Password": "secret12", "Verification code": "998877"},
    )
    assert got["password"] == "secret12"
    assert got["mfa"]["otp"] == "998877"


def test_blank_values_are_ignored():
    got = credentials_from_slot_values(MFA_SLOTS, {
        "email": "u@x.test", "password": "p", "mfaCode": "",
    })
    assert got == {"username": "u@x.test", "password": "p"}   # no empty mfa written


def test_bare_slot_names_without_types_still_map():
    # A recipe that carries only slot_names (no per-slot type) must still work.
    got = credentials_from_slot_values(
        ["email", "password"], {"email": "e@x.test", "password": "pw123456"},
    )
    assert got == {"username": "e@x.test", "password": "pw123456"}


def test_no_slots_falls_back_to_the_supplied_keys():
    got = credentials_from_slot_values(None, {"username": "u", "password": "p"})
    assert got == {"username": "u", "password": "p"}


def test_nothing_supplied_is_empty_never_a_half_credential():
    assert credentials_from_slot_values(MFA_SLOTS, {}) == {}
    assert credentials_from_slot_values(MFA_SLOTS, None) == {}
