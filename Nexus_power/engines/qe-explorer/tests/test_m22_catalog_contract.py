"""M2.2 / T-BR-01..05 — THE EXPLORER HALF of the catalogue wire contract.

This suite proves the PRODUCER half: :func:`app.inventory.form_signal_for` emits
every field the frozen contract names, with the meanings it names, and
:func:`app.inventory.attach_locators` only ever reports a handle the page
declared.

Why it is a separate file from ``test_inventory.py``: that suite is free to
change as the refiner's vocabulary grows, and it should be.  These assertions
are not about this service's preferences — they are one half of an agreement
with a service this process cannot import.  Keeping them apart is what stops
someone from "updating the test to match the code" on a field the other side
still reads.

THE HOLE THIS CLOSES.  Every signal M2.2 restores was ALREADY captured by the
browser and ALREADY read by the catalogue, and died in the one function between
them that neither side's suite covered.  ``options_total`` is the clearest case:
``inventory_js`` counts it, ``catalog._options_total`` reads it, and
``form_signal_for`` never emitted it — so a clipped 250-option enumeration was
catalogued as the complete answer set, with a green suite on both sides.  A
dropped field produces a SMALLER TRUE catalogue, never a failing one.

See ``platform/qe-central/tests/contract/test_m22_catalog_contract.py`` for the
other half.  Together the two files are one proof.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.inventory import (
    LOCATOR_ACCESSIBLE_NAME,
    LOCATOR_CSS_HINT,
    LOCATOR_DOM_ID,
    LOCATOR_TESTID,
    LOCATOR_UNVERIFIED_NO_HANDLE,
    MAX_OPTIONS,
    build_inventory,
    form_signal_for,
)


def _contract() -> dict:
    """Load the frozen contract by walking up to the ``Nexus_power`` root.

    Walked rather than hard-coded because this suite is collected from the
    SERVICE root in CI and from the repository root by some local runners; a
    relative literal would pass in one and vanish in the other.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "m22_catalog_question_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/m22_catalog_question_v1.json not found above %s — the frozen "
        "wire contract is the only thing tying this service's catalogue shape to "
        "qe-central's, and it must not be deleted to make a test pass" % here
    )


CONTRACT = _contract()


def _control(**over) -> dict:
    raw = {"name": "Face Amount", "role": "textbox", "tag": "input",
           "input_type": "text"}
    raw.update(over)
    return build_inventory([raw])[0]


# ── The always-present fields ────────────────────────────────────────────────

def test_every_required_contract_field_is_emitted():
    """A field the contract calls required must be on EVERY signal.

    Not a subset check on one lucky control: asserted for a bare text input with
    no options, no constraints and nothing declared, because that is the control
    most likely to lose a field to a well-meaning "only emit it when it is
    interesting" optimisation.
    """
    sig = form_signal_for(_control())
    assert sig is not None
    missing = [f for f in CONTRACT["required_fields"] if f not in sig]
    assert not missing, (
        "form_signal_for dropped %s. qe-central reads these off "
        "form_snapshot_signals and cannot ask for them again — a dropped field "
        "is a catalogue that silently knows less, never a failure." % missing)


def test_a_button_is_not_a_question():
    """The contract's ``$always`` clause: only value-bearing controls signal."""
    for raw in ({"role": "button", "tag": "button", "name": "Continue"},
                {"role": "link", "tag": "a", "name": "Back"}):
        assert form_signal_for(build_inventory([raw])[0]) is None


# ── T-BR-05 · options_total ──────────────────────────────────────────────────

def test_options_total_survives_a_clipped_read():
    """The honesty marker for a 250-option control.

    THE DEFECT THIS PINS.  The browser counts the true total and the catalogue
    reads it; this function did not pass it on, so the catalogue floored the
    total at what it stored and a clipped enumeration presented as the complete
    answer set.  Nothing failed — the catalogue was simply wrong about the
    application, in the direction that looks like success.
    """
    over_ceiling = MAX_OPTIONS + 1
    rec = _control(role="combobox", tag="select",
                   options=[f"Option {i}" for i in range(MAX_OPTIONS)],
                   options_total=over_ceiling)
    sig = form_signal_for(rec)
    assert sig["options_total"] == over_ceiling
    assert len(sig["options"]) == MAX_OPTIONS
    assert sig["options_total"] > len(sig["options"]), (
        "a clipped read must remain visibly clipped across the boundary")


def test_options_total_never_claims_fewer_answers_than_it_carries():
    """Floored at the stored length, whatever the page said.

    A page that reports a total of 2 while rendering 5 options is not a reason
    for the catalogue to describe 5 answers as 2 — the list is the evidence, and
    the count may not contradict it.
    """
    rec = _control(role="combobox", tag="select",
                   options=["A", "B", "C", "D", "E"], options_total=2)
    assert form_signal_for(rec)["options_total"] == 5


def test_the_option_ceiling_is_the_one_the_contract_froze():
    """The injected JS, the refiner and the catalogue bound options at the SAME
    number.  When those drifted before, the same question kept 48 answers or 300
    depending on which path happened to write it first."""
    assert MAX_OPTIONS == CONTRACT["max_options"]


# ── T-BR-02 · depends_on ─────────────────────────────────────────────────────

def test_depends_on_crosses_when_the_crawl_proved_one():
    rec = _control(role="combobox", tag="select", options=["Travis"])
    rec["depends_on"] = "State"
    assert form_signal_for(rec)["depends_on"] == "State"


