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
import heapq
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from . import emit
from . import value_infer
from .auth import Authenticator, AuthWindow, Credentials
from .browser import BrowserPort, PageObservation
from .fingerprint import state_fingerprint
from .forms import AnswerKey, execute_submit_phase_b, fill_form_phase_a
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

# ─── Honest stop reasons ─────────────────────────────────────────────────────
STOP_COMPLETED = "completed"
STOP_MAX_STATES = "budget_max_states"
STOP_MAX_REQUESTS = "budget_max_requests"
STOP_MAX_WALL_MS = "budget_max_wall_ms"
STOP_CANCELLED = "cancelled"
STOP_AUTH_FAILED = "auth_failed"
STOP_ERROR = "error"

#: Value-bearing kinds that make a state a "form" (buttons are then submit
#: candidates and are NOT auto-clicked in EXPLORE).
_FILLABLE_KINDS = frozenset({"text", "date", "select", "checkbox", "radio", "toggle"})
#: Kinds a nav-discovery pass may click (links always; buttons only on non-form
#: states, and never a guard-flagged irreversible one).
_ACTUATOR_KINDS = frozenset({"link", "button"})
#: Bound on hover-reveal probes per state (mega-menu / fly-out triggers) so a
#: nav-heavy page cannot spend the whole budget hovering.
_MAX_HOVER_REVEALS = 8
#: Bound on the revealed nav ITEMS a single opened menu is probed for (find the
#: one the open made clickable) — keeps a big mega-menu from spending the budget.
_MAX_MENU_ITEMS = 10
#: Bound on DIRECT nav-link click-groundings per state (top in-scope links clicked to
#: record a grounded [click → navigation]). Global dedup (``_grounded_navs``) grounds
#: each unique route once, so this only caps a single nav-heavy page.
_MAX_GROUND_NAVS = 12
#: Bound on network (XHR/fetch) calls recorded per state — a chatty SPA can fire
#: hundreds of requests; keep the evidence stream proportional to the API surface.
_MAX_NETWORK_CALLS = 100
#: Dropdown option-probe bounds — custom comboboxes probed per state, and options kept
#: per dropdown. Read-only (open → read labels → dismiss), bounded, state-restoring.
_MAX_OPTION_PROBES = 8
_MAX_PROBED_OPTIONS = 40
#: ACT-THEN-DIFF bounds — driver choices committed per state to reveal DEPENDENT fields
#: (a select whose options populate only after a prior field is chosen). EXPLORE-phase,
#: no submit; a committed choice is a valid filled-form state to snapshot.
_MAX_DEP_PROBES = 6
#: Wizard/stepper traversal (#1) bounds — steps per single wizard chain, and the
#: crawl-wide advance total; both cap the SAFETY-SENSITIVE submit-boundary probe.
_MAX_WIZARD_STEPS = 6
_MAX_WIZARD_ADVANCES = 24
#: ENTRY-goto retry bounds — the first navigation can race the per-dispatch
#: egress-fence reconfigure (squid allowlist re-read); retry it briefly.
_ENTRY_GOTO_RETRIES = 2
_ENTRY_RETRY_DELAY_S = 2.5

#: An "advance the wizard" control label (Next / Continue / Proceed / Forward) —
#: the POSITIVE intent signal.
_WIZARD_ADVANCE_RE = re.compile(r"\b(next|continue|proceed|forward)\b", re.I)
#: A commit / terminal-boundary label the refuse pack does NOT universally flag
#: (a generic "Submit"/"Confirm"/"Place order"/"Checkout"…). Its presence VETOES
#: an advance even when the guard did not mark the control danger — the second,
#: fail-closed gate over the guard so a wizard walk can never cross a submit.
_WIZARD_COMMIT_RE = re.compile(
    r"\b(submit|send|pay|paying|paid|payment|payments|buy|buying|purchase|"
    r"purchasing|order|checkout|check\s*out|place\s*order|confirm|finish|"
    r"complete|done|agree|accept|sign|book|reserve|schedule|activate|create|"
    r"register|subscribe|delete|cancel|remove|apply)\b",
    re.I,
)


def _is_wizard_advance(name: str) -> bool:
    """True for a Next/Continue/Proceed/Forward control that carries NO commit /
    terminal word — the fail-closed advance gate (any commit signal vetoes)."""
    n = (name or "").strip()
    if not _WIZARD_ADVANCE_RE.search(n):
        return False
    return not _WIZARD_COMMIT_RE.search(n)


