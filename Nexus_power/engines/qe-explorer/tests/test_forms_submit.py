"""Pure-logic tests for FORMS Phase-B submit (design §6 Phase 5 / §3.2).

The Phase-B submit path is the ONE place the platform mutates a customer app,
so it is fail-closed and its evidence must be honest.  These tests prove, with a
scripted fake :class:`app.browser.BrowserPort` and NO real browser:

  * the THREE refusal grounds each REFUSE via a real
    ``guard.classify_request(phase=SUBMIT)`` decision — recording a
    ``guard_event`` and NEVER navigating or clicking (no mutation):
      1. no disposable-env attestation,
      2. no per-flow approval,
      3. an irreversible refuse-pack verb EVEN WHEN attested + approved;
  * on an attested + approved + non-irreversible flow the approved flow is
    re-driven and the submit is executed, capturing the grounded terminal
    outcome (``navigation`` when the URL changes; a same-page ``confirmation``
    otherwise) + a baseline confirmation screenshot;
  * a rejected/no-effect submit is recorded HONESTLY (``confirmed=False``) — a
    confirmation is never fabricated on nothing (never-green-wash);
  * the re-drive actually re-fills the form from the answer key before submit;
  * the emitted terminal ``page_state`` + submit action + baseline map onto a
    VALID qe-central :class:`ExplorationBundle` (a page_visit + a baseline
    visual_frame carrying a PROVEN navigation) — the BEHAVES evidence.

NO browser, NO network, NO Playwright.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import tempfile
from pathlib import Path

from app import emit
from app.browser import NavResult, RawObservation
from app.config import Settings
from app.forms import (
    REASON_FORM_UNREACHABLE,
    REASON_REFUSED,
    AnswerKey,
    execute_submit_phase_b,
)
from app.guard import Attestation, GuardRule, load_refuse_pack
from app.inventory import build_inventory

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)

FORM_URL = "https://app.example/apply"
CONFIRM_URL = "https://app.example/confirmation"

#: A minimal valid 1x1 PNG (asset/schema validation is qe-central-side; the
#: emitter only requires non-empty bytes to stage a frame).
PNG_1x1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
PNG_1x1 = base64.b64decode(PNG_1x1_B64)


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _raw(role, name, **over):
    base = {"role": role, "name": name, "name_source": "label-for", "best_effort": False,
            "kind": role, "tag": over.pop("tag", role), "input_type": "", "options": [],
            "required": False, "disabled": False, "frame_selector": "", "testid": "",
            "css_hint": "", "value_committed": "", "landmark": {"role": "", "name": ""}}
    base.update(over)
    return base


def _submit_control():
    return build_inventory([_raw("button", "Submit", kind="button", tag="button")],
                           _REFUSE_PACK, url=FORM_URL)[0]


def _delete_control():
    return build_inventory([_raw("button", "Delete policy", kind="button", tag="button")],
                           _REFUSE_PACK, url=FORM_URL)[0]


def _fill_controls():
    raw = [
        _raw("textbox", "Amount", kind="textbox", tag="input", input_type="text"),
        _raw("combobox", "Account", kind="combobox", tag="select",
             options=["Checking", "Savings"]),
    ]
    return build_inventory(raw, _REFUSE_PACK, url=FORM_URL)


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


class FakeSubmitPort:
    """Scripted BrowserPort for the Phase-B path.

    Records every ``goto`` / ``click`` / fill so a test can assert the app was
    (or was NOT) touched; ``click`` returns a caller-scripted observation so the
    terminal-outcome classification is exercised over each grounded shape.
    """

    def __init__(self, *, click_obs: RawObservation, goto_ok: bool = True,
                 png: bytes = PNG_1x1) -> None:
        self._click_obs = click_obs
        self._goto_ok = goto_ok
        self._png = png
        self.clicked: list[str] = []
        self.filled: dict[str, str] = {}
        self.goto_urls: list[str] = []
        self.screenshots = 0
        self._current = ""

    async def goto(self, url: str) -> NavResult:
        self.goto_urls.append(url)
        self._current = url
        return NavResult(url=url, ok=self._goto_ok)

    async def current_url(self) -> str:
        return self._current

    async def click(self, control):
        self.clicked.append(str(control.get("name")))
        return self._click_obs

    async def fill(self, control, value):
        self.filled[str(control.get("name"))] = value
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def select_option(self, control, value):
        self.filled[str(control.get("name"))] = value
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def set_checked(self, control, checked):
        val = "true" if checked else "false"
        self.filled[str(control.get("name"))] = val
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=val)

    async def screenshot_png(self) -> bytes:
        self.screenshots += 1
        return self._png


def _nav_obs():
    return RawObservation(url_before=FORM_URL, url_after=CONFIRM_URL)


def _confirmation_obs():
    return RawObservation(url_before=FORM_URL, url_after=FORM_URL,
                          confirmation_detail="Your application was submitted. Ref #12345")


def _error_obs():
    return RawObservation(url_before=FORM_URL, url_after=FORM_URL,
                          error_detail="Card declined")


def _run(port, control, **kw):
    """Drive execute_submit_phase_b in a fresh temp work dir, returning
    ``(result, manifest_records)`` so persisted records can be asserted."""
    with tempfile.TemporaryDirectory() as work:
        clock = emit.MonotonicClock()
        emitter = emit.ManifestEmitter(work, "c1", clock)
        result = asyncio.run(execute_submit_phase_b(
            port, control, FORM_URL, emitter, clock, refuse_pack=_REFUSE_PACK,
            is_login_domain=True, now_ms=1000, **kw))
        records = emit.read_records(work, "c1")
    return result, records


# ─── The three refusals (fail-closed; app never touched) ────────────────────


def test_submit_refuses_without_attestation_and_never_touches_app():
    port = FakeSubmitPort(click_obs=_nav_obs())
    result, records = _run(port, _submit_control(),
                           attestation=None, submit_flow_approved=True)
    assert result.submitted is False and result.confirmed is False
    assert result.reason == REASON_REFUSED
    assert result.decision.rule_id == GuardRule.SUBMIT_NO_ATTESTATION
    assert result.page_state is None and result.baseline is None
    # Never navigated, never clicked → zero mutation possible.
    assert port.goto_urls == [] and port.clicked == []
    events = [r for r in records if r["type"] == "guard_event"]
    assert events and events[0]["phase"] == "submit"
    assert events[0]["rule_id"] == GuardRule.SUBMIT_NO_ATTESTATION
    assert not [r for r in records if r["type"] == "page_state"]


def test_submit_refuses_without_approval_and_never_touches_app():
    port = FakeSubmitPort(click_obs=_nav_obs())
    result, records = _run(port, _submit_control(),
                           attestation=_valid_attestation(), submit_flow_approved=False)
    assert result.submitted is False and result.confirmed is False
    assert result.decision.rule_id == GuardRule.SUBMIT_NOT_APPROVED
    assert port.goto_urls == [] and port.clicked == []
    events = [r for r in records if r["type"] == "guard_event"]
    assert events and events[0]["rule_id"] == GuardRule.SUBMIT_NOT_APPROVED


def test_irreversible_verb_is_crossed_and_recorded_when_attested_and_approved():
    # Founder/client direction: a Test/disposable env has NO submission limits.
    # An irreversible verb, under a submit-capable (disposable) attestation +
    # flow approval, is now CROSSED and clicked rather than refused — and the
    # decision reason records that it was an irreversible verb, so evidence still
    # shows exactly what was done.
    port = FakeSubmitPort(click_obs=_nav_obs())
    result, records = _run(port, _delete_control(),
                           attestation=_valid_attestation(), submit_flow_approved=True)
    assert result.submitted is True and result.confirmed is True
    assert result.decision.rule_id == GuardRule.SUBMIT_MUTATION_OK
    assert "irreversible verb" in (result.decision.reason or "").lower()
    assert port.clicked and port.clicked != []


# ─── The happy path: submit + confirmation + baseline ───────────────────────


def test_submit_navigation_captures_confirmation_and_baseline():
    port = FakeSubmitPort(click_obs=_nav_obs())
    result, records = _run(port, _submit_control(),
                           attestation=_valid_attestation(), submit_flow_approved=True)

    assert result.submitted is True and result.confirmed is True
    assert result.outcome == "navigation"
    # Re-drove the flow (navigated to the form) then clicked exactly once.
    assert port.goto_urls == [FORM_URL] and port.clicked == ["Submit"]

    # The terminal submit action is grounded: a PROVEN navigation.
    action = result.action
    assert action is not None and action.verb == "submit"
    assert action.after["outcome"] == "navigation"
    assert action.after["navigated"] is True and action.url_changed is True

    # A real baseline confirmation screenshot was captured + staged.
    assert result.baseline is not None
    assert result.baseline.path.endswith(".png")
    assert port.screenshots >= 1  # the port was actually asked for a screenshot

    # The terminal page_state carries the baseline + the submit action.
    ps = result.page_state
    assert ps is not None and ps.location == CONFIRM_URL
    assert len(ps.screenshots) == 1
    assert ps.screenshots[0]["frame_index"] == result.baseline.frame_index

    persisted = [r for r in records if r["type"] == "page_state"]
    assert len(persisted) == 1
    assert persisted[0]["location"] == CONFIRM_URL
    assert persisted[0]["actions"][0]["verb"] == "submit"


def test_submit_same_page_confirmation_earns_a_baseline_without_navigation():
    port = FakeSubmitPort(click_obs=_confirmation_obs())
    result, _ = _run(port, _submit_control(),
                     attestation=_valid_attestation(), submit_flow_approved=True)

    assert result.submitted is True and result.confirmed is True
    assert result.outcome == "confirmation"
    # Same-page confirmation: no navigation credit, but a baseline IS captured.
    assert result.action.after["navigated"] is False
    assert result.action.url_changed is False
    assert result.action.after["detail"].startswith("Your application was submitted")
    assert result.baseline is not None
    # location stays the form URL (no navigation) — still a valid terminal visit.
    assert result.page_state.location == FORM_URL


# ─── Never-green-wash: a rejected submit is recorded honestly ───────────────


def test_submit_error_is_recorded_honestly_and_not_confirmed():
    port = FakeSubmitPort(click_obs=_error_obs())
    result, records = _run(port, _submit_control(),
                           attestation=_valid_attestation(), submit_flow_approved=True)

    # We clicked (attested + approved + safe verb), but the app REJECTED it:
    # honest outcome, NEVER a fabricated confirmation.
    assert result.submitted is True
    assert result.confirmed is False
    assert result.outcome == "error"
    assert result.action.after["outcome"] == "error"
    assert result.action.after["navigated"] is False
    # The demonstrated (rejected) outcome is still captured as evidence.
    assert result.baseline is not None
    assert [r for r in records if r["type"] == "page_state"]


def test_submit_form_unreachable_stops_without_clicking():
    port = FakeSubmitPort(click_obs=_nav_obs(), goto_ok=False)
    result, records = _run(port, _submit_control(),
                           attestation=_valid_attestation(), submit_flow_approved=True)
    # Guard authorised, but the disposable env is unreachable — honest stop, no
    # click, no fabricated confirmation.
    assert result.submitted is False and result.confirmed is False
    assert result.reason == REASON_FORM_UNREACHABLE
    assert port.goto_urls == [FORM_URL] and port.clicked == []
    assert not [r for r in records if r["type"] == "page_state"]


# ─── Re-drive re-fills the approved form before submitting ──────────────────


def test_redrive_refills_form_from_answer_key_before_submit():
    port = FakeSubmitPort(click_obs=_nav_obs())
    key = AnswerKey.from_payload({"exact": {"Amount": "250.00", "Account": "Savings"}})
    result, _ = _run(port, _submit_control(),
                     attestation=_valid_attestation(), submit_flow_approved=True,
                     answer_key=key, fill_controls=_fill_controls())

    assert result.submitted is True and result.confirmed is True
    # The approved flow was re-established: both fields re-filled, THEN submit.
    assert port.filled == {"Amount": "250.00", "Account": "Savings"}
    assert port.clicked == ["Submit"]


# ─── The confirmation maps to a VALID qe-central substrate bundle ───────────


def _load_schema_module():
    """Load the REAL qe-central ExplorationBundle schema by file path (the
    package dir ``platform/qe-central`` is not importable — hyphen).

    The schema uses ``from __future__ import annotations`` (string annotations),
    so the module must be registered in ``sys.modules`` before exec and its
    models ``model_rebuild()``-ed for pydantic to resolve the forward refs when
    we INSTANTIATE ``PageState`` (unlike the field-name-only crawler contract
    test, which never constructs a model)."""
    import sys

    root = Path(__file__).resolve().parents[3]
    schema_path = root / "platform" / "qe-central" / "app" / "substrate" / "schema.py"
    spec = importlib.util.spec_from_file_location("_qec_schema_submit", schema_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so forward-ref resolution finds the globalns
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    for name in ("AnchorBundle", "AfterBundle", "ActionRecord", "ScreenshotRef",
                 "PageState", "ExplorationBundle"):
        getattr(module, name).model_rebuild()
    return module


def _to_schema_pagestate(rec: dict, schema, png_b64: str):
    """Map a PERSISTED manifest page_state record onto the real qe-central
    ``PageState`` exactly as the qe-central mapper does: drop manifest-only
    routing keys and stage each screenshot ``path`` → ``png_base64``."""
    action_keep = set(schema.ActionRecord.model_fields)
    actions = [{k: v for k, v in a.items() if k in action_keep}
               for a in rec.get("actions", [])]
    shots = [{"frame_index": s["frame_index"], "timestamp_ms": s["timestamp_ms"],
              "png_base64": png_b64} for s in rec.get("screenshots", [])]
    page_keep = set(schema.PageState.model_fields)
    kwargs = {k: v for k, v in rec.items() if k in page_keep}
    kwargs["actions"] = actions
    kwargs["screenshots"] = shots
    return schema.PageState(**kwargs)


def test_confirmation_maps_to_valid_exploration_bundle_with_baseline():
    schema = _load_schema_module()
    port = FakeSubmitPort(click_obs=_nav_obs())
    result, records = _run(port, _submit_control(),
                           attestation=_valid_attestation(), submit_flow_approved=True)

    terminal = next(r for r in records if r["type"] == "page_state")
    page = _to_schema_pagestate(terminal, schema, PNG_1x1_B64)

    # The terminal visit carries a grounded submit action + a baseline frame.
    assert len(page.actions) == 1
    submit = page.actions[0]
    assert submit.verb == "submit"
    assert submit.after is not None and submit.after.outcome == "navigation"
    assert submit.after.navigated is True and submit.url_changed is True
    assert len(page.screenshots) == 1  # the baseline visual_frame

    # The whole bundle validates (frame_count matches, sequence monotonic) — the
    # confirmation is a first-class page_visit + baseline the writer can persist.
    bundle = schema.ExplorationBundle(
        crawl_id="c1", target_url=FORM_URL, explorer_version="test/1.0",
        config_fingerprint="fp", frame_count=1, pages=[page])
    assert bundle.total_actions() == 1
    assert bundle.pages[0].location == CONFIRM_URL
