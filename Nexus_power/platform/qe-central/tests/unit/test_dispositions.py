"""Phase 1 — exhaustive tests for the six-disposition classifier (the schema owner).

Pins the never-green-wash guarantees Phases 3/4/5 rely on: fail-closed to ASK for
identifiers/credentials/unrecognised fields; PICK only from an OBSERVED option (never
an invented one); OBSERVE/ASK excluded from the pre-fill; a quote form is near-
autonomous (its Recommended list is just ASK+APPROVE); determinism.
"""
from __future__ import annotations

from datetime import date

from app.services import dispositions as dp
from app.services.dispositions import FieldSignal as F


def _one(label, ftype="text", options=(), required=False, **ctx):
    return dp.classify_field(F(label, ftype, tuple(options), required), **ctx)


# ── ASK (fail-closed) — the doctrine's teeth ──────────────────────────────────
def test_ssn_is_ask():
    d = _one("Social Security Number")
    assert d.disposition == dp.ASK and d.default is None


def test_account_number_is_ask():
    assert _one("Account Number").disposition == dp.ASK


def test_policy_id_is_ask():
    assert _one("Policy ID").disposition == dp.ASK


def test_novel_reference_no_is_ask_not_synthesized():
    # The "Reference no." = policy-id hole: a plain text field with a safe-looking
    # type must still fail closed to ASK because the noun+suffix is identifier-shaped.
    d = _one("Reference No.")
    assert d.disposition == dp.ASK
    assert d.default is None


def test_password_type_is_ask():
    assert _one("Passphrase", ftype="password").disposition == dp.ASK


def test_login_username_is_ask():
    assert _one("Username", ftype="text").disposition == dp.ASK


def test_unrecognised_text_field_fails_closed_to_ask():
    # A generic text field the classifier does not affirmatively recognise as safe
    # is ASKed, never given a fabricated value.
    d = _one("Widget Serial")
    assert d.disposition == dp.ASK and d.default is None


def test_card_and_cvv_are_ask():
    assert _one("Credit Card Number").disposition == dp.ASK
    assert _one("CVV").disposition == dp.ASK


# ── PICK — grounded in observed options only ──────────────────────────────────
def test_select_with_options_is_grounded_pick():
    d = _one("State", ftype="select", options=("", "Choose...", "California", "New York"))
    assert d.disposition == dp.PICK and d.default == "California" and d.grounded


def test_select_with_only_placeholders_is_uncaptured_pick_not_ask():
    # No real observed option, BUT a dropdown is a CHOICE, never a free-text value to
    # invent. It stays PICK (ungrounded) + flagged uncaptured — the UI says "a choice we
    # could not read yet — re-crawl", never "enter a real value".
    d = _one("Plan", ftype="select", options=("", "-- Select --", "Choose one"))
    assert d.disposition == dp.PICK and d.default is None
    assert d.grounded is False and d.uncaptured_options is True


def test_custom_dropdown_with_no_captured_options_is_uncaptured_pick():
    # The live bug: a custom SPA dropdown (From Account / Payee) captured with options=[]
    # was shown as "enter a real value" (ASK). It must be a choice, not free text.
    d = _one("From Account", ftype="select", options=())
    assert d.disposition == dp.PICK and d.uncaptured_options is True
    assert "re-crawl" in d.reason.lower()


def test_english_placeholder_prefix_is_not_grounded():
    # "Select an account" / "Choose your state" are placeholders (English scope), not real
    # choices — they must not be pinned as a grounded default (which would false-"ready").
    d = _one("Account", ftype="select", options=("Select an account", "Checking", "Savings"))
    assert d.disposition == dp.PICK and d.default == "Checking" and d.grounded
    d2 = _one("State", ftype="select", options=("Choose your state",))
    assert d2.disposition == dp.PICK and d2.uncaptured_options is True


def test_checkbox_toggle_is_auto_not_ask():
    # A boolean checkbox/toggle is set by the crawl itself — never a value to provide.
    for ft in ("checkbox", "toggle", "switch"):
        d = _one("Enable notifications", ftype=ft)
        assert d.disposition == dp.SYNTHESIZE and d.uncaptured_options is False


def test_radio_options_pick():
    d = _one("Tobacco use", ftype="radio", options=("Yes", "No"))
    assert d.disposition == dp.PICK and d.default == "Yes"


# ── SYNTHESIZE — domain-valid defaults ────────────────────────────────────────
def test_dob_is_an_adult_date_not_today():
    d = _one("Date of Birth", ftype="date", today=date(2026, 6, 15))
    assert d.disposition == dp.SYNTHESIZE
    assert d.default == "1991-06-15"  # 35 years before the injected clock
    assert d.default != "2026-06-15"


def test_email_synthesizes_valid_address():
    d = _one("Email", ftype="email")
    assert d.disposition == dp.SYNTHESIZE and "@" in (d.default or "")


