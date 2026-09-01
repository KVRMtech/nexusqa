"""Pure-logic tests for FORMS (two-phase) + AUTH matching/verification.

Covers (design §7 item 5 / §3.2):
  * answer-key resolution (exact / regex / semantic / no-guess);
  * Phase A fills fillable controls, reads back the committed value, and STOPS
    before submit — terminal buttons are recorded as flow-candidates (with the
    fail-closed guard danger flag), never clicked;
  * PII scrubbed at SOURCE (an SSN answer never lands raw in the action value);
  * Phase B REFUSES without attestation via a REAL guard.classify_request
    SUBMIT decision (three independent refusal grounds); allows only with
    attestation + approval + a non-irreversible verb;
  * login-control matching by accessible name, grounded login verification, and
    the AUTH-window accounting.

NO browser: a scripted fake port serves the fill/click methods.
"""
from __future__ import annotations

import asyncio
import tempfile

from app import emit
from app.auth import (
    AuthWindow,
    Credentials,
    match_login_controls,
    verify_login_success,
)
from app.browser import RawObservation
from app.config import Settings
from app.forms import AnswerKey, execute_submit_phase_b, fill_form_phase_a, gate_submit
from app.guard import Attestation, GuardRule, load_refuse_pack
from app.inventory import build_inventory

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


def _raw(role, name, **over):
    base = {"role": role, "name": name, "name_source": "label-for", "best_effort": False,
            "kind": role, "tag": over.pop("tag", role), "input_type": "", "options": [],
            "required": False, "disabled": False, "frame_selector": "", "testid": "",
            "css_hint": "", "value_committed": "", "landmark": {"role": "", "name": ""}}
    base.update(over)
    return base


class FakeFormPort:
    """Serves only the methods Phase A / Phase B exercise."""

    def __init__(self) -> None:
        self.clicked: list[str] = []

    async def fill(self, control, value):
        return RawObservation(url_before="u", url_after="u", committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before="u", url_after="u", committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before="u", url_after="u",
                              committed_value="true" if checked else "false")

    async def click(self, control):
        self.clicked.append(str(control.get("name")))
        return RawObservation(url_before="u", url_after="u2")


# ─── AnswerKey resolution ───────────────────────────────────────────────────────


def test_answer_key_exact_regex_semantic_and_no_guess():
    key = AnswerKey.from_payload({
        "exact": {"Amount": "250.00"},
        "semantic": {"account": "Savings"},
        "regex_rules": [{"pattern": r"^Date\b", "value": "2026-01-01"}],
    })
    assert key.resolve("Amount") == "250.00"          # exact
    assert key.resolve("Date of birth") == "2026-01-01"  # regex
    assert key.resolve("To account") == "Savings"     # semantic substring
    assert key.resolve("Unmapped field") is None      # no guess


# ─── Phase A: fill + read-back + stop-before-submit + danger flags ──────────────


def _form_controls():
    raw = [
        _raw("textbox", "Amount", kind="textbox", tag="input", input_type="text"),
        _raw("textbox", "SSN", kind="textbox", tag="input", input_type="text"),
        _raw("combobox", "Account", kind="combobox", tag="select",
             options=["Checking", "Savings"]),
        _raw("checkbox", "Agree to terms", kind="checkbox", tag="input", input_type="checkbox"),
        _raw("button", "Continue", kind="button", tag="button"),
        _raw("button", "Delete policy", kind="button", tag="button"),  # irreversible
    ]
    return build_inventory(raw, _REFUSE_PACK, url="https://app.example/apply")


def test_phase_a_fills_reads_back_and_stops_before_submit():
    controls = _form_controls()
    key = AnswerKey.from_payload({"exact": {
        "Amount": "250.00", "SSN": "123-45-6789",
        "Account": "Savings", "Agree to terms": "true",
    }})
    port = FakeFormPort()
    result = asyncio.run(fill_form_phase_a(port, controls, key, emit.MonotonicClock()))

    # Filled the three value fields (Amount, Account, Agree) + SSN = 4.
    assert result.filled == 4
    # NEVER clicked a submit/terminal button in Phase A.
    assert port.clicked == []

    by_label = {a.target_label: a for a in result.actions}
    assert by_label["Amount"].verb == "type" and by_label["Amount"].value == "250.00"
    assert by_label["Account"].verb == "select" and by_label["Account"].value == "Savings"
    assert by_label["Agree to terms"].verb == "click"  # toggle → click verb
    # read-back committed value drives the after-outcome.
    assert by_label["Amount"].after["outcome"] == "value_committed"


