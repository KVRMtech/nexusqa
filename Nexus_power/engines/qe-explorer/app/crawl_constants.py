"""Shared crawl vocabulary: stop reasons, postures, bounds, and pure helpers.

Extracted VERBATIM from :mod:`app.crawler` (M0.3, prerequisite for T-DE-10..13).

WHY THIS MODULE EXISTS.  The four remaining extractions — auth, submit,
discovery and the walk — all read the same regexes, bounds and stop-reason
constants.  If each moved module imported those from ``crawler.py`` the
decomposition would rebuild the exact cycle it exists to remove: crawler
imports walker, walker imports crawler.  Hoisting the shared vocabulary DOWN
into a leaf lets every extracted module import downward and lets ``crawler.py``
re-export the public names, so no external import site changes.

Everything here is a constant, a compiled regex, a frozen dataclass or a pure
function.  There is no state and no I/O, which is what makes it safe to be
depended upon by everything above it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from . import vocab
from .fingerprint import url_template
from .guard import Attestation

# ─── Honest stop reasons ─────────────────────────────────────────────────────
# The three BUDGET stop reasons live with the tracker that raises them
# (:mod:`app.budget`) and are re-exported here so every existing import site —
# production and test — keeps working unchanged.
STOP_COMPLETED = "completed"

STOP_CANCELLED = "cancelled"

STOP_AUTH_FAILED = "auth_failed"

#: Entry is gated behind a login wall, but the crawl was given NO credentials and
#: NO session — nothing to authenticate with. Stop HONESTLY at the sign-in instead
#: of filling synthetic data into a login form for the whole wall-clock budget (the
#: loop that used to surface, dishonestly, as ``budget_max_wall_ms``). Detected
#: structurally: the entry REDIRECTS to a screen presenting a full login form.
STOP_AUTH_REQUIRED = "auth_required_no_credentials"

STOP_ERROR = "error"

# ─── M1.7 · the green-wash stops ─────────────────────────────────────────────
# Three terminal reasons that did not exist, which is exactly why the conditions
# they name used to arrive at the completion line with an empty ``_stop_reason``
# and be reported as ``completed``.  A condition with no name cannot be reported,
# and an unreported failure becomes a success by default.

#: T-GW-01 — an INVENTORY READ FAILED and could not be recovered.  The page
#: behind it was never observed, so nothing may be claimed about it.  Distinct
#: from ``error``: the crawl loop did not crash, it was denied its evidence.
STOP_INVENTORY_FAILED = "inventory_failed"

#: T-GW-03 — this run was dispatched as a RESUME of an existing crawl id and the
#: durable prefix could not be rebuilt into a continuable crawl (a lost or
#: unmounted evidence volume, a truncated manifest).  Failing here is what stops
#: a resume from silently re-crawling from zero and superseding a real crawl's
#: evidence with an empty capture.
STOP_RESUME_UNRECOVERABLE = "resume_unrecoverable"

#: T-GW-01/T-GW-03 — the crawl claimed completion and recorded ZERO page states.
#: The claim is refused: there is no evidence a page was ever observed.  Set by
#: :func:`app.completion.adjudicate`, never by the crawl loop.
STOP_NO_EVIDENCE = "no_evidence"

#: Stop reasons that mean the crawl DID NOT prove what it set out to prove.  The
#: engine, qe-central and the tests all read this one set, so a new failure
#: reason cannot be added on one side and silently read as success on the other.
FAILED_STOP_REASONS = frozenset({
    STOP_ERROR, STOP_AUTH_FAILED, STOP_INVENTORY_FAILED,
    STOP_RESUME_UNRECOVERABLE, STOP_NO_EVIDENCE,
})

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
#: Dropdown option-probe bounds — custom comboboxes probed per state, and options kept
#: per dropdown. Read-only (open → read labels → dismiss), bounded, state-restoring.
_MAX_OPTION_PROBES = 8

_MAX_PROBED_OPTIONS = 40

#: FULL-TRAVERSAL bounds. On an attested test environment the catalogue's job is
#: to hold every answer a question offers — "which state do you live in?" has
#: fifty-one, not forty. A dense underwriting form carries far more than eight
#: dropdowns, so the per-state probe ceiling rises with it. Still bounded, and any
#: list that still gets clipped is marked ``options_truncated`` rather than
#: silently shortened.
_FULL_OPTION_PROBES = 40

_FULL_PROBED_OPTIONS = 300

#: ACT-THEN-DIFF bounds — driver acts committed per state to reveal DEPENDENT options and
#: CONDITIONALLY-REVEALED fields. EXPLORE-phase, no submit; a committed choice/toggle is a
#: valid filled-form state to snapshot.
_MAX_DEP_PROBES = 8

#: FULL-TRAVERSAL dependency budget. Each act answers "which questions change when
#: this one is answered?" — the DEPENDENCY half of the catalogue, and the half a
#: generated negative/boundary case needs in order to be about a real rule rather
#: than a guess. Eight acts covers a probe; a real application form asks more.
_FULL_DEP_PROBES = 24

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

#: TRAVERSAL POSTURE — how far a business journey may be WALKED. Set by qe-central
#: from the environment attestation the operator signed (``prod_guard.
#: traversal_posture``); mirrored here as plain strings because the explorer shares
#: no code with qe-central.
#:
#: This dial decides how the crawl IDENTIFIES the forward control and how deep it
#: may walk — never what it is ALLOWED to click. The refuse-pack danger gate, the
#: commit boundary and the disposable-attestation submit tier are unchanged in
#: every posture and are re-checked at click time.
TRAVERSAL_FULL = "full"

#: The environment is attested non-prod: walk each journey to its end and catalogue
#: it. A funnel sampled six steps deep and reported as covered is green-wash.
TRAVERSAL_PROBE = "probe"

#: Fail-closed default — no signed statement about this environment, so sample it.
#: Byte-identical to the behaviour before the posture existed.
TRAVERSAL_OBSERVE = "observe"

#: Production: catalogue only (paired with ``observe_only``).
TRAVERSAL_POSTURES = (TRAVERSAL_FULL, TRAVERSAL_PROBE, TRAVERSAL_OBSERVE)

#: ENTRY-goto retry bounds — the first navigation can race the per-dispatch
#: egress-fence reconfigure (squid allowlist re-read); retry it briefly.
_ENTRY_GOTO_RETRIES = 2

_ENTRY_RETRY_DELAY_S = 2.5

#: In-app hops :meth:`Crawler._reach_in_app` may take toward a deep route — one
#: ancestor-section click per hop. Three covers section → subsection → page on a
#: real admin IA; anything deeper is honestly skipped, never wandered.
_REACH_MAX_HOPS = 3

#: ``auth_incomplete`` reason: a start-authenticated session WAS injected, but the
#: app still answers the entry with a login wall — the session has EXPIRED (or was
#: An "advance the wizard" control label (Next / Continue / Proceed / Forward) —
#: the POSITIVE intent signal. Union-compiled from the per-language packs in
#: :mod:`app.vocab` (the ``en`` pack is order-preserving, so the compiled
#: pattern is byte-identical to the historical inline literal).
_WIZARD_ADVANCE_RE = vocab.ADVANCE_RE

#: Bounds on the coverage states index. A crawl of a large application must not
#: turn its report into a second copy of the manifest — these are generous
#: enough for any funnel worth cataloguing and hard enough to keep the stats
#: column a report.
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

#: When answering a questionnaire to reach the end of a flow, prefer a
#: negative/decline answer: "Yes" on a health/lifestyle question typically reveals
#: follow-up questions, so choosing "No" keeps the walk short and reaches the
#: submit. A representative pass, not a claim about a real applicant — the choice
#: is recorded as the journey's decision either way.
_NEGATIVE_OPTION_HINTS = frozenset({"no", "none", "n/a", "na", "decline", "never", "false"})

#: value_infer types kept on a walked flow's terminal as business outcomes. P4
#: adds ``reference`` so a policy number / confirmation reference captured at the
#: tail (policy issue) is kept as evidence — value_infer already classifies those
#: as ``reference``. Value-free: a type label, never the value itself.
_BOUNDARY_OUTCOME_TYPES = ("currency", "decision", "percent", "reference")

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

def _url_key(url: str) -> str:
    return url_template(url)

def _host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()

def _segment_label(segment: str) -> str:
    """A URL path segment as the accessible-name it would render as:
    ``new-business`` → ``new business``. Value-free and language-agnostic."""
    return _norm_label(str(segment or "").replace("-", " ").replace("_", " "))

def _reach_label_match(name: Any, wanted: set[str]) -> bool:
    """Does a control's accessible name answer to one of the wanted labels?

    CONTAINMENT, not equality. Real navigation labels decorate the route word —
    the ``/new-business`` section renders as "New Business Queue", the
    ``/new-application`` entry as "+ New Application" — so exact matching missed
    every one of them (live: the wizard the operator onboarded was never entered
    because "new application" != "+ new application"). Containment in either
    direction, with a length floor so a two-letter segment can never match half
    the page.
    """
    label = _norm_label(name)
    if not label:
        return False
    for want in wanted:
        if not want:
            continue
        if len(want) < 4 or len(label) < 4:
            if label == want:
                return True
        elif want in label or label in want:
            return True
    return False

def _reach_target_labels(url: str, discovered_via: str) -> set[str]:
    """The labels a control leading to ``url`` would plausibly carry: the label
    the route was DISCOVERED through, and its own last path segment."""
    labels = {_norm_label(discovered_via)} if discovered_via else set()
    tail = [p for p in urlsplit(url).path.split("/") if p]
    if tail:
        labels.add(_segment_label(tail[-1]))
    labels.discard("")
    return labels

def _reach_ancestors(url: str) -> list[tuple[str, set[str]]]:
    """``[(url_key, labels)]`` for the target's ANCESTOR section pages, deepest
    first. A deep route's in-app entrance lives on its section pages —
    ``/underwriting/new-business/new-application`` is entered from the New
    Business queue — so the climb follows the route's own path, never a guess."""
    parts = urlsplit(url)
    segs = [p for p in (parts.path or "").split("/") if p]
    out: list[tuple[str, set[str]]] = []
    for i in range(len(segs) - 1, 0, -1):
        a_url = f"{parts.scheme}://{parts.netloc}/" + "/".join(segs[:i])
        labels = {_segment_label(segs[i - 1])} - {""}
        if labels:
            out.append((_url_key(a_url), labels))
    return out

