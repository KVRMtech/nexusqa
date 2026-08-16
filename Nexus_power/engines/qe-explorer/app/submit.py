"""Phase-B attested submit (M0.3 / T-DE-11).

Extracted VERBATIM from :mod:`app.crawler`.

DEFAULT-OFF AND DOUBLE-GATED.  A submit fires only when the operator supplied a
per-flow approval list AND a disposable-environment attestation is present.
Without both, the crawl stops at the Phase-A submit boundary, byte-identical to
a crawl that never had this code.

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
from .guard_context import GuardContext
from .budget import (STOP_MAX_REQUESTS, STOP_MAX_STATES, STOP_MAX_WALL_MS,
                     Budget, BudgetTracker)
from .frontier import (Frontier, FrontierItem, _parse_plan_patterns,
                       _section_signature)
from .fingerprint import state_fingerprint
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


class SubmitMixin:
    """Mixed into :class:`app.crawler.Crawler` (T-DE-11)."""

    async def _maybe_submit_phase_b(
        self, item: FrontierItem, controls: Sequence[dict[str, Any]],
        fill: Any, fingerprint: str,
    ) -> None:
        """Phase-5 attested submit: drive the FIRST operator-approved, non-danger flow
        candidate on this form and push the post-submit page onto the frontier so the
        deeper flow gets crawled.

        Triple-gated so a real app mutation only ever happens on an attested disposable
        env for an explicitly approved, non-irreversible flow: (1) this method runs only
        when ``self._submit_enabled`` (approvals + attestation supplied); (2) the flow
        name must be in the operator's ``submit_approvals`` and the candidate non-danger;
        (3) :func:`execute_submit_phase_b` re-runs ``gate_submit`` (attestation + per-flow
        approval + non-irreversible-verb) and REFUSES — recording a guard_event, clicking
        nothing — if any check fails. One submit per state (no combinatorial fan-out); a
        non-navigating or unconfirmed submit is recorded honestly and adds no frontier."""
        for fc in getattr(fill, "flow_candidates", ()):
            name = (getattr(fc, "name", "") or "").strip()
            # On a DISPOSABLE env the blanket covers danger controls too: the guard
            # behind this now allows an irreversible verb there, so skipping them
            # here would keep the old refusal alive one layer up and the operator
            # would see no submit at all with no reason given.
            if not name or not self._submit_approved(name):
                continue
            if getattr(fc, "danger", False) and not self._submit_approve_all:
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

    async def _execute_approved_submit(
        self, *, name: str, control: dict[str, Any], url: str, fingerprint: str,
        depth: int, fill_controls: Sequence[dict[str, Any]] = (),
        renavigate: bool = True,
    ) -> bool:
        """Click ONE approved submit control through the SUBMIT guard, record it, and
        push the post-submit page onto the frontier so the flow BEYOND it is crawled.

        Returns False when this control was already submitted at this state (dedup),
        else True. Shared by the form path (:meth:`_maybe_submit_phase_b`) and the
        next-action path (:meth:`_maybe_submit_next_action`).

        ``renavigate=False`` submits IN PLACE — a wizard TERMINAL (a quote summary
        whose state the walk built up) would lose that state on a re-navigation.
        Either way :func:`execute_submit_phase_b` re-runs ``gate_submit`` (attestation
        + approval + non-irreversible unless the disposable blanket allows it), so the
        guard is never bypassed."""
        flow_key = f"{fingerprint}::{name.lower()}"
        if flow_key in self._submitted_flows:
            return False
        self._submitted_flows.add(flow_key)
        seq = self._next_seq
        self._next_seq += 1
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
                # The renavigation must KEEP THE LOGIN. Phase-B's own goto is raw —
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
        # transactions — in the counter, in the gate floor, and in the weekly
        # yield. This is the one boundary where the product claims something
        # HAPPENED, so it is the last place a count may be generous.
        if getattr(result, "confirmed", False):
            self._forms_confirmed += 1
        ps = result.page_state
        dest = (getattr(ps, "location", "") or "").strip() if ps else ""
        # Honour max_depth for submit-derived states too (mirrors _discover's depth
        # gate) so an attested submit chain cannot crawl past the budget.
        if (result.confirmed and result.outcome == "navigation" and dest
                and depth < self._budget.max_depth and self._in_scope(dest)):
            self._frontier.push(
                FrontierItem(url=dest, depth=depth + 1,
                             discovered_via=f"submit:{name}",
                             parent_fingerprint=fingerprint),
                key=_url_key(dest),
            )
        return True

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
            if c.get("danger") and not self._submit_approve_all:
                continue
            if not self._submit_approved(name):
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
