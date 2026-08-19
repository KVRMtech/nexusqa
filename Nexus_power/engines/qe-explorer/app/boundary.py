"""THE APPROVED SUBMIT CROSSING (A4.3) — the boundary model, the approval seam,
the exactly-once ledger and the outcome milestone.

WHAT WAS MISSING, EXACTLY.

The crawl already refused irreversible controls, and it already had a Phase-B
submit tier.  What it did not have was a *route between them*.  Every path that
could cross a ``danger`` control was gated on ``_submit_approve_all`` — the
``"*"`` blanket — and never on a named, per-control approval:

    walker._pick_advance          `if self._submit_enabled and self._submit_approve_all:`
    submit._maybe_submit_next_action  `if c.get("danger") and not self._submit_approve_all: continue`
    submit._maybe_submit_phase_b      `if fc.danger and not self._submit_approve_all: continue`

So the only way to cross "Bind Coverage" was to authorise EVERY submit in the
application at once.  Least privilege was not merely unenforced, it was
unexpressible.

And the operator could not have named the control anyway.  Approvals are chosen
from a prior crawl's ``coverage.submit_candidates``, and BOTH producers of that
list dropped dangerous controls on the floor:

    walker._note_boundary_controls    `if not name or c.get("danger"): continue`
    discovery                         `fc.name for fc in fill.flow_candidates if not fc.danger`

A control that requires approval was the one control never offered for
approval.  That is the whole deadlock, and it is why completed journeys were
zero: the confirmation page on the far side of a real commit button had never
once been reached.

WHAT THIS MODULE ADDS.

  1. A boundary MODEL with three classes, not two (:func:`classify_boundary`).
     ``submit_candidates`` used to mean both "safe to cross" and "what you may
     approve"; one list cannot carry two meanings without one of them being
     wrong for somebody.  Here ``approvable_boundary`` is its own concept.
  2. An approval GRANT that targets ONE control (:class:`ApprovalGrant`).  Not
     a page, not a wildcard — ``"*"`` is refused at parse time, loudly.
  3. An exactly-once LEDGER (:class:`CrossingLedger`) keyed on the LOGICAL
     boundary, reserved BEFORE the click, so a crash mid-crossing still leaves
     the boundary marked crossed.  Ambiguity resolves to "already crossed".
  4. The OUTCOME MILESTONE (:class:`OutcomeMilestone`) — the verified landing on
     the far side.  Completion is the transition, never the click and never a
     counter.

PURE.  No I/O, no clock, no browser, no global state.  Every function here is
deterministic given its arguments, which is what makes the crossing replayable
and the tests real proofs rather than mocks agreeing with themselves.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .crawl_constants import _AUTH_SESSION_RE, _WIZARD_COMMIT_RE, _norm_label, _url_key

__all__ = [
    "BOUNDARY_SAFE", "BOUNDARY_APPROVABLE", "BOUNDARY_NEVER",
    "BOUNDARY_CLASSES",
    "AUTHORITY_GRANT", "AUTHORITY_NAMED", "AUTHORITY_BLANKET",
    "REASON_DANGER_VERB", "REASON_COMMIT_SHAPE", "REASON_SESSION_TEARDOWN",
    "REASON_NOT_AN_ACTUATOR", "REASON_DISABLED", "REASON_UNNAMED", "REASON_SAFE",
    "RUNG_NAVIGATION", "RUNG_ARIA_STATUS", "RUNG_TRANSITION_TEXT", "RUNG_DIALOG",
    "CONFIRMATION_RUNGS", "DECLARED_CONFIRMATION_RUNGS", "is_confirmation_landing",
    "BoundaryClassification", "classify_boundary", "boundary_key",
    "ApprovalGrant", "ApprovalRegistry", "GrantParseError", "parse_grants",
    "CrossingRecord", "CrossingLedger",
    "OutcomeMilestone", "milestone_id_for", "confirmation_transition",
    "dom_digest",
    "CROSSING_RESERVED", "CROSSING_CROSSED", "CROSSING_REFUSED",
]


# ═══════════════════════════════════════════════════════════════════════════
#  1. THE BOUNDARY MODEL  (T-AC-01)
# ═══════════════════════════════════════════════════════════════════════════

#: The walk may cross this control on its own authority.  Nothing irreversible
#: is declared by its label and the refuse pack does not object.
BOUNDARY_SAFE = "safe"
#: An irreversible commit boundary.  The walk STOPS here and the operator is
#: offered the control by name.  Crossing requires an :class:`ApprovalGrant`.
BOUNDARY_APPROVABLE = "approvable"
#: Never crossable, and never offered for approval, at any privilege level.
#: Reserved for controls whose effect is to destroy the crawl's own ability to
#: observe — a sign-out ends the session, so "approving" it would trade the
#: entire remaining journey for one click that proves nothing.
BOUNDARY_NEVER = "never"

BOUNDARY_CLASSES = (BOUNDARY_SAFE, BOUNDARY_APPROVABLE, BOUNDARY_NEVER)

# ── WHOSE AUTHORITY a crossing ran under.  Stamped on every crossing record so
# an auditor can separate the least-privilege path from the two legacy ones
# without reading code.  A fleet report that cannot tell "the operator named
# this control" from "the operator ticked a box once, months ago" is not an
# audit trail.
#: A per-control :class:`ApprovalGrant` — one control, optionally one page,
#: bounded crossings.  The only authority that may cross an irreversible verb.
AUTHORITY_GRANT = "boundary_grant"
#: A bare label in the legacy ``submit_approvals`` list.  Non-irreversible only.
AUTHORITY_NAMED = "named_approval"
#: The ``"*"`` disposable-env blanket.  Legacy, unchanged, still attested.
AUTHORITY_BLANKET = "disposable_blanket"

REASON_DANGER_VERB = "refuse_pack_irreversible_verb"
REASON_COMMIT_SHAPE = "commit_shaped_label"
REASON_SESSION_TEARDOWN = "session_teardown"
REASON_NOT_AN_ACTUATOR = "not_an_actuator"
REASON_DISABLED = "disabled"
REASON_UNNAMED = "unnamed"
REASON_SAFE = "no_irreversible_signal"

#: The control kinds that can actuate anything.  Everything else (a text box, a
#: heading, a region) has no boundary to classify.
_ACTUATOR_KINDS = frozenset({"button", "link", "submit"})


@dataclass(frozen=True)
class BoundaryClassification:
    """What class of boundary one control is, and WHY.

    The ``reason`` is not decoration.  An operator looking at an approval
    request has to be able to answer "why is this one different from the
    Continue button on the previous step", and a bare boolean cannot tell them.
    """

    cls: str
    reason: str
    rule_id: str = ""
    severity: str = ""

    @property
    def approvable(self) -> bool:
        return self.cls == BOUNDARY_APPROVABLE

    @property
    def safe(self) -> bool:
        return self.cls == BOUNDARY_SAFE


def classify_boundary(control: Mapping[str, Any]) -> BoundaryClassification:
    """Which boundary class this control sits on.  Pure; fail-closed.

    TWO INDEPENDENT SIGNALS make a control approvable, and BOTH are needed.

    ``danger`` is the refuse pack's verdict on the control's LABEL — "Bind
    Coverage" carries ``rp.verb.bind``, "Approve Claim" carries
    ``rp.verb.approve``.  That catches the insurance lexicon precisely and
    catches nothing else.

    But the commonest commit button in the world is labelled "Submit
    Application", and the refuse pack does not flag it — deliberately, since
    ``rp.verb.underwrite`` was scoped OFF the page URL after it marked 20 of 35
    controls on ``/underwriting/new-business/new-application`` as critical,
    including the Back button and the notification bell.  Verified against the
    live pack: ``classify_control_danger("Submit Application", "button", …)``
    returns ``danger=False``.

    So label-danger alone would leave the single most important boundary in the
    product classified ``safe``, and the walk would click it unapproved.  The
    COMMIT SHAPE (``_WIZARD_COMMIT_RE`` — the same vocabulary the wizard walk
    already refuses to advance through) is the second signal, and it is the one
    that actually catches "Submit Application" / "Apply Now" / "Place Order".

    Order matters: session teardown is checked FIRST, so "Sign Out" — which is
    danger via ``rp.verb.logout`` AND would otherwise be offered for approval on
    every page of the application — lands in ``never`` and is never shown.
    """
    kind = str(control.get("kind") or "").strip().lower()
    if kind not in _ACTUATOR_KINDS:
        return BoundaryClassification(BOUNDARY_SAFE, REASON_NOT_AN_ACTUATOR)

    name = str(control.get("name") or "").strip()
    if not name:
        # An unnamed actuator cannot be approved (there is nothing to name in
        # the grant) and must not be crossed. Fail closed: NEVER.
        return BoundaryClassification(BOUNDARY_NEVER, REASON_UNNAMED)

    if _AUTH_SESSION_RE.search(name):
        return BoundaryClassification(BOUNDARY_NEVER, REASON_SESSION_TEARDOWN,
                                      rule_id=str(control.get("danger_rule_id") or ""),
                                      severity=str(control.get("danger_severity") or ""))

    if control.get("disabled"):
        # A disabled control is not a boundary anyone can stand at. Recorded as
        # its own reason so a coverage report never implies the funnel ended at
        # a button that was never live.
        return BoundaryClassification(BOUNDARY_SAFE, REASON_DISABLED)

    if control.get("danger"):
        return BoundaryClassification(
            BOUNDARY_APPROVABLE, REASON_DANGER_VERB,
            rule_id=str(control.get("danger_rule_id") or ""),
            severity=str(control.get("danger_severity") or "critical"))

    if _WIZARD_COMMIT_RE.search(name):
        return BoundaryClassification(BOUNDARY_APPROVABLE, REASON_COMMIT_SHAPE,
                                      severity="high")

    return BoundaryClassification(BOUNDARY_SAFE, REASON_SAFE)


def boundary_key(url: str, control_name: str) -> str:
    """The stable LOGICAL identity of one boundary — the exactly-once key.

    NOT the state fingerprint.  The fingerprint is a function of the live DOM
    (url + controls + dialogs + revealed fields), so the same Submit button
    fingerprints differently on a second traversal that answered one question
    differently, revealed a follow-up block, or arrived with a banner on screen.
    Keying exactly-once on the fingerprint therefore authorises a SECOND
    irreversible submit of the same application, which is precisely the failure
    this key exists to make impossible.

    ``(url_template, normalised label)`` is what a human means by "that submit
    button": the page it lives on and what it says.  ``url_template`` already
    collapses ids and uuids (``/applications/7f3a`` → ``/applications/{id}``),
    so a wizard reached twice under different instance ids is one boundary.
    """
    key = "boundary::%s::%s" % (_url_key(url or ""), _norm_label(control_name))
    return "bnd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


# ═══════════════════════════════════════════════════════════════════════════
#  2. THE APPROVAL SEAM  (T-AC-02)
# ═══════════════════════════════════════════════════════════════════════════

class GrantParseError(ValueError):
    """A malformed or over-broad approval grant.  Raised, never swallowed.

    Fail-LOUD rather than fail-closed here on purpose.  A grant that silently
    parses to nothing looks exactly like a crawl that stopped at the boundary
    for a legitimate reason, and the operator would re-issue the same broken
    approval forever without ever being told why.
    """


@dataclass(frozen=True)
class ApprovalGrant:
    """ONE operator authorisation for ONE boundary control.

    ``control`` is matched by EXACT normalised label — never a substring, never
    a pattern, never a wildcard.  ``"*"`` is rejected at parse time: an approval
    that covers everything cannot be reviewed, cannot be revoked selectively,
    and cannot be attributed to a decision anyone actually made.

    ``url`` and ``state_fingerprint`` NARROW the grant further and are both
    optional.  A grant with neither authorises that label wherever the crawl
    meets it — which is still exactly one control per page and still bounded by
    ``max_crossings``; a grant with ``url`` set authorises it on that page only.
    Narrower is always safe; the scope can only ever subtract.
    """

    control: str
    url: str = ""
    state_fingerprint: str = ""
    max_crossings: int = 1
    approval_id: str = ""
    approved_by: str = ""
    approved_at: str = ""

    def __post_init__(self) -> None:
        if not self.approval_id:
            object.__setattr__(self, "approval_id", self._derive_id())

    def _derive_id(self) -> str:
        raw = "grant::%s::%s::%s::%d" % (
            _norm_label(self.control), _url_key(self.url or ""),
            (self.state_fingerprint or "").strip(), int(self.max_crossings))
        return "apr_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def matches(self, *, control_name: str, url: str = "",
                state_fingerprint: str = "") -> bool:
        """Does this grant authorise THIS control, here, now?

        Every clause is an AND and every clause is exact.  There is deliberately
        no fuzzy path: an approval that "probably meant" a different button is
        the one outcome an irreversible action can never recover from.
        """
        if _norm_label(control_name) != _norm_label(self.control):
            return False
        if self.url and _url_key(url or "") != _url_key(self.url):
            return False
        if self.state_fingerprint and (state_fingerprint or "") != self.state_fingerprint:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "control": self.control,
            "url": self.url,
            "state_fingerprint": self.state_fingerprint,
            "max_crossings": int(self.max_crossings),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def parse_grants(raw: Any) -> tuple[ApprovalGrant, ...]:
    """Parse the wire form of ``boundary_approvals`` into grants.

    Accepts a sequence of mappings.  A bare string is accepted too (it means
    "this control, anywhere, once") because the dispatch layer and the operator
    UI both already speak in labels — but ``"*"`` is refused in BOTH forms.

    Every rejection raises :class:`GrantParseError` naming the offending entry.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes, Mapping)):
        raw = [raw]
    grants: list[ApprovalGrant] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            entry = {"control": entry}
        if not isinstance(entry, Mapping):
            raise GrantParseError(
                f"boundary_approvals[{index}] is {type(entry).__name__}, expected "
                f"an object with a 'control' name")
        control = str(entry.get("control") or entry.get("name") or "").strip()
        if not control:
            raise GrantParseError(
                f"boundary_approvals[{index}] has no 'control' — an approval must "
                f"name exactly one boundary control")
        if control == "*" or "*" in control:
            raise GrantParseError(
                f"boundary_approvals[{index}] is {control!r}. A boundary approval "
                f"targets ONE control by name; a wildcard authorises every "
                f"irreversible action in the application and is refused.")
        try:
            max_crossings = int(entry.get("max_crossings", 1))
        except (TypeError, ValueError):
            raise GrantParseError(
                f"boundary_approvals[{index}].max_crossings is not an integer")
        if max_crossings < 1:
            raise GrantParseError(
                f"boundary_approvals[{index}].max_crossings={max_crossings} — an "
                f"approval that permits zero crossings is a refusal; omit it")
        grant = ApprovalGrant(
            control=control,
            url=str(entry.get("url") or "").strip(),
            state_fingerprint=str(entry.get("state_fingerprint") or "").strip(),
            max_crossings=max_crossings,
            approval_id=str(entry.get("approval_id") or "").strip(),
            approved_by=str(entry.get("approved_by") or "").strip(),
            approved_at=str(entry.get("approved_at") or "").strip(),
        )
        if grant.approval_id in seen:
            continue          # an idempotent re-send of the same grant, not an error
        seen.add(grant.approval_id)
        grants.append(grant)
    return tuple(grants)


