"""M1.4 / T-CF-01..04 — A CONFIRMATION PAGE IS A COMPLETED JOURNEY.

    walk the funnel -> the application says "Application Submitted" ->
    the walk STOPS there -> terminal=confirmation -> completed=True

WHAT WAS WRONG.  ``classify_submit_after`` has been able to return
``confirmation`` since the submit tier was written, and the walk never once
called it: ``emit.build_action_record`` runs ``classify_after``, which has no
``confirmation`` branch at all.  So the only classifier in the codebase that can
say "this landed on a confirmation" was reachable from the submit tier alone,
and a funnel whose last step is an ordinary advance could not produce the
verdict by any path.

The walk then did what it does on every other page: it looked for something to
click.  A confirmation page always offers something — "Back to Dashboard",
"Print Confirmation", "New Application", sometimes a bare "Continue" — so it
clicked one, landed somewhere it had already been, and recorded ``loop`` /
``completed=false`` for a funnel it had just driven to a confirmation number.

THE FOUR ACCEPTANCE TASKS, ONE SECTION EACH:

  T-CF-01  a first-class completing terminal exists  (the ledger, pure)
  T-CF-02  ``OUTCOME_CONFIRMATION`` reaches ``build_flow``  (the whole path)
  T-CF-03  recognized confirmation OUTRANKS loop  (the precedence, pure)
  T-CF-04  the M1.2 proving-ground journey completes honestly  (end to end)

WHAT IS REAL HERE.  The frontier, the budget, the guard, the refuse pack, the
inventory, the fingerprinter, the form engine, the wizard walker, the flow
ledger and the coverage builder are all the production objects.  Only the
BROWSER is scripted, through the same :class:`app.browser.BrowserPort` the
Playwright adapter implements.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import flow_ledger
from app import walker as walker_module
from app.boundary import (DECLARED_CONFIRMATION_RUNGS, RUNG_ARIA_STATUS,
                          RUNG_DIALOG, RUNG_NAVIGATION, RUNG_TRANSITION_TEXT,
                          confirmation_transition, is_confirmation_landing)
from app.browser import (OUTCOME_CONFIRMATION, RawObservation, classify_after,
                         classify_submit_after)
from app.config import Settings
from app.crawler import GuardContext
from app.guard import load_refuse_pack
from tests.characterization.harness import (Fixture, ScriptedPage, control,
                                            disposable_attestation, run_fixture)

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)

HOST = "https://vkpower-life.test"
BENEFICIARY_URL = f"{HOST}/life-insurance/apply/beneficiary"
SIGNATURE_URL = f"{HOST}/life-insurance/apply/signature"
CONFIRMATION_URL = f"{HOST}/life-insurance/apply/confirmation"
DASHBOARD_URL = f"{HOST}/portal/dashboard"

#: Verbatim from the proving ground's confirmation page.  Asserted against that
#: source below, so a page that changes shape BREAKS this proof rather than
#: quietly invalidating it.
CONFIRMATION_HEADING = "Application Submitted"
CONFIRMATION_BODY = ("Your life insurance application has been successfully "
                     "submitted and is now being processed.")
#: The three ways OFF the confirmation page.  Every one is clickable, none of
#: them continues the funnel, and clicking one is what used to record ``loop``.
CONFIRMATION_EXITS = ("Print Confirmation", "Go to Dashboard", "New Application")

_GROUNDS = (Path(__file__).resolve().parents[3] / "proving-grounds" / "vkpower-life"
            / "src" / "app" / "life-insurance" / "apply")
_CONFIRMATION_SOURCE = _GROUNDS / "confirmation" / "page.tsx"
_SIGNATURE_SOURCE = _GROUNDS / "signature" / "page.tsx"


# ═══════════════════════════════════════════════════════════════════════════
#  The application, transcribed
# ═══════════════════════════════════════════════════════════════════════════

def _confirmation_page(*, exits=CONFIRMATION_EXITS, url=CONFIRMATION_URL,
                       texts=None, statuses=None) -> ScriptedPage:
    """The far side: a success banner and a handful of ways to LEAVE."""
    page = ScriptedPage(
        url=url, title="Application Confirmation",
        controls=[control("button", name, tag="button") for name in exits],
        texts=list(texts if texts is not None
                   else [CONFIRMATION_HEADING, CONFIRMATION_BODY]),
        displayed_values=[
            {"label": "Confirmation Number", "selector": "#conf",
             "text": "CONF-20260815-A1B2C3"},
            {"label": "Application Number", "selector": "#appno",
             "text": "VKPL-20260815-D4E5F6"},
        ])
    if statuses is not None:
        page.statuses = list(statuses)
    return page


def m12_pages(*, exits=CONFIRMATION_EXITS, confirmation_texts=None,
              same_page: bool = False, statuses=None,
              exit_leads_back: bool = False) -> dict[str, ScriptedPage]:
    """The tail of the M1.2 funnel: beneficiary -> signature -> confirmation.

    ``same_page`` models the OTHER shape the product must handle — an SPA that
    re-renders the confirmation at the SAME url.  That is the only shape in
    which ``classify_submit_after`` returns ``confirmation`` rather than
    ``navigation`` (it checks the url first), so T-CF-02 is proved on it.

    NOTE the beneficiary step deliberately says "You will receive a confirmation
    email once submitted."  The word is on the page BEFORE anything happens, on
    every step of the funnel, and no step may confirm itself on it.
    """
    confirm_url = SIGNATURE_URL if same_page else CONFIRMATION_URL
    confirmation = _confirmation_page(exits=exits, url=confirm_url,
                                      texts=confirmation_texts, statuses=statuses)
    if exit_leads_back:
        # The exit walks OUT of the funnel and back to a page this journey has
        # already been through — the shape that produced `loop`.
        confirmation.transitions = {exits[0]: "beneficiary"}
    return {
        "beneficiary": ScriptedPage(
            url=BENEFICIARY_URL, title="Beneficiary Designation",
            controls=[
                control("textbox", "Beneficiary Full Name", tag="input",
                        kind="text", input_type="text"),
                control("textbox", "Relationship", tag="input", kind="text",
                        input_type="text"),
                control("button", "Continue", tag="button"),
            ],
            texts=["Designate your beneficiaries",
                   "You will receive a confirmation email once submitted."],
            transitions={"Continue": "signature"}),
        "signature": ScriptedPage(
            url=SIGNATURE_URL, title="Electronic Signature",
            controls=[
                control("textbox", "Type your full legal name", tag="input",
                        kind="text", input_type="text"),
                control("button", "Continue", tag="button"),
            ],
            texts=["Electronic Signature",
                   "You will receive a confirmation email once submitted."],
            transitions={"Continue": "confirmation"}),
        "confirmation": confirmation,
        "dashboard": ScriptedPage(
            url=DASHBOARD_URL, title="Dashboard",
            controls=[control("link", "Policies", href="/portal/policies")]),
    }


def crawl(tmp_path, monkeypatch, pages, *, start="beneficiary",
          target=BENEFICIARY_URL, **over):
    """Run the REAL Crawler over the modelled application."""
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "crawl_mode": "e2e", "wizard_enabled": True, "e2e_wizard_steps": 60,
        "guard_context": GuardContext(refuse_pack=_REFUSE_PACK,
                                      attestation=disposable_attestation()),
    }
    kwargs.update(over)
    fixture = Fixture(name="m14", pages=pages, start=start, target_url=target,
                      kwargs=kwargs)
    _text, digest = run_fixture(fixture, work, monkeypatch)
    return digest["coverage"]


def funnel_flow(cov):
    """The journey that walked the funnel — the deepest one recorded."""
    flows = cov["flows"]
    assert flows, "the crawl recorded no journeys at all"
    return max(flows, key=lambda f: f["step_count"])


# ═══════════════════════════════════════════════════════════════════════════
#  The fixture is tied to the real proving-ground application
# ═══════════════════════════════════════════════════════════════════════════

def test_the_fixture_matches_the_real_m12_proving_ground():
    """A model that has drifted from its subject proves nothing about it.

    If any of these fail, the M1.2 proving ground changed shape and this proof
    must be re-derived — which is the point of asserting it.
    """
    assert _CONFIRMATION_SOURCE.exists(), f"missing: {_CONFIRMATION_SOURCE}"
    src = _CONFIRMATION_SOURCE.read_text(encoding="utf-8")

    # The success declaration, and the fact that it is PLAIN TEXT: no
    # role=status, no aria-live. That is exactly why the transition-text rung
    # has to exist, and why a rule keyed on ARIA alone would not fire here.
    assert CONFIRMATION_HEADING in src
    assert "has been successfully submitted" in src
    assert 'role="status"' not in src and "aria-live" not in src

    # The navigation the confirmation page carries. Every one of these is a
    # live, enabled, clickable control on a page that is nonetheless the END of
    # the journey — which is the entire difficulty M1.4 addresses.
    for label in CONFIRMATION_EXITS:
        assert label in src, f"the confirmation page no longer offers {label!r}"

    # ...and the step that leads to it: a real navigation (router.push), so the
    # landing carries a NEW url and `classify_submit_after` reports `navigation`.
    sig = _SIGNATURE_SOURCE.read_text(encoding="utf-8")
    assert "router.push('/life-insurance/apply/confirmation/')" in sig


# ═══════════════════════════════════════════════════════════════════════════
#  T-CF-01 — THE ARCHITECTURE: a first-class completing terminal
# ═══════════════════════════════════════════════════════════════════════════

def test_TCF01_confirmation_is_a_first_class_terminal():
    assert flow_ledger.TERMINAL_CONFIRMATION == "confirmation"
    assert flow_ledger.TERMINAL_CONFIRMATION in flow_ledger.COMPLETING_TERMINALS


def test_TCF01_the_ledger_derives_completed_from_the_confirmation_terminal():
    """(1) A recognized confirmation produces ``completed=true``."""
    flow = flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url=BENEFICIARY_URL, entry_title="Beneficiary",
        steps=[{"fingerprint": "a", "url": BENEFICIARY_URL, "title": "Beneficiary",
                "fields_filled": 2, "fields_unfilled": 0}],
        terminal=flow_ledger.TERMINAL_CONFIRMATION, terminal_url=CONFIRMATION_URL,
        confirmation_rung=RUNG_TRANSITION_TEXT, confirmation_detail=CONFIRMATION_HEADING)
    assert flow["terminal"] == "confirmation"
    assert flow["completed"] is True
    # The EVIDENCE travels with the claim: which rung proved it and what the
    # application actually said.
    assert flow["confirmation_rung"] == RUNG_TRANSITION_TEXT
    assert flow["confirmation_detail"] == CONFIRMATION_HEADING


def test_TCF01_completion_is_still_derived_and_never_passed_in():
    """The load-bearing property M1.4 must not have loosened: no caller can
    assert completion, so the new terminal cannot become a back door."""
    assert "completed" not in inspect.signature(flow_ledger.build_flow).parameters
    src = inspect.getsource(flow_ledger.build_flow)
    assert '"completed": term in COMPLETING_TERMINALS' in src


def test_TCF01_confirmation_evidence_cannot_dress_up_another_terminal():
    """Passing a rung with a non-confirmation terminal records NOTHING. The
    evidence fields exist to justify the confirmation terminal, not to decorate
    a loop with the language of success."""
    flow = flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url=BENEFICIARY_URL, entry_title="B",
        steps=[], terminal=flow_ledger.TERMINAL_LOOP,
        confirmation_rung=RUNG_ARIA_STATUS, confirmation_detail="Success!")
    assert flow["completed"] is False
    assert "confirmation_rung" not in flow
    assert "confirmation_detail" not in flow


def test_TCF01_the_self_contradiction_tripwire_no_longer_fires_on_success():
    """``advance_contradicts_fills`` means "we filled fields and the app then
    refused to move" — a lead worth chasing. A journey that filled fields and
    reached a confirmation is the opposite of that, and a tripwire that cries
    wolf on success is one nobody reads."""
    flow = flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url=BENEFICIARY_URL, entry_title="B",
        steps=[{"fingerprint": "a", "url": BENEFICIARY_URL, "title": "B",
                "fields_filled": 6, "fields_unfilled": 0}],
        terminal=flow_ledger.TERMINAL_CONFIRMATION,
        confirmation_rung=RUNG_TRANSITION_TEXT)
    assert flow["advance_contradicts_fills"] is False


# ═══════════════════════════════════════════════════════════════════════════
#  T-CF-03 — TERMINAL PRECEDENCE, stated once and testable in isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_TCF03_confirmation_outranks_loop():
    """THE ESSENTIAL RULE. A trigger existed, the budget allowed it, and the
    click went nowhere — every input that used to spell ``loop``. The observed
    confirmation wins."""
    assert flow_ledger.resolve_walk_terminal(
        confirmation=True, nothing_to_click="", budget_left=True
    ) == flow_ledger.TERMINAL_CONFIRMATION
    # ...and with no confirmation, the SAME inputs still spell loop.
    assert flow_ledger.resolve_walk_terminal(
        confirmation=False, nothing_to_click="", budget_left=True
    ) == flow_ledger.TERMINAL_LOOP


def test_TCF03_confirmation_outranks_budget_exhaustion():
    """A funnel that reached its confirmation was not truncated, whatever the
    step counter says."""
    assert flow_ledger.resolve_walk_terminal(
        confirmation=True, budget_left=False
    ) == flow_ledger.TERMINAL_CONFIRMATION


def test_TCF03_an_operator_abort_outranks_everything():
    """"I stopped it" must never quietly become "it finished" — not even when a
    confirmation was on screen when the stop landed."""
    assert flow_ledger.resolve_walk_terminal(
        cancelled=True, confirmation=True, nothing_to_click="", budget_left=False
    ) == flow_ledger.TERMINAL_CANCELLED


@pytest.mark.parametrize("nothing_to_click", [
    flow_ledger.TERMINAL_SUBMIT_BOUNDARY,
    flow_ledger.TERMINAL_NO_ADVANCE,
    flow_ledger.TERMINAL_ORACLE_UNAVAILABLE,
])
def test_TCF03_the_nothing_to_click_verdicts_survive_unchanged(nothing_to_click):
    """(5) + (6) submit_boundary and no_advance still reach the ledger exactly
    as they always did, and oracle_unavailable is still not coverage."""
    assert flow_ledger.resolve_walk_terminal(
        nothing_to_click=nothing_to_click) == nothing_to_click


def test_TCF03_budget_exhaustion_is_still_not_completion():
    """(7) Budget termination remains incomplete."""
    terminal = flow_ledger.resolve_walk_terminal(budget_left=False)
    assert terminal == flow_ledger.TERMINAL_BUDGET
    assert terminal not in flow_ledger.COMPLETING_TERMINALS


def test_TCF03_the_precedence_function_is_not_a_back_door():
    """A caller may not hand it an arbitrary terminal — including a completing
    one — under the guise of "nothing left to click"."""
    for bogus in (flow_ledger.TERMINAL_CONFIRMATION,
                  flow_ledger.TERMINAL_SUBMIT_CROSSED, "made_up"):
        with pytest.raises(ValueError):
            flow_ledger.resolve_walk_terminal(nothing_to_click=bogus)


# ── The recognizer itself: generic, semantic, anti-fabricating ──────────────

def test_the_recognizer_needs_a_DECLARED_rung_not_a_navigation():
    """THE GREEN-WASH THIS CLOSES OFF. Every "Continue" in every wizard
    navigates. If a url change counted as a declaration of success, step one of
    a nine-step funnel would report itself complete."""
    assert RUNG_NAVIGATION not in DECLARED_CONFIRMATION_RUNGS
    assert is_confirmation_landing(
        outcome="navigation", rung=RUNG_NAVIGATION, changed=True) is False


@pytest.mark.parametrize("rung", [RUNG_ARIA_STATUS, RUNG_TRANSITION_TEXT, RUNG_DIALOG])
def test_the_recognizer_accepts_every_way_an_app_declares_success(rung):
    # 2026-08-27: a DETAIL is now required — every rung here asserts that the
    # APPLICATION declared success, and an empty detail is the application
    # saying nothing (see test_a_silent_dialog_declares_nothing.py, and OWASP
    # Juice Shop's welcome overlay, which ended a crawl at 3 pages by being
    # read as a completed journey). The rung must not do the declaring alone.
    said = "Your order was placed. Confirmation #4471."
    assert is_confirmation_landing(outcome="confirmation", rung=rung,
                                   changed=True, detail=said) is True
    assert is_confirmation_landing(outcome="navigation", rung=rung,
                                   changed=True, detail=said) is True


def test_the_recognizer_refuses_an_error():
    """An error may never be read as a confirmation, on any rung."""
    assert is_confirmation_landing(outcome="error", rung=RUNG_ARIA_STATUS,
                                   changed=True) is False


def test_the_recognizer_refuses_a_banner_on_a_page_that_did_not_move():
    """An inline "Saved successfully" toast is not the end of a journey. Without
    this conjunct a nine-step funnel would end at its first autosave."""
    assert is_confirmation_landing(outcome="confirmation", rung=RUNG_ARIA_STATUS,
                                   changed=False) is False


def test_the_recognizer_names_no_url_label_or_title():
    """GENERIC BY CONSTRUCTION. The predicate takes three arguments, none of
    which is a url, a control name, a page title or an application identity —
    so there is nowhere for a "Back to Dashboard" special case to live."""
    # ``detail`` joined the signature on 2026-08-27 and does NOT weaken this
    # property: it is the text the APPLICATION itself rendered, handed in by the
    # caller, not a url, a control name or an application identity. There is
    # still nowhere for a "Back to Dashboard" special case to live.
    params = set(inspect.signature(is_confirmation_landing).parameters)
    assert params == {"outcome", "rung", "changed", "detail"}
    src = inspect.getsource(is_confirmation_landing)
    for banned in ("dashboard", "http", "confirmation.", "/apply", "title"):
        assert banned not in src.lower().split('"""')[-1]


