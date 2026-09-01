"""False-heal-benchmark interface RESTORED after a repo divergence (the analytic
module was never committed). Returns an HONEST 'unavailable' benchmark rather than a
fabricated false-heal rate — the benchmark is the enterprise trust number and must
never be green-washed. The endpoints return 200 (feature unavailable) not a 500.
"""
from __future__ import annotations


def benchmark(events) -> dict:
    try:
        n = len(list(events or []))
    except TypeError:
        n = 0
    return {
        "unavailable": True,
        "reason": "false-heal-benchmark analytics module is not deployed on this build",
        "events": n,
        "false_heal_rate": None,
        "refusal_rate": None,
        "by_cause": {},
    }
