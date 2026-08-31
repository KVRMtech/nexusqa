"""A VALUE THAT IS SEMANTICALLY RIGHT *AND* CONSTRAINT-LEGAL ON THE FIRST TRY.

Three inputs decide a value, and the old generator only ever had one of them:

    semantic type   WHAT the field is           (``app.field_semantics``)
    possessor       WHOSE field it is           (``app.fill_engine.roles``)
    constraints     what the app will ACCEPT    (``app.fill_engine.constraints``)

With only the first, "Beneficiary Date of Birth" and "Date of Birth" are the
same field, a money field is a constant, and a declared ``pattern`` is
decoration.  With all three, the value is derived from the right member of the
household, in the shape the application asked for.

REPAIR IS THE EXCEPTION, NOT THE PATH.  Everything generated here is checked
against the control's own declarations before it is returned
(:func:`app.fill_engine.constraints.violations`), and reshaped until it passes
or the generator gives up and returns nothing.  A field left honestly empty is a
finding; a field filled with a value the application will reject is a round trip
we did not need to make.

EVERY VALUE CARRIES ITS REASONING.  :class:`Candidate` records the semantic
type, the possessor, the persona attribute it came from and a one-line rationale
in the application's own terms.  That is what makes a fill explainable after the
fact instead of merely reproducible — and it is what the repair loop quotes when
it says why it chose a different value the second time.

NOTHING HERE READS A VALUE FROM THE PAGE, a clock, a network or a random source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from .. import field_semantics as S
from .. import vocab
from . import constraints as C
from .options import enumerate_real, is_placeholder_option
from .persona import Persona
from .roles import (Possessor, ROLE_APPLICANT, ROLE_EMPLOYER, ROLE_CHILD,
                    ROLE_SPOUSE, ROLE_BENEFICIARY,
                    ROLE_CONTINGENT_BENEFICIARY, resolve_possessor)

__all__ = ["Candidate", "generate", "dob_part", "money_kind"]

_SPLIT_RE = re.compile(r"[^a-z0-9]+")

#: Month names, in the two spellings a dropdown uses.
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_MONTHS_SHORT = tuple(m[:3] for m in _MONTHS)

#: A field that is one THIRD of a date of birth.  Detected from the label first
#: and from the option set second — both are things the application said.
DOB_MONTH, DOB_DAY, DOB_YEAR = "month", "day", "year"
_DOB_MONTH_TOKENS = frozenset({"month", "mm", "mon", "months", "birthmonth", "mob"})
_DOB_DAY_TOKENS = frozenset({"day", "dd", "days", "birthday", "dayofbirth", "dob_day"})
_DOB_YEAR_TOKENS = frozenset({"year", "yyyy", "yy", "years", "birthyear", "yob"})

#: WHICH money figure.  A page that asks for an income, a coverage amount and a
#: premium must not receive one number three times, which is what a constant
#: guarantees and what the old ``"100"`` produced.
_MONEY_RULES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = (
    (frozenset({"household"}), frozenset(), "household_income"),
    (frozenset({"income", "salary", "wage", "wages", "earnings", "compensation"}),
     frozenset({"other", "spouse", "household"}), "annual_income"),
    (frozenset({"other"}), frozenset(), "other_annual_income"),
    (frozenset({"coverage", "benefit", "face", "insured", "sum", "protection"}),
     frozenset({"existing", "current", "force", "inforce"}), "coverage_amount"),
    (frozenset({"existing", "inforce"}), frozenset(), "existing_coverage"),
    (frozenset({"premium", "contribution"}), frozenset(), "annual_premium"),
    (frozenset({"deductible", "excess"}), frozenset(), "deductible"),
    (frozenset({"savings", "assets", "networth", "investments"}), frozenset(),
     "savings"),
    (frozenset({"expenses", "expense", "outgoings", "spending"}), frozenset(),
     "monthly_expenses"),
)
#: A money field qualified as monthly takes the monthly figure.
_MONTHLY_TOKENS = frozenset({"monthly", "month", "permonth", "mo"})
_ANNUAL_TOKENS = frozenset({"annual", "annually", "yearly", "year", "peryear", "pa"})


@dataclass(frozen=True)
class Candidate:
    """A value, and everything needed to explain it.

    ``value``        what to type, or ``None`` when nothing honest can be produced.
    ``semantic``     the closed-vocabulary type the field was classified as.
    ``possessor``    whose field it is.
    ``source``       the persona attribute the value came from — the traceable
                     provenance the non-functional requirements ask for.
    ``rationale``    one sentence, in the application's own terms.
    ``constrained``  True when the control declared something and the value was
                     shaped to satisfy it, so evidence can distinguish
                     "satisfied a declaration" from "was never constrained".
    """

    value: Optional[str]
    semantic: str = S.UNKNOWN
    possessor: Possessor = field(default_factory=Possessor)
    source: str = ""
    rationale: str = ""
    constrained: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"semantic_type": self.semantic, "source": self.source,
                "rationale": self.rationale[:200], "constrained": self.constrained,
                "possessor": self.possessor.as_dict()}


def _tokens(*texts: Any) -> set[str]:
    out: set[str] = set()
    for text in texts:
        raw = "" if text is None else str(text)
        raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
        out.update(t for t in _SPLIT_RE.split(raw.lower()) if t)
    return out


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _label(control: Mapping[str, Any], name: str = "") -> str:
    return str(name or control.get("name") or control.get("label") or "")


def dob_part(control: Mapping[str, Any], *, name: str = "") -> str:
    """Which THIRD of a birth date this control asks for.

    THE DEFECT THIS CLOSES, exactly as it shipped: a split date of birth was
    answered by handing ``identity.date_of_birth[:4]`` to the option picker for
    all three controls, so the month select, the day select and the year select
    every one of them received the YEAR.  The month and day dropdowns then either
    held nothing or held whatever option happened to contain those four digits,
    and the application rejected a birth date that was never a date.

    Two rungs, both grounded in what the application said:
      1. the control's own label / placeholder / id tokens;
      2. the SHAPE OF ITS OPTIONS — twelve entries that are month names or the
         numbers 1..12 is a month picker whatever it is labelled, 28..31 entries
         is a day picker, and four-digit entries in a plausible birth range are
         years.  Real applications label these ``mm``/``dd``/``yyyy`` or not at
         all, so the option set is often the only evidence there is.
    """
    tokens = _tokens(_label(control, name), control.get("placeholder"),
                     control.get("id"))
    if tokens & _DOB_YEAR_TOKENS:
        return DOB_YEAR
    if tokens & _DOB_MONTH_TOKENS:
        return DOB_MONTH
    if tokens & _DOB_DAY_TOKENS - {"birthday"}:
        return DOB_DAY

    options = [str(o).strip() for o in (control.get("options") or ()) if str(o).strip()]
    real = [o for o in options if not _is_prompt(o)]
    if len(real) >= 10:
        lowered = [_norm(o) for o in real]
        if any(m.lower() in lowered for m in _MONTHS) or \
                all(m in lowered for m in _MONTHS_SHORT[:3]):
            return DOB_MONTH
        numeric = [o for o in real if o.isdigit()]
        if len(numeric) >= len(real) - 1 and numeric:
            values = [int(o) for o in numeric]
            if len(values) <= 12 and max(values) <= 12:
                return DOB_MONTH
            if 27 <= len(values) <= 32 and max(values) <= 31:
                return DOB_DAY
            if all(1900 <= v <= 2100 for v in values):
                return DOB_YEAR
    return ""


def _is_prompt(label: str) -> bool:
    """A "nothing chosen yet" entry.  Deliberately thin — the canonical rule
    lives in :mod:`app.field_values` and is applied by the caller; this is only
    enough to stop a placeholder skewing the option-shape reading above."""
    text = _norm(label).strip("-–—_ .·:…")
    if not text:
        return True
    return text.split()[0] in ("select", "choose", "pick") or label.endswith(("...", "…"))


def money_kind(control: Mapping[str, Any], *, name: str = "") -> tuple[str, str]:
    """WHICH money figure this field wants, and at what cadence.

    Returns ``(attribute, cadence)`` where attribute names a field of
    :class:`app.fill_engine.persona.Money` and cadence is ``annual`` /
    ``monthly`` / ``""``."""
    tokens = _tokens(_label(control, name), control.get("placeholder"))
    cadence = ""
    if tokens & _MONTHLY_TOKENS:
        cadence = "monthly"
    elif tokens & _ANNUAL_TOKENS:
        cadence = "annual"
    for required, forbidden, attribute in _MONEY_RULES:
        if not (tokens & required) or (tokens & forbidden):
            continue
        if attribute == "annual_income" and cadence == "monthly":
            return "monthly_income", cadence
        if attribute == "annual_premium" and cadence == "monthly":
            return "monthly_premium", cadence
        return attribute, cadence
    return "", cadence


# ── choice answering ─────────────────────────────────────────────────────────

_GENDER_TOKENS = frozenset({"gender", "sex"})
_MARITAL_TOKENS = frozenset({"marital", "maritalstatus", "married"})
_RELATIONSHIP_TOKENS = frozenset({"relationship", "relation", "relationto"})
_TOBACCO_TOKENS = frozenset({"tobacco", "smoker", "smoking", "smoke", "nicotine",
                             "vape", "vaping", "cigarette", "cigarettes"})
_EMPLOYMENT_TOKENS = frozenset({"employment", "employed", "occupationstatus",
                                "workstatus"})
_DEPENDENTS_TOKENS = frozenset({"dependents", "dependants", "children",
                                "numberofchildren", "kids"})
_TERM_TOKENS = frozenset({"term", "termlength", "duration", "policyterm"})
_YESNO = frozenset({"yes", "no", "y", "n", "true", "false"})


def _person_for(persona: Persona, possessor: Possessor):
    """The household member a field belongs to, or ``None`` when the household
    genuinely has nobody in that role.

    Returning ``None`` rather than the applicant is the whole point: an
    unmarried applicant has no spouse, and answering a spouse field with the
    applicant's own name is the incoherence the form will reject."""
    role = possessor.role
    if role == ROLE_CHILD:
        return persona.child(possessor.index)
    if role == ROLE_BENEFICIARY:
        return persona.beneficiary if possessor.index == 0 else \
            persona.contingent_beneficiary
    if role == ROLE_CONTINGENT_BENEFICIARY:
        return persona.contingent_beneficiary
    return persona.person(role)


