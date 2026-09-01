"""P0 + P2 — a field can be learned by, and answered coherently.

Two defects this closes, both measured on real funnels:

  * a crawl that asks the client for a value, receives it, and asks the SAME
    question next crawl has learned nothing. Remembering needs a key, and keying on
    the URL makes the learning per-application — worthless across clients. The key
    has to be the field's own semantics.

  * the previous per-field defaults were independently plausible and jointly
    impossible: a person named "Test User" at "1 Test Street, Springfield,
    California 12345". Real applications cross-validate region against postcode and
    age against date of birth, so a form filled that way dies one step past where it
    looked like it was working.

Pure — no browser, no network, no clock dependence beyond an injected date.
"""
from datetime import date

from app import field_semantics as S
from app import field_signature, field_values
from app.identity_pack import REGIONS, derive


def ctl(**kw):
    kw.setdefault("kind", "text")
    return kw


# ── signatures: the key learning is worth anything by ────────────────────────

def test_the_same_field_authored_three_ways_is_one_signature():
    """`firstName`, `first_name` and `First Name` are the same field. Three
    signatures would mean learning the same answer three times."""
    a = field_signature.signature_of(ctl(name="firstName"))
    b = field_signature.signature_of(ctl(name="first_name"))
    c = field_signature.signature_of(ctl(name="First Name"))
    assert a == b == c


def test_the_signature_never_contains_a_value():
    """THE SAFETY PROPERTY. Signatures are shared across clients; values never are.
    If a value could reach the hash it could reach another tenant."""
    secret = "849-22-7710"
    plain = field_signature.compute(ctl(name="SSN"))
    withval = field_signature.compute(ctl(name="SSN", value=secret,
                                          committed_value=secret))
    assert plain["signature"] == withval["signature"]
    assert secret not in repr(withval)


def test_a_cosmetic_redeploy_does_not_expire_a_signature():
    """Build-hash class names change on every deploy. Including them would make
    every learned value expire for no reason."""
    a = field_signature.signature_of(ctl(name="Email", id="css-1a2b3c"))
    b = field_signature.signature_of(ctl(name="Email", id="css-9z8y7x"))
    assert a == b


def test_different_fields_do_not_collide():
    seen = {field_signature.signature_of(ctl(name=n))
            for n in ("Email", "Phone", "Date of birth", "Postcode", "Employer")}
    assert len(seen) == 5


def test_the_declared_validation_separates_two_same_named_fields():
    """One app's 'Number' with maxlength 4 is a card CVC; another's is a quantity.
    The app's own constraint is what tells them apart."""
    a = field_signature.signature_of(ctl(name="Number", maxlength="4"))
    b = field_signature.signature_of(ctl(name="Number", maxlength="20"))
    assert a != b


def test_option_set_shape_counts_but_option_text_does_not():
    """Two country pickers are the same field whether they list 195 countries or
    12 — but a two-option toggle is NOT a fifty-option picker."""
    big_a = field_signature.signature_of(ctl(name="Country", kind="select",
                                             options=[str(i) for i in range(195)]))
    big_b = field_signature.signature_of(ctl(name="Country", kind="select",
                                             options=[f"c{i}" for i in range(120)]))
    small = field_signature.signature_of(ctl(name="Country", kind="select",
                                             options=["Yes", "No"]))
    assert big_a == big_b
    assert big_a != small


# ── classification: the app's own words beat our reading of a label ──────────

def test_the_autocomplete_attribute_wins_over_a_misleading_label():
    """`autocomplete` is a W3C vocabulary — when an app sets it, the app has named
    the field itself. That beats any heuristic on the visible label."""
    sig = field_signature.compute(ctl(name="Contact", autocomplete="family-name"))
    v = S.classify(sig)
    assert v["type"] == S.FAMILY_NAME
    assert v["basis"] == "autocomplete"


def test_a_learned_prior_can_never_override_the_application():
    """A prior is a guess from other apps; `autocomplete` is a declaration by THIS
    app. If a prior could win, one bad generalisation would corrupt every client."""
    sig = field_signature.compute(ctl(name="Reference", autocomplete="email"))
    v = S.classify(sig, priors={sig["signature"]: {"type": S.SSN, "confidence": 0.99}})
    assert v["type"] == S.EMAIL


def test_a_prior_answers_a_field_nothing_else_could():
    sig = field_signature.compute(ctl(name="Policyholder reference code"))
    base = S.classify(sig)
    learned = S.classify(sig, priors={sig["signature"]: {"type": S.USERNAME,
                                                         "confidence": 0.75}})
    assert learned["type"] == S.USERNAME
    assert learned["basis"] == "learned_prior"
    assert base["type"] != S.USERNAME