class ApprovalRegistry:
    """The crawl's set of boundary grants.  Immutable once built; no global state.

    Lookup is FIRST MATCH in the operator's own order, so an approval list is
    read the way it was written and two crawls given the same list resolve the
    same grant — determinism the replay proof depends on.
    """

    __slots__ = ("_grants",)

    def __init__(self, grants: Iterable[ApprovalGrant] = ()) -> None:
        self._grants: tuple[ApprovalGrant, ...] = tuple(grants)

    def __len__(self) -> int:
        return len(self._grants)

    def __bool__(self) -> bool:
        return bool(self._grants)

    @property
    def grants(self) -> tuple[ApprovalGrant, ...]:
        return self._grants

    def grant_for(self, *, control_name: str, url: str = "",
                  state_fingerprint: str = "") -> Optional[ApprovalGrant]:
        """The grant authorising this control here, or ``None``."""
        for grant in self._grants:
            if grant.matches(control_name=control_name, url=url,
                             state_fingerprint=state_fingerprint):
                return grant
        return None

    def approved_labels(self) -> tuple[str, ...]:
        """The distinct control labels this registry authorises — for reporting
        and for the advance-shadowing check, never for matching."""
        out: list[str] = []
        seen: set[str] = set()
        for g in self._grants:
            key = _norm_label(g.control)
            if key and key not in seen:
                seen.add(key)
                out.append(g.control)
        return tuple(out)

    def to_list(self) -> list[dict[str, Any]]:
        return [g.to_dict() for g in self._grants]


