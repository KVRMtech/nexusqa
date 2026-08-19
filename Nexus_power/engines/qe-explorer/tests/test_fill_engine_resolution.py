"""T-FE-05 / T-FE-06 / T-FE-08 — WHOSE FIELD, WHICH THIRD, AND WHAT SHAPE.

Three defects that look unrelated and share one cause: the generator was handed
a semantic TYPE and nothing else.

  * "Beneficiary Name", "Spouse Name" and "Employer Name" all classify as a
    name, so all three were answered with the applicant;
  * a split date of birth classifies as three date-of-birth fields, so all
    three received ``date_of_birth[:4]`` — the YEAR — including the month and
    day dropdowns;
  * a field declaring ``pattern`` classifies BY the pattern and was then filled
    without reference to it.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import field_semantics as S
from app.fill_engine import constraints as C
from app.fill_engine import patterns as P
from app.fill_engine.generator import dob_part, generate, money_kind
from app.fill_engine.persona import derive_persona
from app.fill_engine.roles import (ROLE_APPLICANT, ROLE_BENEFICIARY,
                                   ROLE_CHILD, ROLE_CONTINGENT_BENEFICIARY,
                                   ROLE_EMPLOYER, ROLE_SPOUSE,
                                   resolve_possessor)

PERSONA = derive_persona("tenant::summit-life")
#: A household that definitely HAS a spouse and children, so the relationship
#: assertions below are about resolution and not about an empty household.
MARRIED = next(derive_persona(f"t::a{i}") for i in range(200)
               if derive_persona(f"t::a{i}").spouse is not None
               and derive_persona(f"t::a{i}").children)


def ctl(**kw):
    kw.setdefault("kind", "text")
    return kw


def gen(name, control=None, *, section="", persona=PERSONA, semantic=None,
        answer_choices=True):
    control = control or ctl(name=name)
    control.setdefault("name", name)
    sem = semantic or S.classify({
        "tokens": name.lower().replace("'", " ").split(),
        "kind": control.get("kind", ""),
        "input_type": control.get("input_type", ""),
        "constraints": ("p=" + control["pattern"]) if control.get("pattern") else "",
    })["type"]
    return generate(sem, control, persona, kind=control.get("kind", ""),
                    name=name, section=section, answer_choices=answer_choices)


# ── T-FE-06 · possessor-aware resolution ─────────────────────────────────────

@pytest.mark.parametrize("label,role", [
    ("First Name", ROLE_APPLICANT),
    ("Applicant Last Name", ROLE_APPLICANT),
    ("Beneficiary Name", ROLE_BENEFICIARY),
    ("Primary Beneficiary First Name", ROLE_BENEFICIARY),
    ("Contingent Beneficiary Name", ROLE_CONTINGENT_BENEFICIARY),
    ("Spouse Date of Birth", ROLE_SPOUSE),
    ("Wife's Full Name", ROLE_SPOUSE),
    ("Child Name", ROLE_CHILD),
    ("Dependent 2 Date of Birth", ROLE_CHILD),
    ("Employer Name", ROLE_EMPLOYER),
    ("Name of your employer", ROLE_EMPLOYER),
])
def test_the_possessor_is_read_from_the_label(label, role):
    assert resolve_possessor(ctl(name=label), name=label).role == role


def test_a_bare_label_under_a_beneficiary_heading_belongs_to_the_beneficiary():
    """THE COMMON REAL CASE. Applications label the group once and the fields
    plainly, so reading only the control's own name answers every field in the
    beneficiary section with the applicant."""
    poss = resolve_possessor(ctl(name="First Name"), name="First Name",
                             section="Beneficiary Information")
    assert poss.role == ROLE_BENEFICIARY and poss.basis == "section"


def test_the_control_name_outranks_the_section():
    poss = resolve_possessor(ctl(name="Spouse First Name"),
                             name="Spouse First Name",
                             section="Beneficiary Information")
    assert poss.role == ROLE_SPOUSE and poss.basis == "control_name"


def test_a_role_named_as_a_referent_is_not_the_possessor():
    """"Relationship to Insured" is a field ABOUT the beneficiary that merely
    names the applicant as the other end of the relationship."""
    poss = resolve_possessor(ctl(name="Relationship to Insured"),
                             name="Relationship to Insured",
                             section="Beneficiary Information")
    assert poss.role == ROLE_BENEFICIARY


def test_a_possessive_of_is_still_a_possessor():
    """Only "to"/"with" mark a referent; "of" is possessive and must keep
    working, or the common case breaks to fix the rare one."""
    assert resolve_possessor(ctl(name="Date of Birth of Spouse"),
                             name="Date of Birth of Spouse").role == ROLE_SPOUSE


def test_a_business_partner_is_not_a_household_partner():
    poss = resolve_possessor(ctl(name="Business Partner Name"),
                             name="Business Partner Name")
    assert poss.role != ROLE_SPOUSE and poss.organisation


def test_an_ordinal_selects_which_one():
    assert resolve_possessor(ctl(name="Beneficiary 2 Last Name"),
                             name="Beneficiary 2 Last Name").index == 1
    assert resolve_possessor(ctl(name="Second Beneficiary Name"),
                             name="Second Beneficiary Name").index == 1
    assert resolve_possessor(ctl(name="Child #3 Age"),
                             name="Child #3 Age").index == 2


def test_a_beneficiary_field_receives_the_beneficiary():
    """The defect verbatim: it used to receive the applicant."""
    got = gen("Beneficiary Name").value
    assert got == PERSONA.beneficiary.full_name
    assert got != PERSONA.applicant.full_name


def test_a_spouse_field_receives_the_spouse():
    got = gen("Spouse First Name", persona=MARRIED).value
    assert got == MARRIED.spouse.given_name
    assert got != MARRIED.applicant.given_name


def test_a_child_field_receives_that_child():
    first = gen("Child 1 First Name", persona=MARRIED).value
    assert first == MARRIED.children[0].given_name
    if len(MARRIED.children) > 1:
        assert gen("Child 2 First Name",
                   persona=MARRIED).value == MARRIED.children[1].given_name


def test_an_employer_field_receives_the_employer_and_never_a_person():
    assert gen("Employer Name").value == PERSONA.employment.employer_name
    # The subtler half: these classify as ordinary contact fields and used to be
    # answered with the APPLICANT'S own mobile and home address.
    assert gen("Employer Phone", ctl(name="Employer Phone", input_type="tel")).value \
        == PERSONA.employment.employer_phone
    assert gen("Employer Phone", ctl(name="Employer Phone", input_type="tel")).value \
        != PERSONA.applicant.phone


def test_a_spouse_field_on_a_single_persona_is_left_empty_not_filled_with_the_applicant():
    """The household genuinely has nobody there.  Answering with the applicant
    would contradict the marital status the same form already collected."""
    single = next(derive_persona(f"s::{i}") for i in range(200)
                  if derive_persona(f"s::{i}").spouse is None)
    candidate = gen("Spouse First Name", persona=single)
    assert candidate.value is None
    assert "no spouse" in candidate.rationale


def test_a_relationship_dropdown_agrees_with_the_person_beside_it():
    control = ctl(name="Relationship to Insured", kind="select",
                  options=["Select", "Spouse", "Child", "Parent", "Other"])
    candidate = gen("Relationship to Insured", control,
                    section="Beneficiary Information", persona=MARRIED)
    assert candidate.value == "Spouse"
    assert candidate.value.lower() == MARRIED.beneficiary.relationship


# ── T-FE-05 · split date of birth ────────────────────────────────────────────

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _split_dob(persona):
    month = ctl(name="Birth Month", kind="select", options=["Select"] + MONTHS)
    day = ctl(name="Birth Day", kind="select",
              options=["Select"] + [str(d) for d in range(1, 32)])
    year = ctl(name="Birth Year", kind="select",
               options=["Select"] + [str(y) for y in range(1940, 2011)])
    return month, day, year


def test_each_third_of_a_split_birth_date_gets_its_own_third():
    """The defect verbatim: the month, the day AND the year selects all received
    ``date_of_birth[:4]`` — the year."""
    month_ctl, day_ctl, year_ctl = _split_dob(PERSONA)
    born = date.fromisoformat(PERSONA.applicant.date_of_birth)
    assert gen("Birth Month", month_ctl, semantic=S.DOB).value == MONTHS[born.month - 1]
    assert gen("Birth Day", day_ctl, semantic=S.DOB).value == str(born.day)
    assert gen("Birth Year", year_ctl, semantic=S.DOB).value == str(born.year)


def test_the_three_parts_reassemble_into_the_personas_own_birth_date():
    """The property that matters: not three plausible values, ONE date."""
    for seed in [f"dob::{i}" for i in range(25)]:
        persona = derive_persona(seed)
        month_ctl, day_ctl, year_ctl = _split_dob(persona)
        rebuilt = date(
            int(generate(S.DOB, year_ctl, persona, kind="select",
                         name="Birth Year").value),
            MONTHS.index(generate(S.DOB, month_ctl, persona, kind="select",
                                  name="Birth Month").value) + 1,
            int(generate(S.DOB, day_ctl, persona, kind="select",
                         name="Birth Day").value))
        assert rebuilt.isoformat() == persona.applicant.date_of_birth, seed


def test_the_part_is_read_from_the_options_when_the_label_is_silent():
    """Real applications label these ``mm``/``dd``/``yyyy`` or not at all, so the
    option set is often the only evidence there is."""
    assert dob_part(ctl(name="", kind="select", options=MONTHS)) == "month"
    assert dob_part(ctl(name="", kind="select",
                        options=[str(d) for d in range(1, 32)])) == "day"
    assert dob_part(ctl(name="", kind="select",
                        options=[str(y) for y in range(1950, 2011)])) == "year"


@pytest.mark.parametrize("label,part", [
    ("MM", "month"), ("DD", "day"), ("YYYY", "year"),
    ("Birth Month", "month"), ("Year of Birth", "year"),
])
def test_the_part_is_read_from_the_label_when_it_speaks(label, part):
    assert dob_part(ctl(name=label, kind="select"), name=label) == part


def test_a_spouses_split_birth_date_comes_from_the_spouse():
    """Both axes at once — the right THIRD of the right PERSON."""
    month_ctl = ctl(name="Spouse Birth Month", kind="select",
                    options=["Select"] + MONTHS)
    born = date.fromisoformat(MARRIED.spouse.date_of_birth)
    got = generate(S.DOB, month_ctl, MARRIED, kind="select",
                   name="Spouse Birth Month")
    assert got.value == MONTHS[born.month - 1]


# ── T-FE-08 · constraint-aware generation ────────────────────────────────────

@pytest.mark.parametrize("pattern", [
    r"\d{3}-\d{2}-\d{4}", r"\d{9}", r"[0-9]{5}(-[0-9]{4})?",
    r"[A-Z]{2}\d{6}", r"\(\d{3}\) \d{3}-\d{4}", r"(19|20)\d{2}",
    r"[A-Za-z]{2,10}", r"POL-[0-9]{6}",
])
def test_a_declared_pattern_is_satisfied_on_the_first_attempt(pattern):
    """Repair should be the exception, not the path."""
    control = ctl(name="Reference", pattern=pattern)
    value = gen("Reference", control).value
    assert value is not None, pattern
    assert P.matches(value, pattern), (pattern, value)


def test_a_pattern_reshapes_the_meaningful_value_rather_than_replacing_it():
    """An identity number the persona chose is the SAME number without its
    dashes; regenerating one would discard a value the funnel re-uses on every
    page that asks for it."""
    packed = gen("Social Security Number",
                 ctl(name="Social Security Number", pattern=r"\d{9}")).value
    assert packed == PERSONA.applicant.national_id.replace("-", "")


def test_an_unsupported_pattern_is_refused_rather_than_guessed():
    """A backreference or a lookaround is outside the supported subset, and a
    value that looks right and is not is worse than none."""
    assert P.satisfy(r"(\d)\1") is None
    assert P.satisfy(r"(?<=x)y") is None


def test_a_numeric_range_is_honoured_and_snapped_to_its_step():
    control = ctl(name="Coverage Amount", input_type="number",
                  min="50000", max="250000", step="25000")
    value = float(gen("Coverage Amount", control).value)
    assert 50000 <= value <= 250000
    assert (value - 50000) % 25000 == 0


def test_a_maxlength_makes_room_by_dropping_punctuation_not_information():
    value = gen("Phone", ctl(name="Phone", input_type="tel", maxlength="10")).value
    assert value == PERSONA.applicant.phone[:10] and value.isdigit()


def test_a_value_that_cannot_be_made_legal_is_left_empty_rather_than_typed():
    """A field the crawl could not fill is a finding; one it claims to have
    filled and did not is a lie that fails later, wearing the app's face."""
    control = ctl(name="Full Name", pattern=r"\d{4}", maxlength="2")
    assert gen("Full Name", control).value is None


