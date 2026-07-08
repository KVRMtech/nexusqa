"""QE-Central Contained Explorer — the TWO-PHASE FORM controller (design §3.2).

Phase A (this phase — fully implemented):
  * fill every fillable control for which the answer key supplies test data,
  * READ BACK the committed value from the live control (the recorded value is
    what the field actually holds, never what we intended to type),
  * STOP BEFORE SUBMIT — the terminal/submit controls are recorded as
    flow-candidates (with their fail-closed guard danger flag) and never clicked.

Phase B (submit — Phase-5 scope):
  * a clearly-marked, guarded entry point (:func:`gate_submit` /
    :func:`execute_submit_phase_b`) that makes a REAL
    ``guard.classify_request(phase=SUBMIT)`` decision and REFUSES unless a valid
    disposable-env attestation AND per-flow approval are present AND the control
    is not an irreversible refuse-pack verb.  It is not a stub: with no
    attestation it refuses via the same guard the whole system trusts.

Fill-anywhere is SAFE because containment is the method (the network guard), not
a hope about which control submits: no Phase-A fill can escape a mutating
request — the guard aborts every mutation outside AUTH/SUBMIT (§3.2 doctrine).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import emit
from .browser import BrowserPort
from .guard import GuardDecision, Phase, classify_request
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


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _is_password(control: Mapping[str, Any]) -> bool:
    it = _norm(control.get("input_type")) or _norm((control.get("qec") or {}).get("input_type"))
    return it == "password"


def _truthy(value: str) -> bool:
    return _norm(value) in _TRUTHY


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
        if kind not in FILLABLE_KINDS or _is_password(control) or control.get("disabled"):
            continue
        if not _norm(name):
            logger.info("qec.forms.skip_nameless_field kind=%s", kind)
            continue
        value = answer_key.resolve(name)
        if value is None:
            continue

        action = await _fill_one(port, control, kind, value, clock, phase=phase,
                                 state_id=state_id)
        if action is not None:
            result.actions.append(action)
            result.filled += 1

    logger.info("qec.forms.phase_a filled=%d flow_candidates=%d dangerous=%d",
                result.filled, len(result.flow_candidates),
                sum(1 for f in result.flow_candidates if f.danger))
    return result


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


@dataclass
class SubmitResult:
    """Outcome of a Phase-B submit attempt — honest whether or not it ran."""

    submitted: bool
    decision: GuardDecision
    action: Optional[emit.ActionRecord] = None


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
) -> SubmitResult:
    """Phase-5 submit entry point: REFUSE unless attestation + approval permit.

    The guard is consulted for real (:func:`gate_submit`).  On refusal the
    attempt is recorded as a ``guard_event`` and NO click happens — the app is
    never mutated.  On authorisation (a genuinely attested, approved,
    non-irreversible flow) the submit is driven and recorded with its grounded
    outcome.  In Phase 1 no attestation is ever supplied, so this always
    refuses; the behaviour is proven by the unit tests, not assumed.
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
        return SubmitResult(submitted=False, decision=decision)

    observation = await port.click(control)
    action = emit.build_action_record(
        dict(control), verb="submit", value=None, observation=observation,
        phase=Phase.SUBMIT.value, state_id=state_id, timestamp_ms=clock.now_ms(),
    )
    logger.info("qec.forms.submit_executed rule_id=%s", decision.rule_id)
    return SubmitResult(submitted=True, decision=decision, action=action)
