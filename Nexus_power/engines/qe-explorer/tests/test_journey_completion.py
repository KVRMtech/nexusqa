"""Gate 1 / T-JC-01 — A CRAWL THAT NEVER CROSSED A STEP IS NOT A COMPLETED JOURNEY.

THE HOLE THIS CLOSES.  :func:`app.completion.adjudicate` already refuses a
completion claim that has no page states behind it, no readable inventory behind
it, or a resume it could not rebuild.  All three ask the same question — *did the
crawl SEE anything?* — and a crawl can answer yes to all three while having done
nothing this engine exists to do.

Twenty wizard steps were walked to, one at a time, from the same entry.  Each
arrival was observed, catalogued and counted.  None of them ADVANCED: every
journey stopped on the step it started on, because the forward control stayed
disabled.  ``states`` is 20, ``inventory_failures`` is 0, the resume is clean —
and the crawl reports ``completed``.  What it completed was page discovery,
reported under the name of journey execution.

THE DISTINCTION THIS MODULE DRAWS, and why it is not simply "crossings > 0".

A crawl of an application with no funnel in it — a content site, a dashboard, a
read-only report — has zero crossings and is CORRECTLY complete.  Refusing every
zero-crossing crawl would make those uncrawlable, which is why the invariant is
conditional on the crawl having ATTEMPTED a journey:

    A crawl that WALKED journeys and crossed no step in any of them has not
    completed; a crawl that walked none has nothing to cross and may.

``journeys_walked`` is what distinguishes the two, and it is a count of flows the
walker actually entered — not of forms seen, not of buttons found.
"""
from __future__ import annotations

import pytest

from app import completion
from app.completion import (
    DISPOSITION_COMPLETED,
    DISPOSITION_FAILED,
    DISPOSITION_INCOMPLETE,
    CrawlEvidence,
    adjudicate,
)
from app.crawl_constants import STOP_COMPLETED, STOP_ERROR


def _ev(**kw) -> CrawlEvidence:
    """Evidence for a crawl that saw real pages — so nothing BUT the journey
    question can be what a refusal below is about."""
    base = dict(states=20, actions=40, journeys_walked=0, journey_crossings=0)
    base.update(kw)
    return CrawlEvidence(**base)


# ─── the defect, stated as the case that must never pass ────────────────────

def test_a_journey_that_never_crossed_a_step_is_not_completed():
    """THE REGRESSION TEST FOR THIS GATE.  Twenty states, twenty journeys, zero
    forward transitions: the crawl saw the first step of the funnel twenty times
    and called it a completed crawl of the funnel."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(journeys_walked=20, journey_crossings=0))

    assert verdict.disposition == DISPOSITION_INCOMPLETE
    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING
    assert verdict.downgraded, "the refusal must be visible, not silent"
    assert verdict.claimed_stop_reason == STOP_COMPLETED


def test_one_crossing_is_enough_to_prove_progression():
    """The invariant is about ZERO, not about depth.  A crawl that crossed one
    step did the thing; how far it then got is a coverage question, and coverage
    is reported by the flow ledger, not adjudicated here."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(journeys_walked=20, journey_crossings=1))
    assert verdict.disposition == DISPOSITION_COMPLETED
    assert verdict.stop_reason == STOP_COMPLETED
    assert not verdict.downgraded


def test_an_application_with_no_journey_still_completes():
    """The case that makes this a CONDITIONAL invariant rather than a blanket
    one.  A content site has no funnel to cross.  Refusing it would make "has a
    wizard" a precondition of a successful crawl."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(journeys_walked=0, journey_crossings=0))
    assert verdict.disposition == DISPOSITION_COMPLETED
    assert not verdict.downgraded


# ─── the ordering: a more specific diagnosis always wins ────────────────────

def test_a_crawl_that_saw_nothing_is_reported_as_no_evidence_not_zero_crossing():
    """Both are true of this crawl, and only one is useful.  "No evidence" says
    the crawl never observed a page; "zero crossing" would send an operator
    looking at a funnel that was never reached."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(states=0, journeys_walked=3, journey_crossings=0))
    assert verdict.stop_reason == completion.STOP_NO_EVIDENCE


