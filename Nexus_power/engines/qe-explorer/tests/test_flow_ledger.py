"""End-to-end flow mode - a journey has to be able to say whether it FINISHED.

The crawl records states and actions. A business flow is neither: it is a path
through them. Without flows as objects the product can count pages and fields but
cannot answer the only question a client asks - did you get all the way through
Apply?

The distinction these tests protect: six steps of a fifteen-step funnel is NOT the
Apply flow. Reporting it as one is the same class of claim as a green test that
never ran, so completion is DERIVED from why the walk stopped and there is
deliberately no way for a caller to assert it directly.
"""
import inspect
import re

from app import flow_ledger as FL


def steps(n, unfilled=0):
    return [{"fingerprint": "s%d" % i, "url": "/step/%d" % i, "title": "Step %d" % i,
             "fields_filled": 3, "fields_unfilled": unfilled} for i in range(n)]


def flow(terminal, n=6, unfilled=0, max_steps=20, entry="e1"):
    return FL.build_flow(entry_fingerprint=entry, entry_url="/quote",
                         entry_title="Quote", steps=steps(n, unfilled),
                         terminal=terminal, max_steps=max_steps)


def test_a_walk_that_ran_out_of_budget_is_NOT_a_covered_journey():
    """THE GREEN-WASH THIS PREVENTS. Six steps of a fifteen-step funnel walked
    because the budget ended is not the Apply flow, and must never read as one."""
    assert flow(FL.TERMINAL_BUDGET)["completed"] is False


def test_reaching_the_submit_boundary_IS_a_covered_journey():
    """The funnel was walked to its end; the only thing left is the approval a human
    owes. That is as complete as a non-mutating crawl gets."""
    assert flow(FL.TERMINAL_SUBMIT_BOUNDARY)["completed"] is True


def test_a_step_with_nothing_left_to_advance_is_also_complete():
    assert flow(FL.TERMINAL_NO_ADVANCE)["completed"] is True


def test_a_loop_or_a_cancellation_is_not_complete():
    for t in (FL.TERMINAL_LOOP, FL.TERMINAL_CANCELLED):
        assert flow(t)["completed"] is False, t


def test_completion_cannot_be_asserted_by_a_caller():
    """Derived from the terminal reason. A caller that could pass completed=True
    could report any truncated walk as a covered journey."""
    assert "completed" not in inspect.signature(FL.build_flow).parameters
    assert '"completed": term in COMPLETING_TERMINALS' in inspect.getsource(FL.build_flow)


def test_only_two_terminals_mean_covered():
    assert FL.COMPLETING_TERMINALS == {FL.TERMINAL_SUBMIT_BOUNDARY, FL.TERMINAL_NO_ADVANCE}


def test_an_unrecognised_terminal_fails_closed_to_not_covered():
    """A future terminal nobody thought about must not silently count as coverage."""
    for t in ("something_new", ""):
        assert FL.build_flow(entry_fingerprint="x", entry_url="/", entry_title="",
                             steps=steps(3), terminal=t)["completed"] is False


def test_a_journey_walked_with_unanswered_fields_says_so():
    """Covered STRUCTURALLY but not with real data. Both facts matter and neither
    substitutes for the other."""
    f = flow(FL.TERMINAL_SUBMIT_BOUNDARY, unfilled=2)
    assert f["completed"] is True
    assert f["fully_answered"] is False
    assert f["fields_unanswered"] == 12


def test_a_fully_answered_journey_is_marked_as_such():
    f = flow(FL.TERMINAL_SUBMIT_BOUNDARY, unfilled=0)
    assert f["fully_answered"] is True and f["fields_unanswered"] == 0


def test_a_flow_is_identified_by_where_it_starts():
    """Steps change as the app changes and terminals change as budgets change. Only
    the entry is stable, so a client watches one funnel get deeper over time rather
    than seeing a new flow appear every crawl."""
    shallow = flow(FL.TERMINAL_BUDGET, n=3, entry="same")
    deep = flow(FL.TERMINAL_SUBMIT_BOUNDARY, n=14, entry="same")
    assert shallow["flow_id"] == deep["flow_id"]
    assert flow(FL.TERMINAL_BUDGET, entry="other")["flow_id"] != shallow["flow_id"]


def test_the_summary_states_that_branches_were_NOT_covered():
    """THE CLAIM THAT MUST NOT BE INFERRED. Walking every flow once is one path per
    funnel: every decision point was answered a single way and the alternatives - a
    different premium, a different eligibility outcome - were never visited."""
    s = FL.summarize([flow(FL.TERMINAL_SUBMIT_BOUNDARY), flow(FL.TERMINAL_BUDGET)])
    assert s["branch_coverage"] is False
    assert "single option" in s["branch_coverage_note"]


