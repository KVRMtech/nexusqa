"""P3 — Phase-B attested submit wired into the crawl loop. Default-OFF; fires only
when the operator supplied a per-flow approval list AND a disposable-env attestation,
and even then only for an approved, non-danger candidate (execute_submit_phase_b's
gate re-verifies). A confirmed navigation pushes the post-submit page onto the frontier.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
import types

from app import crawler as crawler_mod
# M0.3/T-DE-11: the Phase-B submit moved into app.submit, so the seam to
# patch is that module's binding of execute_submit_phase_b, not the
# crawler's. Same function, same call site — new home.
from app import submit as submit_mod
from app import emit
from app.browser import RawObservation
from app.config import Settings
from app.crawler import Budget, Crawler, FrontierItem, GuardContext, Phase
from app.forms import FlowCandidate, FormFillResult, SubmitResult, execute_submit_phase_b
from app.guard import load_refuse_pack

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII=")


class _DummyPort:
    """The constructor only stores the port; _maybe_submit_phase_b hands it to a
    monkeypatched execute_submit_phase_b, so no browser behaviour is needed here."""


def _build(tmp_path, *, approvals=(), attestation=None):
    guard = GuardContext(refuse_pack=_REFUSE_PACK, attestation=attestation)
    return Crawler(
        _DummyPort(),
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/form",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
        guard_context=guard, submit_approvals=approvals,
    )


def _fill(name, *, danger=False, control=None):
    fr = FormFillResult()
    fr.flow_candidates = [FlowCandidate(
        name=name, target_kind="button", danger=danger, danger_rule_id="",
        danger_severity="", control=control if control is not None else {"name": name, "kind": "button"},
    )]
    return fr


# ── the enable gate (default-OFF; needs BOTH approvals and an attestation) ───────

def test_submit_disabled_by_default(tmp_path):
    assert _build(tmp_path)._submit_enabled is False


def test_submit_requires_both_approvals_and_attestation(tmp_path):
    assert _build(tmp_path, approvals=["Continue"])._submit_enabled is False
    assert _build(tmp_path, attestation={"env_kind": "disposable"})._submit_enabled is False
    assert _build(tmp_path, approvals=["Continue"],
                  attestation={"env_kind": "disposable"})._submit_enabled is True


# ── _maybe_submit_phase_b behaviour ─────────────────────────────────────────────

def test_approved_flow_is_driven_and_post_submit_page_enqueued(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    seen = {}

    async def fake_submit(port, control, url, emitter, clock, **kw):
        seen["control"] = control
        seen["approved"] = kw.get("submit_flow_approved")
        seen["attestation"] = kw.get("attestation")
        seen["phase_during"] = c._guard.phase           # must be SUBMIT mid-call
        ps = types.SimpleNamespace(location="https://app.example/step2")
        return SubmitResult(submitted=True, decision=None, confirmed=True,
                            outcome="navigation", page_state=ps)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="https://app.example/form", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp1"))

    assert seen["approved"] is True
    assert seen["attestation"] == {"env_kind": "disposable"}
    assert seen["phase_during"] == Phase.SUBMIT
    assert c._guard.phase == Phase.EXPLORE          # restored fail-closed
    assert c._guard.submit_flow_approved is False   # restored
    assert c._forms_submitted == 1
    popped = c._frontier.pop()
    assert popped is not None and popped.url == "https://app.example/step2"
    assert popped.discovered_via == "submit:Continue"


def test_unapproved_flow_is_not_submitted(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    calls = {"n": 0}

    async def fake_submit(*a, **k):
        calls["n"] += 1
        return SubmitResult(submitted=True, decision=None)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Delete", "kind": "button"}],
                                        _fill("Delete"), "fp"))
    assert calls["n"] == 0
    assert c._forms_submitted == 0


def test_danger_candidate_is_never_submitted(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})
    calls = {"n": 0}

    async def fake_submit(*a, **k):
        calls["n"] += 1
        return SubmitResult(submitted=True, decision=None)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue", danger=True), "fp"))
    assert calls["n"] == 0
    assert c._forms_submitted == 0


def test_unconfirmed_submit_adds_no_frontier(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})

    async def fake_submit(*a, **k):
        # submit ran (counts) but no positive terminal outcome → no deeper crawl.
        return SubmitResult(submitted=True, decision=None, confirmed=False, outcome="none")

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    item = FrontierItem(url="u", depth=0)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp"))
    assert c._forms_submitted == 1
    assert c._frontier.pop() is None   # nothing enqueued


class _SubmitPort:
    """A minimal BrowserPort for the REAL execute_submit_phase_b: the submit click
    lands on a post-submit page that renders a computed PREMIUM (the outcome the
    value oracle must ground)."""

    def __init__(self, premium="$189.42"):
        self._premium = premium

    async def goto(self, url):
        return types.SimpleNamespace(ok=True, url=url, error="")

    async def collect_controls(self):
        return []

    async def click(self, control):
        # same-URL confirmation (a computed quote renders in place).
        return RawObservation(url_before="http://acme/#/quote",
                              url_after="http://acme/#/quote", dom_changed=True)

    async def collect_displayed_values(self):
        # the post-submit premium node the crawler must capture.
        return [{"label": "Monthly Premium", "selector": "div.prem", "text": self._premium}]

    async def screenshot_png(self):
        return _PNG


def test_phase_b_captures_post_submit_displayed_values_for_the_value_oracle():
    """SUBMIT-DEPTH → VALUE-ORACLE: the post-submit page is where a premium/decline
    renders. execute_submit_phase_b must capture its displayed_values (normalized +
    #2-classified) so a confirmed expected outcome can ground to a real node —
    without this the crawl reaches the page but the value is invisible."""
    with tempfile.TemporaryDirectory() as work:
        clock = emit.MonotonicClock()
        emitter = emit.ManifestEmitter(work, "c1", clock)
        attestation = types.SimpleNamespace(is_submit_capable=lambda now_ms=None: True)
        result = asyncio.run(execute_submit_phase_b(
            _SubmitPort(), {"name": "Get quote", "kind": "button"},
            "http://acme/#/quote", emitter, clock,
            refuse_pack=_REFUSE_PACK, attestation=attestation,
            submit_flow_approved=True, now_ms=1, sequence_index=0,
        ))
        assert result.submitted is True
        dvs = result.page_state.displayed_values
        prem = next((d for d in dvs if d["selector"] == "div.prem"), None)
        assert prem is not None, "post-submit premium node was not captured"
        # #2 inference ran on the terminal state's value: currency, candidate.
        assert prem["value_type"] == "currency"
        assert prem["value_candidate"] == "true"
        assert prem["text"] == "$189.42"


# ── hardening from adversarial review (#6 submit window, #7 max_depth) ───────────

def test_submit_window_bounds_the_mutating_post_burst():
    # A valid disposable-env attestation so the fine gate (classify_request) would
    # otherwise ALLOW each approved mutating POST — isolating the window bound.
    attestation = types.SimpleNamespace(is_submit_capable=lambda now_ms=None: True)
    guard = GuardContext(refuse_pack=_REFUSE_PACK, attestation=attestation)
    guard.phase = Phase.SUBMIT
    guard.submit_flow_approved = True
    guard.submit_window.open(0)
    # A single approved submit may authorise its POST(s) — NOT an unbounded burst of
    # every POST the page fires during the window. After the request budget the
    # window closes and further mutating POSTs are refused (fail-closed).
    results = [guard.decide("POST", "https://app.example/x", now_ms=1) for _ in range(8)]
    assert any((not r.allow) and r.rule_id == "guard.submit.window_closed" for r in results)


def test_submit_respects_max_depth(tmp_path, monkeypatch):
    c = _build(tmp_path, approvals=["Continue"], attestation={"env_kind": "disposable"})

    async def fake_submit(*a, **k):
        ps = types.SimpleNamespace(location="https://app.example/deep")
        return SubmitResult(submitted=True, decision=None, confirmed=True,
                            outcome="navigation", page_state=ps)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    # An item AT max_depth still submits, but must NOT enqueue a deeper state.
    item = FrontierItem(url="https://app.example/form", depth=c._budget.max_depth)
    asyncio.run(c._maybe_submit_phase_b(item, [{"name": "Continue", "kind": "button"}],
                                        _fill("Continue"), "fp"))
    assert c._forms_submitted == 1
    assert c._frontier.pop() is None


# ── next-action crossing (Apply Now on a formless quote summary) ────────────────

def test_next_action_forward_is_crossed_in_place_and_enqueued(tmp_path, monkeypatch):
    """A quote summary's 'Apply Now' has no form fields, so the form-submit path
    never sees it. The next-action path crosses it IN PLACE (renavigate=False — the
    walk-built summary state must not be discarded) and enqueues the resulting page
    so the application → e-sign funnel is crawled as the continuation."""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    seen = {}

    async def fake_submit(port, control, url, emitter, clock, **kw):
        seen["control_name"] = control.get("name")
        seen["renavigate"] = kw.get("renavigate")
        seen["approved"] = kw.get("submit_flow_approved")
        ps = types.SimpleNamespace(location="https://app.example/portal/apply")
        return SubmitResult(submitted=True, decision=None, confirmed=True,
                            outcome="navigation", page_state=ps)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    controls = [
        {"name": "Apply Now", "kind": "link"},       # forward commit -> crossed
        {"name": "Start Over", "kind": "button"},    # not a commit word -> skipped
        {"name": "Sign out", "kind": "link"},        # auth chrome -> skipped
    ]
    asyncio.run(c._maybe_submit_next_action(
        controls=controls, url="https://app.example/quote/review",
        fingerprint="fpR", depth=0))

    assert seen["control_name"] == "Apply Now"
    assert seen["renavigate"] is False               # crossed IN PLACE, never re-navigated
    assert seen["approved"] is True
    assert c._forms_submitted == 1
    popped = c._frontier.pop()
    assert popped is not None and popped.url == "https://app.example/portal/apply"
    assert popped.discovered_via == "submit:Apply Now"


def test_next_action_not_crossed_when_submit_disabled(tmp_path, monkeypatch):
    """No attestation/approvals → submit disabled → the boundary is never crossed;
    the crawl stops at 'Apply Now' exactly as before."""
    c = _build(tmp_path)          # no approvals, no attestation
    calls = {"n": 0}

    async def fake_submit(*a, **k):
        calls["n"] += 1
        return SubmitResult(submitted=True, decision=None)

    monkeypatch.setattr(submit_mod, "execute_submit_phase_b", fake_submit)
    asyncio.run(c._maybe_submit_next_action(
        controls=[{"name": "Apply Now", "kind": "link"}],
        url="https://app.example/quote/review", fingerprint="fpR", depth=0))
    assert calls["n"] == 0
    assert c._frontier.pop() is None


# ── danger forward control (an application's "Continue to Underwriting Decision") ─

def test_danger_forward_control_is_crossed_on_disposable_blanket(tmp_path):
    """"Continue to Underwriting Decision" is refuse-pack DANGER (rp.verb.underwrite)
    yet is the real next step toward e-sign. Tiers 1-2 skip danger and the oracle
    excludes it, so on a disposable blanket env the walk must return it as a
    submit_control (crossed via the submit path) rather than passing it over for a
    nav link. This is what carries the application funnel past /apply/lifestyle."""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    controls = [
        {"name": "Get a Quote", "kind": "link", "danger": False},                 # nav chrome
        {"name": "Continue to Underwriting Decision", "kind": "button", "danger": True},
        {"name": "Back", "kind": "button", "danger": False},                      # not forward
        {"name": "Sign out", "kind": "button", "danger": True},                   # auth chrome
    ]
    dec = asyncio.run(c._pick_advance_e2e(
        controls, "https://app.example/apply/lifestyle", "Lifestyle", "fpL"))
    assert dec.submit_control is not None
    assert dec.submit_control["name"] == "Continue to Underwriting Decision"
    assert dec.control is None       # crossed, not clicked as a plain advance


def test_danger_forward_control_not_crossed_without_blanket(tmp_path):
    """Without the disposable blanket a danger forward control is left alone —
    production stays at the boundary exactly as before."""
    c = _build(tmp_path, approvals=["some flow"], attestation={"env_kind": "disposable"})
    assert c._submit_approve_all is False       # no "*"
    controls = [{"name": "Continue to Underwriting Decision", "kind": "button", "danger": True}]
    dec = asyncio.run(c._pick_advance_e2e(controls, "u", "t", "fp"))
    assert dec.submit_control is None


# ── questionnaire answering (bare Yes/No buttons on /apply/lifestyle) ────────────

class _RecordingPort(_DummyPort):
    def __init__(self):
        self.clicks = []
    async def click(self, control):
        self.clicks.append(str(control.get("name") or ""))
        return RawObservation(url_before="u", url_after="u")


def test_answer_questionnaire_answers_one_question_per_call_preferring_no(tmp_path):
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls = [
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},   # Q1
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},   # Q2
        {"name": "Continue to Underwriting Decision", "kind": "button", "danger": True},
        {"name": "Back", "kind": "button"},
    ]
    dp1 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert len(dp1) == 1 and dp1[0]["choice"] == "No"        # prefers the negative answer
    assert dp1[0]["options"] == ["Yes", "No"] and dp1[0]["provenance"] == "questionnaire"
    assert c._port.clicks == ["No"]

    dp2 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    # M2.1 — the page declared no question wording, so the crawl records NONE.
    # It used to record "Question 2", which no element on the page ever said.
    assert len(dp2) == 1 and dp2[0]["control_label"] == ""
    assert c._port.clicks == ["No", "No"]                    # second question answered

    dp3 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert dp3 == []                                          # nothing left to answer


def test_answer_questionnaire_ignores_a_page_with_no_repeated_options(tmp_path):
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    # A normal page: unique action buttons, no repeated Yes/No answer set.
    controls = [
        {"name": "Continue", "kind": "button"},
        {"name": "Back", "kind": "button"},
        {"name": "Add Beneficiary", "kind": "button"},
    ]
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []
    assert c._port.clicks == []


def test_answer_questionnaire_never_treats_a_danger_button_as_an_answer(tmp_path):
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    # Repeated but DANGER ("Remove" ×2) — a destructive control is never an answer.
    controls = [
        {"name": "Remove", "kind": "button", "danger": True},
        {"name": "Remove", "kind": "button", "danger": True},
    ]
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []
    assert c._port.clicks == []


# ── P0 lock-in tests: pin the DOM-order grouping so it can't silently regress ────

def test_answer_questionnaire_scales_to_many_questions(tmp_path):
    """The live-proven case: 17 identical Yes/No pairs group into 17 distinct
    questions, answered one per call, in order, then exhaust. Locks the ordinal
    grouping at fleet scale — a regression that merged or dropped questions would
    change this count."""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls: list[dict] = []
    for _ in range(17):
        controls.append({"name": "Yes", "kind": "button"})
        controls.append({"name": "No", "kind": "button"})
    answered: list[str] = []
    sigs: list[str] = []
    for _ in range(17):
        dp = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
        assert len(dp) == 1
        answered.append(dp[0]["control_label"])
        sigs.append(dp[0]["control_signature"])
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []
    # M2.1 — 17 questions, 17 DISTINCT identities, and not one word of invented
    # wording: this page declares no question text, so every label is empty and
    # the catalogue will mark these UNVERIFIED rather than publish "Question 9".
    assert answered == [""] * 17
    assert len(set(sigs)) == 17            # 17 questions, never merged
    assert c._port.clicks == ["No"] * 17


def test_answer_questionnaire_groups_three_option_questions(tmp_path):
    """A question can have >2 options (Never/Sometimes/Often). A new question
    begins only when a label REPEATS, so three-option groups are one question
    each; the negative hint ('never') is still preferred."""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls = [
        {"name": "Never", "kind": "button"}, {"name": "Sometimes", "kind": "button"},
        {"name": "Often", "kind": "button"},
        {"name": "Never", "kind": "button"}, {"name": "Sometimes", "kind": "button"},
        {"name": "Often", "kind": "button"},
    ]
    dp1 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert len(dp1) == 1 and dp1[0]["choice"] == "Never"
    assert dp1[0]["options"] == ["Never", "Sometimes", "Often"]
    dp2 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    # M2.1 — the page declared no question wording, so the crawl records NONE.
    # It used to record "Question 2", which no element on the page ever said.
    assert len(dp2) == 1 and dp2[0]["control_label"] == ""
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []
    assert c._port.clicks == ["Never", "Never"]


def test_answer_questionnaire_excludes_auth_chrome_between_questions(tmp_path):
    """A repeated 'Sign out' is auth chrome (matches the auth regex), never an
    answer, and does not split or invent a question — only the real Yes/No pairs
    are questions. (Options must repeat page-wide to count, so two questions.)"""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls = [
        {"name": "Sign out", "kind": "button"},                              # top chrome
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},  # Q1
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},  # Q2
        {"name": "Sign out", "kind": "button"},                              # bottom chrome
    ]
    dp1 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert len(dp1) == 1 and dp1[0]["options"] == ["Yes", "No"]
    dp2 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    # M2.1 — the page declared no question wording, so the crawl records NONE.
    # It used to record "Question 2", which no element on the page ever said.
    assert len(dp2) == 1 and dp2[0]["control_label"] == ""
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []
    assert c._port.clicks == ["No", "No"]        # 'Sign out' never clicked


def _qsig(ordinal, opts):
    """Replicate _answer_questionnaire's question signature so a test can force it."""
    return "q:" + hashlib.sha256(
        ("%d|%s" % (ordinal, "|".join(sorted(o.lower() for o in opts))))
        .encode("utf-8")).hexdigest()[:24]


def test_answer_questionnaire_honors_a_forced_option_for_branch_walks(tmp_path):
    """P1: a planned branch walk forces the 'Yes' side via choice_overrides keyed
    by the question signature — so a re-crawl can enumerate what 'Yes' reveals.
    Unforced questions keep the negative-preference default."""
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls = [
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},   # Q1
        {"name": "Yes", "kind": "button"}, {"name": "No", "kind": "button"},   # Q2
    ]
    c._choice_overrides = {_qsig(0, ["Yes", "No"]): "yes"}    # force Q1 = Yes
    dp1 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert dp1[0]["choice"] == "Yes"                          # forced, not the default
    assert c._port.clicks == ["Yes"]
    dp2 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert dp2[0]["choice"] == "No"                           # Q2 unforced → negative
    assert c._port.clicks == ["Yes", "No"]