def _reach_href_key(control: Mapping[str, Any], base_url: str) -> str:
    """The url-key a control's HREF resolves to, or ``""``. The strongest reach
    signal — a link's href says where it goes regardless of its label, which is
    the only signal a NAMELESS link offers (this app renders several)."""
    href = str((control.get("qec") or {}).get("href")
               or control.get("href") or "").strip()
    if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
        return ""
    try:
        return _url_key(urljoin(base_url or href, href))
    except Exception:
        return ""

def _reach_pick(
    controls: Sequence[Mapping[str, Any]], *, target_key: str,
    labels: set[str], base_url: str, allow_danger: bool = False,
) -> Optional[dict[str, Any]]:
    """The control to click toward a destination: an HREF resolving to it wins
    (works even nameless), else the first containment label match.

    Danger controls are skipped by default — the reach must not be the thing
    that clicks Delete. ``allow_danger`` relaxes ONLY the pick, and only the
    crawler sets it, only under the disposable-attested blanket: the refuse
    pack's verb vocabulary flags "+ New Application" (rp.verb.apply) exactly
    like a mutating Apply — live, the crawler stood on the queue page holding
    the wizard's entrance and refused it, so the one page the operator onboarded
    stayed unentered. Clicking toward the operator's own route on an env they
    attested disposable follows the same precedent as the danger-forward
    crossing in ``_pick_advance_e2e``; the fail-closed EXPLORE network guard
    remains the hard wall against any actual mutation."""
    by_label: Optional[dict[str, Any]] = None
    for c in controls:
        if str(c.get("kind") or "") not in _ACTUATOR_KINDS:
            continue
        if c.get("danger") and not allow_danger:
            continue
        if target_key and _reach_href_key(c, base_url) == target_key:
            return dict(c)
        if by_label is None and _reach_label_match(c.get("name"), labels):
            by_label = dict(c)
    return by_label

def _norm_label(text: Any) -> str:
    """Accessible-name / route-segment comparison key: case- and space-insensitive.
    Lets ``/new-application`` match a link reading "New application" without any
    app-specific knowledge."""
    return " ".join(("" if text is None else str(text)).split()).strip().lower()

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
