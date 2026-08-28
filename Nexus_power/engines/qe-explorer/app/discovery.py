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
from . import observation_health
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
from .browser import BrowserPort, PageObservation, _norm_url
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
    STOP_INVENTORY_FAILED,
    STOP_COMPLETED,
    STOP_ERROR,
    TRAVERSAL_FULL,
    TRAVERSAL_OBSERVE,
    TRAVERSAL_POSTURES,
    TRAVERSAL_PROBE,
    _ACTUATOR_KINDS,
    _AUTH_SESSION_RE,
    _BOUNDARY_OUTCOME_TYPES,
    is_boundary_outcome,
    _E2E_WIZARD_ADVANCES,
    _E2E_WIZARD_STEPS,
    _ENTRY_GOTO_RETRIES,
    _ENTRY_RETRY_DELAY_S,
    _FILLABLE_KINDS,
    _FULL_DEP_PROBES,
    _FULL_OPTION_PROBES,
    _FULL_PROBED_OPTIONS,
    _MAX_DEP_PROBES,
    _MAX_EXPANSIONS,
    _MAX_TAB_VIEWS,
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
# Identity comes from the identity layer (``self._fingerprinter``), never
# from the hasher directly — see app.state_identity.
from .state_identity import (_MAX_COVERAGE_STATES, _MAX_DANGER_NAMES,
                             _MAX_NETWORK_CALLS, _MAX_STATE_FIELDS,
                             StateFingerprinter, StateRecorder,
                             _action_to_dict, _displayed_values,
                             _form_snapshot, _is_password, _network_calls)
from . import flow_ledger
from .boundary import (BOUNDARY_APPROVABLE, BOUNDARY_SAFE,
                       boundary_key, classify_boundary)
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
from .inventory import (build_inventory, carry_earned_annotations,
                        form_signal_for)

# The SAME vocabulary the walk uses to spot an advance, so the section
# sweep and the wizard walk cannot disagree about what a step is.
from .vocab import ADVANCE_RE, COMMIT_RE

logger = logging.getLogger("app.crawler")

#: How many view-switching navigation controls one sweep will visit. A
#: single-URL application's whole section list is site chrome, so this bounds a
#: sweep of the WHOLE application, not of one page.
_MAX_VIEW_SECTIONS = 24