def _choice_targets(sem: str, control: Mapping[str, Any], persona: Persona,
                    possessor: Possessor, *, name: str = "",
                    ) -> tuple[list[str], str, str]:
    """Ranked option labels to look for, the persona attribute they came from,
    and a rationale.  An empty list means "no persona-specific answer" and the
    caller falls back to the least-assertive option."""
    person = _person_for(persona, possessor)
    tokens = _tokens(_label(control, name), control.get("placeholder"),
                     control.get("id"))
    who = "the applicant" if possessor.is_applicant else f"the {possessor.role}"

    if sem == S.DOB or (tokens & (_DOB_MONTH_TOKENS | _DOB_DAY_TOKENS
                                  | _DOB_YEAR_TOKENS)):
        part = dob_part(control, name=name)
        subject = person or persona.applicant
        if part:
            born = date.fromisoformat(subject.date_of_birth)
            if part == DOB_MONTH:
                return ([_MONTHS[born.month - 1], _MONTHS_SHORT[born.month - 1],
                         f"{born.month:02d}", str(born.month)],
                        f"{subject.role}.date_of_birth.month",
                        f"the MONTH of {who}'s birth date {subject.date_of_birth}")
            if part == DOB_DAY:
                return ([f"{born.day:02d}", str(born.day)],
                        f"{subject.role}.date_of_birth.day",
                        f"the DAY of {who}'s birth date {subject.date_of_birth}")
            return ([str(born.year)], f"{subject.role}.date_of_birth.year",
                    f"the YEAR of {who}'s birth date {subject.date_of_birth}")

    if tokens & _GENDER_TOKENS:
        subject = person or persona.applicant
        wanted = [subject.gender, subject.gender[:1].upper(),
                  "Male" if subject.gender == "male" else "Female"]
        return wanted, f"{subject.role}.gender", f"{who}'s gender"

    if tokens & _MARITAL_TOKENS:
        return ([persona.marital_status], "marital_status",
                f"the household is {persona.marital_status}")

    if tokens & _RELATIONSHIP_TOKENS and person is not None:
        return ([person.relationship], f"{person.role}.relationship",
                f"{who} is the applicant's {person.relationship}")

    if tokens & _TOBACCO_TOKENS:
        # NEVER VOLUNTEER AN ADVERSE FACT.  See persona: the negative answer
        # completes the question and invents nothing about a synthetic person.
        return (["no", "non-smoker", "never", "none"], "tobacco_user",
                "the persona declares no tobacco use, so the question is "
                "answered negatively rather than disclosing a priced condition")

    if tokens & _EMPLOYMENT_TOKENS:
        return ([persona.employment.status, "employed", "full time"],
                "employment.status", "the applicant is employed")

    if tokens & _DEPENDENTS_TOKENS:
        return ([str(persona.dependents)], "dependents",
                f"the household has {persona.dependents} dependent(s)")

    if tokens & _TERM_TOKENS:
        return ([f"{persona.term_years} years", str(persona.term_years)],
                "term_years", f"the persona's policy term is "
                              f"{persona.term_years} years")

    if sem == S.REGION:
        return ([persona.identity.region_name, persona.identity.region_code],
                "identity.region", "the region the applicant's postcode belongs to")
    if sem == S.COUNTRY:
        return ([persona.identity.country, "US", "USA"], "identity.country",
                "the applicant's country")
    if sem == S.CITY:
        return ([persona.identity.city], "identity.city", "the applicant's city")
    if sem == S.CARD_EXPIRY:
        return ([persona.identity.card_expiry.split("/")[0]],
                "identity.card_expiry", "the expiry month of the test card")
    if sem == S.CURRENCY:
        attribute, _ = money_kind(control, name=name)
        if attribute:
            amount = getattr(persona.money, attribute)
            return ([f"${amount:,}", f"{amount:,}", str(amount)],
                    f"money.{attribute}",
                    f"the persona's {attribute.replace('_', ' ')} of {amount}")
    if sem == S.AGE:
        subject = person or persona.applicant
        return ([str(subject.age)], f"{subject.role}.age",
                f"{who}'s age, which agrees with their birth date")
    return [], "", ""


