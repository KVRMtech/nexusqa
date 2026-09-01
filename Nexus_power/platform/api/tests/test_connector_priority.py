"""R4 — priority + business risk survive into every TM connector payload
(audit finding: connectors mapped only name/description/steps)."""
from nexus_sdk.models import ProductionTestCase

from app.services.test_factory.delivery.connectors import (
    _risk_of,
    jira_priority_name,
    priority_prefix,
    testrail_priority_id,
    zephyr_priority_name,
)


def _tc(priority="P1_high", tags=None, **extra):
    c = ProductionTestCase(test_id="t", name="Quote flow", description="d",
                           steps=[], priority=priority, type="functional",
                           tags=tags or [])
    for k, v in extra.items():
        setattr(c, k, v)
    return c


def test_testrail_priority_id_mapping():
    assert testrail_priority_id(_tc("P0_critical")) == 4
    assert testrail_priority_id(_tc("P1_high")) == 3
    assert testrail_priority_id(_tc("P2_medium")) == 2
    assert testrail_priority_id(_tc("P3_low")) == 1
    assert testrail_priority_id(_tc(None)) == 1          # unknown floors LOW, never inflated


def test_zephyr_and_jira_names():
    assert zephyr_priority_name(_tc("P0_critical")) == "High"
    assert zephyr_priority_name(_tc("P2_medium")) == "Normal"
    assert zephyr_priority_name(_tc(None)) == "Low"
    assert jira_priority_name(_tc("P0_critical")) == "Highest"
    assert jira_priority_name(_tc("P1_high")) == "High"
    assert jira_priority_name(_tc("P2_medium")) == "Medium"


def test_risk_read_from_field_or_tag():
    assert _risk_of(_tc(risk_level="high")) == "high"
    assert _risk_of(_tc(tags=["combination", "risk-level:medium"])) == "medium"
    assert _risk_of(_tc()) == ""


def test_priority_prefix_is_portable_and_honest():
    p = priority_prefix(_tc("P0_critical", risk_level="high"))
    assert p == "[P0 critical | risk: high] "
    assert priority_prefix(_tc(None)) == ""              # nothing known -> no noise