def test_coverage_amount_synthesizes_plausible_number():
    d = _one("Coverage Amount", ftype="number")
    assert d.disposition == dp.SYNTHESIZE and d.default == "250000"


def test_first_and_last_name_synthesize():
    assert _one("First Name").default == "Test"
    assert _one("Last Name").default == "User"


def test_message_field_is_not_treated_as_age():
    # LIVE E2E regression (automationexercise.com): "Your Message Here" was synthesized
    # as "35" because a naive substring test found 'age' inside 'message'.
    d = _one("Your Message Here")
    assert d.disposition == dp.SYNTHESIZE
    assert d.default == "QA test"


def test_age_still_matches_as_a_whole_word():
    assert _one("Age", ftype="number").default == "35"
    assert _one("Applicant Age").default == "35"


def test_short_token_substring_collisions_do_not_fire():
    # 'age' in package/mileage/manage; 'sum' in consumer; 'city' in capacity;
    # 'state' in estate — none of these should take the short-token branch.
    assert _one("Package Description").default == "QA test"   # description wins, not age
    assert _one("Real Estate Notes").default == "QA test"     # note wins, not state=CA
    assert _one("Coverage Amount", ftype="number").default == "250000"  # coverage, not age


def test_generic_date_is_not_dob():
    d = _one("Appointment Date", ftype="date", today=date(2026, 6, 15))
    assert d.disposition == dp.SYNTHESIZE and d.default == "2026-06-15"


# ── CARRY / OBSERVE / APPROVE ─────────────────────────────────────────────────
def test_library_match_is_carry():
    d = _one("Member ID", library_keys=["member id"])
    # CARRY takes precedence over ASK when the library already holds the value.
    assert d.disposition == dp.CARRY and not d.editable


def test_observe_label_is_never_filled():
    d = _one("Monthly Premium", observe_labels=["monthly premium"])
    assert d.disposition == dp.OBSERVE and d.default is None and not d.editable


def test_submit_control_is_approve():
    assert _one("Get Quote", ftype="submit").disposition == dp.APPROVE
    assert _one("Continue", submit_labels=["continue"]).disposition == dp.APPROVE


# ── The two-mode manifest ─────────────────────────────────────────────────────
def _quote_form():
    return [
        F("State", "select", ("", "California", "New York")),
        F("Date of Birth", "date"),
        F("Coverage Amount", "number"),
        F("Tobacco use", "radio", ("Yes", "No")),
        F("Email", "email"),
        F("Social Security Number", "text"),
        F("Get Quote", "submit"),
    ]


def test_quote_form_recommended_is_only_ask_plus_approve():
    m = dp.classify_manifest(_quote_form(), today=date(2026, 6, 15))
    codes = {i["disposition"] for i in m["recommended"]}
    assert codes == {dp.ASK, dp.APPROVE}
    # exactly one ASK (the SSN) and one APPROVE (Get Quote) — the human 1%.
    assert m["ask_count"] == 1 and m["approve_count"] == 1
    labels = {i["label"] for i in m["recommended"]}
    assert labels == {"Social Security Number", "Get Quote"}


def test_full_mode_returns_every_field_with_defaults():
    m = dp.classify_manifest(_quote_form(), today=date(2026, 6, 15))
    assert len(m["full"]) == 7
    by_label = {i["label"]: i for i in m["full"]}
    assert by_label["State"]["default"] == "California"
    assert by_label["Social Security Number"]["default"] is None


def test_prefill_omits_observe_and_ask():
    m = dp.classify_manifest(
        _quote_form(),
        observe_targets=[{"label": "Monthly Premium", "source_hint": ".premium"}],
        today=date(2026, 6, 15),
    )
    assert "Social Security Number" not in m["prefill"]  # ASK never prefilled
    assert "Monthly Premium" not in m["prefill"]         # OBSERVE never prefilled
    assert m["prefill"]["State"] == "California"
    assert m["prefill"]["Email"] == "qa.autotest@example.com"


def test_recommended_never_hides_an_ask_even_among_many_auto_fields():
    fields = [F(f"Field {i}", "text") for i in range(20)] + [F("Bank Account", "text")]
    # (generic "Field N" text → ASK fail-closed too, but the explicit account is the point)
    m = dp.classify_manifest(fields)
    assert any(i["label"] == "Bank Account" for i in m["recommended"])


def test_observe_target_appended_and_not_duplicated():
    m = dp.classify_manifest(
        [F("Monthly Premium", "text")],
        observe_targets=[{"label": "Monthly Premium"}],
    )
    premium_items = [i for i in m["full"] if i["label"] == "Monthly Premium"]
    assert len(premium_items) == 1  # not duplicated


# ── Determinism ───────────────────────────────────────────────────────────────
def test_classify_manifest_is_deterministic():
    a = dp.classify_manifest(_quote_form(), today=date(2026, 6, 15))
    b = dp.classify_manifest(_quote_form(), today=date(2026, 6, 15))
    assert a == b