def test_an_unreadable_page_outranks_a_zero_crossing():
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(inventory_failures=1, journeys_walked=3,
                             journey_crossings=0))
    assert verdict.stop_reason == completion.STOP_INVENTORY_FAILED


def test_a_broken_resume_outranks_everything():
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(resumed=True, resume_broken=True,
                             journeys_walked=3, journey_crossings=0))
    assert verdict.stop_reason == completion.STOP_RESUME_UNRECOVERABLE


def test_a_crawl_that_already_failed_keeps_its_own_diagnosis():
    """The machine may only pull a verdict DOWN.  ``error`` carries a traceback
    and ``journey_zero_crossing`` does not — relabelling would destroy the more
    informative reason."""
    verdict = adjudicate(STOP_ERROR,
                         _ev(journeys_walked=3, journey_crossings=0))
    assert verdict.stop_reason == STOP_ERROR
    assert verdict.disposition == DISPOSITION_FAILED


# ─── budget stops are subject to the same test ──────────────────────────────

def test_a_budget_stop_with_no_crossing_is_also_refused():
    """A budget stop maps to ``completed`` — it DID cover what it covered.  But
    a crawl that burned its whole budget re-observing one wizard step covered no
    journey, and this is exactly how a traversal that goes nowhere looks in
    production: it never errors, it runs out of wall clock."""
    verdict = adjudicate("budget_wall_ms",
                         _ev(journeys_walked=8, journey_crossings=0))
    assert verdict.disposition == DISPOSITION_INCOMPLETE
    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING


# ─── a resumed crawl inherits its predecessor's crossings ───────────────────

def test_a_resume_that_adds_no_crossing_but_inherits_one_still_completes():
    """Same doctrine as ``total_states``: a resume that adds nothing new because
    its predecessor already crossed the funnel HAS the evidence, it simply did
    not add to it.  Judging this run's crossings alone would fail every
    late-stage resume."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(states=0, resumed=True, resumed_states=40,
                             journeys_walked=2, journey_crossings=0,
                             resumed_crossings=5))
    assert verdict.disposition == DISPOSITION_COMPLETED
    assert not verdict.downgraded


def test_a_resume_that_inherits_no_crossing_either_is_refused():
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(states=0, resumed=True, resumed_states=40,
                             journeys_walked=2, journey_crossings=0,
                             resumed_crossings=0))
    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING


# ─── the evidence is legible to somebody who does not trust the process ─────

def test_the_evidence_carries_the_journey_counts():
    """Every field of :class:`CrawlEvidence` is answerable from the manifest on
    disk after the process is gone.  The two new ones must be too, or the
    adjudication stops being independently checkable."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(journeys_walked=7, journey_crossings=0))
    assert verdict.evidence["journeys_walked"] == 7
    assert verdict.evidence["journey_crossings"] == 0
    assert verdict.evidence["total_crossings"] == 0


def test_the_detail_says_what_was_refused_and_why():
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(journeys_walked=20, journey_crossings=0))
    assert "20" in verdict.detail
    assert "refused to report completed" in verdict.detail


# ─── the classification matrix, enumerated ──────────────────────────────────

@pytest.mark.parametrize(
    "walked,crossings,expected_disposition", [
        (0,  0, DISPOSITION_COMPLETED),    # no funnel — nothing to cross
        (0,  3, DISPOSITION_COMPLETED),    # crossings without a counted walk
        (1,  0, DISPOSITION_INCOMPLETE),   # one journey, never advanced
        (20, 0, DISPOSITION_INCOMPLETE),   # the live shape of the defect
        (1,  1, DISPOSITION_COMPLETED),    # minimal proven progression
        (20, 4, DISPOSITION_COMPLETED),    # partial coverage, real progression
    ])
