"""SEMANTIC TYPE — what a field is FOR, decided deterministically.

The vocabulary here is CLOSED.  Every consumer — the deterministic classifier
below, the learned cross-client priors, and the LLM field agent that runs outside
this quarantined service — may only ever return one of these constants.  A closed
vocabulary is what makes the agent a proposer rather than a decider: it cannot
invent a category, so it cannot invent a behaviour, and an unrecognised answer is
simply dropped back to :data:`UNKNOWN`.

ORDER OF EVIDENCE (strongest first) — each rung is a thing the APPLICATION said,
not a thing we guessed:

  1. ``autocomplete`` — a W3C-standard vocabulary. When an app sets it, the app
     has NAMED the field's semantics itself. This is a declaration, and it is
     right far more often than any reading of a label.
  2. ``input_type`` / ``inputmode`` — a weaker but still explicit declaration.
  3. the field's own validation ``pattern`` — an app that demands
     ``\\d{3}-\\d{2}-\\d{4}`` has described the value precisely.
  4. learned PRIORS for this exact signature — what this same field turned out to
     be across other applications. Value-free, so it carries no client's data.
  5. the accessible-name tokens — the weakest rung, and last on purpose.

Nothing here reads a value, a URL or a tenant. Pure + deterministic.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# ── the closed vocabulary ────────────────────────────────────────────────────
UNKNOWN = "unknown"

GIVEN_NAME = "given_name"
FAMILY_NAME = "family_name"
FULL_NAME = "full_name"
MIDDLE_NAME = "middle_name"
EMAIL = "email"
PHONE = "phone"
DOB = "date_of_birth"
AGE = "age"
SSN = "national_id"
STREET = "street_address"
STREET_2 = "street_address_2"
CITY = "city"
REGION = "region"
POSTAL_CODE = "postal_code"
COUNTRY = "country"
COMPANY = "company"
JOB_TITLE = "job_title"
URL = "url"
CARD_NUMBER = "card_number"
CARD_EXPIRY = "card_expiry"
CARD_CVC = "card_cvc"
CURRENCY = "currency_amount"
PERCENT = "percent"
QUANTITY = "quantity"
DATE = "date"
TIME = "time"
PASSWORD = "password"
OTP = "one_time_code"
USERNAME = "username"
FREE_TEXT = "free_text"
CHOICE = "choice"
CONSENT = "consent"

#: Every type a classifier may return. Anything else is coerced to UNKNOWN.
VOCABULARY = frozenset({
    UNKNOWN, GIVEN_NAME, FAMILY_NAME, FULL_NAME, MIDDLE_NAME, EMAIL, PHONE, DOB, AGE, SSN,
    STREET, STREET_2, CITY, REGION, POSTAL_CODE, COUNTRY, COMPANY, JOB_TITLE,
    URL, CARD_NUMBER, CARD_EXPIRY, CARD_CVC, CURRENCY, PERCENT, QUANTITY,
    DATE, TIME, PASSWORD, OTP, USERNAME, FREE_TEXT, CHOICE, CONSENT,
})

#: Types whose value is PERSONAL DATA. A value of one of these types is never
#: eligible for cross-client sharing, and is encrypted at rest when remembered
#: for a single client. The classifier marks them so callers cannot forget.
SENSITIVE = frozenset({
    SSN, DOB, CARD_NUMBER, CARD_CVC, CARD_EXPIRY, PASSWORD, OTP,
    PHONE, EMAIL, STREET, STREET_2, FULL_NAME, GIVEN_NAME, FAMILY_NAME, MIDDLE_NAME,
})

#: Types no generator may ever satisfy — they need something from outside the
#: browser, so inventing one produces a test that passes against nothing real.
#: These are the honest residue: the crawl asks the client for them.
UNGENERATABLE = frozenset({OTP, PASSWORD})

# ── rung 1: the W3C autocomplete vocabulary ──────────────────────────────────
# Direct mapping. These are the app's own words, not ours.
_AUTOCOMPLETE = {
    "given-name": GIVEN_NAME, "additional-name": GIVEN_NAME, "family-name": FAMILY_NAME,
    "name": FULL_NAME, "nickname": USERNAME, "username": USERNAME,
    "email": EMAIL, "tel": PHONE, "tel-national": PHONE, "tel-local": PHONE,
    "bday": DOB, "bday-day": DOB, "bday-month": DOB, "bday-year": DOB,
    "street-address": STREET, "address-line1": STREET, "address-line2": STREET_2,
    "address-line3": STREET_2, "address-level2": CITY, "address-level1": REGION,
    "postal-code": POSTAL_CODE, "country": COUNTRY, "country-name": COUNTRY,
    "organization": COMPANY, "organization-title": JOB_TITLE,
    "cc-number": CARD_NUMBER, "cc-exp": CARD_EXPIRY, "cc-exp-month": CARD_EXPIRY,
    "cc-exp-year": CARD_EXPIRY, "cc-csc": CARD_CVC, "cc-name": FULL_NAME,
    "url": URL, "photo": UNKNOWN,
    "new-password": PASSWORD, "current-password": PASSWORD, "one-time-code": OTP,
}

# ── rung 2: native input types ───────────────────────────────────────────────
_INPUT_TYPE = {
    "email": EMAIL, "tel": PHONE, "url": URL, "password": PASSWORD,
    "date": DATE, "time": TIME, "month": DATE, "week": DATE,
    "datetime-local": DATE, "number": QUANTITY, "range": QUANTITY,
}

# ── rung 3: the app's own validation patterns ────────────────────────────────
# A pattern is a precise description of the value. Matching on the DECLARED
# pattern is grounded in what the app demands, not in what a label hints.
_PATTERN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\\d\{3\}.{0,4}\\d\{2\}.{0,4}\\d\{4\}"), SSN),
    (re.compile(r"\\d\{5\}(?:.{0,6}\\d\{4\})?"), POSTAL_CODE),
    (re.compile(r"\\d\{16\}|\\d\{4\}(?:.{0,6}\\d\{4\})\{3\}"), CARD_NUMBER),
    (re.compile(r"\\d\{3,4\}\$?$"), CARD_CVC),
)

# ── rung 5: accessible-name tokens (weakest, last) ───────────────────────────
# Ordered: the FIRST rule whose token requirements are met wins, so more specific
# rules must precede broader ones. Each entry is (required-any, forbidden, type).
_TOKEN_RULES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = (
    (frozenset({"ssn", "socialsecurity"}), frozenset(), SSN),
    (frozenset({"social"}), frozenset({"media", "network"}), SSN),
    (frozenset({"cvv", "cvc", "csc", "securitycode"}), frozenset(), CARD_CVC),
    (frozenset({"expiry", "expiration"}), frozenset(), CARD_EXPIRY),
    (frozenset({"card"}), frozenset({"discard", "medicare"}), CARD_NUMBER),
    (frozenset({"otp", "passcode", "verificationcode"}), frozenset(), OTP),
    (frozenset({"password", "pwd"}), frozenset(), PASSWORD),
    (frozenset({"email"}), frozenset(), EMAIL),
    (frozenset({"phone", "mobile", "telephone", "cell"}), frozenset(), PHONE),
    (frozenset({"birth", "dob", "birthdate", "birthday"}), frozenset(), DOB),
    (frozenset({"age"}), frozenset(), AGE),
    (frozenset({"zip", "postal", "postcode"}), frozenset(), POSTAL_CODE),
    (frozenset({"state", "province", "region"}), frozenset({"statement"}), REGION),
    (frozenset({"city", "town", "suburb"}), frozenset(), CITY),
    (frozenset({"country"}), frozenset(), COUNTRY),
    (frozenset({"street", "address"}), frozenset({"email", "ip"}), STREET),
    (frozenset({"company", "organization", "organisation", "employer", "business"}),
     frozenset(), COMPANY),
    (frozenset({"title", "occupation", "role"}), frozenset({"mr", "mrs"}), JOB_TITLE),
    (frozenset({"website", "url", "homepage"}), frozenset(), URL),
    (frozenset({"username", "userid", "login"}), frozenset(), USERNAME),
    (frozenset({"amount", "premium", "salary", "income", "price", "cost", "total",
                "coverage", "benefit", "deductible"}), frozenset(), CURRENCY),
    (frozenset({"percent", "rate"}), frozenset(), PERCENT),
    (frozenset({"quantity", "qty", "count", "number"}), frozenset({"phone", "card",
                                                                  "social", "policy"}), QUANTITY),
    (frozenset({"agree", "consent", "accept", "terms", "acknowledge", "authorize",
                "authorise"}), frozenset(), CONSENT),
    (frozenset({"date"}), frozenset(), DATE),
)

#: A field whose only NAME is a date MASK is a date field. Applications with
#: no accessible name are labelled by their placeholder, and "yyyy-mm-dd" is
#: then the whole label. MEASURED: such a field classified UNKNOWN, fell to
#: the free-text rung and was answered "autotest" - a string a date input
#: cannot accept, so the submit could never have been valid.
_DATE_MASK_RE = re.compile(
    r"^[\s(\[]*(?:yyyy|yy|dd|mm|hh)(?:[^A-Za-z0-9]{1,3}(?:yyyy|yy|dd|mm|hh)){1,3}[\s)\]]*$",
    re.IGNORECASE,
)

#: A name half is (a qualifier that needs the word "name" beside it, a spelling
#: that stands alone). "Last" only means a surname next to "name" — a live crawl
#: classified "Tobacco use in the last 12 months" as a family name because the
#: qualifier alone was enough, and the funnel was then answered with a surname.
_NAME_PAIR = (
    (frozenset({"first", "given"}), frozenset({"forename", "fname", "firstname",
                                               "givenname"}), GIVEN_NAME),
    (frozenset({"last", "family"}), frozenset({"surname", "lname", "lastname",
                                               "familyname"}), FAMILY_NAME),
    # A MIDDLE NAME IS NOT A FULL NAME, and the difference is not cosmetic.
    #
    # MEASURED on orangehrm 2026-09-04. "Middle Name" carries the token "name"
    # and matched neither half above, so it fell to the FULL_NAME rung below and
    # the funnel was answered "Amelia Lockhart" - the applicant's whole name
    # typed into the middle-name box. The value LOOKS right, which is what makes
    # it worse than a blank: it reads as a working fill in every report.
    #
    # It also cannot be rescued downstream. The model is rung 8 and
    # forms._llm_should_answer only consults it when the ladder produced nothing
    # or its own "autotest" placeholder - so a confidently WRONG deterministic
    # answer silently locks the model out of correcting it.
    (frozenset({"middle"}), frozenset({"middlename", "midname"}), MIDDLE_NAME),
)


def coerce(value: Any) -> str:
    """Force any proposed type into the closed vocabulary.

    This is the cage. An LLM, a stored prior or a future caller may propose
    anything at all; only a member of :data:`VOCABULARY` survives."""
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in VOCABULARY else UNKNOWN


def classify(sig: Mapping[str, Any], *,
             priors: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Decide what a field is for, and say WHICH rung decided it.

    ``priors`` maps ``signature -> semantic_type`` (learned across applications,
    value-free). It is consulted AFTER everything the application itself declared,
    so a learned guess can never override the app's own words.

    Returns ``{type, basis, confidence, sensitive, generatable}``."""
    tokens = set(sig.get("tokens") or ())
    tokens |= set(sig.get("placeholder_tokens") or ())
    joined = "".join(sorted(tokens))

    # 1 · the app named it itself
    ac = str(sig.get("autocomplete") or "").strip().lower()
    if ac and ac not in ("on", "off"):
        # tokens may be space separated ("shipping street-address")
        for part in reversed(ac.split()):
            if part in _AUTOCOMPLETE:
                return _verdict(_AUTOCOMPLETE[part], "autocomplete", 0.98)

    # 2 · native input type
    it = str(sig.get("input_type") or "").strip().lower()
    if it in _INPUT_TYPE and it not in ("number", "range", "date"):
        # number/range/date are structural, not semantic — a date input could be a
        # birth date or an effective date, so let the name rungs refine it first.
        return _verdict(_INPUT_TYPE[it], "input_type", 0.9)

    # 3 · the app's declared validation
    constraints = str(sig.get("constraints") or "")
    if constraints.startswith("p=") or "|p=" in constraints:
        for rx, sem in _PATTERN_RULES:
            if rx.search(constraints):
                return _verdict(sem, "pattern", 0.85)

    # 5a · personal names. A qualifier ("first"/"last") only names a person when
    # the word "name" is actually beside it; standalone spellings need no partner.
    has_name_word = bool(tokens & {"name", "names"})
    for qualifiers, standalone, sem in _NAME_PAIR:
        if tokens & standalone or (has_name_word and (tokens & qualifiers)):
            return _verdict(sem, "name_tokens", 0.8)
    if has_name_word and not (tokens & {"user", "company", "organization",
                                        "organisation", "file", "product", "brand"}):
        return _verdict(FULL_NAME, "name_tokens", 0.75)

    for req, forbidden, sem in _TOKEN_RULES:
        if not (tokens & req):
            # also allow a concatenated spelling ("socialsecuritynumber")
            if not any(r in joined for r in req if len(r) >= 5):
                continue
        if tokens & forbidden:
            continue
        return _verdict(sem, "name_tokens", 0.72)

    # 4 · learned priors — only now, and never over the app's own declarations
    if priors:
        learned = priors.get(str(sig.get("signature") or ""))
        if learned:
            sem = coerce(learned.get("type") if isinstance(learned, Mapping) else learned)
            if sem != UNKNOWN:
                conf = 0.7
                if isinstance(learned, Mapping):
                    try:
                        conf = max(0.5, min(0.8, float(learned.get("confidence") or 0.7)))
                    except (TypeError, ValueError):
                        conf = 0.7
                return _verdict(sem, "learned_prior", conf)

    # A name that IS a date mask ("yyyy-mm-dd", "dd/mm/yyyy") names the FORMAT,
    # which is the application telling us the type in the only place it had.
    raw_name = str(sig.get("name") or sig.get("placeholder") or "").strip()
    if raw_name and _DATE_MASK_RE.match(raw_name):
        return _verdict(DATE, "date_mask", 0.7)

    # structural fallbacks — a shape we can fill safely without claiming meaning
    if it in _INPUT_TYPE:
        return _verdict(_INPUT_TYPE[it], "input_type", 0.6)
    kind = str(sig.get("kind") or "").strip().lower()
    if kind in ("select", "radio"):
        return _verdict(CHOICE, "structural", 0.5)
    if kind in ("checkbox", "toggle"):
        return _verdict(CONSENT, "structural", 0.45)
    if kind in ("text", ""):
        return _verdict(FREE_TEXT, "structural", 0.4)
    return _verdict(UNKNOWN, "none", 0.0)


def _verdict(sem: str, basis: str, confidence: float) -> dict[str, Any]:
    sem = coerce(sem)
    return {
        "type": sem,
        "basis": basis,
        "confidence": round(float(confidence), 3),
        "sensitive": sem in SENSITIVE,
        "generatable": sem not in UNGENERATABLE and sem != UNKNOWN,
    }
