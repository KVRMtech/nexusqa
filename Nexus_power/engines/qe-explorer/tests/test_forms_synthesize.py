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