# ── P4: the tail keeps a policy-number / confirmation reference as evidence ──────

def test_boundary_outcome_types_capture_policy_reference_not_noise():
    """P4: a walked flow's terminal keeps `reference` outcomes (a policy number /
    confirmation proves issuance) alongside currency/decision/percent — but NOT
    low-signal noise (other/number/date), which would dilute the evidence."""
    from app.crawler import _BOUNDARY_OUTCOME_TYPES
    assert "reference" in _BOUNDARY_OUTCOME_TYPES
    assert {"currency", "decision", "percent"} <= set(_BOUNDARY_OUTCOME_TYPES)
    assert "other" not in _BOUNDARY_OUTCOME_TYPES
    assert "number" not in _BOUNDARY_OUTCOME_TYPES
    assert "date" not in _BOUNDARY_OUTCOME_TYPES


# ── M2.1 · T-QT-01/T-QT-04: the question, in the application's own words ───────

def test_answer_questionnaire_records_the_declared_question_wording(tmp_path):
    """A questionnaire that DECLARES its questions is catalogued in its own words.

    This is the defect M2.1 closes. The walker had no handle on a bare-button
    question but its DOM ordinal, so it wrote "Question 1", "Question 2" — text
    no element on the page has ever contained — and the catalogue published it as
    if it were the application's. Here the page states the questions; the crawl
    must repeat them and invent nothing.
    """
    c = _build(tmp_path, approvals=["*"], attestation={"env_kind": "disposable"})
    c._port = _RecordingPort()
    controls = [
        {"name": "Yes", "kind": "button", "question_group_id": "g_tobacco",
         "question_label": "Have you used tobacco in the last 12 months?"},
        {"name": "No", "kind": "button", "question_group_id": "g_tobacco",
         "question_label": "Have you used tobacco in the last 12 months?"},
        {"name": "Yes", "kind": "button", "question_group_id": "g_hosp",
         "question_label": "Have you been hospitalised in the last 5 years?"},
        {"name": "No", "kind": "button", "question_group_id": "g_hosp",
         "question_label": "Have you been hospitalised in the last 5 years?"},
    ]
    dp1 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert dp1[0]["control_label"] == "Have you used tobacco in the last 12 months?"
    # The DECLARED question id is the branch key, so the fold and the catalogue
    # land on ONE identity for this question (T-QT-04).
    assert dp1[0]["group_id"] == "g_tobacco"
    # ONE value: the branch key the fold stores and the key a planned walk
    # forces its answer on must be the same string, or a planned questionnaire
    # walk silently answers the default and reports the branch walked.
    assert dp1[0]["control_signature"] == "g_tobacco"

    dp2 = asyncio.run(c._answer_questionnaire(controls, "u", "fp"))
    assert dp2[0]["control_label"] == "Have you been hospitalised in the last 5 years?"
    assert dp2[0]["group_id"] == "g_hosp"
    assert asyncio.run(c._answer_questionnaire(controls, "u", "fp")) == []


