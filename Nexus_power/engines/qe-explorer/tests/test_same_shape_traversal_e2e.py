"""T-SI-04..06 — the same-shape wizard driven through the REAL Crawler.

The unit tests beside this one prove the identity layer in isolation.  These
prove the thing that actually matters: that a twenty-question, one-URL
questionnaire is walked from question 1 to question 20 by the production
``Crawler``, that the flow ledger says so, and that the depth it reports can be
told apart from a traversal that was merely cut off.

Every fixture here is scripted through the characterization harness, so these
run in milliseconds with no browser and no network.
"""
from __future__ import annotations

import json

import pytest

from app.budget import Budget
from tests.characterization.harness import (Fixture, ScriptedPage, control,
                                            run_fixture)

HOST = "https://app.char"
WIZARD_URL = f"{HOST}/apply/health"
SUMMARY_URL = f"{HOST}/apply/summary"
N_QUESTIONS = 20


def _questionnaire(n: int = N_QUESTIONS, *, distinct: bool = True) -> dict:
    """An ``n``-question wizard that serves EVERY step from ONE URL with the
    SAME three controls — the shape that collapsed to a single fingerprint.

    ``distinct=False`` reuses one declared group across all steps, so the steps
    become genuinely indistinguishable by any signal available.  That is the
    negative control: a fix that manufactures distinctness (from a step counter,
    say) passes the positive case and fails this one.
    """
    pages: dict[str, ScriptedPage] = {}
    for i in range(1, n + 1):
        group = f"name:doc:q{i:02d}" if distinct else "name:doc:answer"
        pages[f"q{i:02d}"] = ScriptedPage(
            url=WIZARD_URL, title="Health Questionnaire",
            controls=[
                control("radio", "Yes", tag="input", input_type="radio",
                        kind="radio", group_key=group),
                control("radio", "No", tag="input", input_type="radio",
                        kind="radio", group_key=group),
                control("button", "Continue", tag="button"),
            ],
            transitions={"Continue": (f"q{i+1:02d}" if i < n else "summary")},
        )
    pages["summary"] = ScriptedPage(
        url=SUMMARY_URL, title="Summary",
        controls=[control("link", "Back Home", href="/apply/health")],
        displayed_values=[{"label": "Premium", "selector": "#p", "text": "$42.00"}],
    )
    return pages


def _crawl(pages: dict, tmp_path, monkeypatch, **kwargs) -> tuple[list, dict]:
    """Run the real Crawler over ``pages``; return (manifest records, coverage)."""
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True)
    crawl_kwargs = {"crawl_mode": "e2e", "wizard_enabled": True,
                    "e2e_wizard_steps": 60}
    crawl_kwargs.update(kwargs)
    fixture = Fixture(name="same_shape", pages=pages, start="q01",
                      target_url=WIZARD_URL, kwargs=crawl_kwargs)
    text, digest = run_fixture(fixture, work, monkeypatch)
    body = text.split("===SUMMARY===")[0]
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    return records, digest["coverage"]


# ─── T-SI-05 · twenty steps, twenty identities, twenty traversals ────────────

def test_twenty_step_questionnaire_is_walked_end_to_end(tmp_path, monkeypatch):
    records, coverage = _crawl(_questionnaire(), tmp_path, monkeypatch)

    states = [r for r in records if r.get("type") == "page_state"]
    ids = {r["state_id"] for r in states}
    # 20 questions + the summary the twentieth Continue leads to.
    assert len(ids) == N_QUESTIONS + 1, "states collapsed or fragmented"

    assert len(coverage["flows"]) == 1, "the funnel must be ONE journey"
    flow = coverage["flows"][0]
    assert flow["step_count"] == N_QUESTIONS + 1
    assert len({s["fingerprint"] for s in flow["steps"]}) == N_QUESTIONS + 1
    # Walked to a genuine end, not stopped by a budget.
    assert flow["terminal"] == "no_advance"
    assert flow["completed"] is True


def test_every_question_is_recorded_as_its_own_decision_point(tmp_path, monkeypatch):
    """Traversing is only half of it — the twenty questions have to survive INTO
    the evidence as twenty questions, which is what the catalogue is built from."""
    _records, coverage = _crawl(_questionnaire(), tmp_path, monkeypatch)
    group_ids = {
        dp.get("group_id")
        for step in coverage["flows"][0]["steps"]
        for dp in step.get("decision_points") or []
        if dp.get("group_id")
    }
    assert len(group_ids) == N_QUESTIONS