# ═══════════════════════════════════════════════════════════════════════════
#  3. EXACTLY-ONCE  (T-AC-05)
# ═══════════════════════════════════════════════════════════════════════════

#: A crossing was reserved and the click is in flight or done. Either way the
#: boundary is spent — see :meth:`CrossingLedger.reserve`.
CROSSING_RESERVED = "reserved"
#: The click happened and the far side was observed.
CROSSING_CROSSED = "crossed"
#: The guard, the ledger or the approval refused. Nothing was clicked.
CROSSING_REFUSED = "refused"


@dataclass
class CrossingRecord:
    """One attempt at one boundary, and what became of it."""

    crossing_id: str
    boundary_key: str
    control_name: str
    url: str
    state_fingerprint: str
    approval_id: str
    sequence_index: int
    status: str = CROSSING_RESERVED
    outcome: str = ""
    confirmed: bool = False
    refusal_reason: str = ""
    reserved_at_ms: int = 0
    completed_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "crossing_id": self.crossing_id,
            "boundary_key": self.boundary_key,
            "control_name": self.control_name,
            "url": self.url,
            "state_fingerprint": self.state_fingerprint,
            "approval_id": self.approval_id,
            "sequence_index": self.sequence_index,
            "status": self.status,
            "outcome": self.outcome,
            "confirmed": self.confirmed,
            "refusal_reason": self.refusal_reason,
            "reserved_at_ms": self.reserved_at_ms,
            "completed_at_ms": self.completed_at_ms,
        }