def test_declared_question_identity_survives_a_question_inserted_above_it(tmp_path):
    """STABLE ACROSS RE-CRAWLS. The ordinal hash re-keyed every question below an
    inserted one, so adding a question to a form made the catalogue believe the
    rest had been replaced. A declared identity does not move."""
    def _q(gid, label):
        return [{"name": n, "kind": "button", "question_group_id": gid,
                 "question_label": label} for n in ("Yes", "No")]

    before = _q("g_tobacco", "Do you use tobacco?") + _q("g_diab", "Diabetes?")
    c1 = _build(tmp_path / "a", approvals=["*"], attestation={"env_kind": "disposable"})
    c1._port = _RecordingPort()
    sig_before = asyncio.run(
        c1._answer_questionnaire(before, "u", "fp"))[0]["control_signature"]

    # The SAME application with one question inserted ABOVE tobacco.
    after = _q("g_new", "Are you a citizen?") + before
    c2 = _build(tmp_path / "b", approvals=["*"], attestation={"env_kind": "disposable"})
    c2._port = _RecordingPort()
    asyncio.run(c2._answer_questionnaire(after, "u", "fp"))           # the new Q1
    second = asyncio.run(c2._answer_questionnaire(after, "u", "fp"))  # tobacco, now Q2
    assert second[0]["control_label"] == "Do you use tobacco?"
    assert second[0]["control_signature"] == sig_before
