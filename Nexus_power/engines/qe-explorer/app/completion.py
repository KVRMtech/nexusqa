"""THE COMPLETION STATE MACHINE — a crawl is complete only when its evidence
says so (M1.7 / T-GW-01, T-GW-03).

Until this module existed, ``stop_reason`` was decided by CONTROL FLOW::

    if not self._stop_reason:
        self._stop_reason = STOP_COMPLETED

i.e. "nothing set a reason, therefore we finished".  That is an inference from
an absence, and it is the last link in every green-wash chain in the engine: an
inventory read that failed, a resume that could not rebuild its frontier, and a
crawl whose very first navigation was refused all reach that line with an empty
``_stop_reason`` and all three claim ``completed``.

The rule this module enforces is the milestone's central invariant:

    A CRAWL IS COMPLETE ONLY IF THE EVIDENCE PROVES IT.

So the terminal state is now ADJUDICATED from measured evidence
(:class:`CrawlEvidence`) rather than asserted by the code path that happened to
run last.  The adjudication is PURE — no clock, no I/O, no crawler — so every
branch of it is unit-testable, and a test can enumerate the whole state space.

TWO DIRECTIONS, DELIBERATELY ASYMMETRIC.  The machine may only ever DOWNGRADE a
claim.  It can turn ``completed`` into ``no_evidence``; it can never turn a
failure into a success, and it never invents a reason nobody set.  That
asymmetry is what makes it safe to put in the terminal path of every crawl:
the worst it can do is refuse to claim something.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .crawl_constants import (
    STOP_AUTH_FAILED,
    STOP_AUTH_REQUIRED,
    STOP_CANCELLED,
    STOP_COMPLETED,
    STOP_ERROR,
    STOP_INVENTORY_FAILED,
    STOP_NO_EVIDENCE,
    STOP_RESUME_UNRECOVERABLE,
)

#: The terminal DISPOSITION a stop reason maps onto — the vocabulary qe-central's
#: exploration row uses.  ``completed`` means "this crawl's claims are backed by
#: evidence"; ``failed`` means "it is not, and the operator must be told".
DISPOSITION_COMPLETED = "completed"
DISPOSITION_FAILED = "failed"
#: An honest STOP that is not a failure of the engine: the app needed credentials
#: we do not have, or an operator cancelled.  Distinct from ``failed`` because
#: the remediation is completely different, and from ``completed`` because the
#: crawl did NOT cover what it set out to cover.
DISPOSITION_INCOMPLETE = "incomplete"

#: stop_reason -> disposition.  Exhaustive over :mod:`app.crawl_constants`; an
#: unmapped reason is treated as ``failed`` (fail-closed — a reason nobody
#: classified must never be allowed to read as a success).
_DISPOSITION: dict[str, str] = {
    STOP_COMPLETED: DISPOSITION_COMPLETED,
    STOP_CANCELLED: DISPOSITION_INCOMPLETE,
    STOP_AUTH_REQUIRED: DISPOSITION_INCOMPLETE,
    STOP_AUTH_FAILED: DISPOSITION_FAILED,
    STOP_ERROR: DISPOSITION_FAILED,
    STOP_INVENTORY_FAILED: DISPOSITION_FAILED,
    STOP_RESUME_UNRECOVERABLE: DISPOSITION_FAILED,
    STOP_NO_EVIDENCE: DISPOSITION_FAILED,
}


def disposition_for(stop_reason: str) -> str:
    """The terminal disposition of ``stop_reason`` (fail-closed on unknowns).

    Budget stops (``budget_states``, ``budget_wall_ms``, …) are minted by
    :mod:`app.budget` rather than named as constants here; a crawl that ran out
    of budget DID cover everything it covered, so it maps to ``completed`` —
    but only if it has the evidence, which :func:`adjudicate` then checks.
    """
    reason = (stop_reason or "").strip()
    if not reason:
        return DISPOSITION_FAILED       # nobody set one: never assume success
    if reason in _DISPOSITION:
        return _DISPOSITION[reason]
    if reason.startswith("budget_"):
        return DISPOSITION_COMPLETED
    return DISPOSITION_FAILED


@dataclass(frozen=True)
class CrawlEvidence:
    """WHAT THE CRAWL ACTUALLY PRODUCED — the only input to a completion claim.

    Every field is a COUNT of something durable.  Nothing here is an intention, a
    flag or a status; each one is answerable from the manifest on disk after the
    process is gone, which is precisely what makes the adjudication verifiable by
    someone who does not trust the process that produced it.
    """

    #: page_state records this RUN wrote.
    states: int = 0
    #: action records this run wrote.
    actions: int = 0
    #: page_state records already durable in the manifest when this run started
    #: (a resumed crawl inherits its predecessor's evidence — see T-GW-03).
    resumed_states: int = 0
    #: Inventory reads that failed and could not be recovered (T-GW-01).
    inventory_failures: int = 0
    #: True when this run was dispatched as a RESUME of an existing crawl id.
    resumed: bool = False
    #: True when a resume was requested and the durable prefix could not be
    #: rebuilt into a continuable crawl.
    resume_broken: bool = False

    @property
    def total_states(self) -> int:
        """All the evidence this crawl id can show, this run plus its prefix."""
        return int(self.states) + int(self.resumed_states)

    def as_dict(self) -> dict:
        return {
            "states": self.states, "actions": self.actions,
            "resumed_states": self.resumed_states,
            "total_states": self.total_states,
            "inventory_failures": self.inventory_failures,
            "resumed": self.resumed, "resume_broken": self.resume_broken,
        }


@dataclass(frozen=True)
class CompletionVerdict:
    """The adjudicated terminal state, plus WHY — never a bare status.

    ``downgraded`` records that a claim was refused, so the downgrade is
    OBSERVABLE (the milestone's "recovery must always be observable" applied to
    completion): a crawl whose ``completed`` was refused logs and emits the
    original claim alongside the verdict, rather than quietly reporting a
    different reason than the engine thought it had.
    """

    stop_reason: str
    disposition: str
    detail: str = ""
    claimed_stop_reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def downgraded(self) -> bool:
        return bool(self.claimed_stop_reason) and self.claimed_stop_reason != self.stop_reason

    @property
    def completed(self) -> bool:
        return self.disposition == DISPOSITION_COMPLETED

    def as_dict(self) -> dict:
        return {
            "stop_reason": self.stop_reason, "disposition": self.disposition,
            "detail": self.detail, "downgraded": self.downgraded,
            "claimed_stop_reason": self.claimed_stop_reason,
            "evidence": dict(self.evidence),
        }


#: The sentence an operator reads when a completion claim was refused for want of
#: evidence.  Named here so the crawler, the tests and the manifest all quote the
#: same words.
NO_EVIDENCE_DETAIL = (
    "refused to report completed: the crawl recorded zero page states, so there "
    "is no evidence a page was ever successfully observed"
)

RESUME_BROKEN_DETAIL = (
    "refused to report completed: this run was dispatched as a resume and the "
    "durable prefix could not be rebuilt into a continuable crawl"
)

INVENTORY_FAILED_DETAIL = (
    "refused to report completed: %d inventory read(s) failed unrecoverably, so "
    "the pages behind them were never observed"
)


def adjudicate(stop_reason: str, evidence: CrawlEvidence) -> CompletionVerdict:
    """Decide the terminal state of a crawl from its evidence.  PURE.

    The order of the checks is the order of severity, and each one may only pull
    the verdict DOWN:

      1. **A resume that could not rebuild.**  The single most dangerous state in
         the engine: the crawl id already owns durable evidence, so a run that
         re-walks nothing and reports ``completed`` would supersede a real crawl
         with an empty one.  Always ``resume_unrecoverable``.
      2. **An unrecovered inventory failure.**  A page the crawl could not read is
         a page the crawl cannot make claims about (T-GW-01).
      3. **Zero states.**  The zero-state completion this milestone exists to
         make impossible.  Evaluated on ``total_states``, so a legitimate resume
         that adds nothing new because its predecessor already covered the app
         still completes — it HAS evidence, it simply did not add to it.
      4. Otherwise the engine's own reason stands.

    Anything already failing keeps its own, more specific, reason: a crawl that
    died with ``error`` must not be relabelled ``no_evidence``, because ``error``
    carries the traceback and ``no_evidence`` does not.
    """
    claimed = (stop_reason or "").strip()
    ev = evidence.as_dict()

    if evidence.resume_broken:
        return CompletionVerdict(
            stop_reason=STOP_RESUME_UNRECOVERABLE,
            disposition=DISPOSITION_FAILED,
            detail=RESUME_BROKEN_DETAIL,
            claimed_stop_reason=claimed, evidence=ev,
        )

    claimed_disposition = disposition_for(claimed)

    # A crawl that already knows it failed keeps its own diagnosis.  Only a claim
    # of SUCCESS is subject to the evidence tests below.
    if claimed_disposition != DISPOSITION_COMPLETED:
        return CompletionVerdict(
            stop_reason=claimed or STOP_NO_EVIDENCE,
            disposition=claimed_disposition,
            detail="", claimed_stop_reason=claimed, evidence=ev,
        )

    if evidence.inventory_failures > 0:
        return CompletionVerdict(
            stop_reason=STOP_INVENTORY_FAILED,
            disposition=DISPOSITION_FAILED,
            detail=INVENTORY_FAILED_DETAIL % evidence.inventory_failures,
            claimed_stop_reason=claimed, evidence=ev,
        )

    if evidence.total_states <= 0:
        return CompletionVerdict(
            stop_reason=STOP_NO_EVIDENCE,
            disposition=DISPOSITION_FAILED,
            detail=NO_EVIDENCE_DETAIL,
            claimed_stop_reason=claimed, evidence=ev,
        )

    return CompletionVerdict(
        stop_reason=claimed, disposition=DISPOSITION_COMPLETED,
        detail="", claimed_stop_reason=claimed, evidence=ev,
    )


__all__ = [
    "DISPOSITION_COMPLETED", "DISPOSITION_FAILED", "DISPOSITION_INCOMPLETE",
    "CrawlEvidence", "CompletionVerdict", "adjudicate", "disposition_for",
    "NO_EVIDENCE_DETAIL", "RESUME_BROKEN_DETAIL", "INVENTORY_FAILED_DETAIL",
]
