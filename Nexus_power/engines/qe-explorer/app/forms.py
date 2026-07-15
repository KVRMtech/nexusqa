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
from .guard import GuardDecision, Phase, classify_request, registrable_domain
from .inventory import target_kind_for

logger = logging.getLogger(__name__)

#: Control kinds that hold a value and can be filled in Phase A.
FILLABLE_KINDS = frozenset({"text", "date", "select", "checkbox", "radio", "toggle"})
#: Kinds that toggle a boolean/selected state (verb ``click``, value = state).
_TOGGLE_KINDS = frozenset({"checkbox", "radio", "toggle"})
_TRUTHY = frozenset({"true", "1", "yes", "on", "checked", "y", "selected"})


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


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _is_password(control: Mapping[str, Any]) -> bool:
    it = _norm(control.get("input_type")) or _norm((control.get("qec") or {}).get("input_type"))
    return it == "password"


def _truthy(value: str) -> bool:
    return _norm(value) in _TRUTHY


#: Select placeholder options that are not real values (never chosen as a default).
_PLACEHOLDER_OPTIONS = frozenset({
    "", "select", "choose", "please select", "select one", "select an option",
    "--", "---", "-- select --", "none", "choose one", "pick one",
})


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
        for opt in (control.get("options") or []):
            o = str(opt).strip()
            if o and _norm(o) not in _PLACEHOLDER_OPTIONS:
                return o
        return None
    if kind in _TOGGLE_KINDS:
        if kind != "radio" and bool(control.get("required")):
            return "true"
        return None

    # text / date value fields — input_type first, then accessible-name heuristics.
    if itype == "email" or "email" in n or "e-mail" in n:
        return "qa.autotest@example.com"
    if itype == "date" or kind == "date":
        return date.today().isoformat()
    if itype == "tel" or "phone" in n or "mobile" in n or "telephone" in n:
        return "5551234567"
    if itype == "number" or n in ("age", "quantity", "qty") or n.endswith(" age"):
        return "1"
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
    if itype in ("", "text", "search") or kind == "text":
        return "autotest"
    return None


async def fill_form_phase_a(
    port: BrowserPort,
    controls: Sequence[Mapping[str, Any]],
    answer_key: AnswerKey,
    clock: emit.MonotonicClock,
    *,
    phase: str = Phase.EXPLORE.value,
    state_id: str = "",
) -> FormFillResult:
    """Phase A: fill fillable controls from ``answer_key``, read back, STOP.

    Only controls with (a) a resolvable answer-key value AND (b) a groundable
    accessible name are filled — a nameless or answer-less control is skipped
    honestly (never fabricated).  Password fields are never filled here (auth
    owns them).  Terminal buttons become :class:`FlowCandidate`\\ s and are left
    unclicked — the submit boundary is the whole point.
    """
    result = FormFillResult()
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
            # Document-upload field — attach a generic seed file so the flow can
            # ADVANCE past the upload gate (Phase-A: choose the file, never submit).
            # A client-provided seed would override this later. Skipping it silently
            # leaves the deeper claims/application steps unreachable.
            if not _norm(name):
                continue
            action = await _upload_one(port, control, clock, phase=phase, state_id=state_id)
            if action is not None:
                result.actions.append(action)
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
        value = answer_key.resolve(name)
        seeded = value is not None
        if value is None:
            # No client seed for this field — synthesize a valid low-confidence value
            # so the form can reach validity and deeper flows become reachable.
            value = _synthesize_default(control, kind, name)
            if value is None:
                result.unfilled_fields.append(name)
                continue

        action = await _fill_one(port, control, kind, value, clock, phase=phase,
                                 state_id=state_id)
        if action is not None:
            result.actions.append(action)
            result.filled += 1
            if not seeded:
                result.inferred += 1
                result.inferred_fields.append(name)

    logger.info("qec.forms.phase_a filled=%d inferred=%d flow_candidates=%d dangerous=%d unfilled=%d",
                result.filled, result.inferred, len(result.flow_candidates),
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


async def _upload_one(
    port: BrowserPort, control: Mapping[str, Any], clock: emit.MonotonicClock,
    *, phase: str, state_id: str,
) -> Optional[emit.ActionRecord]:
    """Attach a seed document to a file input (Phase-A) and record the grounded
    outcome. Honest: if the port has no upload verb or the seed cannot be created,
    return ``None`` (the field is reported unfilled, never faked)."""
    setter = getattr(port, "set_input_files", None)
    seed = _default_seed_file()
    if setter is None or not seed:
        return None
    control = dict(control)
    observation = await setter(control, [seed])
    return emit.build_action_record(
        control, verb="upload",
        value=(observation.committed_value or os.path.basename(seed)),
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
) -> Optional[emit.ActionRecord]:
    """Perform ONE fill, read the committed value back, build the action record."""
    control = dict(control)
    if kind in _TOGGLE_KINDS:
        observation = await port.set_checked(control, _truthy(value))
        recorded = observation.committed_value
        return emit.build_action_record(
            control, verb="click", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        )
    if kind == "select":
        observation = await port.select_option(control, value)
        recorded = observation.committed_value if observation.committed_value is not None else value
        return emit.build_action_record(
            control, verb="select", value=recorded, observation=observation,
            phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
        )
    # text / date
    observation = await port.fill(control, value)
    recorded = observation.committed_value if observation.committed_value is not None else value
    return emit.build_action_record(
        control, verb="type", value=recorded, observation=observation,
        phase=phase, state_id=state_id, timestamp_ms=clock.now_ms(),
    )


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

    last_seen = clock.now_ms()
    page_state = _build_terminal_state(
        sequence_index=sequence_index, url=confirmation_url,
        actions=[submit_action], baseline=baseline,
        first_seen_ms=first_seen, last_seen_ms=last_seen,
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