# ═══════════════════════════════════════════════════════════════════════════
#  T-CF-02 — INTEGRATION: OUTCOME_CONFIRMATION reaches build_flow()
# ═══════════════════════════════════════════════════════════════════════════

def test_TCF02_classify_submit_after_reports_a_same_page_confirmation():
    """The classifier half, in isolation. ``navigation`` is checked FIRST, so
    ``confirmation`` is the verdict exactly when the page did not move — which
    is why the SPA fixture below is the shape this task is proved on."""
    same = RawObservation(url_before=SIGNATURE_URL, url_after=SIGNATURE_URL,
                          confirmation_detail=CONFIRMATION_HEADING)
    assert classify_submit_after(same).outcome == OUTCOME_CONFIRMATION


def test_TCF02_the_walk_calls_the_confirmation_classifier_at_all(tmp_path, monkeypatch):
    """THE SEAM THAT DID NOT EXIST.

    ``emit.build_action_record`` runs ``classify_after``, which has no
    ``confirmation`` branch, so before M1.4 no walk could reach this classifier
    by any path. A spy over the walker's own reference proves the call is real
    and not merely importable — and that the verdict it produced is the one that
    ends up on the journey.
    """
    verdicts: list[str] = []
    real = walker_module.classify_submit_after

    def spy(observation):
        outcome = real(observation)
        verdicts.append(outcome.outcome)
        return outcome

    monkeypatch.setattr(walker_module, "classify_submit_after", spy)
    cov = crawl(tmp_path, monkeypatch, m12_pages(same_page=True))

    assert verdicts, "the walk never consulted classify_submit_after"
    assert OUTCOME_CONFIRMATION in verdicts, (
        f"the same-page confirmation was never classified as one: {verdicts}")
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True


