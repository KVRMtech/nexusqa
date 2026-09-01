"""E2 — Pairwise (all-pairs) covering-array generator.

Given K factors each with N_k levels, produce the minimum set of
configurations that covers every (factor_i, level_a) × (factor_j, level_b)
pair across any two distinct factors.

This is the industry-standard honest coverage model: stronger than
"one option at a time" (E1), weaker than full Cartesian (exponential
and dishonest to promise).  For 3 factors × 4 levels each, full
Cartesian is 64 combinations; pairwise is typically 16–20.

Algorithm: greedy covering-array construction — the simplest correct
approach.  At each step, pick the configuration that covers the most
uncovered pairs.  Optionally seed with must-walk scenarios (client-
declared combinations from O1 rules) so they count toward coverage.

Pure + dependency-free (unit-testable with plain lists).  No DB, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dc_field
from itertools import combinations
from typing import Any


@dataclass(frozen=True)
class Factor:
    key: str
    levels: tuple[str, ...]


@dataclass
class PairwiseResult:
    configurations: list[dict[str, str]]
    total_pairs: int
    covered_pairs: int
    must_walk_count: int = 0


def _all_pairs(factors: list[Factor]) -> set[tuple[str, str, str, str]]:
    """Every (factor_i, level_a, factor_j, level_b) pair to cover."""
    pairs: set[tuple[str, str, str, str]] = set()
    for i, j in combinations(range(len(factors)), 2):
        fi, fj = factors[i], factors[j]
        for li in fi.levels:
            for lj in fj.levels:
                pairs.add((fi.key, li, fj.key, lj))
    return pairs


def _config_covers(
    config: dict[str, str], uncovered: set[tuple[str, str, str, str]],
) -> set[tuple[str, str, str, str]]:
    """Which uncovered pairs this configuration would cover."""
    covered: set[tuple[str, str, str, str]] = set()
    keys = sorted(config.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            ki, kj = keys[i], keys[j]
            pair = (ki, config[ki], kj, config[kj])
            if pair in uncovered:
                covered.add(pair)
    return covered


def _greedy_cover(
    factors: list[Factor],
    uncovered: set[tuple[str, str, str, str]],
    max_configs: int,
) -> list[dict[str, str]]:
    """Greedily pick configurations that maximize pair coverage."""
    configs: list[dict[str, str]] = []
    while uncovered and len(configs) < max_configs:
        best_config: dict[str, str] | None = None
        best_covered: set[tuple[str, str, str, str]] = set()

        for candidate in _candidate_configs(factors, uncovered):
            covered = _config_covers(candidate, uncovered)
            if len(covered) > len(best_covered):
                best_config = candidate
                best_covered = covered
            if len(best_covered) == len(uncovered):
                break

        if best_config is None or not best_covered:
            break
        configs.append(best_config)
        uncovered -= best_covered

    return configs


def _candidate_configs(
    factors: list[Factor],
    uncovered: set[tuple[str, str, str, str]],
) -> list[dict[str, str]]:
    """Generate candidate configurations seeded from uncovered pairs.

    For each uncovered pair, build a config that fixes those two levels
    and greedily fills the rest to maximize additional coverage.
    """
    candidates: list[dict[str, str]] = []
    sampled: set[tuple[str, str, str, str]] = set()
    for pair in list(uncovered)[:200]:
        if pair in sampled:
            continue
        sampled.add(pair)
        fi_key, li, fj_key, lj = pair
        config = {fi_key: li, fj_key: lj}
        for f in factors:
            if f.key not in config:
                best_level = f.levels[0]
                best_count = 0
                for level in f.levels:
                    test = {**config, f.key: level}
                    count = len(_config_covers(test, uncovered))
                    if count > best_count:
                        best_count = count
                        best_level = level
                config[f.key] = best_level
        candidates.append(config)
    return candidates


def generate_pairwise(
    factors: list[Factor],
    *,
    must_walk: list[dict[str, str]] | None = None,
    max_configs: int = 200,
) -> PairwiseResult:
    """Generate a pairwise covering array.

    ``factors``: the decision controls and their options.
    ``must_walk``: client-declared scenarios that MUST be included —
    they count toward pair coverage (seeded first, never duplicated).
    ``max_configs``: hard cap on total configurations (honest bound).

    Returns ``PairwiseResult`` with the configurations, pair counts,
    and must-walk count.
    """
    if len(factors) < 2:
        if factors:
            configs = [{factors[0].key: level} for level in factors[0].levels]
            return PairwiseResult(
                configurations=configs[:max_configs],
                total_pairs=0, covered_pairs=0)
        return PairwiseResult(configurations=[], total_pairs=0, covered_pairs=0)

    all_p = _all_pairs(factors)
    total = len(all_p)
    uncovered = set(all_p)
    configs: list[dict[str, str]] = []
    must_count = 0

    factor_keys = {f.key for f in factors}
    factor_levels: dict[str, set[str]] = {f.key: set(f.levels) for f in factors}
    for mw in (must_walk or []):
        if not isinstance(mw, Mapping):
            continue
        valid = {k: v for k, v in mw.items()
                 if k in factor_keys and v in factor_levels.get(k, set())}
        if len(valid) < 2:
            continue
        configs.append(valid)
        uncovered -= _config_covers(valid, uncovered)
        must_count += 1
        if len(configs) >= max_configs:
            break

    if uncovered and len(configs) < max_configs:
        greedy = _greedy_cover(factors, uncovered, max_configs - len(configs))
        configs.extend(greedy)
        for g in greedy:
            uncovered -= _config_covers(g, uncovered)

    return PairwiseResult(
        configurations=configs,
        total_pairs=total,
        covered_pairs=total - len(uncovered),
        must_walk_count=must_count,
    )


def factors_from_branches(
    branches: Sequence[Mapping[str, Any]],
) -> list[Factor]:
    """Extract factors from journey branch rows.

    Groups branches by ``(node_fp, control_signature)`` → factor key,
    each branch's ``option_label_norm`` → level.  Only factors with
    >=2 levels produce meaningful pairs.
    """
    groups: dict[str, list[str]] = {}
    for b in (branches or []):
        if not isinstance(b, Mapping):
            continue
        sig = str(b.get("control_signature") or "").strip()
        opt = str(b.get("option_label_norm") or "").strip()
        if not sig or not opt:
            continue
        groups.setdefault(sig, [])
        if opt not in groups[sig]:
            groups[sig].append(opt)

    return [Factor(key=sig, levels=tuple(levels))
            for sig, levels in sorted(groups.items())
            if len(levels) >= 2]
