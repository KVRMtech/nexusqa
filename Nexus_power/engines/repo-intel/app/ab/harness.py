"""Directed-vs-blind A/B harness.

Repo-intel is OFF the critical path — it must EARN its place by measurably
lifting crawl discovery. This harness grades a seed-directed crawl against a
blind crawl over the same enumerable universe: recall of each against the
universe route set, and the published delta. A non-positive delta is an
honest signal that seeding did not help (never hidden).

Pure function over already-fetched route sets (the DB reads happen in the
router), so it is unit-testable without a database.
"""
from __future__ import annotations

from typing import Dict, Iterable, Set

from app.extract.registry import normalize_reached_path, normalize_route_pattern


def _norm_pattern_set(paths: Iterable[str]) -> Set[str]:
    return {normalize_route_pattern(p) for p in paths if p}


def _norm_reached_set(paths: Iterable[str]) -> Set[str]:
    return {normalize_reached_path(p) for p in paths if p}


def ab_report(
    universe_routes: Iterable[str],
    directed_reached: Iterable[str],
    blind_reached: Iterable[str],
) -> Dict:
    """Return the directed-vs-blind coverage comparison.

    * ``universe_routes`` — the enumerable route PATTERNS from the App Model
      (the denominator; itself carries its own recall, reported upstream).
    * ``directed_reached`` / ``blind_reached`` — concrete url_paths each crawl
      reached (normalised to their route pattern before comparison).
    """
    universe = _norm_pattern_set(universe_routes)
    directed = _norm_reached_set(directed_reached) & universe
    blind = _norm_reached_set(blind_reached) & universe

    n = len(universe)
    directed_recall = round(len(directed) / n, 4) if n else 0.0
    blind_recall = round(len(blind) / n, 4) if n else 0.0
    delta = round(directed_recall - blind_recall, 4)

    return {
        "universe_size": n,
        "directed_recall": directed_recall,
        "blind_recall": blind_recall,
        "recall_delta": delta,
        "directed_only": sorted(directed - blind),
        "blind_only": sorted(blind - directed),
        # Honest verdict — seeding is only "worth it" when the delta is
        # positive AND meaningful; the caller publishes this verbatim.
        "seeding_helps": delta > 0.0,
    }
