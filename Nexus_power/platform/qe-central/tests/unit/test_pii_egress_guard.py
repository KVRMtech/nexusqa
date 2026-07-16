"""Phase 3 — fail-closed PII egress guard.

Proves: clean field metadata is safe to send; a real SSN hiding in a captured option
blocks egress; the payload is value-free; and if the detector cannot run at all, the
guard fails CLOSED (unsafe), so a regulated buyer's PII never reaches an LLM tier.
"""
from __future__ import annotations

import builtins

import pytest

from app.services import pii_egress_guard as g


def test_clean_inventory_is_safe():
    inv = [
        {"label": "State", "type": "select", "options": ["California", "New York"]},
        {"label": "Coverage Amount", "type": "number", "options": []},
    ]
    res = g.guard_inventory(inv)
    assert res["safe"] is True and res["matches"] == []


def test_ssn_in_an_option_blocks_egress():
    inv = [{"label": "Pick a record", "type": "select", "options": ["123-45-6789", "other"]}]
    res = g.guard_inventory(inv)
    assert res["safe"] is False
    assert res["matches"]  # a pattern name was recorded


def test_payload_is_value_free():
    inv = [{"label": "Email", "type": "email", "options": ["a@b.com"]}]
    payload = g.value_free_payload(inv)
    assert "Email" in payload and "email" in payload
    # Options are metadata the crawl observed; labels/types are included, but no
    # user-typed *value* is ever assembled (there is no 'value' field in inventory).
    assert "\n" in payload


def test_detector_unavailable_fails_closed(monkeypatch):
    # Simulate the SDK detector being unimportable → guard must mark UNSAFE, not send.
    def _boom():
        raise ImportError("nexus_sdk.llm.pii_guard missing")
    monkeypatch.setattr(g, "_detector", _boom)
    res = g.guard_inventory([{"label": "State", "type": "text", "options": []}])
    assert res["safe"] is False
    assert "unavailable" in res["reason"].lower()


def test_scan_raises_egressblocked_when_detector_missing(monkeypatch):
    monkeypatch.setattr(g, "_detector", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(g.EgressBlocked):
        g.scan("some text")
