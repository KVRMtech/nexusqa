"""Agentic-QE suite — the never-green-wash + grounding + governance invariants.

Pins: per-agent toggles + budget/dedupe + provenance (Governor); live-option harvest +
membership + prior-only siblings (Eyes); PRODUCT/SCRIPT/ENVIRONMENT routing with the
5xx->product and infra->environment overrides (Triage); and the two hard Context inert
paths (no live options, recorded value already valid) that run WITHOUT any LLM call.
"""
import asyncio

import pytest

from app.services.agentic import governor
from app.services.agentic import live_options as eyes
from app.services.agentic import triage as tri
from app.services.agentic import semantic_diagnosis as context


# ── Governor ──────────────────────────────────────────────────────────────
def test_governor_toggle_defaults_and_override():
    assert governor.agent_enabled("sentinel") is True      # $0 deterministic default ON
    assert governor.agent_enabled("context") is False      # LLM default OFF
    assert governor.agent_enabled("context", {"agentic": {"context": True}}) is True
    assert governor.agent_enabled("context", {"context": False}) is False
    assert governor.agent_enabled("nope") is False         # unknown -> fail-safe OFF


def test_governor_budget_cap_and_dedupe():
    b = governor.BudgetGuard(max_llm_calls=2)
    assert b.allow("a") is True
    assert b.allow("a") is False      # identical fingerprint deduped
    assert b.allow("b") is True
    assert b.allow("c") is False      # hard cap reached
    assert b.spent == 2 and b.remaining == 0


def test_governor_provenance():
    s = governor.stamp("x", grounding=governor.G_LIVE_CONFIRMED, facts=["f"])
    assert s["confirmed_against_live"] is True and s["is_inference"] is True
    assert governor.stamp("y", grounding=governor.G_DETERMINISTIC)["is_inference"] is False


# ── Eyes (live options) ───────────────────────────────────────────────────
def test_eyes_harvest_and_membership():
    nodes = [{"name": "Ontario", "role": "radio"}, {"name": "Quebec", "role": "radio"},
             {"name": "First name", "role": "textbox"}]
    opts = eyes.live_options(nodes)
    assert "Ontario" in opts and "Quebec" in opts and "First name" not in opts
    assert eyes.value_in_options("ontario", opts) is True
    assert eyes.value_in_options("Florida", opts) is False


def test_eyes_siblings_are_prior_only():
    steps = [{"step_number": 4, "label": "Country", "value": "Canada", "verb": "select"},
             {"step_number": 8, "label": "State", "value": "Florida", "verb": "type"}]
    sibs = eyes.sibling_field_values(steps, failing_step_number=8)
    assert len(sibs) == 1 and sibs[0]["label"] == "Country"


# ── Triage + Verdict ──────────────────────────────────────────────────────
@pytest.mark.parametrize("cause,source,route", [
    ("REAL_REGRESSION", tri.PRODUCT, tri.BUILD),
    ("WRONG_CONTROL_KIND", tri.SCRIPT, tri.FIX),
    ("DATA_VALIDITY_CROSS_FIELD", tri.SCRIPT, tri.FIX),
    ("FLAKE", tri.ENVIRONMENT, tri.FLAG),
    ("NEEDS_REVIEW", tri.UNKNOWN, tri.REVIEW),
])
def test_triage_routing(cause, source, route):
    t = tri.triage({"cause": cause})
    assert t["source"] == source and t["route"] == route


def test_triage_infra_overrides_to_environment():
    t = tri.triage({"cause": "NEEDS_REVIEW"}, error_message="net::ERR_CONNECTION_REFUSED")
    assert t["source"] == tri.ENVIRONMENT and t["route"] == tri.FLAG


def test_triage_5xx_overrides_to_product():
    t = tri.triage({"cause": "WRONG_CONTROL_KIND"}, network={"is_real_bug_signal": True})
    assert t["source"] == tri.PRODUCT and t["route"] == tri.BUILD


# ── Context — never-green-wash inert paths (no LLM call) ───────────────────
def test_context_inert_without_live_options():
    # no option-bearing nodes -> Context cannot see options -> must stay inert.
    out = asyncio.run(context.analyze(
        failing_label="State", failing_value="Florida",
        sibling_fields=[{"label": "Country", "value": "Canada"}],
        nodes=[{"name": "First name", "role": "textbox"}]))
    assert out is None


def test_context_inert_when_recorded_value_is_a_valid_option():
    # recorded value IS among the live options -> no inconsistency -> inert (no LLM).
    out = asyncio.run(context.analyze(
        failing_label="State", failing_value="Ontario",
        sibling_fields=[{"label": "Country", "value": "Canada"}],
        nodes=[{"name": "Ontario", "role": "radio"}, {"name": "Quebec", "role": "radio"}]))
    assert out is None
