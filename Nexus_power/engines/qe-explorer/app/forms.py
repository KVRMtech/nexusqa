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

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from . import emit
from .browser import (
    OUTCOME_CONFIRMATION,
    OUTCOME_NAVIGATION,
    BrowserPort,
    classify_submit_after,
)
from . import field_semantics, field_signature, field_values
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
_PLACEHOLDER_LEAD_VERBS = ("select", "choose", "pick")


def _is_placeholder_option(label: Any, *, first: bool) -> bool:
    """Is this the "nothing chosen yet" entry rather than a real answer?

    Deliberately conservative, because a false positive silently discards a
    legitimate answer: the leading-verb rule applies ONLY to the first option
    (where placeholders conventionally live), so a genuine product called
    "Choose Life Term 20" further down the list is still selectable.
    """
    text = _norm(label)
    if not text or text in _PLACEHOLDER_OPTIONS:
        return True
    stripped = text.strip("-–—_ .·:…")
    if not stripped:
        return True                       # "--", "…", separators
    if text.endswith(("...", "…")) and stripped.split()[0] in _PLACEHOLDER_LEAD_VERBS:
        return True                       # "Select coverage amount..."
    if first and stripped.split()[0] in _PLACEHOLDER_LEAD_VERBS:
        return True                       # "Select a state", "-- Choose term --"
    return False


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
    if kind in ("checkbox", "toggle"):
        return ["checked", "unchecked"]
    # A radio's answers live on its GROUP, not on the element: a single
    # <input type=radio> has no option list of its own, so without the group it
    # enumerates as [] and the decision point vanishes from the graph.
    source = control.get("options")
    if kind == "radio" and control.get("group_options"):
        source = control.get("group_options")
    out: list[str] = []
    for opt in (source or ())[:_MAX_RECORDED_OPTIONS]:
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
        if kind != "radio" and bool(control.get("required")):
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
PROV_RECALLED = "recalled"        # remembered from a previous crawl of THIS client
PROV_SYNTHESIZED = "synthesized"  # generated from the crawl's fictional identity
PROV_NEEDS_INPUT = "needs_input"  # nothing honest could be produced — ask the client
PROV_INTENT_UNMET = "intent_unmet"  # R0: fill attempted but intent verification failed
PROV_PLANNED = "planned"          # a branch-walk plan forced this CHOICE (Journey
                                  # Graph C4) — evidence says exactly why the walk
                                  # took the path it took


def resolve_field(control: Mapping[str, Any], kind: str, name: str,
                  answer_key: "AnswerKey", identity: Identity,
                  *, recalled: Optional[Mapping[str, str]] = None,
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
      2. a value this same client supplied for this same field before;
      3. a value generated for the semantic type the field was classified as;
      4. nothing — the field joins the residue the client is asked for.

    Rungs 3 and 4 are the difference between a crawl that stops at the first
    unfamiliar form and one that reaches the end of a funnel. Rung 1 staying
    above the learning is what stops it from ever overriding a client who told
    us the answer."""
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
            if kind == "radio" and group_id:
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
        entry.update(provenance=PROV_SYNTHESIZED, filled=True)
        return {"value": generated, "entry": entry}

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
    priors: Optional[Mapping[str, Any]] = None,
    data_mode: str = field_values.DATA_MODE_USER,
    choice_overrides: Optional[Mapping[str, str]] = None,
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
                                 recalled=recalled, priors=priors,
                                 data_mode=data_mode,
                                 choice_overrides=choice_overrides)
        entry, value = decision["entry"], decision["value"]
        if value is None:
            result.unfilled_fields.append(name)
            result.field_ledger.append(entry)
            continue

        action, mechanic = await _fill_one(port, control, kind, value, clock,
                                             phase=phase, state_id=state_id)
        if action is None:
            entry.update(filled=False, provenance=PROV_INTENT_UNMET)
            result.unfilled_fields.append(name)
            result.intent_unmet += 1
            result.field_ledger.append(entry)
            continue
        if mechanic:
            entry["mechanic"] = mechanic
        # Decision point DECIDED: record WHICH option this committed fill took
        # (the branch walked; every other recorded option is a branch that was
        # not). Binary states normalize to their two enumerated labels.
        if "options" in entry:
            if kind in ("checkbox", "toggle"):
                entry["choice"] = ("checked"
                                   if _norm(str(value)) in ("true", "1", "yes",
                                                            "on", "checked")
                                   else "unchecked")
            else:
                entry["choice"] = normalize_option(value)
        result.actions.append(action)
        result.filled += 1
        result.field_ledger.append(entry)
        if entry["provenance"] != PROV_PROVIDED:
            result.inferred += 1
            result.inferred_fields.append(name)

    logger.info("qec.forms.phase_a filled=%d inferred=%d intent_unmet=%d flow_candidates=%d dangerous=%d unfilled=%d",
                result.filled, result.inferred, result.intent_unmet, len(result.flow_candidates),
                sum(1 for f in result.flow_candidates if f.danger), len(result.unfilled_fields))
    return result


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
        observation = await port.set_checked(control, _wants_checked(kind, value))
        if observation.intent_met is False:
            return None, ""
        recorded = observation.committed_value
        return emit.build_action_record(
            control, verb="click", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        ), observation.mechanic_used
    if kind == "select":
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

    # ── Submit + observe the grounded terminal outcome ───────────────────────
    observation = await port.click(control)
    outcome = classify_submit_after(observation)
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
            # crawler uses (lazy import: forms is imported BY crawler — avoid the cycle).
            from .crawler import _displayed_values as _normalize_displayed_values
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
    )


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
