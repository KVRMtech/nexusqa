"""Authentication as a CAPABILITY, not a phase (M0.3 / T-DE-10).

Extracted VERBATIM from :mod:`app.crawler`.

THE CENTRAL IDEA THIS CODE ENCODES.  A login wall is not something a crawl
passes once at the start; it is something it can meet at any depth, and the
right response is to answer it and KEEP WALKING rather than end the journey
there.  Hence :meth:`_goto_keeping_login` (navigate without losing the
session), :meth:`_reach_in_app` (get to a deep route the way a user would) and
:meth:`_cross_auth_wall` (answer a wall met mid-journey).

THE HONESTY RULES ARE THE POINT.  Every classifier here exists to keep the
crawl from lying about coverage:

  * a crawl with NO credentials that meets a gated entry STOPS
    (``STOP_AUTH_REQUIRED``) instead of filling synthetic data into a login
    form until the wall-clock budget expires and reporting that as a timeout;
  * a login that VERIFIABLY worked but did not persist is reported as
    ``not_persisted``, never as ``session_expired`` — the remediations are
    opposite, and one of them sends the operator after a proven-correct
    artefact;
  * the relogin budget is bounded so a login loop cannot masquerade as
    progress.

The relogin budget, every classifier threshold and every stop reason are
unchanged by the move.
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
# Identity comes from ``self._fingerprinter`` (app.state_identity), not the
# hasher — one authority for what a page state IS.
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


class AuthFlowMixin:
    """Mixed into :class:`app.crawler.Crawler` (T-DE-10)."""

    async def _maybe_authenticate(self) -> Optional[str]:
        """Run the login flow if credentials were supplied.

        Returns the post-login root URL to explore from, ``self.target_url`` when
        no credentials were supplied, or ``None`` when login could not be
        verified (an honest hard stop — the authenticated app is unreachable).
        """
        if not self._credentials:
            # An injected session needs no login driven — but it DOES need proving.
            # That happens in _note_login_wall_while_authenticated, which sees every
            # recorded state (the entry included) and so costs no extra navigation.
            # No credentials AND no session? A GATED entry (a redirect to a login wall)
            # is caught in the EXPLORE loop by _note_login_wall_without_credentials — on
            # the entry's own navigation, so it costs no extra goto and honest-stops
            # instead of filling the login form and looping it.
            return self.target_url

        self._guard.phase = Phase.AUTH
        self._guard.login_host = self._target_host
        nav = await self._goto_entry(self.target_url)
        if not nav.ok:
            self._stop_reason = STOP_AUTH_FAILED
            logger.warning("qec.crawler.auth_entry_unreachable error=%s", nav.error[:200])
            return None

        login_obs = await self._observe()
        login_png = await self._port.screenshot_png()
        login_ts = self._clock.now_ms()

        authenticator = Authenticator(
            self._port, self._credentials, self._clock, self._refuse_pack,
            self._guard.auth_window, max_relogins=self._max_relogins,
        )
        # Kept for the whole crawl — see _cross_auth_wall.
        self._authenticator = authenticator
        result = await authenticator.login(login_obs)
        self._tracker.note_action(len(result.actions))
        self._storage_state = result.storage_state

        login_controls = build_inventory(login_obs.raw_controls, self._refuse_pack,
                                          url=login_obs.url)
        self._record_state(
            url=login_obs.url, title=login_obs.title, controls=login_controls,
            fingerprint=result.before_fingerprint or self._fingerprinter.fingerprint(
                login_obs.url, login_controls, login_obs.dialog_flags),
            actions=result.actions, screenshots=[(login_png, login_ts)],
            last_seen_ms=self._clock.now_ms(),
        )

        if not result.success:
            # A GENUINE login wall — a password/OTP was submitted and login still
            # failed (wrong credentials or an unautomatable gate) — is an HONEST hard
            # stop: the authenticated app is unreachable, and the operator must fix the
            # credentials. But when NO login form was found or driven
            # (``secret_submitted`` is False) the entry is simply ACCESSIBLE public
            # content the operator pointed us at (credentials are configured for OTHER,
            # gated areas). Refusing to crawl it would throw away a real, reachable flow
            # for no reason — so explore it UNAUTHENTICATED and record a LOUD warning
            # that the authenticated areas were NOT covered (never a silent success).
            if result.secret_submitted:
                self._stop_reason = STOP_AUTH_FAILED
                logger.warning("qec.crawler.login_failed reason=%s", result.reason)
                return None
            self._auth_incomplete = True
            self._auth_incomplete_reason = result.reason
            logger.warning(
                "qec.crawler.auth_incomplete reason=%s — no login form driven at the "
                "entry; exploring UNAUTHENTICATED (authenticated areas NOT covered)",
                result.reason,
            )
            return await self._port.current_url()
        # A DRIVEN, verified login. Recorded so a wall met later can never be
        # mis-diagnosed as "the session expired — re-record it".
        self._login_verified = True
        return await self._port.current_url()

    def _raise_relogin_budget(self) -> None:
        """Let an app that needs a login PER PAGE actually be crawled.

        ``max_relogins`` defaults to 3 because a re-login normally means an expired
        session — a rare event. On an app that drops the login on every page load it is
        the per-navigation cost, so the budget runs out after two pages and the crawl
        stops finding anything. Sized to the state budget (plus headroom for discovery
        resets), and applied ONLY once such an app has been positively identified.
        """
        auth = self._authenticator
        if auth is None:
            return
        needed = max(int(self._budget.max_states) * 3, 60)
        if getattr(auth, "_max_relogins", 0) >= needed:
            return
        auth._max_relogins = needed
        logger.info("qec.crawler.relogin_budget_raised to=%d — this app signs in per "
                    "page load", needed)

    async def _goto_keeping_login(self, url: str) -> None:
        """Re-navigate, and RE-ESTABLISH the login if this app drops it on a page load.

        The crawl reloads a URL in several places — expanding the frontier, resetting the
        page between discovery clicks, re-establishing a wizard step. On an app that
        keeps the signed-in user in client-side state, EVERY one of those logs it out, so
        fixing only the auth-wall path left discovery running logged-out: it clicked 41
        times, found nothing to add, and the crawl reported 4 states and no journeys.
        This makes every reload survivable, so the rest of the crawler keeps working
        exactly as written.

        A no-op for the ordinary (cookie) case — it costs one observation only on an app
        already proven not to persist its login.
        """
        await self._port.goto(url)
        self._tracker.note_request()
        if self._auth_incomplete_reason != AUTH_NOT_PERSISTED or self._authenticator is None:
            return
        obs = await self._observe()
        controls = build_inventory(obs.raw_controls, self._refuse_pack, url=obs.url)
        if match_login_controls(controls) is None:
            return                                  # still signed in — nothing to repair
        prior_phase = self._guard.phase
        self._guard.phase = Phase.AUTH              # the guard permits a login only here
        try:
            result = await self._authenticator.relogin()
        except Exception as exc:
            logger.warning("qec.crawler.relogin_error error=%s", str(exc)[:200])
            return
        finally:
            self._guard.phase = prior_phase
        self._tracker.note_action(len(result.actions))
        if result.success:
            self._login_verified = True
            logger.info("qec.crawler.login_restored_after_reload url_scope=%s",
                        _host_of(url))
            # THE LOGIN IS RESTORED, NOT THE LOCATION. Relogin lands wherever the
            # app sends a fresh sign-in (its dashboard), while every caller of this
            # method is about to act on ``url`` — so before this, a wizard
            # re-establish refilled the DASHBOARD, discovery resets clicked from
            # the wrong page, and a Phase-B renavigation submitted into a login
            # wall (recorded live as five sign-in "pages" inside the stitched
            # journey). Reach the requested page the way a person would — by
            # clicking — which is also the only way that keeps this login alive.
            try:
                landed = await self._port.current_url()
            except Exception:
                return
            if _url_key(landed) == _url_key(url):
                return
            obs2 = await self._observe()
            controls2 = build_inventory(obs2.raw_controls, self._refuse_pack,
                                        url=obs2.url)
            hop = await self._reach_in_app(controls2, url, "")
            if hop is None:
                logger.info(
                    "qec.crawler.reload_reach_failed requested=%s landed=%s — "
                    "callers proceed from the landing page", url[:120], landed[:120])

    async def _reach_in_app(
        self, controls: list[dict[str, Any]], url: str, discovered_via: str,
    ) -> Optional[tuple[PageObservation, list[dict[str, Any]]]]:
        """From the page we are ON, reach ``url`` by CLICKING its in-app link.

        A crawl normally reaches each state by navigating to its URL — the equivalent of
        opening a fresh tab. An app that keeps the signed-in user in client-side state is
        logged OUT by exactly that, so the requested page is never reached, the link
        between two pages is never PROVEN, and with no proven navigation there is no
        coherent journey and therefore no runnable test. Moving the way a person does —
        clicking the link on the page already open — keeps the login alive and grounds
        the navigation at the same time.

        Generic, and MULTI-HOP: the target control is found by the label this route
        was DISCOVERED through, by a link whose accessible name CONTAINS the
        route's own last path segment ("/new-application" ↔ "+ New Application"),
        or by a control whose HREF resolves to the target — the strongest signal,
        and the only one that works on a NAMELESS link. When the target is not on
        the current page, the walk climbs the target's OWN path: a deep route's
        in-app entrance lives on its section pages ("/underwriting/new-business/
        new-application" is entered from the New Business queue), so an ancestor
        segment's control is clicked and the search repeats there — live, the
        single-hop version stood on the dashboard, found no "new application"
        link (it was one section away), and the one page the operator onboarded
        was never entered. Bounded by :data:`_REACH_MAX_HOPS`, loop-guarded, and
        every failure names its reason. No app-specific knowledge anywhere.
        Returns the reached observation, or ``None`` to let the caller fall back.
        """
        want = _url_key(url)
        target_labels = _reach_target_labels(url, discovered_via)
        ancestors = _reach_ancestors(url)
        if not target_labels and not ancestors:
            # Nothing to click TOWARDS — a bare root URL has no discovering label
            # and no path segment. Silence here cost a whole debugging round; say
            # which reason it was, always.
            logger.info("qec.crawler.reach_in_app_skipped reason=no_label url=%s", url[:120])
            return None

        # The refuse pack flags apply/enroll-shaped LABELS as danger — which is a
        # wizard's entrance ("+ New Application") to the letter. On a disposable-
        # attested env with the blanket approval, crossing a forward danger
        # control is already this crawler's precedent (_pick_advance_e2e); reach
        # gets the same, and ONLY toward the route being reached. The EXPLORE
        # network guard stays the hard wall against real mutation either way.
        allow_danger = self._submit_enabled and self._submit_approve_all
        cur_controls: list[dict[str, Any]] = list(controls)
        pages_walked: set[str] = set()
        for hop in range(_REACH_MAX_HOPS + 1):
            try:
                cur_url = await self._port.current_url()
            except Exception:
                cur_url = ""
            pages_walked.add(_url_key(cur_url))
            control = _reach_pick(cur_controls, target_key=want,
                                  labels=target_labels, base_url=cur_url or url,
                                  allow_danger=allow_danger)
            is_target = control is not None
            if control is None:
                # Climb toward the target through its OWN ancestry, deepest
                # section first — never a page we already walked this reach.
                for a_key, a_labels in ancestors:
                    if a_key in pages_walked:
                        continue
                    control = _reach_pick(cur_controls, target_key=a_key,
                                          labels=a_labels, base_url=cur_url or url,
                                          allow_danger=allow_danger)
                    if control is not None:
                        break
            if control is not None and control.get("danger"):
                logger.info(
                    "qec.crawler.reach_danger_crossed name=%r — an apply-shaped label "
                    "on the route the operator onboarded; permitted by the disposable-"
                    "attested blanket, auditable here.",
                    str(control.get("name") or "")[:60])
            if control is None:
                logger.info(
                    "qec.crawler.reach_in_app_skipped reason=no_matching_control "
                    "wanted=%s hop=%d url=%s",
                    sorted(target_labels)[:4], hop, url[:120])
                return None

            # The state we are clicking FROM. A navigation is a transition between
            # two states, so an edge needs a real source.
            from_fp = (self._fingerprinter.fingerprint(
                url=cur_url, controls=cur_controls, dialogs=()) if cur_url else "")
            try:
                click_obs = await self._port.click(control)
            except Exception:
                logger.info("qec.crawler.reach_in_app_skipped reason=click_failed "
                            "hop=%d url=%s", hop, url[:120])
                return None
            self._tracker.note_action()
            reached = await self._observe()
            reached_key = _url_key(reached.url)
            reached_controls = build_inventory(
                reached.raw_controls, self._refuse_pack, url=reached.url)
            if match_login_controls(reached_controls) is not None:
                logger.info("qec.crawler.reach_in_app_skipped reason=bounced_to_wall "
                            "hop=%d url=%s", hop, url[:120])
                return None      # the click bounced us back to the wall

            if is_target:
                if reached_key != want:
                    # The control CLAIMED the target (label/href) and landed
                    # elsewhere — a redirect or a guard. Honest fail, never a
                    # pretend arrival.
                    logger.info(
                        "qec.crawler.reach_in_app_skipped reason=landed_elsewhere "
                        "wanted=%s landed=%s", url[:120], reached.url[:120])
                    return None
                # RECORD THE PROOF, do not merely claim it. The generator's bar
                # (test_factory _navigation_backbone → _grounded_commit_sequence)
                # is an action whose ``after`` carries outcome=navigation AND the
                # DESTINATION in ``detail`` — detail is what lets the generator
                # match the click to the page it reached instead of guessing from
                # adjacency. The click event itself reports nothing on a pushState
                # app, so the outcome is asserted from the route we just PROVED we
                # landed on, and labelled as such.
                action = emit.build_action_record(
                    dict(control), verb="click", value=None, observation=click_obs,
                    phase=Phase.EXPLORE.value, state_id=self._pending_reach_state_id,
                    timestamp_ms=self._clock.now_ms())
                after = dict(action.after or {})
                if not after.get("navigated"):
                    after.update(navigated=True, outcome="navigation")
                    # Strict contract: the label goes in qec, never in after
                    # (AfterBundle forbids extras — refused a whole crawl live).
                    action.qec = dict(action.qec or {})
                    action.qec["navigation_kind"] = "pushstate"
                if not str(after.get("detail") or "").strip():
                    after["detail"] = reached.url[:500]
                action.after = after
                action.to_state = reached_key
                # One pending proof per destination: resets re-reach the same
                # route many times, and stacking identical proofs onto the next
                # recorded state is noise, not more evidence.
                if all(a.to_state != reached_key for a in self._pending_reach_actions):
                    self._pending_reach_actions.append(action)
                self._pending_reach_edge = (from_fp, str(control.get("name") or "")[:120])
                logger.info(
                    "qec.crawler.reached_in_app url_scope=%s via=%r hops=%d — navigated "
                    "by clicking, so the login survived and the link is PROVEN "
                    "(recorded, to_state=%s)",
                    _host_of(reached.url), str(control.get("name") or "")[:40],
                    hop, action.to_state)
                return reached, reached_controls

            # An ANCESTOR hop. A page this reach has already walked ends it —
            # clicking in circles is how a crawl burns its budget silently.
            if reached_key in pages_walked:
                logger.info("qec.crawler.reach_in_app_skipped reason=hop_loop "
                            "url=%s", url[:120])
                return None
            cur_controls = reached_controls

        logger.info("qec.crawler.reach_in_app_skipped reason=hop_budget "
                    "hops=%d url=%s", _REACH_MAX_HOPS, url[:120])
        return None

    async def _cross_auth_wall(
        self, obs: PageObservation, controls: list[dict[str, Any]], url: str,
        discovered_via: str = "",
    ) -> tuple[PageObservation, list[dict[str, Any]]]:
        """Meet a login wall mid-journey, answer it, and CARRY ON.

        Authentication was modelled as a PHASE: log in once at the entry, then
        explore forever. Every real business journey that crosses an auth boundary
        broke on that assumption — the crawl reached the wall and stopped, so the
        catalogue held a public fragment and an authenticated fragment but never
        the end-to-end flow. ``Authenticator.relogin`` was built for exactly this
        and had never been called from anywhere.

        Bounded by the authenticator's own re-login budget, so a login that cannot
        be satisfied costs a fixed number of attempts per crawl rather than looping.
        Returns the observation + inventory to actually record: the post-login page
        when we got through, and the wall itself (honestly) when we did not.
        """
        if self._authenticator is None or match_login_controls(controls) is None:
            return obs, controls

        prior_phase = self._guard.phase
        self._guard.phase = Phase.AUTH          # the guard permits a login only here
        try:
            result = await self._authenticator.relogin()
        except Exception as exc:                # never let a login attempt kill the crawl
            logger.warning("qec.crawler.relogin_error error=%s", str(exc)[:200])
            return obs, controls
        finally:
            self._guard.phase = prior_phase

        self._tracker.note_action(len(result.actions))
        if not result.success:
            logger.warning(
                "qec.crawler.relogin_failed reason=%s — the journey stops at this "
                "auth wall; the authenticated continuation is NOT covered.",
                result.reason,
            )
            return obs, controls

        if result.storage_state:
            self._storage_state = result.storage_state
        self._login_verified = True
        # Return to where the journey was heading. Login usually lands on a
        # dashboard, so without this the crawl resumes somewhere else entirely and
        # the step that provoked the wall is never taken.
        nav = await self._port.goto(url)
        self._tracker.note_request()
        if not nav.ok:
            logger.info("qec.crawler.relogin_return_failed error=%s", (nav.error or "")[:120])
            return obs, controls
        fresh = await self._observe()
        fresh_controls = build_inventory(fresh.raw_controls, self._refuse_pack, url=fresh.url)
        # AUTH THAT DOES NOT SURVIVE A PAGE LOAD. A growing class of apps (SPAs that
        # keep the signed-in user in client-side state rather than a cookie) drop the
        # login on every fresh navigation — so the goto above THROWS AWAY the login we
        # just completed, and the crawl re-lands on the wall no matter how many times it
        # signs in. Live: a carrier-admin app logged in verified THREE times and never
        # left /portal/sign-in. Detect it structurally — we hold a VERIFIED login and the
        # requested page still answers with a login form — then continue IN PLACE from
        # where the login landed instead of navigating cold again. Bounded by the
        # authenticator's own re-login budget, and honestly reported either way.
        if match_login_controls(fresh_controls) is not None:
            self._auth_incomplete = True
            self._auth_incomplete_reason = AUTH_NOT_PERSISTED
            # A login PER PAGE is this app's normal cost, so the default budget of 3 —
            # sized for the occasional expired session — is exhausted after two pages and
            # the crawl silently gives up. Scale it to the states we are allowed to
            # visit; every other safety gate is untouched.
            self._raise_relogin_budget()
            logger.warning(
                "qec.crawler.auth_not_persisted crawl_id=%s url_scope=%s — the login "
                "verified but this app drops it on a fresh page load (its session lives "
                "in the page, not a cookie); continuing IN PLACE from the signed-in "
                "state.", self.crawl_id, _host_of(fresh.url))
            try:
                again = await self._authenticator.relogin()
            except Exception as exc:
                logger.warning("qec.crawler.relogin_error error=%s", str(exc)[:200])
                return fresh, fresh_controls
            self._tracker.note_action(len(again.actions))
            if not again.success:
                return fresh, fresh_controls
            in_place = await self._observe()
            in_place_controls = build_inventory(
                in_place.raw_controls, self._refuse_pack, url=in_place.url)
            # Now reach the page we were ASKED for the way a person would — by clicking
            # its link from here. Re-navigating would log us straight back out, which is
            # why every deep route used to collapse onto this landing page.
            hop = await self._reach_in_app(in_place_controls, url, discovered_via)
            if hop is not None:
                return hop
            logger.info(
                "qec.crawler.auth_continued_in_place url_scope=%s — journey continues "
                "from the signed-in page", _host_of(in_place.url))
            return in_place, in_place_controls
        logger.info(
            "qec.crawler.auth_wall_crossed url_scope=%s — journey continues authenticated",
            _host_of(fresh.url),
        )
        return fresh, fresh_controls

    def _note_login_wall_while_authenticated(
        self, controls: Sequence[dict[str, Any]], requested_url: str = "",
        landed_url: str = "",
    ) -> None:
        """Flag a dead session that only reveals itself DEEPER than the entry.

        Most applications put a PUBLIC page at the root and protect the rest, so
        the entry check in :meth:`_verify_injected_session` sees a marketing page
        and learns nothing — live-observed: the entry was ``/``, and the login wall
        only appeared two states later at ``/login/``. Any explored state that
        presents a full login form, while we are holding a session that was
        supposed to make login unnecessary, means authenticated coverage was NOT
        achieved.

        Deliberately STRICTER than the entry check (username + password + submit,
        not a bare password input) because it runs against every state of every
        crawl: a change-password or payment form must never be read as a login
        wall. Only ever SETS the flag — a crawl that already knows its auth is
        incomplete is not re-diagnosed, and the flag never downgrades to clean.
        """
        if not self._session_injected or self._auth_incomplete:
            return
        if self._guard.phase == Phase.AUTH:
            return  # the credentialed login flow is legitimately at a login form
        if match_login_controls(controls) is None:
            return
        # A login PAGE is not a dead session. Most apps keep /login reachable, and a
        # crawl that follows links will visit it while perfectly signed in — this
        # fired on a crawl that was at that moment inside /portal/dashboard/ and
        # /portal/beneficiaries/, and reported the authenticated app as NOT covered.
        # The unambiguous evidence is being REDIRECTED to a login wall: we asked for
        # one page and the app answered with another that demands a sign-in.
        if not requested_url or not landed_url:
            return
        if _url_key(requested_url) == _url_key(landed_url):
            return
        self._auth_incomplete = True
        if self._login_verified:
            # We SIGNED IN successfully this crawl, so "the session expired, re-record
            # it" is provably the wrong diagnosis: the app simply does not keep a login
            # across page loads. Report that instead of sending the operator round a
            # re-recording loop that cannot end.
            self._auth_incomplete_reason = AUTH_NOT_PERSISTED
            logger.warning(
                "qec.crawler.auth_not_persisted crawl_id=%s — a verified login still "
                "meets a sign-in wall; this app drops the login on a fresh page load.",
                self.crawl_id)
            return
        self._auth_incomplete_reason = AUTH_SESSION_EXPIRED
        logger.warning(
            "qec.crawler.session_expired crawl_id=%s — a start-authenticated session "
            "was injected but the app still presents a login wall; the crawl covered "
            "PUBLIC pages only (authenticated areas NOT covered). Re-record the login.",
            self.crawl_id,
        )

    def _is_dedicated_auth_step(self, controls: Sequence[dict[str, Any]]) -> bool:
        """Is this page a DEDICATED login STEP — a full login form, a username-first
        identifier step, or a secret (password/PIN) screen — and NOT content-rich?

        A page that merely EMBEDS a login widget beside real business content (a
        marketing home with a sign-in box next to a public quote funnel), or a public
        multi-step funnel, is NOT a dedicated step: it carries substantial OTHER
        business inputs. Counting every form-field kind (excluding the matched
        identifier + secret by identity) keeps this generic — a radio/checkbox-driven
        funnel is not mislabeled, and a real login carrying a lone "remember me" still
        qualifies. Language-agnostic: only control SHAPES, never URL/copy.
        """
        login = match_login_controls(controls)
        identifier = match_identifier_step(controls)
        secret = match_secret_field(controls)
        if login is None and identifier is None and secret is None:
            return False
        skip = {""}
        if login is not None:
            skip.add(str(login.username.get("name") or "").strip().lower())
            skip.add(str(login.password.get("name") or "").strip().lower())
        if identifier is not None:
            skip.add(str(identifier.get("name") or "").strip().lower())
        if secret is not None:
            skip.add(str(secret.get("name") or "").strip().lower())
        other_inputs = [
            c for c in controls
            if str(c.get("kind") or "") in _FILLABLE_KINDS
            and not _is_password(c)
            and str(c.get("name") or "").strip().lower() not in skip
        ]
        return len(other_inputs) <= 1

    def _has_public_content(self, controls: Sequence[dict[str, Any]]) -> bool:
        """Genuine public content, not a bare auth step: ≥2 fillable business inputs, or
        ≥3 interactive controls (fields / links / buttons). A lone identifier or secret
        plus an advance button is NOT content — leaving such a page UNLATCHED avoids
        green-washing a gated app we merely failed to recognise as an auth step.
        """
        business = sum(
            1 for c in controls
            if str(c.get("kind") or "") in _FILLABLE_KINDS and not _is_password(c))
        interactive = sum(
            1 for c in controls
            if str(c.get("kind") or "") in _FILLABLE_KINDS
            or str(c.get("kind") or "") in ("link", "button"))
        return business >= 2 or interactive >= 3

    def _classify_no_cred_auth(
        self, controls: Sequence[dict[str, Any]], item: FrontierItem,
        requested_url: str, landed_url: str,
    ) -> str:
        """For a crawl with NO credentials/session: is this state a login WALL it cannot
        pass? Handles single-screen AND multi-step / username-first walls (email → Next
        → password) generically, on any app in any language.

        Returns ``'stop'`` (a SECRET we cannot pass, still inside the gated entry login
        flow → honest hard stop), ``'continue'`` (a username-first identifier step — let
        the crawl fill it and advance so the next screen's secret is reached), or ``''``
        (not a wall → normal behaviour).

        The SECRET field is the proof of a login (no public form asks for a password);
        the gated flow is anchored at a REDIRECTED entry landing on a dedicated auth
        step; once genuine PUBLIC content is captured the entry was not gated, so a
        deeper login page is a sub-area to SKIP, never the entry wall to abort on.
        """
        if self._credentials or self._session_injected:
            return ""
        if self._captured_public:
            return ""
        dedicated = self._is_dedicated_auth_step(controls)
        if not self._auth_flow_active:
            is_entry = item.depth == 0 and not item.parent_fingerprint
            redirected = _url_key(requested_url) != _url_key(landed_url)
            if is_entry and redirected and dedicated:
                self._auth_flow_active = True
            else:
                # Genuine public CONTENT (not a bare, unrecognised auth step) marks the
                # app public so no later login page can abort the crawl. A minimal page we
                # could not classify is left UNLATCHED — never green-washed as public.
                if not dedicated and self._has_public_content(controls):
                    self._captured_public = True
                return ""
        if not dedicated:
            # The gated flow led to real public content → it was not a login wall (or we
            # are past it) — stop treating this crawl as gated.
            self._captured_public = True
            self._auth_flow_active = False
            return ""
        if match_secret_field(controls) is not None:
            if looks_like_signup(controls):
                # A public REGISTRATION page, not a login wall — explore it; the flow was
                # not gated after all (never drop a public signup funnel).
                self._captured_public = True
                self._auth_flow_active = False
                return ""
            self._auth_blocked_reason = AUTH_NO_CREDENTIALS
            logger.warning(
                "qec.crawler.auth_required_no_credentials crawl_id=%s landed=%s — the "
                "entry is behind a login wall (a secret field, no credentials or session "
                "supplied); stopping honestly (authenticated areas NOT covered).",
                self.crawl_id, landed_url)
            return "stop"
        return "continue"

    def _wizard_auth_gate(self, controls: Sequence[dict[str, Any]]) -> bool:
        """The ``_walk_wizard`` counterpart of :meth:`_classify_no_cred_auth` for a crawl
        with no credentials/session.

        Returns True when a SECRET step (password/PIN) inside the gated entry login flow
        must STOP the crawl — a username-first wall whose password sits on a step the
        outer ``_expand`` classifier never re-sees (the walk advances through it inline).
        Also CLEARS the gated-flow state when the walk instead reaches genuine public
        content, so a public multi-step funnel — which has no secret — is neither stopped
        nor leaves a later page mislabeled a wall.
        """
        if self._credentials or self._session_injected:
            return False
        if self._captured_public or not self._auth_flow_active:
            return False
        if match_secret_field(controls) is not None:
            if looks_like_signup(controls):
                # A public REGISTRATION page reached in the walk — not a login wall.
                self._captured_public = True
                self._auth_flow_active = False
                return False
            self._auth_blocked_reason = AUTH_NO_CREDENTIALS
            logger.warning(
                "qec.crawler.auth_required_no_credentials crawl_id=%s — a multi-step "
                "login wall reached a secret step with no credentials; stopping honestly "
                "(authenticated areas NOT covered).", self.crawl_id)
            return True
        if not self._is_dedicated_auth_step(controls):
            # The walk reached genuine public content → not a login wall. Clear the gated
            # flow so no later page can be mislabeled or wrongly aborted.
            self._captured_public = True
            self._auth_flow_active = False
        return False
