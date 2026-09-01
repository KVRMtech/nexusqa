"""M3.4 / T-RS-01 - A RESUMED CRAWL MUST NOT RE-CROSS AN IRREVERSIBLE BOUNDARY.

THE DEFECT THIS GATES.  ``CrossingLedger`` reserves a boundary BEFORE the click
precisely so that a crash mid-crossing leaves it spent - a duplicate irreversible
action is unrecoverable, a missing outcome milestone is not.  That discipline was
correct and it protected nothing across a restart, because the ledger was built
empty in ``Crawler.__init__`` on every start, resume included, and lived only in
RAM.  Exactly-once therefore held for exactly as long as the process did, and the
event it exists to survive - a killed worker - is the event that destroyed it.

WHAT IS PROVEN HERE, against a real ``ManifestEmitter`` on a real temp volume:

  1. the crossing is journalled to the durable manifest BEFORE the click;
  2. a kill that lands between the reservation and the outcome still leaves the
     reservation durable (the write-ahead ordering);
  3. a resume INHERITS that journal and REFUSES the boundary;
  4. the refusal is honest - the resumed crawl still runs, and still catalogues.

The kill is simulated by TRUNCATING the manifest immediately after the reserved
record, which is precisely the prefix a SIGKILL between the write-ahead append
and the landing leaves behind.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import emit
from app import resume_state
from app.boundary import CROSSING_REFUSED, CrossingLedger
from app.budget import Budget
from app.crawler import Crawler
from app.guard_context import GuardContext
from tests.characterization.fixtures import F3_SUBMIT
from tests.characterization.harness import (_REFUSE_PACK, Fixture, ScriptedBrowser,
                                            ScriptedPage, TickClock,
                                            advance_oracle_stub, control, no_sleep,
                                            vision_oracle_stub)

CRAWL_ID = "m34-resume-crawl"


def _build(fixture, work_dir: Path, *, resume: bool) -> Crawler:
    port = ScriptedBrowser(fixture.pages, fixture.start)
    kwargs = dict(fixture.kwargs)
    guard_ctx = kwargs.pop("guard_context", None) or GuardContext(
        refuse_pack=_REFUSE_PACK)
    crawler = Crawler(
        port, crawl_id=CRAWL_ID, tenant_id="m34-tenant",
        target_url=fixture.target_url, work_dir=str(work_dir),
        refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="m34/1.0", guard_version="m34",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="m34-fp",
        guard_context=guard_ctx, sleep=no_sleep,
        advance_oracle=advance_oracle_stub(fixture.advance_reply),
        vision_oracle=vision_oracle_stub(fixture.vision_reply),
        resume=resume, **kwargs)
    if hasattr(port, "bind_crawler"):
        port.bind_crawler(crawler)
    return crawler


def _records(work_dir: Path) -> list:
    return list(emit.read_records(str(work_dir), CRAWL_ID))


def _crossings(work_dir: Path) -> list:
    return [r for r in _records(work_dir) if r.get("type") == emit.REC_CROSSING]


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(emit, "MonotonicClock", TickClock)
    return monkeypatch


def test_a_crossing_is_journalled_before_the_click(frozen, tmp_path):
    """The write-ahead record exists, and it is RESERVED before any outcome."""
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(F3_SUBMIT, work, resume=False).run())

    crossings = _crossings(work)
    assert crossings, "the crawl crossed a boundary and journalled nothing"

    # The FIRST journal record for a crossing is the reservation, and it carries
    # no outcome - proof it was written ahead of the click rather than after it.
    first = crossings[0]
    assert first["status"] == "reserved", first
    assert first["outcome"] == "", "a reserved record must predate its outcome"
    assert first["completed_at_ms"] == 0
    assert first["control_name"], "a journal entry with no control dedups nothing"


def test_a_kill_between_reservation_and_landing_leaves_the_boundary_spent(
        frozen, tmp_path):
    """THE CORE PROOF: kill mid-crossing, resume, and the boundary is refused."""
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(F3_SUBMIT, work, resume=False).run())

    crossings = _crossings(work)
    assert crossings, "precondition: the first run must cross something"
    crossed = crossings[0]

    # -- THE KILL ----------------------------------------------------------
    # Truncate the manifest immediately after the write-ahead record. That is
    # byte-for-byte the prefix a SIGKILL between the reservation append and the
    # landing leaves: the reservation is durable, the outcome never happened.
    manifest = emit.manifest_path(str(work), CRAWL_ID)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if json.loads(line).get("type") == emit.REC_CROSSING:
            manifest.write_text("\n".join(lines[:i + 1]) + "\n", encoding="utf-8")
            break
    else:                                                    # pragma: no cover
        pytest.fail("no crossing record to truncate at")

    survived = _crossings(work)
    assert len(survived) == 1 and survived[0]["status"] == "reserved", (
        "the kill must leave exactly the reservation behind")

    # -- THE RESUME --------------------------------------------------------
    resumed = _build(F3_SUBMIT, work, resume=True)
    assert resumed._crossings.is_spent(
        control_name=crossed["control_name"], url=crossed["url"],
        state_fingerprint=crossed["state_fingerprint"]), (
        "THE DEFECT: the resumed crawl believes this boundary is uncrossed and "
        "will submit the same application a second time")

    # Supporting diagnostic, asserted AFTER the behavioural claim above so a
    # regression reports the duplicate submit rather than an internal counter.
    assert resumed._crossings.inherited == 1

    asyncio.run(resumed.run())

    # The resumed run must not have RESERVED this boundary again.
    fresh = [r for r in _crossings(work)
             if r.get("crossing_id") != crossed["crossing_id"]]
    re_reserved = [r for r in fresh
                   if r.get("status") != CROSSING_REFUSED
                   and r.get("boundary_key") == crossed["boundary_key"]]
    assert not re_reserved, (
        "DUPLICATE IRREVERSIBLE ACTION: the resumed crawl crossed a boundary "
        "the killed run had already crossed: %r" % (re_reserved,))


def test_the_resumed_crawl_still_runs_and_still_catalogues(frozen, tmp_path):
    """Refusing a spent boundary must not turn a resume into a dead crawl."""
    work = tmp_path / "w"
    work.mkdir()
    asyncio.run(_build(F3_SUBMIT, work, resume=False).run())
    before = len([r for r in _records(work)
                  if r.get("type") == emit.REC_PAGE_STATE])
    assert before, "precondition: the first run catalogued something"

    summary = asyncio.run(_build(F3_SUBMIT, work, resume=True).run())
    assert summary.stop_reason != "resume_unrecoverable", summary.detail
    # It inherits the prior evidence rather than reporting an empty re-crawl.
    plan = resume_state.rebuild(_records(work), resuming=True)
    assert plan.recoverable and plan.prior_states >= before


def test_a_refusal_never_spends_a_boundary():
    """An operator who issues a grant AFTER a refusal must be able to cross.

    Restoring refusals as spent would make a re-dispatch-after-approval a silent
    no-op - the boundary would be barred forever by the record of it being
    barred once.
    """
    ledger = CrossingLedger()
    honoured = ledger.restore([
        {"control_name": "Submit Application", "url": "https://app/x",
         "state_fingerprint": "fp1", "boundary_key": "bnd_abc",
         "status": CROSSING_REFUSED},
    ])
    assert honoured == 1
    assert not ledger.is_spent(control_name="Submit Application",
                               url="https://app/x", state_fingerprint="fp1")


def test_a_crossing_journal_alone_is_a_durable_prefix():
    """A crawl killed after its only crossing but before its first page_state
    must still be resumable - dropping that journal re-authorises the boundary.
    """
    plan = resume_state.rebuild(
        [{"type": emit.REC_CROSSING, "control_name": "Pay",
          "url": "https://app/pay", "state_fingerprint": "fp",
          "boundary_key": "bnd_pay", "status": "reserved"}],
        resuming=True)
    assert plan.recoverable, plan.refusal
    assert len(plan.crossings) == 1


def _ten_step_boundary_fixture() -> Fixture:
    """A real same-URL ten-step journey whose sole boundary is its end.

    The distinctive radio group is deliberate: this test proves durable resume,
    not the separate identity-negative-control behaviour.  A production wizard
    commonly keeps one URL while changing its declared question on each step.
    """
    pages = {}
    for step in range(1, 11):
        advance = "Submit Application" if step == 10 else "Continue"
        next_key = "done" if step == 10 else f"step-{step + 1}"
        pages[f"step-{step}"] = ScriptedPage(
            url="https://app.char/apply", title="Ten step application",
            controls=[
                control("radio", "Yes", tag="input", input_type="radio",
                        kind="radio", group_key=f"name:doc:q{step}"),
                control("radio", "No", tag="input", input_type="radio",
                        kind="radio", group_key=f"name:doc:q{step}"),
                control("button", advance, tag="button"),
            ],
            transitions={advance: next_key},
        )
    pages["done"] = ScriptedPage(
        url="https://app.char/apply/confirmed", title="Confirmed",
        controls=[control("link", "Back Home", href="/apply")],
    )
    return Fixture(
        name="ten_step_boundary_resume", pages=pages, start="step-1",
        target_url="https://app.char/apply",
        kwargs={
            "submit_approvals": ["*"],
            "guard_context": GuardContext(
                refuse_pack=_REFUSE_PACK, attestation=F3_SUBMIT.kwargs[
                    "guard_context"].attestation),
            "crawl_mode": "e2e", "wizard_enabled": True,
            "e2e_wizard_steps": 20,
        },
    )


class _KilledAtStepSix(ScriptedBrowser):
    """Simulate SIGKILL just after the ordinary step-six advance starts."""

    async def click(self, control):
        observation = await super().click(control)
        if self._key == "step-7":
            raise SystemExit("simulated worker kill at step 6")
        return observation


def test_kill_at_step_six_resumes_the_ten_step_journey_without_recrossing(
        frozen, tmp_path):
    """Team C exit drill: resume the journal, finish 10 steps, cross once."""
    fixture = _ten_step_boundary_fixture()
    work = tmp_path / "w"
    work.mkdir()

    # Build the interrupted crawl with the normal production Crawler, changing
    # only the port's process-death behaviour.  ``SystemExit`` bypasses the
    # crawler's ordinary Exception handler just as SIGKILL bypasses Python.
    port = _KilledAtStepSix(fixture.pages, fixture.start)
    kwargs = dict(fixture.kwargs)
    crawler = Crawler(
        port, crawl_id=CRAWL_ID, tenant_id="m34-tenant",
        target_url=fixture.target_url, work_dir=str(work),
        refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="m34/1.0", guard_version="m34",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="m34-fp",
        sleep=no_sleep, advance_oracle=advance_oracle_stub(fixture.advance_reply),
        vision_oracle=vision_oracle_stub(fixture.vision_reply), **kwargs)
    port.bind_crawler(crawler)
    with pytest.raises(SystemExit, match="step 6"):
        asyncio.run(crawler.run())

    before = _records(work)
    checkpoints = [r for r in before if r.get("type") == emit.REC_CHECKPOINT]
    assert checkpoints and checkpoints[-1].get("frontier"), (
        "the kill must leave its in-flight journey in the durable checkpoint")
    assert not _crossings(work), "the pre-kill half has not reached the boundary"

    summary = asyncio.run(_build(fixture, work, resume=True).run())
    assert summary.stop_reason == "completed", summary.detail
    milestones = [r for r in _records(work)
                  if r.get("type") == emit.REC_OUTCOME_MILESTONE]
    assert any(r.get("url_after", "").endswith("/confirmed") for r in milestones), (
        "resume stopped at the already-seen entry instead of completing step 10")
    spent_ids = {r.get("crossing_id") for r in _crossings(work)
                 if r.get("status") != CROSSING_REFUSED}
    assert len(spent_ids) == 1, "the terminal boundary was crossed more than once"
