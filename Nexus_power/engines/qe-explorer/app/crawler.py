"""QE-Central Contained Explorer — the CRAWLER state machine (design §3.2).

Drives the ``AUTH → EXPLORE → [SUBMIT-deferred]`` state machine over the
:class:`app.browser.BrowserPort`, emitting confidence-1.0 evidence in the exact
field vocabulary the compiler binds on:

  * a priority FRONTIER keyed on state fingerprints — each unique state is
    visited ONCE (:class:`Frontier` dedups on ``url_template`` at push time and
    on the full ``state_fingerprint`` at expand time);
  * BUDGETS with an honest ``stop_reason`` (:class:`Budget` /
    :class:`BudgetTracker`) — the crawl always reports WHY it stopped, never
    silently truncates;
  * POLITENESS (``rate_per_s`` inter-navigation delay) so a single crawler never
    hammers a client host;
  * RESUME-from-manifest — a re-run continues past the durable prefix without
    revisiting states or colliding sequence/frame indices;
  * every navigation / click / type becomes a grounded ``action`` record whose
    ``after`` bundle is CLASSIFIED from the observed effect
    (:func:`app.browser.classify_after`), never invented; a screenshot is
    captured for every recorded state.

Containment: the crawler NEVER clicks a control the fail-closed guard flags as
irreversible, and never clicks a form's submit button in EXPLORE (Phase A stops
before submit).  The hard net remains the network guard (wired in
:mod:`app.main`); this module is defense-in-depth on top of it.

The state machine is pure orchestration over the port — no Playwright import —
so it is unit-testable with a scripted fake (``tests/test_crawler_logic.py``).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import heapq
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from . import completion
from . import danger_signals
from . import emit
from . import matcher
from . import perception
from . import resume_state
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
from .browser import BrowserPort, PageObservation
from .coverage import CoverageLedger
from .emitter import MetaEmitter
from .filler import ControlFiller, _FIELD_KINDS
from .oracle_gateway import OracleGateway
from .auth_flow import AuthFlowMixin
from .discovery import DiscoveryMixin
from .submit import SubmitMixin
from .walker import WalkerMixin
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
    STOP_INVENTORY_FAILED,
    STOP_RESUME_UNRECOVERABLE,
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
from .boundary import (ApprovalRegistry, CrossingLedger, parse_grants)
from .guard_context import GuardContext
from .budget import (STOP_MAX_REQUESTS, STOP_MAX_STATES, STOP_MAX_WALL_MS,
                     Budget, BudgetTracker, TraversalBudget)
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

logger = logging.getLogger(__name__)


























# ─── The guard context (phase + AUTH-window shared with the route handler) ───




# ─── Crawl summary ───────────────────────────────────────────────────────────


@dataclass
class CrawlSummary:
    crawl_id: str
    stop_reason: str
    states: int
    actions: int
    screenshots: int
    guard_blocks: int
    manifest_path: str
    storage_state: Optional[dict[str, Any]] = None
    detail: str = ""
    #: HOW FAR the crawl actually got: the greatest frontier depth it dequeued
    #: and expanded. Distinct from the max_depth BUDGET (what it was allowed) —
    #: the gap between the two is the difference between "stopped because it ran
    #: out of app" and "stopped because it ran out of permission".
    max_depth_reached: int = 0
    #: What the crawl found vs could fill/advance (forms_found, fields_inferred,
    #: fields_needing_seed, submit_candidates) — the coverage the operator sees.
    coverage: Optional[dict[str, Any]] = None
    #: M1.7 — the terminal DISPOSITION adjudicated from evidence
    #: (:mod:`app.completion`): ``completed`` / ``failed`` / ``incomplete``.
    #: ``stop_reason`` says WHAT happened; this says whether the crawl may be
    #: believed.  qe-central reads this rather than re-deriving the judgement
    #: from a string it would have to keep in sync.
    disposition: str = ""
    #: The evidence the disposition was adjudicated FROM, so the decision can be
    #: re-checked by a reader who does not trust the process that made it.
    evidence: Optional[dict[str, Any]] = None
    #: True when a completion CLAIM was refused for want of evidence — the
    #: milestone's "recovery must always be observable", applied to completion.
    downgraded: bool = False
    #: M1.7 / T-GW-04 — business rules this crawl PROVED, for qe-central to
    #: persist as durable, reusable knowledge.
    discovered_rules: Optional[list] = None


# ─── The crawler ─────────────────────────────────────────────────────────────


class Crawler(AuthFlowMixin, SubmitMixin, DiscoveryMixin, WalkerMixin):
    """The contained explorer's crawl driver.

    One instance = one crawl.  Construct with a :class:`BrowserPort`, the crawl
    identity + evidence knobs, and a validated :class:`Budget`; call
    :meth:`run`.  Progress is observable via :meth:`progress` (for
    ``GET /api/v1/explore/{id}``); :meth:`cancel` requests a graceful stop.
    """

    def __init__(
        self,
        port: BrowserPort,
        *,
        crawl_id: str,
        tenant_id: str,
        target_url: str,
        work_dir: str,
        refuse_pack: Any,
        budget: Budget,
        explorer_version: str,
        guard_version: str,
        refuse_pack_version: str,
        config_fingerprint: str,
        guard_context: GuardContext,
        answer_key: Optional[AnswerKey] = None,
        recalled_values: Optional[dict[str, str]] = None,
        field_priors: Optional[dict[str, Any]] = None,
        identity_seed: str = "",
        data_mode: str = "user",
        crawl_mode: str = "explore",
        traversal: str = TRAVERSAL_PROBE,
        credentials: Optional[Credentials] = None,
        session_injected: bool = False,
        allowed_hosts: Sequence[str] = (),
        max_relogins: int = 3,
        submit_approvals: Sequence[str] = (),
        boundary_approvals: Any = (),
        wizard_enabled: bool = True,
        plan: Optional[dict[str, Any]] = None,
        scope_path_prefixes: Sequence[str] = (),
        sleep: Any = asyncio.sleep,
        advance_oracle: Optional[Callable[..., Any]] = None,
        vision_oracle: Optional[Callable[..., Any]] = None,
        choice_overrides: Optional[Mapping[str, str]] = None,
        e2e_wizard_steps: int = _E2E_WIZARD_STEPS,
        e2e_wizard_advances: int = _E2E_WIZARD_ADVANCES,
        observe_only: bool = False,
        #: M1.7 / T-GW-03 — this dispatch is a RESUME of an existing crawl id.
        #: Changes nothing about HOW the crawl runs; it changes what a missing
        #: durable prefix MEANS.  For a fresh crawl an empty prefix is normal; for
        #: a resume it means the evidence we were told to continue is gone, and
        #: continuing anyway would replace a real crawl with an empty one.
        resume: bool = False,
        #: M1.7 / T-GW-04 — business rules earlier crawls of THIS app proved,
        #: already tenant- and app-scoped by qe-central.  Empty ⇒ byte-identical
        #: pre-M1.7 behaviour: every blocked advance runs the full experiment.
        known_rules: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._port = port
        self.crawl_id = crawl_id
        self.tenant_id = tenant_id
        self.target_url = target_url
        self.work_dir = work_dir
        self._refuse_pack = refuse_pack
        self._budget = budget
        self._explorer_version = explorer_version
        self._guard_version = guard_version
        self._refuse_pack_version = refuse_pack_version
        self._config_fingerprint = config_fingerprint
        self._guard = guard_context
        self._answer_key = answer_key or AnswerKey()
        # FIELD LEARNING. `recalled_values` is tenant-private data decrypted for this
        # one crawl — it is never logged, never emitted into the manifest, and never
        # crosses a process boundary. `field_priors` is pooled and value-free.
        self._recalled_values = dict(recalled_values or {})
        # JOURNEY MEMORY — {signature: value} for questions THIS crawl has already
        # answered. A funnel asks the same thing more than once (an email on the
        # contact step and again on the confirmation step; a policy number
        # captured on one screen and required on the next), and re-deriving each
        # sighting independently produced a different answer each time — which the
        # application then rejected on its own cross-validation, dead-ending the
        # walk on a validation error it was right to raise.
        #
        # Same discipline as ``recalled_values``: in-process for the life of one
        # crawl, never logged, never emitted, never persisted.
        self._journey_values: dict[str, str] = {}
        self._field_priors = dict(field_priors or {})
        # ONE person for the whole crawl: regenerating per form would produce a
        # postcode belonging to a different region than the one selected two steps
        # earlier, which is exactly what an application cross-validates.
        self._identity = derive_identity(identity_seed or "qec")
        # "user" = today's behaviour (a radio group is the client's choice to make);
        # "agent" = answer everything honestly answerable, recording each choice.
        self._data_mode = str(data_mode or "user").strip().lower()
        # "explore" / "target" / "e2e". Only the step budget differs — every safety
        # gate is identical in all three, because a deeper walk must not be a
        # laxer one.
        self._crawl_mode = str(crawl_mode or "explore").strip().lower()
        # TRAVERSAL POSTURE — how far a business journey may be WALKED. Derived by
        # qe-central from the env attestation the operator signed, so a test
        # environment needs no second dial. Unknown values fail closed to "probe".
        # This is not a safety dial: what may be CLICKED is decided by the refuse
        # pack, the danger gate and the disposable-attestation submit tier, none of
        # which read this value.
        self._traversal = str(traversal or TRAVERSAL_PROBE).strip().lower()
        if self._traversal not in TRAVERSAL_POSTURES:
            self._traversal = TRAVERSAL_PROBE
        # A JOURNEY-COMPLETION crawl: walk each funnel to its end and catalogue it,
        # rather than sampling a probe's worth of it. True for an attested non-prod
        # environment, or for an explicitly requested e2e crawl (which is what "e2e"
        # has always meant).
        self._full_traversal = (self._traversal == TRAVERSAL_FULL
                                or self._crawl_mode == "e2e")
        # WALK + CATALOGUE bounds, derived from the posture. Unified into
        # TraversalBudget (T-DE-14) so the crawl's two budget systems sit
        # together; the individual attributes are kept as the collaborators'
        # read interface, and every value is unchanged.
        self._traversal_budget = TraversalBudget.for_posture(
            full_traversal=self._full_traversal,
            e2e_wizard_steps=e2e_wizard_steps,
            e2e_wizard_advances=e2e_wizard_advances)
        self._max_wizard_steps = self._traversal_budget.max_wizard_steps
        self._max_wizard_advances = self._traversal_budget.max_wizard_advances
        self._max_option_probes = self._traversal_budget.max_option_probes
        self._max_probed_options = self._traversal_budget.max_probed_options
        self._max_dep_probes = self._traversal_budget.max_dep_probes
        # BUSINESS FLOWS: one entry per journey walked, carrying whether it actually
        # REACHED THE END. Six steps of a fifteen-step funnel is not the Apply flow.
        self._flows: list[dict[str, Any]] = []
        # E2E: when regex cannot identify the advance control, ask the LLM.
        self._advance_oracle = advance_oracle
        self._vision_oracle = vision_oracle
        # BRANCH WALK (Journey Graph C4): {field signature → forced option
        # label}. Applies ONLY to enumerable controls and ONLY to options they
        # themselves offer (forms rung 0, ``planned`` provenance) — never free
        # text, never a value injection, and no safety gate changes with it.
        self._choice_overrides = dict(choice_overrides or {})
        self._observe_only = bool(observe_only)
        # Every fillable control the crawl met, filled or not — the residue ask and
        # the learning loop are both keyed on this. Values are NOT in it.
        self._field_ledger: list[dict[str, Any]] = []
        self._credentials = credentials
        # A tier-4 storageState was injected for this crawl (captcha/SSO logins the
        # crawler cannot script). It is injected UNVERIFIED, so the AUTH phase must
        # prove it still holds — see :meth:`_maybe_authenticate`.
        self._session_injected = bool(session_injected)
        # AUTH IS A CAPABILITY, NOT A PHASE. Held for the whole crawl so a login
        # wall met mid-journey is answered and WALKED THROUGH, rather than ending
        # the journey there. Built in _maybe_authenticate; None when the operator
        # gave us nothing to log in with.
        self._authenticator: Optional[Authenticator] = None
        self._max_relogins = max_relogins
        self._sleep = sleep

        self._target_host = (urlsplit(target_url).hostname or "").lower()
        self._allowed_hosts = {h.strip().lower() for h in allowed_hosts if str(h).strip()}
        self._allowed_registrable = {registrable_domain(h) for h in self._allowed_hosts}
        self._allowed_registrable.add(registrable_domain(self._target_host))
        # TARGET MODE (R3 Mode 2): normalised path prefixes the crawl is CONFINED
        # to. Only well-formed absolute paths survive; trailing slashes are
        # stripped (except root) so "/quote" matches "/quote" and "/quote/x"
        # but never "/quotes". Empty ⇒ classic whole-app Explore mode.
        self._scope_path_prefixes: tuple[str, ...] = tuple(dict.fromkeys(
            (p.rstrip("/") or "/")
            for p in (str(s).strip() for s in scope_path_prefixes)
            if p.startswith("/")
        ))

        # ── RESUME (M1.7 / T-GW-03) ─────────────────────────────────────────
        # ONE read of the durable prefix feeds BOTH halves of a resume: what the
        # crawl had SEEN (visited fingerprints, sequence/frame indices, the clock
        # offset) and what it still had TO DO (the frontier).  Reading the file
        # twice would let the two halves describe different moments, which is a
        # subtler version of exactly the bug being fixed.
        records = emit.read_records(work_dir, crawl_id)
        prior = emit.scan_resume_state(records)
        self._resume_requested = bool(resume)
        self._resume_plan = resume_state.rebuild(records, resuming=bool(resume))
        self._visited_fingerprints: set[str] = set(prior["visited_fingerprints"])
        self._next_seq = int(prior["next_sequence_index"])
        self._clock = emit.MonotonicClock(offset_ms=int(prior["last_timestamp_ms"]))
        self._emitter = emit.ManifestEmitter(
            work_dir, crawl_id, self._clock,
            next_frame_index=int(prior["next_frame_index"]),
        )
        self._tracker = BudgetTracker(budget, self._clock)
        self._frontier = Frontier(_parse_plan_patterns(plan))
        # ── DURABLE LEARNING (M1.7 / T-GW-04) ───────────────────────────────
        self._known_rules = rules.KnownRules(known_rules or ())
        self._rule_ledger = rules.RuleLedger()
        if self._known_rules:
            logger.info(
                "qec.rules.loaded crawl_id=%s known=%d — blocked advances proved "
                "by earlier crawls of this app will be answered from knowledge "
                "instead of re-run as experiments", crawl_id, len(self._known_rules))
        #: M0.3 collaborators. They read crawl state through the
        #: interfaces they declare (see state_identity.RecorderHost),
        #: so this object satisfies a contract rather than being one.
        #: NOTE: NOT ``_identity`` — that name is already taken by the crawl's
        #: fictional PERSON (derive_identity above), and shadowing it silently
        #: swaps the person for a hasher.
        self._fingerprinter = StateFingerprinter()
        self._recorder = StateRecorder(self)
        self._coverage = CoverageLedger(self)
        self._meta_emitter = MetaEmitter(self, _attestation_dict)
        self._filler = ControlFiller(self)
        #: The ONE seam to an LLM. Owns the tier-3 memo and the
        #: consultation telemetry that used to be five loose counters.
        self._oracle = OracleGateway(advance_oracle, vision_oracle, self._clock)

        self._cancelled = False
        self._stop_reason = ""
        self._done = False
        self._guard_blocks = 0
        self._storage_state: Optional[dict[str, Any]] = None
        # Auth was requested (credentials supplied) but no login form could be driven at
        # the entry — the page is accessible PUBLIC content, so we explore it rather than
        # throw it away. Surfaced LOUDLY in coverage so the operator knows authenticated
        # areas were NOT covered (never a silent, green-washed "success").
        self._auth_incomplete = False
        self._auth_incomplete_reason = ""
        # Set when the entry is behind a login wall and NO credentials/session were
        # supplied — an honest hard STOP (STOP_AUTH_REQUIRED), not partial coverage.
        # Surfaced as coverage.auth_blocked so the app UI says "behind a login, no
        # credentials — record a login" instead of "didn't reach it — re-crawl".
        self._auth_blocked_reason = ""
        # A login was DRIVEN and verified at least once this crawl. It makes
        # "the session expired — re-record" an impossible diagnosis: signing in
        # demonstrably works, so a wall we keep meeting is the app failing to PERSIST
        # the login, not a stale recording.
        self._login_verified = False
        # An honest terminal condition raised mid-expand (currently: the no-credentials
        # login-wall block). The explore loop checks it and stops WITHOUT treating the
        # stop as cancellation or budget exhaustion.
        self._hard_stop = False
        # ── M1.7 / T-GW-01 · unrecovered inventory reads ────────────────────
        # Counted, not merely logged: it is an INPUT to the completion verdict
        # (app.completion.adjudicate), so a crawl that failed to read a page can
        # never claim completion no matter which code path reaches the end.
        self._inventory_failures = 0
        self._inventory_failure_detail = ""
        # NO-CREDENTIALS AUTH-WALL tracking — single-screen AND multi-step / username-
        # first. `_auth_flow_active`: we are inside the gated ENTRY login flow (anchored
        # at a redirected entry that landed on a dedicated auth step). `_captured_public`:
        # the crawl has recorded genuine public content, so any later login page is a
        # deeper gated sub-area to SKIP, never the entry wall to abort on.
        self._auth_flow_active = False
        self._captured_public = False
        # Destinations we have already GROUNDED a nav click to (across all states), so
        # a nav bar repeated on every page grounds each unique route ONCE — the cost of
        # direct-nav grounding stays ~O(unique navs), not O(states × links).
        self._grounded_navs: set[str] = set()
        # Grounded [click → navigation] records produced by :meth:`_reach_in_app`
        # while crossing an auth wall, drained by :meth:`_expand` into the state
        # they reached. They cannot be returned directly: the click happens before
        # the destination state exists, and the landing page it happens FROM is
        # never recorded as a state of its own.
        self._pending_reach_actions: list[emit.ActionRecord] = []
        # The state id stamped on those records. Set to the destination's
        # fingerprint by _expand once known, so the action belongs to a state that
        # actually exists in the manifest rather than to a phantom one.
        self._pending_reach_state_id: str = ""
        # (source_fingerprint, link_label) for the grounded navigation edge that
        # reached the next state. Held until _expand knows the destination's
        # fingerprint, then emitted as a real state→state edge.
        self._pending_reach_edge: Optional[tuple[str, str]] = None
        # Reach keys released back to the frontier after an expansion landed
        # somewhere other than its requested URL — once per key, so a genuine
        # redirect gets one retry and can never loop.
        self._rearmed_keys: set[str] = set()
        # Coverage accounting (crawl-once/run-many legibility): what the crawl found
        # vs could actually fill/advance, so the shallow-vs-full gap is visible and the
        # human's remediation is a NAMED, targeted seed request — never blind guessing.
        self._forms_found = 0
        self._fields_inferred: list[str] = []      # filled with a synthesized default
        self._fields_unfilled: list[str] = []      # no seed AND no safe default -> needs seed
        # Per-field PAGE context for the ones needing a seed: {label, url}. The label
        # alone can't say WHICH flow a field belongs to; the page it appeared on can, so
        # the Seed Manifest can group "to test Transfer, provide these" grounded in the
        # actual page — not a keyword guess. First-appearance order; deduped in coverage.
        self._fields_seed_detail: list[dict[str, str]] = []
        # OPAQUE surfaces the DOM can't read (cross-origin embeds, canvas apps, closed
        # shadow) — detected + named so the coverage ledger flags a blind spot instead of a
        # silent skip. {kind, label, reason}; deduped in coverage.
        self._opaque_surfaces: list[dict[str, str]] = []
        # Interactive controls the matcher registry has NO primitive for — named in the
        # ledger as UNHANDLED (on the roadmap), never a silent skip. {label, kind}.
        self._unhandled_controls: list[dict[str, str]] = []
        self._submit_candidates: list[str] = []    # a submit found but not clicked (Phase-A boundary)
        # Funnels that stopped WITH a forward control present but DISABLED by the
        # app's own validation: [{url, label, reason, missing_fields}]. Names the
        # field whose absence stopped the walk, so a one-step journey explains
        # itself instead of being investigated.
        self._advance_blocked: list[dict[str, Any]] = []
        # Per-button verdicts from the most recent tier-1 miss, carried so the
        # DECLINE line can state why it declined. Kept in memory only.
        self._last_advance_verdicts: list[str] = []
        #: {fingerprint: {ax_fingerprint, location, form_snapshot_signals}} — the
        #: questions each state asked, which is what turns a walked journey into
        #: a catalogue. See _note_state_signals.
        self._states: dict[str, dict[str, Any]] = {}
        #: Choice widgets opened, picked, and unable to confirm the answer back.
        self._open_choice_unverified: int = 0
        #: Committed fills by control kind — see _note_fills_by_kind.
        self._filled_by_kind: dict[str, int] = {}
        #: Greatest frontier depth actually dequeued and expanded (M0.6).
        self._max_depth_reached: int = 0
        #: The question the last unblock experiment answered ("" when it did not
        #: run or did not succeed) — read by the walk to correct the step's own
        #: fill counts and decision points.
        self._last_unblock_field: str = ""
        # Phase-B attested submit (crawl-once/run-many depth): default-OFF. Fires ONLY
        # when the operator supplied a per-flow submit-approval list AND a disposable-env
        # attestation is present — a crawl without both stops at the Phase-A boundary,
        # byte-identical to before. execute_submit_phase_b re-verifies the guard.
        self._submit_approvals = {s.strip().lower() for s in submit_approvals if str(s).strip()}
        # "*" — every submit this app offers is approved. Set by qe-central for an
        # env the operator attested DISPOSABLE: naming each control one at a time is
        # the right ceremony for a live system and pure friction for a throwaway one,
        # which is what the attestation already says this is.
        self._submit_approve_all = "*" in self._submit_approvals
        # NOTE: the enable gate is evaluated AFTER _boundary_grants is built
        # (below) so a crawl authorised purely by per-control grants — the
        # least-privilege shape, with no legacy label list at all — is not
        # silently disabled by a check that only knows about the old seam.
        self._forms_submitted = 0
        #: Submits the APPLICATION confirmed (navigation or success), a subset
        #: of _forms_submitted. See the increment site.
        self._forms_confirmed = 0
        self._submitted_flows: set[str] = set()    # dedup key = f"{fingerprint}::{name}"
        # ── A4.3 THE APPROVED SUBMIT CROSSING ────────────────────────────────
        # The seam that did not exist. `submit_approvals` is a set of LABELS and
        # cannot express "this control, on this page, once" — so crossing an
        # irreversible control was only ever reachable through the "*" blanket,
        # which authorises every submit in the application at once. These carry
        # the per-control grant instead; see app.boundary for the full argument.
        #
        # Parse errors are RAISED, not swallowed: an approval that silently
        # evaluates to nothing is indistinguishable from a crawl that stopped at
        # the boundary for a good reason, and the operator would re-issue the
        # same broken grant forever without being told why.
        self._boundary_grants = ApprovalRegistry(parse_grants(boundary_approvals))
        #: EXACTLY-ONCE. Reserved BEFORE the click, so a crash mid-crossing still
        #: leaves the boundary spent. See CrossingLedger.
        self._crossings = CrossingLedger()
        #: The verified landings. THE canonical journey outcome (T-AC-04) — never
        #: a counter, never the click.
        self._outcome_milestones: list[dict[str, Any]] = []
        #: Irreversible controls this crawl MET and did not cross — the list the
        #: operator picks an approval from. Kept strictly apart from
        #: _submit_candidates, which means "crossable without approval".
        self._approvable_boundary: list[dict[str, Any]] = []
        # Enabled by EITHER seam, and still only with a signed attestation:
        # gate_submit refuses in SUBMIT phase without one, so this flag can
        # never be the thing that authorises a mutation — it only decides
        # whether the crawl bothers to walk up to the boundary at all.
        self._submit_enabled = bool(
            self._submit_approvals or self._boundary_grants
        ) and self._guard.attestation is not None
        # M1.3 CONTROLLED WALK PERSISTENCE. Bind the crawl's manifest sink + wall
        # clock to the authorisation built in the request handler, so every
        # permitted mutation lands in THIS crawl's evidence. Absent (no verified
        # proof) this is a no-op and the crawl is byte-identical to before.
        walk_auth = getattr(self._guard, "walk_authorization", None)
        if walk_auth is not None:
            walk_auth.attach(sink=self._emitter.emit_walk_mutation,
                             wall_clock_ms=lambda: int(time.time() * 1000))
        # M1.5 — hand the browser port the two things it cannot know on its own:
        # the live journey context (for dialog intent) and this crawl's scope
        # test (so a popup onto a third-party origin is recorded and never
        # adopted). PUSHED, not imported: the port must keep no reference to the
        # crawler's module. Reached through getattr so a scripted fake, the
        # jsdom lane and every port written before M1.5 stay valid.
        for verb, value in (("bind_journey_context", self._journey_context),
                            ("bind_scope_check", self._in_scope)):
            binder = getattr(self._port, verb, None)
            if binder is not None:
                try:
                    binder(value)
                except Exception:  # pragma: no cover — a bind failure is not fatal
                    logger.warning("qec.crawler.%s_failed", verb, exc_info=True)
        #: Persistence controls actuated, keyed f"{fingerprint}::{name}" — one
        #: Save Draft per step, never a loop of them.
        self._walk_persisted: set[str] = set()
        #: Why walk persistence was NOT granted, for crawl_meta. Set by the
        #: request handler; "" when it was granted.
        self._walk_denied_reason: str = getattr(self._guard, "walk_denied_reason", "")
        # Questions answered on a bare-button questionnaire this crawl (by a stable
        # per-question signature), so a re-observe of the same page — which looks
        # identical because the buttons carry no selected-state — does not re-answer.
        self._answered_questions: set[str] = set()
        # Wizard/stepper traversal (#1): advance non-danger Next/Continue on filled
        # form states to record deeper steps (the SPA quote-wizard case). Bounded +
        # fingerprint-deduped + fail-closed (danger OR commit-word vetoes). ON by
        # default (the double gate is conservative); a kill-switch for unvetted apps.
        self._wizard_enabled = bool(wizard_enabled)
        self._wizard_advances = 0
        self._wizard_states: set[str] = set()      # entry-step fingerprints already walked

    # -- public control / observation -----------------------------------------

    # -- oracle telemetry (single home: the gateway) ---------------------------

    @property
    def _oracle_memo(self) -> dict[str, tuple[Optional[str], str, str]]:
        return self._oracle.memo

    @property
    def _oracle_consults(self) -> int:
        return self._oracle.consults

    @property
    def _oracle_errors(self) -> int:
        return self._oracle.errors

    @property
    def _oracle_unavailable(self) -> int:
        return self._oracle.unavailable

    @property
    def _oracle_latency_ms(self) -> int:
        return self._oracle.latency_ms

    @property
    def _oracle_picks(self) -> int:
        return self._oracle.picks

    def cancel(self) -> None:
        """Request a graceful stop; the loop flushes the manifest and reports
        the partial crawl with ``stop_reason='cancelled'``."""
        self._cancelled = True

    def now_ms(self) -> int:
        """The crawl's monotonic clock reading (for the route handler's guard
        decision + guard_event timestamps — one clock across the whole crawl)."""
        return self._clock.now_ms()

    def _collect_ledger(self, entries: list[dict[str, Any]], url: str) -> None:
        self._coverage.collect_ledger(entries, url)

    def _note_state_signals(
        self, fingerprint: str, url: str, signals: Mapping[str, Any],
        controls: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self._recorder.note_state_signals(fingerprint, url, signals, controls)

    def _note_fills_by_kind(self, counts: Mapping[str, int]) -> None:
        self._coverage.note_fills_by_kind(counts)

    def _state_signals(self) -> list[dict[str, Any]]:
        return self._recorder.state_signals()

    def _build_coverage(self) -> dict[str, Any]:
        return self._coverage.build()

    @property
    def emitter(self) -> emit.ManifestEmitter:
        return self._emitter

    @property
    def guard(self) -> GuardContext:
        return self._guard

    @property
    def max_depth_reached(self) -> int:
        """Greatest frontier depth actually dequeued and expanded (M0.6).

        Public because the terminal telemetry needs it even when the crawl
        produced no summary — a crawl that died mid-loop still got as far as it
        got, and reporting "unknown depth" for exactly the failures worth
        diagnosing is the blindness this milestone removes.
        """
        return self._max_depth_reached

    def note_network_guard_block(self) -> None:
        """Increment the guard-block counter (the route handler reports blocks
        it aborts so the crawl summary carries an honest total)."""
        self._guard_blocks += 1

    def progress(self) -> dict[str, Any]:
        return {
            "crawl_id": self.crawl_id,
            "running": not self._done,
            "phase": self._guard.phase.value,
            "stop_reason": self._stop_reason,
            "frontier": len(self._frontier),
            "visited_states": len(self._visited_fingerprints),
            "guard_blocks": self._guard_blocks,
            "max_depth_reached": self._max_depth_reached,
            **self._tracker.snapshot(),
        }

    # -- the crawl -------------------------------------------------------------

    async def run(self) -> CrawlSummary:
        """Execute the crawl and return an honest :class:`CrawlSummary`."""
        self._emit_initial_meta()
        detail = ""
        # -- M1.7 / T-GW-03 . A RESUME THAT CANNOT RESUME MUST NOT RUN --------
        # Checked BEFORE the browser is driven. A resume whose durable prefix is
        # gone has nothing to continue, and the one thing it must never do is
        # walk the app from zero under an id that already owns evidence - that
        # supersedes a real crawl with an empty capture, which is the most
        # destructive shape this whole class of bug takes.
        if self._resume_requested and not self._resume_plan.recoverable:
            logger.error(
                "qec.crawler.resume_unrecoverable crawl_id=%s - %s",
                self.crawl_id, self._resume_plan.refusal)
            self._stop_reason = STOP_RESUME_UNRECOVERABLE
            return self._finish(self._resume_plan.refusal)
        try:
            root_url = await self._maybe_authenticate()
            if root_url is None:
                self._stop_reason = self._stop_reason or STOP_AUTH_FAILED
            else:
                self._guard.phase = Phase.EXPLORE
                # GUARANTEE the operator-onboarded entry form is observed post-auth. On the
                # authenticated path, login often redirects target_url to a login page and
                # then lands on a DIFFERENT page (a dashboard); root_url is that landing, so
                # the one page the user explicitly onboarded (base_url) would be dropped
                # unless a nav happens to point back to it. Seed base_url at depth 0 FIRST
                # so it is freshly inventoried with the authenticated session; the frontier's
                # url_template dedup + unique-state fingerprint dedup make it a no-op when
                # login already landed there.
                self._frontier.push(
                    FrontierItem(url=self.target_url, depth=0),
                    key=_url_key(self.target_url),
                )
                if _url_key(root_url) != _url_key(self.target_url):
                    self._frontier.push(
                        FrontierItem(url=root_url, depth=0),
                        key=_url_key(root_url),
                    )
                # -- RESTORE THE WORK LIST (M1.7 / T-GW-03) ------------------
                # AFTER the entry seeds, so a resumed crawl still re-establishes
                # its entry point (the session is new; the app may land it
                # elsewhere) and the seeds keys are already spent before the
                # restore re-arms the rest. Nothing here can ADD reachability:
                # every restored item was reachable when it was queued.
                self._restore_frontier()
                await self._explore_loop()
        except Exception as exc:  # honest terminal error — never a silent crash
            self._stop_reason = STOP_ERROR
            detail = str(exc)[:500]
            logger.exception("qec.crawler.run_failed crawl_id=%s", self.crawl_id)

        # M1.5 — THE TERMINAL FLUSH. Whatever the crawl's exit path (completed,
        # budget, cancelled, auth-required, or the exception handler above), any
        # popup/dialog/download evidence still sitting in the port's buffer is
        # written before the terminal meta. A dialog that was answered and never
        # recorded would leave a crawl whose behaviour cannot be explained from
        # its own manifest, which is exactly what this milestone exists to fix.
        await self._drain_browser_events()
        return self._finish(detail)

    # -- durable resume (M1.7 / T-GW-03) --------------------------------------

    def _restore_frontier(self) -> int:
        """Re-queue the work list a previous run of THIS crawl id left behind.

        The half of a resume that was never written.  ``emit.scan_resume_state``
        restores what the crawl had SEEN; without this, what it still had TO DO
        was rebuilt empty on every start - so the restored visited-set matched
        the freshly-observed entry page, ``_expand`` took its unique-state early
        return, the frontier drained, and the run reported ``completed`` with
        zero states.  The recovery half actively CAUSED the zero-state
        completion; this is the missing counterweight.

        Order matters and is the reason this is a method rather than three lines
        at the call site:

          1. **Push the queued items first.**  Each carries the reach key it was
             deduped under, so pushing re-arms exactly the key it owns.
          2. **Then mark the consumed keys.**  These are routes an earlier run
             already dequeued and expanded.  Marking them AFTER the pushes is
             what stops a restored item from being rejected by a dedup set that
             already contains its own key - which would silently drop the work
             this method exists to restore.

        Ordering within the queue is deliberately NOT restored from the file:
        :meth:`app.frontier.Frontier.push` recomputes novelty rank and plan
        priority from the key, so re-pushing reproduces the same traversal order
        the original run would have had.  The snapshot only has to be COMPLETE,
        not ordered.

        Returns how many items were re-queued (0 for a fresh crawl).
        """
        plan = self._resume_plan
        if not plan.frontier and not plan.spent_keys:
            return 0
        requeued = 0
        for snapshot in plan.frontier:
            item = FrontierItem(
                url=snapshot.url, depth=snapshot.depth, priority=snapshot.priority,
                discovered_via=snapshot.discovered_via,
                parent_fingerprint=snapshot.parent_fingerprint,
            )
            if self._frontier.push(item, key=snapshot.key or _url_key(snapshot.url)):
                requeued += 1
        marked = self._frontier.mark_spent(plan.spent_keys)
        logger.warning(
            "qec.crawler.resume_restored crawl_id=%s requeued=%d spent_keys=%d "
            "prior_states=%d prior_actions=%d - this run CONTINUES the crawl; it "
            "does not start a new one",
            self.crawl_id, requeued, marked, plan.prior_states, plan.prior_actions)
        return requeued

    def _emit_checkpoint(self) -> None:
        """Persist the current work list into the durable manifest.

        Written after every expansion, into the SAME append-only file the
        evidence goes to.  One file means one crash-consistency story: a
        checkpoint can never describe a moment the evidence does not, because
        both are ordered by the same fsynced appends and
        ``emit.read_records`` truncates both at the same partial line.

        Best-effort by construction.  A checkpoint that cannot be written costs a
        resume some re-walking; a checkpoint that KILLS a running crawl costs the
        whole crawl.  The evidence records are the ones whose write failures are
        allowed to propagate.
        """
        try:
            self._emitter.emit_checkpoint(resume_state.build_checkpoint(
                frontier=self._frontier.snapshot_items(),
                visited=self._visited_fingerprints,
                states=self._tracker.states,
                actions=self._tracker.actions,
                spent_keys=self._frontier.spent_keys(),
                sequence_index=self._next_seq,
            ))
        except Exception:  # pragma: no cover - never fail a crawl for a checkpoint
            logger.warning("qec.crawler.checkpoint_failed crawl_id=%s", self.crawl_id,
                           exc_info=True)

    # -- the terminal verdict (M1.7 / T-GW-01, T-GW-03) ------------------------

    def _finish(self, detail: str) -> CrawlSummary:
        """Adjudicate the terminal state from EVIDENCE and build the summary.

        THE LINE THIS REPLACES was ``if not self._stop_reason: self._stop_reason
        = STOP_COMPLETED`` - "nothing set a reason, therefore we finished". That
        is an inference from an absence, and it is the last link in every
        green-wash chain in the engine: a failed inventory read, an unrecoverable
        resume and a crawl that never reached a page all arrive there with an
        empty ``_stop_reason`` and all three used to claim ``completed``.

        The claim now goes through :func:`app.completion.adjudicate`, which may
        only ever pull a verdict DOWN: it can refuse ``completed`` for want of
        evidence, and it can never turn a failure into a success or invent a
        reason nobody set.

        Reached from BOTH terminal paths - the normal exit and the early resume
        refusal - so there is exactly one place a crawl can end. A second exit
        that skipped the adjudicator would be a green-wash hole in the code that
        closes green-wash holes.
        """
        claimed = self._stop_reason or STOP_COMPLETED
        verdict = completion.adjudicate(
            claimed,
            completion.CrawlEvidence(
                states=self._tracker.states,
                actions=self._tracker.actions,
                resumed_states=self._resume_plan.prior_states,
                inventory_failures=self._inventory_failures,
                resumed=self._resume_requested,
                resume_broken=(self._resume_requested
                               and not self._resume_plan.recoverable),
            ),
        )
        self._stop_reason = verdict.stop_reason
        self._done = True
        if verdict.downgraded:
            # LOUD, and in the manifest below. A refused completion claim is the
            # single most important thing an operator can be told about a crawl,
            # and the failure mode being fixed here is precisely one that used to
            # be silent.
            logger.error(
                "qec.crawler.completion_refused crawl_id=%s claimed=%s verdict=%s "
                "evidence=%s - %s",
                self.crawl_id, verdict.claimed_stop_reason, verdict.stop_reason,
                verdict.evidence, verdict.detail)
        detail = detail or verdict.detail or self._inventory_failure_detail
        self._emit_terminal_meta(detail)
        summary = CrawlSummary(
            crawl_id=self.crawl_id,
            stop_reason=self._stop_reason,
            states=self._tracker.states,
            actions=self._tracker.actions,
            screenshots=self._emitter.frame_count,
            guard_blocks=self._guard_blocks,
            manifest_path=str(emit.manifest_path(self.work_dir, self.crawl_id)),
            storage_state=self._storage_state,
            detail=detail,
            coverage=self._build_coverage(),
            max_depth_reached=self._max_depth_reached,
            disposition=verdict.disposition,
            evidence=dict(verdict.evidence),
            downgraded=verdict.downgraded,
            discovered_rules=self._rule_ledger.as_list(),
        )
        logger.info("qec.crawler.completed crawl_id=%s stop_reason=%s states=%d "
                    "actions=%d screenshots=%d guard_blocks=%d",
                    self.crawl_id, self._stop_reason, summary.states,
                    summary.actions, summary.screenshots, summary.guard_blocks)
        # TIER-3 LIVENESS, ONE GREPPABLE LINE (Track 3.1). An all-tier-1 crawl
        # and a crawl whose oracle was never wired produce identical advance
        # counts, so "is tier-3 alive" was an inference from an absence. It is
        # now a fact printed on every crawl whether or not the oracle was used.
        logger.warning(
            "qec.oracle.liveness crawl_id=%s advance_oracle=%s consults=%d "
            "picks=%d unavailable=%d errors=%d",
            self.crawl_id,
            "configured" if self._advance_oracle is not None else "none",
            self._oracle_consults, self._oracle_picks,
            self._oracle_unavailable, self._oracle_errors)
        return summary

    # -- AUTH phase ------------------------------------------------------------











    # -- EXPLORE phase ---------------------------------------------------------

                # one bad state must not kill the crawl — continue honestly.






    # -- href-follow traversal (SPA-robust link following) ---------------------







    async def _probe_select_options(
        self, controls: Sequence[dict[str, Any]], *, url: str,
    ) -> None:
        await self._filler.probe_select_options(controls, url=url)

    def _set_options(self, control: dict[str, Any], options: Sequence[str]) -> None:
        self._filler.set_options(control, options)

    async def _commit_act(self, control: dict[str, Any]) -> bool:
        return await self._filler.commit_act(control)

    async def _commit_choice(self, control: dict[str, Any]) -> bool:
        return await self._filler.commit_choice(control)

    async def _probe_dependencies(
        self, controls: list[dict[str, Any]], *, url: str,
    ) -> None:
        await self._filler.probe_dependencies(controls, url=url)

    def _in_scope_key(self, url: str) -> str:
        """Path-level identity of a URL for the ACT-THEN-DIFF nav guard (host+path, query/
        hash-insensitive) — a same-page DOM change must NOT read as a navigation."""
        parts = urlsplit(url or "")
        return f"{(parts.hostname or '').lower()}{parts.path}"





    # -- wizard / stepper traversal (#1) ---------------------------------------











    # -- state recording -------------------------------------------------------

    def _record_state(self, **kwargs: Any) -> None:
        """Assemble + emit ONE ``page_state`` record with monotonic indices."""
        self._recorder.record_state(**kwargs)

    # -- helpers ---------------------------------------------------------------

    async def _observe(self) -> PageObservation:
        return await self._recorder.observe()

    async def _drain_network(self) -> list[dict[str, Any]]:
        return await self._meta_emitter.drain_network()

    async def _drain_browser_events(self) -> int:
        """M1.5 — flush the port's popup / dialog / download / page-close
        evidence into the manifest.  Best-effort; never raises."""
        try:
            return await self._meta_emitter.drain_browser_events()
        except Exception:  # pragma: no cover — evidence, never a crawl-stopper
            logger.warning("qec.crawler.browser_event_flush_failed", exc_info=True)
            return 0

    def _journey_context(self) -> dict[str, Any]:
        """The live journey context the browser port resolves dialog intent
        against (M1.5 / T-ND-02).

        A CALLABLE handed to the port rather than a value copied into it: the
        guard phase changes several times per crawl (explore → auth → walk →
        submit) and a snapshot taken at construction would be stale by the first
        dialog.  The port never imports the crawler; the crawler pushes this
        reader in, which keeps the dependency arrow pointing the same way M0.3
        set it.
        """
        try:
            phase = self._guard.phase.value
        except Exception:
            phase = ""
        return {
            "phase": phase,
            "observe_only": self._observe_only,
            # A4.3 grants. A native confirm is not an approval, so the ONLY way
            # a destructive dialog is ever accepted is an operator grant that
            # already exists for that control.
            "approved_labels": tuple(sorted(self._submit_approvals)),
        }

    async def _politeness_delay(self) -> None:
        rate = self._budget.rate_per_s
        if rate and rate > 0:
            await self._sleep(1.0 / rate)

    def _in_scope(self, url: str) -> bool:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not host:
            return False
        host_ok = (
            (self._target_host and same_registrable_domain(host, self._target_host))
            or host in self._allowed_hosts
            or registrable_domain(host) in self._allowed_registrable
        )
        if not host_ok:
            return False
        # TARGET MODE: the crawl is confined to the supplied journey's path
        # prefixes — a URL on the right host but outside every prefix is out of
        # scope (the whole point of Mode 2: exhaustive validation of ONE
        # workflow, no unrelated exploration). Query/fragment never matter.
        if not self._scope_path_prefixes:
            return True
        path = parts.path or "/"
        for p in self._scope_path_prefixes:
            if p == "/" or path == p or path.startswith(p + "/"):
                return True
        return False

    def _emit_initial_meta(self) -> None:
        self._meta_emitter.emit_initial_meta()

    def _emit_terminal_meta(self, detail: str) -> None:
        self._meta_emitter.emit_terminal_meta(detail)

    def _meta(self, *, stop_reason: str) -> dict[str, Any]:
        return self._meta_emitter.meta(stop_reason=stop_reason)

# ─── module helpers ──────────────────────────────────────────────────────────