def test_the_walk_is_reproducible_across_runs(tmp_path, monkeypatch):
    """DETERMINISM: same application, same identities, run after run."""
    a, _ = _crawl(_questionnaire(), tmp_path / "a", monkeypatch)
    b, _ = _crawl(_questionnaire(), tmp_path / "b", monkeypatch)
    ids_a = [r["state_id"] for r in a if r.get("type") == "page_state"]
    ids_b = [r["state_id"] for r in b if r.get("type") == "page_state"]
    assert ids_a == ids_b


# ─── The negative control: distinctness is never manufactured ────────────────

def test_indistinguishable_steps_stop_honestly(tmp_path, monkeypatch):
    """Twenty steps that NOTHING can tell apart must be reported as one state
    and a loop — not as twenty steps of progress.

    This is the test a counter-based "fix" fails. It is the whole reason the
    step ordinal is not an identity input for the walk.
    """
    _records, coverage = _crawl(_questionnaire(distinct=False),
                                tmp_path, monkeypatch)
    flow = coverage["flows"][0]
    assert flow["step_count"] == 1
    assert flow["terminal"] == "loop"
    assert flow["completed"] is False


# ─── T-SI-04 · the walk is bounded by the WIZARD budget, not by max_depth ────

def test_a_deep_wizard_is_not_truncated_by_crawl_depth(tmp_path, monkeypatch):
    """``max_depth`` bounds how far the crawl FRONTIER expands; it never had
    anything to say about how many steps ONE journey may take, yet the walk was
    gated on it. ``depth`` starts at the frontier depth of the entry step and
    incremented per wizard step, so a questionnaire reached a couple of links in
    stopped around step four and blamed a budget the operator never set.

    A ``max_depth`` of 2 against a 12-step wizard reproduces that exactly.
    """
    _records, coverage = _crawl(
        _questionnaire(12), tmp_path, monkeypatch,
        budget=Budget(rate_per_s=0, max_depth=2))
    flow = coverage["flows"][0]
    assert flow["step_count"] == 13, "the walk was truncated by crawl depth"
    assert flow["terminal"] == "no_advance"
    assert flow["completed"] is True


def test_the_wizard_step_budget_is_still_enforced(tmp_path, monkeypatch):
    """T-SI-04 DECOUPLED the walk from max_depth; it did not unbound it. The
    budget that IS about journeys still stops one, and still says so."""
    _records, coverage = _crawl(_questionnaire(20), tmp_path, monkeypatch,
                                e2e_wizard_steps=5)
    flow = coverage["flows"][0]
    assert flow["terminal"] == "budget_exhausted"
    assert flow["completed"] is False
    assert flow["step_count"] <= 6


# ─── T-SI-06 · depth that can be read ────────────────────────────────────────

def test_deepest_flow_reports_proven_depth_for_a_completed_walk(tmp_path, monkeypatch):
    _records, coverage = _crawl(_questionnaire(), tmp_path, monkeypatch)
    summary = coverage["flow_summary"]
    assert summary["deepest_flow_steps"] == N_QUESTIONS + 1
    assert summary["deepest_flow_proven_steps"] == N_QUESTIONS + 1
    assert summary["deepest_flow_capped"] is False
    assert summary["deepest_flow_terminal"] == "no_advance"


def test_a_capped_traversal_is_never_reported_as_application_depth(tmp_path, monkeypatch):
    """THE METRIC DEFECT. "Six steps because the app has six" and "six steps
    because we stopped at six" were the same integer and opposite facts, so a
    gate asserting ``deepest_flow >= 5`` passed identically on both."""
    _records, coverage = _crawl(_questionnaire(20), tmp_path, monkeypatch,
                                e2e_wizard_steps=5)
    summary = coverage["flow_summary"]
    assert summary["deepest_flow_capped"] is True
    assert summary["deepest_flow_proven_steps"] == 0
    assert summary["deepest_flow_terminal"] == "budget_exhausted"
    # The floor is still reported — it is a floor, not a measurement.
    assert summary["deepest_flow_steps"] >= 5


@pytest.mark.parametrize("n_questions,expected", [(1, 2), (5, 6), (20, 21)])
def test_deepest_flow_tracks_actual_application_depth(
        n_questions, expected, tmp_path, monkeypatch):
    """A shallow application reports shallow — and reports it as PROVEN, which
    is the distinction that makes the number worth gating on."""
    _records, coverage = _crawl(_questionnaire(n_questions), tmp_path, monkeypatch)
    summary = coverage["flow_summary"]
    assert summary["deepest_flow_proven_steps"] == expected
    assert summary["deepest_flow_capped"] is False
