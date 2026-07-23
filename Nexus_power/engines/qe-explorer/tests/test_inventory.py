"""Unit tests for :mod:`app.inventory` — the control refiner.

Fixtures model the Aegis proving-ground ``:8096 /apply`` wizard widgets
(combobox, slider, accordion, modal, shadow, iframe) plus a repeated-control
grid.  Everything is hand-authored raw-control dicts shaped exactly like
:data:`app.inventory_js.INVENTORY_JS` output — no browser required.
"""
from __future__ import annotations

from app.guard import RefusePack, RefuseRule
from app.inventory import (
    CONTROL_KINDS,
    TARGET_KINDS,
    build_inventory,
    classify_control_danger,
    form_signal_for,
    refine_kind,
    target_kind_for,
)
from app.inventory_js import INVENTORY_JS, INVENTORY_JS_VERSION


# ─── Refuse pack fixture (the REAL guard model — danger is fail-closed) ────────

REFUSE_PACK = RefusePack(
    version="inventory-test-v1",
    irreversible_verbs=(
        RefuseRule(id="delete", match=r"\b(delete|remove|purge|cancel policy)\b",
                   applies_to=["button_name", "url_path"]),
        RefuseRule(id="pay", match=r"\b(pay|transfer|checkout|place order)\b",
                   applies_to=["button_name"]),
        RefuseRule(id="signout", match=r"\b(log ?out|sign ?out)\b",
                   applies_to=["button_name"]),
    ),
)


