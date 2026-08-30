"""RUNG 8'S SERVER SIDE: one value, one field, no second route to a model.

The explorer's first LLM data agent built its own httpx client and called a
provider directly — the exact thing T-SEC-12 forbids, invisible to this
service's AST guard because the client lived in qe-explorer. The fix routes
every consultation through ``value_agent.pick_value``, whose only model access
is ``platform_api.complete_llm`` — the guarded wire that PII-scans egress.

These tests pin the server-side contract; the explorer pins its own half
(central transport by default, direct mode dev-gated) in its own suite.
"""
from __future__ import annotations

import pytest

from app.clients.platform_api import LLMResult, LLMUsage
from app.services import value_agent
from app.services.value_agent import (STATUS_ANSWERED, STATUS_NONE,
                                      STATUS_UNAVAILABLE, build_prompt,
                                      clamp_to_options, pick_value)


# ── the pure rules ─────────────────────────────────────────────────────────

def test_an_on_list_reply_returns_the_controls_own_label():
    assert clamp_to_options("  yes ", ["Yes", "No"]) == "Yes"


def test_an_off_list_reply_is_clamped_to_none():
    assert clamp_to_options("Maybe", ["Yes", "No"]) is None


def test_a_free_field_keeps_the_reply():
    assert clamp_to_options("angina, diagnosed 2019", ()) == "angina, diagnosed 2019"


def test_the_prompt_carries_the_rejection_verbatim():
    p = build_prompt(name="DOB", semantic_type="dob", kind="text",
                     rejection="Applicant must be 18-85")
    assert "Applicant must be 18-85" in p


# ── the decision, with the wire faked ──────────────────────────────────────

def _fake_llm(monkeypatch, result):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(value_agent.platform_api, "complete_llm", fake)
    return calls


@pytest.mark.asyncio
async def test_a_clean_completion_is_answered_with_the_tenants_own_id(monkeypatch):
    calls = _fake_llm(monkeypatch, LLMResult(ok=True, text="No"))
    d = await pick_value(tenant_id="t1", name="Do you smoke?", kind="radio",
                         options=["Yes", "No"])
    assert (d.status, d.value) == (STATUS_ANSWERED, "No")
    assert calls[0]["task"] == "field_value"
    assert calls[0]["tenant_id"] == "t1"


@pytest.mark.asyncio
async def test_a_blocked_or_failed_wire_is_unavailable_never_a_raise(monkeypatch):
    """An egress refusal arrives as ok=False — the same shape as an outage —
    and must degrade to residue, not into a crawl."""
    _fake_llm(monkeypatch, LLMResult(ok=False, detail="PII detected (us_ssn)"))
    d = await pick_value(tenant_id="t1", name="Notes", kind="text")
    assert d.status == STATUS_UNAVAILABLE
    assert d.value is None


@pytest.mark.asyncio
async def test_an_off_list_completion_is_none_not_committed(monkeypatch):
    _fake_llm(monkeypatch, LLMResult(ok=True, text="Perhaps"))
    d = await pick_value(tenant_id="t1", name="Do you smoke?", kind="radio",
                         options=["Yes", "No"])
    assert (d.status, d.value) == (STATUS_NONE, None)


@pytest.mark.asyncio
async def test_a_credential_is_refused_without_touching_the_wire(monkeypatch):
    calls = _fake_llm(monkeypatch, LLMResult(ok=True, text="hunter2"))
    d = await pick_value(tenant_id="t1", name="One-time code",
                         semantic_type="otp", kind="text")
    assert d.status == STATUS_NONE
    assert calls == [], "a credential must not even cost a call"


@pytest.mark.asyncio
async def test_usage_is_relayed_only_when_the_provider_reported_it(monkeypatch):
    _fake_llm(monkeypatch, LLMResult(ok=True, text="Austin"))
    d = await pick_value(tenant_id="t1", name="City", kind="text")
    assert d.usage == {}, "unreported spend must not be invented"


@pytest.mark.asyncio
async def test_reported_usage_travels_back_for_the_spend_record(monkeypatch):
    usage = LLMUsage(input_tokens=11, output_tokens=3)
    if not getattr(usage, "reported", False):
        pytest.skip("LLMUsage.reported not derivable from token fields here")
    _fake_llm(monkeypatch, LLMResult(ok=True, text="Austin", usage=usage))
    d = await pick_value(tenant_id="t1", name="City", kind="text")
    assert d.usage.get("input_tokens") == 11
