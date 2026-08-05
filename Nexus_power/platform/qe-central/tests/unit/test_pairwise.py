"""E2 Pairwise — unit tests for the covering-array generator.

Tests cover factor construction, pair generation, greedy covering,
must-walk seeding, boundary conditions, and the scenario extractor.
"""
from __future__ import annotations

from app.services.pairwise import (
    Factor,
    PairwiseResult,
    _all_pairs,
    factors_from_branches,
    generate_pairwise,
)
from app.services.rule_oracle import normalize_scenarios


# ── factors_from_branches ────────────────────────────────────────────────────

def test_factors_from_branches_groups_by_signature():
    branches = [
        {"control_signature": "sig_A", "option_label_norm": "opt_1"},
        {"control_signature": "sig_A", "option_label_norm": "opt_2"},
        {"control_signature": "sig_B", "option_label_norm": "opt_x"},
        {"control_signature": "sig_B", "option_label_norm": "opt_y"},
    ]
    factors = factors_from_branches(branches)
    assert len(factors) == 2
    by_key = {f.key: f for f in factors}
    assert set(by_key["sig_A"].levels) == {"opt_1", "opt_2"}
    assert set(by_key["sig_B"].levels) == {"opt_x", "opt_y"}


def test_factors_from_branches_skips_single_option():
    branches = [
        {"control_signature": "sig_A", "option_label_norm": "opt_1"},
        {"control_signature": "sig_B", "option_label_norm": "opt_x"},
        {"control_signature": "sig_B", "option_label_norm": "opt_y"},
    ]
    factors = factors_from_branches(branches)
    assert len(factors) == 1
    assert factors[0].key == "sig_B"


def test_factors_from_branches_deduplicates_options():
    branches = [
        {"control_signature": "sig_A", "option_label_norm": "opt_1"},
        {"control_signature": "sig_A", "option_label_norm": "opt_1"},
        {"control_signature": "sig_A", "option_label_norm": "opt_2"},
    ]
    factors = factors_from_branches(branches)
    assert len(factors[0].levels) == 2


def test_factors_from_branches_tolerant_on_garbage():
    factors = factors_from_branches(None)
    assert factors == []
    factors = factors_from_branches([None, "bad", {}])
    assert factors == []


def test_factors_from_branches_skips_blank_sig_or_opt():
    branches = [
        {"control_signature": "", "option_label_norm": "opt_1"},
        {"control_signature": "sig_A", "option_label_norm": ""},
        {"control_signature": "sig_A", "option_label_norm": "opt_1"},
        {"control_signature": "sig_A", "option_label_norm": "opt_2"},
    ]
    factors = factors_from_branches(branches)
    assert len(factors) == 1


# ── _all_pairs ───────────────────────────────────────────────────────────────

def test_all_pairs_two_binary_factors():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
    ]
    pairs = _all_pairs(factors)
    assert len(pairs) == 4  # 2×2


def test_all_pairs_three_factors():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
        Factor(key="C", levels=("a", "b")),
    ]
    pairs = _all_pairs(factors)
    # C(3,2) * 2*2 = 3 * 4 = 12
    assert len(pairs) == 12


def test_all_pairs_asymmetric():
    factors = [
        Factor(key="A", levels=("0", "1", "2")),
        Factor(key="B", levels=("x", "y")),
    ]
    pairs = _all_pairs(factors)
    assert len(pairs) == 6  # 3×2


# ── generate_pairwise ───────────────────────────────────────────────────────

def test_pairwise_two_binary_factors():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
    ]
    result = generate_pairwise(factors)
    assert result.total_pairs == 4
    assert result.covered_pairs == 4
    # With 2 binary factors, you need at most 4 configs (worst case)
    # but a greedy can do it in 4 since each config covers 1 pair
    assert len(result.configurations) <= 4


def test_pairwise_covers_all_pairs():
    factors = [
        Factor(key="A", levels=("0", "1", "2")),
        Factor(key="B", levels=("x", "y", "z")),
        Factor(key="C", levels=("a", "b")),
    ]
    result = generate_pairwise(factors)
    assert result.covered_pairs == result.total_pairs
    # verify every pair is covered by at least one configuration
    all_p = _all_pairs(factors)
    for pair in all_p:
        fi, li, fj, lj = pair
        assert any(
            c.get(fi) == li and c.get(fj) == lj
            for c in result.configurations
        ), f"Pair {pair} not covered"