class CrossingLedger:
    """EXACTLY-ONCE, and the emphasis is on ONCE.

    THE RESERVATION HAPPENS BEFORE THE CLICK.  A ledger that records the
    crossing after the fact has a window — a timeout, a crash, a cancelled
    task — in which the application was mutated and the crawl does not know it.
    On the next traversal it would submit the same application again, and the
    evidence would show one submit where there were two.  So the boundary is
    marked spent BEFORE the irreversible action, and a reservation that never
    completes stays spent.  The cost of that choice is a possible missing
    outcome milestone (visible, recoverable, honest); the cost of the other
    choice is a duplicate irreversible action (invisible, unrecoverable).

    The ledger is keyed on the LOGICAL boundary (:func:`boundary_key`) and
    ALSO on the older ``fingerprint::name`` pair, and a crossing under EITHER
    key blocks a second one.  Two keys, never fewer: dropping the fingerprint
    key would relax a dedup that is already shipped and already relied upon.
    """

    __slots__ = ("_spent", "_records", "_by_boundary")

    def __init__(self) -> None:
        self._spent: set[str] = set()
        self._records: list[CrossingRecord] = []
        self._by_boundary: dict[str, int] = {}

    # -- keys -----------------------------------------------------------------

    @staticmethod
    def _legacy_key(state_fingerprint: str, control_name: str) -> str:
        return "%s::%s" % (state_fingerprint, (control_name or "").strip().lower())

    def keys_for(self, *, control_name: str, url: str,
                 state_fingerprint: str) -> tuple[str, str]:
        return (boundary_key(url, control_name),
                self._legacy_key(state_fingerprint, control_name))

    # -- queries --------------------------------------------------------------

    def crossings(self, *, control_name: str, url: str) -> int:
        """How many times this LOGICAL boundary has been reserved."""
        return self._by_boundary.get(boundary_key(url, control_name), 0)

    def is_spent(self, *, control_name: str, url: str,
                 state_fingerprint: str = "") -> bool:
        """True when this boundary may not be crossed again under either key."""
        return any(k in self._spent for k in self.keys_for(
            control_name=control_name, url=url, state_fingerprint=state_fingerprint))

    def would_exceed(self, *, control_name: str, url: str, state_fingerprint: str,
                     max_crossings: int) -> bool:
        """Would one more crossing exceed the grant's budget?

        ``max_crossings`` defaults to 1 everywhere, so this normally reduces to
        :meth:`is_spent`.  It is expressed as a budget rather than a boolean so
        a deliberate multi-crossing approval (a load-shaped journey an operator
        explicitly authorised N times) is a data change, not a code change.
        """
        bkey, lkey = self.keys_for(control_name=control_name, url=url,
                                   state_fingerprint=state_fingerprint)
        # The legacy key is absolute: this exact control at this exact state has
        # already been operated, and repeating it is a duplicate however
        # generous the budget.
        if lkey in self._spent:
            return True
        # NOT ``is_spent`` here. That asks "has this boundary EVER been
        # crossed", which is True the instant the first reservation lands — so
        # short-circuiting on it made ``max_crossings`` unreachable for every
        # value above 1. The budget has to be compared against the COUNT.
        return self._by_boundary.get(bkey, 0) >= max(1, int(max_crossings))

    # -- mutation -------------------------------------------------------------

    def reserve(self, *, control_name: str, url: str, state_fingerprint: str,
                approval_id: str, sequence_index: int,
                now_ms: int = 0) -> CrossingRecord:
        """Spend the boundary and return its record.  Call BEFORE the click."""
        bkey, lkey = self.keys_for(control_name=control_name, url=url,
                                   state_fingerprint=state_fingerprint)
        self._spent.add(bkey)
        self._spent.add(lkey)
        self._by_boundary[bkey] = self._by_boundary.get(bkey, 0) + 1
        record = CrossingRecord(
            crossing_id="cross_%s_%d" % (bkey[4:16], self._by_boundary[bkey]),
            boundary_key=bkey, control_name=control_name, url=url,
            state_fingerprint=state_fingerprint, approval_id=approval_id,
            sequence_index=int(sequence_index), reserved_at_ms=int(now_ms),
        )
        self._records.append(record)
        return record

    def note_refusal(self, *, control_name: str, url: str, state_fingerprint: str,
                     reason: str, now_ms: int = 0) -> CrossingRecord:
        """Record a refusal WITHOUT spending the boundary.

        A refusal is evidence too — "the crawl reached the commit button and was
        not allowed through" is the single most common honest outcome, and a
        report that shows nothing at all cannot be told apart from a crawl that
        never got there.  It does not spend the boundary: a refusal is not a
        crossing, and re-approving must be able to succeed.
        """
        bkey = boundary_key(url, control_name)
        record = CrossingRecord(
            crossing_id="refuse_%s_%d" % (bkey[4:16], len(self._records) + 1),
            boundary_key=bkey, control_name=control_name, url=url,
            state_fingerprint=state_fingerprint, approval_id="",
            sequence_index=-1, status=CROSSING_REFUSED, refusal_reason=reason,
            reserved_at_ms=int(now_ms),
        )
        self._records.append(record)
        return record

    def complete(self, record: CrossingRecord, *, outcome: str, confirmed: bool,
                 now_ms: int = 0) -> None:
        record.status = CROSSING_CROSSED
        record.outcome = outcome
        record.confirmed = bool(confirmed)
        record.completed_at_ms = int(now_ms)

    # -- reporting ------------------------------------------------------------

    @property
    def records(self) -> tuple[CrossingRecord, ...]:
        return tuple(self._records)

    def to_list(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records]


