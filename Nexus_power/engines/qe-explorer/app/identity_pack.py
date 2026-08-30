"""SYNTHETIC IDENTITY — one coherent fictional person, derived deterministically.

The old per-field defaults were independently plausible and jointly impossible: a
person named "Test User" living at "1 Test Street, Springfield, California 12345".
A real application rejects that at the first address check, at the first
state/postcode cross-validation, or at the first age rule — and the crawl stalls
one step past where it looked like it was working.

Filling MANY fields only helps if the values AGREE WITH EACH OTHER. So an identity
is generated once per crawl and every field is answered from it:

  * the email is built from the person's own name;
  * the postcode is valid for the region, and the region for the country;
  * the date of birth agrees with the age, and both clear an adult-only rule;
  * the card number satisfies Luhn, so a payment step gets a real rejection from
    the processor rather than a client-side format error.

DETERMINISTIC.  The same seed always yields the same person, so a value recorded
in evidence months ago can be regenerated and audited exactly. Nothing here reads
a clock at import, a network, or a random source.

WHY THESE VALUES ARE SAFE TO SEND
    Every reserved-for-fiction range that exists in a standard is used, so a value
    can never collide with a real person or a real account:
      * e-mail domains from RFC 2606 (``example.com``), reserved forever;
      * telephone numbers from the NANP fictional block (``555-01xx``);
      * national-ID area numbers from a block the issuing authority has never
        assigned and never will, so the number is structurally valid and
        structurally impossible;
      * payment numbers from published test BINs that no processor will settle.
    A client who needs a value from THEIR system supplies it, and it is then
    remembered for them alone.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

__all__ = ["Identity", "derive", "REGIONS"]

#: Given / family name pools. A generation vocabulary, not test data: 40 x 40
#: yields 1600 distinct people before any address variation, which is enough for
#: every client and every application to receive a different person.
_GIVEN = (
    "Amelia", "Benjamin", "Clara", "Damian", "Elena", "Felix", "Grace", "Hugo",
    "Imogen", "Julian", "Karina", "Leonard", "Mariam", "Nathan", "Olivia", "Peter",
    "Quinn", "Rosalind", "Simon", "Tessa", "Ulrich", "Vivian", "Wesley", "Xenia",
    "Yolanda", "Zachary", "Adrian", "Beatrice", "Caleb", "Delphine", "Edmund",
    "Fiona", "Gerald", "Harriet", "Ivan", "Josephine", "Keith", "Lorna", "Martin", "Nadia",
)
_FAMILY = (
    "Abernathy", "Blackwood", "Carrington", "Delacroix", "Ellsworth", "Fairbanks",
    "Grimshaw", "Hallowell", "Ingram", "Jephcott", "Kingsley", "Lockhart",
    "Marchetti", "Northcote", "Oakley", "Pemberton", "Quarterman", "Rutherford",
    "Sandoval", "Thorne", "Underwood", "Vandenberg", "Whitfield", "Yarborough",
    "Ashcombe", "Bellamy", "Caldwell", "Draycott", "Everly", "Fenwick", "Garrick",
    "Hawthorne", "Isley", "Jamison", "Kettering", "Langford", "Mowbray", "Nesbitt",
    "Ormsby", "Prescott",
)
_STREETS = (
    "Alder", "Birchwood", "Cedar Ridge", "Dunmore", "Elmhurst", "Fairview",
    "Granville", "Hillcrest", "Inglewood", "Juniper", "Kingsway", "Laurelwood",
    "Meadowbrook", "Northfield", "Orchard", "Pinecrest", "Quarry", "Ridgemont",
    "Stonegate", "Thornbury", "Union", "Verdant", "Westbourne", "Yorkshire",
)
_STREET_TYPES = ("Street", "Avenue", "Road", "Lane", "Drive", "Court", "Way", "Terrace")
_COMPANY_HEAD = ("Meridian", "Northwind", "Ashgrove", "Bluepoint", "Cornerstone",
                 "Silverline", "Redwood", "Harborview")
_COMPANY_TAIL = ("Holdings", "Industries", "Partners", "Group", "Systems",
                 "Associates", "Logistics", "Analytics")
_TITLES = ("Operations Analyst", "Account Manager", "Systems Engineer",
           "Compliance Officer", "Programme Lead", "Field Supervisor")

#: (code, name, a postcode prefix genuinely allocated to that region, a city in it).
#: Reference data — the coherence constraint an application actually validates.
REGIONS: tuple[tuple[str, str, str, str], ...] = (
    ("AL", "Alabama", "352", "Birmingham"),
    ("AZ", "Arizona", "850", "Phoenix"),
    ("CA", "California", "941", "San Francisco"),
    ("CO", "Colorado", "802", "Denver"),
    ("CT", "Connecticut", "061", "Hartford"),
    ("FL", "Florida", "331", "Miami"),
    ("GA", "Georgia", "303", "Atlanta"),
    ("IL", "Illinois", "606", "Chicago"),
    ("IN", "Indiana", "462", "Indianapolis"),
    ("KY", "Kentucky", "402", "Louisville"),
    ("MA", "Massachusetts", "021", "Boston"),
    ("MD", "Maryland", "212", "Baltimore"),
    ("MI", "Michigan", "482", "Detroit"),
    ("MN", "Minnesota", "554", "Minneapolis"),
    ("MO", "Missouri", "631", "Saint Louis"),
    ("NC", "North Carolina", "282", "Charlotte"),
    ("NJ", "New Jersey", "071", "Newark"),
    ("NV", "Nevada", "891", "Las Vegas"),
    ("NY", "New York", "100", "New York"),
    ("OH", "Ohio", "432", "Columbus"),
    ("OR", "Oregon", "972", "Portland"),
    ("PA", "Pennsylvania", "191", "Philadelphia"),
    ("TN", "Tennessee", "372", "Nashville"),
    ("TX", "Texas", "770", "Houston"),
    ("UT", "Utah", "841", "Salt Lake City"),
    ("VA", "Virginia", "232", "Richmond"),
    ("WA", "Washington", "981", "Seattle"),
    ("WI", "Wisconsin", "532", "Milwaukee"),
)

#: Published test card BINs. No processor settles these, and each is the
#: brand's own documented test prefix, so a payment step reaches the processor
#: and returns a real decision instead of failing client-side validation.
_TEST_BINS = ("400000", "510000", "340000", "601100")


def _stream(seed: str) -> list[int]:
    """A deterministic byte stream from the seed — the single source of every
    choice below, so one seed reproduces one person exactly."""
    out: list[int] = []
    counter = 0
    while len(out) < 64:
        block = hashlib.sha256(f"{seed}::{counter}".encode("utf-8")).digest()
        out.extend(block)
        counter += 1
    return out


@dataclass(frozen=True)
class Identity:
    """One coherent fictional person. Every field agrees with every other."""

    seed: str
    given_name: str
    family_name: str
    full_name: str
    username: str
    email: str
    phone: str
    date_of_birth: str
    age: int
    national_id: str
    street_address: str
    street_address_2: str
    city: str
    region_code: str
    region_name: str
    postal_code: str
    country: str
    company: str
    job_title: str
    card_number: str
    card_expiry: str
    card_cvc: str

    def as_dict(self) -> dict[str, str]:
        """Non-secret projection for evidence. The identity is fictional by
        construction, so it is safe to record WHAT was used — and recording it is
        what makes a run reproducible."""
        return {
            "given_name": self.given_name, "family_name": self.family_name,
            "full_name": self.full_name, "email": self.email, "phone": self.phone,
            "date_of_birth": self.date_of_birth, "age": str(self.age),
            "city": self.city, "region_code": self.region_code,
            "postal_code": self.postal_code, "country": self.country,
            "company": self.company,
        }



def _run_tag(run_token: str) -> str:
    """A short, stable, readable token for one run — or "" for none.

    Hashed rather than used raw so a crawl id of any shape or length becomes a
    few URL-safe characters, and readable so a human reading the evidence can
    still recognise the address as ours.
    """
    token = (run_token or "").strip()
    if not token:
        return ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:6]


def derive(seed: str, *, today: Optional[date] = None,
           min_age: int = 30, max_age: int = 55,
           run_token: str = "") -> Identity:
    """Build the one person this crawl will present itself as.

    ``seed`` should be stable for a tenant+application so the same client keeps
    the same person across crawls — a rate quote that changes because the age
    changed between runs is a false difference, not a regression.

    The default age band clears the adult-eligibility rule that gates most
    financial and insurance funnels, so a form does not silently fail validation
    on a field nobody asked about."""
    b = _stream(seed)
    ref = today or date.today()

    given = _GIVEN[b[0] % len(_GIVEN)]
    family = _FAMILY[b[1] % len(_FAMILY)]
    full = f"{given} {family}"

    # age first, then a birth date that PRODUCES that age on the reference day —
    # coherence in the direction the application checks it.
    span = max(1, max_age - min_age)
    age = min_age + (b[2] % span)
    day_offset = b[3] % 365
    try:
        birth = ref.replace(year=ref.year - age) - timedelta(days=day_offset)
    except ValueError:                       # 29 Feb on a non-leap reference year
        birth = ref.replace(year=ref.year - age, day=28) - timedelta(days=day_offset)
    # a birthday that has not occurred yet this year would make the person a year
    # younger than intended; recompute the age the way an application would.
    actual_age = ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))

    code, region, zip_prefix, city = REGIONS[b[4] % len(REGIONS)]
    postal = f"{zip_prefix}{b[5] % 100:02d}"

    street_no = 100 + (b[6] % 8900)
    street = f"{street_no} {_STREETS[b[7] % len(_STREETS)]} {_STREET_TYPES[b[8] % len(_STREET_TYPES)]}"
    unit = f"Suite {1 + (b[9] % 40)}" if b[10] % 3 == 0 else ""

    # UNIQUENESS WITHOUT LOSING THE PERSON.
    #
    # The seed is stable on purpose — the same client keeps the same person, so
    # a rate quote is comparable between runs. That is exactly wrong for the one
    # class of field an application refuses to see twice: crawl an
    # account-opening funnel today and it completes; crawl it tomorrow and the
    # same email arrives at the same application, which answers "already
    # registered" — a rejection no repair can satisfy, because the constraint is
    # about the value's HISTORY, not its shape.
    #
    # So only the fields whose validity depends on never having been used before
    # carry the run token. Name, birth date, address, national id and card are
    # untouched, and an empty token reproduces the old value exactly, so every
    # existing caller and golden is unchanged.
    username = f"{given.lower()}.{family.lower()}{b[11] % 100:02d}"
    tag = _run_tag(run_token)
    if tag:
        username = f"{username}.{tag}"
    # RFC 2606 reserves example.com for documentation — it can never be a real
    # mailbox, so a synthetic address can never reach a real person.
    email = f"{username}@example.com"
    # NANP reserves 555-0100..555-0199 for fiction.
    phone = f"{200 + (b[12] % 700)}5550{100 + (b[13] % 100)}"

    # An area number the issuing authority has never allocated and never will:
    # structurally valid, structurally impossible to belong to anyone. Group and
    # serial avoid the all-zero forms that fail a format check.
    nid = f"9{b[14] % 100:02d}-{1 + (b[15] % 99):02d}-{1 + (b[16] % 9999):04d}"

    company = f"{_COMPANY_HEAD[b[17] % len(_COMPANY_HEAD)]} {_COMPANY_TAIL[b[18] % len(_COMPANY_TAIL)]}"
    title = _TITLES[b[19] % len(_TITLES)]

    card = _luhn(_TEST_BINS[b[20] % len(_TEST_BINS)], b[21:29])
    expiry = f"{1 + (b[30] % 12):02d}/{(ref.year + 2 + (b[31] % 3)) % 100:02d}"
    cvc = f"{100 + (b[32] % 900)}"

    return Identity(
        seed=seed, given_name=given, family_name=family, full_name=full,
        username=username, email=email, phone=phone,
        date_of_birth=birth.isoformat(), age=actual_age, national_id=nid,
        street_address=street, street_address_2=unit, city=city,
        region_code=code, region_name=region, postal_code=postal,
        country="United States", company=company, job_title=title,
        card_number=card, card_expiry=expiry, card_cvc=cvc,
    )


def _luhn(bin_prefix: str, entropy: list[int]) -> str:
    """A 16-digit number that satisfies the Luhn check.

    Without this a payment field fails in the browser and the step never reaches
    the processor — so the crawl learns nothing about the payment flow it was
    sent to exercise."""
    body = bin_prefix + "".join(str(e % 10) for e in entropy)[:9]
    body = body[:15].ljust(15, "0")
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:                       # position of the future check digit
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return body + str((10 - (total % 10)) % 10)
