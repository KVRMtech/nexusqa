"""Reuse-coverage metrics (Phase 4 — proving the flywheel bend HONESTLY).

The moat's headline can't be "app #2 cost fewer browser-seconds" — that is confounded
by app complexity (a simpler second app looks cheaper with zero flywheel). The causal,
honest proof is reuse COVERAGE: what fraction of controls were seeded from the proven-
control ledger, what fraction of ASK fields were resolved as CARRY from the data
library, what fraction of fields were pre-filled from a disposition prior. EVERY figure
carries its raw numerator + denominator (never a bare percentage, never a single
averaged number), and cost is kept STRICTLY SEPARATE from the verdict so a RED app can
never be made to look cheaper-because-greener.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _ratio(numerator: int, denominator: int) -> dict:
    n = max(0, int(numerator))
    d = max(0, int(denominator))
    return {"numerator": n, "denominator": d, "ratio": round(n / d, 4) if d else None}


def coverage(counts: Mapping[str, Any]) -> dict:
    """Compute the reuse-coverage ratios from raw hit/total counters.

    Expected keys (all optional, default 0):
      ledger_seed_hits / total_controls        — proven-control ledger reuse
      carry_library_hits / ask_fields          — ASK→CARRY resolution from the library
      disposition_prefill_hits / total_fields  — disposition-prior pre-fill
    """
    c = counts if isinstance(counts, Mapping) else {}
    g = lambda k: int(c.get(k) or 0)
    return {
        "ledger_seed_coverage": _ratio(g("ledger_seed_hits"), g("total_controls")),
        "carry_coverage": _ratio(g("carry_library_hits"), g("ask_fields")),
        "prefill_coverage": _ratio(g("disposition_prefill_hits"), g("total_fields")),
    }


def curve_point(app_id: str, *, onboarded_at: str, counts: Mapping[str, Any],
                measured_cost: Mapping[str, Any] | None = None) -> dict:
    """One point on the flywheel cost-curve for an app.

    ``measured_cost`` (browser_seconds / human_touches / llm_tokens — real recorded
    rows) travels ALONGSIDE the coverage ratios but is labelled a measured lower bound
    and never mixed into a verdict. Coverage is the causal flywheel signal; cost is
    context (explicitly complexity-caveated).
    """
    return {
        "app_id": app_id,
        "onboarded_at": onboarded_at,
        "coverage": coverage(counts),
        "measured_cost": dict(measured_cost or {}),
        "cost_caveat": "measured lower bound; a cross-app cost delta is confounded by app complexity",
    }