def test_the_classification_matrix(walked, crossings, expected_disposition):
    verdict = adjudicate(
        STOP_COMPLETED,
        _ev(journeys_walked=walked, journey_crossings=crossings))
    assert verdict.disposition == expected_disposition


# ─── false-positive completion is structurally impossible ───────────────────

def test_no_combination_of_evidence_upgrades_a_failure():
    """The asymmetry that makes this safe to put in every crawl's terminal path:
    exhaustively, no evidence turns a non-success claim into a success."""
    for walked in (0, 1, 20):
        for crossings in (0, 1, 9):
            verdict = adjudicate(STOP_ERROR, _ev(journeys_walked=walked,
                                                 journey_crossings=crossings))
            assert verdict.disposition == DISPOSITION_FAILED, (walked, crossings)


# ─── T-JC-02 · progression survives a resume ────────────────────────────────
#
# The limitation this closes was carried openly in the first Gate 1 draft:
# ``resumed_crossings`` was wired to 0 because no manifest record described a
# per-flow step transition. The checkpoint — which already persists ``states``
# and ``actions`` for exactly this purpose — is where the walker's own tally
# belongs, so it now travels there.

from app import resume_state                                    # noqa: E402
from app.emit import REC_CHECKPOINT                             # noqa: E402


def _checkpoint(**kw) -> dict:
    base = dict(frontier=(), visited=(), states=12, actions=30)
    base.update(kw)
    return resume_state.build_checkpoint(**base)


def test_a_checkpoint_carries_the_journey_counters():
    record = _checkpoint(journeys_walked=3, journey_crossings=19)
    assert record["journeys_walked"] == 3
    assert record["journey_crossings"] == 19
    assert record["type"] == REC_CHECKPOINT


def test_a_rebuilt_plan_inherits_the_crossings_the_checkpoint_recorded():
    plan = resume_state.rebuild(
        [{"type": "page_state"},
         _checkpoint(journeys_walked=2, journey_crossings=19)],
        resuming=True)
    assert plan.recoverable, plan.refusal
    assert plan.prior_crossings == 19
    assert plan.prior_journeys == 2


def test_a_prefix_written_before_this_field_existed_still_rebuilds():
    """BACKWARD COMPATIBILITY, as a test rather than a hope.  A manifest written
    by a pre-Gate-1 crawl has no such key, and must resume with zero inherited
    crossings — which is the behaviour it always had — rather than refusing."""
    old_style = {"type": REC_CHECKPOINT, "frontier": [], "spent_keys": [],
                 "visited_count": 4, "states": 12, "actions": 30,
                 "sequence_index": 7}
    plan = resume_state.rebuild([{"type": "page_state"}, old_style],
                                resuming=True)
    assert plan.recoverable
    assert plan.prior_crossings == 0
    assert plan.prior_journeys == 0


def test_the_inherited_crossings_are_what_spare_a_late_resume_a_refusal():
    """The end-to-end reason the field exists: a resume that re-walks two flows
    and advances neither, but whose predecessor crossed the funnel nineteen
    times, HAS the evidence — it simply did not add to it."""
    plan = resume_state.rebuild(
        [{"type": "page_state"}, _checkpoint(journey_crossings=19)],
        resuming=True)

    verdict = adjudicate(STOP_COMPLETED, CrawlEvidence(
        states=0, resumed=True, resumed_states=plan.prior_states or 12,
        journeys_walked=2, journey_crossings=0,
        resumed_crossings=plan.prior_crossings))

    assert verdict.disposition == DISPOSITION_COMPLETED
    assert not verdict.downgraded