def test_depends_on_is_absent_when_nothing_was_proved():
    """Absence is evidence.  An unconditional question must not carry an empty
    dependency, which a consumer could read as "depends on nothing in
    particular" rather than "no dependency was observed"."""
    assert "depends_on" not in form_signal_for(_control())


# ── T-BR-03 · locator evidence ───────────────────────────────────────────────

def test_locator_carries_the_contract_fields():
    loc = form_signal_for(_control(testid="face-amount"))["locator"]
    missing = [f for f in CONTRACT["locator_required_fields"] if f not in loc]
    assert not missing, "locator dropped %s" % missing
    assert loc["strategy"] in CONTRACT["locator_strategies"]
    unknown = set(loc) - set(CONTRACT["locator_required_fields"]) - set(
        CONTRACT["locator_optional_fields"])
    assert not unknown, (
        "locator gained %s, which qe-central will not read.  Add it to the "
        "frozen contract in the same change, or it is dead weight on every page "
        "state in the fleet." % sorted(unknown))


@pytest.mark.parametrize("raw,expected", [
    ({"testid": "x", "id": "y", "name": "N"}, LOCATOR_TESTID),
    ({"id": "y", "name": "N"}, LOCATOR_DOM_ID),
    ({"name": "N"}, LOCATOR_ACCESSIBLE_NAME),
    ({"css_hint": "input.amount"}, LOCATOR_CSS_HINT),
])
def test_the_strongest_declared_handle_wins(raw, expected):
    base = {"role": "textbox", "tag": "input", "input_type": "text", "name": ""}
    base.update(raw)
    assert build_inventory([base])[0]["locator"]["strategy"] == expected


def test_a_control_the_page_identifies_by_nothing_is_unverified_not_invented():
    """The rule this whole task turns on.

    A control with no test attribute, no id, no name and no class is not
    locatable, and the honest record of that is an UNVERIFIED locator carrying
    the reason.  The tempting alternative — synthesise ``:nth-child(4)`` because
    the compiler wants something — would put a selector in a client-facing
    evidence artifact that no crawl ever resolved.
    """
    loc = build_inventory([{"role": "textbox", "tag": "input",
                            "input_type": "text"}])[0]["locator"]
    assert loc["verified"] is False
    assert loc["strategy"] == ""
    assert loc["unverified_reason"] == LOCATOR_UNVERIFIED_NO_HANDLE
    assert loc["unverified_reason"] in CONTRACT["locator_unverified_reasons"]


def test_a_handle_two_controls_share_is_not_a_locator():
    """Uniqueness is why this cannot be decided one control at a time.

    Two elements carrying the same id is not hypothetical — a repeated row
    template or a component rendered twice does it — and an id that resolves to
    two controls identifies neither.
    """
    recs = build_inventory([
        {"role": "textbox", "tag": "input", "input_type": "text", "id": "dup"},
        {"role": "textbox", "tag": "input", "input_type": "text", "id": "dup"},
    ])
    for rec in recs:
        assert rec["locator"]["strategy"] != LOCATOR_DOM_ID
        assert rec["locator"]["verified"] is False


def test_colliding_names_keep_distinct_ordinals():
    """17 identical "Yes" buttons is a real page shape (fixture 09).

    Each member keeps the DOM ordinal that separates it from its twins, so the
    locators stay distinct and each still points at its own control.
    """
    recs = build_inventory([
        {"role": "radio", "tag": "input", "input_type": "radio", "name": "Yes"}
        for _ in range(3)])
    assert [r["locator"]["match_index"] for r in recs] == [0, 1, 2]


def test_group_members_carry_their_own_locator_under_one_question():
    """T-BR-03's grouped-control clause.

    Two radios of one question share a ``group_id`` — so a reader can tell they
    answer the same question — and carry DIFFERENT handles, because they are
    different elements.  Collapsing them onto one locator would point half the
    answers at the wrong control.
    """
    recs = build_inventory([
        {"role": "radio", "tag": "input", "input_type": "radio",
         "name": "Yes", "group_key": "form:tobacco"},
        {"role": "radio", "tag": "input", "input_type": "radio",
         "name": "No", "group_key": "form:tobacco"},
    ])
    yes, no = (r["locator"] for r in recs)
    assert yes["group_id"] and yes["group_id"] == no["group_id"]
    assert yes["value"] != no["value"]


def test_only_the_accessible_name_is_advertised_as_bindable():
    """The compiler binds on the user-facing name and nothing else.

    A ``dom_id`` locator is real evidence and useless to the deterministic
    compiler, and a catalogue that did not say so would invite a generated
    script to target a handle no rung resolves.
    """
    assert form_signal_for(_control())["locator"]["bindable"] is True
    assert form_signal_for(_control(testid="t"))["locator"]["bindable"] is False


# ── Validation (the same seam, fixed one milestone earlier) ──────────────────

def test_the_validation_keys_are_the_frozen_ones():
    rec = _control(input_type="number", min="50000", max="2000000",
                   step="10000", pattern="9[0-9]{2}", minlength="3",
                   maxlength="9")
    sig = form_signal_for(rec)
    for key in CONTRACT["validation_fields"]:
        assert sig.get(key), (
            "%s is declared by the application and named in the contract; "
            "dropping it costs the scenario deriver every boundary case on this "
            "question" % key)


# ── Nothing a user typed may cross ───────────────────────────────────────────

def test_the_committed_value_never_crosses_the_boundary():
    """The catalogue is a record of QUESTIONS.  Shapes cross; answers never do."""
    sig = form_signal_for(_control(value_committed="Priya Raman"))
    assert "Priya Raman" not in json.dumps(sig)
    assert "value_committed" not in sig