def _least_assertive(options: Sequence[str]) -> Optional[str]:
    """The option that asserts the LEAST.

    Every member answers the question equally well and only the negative one
    invents nothing about a synthetic person — the same rule the unblock
    experiment applies, and DOM order is not a safe proxy for it: an application
    that lists "None" last would otherwise have a condition disclosed on its
    behalf."""
    for option in options:
        if vocab.NEGATIVE_OPTION_RE.match(str(option).strip()):
            return option
    return None


# ── text generation ──────────────────────────────────────────────────────────

def _person_text(sem: str, person, persona: Persona) -> tuple[Optional[str], str]:
    mapping = {
        S.GIVEN_NAME: (person.given_name, "given_name"),
        S.FAMILY_NAME: (person.family_name, "family_name"),
        S.FULL_NAME: (person.full_name, "full_name"),
        S.EMAIL: (person.email, "email"),
        S.PHONE: (person.phone, "phone"),
        S.SSN: (person.national_id, "national_id"),
        S.DOB: (person.date_of_birth, "date_of_birth"),
        S.AGE: (str(person.age), "age"),
        S.USERNAME: (persona.identity.username, "username"),
    }
    value, attribute = mapping.get(sem, (None, ""))
    return value, (f"{person.role}.{attribute}" if attribute else "")


