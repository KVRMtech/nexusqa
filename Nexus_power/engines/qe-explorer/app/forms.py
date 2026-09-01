"""QE-Central Contained Explorer — the TWO-PHASE FORM controller (design §3.2).

Phase A (this phase — fully implemented):
  * fill every fillable control for which the answer key supplies test data,
  * READ BACK the committed value from the live control (the recorded value is
    what the field actually holds, never what we intended to type),
  * STOP BEFORE SUBMIT — the terminal/submit controls are recorded as
    flow-candidates (with their fail-closed guard danger flag) and never clicked.

Phase B (submit — Phase-5 scope, ACTIVE):
  * a clearly-marked, guarded entry point (:func:`gate_submit` /
    :func:`execute_submit_phase_b`) that makes a REAL
    ``guard.classify_request(phase=SUBMIT)`` decision and REFUSES unless a valid
    disposable-env attestation AND per-flow approval are present AND the control
    is not an irreversible refuse-pack verb — the three refusal grounds are each
    proven by the unit tests and can never be bypassed;
  * on authorisation it RE-DRIVES the approved flow on the attested disposable
    env (navigate → optional re-fill → click submit), OBSERVES the grounded
    terminal outcome (``navigation`` when the URL changes, else a same-page
    ``confirmation``), and records it as ONE terminal ``page_state`` carrying the
    submit action + a baseline confirmation screenshot — the demonstrated
    outcome the qe-central writer maps to a page_visit + baseline visual_frame
    (the BEHAVES evidence).  A rejected/no-effect submit is recorded HONESTLY
    (``confirmed=False``); a confirmation is never fabricated on nothing.

Fill-anywhere is SAFE because containment is the method (the network guard), not
a hope about which control submits: no Phase-A fill can escape a mutating
request — the guard aborts every mutation outside AUTH/SUBMIT (§3.2 doctrine).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit

from . import emit
from .boundary import (RUNG_DIALOG, RUNG_NAVIGATION,
                       confirmation_transition, dom_digest)
from .browser import (
    OUTCOME_CONFIRMATION,
    OUTCOME_NAVIGATION,
    BrowserPort,
    classify_submit_after,
)
from . import field_semantics, field_signature, field_values
from .fill_engine import constraints as fe_constraints
from .fill_engine import generator as fe_generator
from .fill_engine import widgets as fe_widgets
from .fill_engine.driver import ControlFillDriver, read_page_alerts
from .fill_engine.repair import (RepairBudget, RepairOutcome, RetryPolicy,
                                 repair_loop)
from .fill_engine.validation import PageAlertFilter
from .identity_pack import Identity, derive as derive_identity
from .guard import GuardDecision, Phase, classify_request, registrable_domain
from .inventory import target_kind_for

logger = logging.getLogger(__name__)

#: Control kinds that hold a value and can be filled in Phase A.
#: ``slider``/``color`` (native ``input[type=range|color]``) are Playwright-
#: fillable with a numeric/hex value; a CUSTOM (non-input) slider is not filled
#: here — it stays honestly UNHANDLED (needs the keyboard set-range verb).
FILLABLE_KINDS = frozenset({"text", "date", "select", "checkbox", "radio",
                            "toggle", "slider", "color"})
#: Kinds that toggle a boolean/selected state (verb ``click``, value = state).
_TOGGLE_KINDS = frozenset({"checkbox", "radio", "toggle"})
_TRUTHY = frozenset({"true", "1", "yes", "on", "checked", "y", "selected"})
#: Explicitly NEGATIVE intent for a checkbox/toggle. Anything else means
#: "engage this control" — a synthesized value like "100" is an answer, not a
#: request to switch the control OFF.
_FALSY = frozenset({"false", "0", "no", "off", "unchecked", "n", ""})


@dataclass(frozen=True)
class AnswerKey:
    """Client-supplied test data (design §3.2 ``answer_key {exact, semantic,
    regex_rules}``).  Pure resolution by accessible name — no hard-coded values.

      * ``exact``       : {accessible-name → value} (highest confidence);
      * ``regex_rules`` : [{pattern, value}] matched against the name;
      * ``semantic``    : {keyword → value} substring match on the name.
    """

    exact: dict[str, str] = field(default_factory=dict)
    semantic: dict[str, str] = field(default_factory=dict)
    regex_rules: tuple[tuple["re.Pattern[str]", str], ...] = ()

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> "AnswerKey":
        """Build from the explore-request ``answer_key`` object (tolerant)."""
        if not payload:
            return cls()
        exact = {_norm(k): str(v) for k, v in (payload.get("exact") or {}).items()}
        semantic = {_norm(k): str(v) for k, v in (payload.get("semantic") or {}).items()}
        rules: list[tuple[re.Pattern[str], str]] = []
        for rule in payload.get("regex_rules") or ():
            pattern = str((rule or {}).get("pattern") or "").strip()
            value = str((rule or {}).get("value") or "")
            if not pattern:
                continue
            try:
                rules.append((re.compile(pattern, re.IGNORECASE), value))
            except re.error as exc:
                logger.warning("qec.forms.bad_answer_regex pattern=%s error=%s",
                               pattern[:80], str(exc)[:200])
        return cls(exact=exact, semantic=semantic, regex_rules=tuple(rules))

    def resolve(self, name: str) -> Optional[str]:
        """Resolve the test value for a control ``name``, or ``None`` (no guess)."""
        key = _norm(name)
        if not key:
            return None
        if key in self.exact:
            return self.exact[key]
        for pattern, value in self.regex_rules:
            if pattern.search(name or ""):
                return value
        for keyword, value in self.semantic.items():
            if keyword and keyword in key:
                return value
        return None


@dataclass
class FlowCandidate:
    """A terminal/submit control recorded but NOT clicked in Phase A.

    Carries the fail-closed guard danger flag so Phase-B (and the human
    approver) can see which candidates are irreversible before any submit.
    """

    name: str
    target_kind: str
    danger: bool
    danger_rule_id: str
    danger_severity: str
    control: dict[str, Any]


@dataclass
class FormFillResult:
    """Outcome of Phase A on one page state."""

    actions: list[emit.ActionRecord] = field(default_factory=list)
    flow_candidates: list[FlowCandidate] = field(default_factory=list)
    filled: int = 0
    #: Fields filled from a SYNTHESIZED default (no answer-key hit) — low-confidence,
    #: so the coverage report can flag them and ask for a real seed.
    inferred: int = 0
    inferred_fields: list[str] = field(default_factory=list)
    #: Fillable fields left EMPTY (no seed AND no safe default) — the exact set the
    #: coverage report names as "add seed for these to unlock more flows".
    unfilled_fields: list[str] = field(default_factory=list)
    #: R0 intent contracts — fills whose intent verification returned False.
    intent_unmet: int = 0
    #: Fills that COMMITTED, counted by control kind. "auto_filled" alone cannot
    #: say whether the five dropdowns on a page were answered or five text boxes
    #: were — and a dropdown is the widget class that keeps breaking, so the one
    #: number the gate most needs was the one it could not see.
    filled_by_kind: dict[str, int] = field(default_factory=dict)
    #: Portal-rendered choice widgets that were opened, picked, and then would
    #: NOT read their answer back. A distinct failure class from a fill that was
    #: refused: the answer may well be in the form while the evidence says it is
    #: not, which is the one direction this product must never report loosely.
    open_choice_unverified: int = 0
    #: T-FE-01 — fields the application REJECTED and the repair loop then got
    #: accepted.  The numerator of the repair-success rate; it was structurally
    #: zero before, because there was no way back from a rejection.
    repaired: int = 0
    #: Fields accepted on the FIRST commit.  Constraint-aware generation is
    #: supposed to make this the overwhelming majority — repair is the
    #: exception, not the path — so it is measured rather than asserted.
    first_pass: int = 0
    #: Fields the loop could not get accepted within its budget, and WHY, so an
    #: unrepairable field is a named finding rather than a silent gap.
    repair_failed: list[dict[str, Any]] = field(default_factory=list)
    #: T-FE-02 — page alerts held back as stale, consenting or informational.
    #: The direct measure of the false-positive class control-scoped validation
    #: removes: it used to be zero by construction, and every one of those
    #: alerts wrongly failed a field.
    alerts_suppressed: int = 0
    #: How many times the expensive verdict read actually ran.  The latency
    #: claim ("free on a clean fill") is a measurement, not a promise.
    verdict_reads: int = 0
    #: Widget classes met, and how many of each were ANSWERED — so a class that
    #: silently stops working is visible instead of merely absent.
    widgets_met: dict[str, int] = field(default_factory=dict)
    widgets_answered: dict[str, int] = field(default_factory=dict)
    #: PER-FIELD LEDGER — one entry for every fillable control the crawl met, filled
    #: or not: {name, signature, semantic_type, basis, provenance, filled, sensitive}.
    #: Never a value. This is what makes the residue ask specific ("give me these
    #: three") and what the learning loop is keyed on; without it a second crawl has
    #: no way to know it already asked.
    field_ledger: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_unanswered_decisions(self) -> bool:
        """The page has enumerable decision-point fields (GROUP_ASSEMBLE'd
        radios or selects with options) that the fill could not resolve.
        The advance engine should still fire so the crawler can discover
        what lies beyond the decision, or at minimum report honestly that
        the page is a decision gate — not a dead end."""
        return any(
            e.get("options") and not e.get("filled")
            for e in self.field_ledger
            if isinstance(e, dict)
        )


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _is_password(control: Mapping[str, Any]) -> bool:
    it = _norm(control.get("input_type")) or _norm((control.get("qec") or {}).get("input_type"))
    return it == "password"


def _truthy(value: str) -> bool:
    return _norm(value) in _TRUTHY


def _wants_checked(kind: str, value: str) -> bool:
    """Does this fill intend the control to end up SELECTED?

    A RADIO is only ever meaningfully selected — a crawl never deliberately
    un-picks one, and there is no other way to choose a product. Observed
    live: a product card resolved to the synthesized value "100", which is
    not in the truthy vocabulary, so the crawl asked Playwright to UNCHECK
    the card; nothing was ever selected and the whole quote funnel stayed
    shut. Checkboxes/toggles keep two-way intent, but only an explicitly
    negative word turns them off."""
    if kind == "radio":
        return True
    return _norm(value) not in _FALSY


def _wants_checked_control(control: Mapping[str, Any], kind: str, value: str) -> bool:
    """As above, but aware that the control belongs to a GROUP.

    A grouped checkbox is a MEMBER of one question, and a member is only ever
    filled when it IS the answer — so the fill always means "select this". The
    literal-word rule below it would read a member labelled "No" or "None" as a
    request to switch the control OFF, leaving the question unanswered while the
    ledger recorded an answer: the group's only negative option is exactly the
    one it would fail to select, and it is the option we prefer."""
    if str(control.get("group_id") or ""):
        return True
    return _wants_checked(kind, value)


#: Select placeholder options that are not real values (never chosen as a default).
_PLACEHOLDER_OPTIONS = frozenset({
    "", "select", "choose", "please select", "select one", "select an option",
    "--", "---", "-- select --", "none", "choose one", "pick one",
})

#: An exact-phrase list does not survive contact with real applications. They do
#: not write "select one" — they write "Select coverage amount...", "Choose your
#: state", "-- Select term length --". Those are the SAME thing: the option whose
#: underlying value is "", i.e. nothing chosen.
#:
#: Picking one satisfies the fill and then leaves the field EMPTY, so a
#: validation-gated form never enables its Continue and the crawl stalls on a page
#: it believes it filled. Observed live: Coverage Amount and Term Length were both
#: set to "Select …" and the quote funnel stopped dead at step 2.
#: ONE placeholder rule for the whole crawler, owned by field_values — the
#: lowest layer that has to choose an option. There were TWO lists; fixing
#: only this one still left value_for() picking "Select coverage amount...",
#: so the funnel stayed shut behind a field the ledger reported as filled.
_is_placeholder_option = field_values.is_placeholder_option


def _number_default(control: Mapping[str, Any]) -> str:
    """A number-input default that satisfies the control's OWN declared
    constraints — the DOM's ``min``/``max``/``step`` attributes, captured by the
    inventory. Grounded: nothing is invented; the app itself declared the range.

    A constraint-blind ``"1"`` in an ``<input type=number min="18">`` passes the
    fill but silently VOIDS the whole form submit via browser-native validation
    (live incident: the quote form's Age min=18 → submit outcome=none), so:
      * ``min`` declared → ``min`` (by the HTML spec, min is the step base, so
        it is always a valid value);
      * no min but ``max`` declared below 1 → ``max``;
      * otherwise → ``"1"`` (the old default, still right for unconstrained
        quantity-style fields).
    Values are emitted integer-formatted when whole so ``fill`` commits cleanly.
    """
    def _num(key: str) -> Optional[float]:
        rawv = str(control.get(key) or "").strip()
        if not rawv:
            return None
        try:
            return float(rawv)
        except ValueError:
            return None  # a non-numeric bound (e.g. a date min) is not ours to use

    def _fmt(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else str(x)

    minimum, maximum = _num("min"), _num("max")
    if minimum is not None:
        return _fmt(minimum)
    if maximum is not None and maximum < 1:
        return _fmt(maximum)
    return "1"


#: Controls whose value is a CHOICE among enumerable options — the forks that
#: decide which business path a funnel walks (Journey Graph decision points).
_ENUMERABLE_KINDS = frozenset({"select", "radio", "checkbox", "toggle"})
#: Bound the recorded enumeration (manifest size guard); inventory already
#: clips individual labels.
_MAX_RECORDED_OPTIONS = 24


def normalize_option(label: Any) -> str:
    """One normalization for every option/choice comparison: lowercased,
    whitespace-collapsed, bounded."""
    return re.sub(r"\s+", " ", str(label or "").strip().lower())[:80]


def _enumerable_options(control: Mapping[str, Any], kind: str) -> list[str]:
    """The normalized option labels a decision-point control offers.

    Binary controls (checkbox / toggle) enumerate their two states; select /
    radio enumerate their option labels (product UI text — never values)."""
    # A GROUPED checkbox answers a question the same way a radio does. "Which of
    # these have you been diagnosed with? - Diabetes / Heart disease / Cancer /
    # None of these" is ONE question offering four answers, and enumerating it as
    # ["checked", "unchecked"] describes a form the application does not have:
    # the catalogue then states that the question accepts two answers, neither of
    # which appears anywhere on the page. The override path already treats a
    # grouped checkbox by OPTION LABEL exactly like a radio (see the Rung 0 block
    # below), so this is the enumeration catching up with the answering.
    #
    # A LONE checkbox keeps its two states, because for it they ARE the answers:
    # "I consent to a medical records check" is checked or it is not.
    grouped = bool(str(control.get("group_id") or "")) and bool(
        control.get("group_options"))
    if kind in ("checkbox", "toggle") and not grouped:
        return ["checked", "unchecked"]
    # A radio's answers live on its GROUP, not on the element: a single
    # <input type=radio> has no option list of its own, so without the group it
    # enumerates as [] and the decision point vanishes from the graph.
    source = control.get("options")
    if grouped or (kind == "radio" and control.get("group_options")):
        source = control.get("group_options")
    out: list[str] = []
    for i, opt in enumerate((source or ())[:_MAX_RECORDED_OPTIONS]):
        # "Select coverage amount..." is the ABSENCE of a choice, not a business
        # path. Enumerating it makes the planner dispatch a walk that FORCES the
        # placeholder — Rung 0 bypasses the synthesizer, so the field is left
        # empty, the funnel cannot advance, and the branch is then recorded
        # `walked`: a proven-coverage claim for "nothing selected". Observed
        # live: three such phantom branches, all marked walked, while the quote
        # funnel stayed shut behind them.
        if _is_placeholder_option(opt, first=(i == 0)):
            continue
        label = normalize_option(opt)
        if label:
            out.append(label)
    return out


def _synthesize_default(control: Mapping[str, Any], kind: str, name: str) -> Optional[str]:
    """A structurally-VALID, LOW-CONFIDENCE value for a fillable control that the
    answer key does not cover — so a client-side-validation-gated form can be advanced
    toward validity WITHOUT a hand-authored seed. Grounded in the control's
    ``input_type`` / ``options`` / accessible name. Returns ``None`` when no safe
    default exists (the field is then left empty and named in the coverage report).

    NEVER produces a value for a password field (skipped by the caller) or a danger
    control (those are buttons, never fillable). A required checkbox is auto-checked
    only to clear a blocking gate (e.g. 'I agree'); a radio group / optional toggle is
    left to the human, since which option is a semantic choice."""
    itype = _norm(control.get("input_type"))
    n = _norm(name)

    if kind == "select":
        for i, opt in enumerate(control.get("options") or []):
            o = str(opt).strip()
            if o and not _is_placeholder_option(o, first=(i == 0)):
                return o
        return None
    if kind in _TOGGLE_KINDS:
        # A GROUPED member is never auto-checked on its own account: "required"
        # on one member of a multi-select is a statement about that box, and
        # honouring it per-member would check every required box in the group.
        # Which member answers the question is the group's decision, made above.
        if (kind != "radio" and bool(control.get("required"))
                and not str(control.get("group_id") or "")):
            return "true"
        return None

    # text / date value fields — input_type first, then accessible-name heuristics.
    if itype == "email" or "email" in n or "e-mail" in n:
        return "qa.autotest@example.com"
    # Temporal inputs: every native flavour needs ITS OWN value format — the
    # old blanket ISO-date default made Playwright's fill THROW on
    # input[type=time|month|week|datetime-local] ("Malformed value"), so those
    # fields always errored and were never advanced (requirements-audit R1
    # finding). All values derive from today's clock — valid on any app.
    if itype == "time":
        return "12:00"
    if itype == "month":
        return date.today().strftime("%Y-%m")
    if itype == "week":
        return f"{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]:02d}"
    if itype == "datetime-local":
        return f"{date.today().isoformat()}T12:00"
    if itype == "date" or kind == "date":
        return date.today().isoformat()
    if itype == "tel" or "phone" in n or "mobile" in n or "telephone" in n:
        return "5551234567"
    if itype == "number" or n in ("age", "quantity", "qty") or n.endswith(" age"):
        return _number_default(control)
    if itype == "url" or "website" in n or n.endswith(" url"):
        return "https://example.com"
    if "zip" in n or "postal" in n or "postcode" in n or "post code" in n:
        return "12345"
    if ("first" in n and "name" in n) or n == "fname":
        return "Test"
    if ("last" in n and "name" in n) or n == "lname" or "surname" in n:
        return "User"
    if "full name" in n or n == "name" or n.endswith(" name"):
        return "Test User"
    if "city" in n:
        return "Springfield"
    if n in ("state", "province"):
        return "California"
    if "address" in n or "street" in n:
        return "1 Test Street"
    if "company" in n or "organization" in n or "organisation" in n or "employer" in n:
        return "Autotest Inc"
    if "country" in n:
        return "United States"
    # Native range slider / color input — a structurally-VALID value (grounded
    # in the DOM's declared min/max), so the field commits instead of erroring.
    if kind == "slider" or itype == "range":
        return _slider_default(control)
    if kind == "color" or itype == "color":
        return "#1a2b3c"
    if itype in ("", "text", "search") or kind == "text":
        return "autotest"
    return None


def _slider_default(control: Mapping[str, Any]) -> str:
    """A valid range value: the MIDPOINT of the declared min/max (snapped to
    step when given), else min, else '50'. Grounded in the control's own
    attributes — never invents an out-of-range value."""
    def _num(key: str):
        try:
            return float(str(control.get(key) or "").strip())
        except ValueError:
            return None

    lo, hi, step = _num("min"), _num("max"), _num("step")
    if lo is not None and hi is not None and hi >= lo:
        mid = (lo + hi) / 2.0
        if step and step > 0:
            mid = lo + round((mid - lo) / step) * step
        return str(int(mid)) if float(mid).is_integer() else str(mid)
    if lo is not None:
        return str(int(lo)) if float(lo).is_integer() else str(lo)
    if hi is not None:
        return str(int(hi)) if float(hi).is_integer() else str(hi)
    return "50"


#: Where a filled value came from. Carried into evidence for EVERY field, because
#: a green result that rested on a value nobody confirmed is worth less than one
#: that rested on the client's own data — and a reader cannot tell the difference
#: unless we say so.
PROV_PROVIDED = "provided"        # the client's answer key — explicit, highest trust
PROV_JOURNEY = "journey"          # answered earlier in THIS crawl (see below)
PROV_RECALLED = "recalled"        # remembered from a previous crawl of THIS client
PROV_SYNTHESIZED = "synthesized"  # generated from the crawl's fictional identity
PROV_NEEDS_INPUT = "needs_input"  # nothing honest could be produced — ask the client
PROV_INTENT_UNMET = "intent_unmet"  # R0: fill attempted but intent verification failed
#: A radio-group member that is NOT the chosen answer. Not a gap and not a
#: failure — the question WAS answered, by its sibling — so it must never reach
#: the residue the client is asked to supply.
PROV_GROUP_SIBLING = "group_sibling"
PROV_PLANNED = "planned"          # a branch-walk plan forced this CHOICE (Journey
                                  # Graph C4) — evidence says exactly why the walk
                                  # took the path it took
#: Answered NOT from a value source but from the APPLICATION'S OWN VERDICT: the
#: fill declined this control, the app disabled its forward control, the agent
#: answered it, and the app then enabled that control. The provenance is the
#: strongest of the lot — the application itself confirmed the answer was the
#: one it was waiting for — and it is the only provenance that carries a
#: DISCOVERED BUSINESS RULE rather than a supplied or generated value.
PROV_UNBLOCK = "answered_to_unblock"

#: Returned as the ``mechanic`` when a portal-rendered choice was opened and
#: picked but would not read its answer back. Carried on the existing
#: (action, mechanic) channel so the caller can COUNT the class rather than
#: only log it — a metric the gate can hold at zero, which a log never was.
MECHANIC_OPEN_CHOICE_UNVERIFIED = "open_choice_unverified"


def resolve_field(control: Mapping[str, Any], kind: str, name: str,
                  answer_key: "AnswerKey", identity: Identity,
                  *, recalled: Optional[Mapping[str, str]] = None,
                  journey_values: Optional[Mapping[str, str]] = None,
                  priors: Optional[Mapping[str, Any]] = None,
                  data_mode: str = field_values.DATA_MODE_USER,
                  choice_overrides: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Decide what to type into one control, and record HOW it was decided.

    The order is fixed and fails toward asking rather than guessing:

      0. a branch-walk CHOICE OVERRIDE for this field's signature — the whole
         point of a planned walk is to take the option the default data would
         not, so it must outrank the answer key; it applies ONLY to enumerable
         kinds, ONLY when the forced option is among the control's own
         enumerated options (fail-closed — never free text, never a value
         injection), and it is declared as ``planned`` provenance;
      1. the client's answer key — an explicit instruction always wins;
      2. a value THIS journey already answered for this same field — a funnel
         that asks twice must be answered twice the same way, or the application
         rejects its own steps on cross-validation;
      3. a value this same client supplied for this same field before;
      4. a value generated for the semantic type the field was classified as;
      5. nothing — the field joins the residue the client is asked for.

    Rungs 4 and 5 are the difference between a crawl that stops at the first
    unfamiliar form and one that reaches the end of a funnel. Rung 1 staying
    above the learning is what stops it from ever overriding a client who told
    us the answer.

    EVERY rung stamps ``provenance``. That is what keeps autonomy and honesty
    separable: the crawl may answer as much as it honestly can, and a reader can
    still see exactly which values were the client's and which were invented, so
    a journey completed on synthesized data is never mistaken for one proven on
    the client's own."""
    sig = field_signature.compute(control, kind=kind)
    verdict = field_semantics.classify(sig, priors=priors)
    entry = {
        "name": name,
        "signature": sig["signature"],
        "semantic_type": verdict["type"],
        "basis": verdict["basis"],
        "confidence": verdict["confidence"],
        "sensitive": verdict["sensitive"],
        "filled": False,
        "provenance": PROV_NEEDS_INPUT,
    }
    # DECISION POINTS (Journey Graph C0): an enumerable control is a fork in
    # the business flow — which option is taken decides which path the funnel
    # walks. Record the enumeration (option labels — product UI text, never
    # user values) so the graph can name the branches nobody walked. The
    # ``choice`` is stamped by the caller only when the fill COMMITTED.
    if kind in _ENUMERABLE_KINDS:
        entry["options"] = _enumerable_options(control, kind)
        # The QUESTION this decision point belongs to. Every member of a radio
        # group reports the same group_id, which is what lets the fold record ONE
        # decision with N branches instead of an N×N cross-product, and what a
        # planned walk keys its choice override on.
        group_id = str(control.get("group_id") or "")
        if group_id:
            entry["group_id"] = group_id
        # THE QUESTION IN THE APPLICATION'S OWN WORDS (M2.1). ``name`` above is
        # this control's accessible name, and for a group member that names the
        # ANSWER — "Male", "Yes". The decision point built from this entry used
        # it as the question's label, so the catalogue called a gender question
        # "male" and a tobacco question "yes". The wording lives on the DOM's
        # declared question container and is captured verbatim; "" when the page
        # declared none, and the catalogue then says UNVERIFIED rather than
        # inventing one.
        question_label = str(control.get("question_label") or "").strip()
        if question_label:
            entry["question_label"] = question_label[:200]

    # Rung 0 — a planned branch walk forces WHICH enumerated option this
    # decision point takes. Fail-closed: enumerable kinds only, and the forced
    # option must be one the control itself offers (matched normalized, filled
    # with the control's ORIGINAL option text).
    if choice_overrides and kind in _ENUMERABLE_KINDS:
        # A radio GROUP is one question spread across N separate elements, so its
        # override is keyed by the group — four siblings share one question but
        # hash to four different signatures, and no single member's signature can
        # stand for the choice. The member that IS the forced option checks
        # itself; the browser unchecks the rest, which is why the others return
        # None (untouched) rather than a negative value.
        group_id = str(control.get("group_id") or "")
        forced = normalize_option(
            (group_id and choice_overrides.get(group_id))
            or choice_overrides.get(sig["signature"]) or "")
        if forced:
            # A grouped checkbox is overridden by OPTION LABEL like a radio;
            # only an ungrouped one takes a bare checked/unchecked state.
            if (kind in ("radio", "checkbox") and group_id
                    and forced not in ("checked", "unchecked")):
                if normalize_option(name) == forced:
                    entry.update(provenance=PROV_PLANNED, filled=True)
                    return {"value": name, "entry": entry}
                # A sibling of the forced option: leave it alone entirely. Falling
                # through to the answer key here would let a second member of the
                # same group get selected and silently overturn the planned walk.
                return {"value": None, "entry": entry}
            if kind in ("checkbox", "toggle"):
                if forced in ("checked", "unchecked"):
                    entry.update(provenance=PROV_PLANNED, filled=True)
                    return {"value": "true" if forced == "checked" else "false",
                            "entry": entry}
            else:
                for opt in control.get("options") or ():
                    if normalize_option(opt) == forced:
                        entry.update(provenance=PROV_PLANNED, filled=True)
                        return {"value": str(opt), "entry": entry}

    explicit = answer_key.resolve(name)
    if explicit is not None:
        entry.update(provenance=PROV_PROVIDED, filled=True)
        return {"value": explicit, "entry": entry}

    # Rung 1.5 — A VALUE THIS JOURNEY ALREADY DISCOVERED.
    #
    # A business journey asks the same thing more than once: an email on the
    # contact step and again on the confirmation step, a policy number captured on
    # one screen and required on the next. Re-deriving each sighting independently
    # produced a different answer each time, and an application that
    # cross-validates its own steps rejects that — so the funnel dead-ended on a
    # validation error the app was right to raise.
    #
    # Above `recalled` because THIS journey's answer is the current truth: a value
    # remembered from a crawl last month must not overwrite one this journey has
    # already committed two steps ago and is being validated against.
    if journey_values:
        seen_value = journey_values.get(sig["signature"])
        if seen_value not in (None, ""):
            entry.update(provenance=PROV_JOURNEY, filled=True)
            return {"value": str(seen_value), "entry": entry}

    if recalled:
        prior_value = recalled.get(sig["signature"])
        if prior_value not in (None, ""):
            entry.update(provenance=PROV_RECALLED, filled=True)
            return {"value": str(prior_value), "entry": entry}

    generated = field_values.value_for(verdict["type"], control, identity, kind=kind,
                                       data_mode=data_mode)
    if generated is None and not (kind in _TOGGLE_KINDS and kind == "radio"
                                  and data_mode != field_values.DATA_MODE_AGENT):
        # Last resort: the structural default ladder, which knows control shapes the
        # semantic vocabulary does not cover. Still synthesized, still declared.
        generated = _synthesize_default(control, kind, name)
    if generated is not None:
        # ONE QUESTION, ONE ANSWER. Every member of a radio group resolves to the
        # SAME chosen option, so filling each in turn checks them one after
        # another and the browser unchecks the previous — the LAST member wins,
        # whichever option was actually chosen. The form ends up validly
        # answered, and the ledger records the option we picked rather than the
        # one now selected: a recorded choice that contradicts the DOM, which is
        # the failure this product exists to prevent.
        #
        # Only the member that IS the answer is filled; its siblings are left
        # untouched (the browser owns exclusivity) and marked as belonging to an
        # answered group, so they never inflate the residue the client is asked
        # for. Mirrors the branch-walk override path, which already did this.
        group_id = str(control.get("group_id") or "")
        # Checkbox groups take the same rule as radios. The browser does NOT
        # enforce exclusivity here, so filling each member in turn would check
        # every box: on a health-conditions question that means answering "all
        # of them", inventing a medical history for a synthetic applicant out of
        # nothing but the order the fill happened to iterate in.
        if kind in ("radio", "checkbox") and group_id and _norm(name) != _norm(str(generated)):
            entry.update(provenance=PROV_GROUP_SIBLING, filled=False)
            return {"value": None, "entry": entry}
        entry.update(provenance=PROV_SYNTHESIZED, filled=True)
        return {"value": generated, "entry": entry}

    # Rung 4.5 — AN ENUMERATION WE CANNOT READ YET IS NOT AN UNANSWERABLE ONE.
    #
    # A modern component library (Radix, shadcn, MUI, Headless UI) builds its
    # listbox in a portal on open, so the inventory sees ``options: []`` and every
    # rung above correctly refuses to invent a value for an enumeration it cannot
    # read. The widget can still be ANSWERED — by opening it and taking a real
    # option — and the fill does exactly that, recording the label actually
    # committed. Deferring the choice to fill time is the opposite of inventing
    # one: the answer comes from the application, not from us.
    #
    # Agent mode only: in user mode a semantic choice is the client's to make,
    # and that contract is unchanged.
    if (kind in _ENUMERABLE_KINDS and not entry.get("options")
            and kind not in _TOGGLE_KINDS
            and data_mode == field_values.DATA_MODE_AGENT
            and _is_open_choice(control)):
        entry.update(provenance=PROV_SYNTHESIZED, filled=True)
        return {"value": CHOICE_OPEN_AND_PICK, "entry": entry}

    return {"value": None, "entry": entry}


async def fill_form_phase_a(
    port: BrowserPort,
    controls: Sequence[Mapping[str, Any]],
    answer_key: AnswerKey,
    clock: emit.MonotonicClock,
    *,
    phase: str = Phase.EXPLORE.value,
    state_id: str = "",
    identity: Optional[Identity] = None,
    recalled: Optional[Mapping[str, str]] = None,
    #: {signature: value} for values THIS journey has already committed. Read as
    #: resolver rung 2 and WRITTEN BACK as each fill commits, so a funnel that
    #: asks the same question on step 1 and step 4 answers it the same way both
    #: times. In-process for the life of one crawl; never emitted, never logged,
    #: never persisted — the same discipline as ``recalled``.
    journey_values: Optional[MutableMapping[str, str]] = None,
    priors: Optional[Mapping[str, Any]] = None,
    data_mode: str = field_values.DATA_MODE_USER,
    choice_overrides: Optional[Mapping[str, str]] = None,
    #: T-FE-01 — how many COMMITS one field may take, the first one included.
    #: Bounded on purpose: an unbounded loop against an application that rejects
    #: everything is a denial of service aimed at our own crawl.  Three means one
    #: generation and at most two evidence-driven repairs, which is enough for
    #: every constraint an application states honestly.
    repair_budget: RepairBudget = RepairBudget(),
    #: Gate 1 / T-RE-01 — how many times the SAME value may be re-issued after a
    #: TRANSIENT page race (a detached element, an in-flight navigation, a settle
    #: timeout).  A separate budget from ``repair_budget`` because it answers a
    #: different question: that one bounds how many DIFFERENT values an
    #: application may reject, this one bounds how long we outlast a re-render.
    retry_policy: RetryPolicy = RetryPolicy(),
) -> FormFillResult:
    """Phase A: fill fillable controls from ``answer_key``, read back, STOP.

    Only controls with (a) a resolvable answer-key value AND (b) a groundable
    accessible name are filled — a nameless or answer-less control is skipped
    honestly (never fabricated).  Password fields are never filled here (auth
    owns them).  Terminal buttons become :class:`FlowCandidate`\\ s and are left
    unclicked — the submit boundary is the whole point.
    """
    result = FormFillResult()
    # One coherent person for the whole crawl. Regenerating per field would give a
    # form whose postcode belongs to a different state than its region — internally
    # inconsistent in exactly the way an application validates.
    identity = identity or derive_identity(state_id or "qec")
    # And one coherent HOUSEHOLD around that person, so a field about a spouse,
    # a beneficiary, a child or an employer is answered from the right member
    # rather than from the applicant.
    persona = field_values.persona_for(identity)

    # T-FE-02 — EVERY ALERT ALREADY ON THE PAGE IS STALE FOR EVERY FILL BELOW.
    #
    # Snapshotting here, once, is the whole mechanism.  A cookie banner, a
    # session notice or an error left over from the previous step is in this set
    # by construction, so it can never be read as a verdict on a value that had
    # not been typed when it appeared.  The old page-wide read could not express
    # "already there", which is why one real failure was reported as ten and a
    # consent banner failed every fill on the page.
    alerts = PageAlertFilter(await read_page_alerts(port))

    for control in controls:
        kind = _norm(control.get("kind"))
        name = str(control.get("name") or "")
        if kind == "button":
            result.flow_candidates.append(FlowCandidate(
                name=name,
                target_kind=target_kind_for(control),
                danger=bool(control.get("danger")),
                danger_rule_id=str(control.get("danger_rule_id") or ""),
                danger_severity=str(control.get("danger_severity") or ""),
                control=dict(control),
            ))
            continue
        if _is_file(control) and not control.get("disabled"):
            # Document-upload field (#8) — attach a generic seed file and RECORD the
            # attach as a grounded 'upload' action.  The substrate carries the
            # 'upload' verb (schema.ACTION_VERBS) and the factory generates+compiles
            # a real setInputFiles step from it (founder-approved rung).
            if not _norm(name):
                continue
            upload_action = await _upload_seed(port, control, clock, phase=phase, state_id=state_id)
            if upload_action is not None:
                result.actions.append(upload_action)
                result.filled += 1
                result.inferred += 1
                result.inferred_fields.append(name)
            else:
                result.unfilled_fields.append(name)
            continue
        if kind not in FILLABLE_KINDS or _is_password(control) or control.get("disabled"):
            continue
        if not _norm(name):
            logger.info("qec.forms.skip_nameless_field kind=%s", kind)
            continue
        decision = resolve_field(control, kind, name, answer_key, identity,
                                 recalled=recalled, journey_values=journey_values,
                                 priors=priors, data_mode=data_mode,
                                 choice_overrides=choice_overrides)
        entry, value = decision["entry"], decision["value"]

        # WHICH WIDGET THIS IS.  Counted whether or not it is answered, so a
        # class that quietly stops working shows up as a coverage hole rather
        # than as an absence nobody can see.
        widget = fe_widgets.classify_widget(control, kind=kind)
        entry["widget"] = widget.name
        result.widgets_met[widget.name] = result.widgets_met.get(widget.name, 0) + 1

        if value is None:
            # A radio-group SIBLING is not a gap: its question was answered by
            # the member that IS the answer, and the browser owns exclusivity.
            # Counting it as unfilled would put a value we already chose into the
            # residue the client is asked to supply — asking someone for an
            # answer we have.
            if entry.get("provenance") != PROV_GROUP_SIBLING:
                result.unfilled_fields.append(name)
            result.field_ledger.append(entry)
            continue

        # ── T-FE-01: GENERATE → FILL → READ THE VERDICT → REPAIR ─────────
        outcome = await _fill_with_repair(
            port, control, kind, value, clock, phase=phase, state_id=state_id,
            alerts=alerts, persona=persona, entry=entry, result=result,
            data_mode=data_mode, repair_budget=repair_budget,
            retry_policy=retry_policy)
        action, mechanic = outcome.action, outcome.mechanic

        if action is None:
            entry.update(filled=False, provenance=PROV_INTENT_UNMET)
            result.unfilled_fields.append(name)
            result.intent_unmet += 1
            # A CHOICE WIDGET THAT WOULD NOT CONFIRM ITS OWN ANSWER is its own
            # failure class, and it was only ever a log line — invisible to the
            # gate, so the fix that took it from 6 to 0 could regress unnoticed.
            if mechanic == MECHANIC_OPEN_CHOICE_UNVERIFIED:
                result.open_choice_unverified += 1
            result.field_ledger.append(entry)
            continue
        if mechanic:
            entry["mechanic"] = mechanic
        value = outcome.value if outcome.value is not None else value
        result.widgets_answered[widget.name] = (
            result.widgets_answered.get(widget.name, 0) + 1)
        # WHAT THE FORM ACTUALLY HOLDS, not what we asked it to hold. The two
        # differ whenever the widget decided: an open-and-pick choice is resolved
        # from the options the widget itself offered, so the requested value can
        # be a sentinel that means "take a real one". Recording the request would
        # put a value in the ledger that the form never contained.
        committed = value
        if action.value is not None and str(action.value).strip():
            committed = str(action.value)
        # Decision point DECIDED: record WHICH option this committed fill took
        # (the branch walked; every other recorded option is a branch that was
        # not). Binary states normalize to their two enumerated labels.
        if "options" in entry:
            if kind in ("checkbox", "toggle"):
                entry["choice"] = ("checked"
                                   if _norm(str(committed)) in ("true", "1", "yes",
                                                                "on", "checked")
                                   else "unchecked")
            else:
                entry["choice"] = normalize_option(committed)
                # The enumeration the widget revealed when it was opened — the
                # options the static inventory could not see. Without this the
                # catalogue keeps an empty answer set for exactly the questions
                # whose answers were hardest to get.
                if not entry.get("options") and entry["choice"]:
                    entry["options"] = [entry["choice"]]
        result.actions.append(action)
        result.filled += 1
        result.filled_by_kind[kind] = result.filled_by_kind.get(kind, 0) + 1
        result.field_ledger.append(entry)
        # REMEMBER IT FOR THE REST OF THE JOURNEY. Only a value the browser
        # actually took is worth remembering (a failed fill is recorded above and
        # never reaches here), and only a free-text one: re-imposing a CHOICE
        # would silently overrule the branch walk, whose whole purpose is to take
        # a different option on a later pass through the same question.
        if (journey_values is not None and "options" not in entry
                and not entry.get("sensitive")):
            sig = str(entry.get("signature") or "")
            if sig:
                journey_values.setdefault(sig, str(committed))
        if entry["provenance"] != PROV_PROVIDED:
            result.inferred += 1
            result.inferred_fields.append(name)

    result.alerts_suppressed = alerts.suppressed
    logger.info("qec.forms.phase_a filled=%d inferred=%d intent_unmet=%d flow_candidates=%d dangerous=%d unfilled=%d",
                result.filled, result.inferred, result.intent_unmet, len(result.flow_candidates),
                sum(1 for f in result.flow_candidates if f.danger), len(result.unfilled_fields))
    logger.info(
        "qec.forms.repair first_pass=%d repaired=%d unrepairable=%d "
        "alerts_suppressed=%d verdict_reads=%d widgets=%s",
        result.first_pass, result.repaired, len(result.repair_failed),
        result.alerts_suppressed, result.verdict_reads,
        result.widgets_answered)
    return result


@dataclass
class _FillOutcome:
    """What one control's fill-and-repair produced, for the caller's ledger."""

    action: Optional[emit.ActionRecord]
    mechanic: str = ""
    value: Optional[str] = None


async def _fill_with_repair(
    port: BrowserPort,
    control: Mapping[str, Any],
    kind: str,
    value: str,
    clock: emit.MonotonicClock,
    *,
    phase: str,
    state_id: str,
    alerts: PageAlertFilter,
    persona: Any,
    entry: dict[str, Any],
    result: FormFillResult,
    data_mode: str,
    repair_budget: RepairBudget,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> _FillOutcome:
    """Commit one value, read the application's verdict, and repair on evidence.

    THE ARROW THAT DID NOT EXIST.  The old path called :func:`_fill_one` once: a
    value the application rejected ended the field, the field ended the page, and
    the crawl reported the number of fields it had ATTEMPTED — which is the
    metric that made the whole thing look like it was working.

    Two rules govern every retry here, and they are what make this a repair loop
    rather than a retry loop:

      * a retry must be CAUSED by an observed, control-anchored rejection — no
        signal, no retry;
      * a retry must CHANGE something that rejection named, and say so.

    Both live in :func:`app.fill_engine.repair.repair_loop`; this function
    supplies the two things that loop cannot have — a way to commit through the
    existing mechanic ladder, and a way to ask the generator for a better value
    under the constraints the application has just tightened.

    WHOSE VALUE MAY BE REPLACED.  Only a SYNTHESIZED one.  A value the client
    supplied in their answer key, remembered from a previous crawl, or already
    committed earlier in this journey is not ours to overwrite: if the
    application rejects it, that rejection IS the finding, and quietly
    substituting a generated value would hide the one thing worth reporting.
    Those provenances therefore regenerate to nothing, and the loop stops with
    the rejection recorded against the value that actually failed.
    """
    holder: dict[str, Any] = {"action": None, "mechanic": ""}

    async def _commit(ctl: Mapping[str, Any], candidate: str):
        action, mechanic = await _fill_one(port, ctl, kind, candidate, clock,
                                           phase=phase, state_id=state_id)
        holder["action"], holder["mechanic"] = action, mechanic
        if action is None:
            return None, False, (mechanic or "fill_refused")
        committed = (str(action.value) if action.value is not None else None)
        return committed, True, ""

    driver = ControlFillDriver(port, _commit, alerts)
    cons = fe_constraints.extract(control, kind=kind)

    provenance = str(entry.get("provenance") or "")
    repairable = provenance == PROV_SYNTHESIZED
    semantic = str(entry.get("semantic_type") or "")

    def _regenerate(tightened: fe_constraints.Constraints,
                    refused: "frozenset[str]") -> Optional[str]:
        # Still refuses, and for the same reason: a value that did not come from
        # the generator must never be replaced by one that did.  The difference
        # is that the loop is now TOLD this up front (``repairable=`` below), so
        # it can stop with a reason that names the real gate instead of
        # reporting a constraint search that never happened.
        if not repairable:
            return None
        candidate = fe_generator.generate(
            semantic, control, persona, kind=kind,
            name=str(control.get("name") or ""), cons=tightened,
            answer_choices=(_norm(data_mode) == field_values.DATA_MODE_AGENT))
        if candidate.value is None or candidate.value in refused:
            return None
        entry["repair_rationale"] = candidate.rationale[:300]
        return candidate.value

    outcome: RepairOutcome = await repair_loop(
        driver, control, first_value=value, cons=cons,
        regenerate=_regenerate, budget=repair_budget,
        # ── Gate 1 / T-RE-01+02 ─────────────────────────────────────────────
        # ``repairable`` was previously visible to the loop ONLY as a regenerate
        # callback that returned None, which the loop could not tell apart from
        # a generator that had genuinely run out of candidates.
        #
        # ``retry`` applies to EVERY field regardless of that flag. A transient
        # page race is not a fact about the value, so outlasting one is not a
        # repair — which is why a Security PIN, unrepairable by construction, is
        # nonetheless retried when the form re-renders mid-fill. That single
        # abandoned attempt is the defect this closes.
        repairable=repairable,
        retry=retry_policy,
        first_reason=str(entry.get("rationale")
                         or "the value the generator produced for this field"))

    result.verdict_reads += driver.verdict_reads
    if len(outcome.attempts) > 1 or not outcome.accepted:
        # Only recorded when something actually happened, so the ledger of a
        # clean page stays the size it always was.
        entry["repair"] = outcome.as_dict()

    if outcome.accepted:
        if outcome.first_pass:
            result.first_pass += 1
        else:
            result.repaired += 1
            logger.info(
                "qec.forms.repaired control=%r attempts=%d — %s",
                str(control.get("name") or "")[:40], len(outcome.attempts),
                outcome.explanation()[:300])
        return _FillOutcome(holder["action"], holder["mechanic"], outcome.value)

    # NOT ACCEPTED.  Say which field, how hard we tried, and why we stopped —
    # an unrepairable field is a finding the operator can act on, and a silent
    # one is the failure mode this whole milestone exists to remove.
    result.repair_failed.append({
        "name": str(control.get("name") or "")[:120],
        "attempts": len(outcome.attempts),
        "stop_reason": outcome.stop_reason,
        "explanation": outcome.explanation()[:400],
    })
    logger.warning(
        "qec.forms.unrepairable control=%r attempts=%d stop=%s — %s",
        str(control.get("name") or "")[:40], len(outcome.attempts),
        outcome.stop_reason, outcome.explanation()[:300])
    # A MECHANICAL refusal keeps its old meaning exactly: the widget would not
    # take the value, which the caller records as intent_unmet.  A value the
    # APPLICATION rejected is different in kind, and the ledger says so.
    if outcome.stop_reason.startswith("widget_refused") or not outcome.attempts:
        return _FillOutcome(None, holder["mechanic"])
    return _FillOutcome(None, holder["mechanic"])


def _is_file(control: Mapping[str, Any]) -> bool:
    """True for a file-input control (``<input type=file>``)."""
    it = _norm(control.get("input_type")) or _norm((control.get("qec") or {}).get("input_type"))
    return it == "file"


_SEED_FILE_CACHE: dict[str, str] = {}


def _default_seed_file() -> Optional[str]:
    """Path to a small generic seed document, created once, for Phase-A uploads so a
    document-upload step advances instead of being skipped. The content is a
    placeholder (never a real customer document); a client seed would override it."""
    path = _SEED_FILE_CACHE.get("doc")
    if path and os.path.exists(path):
        return path
    try:
        fd, path = tempfile.mkstemp(prefix="qec-seed-", suffix=".pdf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"%PDF-1.4\n%% QE-Central Phase-A seed document (placeholder)\n")
        _SEED_FILE_CACHE["doc"] = path
        return path
    except Exception as exc:  # a filesystem hiccup must never break a crawl
        logger.warning("qec.forms.seed_file_failed error=%s", str(exc)[:200])
        return None


async def _upload_seed(
    port: BrowserPort, control: Mapping[str, Any], clock: emit.MonotonicClock,
    *, phase: str, state_id: str,
) -> Optional[emit.ActionRecord]:
    """Attach a seed document to a file input and RECORD it as an ``upload`` action
    (#8).  The seed is a placeholder (never a real customer document); a client
    seed would override it.  The attached filename rides as the action's committed
    value — grounded evidence of WHAT was uploaded.  Returns the action, or ``None``
    when the port has no upload verb, the seed can't be created, or the attach
    errored (an honest skip — never a fabricated step).

    The substrate carries ``upload`` (schema.ACTION_VERBS) and the factory now
    both generates an "Attach" step from it and compiles a fail-closed
    ``setInputFiles`` (founder-approved compiler rung)."""
    setter = getattr(port, "set_input_files", None)
    seed = _default_seed_file()
    if setter is None or not seed:
        return None
    observation = await setter(dict(control), [seed])
    if (observation.error_detail or "").strip():
        return None
    return emit.build_action_record(
        dict(control), verb="upload", value=observation.committed_value,
        observation=observation, phase=phase, state_id=state_id,
        timestamp_ms=clock.now_ms(),
    )


#: Sentinel value for an enumerable control whose options are NOT in the DOM
#: until the widget is opened (Radix / shadcn / MUI / Headless UI — every modern
#: component library builds its listbox in a portal on open). The resolver cannot
#: choose from an enumeration it cannot read, but the FILL can: open the widget,
#: take a real option from it, and record the one actually committed.
CHOICE_OPEN_AND_PICK = "\x00qec:open-and-pick"


def _is_open_choice(control: Mapping[str, Any]) -> bool:
    """A choice widget whose options exist only while it is open.

    Requires an EXPLICIT non-``select`` tag — a Radix/shadcn trigger is a
    ``<button>``, so the tag is the positive evidence that the browser's
    ``selectOption`` primitive cannot drive this control. A control with no tag
    recorded is NOT assumed custom: treating unknown as custom routed ordinary
    selects into the open-pick path and broke fills that had always worked
    (caught by the existing decision-point tests). Unknown keeps the native path;
    only a declared custom widget is opened.
    """
    tag = _norm(control.get("tag"))
    return bool(tag) and tag != "select"


#: Settle budget for a portal widget's close+re-render. A Radix/shadcn listbox
#: ANIMATES closed (~150ms) and React re-renders asynchronously, so a read taken
#: the instant after the click still sees the open popup and reads back as a
#: failure. Live: six selections were correctly opened, read and picked — "Claim
#: Type" → "Death Claim" — and every one was discarded as unverified because the
#: verification ran before the DOM had caught up. Bounded and cheap: the first
#: read usually succeeds, and only a genuinely slow widget pays.
_OPEN_CHOICE_SETTLE_TRIES = 4
_OPEN_CHOICE_SETTLE_S = 0.2


def _reads_back_as(
    controls: Sequence[Mapping[str, Any]], control: Mapping[str, Any], label: str,
) -> bool:
    """Did the widget actually COMMIT ``label``? Verified from a fresh read.

    Two conditions, both required and both fail-closed:
      * the popup CLOSED (no ``role=option`` remains) — a still-open listbox
        means the click landed on nothing;
      * the widget now DISPLAYS the chosen label.

    The display check is ANCHORED to the same control where the DOM lets us
    (``testid`` / ``css_hint`` survive the re-render), and accepts CONTAINMENT of
    the label in its accessible name: a Radix trigger's name becomes the
    selection, but component libraries variously render it as "Death Claim" or
    "Claim Type Death Claim", and an equality test rejects the second. Anchored
    containment is safe precisely because it is anchored — an unanchored
    containment test would match the label appearing anywhere on the page.

    Without this a "fill" would be a click we hoped worked, and the field ledger
    would claim a choice the form never took — the form then fails validation and
    the crawl blames the app.
    """
    want = normalize_option(label)
    if not want:
        return False
    for c in controls:
        if str(c.get("role") or "").strip().lower() == "option":
            return False                      # still open ⇒ nothing committed

    testid = str(control.get("testid") or "").strip()
    css = str(control.get("css_hint") or "").strip()
    original = normalize_option(control.get("name") or "")
    for c in controls:
        committed = normalize_option(c.get("value_committed") or "")
        name = normalize_option(c.get("name") or "")
        anchored = bool(
            (testid and str(c.get("testid") or "").strip() == testid)
            or (css and str(c.get("css_hint") or "").strip() == css)
        )
        if anchored and (committed == want or want in name):
            return True
        # The SAME trigger, still wearing its own label with the selection
        # appended ("Gender" → "Gender Male"). Anchored by that original label,
        # so it cannot match some other control that merely contains the word —
        # which an unanchored containment test would.
        if (original and name != original and name.startswith(original)
                and want in name):
            return True
        if committed == want or name == want:
            return True
    return False


async def _fill_open_choice(
    port: BrowserPort,
    control: Mapping[str, Any],
    value: str,
    clock: emit.MonotonicClock,
    *,
    phase: str,
    state_id: str,
) -> tuple[Optional[emit.ActionRecord], str]:
    """Fill a choice widget by OPENING it, picking a real option, and verifying.

    THE DEFECT THIS CLOSES. A shadcn/Radix ``<Select>`` renders as a button whose
    options live in a portal that does not exist until it is opened, so the
    inventory captured ``options: []`` and the resolver — correctly refusing to
    invent a value for an enumeration it could not read — left the field empty.
    Live on the Summit Life application wizard: six of seven step-1 fields were
    filled, ``Gender`` was not, the page's own ``canAdvance()`` requires it, so
    ``Continue`` rendered ``disabled`` and the walk skipped it. Every downstream
    number followed: ``advances_by_tier: {}``, one step deep, no journey.

    The walk was honest and the resolver was honest; the gap was that nobody
    ever opened the widget. Fleet-wide payoff: this shape is the default in every
    modern component library, so the same gap exists on most React apps built in
    the last three years.

    Fails CLOSED at every step — an unopenable widget, an empty listbox, or a
    selection that does not read back returns ``None``, which the caller records
    as ``intent_unmet``. A field the crawl could not fill is a finding; a field
    it claims to have filled and did not is a lie that fails later, elsewhere.
    """
    collect = getattr(port, "collect_controls", None)
    if collect is None:
        return None, ""
    press = getattr(port, "press_key", None)

    async def _dismiss() -> None:
        if press is not None:
            try:
                await press("Escape")
            except Exception:
                pass

    try:
        await port.click(dict(control))
        revealed = await collect()
    except Exception:
        await _dismiss()
        return None, ""

    options = [
        r for r in (revealed or ())
        if str(r.get("role") or "").strip().lower() == "option"
        and str(r.get("name") or "").strip()
        and not r.get("disabled")
    ]
    if not options:
        await _dismiss()
        return None, ""

    # Honour a requested option when the widget offers it; otherwise take the
    # first real one. Either way the RECORDED value is the label committed, never
    # the request — the ledger must say what the form actually holds.
    want = "" if value == CHOICE_OPEN_AND_PICK else normalize_option(value)
    chosen = next(
        (o for o in options if normalize_option(o.get("name")) == want), None
    ) or options[0]
    label = str(chosen.get("name") or "").strip()

    try:
        observation = await port.click(dict(chosen))
    except Exception:
        await _dismiss()
        return None, ""

    # LET THE WIDGET SETTLE. The popup animates closed and React re-renders
    # asynchronously, so the first read can still show the open listbox — which
    # reads back as failure for a selection that in fact succeeded.
    verified = False
    after: Sequence[Mapping[str, Any]] = ()
    for attempt in range(_OPEN_CHOICE_SETTLE_TRIES):
        try:
            after = await collect() or ()
        except Exception:
            break
        if _reads_back_as(after, control, label):
            verified = True
            break
        if attempt + 1 < _OPEN_CHOICE_SETTLE_TRIES:
            await asyncio.sleep(_OPEN_CHOICE_SETTLE_S)

    if not verified:
        await _dismiss()
        # Say WHICH half failed — a still-open popup and a committed-but-unread
        # value need opposite fixes, and collapsing them into "unverified" cost a
        # whole crawl to tell apart.
        still_open = any(
            str(c.get("role") or "").strip().lower() == "option" for c in after)
        logger.info(
            "qec.forms.open_choice_unverified control=%r picked=%r "
            "still_open=%s tries=%d",
            str(control.get("name") or "")[:40], label[:40], still_open,
            _OPEN_CHOICE_SETTLE_TRIES)
        return None, MECHANIC_OPEN_CHOICE_UNVERIFIED

    return emit.build_action_record(
        dict(control), verb="select", value=label, observation=observation,
        phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
    ), "open_pick"


async def _fill_one(
    port: BrowserPort,
    control: Mapping[str, Any],
    kind: str,
    value: str,
    clock: emit.MonotonicClock,
    *,
    phase: str,
    state_id: str,
) -> tuple[Optional[emit.ActionRecord], str]:
    """Perform ONE fill, read the committed value back, build the action record.

    Returns ``(action, mechanic_used)`` — the second element is the R1 ladder
    rung variant that R0-verified (e.g. ``click_element``), or ``""`` when the
    native mechanic succeeded or no ladder was involved.
    """
    control = dict(control)
    if kind in _TOGGLE_KINDS:
        observation = await port.set_checked(
            control, _wants_checked_control(control, kind, value))
        if observation.intent_met is False:
            return None, ""
        recorded = observation.committed_value
        return emit.build_action_record(
            control, verb="click", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        ), observation.mechanic_used
    if kind == "select":
        # A widget whose options live in a portal (Radix/shadcn/MUI) cannot be
        # filled by the browser's select primitive — it is not a <select>. Open
        # it, take a real option, verify the read-back.
        if _is_open_choice(control):
            return await _fill_open_choice(
                port, control, value, clock, phase=phase, state_id=state_id)
        if value == CHOICE_OPEN_AND_PICK:
            return None, ""     # sentinel is meaningless to a native select
        observation = await port.select_option(control, value)
        if observation.intent_met is False:
            return None, ""
        recorded = observation.committed_value if observation.committed_value is not None else value
        return emit.build_action_record(
            control, verb="select", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        ), observation.mechanic_used
    if kind in ("slider", "color"):
        if _norm(control.get("tag")) != "input":
            return None, ""
        observation = await port.fill(control, value)
        if observation.intent_met is False:
            return None, ""
        recorded = observation.committed_value if observation.committed_value is not None else value
        if recorded is None:
            return None, ""
        return emit.build_action_record(
            control, verb="type", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        ), observation.mechanic_used
    # text / date
    observation = await port.fill(control, value)
    if observation.intent_met is False:
        return None, ""
    recorded = observation.committed_value if observation.committed_value is not None else value
    return emit.build_action_record(
        control, verb="type", value=recorded, observation=observation,
        phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
    ), observation.mechanic_used


# ─── Phase B — the guarded submit entry point (Phase-5 scope) ────────────────


#: Terminal outcomes that constitute a positive, BEHAVES-worthy confirmation
#: (design §3.4 tier labeler: navigation OR a demonstrated same-page success).
CONFIRMED_OUTCOMES = frozenset({OUTCOME_NAVIGATION, OUTCOME_CONFIRMATION})

#: Honest non-submit reasons stamped on ``SubmitResult.reason`` (submitted=False).
REASON_REFUSED = "guard_refused"
REASON_FORM_UNREACHABLE = "form_unreachable"


@dataclass
class SubmitResult:
    """Outcome of a Phase-B submit attempt — honest whether or not it ran.

    ``submitted`` records whether the guarded submit control was actually
    clicked; ``decision`` is the always-present guard verdict.  ``confirmed`` is
    True ONLY when a positive terminal outcome (``navigation`` or
    ``confirmation``) was OBSERVED — it is the single flag a caller reads to
    decide BEHAVES-certification, and it is never set on a refusal, an
    unreachable form, or an ``error``/``none`` outcome.  ``action`` is the
    grounded terminal submit action; ``page_state`` is the emitted terminal
    confirmation state; ``baseline`` is its captured confirmation screenshot ref
    (the demonstrated-outcome evidence, ``None`` when capture failed honestly).
    """

    submitted: bool
    decision: GuardDecision
    confirmed: bool = False
    outcome: str = ""
    reason: str = ""
    action: Optional[emit.ActionRecord] = None
    page_state: Optional[emit.PageStateRecord] = None
    baseline: Optional[emit.ScreenshotRecord] = None
    #: A4.3 / T-AC-03 — everything OBSERVED around the one irreversible click,
    #: captured adjacent to it rather than reconstructed by the caller. A
    #: crossing whose evidence is assembled two layers up describes the page the
    #: caller was standing on, not the page the click happened on: this path
    #: RE-NAVIGATES and RE-FILLS before clicking, so those are different pages.
    #: Empty on a refusal (nothing happened, so there is nothing to observe).
    crossing: dict[str, Any] = field(default_factory=dict)


def gate_submit(
    control: Mapping[str, Any],
    url: str,
    *,
    refuse_pack: Any,
    is_login_domain: bool = False,
    attestation: Any = None,
    submit_flow_approved: bool = False,
    now_ms: Optional[int] = None,
) -> GuardDecision:
    """The REAL SUBMIT-phase guard decision for a flow-candidate (pure).

    Delegates verbatim to ``guard.classify_request(method='POST',
    phase=SUBMIT, …)`` so a submit is authorised ONLY on a valid disposable-env
    attestation AND per-flow approval AND a non-irreversible verb.  This is the
    single choke point Phase B (Phase-5) must pass through — never bypassed.
    """
    return classify_request(
        "POST", url, Phase.SUBMIT, refuse_pack, is_login_domain,
        str(control.get("name") or ""),
        attestation=attestation, submit_flow_approved=submit_flow_approved,
        now_ms=now_ms,
    )


async def execute_submit_phase_b(
    port: BrowserPort,
    control: Mapping[str, Any],
    url: str,
    emitter: emit.ManifestEmitter,
    clock: emit.MonotonicClock,
    *,
    refuse_pack: Any,
    is_login_domain: bool = False,
    attestation: Any = None,
    submit_flow_approved: bool = False,
    now_ms: Optional[int] = None,
    state_id: str = "",
    sequence_index: int = 0,
    answer_key: Optional[AnswerKey] = None,
    fill_controls: Sequence[Mapping[str, Any]] = (),
    renavigate: bool = True,
    navigate: Optional[Callable[[str], Any]] = None,
) -> SubmitResult:
    """Phase-5 submit entry point: REFUSE unless attestation + approval permit,
    else re-drive the approved flow, submit, and capture the confirmation.

    The guard is consulted for real (:func:`gate_submit`).  On refusal the
    attempt is recorded as a ``guard_event`` and NO click happens — the app is
    never mutated (the three refusal grounds — no attestation, no per-flow
    approval, and an irreversible verb even when attested+approved — are each
    proven by the unit tests and can never be bypassed).

    On authorisation (a genuinely attested, approved, non-irreversible flow) the
    approved flow is RE-DRIVEN on the attested disposable env: navigate to the
    form ``url``, optionally re-fill it from ``answer_key`` + ``fill_controls``
    (so the submit acts on a populated form), click ``control``, and OBSERVE the
    effect.  The grounded terminal outcome — ``navigation`` (the URL changed) or
    ``confirmation`` (a same-page success region/dialog) — plus a confirmation
    screenshot is recorded as ONE terminal ``page_state`` carrying the submit
    action + a baseline visual_frame, which the qe-central writer maps to a
    page_visit + baseline (the BEHAVES evidence).  ``confirmed`` is set ONLY on a
    positive terminal outcome; an ``error``/``none`` outcome is recorded
    HONESTLY with ``confirmed=False`` — a failed submit is never green-washed
    into a confirmation.

    ``sequence_index`` is the monotonic page-visit index the caller assigns to
    the terminal state (the caller owns the crawl-wide counter).
    :class:`BrowserPort` stays injectable so the whole path is unit-testable
    with a fake browser.
    """
    now = clock.now_ms() if now_ms is None else now_ms
    decision = gate_submit(
        control, url, refuse_pack=refuse_pack, is_login_domain=is_login_domain,
        attestation=attestation, submit_flow_approved=submit_flow_approved, now_ms=now,
    )
    if not decision.allow:
        emitter.emit_guard_event(
            kind=decision.event_kind or "blocked_method", method="POST", url=url,
            rule_id=decision.rule_id, severity=decision.severity,
            reason=decision.reason, phase=Phase.SUBMIT.value,
        )
        logger.info("qec.forms.submit_refused rule_id=%s", decision.rule_id)
        return SubmitResult(submitted=False, decision=decision, reason=REASON_REFUSED)

    # ── Authorised: re-drive the approved flow on the attested disposable env ─
    first_seen = clock.now_ms()
    # renavigate=True (a single-page form): go back to the form URL and re-fill so
    # the submit acts on a populated form. renavigate=False (a wizard TERMINAL, e.g.
    # a quote summary reached by walking start→coverage→personal→…): the page state
    # was BUILT UP by the walk and lives in the SPA context — re-navigating to the
    # summary URL would discard the in-progress quote (and with it the very button
    # we mean to click), so we submit IN PLACE on the page the walk already reached.
    if renavigate:
        # ``navigate`` (when injected) is the caller's login-keeping renavigation:
        # a raw goto logs an app with client-side sessions OUT, so the submit fired
        # into the sign-in wall it landed on. The injected navigator owns its own
        # failure handling; the raw-goto default keeps today's unreachable check.
        if navigate is not None:
            await navigate(url)
        else:
            nav = await port.goto(url)
            if not getattr(nav, "ok", True):
                logger.warning("qec.forms.submit_form_unreachable url=%s", (url or "")[:200])
                return SubmitResult(submitted=False, decision=decision,
                                    reason=REASON_FORM_UNREACHABLE)
        if answer_key is not None and fill_controls:
            # Re-establish the approved form state.  Fills are client-side until the
            # submit POST, which the guard has already authorised for this flow.
            await fill_form_phase_a(
                port, fill_controls, answer_key, clock,
                phase=Phase.SUBMIT.value, state_id=state_id,
            )

    # ── Instrument the BEFORE side, immediately adjacent to the click ────────
    # Adjacent, because this function may have re-navigated and re-filled since
    # the caller last looked at the page. Everything here is best-effort: a port
    # that cannot answer yields an empty reading, which shows up as a weaker
    # milestone, never as a crash and never as a fabricated one.
    before = await _capture_crossing_side(port)
    clicked_at_ms = clock.now_ms()
    shot_before = await _capture_baseline(port, emitter, clock, first_seen)

    # ── Submit + observe the grounded terminal outcome ───────────────────────
    observation = await port.click(control)
    after = await _capture_crossing_side(port)
    observed_at_ms = clock.now_ms()

    # THE MISSING HALF OF classify_submit_after.  Its ``confirmation`` branch
    # reads ``obs.confirmation_detail``, which no adapter has ever written — so
    # a same-page success was unclassifiable and every in-place submit scored
    # ``dom_changed``/``confirmed=False`` forever. The detail is derived here as
    # a TRANSITION (text that appeared, never text that was merely present), so
    # a page that says "you will receive a confirmation" before anything happens
    # still cannot green-wash itself.
    confirm_detail, confirm_rung = confirmation_transition(
        before["texts"], after["texts"],
        aria_before=before["status"], aria_after=after["status"],
        # M1.4 — a button is an offer, not a statement. The far side's own
        # "Print Confirmation" / "Confirm Order" label carries the success
        # vocabulary and declares nothing; the banner beside it is the
        # declaration. Same guard the walk applies, from the inventory this
        # helper already collects.
        control_names=after.get("names") or (),
    )
    if confirm_detail and not (observation.confirmation_detail or "").strip():
        observation = replace(observation, confirmation_detail=confirm_detail)
    outcome = classify_submit_after(observation)
    if outcome.outcome == OUTCOME_NAVIGATION:
        confirm_rung = RUNG_NAVIGATION
    elif outcome.outcome == OUTCOME_CONFIRMATION and not confirm_rung:
        # A dialog-borne confirmation: classify_submit_after reached it through
        # ``dialog_opened``, which the text diff cannot see.
        confirm_rung = RUNG_DIALOG if observation.dialog_opened else ""
    submit_action = emit.build_action_record(
        dict(control), verb="submit", value=None, observation=observation,
        phase=Phase.SUBMIT.value, state_id=state_id, timestamp_ms=clock.now_ms(),
        after_outcome=outcome,
    )

    # ── Capture the confirmation baseline + emit the terminal page_state ─────
    confirmation_url = _confirmation_url(observation, url)
    baseline = await _capture_baseline(port, emitter, clock, first_seen)
    if baseline is not None:
        submit_action.screenshot_after = baseline.path
    submit_action.to_state = _terminal_fingerprint(confirmation_url)

    # The POST-SUBMIT page is exactly where an OUTCOME renders (a computed
    # premium, a decline, a confirmation number). Capture its displayed-value
    # nodes so the value oracle can ground a confirmed expected outcome to a real
    # post-submit node — WITHOUT this, submit-depth reaches the page but the value
    # is invisible. Best-effort (a port without the verb yields nothing).
    displayed_values: list[dict[str, Any]] = []
    _collect_dv = getattr(port, "collect_displayed_values", None)
    if _collect_dv is not None:
        try:
            raw_dv = list(await _collect_dv() or [])
            # normalize + PII-scrub + #2 candidate-classify via the SAME pipeline the
            # crawler uses.  M0.3/T-DE-06: this helper moved to state_identity,
            # so both modules import it DOWNWARD and the old crawler<->forms
            # cycle (previously dodged by importing lazily) no longer exists.
            from .state_identity import _displayed_values as _normalize_displayed_values
            displayed_values = _normalize_displayed_values(raw_dv)
        except Exception:  # never fail a submit over a best-effort capture
            logger.warning("qec.forms.submit_displayed_values_failed", exc_info=True)

    last_seen = clock.now_ms()
    page_state = _build_terminal_state(
        sequence_index=sequence_index, url=confirmation_url,
        actions=[submit_action], baseline=baseline,
        first_seen_ms=first_seen, last_seen_ms=last_seen,
        displayed_values=displayed_values,
    )
    emitter.emit_page_state(page_state)

    confirmed = outcome.outcome in CONFIRMED_OUTCOMES
    logger.info(
        "qec.forms.submit_executed rule_id=%s outcome=%s confirmed=%s baseline=%s",
        decision.rule_id, outcome.outcome, confirmed, baseline is not None,
    )
    return SubmitResult(
        submitted=True, decision=decision, confirmed=confirmed,
        outcome=outcome.outcome, action=submit_action, page_state=page_state,
        baseline=baseline,
        crossing={
            "url_before": str(getattr(observation, "url_before", "") or url),
            "url_after": confirmation_url,
            "navigated": bool(outcome.navigated),
            "outcome": outcome.outcome,
            "confirmation_detail": confirm_detail or (
                observation.confirmation_detail or ""),
            "confirmation_rung": confirm_rung,
            "dom_digest_before": before["digest"],
            "dom_digest_after": after["digest"],
            "screenshot_before": (shot_before.path if shot_before is not None else ""),
            "screenshot_after": (baseline.path if baseline is not None else ""),
            "guard_rule_id": getattr(decision, "rule_id", "") or "",
            "clicked_at_ms": int(clicked_at_ms),
            "observed_at_ms": int(observed_at_ms),
            "outcome_values": list(displayed_values or []),
            "error_detail": (observation.error_detail or "")[:300],
        },
    )


async def _capture_crossing_side(port: BrowserPort) -> dict[str, Any]:
    """One side (before or after) of a boundary crossing, as evidence.

    BEST-EFFORT BY CONSTRUCTION.  Three independent optional port verbs, each
    wrapped separately: a port that implements none of them (every scripted
    fake written before this milestone, and any future adapter) yields empty
    readings and the crossing still runs, still records, and still reports the
    navigation rung.  A capture failure must never be able to block or crash a
    submit that the operator explicitly approved.
    """
    controls: list[dict[str, Any]] = []
    try:
        controls = list(await port.collect_controls() or [])
    except Exception:
        logger.warning("qec.forms.crossing_controls_failed", exc_info=True)
    side = await capture_page_declarations(port)
    return {"digest": dom_digest(controls),
            "names": [str(c.get("name") or "") for c in controls], **side}


async def capture_page_declarations(port: BrowserPort) -> dict[str, list[str]]:
    """What the page SAYS: its declared status regions and its visible text.

    Split out of :func:`_capture_crossing_side` for M1.4. The wizard walk needs
    exactly these two readings on both sides of every advance click, and it does
    NOT need the third — ``collect_controls`` is the expensive verb, its only
    use here is the DOM digest, and the walk re-observes the page in full one
    line later anyway. Calling the crossing helper wholesale would have added two
    redundant full control collections to EVERY step of EVERY walk.

    Best-effort by construction, exactly as before: each optional verb is wrapped
    separately, and a port that implements neither yields empty readings rather
    than raising into a state machine.
    """
    status: list[str] = []
    texts: list[str] = []
    getter = getattr(port, "status_texts", None)
    if getter is not None:
        try:
            status = [str(t) for t in (await getter() or [])]
        except Exception:
            logger.warning("qec.forms.crossing_status_failed", exc_info=True)
    getter = getattr(port, "visible_texts", None)
    if getter is not None:
        try:
            texts = [str(t) for t in (await getter() or [])]
        except Exception:
            logger.warning("qec.forms.crossing_texts_failed", exc_info=True)
    return {"status": status, "texts": texts}


# ─── Phase-B terminal-state helpers (mirror crawler._record_state shape) ─────


def _confirmation_url(observation: Any, form_url: str) -> str:
    """The URL of the terminal confirmation state.

    A navigation submit lands on a new URL (``url_after``); a same-page
    confirmation keeps the form URL.  Falls back to the form URL so the terminal
    ``page_state.location`` is always a valid http(s) URL (schema-required).
    """
    after = str(getattr(observation, "url_after", "") or "").strip()
    before = str(getattr(observation, "url_before", "") or "").strip()
    if after and after != before:
        return after
    return before or form_url


async def _capture_baseline(
    port: BrowserPort,
    emitter: emit.ManifestEmitter,
    clock: emit.MonotonicClock,
    first_seen_ms: int,
) -> Optional[emit.ScreenshotRecord]:
    """Capture + stage the confirmation baseline screenshot (best-effort).

    A submit whose confirmation cannot be screenshotted is recorded WITHOUT a
    baseline (an honest gap the caller sees as ``baseline is None``), never a
    fabricated frame — the terminal outcome still stands on the action's grounded
    ``after`` bundle.
    """
    try:
        png = await port.screenshot_png()
    except Exception as exc:  # a broken capture is an honest gap, not a crash
        logger.warning("qec.forms.baseline_capture_failed error=%s", str(exc)[:200])
        return None
    if not png:
        logger.info("qec.forms.baseline_empty — terminal state carries no baseline")
        return None
    ts = max(int(first_seen_ms), clock.now_ms())
    try:
        return emitter.store_screenshot(png, ts)
    except ValueError:
        logger.warning("qec.forms.baseline_store_rejected — empty screenshot bytes")
        return None


def _build_terminal_state(
    *,
    sequence_index: int,
    url: str,
    actions: Sequence[emit.ActionRecord],
    baseline: Optional[emit.ScreenshotRecord],
    first_seen_ms: int,
    last_seen_ms: int,
    displayed_values: Sequence[dict[str, Any]] = (),
) -> emit.PageStateRecord:
    """Assemble the terminal confirmation ``page_state`` (design §3.2).

    Mirrors :meth:`app.crawler.Crawler._record_state`: url parts split from
    ``location``, monotonic ``subaction_index`` re-numbering, and the baseline
    screenshot timestamp clamped inside ``[first_seen_ms, last_seen_ms]`` (the
    factory's frame-window join requires it — schema
    ``screenshot_outside_visit_window`` rule).  ``state_id`` / ``ax_fingerprint``
    are manifest-only routing keys the qe-central mapper ignores.
    """
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    first = max(0, int(first_seen_ms))
    last = max(first, int(last_seen_ms))
    terminal_fp = _terminal_fingerprint(url)

    ordered_actions: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        action.subaction_index = index
        action.state_id = terminal_fp
        ordered_actions.append(asdict(action))

    shot_records: list[dict[str, Any]] = []
    if baseline is not None:
        clamped = min(max(int(baseline.timestamp_ms), first), last)
        shot_records.append({"frame_index": baseline.frame_index,
                             "timestamp_ms": clamped, "path": baseline.path})

    return emit.PageStateRecord(
        sequence_index=int(sequence_index),
        location=(url or "")[:2000],
        first_seen_ms=first,
        last_seen_ms=last,
        url_host=host[:500],
        url_path=(parts.path or "")[:2000],
        url_query=(parts.query or "")[:2000],
        canonical_host=(registrable_domain(host) or host)[:500],
        displayed_values=list(displayed_values or []),
        actions=ordered_actions,
        screenshots=shot_records,
        state_id=terminal_fp,
        ax_fingerprint=terminal_fp,
    )


def _terminal_fingerprint(url: str) -> str:
    """A stable manifest-only routing id for the terminal confirmation state.

    The qe-central mapper ignores ``state_id`` / ``ax_fingerprint`` (they route
    the crawl graph, not the substrate), so a deterministic hash of the
    confirmation URL is sufficient and keeps re-runs stable.
    """
    digest = hashlib.sha256(("submit:" + (url or "")).encode("utf-8")).hexdigest()
    return "submit_" + digest[:16]
