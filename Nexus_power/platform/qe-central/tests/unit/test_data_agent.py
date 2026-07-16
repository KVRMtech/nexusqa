"""Phase 3 — the Data Agent's never-green-wash guarantees.

The centrepiece is the ADVERSARIAL hard-line proof: an LLM that fabricates an SSN and a
policy number cannot get either value into the fill. Also: the grounding gate (a PICK
not in the observed options is rejected; a valid one is accepted), floor-first behaviour
(LLM disabled ⇒ the deterministic floor), and the measurable LLM delta.
"""
from __future__ import annotations

from datetime import date

from app.services import data_agent as da
from app.services.dispositions import ASK, PICK, SYNTHESIZE


_INV = [
    {"label": "State", "type": "select", "options": ["", "California", "New York"]},
    {"label": "Date of Birth", "type": "date", "options": []},
    {"label": "Social Security Number", "type": "text", "options": []},
    {"label": "Policy Number", "type": "text", "options": []},
    {"label": "Get Quote", "type": "submit", "options": []},
]


# ── THE HARD LINE — the data-layer never-green-wash proof ──────────────────────
def test_fabricated_ssn_and_policy_never_enter_the_fill():
    # A hostile LLM tries to fill the sensitive fields with fabricated values.
    hostile = [
        {"label": "Social Security Number", "disposition": "SYNTHESIZE", "value": "123-45-6789"},
        {"label": "Policy Number", "disposition": "SYNTHESIZE", "value": "POL-000123"},
    ]
    out = da.propose_dispositions(_INV, llm_proposal=hostile, today=date(2026, 6, 15))
    by_label = {i["label"]: i for i in out["items"]}
    # Both remain ASK with NO value...
    assert by_label["Social Security Number"]["disposition"] == ASK
    assert by_label["Social Security Number"]["default"] is None
    assert by_label["Policy Number"]["disposition"] == ASK
    assert by_label["Policy Number"]["default"] is None
    # ...and neither fabricated value appears ANYWHERE in the projected fill.
    fill = da.fill_from_items(out["items"])
    assert "123-45-6789" not in fill.values()
    assert "POL-000123" not in fill.values()
    assert "Social Security Number" not in fill and "Policy Number" not in fill


def test_hardline_fires_even_if_llm_relabels_sensitive_as_pick():
    hostile = [{"label": "Social Security Number", "disposition": "PICK", "value": "123-45-6789"}]
    out = da.propose_dispositions(_INV, llm_proposal=hostile)
    ssn = next(i for i in out["items"] if i["label"] == "Social Security Number")
    assert ssn["disposition"] == ASK and ssn["default"] is None


# ── The grounding gate ────────────────────────────────────────────────────────
def test_llm_pick_rejected_when_option_not_observed():
    proposal = [{"label": "State", "disposition": "PICK", "value": "Texas"}]  # not observed
    out = da.propose_dispositions(_INV, llm_proposal=proposal)
    state = next(i for i in out["items"] if i["label"] == "State")
    # Falls back to the floor default (an observed option), never the hallucinated one.
    assert state["default"] == "California"


def test_llm_pick_accepted_when_option_is_observed():
    proposal = [{"label": "State", "disposition": "PICK", "value": "New York"}]
    out = da.propose_dispositions(_INV, llm_proposal=proposal)
    state = next(i for i in out["items"] if i["label"] == "State")
    assert state["default"] == "New York" and state["disposition"] == PICK


def test_llm_synthesize_rejected_when_value_invalid_for_type():
    inv = [{"label": "Coverage Amount", "type": "number", "options": []}]
    proposal = [{"label": "Coverage Amount", "disposition": "SYNTHESIZE", "value": "lots"}]
    out = da.propose_dispositions(inv, llm_proposal=proposal)
    cov = out["items"][0]
    assert cov["default"] == "250000"  # floor default, not the invalid "lots"


def test_llm_synthesize_accepted_when_valid():
    inv = [{"label": "Coverage Amount", "type": "number", "options": []}]
    proposal = [{"label": "Coverage Amount", "disposition": "SYNTHESIZE", "value": "500000"}]
    out = da.propose_dispositions(inv, llm_proposal=proposal)
    assert out["items"][0]["default"] == "500000"


# ── Floor-first + measurable delta ────────────────────────────────────────────
def test_floor_only_when_llm_disabled():
    out = da.propose_dispositions(_INV, llm_proposal=None, today=date(2026, 6, 15))
    assert out["llm_used"] is False
    assert out["autonomy_delta"] == 0
    by_label = {i["label"]: i for i in out["items"]}
    assert by_label["State"]["disposition"] == PICK
    assert by_label["Date of Birth"]["disposition"] == SYNTHESIZE
    assert by_label["Social Security Number"]["disposition"] == ASK


def test_autonomy_delta_counts_llm_changes():
    proposal = [{"label": "State", "disposition": "PICK", "value": "New York"}]  # changes default
    out = da.propose_dispositions(_INV, llm_proposal=proposal)
    assert out["autonomy_delta"] == 1


def test_fill_excludes_observe_and_ask():
    items = [
        {"label": "State", "disposition": "PICK", "default": "California"},
        {"label": "SSN", "disposition": "ASK", "default": None},
        {"label": "Premium", "disposition": "OBSERVE", "default": None},
    ]
    fill = da.fill_from_items(items)
    assert fill == {"State": "California"}
