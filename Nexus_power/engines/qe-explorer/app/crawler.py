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
import hashlib
import heapq
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from . import danger_signals
from . import emit
from . import matcher
from . import value_infer
from . import vocab
from .auth import Authenticator, AuthWindow, Credentials, match_login_controls
from .browser import BrowserPort, PageObservation
from .fingerprint import state_fingerprint
from . import flow_ledger
from .identity_pack import derive as derive_identity
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
#: ACT-THEN-DIFF bounds — driver acts committed per state to reveal DEPENDENT options and
#: CONDITIONALLY-REVEALED fields. EXPLORE-phase, no submit; a committed choice/toggle is a
#: valid filled-form state to snapshot.
_MAX_DEP_PROBES = 8
#: Value-bearing control kinds (a form FIELD, not a button/link) — a newly-appeared one
#: after a driver act is a conditionally-revealed field.
_FIELD_KINDS = frozenset({
    "text", "date", "select", "radio", "checkbox", "toggle", "slider", "color",
})
#: Wizard/stepper traversal (#1) bounds — steps per single wizard chain, and the
#: crawl-wide advance total; both cap the SAFETY-SENSITIVE submit-boundary probe.
_MAX_WIZARD_STEPS = 6
_MAX_WIZARD_ADVANCES = 24
#: END-TO-END FLOW mode raises them, because the bounds above are a PROBE budget
#: and this is a COVERAGE budget. Six steps is a sample of a fifteen-step quote
#: funnel; walking six and reporting the journey as covered is the green-wash this
#: product exists to prevent. Still bounded, and every other safety gate is
#: untouched: the commit-word veto, the danger gate and the submit boundary decide
#: what may be clicked, not the step count.
_E2E_WIZARD_STEPS = 20
_E2E_WIZARD_ADVANCES = 80
#: ENTRY-goto retry bounds — the first navigation can race the per-dispatch
#: egress-fence reconfigure (squid allowlist re-read); retry it briefly.
_ENTRY_GOTO_RETRIES = 2
_ENTRY_RETRY_DELAY_S = 2.5

#: ``auth_incomplete`` reason: a start-authenticated session WAS injected, but the
#: app still answers the entry with a login wall — the session has EXPIRED (or was
#: revoked). Sessions are captured once and reused for every later crawl, so this
#: is the STEADY STATE of any app crawled more than a session-lifetime apart, not
#: an edge case. Detected structurally (a password field on the entry screen), so
#: it holds for any app in any language — never a URL or copy match.
AUTH_SESSION_EXPIRED = "session_expired"

#: An "advance the wizard" control label (Next / Continue / Proceed / Forward) —
#: the POSITIVE intent signal. Union-compiled from the per-language packs in
#: :mod:`app.vocab` (the ``en`` pack is order-preserving, so the compiled
#: pattern is byte-identical to the historical inline literal).
_WIZARD_ADVANCE_RE = vocab.ADVANCE_RE
#: A commit / terminal-boundary label the refuse pack does NOT universally flag
#: (a generic "Submit"/"Confirm"/"Place order"/"Checkout"…). Its presence VETOES
#: an advance even when the guard did not mark the control danger — the second,
#: fail-closed gate over the guard so a wizard walk can never cross a submit.
#: Union across every language pack: a wider commit vocabulary only ever fails
#: CLOSED (more vetoes, and a wider submit-boundary detection — never fewer).
_WIZARD_COMMIT_RE = vocab.COMMIT_RE


def _links_to_site_root(control: Mapping[str, Any]) -> bool:
    """Does this anchor point at the site ROOT (the header logo / home link)?

    Value-free and structural: only the URL PATH is inspected, never the label,
    because a brand name is unguessable and localised. "/" and "" are home;
    everything deeper is a real destination the funnel may legitimately use.
    """
    href = str((control.get("qec") or {}).get("href")
               or control.get("href") or "").strip()
    if not href:
        return False
    try:
        path = urlsplit(href).path or "/"
    except Exception:
        return False
    return path.rstrip("/") == ""


def _candidate_sig(candidates: Sequence[Mapping[str, Any]]) -> str:
    """A stable signature of the controls that could ACTUALLY be advanced on.

    The oracle's "nothing advances here" is only reusable while the page keeps
    offering the same actionable set. A page fingerprint does not capture this:
    filling a form changes no DOM structure, so the fingerprint is identical
    before and after — while a disabled Continue quietly becomes a live one.
    Keying a negative verdict on the fingerprint alone therefore froze the page
    as unadvanceable forever. Names are product UI text, never values.
    """
    return "|".join(sorted(
        str(c.get("name") or "").strip().lower()
        for c in (candidates or ()) if str(c.get("name") or "").strip()))


