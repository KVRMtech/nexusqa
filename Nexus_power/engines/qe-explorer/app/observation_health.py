"""OBSERVATION HEALTH — the difference between "this page has nothing on it" and
"we failed to read this page" (M1.7 / T-GW-01).

THE HOLE THIS CLOSES.  ``BrowserPort.collect_controls`` has always caught every
exception and returned ``[]``::

    async def collect_controls(self):
        try:
            return list(await self._page.evaluate(INVENTORY_JS) or [])
        except Exception as exc:
            logger.warning("qec.explorer.inventory_failed error=%s", exc)
            return []

That is a lie of omission with a very specific consequence.  ``[]`` is a
perfectly legitimate observation — a static confirmation page really does have
no interactive controls — so every consumer downstream treats the failure as a
fact about the APPLICATION rather than a fact about the CRAWL.  The chain is:

    INVENTORY_JS throws (CSP, a page that redefined an intrinsic the walker
    uses, a context destroyed mid-evaluate, an evaluate that timed out)
        -> collect_controls() returns []
        -> build_inventory([]) returns []
        -> fingerprint(url, controls=[]) returns a perfectly valid 64-char digest
        -> the state records with zero controls and zero actions
        -> nothing is discovered, so nothing is enqueued
        -> the frontier drains
        -> _explore_loop returns
        -> stop_reason = "completed"

A crawl that read NOTHING reports ``completed``, and qe-central writes an empty
substrate over the top of a good one.  No amount of care further down can fix
this, because by the time the ``[]`` has been returned the evidence that it was
a failure has been destroyed.  The repair has to happen at the read.

WHAT THIS MODULE IS.  A value type (:class:`InventoryResult`) that carries the
controls AND the health of the read that produced them, plus the pure classifier
that names WHY a read failed.  No I/O and no Playwright import — it unit-tests
without a browser, and the adapter is the only thing that has to know how to
raise into it.

WHAT IT DELIBERATELY IS NOT.  It does not decide what the crawl DOES about a
failed read; that is the caller's business (see
:meth:`app.discovery.DiscoveryMixin._expand` and :mod:`app.completion`).  This
module only makes the failure impossible to confuse with an empty page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: The read succeeded.  ``controls`` is a FACT about the page — including the
#: legitimate fact that the page has none.
INVENTORY_OK = "ok"

#: ``page.evaluate`` raised.  A JavaScript exception inside INVENTORY_JS, a CSP
#: that refused the injection, a page that broke an intrinsic the walker uses.
INVENTORY_EVAL_FAILED = "eval_failed"

#: The evaluate did not return within its budget.  A page pinned at 100% CPU, or
#: an inventory walk over a pathological DOM.
INVENTORY_TIMEOUT = "timeout"

#: The execution context went away mid-read — a navigation, a same-document
#: replace, or the target page/tab closing under us.  The controls we would have
#: got describe a page that no longer exists.
INVENTORY_CONTEXT_LOST = "context_lost"

#: The evaluate returned, but not the shape the contract promises (not a list, or
#: a list holding non-mappings).  Observation corruption: something answered, and
#: what it said cannot be trusted.
INVENTORY_MALFORMED = "malformed"

#: Every status other than :data:`INVENTORY_OK` — a read whose result must never
#: reach a fingerprint.
FAILURE_STATUSES = frozenset({
    INVENTORY_EVAL_FAILED, INVENTORY_TIMEOUT, INVENTORY_CONTEXT_LOST,
    INVENTORY_MALFORMED,
})

#: Every legal status, so a test can assert the vocabulary is closed.
INVENTORY_STATUSES = frozenset({INVENTORY_OK}) | FAILURE_STATUSES

#: Substrings Playwright/CDP put in the message when the execution context the
#: evaluate was issued against no longer exists.  Matched case-insensitively on
#: the exception TEXT because the adapter must not import Playwright's private
#: error classes to classify (and a jsdom/fake lane has none of them at all).
_CONTEXT_LOST_RX = re.compile(
    r"execution context was destroyed"
    r"|context or browser has been closed"
    r"|target page, context or browser"
    r"|target closed"
    r"|frame was detached"
    r"|because of a navigation",
    re.IGNORECASE,
)

_TIMEOUT_RX = re.compile(r"timeout|timed out|exceeded", re.IGNORECASE)


def classify_inventory_error(exc: BaseException) -> str:
    """Name WHY an inventory read failed, from the exception alone.

    The distinction is operational, not cosmetic.  A ``context_lost`` read is
    worth ONE retry — the page moved under us, and the next read sees the page it
    moved to.  An ``eval_failed`` read is a property of the document; retrying it
    is a way to spend the wall budget discovering the same thing twice.  Both are
    failures, neither may ever fingerprint, and the operator is told which one
    happened.

    Ordered most-specific first: a timeout on an evaluate whose target also
    closed reports ``context_lost``, because that is the actionable fact.
    """
    text = "%s: %s" % (type(exc).__name__, exc)
    if _CONTEXT_LOST_RX.search(text):
        return INVENTORY_CONTEXT_LOST
    if _TIMEOUT_RX.search(text):
        return INVENTORY_TIMEOUT
    return INVENTORY_EVAL_FAILED


def _is_control_shaped(raw: Any) -> bool:
    """A raw control is a mapping.  Anything else means the page answered the
    injection with something that is not the contract."""
    return isinstance(raw, Mapping)


@dataclass(frozen=True)
class InventoryResult:
    """The controls read off a page AND the health of the read that produced them.

    Immutable on purpose: an observation's health is decided once, at the read, by
    the only code that can still see the exception.  Nothing downstream may
    upgrade a failed read to a healthy one — ``ok=False`` is a fact that travels
    with the data for the rest of its life.
    """

    controls: tuple = ()
    status: str = INVENTORY_OK
    error: str = ""

    @property
    def ok(self) -> bool:
        """True only for a read that actually completed.  An empty ``controls`` on
        an ``ok`` result is the legitimate "this page has no controls"."""
        return self.status == INVENTORY_OK

    @property
    def failed(self) -> bool:
        return not self.ok

    def as_list(self) -> list:
        """The controls as the plain list every existing caller expects."""
        return list(self.controls)

    def diagnostic(self) -> str:
        """One operator-facing sentence naming the failure; ``""`` when healthy."""
        if self.ok:
            return ""
        return "inventory read failed (%s): %s" % (self.status, self.error or "no detail")

    @classmethod
    def healthy(cls, controls: Sequence) -> "InventoryResult":
        """A successful read of an already-validated payload."""
        return cls(controls=tuple(c for c in controls if _is_control_shaped(c)))

    @classmethod
    def from_payload(cls, payload: Any) -> "InventoryResult":
        """Classify what ``page.evaluate(INVENTORY_JS)`` actually returned.

        ``None`` is treated as ``[]`` — that is the historical, documented shape
        for "the walker found nothing" and predates this module.  A non-list, or a
        list whose members are not control mappings, is OBSERVATION CORRUPTION:
        something answered the injection and what it said is not the contract, so
        it must not be quietly filtered down to a plausible-looking inventory.
        """
        if payload is None:
            return cls(controls=())
        if not isinstance(payload, (list, tuple)):
            return cls(status=INVENTORY_MALFORMED,
                       error="inventory returned %s, expected a list"
                             % type(payload).__name__)
        bad = sum(1 for c in payload if not _is_control_shaped(c))
        if bad:
            return cls(status=INVENTORY_MALFORMED,
                       error="%d of %d inventory entries are not control mappings"
                             % (bad, len(payload)))
        return cls(controls=tuple(payload))

    @classmethod
    def from_exception(cls, exc: BaseException) -> "InventoryResult":
        """A read that raised — classified, with the message kept bounded."""
        return cls(status=classify_inventory_error(exc), error=str(exc)[:300])


#: Failures whose cause was the page MOVING rather than the page BEING broken.
_RETRYABLE = frozenset({INVENTORY_CONTEXT_LOST, INVENTORY_TIMEOUT})


def is_retryable(status: str) -> bool:
    """Whether a re-read could plausibly succeed where this one failed.

    ``context_lost`` and ``timeout`` are transient by construction — the page was
    moving, or was momentarily too busy.  ``eval_failed`` and ``malformed`` are
    properties of the document; a second read gets the same answer and spends
    budget proving it.  Retry is BOUNDED by the caller to one attempt either way:
    the point of a retry is to survive a race, never to search for a good read.
    """
    return status in _RETRYABLE


__all__ = [
    "INVENTORY_OK", "INVENTORY_EVAL_FAILED", "INVENTORY_TIMEOUT",
    "INVENTORY_CONTEXT_LOST", "INVENTORY_MALFORMED",
    "INVENTORY_STATUSES", "FAILURE_STATUSES",
    "InventoryResult", "classify_inventory_error", "is_retryable",
]
