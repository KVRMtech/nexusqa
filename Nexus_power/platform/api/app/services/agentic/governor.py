"""Governor — the cost + governance + provenance layer for the agentic-QE suite.

Generic and additive. Three jobs:
  1) per-agent ON/OFF (with tenant/run defaults) so customers pay only for what they use;
  2) a per-run BUDGET cap + dedupe so a 50-scenario regression cannot fan out into
     50 LLM calls (a TPM/429 cascade);
  3) PROVENANCE stamping so every agent claim is legibly an INFERENCE, a LIVE-CONFIRMED
     fact, or a DETERMINISTIC verdict — never laundered into apparent ground truth.

The Governor only decides WHETHER to spend and HOW to label — never WHETHER to pass.
No agent turns a step green; the orthogonal oracle still disposes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# The agentic suite. $0 deterministic agents default ON; LLM agents default OFF
# (opt-in for cost). A tenant or a single run can override any of these via prefs.
AGENTS = ("sentinel", "context", "triage", "verdict", "intent")

_DEFAULTS = {
    "sentinel": True,    # auto-run the $0 deterministic diagnosis on every failure (read-only)
    "triage":   True,    # $0 deterministic PRODUCT / SCRIPT / ENVIRONMENT routing
    "verdict":  True,    # $0 deterministic fix / build / flag disposition (derived from triage)
    "context":  False,   # LLM cross-field semantic reasoner — opt-in (cost)
    "intent":   False,   # LLM requirement / intent oracle — opt-in (cost)
}

# Grounding classes for a claim's provenance (most-trusted first).
G_DETERMINISTIC = "deterministic"    # a pure deterministic verdict (no LLM)
G_LIVE_CONFIRMED = "live_confirmed"  # an LLM claim confirmed against a captured live fact
G_INFERRED = "inferred"              # an LLM inference NOT confirmed against a live fact


def agent_enabled(name: str, prefs: dict | None = None) -> bool:
    """Is agent ``name`` on? ``prefs`` (per-tenant / per-run, surface-prefs style)
    overrides the default. Unknown names default OFF (fail-safe)."""
    name = (name or "").strip().lower()
    if name not in _DEFAULTS:
        return False
    p = prefs or {}
    agentic = p.get("agentic") if isinstance(p.get("agentic"), dict) else p
    val = agentic.get(name) if isinstance(agentic, dict) else None
    return bool(_DEFAULTS[name] if val is None else val)


def default_prefs() -> dict:
    """The default per-agent on/off map (for a fresh tenant / the UI)."""
    return dict(_DEFAULTS)


@dataclass
class BudgetGuard:
    """A per-run cap + dedupe for EXPENSIVE (LLM) agent calls. Cheap deterministic
    agents do NOT consume budget. ``allow`` collapses identical failures (same step
    fingerprint) so a regression repeated across N scenarios costs ONE call."""
    max_llm_calls: int = 5
    _spent: int = 0
    _seen: set = field(default_factory=set)

    def fingerprint(self, *parts) -> str:
        raw = "\x1f".join(str(p if p is not None else "") for p in parts)
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]

    def allow(self, *fingerprint_parts) -> bool:
        """True iff an LLM call is permitted for this failure: budget remains AND this
        is not a duplicate of one already spent this run. Records the spend on True."""
        if self._spent >= self.max_llm_calls:
            return False
        fp = self.fingerprint(*fingerprint_parts)
        if fp in self._seen:
            return False
        self._seen.add(fp)
        self._spent += 1
        return True

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(0, self.max_llm_calls - self._spent)


def stamp(claim: str, *, grounding: str, facts=None) -> dict:
    """Wrap an agent claim with its provenance so the UI renders it honestly: a
    deterministic verdict, a live-confirmed fact, or an inference. ``facts`` are the
    grounded observations the claim rests on (e.g. the live option set it was checked
    against)."""
    g = grounding if grounding in (G_DETERMINISTIC, G_LIVE_CONFIRMED, G_INFERRED) else G_INFERRED
    return {
        "claim": claim or "",
        "grounding": g,
        "is_inference": g != G_DETERMINISTIC,
        "confirmed_against_live": g == G_LIVE_CONFIRMED,
        "facts": list(facts or []),
    }