def test_TCF02_the_verdict_survives_into_the_flow_artifact(tmp_path, monkeypatch):
    """(8) End to end, with nothing stubbed but the browser: the classifier's
    verdict is not merely computed, it DECIDES the terminal, and its evidence is
    written into the artifact a client reads."""
    cov = crawl(tmp_path, monkeypatch, m12_pages(same_page=True))
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True
    assert flow["confirmation_rung"] == RUNG_TRANSITION_TEXT
    assert CONFIRMATION_HEADING in flow["confirmation_detail"]


def test_TCF02_an_application_declared_status_region_is_honoured(tmp_path, monkeypatch):
    """The strongest same-page rung: the app published a ``role=status`` region.
    Threaded through the same path, so no shape of declaration is left out."""
    cov = crawl(tmp_path, monkeypatch,
                m12_pages(confirmation_texts=[], statuses=[CONFIRMATION_HEADING]))
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["confirmation_rung"] == RUNG_ARIA_STATUS


# ═══════════════════════════════════════════════════════════════════════════
#  T-CF-04 — THE M1.2 PROVING-GROUND JOURNEY, END TO END
# ═══════════════════════════════════════════════════════════════════════════

def test_TCF04_the_m12_journey_terminates_as_a_completed_confirmation(
        tmp_path, monkeypatch):
    """(9) THE MILESTONE CLAIM. The funnel is walked, the application says
    "Application Submitted", and the journey is recorded as covered — at the
    confirmation page, not one hop past it."""
    cov = crawl(tmp_path, monkeypatch, m12_pages())
    flow = funnel_flow(cov)

    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True
    # ...and NOT the two ways it used to end.
    assert flow["terminal"] != flow_ledger.TERMINAL_LOOP
    assert flow["terminal"] != flow_ledger.TERMINAL_BUDGET
    # The walk stopped ON the confirmation, not on whatever "Go to Dashboard"
    # leads to. A journey whose terminal_url is the dashboard has walked out of
    # the funnel it was recording.
    assert flow["terminal_url"] == CONFIRMATION_URL
    assert [s["url"] for s in flow["steps"]] == [
        BENEFICIARY_URL, SIGNATURE_URL, CONFIRMATION_URL]
    # The outcome the funnel PRODUCED is still captured on that terminal step.
    labels = {v["label"] for v in flow["outcome_values"]}
    assert "Confirmation Number" in labels