def test_the_summary_separates_covered_from_truncated():
    s = FL.summarize([flow(FL.TERMINAL_SUBMIT_BOUNDARY), flow(FL.TERMINAL_NO_ADVANCE),
                      flow(FL.TERMINAL_BUDGET), flow(FL.TERMINAL_LOOP)])
    assert s["flows_found"] == 4
    assert s["flows_completed"] == 2
    assert s["flows_truncated"] == 2
    assert s["truncation_reasons"] == {FL.TERMINAL_BUDGET: 1, FL.TERMINAL_LOOP: 1}


def test_the_summary_names_WHY_each_truncated_flow_stopped():
    """'Stopped' is not actionable; 'ran out of a 6-step budget' is."""
    s = FL.summarize([flow(FL.TERMINAL_BUDGET, max_steps=6)])
    assert FL.TERMINAL_BUDGET in s["truncation_reasons"]
    assert flow(FL.TERMINAL_BUDGET, max_steps=6)["step_budget"] == 6


def test_an_empty_crawl_summarises_without_crashing():
    s = FL.summarize([])
    assert s["flows_found"] == 0 and s["branch_coverage"] is False


def test_the_outcome_a_funnel_produced_is_kept():
    """A premium or an eligibility decision is what the journey was FOR - and in the
    next phase it decides whether a different choice produced a different path."""
    f = FL.build_flow(entry_fingerprint="e", entry_url="/q", entry_title="Q",
                      steps=steps(4), terminal=FL.TERMINAL_SUBMIT_BOUNDARY,
                      outcome_values=[{"label": "Monthly premium", "value": "$92.40",
                                       "value_type": "currency"}])
    assert f["outcome_values"][0]["value"] == "$92.40"
    assert f["outcome_values"][0]["value_type"] == "currency"


_CRAWLER = open("app/crawler.py", encoding="utf-8").read()


def test_end_to_end_mode_only_changes_the_step_budget():
    """A deeper walk must not be a laxer one. The commit-word veto, the danger gate
    and the submit boundary decide what may be clicked - not the step count."""
    assert "_E2E_WIZARD_STEPS" in _CRAWLER and "_E2E_WIZARD_ADVANCES" in _CRAWLER
    seg = _CRAWLER[_CRAWLER.index("self._crawl_mode = "):]
    seg = seg[:seg.index("self._flows")]
    for gate in ("submit_approvals", "danger", "_WIZARD_COMMIT_RE", "attestation"):
        assert gate not in seg, "%s must not depend on the crawl mode" % gate


def test_the_deeper_budget_is_still_bounded():
    n = int(re.search(r"_E2E_WIZARD_STEPS = (\d+)", _CRAWLER).group(1))
    a = int(re.search(r"_E2E_WIZARD_ADVANCES = (\d+)", _CRAWLER).group(1))
    assert 6 < n <= 40 and 24 < a <= 200


def test_a_mode_nobody_set_gets_the_shallow_budget():
    seg = _CRAWLER[_CRAWLER.index("self._max_wizard_steps"):][:400]
    assert 'if self._crawl_mode == "e2e"' in seg
    assert "_MAX_WIZARD_STEPS" in seg


def test_a_single_page_form_is_still_a_journey():
    """FOUND LIVE. Recording only multi-step wizards made an application with a real
    quote form report ZERO flows — which reads as "no journeys here" when the truth
    is "one journey, one step long"."""
    seg = _CRAWLER[_CRAWLER.index("walked = False"):]
    seg = seg[:seg.index("async def ")]
    assert "if not walked and is_form and fill is not None:" in seg
    assert "flow_ledger.build_flow(" in seg
    assert "TERMINAL_SUBMIT_BOUNDARY" in seg


def test_a_one_step_journey_that_reaches_submit_counts_as_covered():
    f = FL.build_flow(entry_fingerprint="one", entry_url="/quote", entry_title="Quote",
                      steps=steps(1), terminal=FL.TERMINAL_SUBMIT_BOUNDARY, max_steps=20)
    assert f["completed"] is True and f["step_count"] == 1


# ── self-contradiction tripwire ──────────────────────────────────────────────

def _step(fp="f1", filled=0, unfilled=0):
    return {"fingerprint": fp, "url": "u", "title": "t",
            "fields_filled": filled, "fields_unfilled": unfilled}