def test_pairwise_fewer_configs_than_cartesian():
    factors = [
        Factor(key="A", levels=("0", "1", "2", "3")),
        Factor(key="B", levels=("x", "y", "z", "w")),
        Factor(key="C", levels=("a", "b", "c", "d")),
    ]
    result = generate_pairwise(factors)
    cartesian = 4 * 4 * 4  # 64
    assert len(result.configurations) < cartesian
    assert result.covered_pairs == result.total_pairs


def test_pairwise_three_binary_factors():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
        Factor(key="C", levels=("a", "b")),
    ]
    result = generate_pairwise(factors)
    assert result.total_pairs == 12
    assert result.covered_pairs == 12
    # optimal for 3 binary factors is 4 configs
    assert len(result.configurations) <= 8


def test_pairwise_with_must_walk():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
        Factor(key="C", levels=("a", "b")),
    ]
    must_walk = [{"A": "0", "B": "x", "C": "a"}]
    result = generate_pairwise(factors, must_walk=must_walk)
    assert result.must_walk_count == 1
    assert result.covered_pairs == result.total_pairs
    # first config should be the must-walk
    assert result.configurations[0] == {"A": "0", "B": "x", "C": "a"}


def test_pairwise_must_walk_counts_toward_coverage():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
    ]
    must_walk = [
        {"A": "0", "B": "x"},
        {"A": "0", "B": "y"},
        {"A": "1", "B": "x"},
        {"A": "1", "B": "y"},
    ]
    result = generate_pairwise(factors, must_walk=must_walk)
    assert result.must_walk_count == 4
    assert result.covered_pairs == 4
    # all must-walks cover everything — no greedy configs needed
    assert len(result.configurations) == 4


def test_pairwise_must_walk_invalid_key_skipped():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
    ]
    must_walk = [{"A": "0", "NONEXISTENT": "z"}]
    result = generate_pairwise(factors, must_walk=must_walk)
    assert result.must_walk_count == 0


def test_pairwise_must_walk_invalid_level_skipped():
    factors = [
        Factor(key="A", levels=("0", "1")),
        Factor(key="B", levels=("x", "y")),
    ]
    must_walk = [{"A": "0", "B": "NONEXISTENT"}]
    result = generate_pairwise(factors, must_walk=must_walk)
    assert result.must_walk_count == 0


def test_pairwise_max_configs():
    factors = [
        Factor(key="A", levels=("0", "1", "2", "3")),
        Factor(key="B", levels=("x", "y", "z", "w")),
        Factor(key="C", levels=("a", "b", "c", "d")),
    ]
    result = generate_pairwise(factors, max_configs=3)
    assert len(result.configurations) <= 3


def test_pairwise_single_factor():
    factors = [Factor(key="A", levels=("0", "1", "2"))]
    result = generate_pairwise(factors)
    assert result.total_pairs == 0
    assert len(result.configurations) == 3


def test_pairwise_empty_factors():
    result = generate_pairwise([])
    assert result.total_pairs == 0
    assert result.configurations == []


# ── normalize_scenarios ──────────────────────────────────────────────────────

def test_normalize_scenarios_basic():
    rules = [
        {"kind": "scenario",
         "choices": {"product_type": "Term Life", "tobacco": "No"},
         "source": "underwriting_matrix"},
    ]
    scenarios = normalize_scenarios(rules)
    assert len(scenarios) == 1
    assert scenarios[0] == {"product_type": "Term Life", "tobacco": "No"}


def test_normalize_scenarios_skips_non_scenario_kind():
    rules = [
        {"kind": "outcome_rule", "field": "premium", "expected": 28.0},
        {"kind": "scenario", "choices": {"a": "1", "b": "2"}},
    ]
    scenarios = normalize_scenarios(rules)
    assert len(scenarios) == 1


def test_normalize_scenarios_skips_single_choice():
    rules = [{"kind": "scenario", "choices": {"only_one": "val"}}]
    scenarios = normalize_scenarios(rules)
    assert scenarios == []


def test_normalize_scenarios_reads_overrides_alias():
    rules = [{"kind": "scenario", "overrides": {"a": "1", "b": "2"}}]
    scenarios = normalize_scenarios(rules)
    assert len(scenarios) == 1


def test_normalize_scenarios_tolerant_on_garbage():
    assert normalize_scenarios(None) == []
    assert normalize_scenarios([None, "bad"]) == []
    assert normalize_scenarios([{"kind": "scenario", "choices": "not-a-map"}]) == []


def test_normalize_scenarios_strips_blank_keys():
    rules = [{"kind": "scenario", "choices": {"": "val", "a": "1", "b": "2"}}]
    scenarios = normalize_scenarios(rules)
    assert "" not in scenarios[0]
    assert len(scenarios[0]) == 2
