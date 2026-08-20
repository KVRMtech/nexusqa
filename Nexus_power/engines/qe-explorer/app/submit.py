"""Phase-B attested submit, and the A4.3 APPROVED BOUNDARY CROSSING.

Extracted VERBATIM from :mod:`app.crawler` (M0.3 / T-DE-11); the crossing
ladder, the exactly-once ledger and the outcome milestone were added here by
A4.3.  The boundary model itself is pure and lives in :mod:`app.boundary`.

DEFAULT-OFF AND DOUBLE-GATED.  A submit fires only when the operator supplied an
approval — a per-control ``boundary_approvals`` grant or the legacy
``submit_approvals`` label list — AND a disposable-environment attestation is
present.  Without both, the crawl stops at the Phase-A submit boundary,
byte-identical to a crawl that never had this code.

THREE AUTHORITIES, ONE LADDER (:meth:`SubmitMixin._authorize_crossing`).  A
per-control grant is the only one that may cross a refuse-pack irreversible
verb; the two legacy seams are unchanged and cannot.  Before A4.3 there was no
grant at all, so the ONLY route across an irreversible control was the ``"*"``
blanket — which authorises every submit the application offers at once.

THE APPROVAL IS NOT A STANDING PERMISSION.  ``execute_submit_phase_b``
re-verifies the guard at the moment of the click, not merely at admission, and
the danger classification is re-checked against the control actually being
operated.  An approval list says WHICH flows may be submitted; it never says a
particular click is safe.

``forms_confirmed`` is tracked separately from ``forms_submitted`` on purpose:
a submit that FIRED is not a submit that WORKED, and only the application's own
positive terminal outcome may increment the former.  A floor on attempts cannot
tell a working funnel from a broken one.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import heapq
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from . import danger_signals
from . import emit
from . import matcher
from . import perception
from . import value_infer
from . import vocab
from .auth import (
    AUTH_NO_CREDENTIALS,
    AUTH_NOT_PERSISTED,
    AUTH_SESSION_EXPIRED,
    Authenticator,
    AuthWindow,
    Credentials,
    looks_like_signup,
    match_identifier_step,
    match_login_controls,
    match_secret_field,
)
from .browser import BrowserPort, PageObservation
from .coverage import CoverageLedger
from .emitter import MetaEmitter
from .filler import ControlFiller, _FIELD_KINDS
from .oracle_gateway import OracleGateway
from .crawl_constants import (  # noqa: F401  (re-exported public vocabulary)
    AdvanceDecision,
    NEXT_ACTION_DESTRUCTIVE,
    NEXT_ACTION_FORWARD,
    NEXT_ACTION_NAVIGATIONAL,
    ORACLE_NONE,
    ORACLE_NOT_CONSULTED,
    ORACLE_PICKED,
    ORACLE_UNAVAILABLE,
    STOP_AUTH_FAILED,
    STOP_AUTH_REQUIRED,
    STOP_CANCELLED,
    STOP_COMPLETED,
    STOP_ERROR,
    TRAVERSAL_FULL,
    TRAVERSAL_OBSERVE,
    TRAVERSAL_POSTURES,
    TRAVERSAL_PROBE,
    _ACTUATOR_KINDS,
    _AUTH_SESSION_RE,
    _BOUNDARY_OUTCOME_TYPES,
    _E2E_WIZARD_ADVANCES,
    _E2E_WIZARD_STEPS,
    _ENTRY_GOTO_RETRIES,
    _ENTRY_RETRY_DELAY_S,
    _FILLABLE_KINDS,
    _FULL_DEP_PROBES,
    _FULL_OPTION_PROBES,
    _FULL_PROBED_OPTIONS,
    _MAX_DEP_PROBES,
    _MAX_GROUND_NAVS,
    _MAX_HOVER_REVEALS,
    _MAX_MENU_ITEMS,
    _MAX_OPTION_PROBES,
    _MAX_PROBED_OPTIONS,
    _MAX_WIZARD_ADVANCES,
    _MAX_WIZARD_STEPS,
    _NEGATIVE_OPTION_HINTS,
    _REACH_MAX_HOPS,
    _WIZARD_ADVANCE_RE,
    _WIZARD_COMMIT_RE,
    _attestation_dict,
    _candidate_sig,
    _decision_points,
    _host_of,
    _is_wizard_advance,
    _links_to_site_root,
    _next_action_decisions,
    _norm_label,
    _reach_ancestors,
    _reach_href_key,
    _reach_label_match,
    _reach_pick,
    _reach_target_labels,
    _segment_label,
    _url_key)
from .boundary import (AUTHORITY_BLANKET, AUTHORITY_GRANT, AUTHORITY_NAMED,
                       BOUNDARY_APPROVABLE, BOUNDARY_NEVER, REASON_DANGER_VERB,
                       ApprovalGrant, CrossingRecord, OutcomeMilestone,
                       classify_boundary, milestone_id_for)
from .guard_context import GuardContext
from .budget import (STOP_MAX_REQUESTS, STOP_MAX_STATES, STOP_MAX_WALL_MS,
                     Budget, BudgetTracker)
from .frontier import (Frontier, FrontierItem, _parse_plan_patterns,
                       _section_signature)
from .state_identity import (_MAX_COVERAGE_STATES, _MAX_DANGER_NAMES,
                             _MAX_NETWORK_CALLS, _MAX_STATE_FIELDS,
                             StateFingerprinter, StateRecorder,
                             _action_to_dict, _displayed_values,
                             _form_snapshot, _is_password, _network_calls)
from . import flow_ledger
from .identity_pack import derive as derive_identity
from .forms import (AnswerKey, PROV_UNBLOCK, execute_submit_phase_b,
                    fill_form_phase_a)
from .guard import (
    EVENT_BLOCKED_METHOD,
    MUTATING_METHODS,
    GuardDecision,
    Phase,
    classify_request,
    registrable_domain,
    same_registrable_domain,
)
from .inventory import build_inventory, form_signal_for

logger = logging.getLogger("app.crawler")

#: Why a crossing was refused when its write-ahead journal record could not be
#: made durable (M3.4 / T-RS-01).  Distinct from every authorisation refusal:
#: the operator DID approve this boundary, and the crawl declined anyway because
#: it could not guarantee it would only cross once.
REFUSAL_JOURNAL_UNAVAILABLE = "crossing_journal_unavailable"


class SubmitMixin:
    """Mixed into :class:`app.crawler.Crawler` (T-DE-11)."""

    def _journal_crossing(self, record: CrossingRecord, *, required: bool = True) -> bool:
        """Append ``record`` to the durable crossing journal (M3.4 / T-RS-01).

        Returns True when the record is durable (or when journalling is not
        available at all - see below).  ``required=False`` is the post-landing
        update, where a failure is survivable because the write-ahead record has
        already spent the boundary.

        THE EMITTER-WITHOUT-A-JOURNAL CASE.  Unit tests drive this mixin with
        stub emitters that predate ``emit_crossing``.  Those stubs write no
        manifest at all, so there is no resume to protect and refusing every
        crossing would fail a large body of tests over a risk that cannot exist
        on that path.  A real :class:`app.emit.ManifestEmitter` always has the
        method, so the production path is always journalled; the absence is
        logged rather than passed over in silence.
        """
        emitter = getattr(self, "_emitter", None)
        emit_crossing = getattr(emitter, "emit_crossing", None)
        if emit_crossing is None:
            if required:
                logger.warning(
                    "qec.boundary.journal_absent emitter=%s - this emitter cannot "
                    "journal crossings; exactly-once holds only in-process.",
                    type(emitter).__name__)
            return True
        try:
            emit_crossing(record.to_dict())
            return True
        except Exception:
            logger.exception("qec.boundary.journal_write_failed crossing_id=%s",
                             record.crossing_id)
            return False

    async def _maybe_submit_phase_b(
        self, item: FrontierItem, controls: Sequence[dict[str, Any]],
        fill: Any, fingerprint: str,
    ) -> None:
        """Phase-5 attested submit: drive the FIRST authorised flow candidate on this
        form and push the post-submit page onto the frontier so the deeper flow gets
        crawled.

        Triple-gated so a real app mutation only ever happens under an explicit
        authorisation: (1) :meth:`_may_attempt_crossing` runs the same ladder the
        executor enforces, so the filter and the decision can never disagree;
        (2) :meth:`_execute_approved_submit` re-authorises, checks exactly-once,
        and reserves the boundary before clicking; (3)
        :func:`execute_submit_phase_b` re-runs ``gate_submit`` at click time and
        REFUSES — recording a guard_event, clicking nothing — if any check fails.

        An irreversible candidate is no longer skipped unconditionally. It used
        to be (``if fc.danger and not self._submit_approve_all: continue``),
        which is what made a named per-control approval impossible and left the
        ``"*"`` blanket as the only way through.

        One submit per state (no combinatorial fan-out); a non-navigating or
        unconfirmed submit is recorded honestly and adds no frontier."""
        for fc in getattr(fill, "flow_candidates", ()):
            name = (getattr(fc, "name", "") or "").strip()
            if not name:
                continue
            # ONE LADDER, ONE ANSWER. This used to re-derive authorisation with
            # its own pair of conditions (`_submit_approved` plus `danger and not
            # approve_all`), and the danger clause is precisely what made a named
            # per-control approval impossible: an irreversible flow candidate was
            # skipped here no matter what the operator had approved, so the "*"
            # blanket was the only route through. The filter now asks the same
            # ladder the executor enforces, so the two can never disagree.
            probe = getattr(fc, "control", None)
            probe = dict(probe) if isinstance(probe, dict) else {}
            probe.setdefault("kind", "button")
            probe["name"] = name
            probe["danger"] = bool(getattr(fc, "danger", False))
            probe["danger_rule_id"] = str(getattr(fc, "danger_rule_id", "") or "")
            probe["danger_severity"] = str(getattr(fc, "danger_severity", "") or "")
            if not self._may_attempt_crossing(
                    name=name, control=probe, url=item.url, fingerprint=fingerprint):
                continue
            # The candidate carries the exact submit control it was recorded from; fall
            # back to a name match in the snapshot if it is somehow absent.
            control = getattr(fc, "control", None)
            if not isinstance(control, dict) or not control:
                control = next(
                    (c for c in controls
                     if str(c.get("name") or "").strip().lower() == name.lower()
                     and c.get("kind") in ("button", "submit")),
                    None,
                )
            if not control:
                continue
            if await self._execute_approved_submit(
                    name=name, control=control, url=item.url, fingerprint=fingerprint,
                    depth=item.depth, fill_controls=controls, renavigate=True):
                return  # one submit per state — avoid combinatorial explosion

    # -- A4.3 AUTHORISATION --------------------------------------------------

    def _authorize_crossing(
        self, *, name: str, control: Mapping[str, Any], url: str,
        fingerprint: str,
    ) -> tuple[Optional[ApprovalGrant], str, str]:
        """May this control be crossed, on whose authority - ``(grant, authority, refusal)``.

        THE LADDER, STRONGEST RUNG FIRST.  Either ``authority`` is set and
        ``refusal`` is empty, or ``refusal`` names why not.  Pure and
        synchronous: the decision is a function of the operator's approvals and
        the control in front of us, so it replays identically and can be
        asserted on directly.

          1. A per-control :class:`ApprovalGrant`.  Least privilege, works on any
             env kind, and the ONLY route that can cross a refuse-pack
             irreversible verb.  This rung did not exist before A4.3, which is
             the whole defect: crossing "Bind Coverage" required ``"*"``.
          2. A bare label in ``submit_approvals`` - the shipped behaviour, kept
             verbatim, and deliberately NOT extended to irreversible verbs.  A
             flat list of labels cannot express "this control, on this page,
             once", so it must not be what authorises a point of no return.
          3. The ``"*"`` disposable blanket - also shipped, also unchanged.  It
             still requires a signed disposable attestation and still refuses
             step-advance labels.

        ``BOUNDARY_NEVER`` short-circuits everything above it.  No approval of
        any strength crosses a sign-out: the click would end the session the
        remaining journey is observed through, so it can only ever trade real
        coverage for one meaningless data point.
        """
        klass = classify_boundary(control)
        if klass.cls == BOUNDARY_NEVER:
            return None, "", "boundary_never:%s" % klass.reason

        grant = self._boundary_grants.grant_for(
            control_name=name, url=url, state_fingerprint=fingerprint)
        if grant is not None:
            attestation = self._guard.attestation
            if attestation is None:
                # Say this plainly rather than letting the guard refuse a POST
                # the operator believes they authorised: the grant is valid, the
                # ENVIRONMENT is not attested, and those need different remedies.
                return None, "", "grant_without_attestation"
            if not attestation.is_submit_capable():
                # CHECKED HERE RATHER THAN ONLY AT THE GUARD, and the reason is
                # the ledger. ``gate_submit`` would refuse this too — it requires
                # a disposable, attributed, unexpired attestation — but only
                # AFTER the boundary has been reserved, so a grant issued against
                # a staging or lapsed attestation would spend the boundary
                # without ever crossing it and the operator would be told
                # nothing they could act on. Refusing before the reservation
                # leaves the boundary intact and names the actual problem.
                return None, "", "attestation_not_submit_capable"
            return grant, AUTHORITY_GRANT, ""

        if not self._submit_enabled:
            return None, "", "submit_not_enabled"

        if klass.cls == BOUNDARY_APPROVABLE and klass.reason == REASON_DANGER_VERB:
            if self._submit_approve_all:
                return None, AUTHORITY_BLANKET, ""
            # NAMED-BUT-DANGEROUS.  The operator listed this label in
            # submit_approvals and the control carries an irreversible verb.
            # Refused - and refused LOUDLY at the call site, because silently
            # treating it as unapproved is how an operator concludes the feature
            # is broken rather than that their approval was the wrong shape.
            return None, "", "danger_requires_boundary_grant"

        if self._submit_approved(name):
            named = name.strip().lower() in self._submit_approvals
            return None, (AUTHORITY_NAMED if named else AUTHORITY_BLANKET), ""
        return None, "", "not_approved"

    def _may_attempt_crossing(self, *, name: str, control: Mapping[str, Any],
                              url: str, fingerprint: str) -> bool:
        """Cheap predicate for the discovery loops - is this worth attempting?

        Authoritative refusal still happens inside
        :meth:`_execute_approved_submit`; this only keeps the loops from walking
        every button on a page through the full path just to be told no.
        """
        _grant, authority, refusal = self._authorize_crossing(
            name=name, control=control, url=url, fingerprint=fingerprint)
        return not refusal and bool(authority)

    async def _execute_approved_submit(
        self, *, name: str, control: dict[str, Any], url: str, fingerprint: str,
        depth: int, fill_controls: Sequence[dict[str, Any]] = (),
        renavigate: bool = True,
    ) -> bool:
        """CROSS ONE APPROVED BOUNDARY, EXACTLY ONCE, AND RECORD THE LANDING.

        The single authoritative crossing path - the form path, the next-action
        path and the walk's danger-forward tier all arrive here, so there is one
        place where authorisation, exactly-once, the guard and the outcome
        milestone are decided, and no second place where any of them can drift.

        Order is load-bearing:

          1. AUTHORISE   (:meth:`_authorize_crossing`) - a refusal is recorded as
                          evidence and clicks nothing.
          2. EXACTLY-ONCE reserve the boundary BEFORE the click.  A crossing that
                          dies mid-flight leaves the boundary spent, because a
                          duplicate irreversible action is unrecoverable and a
                          missing milestone is not.
          3. GUARD        ``execute_submit_phase_b`` re-runs ``gate_submit`` at
                          click time.  The approval says WHICH control; the guard
                          still says whether THIS click may proceed.
          4. MILESTONE    the verified landing, derived from what was OBSERVED -
                          never from ``submitted``.

        Returns False when nothing was clicked (refused, or already crossed).

        ``renavigate=False`` submits IN PLACE - a wizard terminal whose state the
        walk built up would lose it on a re-navigation, and with it the very
        button we mean to click.
        """
        grant, authority, refusal = self._authorize_crossing(
            name=name, control=control, url=url, fingerprint=fingerprint)
        if refusal:
            self._crossings.note_refusal(
                control_name=name, url=url, state_fingerprint=fingerprint,
                reason=refusal, now_ms=self._clock.now_ms())
            logger.warning(
                "qec.boundary.refused control=%r url=%s reason=%s - the crawl "
                "reached an irreversible boundary and was NOT authorised to "
                "cross it. Issue a boundary_approvals grant naming this control "
                "to complete the journey.",
                name[:60], (url or "")[:120], refusal)
            return False

        max_crossings = grant.max_crossings if grant is not None else 1
        # EXACTLY-ONCE, UNDER BOTH KEYS.  ``_submitted_flows`` is the shipped
        # fingerprint-scoped dedup and stays; the ledger adds the LOGICAL key
        # (page + label), which is the one that survives a second traversal
        # arriving with a different DOM and therefore a different fingerprint.
        flow_key = "%s::%s" % (fingerprint, name.lower())
        if flow_key in self._submitted_flows or self._crossings.would_exceed(
                control_name=name, url=url, state_fingerprint=fingerprint,
                max_crossings=max_crossings):
            logger.info(
                "qec.boundary.already_crossed control=%r url=%s - this boundary "
                "has been crossed under this approval; NOT submitting again.",
                name[:60], (url or "")[:120])
            return False
        self._submitted_flows.add(flow_key)

        seq = self._next_seq
        self._next_seq += 1
        record = self._crossings.reserve(
            control_name=name, url=url, state_fingerprint=fingerprint,
            approval_id=(grant.approval_id if grant is not None else authority),
            sequence_index=seq, now_ms=self._clock.now_ms())
        # -- WRITE AHEAD (M3.4 / T-RS-01) ------------------------------------
        # The reservation is made DURABLE before the click, not after it. An
        # in-RAM reservation makes exactly-once true only for as long as the
        # process lives, and the failure it guards against - a kill mid-crossing
        # - is precisely the event that ends the process. Journaling here is
        # what lets a resumed crawl know this boundary is spent.
        #
        # FAIL-CLOSED, uniquely on this path: if the reservation cannot be
        # written, the click does not happen. Everywhere else a failed emit
        # costs evidence; here proceeding would risk a SECOND irreversible
        # action against the customer's application on the next resume, and no
        # amount of captured evidence is worth that.
        if not self._journal_crossing(record):
            self._crossings.note_refusal(
                control_name=name, url=url, state_fingerprint=fingerprint,
                reason=REFUSAL_JOURNAL_UNAVAILABLE, now_ms=self._clock.now_ms())
            logger.error(
                "qec.boundary.journal_failed control=%r url=%s - the crossing "
                "reservation could not be made durable; REFUSING to click. A "
                "crossing that is not journalled would be crossed again by any "
                "resume of this crawl id.", name[:60], (url or "")[:120])
            return False
        logger.warning(
            "qec.boundary.crossing crossing_id=%s control=%r url=%s authority=%s "
            "approval_id=%s attestation=%s - ONE irreversible click, explicitly "
            "authorised, about to fire.",
            record.crossing_id, name[:60], (url or "")[:120], authority,
            record.approval_id, getattr(self._guard.attestation, "env_kind", "none"))

        prev_phase = self._guard.phase
        prev_approved = self._guard.submit_flow_approved
        # Flip the shared guard to SUBMIT so the network route handler authorises the
        # approved submit POST; restore EXPLORE no matter what (fail-closed default).
        # Open a FRESH bounded window per submit so the burst budget can't accrue.
        self._guard.phase = Phase.SUBMIT
        self._guard.submit_flow_approved = True
        self._guard.submit_window.open(self._clock.now_ms())
        try:
            result = await execute_submit_phase_b(
                self._port, control, url, self._emitter, self._clock,
                refuse_pack=self._refuse_pack, is_login_domain=False,
                attestation=self._guard.attestation, submit_flow_approved=True,
                now_ms=self._clock.now_ms(), state_id=fingerprint,
                sequence_index=seq, answer_key=self._answer_key,
                fill_controls=fill_controls, renavigate=renavigate,
                # The renavigation must KEEP THE LOGIN. Phase-B's own goto is raw -
                # on an app that drops its login per page load it lands on the
                # SIGN-IN WALL, so the submit fired into a login form (recorded
                # live: nine submits, outcome=error, five sign-in "pages" stitched
                # into the journey where business pages belonged). The injected
                # navigator re-signs-in and CLICKS back to the requested page; the
                # re-fill then acts on the real form. Cookie apps see the same goto
                # they always did.
                navigate=self._goto_keeping_login,
            )
        finally:
            self._guard.phase = prev_phase
            self._guard.submit_flow_approved = prev_approved
        if result.submitted:
            self._forms_submitted += 1
            self._tracker.note_action()
        # A SUBMIT THAT FIRED IS NOT A SUBMIT THAT WORKED. `submitted` is set
        # whatever the outcome; `confirmed` is the separate fact that the
        # application answered with a navigation or a success confirmation. The
        # crawl computed that distinction and then dropped it, so nine submits
        # that all errored scored exactly the same as nine completed business
        # transactions - in the counter, in the gate floor, and in the weekly
        # yield. This is the one boundary where the product claims something
        # HAPPENED, so it is the last place a count may be generous.
        if getattr(result, "confirmed", False):
            self._forms_confirmed += 1
        self._crossings.complete(
            record, outcome=str(getattr(result, "outcome", "") or ""),
            confirmed=bool(getattr(result, "confirmed", False)),
            now_ms=self._clock.now_ms())
        # The SECOND journal write: same crossing, now carrying its outcome.
        # Best-effort, and deliberately so - the boundary is already durably
        # spent by the write-ahead record above, so losing this one costs a
        # resumed run some outcome detail and can never cost a duplicate click.
        self._journal_crossing(record, required=False)
        milestone = self._record_outcome_milestone(record, grant, result)
        ps = result.page_state
        dest = (getattr(ps, "location", "") or "").strip() if ps else ""
        # Honour max_depth for submit-derived states too (mirrors _discover's depth
        # gate) so an attested submit chain cannot crawl past the budget.
        if (result.confirmed and result.outcome == "navigation" and dest
                and depth < self._budget.max_depth and self._in_scope(dest)):
            self._frontier.push(
                FrontierItem(url=dest, depth=depth + 1,
                             discovered_via="submit:%s" % name,
                             parent_fingerprint=fingerprint),
                key=_url_key(dest),
            )
        if milestone is not None:
            logger.warning(
                "qec.boundary.outcome_milestone milestone_id=%s crossing_id=%s "
                "outcome=%s rung=%s verified=%s url_after=%s - %s",
                milestone.milestone_id, record.crossing_id, milestone.outcome,
                milestone.confirmation_rung or "(none)", milestone.verified,
                (milestone.url_after or "")[:120],
                "JOURNEY COMPLETED" if milestone.verified else
                "crossed but NOT verified: the far side was not a confirmation")
        return True

    def _record_outcome_milestone(
        self, record: CrossingRecord, grant: Optional[ApprovalGrant], result: Any,
    ) -> Optional[OutcomeMilestone]:
        """Mint the outcome milestone from what the crossing OBSERVED (T-AC-04).

        Completion is the landing, not the click, so every field here comes off
        the evidence bundle ``execute_submit_phase_b`` captured adjacent to the
        click.  ``verified`` is computed by :class:`OutcomeMilestone` itself and
        cannot be supplied - if it were an argument, some caller would eventually
        pass ``True``.
        """
        evidence = dict(getattr(result, "crossing", None) or {})
        if not evidence and not getattr(result, "submitted", False):
            return None
        attestation = self._guard.attestation
        milestone = OutcomeMilestone(
            milestone_id=milestone_id_for(record.crossing_id),
            crossing_id=record.crossing_id,
            approval_id=record.approval_id,
            boundary_key=record.boundary_key,
            control_name=record.control_name,
            url_before=str(evidence.get("url_before") or record.url),
            url_after=str(evidence.get("url_after") or ""),
            navigated=bool(evidence.get("navigated")),
            outcome=str(evidence.get("outcome") or getattr(result, "outcome", "") or ""),
            confirmation_detail=str(evidence.get("confirmation_detail") or ""),
            confirmation_rung=str(evidence.get("confirmation_rung") or ""),
            state_fingerprint_before=record.state_fingerprint,
            state_fingerprint_after=str(
                getattr(getattr(result, "page_state", None), "state_id", "") or ""),
            dom_digest_before=str(evidence.get("dom_digest_before") or ""),
            dom_digest_after=str(evidence.get("dom_digest_after") or ""),
            screenshot_before=str(evidence.get("screenshot_before") or ""),
            screenshot_after=str(evidence.get("screenshot_after") or ""),
            attestation_env_kind=str(getattr(attestation, "env_kind", "") or ""),
            attestation_attributed_to=str(getattr(attestation, "attested_by", "") or ""),
            refuse_pack_version=str(getattr(self._refuse_pack, "version", "") or ""),
            guard_rule_id=str(evidence.get("guard_rule_id") or ""),
            clicked_at_ms=int(evidence.get("clicked_at_ms") or 0),
            observed_at_ms=int(evidence.get("observed_at_ms") or 0),
            outcome_values=list(evidence.get("outcome_values") or []),
        )
        row = milestone.to_dict()
        if grant is not None:
            row["grant"] = grant.to_dict()
        self._outcome_milestones.append(row)
        self._emitter.emit_outcome_milestone(row)
        return milestone

    async def _maybe_submit_next_action(
        self, *, controls: Sequence[dict[str, Any]], url: str, fingerprint: str,
        depth: int,
    ) -> None:
        """Cross an approved FORWARD next-action control (Apply Now) on a page with
        no fillable form — a quote summary is exactly this shape.

        The form-submit path only ever sees controls a FILL produced, so a formless
        decision page never crossed its own boundary and the crawl stopped at
        'Apply Now'. This carries it PAST — click Apply Now, land on /portal/apply,
        and the pushed frontier page is then crawled/walked as the continuation
        (the application → e-sign funnel). Same triple gate as any submit: a
        disposable attestation + approval + the refuse pack, re-checked inside
        execute_submit_phase_b — a non-disposable env stays at the boundary exactly
        as before (self._submit_enabled is False without approvals + attestation)."""
        if not self._submit_enabled:
            return
        for c in controls:
            if c.get("kind") not in ("button", "link") or c.get("disabled"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or _AUTH_SESSION_RE.search(name):
                continue
            if not _WIZARD_COMMIT_RE.search(name):
                continue  # only a FORWARD commit action crosses a boundary here
            # Same single ladder as the form path — see _may_attempt_crossing.
            # The `danger and not approve_all` line this replaces is the second
            # of the three sites that made an irreversible control crossable
            # ONLY under "*"; a grant naming this control now reaches the
            # executor, and nothing else has been loosened.
            if not self._may_attempt_crossing(
                    name=name, control=c, url=url, fingerprint=fingerprint):
                continue
            if await self._execute_approved_submit(
                    name=name, control=dict(c), url=url, fingerprint=fingerprint,
                    depth=depth, renavigate=False):
                return  # one crossing per state

    def _pick_submit_candidate(self, controls: Sequence[dict[str, Any]]) -> bool:
        """True when this step ends at a control the walk may not cross.

        That is the difference between a journey that FINISHED at its natural
        boundary and one that merely ran out of anything to click: reaching the
        Submit / Apply button means the funnel was walked to its end, and the only
        thing left is the approval a human owes. A step with no button at all is
        also an end, but a different one, and a report that conflates them cannot
        tell a covered journey from a dead one."""
        for c in controls or ():
            if str(c.get("kind") or "").strip().lower() != "button":
                continue
            name = str(c.get("name") or "")
            if not name:
                continue
            # The commit vocabulary IS the boundary: these are exactly the labels the
            # wizard walk refuses to advance through.
            if _WIZARD_COMMIT_RE.search(name) or c.get("danger"):
                return True
        return False

    def _submit_approved(self, name: str) -> bool:
        """Is this submit control approved to be pressed?

        Either named explicitly by the operator, or covered by the DISPOSABLE-env
        blanket ("*").

        The blanket deliberately still refuses a step-ADVANCE label. `Continue` is a
        submit candidate on a wizard step AND the control that walks the funnel, and
        an approved name is owned by the Phase-B path — so approving it stops the
        walk dead and the catalogue records one-step journeys. That is the exact
        outcome `_reject_advance_shadowing_approvals` refuses to let an operator
        configure by hand, and a blanket must not reintroduce it by the back door.
        """
        n = name.strip().lower()
        if not n:
            return False
        if n in self._submit_approvals:
            return True
        return self._submit_approve_all and not _WIZARD_ADVANCE_RE.search(n)
