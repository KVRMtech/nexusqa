"""T-FE-07 / T-FE-10 — THE WHOLE ENGINE, AGAINST AN APPLICATION THAT ARGUES BACK.

Every test above this one exercises a subsystem.  This file drives the shipped
:func:`app.forms.fill_form_phase_a` against a fake life-insurance application
that behaves the way the real ones do:

  * it renders a NATIVE select, a RADIO group, a CHECKBOX group and a
    portal-rendered COMBOBOX whose options do not exist until it is opened —
    four widget classes, one page, no posture change;
  * it declares ``pattern``, ``min``/``max`` and ``required`` on the fields it
    cares about, and REJECTS values that break them, publishing the reason
    through ``aria-errormessage`` like a real form library;
  * it keeps a cookie banner up in a ``role=alert`` region for the entire fill,
    because that is what applications do and it is what used to fail every
    field on the page.

The measurements at the bottom are the quality metrics reported for this
milestone.  They are read out of the shipped result object, not computed by the
test, so a regression moves the number rather than the assertion.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.browser import RawObservation
from app.config import Settings
from app.emit import MonotonicClock
from app.field_values import DATA_MODE_AGENT
from app.fill_engine import patterns as P
from app.fill_engine.persona import derive_persona
from app.forms import AnswerKey, fill_form_phase_a
from app.guard import load_refuse_pack
from app.identity_pack import derive as derive_identity
from app.inventory import build_inventory

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)
SEED = "acme-insurance::summit-life-application"
IDENTITY = derive_identity(SEED)
PERSONA = derive_persona(SEED, identity=IDENTITY)

COOKIE_BANNER = "We use cookies to improve your experience. Accept all."
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def control(name, kind="text", **kw):
    base = {
        "role": kw.pop("role", ""), "name": name, "name_source": "label",
        "best_effort": False, "kind": kind, "tag": kw.pop("tag", "input"),
        "input_type": kw.pop("input_type", "text"), "options": kw.pop("options", []),
        "required": kw.pop("required", False), "disabled": False,
        "frame_selector": "", "testid": name.lower().replace(" ", "-"),
        "css_hint": "", "value_committed": "", "group_key": kw.pop("group_key", ""),
        "id": kw.pop("id", name.lower().replace(" ", "_")),
        "landmark": {"role": "region", "name": kw.pop("section", "")},
        "section": kw.pop("section2", ""),
    }
    base.update(kw)
    return base


def application_controls():
    """One page of a life application, as an inventory actually captures it."""
    return [
        # ── applicant, plain text ────────────────────────────────────────
        control("First Name", section2="About You"),
        control("Last Name", section2="About You"),
        control("Email", input_type="email", section2="About You"),
        # A pattern the persona's own value does not match unpunctuated.
        control("Social Security Number", pattern=r"\d{3}-\d{2}-\d{4}",
                required=True, section2="About You"),
        # A range the persona's age may or may not fall inside.
        control("Age", input_type="number", min="21", max="65", required=True,
                section2="About You"),
        control("Annual Income", input_type="number", required=True,
                section2="About You"),
        # ── split date of birth, three native selects ────────────────────
        control("Birth Month", kind="select", tag="select",
                options=["Select month"] + MONTHS, section2="About You"),
        control("Birth Day", kind="select", tag="select",
                options=["Select day"] + [str(d) for d in range(1, 32)],
                section2="About You"),
        control("Birth Year", kind="select", tag="select",
                options=["Select year"] + [str(y) for y in range(1940, 2011)],
                section2="About You"),
        # ── native select ────────────────────────────────────────────────
        control("State", kind="select", tag="select",
                options=["-- Select --", "California", "New York", "Texas",
                         IDENTITY.region_name], section2="About You"),
        # ── radio group ──────────────────────────────────────────────────
        control("Yes", kind="radio", input_type="radio", group_key="tobacco",
                id="tobacco_yes", section2="Health"),
        control("No", kind="radio", input_type="radio", group_key="tobacco",
                id="tobacco_no", section2="Health"),
        # ── checkbox group ───────────────────────────────────────────────
        control("Type 2 Diabetes", kind="checkbox", input_type="checkbox",
                group_key="conditions", section2="Health"),
        control("None of the above", kind="checkbox", input_type="checkbox",
                group_key="conditions", section2="Health"),
        # ── portal-rendered combobox (Radix/shadcn) ──────────────────────
        control("Gender", kind="select", tag="button", role="combobox",
                options=[], section2="About You"),
        # ── the beneficiary section: bare labels under a heading ─────────
        control("First Name", id="ben_first", section2="Beneficiary Information"),
        control("Last Name", id="ben_last", section2="Beneficiary Information"),
        control("Relationship to Insured", kind="select", tag="select",
                options=["Select", "Spouse", "Child", "Parent", "Other"],
                id="ben_rel", section2="Beneficiary Information"),
        # ── employer ─────────────────────────────────────────────────────
        control("Employer Name", section2="Employment"),
        control("Employer Phone", input_type="tel", section2="Employment"),
    ]


class FakeApplication:
    """A form that validates, rejects, explains — and shows a cookie banner."""

    def __init__(self, *, banner: bool = True, reject: dict | None = None,
                 combobox_options=("Male", "Female", "Other")):
        self.values: dict[str, str] = {}
        self.checked: list[str] = []
        self.banner = banner
        #: {field id: (predicate, message)} — the rules this application enforces
        #: beyond what it declared in the DOM, exactly like a zod schema does.
        self.reject = reject or {}
        self.errors: dict[str, str] = {}
        self.combobox_options = list(combobox_options)
        self._open = False
        self.commits = 0

    # ── the port surface ────────────────────────────────────────────────
    async def error_texts(self):
        out = [COOKIE_BANNER] if self.banner else []
        out.extend(self.errors.values())
        return out

    async def fill(self, ctl, value):
        return self._record(ctl, value)

    async def select_option(self, ctl, value):
        return self._record(ctl, value)

    async def set_checked(self, ctl, value):
        name = str(ctl.get("name") or "")
        if value:
            self.checked.append(name)
        return self._record(ctl, "true" if value else "false")

    async def press_key(self, key):
        self._open = False

    async def click(self, ctl):
        if str(ctl.get("role") or "") == "combobox":
            self._open = True
            return RawObservation(url_before="/a", url_after="/a")
        if str(ctl.get("role") or "") == "option":
            self.values["gender"] = str(ctl.get("name") or "")
            self._open = False
            return RawObservation(url_before="/a", url_after="/a",
                                  committed_value=str(ctl.get("name") or ""))
        return RawObservation(url_before="/a", url_after="/a")

    async def collect_controls(self):
        out = []
        if self._open:
            out.extend({"name": o, "role": "option", "kind": "option",
                        "tag": "div", "options": [], "disabled": False}
                       for o in self.combobox_options)
        for ctl in application_controls():
            record = dict(ctl)
            field_id = str(ctl.get("id") or "")
            if field_id == "gender" and self.values.get("gender"):
                record["name"] = self.values["gender"]
                record["value_committed"] = self.values["gender"]
            if field_id in self.errors:
                record["aria_invalid"] = "true"
                record["error_text"] = self.errors[field_id]
            out.append(record)
        # The error nodes a form library renders, addressed by convention.
        out.extend({"id": f"{k}-error", "text": v, "name": v}
                   for k, v in self.errors.items())
        return out

    # ── the application's own validation ────────────────────────────────
    def _record(self, ctl, value):
        self.commits += 1
        field_id = str(ctl.get("id") or "")
        rule = self.reject.get(field_id)
        if rule is not None:
            predicate, message = rule
            if not predicate(value):
                self.errors[field_id] = message
                return RawObservation(url_before="/a", url_after="/a",
                                      committed_value=value)
            self.errors.pop(field_id, None)
        self.values[field_id] = value
        return RawObservation(url_before="/a", url_after="/a",
                              committed_value=value)


def run(app=None, *, mode=DATA_MODE_AGENT):
    app = app or FakeApplication()
    built = build_inventory(application_controls(), _REFUSE,
                            url="https://app.example/apply")
    result = asyncio.run(fill_form_phase_a(
        app, built, AnswerKey.from_payload(None), MonotonicClock(),
        state_id="apply", identity=IDENTITY, data_mode=mode))
    return app, result, built


def ledger(result, name, index=0):
    rows = [e for e in result.field_ledger if e.get("name") == name]
    return rows[index] if len(rows) > index else None


# ── T-FE-07 · widget coverage, with no posture change ────────────────────────

def test_every_widget_class_on_the_page_is_answered():
    """Four widget classes, one page.  A radio group and a portal combobox used
    to need an operator to change posture before the crawl would answer them."""
    app, result, _ = run()
    for widget in ("text", "native_select", "radio_group", "checkbox_group",
                   "aria_combobox"):
        assert result.widgets_met.get(widget), f"{widget} not met"
        assert result.widgets_answered.get(widget), f"{widget} not answered"


def test_the_radio_group_is_answered_exactly_once_and_negatively():
    app, result, _ = run()
    tobacco = [n for n in app.checked if n in ("Yes", "No")]
    assert tobacco == ["No"], "one answer, and the one that invents nothing"


def test_the_checkbox_group_discloses_no_medical_history():
    app, result, _ = run()
    conditions = [n for n in app.checked
                  if n in ("Type 2 Diabetes", "None of the above")]
    assert conditions == ["None of the above"]


def test_the_portal_combobox_is_opened_picked_and_read_back():
    """A shadcn/Radix ``<Select>`` renders as a button whose options live in a
    portal that does not exist until it is opened."""
    app, result, _ = run()
    assert app.values.get("gender") in ("Male", "Female", "Other")
    assert result.open_choice_unverified == 0


def test_a_native_select_lands_on_the_personas_own_region():
    app, result, _ = run()
    assert app.values["state"] == IDENTITY.region_name


def test_no_dropdown_is_left_holding_a_placeholder():
    """"Select coverage amount…" leaves the field EMPTY while the fill reports
    success, so a validation-gated form never enables Continue."""
    app, _, _ = run()
    for field_id in ("state", "birth_month", "birth_day", "birth_year", "ben_rel"):
        assert not str(app.values.get(field_id, "")).lower().startswith("select")


# ── T-FE-05 / T-FE-06 through the shipped fill ───────────────────────────────

def test_the_split_birth_date_reassembles_into_the_personas_own():
    app, _, _ = run()
    rebuilt = date(int(app.values["birth_year"]),
                   MONTHS.index(app.values["birth_month"]) + 1,
                   int(app.values["birth_day"]))
    assert rebuilt.isoformat() == PERSONA.applicant.date_of_birth


def test_the_beneficiary_section_receives_the_beneficiary():
    """Bare labels under a heading — the case that used to fill the beneficiary
    with the insured, which no carrier accepts."""
    app, _, _ = run()
    assert app.values["ben_first"] == PERSONA.beneficiary.given_name
    assert app.values["ben_last"] == PERSONA.beneficiary.family_name
    assert app.values["ben_first"] != app.values["first_name"]


def test_the_relationship_agrees_with_the_beneficiary_beside_it():
    app, _, _ = run()
    assert app.values["ben_rel"].lower() == PERSONA.beneficiary.relationship


def test_the_employer_fields_receive_the_employer():
    app, _, _ = run()
    assert app.values["employer_name"] == PERSONA.employment.employer_name
    assert app.values["employer_phone"] == PERSONA.employment.employer_phone
    assert app.values["employer_phone"] != PERSONA.applicant.phone


def test_money_comes_from_the_persona_and_is_never_the_old_constant():
    app, _, _ = run()
    assert app.values["annual_income"] == str(PERSONA.money.annual_income)
    assert app.values["annual_income"] != "100"


def test_a_declared_pattern_is_satisfied_on_the_first_attempt():
    app, result, _ = run()
    assert P.matches(app.values["social_security_number"], r"\d{3}-\d{2}-\d{4}")
    row = ledger(result, "Social Security Number")
    assert "repair" not in row, "constraint-aware generation, not repair"


def test_a_declared_range_is_honoured():
    app, _, _ = run()
    assert 21 <= int(app.values["age"]) <= 65


# ── T-FE-02 · the banner fails nothing ───────────────────────────────────────

def test_a_cookie_banner_present_throughout_fails_no_field():
    """THE DEFECT VERBATIM. A ``role=alert`` consent banner used to be read as
    the verdict on every fill on the page."""
    app, result, _ = run(FakeApplication(banner=True))
    assert result.intent_unmet == 0
    assert result.repair_failed == []
    assert result.filled >= 15


def test_the_banner_is_counted_as_suppressed_rather_than_silently_dropped():
    """The direct measure of the false-positive class removed.  It used to be
    zero by construction, and every one of those alerts failed a field."""
    _, result, _ = run(FakeApplication(banner=True))
    assert result.alerts_suppressed > 0


def test_one_fields_error_does_not_poison_the_fields_that_follow():
    """An error raised by an early field stays in the DOM while the later ones
    are filled; one real failure used to be reported as many."""
    app, result, _ = run(FakeApplication(reject={
        "age": (lambda v: False, "Age must be between 30 and 40"),
    }))
    later = ("employer_name", "employer_phone", "ben_first", "annual_income")
    for field_id in later:
        assert field_id in app.values, f"{field_id} was poisoned by the Age error"
    assert [f["name"] for f in result.repair_failed] == ["Age"]


# ── T-FE-01 · rejected values are repaired and accepted ──────────────────────

def test_a_rejected_value_is_repaired_and_the_application_accepts_it():
    """The end-to-end proof: the application states a rule it never declared in
    the DOM, rejects, explains, and the engine satisfies it."""
    app, result, _ = run(FakeApplication(reject={
        "age": (lambda v: v.isdigit() and int(v) >= 40,
                "Age must be at least 40 for this product"),
    }))
    assert int(app.values["age"]) >= 40
    assert result.repaired == 1
    row = ledger(result, "Age")
    assert row["repair"]["accepted"] and row["repair"]["attempt_count"] == 2


def test_the_repair_records_why_the_second_value_was_chosen():
    _, result, _ = run(FakeApplication(reject={
        "age": (lambda v: v.isdigit() and int(v) >= 40,
                "Age must be at least 40 for this product"),
    }))
    reason = ledger(result, "Age")["repair"]["attempts"][1]["reason"]
    assert "Age must be at least 40" in reason
    assert "min" in reason


def test_an_unrepairable_field_is_a_named_finding_not_a_silent_gap():
    app, result, _ = run(FakeApplication(reject={
        "age": (lambda v: False, "Age is not eligible"),
    }))
    assert [f["name"] for f in result.repair_failed] == ["Age"]
    assert result.repair_failed[0]["stop_reason"]


def test_repair_stays_the_exception_and_first_pass_is_the_rule():
    """Constraint-aware generation is supposed to make repair rare."""
    _, result, _ = run()
    assert result.repaired == 0
    assert result.first_pass == result.filled


def test_a_clean_fill_pays_nothing_for_the_verdict_read():
    """The latency claim, measured: the expensive control re-read runs only on
    suspicion, and a page with a banner and no errors raises none."""
    _, result, _ = run(FakeApplication(banner=True))
    assert result.verdict_reads == 0


# ── T-FE-04 / T-FE-09 · stability across crawls ──────────────────────────────

def test_two_crawls_of_one_application_fill_the_identical_values():
    """Deterministic replay, end to end: the same application, crawled twice,
    presents itself as the same person."""
    first, _, _ = run()
    second, _, _ = run()
    assert first.values == second.values
    assert first.checked == second.checked


def test_a_different_application_gets_a_different_applicant():
    other_identity = derive_identity("acme-insurance::a-different-product")
    built = build_inventory(application_controls(), _REFUSE, url="https://x/apply")
    app = FakeApplication()
    asyncio.run(fill_form_phase_a(
        app, built, AnswerKey.from_payload(None), MonotonicClock(),
        state_id="apply", identity=other_identity, data_mode=DATA_MODE_AGENT))
    baseline, _, _ = run()
    assert app.values["first_name"] != baseline.values["first_name"]


# ── the quality report ───────────────────────────────────────────────────────

def test_the_measured_completion_is_validated_not_merely_attempted():
    """"Measure actual validated completion — not attempted fills."

    Every number below comes out of the shipped result object."""
    _, result, built = run()
    fillable = [c for c in built
                if c.get("kind") in ("text", "date", "select", "checkbox",
                                     "radio", "toggle")
                and str(c.get("name") or "")]
    answered = result.first_pass + result.repaired
    assert answered == result.filled, "every counted fill was ACCEPTED"
    # Radio and checkbox siblings are answered by their group, so the ceiling is
    # below the raw control count; two groups of two contribute one answer each.
    assert answered >= len(fillable) - 2 - result.intent_unmet
    assert result.intent_unmet == 0
    assert not result.repair_failed