# ═══════════════════════════════════════════════════════════════════════════
#  4. THE OUTCOME MILESTONE  (T-AC-03 / T-AC-04)
# ═══════════════════════════════════════════════════════════════════════════

#: The URL changed.  The strongest, most machine-checkable landing proof there
#: is, and the only one that survives an application redesign.
RUNG_NAVIGATION = "navigation"
#: A ``role=status`` / ``aria-live=polite`` region the application itself
#: declared as a status message, present AFTER and absent BEFORE.
RUNG_ARIA_STATUS = "aria_status"
#: Visible success-shaped text that appeared as a RESULT of the crossing.
#: Weaker than the two above and labelled as such wherever it is reported.
RUNG_TRANSITION_TEXT = "transition_text"
#: A non-error dialog/modal opened — a receipt or confirmation overlay.
RUNG_DIALOG = "dialog"

#: Strongest first.  A milestone records exactly which rung proved it, so no
#: reader ever has to guess how strong the evidence behind a completion is.
CONFIRMATION_RUNGS = (RUNG_NAVIGATION, RUNG_ARIA_STATUS, RUNG_DIALOG,
                      RUNG_TRANSITION_TEXT)

#: M1.4 — the rungs on which the APPLICATION ITSELF declared success: a status
#: region it published, success-shaped text that APPEARED, or a non-error dialog
#: it opened.
#:
#: ``RUNG_NAVIGATION`` is deliberately absent, and its absence is the whole
#: safety property.  A URL change proves that a click LANDED somewhere; it says
#: nothing about whether the somewhere is a confirmation.  Every "Continue" in
#: every wizard navigates, so a rule that counted navigation as a declaration
#: would report step one of a nine-step funnel as a completed journey — the
#: exact green-wash this ladder exists to prevent.  The submit path may still
#: record ``navigation`` as its milestone rung, because there the click was a
#: COMMIT and the landing is therefore evidence about that commit; a walk's
#: ordinary advance carries no such prior.
DECLARED_CONFIRMATION_RUNGS = frozenset({RUNG_ARIA_STATUS, RUNG_TRANSITION_TEXT,
                                         RUNG_DIALOG})