def test_TCF04_back_to_dashboard_does_not_stop_it_completing(tmp_path, monkeypatch):
    """(2) The literal control from the bug report, on the page, live and
    clickable, next to a bare "Continue" the advance tiers WILL pick."""
    pages = m12_pages(exits=("Back to Dashboard", "Continue", "Print Confirmation"))
    pages["confirmation"].transitions = {"Back to Dashboard": "dashboard",
                                         "Continue": "dashboard"}
    cov = crawl(tmp_path, monkeypatch, pages)
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True
    assert flow["terminal_url"] == CONFIRMATION_URL


def test_TCF04_clickable_navigation_does_not_become_a_loop(tmp_path, monkeypatch):
    """(3) THE EXACT REGRESSION. The confirmation offers a "Continue" that goes
    nowhere; the walk clicks it, the page does not change, and every input that
    used to spell ``loop`` is present. The recognized confirmation wins."""
    pages = m12_pages(exits=("Continue", "Print Confirmation", "New Application"))
    cov = crawl(tmp_path, monkeypatch, pages)
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True


def test_TCF04_an_exit_leading_back_into_the_funnel_is_not_a_loop(
        tmp_path, monkeypatch):
    """The nastiest shape: the only exit walks BACK to a page this journey has
    already been through — the textbook definition of a loop, on a page the
    application has already declared a success."""
    cov = crawl(tmp_path, monkeypatch, m12_pages(exit_leads_back=True))
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_CONFIRMATION
    assert flow["completed"] is True