def test_common_identity_fields_classify_from_the_label_alone():
    for name, want in (("Email address", S.EMAIL), ("Mobile phone", S.PHONE),
                       ("Date of birth", S.DOB), ("Social Security Number", S.SSN),
                       ("ZIP code", S.POSTAL_CODE), ("First name", S.GIVEN_NAME),
                       ("Last name", S.FAMILY_NAME), ("Employer", S.COMPANY),
                       ("City", S.CITY), ("State", S.REGION)):
        got = S.classify(field_signature.compute(ctl(name=name)))["type"]
        assert got == want, f"{name} -> {got}"


def test_a_proposed_type_outside_the_vocabulary_is_refused():
    """THE CAGE. The field agent runs outside this service and may propose anything.
    Only a member of the closed vocabulary survives."""
    for forged in ("delete_everything", "admin", "", None, "SQL", "sudo"):
        assert S.coerce(forged) == S.UNKNOWN


def test_the_things_no_generator_may_invent_are_named():
    """A one-time code invented by us proves nothing. These must stay residue."""
    assert S.OTP in S.UNGENERATABLE
    assert S.PASSWORD in S.UNGENERATABLE
    for sem in (S.OTP, S.PASSWORD):
        assert S.classify(field_signature.compute(
            ctl(name={S.OTP: "One time passcode", S.PASSWORD: "Password"}[sem])
        ))["generatable"] is False


def test_personal_data_types_are_flagged_sensitive():
    """Callers must not have to remember which types are PII."""
    for name in ("Social Security Number", "Date of birth", "Email address"):
        assert S.classify(field_signature.compute(ctl(name=name)))["sensitive"] is True


# ── the identity: coherent, fictional, reproducible ──────────────────────────

REF = date(2026, 8, 2)


def test_the_same_seed_always_produces_the_same_person():
    """Evidence recorded months ago must regenerate exactly, or a run cannot be
    audited and a rate quote changes for reasons nobody can explain."""
    assert derive("tenant-a::app-1", today=REF) == derive("tenant-a::app-1", today=REF)


def test_different_clients_get_different_people():
    a = derive("tenant-a::app-1", today=REF)
    b = derive("tenant-b::app-1", today=REF)
    assert a.full_name != b.full_name or a.postal_code != b.postal_code


def test_the_postcode_belongs_to_the_region():
    """THE COHERENCE DEFECT. Applications cross-validate these, and a mismatch
    fails on a field nobody was looking at."""
    prefixes = {code: pfx for code, _n, pfx, _c in REGIONS}
    for i in range(60):
        ident = derive(f"seed-{i}", today=REF)
        assert ident.postal_code.startswith(prefixes[ident.region_code]), ident


def test_the_city_belongs_to_the_region():
    cities = {code: city for code, _n, _p, city in REGIONS}
    for i in range(40):
        ident = derive(f"c-{i}", today=REF)
        assert ident.city == cities[ident.region_code]


def test_the_age_agrees_with_the_date_of_birth():
    """An age field and a DOB field on the same form must not contradict."""
    for i in range(80):
        ident = derive(f"age-{i}", today=REF)
        born = date.fromisoformat(ident.date_of_birth)
        computed = REF.year - born.year - ((REF.month, REF.day) < (born.month, born.day))
        assert computed == ident.age, ident.date_of_birth


def test_everyone_is_an_adult():
    """Financial and insurance funnels gate on this before anything else."""
    for i in range(80):
        assert derive(f"adult-{i}", today=REF).age >= 18


def test_the_email_is_built_from_the_persons_own_name():
    ident = derive("coherent", today=REF)
    assert ident.given_name.lower() in ident.email
    assert ident.family_name.lower() in ident.email


def test_every_generated_value_lives_in_a_reserved_fictional_range():
    """None of these can ever collide with a real person or a real account."""
    for i in range(50):
        ident = derive(f"fiction-{i}", today=REF)
        assert ident.email.endswith("@example.com")          # RFC 2606
        assert "5550" in ident.phone                          # NANP fictional block
        assert ident.national_id.startswith("9")              # never allocated


def test_the_national_id_is_structurally_valid():
    """Format-valid so it passes the app's check; from an unissued block so it
    belongs to nobody."""
    for i in range(50):
        nid = derive(f"nid-{i}", today=REF).national_id
        area, group, serial = nid.split("-")
        assert len(area) == 3 and len(group) == 2 and len(serial) == 4
        assert group != "00" and serial != "0000"


def test_the_card_number_passes_luhn():
    """Without this a payment step fails in the browser and never reaches the
    processor, so the crawl learns nothing about the flow it was sent to test."""
    for i in range(40):
        pan = derive(f"card-{i}", today=REF).card_number
        assert len(pan) == 16 and pan.isdigit()
        total = 0
        for j, ch in enumerate(reversed(pan)):
            d = int(ch)
            if j % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        assert total % 10 == 0, pan


def test_a_leap_day_reference_date_does_not_crash():
    assert derive("leap", today=date(2028, 2, 29)).age >= 18


# ── generation: the value fits the control, not just the meaning ─────────────

IDENT = derive("values", today=REF)


