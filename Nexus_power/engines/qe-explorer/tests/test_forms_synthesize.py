"""P2 — typed default filler: synthesize valid low-confidence values for fields the
answer key doesn't cover, so a validation-gated form advances without a hand seed.
Never a password (skipped upstream) or a semantic radio/optional-toggle choice."""
from __future__ import annotations

from app.forms import _synthesize_default


def _syn(kind="text", name="", input_type="", options=None, required=False, **extra):
    ctl = {"kind": kind, "name": name, "input_type": input_type,
           "options": options or [], "required": required, **extra}
    return _synthesize_default(ctl, kind, name)


def test_email_by_input_type_and_by_name():
    assert _syn(input_type="email", name="X") == "qa.autotest@example.com"
    assert _syn(name="Email Address") == "qa.autotest@example.com"


def test_date_is_iso_today():
    from datetime import date
    assert _syn(kind="date", name="Date of Birth") == date.today().isoformat()
    assert _syn(input_type="date", name="Start") == date.today().isoformat()


def test_each_temporal_input_gets_its_own_valid_format():
    """R1 audit finding: time/month/week/datetime-local were synthesized a plain
    DATE — an invalid value for those input types, so Playwright's fill threw
    and the field always errored. Each flavour now gets its own valid format."""
    from datetime import date
    assert _syn(input_type="time", kind="date", name="Appointment time") == "12:00"
    assert _syn(input_type="month", kind="date", name="Statement month") == date.today().strftime("%Y-%m")
    wk = _syn(input_type="week", kind="date", name="Delivery week")
    assert "-W" in wk and len(wk.split("-W")[1]) == 2
    dtl = _syn(input_type="datetime-local", kind="date", name="Pickup")
    assert dtl == f"{date.today().isoformat()}T12:00"


def test_phone_number_zip_url():
    assert _syn(input_type="tel", name="Mobile") == "5551234567"
    assert _syn(name="Phone Number") == "5551234567"
    assert _syn(input_type="number", name="Age") == "1"
    assert _syn(name="Zip Code") == "12345"
    assert _syn(name="Postal Code") == "12345"
    assert _syn(input_type="url", name="Website") == "https://example.com"


def test_slider_default_is_valid_and_grounded_in_min_max():
    """R1: sliders were detected + refused (None), so a range-gated form never
    advanced. A native range now gets a VALID midpoint value grounded in the
    control's declared min/max/step — never an out-of-range guess."""
    assert _syn(kind="slider", name="Coverage", min="0", max="100") == "50"      # midpoint
    assert _syn(kind="slider", name="Amount", min="10", max="30", step="5") == "20"
    assert _syn(input_type="range", name="Volume", min="18") == "18"             # min only
    assert _syn(kind="slider", name="Bare") == "50"                             # no bounds
    assert _syn(kind="color", name="Theme colour") == "#1a2b3c"
    assert _syn(input_type="color", name="Accent") == "#1a2b3c"


def test_number_default_honours_declared_min_max_step():
    """Live incident: <input type=number min=18 max=80> ('Age' on the quote form)
    auto-filled with a constraint-blind '1' → browser-native validation silently
    VOIDED the Phase-B submit (outcome=none). The default must satisfy the
    control's OWN declared constraints — min when present (spec: min is the step
    base, always valid), max when it forbids 1, else the old '1'."""
    assert _syn(input_type="number", name="Age", min="18", max="80") == "18"
    assert _syn(input_type="number", name="Qty", min="0.5", step="0.5") == "0.5"
    assert _syn(input_type="number", name="Discount", max="0") == "0"
    assert _syn(input_type="number", name="Qty", max="10") == "1"      # 1 still valid
    assert _syn(input_type="number", name="Qty", min="oops") == "1"    # junk bound ignored
    assert _syn(name="Age") == "1"                                     # no constraints -> unchanged


def test_name_fields():
    assert _syn(name="First Name") == "Test"
    assert _syn(name="Last Name") == "User"
    assert _syn(name="Full Name") == "Test User"
    assert _syn(name="Name") == "Test User"


def test_address_fields():
    assert _syn(name="City") == "Springfield"
    assert _syn(name="Street Address") == "1 Test Street"
    assert _syn(name="Company") == "Autotest Inc"
    assert _syn(name="Country") == "United States"


def test_select_picks_first_real_option_skips_placeholder():
    assert _syn(kind="select", name="Rider", options=["-- Select --", "Term", "Whole"]) == "Term"
    # a select with only a placeholder yields no default (named in coverage instead)
    assert _syn(kind="select", name="X", options=["Please select"]) is None
    assert _syn(kind="select", name="X", options=[]) is None


