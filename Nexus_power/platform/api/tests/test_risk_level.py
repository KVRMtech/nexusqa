"""R4 — business risk_level on every generated case + surviving to export.

Audit finding: no risk field existed anywhere on the case, generator, or
exports. Deterministic, grounded in the case's own step text, domain-neutral.
"""
from nexus_sdk.models import ProductionTestCase, ProductionTestStep

from app.services.test_factory.generator import _grade_case, _risk_level
from app.services.test_factory.delivery.exporters import _BASE_HEADERS, _step_rows


def _case(name, actions, priority="P1_high"):
    return ProductionTestCase(
        test_id="t", name=name, description="",
        steps=[ProductionTestStep(step_number=i + 1, action=a, expected="ok",
                                  expected_result="ok")
               for i, a in enumerate(actions)],
        priority=priority, type="functional", tags=[])


def test_money_and_destructive_flows_are_high_risk():
    assert _risk_level(_case("Quote flow", ["Click 'Calculate my premium'"])) == "high"
    assert _risk_level(_case("Checkout", ["Enter card number", "Click Pay"])) == "high"
    assert _risk_level(_case("Delete account", ["Click 'Delete account'"])) == "high"
    assert _risk_level(_case("Transfer", ["Enter amount", "Click Transfer funds"])) == "high"


def test_auth_and_mutations_are_medium():
    assert _risk_level(_case("Login", ["Enter email", "Click Sign in"])) == "medium"
    assert _risk_level(_case("Update profile", ["Click Save"])) == "medium"
    # a P0 archetype with no keyword hit still floors at medium
    assert _risk_level(_case("Primary E2E", ["Click 'Widget'"], priority="P0_critical")) == "medium"


def test_plain_navigation_is_low():
    assert _risk_level(_case("Navigate to About", ["Click 'About'"])) == "low"
    assert _risk_level(_case("View products", ["Click 'Products'"])) == "low"


def test_grade_case_stamps_field_and_tag():
    c = _case("Quote flow", ["Click 'Calculate my premium'"])
    _grade_case(c)
    assert getattr(c, "risk_level", None) == "high"
    assert "risk-level:high" in c.tags


def test_export_carries_priority_and_risk_columns():
    assert "Priority" in _BASE_HEADERS and "Risk" in _BASE_HEADERS
    c = _case("Delete account", ["Click 'Delete account'"], priority="P0_critical")
    _grade_case(c)
    rows = _step_rows(c)
    # columns: S.No, Name, Description, Priority, Risk, Steps, Data, Expected
    pri_idx, risk_idx = _BASE_HEADERS.index("Priority"), _BASE_HEADERS.index("Risk")
    assert rows[0][pri_idx] == "P0 critical"
    assert rows[0][risk_idx] == "HIGH"
