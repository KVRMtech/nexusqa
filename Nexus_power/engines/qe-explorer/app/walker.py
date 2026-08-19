"""The wizard walk — carrying a business journey to its end (T-DE-13).

Extracted VERBATIM from :mod:`app.crawler`.  This is the largest extraction in
M0.3 and the one with the least tolerance for cleverness: it decides how far
every funnel is walked, and therefore what the catalogue contains.

THE THREE-TIER ADVANCE, and why all three exist.  Tier 1 is a strict regex on
the control label; tier 2 is the structural fallback; tier 3 asks the oracle.
A crawl whose advances are all tier-1 is indistinguishable from a crawl whose
oracle is dead, which is why the tier is recorded per advance and the oracle
consult is counted (see :mod:`app.oracle_gateway`).

A WALK THAT STOPS MUST SAY WHY.  ``_note_advance_blocked`` names the field whose
absence disabled the forward control, so a one-step journey explains itself
instead of being investigated by hand.  ``_answer_to_unblock`` then runs the
experiment the DOM cannot express — a "choose at least one of these" rule is not
representable in HTML, so the only way to learn it is to answer a question the
fill declined and ask the application again.

TRUNCATION IS NEVER COMPLETION.  ``flow_ledger.build_flow`` DERIVES
``completed`` from the terminal reason; six steps of a fifteen-step funnel is
not the Apply flow, and no code path here may report it as one.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import heapq
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from . import danger_signals
from . import emit
from . import matcher
from . import perception
from . import rules
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
from .browser import (OUTCOME_CONFIRMATION, BrowserPort, PageObservation,
                      classify_submit_after)
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
# NO ``state_fingerprint`` IMPORT. The walk constructs no identities of its
# own; it asks WalkIdentity (state_identity.py), which is the single
# authority for what a page state IS. Re-adding a direct import here is
# how the two call sites that caused the same-shape collapse got in.
from .perception import perceptual_hash_png
from .state_identity import (_MAX_COVERAGE_STATES, _MAX_DANGER_NAMES,
                             _MAX_NETWORK_CALLS, _MAX_STATE_FIELDS,
                             StateFingerprinter, StateRecorder, StepSignals,
                             WalkIdentity, structural_signature,
                             _action_to_dict, _displayed_values,
                             _form_snapshot, _is_password, _network_calls)
from . import flow_ledger
from .boundary import (BOUNDARY_APPROVABLE, BOUNDARY_SAFE, RUNG_DIALOG,
                       boundary_key, classify_boundary, confirmation_transition,
                       is_confirmation_landing)
from .identity_pack import derive as derive_identity
from .forms import (AnswerKey, PROV_UNBLOCK, capture_page_declarations,
                    execute_submit_phase_b, fill_form_phase_a)
from .guard import (
    EVENT_BLOCKED_METHOD,
    MUTATING_METHODS,
    GuardDecision,
    Phase,
    classify_action_verb,
    classify_request,
    registrable_domain,
    same_registrable_domain,
)
from .inventory import build_inventory, form_signal_for

logger = logging.getLogger("app.crawler")

#: M1.4 — how much of a journey's text history the confirmation diff remembers.
#: Bounded because the history grows with every step of a 60-step walk and a
#: content-heavy application can render hundreds of text nodes per page; an
#: unbounded accumulator would be a slow leak proportional to funnel depth. The
#: MOST RECENT entries are kept, which is where a repeat of the boilerplate a
#: confirmation might be confused with actually lives.
_MAX_WALK_TEXT_HISTORY = 4000


def _recent_text_history(seq: list[str]) -> list[str]:
    return seq[-_MAX_WALK_TEXT_HISTORY:]


class WalkerMixin:
    """Mixed into :class:`app.crawler.Crawler` (T-DE-13)."""

    # ── M1.3 · CONTROLLED WALK PERSISTENCE ───────────────────────────────────
    #
    # THE ROOT CAUSE THESE THREE METHODS ADDRESS.  The guard was built on
    # ``EXPLORE == READ ONLY``, and an enterprise wizard breaks that assumption
    # in the middle of ordinary navigation: the Continue on step 3 POSTs the
    # step to the server, and the server will not render step 4 until it has.
    # The walk therefore died at the first server-validated step and every page
    # behind it went uncatalogued — recorded honestly as a one-step journey,
    # which is the correct report and a useless one.
    #
    # WHAT DID **NOT** CHANGE.  Absent a verified platform provisioning proof,
    # every method here is a no-op: ``_walk_authorization`` returns None, the
    # window never opens, the phase never leaves EXPLORE, and the mutation is
    # blocked by the same rule that blocked it before this milestone. The
    # capability is switched on by cryptography, not by configuration.

    def _walk_authorization(self):
        """This crawl's :class:`app.walk_persist.WalkAuthorization`, or ``None``.

        ``None`` means "behave exactly as the crawler did before M1.3", and is
        the value for every crawl without a verified proof — including every
        production crawl, forever."""
        guard = getattr(self, "_guard", None)
        if guard is None or not getattr(guard, "walk_attested", False):
            return None
        if getattr(self, "_observe_only", False):
            # Production posture is catalogue-only. A proof could never name a
            # production environment (env_kind must be 'disposable'), so this is
            # belt-and-braces — and belt-and-braces is the point.
            return None
        return getattr(guard, "walk_authorization", None)

    def _begin_walk_step(self, *, journey_id: str, step_index: int,
                         step_fingerprint: str) -> None:
        """Enter a logical step: reset the per-step mutation budget.

        DETERMINISTIC AND AUTOMATIC.  The reset is keyed to the step identity the
        walk already computes, so it happens exactly once per step whatever the
        page does, and a step that is merely re-observed does NOT refill its
        allowance (see ``StepMutationBudget.begin``)."""
        auth = self._walk_authorization()
        if auth is None:
            return
        auth.begin_step(journey_id=journey_id, step_index=int(step_index),
                        step_fingerprint=step_fingerprint,
                        now_ms=self._clock.now_ms())

    @contextlib.asynccontextmanager
    async def _walk_persistence_window(self, control_name: str):
        """Open the narrow WALK window around ONE actuation, then close it.

        Yields True when the window actually opened (attested crawl), False
        otherwise — callers use the flag only for reporting; the click itself is
        identical either way, because whether its network effects are permitted
        is the guard's decision and not the walker's.

        The phase is restored in ``finally`` on every path, including an
        exception: a crawl that raised mid-click must not be left standing in a
        phase that permits writes.  This mirrors ``_execute_approved_submit``
        exactly, which is the pattern this extends."""
        auth = self._walk_authorization()
        if auth is None:
            yield False
            return
        prev_phase = self._guard.phase
        auth.authorize_step(True)
        auth.open_window(control_name, self._clock.now_ms())
        self._guard.phase = Phase.WALK
        try:
            yield True
        finally:
            self._guard.phase = prev_phase
            auth.close_window()
            auth.authorize_step(False)

    def _pick_persistence_control(
        self, controls: Sequence[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """A NON-ADVANCING persistence control on this step, or ``None``.

        Four independent vetoes, every one of them fail-closed:
          * the WHOLE accessible name must read as persistence (``PERSISTENCE_RE``
            is a full-string match — "Save Draft and Submit Application" fails);
          * no commit word anywhere in the label (``COMMIT_RE``);
          * no advance word — a control that moves the funnel is an ADVANCE and
            is picked by the advance tiers, not here (fixture 10's whole point:
            a walk that treats Save Draft as an advance records a step that
            never happened);
          * no refuse-pack irreversible verb, and not an operator-approved
            submit name.
        """
        for control in controls or ():
            if control.get("disabled"):
                continue
            if control.get("kind") not in ("button", "submit"):
                continue
            name = str(control.get("name") or "").strip()
            if not name or not vocab.PERSISTENCE_RE.match(name):
                continue
            if vocab.COMMIT_RE.search(name) or vocab.ADVANCE_RE.search(name):
                continue
            verdict = classify_action_verb(name, "", self._refuse_pack)
            if verdict.irreversible:
                continue
            if name.lower() in getattr(self, "_submit_approvals", ()):  # a submit
                continue
            return control
        return None

    async def _maybe_persist_walk_step(
        self, controls: Sequence[dict[str, Any]], url: str, fingerprint: str,
    ) -> Optional[Any]:
        """Actuate this step's Save Draft (or equivalent) inside a walk window.

        Returns the recorded :class:`app.emit.ActionRecord` when it fired, else
        ``None``.  Deduped per (step, control) so a re-observed step cannot loop
        on its own draft save."""
        auth = self._walk_authorization()
        if auth is None:
            return None
        control = self._pick_persistence_control(controls)
        if control is None:
            return None
        name = str(control.get("name") or "").strip()
        key = f"{fingerprint}::{name.lower()}"
        if key in self._walk_persisted:
            return None
        self._walk_persisted.add(key)
        await self._politeness_delay()
        async with self._walk_persistence_window(name):
            observation = await self._port.click(control)
        self._tracker.note_request()
        self._tracker.note_action()
        logger.info("qec.walk.persisted control=%r url=%s remaining_budget=%d",
                    name[:40], url, auth.budget.remaining)
        return emit.build_action_record(
            dict(control), verb="click", value=None, observation=observation,
            phase=Phase.WALK.value, state_id=fingerprint,
            timestamp_ms=self._clock.now_ms())

    async def _answer_questionnaire(
        self, controls: Sequence[dict[str, Any]], url: str, fingerprint: str,
    ) -> list[dict[str, Any]]:
        """Answer ONE unanswered question of a bare-button questionnaire.

        A lifestyle/health page renders each question as a pair of plain <button>s
        ("Yes"/"No") — not form fields, so the fill never sees them, and not a
        dropdown, so the decision-point path never sees them either. The gated
        "Continue" stays validation-locked until each is answered, which is where
        the application funnel dead-ends. This clicks one option so the walk can
        proceed to underwriting → e-sign, and records the choice as a decision point
        so the answered question becomes a branch in the catalogue.

        ONE question per call: the walk re-observes and calls again, so every click
        lands on a FRESH ``match_index`` (the only handle on identical bare buttons)
        rather than a stale ordinal from a page that re-rendered under it.

        An option is a button whose accessible name REPEATS on the page (one per
        question), enabled, non-danger, not a commit/advance action, not auth chrome
        — so a lone "Continue"/"Back"/"Sign out" is never mistaken for an answer.
        """
        from collections import Counter
        label_counts = Counter(
            str(c.get("name") or "").strip().lower() for c in controls
            if c.get("kind") == "button" and not c.get("disabled"))

        def _is_option(c: Mapping[str, Any]) -> bool:
            n = str(c.get("name") or "").strip()
            if c.get("kind") != "button" or c.get("disabled") or c.get("danger"):
                return False
            if not n or _AUTH_SESSION_RE.search(n):
                return False
            if _WIZARD_COMMIT_RE.search(n) or _WIZARD_ADVANCE_RE.search(n):
                return False
            return label_counts.get(n.lower(), 0) >= 2

        # Group by DOM order: a new question begins when a label repeats.
        groups: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        cur_labels: set[str] = set()
        for c in controls:
            if not _is_option(c):
                continue
            nl = str(c.get("name") or "").strip().lower()
            if nl in cur_labels:
                groups.append(cur)
                cur, cur_labels = [], set()
            cur.append(dict(c))
            cur_labels.add(nl)
        if cur:
            groups.append(cur)

        for ordinal, group in enumerate(groups):
            opts = [str(c.get("name") or "").strip() for c in group]
            sig = "q:" + hashlib.sha256(
                ("%d|%s" % (ordinal, "|".join(sorted(o.lower() for o in opts))))
                .encode("utf-8")).hexdigest()[:24]
            if sig in self._answered_questions:
                continue
            # A planned branch walk forces a specific answer (Journey Graph P1):
            # branch_planner keys choice_overrides by the question's own signature
            # → option label, so a re-crawl can walk the "Yes" side to enumerate
            # what it reveals. Naturally gated — overrides are populated only for
            # planned walks, so a normal crawl keeps the negative-preference below.
            forced = self._choice_overrides.get(sig) if self._choice_overrides else None
            chosen = None
            if forced:
                fnorm = str(forced).strip().lower()
                chosen = next(
                    (c for c in group
                     if str(c.get("name") or "").strip().lower() == fnorm), None)
            if chosen is None:
                # Prefer a negative/decline answer — it minimises the follow-up
                # questions a "Yes" tends to reveal, so the walk reaches the end.
                chosen = next(
                    (c for c in group
                     if str(c.get("name") or "").strip().lower() in _NEGATIVE_OPTION_HINTS),
                    group[0])
            try:
                await self._port.click(chosen)
                self._tracker.note_action()
            except Exception as exc:  # a failing click must not loop forever
                logger.info("qec.questionnaire.click_failed q=%d error=%s",
                            ordinal, str(exc)[:120])
                self._answered_questions.add(sig)
                continue
            self._answered_questions.add(sig)
            # Structured fleet telemetry (was WARNING — too noisy at 1000-app
            # scale; the event is normal, not a warning). Keyed so questionnaire
            # coverage is measurable per app without log-level spam.
            logger.info(
                "qec.questionnaire.answered url=%s groups=%d ordinal=%d choice=%s "
                "match_index=%s", (url or "")[:80], len(groups), ordinal,
                str(chosen.get("name") or "")[:20], chosen.get("match_index"))
            return [{
                "control_signature": sig,
                "control_label": "Question %d" % (ordinal + 1),
                "options": opts[:12],
                "provenance": "questionnaire",
                "choice": str(chosen.get("name") or "").strip()[:80],
            }]
        return []

    def _note_advance_blocked(
        self, controls: Sequence[dict[str, Any]], url: str, fill: Any,
    ) -> str:
        """A funnel that stops WITH a forward control present — say why, by name.

        Returns the blocked control's label ("" when nothing forward is blocked),
        so the caller can try to ANSWER the block rather than only describe it.

        THE COST OF NOT DOING THIS. A Radix ``Gender`` select was never filled
        (its options are not in the DOM until it is opened), the application's own
        validation therefore disabled ``Continue``, and the walk — correctly —
        skipped a disabled control. Every downstream number was accurate and none
        of them said which field was missing. It took five crawls, a manifest
        query, and a read of the app's own source to name a field the crawl had
        in its hand the whole time.

        Recorded as a first-class finding on the coverage ledger, and the missing
        fields are pushed into the seed residue so the operator's remediation is
        "supply Gender" rather than "the crawl went one step deep".

        Value-free: control labels are product UI text, never user data.
        """
        blocked_label = ""
        for c in controls:
            if c.get("kind") not in ("button", "link"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or not _WIZARD_ADVANCE_RE.search(name):
                continue
            if c.get("disabled"):
                blocked_label = name
                break
        if not blocked_label:
            return ""                  # no forward control at all — a real terminal

        missing = [str(n) for n in getattr(fill, "unfilled_fields", ()) or ()][:12]
        record = {
            "url": url[:300],
            "label": blocked_label[:120],
            # The app itself disabled it, which is a STATEMENT about its own
            # validation — not a crawler limitation and never the app's fault.
            "reason": "advance_disabled_by_app_validation",
            "missing_fields": missing,
        }
        if not any(b.get("url") == record["url"] and b.get("label") == record["label"]
                   for b in self._advance_blocked):
            self._advance_blocked.append(record)
        # The residue ask must name these too — they are precisely the fields
        # whose absence stopped the funnel, which makes them the highest-value
        # thing anyone could supply.
        for name in missing:
            if not any(d.get("label") == name and d.get("url") == url
                       for d in self._fields_seed_detail):
                self._fields_seed_detail.append({"label": name, "url": url})
            if name not in self._fields_unfilled:
                self._fields_unfilled.append(name)
        logger.warning(
            "qec.wizard.advance_blocked url=%s label=%r missing=%s — the app "
            "disabled its own forward control because these fields are unfilled",
            url[:120], blocked_label[:40], missing[:6])
        return blocked_label

    async def _answer_to_unblock(
        self, controls: Sequence[dict[str, Any]], blocked_label: str,
        url: str, fill: Any,
    ) -> Sequence[dict[str, Any]]:
        """The app disabled its own forward control. ANSWER A QUESTION THE FILL
        DECLINED, AND ASK THE APP AGAIN.

        THE HOLE THIS CLOSES, and why no amount of DOM reading could close it.
        A "choose at least one of these" question CANNOT BE EXPRESSED IN HTML.
        ``required`` on a checkbox means *that one box* must be checked, which is
        never what a multi-select question means, so every framework puts the
        rule in script instead — a zod ``.min(1)``, an Angular validator, a hand
        written ``canAdvance()``. The constraint is therefore invisible to any
        crawler that only reads markup, and the fill correctly declined to answer
        eight optional-looking checkboxes on a step whose Continue was disabled
        precisely because none of them were answered. The walk stopped one step
        short of the end of the application and named eight fields for a human to
        supply — a human doing, by hand, the thing this product exists to do.

        THE EXPERIMENT. We do not infer the rule; we TEST it. Answer one declined
        question, re-read the page, and let the application render its own
        verdict on the forward control. That verdict is evidence of a kind no
        static read can produce, and it is worth more than the unblocked walk:
        "at least one Health Condition must be selected before Continue" is
        exactly the tacit business rule the catalogue is for, and the app just
        proved it. Recorded as such, with ``PROV_UNBLOCK`` on the field.

        WHICH QUESTION. The member that ASSERTS THE LEAST — "None", "N/A" — else
        DOM order. Checking "Type 2 Diabetes" would answer the question just as
        well and fabricate a medical history for a synthetic person on an
        insurance application. Both unblock the walk; only one invents nothing.

        BOUNDED AND REVERSIBLE. Exactly one attempt per blocked step: if
        answering one question does not clear the validation, the block is about
        something else and a second guess would be a search rather than an
        experiment. A failed attempt is UNDONE, so a change that bought nothing
        never reaches the recorded form snapshot.

        Test environments only (product scope) and a form control, never an
        actuator — the same class of act as typing into a text field, which this
        crawler already does everywhere. Value-free: labels are product UI text.
        """
        self._last_unblock_field = ""
        declined = {_norm_label(n)
                    for n in getattr(fill, "unfilled_fields", ()) or ()}
        if not declined:
            return controls
        candidates: list[dict[str, Any]] = []
        for c in controls:
            if c.get("kind") not in ("checkbox", "toggle"):
                continue
            if c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or _norm_label(name) not in declined:
                continue
            # Already on: answering it again would change nothing and the app has
            # plainly not accepted it as sufficient.
            if str(c.get("value_committed") or "").strip().lower() == "true":
                continue
            candidates.append(c)
        if not candidates:
            return controls              # nothing declined is answerable this way

        # ── M1.7 / T-GW-04 · ASK WHAT WE ALREADY PROVED ─────────────────────
        # An earlier crawl of THIS application may already have made the app
        # render its verdict on THIS control, and recorded which question
        # unblocked it. Until this milestone that knowledge died with the crawl
        # that found it, so every run re-derived the same rule by re-running the
        # same speculative choice against the same checkbox.
        #
        # WHAT REUSE REMOVES, precisely: the GUESS. Below, with no knowledge, the
        # walk picks the least-asserting declined question and finds out what
        # happens - and when the guess is wrong the change is reverted, the block
        # stands, and the funnel stops one step short of the end. With a rule the
        # choice is not a guess, so that failure mode is gone.
        #
        # WHAT REUSE DOES NOT REMOVE: the ACTION, and the app's confirmation of
        # it. The control is still set and the page is still re-read, because a
        # rule is knowledge ABOUT an application, never a substitute for having
        # done the thing - and a walk that reported an advance it had not
        # actually unblocked would be this milestone's own failure mode, rebuilt
        # inside the feature meant to close it.
        known = self._known_rules.lookup(url=url, blocked_label=blocked_label)
        reused = False
        pick = None
        if known is not None:
            wanted = rules.norm_label(known.field_label)
            pick = next((c for c in candidates
                         if rules.norm_label(c.get("name")) == wanted), None)
            if pick is not None:
                reused = True
                logger.info(
                    "qec.rules.reused url=%s advance=%r field=%r - answered from a "
                    "rule an earlier crawl proved; no experiment run",
                    url[:120], blocked_label[:40], known.field_label[:40])
            else:
                # The rule names a question this page is not currently asking (a
                # branch that hides it, or the app changed). Fall through to the
                # experiment rather than force a control that is not there.
                logger.info(
                    "qec.rules.stale url=%s advance=%r field=%r - the known rule "
                    "names a question this state does not ask; experimenting",
                    url[:120], blocked_label[:40], known.field_label[:40])
        if pick is None:
            pick = next(
                (c for c in candidates
                 if vocab.NEGATIVE_OPTION_RE.match(str(c.get("name") or "").strip())),
                candidates[0])
        pick_name = str(pick.get("name") or "").strip()
        try:
            observation = await self._port.set_checked(pick, True)
            if observation.intent_met is False:
                logger.warning(
                    "qec.wizard.unblock_fill_failed url=%s field=%r — the control "
                    "did not take the answer; block stands", url[:120],
                    pick_name[:40])
                return controls
            reobs = await self._observe()
            refreshed = build_inventory(reobs.raw_controls, self._refuse_pack,
                                        url=reobs.url)
            cleared = any(
                str(c.get("name") or "").strip() == blocked_label
                and not c.get("disabled")
                for c in refreshed
                if c.get("kind") in ("button", "link"))
            if not cleared:
                # Bought nothing — put the page back the way the app had it.
                await self._port.set_checked(pick, False)
                logger.warning(
                    "qec.wizard.unblock_declined url=%s field=%r advance=%r — "
                    "answering it did not enable the forward control, so the "
                    "block is about something else; change reverted",
                    url[:120], pick_name[:40], blocked_label[:40])
                return controls
        except Exception as exc:                       # never fail the crawl for this
            logger.warning("qec.wizard.unblock_error url=%s field=%r err=%s",
                           url[:120], pick_name[:40], exc)
            return controls

        # THE APP CONFIRMED IT. Record the rule, correct the residue, and re-provenance
        # the field — it is answered, and answered by the strongest evidence there is.
        # THE RESIDUE'S CLAIM IS NOW DISPROVEN FOR ALL OF THEM. That list means
        # "the fields whose absence STOPPED THE FUNNEL", and the funnel is no
        # longer stopped — the app enabled its forward control with the other
        # controls exactly as they were. Dropping only the answered one would
        # leave seven fields on an operator's to-do list under a heading the
        # application itself has just contradicted. The record keeps its
        # missing_fields as evidence of the page as it stood.
        released: set[str] = {_norm_label(pick_name)}
        proof = (
            "%s requires an answer to %r before it is enabled "
            "(proven: the app enabled it when the agent answered)"
            % (blocked_label[:60], pick_name[:60]))
        # ── M1.7 / T-GW-04 · THE RULE OUTLIVES THIS CRAWL ───────────────────
        # This sentence used to be written into a list on the crawler object,
        # counted once by qe-central, and thrown away - so the next crawl of the
        # same application re-ran the same experiment to re-derive it. It is now
        # also minted as a first-class, keyed, versioned rule that travels out on
        # the completion callback for qe-central to persist against
        # (tenant, app). Recorded on the REUSE path too: a rule that was applied
        # and confirmed again is a rule whose evidence is fresher, and dropping
        # it here would slowly starve the store of everything it had learned.
        self._rule_ledger.add(rules.discover(
            url=url, blocked_label=blocked_label, field_label=pick_name,
            proof=proof))
        for b in self._advance_blocked:
            if b.get("url") == url[:300] and b.get("label") == blocked_label[:120]:
                b["resolved_by_agent"] = pick_name[:120]
                b["rule_reused"] = reused
                b["business_rule"] = proof
                released.update(_norm_label(m)
                                for m in (b.get("missing_fields") or ()))
        self._fields_unfilled = [n for n in self._fields_unfilled
                                 if _norm_label(n) not in released]
        self._fields_seed_detail = [
            d for d in self._fields_seed_detail
            if not (_norm_label(d.get("label")) in released
                    and d.get("url") == url)]
        # BOTH ledgers: the crawl-wide one the residue is built from, and the
        # fill's own, which the CURRENT step's decision points are derived from.
        # Updating only the first leaves the step reporting `needs_input` for a
        # question the application has just confirmed we answered.
        for ledger in (self._field_ledger, getattr(fill, "field_ledger", None) or ()):
            for row in ledger:
                if _norm_label(row.get("label") or row.get("name")) != _norm_label(pick_name):
                    continue
                # PROV_UNBLOCK means "this crawl proved it just now";
                # PROV_KNOWN_RULE means "an earlier crawl proved it and this one
                # applied and re-confirmed it". Keeping them apart is what lets a
                # report say whether a claim rests on evidence from THIS run or on
                # inherited evidence - conflating them would make an inherited
                # rule indistinguishable from a fresh proof, which is exactly the
                # kind of blur this milestone exists to remove.
                row["provenance"] = rules.PROV_KNOWN_RULE if reused else PROV_UNBLOCK
                row["filled"] = True
                if "options" in row:
                    row["choice"] = "checked"
        unfilled = getattr(fill, "unfilled_fields", None)
        if isinstance(unfilled, list):
            unfilled[:] = [n for n in unfilled
                           if _norm_label(n) != _norm_label(pick_name)]
        self._last_unblock_field = pick_name
        logger.warning(
            "qec.wizard.unblocked url=%s field=%r advance=%r — the app enabled "
            "its own forward control once the agent answered; BUSINESS RULE "
            "discovered: %r is gated on that question",
            url[:120], pick_name[:40], blocked_label[:40], blocked_label[:40])
        return refreshed

    def _note_boundary_controls(self, controls: Sequence[dict[str, Any]],
                                *, url: str = "") -> None:
        """Record the commit-boundary controls this state offers, SPLIT BY CLASS.

        THE LIST THAT DROPPED THE ONLY CONTROLS THAT NEEDED IT.

        This used to append to one list, ``_submit_candidates``, and skip every
        control the refuse pack flagged: ``if not name or c.get("danger"):
        continue``.  ``_submit_candidates`` is where qe-central builds the
        operator's approval picker from — so the controls that REQUIRE an
        approval were exactly the controls never offered for one.  A dangerous
        boundary could not be approved because it could not be seen, and it
        could not be seen because it was dangerous.  That deadlock is why
        completed journeys were zero.

        Two lists now, with two meanings that cannot be confused (T-AC-01):

          ``_approvable_boundary``  irreversible.  The walk STOPS.  The operator
                                    is offered the control by name and may issue
                                    a per-control grant.
          ``_submit_candidates``    crossable on the crawl's own authority — the
                                    forward controls the walk actually walks.

        Recording only.  Nothing here decides to click anything.
        """
        for c in controls:
            if c.get("kind") not in ("button", "link", "submit"):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            klass = classify_boundary(c)
            if klass.cls == BOUNDARY_APPROVABLE:
                # A DISABLED commit button is still the boundary this journey
                # ends at — it is the reason the funnel stopped, and an operator
                # who cannot see it will keep asking why the crawl went nowhere.
                # classify_boundary already reports a disabled control as safe;
                # this branch is reached only for a live one.
                self._approvable_boundary.append({
                    "label": name,
                    "url": url,
                    "reason": klass.reason,
                    "rule_id": klass.rule_id,
                    "severity": klass.severity,
                    "boundary_key": boundary_key(url, name),
                })
            elif klass.cls == BOUNDARY_SAFE and not c.get("disabled"):
                # The forward controls the walk crosses WITHOUT asking anyone.
                # Kept because a report that lists only what it refused cannot
                # show that anything was covered at all.
                if _is_wizard_advance(name):
                    self._submit_candidates.append(name)

    def _link_crossing_to_flow(self, flow_index: int, milestones_before: int) -> None:
        """Attach the outcome milestone to the journey that produced it (T-AC-06).

        THE JOURNEY IS REBUILT, NOT PATCHED.  ``journey_completed`` is derived
        inside :func:`flow_ledger.build_flow` from the milestone's own
        ``verified`` flag, and the entire point of deriving it there is that no
        caller can set it.  Poking the field into the existing dict would be
        exactly the caller-set completion this design refuses, so the flow is
        rebuilt through the same constructor with the milestone supplied and the
        derivation runs once, in one place.

        No milestone means no upgrade: the journey keeps its honest
        ``submit_boundary`` terminal, which is the correct record of a walk that
        reached the commit button and was not authorised to cross it.
        """
        if not (0 <= flow_index < len(self._flows)):
            return
        if len(self._outcome_milestones) <= milestones_before:
            return
        milestone = self._outcome_milestones[-1]
        flow = self._flows[flow_index]
        self._flows[flow_index] = flow_ledger.build_flow(
            entry_fingerprint=str(flow.get("entry_fingerprint") or ""),
            entry_url=str(flow.get("entry_url") or ""),
            entry_title=str(flow.get("entry_title") or ""),
            steps=flow.get("steps") or (),
            terminal=flow_ledger.TERMINAL_SUBMIT_CROSSED,
            terminal_url=str(milestone.get("url_after")
                             or flow.get("terminal_url") or ""),
            outcome_values=flow.get("outcome_values") or (),
            max_steps=int(flow.get("step_budget") or 0),
            outcome_milestone=milestone,
        )

    def _pick_wizard_advance(self, controls: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """The first non-danger advance control (Next/Continue/Proceed/Forward) to
        step the wizard, or ``None``.  Fail-closed: skips danger/disabled/nameless
        controls, skips any name the operator approved for an attested Phase-B
        submit (that path owns it), and vetoes on any commit/terminal word."""
        for c in controls:
            if c.get("kind") != "button" or c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            if name.lower() in self._submit_approvals:
                # The Phase-B submit path owns this control, so the wizard must
                # not touch it — that guard stays. But when the approved label is
                # ALSO a generic advance word, the operator has (almost certainly
                # unknowingly) disabled every step of the funnel: "Continue",
                # "Next" and "Submit" are the same label on step 2 as on step 5.
                # Observed live: an app with submit_approvals=["Continue"]
                # recorded its five-step quote funnel as five one-step journeys,
                # with no error anywhere. Silence is the defect; say it loudly.
                if _is_wizard_advance(name):
                    logger.warning(
                        "qec.wizard.advance_shadowed_by_submit_approval name=%r "
                        "— this label is approved for Phase-B submit AND is a "
                        "generic advance word, so every wizard step using it is "
                        "unwalkable. Approve the FINAL submit control only.",
                        name[:40])
                continue
            if _is_wizard_advance(name):
                return c
        # DIAGNOSTIC: no advance found. An offline reproduction of the same page
        # DOES yield a pickable "Continue" (button, enabled, not danger), so what
        # the crawl actually holds here differs from what the page shows — and
        # only the crawl can say how. Names are product UI text, never values.
        # Every input to this loop checks out when replayed offline, and it still
        # finds nothing live. submit_approvals is the last unverified one: a name
        # listed there is SKIPPED here on purpose (the attested Phase-B submit
        # path owns it), so an app that approved "Continue" for submit would make
        # every wizard step in the funnel unadvanceable.
        # DIAGNOSTIC ORDERING: NEAR-MISSES FIRST, ALWAYS.
        #
        # This printed the first 8 buttons in DOM order, and page chrome fills
        # that window — a notification bell, an avatar, a couple of nameless
        # icons. The one control anyone needs to see is the advance-shaped one,
        # and on a real page it sits at the BOTTOM of the form, so it was
        # truncated away every single time. Two investigation round-trips were
        # spent concluding "Continue was never captured" when the log had simply
        # never shown it.
        #
        # Now: every control whose label looks like an advance is listed FIRST
        # and never truncated away, because that is the set whose verdict
        # explains the decline. Chrome fills whatever room is left.
        buttons = [c for c in (controls or ()) if c.get("kind") == "button"]

        def _verdict(c: dict[str, Any]) -> str:
            n = str(c.get("name") or "").strip()
            return (f"{n[:24] or '(nameless)'}"
                    f":btn={c.get('kind') == 'button'}"
                    f":dis={bool(c.get('disabled'))}:dang={bool(c.get('danger'))}"
                    f":appr={n.lower() in self._submit_approvals}"
                    f":adv={_is_wizard_advance(n)}")

        def _advance_shaped(c: dict[str, Any]) -> bool:
            return bool(_WIZARD_ADVANCE_RE.search(str(c.get("name") or "")))

        near = [c for c in buttons if _advance_shaped(c)]
        rest = [c for c in buttons if not _advance_shaped(c)]
        self._last_advance_verdicts = (
            [_verdict(c) for c in near] + [_verdict(c) for c in rest][:6])
        logger.info(
            "qec.wizard.no_tier1 approvals=%s buttons=%d advance_shaped=%d "
            "verdicts=%s",
            sorted(self._submit_approvals), len(buttons), len(near),
            self._last_advance_verdicts)
        return None

    def _tier3_candidates(
        self, controls: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Controls the agent oracle is ALLOWED to choose between.

        SAFETY-SENSITIVE (the commit boundary): the oracle picks whatever a
        human would click to move forward — so anything a human must NOT be
        allowed to click for us is removed BEFORE the oracle ever sees it, not
        after. Excluded, in every case:

          * danger controls (refuse-pack irreversibility — never relaxed),
          * disabled controls,
          * commit-word labels (``_WIZARD_COMMIT_RE``) — a Submit/Pay/Sign is
            the submit boundary, something the walk STOPS at, never something
            an LLM may choose (only the attested Phase-B path crosses it),
          * operator-approved submit names (``submit_approvals`` — that path
            owns them),
          * nameless controls — no name means no commit signal to veto on and
            no signal for the oracle; picking one is a blind click. Fail
            closed.

        Links stay eligible (framework apps render advances as anchors) under
        the same name gates. This filter also bounds the prompt-injection
        surface: page-authored control names can steer the pick only within
        this pre-filtered set, every member of which the crawl was already
        willing to click.
        """
        out: list[dict[str, Any]] = []
        for c in controls:
            if c.get("kind") not in ("button", "link"):
                continue
            if c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name.lower() in self._submit_approvals:
                continue
            if _WIZARD_COMMIT_RE.search(name):
                continue
            # SITE CHROME IS NOT A FUNNEL STEP. A link to the site ROOT is the
            # header logo / "home" affordance; taking it always leaves the flow.
            # Nothing in its LABEL says so — observed live, the oracle weighed
            # "V VKPower Life Insurance" against "Continue" on the quote page,
            # chose the logo, navigated to the homepage and came back, and the
            # funnel was recorded as a `loop`. Links otherwise stay eligible,
            # because framework apps really do render an advance as an anchor.
            if c.get("kind") == "link" and _links_to_site_root(c):
                continue
            out.append(c)
        return out

    async def _pick_advance_e2e(
        self, controls: Sequence[dict[str, Any]],
        page_url: str = "", page_title: str = "", fingerprint: str = "",
    ) -> AdvanceDecision:
        """E2E-mode advance: 3-tier detection so the crawl walks flows the way
        a real human would, regardless of button label conventions.

        Tier 1 — strict regex (same as explore/target, fast + free).
        Tier 2 — advance word present, commit-word veto IGNORED. Catches
                 "Continue to Payment", "Proceed to Checkout", etc.
        Tier 3 — agent oracle over :meth:`_tier3_candidates`. Catches
                 "See My Quote", "Apply Now", "Go", or any label regex can
                 never anticipate.

        The danger gate is NEVER relaxed, and the commit boundary is
        structurally unreachable: Tiers 1-2 veto commit words per-control and
        Tier 3 filters them out of the candidate set entirely.

        Returns an :class:`AdvanceDecision` — the control (or ``None``), the
        tier that decided, and the oracle consultation status. The status is
        what lets ``_walk_wizard`` end a walk as ``oracle_unavailable`` (NOT
        covered) instead of ``no_advance`` (covered) when the agent could not
        be reached: unknown must never be reported as finished.
        """
        # Tier 1: strict regex — identical to explore mode.
        strict = self._pick_wizard_advance(controls)
        if strict is not None:
            return AdvanceDecision(control=strict, tier=1)

        # Tier 2: the commit veto is lifted ONLY for destination-shaped labels
        # — advance word, then a destination preposition, then the commit word
        # strictly after it ("Continue to Payment" navigates to the payment
        # STEP; it does not pay). A conjunction shape ("Continue & Place
        # Order") says it commits, so it never passes here — and Tier 3
        # filters commit labels too, so such a page ends at the boundary.
        for c in controls:
            if c.get("kind") != "button" or c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name.lower() in self._submit_approvals:
                continue
            if vocab.is_destination_advance(name):
                return AdvanceDecision(control=c, tier=2)

        # DANGER FORWARD (disposable blanket only): tiers 1-2 skip refuse-pack
        # danger controls, but an application's forward step can BE one —
        # "Continue to Underwriting Decision" is danger via rp.verb.underwrite, yet
        # it is the real next page toward e-sign. On a disposable env where submit
        # is blanket-approved, cross it through the SUBMIT path (a plain advance
        # click would be blocked by the network guard in EXPLORE phase). Returned as
        # submit_control so the walk crosses rather than clicks. Only a forward-shaped
        # (commit/advance) danger control qualifies — never "Back", never a nav link,
        # never a sign-out — so the walk cannot be sent sideways. This runs BEFORE
        # the oracle precisely so a real forward step is never passed over for a nav
        # link the oracle might otherwise pick.
        # A4.3: the gate here was `self._submit_approve_all` — the THIRD and last
        # site that made an irreversible forward step crossable only under "*".
        # An operator who had named this exact control was refused by this line
        # and never told why. It now asks the same authorisation ladder the
        # executor enforces, so a per-control grant reaches the crossing and a
        # crawl with no approvals behaves exactly as it always did.
        if self._submit_enabled:
            for c in controls:
                if c.get("kind") not in ("button", "link") or c.get("disabled"):
                    continue
                if not c.get("danger"):
                    continue
                name = str(c.get("name") or "").strip()
                if not name or _AUTH_SESSION_RE.search(name):
                    continue
                if not (_WIZARD_COMMIT_RE.search(name)
                        or _WIZARD_ADVANCE_RE.search(name)):
                    continue
                if self._may_attempt_crossing(
                        name=name, control=c, url=page_url, fingerprint=fingerprint):
                    return AdvanceDecision(submit_control=dict(c))

        # Tier 3: ask the agent which control advances the flow.
        if not self._oracle.advance_configured:
            return AdvanceDecision()
        candidates = self._tier3_candidates(controls)
        if not candidates:
            # Everything forward was a commit/danger control — that is the
            # submit boundary, not an oracle question.
            return AdvanceDecision()

        # Memo: same fingerprint ⇒ same inventory ⇒ same answer. One LLM call
        # per unique stuck page per crawl.
        if fingerprint and fingerprint in self._oracle_memo:
            memo_name, memo_status, memo_sig = self._oracle_memo[fingerprint]
            if memo_status == ORACLE_PICKED and memo_name:
                for c in candidates:
                    if str(c.get("name") or "").strip().lower() == memo_name:
                        return AdvanceDecision(
                            control=c, tier=3,
                            oracle_status=ORACLE_PICKED, signature=memo_sig)
            # A NEGATIVE answer is only reusable while the page offers the SAME
            # actionable controls. A wizard step's Continue is disabled until its
            # form is filled, so the first visit legitimately has no advance —
            # replaying that against a later, FILLED visit made the page
            # permanently unadvanceable. Observed on the VKPower quote funnel:
            # coverage/ and personal/ answered oracle=none on every later visit
            # and each was recorded as its own one-step "journey".
            if memo_status == ORACLE_NONE and memo_sig == _candidate_sig(candidates):
                return AdvanceDecision(
                    oracle_status=ORACLE_NONE, signature=memo_sig)

        # TIER-3 CONSULTATION TELEMETRY (Track 3.3). Tier 3 is the mechanism that
        # makes "any label convention" true, and it had never been observed
        # working: a crawl whose advances were all tier-1 is indistinguishable
        # from a crawl whose oracle was dead. An oracle outage must show up as a
        # number, not as mysterious one-step journeys.
        outcome = await self._oracle.advance(candidates, page_title, page_url)
        if not isinstance(outcome, dict):
            self._oracle.note_unavailable()
            return AdvanceDecision(oracle_status=ORACLE_UNAVAILABLE)
        status = str(outcome.get("status") or "")
        signature = str(outcome.get("signature") or "")
        idx = outcome.get("index")
        if status == ORACLE_PICKED and isinstance(idx, int) and 0 <= idx < len(candidates):
            picked = candidates[idx]
            if fingerprint:
                self._oracle_memo[fingerprint] = (
                    str(picked.get("name") or "").strip().lower(),
                    ORACLE_PICKED, signature)
            self._oracle.note_pick()
            logger.warning(
                "qec.oracle.picked url=%s control=%r — TIER 3 CHOSE A CONTROL "
                "no label rule could reach", (page_url or "")[:120],
                str(picked.get("name") or "")[:60])
            return AdvanceDecision(
                control=picked, tier=3,
                oracle_status=ORACLE_PICKED, signature=signature)
        if status == ORACLE_NONE:
            # Memoized against the ACTIONABLE CANDIDATE SET, not the fingerprint
            # alone: "nothing advances here" stays true only while the page keeps
            # offering the same enabled controls. "Nothing advances here" is only true of
            # the page AS IT WAS. A wizard step's Continue is disabled until its
            # form is filled, so the FIRST visit legitimately has no advance —
            # and caching that answer against the fingerprint makes the page
            # permanently unadvanceable, including on the very next visit when
            # the form IS filled and Continue is live. Observed on the VKPower
            # quote funnel: coverage/ and personal/ returned oracle=none on every
            # later visit and each was recorded as its own one-step "journey".
            #
            # A POSITIVE pick is still memoized above: it stays true for the same
            # state and is what the memo exists to save. A negative is cheap to
            # re-ask and is exactly the answer that goes stale.
            if fingerprint:
                self._oracle_memo[fingerprint] = (
                    None, ORACLE_NONE, _candidate_sig(candidates))
            return AdvanceDecision(oracle_status=ORACLE_NONE, signature=signature)
        # Transport failure, cap, open circuit, or an unreadable reply: the
        # decision was NOT made. Never memoized, never "none".
        return AdvanceDecision(oracle_status=ORACLE_UNAVAILABLE)

    async def _pick_advance(
        self, controls: Sequence[dict[str, Any]],
        page_url: str = "", page_title: str = "", fingerprint: str = "",
    ) -> AdvanceDecision:
        """POSTURE-routed advance decision.

        A journey-completion crawl (an attested non-prod environment, or an
        explicit e2e request) runs the full 3-tier detection; a probe keeps the
        strict regex and no oracle state.

        WHY THE POSTURE AND NOT THE MODE. Tier 1 recognises a forward control only
        when its label carries a generic advance word, so an application whose
        steps read "Save and Return", "Review Application" or "See My Quote" had
        no advance at all and every journey was recorded one step deep — live, six
        flows on a carrier admin app, all ``steps: 1``. Button wording is not
        something a test tool gets to standardise across a thousand applications;
        identifying the forward control is the tool's job, and on an environment
        the operator has attested it should use every means it has.

        The commit boundary is NOT relaxed by this: tiers 1-2 veto commit words
        per-control (tier 2 admits one only in destination position — "Continue to
        Payment" navigates, it does not pay), and tier 3's candidate set is
        commit-filtered before the oracle sees it. Crossing a real submit remains
        the separately-gated, disposable-attested path."""
        if self._full_traversal:
            return await self._pick_advance_e2e(
                controls, page_url, page_title, fingerprint)
        strict = self._pick_wizard_advance(controls)
        if strict is not None:
            return AdvanceDecision(control=strict, tier=1)
        return AdvanceDecision()

    async def _walk_wizard(
        self, *, item: FrontierItem, url: str, title: str,
        controls: Sequence[dict[str, Any]], fingerprint: str,
        base_actions: list[emit.ActionRecord], entry_shot: tuple[bytes, int],
        first_seen_ms: int, displayed_values: Sequence[dict[str, Any]],
        network_calls: Sequence[dict[str, Any]],
        entry_pick: Optional[AdvanceDecision] = None,
    ) -> bool:
        """Walk a multi-step wizard from a filled form step: click the advance
        control, RECORD each grounded step in place, repeat.  Returns True when it
        took over recording (so ``_expand`` must not re-record), False when there
        is nothing to advance.

        Safety (SAFETY-SENSITIVE submit boundary): bounded by
        :data:`_MAX_WIZARD_STEPS` / :data:`_MAX_WIZARD_ADVANCES` / ``max_depth``,
        deduped by state fingerprint (no loop, no entry-step re-walk), and
        FAIL-CLOSED — an advance happens only when the click has a real observed
        effect AND yields a new unseen state; a no-op/loop ends the walk.  The
        advance control passed that tier's gates: the danger gate and the
        approved-submit-name exclusion hold in EVERY tier, Tier 1 additionally
        vetoes commit words per-control, Tier 2 permits a commit word only as a
        destination ("Continue to Payment" navigates; it does not pay), and
        Tier-3 candidates are commit-filtered before the oracle sees them — so
        no tier can hand this walk a control that crosses the submit boundary."""
        if entry_pick is None:
            entry_pick = await self._pick_advance(controls, url, title, fingerprint)
        # DIAGNOSTIC: a walk that declines leaves the page recorded as a
        # single-page flow, so a five-page funnel becomes five one-step
        # "journeys". WHICH gate declined is the difference between a dedup
        # (the page was already walked) and a page we could not advance at all,
        # and those need opposite fixes. Value-free: reason + url only.
        if fingerprint in self._wizard_states:
            logger.info("qec.wizard.declined reason=already_walked url=%s", url)
            return False
        if entry_pick.control is None:
            # The verdicts ride on the SAME line as the decline. They were a
            # separate log record, so every grep for the decline lost the
            # explanation and every grep for the verdicts lost the page — the
            # two facts that only mean something together.
            logger.info("qec.wizard.declined reason=no_advance_control url=%s "
                        "tier=%s oracle=%s controls=%d verdicts=%s",
                        url, entry_pick.tier, entry_pick.oracle_status,
                        len(controls or ()),
                        getattr(self, "_last_advance_verdicts", []))
            return False
        self._wizard_states.add(fingerprint)

        # _discover (hover-reveal + click-pass) may have navigated the live page via a
        # goto reset, discarding the Phase-A fills done for THIS step. Re-establish the
        # FILLED entry step so a validation-gated advance actually fires (a nav menu on
        # the wizard page must not silently defeat the walk). The re-fill's action
        # records are redundant with base_actions (the canonical Phase-A fills) and are
        # DISCARDED — the recorded step keeps its original snapshot + fill actions.
        await self._goto_keeping_login(item.url)
        reobs = await self._observe()
        refreshed = build_inventory(reobs.raw_controls, self._refuse_pack, url=reobs.url)
        # The re-fill's ledger is ALSO the entry step's truth: real fill counts
        # and the decision points (forks) this step offered — Journey Graph C0.
        cur_filled, cur_unfilled, cur_intent_unmet = 0, 0, 0
        cur_dps: list[dict[str, Any]] = []
        if any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c) for c in refreshed):
            refill = await fill_form_phase_a(
                self._port, refreshed, self._answer_key or AnswerKey(), self._clock,
                phase=Phase.EXPLORE.value, state_id=fingerprint,
                identity=self._identity, recalled=self._recalled_values,
                journey_values=self._journey_values,
                priors=self._field_priors, data_mode=self._data_mode,
                choice_overrides=self._choice_overrides)
            cur_filled = refill.filled
            cur_unfilled = len(refill.unfilled_fields)
            cur_intent_unmet = refill.intent_unmet
            self._open_choice_unverified += refill.open_choice_unverified
            self._note_fills_by_kind(refill.filled_by_kind)
            cur_dps = _decision_points(refill.field_ledger)
            # The walk re-navigates and re-fills its ENTRY step from scratch, so
            # an unblock the outer form path already won has been undone by the
            # time we get here. Without this the walk cannot even start on a step
            # whose forward control the app gates on an undeclared question.
            entry_blocked = self._note_advance_blocked(refreshed, reobs.url or "", refill)
            if entry_blocked:
                refreshed = list(await self._answer_to_unblock(
                    refreshed, entry_blocked, reobs.url or "", refill))
                if self._last_unblock_field:
                    cur_filled += 1
                    cur_unfilled = max(0, cur_unfilled - 1)
                    cur_dps = _decision_points(refill.field_ledger)
                    # The page genuinely changed, so the snapshot taken before
                    # the answer is now stale — and it is the one the loop reads
                    # its first advance from. Only on success: an unchanged page
                    # keeps the caller's snapshot exactly as before.
                    controls = refreshed

        cur_url, cur_title, cur_controls, cur_fp = url, title, controls, fingerprint
        # THIS WALK's own path. Loop protection has to mean "I have already been
        # here IN THIS JOURNEY" — not "the crawler has seen this page at some
        # point". The crawler's outer loop visits every funnel page as a state in
        # its own right, so testing against the global set meant a walk could
        # never advance THROUGH a page that had been seen, and a five-page quote
        # funnel was recorded as a chain of two-step fragments terminating in
        # `loop`. The journey was walked; the evidence just never said so.
        walk_seen: set[str] = {fingerprint}
        # T-SI-01..03 — THE IDENTITY AUTHORITY FOR THIS JOURNEY.
        #
        # ``walk_seen`` above is only as good as the identities put into it, and
        # for a one-question-at-a-time questionnaire those identities used to be
        # all the same one: url, interactive controls and dialogs are constant
        # across every step, so twenty steps hashed to ONE digest and the second
        # step was rejected as already-visited. The walk terminated at step 1 and
        # reported a twenty-question funnel as a one-step fragment.
        #
        # WalkIdentity holds the previous step, which is what the hasher cannot
        # see, and admits a discriminating signal only when one is actually
        # OBSERVED — the DOM's own question grouping first, then what an answer
        # revealed, then the pixels. When nothing differs it returns the previous
        # digest unchanged, so a no-op Continue is still caught as a loop instead
        # of minting a fake step. One instance per walk; nothing global.
        # SEED THE ENTRY STEP WITH ITS OWN SIGNALS. An unseeded entry carries an
        # empty structural digest and no perceptual hash, so the FIRST advance
        # compared a real signature against a blank one, called that a
        # difference, and minted a distinct state for a click that had done
        # nothing — the exact green-wash this layer exists to prevent, arriving
        # through the back door. The entry screenshot is already in hand
        # (``entry_shot``), so its hash costs one aHash per WALK and no I/O.
        identity = WalkIdentity(
            self._fingerprinter, entry_fingerprint=fingerprint,
            entry_signals=StepSignals(
                base=fingerprint,
                structural=structural_signature(controls),
                perceptual=perceptual_hash_png(
                    entry_shot[0] if entry_shot else b"")))
        cur_actions = list(base_actions)
        cur_shot, cur_first = entry_shot, first_seen_ms
        cur_dv, cur_nc = displayed_values, network_calls
        depth, steps = item.depth, 0
        flow_steps: list[dict[str, Any]] = []
        # M1.4 · THE JOURNEY'S OWN CONFIRMATION, once one has been OBSERVED.
        # Empty until a step lands on a page the application declared a success
        # (see :func:`app.boundary.is_confirmation_landing`); non-empty, it is
        # both the reason this walk ends and the evidence recorded with it.
        confirm_rung, confirm_detail = "", ""
        # EVERY TEXT THIS JOURNEY HAS ALREADY SEEN — the "before" side of the
        # confirmation diff, widened from one step to the whole walk.
        #
        # A confirmation is text that is NEW. Diffing only against the PREVIOUS
        # page makes that test far too weak on a walk, because a walk can go
        # BACKWARDS: click something on a dead-end page, land back on step one,
        # and step one's own "You will receive a confirmation email once
        # submitted" is, relative to the page just left, brand new. Measured on a
        # fixture built to be a plain loop, that scored `transition_text` and
        # completed a journey that had gone in a circle. Text the journey has
        # read before is not news, wherever it reappears.
        walk_texts: list[str] = []
        walk_status: list[str] = []


        def _step_record(**extra: Any) -> dict[str, Any]:
            """The CURRENT step's flow entry: its own fill counts and its own
            decision points (never the next step's — the off-by-one this
            replaces)."""
            rec: dict[str, Any] = {
                "fingerprint": cur_fp, "url": cur_url, "title": cur_title,
                "fields_filled": cur_filled, "fields_unfilled": cur_unfilled,
            }
            if cur_intent_unmet:
                rec["intent_unmet"] = cur_intent_unmet
            # A next-action fork on THIS step (the quote-summary terminal:
            # Apply Now / Start Over / Back to Dashboard). Merged for every step,
            # but the emitter returns [] unless the page presents a forward commit
            # action + an alternative — so ordinary wizard steps (whose only button
            # is an advance-word Continue) contribute nothing. This is what records
            # the fork when the summary is the walk's TERMINAL, which the standalone
            # _expand path never reaches (the walk consumes the page).
            dps = list(cur_dps)
            dps.extend(_next_action_decisions(cur_controls, cur_fp))
            if dps:
                rec["decision_points"] = dps
            rec.update(extra)
            return rec

        while True:
            # NO-CREDENTIALS multi-step login wall: this wizard step presents a SECRET
            # (password/PIN) we cannot fill, still inside the gated entry login flow →
            # STOP honestly before submitting synthetic data and looping. The walk owns
            # recording, so record this step and end (the explore loop sees _hard_stop).
            if self._wizard_auth_gate(cur_controls):
                self._record_state(
                    url=cur_url, title=cur_title, controls=cur_controls, fingerprint=cur_fp,
                    actions=cur_actions, screenshots=[cur_shot],
                    first_seen_ms=cur_first, last_seen_ms=self._clock.now_ms(),
                    displayed_values=cur_dv, network_calls=cur_nc)
                self._stop_reason = STOP_AUTH_REQUIRED
                self._hard_stop = True
                return True
            # Answer a bare-button questionnaire BEFORE deciding the advance: the
            # gated "Continue" only unlocks once the questions are answered, so
            # without this the walk dead-ends on a page like /apply/lifestyle. One
            # question per pass; re-observe so the next click uses a fresh ordinal.
            # A no-op on any page without a repeated-option questionnaire (returns
            # []), so it never touches ordinary steps.
            # Gated on TRAVERSAL, not on submit. Answering a health/lifestyle
            # question is a FILL — it commits nothing and crosses no boundary (the
            # option filter below excludes every commit, advance, danger and auth
            # control, and an EXPLORE-phase mutating request is blocked by the
            # network guard regardless). Requiring submit rights to answer a
            # question meant an app whose "Continue" is validation-locked behind a
            # questionnaire dead-ended on that page and recorded a one-step journey,
            # even on an environment attested for full traversal. Never in
            # observe-only: production is catalogued, never interacted with.
            # M1.4 · ``not confirm_rung`` — see THE CONFIRMATION IS THE END OF
            # THE JOURNEY below. Answering questions on a page that has already
            # said "Application Submitted" answers nothing.
            if ((self._submit_enabled or self._full_traversal)
                    and not self._observe_only and not confirm_rung):
                pre_q_controls = cur_controls    # snapshot for the trigger→child diff
                q_dps = await self._answer_questionnaire(cur_controls, cur_url, cur_fp)
                if q_dps:
                    obs_q = await self._observe()
                    cur_controls = build_inventory(
                        obs_q.raw_controls, self._refuse_pack, url=obs_q.url)
                    cur_url = obs_q.url
                    # Record what THIS answer activated (trigger→child, P1): the
                    # controls that appeared after the click but were absent before
                    # it. Attached to the question just answered so the fold stores
                    # it on the walked branch — "Yes reveals these, No does not".
                    revealed = flow_ledger.activated_signatures(
                        pre_q_controls, cur_controls)
                    if revealed:
                        q_dps[-1]["reveals"] = revealed
                    # T-SI-03. The reveal delta is already computed here and was
                    # already value-free (``kind:accessible-name`` counts, never a
                    # user value) — it was recorded as evidence and thrown away as
                    # IDENTITY. Answering "Yes" to "any pre-existing conditions?"
                    # can expand the same step into a different one; feeding the
                    # delta to the identity layer is what lets that count as a
                    # distinct state. RESYNC, not advance: we are still standing on
                    # the step we were on, so the step counter must not move.
                    cur_fp, _q_signals = identity.identify(
                        url=obs_q.url, controls=cur_controls,
                        dialogs=obs_q.dialog_flags, revealed=revealed,
                        page_token=obs_q.page_token)
                    identity.resync(cur_fp, _q_signals)
                    walk_seen.add(cur_fp)
                    cur_dps = list(cur_dps) + q_dps   # record the question on this step
                    continue
            # M1.3 · ENTER THE LOGICAL STEP. Resets the per-step mutation
            # budget deterministically, exactly once per step, before anything
            # on this step can be actuated. A no-op without a verified proof.
            self._begin_walk_step(journey_id=fingerprint, step_index=steps,
                                  step_fingerprint=cur_fp)
            # This step's own NON-ADVANCING persistence (Save Draft / calculate
            # quote / check eligibility). It writes server state and does NOT
            # move the funnel, so it is actuated here and the advance is still
            # decided by the tiers below — a walk that counted Save Draft as an
            # advance would record a step that never happened (fixture 10).
            persist_action = (
                None if confirm_rung
                else await self._maybe_persist_walk_step(
                    cur_controls, cur_url, cur_fp))
            if persist_action is not None:
                cur_actions.append(persist_action)
                obs_p = await self._observe()
                cur_controls = build_inventory(
                    obs_p.raw_controls, self._refuse_pack, url=obs_p.url)
                cur_url = obs_p.url
                # RESYNC, not advance — the same precedent (and the same
                # reasoning) as the questionnaire path above: the page changed,
                # we are still standing on the step we were standing on, so the
                # step counter must not move.
                cur_fp, _p_signals = identity.identify(
                    url=obs_p.url, controls=cur_controls,
                    dialogs=obs_p.dialog_flags, page_token=obs_p.page_token)
                identity.resync(cur_fp, _p_signals)
                walk_seen.add(cur_fp)
            # ── M1.4 · THE CONFIRMATION IS THE END OF THE JOURNEY ───────────
            #
            # The previous step landed on a page the APPLICATION declared a
            # success. Everything the walk does from here — filling, persisting,
            # picking an advance — exists to move a funnel FORWARD, and there is
            # no forward left: what a confirmation page offers is a handful of
            # ways to LEAVE (Back to Dashboard, Home, Print, New Application,
            # sometimes a bare "Continue"), and each of them starts a different
            # journey or ends this one somewhere it has already been.
            #
            # Walking one of them is what produced the bug this milestone
            # closes. The click either did nothing or landed on an already-seen
            # state, so the walk recorded ``loop`` / ``completed=false`` for a
            # funnel it had just driven to a confirmation number. Declining to
            # advance out of a terminal is what makes ``confirmation`` a terminal
            # rather than one more step, and it is deliberately the ONLY thing
            # that changes here: a walk that never observed a confirmation takes
            # exactly the path it always took, loop detection included.
            pick = (AdvanceDecision() if confirm_rung
                    else await self._pick_advance(
                        cur_controls, cur_url, cur_title, cur_fp))
            if pick.submit_control is not None:
                # A danger forward step the advance tiers had to skip (e.g.
                # "Continue to Underwriting Decision"). Record THIS page as a crossed
                # submit boundary, then cross it through the submit path so the
                # application funnel continues toward e-sign. The crossing pushes the
                # next page onto the frontier; the walk ends here and the outer loop
                # picks the continuation up.
                flow_steps.append(_step_record())
                self._flows.append(flow_ledger.build_flow(
                    entry_fingerprint=fingerprint, entry_url=url, entry_title=title,
                    steps=flow_steps, terminal=flow_ledger.TERMINAL_SUBMIT_BOUNDARY,
                    terminal_url=cur_url,
                    outcome_values=[
                        v for v in _displayed_values(cur_dv or ())
                        if str(v.get("value_type") or "")
                        in _BOUNDARY_OUTCOME_TYPES],
                    max_steps=self._max_wizard_steps))
                # The journey is recorded BEFORE the crossing is attempted, so a
                # crossing that refuses, throws or never returns still leaves the
                # walk in the ledger with an honest `submit_boundary` terminal.
                # If it lands, _link_crossing_to_flow upgrades this entry in
                # place — the flow is never written twice and never left claiming
                # a crossing that did not happen.
                flow_index = len(self._flows) - 1
                milestones_before = len(self._outcome_milestones)
                await self._execute_approved_submit(
                    name=str(pick.submit_control.get("name") or "").strip(),
                    control=pick.submit_control, url=cur_url, fingerprint=cur_fp,
                    depth=item.depth, renavigate=False)
                self._link_crossing_to_flow(flow_index, milestones_before)
                return True
            trig = pick.control
            advance: Optional[tuple[Any, list[dict[str, Any]], str]] = None
            # T-SI-04 — TWO BUDGETS, AND THIS WALK IS ONLY BOUND BY ONE.
            #
            # ``max_depth`` (default 6) bounds how far the CRAWL FRONTIER
            # expands: how many links deep from the seed a new state may be
            # discovered. ``max_wizard_steps`` (default 60 on a full-traversal
            # posture) bounds how far ONE JOURNEY is walked. They answer
            # different questions, and this loop was gated on both.
            #
            # ``depth`` starts at the frontier depth of the step we entered on
            # and increments per wizard step, so a questionnaire reached two
            # links in had FOUR steps of headroom before ``depth`` hit 6 — a
            # twenty-question funnel stopped at question four and recorded
            # TERMINAL_BUDGET, with the 60-step budget the operator configured
            # never once consulted. The frontier is still bounded: max_depth is
            # enforced at all four expansion points in ``discovery.py`` and in
            # ``submit.py``, none of which this touches. What changes is that a
            # journey is no longer truncated by a number that was never about
            # journeys.
            budget_left = (steps < self._max_wizard_steps
                           and self._wizard_advances < self._max_wizard_advances)
            stopped = bool(self._tracker.stop_reason() or self._cancelled)
            if trig is not None and budget_left and not stopped:
                await self._politeness_delay()
                # M1.3 · THE ADVANCE ITSELF MAY PERSIST. This is the root cause:
                # a server-validated Next POSTs the step and the server will not
                # render the next one until it has. The window is open for THIS
                # click only, and closes before the observation below — so an
                # autosave that fires a second later is refused exactly as it is
                # today. Without a verified proof the window never opens and the
                # phase never leaves EXPLORE: byte-identical to before.
                trig_name = str((trig or {}).get("name") or "")
                # M1.4 · THE BEFORE SIDE. Captured immediately adjacent to the
                # click, exactly as the submit path captures it, because a
                # confirmation is a TRANSITION — text that was absent before and
                # present after. Reading the after-state alone would score a form
                # that says "you will receive a confirmation email" as a
                # confirmation before anything had happened. Best-effort by
                # construction: a port that implements neither optional verb
                # yields empty readings and the walk behaves exactly as before.
                before_side = await capture_page_declarations(self._port)
                walk_texts = _recent_text_history(
                    walk_texts + before_side["texts"])
                walk_status = _recent_text_history(
                    walk_status + before_side["status"])
                async with self._walk_persistence_window(trig_name):
                    observation = await self._port.click(trig)
                after_side = await capture_page_declarations(self._port)
                self._tracker.note_request()
                action = emit.build_action_record(
                    dict(trig), verb="click", value=None, observation=observation,
                    phase=Phase.EXPLORE.value, state_id=cur_fp,
                    timestamp_ms=self._clock.now_ms())
                self._tracker.note_action()
                outcome = str((action.after or {}).get("outcome") or "")
                obs = await self._observe()
                new_controls = build_inventory(obs.raw_controls, self._refuse_pack, url=obs.url)
                # ── M1.4 · THE CLASSIFIER THE WALK NEVER CALLED ─────────────
                #
                # ``build_action_record`` above runs ``classify_after``, which
                # has no ``confirmation`` branch at all — so the one classifier
                # in the codebase that can say "this landed on a confirmation"
                # was reachable only from the submit tier, and a funnel whose
                # last step is an ordinary advance could not produce the verdict
                # by any path.
                #
                # Decided HERE, below the inventory, because the landing page's
                # control names are part of the evidence: a button is an offer,
                # not a statement. The M1.2 confirmation page offers "Print
                # Confirmation" three lines from the banner that is the real
                # declaration, and without the inventory the diff could return
                # the button.
                #
                # ``observation`` is left PRISTINE for the action record already
                # built above; the derived detail is supplied to the classifier
                # on a copy, so nothing about the manifest changes.
                landed_detail, landed_rung = confirmation_transition(
                    walk_texts, after_side["texts"],
                    aria_before=walk_status, aria_after=after_side["status"],
                    control_names=[str(c.get("name") or "") for c in new_controls])
                landed = classify_submit_after(
                    replace(observation, confirmation_detail=landed_detail)
                    if landed_detail and not (observation.confirmation_detail or "").strip()
                    else observation)
                if landed.outcome == OUTCOME_CONFIRMATION and not landed_rung:
                    # A dialog-borne confirmation: ``classify_submit_after``
                    # reached it through ``dialog_opened``, which the text diff
                    # cannot see. Same reconciliation as the submit path.
                    landed_rung = RUNG_DIALOG if observation.dialog_opened else ""
                # T-SI-01/02 — IDENTIFY, never hash directly. The identity layer
                # decides which signals this observation has EARNED (see
                # WalkIdentity); the walk's job is to hand it everything it has
                # observed and to pay for a screenshot only when the DOM has
                # already proved insufficient.
                probe_png: Optional[bytes] = None
                probe_phash = ""
                if identity.needs_perception(url=obs.url, controls=new_controls,
                                             dialogs=obs.dialog_flags,
                                             page_token=obs.page_token):
                    # RUNG 4, and the only rung that costs I/O. Reached only when
                    # url, controls, dialogs AND the declared question grouping
                    # are all identical to the previous step — a DOM-opaque
                    # surface, or a wizard whose steps differ in rendered text
                    # alone. The shot is reused as this step's evidence
                    # screenshot below, so a successful advance pays nothing
                    # extra; only a stalled step (which ends the walk anyway)
                    # costs one capture.
                    probe_png = await self._port.screenshot_png()
                    probe_ts = self._clock.now_ms()
                    probe_phash = perceptual_hash_png(probe_png or b"")
                # T-ND-04 — the post-action observation may have come from a
                # DIFFERENT page than the pre-action one (a target=_blank step
                # adopted a new tab). The identity follows the page the
                # inventory above was read from, so a popup can never inherit
                # the opener's fingerprint.
                new_fp, new_signals = identity.identify(
                    url=obs.url, controls=new_controls,
                    dialogs=obs.dialog_flags, perceptual_hash=probe_phash,
                    page_token=obs.page_token)
                # a GENUINE advance: an observable effect AND a state this WALK
                # has not already been through (see ``walk_seen``).
                # A NEW STATE IS ITSELF THE EVIDENCE.
                #
                # The click-time outcome classifier only sees navigation, DOM
                # mutation on the clicked node, or a dialog. An SPA wizard that
                # advances by re-rendering in place trips none of those, so it
                # reports 'none' while the page has demonstrably become the next
                # step. Requiring it vetoed real advances: observed live on the
                # VKPower funnel —
                #     clicked='Continue' outcome='none' same_fp=False
                #                        already_in_walk=False
                # — the fingerprint had changed and the state was unvisited, and
                # the walk still refused, recording a five-page funnel as
                # two-step fragments.
                #
                # The fingerprint is the stronger signal: it is computed from a
                # FRESH observation taken after the click (url + controls +
                # dialogs), not from the click event. A fingerprint this journey
                # has not seen IS a step forward. The outcome is kept as
                # corroboration in the action record, never as the gate.
                # M1.4 · DID THIS STEP LAND ON A RECOGNIZED CONFIRMATION?
                #
                # Decided BEFORE and INDEPENDENTLY of whether the click counted
                # as an advance, because both answers matter and they are not the
                # same question. A submit that navigates to a confirmation page
                # advances (and that page becomes this journey's last step); a
                # submit that re-renders the SAME page with a success banner, or
                # one that drops the walk back onto a state it has already
                # visited, does NOT advance — and that is precisely the case the
                # ledger used to record as ``loop``.
                #
                # ``changed`` is the anti-green-wash conjunct: the page must have
                # actually moved (a new URL, or an interactive shape this journey
                # has not seen). Without it an inline "Saved successfully" toast
                # on step two would end a nine-step funnel.
                if is_confirmation_landing(
                        outcome=landed.outcome, rung=landed_rung,
                        changed=bool(landed.navigated or new_fp != cur_fp)):
                    confirm_rung, confirm_detail = landed_rung, landed_detail
                    logger.warning(
                        "qec.wizard.confirmation url=%s rung=%s detail=%r — the "
                        "application DECLARED this journey complete; the walk "
                        "stops here rather than clicking its way off the "
                        "confirmation page and recording a loop.",
                        (obs.url or cur_url)[:120], landed_rung,
                        (landed_detail or "")[:80])
                if new_fp != cur_fp and new_fp not in walk_seen:
                    cur_actions.append(action)
                    walk_seen.add(new_fp)
                    identity.advance(new_fp, new_signals)
                    advance = (obs, new_controls, new_fp)
                # WALK TRACE. Six rounds of reasoning about why a five-page funnel
                # records as two-step fragments have each disproved the previous
                # theory. This states, per step, exactly which of the three
                # advance conditions failed — an unchanged page (outcome), a
                # no-op click (same fingerprint), or a state this journey already
                # visited — instead of collapsing all three into "loop".
                else:
                    logger.info(
                        "qec.wizard.step_stalled url=%s clicked=%r outcome=%r "
                        "same_fp=%s already_in_walk=%s",
                        cur_url, str((trig or {}).get("name") or "")[:30],
                        outcome or "(none)", new_fp == cur_fp,
                        new_fp in walk_seen)

            # record the CURRENT step (its fills + the onward advance click if any).
            self._record_state(
                url=cur_url, title=cur_title, controls=cur_controls, fingerprint=cur_fp,
                actions=cur_actions, screenshots=[cur_shot],
                first_seen_ms=cur_first, last_seen_ms=self._clock.now_ms(),
                displayed_values=cur_dv, network_calls=cur_nc)
            if advance is None:
                # WHY the walk ended decides whether this journey was covered. A
                # submit boundary or a step with nothing to advance means the funnel
                # was walked to its end; running out of budget means it was not, and
                # the difference must survive into the report.
                #
                # M1.4 — the ORDER of those reasons is now stated once, in
                # ``flow_ledger.resolve_walk_terminal``, instead of being implied
                # by the shape of an ``if`` chain here. This site keeps the two
                # judgements only IT can make: whether the walk was stopped, and
                # (when nothing was clickable) which of the three "nothing left"
                # verdicts applies.
                nothing_to_click = ""
                if trig is None:
                    if pick.oracle_status == ORACLE_UNAVAILABLE:
                        # The regex tiers found nothing and the agent could not
                        # be reached — whether more funnel existed is UNKNOWN.
                        # Unknown is never reported as covered; even a commit
                        # button on this page does not upgrade the walk to a
                        # covered boundary, because a non-commit advance may
                        # have existed that nobody could identify.
                        nothing_to_click = flow_ledger.TERMINAL_ORACLE_UNAVAILABLE
                    elif self._pick_submit_candidate(cur_controls):
                        nothing_to_click = flow_ledger.TERMINAL_SUBMIT_BOUNDARY
                    else:
                        nothing_to_click = flow_ledger.TERMINAL_NO_ADVANCE
                terminal = flow_ledger.resolve_walk_terminal(
                    cancelled=stopped,
                    confirmation=bool(confirm_rung),
                    nothing_to_click=nothing_to_click,
                    budget_left=budget_left)
                flow_steps.append(_step_record())
                self._flows.append(flow_ledger.build_flow(
                    entry_fingerprint=fingerprint, entry_url=url, entry_title=title,
                    steps=flow_steps, terminal=terminal, terminal_url=cur_url,
                    # NORMALISE FIRST. collect_displayed_values returns the raw
                    # {label, selector, text} nodes; ``value_type`` is added by
                    # the value-oracle inference in _displayed_values. Filtering
                    # the raw list on a key it does not carry matched NOTHING, so
                    # a journey that walked to a displayed premium recorded no
                    # outcome at all — the funnel was proven and the proof was
                    # thrown away one line before it was stored.
                    outcome_values=[
                        v for v in _displayed_values(cur_dv or ())
                        if str(v.get("value_type") or "")
                        in _BOUNDARY_OUTCOME_TYPES],
                    max_steps=self._max_wizard_steps,
                    # M1.4 — WHAT the app said and on which rung, recorded with
                    # the terminal it justifies. The ledger drops both unless the
                    # terminal actually IS ``confirmation``, so this cannot be
                    # used to dress up any other ending.
                    confirmation_rung=confirm_rung,
                    confirmation_detail=confirm_detail))
                # CROSS the boundary. The walk reached a submit boundary (a quote
                # summary with "Apply Now") and recorded it; now, on a disposable
                # attested env, click the approved forward action IN PLACE (the
                # summary's state was built by this walk — never re-navigate) and
                # push the resulting page (/portal/apply) so the application → e-sign
                # funnel is crawled as the continuation. Gated exactly as any submit;
                # a non-disposable env leaves self._submit_enabled False and stops at
                # the boundary as before.
                if terminal == flow_ledger.TERMINAL_SUBMIT_BOUNDARY:
                    flow_index = len(self._flows) - 1
                    milestones_before = len(self._outcome_milestones)
                    await self._maybe_submit_next_action(
                        controls=cur_controls, url=cur_url, fingerprint=cur_fp,
                        depth=item.depth)
                    self._link_crossing_to_flow(flow_index, milestones_before)
                return True

            obs, new_controls, new_fp = advance
            # WHO decided this advance — per-step audit evidence (tier 3 = the
            # agent decided; its value-free decision signature rides along so a
            # PROVEN pick can be harvested into tenant advance memory).
            advance_evidence: dict[str, Any] = {
                "tier": pick.tier,
                "control_name": str((trig or {}).get("name") or "")[:120],
                "oracle": pick.tier == 3,
            }
            if pick.signature:
                advance_evidence["signature"] = pick.signature
            flow_steps.append(_step_record(advance=advance_evidence))
            self._visited_fingerprints.add(new_fp)
            self._wizard_advances += 1
            steps += 1
            depth += 1
            # capture + fill the new step so a validation-gated onward Next can fire.
            step_first = self._clock.now_ms()
            # REUSE the perceptual probe's capture when rung 4 already took one:
            # it is a shot of exactly this page at exactly this moment (both are
            # taken after the advance observation and before this step's fills),
            # so re-capturing would buy an identical image for a second
            # round-trip. Absent a probe (the common path) this is unchanged.
            if probe_png is not None:
                step_png, step_ts = probe_png, probe_ts
            else:
                step_png = await self._port.screenshot_png()
                step_ts = self._clock.now_ms()
            step_actions: list[emit.ActionRecord] = []
            # The NEW step's own truth — attached to ITS record when it is
            # appended (advance or terminal), never to the step just left.
            cur_filled, cur_unfilled, cur_intent_unmet, cur_dps = 0, 0, 0, []
            if any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c) for c in new_controls):
                filled = await fill_form_phase_a(
                    self._port, new_controls, self._answer_key or AnswerKey(), self._clock,
                    phase=Phase.EXPLORE.value, state_id=new_fp,
                    identity=self._identity, recalled=self._recalled_values,
                    journey_values=self._journey_values,
                    priors=self._field_priors, data_mode=self._data_mode,
                    choice_overrides=self._choice_overrides)
                step_actions.extend(filled.actions)
                self._tracker.note_action(len(filled.actions))
                cur_filled = filled.filled
                cur_unfilled = len(filled.unfilled_fields)
                cur_intent_unmet = filled.intent_unmet
                self._open_choice_unverified += filled.open_choice_unverified
                self._note_fills_by_kind(filled.filled_by_kind)
                cur_dps = _decision_points(filled.field_ledger)
                self._collect_ledger(filled.field_ledger, obs.url or "")
                if filled.filled:
                    after_fill = await self._observe()
                    new_controls = build_inventory(
                        after_fill.raw_controls, self._refuse_pack, url=after_fill.url)
                # THE APP'S OWN VERDICT, AT EVERY STEP OF THE WALK. Step 4 of a
                # five-step application is only ever reached from inside this
                # loop, so the hook on the outer form path could never see the
                # block that actually ends the journey — the outer path sees
                # step 1 and nothing after it. This is where a wizard dies, so
                # this is where the question has to be asked.
                blocked = self._note_advance_blocked(
                    new_controls, obs.url or "", filled)
                if blocked:
                    new_controls = list(await self._answer_to_unblock(
                        new_controls, blocked, obs.url or "", filled))
                    if self._last_unblock_field:
                        # The step's own record must reflect the answer, or it
                        # reports a question as unanswered that the application
                        # confirmed we answered.
                        cur_filled += 1
                        cur_unfilled = max(0, cur_unfilled - 1)
                        cur_dps = _decision_points(filled.field_ledger)
            cur_url, cur_title, cur_controls, cur_fp = obs.url, obs.title, new_controls, new_fp
            cur_actions = step_actions
            cur_shot, cur_first = (step_png, step_ts), step_first
            cur_dv = await self._port.collect_displayed_values()
            cur_nc = await self._drain_network()
