"""BUSINESS FLOWS — a journey is a PATH, and a path has to be able to say whether
it finished.

The crawl records states and actions. A business flow is neither: it is a *path*
through them — an entry, an ordered sequence of steps, and a terminal. Without
flows as objects the product can count pages and fields but cannot answer the only
question a client actually asks: **did you get all the way through Apply?**

The distinction this module exists to keep is between:

    COMPLETED   the walk reached a natural end — a submit boundary, or a step with
                nothing left to advance. The journey was covered.
    TRUNCATED   the walk stopped because it ran out of budget, looped, or was
                cancelled. The journey was NOT covered, and saying it was is the
                same class of claim as a green test that never ran.

Six steps of a fifteen-step funnel is not "the Apply flow". A crawl that reports
it as one has green-washed coverage, which is the failure this whole product
exists to prevent — so the terminal reason is recorded on every flow and the
completion flag is derived from it, never set directly.

Pure + deterministic: no clock, no I/O.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "TERMINAL_SUBMIT_BOUNDARY", "TERMINAL_NO_ADVANCE", "TERMINAL_BUDGET",
    "TERMINAL_LOOP", "TERMINAL_CANCELLED", "COMPLETING_TERMINALS",
    "flow_id_for", "build_flow", "summarize",
]

#: The walk stopped at a control it may not cross without an approval — the end of
#: the journey as far as a non-mutating crawl is concerned. The funnel WAS covered.
TERMINAL_SUBMIT_BOUNDARY = "submit_boundary"
#: Nothing left to advance: the last step offered no Next/Continue. Also complete.
TERMINAL_NO_ADVANCE = "no_advance"
#: Ran out of step / advance / depth budget. NOT complete — there was more funnel.
TERMINAL_BUDGET = "budget_exhausted"
#: The next step was a state already seen — the app looped us. NOT complete.
TERMINAL_LOOP = "loop"
#: The crawl was stopped. NOT complete.
TERMINAL_CANCELLED = "cancelled"

#: Exactly the terminals that mean the journey was actually covered. A terminal not
#: in this set produces ``completed=False``, and there is deliberately no way for a
#: caller to override that.
COMPLETING_TERMINALS = frozenset({TERMINAL_SUBMIT_BOUNDARY, TERMINAL_NO_ADVANCE})


def flow_id_for(entry_fingerprint: str) -> str:
    """A flow is identified by where it STARTS.

    The entry state is the only part of a journey that is stable across crawls: the
    steps change as the app changes, and the terminal changes as budgets change. So
    the same funnel keeps one id and a client can watch it get deeper over time
    rather than seeing a new flow appear every crawl."""
    return hashlib.sha256(f"flow::{entry_fingerprint}".encode("utf-8")).hexdigest()[:24]


def build_flow(*, entry_fingerprint: str, entry_url: str, entry_title: str,
               steps: Sequence[Mapping[str, Any]], terminal: str,
               terminal_url: str = "", outcome_values: Iterable[Mapping[str, Any]] = (),
               max_steps: int = 0) -> dict[str, Any]:
    """One recorded journey.

    ``completed`` is DERIVED from the terminal reason, never passed in — the whole
    point is that a truncated walk cannot be reported as a covered journey."""
    term = str(terminal or TERMINAL_BUDGET)
    step_list = [
        {
            "fingerprint": str(s.get("fingerprint") or ""),
            "url": str(s.get("url") or ""),
            "title": str(s.get("title") or "")[:200],
            "fields_filled": int(s.get("fields_filled") or 0),
            "fields_unfilled": int(s.get("fields_unfilled") or 0),
        }
        for s in (steps or ())
    ]
    # An outcome the funnel produced — a premium, an eligibility decision. This is
    # what makes a flow worth having walked, and (in the next phase) what decides
    # whether a different choice produced a genuinely different business path.
    outcomes = [
        {"label": str(v.get("label") or "")[:120],
         "value": str(v.get("value") or "")[:200],
         "value_type": str(v.get("value_type") or "")}
        for v in (outcome_values or ())
    ][:12]

    unfilled = sum(s["fields_unfilled"] for s in step_list)
    return {
        "flow_id": flow_id_for(entry_fingerprint),
        "entry_fingerprint": entry_fingerprint,
        "entry_url": entry_url,
        "entry_title": str(entry_title or "")[:200],
        "steps": step_list,
        "step_count": len(step_list),
        "terminal": term,
        "terminal_url": terminal_url,
        "completed": term in COMPLETING_TERMINALS,
        # A journey walked to its end while fields went unanswered was covered
        # STRUCTURALLY but not with real data. Both facts matter and neither
        # substitutes for the other.
        "fields_unanswered": unfilled,
        "fully_answered": unfilled == 0,
        "outcome_values": outcomes,
        # Recorded so a truncated flow can say what it hit rather than only that it
        # stopped — "6 of a 6-step budget" is actionable; "stopped" is not.
        "step_budget": int(max_steps or 0),
    }


def summarize(flows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The coverage claim, stated so it cannot be read as more than it is.

    ``branch_coverage`` is reported as ``False`` explicitly rather than omitted: a
    reader must not be able to infer that walking every flow once means every
    business path was covered. One path per funnel is one path, and the decision
    points that fork the funnel have not been explored."""
    total = len(flows)
    done = [f for f in flows if f.get("completed")]
    answered = [f for f in done if f.get("fully_answered")]
    truncated = [f for f in flows if not f.get("completed")]
    reasons: dict[str, int] = {}
    for f in truncated:
        r = str(f.get("terminal") or "")
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "flows_found": total,
        "flows_completed": len(done),
        "flows_fully_answered": len(answered),
        "flows_truncated": len(truncated),
        "truncation_reasons": reasons,
        "deepest_flow_steps": max((int(f.get("step_count") or 0) for f in flows), default=0),
        # THE HONESTY FLAG. One path per funnel was walked. Which option was taken
        # at each decision point was decided once, and the alternatives — a
        # different premium, a different eligibility outcome — were never visited.
        "branch_coverage": False,
        "branch_coverage_note": (
            "One path per journey. At each decision point a single option was taken, "
            "so business paths behind the other options were not visited."
        ),
    }
