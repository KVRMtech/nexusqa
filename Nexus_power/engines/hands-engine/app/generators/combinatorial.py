"""
Hands Engine — Combinatorial Generator.

Generates pairwise and full Cartesian product combinatorial test data
sets with constraint support for insurance testing scenarios.
"""

from __future__ import annotations

import random
import itertools
from typing import Optional, Any


class CombinatorialGenerator:
    """Generates combinatorial test data using pairwise or full strategies."""

    @staticmethod
    def generate_pairwise(dimensions: dict[str, list[Any]], max_combinations: int = 1000) -> list[dict]:
        """
        Generate pairwise combinations that cover every pair of values
        at least once. Uses a greedy algorithm.
        """
        keys = list(dimensions.keys())
        if len(keys) < 2:
            # Only one dimension — just return all values
            return [{keys[0]: v} for v in dimensions[keys[0]][:max_combinations]]

        # Generate all required pairs
        uncovered_pairs: set[tuple] = set()
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for vi in dimensions[keys[i]]:
                    for vj in dimensions[keys[j]]:
                        uncovered_pairs.add((i, j, str(vi), str(vj)))

        # Reverse lookup for string→original value
        value_map: dict[str, dict[str, Any]] = {}
        for key in keys:
            value_map[key] = {str(v): v for v in dimensions[key]}

        results: list[dict] = []
        rng = random.Random(42)

        while uncovered_pairs and len(results) < max_combinations:
            # Greedy: pick the combination that covers the most uncovered pairs
            best_combo: dict = {}
            best_score = -1

            for _ in range(50):  # sample 50 random combos, pick best
                candidate = {k: str(rng.choice(dimensions[k])) for k in keys}
                score = 0
                for i in range(len(keys)):
                    for j in range(i + 1, len(keys)):
                        pair = (i, j, candidate[keys[i]], candidate[keys[j]])
                        if pair in uncovered_pairs:
                            score += 1
                if score > best_score:
                    best_score = score
                    best_combo = candidate

            if best_score == 0:
                break

            # Mark pairs as covered
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    pair = (i, j, best_combo[keys[i]], best_combo[keys[j]])
                    uncovered_pairs.discard(pair)

            # Restore original types
            row = {}
            for k in keys:
                str_val = best_combo[k]
                row[k] = value_map[k].get(str_val, str_val)
            results.append(row)

        return results

    @staticmethod
    def generate_full(
        dimensions: dict[str, list[Any]],
        max_combinations: int = 50_000,
        constraints: Optional[list[dict]] = None,
    ) -> list[dict]:
        """Full Cartesian product (with optional constraints)."""
        keys = list(dimensions.keys())
        values = [dimensions[k] for k in keys]

        total_possible = 1
        for v in values:
            total_possible *= len(v)

        results: list[dict] = []
        for combo in itertools.product(*values):
            row = dict(zip(keys, combo))

            # Apply constraints
            if constraints and not CombinatorialGenerator._check_constraints(row, constraints):
                continue

            results.append(row)
            if len(results) >= max_combinations:
                break

        return results

    @staticmethod
    def _check_constraints(row: dict, constraints: list[dict]) -> bool:
        """Check if a row satisfies all constraints."""
        for c in constraints:
            condition = c.get("if", {})
            action = c.get("then", {})

            # Check if condition matches
            condition_matches = True
            for field, check in condition.items():
                val = row.get(field)
                if isinstance(check, str):
                    if check.startswith("<"):
                        if not (val is not None and float(val) < float(check[1:])):
                            condition_matches = False
                    elif check.startswith(">"):
                        if not (val is not None and float(val) > float(check[1:])):
                            condition_matches = False
                    elif check.startswith("!="):
                        if str(val) == check[2:].strip():
                            condition_matches = False
                    else:
                        if str(val) != str(check):
                            condition_matches = False

            if condition_matches:
                # Apply action (filter out if action says exclude)
                for field, check in action.items():
                    val = row.get(field)
                    if isinstance(check, str) and check.startswith("!="):
                        if str(val) == check[2:].strip():
                            return False
                    elif isinstance(check, str) and check.startswith("exclude"):
                        return False
        return True
