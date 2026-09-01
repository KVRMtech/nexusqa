"""T-FE-03 / T-FE-04 — ONE COHERENT PERSON, AND THE SAME ONE EVERY CRAWL.

The old identity was one person with no relationships and no money.  Every field
about a spouse, a beneficiary, a child or an employer was answered with the
applicant, and every money field was answered with the constant ``100`` — so an
annual income and a coverage amount came back as the same number, and a policy
named its own insured as the beneficiary of the death benefit.  No carrier
accepts either, and the rejection was then reported as the application's fault.

These tests read the persona the way an APPLICATION reads it: they re-derive
each cross-field rule from the finished object rather than trusting the code
that built it.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.fill_engine.learning import identity_seed
from app.fill_engine.persona import (MARITAL_MARRIED, OCCUPATION_BANDS,
                                     derive_persona)
from app.identity_pack import derive as derive_identity

#: A spread of seeds, so a rule that holds for one household and not the next is
#: caught here rather than on a client's application.
SEEDS = [f"tenant-{i % 5}::app-{i}" for i in range(120)]


# ── coherence ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", SEEDS)
def test_every_persona_is_internally_coherent(seed):
    """The headline invariant. Filling many fields only helps if the values
    AGREE WITH EACH OTHER — an underwriting rule checks the agreement, not the
    count."""
    persona = derive_persona(seed)
    report = persona.coherence_report()
    broken = [rule for rule, ok in report.items() if not ok]
    assert not broken, f"{seed}: {broken}"


def test_age_is_recomputed_from_the_birth_date_not_merely_stored():
    """Coherence in the direction the application checks it: age FROM date."""
    persona = derive_persona("coherence::age")
    born = date.fromisoformat(persona.applicant.date_of_birth)
    # Re-derive the reference day the same way an application would, from the
    # identity's own claim, and confirm the arithmetic closes.
    ref = born.replace(year=born.year + persona.applicant.age)
    recomputed = ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))
    assert recomputed == persona.applicant.age


def test_a_leap_day_applicant_is_coherent_on_every_day_of_the_year():
    """29 February, pinned — the one birth date the age arithmetic cannot round.

    ``_reference_date_for`` recovers the day a persona's ages are true on by
    moving the birth date forward by the claimed age. For a leap-day birth that
    day does not exist in a common year, and the fallback used to be 28 February
    — the day BEFORE the birthday, on which the person is a year younger than
    they say they are. Every leap-day persona was therefore internally
    incoherent: it presented an application with an age and a date of birth that
    contradicted each other, which is exactly the rejection this module exists
    to stop being blamed on the application.

    It is pinned HERE, with the identity supplied, rather than left to the seed
    sweep above. ``identity_pack.derive`` builds the birth date from
    ``date.today()``, so whether any of the 120 seeds lands on 29 February
    depends on the DAY THE SUITE RUNS: the defect passed CI for weeks and then
    failed on 21 August 2026 with no code change between the two runs. A rule
    that only holds on most days is not a rule.
    """
    from dataclasses import replace

    base = derive_identity("leap::probe")
    # LEAP YEARS, named rather than computed from the current year — including
    # 2000, the century that IS a leap year, and 1900 is deliberately absent
    # because it is not. `date.fromisoformat` below refuses 29 February in a
    # common year, so a typo here fails loudly instead of silently testing an
    # ordinary date.
    age = 40
    for birth_year in (1984, 1988, 1996, 2000):
        dob = f"{birth_year}-02-29"
        assert date.fromisoformat(dob).day == 29
        ident = replace(base, date_of_birth=dob, age=age)

        persona = derive_persona("leap::probe", identity=ident)
        broken = [rule for rule, ok in persona.coherence_report().items() if not ok]
        assert not broken, (
            f"a persona born on 29 February {birth_year} claiming age {age} "
            f"is incoherent: {broken}")
        assert persona.applicant.age == age, (
            f"the leap-day applicant's age was rewritten to "
            f"{persona.applicant.age}, expected {age}")


def test_marital_status_and_the_spouse_agree_in_both_directions():
    """A form that asks for a marital status AND spouse details cross-validates
    them; declaring "single" and then naming a spouse is rejected, and so is the
    reverse."""
    for seed in SEEDS[:40]:
        persona = derive_persona(seed)
        assert (persona.marital_status == MARITAL_MARRIED) == (persona.spouse is not None)


def test_dependents_exist_before_dependent_names_are_asked_for():
    """"Dependents must exist before dependent names appear" — the count and the
    people are one fact, not two."""
    for seed in SEEDS[:40]:
        persona = derive_persona(seed)
        assert persona.dependents == len(persona.children)
        for index in range(persona.dependents):
            assert persona.child(index) is not None
        assert persona.child(persona.dependents) is None


def test_a_child_is_young_enough_to_be_one():
    for seed in SEEDS[:40]:
        persona = derive_persona(seed)
        for child in persona.children:
            assert child.age < persona.applicant.age - 19, seed


# ── money ────────────────────────────────────────────────────────────────────

def test_income_is_plausible_for_the_occupation():
    """"Income must be plausible for occupation" — the band comes from the job
    title the identity already chose, so the two cannot contradict."""
    for seed in SEEDS:
        persona = derive_persona(seed)
        low, high, _ = OCCUPATION_BANDS[persona.employment.job_title.lower()]
        assert low <= persona.money.annual_income <= high, seed


def test_no_money_field_is_the_old_constant():
    """The defect verbatim: every currency field answered ``100``."""
    for seed in SEEDS[:30]:
        money = derive_persona(seed).money
        assert 100 not in (money.annual_income, money.coverage_amount,
                           money.annual_premium, money.household_income)


def test_the_money_figures_on_one_page_are_different_numbers():
    """A page asking for income, coverage and premium must not receive one
    number three times — which is exactly what a constant guarantees."""
    for seed in SEEDS[:30]:
        money = derive_persona(seed).money
        distinct = {money.annual_income, money.coverage_amount,
                    money.annual_premium, money.deductible}
        assert len(distinct) == 4, seed


def test_coverage_and_premium_stand_in_a_sane_relation_to_income():
    for seed in SEEDS:
        persona = derive_persona(seed)
        money = persona.money
        assert money.coverage_amount > money.annual_income, seed
        assert money.annual_premium < money.annual_income, seed
        assert money.monthly_premium * 12 <= money.annual_premium * 1.1 + 12


def test_money_differs_between_personas():
    """Derived from the persona, therefore a different persona gives a different
    number.  A constant would make this test impossible to write."""
    amounts = {derive_persona(seed).money.coverage_amount for seed in SEEDS}
    assert len(amounts) > 1


# ── relationships ────────────────────────────────────────────────────────────

def test_the_beneficiary_is_never_the_applicant():
    """A policy cannot name its own insured as the beneficiary of the death
    benefit; the old engine did exactly that on every application."""
    for seed in SEEDS:
        persona = derive_persona(seed)
        assert persona.beneficiary.full_name != persona.applicant.full_name, seed
        assert persona.contingent_beneficiary.full_name != persona.applicant.full_name


def test_the_two_beneficiaries_are_two_different_people():
    for seed in SEEDS:
        persona = derive_persona(seed)
        assert (persona.beneficiary.full_name
                != persona.contingent_beneficiary.full_name), seed


def test_relatives_share_the_applicants_family_name():
    """A spouse and children who share no surname with the applicant is the kind
    of quiet incoherence a human reviewer spots instantly."""
    for seed in SEEDS[:40]:
        persona = derive_persona(seed)
        family = persona.applicant.family_name
        if persona.spouse:
            assert persona.spouse.family_name == family
        for child in persona.children:
            assert child.family_name == family


def test_an_unmarried_persona_simply_has_no_spouse():
    """Returning ``None`` rather than the applicant is the whole point: a spouse
    field on an unmarried applicant does not apply, and answering it with the
    applicant is the defect."""
    unmarried = next(derive_persona(s) for s in SEEDS
                     if derive_persona(s).marital_status != MARITAL_MARRIED)
    assert unmarried.person("spouse") is None


# ── adverse facts ────────────────────────────────────────────────────────────

def test_no_persona_ever_volunteers_an_adverse_fact():
    """A crawl that discloses tobacco use has fabricated a fact about a person on
    an insurance application.  The negative answer completes the form just as
    well, so it is never varied by seed."""
    assert not any(derive_persona(seed).tobacco_user for seed in SEEDS)


# ── determinism and isolation (T-FE-04) ──────────────────────────────────────

def test_two_derivations_of_one_seed_produce_the_identical_person():
    """Deterministic replay: a value recorded in evidence months ago must be
    regenerable exactly."""
    for seed in SEEDS[:20]:
        assert derive_persona(seed).as_dict() == derive_persona(seed).as_dict()


def test_different_applications_get_different_people():
    """Isolation: one tenant's two applications must not share an applicant, or a
    value remembered for one leaks into the other's evidence."""
    names = {derive_persona(identity_seed("t1", f"app-{i}")).applicant.full_name
             for i in range(40)}
    assert len(names) > 1


def test_the_applicant_is_the_identity_verbatim():
    """Backward compatibility, asserted rather than assumed: every value the old
    engine produced from ``identity.x`` still comes from the same place."""
    identity = derive_identity("compat::seed")
    persona = derive_persona("compat::seed", identity=identity)
    assert persona.applicant.full_name == identity.full_name
    assert persona.applicant.date_of_birth == identity.date_of_birth
    assert persona.applicant.age == identity.age
    assert persona.applicant.email == identity.email
    assert persona.employment.employer_name == identity.company


def test_a_persona_grown_around_an_explicit_identity_keeps_that_identity():
    """Callers hold an identity derived against an explicit reference date; a
    household that silently re-derived it would put a DIFFERENT applicant in the
    same form."""
    identity = derive_identity("explicit", today=date(2025, 6, 1))
    persona = derive_persona("explicit", identity=identity)
    assert persona.identity is identity
    assert persona.is_coherent()
