"""Value-oracle INFERENCE (#2) — the crawl-side candidate classifier.

These pin the classification + candidacy rules the crawler uses to SURFACE
candidate expected values from a page's rendered value nodes.  Inference only —
the frozen factory oracle still does the proving.
"""
from app.value_infer import (
    VALUE_TYPE_CURRENCY,
    VALUE_TYPE_DECISION,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_OTHER,
    VALUE_TYPE_PERCENT,
    classify_value,
    infer_candidate,
)


def test_classify_currency_percent_decision():
    assert classify_value("$75.00") == VALUE_TYPE_CURRENCY
    assert classify_value("USD 1,200.50") == VALUE_TYPE_CURRENCY
    assert classify_value("12.5%") == VALUE_TYPE_PERCENT
    assert classify_value("Approved") == VALUE_TYPE_DECISION
    assert classify_value("Your application was declined") == VALUE_TYPE_DECISION


def test_classify_currency_beats_bare_number():
    # a money value is currency, not a bare number (ordering matters).
    assert classify_value("$42") == VALUE_TYPE_CURRENCY
    assert classify_value("42") == VALUE_TYPE_NUMBER


def test_classify_prose_is_other():
    assert classify_value("Welcome to your dashboard") == VALUE_TYPE_OTHER
    assert classify_value("") == VALUE_TYPE_OTHER


def test_high_signal_values_are_candidates():
    prem = infer_candidate("Monthly Premium", "$75.00")
    assert prem["is_candidate"] and prem["value_type"] == VALUE_TYPE_CURRENCY
    # an outcome-shaped label lifts confidence above a bare currency node.
    assert prem["confidence"] > infer_candidate("", "$75.00")["confidence"]

    dec = infer_candidate("Decision", "Approved")
    assert dec["is_candidate"] and dec["value_type"] == VALUE_TYPE_DECISION


def test_bare_number_is_a_candidate_only_under_an_outcome_label():
    # a number under "Total amount" is a candidate; the same number as chrome is not.
    assert infer_candidate("Total amount", "1200")["is_candidate"] is True
    assert infer_candidate("Step", "1200")["is_candidate"] is False


def test_prose_is_never_a_candidate():
    out = infer_candidate("Heading", "Manage your policies here")
    assert out["is_candidate"] is False
    assert out["value_type"] == VALUE_TYPE_OTHER
    assert out["confidence"] == 0.0
