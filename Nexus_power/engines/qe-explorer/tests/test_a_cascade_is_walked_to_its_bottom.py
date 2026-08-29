"""THE CASCADE IS WALKED TO ITS BOTTOM, AND THE BOTTOM IS RECORDED.

The shape the client asked about, in miniature and driven end to end:

    Q1  Heart disease?            Yes -> reveals Q1a, Q1b
    Q1a Under specialist care?    Yes -> reveals Q1a-i
    Q2  Diabetes?                 (no reveal)

Three levels. A one-path crawl sees Q1 and Q2 and reports two questions. A sweep
must ask every question with every answer and follow what each answer reveals,
reaching all five.

The page here is a FAKE that behaves like a real one -- answers are stateful,
reveals depend on the exact answer, and a reset clears everything -- so the
recursion, the reset discipline and the ledger are all exercised without a
browser. The browser's own behaviour is proven separately against the fixture.
"""
from __future__ import annotations

import asyncio

from app.branch_walk import sweep_page


class FakePage:
    """A questionnaire whose reveals are stateful, like a real one."""

    BASE = [("q1", "Heart disease?"), ("q2", "Diabetes?")]
    REVEALS = {
        ("q1", "Yes"): [("q1a", "Under specialist care?"), ("q1b", "Which year?")],
        ("q1a", "Yes"): [("q1ai", "Which specialist?")],
    }

    def __init__(self):
        self.answers: dict[str, str] = {}
        self.fills = 0
        self.resets = 0

    # -- what the page currently shows -------------------------------------
    def _visible(self):
        out = list(self.BASE)
        added = True
        while added:                      # reveals can cascade
            added = False
            for qid, ans in list(self.answers.items()):
                for rid, label in self.REVEALS.get((qid, ans), ()):
                    if (rid, label) not in out:
                        out.append((rid, label))
                        added = True
        return out

    async def observe(self):
        return {"url": "http://fake/", "visible": self._visible()}

    def build_controls(self, obs):
        recs = []
        for qid, label in obs["visible"]:
            for opt in ("Yes", "No"):
                recs.append({"name": opt, "kind": "radio", "group_id": qid,
                             "question_label": label, "group_options": ["Yes", "No"]})
        return recs

    async def fill(self, control, value):
        self.fills += 1
        self.answers[control["group_id"]] = value

    async def reset(self):
        self.resets += 1
        self.answers.clear()


class _Port:
    """Models the REAL port's surface: a radio is clicked, not typed into.

    The first version of this fake offered only `fill`, so the sweep passed
    against it while the live crawl reported 60 questions, 138 answers and ZERO
    reveals -- a radio filled rather than clicked dispatches no `change` event,
    and the page's conditional logic never runs. A fake that cannot express that
    difference cannot catch it.
    """

    def __init__(self, page):
        self._p = page

    async def click(self, control):
        # The clicked MEMBER carries the answer, exactly as in the DOM.
        await self._p.fill(control, str(control.get("name") or ""))

    async def select_option(self, control, value):
        await self._p.fill(control, value)

    async def fill(self, control, value):
        raise AssertionError("a radio must be CLICKED, not filled")


def _run(max_visits=400, max_depth=6):
    page = FakePage()
    led = asyncio.run(sweep_page(
        port=_Port(page), observe=page.observe,
        build_controls=page.build_controls, reset=page.reset,
        max_visits=max_visits, max_depth=max_depth))
    return page, led


# ── the measured shape ─────────────────────────────────────────────────────

def test_every_question_including_the_hidden_ones_is_asked():
    """THE POINT. A one-path crawl sees 2 questions; all five are reachable."""
    _, led = _run()
    assert led.questions_seen == {"q1", "q2", "q1a", "q1b", "q1ai"}


def test_both_answers_of_every_question_are_taken():
    _, led = _run()
    for q in ("q1", "q2", "q1a", "q1b", "q1ai"):
        opts = {v["option"] for v in led.visits if v["question"] == q}
        assert opts == {"Yes", "No"}, f"{q} was not asked both ways: {opts}"


def test_the_reveal_is_attributed_to_the_answer_that_caused_it():
    _, led = _run()
    yes = [v for v in led.visits if v["question"] == "q1" and v["option"] == "Yes"]
    no = [v for v in led.visits if v["question"] == "q1" and v["option"] == "No"]
    assert yes[0]["revealed_count"] == 2
    assert no[0]["revealed_count"] == 0, "No must not be credited with Yes's reveal"


def test_the_third_level_is_reached_through_the_second():
    _, led = _run()
    deep = [v for v in led.visits if v["question"] == "q1ai"]
    assert deep, "the level-3 question was never asked"
    assert all(v["depth"] >= 2 for v in deep)


# ── the discipline that makes it honest ────────────────────────────────────

def test_the_page_is_reset_between_answers():
    """Without a reset, Q2's reveals would be attributed to Q1's state."""
    page, led = _run()
    assert page.resets >= len(led.visits)


def test_a_sweep_never_claims_to_be_combinatorial():
    _, led = _run()
    s = led.summary()
    assert s["mode"] == "per_question_sweep" and s["combinatorial"] is False


# ── the bounds ─────────────────────────────────────────────────────────────

def test_the_visit_budget_stops_the_sweep_and_says_so():
    _, led = _run(max_visits=3)
    assert len(led.visits) == 3
    assert any(s["reason"] == "budget_exhausted" for s in led.skipped)


def test_the_depth_limit_stops_the_descent():
    _, led = _run(max_depth=1)
    assert led.questions_seen == {"q1", "q2"}, "descent should not have happened"


# ── breadth before depth ───────────────────────────────────────────────────

def test_every_top_level_question_is_asked_before_any_descent():
    """MEASURED (underwriting fixture, 2026-08-29). The first live sweep recursed
    on the first reveal it found and spent its budget like this:

        answers by depth: {0:5, 1:5, 2:5, 3:19, 4:51, 5:315}

    315 of 400 answers four levels down ONE cascade, while 59 of the page's 64
    questions had never been asked at all. For "prove every question on this page
    was covered" that is exactly the wrong order.
    """
    _, led = _run()
    order = [(v["depth"], i) for i, v in enumerate(led.visits)]
    first_deeper = next((i for d, i in order if d > 0), None)
    assert first_deeper is not None, "the cascade was never followed"
    depth0_before = {v["question"] for v in led.visits[:first_deeper] if v["depth"] == 0}
    assert depth0_before == {"q1", "q2"}, (
        f"descended before finishing the page: {depth0_before}")


def test_a_budget_that_runs_out_buys_the_page_not_one_cascade():
    """With only enough budget for the top level, it must buy the top level."""
    _, led = _run(max_visits=4)
    assert {v["question"] for v in led.visits} == {"q1", "q2"}
    assert all(v["depth"] == 0 for v in led.visits)