#: The observed outcomes that can carry a confirmation.  ``error`` is excluded by
#: construction (``classify_submit_after`` checks it BEFORE confirmation, so a
#: rejected action can never arrive here), and ``none``/``dom_changed`` mean the
#: classifier found nothing positive to say.
_POSITIVE_TERMINAL_OUTCOMES = frozenset({"navigation", "confirmation"})


def is_confirmation_landing(*, outcome: str, rung: str, changed: bool) -> bool:
    """Did this transition land on a RECOGNIZED confirmation?  (M1.4 / T-CF-03)

    THE SAME THREE CONJUNCTS AS :attr:`OutcomeMilestone.verified`, over the
    SAME shared outcome set, and differing on exactly one deliberate point —
    the rung ladder (see :data:`DECLARED_CONFIRMATION_RUNGS`):

      * the observed outcome is a positive terminal one — never an error, never
        an honest "nothing happened";
      * a DECLARED rung names how success was established — the application said
        so, we did not infer it from a URL, a button label or a page title;
      * the page actually moved.  A banner that appears while the interactive
        shape and the URL both stay put is a toast ("Saved"), not the end of a
        journey, and a rule without this conjunct would end every funnel at its
        first autosave.

    Generic by construction: no URL, no label, no title and no application
    knowledge participates.  The only vocabulary involved is
    :data:`_SUCCESS_RE`, and it is consulted by :func:`confirmation_transition`
    against text that was ABSENT before the action and PRESENT after it — so a
    page that says "you will receive a confirmation email" before anything has
    happened still cannot confirm itself.
    """
    if str(outcome or "") not in _POSITIVE_TERMINAL_OUTCOMES:
        return False
    if str(rung or "") not in DECLARED_CONFIRMATION_RUNGS:
        return False
    return bool(changed)

