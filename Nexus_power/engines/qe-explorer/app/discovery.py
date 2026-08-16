"""Discovery: the explore loop, expansion, and navigation grounding (T-DE-12).

Extracted VERBATIM from :mod:`app.crawler`.

TRAVERSAL ORDER IS BEHAVIOUR.  This module decides what the crawl visits and in
what sequence, so every ordering decision in it is observable in the manifest of
every crawl.  Nothing here was reordered, simplified or de-duplicated.

GROUNDED NAVIGATION, NOT URL GUESSING.  ``_ground_nav_links`` and
``_menu_reveal`` exist because a route that is only reachable by CLICKING —
behind a hover menu, or rendered by a client-side router that never exposes an
href — cannot be reached by synthesising a URL.  The crawl proves it can get
there the way a user does, and records the click that did it, which is what
turns a visited page into evidence rather than an assertion.

Both are bounded (``_MAX_GROUND_NAVS``, ``_MAX_MENU_ITEMS``,
``_MAX_HOVER_REVEALS``) and both dedupe across states, so a nav bar repeated on
every page costs O(unique routes) rather than O(states x links).
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


class DiscoveryMixin:
    """Mixed into :class:`app.crawler.Crawler` (T-DE-12)."""

    async def _explore_loop(self) -> None:
        while True:
            if self._cancelled:
                self._stop_reason = STOP_CANCELLED
                return
            if self._hard_stop:
                return  # an honest terminal set mid-expand (e.g. a no-credentials login wall)
            reason = self._tracker.stop_reason()
            if reason:
                self._stop_reason = reason
                return
            item = self._frontier.pop()
            if item is None:
                return  # frontier exhausted → completed
            # Depth reached is recorded on DEQUEUE, not on enqueue: a URL pushed
            # at depth 5 that the budget never let us visit was never reached,
            # and counting it would report coverage the crawl does not have.
            if item.depth > self._max_depth_reached:
                self._max_depth_reached = item.depth
            try:
                await self._expand(item)
            except Exception:
                logger.exception("qec.crawler.expand_failed url_scope=%s depth=%d",
                                 _host_of(item.url), item.depth)

    async def _goto_entry(self, url: str) -> Any:
        """The crawl's ENTRY navigation, with a small bounded retry.

        The per-dispatch egress fence (squid allowlist) is rewritten just before
        the crawl starts and re-read asynchronously — the very first goto can race
        that reconfigure and be refused (live-observed: ERR_TUNNEL_CONNECTION_FAILED
        killing a whole crawl as auth_failed/0-states).  Retry the ENTRY goto only,
        a bounded number of times; a still-failing entry stays an HONEST failure."""
        nav = await self._port.goto(url)
        self._tracker.note_request()
        for attempt in range(_ENTRY_GOTO_RETRIES):
            if nav.ok or self._cancelled:
                return nav
            logger.info("qec.crawler.entry_goto_retry attempt=%d error=%s",
                        attempt + 1, (nav.error or "")[:120])
            await self._sleep(_ENTRY_RETRY_DELAY_S)
            nav = await self._port.goto(url)
            self._tracker.note_request()
        return nav

    async def _expand(self, item: FrontierItem) -> None:
        await self._politeness_delay()
        if item.depth == 0 and not item.parent_fingerprint:
            # the ROOT entry: retry the fence-reconfigure race (see _goto_entry).
            nav = await self._goto_entry(item.url)
        else:
            nav = await self._port.goto(item.url)
            self._tracker.note_request()
        if not nav.ok:
            self._emitter.emit_edge(from_state=item.parent_fingerprint, to_state="",
                                    verb="navigate",
                                    target_label=item.discovered_via)
            logger.info("qec.crawler.unreachable depth=%d error=%s",
                        item.depth, nav.error[:120])
            return

        # Materialize lazy / virtual-scroll content before inventorying this state so
        # windowed data grids + below-the-fold controls are captured, not only the
        # initial viewport. Read-only + best-effort; a port without it is a no-op.
        materialize = getattr(self._port, "materialize", None)
        if materialize is not None:
            await materialize()

        obs = await self._observe()
        # SCOPE GATE: a goto can REDIRECT off-domain (an SSO re-redirect to an IdP, an
        # expired session, an external link). Frontier pushes are already scope-gated, but a
        # redirect lands us elsewhere — we must NOT inventory/record an off-domain page as
        # the app's own substrate (that would attribute Okta/Google content to the app).
        if not self._in_scope(obs.url):
            self._emitter.emit_edge(from_state=item.parent_fingerprint, to_state="",
                                    verb="navigate", target_label=item.discovered_via)
            logger.info("qec.crawler.out_of_scope depth=%d url=%s",
                        item.depth, (obs.url or "")[:120])
            return
        controls = build_inventory(obs.raw_controls, self._refuse_pack, url=obs.url)
        # An auth wall is a STEP IN THE JOURNEY, not the end of it. Real business
        # journeys cross one all the time (public quote → authenticated apply →
        # e-sign); stopping here would catalogue two fragments and never the flow
        # the business actually sells.
        obs, controls = await self._cross_auth_wall(
            obs, controls, item.url, item.discovered_via)
        # Requested vs landed is what separates "the app sent us to sign in" from
        # "the crawl followed a link to the sign-in page".
        self._note_login_wall_while_authenticated(controls, item.url, obs.url)
        # Honest login-wall handling for a NO-credentials crawl — single-screen AND
        # multi-step / username-first (email → Next → password). Stop at the SECRET we
        # cannot pass, BEFORE filling it and looping to the wall-clock budget (the
        # client's "still running" crawl). A genuinely public app — or a DEEPER gated
        # sub-area once public content is covered — is never aborted (see
        # _classify_no_cred_auth). A username-first wall walked INLINE by _walk_wizard is
        # caught there by _secret_wall_reached; this call establishes the gated flow.
        if self._classify_no_cred_auth(controls, item, item.url, obs.url) == "stop":
            wall_fp = state_fingerprint(obs.url, controls, obs.dialog_flags)
            if wall_fp not in self._visited_fingerprints:
                self._visited_fingerprints.add(wall_fp)
                self._record_state(
                    url=obs.url, title=obs.title, controls=controls, fingerprint=wall_fp,
                    actions=[],
                    screenshots=[(await self._port.screenshot_png(), self._clock.now_ms())],
                    last_seen_ms=self._clock.now_ms(),
                )
            self._stop_reason = STOP_AUTH_REQUIRED
            self._hard_stop = True
            return
        fingerprint = state_fingerprint(obs.url, controls, obs.dialog_flags)

        # AN ITEM IS ONLY SPENT WHEN ITS URL WAS ACTUALLY OBSERVED. The frontier's
        # push-time dedup marked this item's key used the moment it was queued —
        # but on an app that drops its login per page load, the goto above lands on
        # the sign-in/landing page instead, and the expansion records THAT. The
        # requested route was never seen, yet its key stayed spent, so a real
        # click-path to it discovered later could never re-enqueue it. Live: the
        # operator-onboarded wizard entry became permanently unreachable at t=0.
        # Released ONCE per key (bounded — an app that genuinely redirects a route
        # gets one retry, never a loop).
        landed_key = _url_key(obs.url)
        item_key = _url_key(item.url)
        if landed_key != item_key and item_key not in self._rearmed_keys:
            self._rearmed_keys.add(item_key)
            if self._frontier.release(item_key):
                logger.info(
                    "qec.crawler.entry_rearmed requested=%s landed=%s — the route "
                    "was not reached; its key is released so a discovered in-app "
                    "path can enqueue it again", item.url[:120], obs.url[:120])

        if item.parent_fingerprint:
            self._emitter.emit_edge(from_state=item.parent_fingerprint,
                                    to_state=fingerprint, verb="navigate",
                                    target_label=item.discovered_via)
        if fingerprint in self._visited_fingerprints:
            return  # unique-state dedup: already recorded this exact state
        self._visited_fingerprints.add(fingerprint)

        first_seen = self._clock.now_ms()
        entry_png = await self._port.screenshot_png()
        entry_ts = self._clock.now_ms()

        actions: list[emit.ActionRecord] = []
        # A grounded [click → navigation] that REACHED this state (an auth-wall
        # crossing that clicked its way here rather than re-navigating). Stamped
        # with this state's id now that it exists, so the proof is attached to a
        # real state instead of being logged and dropped — which is what left the
        # generator counting every navigation on this app as unproven.
        if self._pending_reach_actions:
            for pending in self._pending_reach_actions:
                pending.state_id = fingerprint
            actions.extend(self._pending_reach_actions)
            logger.info("qec.crawler.reach_proof_recorded count=%d state=%s",
                        len(self._pending_reach_actions), fingerprint[:12])
            self._pending_reach_actions = []
        if self._pending_reach_edge:
            src, label = self._pending_reach_edge
            self._pending_reach_edge = None
            # A self-edge is not a navigation. Emitting one would put a transition
            # into the graph that goes nowhere, which is worse than none at all.
            if src and src != fingerprint:
                self._emitter.emit_edge(from_state=src, to_state=fingerprint,
                                        verb="navigate", target_label=label)
                logger.info("qec.crawler.reach_edge_emitted from=%s to=%s via=%r",
                            src[:12], fingerprint[:12], label[:40])
        # Phase A: fill the form (if any), read back committed values.
        is_form = (
            not self._observe_only
            and any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c)
                    for c in controls)
        )
        snapshot_controls = controls
        fill = None  # hoisted: Phase-B (below) reads fill.flow_candidates
        if is_form:
            # Fill even with NO answer key: the typed default filler synthesizes valid
            # low-confidence values so validation-gated forms advance and deeper flows
            # become reachable (client seeds still win where present).
            self._forms_found += 1
            fill = await fill_form_phase_a(
                self._port, controls, self._answer_key or AnswerKey(), self._clock,
                phase=Phase.EXPLORE.value, state_id=fingerprint,
                identity=self._identity, recalled=self._recalled_values,
                journey_values=self._journey_values,
                priors=self._field_priors, data_mode=self._data_mode,
                choice_overrides=self._choice_overrides,
            )
            actions.extend(fill.actions)
            self._tracker.note_action(len(fill.actions))
            self._fields_inferred.extend(fill.inferred_fields)
            self._open_choice_unverified += fill.open_choice_unverified
            self._note_fills_by_kind(fill.filled_by_kind)
            self._fields_unfilled.extend(fill.unfilled_fields)
            # Tag each unfilled field with the page it appeared on (grounds flow grouping).
            self._fields_seed_detail.extend(
                {"label": lbl, "url": obs.url or ""} for lbl in fill.unfilled_fields)
            # The signature ledger: what each field IS and how it got answered. This
            # is the key the residue ask and the learning loop are both built on —
            # without it a second crawl has no way to know it already asked. Values
            # are deliberately not carried.
            self._collect_ledger(fill.field_ledger, obs.url or "")
            self._submit_candidates.extend(
                fc.name for fc in fill.flow_candidates if fc.name and not fc.danger)
            if fill.filled:
                # re-inventory so form_snapshot carries the committed values.
                after_fill = await self._observe()
                snapshot_controls = build_inventory(after_fill.raw_controls,
                                                     self._refuse_pack, url=after_fill.url)

        # Read the real OPTION LABELS of custom dropdowns that build their menu only on
        # open (a common SPA pattern the static inventory can't see) so a choice is shown
        # with its actual options, not "options not captured". Runs BEFORE navigation
        # discovery (which leaves the page); best-effort + state-restoring.
        await self._probe_select_options(snapshot_controls, url=obs.url)
        # ACT-THEN-DIFF: commit a driver choice to reveal DEPENDENT fields whose options
        # only populate after a prior field is chosen (e.g. To Account after From Account).
        await self._probe_dependencies(snapshot_controls, url=obs.url)
        # UNHANDLED controls: interactive controls the matcher has no primitive for → named in
        # the coverage ledger (never a silent skip). A NAMELESS unsupported control (incl. a
        # drag-drop handle) is ledgered with a synthesized positional label instead of being
        # silently dropped — closes the requirements-audit honesty leak.
        for _idx, _c in enumerate(snapshot_controls):
            if matcher.is_unhandled(_c):
                _label = str(_c.get("name") or "").strip() \
                    or f"{(_c.get('kind') or 'control')}#{_idx} (unnamed)"
                self._unhandled_controls.append({
                    "label": _label, "kind": str(_c.get("kind") or ""),
                    "reason": matcher.unhandled_reason(_c)})

        # Navigation discovery (grounded): click safe actionable controls.
        actions.extend(await self._discover(item, controls, is_form, fingerprint,
                                            budget_left=self._budget.max_actions_per_state - len(actions)))

        last_seen = self._clock.now_ms()
        # ANSWERS P1.B — capture rendered value nodes in the page's FINAL state (after
        # fills + discovery clicks reveal outputs like a computed premium).
        displayed_values = await self._port.collect_displayed_values()
        # API/network mining — drain the XHR/fetch calls the app made during this
        # visit (diagnostics-only; the app's real API surface as grounded evidence).
        # Best-effort: a port without the verb yields nothing, never breaks a crawl.
        network_calls = await self._drain_network()
        # OPAQUE-SURFACE detection (best-effort): positively find DOM-unreadable surfaces on
        # this state so the coverage ledger names them, never a silent "clean" scan.
        collect_opaque = getattr(self._port, "collect_opaque", None)
        if collect_opaque is not None:
            try:
                self._opaque_surfaces.extend(await collect_opaque())
            except Exception:
                pass
        # VISION PERCEIVER (U2): a DOM-opaque page (canvas / Flutter Web) yields
        # controls the DOM can't read. When vision is enabled (per-tenant, default
        # OFF) AND the page is opaque + sparse, perceive its controls + displayed
        # outcomes from a screenshot and attach them to the OPAQUE ledger as
        # vision-sourced evidence (capture_mode=vision, unverified). Gated + best-
        # effort: a no-op without the oracle or on a normal page. This records what
        # vision SAW; it does NOT act on it (coordinate action + R0 is the next
        # increment) — so nothing unverified enters the proven catalog.
        if self._oracle.vision_configured and perception.should_perceive(
                snapshot_controls, self._opaque_surfaces):
            try:
                shot = await self._port.screenshot_png()
                b64 = base64.b64encode(shot).decode("ascii") if shot else ""
                pv = await self._oracle.perceive(b64, {"url": obs.url})
                pv_controls = pv.get("controls") or []
                if pv_controls:
                    self._opaque_surfaces.append({
                        "kind": "vision_perceived",
                        "label": "%d controls perceived by vision" % len(pv_controls),
                        "reason": ("DOM-opaque page; vision enumerated its controls "
                                   "(unverified — not yet acted on)"),
                        "capture_mode": "vision",
                        "controls": pv_controls[:24],
                        "displayed_values": (pv.get("displayed_values") or [])[:12],
                    })
                    logger.info(
                        "qec.vision.perceived url=%s controls=%d values=%d",
                        (obs.url or "")[:80], len(pv_controls),
                        len(pv.get("displayed_values") or []))
            except Exception:
                pass
        # WIZARD/STEPPER (#1): on a FILLED form state, advance a non-danger
        # Next/Continue to record deeper wizard steps in place (SPA quote wizards
        # live at one URL — step 2 is reachable only by the click sequence). The
        # walk OWNS the recording of this step + every step it reaches; when there
        # is no advance trigger it returns False and this state is recorded normally.
        walked = False
        entry_pick = AdvanceDecision()
        # LEGIBILITY RUNS ON EVERY FORM, not only on the ones the walk engages.
        # Gated behind the walk's own precondition, this never fired on the page
        # it was built for: the fill committed NOTHING there (every field was a
        # portal-rendered choice), so `fill.filled` was 0, the gate stayed shut,
        # and the one page that most needed an explanation produced none. The
        # blockage is a fact about the page whether or not we then try to walk it.
        if is_form and fill is not None:
            blocked_label = self._note_advance_blocked(
                snapshot_controls, obs.url, fill)
            # DESCRIBING THE BLOCK IS NOT ANSWERING IT. The app has just stated,
            # by disabling its own forward control, exactly what it is waiting
            # for — so try to satisfy it before recording a human ask.
            if blocked_label:
                snapshot_controls = await self._answer_to_unblock(
                    snapshot_controls, blocked_label, obs.url or "", fill)
        if self._wizard_enabled and is_form and fill is not None and (fill.filled or fill.has_unanswered_decisions):
            entry_pick = await self._pick_advance(
                snapshot_controls, obs.url, obs.title, fingerprint)
            logger.info("qec.wizard.gate_open url=%s filled=%d pick=%r",
                        obs.url, fill.filled,
                        str((entry_pick.control or {}).get("name") or "")[:40])
            walked = await self._walk_wizard(
                item=item, url=obs.url, title=obs.title, controls=snapshot_controls,
                fingerprint=fingerprint, base_actions=actions,
                entry_shot=(entry_png, entry_ts), first_seen_ms=first_seen,
                displayed_values=displayed_values, network_calls=network_calls,
                entry_pick=entry_pick,
            )
        elif is_form:
            # DIAGNOSTIC ONLY — never changes behaviour. A FORM the wizard never
            # even looked at: on a revisit the fields are already populated, so
            # filled==0 and nothing reads as an unanswered decision. The gate
            # closes and the page is recorded as its own one-step "journey"
            # instead of joining the walk that runs through it.
            logger.info("qec.wizard.gate_closed url=%s filled=%s decisions=%s",
                        obs.url,
                        getattr(fill, "filled", None) if fill else None,
                        getattr(fill, "has_unanswered_decisions", None) if fill else None)

        # A single-page form that ends at a Submit IS a business journey — a
        # one-step one. Recording only multi-step wizards made an application with a
        # real quote form report ZERO flows, which reads as "no journeys here" when
        # the truth is "one journey, one step long".
        if not walked and is_form and fill is not None:
            # The entry-level honesty rung: when the tiers found nothing AND the
            # agent could not be reached, whether this form advances is UNKNOWN —
            # the one-step journey must say so, never "no_advance" (covered).
            if entry_pick.oracle_status == ORACLE_UNAVAILABLE:
                single_terminal = flow_ledger.TERMINAL_ORACLE_UNAVAILABLE
            elif self._pick_submit_candidate(snapshot_controls):
                single_terminal = flow_ledger.TERMINAL_SUBMIT_BOUNDARY
            else:
                single_terminal = flow_ledger.TERMINAL_NO_ADVANCE
            single_step: dict[str, Any] = {
                "fingerprint": fingerprint, "url": obs.url, "title": obs.title,
                "fields_filled": fill.filled,
                "fields_unfilled": len(fill.unfilled_fields),
            }
            single_dps = _decision_points(fill.field_ledger)
            if single_dps:
                single_step["decision_points"] = single_dps
            self._flows.append(flow_ledger.build_flow(
                entry_fingerprint=fingerprint, entry_url=obs.url, entry_title=obs.title,
                steps=[single_step],
                terminal=single_terminal,
                terminal_url=obs.url,
                # Same normalisation as the wizard walk: value_type exists
                # only after the value-oracle inference.
                outcome_values=[
                    v for v in _displayed_values(displayed_values or ())
                    if str(v.get("value_type") or "")
                    in _BOUNDARY_OUTCOME_TYPES],
                max_steps=self._max_wizard_steps))
        # A NON-form page that is a next-action fork (a quote summary: Apply Now /
        # Start Over / Back to Dashboard) is a one-step business flow with a
        # 3-branch decision. Without this the fork lived only in the flat
        # submit_candidates coverage list and never became journey branches.
        elif not walked and not is_form:
            nd = _next_action_decisions(snapshot_controls, fingerprint)
            if nd:
                self._flows.append(flow_ledger.build_flow(
                    entry_fingerprint=fingerprint, entry_url=obs.url,
                    entry_title=obs.title,
                    steps=[{
                        "fingerprint": fingerprint, "url": obs.url,
                        "title": obs.title, "fields_filled": 0,
                        "fields_unfilled": 0, "decision_points": nd,
                    }],
                    # A forward option always exists (the emitter requires it), so
                    # this page IS the submit boundary of its flow.
                    terminal=flow_ledger.TERMINAL_SUBMIT_BOUNDARY,
                    terminal_url=obs.url,
                    outcome_values=[
                        v for v in _displayed_values(displayed_values or ())
                        if str(v.get("value_type") or "")
                        in _BOUNDARY_OUTCOME_TYPES],
                    max_steps=self._max_wizard_steps))
        if not walked:
            self._record_state(
                url=obs.url, title=obs.title, controls=snapshot_controls,
                fingerprint=fingerprint, actions=actions,
                screenshots=[(entry_png, entry_ts)],
                first_seen_ms=first_seen, last_seen_ms=last_seen,
                displayed_values=displayed_values, network_calls=network_calls,
            )

        # Phase B (attested submit): after the form state is recorded, drive the
        # FIRST operator-approved non-danger flow and push the post-submit page onto
        # the frontier so the deeper flow is crawled. Default-OFF (self._submit_enabled).
        if self._submit_enabled and is_form and fill is not None:
            await self._maybe_submit_phase_b(item, snapshot_controls, fill, fingerprint)
        elif self._submit_enabled and not is_form and not walked:
            # A formless decision page reached directly — a quote summary whose only
            # action is "Apply Now". No fill produced a candidate, so the form path
            # above never sees it; cross the approved forward action here so the
            # crawl continues past it into the application funnel.
            await self._maybe_submit_next_action(
                controls=snapshot_controls, url=obs.url, fingerprint=fingerprint,
                depth=item.depth)

    async def _discover(
        self, item: FrontierItem, controls: Sequence[dict[str, Any]], is_form: bool,
        fingerprint: str, *, budget_left: int,
    ) -> list[emit.ActionRecord]:
        """Traverse + record: enqueue in-scope navigation destinations (from link
        HREFS, robust to pushState/SPA routing) and click safe actionable controls
        to record grounded outcomes.  Never clicks an irreversible control, and
        never clicks a button on a form state (submit boundary)."""
        if item.depth >= self._budget.max_depth:
            return []
        candidates = [
            c for c in controls
            if str(c.get("name") or "").strip()
            and not c.get("disabled")
            and not c.get("danger")
            and (c.get("kind") == "link" or (not is_form and c.get("kind") == "button"))
        ]
        # Rank route-changing links ahead of same-page chrome so the per-state click
        # budget reaches real routes before it is exhausted by nav/footer chrome.
        candidates = self._rank_candidates(candidates, item.url)
        # HREF-FOLLOW (SPA traversal): enqueue in-scope, route-shaped link destinations
        # DIRECTLY from their href — a grounded navigation target — so discovery no
        # longer depends on a click producing an observable page.url delta (which
        # history/pushState SPAs often don't within the settle window). Bounded +
        # convergent: the frontier's url_template key dedups (every /product/{id}
        # collapses to one milestone) and skips already-enqueued / current states.
        self._enqueue_link_hrefs(candidates, item, fingerprint)
        actions: list[emit.ActionRecord] = []
        # MENU-REVEAL: some nav is hidden inside a hover fly-out (aria-haspopup) OR a
        # click dropdown (aria-expanded) whose items can't be clicked until the menu
        # opens. Open the menu, click the revealed item, and record the GROUNDED
        # [open, nav-click] path so the generated flow is runnable. Bounded.
        actions.extend(await self._menu_reveal(item, controls, fingerprint))
        # DIRECT-NAV GROUNDING: the href-follow above DISCOVERS link destinations but
        # records no grounded CLICK (it deliberately skips clicking href links for
        # speed). Classic multi-page sites (a plain <a href> nav bar) therefore ground
        # nothing, so no coherent journey can be built. Here we CLICK the top in-scope
        # nav links and record the [click → navigation] the journey generator needs —
        # each unique route grounded ONCE (global dedup), menu-gated items left to
        # _menu_reveal. This is the fix for "link-based site → empty / wandering tests".
        actions.extend(await self._ground_nav_links(item, candidates, fingerprint, budget_left))

        if budget_left <= 0:
            return actions
        # PERF: skip clicking links whose destination was ALREADY enqueued from the
        # href — that navigation is grounded when the destination is expanded (an
        # edge is emitted with the link's name), so re-clicking it here only costs a
        # page reset + navigates away. The click pass targets STATEFUL controls:
        # buttons (reveal actions/state) and href-less links (JS-nav needs a click to
        # discover). This turns an O(links) navigate-and-reset loop into O(a few
        # stateful probes) — the fix for the per-page crawl cost at fleet scale.
        click_candidates = [
            c for c in candidates
            if not (c.get("kind") == "link" and self._link_destination(c, item.url))
        ]
        needs_reset = True  # the Phase-A fills may have left the page dirty → start fresh
        for control in click_candidates[:budget_left]:
            if self._tracker.stop_reason() or self._cancelled:
                break
            await self._politeness_delay()
            # PERF: reset to the recorded state ONLY when the previous probe actually
            # changed the page. A no-op click (outcome 'none') leaves us on item.url,
            # so the next probe is still from the recorded state and needs no
            # navigation — lazy reset preserves per-probe grounding at a fraction of
            # the page loads.
            if needs_reset:
                await self._goto_keeping_login(item.url)
                needs_reset = False
            observation = await self._port.click(control)
            self._tracker.note_request()
            action = emit.build_action_record(
                dict(control), verb="click", value=None, observation=observation,
                phase=Phase.EXPLORE.value, state_id=fingerprint,
                timestamp_ms=self._clock.now_ms(),
            )
            self._tracker.note_action()
            if action.after and str(action.after.get("outcome") or "") != "none":
                needs_reset = True  # state changed → restore item.url before the next probe
            if action.after and action.after.get("navigated"):
                dest = observation.url_after
                action.to_state = _url_key(dest)
                if self._in_scope(dest):
                    self._frontier.push(
                        FrontierItem(url=dest, depth=item.depth + 1,
                                     discovered_via=str(control.get("name") or ""),
                                     parent_fingerprint=fingerprint),
                        key=_url_key(dest),
                    )
            actions.append(action)
        return actions

    @staticmethod
    def _nav_is_menu_gated(control: dict[str, Any]) -> bool:
        """A link HIDDEN inside a collapsed menu (Bootstrap ``dropdown-item`` / ARIA
        ``menuitem``) or a disclosure TOGGLE (haspopup / aria-expanded) — not a plain
        visible destination. Menu-gated items are grounded by :meth:`_menu_reveal`
        (which opens the menu first); clicking one here would just burn the 5s action
        timeout on a hidden element, so it is skipped."""
        q = control.get("qec") or {}
        css = str(q.get("css_hint") or "").lower()
        role = str(q.get("role") or "").strip().lower()
        if "dropdown-item" in css or "menu-item" in css or role == "menuitem":
            return True
        return bool(str(q.get("haspopup") or "").strip() or str(q.get("expanded") or "").strip())

    async def _ground_nav_links(
        self, item: FrontierItem, candidates: Sequence[dict[str, Any]],
        fingerprint: str, budget_left: int,
    ) -> list[emit.ActionRecord]:
        """GROUND direct nav-link navigations: CLICK the top in-scope nav links and
        record the ``[click → navigation]`` a runnable journey needs.

        The discovery pass (:meth:`_enqueue_link_hrefs`) follows link HREFS to find
        pages but records NO grounded click; this fills that gap for classic
        multi-page sites (a plain ``<a href>`` nav bar) so they produce coherent
        grounded journeys instead of empty/wandering tests. Each UNIQUE destination is
        grounded ONCE across the whole crawl (``self._grounded_navs``) — a nav bar
        repeated on every page costs ~O(unique routes), not O(states × links).
        Menu-gated items are left to :meth:`_menu_reveal`; bounded by
        :data:`_MAX_GROUND_NAVS` and the per-state click budget."""
        if budget_left <= 0 or item.depth >= self._budget.max_depth:
            return []
        click = getattr(self._port, "click", None)
        if click is None:
            return []
        targets: list[tuple[dict[str, Any], str]] = []
        seen_keys: set[str] = set()
        for c in candidates:
            if c.get("kind") != "link" or self._nav_is_menu_gated(c):
                continue
            dest = self._link_destination(c, item.url)
            if not dest:
                continue
            key = _url_key(dest)
            if key in self._grounded_navs or key in seen_keys:
                continue
            seen_keys.add(key)
            targets.append((c, key))
            if len(targets) >= min(_MAX_GROUND_NAVS, budget_left):
                break
        recorded: list[emit.ActionRecord] = []
        for control, key in targets:
            if self._tracker.stop_reason() or self._cancelled:
                break
            if key in self._grounded_navs:
                continue
            # Mark the route TRIED up-front so it is never re-clicked from another
            # state — bounds the cost to one attempt per unique route even on a
            # pushState SPA whose click shows no URL delta (href-follow still
            # discovered it; a grounded click just isn't available there).
            self._grounded_navs.add(key)
            await self._politeness_delay()
            # reset — a real nav leaves the page (login re-established if this app
            # drops it on a reload, else byte-identical to a plain goto)
            await self._goto_keeping_login(item.url)
            # The URL we are ACTUALLY standing on. Not necessarily ``item.url``:
            # on an app that drops its login per page load the reset above signs
            # back in and can land elsewhere, and comparing against the wrong
            # baseline would either miss a real navigation or invent one.
            try:
                before_url = await self._port.current_url()
            except Exception:
                before_url = item.url
            try:
                obs = await self._port.click(control)
            except Exception:
                continue  # hidden / not actionable (5s cap) — href-follow still found it
            self._tracker.note_request()
            action = emit.build_action_record(
                dict(control), verb="click", value=None, observation=obs,
                phase=Phase.EXPLORE.value, state_id=fingerprint,
                timestamp_ms=self._clock.now_ms(),
            )
            self._tracker.note_action()
            arrived = ""
            if action.after and action.after.get("navigated"):
                arrived = obs.url_after
            else:
                # SPA pushState — THE REASON A CRAWL CAPTURES PAGES AND STILL
                # CANNOT BUILD AN END-TO-END TEST.
                #
                # The click-time classifier only recognises a browser navigation
                # event, a DOM mutation on the clicked node, or a dialog. A
                # framework app changes route by pushState and re-renders in
                # place, so it trips none of them — the click reports nothing
                # while the app has demonstrably moved to another route.
                #
                # href-follow still DISCOVERED those routes, so their pages were
                # crawled and catalogued; what was never recorded was a PROVEN
                # [click → navigation] edge between them. Live on an admin
                # console: 16 pages captured, "PROVED only 0 of the 15
                # navigations", and therefore no coherent E2E — every page known,
                # no way to say how a user gets from one to the next.
                #
                # The live URL is the stronger signal, and it is read from the
                # page AFTER the click rather than taken from the click event. A
                # different route really was reached by really clicking this
                # control, which is exactly what a grounded edge asserts. Same
                # lesson the wizard walk already learned: the new state IS the
                # evidence, and the click-time outcome is corroboration.
                try:
                    live = await self._port.current_url()
                except Exception:
                    live = ""
                if live and _url_key(live) != _url_key(before_url):
                    arrived = live
                    # Say HOW it was detected, so a grounded edge is auditable
                    # and a soft-navigated one is never silently indistinguishable
                    # from a hard browser navigation.
                    after = dict(action.after or {})
                    after.update(navigated=True, outcome="navigation")
                    action.after = after
                    # The HOW-detected label rides in ``qec`` — the open
                    # diagnostics dict — never inside ``after``: AfterBundle is a
                    # strict mirrored contract (extra='forbid'), and a foreign key
                    # there makes the writer refuse the whole crawl. Proven live:
                    # one soft-classified click, "Extra inputs are not permitted",
                    # 35 minutes of evidence refused.
                    action.qec = dict(action.qec or {})
                    action.qec["navigation_kind"] = "pushstate"
            if arrived:
                action.to_state = _url_key(arrived)
                self._grounded_navs.add(key)
                self._grounded_navs.add(_url_key(arrived))
                recorded.append(action)
                if self._in_scope(arrived):
                    self._frontier.push(
                        FrontierItem(url=arrived, depth=item.depth + 1,
                                     discovered_via=str(control.get("name") or ""),
                                     parent_fingerprint=fingerprint),
                        key=_url_key(arrived),
                    )
        return recorded

    @staticmethod
    def _href_of(control: dict[str, Any]) -> str:
        """The link destination the inventory captured (``qec.href``), or ""."""
        return str((control.get("qec") or {}).get("href") or "").strip()

    def _resolve_href(self, href: str, base_url: str) -> str:
        """Resolve a raw link href against the page URL into an absolute http(s) URL
        to enqueue, or "" for a NON-navigational href (mailto/tel/sms/js/data/blob,
        or a bare cosmetic ``#anchor``).  A route-shaped hash (``#/orders``) is kept
        — hash routes are real client routes (``url_template`` preserves them)."""
        h = (href or "").strip()
        if not h:
            return ""
        low = h.lower()
        if low.startswith(("mailto:", "tel:", "sms:", "javascript:", "data:", "blob:", "about:")):
            return ""
        if h.startswith("#"):
            frag = h[1:]
            if not (frag.startswith("/") or frag.startswith("!") or "/" in frag):
                return ""  # bare in-page anchor — cosmetic, not a client route
        try:
            absu = urljoin(base_url or "", h)
        except Exception:
            return ""
        if (urlsplit(absu).scheme or "").lower() not in ("http", "https"):
            return ""
        return absu

    def _link_destination(self, control: dict[str, Any], base_url: str) -> str:
        """The in-scope, NEW-milestone URL a link control points at, or "" when it
        is out-of-scope, non-navigational, or resolves to the current page's state
        template (no new milestone)."""
        if control.get("kind") != "link":
            return ""
        dest = self._resolve_href(self._href_of(control), base_url)
        if not dest or not self._in_scope(dest):
            return ""
        if _url_key(dest) == _url_key(base_url):
            return ""
        return dest

    def _rank_candidates(
        self, candidates: list[dict[str, Any]], base_url: str,
    ) -> list[dict[str, Any]]:
        """Stable-partition so route-changing links (a distinct in-scope destination)
        come first — otherwise same-page nav/footer chrome, first in the DOM, spends
        the per-state click budget before any real route is reached."""
        routey = [c for c in candidates if self._link_destination(c, base_url)]
        if not routey:
            return list(candidates)
        rest = [c for c in candidates if not self._link_destination(c, base_url)]
        return routey + rest

    def _enqueue_link_hrefs(
        self, candidates: Sequence[dict[str, Any]], item: FrontierItem, fingerprint: str,
    ) -> None:
        """Push every in-scope, route-shaped link destination onto the frontier from
        its href.  The traversal fix for history/pushState SPAs; bounded + convergent
        via the frontier's url_template dedup (id-routes collapse; already-enqueued /
        current states are skipped)."""
        if item.depth >= self._budget.max_depth:
            return
        for control in candidates:
            dest = self._link_destination(control, item.url)
            if not dest:
                continue
            self._frontier.push(
                FrontierItem(
                    url=dest, depth=item.depth + 1,
                    discovered_via=str(control.get("name") or ""),
                    parent_fingerprint=fingerprint,
                ),
                key=_url_key(dest),
            )

    async def _menu_reveal(
        self, item: FrontierItem, controls: Sequence[dict[str, Any]], fingerprint: str,
    ) -> list[emit.ActionRecord]:
        """Open collapsed nav MENUS and record a GROUNDED click-path to the nav they
        reveal.  Two menu shapes, both generic (ARIA, never app selectors):

          * ``aria-haspopup`` — a HOVER fly-out / mega-menu (hover to reveal);
          * ``aria-expanded`` — a CLICK dropdown / disclosure (a Bootstrap
            ``dropdown-toggle`` etc.) whose items are HIDDEN until it is clicked.

        The second case is the fix for the live defect: a bare click on a hidden
        dropdown item TIMES OUT (recorded honestly as ``error``), so the crawler
        reached those routes only by href-follow — leaving NO grounded click a
        generated test could replay.  Here we OPEN the menu, then CLICK the revealed
        in-scope nav item and OBSERVE its navigation, returning the grounded
        ``[open, nav-click]`` actions to attach to this state — so the generator can
        compile a RUNNABLE flow (open menu → click item → arrive), not an
        un-driveable href milestone.  Bounded (:data:`_MAX_HOVER_REVEALS`) +
        best-effort; enqueues the destination even when the click can't be grounded
        (discovery is preserved)."""
        click = getattr(self._port, "click", None)
        hover = getattr(self._port, "hover", None)
        if click is None or item.depth >= self._budget.max_depth:
            return []

        def _opener(c: dict[str, Any]) -> str:
            """'' if not a menu opener, else 'hover' (haspopup) or 'click' (expanded)."""
            if c.get("danger") or c.get("disabled") or not str(c.get("name") or "").strip():
                return ""
            q = c.get("qec") or {}
            if str(q.get("haspopup") or "").strip():
                return "hover"
            if str(q.get("expanded") or "").strip():   # any aria-expanded => a toggle
                return "click"
            return ""

        triggers = [(c, _opener(c)) for c in controls]
        triggers = [(c, m) for c, m in triggers if m][:_MAX_HOVER_REVEALS]
        if not triggers:
            return []
        recorded: list[emit.ActionRecord] = []
        # In-scope nav DESTINATIONS already reachable without opening a menu — used
        # only to PREFER a genuinely menu-gated route, NOT to skip (the dropdown
        # items are inventoried even while HIDDEN, so their hrefs are already
        # "known"; the whole point is to GROUND a click a bare probe can't perform).
        preopen = {
            _url_key(d) for d in
            (self._link_destination(c, item.url) for c in controls) if d
        }
        for control, mode in triggers:
            if self._tracker.stop_reason() or self._cancelled:
                break
            await self._politeness_delay()
            await self._goto_keeping_login(item.url)  # open from the clean recorded state
            self._tracker.note_request()
            # OPEN the menu the way its shape requires: a HOVER fly-out (haspopup)
            # opens on hover — clicking it might navigate away; a CLICK dropdown
            # (aria-expanded) opens on click, recorded as a grounded action so the
            # replay opens the menu the same way before clicking an item.
            open_action: Optional[emit.ActionRecord] = None
            try:
                if mode == "hover" and hover is not None:
                    await hover(control)
                else:
                    open_obs = await self._port.click(control)
                    open_action = emit.build_action_record(
                        dict(control), verb="click", value=None, observation=open_obs,
                        phase=Phase.EXPLORE.value, state_id=fingerprint,
                        timestamp_ms=self._clock.now_ms())
                    self._tracker.note_action()
            except Exception:
                continue
            self._tracker.note_request()
            revealed = build_inventory(
                await self._port.collect_controls(), self._refuse_pack, url=item.url)
            targets = [
                (rc, d) for rc in revealed
                for d in [self._link_destination(rc, item.url)] if d
            ]
            # (a) DISCOVERY: enqueue any route that appeared ONLY after opening
            # (a hover fly-out mints new hrefs) so nothing is lost even when the
            # grounded click below can't be captured.
            for _rc, _dest in targets:
                if _url_key(_dest) not in preopen:
                    self._frontier.push(
                        FrontierItem(url=_dest, depth=item.depth + 1,
                                     discovered_via=f"menu:{control.get('name') or ''}",
                                     parent_fingerprint=fingerprint),
                        key=_url_key(_dest))
            # (b) GROUNDING: try-click the revealed in-scope nav links; KEEP the FIRST
            # that actually navigates (the one the open made clickable). Prefer a
            # menu-GATED route (a hidden dropdown item), else any.
            targets.sort(key=lambda t: 0 if _url_key(t[1]) not in preopen else 1)
            grounded = False
            for rc, dest in targets[:_MAX_MENU_ITEMS]:
                try:
                    nav_obs = await self._port.click(rc)
                except Exception:
                    continue   # still hidden / not actionable — try the next item
                self._tracker.note_request()
                nav_action = emit.build_action_record(
                    dict(rc), verb="click", value=None, observation=nav_obs,
                    phase=Phase.EXPLORE.value, state_id=fingerprint,
                    timestamp_ms=self._clock.now_ms())
                self._tracker.note_action()
                if nav_action.after and nav_action.after.get("navigated"):
                    arrived = nav_obs.url_after
                    if open_action is not None:
                        recorded.append(open_action)  # replay opens first
                    nav_action.to_state = _url_key(arrived)
                    recorded.append(nav_action)
                    # Mark this route grounded so the direct-nav pass doesn't re-click it.
                    self._grounded_navs.add(_url_key(arrived))
                    if self._in_scope(arrived):
                        self._frontier.push(
                            FrontierItem(url=arrived, depth=item.depth + 1,
                                         discovered_via=f"menu:{control.get('name') or ''}",
                                         parent_fingerprint=fingerprint),
                            key=_url_key(arrived))
                    grounded = True
                    break   # ONE grounded path per state (a nav leaves the page;
                            # replaying a second open from here would be off-page)
                # a no-op/hidden probe leaves us on item.url — safe to try the next.
            if grounded:
                break   # one grounded menu path is enough to make the flow runnable
        return recorded
