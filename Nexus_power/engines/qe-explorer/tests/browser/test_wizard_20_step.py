"""Gate 1 / T-WZ-01 — FIXTURE 27 DRIVEN THROUGH ALL TWENTY STEPS.

WHY THIS IS A SEPARATE TEST MODULE and not more keys in ``expected.json``.

Every other browser assertion in this suite is a claim about ONE observation: a
``collect()`` returns controls and the fixture's contract describes them.  The
claim fixture 27 exists to support cannot be made that way.  "The walk knows it
moved" is a statement about the RELATIONSHIP BETWEEN OBSERVATIONS, and a lane
that collects once has, by construction, no way to state it — which is exactly
why the twenty-step collapse (F1) survived a suite full of green capture tests.

So this module navigates.  It drives the real fixture in real Chromium the way a
person would — answer, Continue, answer, Continue — and asserts the four
properties the fixture was built to hold, each of which is invisible to a single
capture:

* the validation checkpoint is real and is stated NOWHERE in the markup;
* twenty steps that are structurally identical are still twenty steps;
* state survives backward navigation;
* the branch at step 7 is a branch, and reversing the answer removes it.

The last test closes the loop back onto Gate 1's adjudication: a route walked
end to end yields nineteen crossings, and the SAME fixture read by a collapsed
walk yields zero — which ``app.completion.adjudicate`` refuses to call complete.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.completion import (
    DISPOSITION_COMPLETED,
    DISPOSITION_INCOMPLETE,
    STOP_JOURNEY_ZERO_CROSSING,
    CrawlEvidence,
    adjudicate,
)
from app.crawl_constants import STOP_COMPLETED

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

FIXTURE = "27-wizard-20-step-samefingerprint"

#: The application's own step count, and the length of the route when the
#: step-7 branch is NOT taken.  Pinned here so a change to the fixture that
#: quietly shortens it fails this suite rather than passing a smaller claim.
STEPS = 20


# ─── driving primitives ─────────────────────────────────────────────────────

async def _open(page: Any, fixture_server: Any) -> None:
    await page.goto(fixture_server.url(FIXTURE), wait_until="domcontentloaded")


async def _legend(page: Any) -> str:
    return (await page.inner_text("#qlegend")).strip()


async def _progress(page: Any) -> str:
    return (await page.inner_text("#progress")).strip()


async def _radio_name(page: Any) -> str:
    """The DOM's own declaration of WHICH QUESTION the two inputs answer."""
    return await page.get_attribute("#opt-yes", "name")


async def _answer(page: Any, choice: str) -> None:
    await page.check("#opt-yes" if choice == "yes" else "#opt-no")


async def _continue(page: Any) -> None:
    await page.click("#next")


async def _back(page: Any) -> None:
    await page.click("#back")


async def _next_disabled(page: Any) -> bool:
    return await page.is_disabled("#next")


# ─── 1. the validation checkpoint nothing in the markup declares ────────────

def test_continue_is_disabled_until_the_question_is_answered(pw, fixture_server):
    """THE SHAPE ``_answer_to_unblock`` EXISTS FOR.  ``required`` on a radio means
    "that input must be checked", which is never what "this question must be
    answered" means — so every framework puts the rule in script, where no amount
    of DOM reading can find it.  Here it is, in a fixture, so the radio unblock
    path (Gate 1 / T-RG-01) has something real to be proven against."""
    async def _run():
        page = pw.page
        await _open(page, fixture_server)

        assert await _next_disabled(page), "step 1 opens blocked"
        # And the markup says nothing about it.
        assert await page.get_attribute("#opt-yes", "required") is None
        assert await page.get_attribute("#opt-no", "required") is None

        await _answer(page, "no")
        assert not await _next_disabled(page), (
            "the application enabled its own forward control once answered — "
            "that verdict is the evidence the unblock experiment collects")

    pw.run(_run())


