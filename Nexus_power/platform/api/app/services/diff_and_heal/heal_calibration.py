"""Heal-calibration interface RESTORED after a repo divergence (the analytic module
was never committed). Returns an HONEST 'unavailable' calibration rather than
fabricated reliability/ECE numbers — a trust metric we did not compute must never be
green-washed. The endpoint returns 200 (feature unavailable) instead of a 500.
"""
from __future__ import annotations


def calibrate(observations) -> dict:
    try:
        n = len(list(observations or []))
    except TypeError:
        n = 0
    return {
        "unavailable": True,
        "reason": "heal-calibration analytics module is not deployed on this build",
        "observations": n,
        "reliability": None,
        "ece": None,
        "recommended_min_confidence": None,
    }