def test_phase_a_scrubs_pii_at_source():
    controls = _form_controls()
    key = AnswerKey.from_payload({"exact": {"SSN": "123-45-6789"}})
    result = asyncio.run(fill_form_phase_a(FakeFormPort(), controls, key,
                                           emit.MonotonicClock()))
    ssn_action = next(a for a in result.actions if a.target_label == "SSN")
    assert "123-45-6789" not in (ssn_action.value or "")
    assert "[REDACTED:ssn]" == ssn_action.value


def test_phase_a_flow_candidates_carry_guard_danger_flag():
    controls = _form_controls()
    result = asyncio.run(fill_form_phase_a(FakeFormPort(), controls, AnswerKey(),
                                           emit.MonotonicClock()))
    cands = {c.name: c for c in result.flow_candidates}
    assert set(cands) == {"Continue", "Delete policy"}
    assert cands["Continue"].danger is False
    assert cands["Delete policy"].danger is True  # matched rp.verb.delete
    assert cands["Delete policy"].danger_rule_id.startswith("rp.verb.")


# ─── Phase B: the real SUBMIT-phase guard gate ─────────────────────────────────


def _submit_control():
    return build_inventory([_raw("button", "Continue", kind="button", tag="button")],
                           _REFUSE_PACK, url="https://app.example/apply")[0]


def _delete_control():
    return build_inventory([_raw("button", "Delete policy", kind="button", tag="button")],
                           _REFUSE_PACK, url="https://app.example/apply")[0]


def _valid_attestation(now_ms=None):
    # M0.5 T-SEC-08: ``expires_at_ms`` is EPOCH millis and freshness is checked
    # against the wall clock. The old default (1000 + 100000) was a monotonic-
    # style number that only ever looked fresh because the comparison itself was
    # broken; a live attestation has to be built from the same clock that judges
    # it.
    import time as _t

    base = int(_t.time() * 1000) if now_ms is None else int(now_ms)
    return Attestation(attested_by="qa-lead", env_kind="disposable",
                       reset_procedure="db reset", expires_at_ms=base + 100000)


def test_phase_b_refuses_without_attestation():
    d = gate_submit(_submit_control(), "https://app.example/apply",
                    refuse_pack=_REFUSE_PACK, is_login_domain=True,
                    attestation=None, submit_flow_approved=True, now_ms=1000)
    assert d.allow is False and d.rule_id == GuardRule.SUBMIT_NO_ATTESTATION


def test_phase_b_refuses_without_approval():
    d = gate_submit(_submit_control(), "https://app.example/apply",
                    refuse_pack=_REFUSE_PACK, is_login_domain=True,
                    attestation=_valid_attestation(), submit_flow_approved=False,
                    now_ms=1000)
    assert d.allow is False and d.rule_id == GuardRule.SUBMIT_NOT_APPROVED


def test_phase_b_crosses_irreversible_when_attested_and_approved():
    # Founder/client direction: no submission limits on a Test/disposable env.
    # Past the disposable attestation + approval gate, an irreversible verb is
    # allowed and recorded (the reason names it) rather than refused.
    d = gate_submit(_delete_control(), "https://app.example/apply",
                    refuse_pack=_REFUSE_PACK, is_login_domain=True,
                    attestation=_valid_attestation(), submit_flow_approved=True,
                    now_ms=1000)
    assert d.allow is True and d.rule_id == GuardRule.SUBMIT_MUTATION_OK
    assert "irreversible verb" in (d.reason or "").lower()


def test_phase_b_allows_only_with_attestation_approval_and_safe_verb():
    d = gate_submit(_submit_control(), "https://app.example/apply",
                    refuse_pack=_REFUSE_PACK, is_login_domain=True,
                    attestation=_valid_attestation(), submit_flow_approved=True,
                    now_ms=1000)
    assert d.allow is True and d.rule_id == GuardRule.SUBMIT_MUTATION_OK


def test_execute_submit_phase_b_refuses_and_records_guard_event_without_clicking():
    with tempfile.TemporaryDirectory() as work:
        clock = emit.MonotonicClock()
        emitter = emit.ManifestEmitter(work, "cX", clock)
        port = FakeFormPort()
        result = asyncio.run(execute_submit_phase_b(
            port, _submit_control(), "https://app.example/apply", emitter, clock,
            refuse_pack=_REFUSE_PACK, is_login_domain=True,
            attestation=None, submit_flow_approved=False,
        ))
        assert result.submitted is False
        assert port.clicked == []  # NEVER clicked — no mutation
        events = [r for r in emit.read_records(work, "cX") if r["type"] == "guard_event"]
        assert events and events[0]["phase"] == "submit"


# ─── AUTH: matching, verification, window ───────────────────────────────────────