def test_back_is_disabled_only_on_the_first_step(pw, fixture_server):
    async def _run():
        page = pw.page
        await _open(page, fixture_server)
        assert await page.is_disabled("#back")

        await _answer(page, "no")
        await _continue(page)
        assert not await page.is_disabled("#back")

    pw.run(_run())


# ─── 2. twenty identical steps are still twenty steps ───────────────────────

def test_all_twenty_steps_are_walkable(pw, fixture_server):
    """THE F1 REGRESSION, AS A WALK.  Each step renders the same four controls, so
    a fingerprint built from the control-name set is identical on all twenty and
    the walk sees one state visited twenty times.  The route below is what the
    application actually has."""
    async def _run():
        page = pw.page
        await _open(page, fixture_server)

        legends, names, progress = [], [], []
        for step in range(STEPS):
            legends.append(await _legend(page))
            names.append(await _radio_name(page))
            progress.append(await _progress(page))
            await _answer(page, "no")          # "No" everywhere: no branch taken
            await _continue(page)

        assert len(set(names)) == STEPS, (
            "the radio name attribute is the ONLY per-step identity on this "
            "page; %d distinct values across %d steps means the fixture stopped "
            "being adversarial" % (len(set(names)), STEPS))
        assert len(set(legends)) == STEPS, "every question is worded differently"
        assert progress[0] == "Step 1 of 20"
        assert progress[-1] == "Step 20 of 20"
        assert await page.is_visible("#done"), "the funnel reaches its end"

    pw.run(_run())


def test_every_step_renders_the_identical_control_shape(pw, fixture_server):
    """The adversarial property, MEASURED rather than asserted in prose: the
    (role, name) evidence is byte-identical on all twenty steps, so nothing
    accumulated from it can ever tell them apart."""
    async def _run():
        page = pw.page
        await _open(page, fixture_server)

        shapes = set()
        for _ in range(STEPS):
            shapes.add(await page.eval_on_selector_all(
                "input,button",
                "els => els.map(e => e.tagName + ':' + (e.type || '') + ':' +"
                " (e.textContent || e.value || '').trim()).join('|')"))
            await _answer(page, "no")
            await _continue(page)

        assert len(shapes) == 1, (
            "expected ONE control shape across all steps, got %d — the fixture "
            "must not leak per-step identity into the control set" % len(shapes))

    pw.run(_run())


# ─── 3. state persists across backward navigation ───────────────────────────

def test_an_answer_survives_going_back(pw, fixture_server):
    """A re-visited step is the SAME step.  If answers were cleared, a walk that
    used Back would re-answer questions it had already answered and the funnel
    would never terminate."""
    async def _run():
        page = pw.page
        await _open(page, fixture_server)

        first_q = await _radio_name(page)
        await _answer(page, "no")
        await _continue(page)
        await _answer(page, "yes")
        await _back(page)

        assert await _radio_name(page) == first_q, "back landed on step 1"
        assert await page.is_checked("#opt-no"), "the earlier answer is still there"
        assert not await _next_disabled(page), (
            "an already-answered step opens UNBLOCKED — the validation rule is "
            "evaluated against stored state, not against a fresh form")

    pw.run(_run())


def test_going_forward_again_returns_the_answer_given_the_first_time(pw,
                                                                    fixture_server):
    async def _run():
        page = pw.page
        await _open(page, fixture_server)
        await _answer(page, "no")
        await _continue(page)
        await _answer(page, "yes")
        second_q = await _radio_name(page)

        await _back(page)
        await _continue(page)

        assert await _radio_name(page) == second_q
        assert await page.is_checked("#opt-yes")

    pw.run(_run())


# ─── 4. the conditional transition is a branch, not a skipped number ────────

def test_answering_yes_at_step_seven_inserts_a_follow_up_step(pw, fixture_server):
    async def _run():
        page = pw.page
        await _open(page, fixture_server)

        for _ in range(6):                     # steps 1-6
            await _answer(page, "no")
            await _continue(page)

        assert await _radio_name(page) == "q07"
        await _answer(page, "yes")
        assert await _progress(page) == "Step 7 of 21", (
            "the route LENGTHENED the moment the answer was given — the branch "
            "is recomputed from the answers, not appended once")
        await _continue(page)
        assert await _radio_name(page) == "q07a"

    pw.run(_run())