def test_filled_fields_plus_a_refused_advance_raises_the_tripwire():
    """We claim to have filled the form; the app refuses to move. Both cannot be
    comfortable — either the app is blocked (a finding) or a fill was recorded
    successful while the control never took the value (a defect).

    This is the cheapest check on our own honesty: no LLM, no app knowledge,
    true everywhere. An entire class of defects hid behind "filled" — a locator
    matching nothing, a read-back in the wrong vocabulary, a placeholder taken
    as an answer — and every one of them would have raised THIS flag on the
    first crawl of the first application."""
    f = FL.build_flow(
        entry_fingerprint="fpA", entry_url="u", entry_title="Coverage",
        steps=[_step(filled=2)], terminal=FL.TERMINAL_LOOP, max_steps=20)
    assert f["advance_contradicts_fills"] is True
    assert f["fields_filled_total"] == 2


def test_reaching_the_end_of_a_funnel_is_not_a_contradiction():
    """no_advance means "nothing here could advance it", which at the END of a
    funnel is correct completion. A review page that displays a premium and
    offers no next step is what success looks like — firing the tripwire there
    flagged the very journey it exists to protect, and a tripwire that cries
    wolf on success is one nobody reads."""
    f = FL.build_flow(
        entry_fingerprint="fpA", entry_url="u", entry_title="Review",
        steps=[_step(filled=1)], terminal=FL.TERMINAL_NO_ADVANCE,
        outcome_values=[{"label": "Monthly Premium", "text": "$4.24",
                         "value_type": "currency"}],
        max_steps=20)
    assert f["advance_contradicts_fills"] is False


def test_a_page_we_never_filled_is_not_suspicious():
    """A funnel stopped at an unanswered decision is EXPECTED to loop. Flagging
    that would bury the real signal in noise."""
    f = FL.build_flow(
        entry_fingerprint="fpA", entry_url="u", entry_title="Product",
        steps=[_step(filled=0, unfilled=4)], terminal=FL.TERMINAL_LOOP,
        max_steps=20)
    assert f["advance_contradicts_fills"] is False


def test_a_flow_that_reached_the_submit_boundary_is_not_suspicious():
    f = FL.build_flow(
        entry_fingerprint="fpA", entry_url="u", entry_title="Review",
        steps=[_step(filled=3)], terminal=FL.TERMINAL_SUBMIT_BOUNDARY,
        max_steps=20)
    assert f["advance_contradicts_fills"] is False


def test_summary_rolls_the_contradiction_up_for_fleet_monitoring():
    """Across 1000 applications this count is how a systemic fill defect is
    found ONCE instead of rediscovered per app."""
    bad = FL.build_flow(
        entry_fingerprint="fpA", entry_url="u", entry_title="Coverage",
        steps=[_step(filled=2)], terminal=FL.TERMINAL_LOOP, max_steps=20)
    good = FL.build_flow(
        entry_fingerprint="fpB", entry_url="u", entry_title="Review",
        steps=[_step(filled=3)], terminal=FL.TERMINAL_SUBMIT_BOUNDARY,
        max_steps=20)
    s = FL.summarize([bad, good])
    assert s["advance_contradicts_fills"] == 1


def test_the_outcome_value_survives_the_fold():
    """Regression: the crawl captured "Estimated Monthly Premium = $9.26" on the
    review page and the journey recorded a premium of "".

    The displayed-value contract is {label, selector, TEXT}; build_flow read only
    ``value``, so every outcome folded to empty. A journey without its outcome is
    a walk, not evidence — and the premium is the whole point of a quote funnel."""
    f = FL.build_flow(
        entry_fingerprint="fpQ", entry_url="u", entry_title="Quote",
        steps=[_step(filled=3)],
        outcome_values=[
            {"label": "Estimated Monthly Premium", "selector": "#prem",
             "text": "$9.26", "value_type": "currency"},
            {"label": "Coverage Amount", "selector": "#cov",
             "text": "$50,000", "value_type": "currency"},
        ],
        terminal=FL.TERMINAL_SUBMIT_BOUNDARY, max_steps=20)
    got = {o["label"]: o["value"] for o in f["outcome_values"]}
    assert got == {"Estimated Monthly Premium": "$9.26",
                   "Coverage Amount": "$50,000"}


def test_an_explicit_value_key_still_wins():
    """Producers that already speak {label, value} are unaffected."""
    f = FL.build_flow(
        entry_fingerprint="fpQ", entry_url="u", entry_title="Quote",
        steps=[_step(filled=1)],
        outcome_values=[{"label": "Premium", "value": "$1.00",
                         "text": "ignored", "value_type": "currency"}],
        terminal=FL.TERMINAL_SUBMIT_BOUNDARY, max_steps=20)
    assert f["outcome_values"][0]["value"] == "$1.00"
