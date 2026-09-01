"""FLEET FUNNEL (C4) — "where are we failing?" is a dashboard, not an investigation.

The per-crawl honesty was always there: every crawl writes a precise diagnosis, an
honest stop reason, a posture, a generation outcome. Nothing aggregated them. So a
fleet-wide collapse — weekly yield 86% to 16%, four apps consuming 450 of 563
crawls, 270 crawls generating nothing — surfaced as a founder escalation two
months later instead of as a number that moved.

The module computes NOTHING new. Every field it reads was written by the crawl
that produced it, because a telemetry layer that derives its own numbers can
disagree with the evidence and then nobody trusts either. These tests pin that
property as hard as they pin the arithmetic.
"""
from __future__ import annotations

from app.services.fleet_funnel import crawl_row, summarize, worst_stage


def _crawl(status="completed", *, visits=0, forms=0, flows=0, deepest=0,
           tests=0, app="app-1", reason="", attempted=True, advances=0,
           outcome=None, degraded=False, blocked=0, proven=None):
    generate = {"generated": tests, "no_cases_reason": reason,
                "attempted": attempted}
    if outcome:
        generate["outcome"] = outcome
    stats = {
        "visits": visits,
        "generate": generate,
        "coverage": {
            "forms_found": forms,
            "traversal": "full",
            "advance_blocked": [{"label": "Continue"}] * blocked,
            "flow_summary": {"flows_found": flows, "deepest_flow_steps": deepest,
                             "advances_by_tier": ({"1": advances} if advances else {}),
                             **({} if proven is None
                                else {"deepest_flow_proven_steps": proven})},
        },
    }
    if degraded:
        stats["degraded"] = {"traversal": {"requested": "full", "actual": "probe"}}
    return {"app_id": app, "status": status, "stats": stats}


# ── the funnel names the stage that loses the most ─────────────────────────

def test_the_worst_stage_is_the_one_to_fix_next():
    """The measure-first loop in one field: rather than arguing about
    priorities, read which stage drops the most."""
    rows = [_crawl(visits=20, forms=5, flows=6, deepest=1, tests=3)
            for _ in range(8)]
    s = summarize(rows)
    # Every crawl generated tests but none walked deeper than its entry page —
    # exactly today's live shape.
    assert worst_stage(s)["stage"] == "deep enough for E2E"
    assert s["e2e_capable_pct"] == 0.0
    assert s["yield_pct"] == 100.0


def test_a_crawl_that_never_completed_is_lost_at_the_first_stage():
    rows = [_crawl("stalled"), _crawl("failed"), _crawl(visits=10, forms=1,
                                                        flows=1, deepest=2, tests=2)]
    stages = {s["stage"]: s for s in summarize(rows)["stages"]}
    assert stages["completed"]["lost_here"] == 2
    assert stages["deep enough for E2E"]["crawls"] == 1


def test_the_drop_between_stages_is_what_is_reported():
    """A stage's raw count says little; the LOSS is the signal."""
    rows = ([_crawl(visits=10, forms=0)] * 3          # pages, then no forms
            + [_crawl(visits=10, forms=2, flows=1, deepest=2, tests=1)])
    stages = {s["stage"]: s for s in summarize(rows)["stages"]}
    assert stages["found forms"]["lost_here"] == 3
    assert stages["found forms"]["lost_pct"] == 75.0


# ── it reads, it does not derive ───────────────────────────────────────────

def test_every_number_comes_from_the_crawl_that_wrote_it():
    r = crawl_row(_crawl(visits=24, forms=7, flows=8, deepest=1, tests=12,
                         advances=3))
    assert (r["pages"], r["forms"], r["flows"], r["tests"]) == (24, 7, 8, 12)
    assert r["advances"] == 3


def test_an_older_row_without_B4_outcome_is_classified_the_same_way():
    """Rows predate the outcome field; inferring it differently here would make
    the dashboard disagree with the row it came from."""
    assert crawl_row(_crawl(tests=4))["generate_outcome"] == "generated"
    assert crawl_row(_crawl(reason="no coherent flow"))["generate_outcome"] == "no_cases"
    assert crawl_row(_crawl())["generate_outcome"] == "unexplained"
    assert crawl_row(_crawl(attempted=False))["generate_outcome"] == "not_attempted"