def test_answering_no_at_step_seven_takes_no_branch(pw, fixture_server):
    async def _run():
        page = pw.page
        await _open(page, fixture_server)
        for _ in range(6):
            await _answer(page, "no")
            await _continue(page)

        await _answer(page, "no")
        assert await _progress(page) == "Step 7 of 20"
        await _continue(page)
        assert await _radio_name(page) == "q08", "straight on to 8"

    pw.run(_run())


def test_reversing_the_branch_answer_removes_the_inserted_step(pw, fixture_server):
    """A branch that could only ever be ADDED would be a one-way door, and a walk
    that explored the other option would carry the first option's step with it."""
    async def _run():
        page = pw.page
        await _open(page, fixture_server)
        for _ in range(6):
            await _answer(page, "no")
            await _continue(page)

        await _answer(page, "yes")
        assert await _progress(page) == "Step 7 of 21"
        await _answer(page, "no")
        assert await _progress(page) == "Step 7 of 20"
        await _continue(page)
        assert await _radio_name(page) == "q08"

    pw.run(_run())


# ─── 5. deterministic replay ────────────────────────────────────────────────

def test_two_identical_walks_produce_the_identical_route(pw, fixture_server):
    """Every transition is a synchronous function of (step, answers): no timer,
    no network, no randomness.  Without that this fixture could not be a replay
    target, and a golden recorded from it would be a flake generator."""
    async def _walk() -> list[str]:
        page = pw.page
        await _open(page, fixture_server)
        route = []
        for step in range(8):
            route.append(await _radio_name(page))
            await _answer(page, "yes" if step == 6 else "no")
            await _continue(page)
        return route

    first = pw.run(_walk())
    second = pw.run(_walk())
    assert first == second
    assert "q07a" in first, "the branch was taken in both"


# ─── 6. what this fixture means for the Gate 1 completion verdict ───────────

def test_a_full_walk_of_this_fixture_yields_nineteen_crossings(pw, fixture_server):
    """THE POINT OF THE WHOLE FIXTURE, stated as the number the adjudicator reads.

    A journey of twenty steps crosses nineteen times — ``step_count - 1``, which
    is exactly what ``crawler`` sums into ``journey_crossings``.  Measured from a
    real walk rather than assumed, then handed to the real adjudicator."""
    async def _run() -> int:
        page = pw.page
        await _open(page, fixture_server)
        crossed = 0
        for _ in range(STEPS - 1):
            await _answer(page, "no")
            await _continue(page)
            crossed += 1
        return crossed

    crossings = pw.run(_run())
    assert crossings == STEPS - 1 == 19

    verdict = adjudicate(STOP_COMPLETED, CrawlEvidence(
        states=STEPS, journeys_walked=1, journey_crossings=crossings))
    assert verdict.disposition == DISPOSITION_COMPLETED
    assert not verdict.downgraded


def test_a_collapsed_walk_of_this_fixture_is_refused(pw, fixture_server):
    """The counterfactual, on the same fixture and against the same adjudicator.

    A walk that treats all twenty steps as one state stops after step 1 having
    crossed nothing.  It still observed a page, still wrote a manifest, and
    before Gate 1 still reported ``completed``."""
    async def _run() -> int:
        page = pw.page
        await _open(page, fixture_server)
        await _answer(page, "no")
        return 0                               # the collapse: never advanced

    crossings = pw.run(_run())

    verdict = adjudicate(STOP_COMPLETED, CrawlEvidence(
        states=STEPS, journeys_walked=1, journey_crossings=crossings))
    assert verdict.disposition == DISPOSITION_INCOMPLETE
    assert verdict.stop_reason == STOP_JOURNEY_ZERO_CROSSING