def test_TCF04_the_walk_does_not_wander_off_the_confirmation(tmp_path, monkeypatch):
    """A confirmation whose "Continue" really does navigate — off the funnel, to
    the dashboard. Following it recorded a FOURTH step and moved the journey's
    terminal to a page that has nothing to do with the funnel. A terminal is a
    terminal: the walk stops."""
    pages = m12_pages(exits=("Continue", "Print Confirmation"))
    pages["confirmation"].transitions = {"Continue": "dashboard"}
    cov = crawl(tmp_path, monkeypatch, pages)
    flow = funnel_flow(cov)
    assert flow["step_count"] == 3
    assert flow["terminal_url"] == CONFIRMATION_URL
    assert DASHBOARD_URL not in [s["url"] for s in flow["steps"]]


# ═══════════════════════════════════════════════════════════════════════════
#  PRESERVED SEMANTICS — the endings M1.4 may not have touched
# ═══════════════════════════════════════════════════════════════════════════

def test_a_genuine_loop_is_still_not_completed(tmp_path, monkeypatch):
    """(4) No success declaration anywhere, and the advance walks back to a page
    already visited. Loop detection is untouched.

    The last page deliberately carries NO success vocabulary, and the funnel's
    earlier steps deliberately DO ("You will receive a confirmation email once
    submitted") — so a classifier that scored the after-state alone, or that
    diffed only against the immediately previous page, would score this journey
    as complete. Both did, and this fixture is why neither does now.
    """
    pages = m12_pages(confirmation_texts=["Your details", "Next steps"])
    pages["confirmation"].controls = [control("button", "Continue", tag="button")]
    pages["confirmation"].transitions = {"Continue": "beneficiary"}
    cov = crawl(tmp_path, monkeypatch, pages)
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_LOOP
    assert flow["completed"] is False