def test_a_resume_whose_predecessor_also_crossed_nothing_is_still_refused():
    """Inheriting must not become a way to launder a zero-crossing crawl: the
    predecessor's zero is inherited just as faithfully as its nineteen."""
    plan = resume_state.rebuild(
        [{"type": "page_state"}, _checkpoint(journey_crossings=0)],
        resuming=True)

    verdict = adjudicate(STOP_COMPLETED, CrawlEvidence(
        states=0, resumed=True, resumed_states=12,
        journeys_walked=2, journey_crossings=0,
        resumed_crossings=plan.prior_crossings))

    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING


# ─── A2.2 · one state cannot have shown a crossing ──────────────────────────

def test_a_one_state_crawl_is_not_accused_of_a_dead_funnel():
    """THE MISDIAGNOSIS THIS PREVENTS.

    ``journey_zero_crossing`` names a specific remediation — its own disposition
    note says "the remediation is a look at the funnel ... not at the engine".
    That is a category error for a crawl that observed ONE state: a crossing is a
    relation BETWEEN two states, so there was never an observation that could
    have shown one. It reports the absence of an opportunity as the absence of a
    capability.

    Measured on characterization fixture ``09-questionnaire-20-samefingerprint``
    — a twenty-question questionnaire crawled with ``max_states=1``. It walked
    one journey, could not reach a second state because the cap forbade it, and
    was reported as a funnel that does not advance.
    """
    verdict = adjudicate("budget_max_states",
                         _ev(states=1, journeys_walked=1, journey_crossings=0))

    assert verdict.stop_reason == "budget_max_states", (
        f"a one-state crawl was re-labelled {verdict.stop_reason!r}, which sends "
        f"the operator to look at a funnel the crawl never had room to try")
    assert verdict.disposition == DISPOSITION_COMPLETED
    assert not verdict.downgraded


def test_two_states_and_no_crossing_is_still_refused():
    """THE BOUNDARY IS EXACTLY ONE. With two states observed and no crossing
    between them, the crawl DID have the opportunity and did not take it — which
    is the finding the gate exists for."""
    verdict = adjudicate("budget_max_states",
                         _ev(states=2, journeys_walked=1, journey_crossings=0))

    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING
    assert verdict.disposition == DISPOSITION_INCOMPLETE
    assert verdict.downgraded


def test_it_is_not_a_budget_exemption():
    """THE DISTINCTION THAT MAKES THIS SAFE, asserted rather than described.

    ``test_a_budget_stop_with_no_crossing_is_also_refused`` above protects the
    real production failure: "a crawl that burned its whole budget re-observing
    one wizard step covered no journey ... it never errors, it runs out of wall
    clock". That crawl saw MANY states and crossed none. Exempting budget stops
    would have hidden it; this condition is about how many states were OBSERVED,
    not about why the crawl stopped.
    """
    verdict = adjudicate("budget_wall_ms",
                         _ev(states=40, journeys_walked=8, journey_crossings=0))

    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING
    assert verdict.disposition == DISPOSITION_INCOMPLETE


def test_a_one_state_crawl_is_still_adjudicated_by_every_check_above_it():
    """The new clause cannot smuggle anything through: the checks ABOVE the
    zero-crossing rule still apply, so a one-state crawl whose inventory failed
    is still FAILED."""
    verdict = adjudicate("budget_max_states",
                         _ev(states=1, journeys_walked=1, journey_crossings=0,
                             inventory_failures=1))

    assert verdict.stop_reason == completion.STOP_INVENTORY_FAILED
    assert verdict.disposition != DISPOSITION_COMPLETED


def test_a_resume_whose_states_are_inherited_is_judged_on_the_total():
    """``total_states`` is the reading, not this run's count — the same doctrine
    the resume tests above establish. A resume that adds one state to a
    predecessor's forty had every opportunity to cross."""
    verdict = adjudicate(STOP_COMPLETED,
                         _ev(states=1, resumed=True, resumed_states=40,
                             journeys_walked=2, journey_crossings=0,
                             resumed_crossings=0))

    assert verdict.stop_reason == completion.STOP_JOURNEY_ZERO_CROSSING
