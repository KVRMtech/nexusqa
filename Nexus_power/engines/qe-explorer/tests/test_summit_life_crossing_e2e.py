"""T-AC-07 — THE SUMMIT-LIFE-CARRIER PROOF, END TO END, THROUGH THE REAL CRAWLER.

    Walk → reach the submit boundary → request approval → approval granted →
    submit exactly once → reach the confirmation → store the outcome milestone →
    journey completed → replay → no second submission.

WHAT IS REAL HERE.  The frontier, the budget, the guard, the refuse pack, the
inventory, the fingerprinter, the form engine, the wizard walker, the flow
ledger, the coverage builder, the boundary model, the approval registry, the
crossing ledger and the milestone are all the production objects.  Only the
BROWSER is scripted, through the same :class:`app.browser.BrowserPort` the
Playwright adapter implements.

WHY THE APPLICATION IS MODELLED RATHER THAN CONTACTED.  A live crawl cannot be
a gate: wall-clock timestamps, network jitter and the target's own state move
between runs, so two live crawls diff as weather.  The fixture below is
transcribed from the real source of the target application, and the transcription
is itself asserted against that source by
``test_the_fixture_matches_the_real_summit_life_application`` — so a page that
changes shape breaks this proof rather than quietly invalidating it.

    Nexus_power/proving-grounds/summit-life-carrier/src/app/(platform)/
        underwriting/new-business/new-application/page.tsx

Three facts from that file drive everything:

  1. FIVE STEPS, ONE URL.  ``useState`` step 0-4 re-renders in place; the URL
     never changes and there is no route transition to observe.
  2. THE COMMIT CONTROL IS "Submit Application", and it is NOT flagged by the
     refuse pack (it is a button, so no url rule applies and no verb matches).
     It is approvable by SHAPE, which is the rung the boundary model adds.
  3. THE SUBMIT DOES NOT NAVIGATE.  ``handleSubmit`` runs 29 in-page POSTs via
     ``executeFlow`` and renders ``ApiCallTracker``; the success banner is a
     plain ``div`` — no route change, no dialog, no ARIA role.  Before A4.3 this
     shape could not produce ``confirmed=True`` by any path, because
     ``confirmation_detail`` had no producer anywhere in ``app/``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import flow_ledger
from tests.characterization.harness import (Fixture, ScriptedPage, control,
                                            disposable_attestation, run_fixture)
from app.crawler import GuardContext
from app.guard import load_refuse_pack
from app.config import Settings

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)

HOST = "https://summitlife-admin.test"
WIZARD_URL = f"{HOST}/underwriting/new-business/new-application"

#: The commit control, verbatim from the application source.
SUBMIT_LABEL = "Submit Application"
#: The success banner text, verbatim from ``api-call-tracker.tsx``. It is an
#: undecorated ``div`` — no role=status, no aria-live — which is exactly why the
#: transition rung exists.
SUCCESS_BANNER = "All 29 API calls completed successfully"

_APP_SOURCE = (Path(__file__).resolve().parents[3] / "proving-grounds"
               / "summit-life-carrier" / "src" / "app" / "(platform)"
               / "underwriting" / "new-business" / "new-application" / "page.tsx")
_TRACKER_SOURCE = (Path(__file__).resolve().parents[3] / "proving-grounds"
                   / "summit-life-carrier" / "src" / "components" / "domain"
                   / "api-call-tracker.tsx")


# ═══════════════════════════════════════════════════════════════════════════
#  The application, transcribed
# ═══════════════════════════════════════════════════════════════════════════

_STEPS = [
    ("Applicant", [("First Name", "text"), ("Last Name", "text"),
                   ("Date of Birth", "date"), ("Social Security Number", "text"),
                   ("Email Address", "text"), ("Phone Number", "text")]),
    ("Address & Employment", [("Street Address", "text"), ("City", "text"),
                              ("ZIP Code", "text"), ("Occupation", "text"),
                              ("Employer", "text"), ("Annual Income", "number")]),
    ("Coverage Details", [("Face Amount ($)", "number")]),
    ("Health Information", [("Primary Physician", "text"),
                            ("Last Physical Exam", "date")]),
]


def _fields(spec):
    out = []
    for label, kind in spec:
        input_type = kind if kind in ("date", "number") else "text"
        out.append(control("textbox", label, tag="input", kind="text",
                           input_type=input_type))
    return out


def summit_life_pages(*, confirms: bool = True) -> dict[str, ScriptedPage]:
    """The five-step new-business application, one URL throughout.

    ``confirms=False`` is the NEGATIVE CONTROL: the identical funnel whose
    submit produces no observable change at all. It must cross once and report
    the journey as NOT completed — a fixture that only models success proves
    nothing about a system whose whole job is to refuse to green-wash.
    """
    pages: dict[str, ScriptedPage] = {}
    for index, (heading, spec) in enumerate(_STEPS):
        pages[f"step{index}"] = ScriptedPage(
            url=WIZARD_URL, title="Submit New Application",
            controls=[
                control("button", "Back", tag="button"),
                *_fields(spec),
                control("button", "Continue", tag="button"),
            ],
            texts=[heading, "Submit the application to begin processing"],
            transitions={"Continue": f"step{index + 1}"},
        )
    # Step 5 — Review & Submit. The ONLY step that offers the commit control.
    pages["step4"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=[
            control("button", "Back", tag="button"),
            control("button", SUBMIT_LABEL, tag="button"),
        ],
        texts=["Review & Submit",
               "Review all information before submitting the application"],
        displayed_values=[{"label": "Face Amount", "selector": "#fa",
                           "text": "$500,000"}],
        transitions={SUBMIT_LABEL: "submitted" if confirms else "step4"},
    )
    # The far side: same URL, the form gone, the tracker's success banner up.
    pages["submitted"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=[control("button", "Back", tag="button")],
        texts=["Review & Submit", SUCCESS_BANNER, "29 API calls across 7 phases"],
        displayed_values=[{"label": "Face Amount", "selector": "#fa",
                           "text": "$500,000"}],
    )
    return pages


def crawl(tmp_path, monkeypatch, *, grants=(), approvals=(), confirms=True,
          attested=True):
    """Run the REAL Crawler over the modelled application."""
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True, exist_ok=True)
    guard = GuardContext(
        refuse_pack=_REFUSE_PACK,
        attestation=disposable_attestation() if attested else None)
    fixture = Fixture(
        name="summit_life", pages=summit_life_pages(confirms=confirms),
        start="step0", target_url=WIZARD_URL,
        kwargs={"crawl_mode": "e2e", "wizard_enabled": True,
                "e2e_wizard_steps": 60, "guard_context": guard,
                "boundary_approvals": list(grants),
                "submit_approvals": list(approvals)},
    )
    text, digest = run_fixture(fixture, work, monkeypatch)
    body = text.split("===SUMMARY===")[0]
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    return records, digest["coverage"]


# ═══════════════════════════════════════════════════════════════════════════
#  The fixture is tied to the real application
# ═══════════════════════════════════════════════════════════════════════════

def test_the_fixture_matches_the_real_summit_life_application():
    """A model that has drifted from its subject proves nothing about it.

    If any of these fail, the target application changed shape and this whole
    proof must be re-derived — which is the point of asserting it.
    """
    assert _APP_SOURCE.exists(), f"target application source missing: {_APP_SOURCE}"
    src = _APP_SOURCE.read_text(encoding="utf-8")

    assert "'Review & Submit'" in src, "the fifth step is no longer Review & Submit"
    assert src.count("step === ") >= 5 or "setStep" in src
    # The commit control, and the fact that it is the ONLY control on the last
    # step (step < 4 renders Continue; step === 4 renders Submit Application).
    assert f">{{submitting ? 'Submitting...' : '{SUBMIT_LABEL}'}}" in src
    assert "{step < 4 && (" in src and "{step === 4 && (" in src
    # It does NOT navigate: handleSubmit runs an API flow and sets local state.
    assert "await executeFlow(steps, token, setApiResults);" in src
    assert "router.push" not in src.split("const handleSubmit")[1].split("};")[0]

    tracker = _TRACKER_SOURCE.read_text(encoding="utf-8")
    assert "API calls completed successfully" in tracker
    # The banner carries NO aria role — which is the whole reason the transition
    # rung had to exist. If this ever gains role="status", the stronger rung
    # takes over and this assertion should be updated, not deleted.
    banner = tracker.split("{/* Success banner */}")[1][:900]
    assert 'role="status"' not in banner and 'aria-live' not in banner


def test_the_commit_control_is_not_flagged_by_the_refuse_pack():
    """Pinned because the whole boundary model turns on it: "Submit
    Application" on a BUTTON carries no irreversible verb, so a model keyed on
    ``danger`` alone would let the walk click it unapproved."""
    from app.inventory import classify_control_danger
    danger, _rule, _sev = classify_control_danger(
        SUBMIT_LABEL, "button", "button", _REFUSE_PACK, "")
    assert danger is False


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 1 — walk, reach the boundary, STOP, and ask
# ═══════════════════════════════════════════════════════════════════════════

def test_with_no_approval_the_walk_stops_at_the_boundary(tmp_path, monkeypatch):
    """The behaviour that was already correct, re-proved so the milestone can
    never be accused of loosening it."""
    _records, cov = crawl(tmp_path, monkeypatch)
    assert cov["boundaries_crossed"] == 0
    assert cov["outcome_milestones"] == []
    assert cov["journeys_completed"] == 0
    assert cov["forms_submitted"] == 0


def test_the_walk_reaches_the_fifth_step(tmp_path, monkeypatch):
    _records, cov = crawl(tmp_path, monkeypatch)
    flow = max(cov["flows"], key=lambda f: f["step_count"])
    assert flow["step_count"] >= 5, (
        f"the funnel is five steps; the walk covered {flow['step_count']}")
    assert flow["terminal"] == flow_ledger.TERMINAL_SUBMIT_BOUNDARY
    assert flow["completed"] is True


def test_the_boundary_is_OFFERED_so_it_can_be_approved(tmp_path, monkeypatch):
    """THE DEADLOCK, BROKEN.

    An approval is picked from a prior crawl's coverage. Before A4.3 both
    producers of that list dropped the controls that need approving, so the
    operator was asked for an approval the product gave them no way to grant.
    """
    _records, cov = crawl(tmp_path, monkeypatch)
    labels = [row["label"] for row in cov["approvable_boundary"]]
    assert SUBMIT_LABEL in labels
    row = next(r for r in cov["approvable_boundary"] if r["label"] == SUBMIT_LABEL)
    assert row["url"] == WIZARD_URL
    assert row["reason"] == "commit_shaped_label"
    assert row["boundary_key"].startswith("bnd_")
    # T-AC-01: it is in the approvable list and in NO other list.
    assert SUBMIT_LABEL not in cov["submit_candidates"]


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 2 — approval granted, ONE crossing, a real confirmation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def approved_crawl(tmp_path, monkeypatch):
    """The grant an operator would issue from the row above, and its crawl."""
    grants = [{"control": SUBMIT_LABEL, "url": WIZARD_URL, "max_crossings": 1,
               "approved_by": "operator@nexus.test",
               "approved_at": "2026-08-18T00:00:00Z"}]
    return crawl(tmp_path, monkeypatch, grants=grants)


def test_the_approved_boundary_is_crossed_exactly_once(approved_crawl):
    _records, cov = approved_crawl
    crossed = [r for r in cov["boundary_crossings"] if r["status"] == "crossed"]
    assert len(crossed) == 1, f"expected ONE crossing, got {cov['boundary_crossings']}"
    assert crossed[0]["control_name"] == SUBMIT_LABEL
    assert cov["boundaries_crossed"] == 1


def test_the_crossing_ran_under_the_operators_own_grant(approved_crawl):
    _records, cov = approved_crawl
    milestone = cov["outcome_milestones"][0]
    assert milestone["approval_id"].startswith("apr_")
    assert milestone["grant"]["control"] == SUBMIT_LABEL
    assert milestone["grant"]["approved_by"] == "operator@nexus.test"
    assert milestone["grant"]["max_crossings"] == 1
    assert milestone["attestation_env_kind"] == "disposable"
    assert milestone["refuse_pack_version"] == _REFUSE_PACK.version
    assert milestone["guard_rule_id"], "the guard's verdict rides on the evidence"


def test_the_confirmation_page_is_reached_and_verified(approved_crawl):
    """T-AC-04. The application does not navigate and opens no dialog, so this
    is precisely the shape that was unverifiable before the milestone."""
    _records, cov = approved_crawl
    milestone = cov["outcome_milestones"][0]
    assert milestone["outcome"] == "confirmation"
    assert milestone["confirmation_rung"] == "transition_text"
    assert SUCCESS_BANNER in milestone["confirmation_detail"]
    assert milestone["navigated"] is False
    assert milestone["dom_digest_before"] != milestone["dom_digest_after"]
    assert milestone["verified"] is True


def test_the_outcome_milestone_carries_the_full_evidence(approved_crawl):
    """T-AC-03: every observation the crossing made, stored."""
    _records, cov = approved_crawl
    m = cov["outcome_milestones"][0]
    for key in ("milestone_id", "crossing_id", "approval_id", "boundary_key",
                "control_name", "url_before", "url_after", "outcome",
                "confirmation_detail", "confirmation_rung",
                "dom_digest_before", "dom_digest_after",
                "screenshot_before", "screenshot_after",
                "attestation_env_kind", "attestation_attributed_to",
                "refuse_pack_version", "guard_rule_id",
                "clicked_at_ms", "observed_at_ms", "latency_ms", "verified"):
        assert key in m, f"outcome milestone is missing {key}"
    assert m["screenshot_before"].endswith(".png")
    assert m["screenshot_after"].endswith(".png")
    assert m["screenshot_before"] != m["screenshot_after"]
    assert m["observed_at_ms"] >= m["clicked_at_ms"]


def test_the_milestone_is_in_the_manifest(approved_crawl):
    records, _cov = approved_crawl
    rows = [r for r in records if r.get("type") == "outcome_milestone"]
    assert len(rows) == 1
    assert rows[0]["control_name"] == SUBMIT_LABEL
    assert rows[0]["verified"] is True


def test_the_journey_is_reported_as_completed_end_to_end(approved_crawl):
    """THE PRODUCT CLAIM. One journey, completed, under explicit approval."""
    _records, cov = approved_crawl
    assert cov["journeys_completed"] == 1
    journeys = [f for f in cov["flows"] if f.get("journey_completed")]
    assert len(journeys) == 1
    journey = journeys[0]
    assert journey["terminal"] == flow_ledger.TERMINAL_SUBMIT_CROSSED
    assert journey["outcome_milestone"]["verified"] is True
    assert cov["flow_summary"]["journeys_completed"] == 1
    assert cov["flow_summary"]["boundaries_crossed"] == 1


def test_the_summary_states_the_claim_in_words(approved_crawl):
    _records, cov = approved_crawl
    assert "1 journey(s) completed end-to-end through an approved crossing" in \
        cov["summary"]


# ═══════════════════════════════════════════════════════════════════════════
#  STAGE 3 — REPLAY: the second traversal must not submit
# ═══════════════════════════════════════════════════════════════════════════

def test_a_replay_produces_the_identical_crossing(tmp_path, monkeypatch):
    """DETERMINISTIC REPLAY. Same application, same grant, same identities —
    the same one crossing, keyed the same way, resolving the same approval."""
    grants = [{"control": SUBMIT_LABEL, "url": WIZARD_URL}]
    _a, cov_a = crawl(tmp_path / "run-a", monkeypatch, grants=grants)
    _b, cov_b = crawl(tmp_path / "run-b", monkeypatch, grants=grants)

    def signature(cov):
        m = cov["outcome_milestones"][0]
        return (m["approval_id"], m["boundary_key"], m["crossing_id"],
                m["control_name"], m["outcome"], m["confirmation_rung"],
                m["confirmation_detail"], m["dom_digest_before"],
                m["dom_digest_after"], m["verified"])

    assert signature(cov_a) == signature(cov_b)
    assert cov_a["boundaries_crossed"] == cov_b["boundaries_crossed"] == 1


def test_the_second_traversal_within_one_crawl_never_resubmits(tmp_path, monkeypatch):
    """T-AC-07's replay leg, at the level that matters: the SAME crawl walking
    the SAME funnel again must not fire a second irreversible click.

    The crawl below is given a step budget that lets the walker re-enter the
    funnel, and the manifest is then read for submit ACTIONS — the ground truth
    of how many times the button was pressed, independent of any counter.
    """
    grants = [{"control": SUBMIT_LABEL, "url": WIZARD_URL}]
    records, cov = crawl(tmp_path, monkeypatch, grants=grants)
    submit_actions = [
        a for r in records if r.get("type") == "page_state"
        for a in (r.get("actions") or [])
        if a.get("verb") == "submit" and a.get("target_label") == SUBMIT_LABEL
    ]
    assert len(submit_actions) == 1, (
        f"the commit button was pressed {len(submit_actions)} times")
    assert cov["forms_submitted"] == 1
    assert len(cov["outcome_milestones"]) == 1


def test_a_grant_for_a_different_control_crosses_nothing(tmp_path, monkeypatch):
    """T-AC-02 on the real funnel: approval for one control cannot authorise
    another. "Continue" is walked by the wizard, not crossed; the boundary
    stays shut."""
    _records, cov = crawl(
        tmp_path, monkeypatch, grants=[{"control": "Bind Coverage"}])
    assert cov["boundaries_crossed"] == 0
    assert cov["journeys_completed"] == 0


def test_a_grant_scoped_to_another_page_crosses_nothing(tmp_path, monkeypatch):
    _records, cov = crawl(
        tmp_path, monkeypatch,
        grants=[{"control": SUBMIT_LABEL, "url": f"{HOST}/claims/new-fnol"}])
    assert cov["boundaries_crossed"] == 0
    assert cov["journeys_completed"] == 0


def test_a_grant_on_an_unattested_environment_crosses_nothing(tmp_path, monkeypatch):
    _records, cov = crawl(
        tmp_path, monkeypatch, attested=False,
        grants=[{"control": SUBMIT_LABEL, "url": WIZARD_URL}])
    assert cov["boundaries_crossed"] == 0
    assert cov["journeys_completed"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  THE NEGATIVE CONTROL — a crossing that lands nowhere is not a journey
# ═══════════════════════════════════════════════════════════════════════════

def test_a_submit_that_produces_nothing_is_crossed_but_not_completed(
        tmp_path, monkeypatch):
    """The same funnel, the same approval, an application that does not answer.

    This is the test that makes the positive one mean something: the pipeline
    can produce ``journeys_completed == 1``, and here it must produce 0 while
    still honestly recording that the irreversible click DID happen.
    """
    grants = [{"control": SUBMIT_LABEL, "url": WIZARD_URL}]
    _records, cov = crawl(tmp_path, monkeypatch, grants=grants, confirms=False)
    assert cov["boundaries_crossed"] == 1, "the click must still be recorded"
    assert cov["journeys_completed"] == 0
    milestone = cov["outcome_milestones"][0]
    assert milestone["verified"] is False
    assert milestone["confirmation_rung"] == ""
    assert not any(f.get("journey_completed") for f in cov["flows"])