def test_submit_boundary_completion_is_intact(tmp_path, monkeypatch):
    """(5) A funnel that ends in front of a commit control it was not
    authorised to cross is still a covered journey."""
    pages = m12_pages()
    pages["signature"] = ScriptedPage(
        url=SIGNATURE_URL, title="Electronic Signature",
        controls=[control("textbox", "Type your full legal name", tag="input",
                          kind="text", input_type="text"),
                  control("button", "Sign & Submit Application", tag="button")],
        texts=["Electronic Signature"])
    cov = crawl(tmp_path, monkeypatch, pages)
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_SUBMIT_BOUNDARY
    assert flow["completed"] is True


def test_no_advance_completion_is_intact(tmp_path, monkeypatch):
    """(6) A last page with nothing to advance and NOTHING declared still ends
    as ``no_advance`` — the confirmation terminal did not swallow it."""
    cov = crawl(tmp_path, monkeypatch,
                m12_pages(confirmation_texts=["Your details", "Next steps"]))
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_NO_ADVANCE
    assert flow["completed"] is True


def test_budget_termination_is_still_incomplete(tmp_path, monkeypatch):
    """(7) A truncated walk is not a covered journey."""
    cov = crawl(tmp_path, monkeypatch, m12_pages(), e2e_wizard_steps=1)
    flow = funnel_flow(cov)
    assert flow["terminal"] == flow_ledger.TERMINAL_BUDGET
    assert flow["completed"] is False


