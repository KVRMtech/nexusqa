"""Login-type fingerprint — the reuse-matching key (Phase 5, record-once/reuse).

Pins the deterministic behaviour the recipe-library reuse depends on:
- the SAME login type (host + path + form shape) -> the SAME key (safe reuse);
- dotcom vs portal on ONE host -> DIFFERENT keys (no false reuse);
- cosmetic differences (case, field order, whitespace, dynamic ids) -> SAME key.
Pure functions — no DB, no browser.
"""
from app.services.test_factory import login_fingerprint as fp

# The USAA case from the plan: same host, two different login types.
DOTCOM = dict(
    domain="usaa.com", login_path="/login",
    fields=[{"name": "email", "type": "email"},
            {"name": "password", "type": "password"}],
    submit="Log on")
PORTAL = dict(
    domain="usaa.com", login_path="/portal/login",
    fields=[{"name": "member_number", "type": "text"},
            {"name": "password", "type": "password"},
            {"name": "pin", "type": "text"}],
    submit="Continue")


def test_same_login_type_same_key():
    # A second app with the identical portal login shape reuses the same recipe.
    assert fp.login_type_key(**PORTAL) == fp.login_type_key(**PORTAL)
    portal_other_app = dict(PORTAL)
    assert fp.login_type_key(**portal_other_app) == fp.login_type_key(**PORTAL)


def test_dotcom_and_portal_on_one_host_are_distinct():
    # base URL / domain alone is NOT enough — the form + path separate them.
    assert fp.login_type_key(**DOTCOM) != fp.login_type_key(**PORTAL)


def test_different_domain_is_a_different_type():
    other = dict(PORTAL, domain="example-life.com")
    assert fp.login_type_key(**other) != fp.login_type_key(**PORTAL)


def test_cosmetic_invariance_case_order_and_dynamic_ids():
    # Field order swapped, case changed, a dynamic element id, extra whitespace —
    # all cosmetic; the key must be identical.
    noisy = dict(
        domain="USAA.com ", login_path="/portal/login?ref=abc#top",
        fields=[{"name": "PASSWORD", "type": "Password"},
                {"name": "member_number_9f3a1c22b7", "type": "text"},
                {"name": "PIN", "type": "text"}],
        submit="  continue ")
    clean = dict(
        domain="usaa.com", login_path="/portal/login",
        fields=[{"name": "member_number", "type": "text"},
                {"name": "password", "type": "password"},
                {"name": "pin", "type": "text"}],
        submit="Continue")
    assert fp.login_type_key(**noisy) == fp.login_type_key(**clean)


def test_a_changed_field_set_changes_the_type():
    # portal that drops the PIN field is a different login type (structural change).
    no_pin = dict(PORTAL, fields=[{"name": "member_number", "type": "text"},
                                  {"name": "password", "type": "password"}])
    assert fp.login_type_key(**no_pin) != fp.login_type_key(**PORTAL)


def test_key_shape_and_stability():
    k = fp.login_type_key(**PORTAL)
    assert k.startswith("lt_") and len(k) == 27  # 'lt_' + 24 hex
    assert k == fp.login_type_key(**PORTAL)  # deterministic


def test_field_identifier_falls_back_label_then_slot_then_autocomplete():
    by_name = fp.login_form_signature(fields=[{"name": "member_number", "type": "text"}])
    by_label = fp.login_form_signature(fields=[{"label": "Member Number", "type": "text"}])
    by_slot = fp.login_form_signature(fields=[{"slot": "member number", "type": "text"}])
    assert by_name == by_label == by_slot  # normalized to the same handle


def test_descriptor_carries_readable_parts_for_the_reuse_prompt():
    d = fp.login_type_descriptor(**PORTAL)
    assert d["key"] == fp.login_type_key(**PORTAL)
    assert d["domain"] == "usaa.com"
    assert d["login_path"] == "/portal/login"
    assert d["field_count"] == 3
    assert "submit:continue" in d["form_signature"]