def test_required_checkbox_is_checked_optional_and_radio_are_not():
    assert _syn(kind="checkbox", name="I agree to terms", required=True) == "true"
    assert _syn(kind="checkbox", name="Subscribe", required=False) is None
    assert _syn(kind="radio", name="Gender", required=True) is None  # semantic choice


def test_generic_text_fallback():
    assert _syn(kind="text", name="Some Field") == "autotest"
    assert _syn(input_type="search", name="Search") == "autotest"


# ── placeholder options: "Select coverage amount..." is NOT an answer ─────────

def test_specific_placeholders_are_not_chosen_as_defaults():
    """Regression: the quote funnel stalled at step 2 because Coverage Amount and
    Term Length were both set to their "Select ..." option. That option's value is
    "", so the field stayed empty, Continue stayed disabled, and the crawl
    reported a page it believed it had filled. An exact-phrase list never matched
    these — real apps write specific placeholders."""
    from app.forms import _synthesize_default

    for placeholder, real in (
        ("Select coverage amount...", "$50,000"),
        ("Select term length...", "10 Years"),
        ("Select military status...", "Active Duty Officer"),
        ("-- Choose your state --", "Alabama"),
        ("Choose one", "Gold"),
        ("…", "Bronze"),
    ):
        control = {"options": [placeholder, real]}
        got = _synthesize_default(control, "select", "Some Field")
        assert got == real, f"{placeholder!r} was chosen instead of {real!r}"


def test_a_real_answer_that_starts_with_a_verb_is_still_selectable():
    """Conservative by design: a false positive silently discards a legitimate
    business path. The leading-verb rule applies only to the FIRST option, where
    placeholders conventionally live."""
    from app.forms import _synthesize_default

    control = {"options": ["-- Select a plan --", "Choose Life Term 20", "Gold"]}
    assert _synthesize_default(control, "select", "Plan") == "Choose Life Term 20"


def test_an_all_placeholder_select_yields_nothing_rather_than_a_lie():
    from app.forms import _synthesize_default
    control = {"options": ["Select one", "--", ""]}
    assert _synthesize_default(control, "select", "Empty") is None


def test_both_option_choosers_agree_on_what_a_placeholder_is():
    """There were TWO placeholder lists and I fixed only one.

    forms._synthesize_default is a FALLBACK — field_values.value_for runs first
    and wins. Fixing the fallback while value_for still returned "Select coverage
    amount..." left the funnel shut behind a field the ledger reported as filled,
    and looked exactly like the fix had not worked. They must never diverge
    again, so they are now one function."""
    from app import field_values, forms

    assert forms._is_placeholder_option is field_values.is_placeholder_option

    opts = ["Select coverage amount...", "$50,000", "$100,000"]
    # the FALLBACK skips the placeholder...
    assert forms._synthesize_default({"options": opts}, "select", "Coverage") == "$50,000"
    # ...and so does the path that actually runs first.
    assert field_values.enumerate_real(opts) == ["$50,000", "$100,000"]


def test_value_for_never_answers_a_select_with_its_placeholder():
    """The live regression: value_for returned the placeholder for all three
    dropdowns on the coverage page, so the form was 'filled' and still empty."""
    from app import field_values
    from app.identity_pack import derive

    ident = derive("qec-test")
    for label, opts, expect in (
        ("Coverage Amount", ["Select coverage amount...", "$50,000"], "$50,000"),
        ("Term Length", ["Select term length...", "10 Years"], "10 Years"),
        ("Military Affiliation", ["Select military status...", "Veteran"], "Veteran"),
    ):
        got = field_values.value_for(
            "choice", {"name": label, "kind": "select", "options": opts},
            ident, kind="select", data_mode="agent")
        assert got == expect, f"{label}: value_for returned {got!r}"


def test_a_first_option_that_trails_off_is_a_placeholder_even_without_a_verb():
    """"Feet..." / "Inches..." are the same "nothing chosen yet" entry wearing
    the field's own name instead of a select/choose verb.

    Observed live: the health step's height dropdowns were answered "Feet..."
    and "Inches...", leaving both empty, and the funnel stopped one page short
    of the quote."""
    from app import field_values

    assert field_values.is_placeholder_option("Feet...", first=True) is True
    assert field_values.is_placeholder_option("Inches...", first=True) is True
    assert field_values.enumerate_real(["Feet...", "4", "5", "6"]) == ["4", "5", "6"]


def test_a_later_option_that_trails_off_is_still_a_real_answer():
    """Restricted to the first option on purpose: an answer further down a list
    that happens to trail off ("Other...") is a real business path, and dropping
    it would silently remove coverage."""
    from app import field_values
    assert field_values.is_placeholder_option("Other...", first=False) is False
    assert field_values.enumerate_real(
        ["Select one", "Gold", "Other..."]) == ["Gold", "Other..."]