def test_a_region_dropdown_lands_on_the_identitys_own_region():
    """The whole point of coherence: the selected state must be the state the
    postcode belongs to."""
    control = ctl(kind="select", name="State",
                  options=["Select", "California", "Texas", IDENT.region_name])
    got = field_values.value_for(S.REGION, control, IDENT, kind="select")
    assert got == IDENT.region_name


def test_a_dropdown_never_returns_the_placeholder():
    control = ctl(kind="select", name="Country", options=["-- Select --", "Canada"])
    assert field_values.value_for(S.COUNTRY, control, IDENT, kind="select") == "Canada"


def test_a_declared_maxlength_is_respected():
    """A semantically perfect value that violates the field's own constraint fails
    exactly like a wrong one."""
    got = field_values.value_for(S.SSN, ctl(name="SSN", maxlength="9"), IDENT)
    assert len(got) <= 9 and got.isdigit()


def test_a_number_field_honours_its_own_min():
    """A constraint-blind value passes the fill and then voids the submit via native
    validation — and the failure looks like the application's."""
    got = field_values.value_for(S.AGE, ctl(name="Age", input_type="number",
                                            min="65", max="99"), IDENT)
    assert int(got) >= 65


def test_a_date_input_flavour_gets_its_own_format():
    """A blanket ISO string makes month/week/time inputs throw, so the field never
    advances."""
    assert "-W" in field_values.value_for(S.DOB, ctl(name="Born", input_type="week"), IDENT)
    assert len(field_values.value_for(S.DOB, ctl(name="Born", input_type="month"), IDENT)) == 7
    assert field_values.value_for(S.TIME, ctl(name="When", input_type="time"), IDENT) == "12:00"


def test_an_ungeneratable_field_returns_nothing():
    """Not an empty string — nothing. It has to become residue, not a filled field
    holding a meaningless value."""
    for sem in (S.OTP, S.PASSWORD):
        assert field_values.value_for(sem, ctl(name="x"), IDENT) is None


def test_an_optional_toggle_is_left_alone_but_a_required_consent_is_cleared():
    """Choosing an optional toggle invents a scenario the client never asked to
    test; a required consent is only a gate."""
    assert field_values.value_for(S.CONSENT, ctl(kind="checkbox", name="Marketing"),
                                  IDENT, kind="checkbox") is None
    assert field_values.value_for(S.CONSENT, ctl(kind="checkbox", name="I agree",
                                                 required=True),
                                  IDENT, kind="checkbox") == "true"


def test_no_value_is_ever_produced_for_an_unknown_type():
    assert field_values.value_for(S.UNKNOWN, ctl(name="???"), IDENT) is None


# ── found by a LIVE crawl, not by review ─────────────────────────────────────

def test_a_qualifier_alone_does_not_mean_a_persons_name():
    """FOUND LIVE. A real crawl classified "Tobacco use in the last 12 months" as a
    family name, because the qualifier "last" was enough on its own — and the
    underwriting question was then answered with a surname.

    "Last" means a surname only next to the word "name"; spellings that stand alone
    (surname / lname) need no partner."""
    assert S.classify(field_signature.compute(
        ctl(name="Tobacco use in the last 12 months")))["type"] != S.FAMILY_NAME
    assert S.classify(field_signature.compute(
        ctl(name="When did you last visit?")))["type"] != S.FAMILY_NAME
    assert S.classify(field_signature.compute(
        ctl(name="First contact date")))["type"] != S.GIVEN_NAME
    # and the real ones still work
    for name, want in (("Last name", S.FAMILY_NAME), ("Surname", S.FAMILY_NAME),
                       ("lname", S.FAMILY_NAME), ("First name", S.GIVEN_NAME),
                       ("fname", S.GIVEN_NAME), ("Full name", S.FULL_NAME)):
        assert S.classify(field_signature.compute(ctl(name=name)))["type"] == want, name


def test_a_radio_group_is_left_to_the_client_unless_agent_mode_is_on():
    """FOUND LIVE. Filling radio groups by default silently changed what the crawl
    does: picking "smoker = no" decides which business path gets exercised, and
    nothing in the report would say so.

    That is the operator's DATA dial, not a default. "user" must stay byte-identical
    to the behaviour that existed before any of this."""
    radio = ctl(kind="radio", name="Do you use tobacco?", options=["Yes", "No"])
    assert field_values.value_for(S.CHOICE, radio, IDENT, kind="radio") is None
    assert field_values.value_for(
        S.CHOICE, radio, IDENT, kind="radio",
        data_mode=field_values.DATA_MODE_AGENT) in ("Yes", "No")


def test_a_dropdown_is_still_filled_in_user_mode():
    """Selects were always filled — only radios were left alone. Gating both would
    be a silent regression in the other direction."""
    sel = ctl(kind="select", name="Term length", options=["Select", "10", "20"])
    assert field_values.value_for(S.CHOICE, sel, IDENT, kind="select") == "10"


def test_the_default_data_mode_is_the_conservative_one():
    import inspect
    sig = inspect.signature(field_values.value_for)
    assert sig.parameters["data_mode"].default == field_values.DATA_MODE_USER