# ─── Budgets ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Budget:
    """Crawl budgets (design §3.2 defaults; env-overridable via config)."""

    max_states: int = 200
    max_depth: int = 6
    max_actions_per_state: int = 30
    max_wall_ms: int = 1_800_000
    max_requests: int = 5000
    rate_per_s: float = 1.0

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "Budget":
        d = dict(data or {})
        base = cls()
        return cls(
            max_states=int(d.get("max_states", base.max_states)),
            max_depth=int(d.get("max_depth", base.max_depth)),
            max_actions_per_state=int(d.get("max_actions_per_state", base.max_actions_per_state)),
            max_wall_ms=int(d.get("max_wall_ms", base.max_wall_ms)),
            max_requests=int(d.get("max_requests", base.max_requests)),
            rate_per_s=float(d.get("rate_per_s", base.rate_per_s)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_states": self.max_states, "max_depth": self.max_depth,
            "max_actions_per_state": self.max_actions_per_state,
            "max_wall_ms": self.max_wall_ms, "max_requests": self.max_requests,
            "rate_per_s": self.rate_per_s,
        }


class BudgetTracker:
    """Tracks crawl progress against a :class:`Budget` and reports the honest
    terminal reason.

    ``requests`` counts CRAWLER-INITIATED browser operations (navigations +
    actions) — a deterministic, crawler-owned proxy for network volume (the
    literal network cap is enforced structurally by squid's host allowlist and
    the guard's method block, not by a counter).  ``elapsed_ms`` is measured
    from THIS run's start (not the resume offset) so the wall budget is per-run.
    """

    def __init__(self, budget: Budget, clock: emit.MonotonicClock) -> None:
        self.budget = budget
        self._clock = clock
        self._start_ms = clock.now_ms()
        self.states = 0
        self.actions = 0
        self.requests = 0

    def note_state(self) -> None:
        self.states += 1

    def note_action(self, n: int = 1) -> None:
        self.actions += n

    def note_request(self, n: int = 1) -> None:
        self.requests += n

    @property
    def elapsed_ms(self) -> int:
        return self._clock.now_ms() - self._start_ms

    def stop_reason(self) -> str:
        """Return the honest budget stop reason, or ``""`` while within budget.

        Precedence (deterministic, documented): wall-clock, then requests, then
        states — the hardest external constraint first.
        """
        if self.budget.max_wall_ms and self.elapsed_ms >= self.budget.max_wall_ms:
            return STOP_MAX_WALL_MS
        if self.budget.max_requests and self.requests >= self.budget.max_requests:
            return STOP_MAX_REQUESTS
        if self.budget.max_states and self.states >= self.budget.max_states:
            return STOP_MAX_STATES
        return ""

    def snapshot(self) -> dict[str, Any]:
        return {"states": self.states, "actions": self.actions,
                "requests": self.requests, "elapsed_ms": self.elapsed_ms}


# ─── Priority frontier ───────────────────────────────────────────────────────


@dataclass
class FrontierItem:
    """A state to visit, described by how to REACH it (a URL to goto in Phase 1)
    plus its BFS depth and (Phase-2 seed) priority."""

    url: str
    depth: int = 0
    priority: int = 0
    discovered_via: str = ""
    parent_fingerprint: str = ""


def _section_signature(url_template: str) -> str:
    """The app SECTION an item belongs to — the first two path segments of its
    (id-collapsed) ``url_template`` (``/account/settings/*`` → ``account/settings``,
    ``/`` → ``""``).  The unit of novelty for the information-gain planner."""
    path = urlsplit(url_template or "").path or ""
    segs = [s for s in path.split("/") if s][:2]
    return "/".join(segs)


#: The explorer RE-BOUNDS a plan from qe-central (defense in depth — a plan is
#: ordering data, never an attack surface): a safe substring pattern only, weight
#: clamped 1..3, at most 8 patterns.  Mirrors the qe-central planner validation.
_PLAN_MAX_PATTERNS = 8
_PLAN_PATTERN_RX = re.compile(r"^[a-z0-9][a-z0-9/_.#-]{0,59}$")


def _parse_plan_patterns(plan: Optional[dict[str, Any]]) -> list[tuple[str, int]]:
    """Project a dispatch ``plan`` dict onto bounded ``[(pattern, weight)]`` the
    frontier can apply.  Fully defensive: any malformed/oversized/unsafe entry is
    dropped, an empty result ⇒ a byte-identical crawl."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in ((plan or {}).get("priority_patterns") or ()):
        if not isinstance(item, dict) or len(out) >= _PLAN_MAX_PATTERNS:
            continue
        pattern = str(item.get("pattern") or "").strip().lower()[:60]
        if not pattern or pattern in seen or not _PLAN_PATTERN_RX.match(pattern):
            continue
        try:
            weight = max(1, min(3, int(item.get("weight") or 1)))
        except (TypeError, ValueError):
            weight = 1
        seen.add(pattern)
        out.append((pattern, weight))
    return out


class Frontier:
    """A min-priority queue of :class:`FrontierItem` deduped by reach key.

    Ordering is ``(priority, novelty_rank, depth, insertion)``:

      * ``priority``     — an explicit Phase-2 seed can still raise a critical
        route ahead of everything (unchanged);
      * ``novelty_rank`` — the INFORMATION-GAIN planner (#3): the Nth item queued
        from a given app SECTION gets rank N-1, so the FIRST item of every section
        is visited before any section's second item.  Under a finite state budget
        this spends the budget on breadth-of-app-regions (maximal new information)
        instead of draining one link-heavy section before touching the rest;
      * ``depth`` then ``insertion`` — breadth-first / FIFO within a novelty tier.

    Push-time dedup on the reach key (``url_template``) keeps the queue finite; the
    crawler additionally dedups on the full state fingerprint at expand time so
    distinct URLs that render the SAME state are visited once.
    """

    def __init__(self, plan_patterns: Sequence[tuple[str, int]] = ()) -> None:
        self._heap: list[tuple[int, int, int, int, FrontierItem]] = []
        self._seq = 0
        self._enqueued_keys: set[str] = set()
        #: information-gain planner: items already queued per app section, so a
        #: newly-seen section outranks the Nth sibling of a saturated one.
        self._section_counts: dict[str, int] = {}
        #: CAGED-PLANNER priorities: (lowercased substring, weight 1..3) grounded +
        #: validated in qe-central. A frontier item whose reach key contains a
        #: pattern gets priority -weight (min-heap ⇒ visited earlier). This ONLY
        #: reorders; it can never add a state or change what is reachable.
        self._plan_patterns: list[tuple[str, int]] = [
            (str(p).lower(), int(w)) for p, w in (plan_patterns or ()) if str(p).strip()
        ]

    def _plan_priority(self, key: str) -> int:
        """The most-negative plan weight among patterns occurring in ``key`` (a
        url_template), or 0 when the plan does not touch this route."""
        if not self._plan_patterns:
            return 0
        kl = key.lower()
        best = 0
        for pattern, weight in self._plan_patterns:
            if pattern in kl:
                best = min(best, -weight)
        return best

    def push(self, item: FrontierItem, *, key: str) -> bool:
        if key in self._enqueued_keys:
            return False
        self._enqueued_keys.add(key)
        # Novelty rank = how many items are ALREADY queued from this item's section
        # (the reach key IS the url_template). 0 for the first, growing per sibling.
        section = _section_signature(key)
        novelty_rank = self._section_counts.get(section, 0)
        self._section_counts[section] = novelty_rank + 1
        # An EXPLICIT caller priority (a Phase-2 seed) wins; otherwise the caged
        # planner may raise a high-value section ahead of the rest. Ordering-only.
        priority = item.priority if item.priority != 0 else self._plan_priority(key)
        heapq.heappush(self._heap, (priority, novelty_rank, item.depth, self._seq, item))
        self._seq += 1
        return True

    def pop(self) -> Optional[FrontierItem]:
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[-1]

    def __len__(self) -> int:
        return len(self._heap)


# ─── The guard context (phase + AUTH-window shared with the route handler) ───


@dataclass
class GuardContext:
    """Mutable guard state shared between the crawler and the Playwright route
    handler (:mod:`app.main`).  The crawler flips :attr:`phase` as the state
    machine advances; the route handler consults :meth:`decide` for EVERY
    network request so the fail-closed policy tracks the live phase.
    """

    refuse_pack: Any
    login_host: str = ""
    phase: Phase = Phase.EXPLORE
    auth_window: AuthWindow = field(default_factory=lambda: AuthWindow(max_requests=10, window_ms=30_000))
    #: Bounds the mutating-POST burst a single approved Phase-B submit may emit, so
    #: the SUBMIT window authorises the approved flow's POST(s) — NOT unlimited
    #: analytics/autosave/co-located POSTs that happen to fire during the window.
    #: Opened by the crawler at each submit; fail-closed when over budget / past T.
    submit_window: AuthWindow = field(default_factory=lambda: AuthWindow(max_requests=4, window_ms=15_000))
    attestation: Any = None
    submit_flow_approved: bool = False
    #: Federated / SSO login (#7): the DECLARED trusted Identity-Provider domains
    #: (login.microsoftonline.com / okta.com / …) a login flow may redirect to.
    #: Normalized to registrable domains in ``__post_init__``.  Empty ⇒ SSO
    #: cross-domain is refused exactly as before (byte-identical, fail-closed).
    idp_domains: frozenset = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Normalize the declared IdP allowlist to registrable domains ONCE so the
        # per-request check is an exact-set membership (never a suffix/substring
        # trick like 'okta.com.attacker.net').
        object.__setattr__(self, "idp_domains", frozenset(
            rd for rd in (registrable_domain(str(d).strip().lower())
                          for d in (self.idp_domains or ())) if rd
        ))

    def _is_declared_idp(self, host: str) -> bool:
        """True iff ``host``'s registrable domain is in the declared IdP allowlist
        — EXACT registrable-domain membership (never a substring/suffix match), so
        only a domain the operator explicitly declared can pass."""
        if not self.idp_domains or not host:
            return False
        rd = registrable_domain(host)
        return bool(rd) and rd in self.idp_domains

    def decide(self, method: str, url: str, *, now_ms: int,
               action_button_name: str = "") -> GuardDecision:
        """The full per-request decision, adding the caller-enforced AUTH window
        on top of the pure :func:`app.guard.classify_request`."""
        host = urlsplit(url or "").hostname or ""
        is_login = same_registrable_domain(host, self.login_host) if self.login_host else False
        # Federated / SSO login (#7): DURING the AUTH window only, a redirect to a
        # DECLARED IdP registrable domain counts as a login domain so the SSO POST
        # is not blocked as off-domain.  Narrow + fail-closed: AUTH phase only,
        # declared domains only, and still bounded by the ≤N-req/≤T-ms auth window
        # enforced just below (the IdP burst is not an open door).
        if not is_login and self.phase is Phase.AUTH and self._is_declared_idp(host):
            is_login = True
        if self.phase is Phase.AUTH:
            self.auth_window.note(now_ms)
            if (method or "").strip().upper() in MUTATING_METHODS and not self.auth_window.is_open(now_ms):
                return GuardDecision(
                    allow=False,
                    reason="AUTH window closed — login burst exceeded the "
                           "request/time budget",
                    rule_id="guard.auth.window_closed",
                    event_kind=EVENT_BLOCKED_METHOD, severity="critical",
                )
        if self.phase is Phase.SUBMIT:
            # Same caller-side budget as AUTH: an approved submit authorises a small
            # mutating-POST burst, not an open door for every POST the page fires
            # during the goto→refill→click window (analytics/autosave/co-located forms).
            self.submit_window.note(now_ms)
            if (method or "").strip().upper() in MUTATING_METHODS and not self.submit_window.is_open(now_ms):
                return GuardDecision(
                    allow=False,
                    reason="SUBMIT window closed — the approved flow exceeded the "
                           "request/time budget",
                    rule_id="guard.submit.window_closed",
                    event_kind=EVENT_BLOCKED_METHOD, severity="critical",
                )
        return classify_request(
            method, url, self.phase, self.refuse_pack, is_login, action_button_name,
            attestation=self.attestation, submit_flow_approved=self.submit_flow_approved,
            now_ms=now_ms,
        )


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
    #: What the crawl found vs could fill/advance (forms_found, fields_inferred,
    #: fields_needing_seed, submit_candidates) — the coverage the operator sees.
    coverage: Optional[dict[str, Any]] = None


# ─── The crawler ─────────────────────────────────────────────────────────────


class Crawler:
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
        credentials: Optional[Credentials] = None,
        allowed_hosts: Sequence[str] = (),
        max_relogins: int = 3,
        submit_approvals: Sequence[str] = (),
        wizard_enabled: bool = True,
        plan: Optional[dict[str, Any]] = None,
        sleep: Any = asyncio.sleep,
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
        self._credentials = credentials
        self._max_relogins = max_relogins
        self._sleep = sleep

        self._target_host = (urlsplit(target_url).hostname or "").lower()
        self._allowed_hosts = {h.strip().lower() for h in allowed_hosts if str(h).strip()}
        self._allowed_registrable = {registrable_domain(h) for h in self._allowed_hosts}
        self._allowed_registrable.add(registrable_domain(self._target_host))

        # Resume: seed visited/seq/frame from the durable manifest prefix.
        prior = emit.scan_resume_state(emit.read_records(work_dir, crawl_id))
        self._visited_fingerprints: set[str] = set(prior["visited_fingerprints"])
        self._next_seq = int(prior["next_sequence_index"])
        self._clock = emit.MonotonicClock(offset_ms=int(prior["last_timestamp_ms"]))
        self._emitter = emit.ManifestEmitter(
            work_dir, crawl_id, self._clock,
            next_frame_index=int(prior["next_frame_index"]),
        )
        self._tracker = BudgetTracker(budget, self._clock)
        self._frontier = Frontier(_parse_plan_patterns(plan))

        self._cancelled = False
        self._stop_reason = ""
        self._done = False
        self._guard_blocks = 0
        self._storage_state: Optional[dict[str, Any]] = None
        # Destinations we have already GROUNDED a nav click to (across all states), so
        # a nav bar repeated on every page grounds each unique route ONCE — the cost of
        # direct-nav grounding stays ~O(unique navs), not O(states × links).
        self._grounded_navs: set[str] = set()
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
        self._submit_candidates: list[str] = []    # a submit found but not clicked (Phase-A boundary)
        # Phase-B attested submit (crawl-once/run-many depth): default-OFF. Fires ONLY
        # when the operator supplied a per-flow submit-approval list AND a disposable-env
        # attestation is present — a crawl without both stops at the Phase-A boundary,
        # byte-identical to before. execute_submit_phase_b re-verifies the guard.
        self._submit_approvals = {s.strip().lower() for s in submit_approvals if str(s).strip()}
        self._submit_enabled = bool(self._submit_approvals) and self._guard.attestation is not None
        self._forms_submitted = 0
        self._submitted_flows: set[str] = set()    # dedup key = f"{fingerprint}::{name}"
        # Wizard/stepper traversal (#1): advance non-danger Next/Continue on filled
        # form states to record deeper steps (the SPA quote-wizard case). Bounded +
        # fingerprint-deduped + fail-closed (danger OR commit-word vetoes). ON by
        # default (the double gate is conservative); a kill-switch for unvetted apps.
        self._wizard_enabled = bool(wizard_enabled)
        self._wizard_advances = 0
        self._wizard_states: set[str] = set()      # entry-step fingerprints already walked

    # -- public control / observation -----------------------------------------

    def cancel(self) -> None:
        """Request a graceful stop; the loop flushes the manifest and reports
        the partial crawl with ``stop_reason='cancelled'``."""
        self._cancelled = True

    def now_ms(self) -> int:
        """The crawl's monotonic clock reading (for the route handler's guard
        decision + guard_event timestamps — one clock across the whole crawl)."""
        return self._clock.now_ms()

    def _build_coverage(self) -> dict[str, Any]:
        """The crawl's coverage account (deduped, first-appearance order): what was
        found vs could be filled/advanced. ``forms_submitted`` is 0 in the explore
        phase (the submit boundary) — ``submit_candidates`` are the flows a Phase-B
        attested submit would carry deeper. Turns the shallow-vs-full gap into a
        NAMED, targeted seed request instead of blind guessing."""
        def _dedup(items: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for it in items:
                k = (it or "").strip()
                if k and k.lower() not in seen:
                    seen.add(k.lower())
                    out.append(k)
            return out

        def _dedup_detail(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                lbl = (d.get("label") or "").strip()
                if lbl and lbl.lower() not in seen:
                    seen.add(lbl.lower())
                    out.append({"label": lbl, "url": (d.get("url") or "").strip()})
            return out

        inferred = _dedup(self._fields_inferred)
        needs_seed = _dedup(self._fields_unfilled)
        needs_seed_detail = _dedup_detail(self._fields_seed_detail)
        submits = _dedup(self._submit_candidates)
        unexercised = max(0, len(submits) - self._forms_submitted)
        return {
            "forms_found": self._forms_found,
            "forms_submitted": self._forms_submitted,
            "fields_inferred": inferred,
            "fields_needing_seed": needs_seed,
            # Per-field page context {label, url} — the grounded source for flow grouping.
            # Kept alongside the flat list (which stays for back-compat).
            "fields_needing_seed_detail": needs_seed_detail,
            "submit_candidates": submits,
            "summary": (
                f"{self._forms_found} form(s) found; "
                f"{len(inferred)} field(s) auto-filled with a default; "
                f"{len(needs_seed)} field(s) need a real seed; "
                f"{self._forms_submitted} submit(s) exercised (Phase-B), "
                f"{unexercised} at the submit boundary."
            ),
        }

    @property
    def emitter(self) -> emit.ManifestEmitter:
        return self._emitter

    @property
    def guard(self) -> GuardContext:
        return self._guard

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
            **self._tracker.snapshot(),
        }

    # -- the crawl -------------------------------------------------------------

    async def run(self) -> CrawlSummary:
        """Execute the crawl and return an honest :class:`CrawlSummary`."""
        self._emit_initial_meta()
        detail = ""
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
                await self._explore_loop()
        except Exception as exc:  # honest terminal error — never a silent crash
            self._stop_reason = STOP_ERROR
            detail = str(exc)[:500]
            logger.exception("qec.crawler.run_failed crawl_id=%s", self.crawl_id)

        if not self._stop_reason:
            self._stop_reason = STOP_COMPLETED
        self._done = True
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
        )
        logger.info("qec.crawler.completed crawl_id=%s stop_reason=%s states=%d "
                    "actions=%d screenshots=%d guard_blocks=%d",
                    self.crawl_id, self._stop_reason, summary.states,
                    summary.actions, summary.screenshots, summary.guard_blocks)
        return summary

    # -- AUTH phase ------------------------------------------------------------

    async def _maybe_authenticate(self) -> Optional[str]:
        """Run the login flow if credentials were supplied.

        Returns the post-login root URL to explore from, ``self.target_url`` when
        no credentials were supplied, or ``None`` when login could not be
        verified (an honest hard stop — the authenticated app is unreachable).
        """
        if not self._credentials:
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
        result = await authenticator.login(login_obs)
        self._tracker.note_action(len(result.actions))
        self._storage_state = result.storage_state

        login_controls = build_inventory(login_obs.raw_controls, self._refuse_pack,
                                          url=login_obs.url)
        self._record_state(
            url=login_obs.url, title=login_obs.title, controls=login_controls,
            fingerprint=result.before_fingerprint or state_fingerprint(
                login_obs.url, login_controls, login_obs.dialog_flags),
            actions=result.actions, screenshots=[(login_png, login_ts)],
            last_seen_ms=self._clock.now_ms(),
        )

        if not result.success:
            self._stop_reason = STOP_AUTH_FAILED
            logger.warning("qec.crawler.login_failed reason=%s", result.reason)
            return None
        return await self._port.current_url()

    # -- EXPLORE phase ---------------------------------------------------------

    async def _explore_loop(self) -> None:
        while True:
            if self._cancelled:
                self._stop_reason = STOP_CANCELLED
                return
            reason = self._tracker.stop_reason()
            if reason:
                self._stop_reason = reason
                return
            item = self._frontier.pop()
            if item is None:
                return  # frontier exhausted → completed
            try:
                await self._expand(item)
            except Exception:
                logger.exception("qec.crawler.expand_failed url_scope=%s depth=%d",
                                 _host_of(item.url), item.depth)
                # one bad state must not kill the crawl — continue honestly.

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
        fingerprint = state_fingerprint(obs.url, controls, obs.dialog_flags)

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
        # Phase A: fill the form (if any), read back committed values.
        is_form = any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c)
                      for c in controls)
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
            )
            actions.extend(fill.actions)
            self._tracker.note_action(len(fill.actions))
            self._fields_inferred.extend(fill.inferred_fields)
            self._fields_unfilled.extend(fill.unfilled_fields)
            # Tag each unfilled field with the page it appeared on (grounds flow grouping).
            self._fields_seed_detail.extend(
                {"label": lbl, "url": obs.url or ""} for lbl in fill.unfilled_fields)
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
        # WIZARD/STEPPER (#1): on a FILLED form state, advance a non-danger
        # Next/Continue to record deeper wizard steps in place (SPA quote wizards
        # live at one URL — step 2 is reachable only by the click sequence). The
        # walk OWNS the recording of this step + every step it reaches; when there
        # is no advance trigger it returns False and this state is recorded normally.
        walked = False
        if self._wizard_enabled and is_form and fill is not None and fill.filled:
            walked = await self._walk_wizard(
                item=item, url=obs.url, title=obs.title, controls=snapshot_controls,
                fingerprint=fingerprint, base_actions=actions,
                entry_shot=(entry_png, entry_ts), first_seen_ms=first_seen,
                displayed_values=displayed_values, network_calls=network_calls,
            )
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
                await self._port.goto(item.url)
                self._tracker.note_request()
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
            await self._port.goto(item.url)  # reset — a real nav leaves the page
            self._tracker.note_request()
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
            if action.after and action.after.get("navigated"):
                arrived = obs.url_after
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

    # -- href-follow traversal (SPA-robust link following) ---------------------

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
        from urllib.parse import urljoin
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
            await self._port.goto(item.url)  # open from the clean recorded state
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

    async def _probe_select_options(
        self, controls: Sequence[dict[str, Any]], *, url: str,
    ) -> None:
        """Read the option LABELS of CUSTOM dropdowns whose options the static inventory
        couldn't see (a widget that builds them only on OPEN). For each opener: click to
        open, read the revealed ``[role=option]`` LABELS, then dismiss (Escape) so the page
        is restored before the next read. Enriches the control's ``options`` in place so the
        form_snapshot carries the real choices.

        DISCIPLINE (never green-wash): LABELS only, never values/locators; native ``<select>``
        is skipped (optionsOf already reads it, and a browser-native popup isn't DOM-readable);
        ONE dropdown open at a time (dismissed before the next) so options are attributed to
        the control that was opened; bounded by ``_MAX_OPTION_PROBES``; any failure leaves the
        control's options empty — an honest 'unread choice', never a fabricated list."""
        collect = getattr(self._port, "collect_controls", None)
        press = getattr(self._port, "press_key", None)
        if collect is None:
            return
        probed = 0
        for c in controls:
            if probed >= _MAX_OPTION_PROBES:
                break
            if c.get("kind") != "select" or c.get("options"):
                continue  # not a choice control, or options already captured statically
            if c.get("disabled") or c.get("danger"):
                continue
            if (c.get("tag") or "").strip().lower() == "select":
                continue  # NATIVE select — handled by optionsOf; native popup isn't readable
            if (c.get("role") or "").strip().lower() not in ("combobox", "listbox"):
                continue  # only an ARIA combobox opener
            try:
                open_obs = await self._port.click(dict(c))
                self._tracker.note_action()
                self._tracker.note_request()
                # A dropdown that navigated is not a dropdown — bail on that control.
                if getattr(open_obs, "url_after", None) and getattr(open_obs, "url_before", None) \
                        and open_obs.url_after != open_obs.url_before:
                    continue
                revealed = build_inventory(await collect(), self._refuse_pack, url=url)
                opts: list[str] = []
                seen: set[str] = set()
                for r in revealed:
                    if (r.get("role") or "").strip().lower() == "option":
                        nm = str(r.get("name") or "").strip()
                        if nm and nm.lower() not in seen:
                            seen.add(nm.lower())
                            opts.append(nm)
                if press is not None:
                    await press("Escape")  # restore: dismiss the opened listbox
                if opts:
                    c["options"] = opts[:_MAX_PROBED_OPTIONS]
                    if isinstance(c.get("qec"), dict):
                        c["qec"]["options"] = c["options"]
                    probed += 1
            except Exception:
                continue

    async def _commit_choice(self, control: dict[str, Any]) -> bool:
        """Grounded-select a driver's FIRST real option so a dependent field can react:
        a native <select> via select_option; a custom combobox by opening it and clicking
        the matching [role=option]. Returns True iff a value was committed. EXPLORE-phase
        only — a chosen dropdown value commits NOTHING server-side (no submit)."""
        opts = [o for o in (control.get("options") or []) if str(o).strip()]
        if not opts:
            return False
        first = str(opts[0]).strip()
        tag = (control.get("tag") or "").strip().lower()
        select_option = getattr(self._port, "select_option", None)
        if tag == "select" and select_option is not None:
            try:
                await select_option(dict(control), first)
                self._tracker.note_action()
                return True
            except Exception:
                return False
        collect = getattr(self._port, "collect_controls", None)
        if collect is None:
            return False
        try:
            await self._port.click(dict(control))            # open the listbox
            revealed = build_inventory(await collect(), self._refuse_pack, url="")
            for r in revealed:
                if (r.get("role") or "").strip().lower() == "option" \
                        and str(r.get("name") or "").strip() == first:
                    await self._port.click(dict(r))          # commit the choice
                    self._tracker.note_action()
                    return True
        except Exception:
            return False
        return False

    async def _probe_dependencies(
        self, controls: Sequence[dict[str, Any]], *, url: str,
    ) -> None:
        """ACT-THEN-DIFF (v1: dependent choices). For a DRIVER select (options captured),
        commit its first option, re-observe, and DIFF: any select that was empty and is now
        POPULATED is a dependent — capture its options and tag depends_on=<driver>. Bounded,
        EXPLORE-phase (no submit), enriches ``controls`` in place so the snapshot carries the
        dependent's real options instead of an empty 'unread choice'.

        HONESTY: the captured options are for ONE branch of the driver, so the dependent is
        tagged depends_on (never presented as a fixed list). Any failure leaves the dependent
        empty — an honest unread choice, never fabricated."""
        collect = getattr(self._port, "collect_controls", None)
        if collect is None:
            return
        drivers = [c for c in controls
                   if c.get("kind") == "select" and c.get("options")
                   and not c.get("disabled") and not c.get("danger")]
        empty_by_name = {
            str(c.get("name") or ""): c for c in controls
            if c.get("kind") == "select" and not c.get("options") and c.get("name")
        }
        if not drivers or not empty_by_name:
            return
        acted = 0
        for d in drivers:
            if acted >= _MAX_DEP_PROBES or not empty_by_name:
                break
            if not await self._commit_choice(d):
                continue
            acted += 1
            # Re-inventory after the commit; a NATIVE dependent select already carries its
            # newly-populated options here.
            after = build_inventory(await collect(), self._refuse_pack, url=url)
            pending = [c for c in after
                       if c.get("kind") == "select" and str(c.get("name") or "") in empty_by_name]
            # A CUSTOM dependent combobox still reveals its (now-populated) options only on
            # OPEN — open-probe the still-empty ones so the dependency actually surfaces.
            await self._probe_select_options(
                [c for c in pending if not c.get("options")], url=url)
            for r in pending:
                nm = str(r.get("name") or "")
                if r.get("options") and nm in empty_by_name:
                    tgt = empty_by_name.pop(nm)
                    tgt["options"] = list(r.get("options") or [])[:_MAX_PROBED_OPTIONS]
                    tgt["depends_on"] = d.get("name") or ""
                    if isinstance(tgt.get("qec"), dict):
                        tgt["qec"]["options"] = tgt["options"]

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
            if not name or getattr(fc, "danger", False) or name.lower() not in self._submit_approvals:
                continue
            flow_key = f"{fingerprint}::{name.lower()}"
            if flow_key in self._submitted_flows:
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
            self._submitted_flows.add(flow_key)
            seq = self._next_seq
            self._next_seq += 1
            prev_phase = self._guard.phase
            prev_approved = self._guard.submit_flow_approved
            # Flip the shared guard to SUBMIT so the network route handler authorises the
            # approved submit POST; restore EXPLORE no matter what (fail-closed default).
            # Open a FRESH bounded window per submit so the burst budget can't accrue
            # across flows.
            self._guard.phase = Phase.SUBMIT
            self._guard.submit_flow_approved = True
            self._guard.submit_window.open(self._clock.now_ms())
            try:
                result = await execute_submit_phase_b(
                    self._port, control, item.url, self._emitter, self._clock,
                    refuse_pack=self._refuse_pack,
                    is_login_domain=False,
                    attestation=self._guard.attestation,
                    submit_flow_approved=True,
                    now_ms=self._clock.now_ms(),
                    state_id=fingerprint,
                    sequence_index=seq,
                    answer_key=self._answer_key,
                    fill_controls=controls,
                )
            finally:
                self._guard.phase = prev_phase
                self._guard.submit_flow_approved = prev_approved
            if result.submitted:
                self._forms_submitted += 1
                self._tracker.note_action()
            ps = result.page_state
            dest = (getattr(ps, "location", "") or "").strip() if ps else ""
            # Honour max_depth for submit-derived states too (mirrors _discover's
            # depth gate) so an attested submit chain cannot crawl past the budget.
            if (result.confirmed and result.outcome == "navigation" and dest
                    and item.depth < self._budget.max_depth and self._in_scope(dest)):
                self._frontier.push(
                    FrontierItem(url=dest, depth=item.depth + 1,
                                 discovered_via=f"submit:{name}",
                                 parent_fingerprint=fingerprint),
                    key=_url_key(dest),
                )
            return  # one submit per state — avoid combinatorial explosion

    # -- wizard / stepper traversal (#1) ---------------------------------------

    def _pick_wizard_advance(self, controls: Sequence[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """The first non-danger advance control (Next/Continue/Proceed/Forward) to
        step the wizard, or ``None``.  Fail-closed: skips danger/disabled/nameless
        controls, skips any name the operator approved for an attested Phase-B
        submit (that path owns it), and vetoes on any commit/terminal word."""
        for c in controls:
            if c.get("kind") != "button" or c.get("disabled") or c.get("danger"):
                continue
            name = str(c.get("name") or "").strip()
            if not name or name.lower() in self._submit_approvals:
                continue
            if _is_wizard_advance(name):
                return c
        return None

    async def _walk_wizard(
        self, *, item: FrontierItem, url: str, title: str,
        controls: Sequence[dict[str, Any]], fingerprint: str,
        base_actions: list[emit.ActionRecord], entry_shot: tuple[bytes, int],
        first_seen_ms: int, displayed_values: Sequence[dict[str, Any]],
        network_calls: Sequence[dict[str, Any]],
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
        advance control itself already passed the danger + commit-word gates."""
        if fingerprint in self._wizard_states or self._pick_wizard_advance(controls) is None:
            return False
        self._wizard_states.add(fingerprint)

        # _discover (hover-reveal + click-pass) may have navigated the live page via a
        # goto reset, discarding the Phase-A fills done for THIS step. Re-establish the
        # FILLED entry step so a validation-gated advance actually fires (a nav menu on
        # the wizard page must not silently defeat the walk). The re-fill's action
        # records are redundant with base_actions (the canonical Phase-A fills) and are
        # DISCARDED — the recorded step keeps its original snapshot + fill actions.
        await self._port.goto(item.url)
        self._tracker.note_request()
        reobs = await self._observe()
        refreshed = build_inventory(reobs.raw_controls, self._refuse_pack, url=reobs.url)
        if any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c) for c in refreshed):
            await fill_form_phase_a(
                self._port, refreshed, self._answer_key or AnswerKey(), self._clock,
                phase=Phase.EXPLORE.value, state_id=fingerprint)

        cur_url, cur_title, cur_controls, cur_fp = url, title, controls, fingerprint
        cur_actions = list(base_actions)
        cur_shot, cur_first = entry_shot, first_seen_ms
        cur_dv, cur_nc = displayed_values, network_calls
        depth, steps = item.depth, 0

        while True:
            trig = self._pick_wizard_advance(cur_controls)
            advance: Optional[tuple[Any, list[dict[str, Any]], str]] = None
            if (trig is not None and steps < _MAX_WIZARD_STEPS
                    and self._wizard_advances < _MAX_WIZARD_ADVANCES
                    and depth < self._budget.max_depth
                    and not (self._tracker.stop_reason() or self._cancelled)):
                await self._politeness_delay()
                observation = await self._port.click(trig)
                self._tracker.note_request()
                action = emit.build_action_record(
                    dict(trig), verb="click", value=None, observation=observation,
                    phase=Phase.EXPLORE.value, state_id=cur_fp,
                    timestamp_ms=self._clock.now_ms())
                self._tracker.note_action()
                outcome = str((action.after or {}).get("outcome") or "")
                obs = await self._observe()
                new_controls = build_inventory(obs.raw_controls, self._refuse_pack, url=obs.url)
                new_fp = state_fingerprint(obs.url, new_controls, obs.dialog_flags)
                # a GENUINE advance: an observable effect AND a NEW unseen state.
                if (outcome not in ("", "none") and new_fp != cur_fp
                        and new_fp not in self._visited_fingerprints):
                    cur_actions.append(action)
                    advance = (obs, new_controls, new_fp)

            # record the CURRENT step (its fills + the onward advance click if any).
            self._record_state(
                url=cur_url, title=cur_title, controls=cur_controls, fingerprint=cur_fp,
                actions=cur_actions, screenshots=[cur_shot],
                first_seen_ms=cur_first, last_seen_ms=self._clock.now_ms(),
                displayed_values=cur_dv, network_calls=cur_nc)
            if advance is None:
                return True

            obs, new_controls, new_fp = advance
            self._visited_fingerprints.add(new_fp)
            self._wizard_advances += 1
            steps += 1
            depth += 1
            # capture + fill the new step so a validation-gated onward Next can fire.
            step_first = self._clock.now_ms()
            step_png = await self._port.screenshot_png()
            step_ts = self._clock.now_ms()
            step_actions: list[emit.ActionRecord] = []
            if any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c) for c in new_controls):
                filled = await fill_form_phase_a(
                    self._port, new_controls, self._answer_key or AnswerKey(), self._clock,
                    phase=Phase.EXPLORE.value, state_id=new_fp)
                step_actions.extend(filled.actions)
                self._tracker.note_action(len(filled.actions))
                if filled.filled:
                    after_fill = await self._observe()
                    new_controls = build_inventory(
                        after_fill.raw_controls, self._refuse_pack, url=after_fill.url)
            cur_url, cur_title, cur_controls, cur_fp = obs.url, obs.title, new_controls, new_fp
            cur_actions = step_actions
            cur_shot, cur_first = (step_png, step_ts), step_first
            cur_dv = await self._port.collect_displayed_values()
            cur_nc = await self._drain_network()

    # -- state recording -------------------------------------------------------

    def _record_state(
        self,
        *,
        url: str,
        title: str,
        controls: Sequence[dict[str, Any]],
        fingerprint: str,
        actions: Sequence[emit.ActionRecord],
        screenshots: Sequence[tuple[bytes, int]],
        first_seen_ms: Optional[int] = None,
        last_seen_ms: Optional[int] = None,
        displayed_values: Sequence[dict[str, Any]] = (),
        network_calls: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Assemble + emit ONE ``page_state`` record with monotonic indices."""
        seq = self._next_seq
        self._next_seq += 1
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()

        form_snapshot, form_signals = _form_snapshot(controls)

        shot_records: list[dict[str, Any]] = []
        first = first_seen_ms if first_seen_ms is not None else (
            min((ts for _, ts in screenshots), default=self._clock.now_ms()))
        last = last_seen_ms if last_seen_ms is not None else self._clock.now_ms()
        for png, ts in screenshots:
            # clamp the screenshot timestamp inside the visit window (the
            # factory's frame-window join requires it — schema
            # screenshot_outside_visit_window rule).
            clamped = min(max(int(ts), first), last)
            try:
                rec = self._emitter.store_screenshot(png, clamped)
                shot_records.append({"frame_index": rec.frame_index,
                                     "timestamp_ms": rec.timestamp_ms,
                                     "path": rec.path})
            except ValueError:
                logger.warning("qec.crawler.empty_screenshot_skipped seq=%d", seq)

        ordered_actions: list[dict[str, Any]] = []
        for i, action in enumerate(actions):
            action.subaction_index = i
            action.state_id = fingerprint
            ordered_actions.append(_action_to_dict(action))

        record = emit.PageStateRecord(
            sequence_index=seq,
            location=url[:2000],
            first_seen_ms=first,
            last_seen_ms=max(first, last),
            title=(title or "")[:500],
            url_host=host[:500],
            url_path=(parts.path or "")[:2000],
            url_query=(parts.query or "")[:2000],
            canonical_host=(registrable_domain(host) or host)[:500],
            form_snapshot=form_snapshot,
            form_snapshot_signals=form_signals,
            displayed_values=_displayed_values(displayed_values),
            network_calls=_network_calls(network_calls),
            actions=ordered_actions,
            screenshots=shot_records,
            state_id=fingerprint,
            ax_fingerprint=fingerprint,
        )
        self._emitter.emit_page_state(record)
        self._tracker.note_state()

    # -- helpers ---------------------------------------------------------------

    async def _observe(self) -> PageObservation:
        return PageObservation(
            url=await self._port.current_url(),
            title=await self._port.title(),
            raw_controls=await self._port.collect_controls(),
            dialog_flags=await self._port.dialog_flags(),
            error_texts=await self._port.error_texts(),
        )

    async def _drain_network(self) -> list[dict[str, Any]]:
        """API/network mining — drain the XHR/fetch calls the app made during this
        visit from the port's capture buffer (an optional verb, accessed by
        ``getattr`` so a fake/older adapter without it is a clean no-op).  The
        adapter buffers + PII-scrubs at source; here we only relay best-effort."""
        drain = getattr(self._port, "drain_network", None)
        if drain is None:
            return []
        try:
            return list(await drain() or [])
        except Exception:
            logger.warning("qec.crawler.network_drain_failed", exc_info=True)
            return []

    async def _politeness_delay(self) -> None:
        rate = self._budget.rate_per_s
        if rate and rate > 0:
            await self._sleep(1.0 / rate)

    def _in_scope(self, url: str) -> bool:
        host = (urlsplit(url or "").hostname or "").lower()
        if not host:
            return False
        if self._target_host and same_registrable_domain(host, self._target_host):
            return True
        return host in self._allowed_hosts or registrable_domain(host) in self._allowed_registrable

    def _emit_initial_meta(self) -> None:
        self._emitter.emit_crawl_meta(self._meta(stop_reason=""))

    def _emit_terminal_meta(self, detail: str) -> None:
        meta = self._meta(stop_reason=self._stop_reason)
        meta["frame_count"] = self._emitter.frame_count
        meta["stats"] = self._tracker.snapshot()
        meta["guard_blocks"] = self._guard_blocks
        if detail:
            meta["detail"] = detail
        self._emitter.emit_crawl_meta(meta)

    def _meta(self, *, stop_reason: str) -> dict[str, Any]:
        attestation = self._guard.attestation
        return {
            "crawl_id": self.crawl_id,
            "target_url": self.target_url,
            "explorer_version": self._explorer_version,
            "config_fingerprint": self._config_fingerprint,
            "frame_count": self._emitter.frame_count,
            "budgets": self._budget.as_dict(),
            "guard_version": self._guard_version,
            "refuse_pack_version": self._refuse_pack_version,
            "attestation": _attestation_dict(attestation),
            "stop_reason": stop_reason,
        }


# ─── module helpers ──────────────────────────────────────────────────────────


def _is_password(control: dict[str, Any]) -> bool:
    it = str(control.get("input_type") or "").strip().lower()
    if not it:
        it = str((control.get("qec") or {}).get("input_type") or "").strip().lower()
    return it == "password"


def _form_snapshot(controls: Sequence[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict]]:
    """Build ``form_snapshot`` (label→scrubbed committed value) + signals."""
    snapshot: dict[str, str] = {}
    signals: dict[str, dict] = {}
    for control in controls:
        signal = form_signal_for(control)
        if signal is None:
            continue
        label = str(control.get("name") or "").strip()
        if not label:
            continue
        secret = _is_password(control)
        raw = control.get("value_committed") or ""
        snapshot[label] = emit.scrub_value(raw, is_secret=secret).value
        if secret:
            # A password input refines to kind 'text' (its password-ness lives in
            # input_type); stamp the signal so the substrate writer's redaction
            # recognises it (writer._is_password_signal reads type=='password').
            signal = {**signal, "type": "password"}
        signals[label] = signal
    return snapshot, signals


def _displayed_values(raw: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """ANSWERS P1.B — normalize + scrub captured displayed value nodes into
    ``[{label, selector, text}]`` (deduped). The text is scrubbed like a form value
    (it may be PII-adjacent, e.g. an amount); label + selector let the value oracle
    ground an expected outcome to this rendered node without a client source_hint.

    VALUE-ORACLE INFERENCE (#2): each node is additively annotated with a
    crawl-side classification — ``value_type`` (currency/percent/decision/…),
    ``value_candidate`` (is this a likely expected OUTCOME to assert on?),
    ``value_confidence`` and ``value_reason``.  Inference ONLY — it surfaces
    candidates for confirmation; the frozen factory oracle still does the PROVING.
    The extra keys are all-string + additive (existing consumers read only
    label/selector/text via ``.get`` and ignore them)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        selector = str(r.get("selector") or "").strip()
        text = emit.scrub_value(str(r.get("text") or "")).value.strip()
        if not (selector and text):
            continue
        key = f"{selector}|{text}"
        if key in seen:
            continue
        seen.add(key)
        label = str(r.get("label") or "").strip()[:200]
        inferred = value_infer.infer_candidate(label, text)
        out.append({
            "label": label, "selector": selector[:300], "text": text[:200],
            "value_type": inferred["value_type"],
            "value_candidate": "true" if inferred["is_candidate"] else "false",
            "value_confidence": f"{inferred['confidence']:.2f}",
            "value_reason": inferred["reason"][:120],
        })
    return out


def _network_calls(raw: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """API/network mining — normalize + PII-scrub captured XHR/fetch/SSE/WebSocket
    calls into ``[{method, url, has_query, status, resource_type, request_mime,
    response_mime, response_bytes, timestamp_ms}]`` (deduped, bounded, ALL-string
    values so the schema's ``dict[str, str]`` can never refuse a bundle over a
    diagnostic).

    Safety: the query string is DROPPED here regardless of what the adapter sent
    (a query param is the likeliest PII carrier — ``has_query`` preserves the
    honest fact that one existed, without its values), and the query-stripped URL
    is re-scrubbed for path-embedded PII (belt-and-suspenders over the adapter's
    source-side scrub).  http(s) API calls AND ws(s) real-time endpoints (D) are
    evidence; every other scheme is dropped."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        parts = urlsplit(str(r.get("url") or "").strip())
        if (parts.scheme or "").lower() not in ("http", "https", "ws", "wss"):
            continue
        url = emit.scrub_value(f"{parts.scheme}://{parts.netloc}{parts.path}").value.strip()
        if not url:
            continue
        had_query = bool(parts.query) or bool(r.get("has_query"))
        method = str(r.get("method") or "").strip().upper()[:10]
        status = str(r.get("status") or "").strip()[:3]
        key = f"{method}|{url}|{status}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "method": method,
            "url": url[:1000],
            "has_query": "true" if had_query else "false",
            "status": status,
            "resource_type": str(r.get("resource_type") or "").strip()[:20],
            "request_mime": str(r.get("request_mime") or "").strip()[:100],
            "response_mime": str(r.get("response_mime") or "").strip()[:100],
            "response_bytes": str(r.get("response_bytes") or "").strip()[:12],
            "timestamp_ms": str(r.get("timestamp_ms") or "").strip()[:15],
        })
        if len(out) >= _MAX_NETWORK_CALLS:
            break
    return out


def _action_to_dict(action: emit.ActionRecord) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(action)


def _url_key(url: str) -> str:
    from .fingerprint import url_template
    return url_template(url)


def _host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _attestation_dict(attestation: Any) -> Optional[dict[str, Any]]:
    if attestation is None:
        return None
    for attr in ("model_dump", "_asdict"):
        fn = getattr(attestation, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                break
    if isinstance(attestation, dict):
        return dict(attestation)
    return {"present": True}