def _raw(**over):
    base = {
        "role": "", "name": "", "name_source": "content", "best_effort": False,
        "kind": "", "tag": "", "input_type": "", "options": [],
        "required": False, "disabled": False, "frame_selector": "",
        "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


# ─── refine_kind: the compiler observed.kind vocabulary ────────────────────────


def test_refine_kind_covers_aegis_widgets():
    # Native <select>.
    assert refine_kind(role="combobox", tag="select", input_type="",
                       options=["Term", "Whole"], value="") == "select"
    # Custom ARIA combobox on a div (no native options captured).
    assert refine_kind(role="combobox", tag="div", input_type="",
                       options=[], value="") == "select"
    # Native range slider ⇒ 'slider' (a distinct FILLABLE kind: the synthesizer
    # sets a valid midpoint; a range must never be typed a text string).
    assert refine_kind(role="slider", tag="input", input_type="range",
                       options=[], value="250000") == "slider"
    # Accordion header is a button.
    assert refine_kind(role="button", tag="button", input_type="",
                       options=[], value="") == "button"
    # role=switch ⇒ toggle.
    assert refine_kind(role="switch", tag="button", input_type="",
                       options=[], value="") == "toggle"
    # Checkbox / radio by input type.
    assert refine_kind(role="", tag="input", input_type="checkbox",
                       options=[], value="") == "checkbox"
    assert refine_kind(role="", tag="input", input_type="radio",
                       options=[], value="") == "radio"
    # Native date input.
    assert refine_kind(role="", tag="input", input_type="date",
                       options=[], value="2026-01-01") == "date"
    # Text field whose value looks like a date, no native date type ⇒ date.
    assert refine_kind(role="textbox", tag="input", input_type="text",
                       options=[], value="01/02/2026") == "date"
    # Plain text field.
    assert refine_kind(role="textbox", tag="input", input_type="text",
                       options=[], value="Jane") == "text"
    # Anchor link.
    assert refine_kind(role="link", tag="a", input_type="",
                       options=[], value="") == "link"


def test_refine_kind_all_values_are_in_vocabulary():
    for kind in (
        refine_kind(role="combobox", tag="select", input_type="", options=[], value=""),
        refine_kind(role="switch", tag="div", input_type="", options=[], value=""),
        refine_kind(role="", tag="input", input_type="text", options=[], value=""),
        refine_kind(role="", tag="div", input_type="", options=[], value=""),
    ):
        assert kind in CONTROL_KINDS


# ─── build_inventory: names, kinds, qec bucket, form signals ───────────────────


def test_build_inventory_preserves_name_and_kind_and_qec():
    raw = [
        _raw(role="combobox", tag="div", name="Coverage type", testid="cov-type",
             css_hint="div.select#cov", input_type=""),
        _raw(role="slider", tag="input", input_type="range", name="Coverage amount",
             value_committed="250000"),
        _raw(role="button", tag="button", name="Beneficiaries", testid="acc-benef"),
    ]
    recs = build_inventory(raw, REFUSE_PACK)
    by_name = {r["name"]: r for r in recs}

    cov = by_name["Coverage type"]
    assert cov["kind"] == "select"
    assert cov["role"] == "combobox"
    # Diagnostics live ONLY in qec — no compiler rung binds them.
    assert cov["qec"]["testid"] == "cov-type"
    assert cov["qec"]["css_hint"] == "div.select#cov"
    assert cov["qec"]["role"] == "combobox"
    # target_kind + form-signal mappings hit the two distinct vocabularies.
    assert target_kind_for(cov) == "dropdown"
    assert target_kind_for(cov) in TARGET_KINDS
    assert form_signal_for(cov) == {"type": "select", "options": [], "required": False}

    amount = by_name["Coverage amount"]
    # native range ⇒ kind 'slider' — a first-class FILLABLE control (the
    # synthesizer sets a valid midpoint from min/max; typing a string into a
    # range is invalid, which is exactly why it is typed distinctly from text).
    assert amount["kind"] == "slider"
    assert amount["value_committed"] == "250000"

    # A button is not a form field.
    assert form_signal_for(by_name["Beneficiaries"]) is None
    assert target_kind_for(by_name["Beneficiaries"]) == "button"


def test_password_input_type_survives_into_qec_for_redaction():
    # writer.action_is_secret reads qec.input_type == 'password' (writer.py:119).
    recs = build_inventory(
        [_raw(role="textbox", tag="input", input_type="password", name="Password")],
        REFUSE_PACK,
    )
    assert recs[0]["qec"]["input_type"] == "password"


# ─── Accessible-name best-effort flagging ──────────────────────────────────────


def test_placeholder_name_flagged_best_effort():
    recs = build_inventory([
        _raw(role="textbox", tag="input", input_type="text", name="Enter your SSN",
             name_source="placeholder", best_effort=True),
        _raw(role="textbox", tag="input", input_type="text", name="First name",
             name_source="label-for", best_effort=False),
    ], REFUSE_PACK)
    by_name = {r["name"]: r for r in recs}
    assert by_name["Enter your SSN"]["best_effort_name"] is True
    assert by_name["First name"]["best_effort_name"] is False


def test_missing_name_is_best_effort_and_honest():
    recs = build_inventory([_raw(role="button", tag="button", name="")], REFUSE_PACK)
    assert recs[0]["name"] == ""
    assert recs[0]["best_effort_name"] is True


# ─── Danger classification ─────────────────────────────────────────────────────


def test_danger_flag_on_irreversible_button():
    recs = build_inventory([
        _raw(role="button", tag="button", name="Delete policy"),
        _raw(role="button", tag="button", name="Pay now"),
        _raw(role="link", tag="a", name="Log out"),
        _raw(role="button", tag="button", name="Save draft"),
    ], REFUSE_PACK)
    by_name = {r["name"]: r for r in recs}
    assert by_name["Delete policy"]["danger"] is True
    assert by_name["Delete policy"]["danger_rule_id"] == "delete"
    assert by_name["Pay now"]["danger"] is True
    assert by_name["Pay now"]["danger_rule_id"] == "pay"
    assert by_name["Log out"]["danger"] is True
    assert by_name["Save draft"]["danger"] is False


def test_danger_only_applies_to_actionable_controls():
    # A TEXT field named 'Delete reason' is not an actuator — never danger.
    danger, rule, severity = classify_control_danger(
        "Delete reason", "text", "textbox", REFUSE_PACK)
    assert danger is False and rule == ""


def test_danger_carries_guard_severity():
    danger, rule, severity = classify_control_danger(
        "Delete policy", "button", "button", REFUSE_PACK)
    assert danger is True and rule == "delete" and severity == "critical"


def test_danger_no_refuse_pack_is_fail_closed():
    # Fail-CLOSED: with no vetted policy, an actionable control is a never-click.
    danger, rule, severity = classify_control_danger(
        "Delete policy", "button", "button", None)
    assert danger is True and rule != ""


def test_no_pack_still_ignores_non_actionable_controls():
    # A text field is never an actuator, even with no pack — no false never-click.
    danger, rule, severity = classify_control_danger(
        "Notes", "text", "textbox", None)
    assert danger is False


# ─── Anchor disambiguation (only on (frame, role, name) collision) ─────────────


def test_repeated_control_gets_row_anchor_unique_does_not():
    raw = [
        _raw(role="button", tag="button", name="Select",
             landmark={"role": "row", "name": "Term Life 20yr"}),
        _raw(role="button", tag="button", name="Select",
             landmark={"role": "row", "name": "Whole Life"}),
        _raw(role="button", tag="button", name="Find plans",
             landmark={"role": "region", "name": "Quote"}),
    ]
    recs = build_inventory(raw, REFUSE_PACK)
    selects = [r for r in recs if r["name"] == "Select"]
    assert {r["anchor"]["label"] for r in selects} == {"Term Life 20yr", "Whole Life"}
    assert all(r["anchor"]["kind"] == "row" for r in selects)
    # A unique control carries NO anchor (compiler scope = page).
    find = next(r for r in recs if r["name"] == "Find plans")
    assert find["anchor"] is None


def test_card_and_block_anchor_kinds():
    # article landmark → 'article' anchor kind.
    raw_card = [
        _raw(role="button", tag="button", name="Choose",
             landmark={"role": "article", "name": "Gold plan"}),
        _raw(role="button", tag="button", name="Choose",
             landmark={"role": "article", "name": "Silver plan"}),
    ]
    recs = build_inventory(raw_card, REFUSE_PACK)
    assert all(r["anchor"]["kind"] == "article" for r in recs)

    # A non-landmark ancestor (role=form) → text-based 'block' fallback.
    raw_block = [
        _raw(role="textbox", tag="input", input_type="text", name="Amount",
             landmark={"role": "form", "name": "Beneficiary 1"}),
        _raw(role="textbox", tag="input", input_type="text", name="Amount",
             landmark={"role": "form", "name": "Beneficiary 2"}),
    ]
    recs = build_inventory(raw_block, REFUSE_PACK)
    assert {r["anchor"]["label"] for r in recs} == {"Beneficiary 1", "Beneficiary 2"}
    assert all(r["anchor"]["kind"] == "block" for r in recs)


def test_collision_across_frames_is_not_ambiguous():
    # Same name in the main frame and inside an iframe ⇒ frameLocator already
    # disambiguates ⇒ NO anchor attached on either.
    raw = [
        _raw(role="button", tag="button", name="Submit", frame_selector="",
             landmark={"role": "row", "name": "Main"}),
        _raw(role="button", tag="button", name="Submit", frame_selector="iframe#pay",
             landmark={"role": "row", "name": "Payment"}),
    ]
    recs = build_inventory(raw, REFUSE_PACK)
    assert all(r["anchor"] is None for r in recs)


def test_colliding_control_without_landmark_stays_unanchored():
    raw = [
        _raw(role="button", tag="button", name="Edit", landmark={"role": "", "name": ""}),
        _raw(role="button", tag="button", name="Edit", landmark={"role": "", "name": ""}),
    ]
    recs = build_inventory(raw, REFUSE_PACK)
    assert all(r["anchor"] is None for r in recs)   # honest: cannot invent a locator


# ─── iframe + shadow transparency ──────────────────────────────────────────────


def test_iframe_control_preserves_frame_selector():
    recs = build_inventory([
        _raw(role="textbox", tag="input", input_type="text", name="Card number",
             frame_selector="iframe#stripe"),
    ], REFUSE_PACK)
    assert recs[0]["frame_selector"] == "iframe#stripe"
    assert recs[0]["qec"]["frame_selector"] == "iframe#stripe"


def test_shadow_control_is_plain_main_frame_control():
    # Open shadow DOM does NOT change the frame — Playwright pierces it — so a
    # shadow-hosted control carries an empty frame_selector, refined normally.
    recs = build_inventory([
        _raw(role="button", tag="button", name="Sign", frame_selector=""),
    ], REFUSE_PACK)
    assert recs[0]["frame_selector"] == ""
    assert recs[0]["kind"] == "button"


# ─── Determinism / order preservation ──────────────────────────────────────────


def test_inventory_is_order_preserving_and_deterministic():
    raw = [
        _raw(role="textbox", tag="input", input_type="text", name="First name"),
        _raw(role="textbox", tag="input", input_type="text", name="Last name"),
        _raw(role="button", tag="button", name="Continue"),
    ]
    a = build_inventory(raw, REFUSE_PACK)
    b = build_inventory(raw, REFUSE_PACK)
    assert [r["name"] for r in a] == ["First name", "Last name", "Continue"]
    assert a == b


# ─── Injected-JS constant sanity (not executed locally) ────────────────────────


def test_injected_js_is_a_self_invoking_expression():
    js = INVENTORY_JS.strip()
    assert js.startswith("(()") and js.endswith(")()")
    # Balanced braces is a cheap syntactic guard for a non-executed string.
    assert js.count("{") == js.count("}")
    assert js.count("(") == js.count(")")
    assert INVENTORY_JS_VERSION == "inv-js-v4"