def _login_controls():
    raw = [
        _raw("textbox", "Username", kind="textbox", tag="input", input_type="text"),
        _raw("textbox", "Password", kind="textbox", tag="input", input_type="password"),
        _raw("button", "Sign in", kind="button", tag="button"),
    ]
    return build_inventory(raw, _REFUSE_PACK, url="https://app.example/login")


def test_match_login_controls_by_accessible_name():
    matched = match_login_controls(_login_controls())
    assert matched is not None
    assert matched.username["name"] == "Username"
    assert matched.password["input_type"] == "password"
    assert matched.submit["name"] == "Sign in"


def test_match_login_controls_none_when_no_password():
    raw = [_raw("textbox", "Search", kind="textbox", tag="input", input_type="text"),
           _raw("button", "Go", kind="button", tag="button")]
    controls = build_inventory(raw, _REFUSE_PACK, url="https://app.example/")
    assert match_login_controls(controls) is None


def test_verify_login_success_requires_change_no_password_no_error():
    ok, _ = verify_login_success(
        before_fingerprint="a", after_fingerprint="b",
        after_controls=[{"input_type": "text", "kind": "text"}], after_errors=[])
    assert ok is True

    same, reason = verify_login_success(
        before_fingerprint="a", after_fingerprint="a",
        after_controls=[], after_errors=[])
    assert same is False and "unchanged" in reason

    has_pw, reason2 = verify_login_success(
        before_fingerprint="a", after_fingerprint="b",
        after_controls=[{"input_type": "password"}], after_errors=[])
    assert has_pw is False and "password" in reason2

    err, reason3 = verify_login_success(
        before_fingerprint="a", after_fingerprint="b",
        after_controls=[], after_errors=["Invalid credentials"])
    assert err is False and "error" in reason3


def test_auth_window_closes_on_request_and_time_budgets():
    w = AuthWindow(max_requests=2, window_ms=1000)
    assert w.is_open(0) is False           # not opened yet → closed
    w.open(now_ms=100)
    w.note(150); w.note(160)
    assert w.is_open(200) is True          # 2 requests within budget + time
    w.note(170)                            # 3rd request → over request budget
    assert w.is_open(200) is False
    w2 = AuthWindow(max_requests=10, window_ms=1000)
    w2.open(now_ms=0)
    w2.note(500)
    assert w2.is_open(500) is True
    assert w2.is_open(1001) is False       # past the time budget


def test_credentials_from_payload():
    assert Credentials.from_payload(None) is None
    assert Credentials.from_payload({"username": "u"}) is None  # password required
    creds = Credentials.from_payload({"username": "u", "password": "p"})
    assert creds is not None and creds.username == "u"


# ── Custom choice cards (client showstopper: quote funnel never opened) ──

def test_radio_intent_is_always_select_never_uncheck():
    """A product card resolved to the synthesized value '100', which is not
    truthy — the crawl asked Playwright to UNCHECK it, so no product was ever
    chosen and the quote funnel stayed shut. A radio is only ever selected."""
    from app.forms import _wants_checked
    assert _wants_checked("radio", "100") is True
    assert _wants_checked("radio", "false") is True
    assert _wants_checked("checkbox", "100") is True
    assert _wants_checked("checkbox", "false") is False
    assert _wants_checked("toggle", "off") is False


def test_errored_toggle_fill_is_honest_residue_not_a_fill():
    """Three product cards errored yet the step reported '3 fields filled' —
    the walk then hunted for a Continue the app had never enabled."""
    import asyncio

    from app import emit
    from app.browser import RawObservation
    from app.forms import _fill_one

    class _ErrPort:
        async def set_checked(self, control, checked):
            return RawObservation(url_before="u", url_after="u",
                                  error_detail="action_error: Timeout 5000ms",
                                  intent_met=False)

    action, mechanic = asyncio.run(_fill_one(
        _ErrPort(), {"name": "Term Life", "kind": "radio"}, "radio", "100",
        emit.MonotonicClock(), phase="explore", state_id="fp"))
    assert action is None


def test_custom_card_falls_back_to_click_when_set_checked_fails():
    """The engine must try the native check, then CLICK — a styled card is
    selected the way a human selects it."""
    # M0.3/T-DE-05: the Playwright adapter was lifted out of app/main.py into
    # app/playwright_port.py. Same code, new home — the assertion is unchanged.
    src = open("app/playwright_port.py", encoding="utf-8").read()
    seg = src[src.index('elif kind == "checked":'):]
    seg = seg[:seg.index("except Exception as exc:")]
    assert "set_checked" in seg and "locator.click()" in seg