def test_an_explicit_outcome_is_never_second_guessed():
    assert crawl_row(_crawl(tests=0, outcome="error"))["generate_outcome"] == "error"


# ── the two "why" breakdowns ───────────────────────────────────────────────

def test_zero_yield_apps_are_named_and_ordered_by_waste():
    """The C1 brake stops the loop; this names who was in it, worst first."""
    rows = ([_crawl(app="loop-a")] * 9 + [_crawl(app="loop-b")] * 4
            + [_crawl(app="good", tests=3, visits=5, forms=1, flows=1, deepest=2)])
    apps = summarize(rows)["zero_yield_apps"]
    assert [a["app_id"] for a in apps] == ["loop-a", "loop-b"]
    assert apps[0]["crawls"] == 9


def test_a_new_app_with_one_barren_crawl_is_not_named():
    """Naming an app after a single empty crawl would make the list noise."""
    assert summarize([_crawl(app="fresh")])["zero_yield_apps"] == []


def test_unexplained_generation_is_called_out_as_OUR_gap():
    notes = " ".join(summarize([_crawl()] * 3)["notes"])
    assert "gap in the generator" in notes
    assert "not a finding about those applications" in notes


def test_a_blocked_advance_is_surfaced_with_its_value_named():
    notes = " ".join(summarize([_crawl(visits=5, forms=2, blocked=1)])["notes"])
    assert "disabled" in notes and "highest-value" in notes


def test_a_degraded_posture_is_counted():
    notes = " ".join(summarize([_crawl(degraded=True)])["notes"])
    assert "lower posture than requested" in notes


# ── never invent a funnel ──────────────────────────────────────────────────

def test_an_empty_fleet_reports_nothing_rather_than_zeroes_that_look_real():
    s = summarize([])
    assert s["crawls"] == 0 and s["stages"] == [] and s["yield_pct"] == 0.0
    assert worst_stage(s) == {}


def test_junk_rows_never_crash_the_dashboard():
    for rows in ([None, 3, "x"], [{}], [{"stats": "nope"}]):
        s = summarize([r for r in rows if isinstance(r, dict)])
        assert isinstance(s["yield_pct"], float)


# ── A19 · depth that is a floor is not depth ───────────────────────────────

def test_a_capped_walk_is_not_deep_enough_for_e2e():
    """The regression Gate 2 measured: vkpower-life reported deepest_flow=10 and
    had proven NONE of it -- the walk was cut off, not finished. Counted on
    walked depth alone, a fleet of truncated traversals scores exactly like a
    fleet of completed journeys at the one stage the product turns on."""
    rows = [_crawl(visits=10, forms=1, flows=3, deepest=10, proven=0, tests=1)]
    summary = summarize(rows)
    stages = {s["stage"]: s for s in summary["stages"]}
    assert stages["deep enough for E2E"]["crawls"] == 0, (
        "a walk that proved no depth was counted as E2E-capable on the strength "
        "of a number its own producer calls a floor")
    assert summary["capped_depth_crawls"] == 1
    assert any("floor" in n for n in summary["notes"]), (
        "the crawls removed from the stage must be NAMED -- a stage that "
        "silently drops rows is how a fleet report stops being believed")


def test_a_proven_walk_is_deep_enough_for_e2e():
    rows = [_crawl(visits=10, forms=1, flows=3, deepest=6, proven=6, tests=1)]
    summary = summarize(rows)
    stages = {s["stage"]: s for s in summary["stages"]}
    assert stages["deep enough for E2E"]["crawls"] == 1
    assert summary["capped_depth_crawls"] == 0


def test_a_crawl_recorded_before_proven_depth_existed_is_not_reclassified():
    """A row that carries no opinion about proven depth must not be read as
    having proved nothing -- that would delete every historical crawl from the
    E2E stage the day this shipped, and read as a fleet-wide regression."""
    rows = [_crawl(visits=10, forms=1, flows=3, deepest=6, tests=1)]  # proven absent
    stages = {s["stage"]: s for s in summarize(rows)["stages"]}
    assert stages["deep enough for E2E"]["crawls"] == 1
