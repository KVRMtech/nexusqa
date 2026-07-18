"""Coverage ledger — the honesty spine. Every captured control gets one honest disposition;
per-app coverage is a measured number; nothing under-captured is silently counted as covered.
"""
from app.services import coverage_ledger as cl


def _d(sig):
    return cl.classify_control(sig)[0]


def test_choice_with_options_is_fully_captured():
    assert _d({"type": "select", "options": ["Checking", "Savings"]}) == cl.FULLY


def test_choice_without_options_is_partial():
    assert _d({"type": "select", "options": []}) == cl.PARTIAL


def test_dependent_choice_is_partial_conditional():
    # Options captured for ONE branch of a driver — honest PARTIAL, not a fixed FULLY list.
    d, reason = cl.classify_control({"type": "select", "options": ["High-Yield Savings"], "depends_on": "From Account"})
    assert d == cl.PARTIAL and "From Account" in reason


def test_slider_and_radio_are_partial_not_fully():
    assert _d({"type": "slider"}) == cl.PARTIAL
    assert _d({"type": "radio"}) == cl.PARTIAL


def test_value_field_and_checkbox_are_fully():
    assert _d({"type": "text"}) == cl.FULLY
    assert _d({"type": "checkbox"}) == cl.FULLY


def test_conditionally_revealed_field_is_partial():
    assert _d({"type": "date", "depends_on": "Schedule for later"}) == cl.PARTIAL


def test_coverage_number_and_named_gaps():
    # Mirrors the live transfer form.
    inv = [
        {"label": "From Account", "type": "select", "options": ["Everyday Checking", "High-Yield Savings"]},
        {"label": "To Account", "type": "select", "options": ["High-Yield Savings"], "depends_on": "From Account"},
        {"label": "Amount", "type": "text"},
        {"label": "Select date", "type": "date", "depends_on": "Schedule for later"},
    ]
    led = cl.build_coverage_ledger(inv)
    assert led["total"] == 4
    assert led["fully"] == 2 and led["partial"] == 2       # From Account + Amount FULLY
    assert led["coverage_pct"] == 50
    # Every non-FULLY control is a NAMED gap with a reason — never hidden.
    gap_labels = {g["label"] for g in led["gaps"]}
    assert gap_labels == {"To Account", "Select date"}
    assert all(g.get("reason") for g in led["gaps"])


def test_opaque_surfaces_are_named_never_counted_as_covered():
    inv = [{"label": "Amount", "type": "text"}]
    opaque = [
        {"kind": "cross_origin_iframe", "label": "js.stripe.com", "reason": "a cross-origin embed the DOM can't read"},
        {"kind": "canvas", "label": "chart region", "reason": "a canvas-rendered surface"},
    ]
    led = cl.build_coverage_ledger(inv, opaque_surfaces=opaque)
    assert led["opaque"] == 2 and led["fully"] == 1
    # coverage % is over readable controls (opaque excluded), and stays 100% here.
    assert led["coverage_pct"] == 100
    # both opaque surfaces are NAMED gaps with reasons — a blind spot is never silent.
    opaque_gaps = [g for g in led["gaps"] if g["disposition"] == cl.OPAQUE]
    assert {g["label"] for g in opaque_gaps} == {"js.stripe.com", "chart region"}


def test_unhandled_controls_are_named_in_the_ledger():
    inv = [{"label": "Amount", "type": "text"}]
    unhandled = [{"label": "Data grid", "kind": "grid"}, {"label": "Amount", "kind": "text"}]
    led = cl.build_coverage_ledger(inv, unhandled_controls=unhandled)
    # 'Amount' already captured → not double-counted; 'Data grid' becomes a named UNHANDLED row.
    assert led["unhandled"] == 1
    row = next(g for g in led["gaps"] if g["disposition"] == cl.UNHANDLED)
    assert row["label"] == "Data grid" and "roadmap" in row["reason"]


def test_empty_inventory_is_honest_zero():
    led = cl.build_coverage_ledger([])
    assert led["total"] == 0 and led["coverage_pct"] == 0 and led["gaps"] == []
