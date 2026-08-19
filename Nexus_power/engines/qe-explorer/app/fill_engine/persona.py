"""ONE COHERENT HOUSEHOLD — the applicant and everybody the form asks about.

:mod:`app.identity_pack` already builds one internally-consistent person, and
that person is unchanged here: :attr:`Persona.identity` IS the identity the rest
of the crawler has always used, so nothing that reads it shifts.

What it could not do is answer a form that asks about somebody ELSE.  A life
application asks for a spouse's date of birth, a beneficiary's name and
relationship, a child's age, an employer's address and an annual income that has
to look like the occupation two fields up.  With one person and no
relationships, every one of those fields was answered with the applicant —
internally inconsistent in precisely the way an underwriting rule checks.

So an identity becomes a HOUSEHOLD:

    applicant      the identity, verbatim
    spouse         present if and only if the marital status says married
    children       exactly ``dependents`` of them, each younger than the
                   applicant by a plausible span
    beneficiary    a REAL other person — the spouse when there is one, else the
                   eldest adult child, else a sibling; never the applicant, and
                   never a name invented at the point of use
    employer       the identity's own company, with an income band that fits the
                   identity's own job title
    money          income, coverage, premium, deductible and savings, all
                   derived from that income

COHERENCE IS THE POINT, and it is asserted rather than hoped for.
:meth:`Persona.coherence_report` re-derives every cross-field rule FROM THE
FINISHED OBJECT — age from date of birth, married exactly when a spouse exists,
dependents equal to the number of children, income inside the band its
occupation implies — which is what the acceptance test reads.

LEAST-ASSERTIVE ON ADVERSE FACTS.  Tobacco use, medical history and anything
else an underwriter prices against is answered NEGATIVELY and never varied by
seed.  Not because a smoker is implausible, but because a crawl that volunteers
a medical history for a synthetic applicant has invented a fact about a person
on an insurance application; the negative answer completes the form just as
well.  The same rule already governs multi-select health questions
(``vocab.NEGATIVE_OPTION_RE``).

DETERMINISTIC.  No clock, no network, no randomness — the same seed yields the
same household, always, so a value recorded in evidence months ago is
reproducible today.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..identity_pack import Identity, derive as derive_identity

__all__ = [
    "Person", "Employment", "Money", "Persona", "derive_persona",
    "MARITAL_SINGLE", "MARITAL_MARRIED", "MARITAL_DIVORCED", "MARITAL_WIDOWED",
    "OCCUPATION_BANDS",
]

MARITAL_SINGLE = "single"
MARITAL_MARRIED = "married"
MARITAL_DIVORCED = "divorced"
MARITAL_WIDOWED = "widowed"

#: The applicant's job title decides the income band.  The titles are exactly
#: the ones :mod:`app.identity_pack` chooses from, so the income does not need a
#: second, independent — and therefore possibly contradictory — draw.  Bands are
#: ordinary full-time US salary ranges; the only property that matters is that
#: the number is PLAUSIBLE FOR THE TITLE, which is what an affordability or
#: underwriting rule actually checks.
OCCUPATION_BANDS: dict[str, tuple[int, int, str]] = {
    "operations analyst":  (62_000, 94_000, "Professional Services"),
    "account manager":     (68_000, 118_000, "Sales"),
    "systems engineer":    (95_000, 155_000, "Technology"),
    "compliance officer":  (78_000, 132_000, "Financial Services"),
    "programme lead":      (105_000, 165_000, "Professional Services"),
    "field supervisor":    (58_000, 88_000, "Construction"),
}
#: A title the identity pack does not know about still needs a band.
_DEFAULT_BAND = (55_000, 95_000, "General")

#: Given names for the OTHER members of the household.  A different pool from
#: the identity's on purpose, so a spouse can never come out as the applicant
#: wearing the same first name.
_RELATIVE_GIVEN = (
    "Adele", "Brendan", "Camille", "Dominic", "Esme", "Franklin", "Giselle",
    "Hollis", "Ione", "Jasper", "Kendra", "Lucian", "Maeve", "Nolan", "Odette",
    "Pierce", "Rhiannon", "Silas", "Tamsin", "Vaughn",
)

_DEDUCTIBLES = (500, 1_000, 2_500, 5_000)
_TERM_YEARS = (10, 15, 20, 30)


def _stream(seed: str, *, length: int = 48) -> list[int]:
    """A deterministic byte stream — the single source of every choice below."""
    out: list[int] = []
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(f"{seed}::{counter}".encode("utf-8")).digest())
        counter += 1
    return out[:length]


def _round_to(value: float, step: int) -> int:
    """Round to a step a human would actually write on a form.  A coverage
    amount of 1,347,912 is arithmetically derived and obviously synthetic; the
    same figure at 1,350,000 is the number an applicant would enter."""
    if step <= 0:
        return int(value)
    return int(round(value / step) * step)


@dataclass(frozen=True)
class Person:
    """One member of the household — complete enough to answer any field a form
    can ask about a person, so no consumer ever has to invent a missing half."""

    role: str
    given_name: str
    family_name: str
    full_name: str
    date_of_birth: str
    age: int
    gender: str
    email: str
    phone: str
    national_id: str
    #: How this person stands to the APPLICANT ("self", "spouse", "child",
    #: "sibling").  A relationship dropdown is answered from this, so the answer
    #: agrees with the person the adjacent name fields describe.
    relationship: str

    def as_dict(self) -> dict[str, str]:
        """Non-secret projection for evidence.  Everyone here is fictional by
        construction, so recording WHAT was used is what makes a run auditable."""
        return {
            "role": self.role, "full_name": self.full_name,
            "date_of_birth": self.date_of_birth, "age": str(self.age),
            "gender": self.gender, "relationship": self.relationship,
        }


@dataclass(frozen=True)
class Employment:
    """The applicant's employer, as a form asks about it — an entity in its own
    right, which is why an "Employer Name" field must not receive a person."""

    employer_name: str
    job_title: str
    industry: str
    annual_income: int
    years_employed: int
    employer_phone: str
    employer_street: str
    employer_city: str
    employer_region_code: str
    employer_postal_code: str
    status: str          # employed | self-employed | retired


@dataclass(frozen=True)
class Money:
    """Every currency figure the household implies.

    A single page asks for several of them at once, and they must not all be the
    same number: an annual income of 100 and a coverage amount of 100 is exactly
    the shape the old constant produced, and no underwriting rule accepts it."""

    annual_income: int
    monthly_income: int
    other_annual_income: int
    household_income: int
    coverage_amount: int
    annual_premium: int
    monthly_premium: int
    deductible: int
    savings: int
    monthly_expenses: int
    existing_coverage: int

    def as_dict(self) -> dict[str, int]:
        return {
            "annual_income": self.annual_income,
            "monthly_income": self.monthly_income,
            "other_annual_income": self.other_annual_income,
            "household_income": self.household_income,
            "coverage_amount": self.coverage_amount,
            "annual_premium": self.annual_premium,
            "monthly_premium": self.monthly_premium,
            "deductible": self.deductible,
            "savings": self.savings,
            "monthly_expenses": self.monthly_expenses,
            "existing_coverage": self.existing_coverage,
        }


@dataclass(frozen=True)
class Persona:
    """The whole household, plus the identity it was grown from."""

    seed: str
    identity: Identity
    applicant: Person
    spouse: Optional[Person]
    children: tuple[Person, ...]
    beneficiary: Person
    contingent_beneficiary: Person
    employment: Employment
    money: Money
    marital_status: str
    dependents: int
    tobacco_user: bool
    term_years: int
    #: Everyone, by role key, for a resolver that has a role and wants a person.
    _by_role: dict[str, Person] = field(default_factory=dict, repr=False)

    def person(self, role: str) -> Optional[Person]:
        """The household member playing ``role``, or ``None`` when the household
        has nobody in it.  An unmarried applicant genuinely has no spouse, and
        answering a spouse field with the applicant is the defect, not the fix."""
        return self._by_role.get(str(role or "").strip().lower())

    def child(self, index: int = 0) -> Optional[Person]:
        return self.children[index] if 0 <= index < len(self.children) else None

    def coherence_report(self) -> dict[str, bool]:
        """Re-derive every cross-field rule FROM THE FINISHED OBJECT.

        Not a formality.  Asserting the rules here rather than only at
        construction means a future edit that breaks one is caught by a test
        that reads the persona the way an application does, instead of by
        trusting the code that built it."""
        ref = _reference_date_for(self.identity)
        checks: dict[str, bool] = {}
        checks["age_matches_dob"] = self.applicant.age == _age_on(
            self.applicant.date_of_birth, ref)
        checks["every_member_age_matches_dob"] = all(
            p.age == _age_on(p.date_of_birth, ref)
            for p in self._by_role.values())
        checks["married_iff_spouse"] = (
            (self.marital_status == MARITAL_MARRIED) == (self.spouse is not None))
        checks["dependents_match_children"] = self.dependents == len(self.children)
        checks["children_younger_than_applicant"] = all(
            c.age < self.applicant.age for c in self.children)
        checks["beneficiary_is_not_applicant"] = (
            self.beneficiary.full_name != self.applicant.full_name)
        checks["contingent_differs_from_primary"] = (
            self.contingent_beneficiary.full_name != self.beneficiary.full_name)
        lo, hi, _ = OCCUPATION_BANDS.get(
            self.employment.job_title.strip().lower(), _DEFAULT_BAND)
        checks["income_plausible_for_occupation"] = (
            lo <= self.money.annual_income <= hi)
        checks["coverage_exceeds_income"] = (
            self.money.coverage_amount > self.money.annual_income)
        checks["premium_below_income"] = (
            self.money.annual_premium < self.money.annual_income)
        checks["monthly_income_consistent"] = (
            abs(self.money.monthly_income * 12 - self.money.annual_income) <= 12)
        checks["employer_matches_identity"] = (
            self.employment.employer_name == self.identity.company)
        checks["years_employed_fit_age"] = (
            0 < self.employment.years_employed <= max(1, self.applicant.age - 18))
        checks["spouse_shares_family_name"] = (
            self.spouse is None
            or self.spouse.family_name == self.applicant.family_name)
        return checks

    def is_coherent(self) -> bool:
        return all(self.coherence_report().values())

    def as_dict(self) -> dict[str, object]:
        """Evidence projection — what this crawl presented itself as."""
        return {
            "seed": self.seed,
            "applicant": self.applicant.as_dict(),
            "spouse": self.spouse.as_dict() if self.spouse else None,
            "children": [c.as_dict() for c in self.children],
            "beneficiary": self.beneficiary.as_dict(),
            "contingent_beneficiary": self.contingent_beneficiary.as_dict(),
            "marital_status": self.marital_status,
            "dependents": self.dependents,
            "tobacco_user": self.tobacco_user,
            "employer": self.employment.employer_name,
            "job_title": self.employment.job_title,
            "money": self.money.as_dict(),
        }


def _reference_date(seed: str) -> date:
    """The day the household derived from ``seed`` has its ages true on."""
    return _reference_date_for(derive_identity(seed))


def _reference_date_for(ident: Identity) -> date:
    """The day this persona's ages are true on.

    Derived from the identity's OWN birth date and claimed age rather than from
    the clock, so ``age`` and ``date_of_birth`` cannot drift apart between the
    run that recorded them and the run that replays them.
    ``identity_pack.derive`` reads ``date.today()`` when it builds the birth
    date; we recover its reference day by asking the identity what age it
    claims, which is a fact stored in the object rather than read from a clock."""
    birth = date.fromisoformat(ident.date_of_birth)
    try:
        return birth.replace(year=birth.year + ident.age)
    except ValueError:                       # 29 February
        return birth.replace(year=birth.year + ident.age, day=28)


def _age_on(iso_birth: str, ref: date) -> int:
    b = date.fromisoformat(iso_birth)
    return ref.year - b.year - ((ref.month, ref.day) < (b.month, b.day))


def _birth_for_age(ref: date, age: int, day_offset: int) -> str:
    """A birth date that PRODUCES ``age`` on ``ref``.

    Coherence in the direction an application checks it, which is always
    age-from-date and never the reverse."""
    try:
        anchor = ref.replace(year=ref.year - age)
    except ValueError:
        anchor = ref.replace(year=ref.year - age, day=28)
    birth = anchor - timedelta(days=day_offset % 300)
    # A birthday that has not happened yet this year makes the person a year
    # younger than intended; walk back a year rather than lie about the age.
    guard = 0
    while _age_on(birth.isoformat(), ref) != age and guard < 3:
        birth = birth - timedelta(days=365)
        guard += 1
    return birth.isoformat()


def _person(seed: str, *, role: str, relationship: str, family_name: str,
            age: int, ref: date, gender_bit: int, index: int = 0) -> Person:
    b = _stream(f"{seed}::{role}::{index}", length=16)
    given = _RELATIVE_GIVEN[b[0] % len(_RELATIVE_GIVEN)]
    full = f"{given} {family_name}"
    dob = _birth_for_age(ref, age, b[1])
    # RFC 2606 reserves example.com forever and the NANP reserves 555-01xx for
    # fiction — the same two guarantees the identity pack relies on, for the same
    # reason: a synthetic person's contact details must be structurally incapable
    # of reaching a real one.
    email = f"{given.lower()}.{family_name.lower()}{b[2] % 100:02d}@example.com"
    phone = f"{200 + (b[3] % 700)}5550{100 + (b[4] % 100)}"
    nid = f"9{b[5] % 100:02d}-{1 + (b[6] % 99):02d}-{1 + (b[7] % 9999):04d}"
    return Person(
        role=role, given_name=given, family_name=family_name, full_name=full,
        date_of_birth=dob, age=_age_on(dob, ref),
        gender="female" if gender_bit % 2 == 0 else "male",
        email=email, phone=phone, national_id=nid, relationship=relationship,
    )


def derive_persona(seed: str, *, identity: Optional[Identity] = None) -> Persona:
    """Build the one household this crawl will present itself as.

    ``seed`` must be stable for a tenant + APPLICATION and never carry a
    per-crawl artifact id, or the applicant changes between runs and every rate
    quote reads as a regression when nothing regressed.
    :mod:`app.fill_engine.learning` owns that key.

    ``identity`` GROWS THE HOUSEHOLD AROUND A PERSON WE ALREADY HAVE.  Every
    caller inside the crawl is handed an :class:`~app.identity_pack.Identity`
    that was derived once for the whole run, sometimes against an explicit
    reference date; re-deriving it here from the seed alone would produce a
    household whose applicant is a DIFFERENT person from the one the rest of the
    fill is using, which is the exact class of incoherence this module exists to
    remove.  Pass the identity and the applicant is that identity, verbatim."""
    seed = str(seed or "qec")
    identity = identity if identity is not None else derive_identity(seed)
    ref = _reference_date_for(identity)
    b = _stream(f"{seed}::household")

    applicant = Person(
        role="applicant", given_name=identity.given_name,
        family_name=identity.family_name, full_name=identity.full_name,
        date_of_birth=identity.date_of_birth, age=identity.age,
        gender="female" if b[0] % 2 == 0 else "male",
        email=identity.email, phone=identity.phone,
        national_id=identity.national_id, relationship="self",
    )

    # ── household shape ──────────────────────────────────────────────────
    marital = (MARITAL_MARRIED, MARITAL_SINGLE, MARITAL_MARRIED,
               MARITAL_DIVORCED, MARITAL_MARRIED, MARITAL_WIDOWED)[b[1] % 6]
    spouse = None
    if marital == MARITAL_MARRIED:
        spouse_age = max(21, applicant.age - 4 + (b[2] % 9))
        spouse = _person(seed, role="spouse", relationship="spouse",
                         family_name=identity.family_name, age=spouse_age,
                         ref=ref, gender_bit=b[0] + 1)

    # A child must be young enough to BE one: the applicant was at least 20 when
    # they were born, so no child is older than that allows.
    max_child_age = max(0, applicant.age - 20)
    dependents = 0 if max_child_age < 1 else (b[3] % 4)
    children: list[Person] = []
    for i in range(dependents):
        child_age = 1 + ((b[4 + i] + i * 7) % max(1, max_child_age))
        children.append(_person(seed, role="child", relationship="child",
                                family_name=identity.family_name,
                                age=child_age, ref=ref, gender_bit=b[8 + i],
                                index=i))
    children_t = tuple(children)

    # ── beneficiaries ────────────────────────────────────────────────────
    # A REAL other person, chosen by the household's own shape.  The old engine
    # answered these with the applicant, which every carrier rejects: a policy
    # cannot name its own insured as the beneficiary of their death benefit.
    sibling = _person(seed, role="sibling", relationship="sibling",
                      family_name=identity.family_name,
                      age=max(21, applicant.age - 6 + (b[12] % 13)), ref=ref,
                      gender_bit=b[13])
    adult_children = [c for c in children_t if c.age >= 18]
    if spouse is not None:
        primary, contingent = spouse, (adult_children[0] if adult_children
                                       else sibling)
    elif adult_children:
        primary, contingent = adult_children[0], sibling
    else:
        primary = sibling
        contingent = _person(seed, role="sibling", relationship="sibling",
                             family_name=identity.family_name,
                             age=max(21, applicant.age - 10 + (b[14] % 19)),
                             ref=ref, gender_bit=b[15], index=1)
    # Distinctness is a rule, not a hope: two beneficiaries with one name is the
    # same incoherence as naming the applicant, only harder to see.
    attempt = 2
    while contingent.full_name == primary.full_name and attempt < 8:
        contingent = _person(seed, role="sibling", relationship="sibling",
                             family_name=identity.family_name,
                             age=max(21, applicant.age - 12 + (b[16] % 21)),
                             ref=ref, gender_bit=b[17] + attempt, index=attempt)
        attempt += 1
    beneficiary = _relabel(primary, "beneficiary")
    contingent_beneficiary = _relabel(contingent, "contingent_beneficiary")

    # ── employment + money ───────────────────────────────────────────────
    lo, hi, industry = OCCUPATION_BANDS.get(
        identity.job_title.strip().lower(), _DEFAULT_BAND)
    span = max(1, (hi - lo) // 500)
    income = min(hi, max(lo, lo + (b[18] % span) * 500))
    years_employed = 1 + (b[19] % max(1, applicant.age - 18))
    employment = Employment(
        employer_name=identity.company, job_title=identity.job_title,
        industry=industry, annual_income=income, years_employed=years_employed,
        employer_phone=f"{200 + (b[20] % 700)}5550{100 + (b[21] % 100)}",
        employer_street=f"{200 + (b[22] % 800)} Commerce Park",
        employer_city=identity.city, employer_region_code=identity.region_code,
        employer_postal_code=identity.postal_code, status="employed",
    )

    # Coverage follows the industry's own rule of thumb (about ten times income),
    # rounded the way an applicant would write it and clamped to a range a
    # carrier actually offers.  Premium follows from coverage and age, so a page
    # showing both cannot contradict itself.
    coverage = min(5_000_000, max(50_000, _round_to(income * 10, 50_000)))
    annual_premium = max(180, _round_to(
        coverage / 1000.0 * (0.9 + max(0, applicant.age - 30) * 0.06), 12))
    other_income = _round_to(income * ((b[23] % 20) / 100.0), 500)
    spouse_income = 0
    if spouse is not None:
        spouse_income = _round_to(income * (0.55 + (b[24] % 60) / 100.0), 500)
    money = Money(
        annual_income=income,
        monthly_income=income // 12,
        other_annual_income=other_income,
        household_income=income + spouse_income + other_income,
        coverage_amount=coverage,
        annual_premium=annual_premium,
        monthly_premium=max(15, _round_to(annual_premium / 12.0, 1)),
        deductible=_DEDUCTIBLES[b[25] % len(_DEDUCTIBLES)],
        savings=_round_to(income * (0.4 + (b[26] % 120) / 100.0), 1_000),
        monthly_expenses=_round_to(income / 12.0 * 0.55, 50),
        existing_coverage=_round_to(income * (b[27] % 4), 25_000),
    )

    by_role: dict[str, Person] = {
        "applicant": applicant, "self": applicant, "insured": applicant,
        "beneficiary": beneficiary,
        "contingent_beneficiary": contingent_beneficiary,
        "sibling": sibling,
    }
    if spouse is not None:
        by_role["spouse"] = spouse
    for i, c in enumerate(children_t):
        by_role[f"child:{i}"] = c
    if children_t:
        by_role["child"] = children_t[0]

    return Persona(
        seed=seed, identity=identity, applicant=applicant, spouse=spouse,
        children=children_t, beneficiary=beneficiary,
        contingent_beneficiary=contingent_beneficiary, employment=employment,
        money=money, marital_status=marital, dependents=len(children_t),
        # ADVERSE FACTS ARE NEVER INVENTED — see the module docstring.  A crawl
        # that discloses tobacco use on an application has fabricated a fact
        # about a person, and the negative answer completes the form just as well.
        tobacco_user=False,
        term_years=_TERM_YEARS[b[28] % len(_TERM_YEARS)],
        _by_role=by_role,
    )


def _relabel(person: Person, role: str) -> Person:
    """The same human, wearing the role this form asks about them in.

    A beneficiary IS the spouse; giving the role its own object rather than a
    reference is what lets ``persona.person('beneficiary')`` and
    ``persona.person('spouse')`` both answer without either one implying the
    other is a different person."""
    return Person(
        role=role, given_name=person.given_name, family_name=person.family_name,
        full_name=person.full_name, date_of_birth=person.date_of_birth,
        age=person.age, gender=person.gender, email=person.email,
        phone=person.phone, national_id=person.national_id,
        relationship=person.relationship,
    )
