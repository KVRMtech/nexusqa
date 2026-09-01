"""G7 / C1 — A CRAWL KILLED MID-JOURNEY MUST FINISH THE JOURNEY WHEN RESUMED.

WHY THIS FILE EXISTS.  Resume has been wired since M1.7 and had never been
exercised against a JOURNEY.  Every existing resume test kills a crawl between
PAGES — the frontier is the work list, the frontier is restored, the crawl
continues.  A funnel is not on the frontier.  A ten-step wizard is walked
INSIDE one expansion of one URL, so the unit of lost work on a kill at step 6
is not a queue entry; it is the six steps of progress the walk had made, and
nothing in the checkpoint describes them.

THE TWO CLAIMS, and they pull in opposite directions:

  FORWARD   a resume must CONTINUE the journey — steps 7..10 get walked.
  BACKWARD  a resume must NOT re-cross the irreversible boundary the killed
            run already crossed (M3.4, proven next door; asserted here again
            end to end because the forward fix must not break it).

A fix that satisfies only one of these is worse than no fix.  Continuing by
forgetting what the crawl had seen re-submits the application; refusing to
re-walk keeps the boundary safe and leaves the journey permanently half-done
while the report says ``completed``.

THE KILL IS A TRUNCATION, byte for byte the prefix a SIGKILL leaves: the
manifest is cut immediately after the Nth page_state record, so everything
before it is durable and nothing after it ever happened.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import emit
from app.budget import Budget
from app.crawler import Crawler
from app.guard_context import GuardContext
from tests.characterization.harness import (_REFUSE_PACK, ScriptedBrowser,
                                            ScriptedPage, TickClock,
                                            advance_oracle_stub, control,
                                            disposable_attestation, no_sleep,
                                            vision_oracle_stub)

CRAWL_ID = "c1-resume-journey"
HOST = "https://app.c1"
WIZARD_URL = f"{HOST}/apply"
DONE_URL = f"{HOST}/apply/confirmed"
STEPS = 10
#: The step the crawl is killed at — the exit criterion's "step 6 of 10".
KILL_AT_STEP = 6


def _wizard(n: int = STEPS) -> dict:
    """An ``n``-step questionnaire served from ONE url, ending at a submit.

    Each step declares its own radio group, so the steps are genuinely
    distinguishable and a walk that reaches step 10 has really moved ten times
    (``test_same_shape_traversal_e2e`` covers the opposite, indistinguishable
    case — this fixture must never be used to argue that point).
    """
    pages: dict[str, ScriptedPage] = {}
    for i in range(1, n + 1):
        last = i == n
        forward = "Submit Application" if last else "Continue"
        pages[f"q{i:02d}"] = ScriptedPage(
            url=WIZARD_URL, title="Application",
            controls=[
                control("radio", "Yes", tag="input", input_type="radio",
                        kind="radio", group_key=f"name:app:q{i:02d}"),
                control("radio", "No", tag="input", input_type="radio",
                        kind="radio", group_key=f"name:app:q{i:02d}"),
                control("button", forward, tag="button"),
            ],
            transitions={forward: ("done" if last else f"q{i + 1:02d}")},
        )
    pages["done"] = ScriptedPage(
        url=DONE_URL, title="Application Submitted",
        controls=[control("link", "Back Home", href="/apply")],
        statuses=["Your application has been submitted."],
        texts=["Your application has been submitted. Reference APP-8891."],
        displayed_values=[{"label": "Reference", "selector": "#ref",
                           "text": "APP-8891"}],
    )
    return pages


def _build(work_dir: Path, *, resume: bool, pages: dict | None = None) -> Crawler:
    port = ScriptedBrowser(pages or _wizard(), "q01")
    crawler = Crawler(
        port, crawl_id=CRAWL_ID, tenant_id="c1-tenant", target_url=WIZARD_URL,
        work_dir=str(work_dir), refuse_pack=_REFUSE_PACK,
        budget=Budget(rate_per_s=0), explorer_version="c1/1.0",
        guard_version="c1", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="c1-fp",
        guard_context=GuardContext(refuse_pack=_REFUSE_PACK,
                                   attestation=disposable_attestation()),
        sleep=no_sleep, advance_oracle=advance_oracle_stub(None),
        vision_oracle=vision_oracle_stub(None),
        submit_approvals=["*"], crawl_mode="e2e", wizard_enabled=True,
        e2e_wizard_steps=60, resume=resume)
    if hasattr(port, "bind_crawler"):
        port.bind_crawler(crawler)
    return crawler


def _records(work_dir: Path) -> list[dict]:
    return list(emit.read_records(str(work_dir), CRAWL_ID))


def _page_states(work_dir: Path) -> list[dict]:
    return [r for r in _records(work_dir) if r.get("type") == emit.REC_PAGE_STATE]


def _crossings(work_dir: Path) -> list[dict]:
    return [r for r in _records(work_dir) if r.get("type") == emit.REC_CROSSING]


def _kill_after_step(work_dir: Path, step: int) -> int:
    """Truncate the manifest right after the ``step``-th page_state record.

    Returns how many page_state records survive.  This is the whole kill: a
    SIGKILL between two fsynced appends leaves exactly this prefix.
    """
    manifest = emit.manifest_path(str(work_dir), CRAWL_ID)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    seen = 0
    for i, line in enumerate(lines):
        if json.loads(line).get("type") == emit.REC_PAGE_STATE:
            seen += 1
            if seen == step:
                manifest.write_text("\n".join(lines[:i + 1]) + "\n",
                                    encoding="utf-8")
                return seen
    raise AssertionError(
        "the first run recorded only %d page states; cannot kill at step %d"
        % (seen, step))


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(emit, "MonotonicClock", TickClock)
    return monkeypatch


# ─── the baseline: what an UNINTERRUPTED run of this journey looks like ──────

def test_the_uninterrupted_journey_walks_ten_steps_and_crosses_once(frozen, tmp_path):
    """PRECONDITION for everything below.  Without it the resume assertions
    could pass against a journey that never worked in the first place."""
    work = tmp_path / "w"
    work.mkdir()
    summary = asyncio.run(_build(work, resume=False).run())

    flows = summary.coverage["flows"]
    assert flows, "the crawl walked no journey at all"
    deepest = max(int(f.get("step_count") or 0) for f in flows)
    assert deepest >= STEPS, (
        "the fixture's own journey does not reach %d steps (deepest=%d) — fix "
        "the fixture before trusting any resume claim" % (STEPS, deepest))
    crossed = [c for c in _crossings(work) if c.get("status") == "crossed"]
    assert len(crossed) == 1, crossed


# ─── FORWARD: the journey must CONTINUE ─────────────────────────────────────

#: MEASURED ON THIS COMMIT, not predicted: with a manifest truncated after step
#: 6 of 10, the resumed crawl appends ZERO page states and reports
#: ``stop_reason='completed'``.  The journey is abandoned half-walked and the
#: verdict says it finished.
#:
#: WHY THE TWO TESTS BELOW ARE STRICT-XFAIL RATHER THAN DELETED OR RELAXED.
#: The gate is the point: a relaxed assertion ("the resume did not crash") would
#: pass on the defect forever, and deleting it would leave the defect unmeasured
#: — which is the state that let resume ship wired-but-never-exercised in the
#: first place.  ``strict=True`` means the day the engine fixes this these turn
#: into XPASS *failures*, so the marker cannot outlive its subject: whoever
#: lands the fix is told, by a red suite, to delete these two lines.
#:
#: THE CAUSE, for whoever removes it.  A funnel is walked inside ONE expansion
#: of ONE url, so a kill mid-wizard loses progress the frontier never described.
#: The resumed crawl re-navigates, re-observes step 1, finds its fingerprint in
#: the restored visited-set and takes ``_expand``'s unique-state early return
#: (``discovery.py``), so ``_walk_wizard`` is never entered.  A fix has to let a
#: CHECKPOINTED, still-queued item replay its already-seen entry state exactly
#: once — and must not become a blanket dedup bypass, or a crawl would record
#: every state twice.  The crossing ledger stays authoritative either way, which
#: is what ``test_a_kill_after_the_crossing_never_crosses_again`` below holds on
#: to while this is open.
_RESUME_MID_JOURNEY_DEFECT = pytest.mark.xfail(
    strict=True,
    reason=(
        "a crawl killed inside a wizard resumes and walks nothing: the entry "
        "step is in the restored visited-set, so _expand returns before the "
        "walk. Remove this marker (both uses) with the fix that lets a "
        "checkpointed in-flight item replay its entry state once."),
)


@_RESUME_MID_JOURNEY_DEFECT
def test_a_journey_killed_at_step_6_is_finished_by_the_resume(frozen, tmp_path):
    """THE C1 EXIT CRITERION, in the engine: kill at step 6 of 10, resume, and
    the journey reaches its end.

    THE DEFECT THIS GATES.  A funnel is walked inside ONE expansion of ONE url.
    The resumed crawl re-navigates to that url, observes step 1, finds its
    fingerprint in the restored visited-set and takes ``_expand``'s unique-state
    early return — so the walk is never entered and steps 7..10 are never
    reached.  The crawl then reports ``completed`` on its inherited evidence,
    which is a half-walked journey wearing a finished journey's verdict.
    """
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(work, resume=False).run())
    survived = _kill_after_step(work, KILL_AT_STEP)
    assert survived == KILL_AT_STEP

    summary = asyncio.run(_build(work, resume=True).run())

    # The evidence, read off the manifest rather than off the summary: the
    # resumed run must have appended steps the killed run never recorded.
    after = _page_states(work)
    assert len(after) > KILL_AT_STEP, (
        "THE DEFECT: the resume added no page state at all — the journey was "
        "abandoned at step %d and the crawl reported %r"
        % (KILL_AT_STEP, summary.stop_reason))

    # And the journey itself must have reached its end, not merely grown.
    flows = summary.coverage["flows"]
    deepest = max([int(f.get("step_count") or 0) for f in flows] or [0])
    assert deepest >= STEPS - KILL_AT_STEP, (
        "the resumed run walked only %d steps; the journey it inherited had %d "
        "of %d left to walk" % (deepest, STEPS - KILL_AT_STEP, STEPS))
    assert any(f.get("completed") for f in flows), (
        "no journey in the resumed run reached a terminal — the funnel is "
        "still unfinished and nothing says so")


@_RESUME_MID_JOURNEY_DEFECT
def test_the_resumed_run_crosses_the_boundary_it_never_reached(frozen, tmp_path):
    """A kill BEFORE the crossing must leave the crossing available.

    The backward guarantee (never cross twice) is worthless if it is achieved
    by never crossing at all, so this is asserted separately from the refusal
    tests: the boundary the killed run never reached IS crossed by the resume,
    exactly once.
    """
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(work, resume=False).run())
    _kill_after_step(work, KILL_AT_STEP)
    assert not _crossings(work), (
        "precondition: the truncated prefix must predate the crossing")

    asyncio.run(_build(work, resume=True).run())

    crossed = [c for c in _crossings(work) if c.get("status") == "crossed"]
    assert len(crossed) == 1, (
        "the resumed journey crossed %d times; exactly one is the whole claim: "
        "%r" % (len(crossed), crossed))


# ─── CONTINUING MUST NOT MEAN RE-RECORDING ──────────────────────────────────

def test_a_resumed_journey_never_records_one_state_twice(frozen, tmp_path):
    """A resume that continues by RE-WALKING must not re-record what it re-walks.

    THE TRAP THIS GATES, and it is the failure mode a fix for the two xfails
    above walks straight into.  The cheapest way to make a killed journey finish
    is to let the resumed crawl replay its entry state and walk the funnel again
    from step 1.  The walk then records every step it re-walks, so the six steps
    the killed run had already made durable are appended a SECOND time under the
    SAME ``ax_fingerprint``.

    Nothing downstream refuses that.  ``ax_fingerprint`` is a manifest-only field
    (``manifest_mapper.MANIFEST_ONLY_PAGE_FIELDS``) and is dropped before the
    bundle exists, so the mapper cannot dedup by identity even in principle; it
    emits one bundle page per record and the substrate writes one ``page_visit``
    row per page.  Every evidence rule the bundle schema checks —
    strictly-increasing ``sequence_index``, non-decreasing ``first_seen_ms``,
    unique ``frame_index`` — is satisfied by a replay, because the resumed run
    continues all three counters honestly.

    So the corruption is silent and it is a DOUBLE COUNT of the client's
    application: an eleven-state app reported as eighteen page visits, with the
    crawl's own ``coverage.states`` (deduped by fingerprint) disagreeing with the
    manifest the bundle is built from.

    The skip below is deliberate and is not a hole: while the resume records
    nothing at all there is genuinely nothing to deduplicate, and a silent pass
    in that state would be a check that holds with its subject absent.  It names
    the open defect instead.
    """
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(work, resume=False).run())
    _kill_after_step(work, KILL_AT_STEP)
    before = [r.get("ax_fingerprint") for r in _page_states(work)]

    asyncio.run(_build(work, resume=True).run())

    after = [r.get("ax_fingerprint") for r in _page_states(work)]
    if len(after) == len(before):
        pytest.skip(
            "the resume-mid-journey defect is still open (see the strict xfails "
            "above): nothing was replayed, so there is nothing to deduplicate")

    seen: dict[str, int] = {}
    for fp in after:
        seen[fp] = seen.get(fp, 0) + 1
    duplicated = {fp: n for fp, n in seen.items() if n > 1}
    assert not duplicated, (
        "the resumed crawl re-recorded %d state(s) it had already made durable, "
        "so this application's evidence now double-counts: %d page_state records "
        "for %d distinct states. Continue the journey without re-recording its "
        "durable prefix — or dedup at record time. Duplicated: %r"
        % (len(duplicated), len(after), len(seen), sorted(duplicated)))


# ─── BACKWARD: the journey must NOT re-cross ────────────────────────────────

def test_a_kill_after_the_crossing_never_crosses_again(frozen, tmp_path):
    """The forward fix must not reopen M3.4.  Kill AFTER the boundary was
    reserved and the resumed journey must refuse it — however far it walks."""
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(work, resume=False).run())
    before = _crossings(work)
    assert before, "precondition: the first run must cross something"
    spent = {c["boundary_key"] for c in before if c.get("status") != "refused"}

    asyncio.run(_build(work, resume=True).run())

    after = _crossings(work)
    fresh = [c for c in after[len(before):] if c.get("status") != "refused"]
    re_crossed = [c for c in fresh if c.get("boundary_key") in spent]
    assert not re_crossed, (
        "DUPLICATE IRREVERSIBLE ACTION: the resumed journey crossed a boundary "
        "the killed run had already crossed: %r" % (re_crossed,))
