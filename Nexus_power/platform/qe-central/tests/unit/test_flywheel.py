"""Phase 4 — flywheel core: funnel classifier, reuse-coverage, data-library slot guard.

Pins: the funnel join key is deterministic and returns 'unknown' honestly; the moat
metric is reuse COVERAGE with numerator/denominator (not a confounded cost delta); and
the data library NEVER carries the wrong secret (single-slot match only; ambiguous or
shape-only matches stay ASK).
"""
from __future__ import annotations

from app.services import data_library as lib
from app.services import funnel_classifier as fc
from app.services import reuse_coverage as rc


# ── funnel classifier ─────────────────────────────────────────────────────────
def test_quote_funnel_is_recognised_with_evidence():
    r = fc.classify_funnel(
        routes=["/get-a-quote", "/quote/step-2"],
        labels=["Coverage Amount", "Term Length"],
        texts=["Get Quote"],
    )
    assert r["funnel_type"] == fc.QUOTE
    assert r["evidence"] and r["confidence"] > 0


def test_login_funnel():
    r = fc.classify_funnel(routes=["/login"], labels=["Username", "Password"])
    assert r["funnel_type"] == fc.LOGIN


def test_transfer_beats_generic_on_money_route():
    r = fc.classify_funnel(routes=["/transfer"], labels=["From Account", "To Account", "Payee"])
    assert r["funnel_type"] == fc.TRANSFER


def test_ambiguous_journey_is_unknown_not_forced():
    r = fc.classify_funnel(routes=["/home"], labels=["Newsletter"], texts=["Learn more"])
    assert r["funnel_type"] == fc.UNKNOWN and r["confidence"] == 0.0


def test_single_weak_keyword_stays_unknown():
    # One keyword (weight 1) is below the min score → honest unknown, not a forced bucket.
    r = fc.classify_funnel(labels=["search"])
    assert r["funnel_type"] == fc.UNKNOWN


def test_funnel_is_deterministic():
    j = {"routes": ["/checkout"], "labels": ["Card Number"], "texts": ["Place Order"]}
    assert fc.classify_journey(j) == fc.classify_journey(j)


# ── reuse coverage (the honest moat metric) ───────────────────────────────────
def test_coverage_carries_numerator_and_denominator():
    cov = rc.coverage({
        "ledger_seed_hits": 8, "total_controls": 10,
        "carry_library_hits": 2, "ask_fields": 3,
        "disposition_prefill_hits": 5, "total_fields": 12,
    })
    assert cov["ledger_seed_coverage"] == {"numerator": 8, "denominator": 10, "ratio": 0.8}
    assert cov["carry_coverage"]["numerator"] == 2 and cov["carry_coverage"]["denominator"] == 3
    assert cov["prefill_coverage"]["ratio"] == round(5 / 12, 4)


def test_coverage_zero_denominator_is_none_not_crash():
    cov = rc.coverage({"ledger_seed_hits": 0, "total_controls": 0})
    assert cov["ledger_seed_coverage"]["ratio"] is None


def test_curve_point_keeps_cost_separate_and_caveated():
    p = rc.curve_point("app-1", onboarded_at="2026-07-16",
                       counts={"ledger_seed_hits": 3, "total_controls": 4},
                       measured_cost={"browser_seconds": 42})
    assert p["coverage"]["ledger_seed_coverage"]["ratio"] == 0.75
    assert p["measured_cost"]["browser_seconds"] == 42
    assert "confounded" in p["cost_caveat"]


# ── data library slot guard (never carry the wrong secret) ────────────────────
def test_single_slot_match_carries():
    r = lib.resolve("Member ID", {"member id": True})
    assert r["disposition"] == lib.CARRY and r["slot_key"] == "member id"


def test_no_slot_stays_ask():
    r = lib.resolve("Favourite Colour", {"member id": True})
    assert r["disposition"] == lib.ASK and r["slot_key"] is None


def test_ambiguous_two_slots_refuses_to_carry():
    # "Account Number" matches both "account" and "account number" → collision → ASK.
    r = lib.resolve("Account Number", {"account": True, "account number": True})
    assert r["disposition"] == lib.ASK
    assert r["reason"] == lib.R_COLLISION


def test_shape_only_similarity_does_not_carry_wrong_secret():
    # A routing-number slot must NOT be carried into an SSN field (no shape inference).
    r = lib.resolve("Social Security Number", {"routing number": True})
    assert r["disposition"] == lib.ASK


def test_matched_but_empty_slot_stays_ask():
    r = lib.resolve("Member ID", {"member id": False})
    assert r["disposition"] == lib.ASK and r["reason"] == lib.R_NO_VALUE


def test_short_token_matches_whole_word_only():
    # "ssn" must match "SSN" but not accidentally a substring inside another word.
    assert lib.resolve("SSN", {"ssn": True})["disposition"] == lib.CARRY
    assert lib.resolve("Lesson Plan", {"ssn": True})["disposition"] == lib.ASK


def test_carry_library_keys_only_returns_filled_slots():
    assert lib.carry_library_keys({"ssn": True, "member id": False, "policy": True}) == ["policy", "ssn"]