def test_ordinary_navigation_is_not_a_confirmation(tmp_path, monkeypatch):
    """Every wizard step navigates. None of them may complete a journey: the
    signature step is reached by a plain "Continue" and the walk carries on."""
    cov = crawl(tmp_path, monkeypatch,
                m12_pages(confirmation_texts=["Your details", "Next steps"]))
    flow = funnel_flow(cov)
    assert flow["step_count"] == 3
    assert flow["terminal"] != flow_ledger.TERMINAL_CONFIRMATION


def test_success_wording_present_before_the_click_cannot_confirm(tmp_path, monkeypatch):
    """THE ANTI-FABRICATION PROPERTY, end to end. Every step of this funnel says
    "You will receive a confirmation email once submitted." The word is on the
    page before anything has happened, and no step confirms itself on it."""
    cov = crawl(tmp_path, monkeypatch, m12_pages(
        confirmation_texts=["You will receive a confirmation email once submitted."]))
    flow = funnel_flow(cov)
    assert flow["terminal"] != flow_ledger.TERMINAL_CONFIRMATION


def test_recognising_a_confirmation_does_not_change_the_action_record():
    """CONTAINMENT. The walk injects the derived ``confirmation_detail`` into the
    observation it hands ``build_action_record``, and that must remain invisible
    to the manifest: ``classify_after`` (which the EXPLORE-phase record uses) has
    no confirmation branch and never reads the field.

    This is why the characterization goldens did not move, and it is deliberate.
    ``confirmation`` is a SUBMIT-tier outcome that earns a baseline visual frame;
    minting it from an ordinary explore click would hand downstream credit to
    every advance that happened to land on a success page. The recognition feeds
    the flow TERMINAL and nothing else.
    """
    plain = RawObservation(url_before=SIGNATURE_URL, url_after=SIGNATURE_URL)
    injected = RawObservation(url_before=SIGNATURE_URL, url_after=SIGNATURE_URL,
                              confirmation_detail=CONFIRMATION_HEADING)
    assert classify_after(plain) == classify_after(injected)
    # ...and the two classifiers genuinely disagree here, which is the whole
    # reason the walk had to call the other one.
    assert classify_submit_after(injected).outcome == OUTCOME_CONFIRMATION
    assert classify_after(injected).outcome != OUTCOME_CONFIRMATION