#: Conservative, positive success vocabulary.  Deliberately short: every term
#: here is one an application only renders when something WORKED.  "processing",
#: "pending" and "please wait" are absent on purpose — they are the states a
#: half-finished submit sits in, and counting them would green-wash exactly the
#: failure this milestone exists to detect.
_SUCCESS_RE = re.compile(
    r"\b("
    r"success(ful(ly)?)?"
    r"|confirm(ed|ation)"
    r"|thank\s*you"
    r"|(has\s+been|was|were)\s+(submitted|received|created|recorded|completed)"
    r"|application\s+(id|number|reference|received|submitted)"
    r"|(reference|confirmation|policy|claim)\s*(number|no\.?|id)"
    r"|completed\s+successfully"
    r")\b",
    re.IGNORECASE,
)


def confirmation_transition(
    before: Sequence[str], after: Sequence[str], *,
    aria_before: Sequence[str] = (), aria_after: Sequence[str] = (),
    control_names: Sequence[str] = (),
) -> tuple[str, str]:
    """The grounded confirmation this crossing PRODUCED — ``(detail, rung)``.

    THE ANTI-FABRICATION PROPERTY IS THE DIFFERENCE, NOT THE MATCH.

    A page that says "Submit your application — you will receive a confirmation
    email" contains the word "confirmation" before anything has happened.  Any
    classifier that scores the after-state alone reports that page as a
    confirmed submit, and it will do so on an application that errored.  So the
    only thing that counts here is text that was NOT on the page before the
    click and IS on it after: a transition, which is what a confirmation
    actually is.

    ``aria_*`` are the application's own declared status regions
    (``role=status`` / ``aria-live=polite``); they outrank free text because the
    application is telling us, rather than us inferring.  Both are diffed the
    same way.

    ``control_names`` are the accessible names of the landing page's interactive
    controls, and any candidate that IS one is discarded.  A button is an offer,
    not a statement: "Print Confirmation", "Confirm Order" and "Claim Number"
    (a field label) all carry the success vocabulary while declaring nothing at
    all, and a page whose only match is its own button label has said nothing.
    Measured on the M1.2 proving ground, whose confirmation page offers a
    ``Print Confirmation`` button three lines from the banner that is the real
    declaration.  Optional, so a caller that has no inventory to hand behaves
    exactly as before.

    Returns ``("", "")`` when nothing was produced — an honest silence, never a
    manufactured confirmation.
    """
    banned = {_norm_label(str(n or "")) for n in (control_names or ()) if str(n or "")}

    def _fresh(before_seq: Sequence[str], after_seq: Sequence[str]) -> list[str]:
        seen = {" ".join(str(t or "").split()).strip().lower() for t in before_seq}
        out: list[str] = []
        for text in after_seq:
            norm = " ".join(str(text or "").split()).strip()
            if norm and norm.lower() not in seen:
                out.append(norm)
        return out

    for text in _fresh(aria_before, aria_after):
        # A declared status region that appeared is a confirmation on the
        # application's own authority; we do not second-guess its wording.
        return text[:300], RUNG_ARIA_STATUS

    for text in _fresh(before, after):
        if _norm_label(text) in banned:
            continue
        if _SUCCESS_RE.search(text):
            return text[:300], RUNG_TRANSITION_TEXT

    return "", ""


def dom_digest(controls: Sequence[Mapping[str, Any]]) -> str:
    """A VALUE-FREE digest of the page's interactive shape.

    T-AC-03 asks for the DOM before and after.  The crawl may not store a raw
    DOM: it is full of the customer's own data, this evidence travels back to
    qe-central, and the product's standing rule is that identity is UI shape and
    never a user value (see ``field_signature`` and
    ``flow_ledger.activated_signatures``, which make the same trade).

    So the digest is over ``kind:accessible-name`` counts — enough to prove the
    page became a different page, carrying no value that could identify anyone.
    Counts, not a set: revealing a SECOND "Yes/No" pair changes the shape while
    adding no new label, and a set-diff would call that no change at all.
    """
    parts: list[str] = []
    for c in controls or ():
        if not isinstance(c, Mapping):
            continue
        kind = str(c.get("kind") or "").strip().lower()
        name = _norm_label(c.get("name") or "")
        if kind or name:
            parts.append("%s:%s" % (kind, name[:60]))
    parts.sort()
    blob = "|".join(parts)
    return "dom_%s_%d" % (hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20],
                          len(parts))