def _employer_text(sem: str, persona: Persona) -> tuple[Optional[str], str]:
    """An organisation's fields come from the EMPLOYER, never from a person.

    "Employer Name" classified as ``company`` and was answered with the
    applicant's company, which was right by luck; "Employer Phone" classified as
    ``phone`` and was answered with the APPLICANT'S mobile number, and
    "Employer Address" with the applicant's home.  A possessor of ``employer``
    routes all three."""
    e = persona.employment
    mapping = {
        S.COMPANY: (e.employer_name, "employer_name"),
        S.FULL_NAME: (e.employer_name, "employer_name"),
        S.GIVEN_NAME: (e.employer_name, "employer_name"),
        S.FAMILY_NAME: (e.employer_name, "employer_name"),
        S.JOB_TITLE: (e.job_title, "job_title"),
        S.PHONE: (e.employer_phone, "employer_phone"),
        S.STREET: (e.employer_street, "employer_street"),
        S.CITY: (e.employer_city, "employer_city"),
        S.REGION: (persona.identity.region_name, "employer_region"),
        S.POSTAL_CODE: (e.employer_postal_code, "employer_postal_code"),
        S.CURRENCY: (str(e.annual_income), "annual_income"),
        S.FREE_TEXT: (e.industry, "industry"),
    }
    value, attribute = mapping.get(sem, (None, ""))
    return value, (f"employment.{attribute}" if attribute else "")


