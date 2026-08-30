"""THE SECOND CRAWL OF AN ACCOUNT-CREATION FLOW MUST NOT COLLIDE WITH THE FIRST.

``derive(seed)`` is deliberately stable: "``seed`` should be stable for a
tenant+application so the same client keeps the same person across crawls — a
rate quote that changes because the age changed between runs is a false
difference, not a regression." That is exactly right for a quote, and exactly
wrong for the one class of field an application refuses to see twice.

Crawl an account-opening funnel today and it completes. Crawl it again tomorrow
and the same email arrives at the same application, which answers "that email
is already registered" — and the funnel dead-ends on a rejection no repair can
satisfy, because the constraint is not about the VALUE's shape but about its
history. Every top-100 sign-up flow has this field.

THE SPLIT: identity stays stable, and only the fields whose validity depends on
never having been used before are salted per run. Name, date of birth, address,
national id, card — all unchanged across runs, so a rate quote is still
comparable. Email and username carry a short run token.

WHAT IS NOT CHANGED, pinned below: RFC 2606 ``example.com`` still means a
synthetic address can never reach a real person; the local part stays readable;
and with no run token supplied, ``derive`` produces exactly what it always did,
so every existing caller and golden is untouched.
"""
from __future__ import annotations

from app.identity_pack import derive


def test_the_same_seed_still_gives_the_same_person():
    """THE CONTROL, and the reason the seed is stable in the first place."""
    a, b = derive("tenant-1:app-1"), derive("tenant-1:app-1")
    assert a.full_name == b.full_name
    assert a.date_of_birth == b.date_of_birth
    assert a.national_id == b.national_id
    assert a.postal_code == b.postal_code
    assert a.card_number == b.card_number


def test_two_runs_get_different_emails_and_usernames():
    """THE BUG: the same email twice is 'already registered' on run two."""
    a = derive("tenant-1:app-1", run_token="c1")
    b = derive("tenant-1:app-1", run_token="c2")
    assert a.email != b.email
    assert a.username != b.username


def test_but_they_are_still_the_same_person():
    """Only the fields that must be unique move; the identity does not."""
    a = derive("tenant-1:app-1", run_token="c1")
    b = derive("tenant-1:app-1", run_token="c2")
    assert a.full_name == b.full_name
    assert a.date_of_birth == b.date_of_birth
    assert a.national_id == b.national_id
    assert a.street_address == b.street_address


def test_a_run_token_is_stable_within_its_own_run():
    a, b = derive("s", run_token="c1"), derive("s", run_token="c1")
    assert a.email == b.email and a.username == b.username


def test_no_run_token_reproduces_today_s_behaviour_exactly():
    """Every existing caller and golden must be untouched."""
    plain, explicit = derive("s"), derive("s", run_token="")
    assert plain.email == explicit.email
    assert plain.username == explicit.username


def test_the_address_can_never_reach_a_real_person():
    """RFC 2606 reserves example.com for documentation — salting must not
    accidentally move the address to a domain someone could receive mail at."""
    assert derive("s", run_token="c1").email.endswith("@example.com")


def test_the_local_part_stays_readable_for_a_human_reviewing_evidence():
    email = derive("s", run_token="c1").email
    local = email.split("@")[0]
    assert len(local) <= 40
    assert local.replace(".", "").replace("-", "").isalnum()


def test_the_zip_belongs_to_the_state_it_was_generated_with():
    """Not new — pinned because a USPS-validating quote engine rejects a ZIP
    that does not belong to its state, and nothing else asserts this."""
    from app.identity_pack import REGIONS
    ident = derive("geo-check")
    row = [r for r in REGIONS if r[0] == ident.region_code]
    assert row, f"unknown region {ident.region_code}"
    _, _, zip_prefix, city = row[0]
    assert ident.postal_code.startswith(zip_prefix)
    assert ident.city == city
