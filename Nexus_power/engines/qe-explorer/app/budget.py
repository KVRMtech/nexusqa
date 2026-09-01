"""Crawl budgets and the honest terminal reason (M0.3 / T-DE-03).

Extracted VERBATIM from :mod:`app.crawler`.  The three budget stop reasons
travel with the tracker that raises them: they are the budget's own vocabulary,
and leaving them behind would mean :class:`BudgetTracker` importing the module
it was extracted from.  :mod:`app.crawler` re-exports all five names, so every
existing import site — production and test — is unaffected.

This module has NO runtime dependency on any other ``app`` module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from .crawl_constants import (_FULL_DEP_PROBES, _FULL_OPTION_PROBES,
                              _FULL_PROBED_OPTIONS, _MAX_DEP_PROBES,
                              _MAX_OPTION_PROBES, _MAX_PROBED_OPTIONS,
                              _MAX_WIZARD_ADVANCES, _MAX_WIZARD_STEPS)

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .emit import MonotonicClock


# ─── Honest stop reasons owned by the budget ─────────────────────────────────
STOP_MAX_STATES = "budget_max_states"
STOP_MAX_REQUESTS = "budget_max_requests"
STOP_MAX_WALL_MS = "budget_max_wall_ms"


# ─── Budgets ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Budget:
    """Crawl budgets (design §3.2 defaults; env-overridable via config)."""

    max_states: int = 200
    max_depth: int = 6
    max_actions_per_state: int = 30
    max_wall_ms: int = 1_800_000
    max_requests: int = 5000
    rate_per_s: float = 1.0

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "Budget":
        d = dict(data or {})
        base = cls()
        return cls(
            max_states=int(d.get("max_states", base.max_states)),
            max_depth=int(d.get("max_depth", base.max_depth)),
            max_actions_per_state=int(d.get("max_actions_per_state", base.max_actions_per_state)),
            max_wall_ms=int(d.get("max_wall_ms", base.max_wall_ms)),
            max_requests=int(d.get("max_requests", base.max_requests)),
            rate_per_s=float(d.get("rate_per_s", base.rate_per_s)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_states": self.max_states, "max_depth": self.max_depth,
            "max_actions_per_state": self.max_actions_per_state,
            "max_wall_ms": self.max_wall_ms, "max_requests": self.max_requests,
            "rate_per_s": self.rate_per_s,
        }


@dataclass(frozen=True)
class TraversalBudget:
    """How deep a JOURNEY may be walked, and how much of a form is enumerated.

    THE SECOND BUDGET SYSTEM, now beside the first (M0.3 / T-DE-14).  ``Budget``
    bounds the CRAWL (states, requests, wall-clock); these five bound the WALK
    and the CATALOGUE.  They were computed inline in ``Crawler.__init__`` from
    the traversal posture, which meant the two budget systems lived nowhere near
    each other and neither could be reviewed as a whole.  Same values, same
    derivation — one home.

    THIS IS NOT A SAFETY DIAL.  What may be CLICKED is decided by the refuse
    pack, the danger gate and the disposable-attestation submit tier, none of
    which read these numbers.  A deeper walk must never be a laxer one.
    """

    max_wizard_steps: int
    max_wizard_advances: int
    max_option_probes: int
    max_probed_options: int
    max_dep_probes: int

    @classmethod
    def for_posture(cls, *, full_traversal: bool, e2e_wizard_steps: int,
                    e2e_wizard_advances: int) -> "TraversalBudget":
        """Derive the bounds for this crawl's posture.

        E2E budgets are DEPLOY-CONFIGURABLE (a fifteen-step funnel needs more
        than a probe budget); a probe-posture crawl keeps the probe bounds.

        CATALOGUE COMPLETENESS: a probe samples the shape of a form; a
        full-traversal crawl has to hold every answer each question offers,
        because that enumeration IS the test data for the positive, negative
        and boundary cases generated from it.  Bounded in both postures — what
        changes is whether the bound is sized for a sample or for a real
        answer set.
        """
        if full_traversal:
            return cls(
                max_wizard_steps=int(e2e_wizard_steps),
                max_wizard_advances=int(e2e_wizard_advances),
                max_option_probes=_FULL_OPTION_PROBES,
                max_probed_options=_FULL_PROBED_OPTIONS,
                max_dep_probes=_FULL_DEP_PROBES,
            )
        return cls(
            max_wizard_steps=_MAX_WIZARD_STEPS,
            max_wizard_advances=_MAX_WIZARD_ADVANCES,
            max_option_probes=_MAX_OPTION_PROBES,
            max_probed_options=_MAX_PROBED_OPTIONS,
            max_dep_probes=_MAX_DEP_PROBES,
        )


class BudgetTracker:
    """Tracks crawl progress against a :class:`Budget` and reports the honest
    terminal reason.

    ``requests`` counts CRAWLER-INITIATED browser operations (navigations +
    actions) — a deterministic, crawler-owned proxy for network volume (the
    literal network cap is enforced structurally by squid's host allowlist and
    the guard's method block, not by a counter).  ``elapsed_ms`` is measured
    from THIS run's start (not the resume offset) so the wall budget is per-run.
    """

    def __init__(self, budget: Budget, clock: "MonotonicClock") -> None:
        self.budget = budget
        self._clock = clock
        self._start_ms = clock.now_ms()
        self.states = 0
        self.actions = 0
        self.requests = 0

    def note_state(self) -> None:
        self.states += 1

    def note_action(self, n: int = 1) -> None:
        self.actions += n

    def note_request(self, n: int = 1) -> None:
        self.requests += n

    @property
    def elapsed_ms(self) -> int:
        return self._clock.now_ms() - self._start_ms

    def stop_reason(self) -> str:
        """Return the honest budget stop reason, or ``""`` while within budget.

        Precedence (deterministic, documented): wall-clock, then requests, then
        states — the hardest external constraint first.
        """
        if self.budget.max_wall_ms and self.elapsed_ms >= self.budget.max_wall_ms:
            return STOP_MAX_WALL_MS
        if self.budget.max_requests and self.requests >= self.budget.max_requests:
            return STOP_MAX_REQUESTS
        if self.budget.max_states and self.states >= self.budget.max_states:
            return STOP_MAX_STATES
        return ""

    def snapshot(self) -> dict[str, Any]:
        return {"states": self.states, "actions": self.actions,
                "requests": self.requests, "elapsed_ms": self.elapsed_ms}


__all__ = ["STOP_MAX_REQUESTS", "STOP_MAX_STATES", "STOP_MAX_WALL_MS",
           "Budget", "BudgetTracker", "TraversalBudget"]
