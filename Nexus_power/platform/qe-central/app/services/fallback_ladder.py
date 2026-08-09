"""P5 — the fallback ladder (honest resolution of a stuck decision).

Every stuck decision descends a fixed ladder, cheapest and most auditable first:

    1. deterministic  — the rule/vocab engine resolved it (grounded by construction)
    2. agentic        — a vision/reasoning agent resolved it AND a verifier proved
                        the action registered (unverified agentic is NOT coverage)
    3. record_once    — no automation could resolve it, but it is recordable: a
                        human records the choreography ONCE and it replays after
    4. human          — a person must resolve it each time; flagged, never skipped

The point is honesty at 1000-app scale: coverage is reported by the rung that
resolved each decision, and an agent that could not be *verified* never counts as
covered — it descends. A silently-skipped widget is the failure this prevents.

Pure + deterministic: this decides the rung from each rung's offered result; the
crawler/agents supply those results and act on the decision.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .coverage import PROV_DETERMINISTIC, PROV_INFERRED, PROV_LIVE_CONFIRMED
from .touch_meter import TOUCH_WIDGET_RECORD, TOUCH_WIDGET_RESOLVE

RUNG_DETERMINISTIC = "deterministic"
RUNG_AGENTIC = "agentic"
RUNG_RECORD_ONCE = "record_once"
RUNG_HUMAN = "human"
RUNGS = (RUNG_DETERMINISTIC, RUNG_AGENTIC, RUNG_RECORD_ONCE, RUNG_HUMAN)


def resolve_rung(
    *,
    deterministic: Mapping[str, Any] | None = None,
    agentic: Mapping[str, Any] | None = None,
    record_once_available: bool = False,
) -> dict[str, Any]:
    """Pick the honest rung for one stuck decision.

    ``deterministic``: ``{"resolved": bool}`` — the rule/vocab engine's result.
    ``agentic``: ``{"resolved": bool, "verified": bool}`` — an agent's result and
        whether a verifier proved the action registered. ``resolved`` without
        ``verified`` is an attempt, NOT coverage.
    ``record_once_available``: whether this widget can be handled by a one-time
        human recording.

    Returns ``{rung, resolved, verified, provenance, needs_human, touch_type,
    reason}``. ``resolved`` means the decision is (or will be, via replay) handled;
    ``needs_human`` means a person is required now.
    """
    if deterministic and deterministic.get("resolved"):
        return {
            "rung": RUNG_DETERMINISTIC, "resolved": True, "verified": True,
            "provenance": PROV_DETERMINISTIC, "needs_human": False,
            "touch_type": None,
            "reason": "resolved deterministically (grounded by construction)",
        }

    if agentic and agentic.get("resolved"):
        if agentic.get("verified"):
            return {
                "rung": RUNG_AGENTIC, "resolved": True, "verified": True,
                "provenance": PROV_LIVE_CONFIRMED, "needs_human": False,
                "touch_type": None,
                "reason": "agent resolved and a verifier proved it registered",
            }
        # Resolved but UNVERIFIED — not coverage. Descend, and say why.
        unverified_reason = ("agent proposed an action but it could not be "
                             "verified — descending, not counted as covered")
    else:
        unverified_reason = "no automation resolved it"

    if record_once_available:
        return {
            "rung": RUNG_RECORD_ONCE, "resolved": True, "verified": False,
            "provenance": PROV_INFERRED, "needs_human": True,
            "touch_type": TOUCH_WIDGET_RECORD,
            "reason": f"{unverified_reason}; recordable once, then replays",
        }

    return {
        "rung": RUNG_HUMAN, "resolved": False, "verified": False,
        "provenance": PROV_INFERRED, "needs_human": True,
        "touch_type": TOUCH_WIDGET_RESOLVE,
        "reason": f"{unverified_reason}; needs a person to resolve",
    }


def coverage_by_rung(decisions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll up ladder decisions into an honest per-app coverage report.

    No averaged autonomy number (touch_meter's law): counts per rung, plus the
    count that still needs a human. A decision is "covered" only if it resolved.
    """
    counts = {r: 0 for r in RUNGS}
    resolved = needs_human = 0
    for d in decisions:
        rung = str(d.get("rung") or "")
        if rung in counts:
            counts[rung] += 1
        if d.get("resolved"):
            resolved += 1
        if d.get("needs_human"):
            needs_human += 1
    total = len(decisions)
    return {
        "total": total,
        "by_rung": counts,
        "resolved": resolved,
        "needs_human": needs_human,
        "unresolved": total - resolved,
    }