#: How many candidates make a SECTION LIST rather than a couple of stray
#: buttons. A form with one "Yes" and one "No" is not an application navigating
#: without URLs, and sweeping it buys nothing while costing a click each -
#: measured on the f3_questionnaire_submit characterization fixture, where the
#: sweep added 2 actions and not one new state. The frontier and the walk
#: already cover an application that small.
_MIN_VIEW_SECTIONS = 3


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
                # An application that navigates WITHOUT URLs leaves the frontier
                # empty after the entry, however many screens it has. Sweep its
                # own navigation once before calling the crawl complete; an app
                # with routes finds no candidates and pays a single observation.
                # ONLY for an application that navigates without URLs. If a
                # single link href was ever enqueued, discovery had a frontier
                # to work with and the sweep is not what ended the crawl — so a
                # routed application pays nothing at all here, not even the
                # observation.
                if (not getattr(self, "_view_sweep_done", False)
                        and not getattr(self, "_link_hrefs_enqueued", 0)):
                    self._view_sweep_done = True
                    if await self._sweep_view_navigation():
                        continue
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
            finally:
                # M1.5 — flush this expansion's popup / dialog / download
                # evidence. In the ``finally`` deliberately: an expansion that
                # threw is precisely the one whose browser events explain WHY,
                # and losing them there would leave the exception above with no
                # context. Best-effort; never raises.
                await self._drain_browser_events()
                # M1.7 / T-GW-03 — CHECKPOINT THE WORK LIST, also in the
                # ``finally``. The expansion that threw is exactly the one after
                # which the process is most likely to die, so it is the one whose
                # frontier is most worth persisting. Written per expansion rather
                # than per N states because the unit of lost work on a kill is one
                # expansion, and a cheaper cadence would trade durability for an
                # append the crawl is already paying for on every state.
                self._emit_checkpoint()

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

    async def _recover_inventory(self, obs: Any, item: FrontierItem) -> bool:
        """One bounded attempt to rescue a failed inventory read (T-GW-01).

        Returns True only when the RE-READ succeeded, in which case the caller
        continues with the observation this method installed.  Returns False when
        the page could not be read — and, before doing so, terminates the crawl
        honestly.

        WHY EXACTLY ONE RETRY.  ``context_lost`` and ``timeout`` are transient by
        construction: the page was navigating, or was briefly too busy, and the
        next read sees the page it moved to.  ``eval_failed`` and ``malformed``
        are properties of the DOCUMENT — a second read returns the same answer
        and spends wall budget proving it, which is why
        :func:`app.observation_health.is_retryable` gates the attempt.  Retrying
        harder would be a search for a good read, and a crawler that searches
        until it gets an answer it likes is the thing this milestone exists to
        remove.

        WHY THE WHOLE CRAWL STOPS.  An inventory failure is almost never local:
        the injection is refused by a CSP that covers the app, or the browser has
        lost its context, or the page broke an intrinsic the walker needs. The
        remaining frontier would produce state after state of the same failure,
        each one recorded as an empty page. Terminating with a named reason gives
        the operator ONE diagnosis instead of forty empty states — and, crucially,
        the manifest written up to this point stays valid and resumable, so the
        crawl can be continued once the cause is fixed (T-GW-03).
        """
        detail = obs.health_detail()
        self._inventory_failures += 1
        if observation_health.is_retryable(obs.inventory_status):
            logger.warning(
                "qec.crawler.inventory_retry status=%s depth=%d url=%s — the page "
                "moved or stalled under the read; re-reading once",
                obs.inventory_status, item.depth, (obs.url or "")[:120])
            await self._politeness_delay()
            retry = await self._observe()
            if retry.inventory_ok:
                self._inventory_failures -= 1
                # Install the healthy observation IN PLACE so the caller's local
                # ``obs`` is the one that was actually read.  A retry whose result
                # went somewhere the caller could not see would be a second,
                # quieter version of the bug this closes.
                obs.__dict__.update(retry.__dict__)
                logger.info("qec.crawler.inventory_recovered depth=%d url=%s",
                            item.depth, (retry.url or "")[:120])
                return True
            detail = retry.health_detail()

        self._stop_reason = STOP_INVENTORY_FAILED
        self._hard_stop = True
        self._inventory_failure_detail = detail
        logger.error(
            "qec.crawler.inventory_failed_terminal depth=%d detail=%s — the crawl "
            "is stopping as FAILED rather than recording an unobserved page as "
            "an empty one", item.depth, detail[:300])
        # The failure goes in the MANIFEST, not only the log: a crawl that ended
        # this way must be explainable from its own durable evidence by someone
        # who does not have the container's stderr.
        self._emitter.emit_guard_event(
            kind="inventory_failed", method="GET",
            url=(obs.url or item.url), rule_id=obs.inventory_status,
            severity="fatal", reason=detail[:500],
            phase=self._guard.phase.value,
        )
        return False

    async def _collect_opaque_now(self) -> list[dict[str, Any]]:
        """The DOM-unreadable surfaces on the page as it stands.  Best-effort.

        Called ONCE per state, before the fingerprint is taken, because two
        consumers need the same answer: vision-aware identity (T-VIS-02) and the
        coverage ledger.  Evaluating twice could return two different pictures of
        one state, and the digest would then have been taken from a page the
        ledger does not describe.
        """
        collect = getattr(self._port, "collect_opaque", None)
        if collect is None:
            return []
        try:
            return list(await collect() or [])
        except Exception:
            return []

    def _note_vision_result(self, result: Any, *, url: str) -> None:
        """Fold ONE state's vision outcome into the crawl-level ledger.

        BOTH HALVES ARE RECORDED.  The verified controls are already in the
        inventory; what this adds is the REFUSED half — the perceptions the model
        offered and the page did not confirm. A wrong perception that leaves no
        trace is indistinguishable from a perception that never happened, and
        that difference is the only way an operator can learn to distrust a
        model. Refused rows live in the vision ledger and in NOTHING the
        catalogue reads.
        """
        if result is None:
            return
        row = result.as_ledger()
        row["url"] = (url or "")[:2000]
        self._vision_ledger.append(row)
        if result.perceived and not result.promoted:
            # Named in the OPAQUE ledger too, so the coverage report says this
            # page was seen, perceived, and not proven — never silently clean.
            # ``vision_perceived`` — an OBSERVED kind, so the row reads as
            # evidence rather than as a blind spot, and a later escalation
            # decision never re-routes it as an unread surface.
            self._opaque_surfaces.append({
                "kind": "vision_perceived",
                "label": "%d perceived control(s), none R0-verified" % result.perceived,
                "reason": ("vision proposed controls on a DOM-opaque page and no "
                           "coordinate action could be verified — nothing was "
                           "catalogued from this perception"),
            })

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
        # ── M1.7 / T-GW-01 · THE EVIDENCE GATE ───────────────────────────────
        # An inventory read that FAILED is not a page with no controls, and this
        # is the last point at which the two are still distinguishable. Below
        # this line the observation becomes a fingerprint, a recorded state and a
        # coverage claim; a failed read admitted here is a page the crawl never
        # saw being reported as a page the crawl covered.
        if not obs.inventory_ok and not await self._recover_inventory(obs, item):
            return
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
            wall_fp = self._fingerprinter.fingerprint(
                url=obs.url, controls=controls, dialogs=obs.dialog_flags,
                page_token=obs.page_token, observation_ok=obs.inventory_ok)
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
        # ── M2.6 / T-CAP-03 · OPEN THE DOORS BEFORE CATALOGUING THE ROOM ────
        # A field inside a collapsed accordion is not on the page, so capture
        # correctly refuses to catalogue it — and the question the application
        # asks there was never recorded by any crawl of it. Open what the DOM
        # itself declares to be shut, then read again. Runs HERE, before the
        # identity is taken, so the state that is fingerprinted, screenshotted
        # and recorded is the state that was actually catalogued.
        expansion_actions, controls, obs = await self._expand_disclosures(
            item, controls, obs)

        # ── M3.1 / T-VIS-02 · VISION-AWARE STATE IDENTITY ───────────────────
        # Rung 4 of the identity ladder (the perceptual hash) has existed since
        # M1.1 and only the WALK ever supplied one. Discovery — the path that
        # records states and feeds the catalogue — passed none, so a canvas
        # application whose screens share one URL and one (empty) DOM collapsed
        # to a SINGLE fingerprint and every screen after the first was dropped by
        # the `_visited_fingerprints` dedup below.
        #
        # The hash is admitted on ONE condition: this state is DOM-opaque and
        # DOM-sparse, i.e. exactly the states `should_perceive` escalates. A page
        # the DOM explains is fingerprinted byte-for-byte as it always was, so no
        # historical identity moves and a cosmetic repaint on an ordinary page
        # still cannot fragment its state.
        opaque_here = await self._collect_opaque_now()
        state_phash = ""
        if opaque_here and perception.should_perceive(controls, opaque_here):
            state_phash = perception.perceptual_hash_png(
                await self._port.screenshot_png() or b"")
            if state_phash:
                logger.info(
                    "qec.vision.identity_phash url=%s phash=%s — the DOM cannot "
                    "tell this state apart, so the pixels are admitted",
                    (obs.url or "")[:120], state_phash[:16])
        # M1.5 / T-ND-04 — the identity is computed from the page the
        # observation was actually READ from. When a popup was adopted mid-visit
        # this is the popup, not the page the goto landed on.
        fingerprint = self._fingerprinter.fingerprint(
            url=obs.url, controls=controls, dialogs=obs.dialog_flags,
            perceptual_hash=state_phash,
            page_token=obs.page_token, observation_ok=obs.inventory_ok)

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
        # THE OPENING IS PART OF THE STATE, not a prelude to it. A catalogue
        # entry for a field that only exists once a section is open is
        # unbindable at replay unless the run that binds it opens the section
        # first — the exact capture-says-covered / replay-cannot-bind shape the
        # browser harness exists to catch. These are stamped with the identity
        # of the state they produced and recorded FIRST, so the generated flow
        # opens before it fills.
        if expansion_actions:
            for opened in expansion_actions:
                opened.state_id = fingerprint
            actions.extend(expansion_actions)
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
            # A4.3 / T-AC-01 — the SECOND producer that dropped exactly the
            # controls needing approval (`if fc.name and not fc.danger`). A form
            # whose submit is "Bind Coverage" contributed nothing at all, so the
            # operator was never shown the one control the crawl was waiting on.
            # Classified now, and routed to the list that matches its class.
            for fc in fill.flow_candidates:
                fc_name = str(getattr(fc, "name", "") or "").strip()
                if not fc_name:
                    continue
                fc_control = getattr(fc, "control", None)
                probe = dict(fc_control) if isinstance(fc_control, dict) else {}
                probe.setdefault("kind", "button")
                probe["name"] = fc_name
                probe["danger"] = bool(getattr(fc, "danger", False))
                probe["danger_rule_id"] = str(getattr(fc, "danger_rule_id", "") or "")
                probe["danger_severity"] = str(getattr(fc, "danger_severity", "") or "")
                klass = classify_boundary(probe)
                if klass.cls == BOUNDARY_APPROVABLE:
                    self._approvable_boundary.append({
                        "label": fc_name,
                        "url": obs.url or "",
                        "reason": klass.reason,
                        "rule_id": klass.rule_id,
                        "severity": klass.severity,
                        "boundary_key": boundary_key(obs.url or "", fc_name),
                    })
                elif klass.cls == BOUNDARY_SAFE:
                    self._submit_candidates.append(fc_name)
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
        #
        # A2.2 — ...BUT ONLY IF IT IS STILL THIS PAGE. The whole point of reading
        # LAST is that a discovery click can REVEAL an output in place, and that
        # revealed premium belongs to this state. A discovery click can equally
        # NAVIGATE, and then this read lands on a different document while
        # ``fingerprint`` still names the one we started on — so another page's
        # outputs are attributed to this state.
        #
        # Measured on the M2.4 quote funnel: `_discover` clicked "Get Quote", the
        # browser moved to /result.html, and the ENTRY state was recorded carrying
        # "Your monthly premium = 42.50" — a value that page never renders. The
        # fold then produced an entry node claiming the result node's outcome.
        # Invisible until A2.2 admitted outcome states into coverage, because
        # before that neither page reached the index at all.
        #
        # The same-document test is ``browser._norm_url``, deliberately: it is the
        # one R0 already uses to decide whether a click navigated, so "did we move"
        # gets one answer across the engine — cosmetic anchors and trailing slashes
        # do not count, an SPA hash-route change does.
        displayed_values = await self._port.collect_displayed_values()
        if displayed_values:
            url_now = await self._port.current_url()
            if _norm_url(url_now or "") != _norm_url(obs.url or ""):
                logger.info(
                    "qec.discovery.displayed_values_dropped from=%s to=%s count=%d "
                    "— discovery navigated away, so these values are another "
                    "page's and are not this state's evidence",
                    (obs.url or "")[:120], (url_now or "")[:120],
                    len(displayed_values))
                displayed_values = []
        # API/network mining — drain the XHR/fetch calls the app made during this
        # visit (diagnostics-only; the app's real API surface as grounded evidence).
        # Best-effort: a port without the verb yields nothing, never breaks a crawl.
        network_calls = await self._drain_network()
        # OPAQUE-SURFACE detection (best-effort): positively find DOM-unreadable surfaces on
        # this state so the coverage ledger names them, never a silent "clean" scan.
        # Collected ONCE, before the fingerprint was taken (T-VIS-02), and folded
        # into the ledger here so a single probe serves both identity and
        # coverage — a second evaluation could disagree with the first and would
        # make the ledger describe a state the digest was not taken from.
        if opaque_here:
            self._opaque_surfaces.extend(opaque_here)
        # ── M3.2 / T-FR-01 · THE FRAME LEDGER ───────────────────────────────
        # The port has already ENTERED the cross-origin frames on this page —
        # their controls are in the inventory above, catalogued on exactly the
        # same terms as a control beside them — and this is the account of that:
        # one row per frame met, entered or refused, with the reason.
        #
        # Without it, a crawl that read a payment iframe and a crawl that could
        # not address one produce identical coverage, and the surfaces this
        # milestone exists to open are indistinguishable from the ones still
        # shut. A refusal is recorded as loudly as an entry for the same reason
        # the opaque ledger exists at all.
        drain_frames = getattr(self._port, "drain_frame_evidence", None)
        if drain_frames is not None:
            try:
                frame_rows = list(await drain_frames() or [])
            except Exception:
                frame_rows = []
            for frame_row in frame_rows:
                entered = frame_row.get("status") == "entered"
                # The label carries the SELECTOR as well as the origin,
                # because the ledger dedupes on it: two embeds from one vendor
                # host are two surfaces, and labelling both with the host alone
                # collapses them into one row that reports half the truth.
                origin = str(frame_row.get("label") or "embedded frame")
                selector = str(frame_row.get("selector") or "")
                self._opaque_surfaces.append({
                    "kind": "frame_entered" if entered else "frame_not_entered",
                    "label": ("%s (%s)" % (origin, selector[:80])) if selector else origin,
                    "reason": str(frame_row.get("reason") or ""),
                })
            if frame_rows:
                logger.info(
                    "qec.explorer.frames_ledgered url=%s entered=%d refused=%d",
                    (obs.url or "")[:120],
                    sum(1 for r in frame_rows if r.get("status") == "entered"),
                    sum(1 for r in frame_rows if r.get("status") != "entered"))
        # ── M3.1 / T-VIS-01 · THE VISION ESCALATION LOOP ────────────────────
        # Until this milestone the block here perceived and STOPPED: it appended
        # a prose row to the opaque ledger and said so in its own comment
        # ("This records what vision SAW; it does NOT act on it"). Everything
        # after synthesis — the coordinate rung, R0, promotion — was built and
        # unreachable.
        #
        # It now runs the whole loop, and the law it enforces is that a vision
        # prediction is never catalogue truth: `run()` returns ONLY the controls
        # it clicked at a coordinate and then MEASURED the page responding to.
        # Those join `snapshot_controls`, from where they reach the catalogue by
        # the identical route a DOM control takes. Everything else is written to
        # the vision ledger as REFUSED and reaches nothing the catalogue reads.
        #
        # `act` is the crawl posture: an observe-only crawl perceives but never
        # actuates, and therefore promotes nothing — which is correct rather than
        # unfortunate, because nothing was verified.
        if self._vision is not None and opaque_here:
            vres = await self._vision.run(
                url=obs.url or "", controls=snapshot_controls,
                opaque_surfaces=opaque_here, act=not self._observe_only)
            self._note_vision_result(vres, url=obs.url or "")
            if vres.promoted:
                # THE ONLY PROMOTION PATH. Folded into the inventory the state is
                # recorded from, so `note_state_signals` -> coverage.states ->
                # qe-central's catalogue sees them exactly as it sees DOM
                # controls, carrying `capture_mode=vision` + `r0_verified=True`
                # so their provenance survives the crossing.
                snapshot_controls = list(snapshot_controls) + list(vres.promoted)
                logger.info(
                    "qec.vision.promoted url=%s verified=%d refused=%d — only the "
                    "verified controls entered the state inventory",
                    (obs.url or "")[:120], vres.verified, vres.refused)
            if vres.outcomes:
                displayed_values = list(displayed_values or []) + list(vres.outcomes)
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
                # The unblock experiment MUST re-read the page — that re-read is
                # how the application renders its verdict — and the fresh
                # inventory it returns is silent about everything the DOM never
                # said. Carry the findings the earlier passes EARNED across it,
                # or a dependency proved sixty lines ago dies here and the
                # catalogue reports a conditional question as unconditional.
                snapshot_controls = carry_earned_annotations(
                    snapshot_controls,
                    list(await self._answer_to_unblock(
                        snapshot_controls, blocked_label, obs.url or "", fill)))
        # ── R9 · A FORMLESS STEP MUST STILL SAY WHY IT STOPPED ──────────────
        # The legibility gate above is behind ``is_form``, and ``is_form``
        # requires a FILLABLE control. A step that asks its question with cards
        # alone therefore has no fill, never reaches `_note_advance_blocked`, and
        # stops in SILENCE — the exact failure R9 measured on vkpower-life's
        # payment step:
        #
        #     controls_total 14 | question_groups [] | form_snapshot_signals {}
        #     advance_blocked for this url: NONE
        #
        # while the walker's own verdict line for that page read
        # ``Continue to Beneficiary : dis=True dang=False adv=True`` — it could
        # see the disabled advance the whole time and had nowhere to record it.
        #
        # So the block is NAMED here whether or not there was a form to blame it
        # on, and only then is the card grid tried. Naming comes first on purpose:
        # if the picker cannot handle the step, the run still says which control
        # the application disabled instead of reporting a clean stop.
        if not is_form:
            formless_blocked = self._note_advance_blocked(
                snapshot_controls, obs.url, None)
            if formless_blocked:
                logger.info(
                    "qec.wizard.formless_step url=%s controls=%d blocked=%r "
                    "— a step with no fillable control still has a forward "
                    "control the app disabled; naming it before trying to answer",
                    (obs.url or "")[:120], len(snapshot_controls or ()),
                    formless_blocked[:40])
            if formless_blocked and await self._pick_card_to_unblock(
                    snapshot_controls, formless_blocked, obs.url or ""):
                # The APP re-enabled its own forward control. Re-read the step so
                # the walk advances from what the page is now, not from the
                # snapshot taken before the question was answered.
                reobs_card = await self._observe()
                snapshot_controls = carry_earned_annotations(
                    snapshot_controls,
                    build_inventory(reobs_card.raw_controls, self._refuse_pack,
                                    url=reobs_card.url))

        # A2.2 — A STEP WHOSE ONLY ANSWER IS A BUTTON IS STILL A STEP.
        #
        # ``is_form`` requires a FILLABLE control, so a page that asks its question
        # with buttons alone (a "Get Quote" funnel, a Yes/No triage step, most
        # SPA wizards) was never a form, never had a ``fill``, and could never open
        # this gate. It fell to the ``not is_form`` fork branch below and was
        # recorded as a one-step decision — so an application the crawl DID actuate
        # end to end reported ``forms_found=0``, ``flows=0``, ``journeys_completed=0``.
        # That is the A22 blocker, and it is not a property of the application.
        #
        # WHY OPENING THE GATE IS SAFE, stated as a property of the code and not as
        # an intention. ``_walk_wizard`` never took ``fill`` — it picks its advance
        # control out of ``controls`` and every gate that protects a click lives
        # INSIDE it: the danger gate, the approved-submit-name exclusion, Tier 1's
        # per-control commit veto, Tier 2's destination-only rule, the commit filter
        # applied before Tier 3 sees a candidate, and the fail-closed advance rule
        # (a real observed effect AND a new unseen state). Nothing here weakens any
        # of them; this only stops refusing to ASK.
        #
        # AND IT CANNOT LOSE A RECORDING. ``walked`` is what suppresses the two
        # fallback branches below. If the walk declines — ``_pick_advance`` returns
        # no control, which is what a hub full of links does — ``walked`` stays
        # False and the ``not is_form`` fork branch records exactly what it recorded
        # before. The change is therefore strictly additive: it can turn "no journey"
        # into "a walked journey", never the reverse.
        #
        # Scoped to pages holding a BUTTON. A page whose only controls are links is
        # discovery's job (``_enqueue_link_hrefs``), and opening the gate for it
        # would consult the advance tiers — including the Tier-3 oracle — on every
        # hub in the crawl, which is a cost decision nobody asked for.
        bare_button_step = (
            not is_form
            and any(c.get("kind") == "button" for c in snapshot_controls)
        )
        if self._wizard_enabled and (
            (is_form and fill is not None
             and (fill.filled or fill.has_unanswered_decisions))
            or bare_button_step
        ):
            entry_pick = await self._pick_advance(
                snapshot_controls, obs.url, obs.title, fingerprint)
            logger.info("qec.wizard.gate_open url=%s filled=%s bare_button=%s pick=%r",
                        obs.url,
                        fill.filled if fill is not None else "n/a",
                        bare_button_step,
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
        # M1.4 · WHICH flow (if any) THIS call site owns.
        #
        # A crossing performed below must be attached to the journey that
        # produced it, and only the site that BUILT that journey knows which one
        # it is. The walker has done this since A4.3; this path never did, so a
        # single-page form that crossed an approved boundary and landed on a
        # confirmation recorded the milestone, dropped it before ``build_flow``,
        # and reported ``journey_completed=false`` on a journey the crawl had
        # watched complete. ``None`` means "this state produced no flow of its
        # own" — the walk owns it, or there was nothing to record — and nothing
        # is linked.
        owned_flow_index: Optional[int] = None
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
                    if is_boundary_outcome(v)],
                max_steps=self._max_wizard_steps))
            owned_flow_index = len(self._flows) - 1
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
                        if is_boundary_outcome(v)],
                    max_steps=self._max_wizard_steps))
                owned_flow_index = len(self._flows) - 1
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
        milestones_before = len(self._outcome_milestones)
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
        # THE JOURNEY IS REBUILT, NOT PATCHED — the same helper, and therefore
        # the same single derivation of ``journey_completed``, the walk uses. A
        # no-op unless this state owned a flow AND a crossing actually minted a
        # milestone, so a refused or already-spent boundary changes nothing.
        if owned_flow_index is not None:
            self._link_crossing_to_flow(owned_flow_index, milestones_before)

        # M2.6 / T-CAP-03 - the other half of expansion. The additive pass above
        # deliberately refuses to click a tab, because a tab panel is not more of
        # this page. It is a DIFFERENT page, and this is where it gets recorded
        # as one. Last, because it navigates: everything this visit owns has
        # already been recorded by the time it runs.
        if not walked:
            await self._tab_views(item, snapshot_controls, fingerprint)

    # -- M2.6 / T-CAP-03 . deliberate expansion ----------------------------

    def _collapsed_disclosures(
        self, controls: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """The controls on this page that the DOM ITSELF declares to be shut
        doors - in document order.

        DECIDED FROM CAPTURED EVIDENCE, NEVER FROM A GUESS. ``qec.disclosure``
        is capture's normalisation of three explicit declarations (a closed
        ``<details>``, ``aria-expanded="false"``, an unselected ``role=tab``);
        nothing is inferred from a class name, a chevron glyph or the shape of
        the DOM. That is the whole difference between an expansion pass and
        blind clicking: a page that declares nothing is left completely alone,
        and this returns an empty list for the overwhelming majority of states.

        Excluded even when collapsed:

          * DANGER and DISABLED controls - the refuse-pack gate is never
            relaxed to see more of a page;
          * MENU openers (``aria-haspopup``, ``role=menuitem``) - a fly-out is
            navigation, owned by :meth:`_menu_reveal`, and opening one here
            would fold the nav bar into this state's own control set;
          * ADVANCE and COMMIT labels - a "Next" or a "Submit" that happens to
            carry ``aria-expanded`` is a step in the flow, not a section of this
            page, and the expansion pass must never be the thing that advances
            or submits;
          * an operator-approved submit name, and a sign-out;
          * nameless controls - no name means no label to veto on and nothing to
            put in the evidence record; fail closed.
        """
        out: list[dict[str, Any]] = []
        for c in controls:
            q = c.get("qec") or {}
            if str(q.get("disclosure") or "").strip().lower() != "collapsed":
                continue
            if c.get("kind") not in _ACTUATOR_KINDS:
                continue
            if c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name.lower() in self._submit_approvals:
                continue
            if str(q.get("haspopup") or "").strip():
                continue
            if str(q.get("role") or "").strip().lower() in ("menuitem", "menu"):
                continue
            if str(q.get("role") or "").strip().lower() == "tab":
                # A TAB IS DECLARED MUTUALLY EXCLUSIVE, so there is nothing to
                # discover by clicking it: selecting one panel deselects
                # another, and its controls and the current panel's are never on
                # screen together. Merging them would catalogue a page that has
                # never existed; and unlike a disclosure a tab does not toggle,
                # so clicking it again does not put the page back - the pass
                # would have to abandon the visit to undo a click it could have
                # known not to make. Measured on fixture 22: attempting one cost
                # a click, a re-read, a failed undo and every expansion already
                # earned on that page. Recorded, not silently dropped: see
                # `_tab_views`, which gives each panel its own state.
                continue
            if _WIZARD_COMMIT_RE.search(name) or _WIZARD_ADVANCE_RE.search(name):
                continue
            if _AUTH_SESSION_RE.search(name):
                continue
            out.append(c)
        return out

    @staticmethod
    def _control_keys(
        controls: Sequence[Mapping[str, Any]],
    ) -> set[tuple[str, str, str]]:
        """A set identity for a captured control list - enough to answer "did
        this page keep everything it had and gain more?", and nothing else.

        Deliberately (kind, name, css_hint) and not the whole record: a control
        whose disclosure flag flipped from ``collapsed`` to ``expanded`` is the
        SAME control, and comparing full records would read every successful
        expansion as a loss.
        """
        keys: set[tuple[str, str, str]] = set()
        for c in controls or ():
            q = c.get("qec") or {}
            keys.add((str(c.get("kind") or ""),
                      str(c.get("name") or "").strip().lower(),
                      str(q.get("css_hint") or "")))
        return keys

    async def _read_controls(self, url: str) -> list[dict[str, Any]]:
        """A control-only re-read of the live page (no screenshot, no dialog or
        network drain): the cheapest observation that can answer whether an
        expansion revealed anything."""
        raw = await self._port.collect_controls()
        self._tracker.note_request()
        return build_inventory(raw, self._refuse_pack, url=url)

    async def _expand_disclosures(
        self, item: FrontierItem, controls: list[dict[str, Any]], obs: Any,
    ) -> tuple[list[emit.ActionRecord], list[dict[str, Any]], Any]:
        """Open the collapsed sections of this page, then re-read it.

        Returns ``(actions, controls, observation)`` - the grounded opens that
        were KEPT, and the control list / observation the state should be
        catalogued from. On any page with nothing collapsed this is a pure
        pass-through that costs one list comprehension and no browser round
        trip at all.

        THE ACCEPTANCE TEST FOR EACH CLICK IS EVIDENCE, NOT INTENT. After every
        open the page is re-read and the new control set must be a strict
        SUPERSET of the one before it. That single rule is what keeps this from
        degenerating into clicking things and hoping:

          * a click that revealed nothing is not recorded - it is not evidence
            of anything, and recording it would put a step into the generated
            flow that does nothing;
          * a click that revealed something while HIDING something else did not
            open a door, it turned a dial. A tab strip is the common case: the
            page after it is a different, equally real state, but folding it
            into this one would catalogue a page that never existed - the
            controls of two panels that are never on screen together. It is
            undone (the same control, clicked again) and skipped;
          * a click that LEFT THE PAGE was never a disclosure at all. The visit
            is restarted from the entry URL and the whole pass is abandoned, so
            a mislabelled control costs one navigation and never a wrong
            catalogue.

        Bounded by :data:`_MAX_EXPANSIONS` in document order - a stable prefix,
        not a sample - and what was left shut is logged rather than implied.
        """
        collapsed = self._collapsed_disclosures(controls)
        if not collapsed:
            return [], controls, obs
        if len(collapsed) > _MAX_EXPANSIONS:
            logger.info(
                "qec.crawler.expansion_bounded url=%s collapsed=%d opening=%d - "
                "the rest stay shut and their fields stay uncatalogued",
                (obs.url or "")[:120], len(collapsed), _MAX_EXPANSIONS)
        targets = collapsed[:_MAX_EXPANSIONS]

        kept: list[emit.ActionRecord] = []
        before = self._control_keys(controls)
        opened = skipped = 0
        for control in targets:
            if self._tracker.stop_reason() or self._cancelled:
                break
            await self._politeness_delay()
            observation = await self._port.click(control)
            self._tracker.note_request()
            action = emit.build_action_record(
                dict(control), verb="click", value=None, observation=observation,
                phase=Phase.EXPLORE.value, state_id="",
                timestamp_ms=self._clock.now_ms(),
            )
            self._tracker.note_action()
            after = action.after or {}
            if after.get("navigated"):
                logger.info(
                    "qec.crawler.expansion_navigated url=%s control=%r - a "
                    "control that declared itself a disclosure left the page; "
                    "the visit is restarted and nothing is expanded",
                    (obs.url or "")[:120], str(control.get("name") or "")[:60])
                self._expansions_skipped += skipped + 1
                return await self._expansion_restart(item, controls, obs)
            outcome = str(after.get("outcome") or "")
            if outcome in ("none", "error"):
                # `none` - the click landed and the page did not move.
                # `error` - the click never landed at all (an unresolvable
                # handle, an action timeout). Neither opened anything, and
                # neither left the page changed, so there is nothing to undo and
                # nothing to record. Distinguished in the log because they call
                # for different work: `none` is a page that lied about being a
                # disclosure, `error` is a control this engine could not reach.
                skipped += 1
                if outcome == "error":
                    logger.info(
                        "qec.crawler.expansion_unreachable url=%s control=%r "
                        "detail=%s - the section stays shut and its fields stay "
                        "uncatalogued", (obs.url or "")[:120],
                        str(control.get("name") or "")[:60],
                        str(after.get("detail") or "")[:120])
                continue
            now_keys = self._control_keys(await self._read_controls(obs.url or ""))
            if not (now_keys > before):
                # Not a door: a dial. Put it back the way it was.
                skipped += 1
                logger.info(
                    "qec.crawler.expansion_not_additive url=%s control=%r "
                    "gained=%d lost=%d - this view is a DIFFERENT state, not "
                    "more of this one; it is restored, not merged",
                    (obs.url or "")[:120], str(control.get("name") or "")[:60],
                    len(now_keys - before), len(before - now_keys))
                await self._port.click(control)
                self._tracker.note_request()
                restored = self._control_keys(
                    await self._read_controls(obs.url or ""))
                if restored != before:
                    logger.info(
                        "qec.crawler.expansion_undo_failed url=%s opened=%d - "
                        "restarting the visit rather than cataloguing a page "
                        "nobody saw; the %d section(s) already opened are given "
                        "up with it, because after the restart they are shut "
                        "again and claiming them would be a fabrication",
                        (obs.url or "")[:120], opened, opened)
                    self._expansions_skipped += skipped
                    return await self._expansion_restart(item, controls, obs)
                continue
            kept.append(action)
            before = now_keys
            opened += 1

        # COUNTED BEFORE THE EARLY RETURN. A page where every declared
        # disclosure was unreachable used to report `skipped=0` because the
        # counters were only updated on the success path - which is precisely
        # the invisible failure they exist to make visible.
        self._expansions_skipped += skipped
        if not kept:
            return [], controls, obs
        # One full observation now that the page is open - the state is
        # catalogued, fingerprinted and screenshotted from THIS.
        final = await self._observe()
        if not final.inventory_ok or not self._in_scope(final.url):
            logger.info(
                "qec.crawler.expansion_reread_failed url=%s - the page was "
                "opened but could not be re-read; the unexpanded capture stands",
                (obs.url or "")[:120])
            return [], controls, obs
        expanded = build_inventory(final.raw_controls, self._refuse_pack,
                                   url=final.url)
        self._expansions_opened += opened
        logger.info(
            "qec.crawler.expanded url=%s opened=%d skipped=%d controls=%d->%d - "
            "fields behind a collapsed section are catalogued, and the opens "
            "that revealed them are recorded so a replay can reach them",
            (final.url or "")[:120], opened, skipped, len(controls), len(expanded))
        return kept, expanded, final

    async def _expansion_restart(
        self, item: FrontierItem, controls: list[dict[str, Any]], obs: Any,
    ) -> tuple[list[emit.ActionRecord], list[dict[str, Any]], Any]:
        """Abandon the expansion pass and re-read the entry state.

        The page is in a shape this visit did not intend, so nothing about it is
        kept: no actions, and the catalogue comes from a fresh read. If even
        that read fails, the pre-expansion observation stands - degrading to
        "no expansion" is always available, degrading to a wrong catalogue is
        not.
        """
        await self._goto_keeping_login(item.url)
        fresh = await self._observe()
        if fresh.inventory_ok and self._in_scope(fresh.url):
            return [], build_inventory(fresh.raw_controls, self._refuse_pack,
                                       url=fresh.url), fresh
        return [], controls, obs

    def _unselected_tabs(
        self, controls: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """The tabs of this page whose panel is not the one on screen."""
        out: list[dict[str, Any]] = []
        for c in controls:
            q = c.get("qec") or {}
            if str(q.get("role") or "").strip().lower() != "tab":
                continue
            if str(q.get("disclosure") or "").strip().lower() != "collapsed":
                continue
            if c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name.lower() in self._submit_approvals:
                continue
            if _WIZARD_COMMIT_RE.search(name) or _AUTH_SESSION_RE.search(name):
                continue
            out.append(c)
        return out

    @staticmethod
    def _same_control(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
        """Is this the same control, seen in a later capture of the same page?"""
        qa, qb = (a.get("qec") or {}), (b.get("qec") or {})
        return (str(a.get("kind") or "") == str(b.get("kind") or "")
                and str(a.get("name") or "").strip().lower()
                == str(b.get("name") or "").strip().lower()
                and str(qa.get("css_hint") or "") == str(qb.get("css_hint") or ""))

    def _view_navigation_candidates(
        self, controls: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Named, safe, enabled BUTTONS that switch the view — the section list
        of an application that navigates without URLs.

        Pure and separately testable. Danger controls are excluded outright:
        this pass exists to find sections, and it is not an authorisation to
        cross anything. Links are excluded because discovery already follows
        their hrefs; sweeping them again would only re-walk the frontier.
        """
        out: list[dict[str, Any]] = []
        for c in controls:
            if str(c.get("kind") or "").lower() != "button":
                continue
            if c.get("danger") or c.get("disabled"):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            # A WIZARD ADVANCE IS NOT A SECTION. "Next" / "Continue" / "Save and
            # continue" belong to the walk, which has its own tiers, its own
            # evidence and its own kill switch -- clicking them here would drive
            # a funnel from a pass that is only meant to enumerate the
            # application's sections, and would do it even with the walk
            # switched off.
            if ADVANCE_RE.search(name) or COMMIT_RE.search(name):
                continue
            out.append(c)
        return out

    async def _sweep_view_navigation(self) -> int:
        """Visit each section of an application that navigates WITHOUT URLs.

        WHY THIS EXISTS. Discovery expands the frontier from link HREFS
        (:meth:`_enqueue_link_hrefs`), which is the right and cheap thing to do
        for an application with routes. An application whose navigation is
        buttons calling a client-side view switcher offers discovery nothing to
        enqueue, so the frontier drains after the entry and the crawl ends
        having seen one screen.

        MEASURED, after authentication, on 2026-08-27:

            summit-life-carrier   14 link destinations, 3 buttons  -> 14 pages
            LifeOps (client)       0 link destinations, 22 buttons ->  2 pages

        LifeOps runs quote, apply, underwrite, issue, service and claim at a
        single address. Its 12 sections were unreachable by construction, not by
        budget — the crawl was not blocked and did not fail, it had nothing left
        to follow.

        Runs ONLY once the frontier is exhausted, so an application with routes
        pays nothing for it. The navigation is persistent site chrome, so each
        section is entered from wherever the previous one left the page — there
        is no base to return to and none is needed. Bounded by
        :data:`_MAX_VIEW_SECTIONS`, by the crawl budget, and by the fingerprint
        dedup: a control that switches to a view already recorded costs one
        click and adds no state.
        """
        obs = await self._observe()
        if not obs.inventory_ok or not self._in_scope(obs.url):
            return 0
        base = build_inventory(obs.raw_controls, self._refuse_pack, url=obs.url)
        candidates = self._view_navigation_candidates(base)
        if len(candidates) < _MIN_VIEW_SECTIONS:
            return 0
        base_fp = self._fingerprinter.fingerprint(
            url=obs.url, controls=base, dialogs=obs.dialog_flags,
            page_token=obs.page_token, observation_ok=obs.inventory_ok)
        if len(candidates) > _MAX_VIEW_SECTIONS:
            logger.info(
                "qec.crawler.view_sweep_bounded url=%s sections=%d visiting=%d - "
                "the rest are not visited and their fields stay uncatalogued",
                (obs.url or "")[:120], len(candidates), _MAX_VIEW_SECTIONS)
        recorded = 0
        parent = base_fp
        for control in candidates[:_MAX_VIEW_SECTIONS]:
            if self._tracker.stop_reason() or self._cancelled or self._hard_stop:
                break
            label = str(control.get("name") or "").strip()
            live = self._relocate(control, base) if hasattr(self, "_relocate") else control
            observation = await self._port.click(dict(live or control))
            action = emit.build_action_record(
                dict(live or control), verb="click", value=None,
                observation=observation, phase=Phase.EXPLORE.value, state_id="",
                timestamp_ms=self._clock.now_ms(),
            )
            self._tracker.note_action()
            after = action.after or {}
            if str(after.get("outcome") or "") in ("none", "error"):
                continue
            view_obs = await self._observe()
            if not view_obs.inventory_ok or not self._in_scope(view_obs.url):
                continue
            view = build_inventory(view_obs.raw_controls, self._refuse_pack,
                                   url=view_obs.url)
            view_fp = self._fingerprinter.fingerprint(
                url=view_obs.url, controls=view, dialogs=view_obs.dialog_flags,
                page_token=view_obs.page_token,
                observation_ok=view_obs.inventory_ok)
            if view_fp in self._visited_fingerprints:
                continue
            self._visited_fingerprints.add(view_fp)
            # FILL THE SECTION'S OWN FORM, exactly as the expansion pass does.
            #
            # Recording a section without filling it catalogues controls and
            # tests nothing. Measured on LifeOps: the sweep found 16 pages
            # carrying 23 distinct fields and the crawl filled ONE of them (4%)
            # — the persona selector — because the fill pass runs only on the
            # expansion path and these views are reached from here. Same engine,
            # same answer key, same ledger; the fields simply had nobody asking.
            step_actions = [action]
            if not self._observe_only and any(
                    (c.get("kind") in _FILLABLE_KINDS) and not _is_password(c)
                    for c in view):
                self._forms_found += 1
                fill = await fill_form_phase_a(
                    self._port, view, self._answer_key or AnswerKey(), self._clock,
                    phase=Phase.EXPLORE.value, state_id=view_fp,
                    identity=self._identity, recalled=self._recalled_values,
                    journey_values=self._journey_values,
                    priors=self._field_priors, data_mode=self._data_mode,
                    choice_overrides=self._choice_overrides,
                )
                step_actions.extend(fill.actions)
                self._tracker.note_action(len(fill.actions))
                self._fields_inferred.extend(fill.inferred_fields)
                self._open_choice_unverified += fill.open_choice_unverified
                self._note_fills_by_kind(fill.filled_by_kind)
                self._fields_unfilled.extend(fill.unfilled_fields)
                self._fields_seed_detail.extend(
                    {"label": lbl, "url": view_obs.url or ""}
                    for lbl in fill.unfilled_fields)
                self._collect_ledger(fill.field_ledger, view_obs.url or "")
                # Re-read: the fill may have revealed dependent questions, and
                # the recorded state must be the page as it stands after it.
                after_fill = await self._observe()
                if after_fill.inventory_ok:
                    view = list(build_inventory(after_fill.raw_controls,
                                                self._refuse_pack,
                                                url=after_fill.url))
                    view_obs = after_fill
            now = self._clock.now_ms()
            action.state_id = view_fp
            if parent and parent != view_fp:
                self._emitter.emit_edge(from_state=parent, to_state=view_fp,
                                        verb="click", target_label=label)
            for act in step_actions:
                act.state_id = view_fp
            self._record_state(
                url=view_obs.url, title=view_obs.title, controls=view,
                fingerprint=view_fp, actions=step_actions,
                screenshots=[(await self._port.screenshot_png(), now)],
                first_seen_ms=now, last_seen_ms=self._clock.now_ms(),
            )
            parent = view_fp
            recorded += 1
        if recorded:
            logger.info(
                "qec.crawler.view_sweep recorded=%d of %d section(s) - an "
                "application that navigates without URLs, enumerated by its own "
                "navigation rather than by hrefs it does not have",
                recorded, min(len(candidates), _MAX_VIEW_SECTIONS))
        return recorded

    async def _tab_views(
        self, item: FrontierItem, controls: Sequence[dict[str, Any]],
        parent_fingerprint: str,
    ) -> None:
        """Record each unselected tab panel as the state it actually is.

        WHY THIS IS NOT PART OF THE EXPANSION PASS. Opening an accordion gives
        you a page with MORE on it; selecting a tab gives you a DIFFERENT page.
        Folding the second into the first would put the controls of two panels
        that are never on screen together into one catalogued state - a page no
        user of the application has ever seen - and the generated script that
        binds to it would be unrunnable by construction. So the panels are not
        merged; each is fingerprinted, screenshotted and recorded on its own
        terms, reached by a GROUNDED click that is recorded with it. The
        catalogue gains the questions behind every tab, and every one of them is
        attached to a state where it really is on screen.

        Each view is entered from a FRESH LOAD rather than from wherever the
        visit happened to leave the page: this runs after navigation discovery,
        which deliberately clicks its way around, and a tab selected on top of an
        unknown page state is not evidence of anything. The additive pass runs
        again inside each view, so a collapsed section INSIDE a tab is opened
        too.

        Bounded by :data:`_MAX_TAB_VIEWS`, by the crawl's own budget, and by the
        fingerprint dedup - a tab whose panel is identical to one already
        recorded costs one click and adds no state.
        """
        tabs = self._unselected_tabs(controls)
        if not tabs:
            return
        if len(tabs) > _MAX_TAB_VIEWS:
            logger.info(
                "qec.crawler.tab_views_bounded url=%s tabs=%d recording=%d - the "
                "rest are not visited and their fields stay uncatalogued",
                (item.url or "")[:120], len(tabs), _MAX_TAB_VIEWS)
        recorded = 0
        for tab in tabs[:_MAX_TAB_VIEWS]:
            if self._tracker.stop_reason() or self._cancelled or self._hard_stop:
                break
            label = str(tab.get("name") or "").strip()
            await self._politeness_delay()
            await self._goto_keeping_login(item.url)
            base_obs = await self._observe()
            if not base_obs.inventory_ok or not self._in_scope(base_obs.url):
                logger.info(
                    "qec.crawler.tab_view_unreadable url=%s tab=%r - the reload "
                    "could not be read; the panel stays uncatalogued",
                    (item.url or "")[:120], label[:60])
                continue
            base = build_inventory(base_obs.raw_controls, self._refuse_pack,
                                   url=base_obs.url)
            live = next((c for c in base if self._same_control(c, tab)), None)
            if live is None:
                continue          # the page no longer offers it; nothing to say
            observation = await self._port.click(live)
            self._tracker.note_request()
            action = emit.build_action_record(
                dict(live), verb="click", value=None, observation=observation,
                phase=Phase.EXPLORE.value, state_id="",
                timestamp_ms=self._clock.now_ms(),
            )
            self._tracker.note_action()
            after = action.after or {}
            outcome = str(after.get("outcome") or "")
            if outcome in ("none", "error") or after.get("navigated"):
                # Nothing happened, the click never landed, or it turned out to
                # be a link. None of the three is a panel this crawl can show.
                logger.info(
                    "qec.crawler.tab_view_skipped url=%s tab=%r outcome=%s",
                    (item.url or "")[:120], label[:60], outcome or "navigated")
                continue
            view_obs = await self._observe()
            if not view_obs.inventory_ok or not self._in_scope(view_obs.url):
                continue
            view = build_inventory(view_obs.raw_controls, self._refuse_pack,
                                   url=view_obs.url)
            # A collapsed section INSIDE the panel is still a shut door.
            opened, view, view_obs = await self._expand_disclosures(
                item, view, view_obs)
            view_fp = self._fingerprinter.fingerprint(
                url=view_obs.url, controls=view, dialogs=view_obs.dialog_flags,
                page_token=view_obs.page_token,
                observation_ok=view_obs.inventory_ok)
            if view_fp in self._visited_fingerprints:
                continue
            self._visited_fingerprints.add(view_fp)
            now = self._clock.now_ms()
            for act in [action, *opened]:
                act.state_id = view_fp
            if parent_fingerprint and parent_fingerprint != view_fp:
                self._emitter.emit_edge(
                    from_state=parent_fingerprint, to_state=view_fp,
                    verb="click", target_label=label)
            self._record_state(
                url=view_obs.url, title=view_obs.title, controls=view,
                fingerprint=view_fp, actions=[action, *opened],
                screenshots=[(await self._port.screenshot_png(), now)],
                first_seen_ms=now, last_seen_ms=self._clock.now_ms(),
            )
            recorded += 1
            self._tab_views_recorded += 1
        if recorded:
            logger.info(
                "qec.crawler.tab_views url=%s recorded=%d of %d - each panel is "
                "its own state, reached by a recorded click, never merged into "
                "the page that offered it",
                (item.url or "")[:120], recorded, len(tabs))

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
            # Whether this application gave discovery ANYTHING to follow. The
            # section sweep runs only when the answer is no — see
            # :meth:`_sweep_view_navigation`.
            self._link_hrefs_enqueued = getattr(self, "_link_hrefs_enqueued", 0) + 1

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