@dataclass
class OutcomeMilestone:
    """THE CANONICAL JOURNEY OUTCOME — the verified landing on the far side.

    COMPLETION IS NOT THE CLICK.  A submit that fired is not a submit that
    worked; the crawl has recorded nine of those, every one an error, and every
    one indistinguishable from nine completed business transactions in the
    counters.  Completion is the observed transition ONTO the resulting page and
    the verification that the page is a genuine confirmation — which is why
    ``verified`` is derived here from evidence and cannot be passed in.

    Everything on this record is an OBSERVATION or the operator's own signed
    input.  Nothing is inferred, nothing is defaulted to the optimistic value.
    """

    milestone_id: str
    crossing_id: str
    approval_id: str
    boundary_key: str
    control_name: str

    # -- the transition ----------------------------------------------------
    url_before: str = ""
    url_after: str = ""
    navigated: bool = False
    outcome: str = ""
    confirmation_detail: str = ""
    confirmation_rung: str = ""

    # -- the two page shapes ------------------------------------------------
    state_fingerprint_before: str = ""
    state_fingerprint_after: str = ""
    dom_digest_before: str = ""
    dom_digest_after: str = ""
    screenshot_before: str = ""
    screenshot_after: str = ""

    # -- the authority ------------------------------------------------------
    attestation_env_kind: str = ""
    attestation_attributed_to: str = ""
    refuse_pack_version: str = ""
    guard_rule_id: str = ""

    # -- the clock ----------------------------------------------------------
    clicked_at_ms: int = 0
    observed_at_ms: int = 0

    # -- what the far side displayed ---------------------------------------
    outcome_values: list[dict[str, Any]] = field(default_factory=list)

    @property
    def latency_ms(self) -> int:
        return max(0, int(self.observed_at_ms) - int(self.clicked_at_ms))

    @property
    def verified(self) -> bool:
        """Did the crawl OBSERVE a landing on a genuine confirmation?

        Three conjuncts, and the third is the one that used to be missing:
          * the crossing produced a positive terminal outcome, AND
          * a confirmation rung names HOW that was established, AND
          * the page actually changed — a new URL or a different interactive
            shape.  A submit that leaves the page byte-identical has not landed
            anywhere, whatever banner text a classifier thinks it found.
        """
        if self.outcome not in _POSITIVE_TERMINAL_OUTCOMES:
            return False
        if self.confirmation_rung not in CONFIRMATION_RUNGS:
            return False
        if self.navigated:
            return True
        return bool(self.dom_digest_after
                    and self.dom_digest_after != self.dom_digest_before)

    def to_dict(self) -> dict[str, Any]:
        return {
            "milestone_id": self.milestone_id,
            "crossing_id": self.crossing_id,
            "approval_id": self.approval_id,
            "boundary_key": self.boundary_key,
            "control_name": self.control_name,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "navigated": self.navigated,
            "outcome": self.outcome,
            "confirmation_detail": self.confirmation_detail,
            "confirmation_rung": self.confirmation_rung,
            "state_fingerprint_before": self.state_fingerprint_before,
            "state_fingerprint_after": self.state_fingerprint_after,
            "dom_digest_before": self.dom_digest_before,
            "dom_digest_after": self.dom_digest_after,
            "screenshot_before": self.screenshot_before,
            "screenshot_after": self.screenshot_after,
            "attestation_env_kind": self.attestation_env_kind,
            "attestation_attributed_to": self.attestation_attributed_to,
            "refuse_pack_version": self.refuse_pack_version,
            "guard_rule_id": self.guard_rule_id,
            "clicked_at_ms": self.clicked_at_ms,
            "observed_at_ms": self.observed_at_ms,
            "latency_ms": self.latency_ms,
            "outcome_values": list(self.outcome_values or []),
            # DERIVED, and derived HERE so no consumer can set it. The whole
            # point of the milestone is that completion is a measurement.
            "verified": self.verified,
        }


def milestone_id_for(crossing_id: str) -> str:
    return "mst_" + hashlib.sha256(
        ("milestone::" + (crossing_id or "")).encode("utf-8")).hexdigest()[:16]