def test_a_button_label_is_not_a_declaration():
    """A BUTTON IS AN OFFER, NOT A STATEMENT.

    Measured on the M1.2 proving ground: its confirmation page offers a "Print
    Confirmation" button, and half the insurance forms in existence carry a
    "Policy Number" or "Claim Number" FIELD LABEL. Every one of those matches
    the success vocabulary while declaring nothing whatsoever, and a page whose
    only match is its own control label has said nothing about what happened.
    """
    before = ["Review your application"]
    after = ["Print Confirmation", "Go to Dashboard"]
    detail, rung = confirmation_transition(
        before, after, control_names=["Print Confirmation", "Go to Dashboard"])
    assert (detail, rung) == ("", "")
    # ...and the guard is narrow: the banner BESIDE the button still lands.
    detail, rung = confirmation_transition(
        before, [CONFIRMATION_HEADING] + after,
        control_names=["Print Confirmation", "Go to Dashboard"])
    assert detail == CONFIRMATION_HEADING
    assert rung == RUNG_TRANSITION_TEXT


def test_a_field_label_carrying_the_vocabulary_is_not_a_declaration():
    """The insurance-form case specifically: an application step that asks for
    an existing "Policy Number" must not read as a completed journey."""
    detail, rung = confirmation_transition(
        ["Do you have existing coverage?"],
        ["Policy Number", "Insurer Name"],
        control_names=["Policy Number", "Insurer Name"])
    assert (detail, rung) == ("", "")


def test_the_button_label_guard_reaches_the_live_walk(tmp_path, monkeypatch):
    """End to end: a confirmation page whose ONLY success-shaped text is its own
    button label completes nothing. Without the guard the walk would have read
    "Print Confirmation" as the application declaring success."""
    cov = crawl(tmp_path, monkeypatch, m12_pages(
        exits=("Print Confirmation", "Continue"),
        confirmation_texts=["Your details", "Next steps"]))
    flow = funnel_flow(cov)
    assert flow["terminal"] != flow_ledger.TERMINAL_CONFIRMATION
    assert "confirmation_rung" not in flow