def test_every_generated_value_carries_its_provenance_and_reasoning():
    """The non-functional requirement, asserted: every generated value must
    have traceable provenance."""
    candidate = gen("Annual Income", ctl(name="Annual Income", input_type="number"))
    assert candidate.source == "money.annual_income"
    assert candidate.rationale
    assert candidate.as_dict()["possessor"]["role"] == ROLE_APPLICANT


@pytest.mark.parametrize("label,attribute", [
    ("Annual Income", "annual_income"),
    ("Monthly Income", "monthly_income"),
    ("Household Income", "household_income"),
    ("Coverage Amount", "coverage_amount"),
    ("Death Benefit", "coverage_amount"),
    ("Annual Premium", "annual_premium"),
    ("Monthly Premium", "monthly_premium"),
    ("Deductible", "deductible"),
    ("Total Savings", "savings"),
])
def test_a_money_field_takes_the_figure_it_actually_asked_for(label, attribute):
    assert money_kind(ctl(name=label), name=label)[0] == attribute
    got = gen(label, ctl(name=label, input_type="number"), semantic=S.CURRENCY)
    assert got.value == str(getattr(PERSONA.money, attribute))


def test_an_unqualified_money_field_is_still_derived_from_the_persona():
    """Not a constant.  Two different personas must give two different amounts,
    which is precisely what ``100`` could never do."""
    amounts = {generate(S.CURRENCY, ctl(name="Amount", input_type="number"),
                        derive_persona(f"m::{i}"), kind="text",
                        name="Amount").value for i in range(30)}
    assert len(amounts) > 1
    assert "100" not in amounts or len(amounts) > 5


def test_constraints_report_what_the_control_declared():
    """The declaration is carried into evidence, so a reader can tell "satisfied
    a rule" from "was never constrained"."""
    declared = C.extract(ctl(name="Age", input_type="number", min="18", max="65",
                             required=True))
    assert declared.as_dict() == {"required": True, "min": 18.0, "max": 65.0,
                                  "input_type": "number"}
    assert not C.extract(ctl(name="Notes")).declared