def _is_wizard_advance(name: str) -> bool:
    """True for a Next/Continue/Proceed/Forward control that carries NO commit /
    terminal word — the fail-closed advance gate (any commit signal vetoes)."""
    n = (name or "").strip()
    if not _WIZARD_ADVANCE_RE.search(n):
        return False
    return not _WIZARD_COMMIT_RE.search(n)


def _decision_points(field_ledger: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The decision points a fill met on one step (Journey Graph C0).

    An enumerable control (its ledger entry carries ``options``) is a fork in
    the business flow. Each record names the fork (value-free control
    signature + label), the enumerated option labels, WHICH option the fill
    took (``choice`` — absent when the field went unanswered: a fork
    discovered but not decided), and the provenance of that choice."""
    out: list[dict[str, Any]] = []
    for e in field_ledger or ():
        if not isinstance(e, Mapping) or "options" not in e:
            continue
        dp: dict[str, Any] = {
            "control_signature": str(e.get("signature") or ""),
            "control_label": str(e.get("name") or "")[:120],
            "options": [str(o)[:80] for o in (e.get("options") or ())][:24],
            "provenance": str(e.get("provenance") or ""),
        }
        # The QUESTION, when the DOM declared one (a radio group). Every member
        # reports the same group_id, so the fold records ONE decision with N
        # branches instead of one phantom decision per member.
        group_id = str(e.get("group_id") or "")
        if group_id:
            dp["group_id"] = group_id
        choice = e.get("choice")
        if choice:
            dp["choice"] = str(choice)[:80]
        out.append(dp)
    return out


#: Next-action fork classes (Journey Graph). A terminal decision page (a quote
#: summary, an order confirmation) offers a choice between NEXT-ACTION controls,
#: not a form field. Each option is classified so the fold knows which is the
#: forward business path (walkable, subject to Phase-B approval) and which are
#: surfaced-but-not-walked. Value-free: derived from the refuse pack (danger) and
#: the language-pack commit vocabulary, never from hardcoded labels.
NEXT_ACTION_FORWARD = "forward"           # a commit-word action (Apply Now, Place Order)
NEXT_ACTION_DESTRUCTIVE = "destructive"   # refuse-pack danger (Start Over, Delete)
NEXT_ACTION_NAVIGATIONAL = "navigational"  # leaves the funnel (Back to Dashboard, Home)

#: A SESSION control (sign/log in|out|on) is authentication chrome present on
#: every authenticated page — never a business next-action. It is excluded from a
#: next-action fork entirely, because the commit vocabulary matches "sign" in
#: "Sign out", which would otherwise make logging out a walkable "forward" branch
#: on every page. Generic session vocabulary, not app labels.
_AUTH_SESSION_RE = re.compile(r"\b(sign|log)\s*(in|out|on)\b", re.IGNORECASE)


def _next_action_decisions(
    controls: Sequence[Mapping[str, Any]], fingerprint: str,
) -> list[dict[str, Any]]:
    """A next-action fork: the mutually-exclusive things a decision page offers.

    A page whose whole content is a choice between action BUTTONS — a quote
    summary's "Apply Now / Start Over / Back to Dashboard" — is a business fork,
    but it carries no fillable field, so :func:`_decision_points` (which reads the
    form ledger) never sees it and the fork was lost. This produces the decision
    point directly from the controls so the fold turns it into one branch per
    option, exactly as it does for a ``<select>``.

    SCOPED to avoid turning every nav bar into a fork:
      * a BUTTON is always an action and is kept; a LINK is kept only when it
        carries an action signal — a commit word, a danger flag, or the site root —
        so a plain in-page nav link (Dashboard, Beneficiaries) stays out as chrome;
      * a session control (sign/log in|out) is excluded — it is auth chrome;
      * the node is emitted ONLY when at least one option is a FORWARD commit
        action (Apply Now, Place Order, Submit Application). A page with no forward
        action emits nothing.

    The forward action is the reliable, value-free signal (the same commit
    vocabulary the submit boundary already uses). A single forward option is
    enough — a quote summary whose only groundable action is "Apply Now" is a
    business flow ("apply"), recorded as one walkable branch. Non-forward
    alternatives on the SAME page (a plain "Start Over"/"Back" link with no commit
    or danger signal) are structurally indistinguishable from nav chrome in a flat
    control list, so they are NOT invented as branches — recording only what can be
    grounded, never a guess.

    Value-free throughout: classification uses the refuse-pack ``danger`` flag +
    the commit vocabulary + the structural site-root check — no hardcoded strings,
    so it generalises to any app in any language.
    """
    options: list[str] = []
    classes: dict[str, str] = {}
    for c in controls or ():
        if c.get("disabled"):
            continue
        kind = c.get("kind")
        name = str(c.get("name") or "").strip()
        if not name or kind not in ("button", "link"):
            continue
        if _AUTH_SESSION_RE.search(name):
            continue  # a sign-in / sign-out control is session chrome, never a fork option
        # Classify — and let the class ALSO decide inclusion, because SPA apps
        # render their action controls as anchors (a Next.js "Apply Now" is an
        # <a>, not a <button>). A BUTTON is always an action, so it is kept whatever
        # it says. A LINK is kept only when it carries an action signal — a commit
        # word (Apply Now), a danger flag (Start Over), or the site root (Home) —
        # so a plain in-page nav link (Dashboard, Beneficiaries, Back to Dashboard)
        # stays out as chrome. That signal is what separates an action-link from a
        # navbar-link without any per-app labels.
        if c.get("danger"):
            cls = NEXT_ACTION_DESTRUCTIVE
        elif _WIZARD_COMMIT_RE.search(name):
            cls = NEXT_ACTION_FORWARD
        elif kind == "link" and _links_to_site_root(c):
            cls = NEXT_ACTION_NAVIGATIONAL
        elif kind == "button":
            cls = NEXT_ACTION_NAVIGATIONAL   # a plain action button (e.g. "Back")
        else:
            continue  # a plain in-page nav link — chrome, not a decision
        if name in classes:
            continue  # de-dup by label (first classification wins)
        options.append(name)
        classes[name] = cls
    if not any(v == NEXT_ACTION_FORWARD for v in classes.values()):
        return []
    # A value-free, crawl-stable signature: the SET of option labels on this node.
    # Sorting makes it order-independent; binding to the node fingerprint keeps two
    # different pages' identical button sets distinct. Never a value, only labels.
    digest = hashlib.sha256(
        ("\x1f".join(sorted(n.lower() for n in options)) + "@" + (fingerprint or "")).encode("utf-8")
    ).hexdigest()[:32]
    return [{
        "control_signature": f"nextaction:{digest}",
        "control_label": "Next action",
        "options": options[:24],
        "provenance": "next_action",
        "option_classes": {n: classes[n] for n in options[:24]},
    }]


#: Oracle consultation outcomes. "The LLM said nothing advances" and "the LLM
#: could not be asked / could not answer" are DIFFERENT facts: the first ends a
#: journey honestly (no_advance), the second must end it as NOT covered
#: (oracle_unavailable) — an infrastructure failure is never a covered journey.
ORACLE_NOT_CONSULTED = "not_consulted"
ORACLE_PICKED = "picked"
ORACLE_NONE = "none"
ORACLE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AdvanceDecision:
    """One advance decision: WHICH control (if any), WHO decided (tier), and —
    when the agent oracle was consulted — HOW that consultation ended.

    ``tier`` is the evidence: 1 = strict regex, 2 = relaxed regex, 3 = agent
    oracle, 0 = no advance found.  ``signature`` is the oracle's value-free
    decision-point signature (empty unless tier 3), carried into the flow step
    so a PROVEN pick can be harvested into tenant advance memory."""

    control: Optional[dict[str, Any]] = None
    tier: int = 0
    oracle_status: str = ORACLE_NOT_CONSULTED
    signature: str = ""
    #: A forward control the normal advance tiers had to skip because it is
    #: refuse-pack DANGER (an application's "Continue to Underwriting Decision" —
    #: "underwrite" is irreversible), but which IS the real next step. On a
    #: disposable-blanket env the walk crosses it through the SUBMIT path instead
    #: of clicking it as a plain advance (which the network guard would block).
    #: None on every ordinary step — non-danger advances are handled by tiers 1-3.
    submit_control: Optional[dict[str, Any]] = None


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
        recalled_values: Optional[dict[str, str]] = None,
        field_priors: Optional[dict[str, Any]] = None,
        identity_seed: str = "",
        data_mode: str = "user",
        crawl_mode: str = "explore",
        credentials: Optional[Credentials] = None,
        session_injected: bool = False,
        allowed_hosts: Sequence[str] = (),
        max_relogins: int = 3,
        submit_approvals: Sequence[str] = (),
        wizard_enabled: bool = True,
        plan: Optional[dict[str, Any]] = None,
        scope_path_prefixes: Sequence[str] = (),
        sleep: Any = asyncio.sleep,
        advance_oracle: Optional[Callable[..., Any]] = None,
        choice_overrides: Optional[Mapping[str, str]] = None,
        e2e_wizard_steps: int = _E2E_WIZARD_STEPS,
        e2e_wizard_advances: int = _E2E_WIZARD_ADVANCES,
        observe_only: bool = False,
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
        # E2E budgets are DEPLOY-CONFIGURABLE (a fifteen-step funnel needs
        # more than a probe budget); explore/target keep the probe bounds.
        self._max_wizard_steps = (int(e2e_wizard_steps) if self._crawl_mode == "e2e"
                                  else _MAX_WIZARD_STEPS)
        self._max_wizard_advances = (int(e2e_wizard_advances) if self._crawl_mode == "e2e"
                                     else _MAX_WIZARD_ADVANCES)
        # BUSINESS FLOWS: one entry per journey walked, carrying whether it actually
        # REACHED THE END. Six steps of a fifteen-step funnel is not the Apply flow.
        self._flows: list[dict[str, Any]] = []
        # E2E: when regex cannot identify the advance control, ask the LLM.
        self._advance_oracle = advance_oracle
        # Tier-3 outcomes memoized per state fingerprint: the wizard entry check
        # and the loop's first iteration see the SAME page, and a re-visited step
        # must not pay a second LLM call. ``unavailable`` is deliberately NOT
        # memoized — transient trouble may pass; the circuit breaker (in the
        # oracle callable) owns systemic failure. Value: (picked control name or
        # None, oracle status, decision-point signature).
        self._oracle_memo: dict[str, tuple[Optional[str], str, str]] = {}
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
        # Auth was requested (credentials supplied) but no login form could be driven at
        # the entry — the page is accessible PUBLIC content, so we explore it rather than
        # throw it away. Surfaced LOUDLY in coverage so the operator knows authenticated
        # areas were NOT covered (never a silent, green-washed "success").
        self._auth_incomplete = False
        self._auth_incomplete_reason = ""
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
        # OPAQUE surfaces the DOM can't read (cross-origin embeds, canvas apps, closed
        # shadow) — detected + named so the coverage ledger flags a blind spot instead of a
        # silent skip. {kind, label, reason}; deduped in coverage.
        self._opaque_surfaces: list[dict[str, str]] = []
        # Interactive controls the matcher registry has NO primitive for — named in the
        # ledger as UNHANDLED (on the roadmap), never a silent skip. {label, kind}.
        self._unhandled_controls: list[dict[str, str]] = []
        self._submit_candidates: list[str] = []    # a submit found but not clicked (Phase-A boundary)
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

    def _collect_ledger(self, entries: list[dict[str, Any]], url: str) -> None:
        """Merge this state's field ledger into the crawl-wide one, deduped by
        signature.

        Deduped because the same field on ten pages is ONE thing to ask the client
        about — a residue list that repeats itself is the reason operators stop
        reading it. The first sighting wins and keeps its page, which is what lets
        the ask say WHICH flow the field belongs to."""
        seen = {e.get("signature") for e in self._field_ledger}
        for entry in entries or ():
            sig = entry.get("signature")
            if not sig or sig in seen:
                continue
            seen.add(sig)
            row = dict(entry)
            row["url"] = url
            self._field_ledger.append(row)

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

        def _dedup_opaque(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                k = f"{d.get('kind')}|{d.get('label')}"
                if k not in seen:
                    seen.add(k)
                    out.append({"kind": str(d.get("kind") or ""),
                                "label": str(d.get("label") or ""),
                                "reason": str(d.get("reason") or "")})
            return out

        def _dedup_unhandled(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                lbl = str(d.get("label") or "").strip()
                if lbl and lbl.lower() not in seen:
                    seen.add(lbl.lower())
                    out.append({"label": lbl, "kind": str(d.get("kind") or ""),
                                "reason": str(d.get("reason") or "")})
            return out

        inferred = _dedup(self._fields_inferred)
        needs_seed = _dedup(self._fields_unfilled)
        field_ledger = self._field_ledger[:500]
        needs_seed_detail = _dedup_detail(self._fields_seed_detail)
        opaque_surfaces = _dedup_opaque(self._opaque_surfaces)
        unhandled_controls = _dedup_unhandled(self._unhandled_controls)
        submits = _dedup(self._submit_candidates)
        unexercised = max(0, len(submits) - self._forms_submitted)
        # Honest, LOUD auth prefix: if credentials were supplied but no login form could
        # be driven, the crawl covered PUBLIC pages only — say so plainly, never imply the
        # authenticated app was covered.
        if not self._auth_incomplete:
            auth_prefix = ""
        elif self._auth_incomplete_reason == AUTH_SESSION_EXPIRED:
            # Different cause, different remediation: nobody needs to check the
            # credentials — the stored SESSION died and must be re-recorded.
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — the stored login session has "
                "EXPIRED (the app still presented a login wall while holding it); "
                "crawled the accessible (public) pages only. Re-record the login to "
                "restore authenticated coverage. "
            )
        else:
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — credentials were supplied but no login "
                f"form was found/completed at the entry ({self._auth_incomplete_reason}); "
                "crawled the accessible (public) pages only. "
            )
        return {
            "forms_found": self._forms_found,
            "forms_submitted": self._forms_submitted,
            "fields_inferred": inferred,
            # PER-FIELD LEDGER (field learning). One entry per distinct field the
            # crawl met: what it is, how it was answered, whether it committed.
            # No values — this travels back to qe-central and into evidence.
            "field_ledger": field_ledger,
            # BUSINESS FLOWS. Each journey walked, and whether it REACHED THE END.
            # The summary states branch_coverage=False explicitly, so walking every
            # flow once can never be read as having covered every business path.
            "flows": self._flows[:200],
            "flow_summary": flow_ledger.summarize(self._flows),
            "crawl_mode": self._crawl_mode,
            "fields_needing_seed": needs_seed,
            # Per-field page context {label, url} — the grounded source for flow grouping.
            # Kept alongside the flat list (which stays for back-compat).
            "fields_needing_seed_detail": needs_seed_detail,
            # DOM-unreadable surfaces detected on the crawl → the ledger's OPAQUE rows.
            "opaque_surfaces": opaque_surfaces,
            # Interactive controls the matcher has no primitive for → the ledger's UNHANDLED rows.
            "unhandled_controls": unhandled_controls,
            "submit_candidates": submits,
            # Auth was requested but no login form could be driven → PUBLIC-only crawl.
            # Surfaced as first-class coverage so the operator is never misled.
            "auth_incomplete": self._auth_incomplete,
            "auth_reason": self._auth_incomplete_reason,
            "summary": (
                auth_prefix
                + f"{self._forms_found} form(s) found; "
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
            # An injected session needs no login driven — but it DOES need proving.
            # That happens in _note_login_wall_while_authenticated, which sees every
            # recorded state (the entry included) and so costs no extra navigation.
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
            fingerprint=result.before_fingerprint or state_fingerprint(
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
        return await self._port.current_url()

    async def _cross_auth_wall(
        self, obs: PageObservation, controls: list[dict[str, Any]], url: str,
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
        # Return to where the journey was heading. Login usually lands on a
        # dashboard, so without this the crawl resumes somewhere else entirely and
        # the step that provoked the wall is never taken.
        nav = await self._port.goto(url)
        self._tracker.note_request()
        if not nav.ok:
            logger.info("qec.crawler.relogin_return_failed error=%s", (nav.error or "")[:120])
            return obs, controls
        fresh = await self._observe()
        logger.info(
            "qec.crawler.auth_wall_crossed url_scope=%s — journey continues authenticated",
            _host_of(fresh.url),
        )
        return fresh, build_inventory(fresh.raw_controls, self._refuse_pack, url=fresh.url)

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
        self._auth_incomplete_reason = AUTH_SESSION_EXPIRED
        logger.warning(
            "qec.crawler.session_expired crawl_id=%s — a start-authenticated session "
            "was injected but the app still presents a login wall; the crawl covered "
            "PUBLIC pages only (authenticated areas NOT covered). Re-record the login.",
            self.crawl_id,
        )

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
        # An auth wall is a STEP IN THE JOURNEY, not the end of it. Real business
        # journeys cross one all the time (public quote → authenticated apply →
        # e-sign); stopping here would catalogue two fragments and never the flow
        # the business actually sells.
        obs, controls = await self._cross_auth_wall(obs, controls, item.url)
        # Requested vs landed is what separates "the app sent us to sign in" from
        # "the crawl followed a link to the sign-in page".
        self._note_login_wall_while_authenticated(controls, item.url, obs.url)
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
                priors=self._field_priors, data_mode=self._data_mode,
                choice_overrides=self._choice_overrides,
            )
            actions.extend(fill.actions)
            self._tracker.note_action(len(fill.actions))
            self._fields_inferred.extend(fill.inferred_fields)
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
        # WIZARD/STEPPER (#1): on a FILLED form state, advance a non-danger
        # Next/Continue to record deeper wizard steps in place (SPA quote wizards
        # live at one URL — step 2 is reachable only by the click sequence). The
        # walk OWNS the recording of this step + every step it reaches; when there
        # is no advance trigger it returns False and this state is recorded normally.
        walked = False
        entry_pick = AdvanceDecision()
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
                    in ("currency", "decision", "percent")],
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
                        in ("currency", "decision", "percent")],
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
            # The matcher registry decides which controls need the open-probe (a custom choice
            # whose options only appear on open) — new widgets plug in via a matcher rule.
            if not matcher.needs_open_probe(c):
                continue
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

    async def _commit_act(self, control: dict[str, Any]) -> bool:
        """Perform ONE grounded, non-submitting act on a driver control so a dependent
        field/options can react: a SELECT commits its first option; a RADIO is clicked; a
        CHECKBOX/TOGGLE is switched on. Returns True iff an act fired. EXPLORE-phase only —
        none of these submit anything server-side."""
        # SAFETY: never actuate a control that isn't affirmatively a safe value control —
        # a destructive / money-moving / account-consequential label (any language) or a
        # danger-flagged control is left alone (fail-closed).
        if not danger_signals.safe_to_actuate(control):
            return False
        kind = control.get("kind")
        if kind == "select":
            return await self._commit_choice(control)
        try:
            if kind == "radio":
                await self._port.click(dict(control))
            elif kind in ("checkbox", "toggle"):
                set_checked = getattr(self._port, "set_checked", None)
                if set_checked is not None:
                    await set_checked(dict(control), True)
                else:
                    await self._port.click(dict(control))
            else:
                return False
            self._tracker.note_action()
            return True
        except Exception:
            return False

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
        self, controls: list[dict[str, Any]], *, url: str,
    ) -> None:
        """ACT-THEN-DIFF: commit ONE driver act (select an option / pick a radio / switch a
        toggle), re-observe, and DIFF the inventory to capture what the act CHANGED:
          (a) a DEPENDENT select whose options only populate after the act (To Account after
              From Account) — captured + tagged depends_on;
          (b) a CONDITIONALLY-REVEALED field that only appears after the act (choose 'Other'
              -> a text field; 'Schedule for later' -> a date picker) — appended to the
              snapshot + tagged depends_on.
        Bounded, EXPLORE-phase (no submit). HONESTY: everything captured this way is tagged
        depends_on=<driver> so it reads as CONDITIONAL on that driver, never as always-present
        or a fixed list; any failure leaves the field an honest unread/absent state; if an act
        navigates away, the pass bails rather than attributing another page's fields."""
        collect = getattr(self._port, "collect_controls", None)
        current_url = getattr(self._port, "current_url", None)
        if collect is None:
            return

        def _key(c: Mapping[str, Any]) -> str:
            return str(c.get("name") or "").strip().lower()

        # The matcher registry identifies ACT-THEN-DIFF drivers (a choice/radio/toggle whose
        # act can reveal a dependent); the safety gate in _commit_act still fail-closes each.
        drivers = [c for c in controls if matcher.is_diff_driver(c)]
        if not drivers:
            return
        seen_names = {_key(c) for c in controls if c.get("name")}
        empty_by_name = {
            str(c.get("name") or ""): c for c in controls
            if c.get("kind") == "select" and not c.get("options") and c.get("name")
        }
        acted = 0
        for d in drivers:
            if acted >= _MAX_DEP_PROBES:
                break
            if not await self._commit_act(d):
                continue
            acted += 1
            # If the act navigated away, this page is gone — do not attribute its fields.
            if current_url is not None:
                try:
                    if self._in_scope_key(await current_url()) != self._in_scope_key(url):
                        return
                except Exception:
                    pass
            after = build_inventory(await collect(), self._refuse_pack, url=url)
            driver_label = d.get("name") or ""

            # (a) DEPENDENT selects: empty -> populated (open-probe custom ones so they surface).
            pending = [c for c in after
                       if c.get("kind") == "select" and str(c.get("name") or "") in empty_by_name]
            await self._probe_select_options(
                [c for c in pending if not c.get("options")], url=url)
            for r in pending:
                nm = str(r.get("name") or "")
                if r.get("options") and nm in empty_by_name:
                    tgt = empty_by_name.pop(nm)
                    tgt["options"] = list(r.get("options") or [])[:_MAX_PROBED_OPTIONS]
                    tgt["depends_on"] = driver_label
                    if isinstance(tgt.get("qec"), dict):
                        tgt["qec"]["options"] = tgt["options"]

            # (b) CONDITIONALLY-REVEALED fields: value-bearing controls that were not present
            # before the act. Append to the snapshot (so the manifest sees them) tagged
            # depends_on; if a revealed field is itself an empty custom select, register it as
            # a further dependent to probe on a later act.
            for r in after:
                k = _key(r)
                if not k or k in seen_names or r.get("kind") not in _FIELD_KINDS:
                    continue
                seen_names.add(k)
                r["depends_on"] = driver_label
                if isinstance(r.get("qec"), dict):
                    r["qec"]["depends_on"] = driver_label
                controls.append(r)
                if r.get("kind") == "select" and not r.get("options"):
                    empty_by_name[str(r.get("name") or "")] = r

    def _in_scope_key(self, url: str) -> str:
        """Path-level identity of a URL for the ACT-THEN-DIFF nav guard (host+path, query/
        hash-insensitive) — a same-page DOM change must NOT read as a navigation."""
        parts = urlsplit(url or "")
        return f"{(parts.hostname or '').lower()}{parts.path}"

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
            )
        finally:
            self._guard.phase = prev_phase
            self._guard.submit_flow_approved = prev_approved
        if result.submitted:
            self._forms_submitted += 1
            self._tracker.note_action()
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

    # -- wizard / stepper traversal (#1) ---------------------------------------

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

    def _note_boundary_controls(self, controls: Sequence[dict[str, Any]]) -> None:
        """Record the commit-boundary controls this state offers.

        Submit candidates used to be collected ONLY from a form fill, so a page
        with no input fields contributed none. That is exactly the shape of the
        page where a business journey actually forks: a quote summary whose whole
        content is "here is your price — Apply Now / Start Over / Back to
        Dashboard". Live-observed on the VKPower funnel — the walk ended
        `no_advance`, `Apply Now` never appeared in `submit_candidates`, and no
        branch was ever recorded, so the catalogue held no continuation at all and
        the operator had nothing to approve.

        Recording only. Nothing here decides to click anything; the walk's commit
        veto and the Phase-B approval path are untouched.
        """
        for c in controls:
            if c.get("kind") not in ("button", "link") or c.get("disabled"):
                continue
            name = str(c.get("name") or "").strip()
            # Danger controls are the refuse pack's call and are never offered as
            # something to approve — "Start Over" wipes the quote.
            if not name or c.get("danger"):
                continue
            if _WIZARD_COMMIT_RE.search(name):
                self._submit_candidates.append(name)

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
        logger.info(
            "qec.wizard.no_tier1 approvals=%s verdicts=%s",
            sorted(self._submit_approvals),
            [f"{str(c.get('name') or '')[:20]}"
             f":btn={c.get('kind') == 'button'}"
             f":dis={bool(c.get('disabled'))}:dang={bool(c.get('danger'))}"
             f":appr={str(c.get('name') or '').strip().lower() in self._submit_approvals}"
             f":adv={_is_wizard_advance(str(c.get('name') or '').strip())}"
             for c in (controls or ()) if c.get("kind") == "button"][:8])
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

        # DIAGNOSTIC (temporary): reaching here means tiers 1-2 found no advance —
        # the walk is STUCK. Dump the control inventory WITH toggle/group state
        # (pressed / aria_checked / group_key / testid) so a questionnaire rendered
        # as custom buttons (a "Yes"/"No" answer set) becomes legible: which buttons
        # are answers, how they group, and which is selected. Runs BEFORE the
        # danger-forward crossing precisely so it still fires on a page like
        # /apply/lifestyle. Value-free. Remove once questionnaire capture is built.
        logger.warning(
            "qec.wizard.stuck_inventory url=%s n=%d controls=%s",
            (page_url or "")[:120], len(controls),
            [{"name": str(c.get("name") or "")[:30], "kind": c.get("kind"),
              # anchor/landmark decide whether an identical bare button can be
              # targeted at all: a per-question container name is the only handle.
              "anchor": str((c.get("anchor") or {}).get("label") or "")[:40],
              "landmark": str((c.get("landmark") or {}).get("name") or "")[:40],
              "group": str(c.get("group_key") or "")[:16],
              "dis": bool(c.get("disabled")), "dng": bool(c.get("danger"))}
             for c in controls][:50],
        )

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
        if self._submit_enabled and self._submit_approve_all:
            for c in controls:
                if c.get("kind") not in ("button", "link") or c.get("disabled"):
                    continue
                if not c.get("danger"):
                    continue
                name = str(c.get("name") or "").strip()
                if not name or _AUTH_SESSION_RE.search(name):
                    continue
                if _WIZARD_COMMIT_RE.search(name) or _WIZARD_ADVANCE_RE.search(name):
                    return AdvanceDecision(submit_control=dict(c))

        # Tier 3: ask the agent which control advances the flow.
        if self._advance_oracle is None:
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

        try:
            outcome = await self._advance_oracle(candidates, page_title, page_url)
        except Exception:
            outcome = None
        if not isinstance(outcome, dict):
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
        """Mode-routed advance decision. Explore/target stays the strict regex
        with no oracle state; E2E runs the 3-tier detection."""
        if self._crawl_mode == "e2e":
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
            logger.info("qec.wizard.declined reason=no_advance_control url=%s "
                        "tier=%s oracle=%s controls=%d",
                        url, entry_pick.tier, entry_pick.oracle_status,
                        len(controls or ()))
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
        # The re-fill's ledger is ALSO the entry step's truth: real fill counts
        # and the decision points (forks) this step offered — Journey Graph C0.
        cur_filled, cur_unfilled, cur_intent_unmet = 0, 0, 0
        cur_dps: list[dict[str, Any]] = []
        if any((c.get("kind") in _FILLABLE_KINDS) and not _is_password(c) for c in refreshed):
            refill = await fill_form_phase_a(
                self._port, refreshed, self._answer_key or AnswerKey(), self._clock,
                phase=Phase.EXPLORE.value, state_id=fingerprint,
                identity=self._identity, recalled=self._recalled_values,
                priors=self._field_priors, data_mode=self._data_mode,
                choice_overrides=self._choice_overrides)
            cur_filled = refill.filled
            cur_unfilled = len(refill.unfilled_fields)
            cur_intent_unmet = refill.intent_unmet
            cur_dps = _decision_points(refill.field_ledger)

        cur_url, cur_title, cur_controls, cur_fp = url, title, controls, fingerprint
        # THIS WALK's own path. Loop protection has to mean "I have already been
        # here IN THIS JOURNEY" — not "the crawler has seen this page at some
        # point". The crawler's outer loop visits every funnel page as a state in
        # its own right, so testing against the global set meant a walk could
        # never advance THROUGH a page that had been seen, and a five-page quote
        # funnel was recorded as a chain of two-step fragments terminating in
        # `loop`. The journey was walked; the evidence just never said so.
        walk_seen: set[str] = {fingerprint}
        cur_actions = list(base_actions)
        cur_shot, cur_first = entry_shot, first_seen_ms
        cur_dv, cur_nc = displayed_values, network_calls
        depth, steps = item.depth, 0
        flow_steps: list[dict[str, Any]] = []

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
            pick = await self._pick_advance(cur_controls, cur_url, cur_title, cur_fp)
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
                        in ("currency", "decision", "percent")],
                    max_steps=self._max_wizard_steps))
                await self._execute_approved_submit(
                    name=str(pick.submit_control.get("name") or "").strip(),
                    control=pick.submit_control, url=cur_url, fingerprint=cur_fp,
                    depth=item.depth, renavigate=False)
                return True
            trig = pick.control
            advance: Optional[tuple[Any, list[dict[str, Any]], str]] = None
            budget_left = (steps < self._max_wizard_steps
                           and self._wizard_advances < self._max_wizard_advances
                           and depth < self._budget.max_depth)
            stopped = bool(self._tracker.stop_reason() or self._cancelled)
            if trig is not None and budget_left and not stopped:
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
                if new_fp != cur_fp and new_fp not in walk_seen:
                    cur_actions.append(action)
                    walk_seen.add(new_fp)
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
                if stopped:
                    terminal = flow_ledger.TERMINAL_CANCELLED
                elif trig is None:
                    if pick.oracle_status == ORACLE_UNAVAILABLE:
                        # The regex tiers found nothing and the agent could not
                        # be reached — whether more funnel existed is UNKNOWN.
                        # Unknown is never reported as covered; even a commit
                        # button on this page does not upgrade the walk to a
                        # covered boundary, because a non-commit advance may
                        # have existed that nobody could identify.
                        terminal = flow_ledger.TERMINAL_ORACLE_UNAVAILABLE
                    elif self._pick_submit_candidate(cur_controls):
                        terminal = flow_ledger.TERMINAL_SUBMIT_BOUNDARY
                    else:
                        terminal = flow_ledger.TERMINAL_NO_ADVANCE
                elif not budget_left:
                    terminal = flow_ledger.TERMINAL_BUDGET
                else:
                    # A trigger existed and the budget allowed it, so the click
                    # produced no effect or landed on a state already seen.
                    terminal = flow_ledger.TERMINAL_LOOP
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
                        in ("currency", "decision", "percent")],
                    max_steps=self._max_wizard_steps))
                # CROSS the boundary. The walk reached a submit boundary (a quote
                # summary with "Apply Now") and recorded it; now, on a disposable
                # attested env, click the approved forward action IN PLACE (the
                # summary's state was built by this walk — never re-navigate) and
                # push the resulting page (/portal/apply) so the application → e-sign
                # funnel is crawled as the continuation. Gated exactly as any submit;
                # a non-disposable env leaves self._submit_enabled False and stops at
                # the boundary as before.
                if terminal == flow_ledger.TERMINAL_SUBMIT_BOUNDARY:
                    await self._maybe_submit_next_action(
                        controls=cur_controls, url=cur_url, fingerprint=cur_fp,
                        depth=item.depth)
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
                    priors=self._field_priors, data_mode=self._data_mode,
                    choice_overrides=self._choice_overrides)
                step_actions.extend(filled.actions)
                self._tracker.note_action(len(filled.actions))
                cur_filled = filled.filled
                cur_unfilled = len(filled.unfilled_fields)
                cur_intent_unmet = filled.intent_unmet
                cur_dps = _decision_points(filled.field_ledger)
                self._collect_ledger(filled.field_ledger, obs.url or "")
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
        # EVERY recorded state, whichever path reached it. Hung off _expand alone,
        # this missed every page the WIZARD WALK reaches — which is precisely where
        # a funnel ends. Live-observed: the quote-summary page rendered `Apply Now`,
        # `Start Over` and `Back to Dashboard` (confirmed in the crawl's own
        # screenshot) and contributed no boundary control at all, while the same fix
        # captured `Add Beneficiary` on a page reached by ordinary navigation.
        self._note_boundary_controls(controls)
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
        meta = {
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
        if self._scope_path_prefixes:  # Target-mode audit trail (mapper ignores extras)
            meta["scope_path_prefixes"] = list(self._scope_path_prefixes)
        return meta


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
