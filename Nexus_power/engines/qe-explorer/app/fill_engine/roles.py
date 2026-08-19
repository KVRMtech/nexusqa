"""WHOSE FIELD IS THIS? — possessor-aware semantic resolution.

:mod:`app.field_semantics` answers WHAT a field is: a full name, a date of
birth, a phone number.  That is necessary and it is not sufficient, because a
form asks the same question about several different people:

    Applicant Name        →  the applicant
    Spouse Date of Birth  →  the spouse
    Beneficiary Name      →  the beneficiary
    Employer Name         →  the employer, which is not a person at all
    Child 2 First Name    →  the second dependent

The old engine classified all five as a name or a date and answered every one of
them from the applicant.  On a life application that is not a cosmetic error: the
beneficiary and the insured come back identical, which is a state no carrier
accepts, and the crawl gets a rejection it then reports as the application's
fault.

So resolution has TWO axes, decided independently here:

    possessor   the entity the value belongs to
    subject     whether the field asks about that entity's ORGANISATION rather
                than about the person ("Employer Name", "Company Phone")

WHERE THE EVIDENCE COMES FROM, strongest first:

  1. the control's own accessible name — "Beneficiary First Name" says it;
  2. the SECTION it sits in — a bare "First Name" under a legend reading
     "Beneficiary Information" belongs to the beneficiary, and this is the
     common case in real applications, which label the group once and the
     fields plainly;
  3. nothing — the applicant, which is the right default and the one the old
     engine applied unconditionally.

ORDINALS.  "Beneficiary 2 Last Name", "Child #3 Date of Birth" and "Dependent
(2) Age" all carry an index, and a form with two beneficiaries must not receive
one person twice.  The index is parsed and carried, and the persona resolves it.

PURE + DETERMINISTIC.  No I/O, no clock.  Reads only product UI text, never a
value — the same discipline as :mod:`app.field_signature`, and for the same
reason: this runs on every control of every crawl.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "Possessor", "resolve_possessor",
    "ROLE_APPLICANT", "ROLE_SPOUSE", "ROLE_CHILD", "ROLE_BENEFICIARY",
    "ROLE_CONTINGENT_BENEFICIARY", "ROLE_EMPLOYER", "ROLE_AGENT",
    "ROLE_EMERGENCY_CONTACT", "PERSON_ROLES",
]

ROLE_APPLICANT = "applicant"
ROLE_SPOUSE = "spouse"
ROLE_CHILD = "child"
ROLE_BENEFICIARY = "beneficiary"
ROLE_CONTINGENT_BENEFICIARY = "contingent_beneficiary"
ROLE_EMPLOYER = "employer"
ROLE_AGENT = "agent"
ROLE_EMERGENCY_CONTACT = "emergency_contact"

#: Roles that name a PERSON.  ``employer`` is deliberately absent: an employer
#: field wants an organisation, and handing it a person is the mirror image of
#: the defect this module closes.
PERSON_ROLES = frozenset({
    ROLE_APPLICANT, ROLE_SPOUSE, ROLE_CHILD, ROLE_BENEFICIARY,
    ROLE_CONTINGENT_BENEFICIARY, ROLE_AGENT, ROLE_EMERGENCY_CONTACT,
})

#: ORDER IS THE RULE.  The first pattern that matches wins, so the more specific
#: possessor must come first: "contingent beneficiary" before "beneficiary",
#: "spouse" before the applicant's own words.  Each entry is
#: ``(pattern, role)`` and every pattern is matched against the control name
#: FIRST and the section heading second.
_POSSESSOR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:contingent|secondary|alternate)\s+beneficiar", re.I),
     ROLE_CONTINGENT_BENEFICIARY),
    (re.compile(r"\bbeneficiar(?:y|ies)\b", re.I), ROLE_BENEFICIARY),
    # "Spouse", and the words applications actually use for one.  "Partner" is
    # included; "business partner" is excluded below by the veto list, because
    # it is an organisation relationship and not a household one.
    (re.compile(r"\b(?:spouse|spousal|husband|wife|partner|co[\s-]?applicant"
                r"|joint\s+applicant)\b", re.I), ROLE_SPOUSE),
    (re.compile(r"\b(?:child|children|son|daughter|dependent|dependant|minor)\b",
                re.I), ROLE_CHILD),
    (re.compile(r"\b(?:emergency\s+contact|next\s+of\s+kin)\b", re.I),
     ROLE_EMERGENCY_CONTACT),
    (re.compile(r"\b(?:agent|advisor|adviser|producer|broker|representative)\b",
                re.I), ROLE_AGENT),
    (re.compile(r"\b(?:applicant|insured|proposer|policy\s*holder|policyholder"
                r"|owner|member|primary)\b", re.I), ROLE_APPLICANT),
)

#: Words that make a "partner"/"owner" mention ORGANISATIONAL rather than
#: household.  Without this, "Business Partner Name" resolves to the spouse.
_ORG_CONTEXT_VETO = re.compile(
    r"\b(?:business|corporate|company|firm|trading|channel)\b", re.I)

#: A ROLE MENTIONED AS A REFERENT IS NOT THE POSSESSOR.
#:
#: "Relationship to Insured" is a field ABOUT the beneficiary that merely names
#: the applicant as the other end of the relationship; "Relation to Applicant"
#: and "Payable to Owner" have the same shape.  Reading the referent as the
#: possessor answered a beneficiary's relationship dropdown from the applicant —
#: the very substitution this module exists to stop, and an easy one to miss,
#: because the field really does contain the word "Insured".
#:
#: Only "to" and "with" count as referent markers.  "of" is deliberately
#: excluded: "Date of Birth of Spouse" is possessive, not referential, and
#: getting that wrong would break the common case in order to fix the rare one.
_REFERENT_MARKER_RE = re.compile(r"\b(?:to|with)\s+(?:the\s+|your\s+)?$", re.I)

#: The field asks about an ORGANISATION.  Checked independently of the
#: possessor, because "Spouse Employer Name" is an organisation belonging to the
#: spouse — two facts, not one.
_ORGANISATION_RE = re.compile(
    r"\b(?:employer|employers|company|companies|business|organi[sz]ation"
    r"|organisation|firm|corporation|corp|employer's|workplace|trust|estate)\b",
    re.I)

#: "Name of employer", "name of your company" — the possessive written the other
#: way round.  A separate pattern because the organisation word follows the
#: field word rather than qualifying it.
_ORGANISATION_OF_RE = re.compile(
    r"\bname\s+of\s+(?:your\s+|the\s+)?(?:employer|company|business|firm)\b",
    re.I)

#: An ordinal written any of the ways forms write them: "Beneficiary 2",
#: "Child #3", "Dependent (2)", "Second Beneficiary".
_ORDINAL_DIGIT_RE = re.compile(r"[#(\[]?\s*(\d{1,2})\s*[)\]]?\s*$|"
                               r"\b(\d{1,2})\b")
_ORDINAL_WORDS = {
    "first": 0, "primary": 0, "1st": 0,
    "second": 1, "secondary": 1, "2nd": 1,
    "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
}

#: Section headings that mean "these fields are about the applicant", so a
#: section does not have to be silent for the default to be right.
_SELF_SECTION_RE = re.compile(
    r"\b(?:your|about\s+you|personal\s+(?:details|information)"
    r"|applicant|insured|contact\s+(?:details|information))\b", re.I)


@dataclass(frozen=True)
class Possessor:
    """Who — and what — a field is about.

    ``role``      one of the ``ROLE_*`` constants.
    ``index``     which one, when the form asks about several (0-based).
    ``organisation``  the field wants an ORGANISATION belonging to ``role``,
                  not the person: "Spouse Employer Name" is
                  ``role=spouse, organisation=True``.
    ``basis``     which rung decided it — ``control_name``, ``section`` or
                  ``default``.  Carried so a resolution can always be explained
                  rather than merely trusted, exactly like a semantic verdict.
    ``evidence``  the text the decision was read from, bounded.  Product UI
                  text, never a value.
    """

    role: str = ROLE_APPLICANT
    index: int = 0
    organisation: bool = False
    basis: str = "default"
    evidence: str = ""

    @property
    def is_person(self) -> bool:
        return (not self.organisation) and self.role in PERSON_ROLES

    @property
    def is_applicant(self) -> bool:
        return self.role == ROLE_APPLICANT and not self.organisation

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "index": self.index,
                "organisation": self.organisation, "basis": self.basis}


def _text(value: Any) -> str:
    return " ".join(("" if value is None else str(value)).split())


def _section_text(control: Mapping[str, Any]) -> str:
    """The heading the control sits under, from whatever the inventory captured.

    ``section`` is the explicit field; ``anchor.label`` is the nearest landmark
    name, which the inventory already computes for collision disambiguation and
    which is the same string a legend or a card heading produces."""
    section = _text(control.get("section"))
    if section:
        return section
    anchor = control.get("anchor")
    if isinstance(anchor, Mapping):
        return _text(anchor.get("label"))
    qec = control.get("qec")
    if isinstance(qec, Mapping):
        return _text(qec.get("section")) or _text(qec.get("group_label"))
    return ""


def _match_role(text: str) -> Optional[tuple[str, str]]:
    """First matching possessor rule, with the phrase it matched on."""
    if not text:
        return None
    for pattern, role in _POSSESSOR_RULES:
        m = pattern.search(text)
        if not m:
            continue
        if role == ROLE_SPOUSE and _ORG_CONTEXT_VETO.search(text):
            # "Business Partner Name" is not a household relationship.  Keep
            # looking rather than returning the wrong person.
            continue
        if _REFERENT_MARKER_RE.search(text[:m.start()]):
            # The role is the OTHER END of a relationship this field describes,
            # not the owner of the value.  Keep looking; the section heading
            # usually names the real possessor.
            continue
        return role, m.group(0)
    return None


def _ordinal(text: str, role: str) -> int:
    """Which one of several.  Zero when the form asks about only one.

    Read from the words immediately around the ROLE mention, never from the
    whole label: "Beneficiary 2 Address Line 1" must yield beneficiary 2, not
    beneficiary 1 from the trailing "Line 1"."""
    if not text or not role:
        return 0
    for pattern, r in _POSSESSOR_RULES:
        if r != role:
            continue
        m = pattern.search(text)
        if not m:
            continue
        # A word ordinal in the ~14 characters before the role mention.
        before = text[max(0, m.start() - 14):m.start()].lower()
        for word, idx in _ORDINAL_WORDS.items():
            if re.search(r"\b" + re.escape(word) + r"\b", before):
                return idx
        # A digit in the ~6 characters after it.
        after = text[m.end():m.end() + 6]
        d = re.search(r"[#(\[]?\s*(\d{1,2})\b", after)
        if d:
            n = int(d.group(1))
            return max(0, n - 1)     # forms count from one; we index from zero
        return 0
    return 0


def resolve_possessor(control: Mapping[str, Any], *, name: str = "",
                      section: str = "") -> Possessor:
    """Decide whose field this is, and say which rung decided it.

    ``name``/``section`` override what the control carries — the caller
    sometimes has a better label (a group's question text) than the element
    does.  Everything read here is product UI text; no value is ever consulted.
    """
    label = _text(name) or _text(control.get("name")) or _text(
        control.get("label"))
    heading = _text(section) or _section_text(control)
    placeholder = _text(control.get("placeholder"))

    # An organisation field is recognised independently of the possessor, so
    # "Spouse Employer Name" keeps BOTH facts.
    org_source = f"{label} {placeholder}"
    organisation = bool(_ORGANISATION_RE.search(org_source)
                        or _ORGANISATION_OF_RE.search(org_source))

    hit = _match_role(label)
    basis, evidence = "control_name", ""
    if hit is None:
        hit = _match_role(placeholder)
        if hit is not None:
            basis = "placeholder"
    if hit is None and heading:
        # THE COMMON REAL CASE: the group is labelled once and the fields are
        # labelled plainly.  A bare "First Name" under "Beneficiary Information"
        # is a beneficiary's first name, and reading only the control's own name
        # is exactly how the old engine answered it with the applicant.
        if not _SELF_SECTION_RE.search(heading):
            hit = _match_role(heading)
            if hit is not None:
                basis = "section"
    if hit is None:
        # An organisation field with no possessor mention belongs to the
        # applicant's employer — "Employer Name" on a page about you.
        if organisation:
            return Possessor(role=ROLE_EMPLOYER, index=0, organisation=True,
                             basis="control_name", evidence=label[:80])
        return Possessor(basis="default", evidence="")

    role, matched = hit
    source = label if basis == "control_name" else (
        placeholder if basis == "placeholder" else heading)
    index = _ordinal(source, role)
    if organisation and role == ROLE_APPLICANT:
        # "Applicant Employer Name" — the possessor mention is the applicant, but
        # the value wanted is their employer.
        return Possessor(role=ROLE_EMPLOYER, index=0, organisation=True,
                         basis=basis, evidence=matched[:80])
    return Possessor(role=role, index=index, organisation=organisation,
                     basis=basis, evidence=matched[:80])
