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
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit

from . import emit
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


class Frontier:
    """A min-priority queue of :class:`FrontierItem` deduped by reach key.

    Ordering is ``(priority, depth, insertion)`` so a Phase-2 seed manifest can
    raise a critical route's priority while Phase 1 degrades to breadth-first by
    depth.  Push-time dedup on the reach key (``url_template``) keeps the queue
    finite; the crawler additionally dedups on the full state fingerprint at
    expand time so distinct URLs that render the SAME state are visited once.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, FrontierItem]] = []
        self._seq = 0
        self._enqueued_keys: set[str] = set()

    def push(self, item: FrontierItem, *, key: str) -> bool:
        if key in self._enqueued_keys:
            return False
        self._enqueued_keys.add(key)
        heapq.heappush(self._heap, (item.priority, item.depth, self._seq, item))
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

    def decide(self, method: str, url: str, *, now_ms: int,
               action_button_name: str = "") -> GuardDecision:
        """The full per-request decision, adding the caller-enforced AUTH window
        on top of the pure :func:`app.guard.classify_request`."""
        host = urlsplit(url or "").hostname or ""
        is_login = same_registrable_domain(host, self.login_host) if self.login_host else False
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
        self._frontier = Frontier()

        self._cancelled = False
        self._stop_reason = ""
        self._done = False
        self._guard_blocks = 0
        self._storage_state: Optional[dict[str, Any]] = None
        # Coverage accounting (crawl-once/run-many legibility): what the crawl found
        # vs could actually fill/advance, so the shallow-vs-full gap is visible and the
        # human's remediation is a NAMED, targeted seed request — never blind guessing.
        self._forms_found = 0
        self._fields_inferred: list[str] = []      # filled with a synthesized default
        self._fields_unfilled: list[str] = []      # no seed AND no safe default -> needs seed
        self._submit_candidates: list[str] = []    # a submit found but not clicked (Phase-A boundary)
        # Phase-B attested submit (crawl-once/run-many depth): default-OFF. Fires ONLY
        # when the operator supplied a per-flow submit-approval list AND a disposable-env
        # attestation is present — a crawl without both stops at the Phase-A boundary,
        # byte-identical to before. execute_submit_phase_b re-verifies the guard.
        self._submit_approvals = {s.strip().lower() for s in submit_approvals if str(s).strip()}
        self._submit_enabled = bool(self._submit_approvals) and self._guard.attestation is not None
        self._forms_submitted = 0
        self._submitted_flows: set[str] = set()    # dedup key = f"{fingerprint}::{name}"

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

        inferred = _dedup(self._fields_inferred)
        needs_seed = _dedup(self._fields_unfilled)
        submits = _dedup(self._submit_candidates)
        unexercised = max(0, len(submits) - self._forms_submitted)
        return {
            "forms_found": self._forms_found,
            "forms_submitted": self._forms_submitted,
            "fields_inferred": inferred,
            "fields_needing_seed": needs_seed,
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
        nav = await self._port.goto(self.target_url)
        self._tracker.note_request()
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

    async def _expand(self, item: FrontierItem) -> None:
        await self._politeness_delay()
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
            self._submit_candidates.extend(
                fc.name for fc in fill.flow_candidates if fc.name and not fc.danger)
            if fill.filled:
                # re-inventory so form_snapshot carries the committed values.
                after_fill = await self._observe()
                snapshot_controls = build_inventory(after_fill.raw_controls,
                                                     self._refuse_pack, url=after_fill.url)

        # Navigation discovery (grounded): click safe actionable controls.
        actions.extend(await self._discover(item, controls, is_form, fingerprint,
                                            budget_left=self._budget.max_actions_per_state - len(actions)))

        last_seen = self._clock.now_ms()
        # ANSWERS P1.B — capture rendered value nodes in the page's FINAL state (after
        # fills + discovery clicks reveal outputs like a computed premium).
        displayed_values = await self._port.collect_displayed_values()
        self._record_state(
            url=obs.url, title=obs.title, controls=snapshot_controls,
            fingerprint=fingerprint, actions=actions,
            screenshots=[(entry_png, entry_ts)],
            first_seen_ms=first_seen, last_seen_ms=last_seen,
            displayed_values=displayed_values,
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
    ground an expected outcome to this rendered node without a client source_hint."""
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
        out.append({"label": str(r.get("label") or "").strip()[:200],
                    "selector": selector[:300], "text": text[:200]})
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