def _date_for_flavour(iso: str, input_type: str) -> str:
    """Render a date the way THIS input flavour demands.  A blanket ISO string
    makes a time/month/week input throw, so the field never advances."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        d = date.fromisoformat("2000-01-01")
    if input_type == "month":
        return d.strftime("%Y-%m")
    if input_type == "week":
        iso_cal = d.isocalendar()
        return f"{iso_cal[0]}-W{iso_cal[1]:02d}"
    if input_type == "datetime-local":
        return f"{d.isoformat()}T12:00"
    if input_type == "time":
        return "12:00"
    return d.isoformat()


def generate(semantic_type: str, control: Mapping[str, Any], persona: Persona, *,
             kind: str = "", name: str = "", section: str = "",
             possessor: Optional[Possessor] = None,
             cons: Optional[C.Constraints] = None,
             answer_choices: bool = True,
             today: Optional[date] = None) -> Candidate:
    """The value to type, with the reasoning that produced it.

    ``answer_choices`` is the operator's data dial reaching this layer: when
    False a semantic CHOICE (a radio group, a multi-select) is left for the
    client to make, exactly as the ``user`` data mode always did.  Everything
    else is answered either way.

    A ``value`` of ``None`` is a real answer, not a failure: for a one-time code
    or a password there is nothing a generator could invent that would mean
    anything, so the field becomes residue the client is asked for.  Inventing
    one produces a test that passes against nothing.
    """
    sem = S.coerce(semantic_type)
    k = _norm(kind) or _norm(control.get("kind"))
    label = _label(control, name)
    poss = possessor if possessor is not None else resolve_possessor(
        control, name=label, section=section)
    cons = cons if cons is not None else C.extract(control, kind=k)

    def _refuse(reason: str) -> Candidate:
        return Candidate(None, semantic=sem, possessor=poss, rationale=reason)

    if sem in S.UNGENERATABLE:
        return _refuse("no generator can honestly produce a password or a "
                       "one-time code; the field becomes residue")

    is_group = bool(control.get("group_id"))
    if not answer_choices and (k == "radio" or (k == "checkbox" and is_group)):
        return _refuse("a semantic choice is the client's to make in this data "
                       "mode; the crawl must not decide which business path is "
                       "exercised without saying so")

    # ── choice controls ──────────────────────────────────────────────────
    # A PROMPT IS NOT AN ANSWER.  ``Constraints.options`` reports what the
    # control declares, placeholders included, because that is what it is for;
    # choosing from that list unfiltered is how "Select coverage amount…" got
    # committed and the funnel stalled behind a field the ledger called filled.
    # Group members are never filtered: a set of radios or checkboxes has no
    # prompt among them by construction, and filtering deleted the member
    # labelled "None" — the only negative answer, and the one we prefer.
    if control.get("group_id") or (not control.get("options")
                                   and control.get("group_options")):
        options = [o for o in cons.options if o]
    else:
        options = enumerate_real(list(cons.options))
    if k in ("select", "radio") or options:
        wanted, source, why = _choice_targets(sem, control, persona, poss,
                                              name=label)
        picked = C.option_matching(options, *wanted) if wanted else None
        if picked is not None:
            return Candidate(picked, semantic=sem, possessor=poss, source=source,
                             rationale=why, constrained=cons.declared)
        if k == "checkbox" and is_group:
            negative = _least_assertive(options)
            if negative is not None:
                return Candidate(
                    negative, semantic=sem, possessor=poss,
                    source="least_assertive",
                    rationale="a multi-select is answered with the member that "
                              "asserts the least, so nothing is invented about "
                              "a synthetic person",
                    constrained=cons.declared)
        if wanted and options:
            # The persona HAS an answer and the control does not offer it.  A
            # yes/no question is still answerable — a tobacco question whose
            # options are "Yes"/"No" never matches the literal "non-smoker" —
            # so fall through to the negative member before giving up.
            if all(_norm(o) in _YESNO for o in options):
                negative = _least_assertive(options)
                if negative is not None:
                    return Candidate(
                        negative, semantic=sem, possessor=poss, source=source,
                        rationale=f"{why}; the control offers only yes/no, so "
                                  "the negative answer carries it",
                        constrained=cons.declared)
        if options:
            negative = _least_assertive(options) if k in ("radio", "checkbox") else None
            chosen = negative or options[0]
            return Candidate(
                chosen, semantic=sem, possessor=poss,
                source="first_offered_option" if negative is None else "least_assertive",
                rationale="no persona attribute matches this enumeration, so the "
                          + ("least-assertive" if negative else "first")
                          + " option the control itself offers was taken",
                constrained=cons.declared)
        # No readable enumeration.  The caller (the widget adapter) opens the
        # widget and takes a real option; saying so here rather than inventing a
        # label is the difference between deferring a choice and guessing one.
        if k == "select":
            return _refuse("the control offers no readable enumeration; the "
                           "widget adapter must open it and take a real option")

    if k in ("checkbox", "toggle"):
        if sem == S.CONSENT and cons.required:
            return Candidate("true", semantic=sem, possessor=poss,
                             source="required_consent",
                             rationale="a required consent is a gate, and clearing "
                                       "it changes nothing about the scenario",
                             constrained=True)
        return _refuse("an optional toggle changes what the application does; "
                       "choosing for the client would invent a scenario")

    # ── value fields ─────────────────────────────────────────────────────
    raw: Optional[str] = None
    source = ""
    who = "the applicant" if poss.is_applicant else f"the {poss.role}"

    if poss.organisation or poss.role == ROLE_EMPLOYER:
        raw, source = _employer_text(sem, persona)
        who = "the employer"
    if raw is None:
        person = _person_for(persona, poss)
        if person is None and poss.role in (ROLE_SPOUSE, ROLE_CHILD):
            # THE HOUSEHOLD GENUINELY HAS NOBODY HERE.  Answering with the
            # applicant is the defect; leaving it empty is the honest answer and
            # is also what the application expects when the question does not
            # apply.
            return _refuse(
                f"the persona has no {poss.role}, so this field does not apply; "
                "answering it with the applicant would contradict the marital "
                "status and dependants already declared")
        person = person or persona.applicant
        raw, source = _person_text(sem, person, persona)

    if raw is None:
        identity = persona.identity
        static = {
            S.COMPANY: (identity.company, "identity.company"),
            S.JOB_TITLE: (identity.job_title, "identity.job_title"),
            S.STREET: (identity.street_address, "identity.street_address"),
            S.STREET_2: (identity.street_address_2 or identity.street_address,
                         "identity.street_address_2"),
            S.CITY: (identity.city, "identity.city"),
            S.REGION: (identity.region_name, "identity.region_name"),
            S.POSTAL_CODE: (identity.postal_code, "identity.postal_code"),
            S.COUNTRY: (identity.country, "identity.country"),
            S.URL: ("https://example.com", "reserved_example_domain"),
            S.CARD_NUMBER: (identity.card_number, "identity.card_number"),
            S.CARD_CVC: (identity.card_cvc, "identity.card_cvc"),
            S.CARD_EXPIRY: (identity.card_expiry, "identity.card_expiry"),
            S.FREE_TEXT: ("autotest", "placeholder_text"),
        }
        raw, source = static.get(sem, (None, ""))

    if raw is None:
        if sem == S.DATE:
            # An unqualified date on a form is almost always "today or later"
            # (an effective date, a start date).  Derived from the persona's own
            # reference day rather than a clock, so it replays.
            raw = (today or date.today()).isoformat()
            source = "reference_date"
        elif sem == S.CURRENCY:
            attribute, _cadence = money_kind(control, name=label)
            if not attribute:
                # A MONEY FIELD IS NEVER A CONSTANT.  An unqualified amount is
                # still derived from the household, so two personas differ and
                # two fields on one page do not collide.
                amount = max(100, (persona.money.monthly_income // 10) * 10)
                source = "money.derived_generic"
            else:
                amount = getattr(persona.money, attribute)
                source = f"money.{attribute}"
            raw = str(amount)
        elif sem == S.PERCENT:
            # AN ALLOCATION IS FILLED TO ITS WHOLE. A percent whose control
            # declares min>=1 and max=100 is a SHARE the application requires to
            # be claimed and allows to be total — a beneficiary allocation, a
            # fund split, an ownership stake. The walk adds exactly one row, so
            # the coherent single-row scenario is the whole: 100. MEASURED on
            # vkpowerlife's beneficiary step (min=1 max=100, "allocations must
            # total 100%"), where the old constant 10 dead-ended the funnel one
            # page from e-sign.
            #
            # STRUCTURE, NEVER VOCABULARY — the same doctrine as filterScopeOf:
            # the rule reads the control's own declared bounds, so it holds in
            # every language the crawled application might speak. A percent
            # without that declared shape (a discount, an unconstrained rate)
            # keeps the modest default.
            if (cons.minimum is not None and cons.minimum >= 1
                    and cons.maximum == 100):
                raw = "100"
                source = "percent_whole_allocation"
            else:
                raw = "10"
                source = "percent_default"
        elif sem == S.QUANTITY:
            raw = "1"
            source = "quantity_default"
        elif sem == S.TIME:
            raw = "12:00"
            source = "time_default"
        elif sem == S.CONSENT:
            if cons.required:
                return Candidate("true", semantic=sem, possessor=poss,
                                 source="required_consent",
                                 rationale="a required consent is a gate",
                                 constrained=True)
            return _refuse("an optional consent is the client's to give")

    if raw is None:
        return _refuse(f"no persona attribute answers a {sem} field")

    # Temporal flavours need their own rendering before any constraint check —
    # an ISO string in a month input is not merely out of range, it throws.
    if sem in (S.DOB, S.DATE) and cons.input_type in ("month", "week",
                                                      "datetime-local", "time"):
        raw = _date_for_flavour(raw, cons.input_type)

    rationale = _rationale(sem, source, who, raw)

    # ── make it legal before it is typed ─────────────────────────────────
    breaches = C.violations(raw, cons, check_options=False)
    if not breaches:
        return Candidate(raw, semantic=sem, possessor=poss, source=source,
                         rationale=rationale, constrained=cons.declared)
    conformed = C.conform(raw, cons, semantic=sem)
    if conformed is None or C.violations(conformed, cons, check_options=False):
        remaining = C.violations(conformed if conformed is not None else raw,
                                 cons, check_options=False)
        codes = ", ".join(v.code for v in remaining) or "unknown"
        return _refuse(
            f"{rationale}; it breaks the control's declared {codes} and no "
            "reshaping satisfies the declaration, so the field is left honestly "
            "empty rather than filled with a value the application will reject")
    detail = ", ".join(f"{v.code}({v.detail})" for v in breaches)
    return Candidate(
        conformed, semantic=sem, possessor=poss, source=source,
        rationale=f"{rationale}; reshaped to satisfy the declared {detail}",
        constrained=True)


def _rationale(sem: str, source: str, who: str, value: str) -> str:
    if source.startswith("money."):
        return f"{source.split('.', 1)[1].replace('_', ' ')} from the persona"
    if source.startswith("employment."):
        return f"the employer's {source.split('.', 1)[1].replace('_', ' ')}"
    if "." in source:
        return f"{who}'s {source.split('.', 1)[1].replace('_', ' ')}"
    return f"a {sem.replace('_', ' ')} for {who}"
