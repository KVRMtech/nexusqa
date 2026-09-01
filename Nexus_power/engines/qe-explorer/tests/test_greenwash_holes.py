"""M1.7 — THE GREEN-WASH PROVING FRAMEWORK (T-GW-01 … T-GW-05).

Every test here asserts a NEGATIVE: that some path which used to report success
no longer can.  They are grouped by the invariant they defend, and each group
starts with a REGRESSION test — one that fails on the pre-M1.7 code — so the
suite is evidence that the holes were closed, not merely that the new code runs.

The invariants, restated as the assertions below enforce them:

    a crawl without evidence   != completed
    a callback without durable state != success
    a crash                    != completion
    a resume                   != a new crawl
    discovered knowledge       != temporary memory
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import completion, completion_manifest, observation_health, resume_state, rules
from app.browser import BrowserPort, NavResult, PageObservation, RawObservation
from app.crawl_constants import (
    STOP_COMPLETED,
    STOP_INVENTORY_FAILED,
    STOP_NO_EVIDENCE,
    STOP_RESUME_UNRECOVERABLE,
)
from app.crawler import Budget, Crawler, GuardContext
from app.frontier import Frontier, FrontierItem
from app.guard import load_refuse_pack
from app.state_identity import CorruptObservationError, StateFingerprinter

_REFUSE_PACK = load_refuse_pack(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "app", "refuse_pack.yaml")
)

PNG_1x1 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


# ══════════════════════════════════════════════════════════════════════════════
# A scripted port whose inventory read can be made to FAIL, not merely be empty.
# ══════════════════════════════════════════════════════════════════════════════


class ScriptedPort(BrowserPort):
    """A fake browser whose inventory read is independently controllable.

    ``inventory_raises`` makes ``collect_controls_result`` raise the given
    exception — the shape a real ``page.evaluate(INVENTORY_JS)`` failure takes.
    ``inventory_payload`` makes it return a NON-list, i.e. observation
    corruption.  Everything else behaves like an ordinary scripted page so the
    difference under test is exactly the one being tested.
    """

    def __init__(self, pages: dict, start_url: str) -> None:
        self._pages = dict(pages)
        self._current = start_url
        self.inventory_raises: BaseException | None = None
        self.inventory_payload: object = None
        self.inventory_reads = 0
        #: Fail only the first N reads, then recover — for the retry test.
        self.fail_first = 0

    async def goto(self, url: str) -> NavResult:
        self._current = url
        return NavResult(url=url, ok=url in self._pages)

    async def current_url(self) -> str:
        return self._current

    async def title(self) -> str:
        return ""

    async def collect_controls_result(self):
        self.inventory_reads += 1
        if self.fail_first and self.inventory_reads <= self.fail_first:
            return observation_health.InventoryResult.from_exception(
                RuntimeError("Execution context was destroyed"))
        if self.inventory_raises is not None:
            return observation_health.InventoryResult.from_exception(self.inventory_raises)
        if self.inventory_payload is not None:
            return observation_health.InventoryResult.from_payload(self.inventory_payload)
        return observation_health.InventoryResult.healthy(
            [dict(c) for c in self._pages.get(self._current, [])])

    async def collect_controls(self):
        return (await self.collect_controls_result()).as_list()

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        return RawObservation(url_before=self._current, url_after=self._current)

    async def hover(self, control):
        return RawObservation(url_before=self._current, url_after=self._current)

    async def fill(self, control, value):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value="true" if checked else "false")

    async def storage_state(self):
        return {"cookies": [], "origins": []}


class LegacyPort(ScriptedPort):
    """A port that implements ONLY ``collect_controls`` — every fake written
    before M1.7, and the jsdom lane.  Its ``[]`` genuinely IS the page it is
    pretending to be, so it must degrade to a HEALTHY read."""

    collect_controls_result = None       # type: ignore[assignment]

    async def collect_controls(self):
        self.inventory_reads += 1
        return [dict(c) for c in self._pages.get(self._current, [])]


class ProtocolSubclassFake(BrowserPort):
    """A fake that SUBCLASSES the port and never mentions the checked read.

    This is the shape of every pre-M1.7 fake in this repository, and the shape
    that broke when ``collect_controls_result`` was first added: the class
    inherits ``BrowserPort``'s ``...`` body, so ``getattr`` finds the method and
    awaiting it returns ``None``.
    """

    def __init__(self, pages: dict, start_url: str) -> None:
        self._pages = dict(pages)
        self._current = start_url

    async def goto(self, url: str) -> NavResult:
        self._current = url
        return NavResult(url=url, ok=url in self._pages)

    async def current_url(self) -> str:
        return self._current

    async def title(self) -> str:
        return ""

    async def collect_controls(self):
        return [dict(c) for c in self._pages.get(self._current, [])]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        return RawObservation(url_before=self._current, url_after=self._current)

    async def storage_state(self):
        return {"cookies": [], "origins": []}


async def _no_sleep(_seconds: float) -> None:
    return None


def _crawler(port, work_dir, **kwargs):
    return Crawler(
        port,
        crawl_id=kwargs.pop("crawl_id", "gw1"), tenant_id="t1",
        target_url=kwargs.pop("target_url", "https://app.test/home"),
        work_dir=str(work_dir), refuse_pack=_REFUSE_PACK,
        budget=Budget(rate_per_s=0), explorer_version="test/1.0",
        guard_version="test", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE_PACK),
        sleep=_no_sleep, **kwargs,
    )


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-01 — a failed inventory read is NOT an empty page
# ══════════════════════════════════════════════════════════════════════════════


def test_inventory_failure_is_distinguishable_from_an_empty_page():
    """THE ROOT DEFECT, at the smallest scale it exists at.

    Both of these produce zero controls.  Before M1.7 both produced ``[]`` and
    nothing downstream could tell them apart, which is the entire green-wash
    chain in one line.
    """
    empty = observation_health.InventoryResult.healthy([])
    failed = observation_health.InventoryResult.from_exception(
        RuntimeError("ReferenceError: x is not defined"))

    assert empty.as_list() == failed.as_list() == []      # indistinguishable BY CONTENT
    assert empty.ok and not failed.ok                    # distinguishable BY HEALTH
    assert failed.status == observation_health.INVENTORY_EVAL_FAILED
    assert "ReferenceError" in failed.diagnostic()


@pytest.mark.parametrize("message,expected", [
    ("Execution context was destroyed, most likely because of a navigation",
     observation_health.INVENTORY_CONTEXT_LOST),
    ("Target page, context or browser has been closed",
     observation_health.INVENTORY_CONTEXT_LOST),
    ("Timeout 30000ms exceeded", observation_health.INVENTORY_TIMEOUT),
    ("TypeError: Cannot read properties of null",
     observation_health.INVENTORY_EVAL_FAILED),
])
def test_inventory_errors_are_classified_by_cause(message, expected):
    assert observation_health.classify_inventory_error(RuntimeError(message)) == expected


def test_corrupt_payload_is_not_silently_filtered():
    """Observation CORRUPTION: something answered, and what it said is not the
    contract.  Filtering it down to a plausible inventory would be the original
    bug wearing a different coat."""
    assert observation_health.InventoryResult.from_payload("not-a-list").status == \
        observation_health.INVENTORY_MALFORMED
    assert observation_health.InventoryResult.from_payload([{"name": "ok"}, 7]).status == \
        observation_health.INVENTORY_MALFORMED
    # None is the documented "walker found nothing" shape and predates this module.
    assert observation_health.InventoryResult.from_payload(None).ok


def test_fingerprint_refuses_a_corrupted_observation():
    """THE CHOKE POINT.  A failed read must not be able to mint an identity."""
    fingerprinter = StateFingerprinter()
    controls = [{"name": "Continue", "kind": "button", "role": "button"}]

    healthy = fingerprinter.fingerprint(url="https://app.test/x", controls=controls)
    assert len(healthy) == 64

    with pytest.raises(CorruptObservationError) as caught:
        fingerprinter.fingerprint(url="https://app.test/x", controls=[],
                                  observation_ok=False)
    assert "https://app.test/x" in str(caught.value)


def test_forced_inventory_crash_terminates_the_crawl_as_failed(tmp_path):
    """T-GW-01 ACCEPTANCE — the whole chain, end to end.

    A page whose inventory read throws used to record as an empty state, drain
    the frontier and report ``completed``.  It must now terminate the crawl as
    ``inventory_failed`` with a named diagnosis, and the completion adjudicator
    must refuse the success claim.
    """
    port = ScriptedPort({"https://app.test/home": [{"name": "Go", "kind": "link",
                                                    "role": "link", "href": "/next"}]},
                        "https://app.test/home")
    port.inventory_raises = RuntimeError("ReferenceError: Array.prototype.map is not a function")
    crawler = _crawler(port, tmp_path)

    summary = asyncio.run(crawler.run())

    assert summary.stop_reason == STOP_INVENTORY_FAILED
    assert summary.disposition == completion.DISPOSITION_FAILED
    assert summary.states == 0
    assert "ReferenceError" in summary.detail
    # And the failure is explainable from the DURABLE artefact, not only the log.
    records = [json.loads(line) for line in
               (tmp_path / "gw1" / "manifest.jsonl").read_text().splitlines() if line]
    guard_events = [r for r in records if r.get("type") == "guard_event"]
    assert any(e.get("kind") == "inventory_failed" for e in guard_events)


def test_transient_inventory_failure_is_retried_once_and_recovers(tmp_path):
    """A page that MOVED under the read is worth one re-read; a page that is
    BROKEN is not.  The crawl must survive the first and fail on the second."""
    port = ScriptedPort({"https://app.test/home": [{"name": "Go", "kind": "link",
                                                    "role": "link", "href": "/next"}]},
                        "https://app.test/home")
    port.fail_first = 1                       # one context-lost read, then healthy
    crawler = _crawler(port, tmp_path)

    summary = asyncio.run(crawler.run())

    assert summary.stop_reason != STOP_INVENTORY_FAILED
    assert summary.states >= 1
    assert port.inventory_reads >= 2          # it really did re-read


def test_a_legitimately_empty_page_still_completes(tmp_path):
    """THE OTHER HALF OF THE CONTRACT, and the one a careless fix breaks.

    A confirmation page with no interactive controls is a real, coverable state.
    Refusing it would trade a false success for a false failure.
    """
    port = ScriptedPort({"https://app.test/home": []}, "https://app.test/home")
    summary = asyncio.run(_crawler(port, tmp_path).run())

    assert summary.stop_reason == STOP_COMPLETED
    assert summary.disposition == completion.DISPOSITION_COMPLETED
    assert summary.states == 1


def test_a_protocol_subclass_stub_is_not_mistaken_for_an_empty_page(tmp_path):
    """THE REGRESSION THIS FIX ITSELF CAUSED, pinned so it cannot come back.

    ``BrowserPort`` is a ``Protocol`` and every fake in this suite SUBCLASSES it,
    so each inherits the protocol's ``...`` method bodies. ``getattr`` therefore
    finds ``collect_controls_result`` on a fake that never implemented it, and
    awaiting the inherited stub yields ``None``.

    Reading that ``None`` as an empty inventory reported every scripted
    application as a blank document — the exact confusion this milestone exists
    to remove, reintroduced by the code removing it. ``None`` means NOT
    IMPLEMENTED and must fall through to the legacy read.
    """
    pages = {"https://app.test/home": [{"name": "Go", "kind": "link", "role": "link",
                                        "href": "https://app.test/next"}],
             "https://app.test/next": []}
    port = ProtocolSubclassFake(pages, "https://app.test/home")

    # Prove the stub really is reachable and really yields None, or this test is
    # asserting nothing at all.
    assert getattr(port, "collect_controls_result", None) is not None
    assert asyncio.run(port.collect_controls_result()) is None

    summary = asyncio.run(_crawler(port, tmp_path).run())

    assert summary.stop_reason == STOP_COMPLETED
    assert summary.states == 2, "the stub's None was read as an empty page"


def test_a_port_without_the_checked_read_degrades_to_healthy(tmp_path):
    """BACKWARD COMPATIBILITY. Every fake port written before M1.7 implements
    only ``collect_controls``; its ``[]`` is the page it is pretending to be."""
    port = LegacyPort({"https://app.test/home": []}, "https://app.test/home")
    summary = asyncio.run(_crawler(port, tmp_path).run())

    assert summary.stop_reason == STOP_COMPLETED
    assert summary.states == 1


# ══════════════════════════════════════════════════════════════════════════════
# The completion state machine — a crawl without evidence != completed
# ══════════════════════════════════════════════════════════════════════════════


def test_zero_state_completion_is_impossible():
    verdict = completion.adjudicate(STOP_COMPLETED, completion.CrawlEvidence(states=0))
    assert verdict.stop_reason == STOP_NO_EVIDENCE
    assert verdict.disposition == completion.DISPOSITION_FAILED
    assert verdict.downgraded
    assert verdict.claimed_stop_reason == STOP_COMPLETED


def test_a_resumed_crawl_that_adds_nothing_still_completes():
    """The evidence test is on TOTAL states, not this run's.  A resume whose
    predecessor already covered the app has evidence; it simply did not add to
    it.  Judging on this run alone would fail every successful resume."""
    verdict = completion.adjudicate(
        STOP_COMPLETED,
        completion.CrawlEvidence(states=0, resumed_states=42, resumed=True))
    assert verdict.stop_reason == STOP_COMPLETED
    assert verdict.disposition == completion.DISPOSITION_COMPLETED
    assert not verdict.downgraded


def test_an_unrecovered_inventory_failure_defeats_a_completion_claim():
    verdict = completion.adjudicate(
        STOP_COMPLETED, completion.CrawlEvidence(states=9, inventory_failures=1))
    assert verdict.stop_reason == STOP_INVENTORY_FAILED
    assert verdict.disposition == completion.DISPOSITION_FAILED


def test_the_machine_never_upgrades_a_failure():
    """It may only pull a verdict DOWN.  A crawl that died with a traceback keeps
    ``error`` — relabelling it would discard the one field carrying the cause."""
    verdict = completion.adjudicate("error", completion.CrawlEvidence(states=50))
    assert verdict.stop_reason == "error"
    assert verdict.disposition == completion.DISPOSITION_FAILED
    assert not verdict.downgraded


def test_an_empty_stop_reason_is_never_a_success():
    assert completion.disposition_for("") == completion.DISPOSITION_FAILED
    assert completion.disposition_for("something_nobody_classified") == \
        completion.DISPOSITION_FAILED
    assert completion.disposition_for("budget_max_states") == \
        completion.DISPOSITION_COMPLETED


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-02 — a callback without durable state != success
# ══════════════════════════════════════════════════════════════════════════════


def test_completion_record_is_durable_and_atomic(tmp_path):
    body = {"crawl_id": "c9", "tenant_id": "t1", "exploration_id": "e1",
            "stop_reason": "completed"}
    completion_manifest.write_completion(str(tmp_path), "c9", body)

    assert completion_manifest.read_completion(str(tmp_path), "c9") == body
    # No temp files survive an atomic write.
    assert not list((tmp_path / "c9").glob(".completion-*.tmp"))


def test_an_unacked_completion_is_an_orphan(tmp_path):
    body = {"crawl_id": "c9", "tenant_id": "t1", "exploration_id": "e1"}
    completion_manifest.write_completion(str(tmp_path), "c9", body)

    assert completion_manifest.is_orphaned(str(tmp_path), "c9")
    pending = completion_manifest.pending_completions(str(tmp_path))
    assert [p.crawl_id for p in pending] == ["c9"]

    completion_manifest.mark_delivered(str(tmp_path), "c9")

    assert not completion_manifest.is_orphaned(str(tmp_path), "c9")
    assert completion_manifest.pending_completions(str(tmp_path)) == []


def test_a_corrupt_completion_record_is_treated_as_absent(tmp_path):
    (tmp_path / "c9").mkdir()
    (tmp_path / "c9" / completion_manifest.COMPLETION_FILENAME).write_text("{not json")

    assert completion_manifest.read_completion(str(tmp_path), "c9") is None
    assert completion_manifest.pending_completions(str(tmp_path)) == []


def test_an_unroutable_completion_is_not_retried_into_a_404():
    assert not completion_manifest.completion_body_is_sane({"crawl_id": "c9"})
    assert completion_manifest.completion_body_is_sane(
        {"crawl_id": "c9", "tenant_id": "t1", "exploration_id": "e1"})


def test_backoff_is_bounded_and_deterministic():
    delays = [completion_manifest.backoff_delay(n) for n in range(1, 8)]
    assert delays[0] == 0.0                        # the first attempt is immediate
    assert delays == sorted(delays)                # monotonic
    assert max(delays) <= completion_manifest.DEFAULT_MAX_DELAY_S
    # Deterministic: a fault-injection test can assert the schedule exactly.
    assert delays == [completion_manifest.backoff_delay(n) for n in range(1, 8)]


def test_attempts_are_logged_so_recovery_is_observable(tmp_path):
    completion_manifest.write_completion(str(tmp_path), "c9", {"crawl_id": "c9"})
    completion_manifest.record_attempt(str(tmp_path), "c9", attempt=1, ok=False,
                                       error="connection refused")
    completion_manifest.record_attempt(str(tmp_path), "c9", attempt=2, ok=True, status=200)

    attempts = completion_manifest.read_attempts(str(tmp_path), "c9")
    assert [a["ok"] for a in attempts] == [False, True]
    assert attempts[0]["error"] == "connection refused"


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-03 — a resume != a new crawl
# ══════════════════════════════════════════════════════════════════════════════


def test_frontier_stamps_its_reach_key_on_every_item():
    frontier = Frontier()
    item = FrontierItem(url="https://app.test/a/1")
    frontier.push(item, key="https://app.test/a/*")
    assert item.key == "https://app.test/a/*"
    assert frontier.spent_keys() == {"https://app.test/a/*"}


def test_checkpoint_round_trips_the_work_list():
    frontier = Frontier()
    for n in range(3):
        frontier.push(FrontierItem(url=f"https://app.test/p{n}", depth=n),
                      key=f"https://app.test/p{n}")
    frontier.pop()                                  # one consumed, two still queued

    record = resume_state.build_checkpoint(
        frontier=frontier.snapshot_items(), visited=set(), states=1, actions=2,
        spent_keys=frontier.spent_keys())
    plan = resume_state.rebuild([record], resuming=True)

    assert plan.recoverable
    assert len(plan.frontier) == 2
    # The consumed key is remembered; the QUEUED keys are not marked spent, or
    # re-pushing them would be rejected by their own dedup entries.
    queued = {item.key for item in plan.frontier}
    assert queued.isdisjoint(plan.spent_keys)
    assert len(plan.spent_keys) == 1


def test_restoring_a_checkpoint_actually_re_queues_the_work():
    """THE REGRESSION THAT MADE RESUME UNREACHABLE.

    Restore order is load-bearing: mark the spent keys first and every restored
    item is rejected by its own key, leaving an empty frontier — the zero-state
    completion, rebuilt by the code meant to prevent it.
    """
    original = Frontier()
    for n in range(4):
        original.push(FrontierItem(url=f"https://app.test/p{n}"),
                      key=f"https://app.test/p{n}")
    original.pop()

    plan = resume_state.rebuild([resume_state.build_checkpoint(
        frontier=original.snapshot_items(), visited=set(), states=1, actions=0,
        spent_keys=original.spent_keys())], resuming=True)

    restored = Frontier()
    requeued = sum(1 for snap in plan.frontier
                   if restored.push(FrontierItem(url=snap.url, depth=snap.depth),
                                    key=snap.key))
    restored.mark_spent(plan.spent_keys)

    assert requeued == 3
    assert len(restored) == 3
    # And the already-expanded route stays spent, so it is not walked twice.
    assert not restored.push(FrontierItem(url="https://app.test/p0"),
                             key="https://app.test/p0")


def test_a_resume_with_no_durable_prefix_fails_honestly():
    plan = resume_state.rebuild([], resuming=True)
    assert not plan.recoverable
    assert "nothing to continue" in plan.refusal


def test_a_fresh_crawl_with_no_prefix_is_not_a_failure():
    plan = resume_state.rebuild([], resuming=False)
    assert plan.recoverable and plan.refusal == ""


def test_resume_without_evidence_never_reports_a_zero_state_completion(tmp_path):
    """T-GW-03 ACCEPTANCE. The most destructive shape of the bug: a resume whose
    evidence volume is gone must NOT walk from zero and supersede a real crawl
    with an empty capture."""
    port = ScriptedPort({"https://app.test/home": []}, "https://app.test/home")
    summary = asyncio.run(_crawler(port, tmp_path, resume=True).run())

    assert summary.stop_reason == STOP_RESUME_UNRECOVERABLE
    assert summary.disposition == completion.DISPOSITION_FAILED
    assert summary.states == 0
    assert port.inventory_reads == 0            # it never even drove the browser


def test_a_killed_crawl_resumes_under_the_same_id_and_continues(tmp_path):
    """T-GW-05 SCENARIO — killed crawl -> resume -> continue -> complete.

    The first run is cut short by a state budget of one.  The second run is
    dispatched as a RESUME under the SAME crawl id, restores the frontier from
    the checkpoint, and covers the pages the first run never reached — appending
    to one manifest rather than starting a second.
    """
    pages = {
        "https://app.test/home": [
            {"name": "Alpha", "kind": "link", "role": "link",
             "href": "https://app.test/alpha"},
            {"name": "Beta", "kind": "link", "role": "link",
             "href": "https://app.test/beta"},
        ],
        "https://app.test/alpha": [],
        "https://app.test/beta": [],
    }

    first = Crawler(
        ScriptedPort(pages, "https://app.test/home"),
        crawl_id="kill1", tenant_id="t1", target_url="https://app.test/home",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK,
        budget=Budget(rate_per_s=0, max_states=1),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE_PACK), sleep=_no_sleep,
    )
    first_summary = asyncio.run(first.run())
    assert first_summary.states == 1
    assert first_summary.stop_reason.startswith("budget_")

    manifest = tmp_path / "kill1" / "manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_text().splitlines() if line]
    assert any(r.get("type") == "checkpoint" for r in records), \
        "the interrupted run must leave a durable work list"

    second = Crawler(
        ScriptedPort(pages, "https://app.test/home"),
        crawl_id="kill1",                       # THE SAME CRAWL ID
        tenant_id="t1", target_url="https://app.test/home",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK,
        budget=Budget(rate_per_s=0), explorer_version="test/1.0",
        guard_version="test", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE_PACK), sleep=_no_sleep,
        resume=True,
    )
    second_summary = asyncio.run(second.run())

    assert second_summary.crawl_id == "kill1"           # identity preserved
    assert second_summary.disposition == completion.DISPOSITION_COMPLETED
    assert second_summary.evidence["resumed"] is True
    assert second_summary.evidence["resumed_states"] == 1
    # It CONTINUED: the pages the budget cut off are covered on the second run.
    assert second_summary.states >= 1
    # ONE manifest, appended to — never a second crawl's worth of evidence.
    assert sorted(p.name for p in (tmp_path / "kill1").iterdir()
                  if p.suffix == ".jsonl") == ["manifest.jsonl"]
    after = [json.loads(line) for line in manifest.read_text().splitlines() if line]
    assert len(after) > len(records)


# ══════════════════════════════════════════════════════════════════════════════
# T-GW-04 — discovered knowledge != temporary memory
# ══════════════════════════════════════════════════════════════════════════════


def test_rule_identity_survives_ids_in_the_url():
    """Keyed on the URL TEMPLATE: two applicants on the same wizard step are one
    rule, not one rule per applicant."""
    a = rules.discover(url="https://app.test/application/8814/health",
                       blocked_label="Continue", field_label="None of these")
    b = rules.discover(url="https://app.test/application/9137/health",
                       blocked_label="Continue", field_label="None of these")
    assert a.key == b.key
    c = rules.discover(url="https://app.test/application/8814/health",
                       blocked_label="Continue", field_label="Type 2 Diabetes")
    assert c.key != a.key


def test_known_rules_lookup_is_label_normalised_and_measured():
    store = rules.KnownRules([rules.discover(
        url="https://app.test/apply/1/health", blocked_label="Continue",
        field_label="None of these", proof="proven").as_dict()])

    assert store.lookup(url="https://app.test/apply/77/health",
                        blocked_label="  CONTINUE ") is not None
    assert store.lookup(url="https://app.test/apply/77/other",
                        blocked_label="Continue") is None
    assert store.stats() == {"known": 1, "lookups": 2, "hits": 1, "misses": 1,
                             "reuse_rate": 0.5}


def test_an_empty_rule_store_is_pre_m17_behaviour():
    store = rules.KnownRules([])
    assert not store
    assert store.lookup(url="https://app.test/x", blocked_label="Continue") is None
    assert store.stats()["reuse_rate"] == 0.0     # never a flattering 1.0


def test_a_rule_from_a_newer_explorer_is_ignored_not_guessed_at():
    """FAIL-CLOSED on the schema version. One repeated experiment is far cheaper
    than acting on a record this reader would misinterpret."""
    future = rules.discover(url="https://app.test/x", blocked_label="Continue",
                            field_label="None").as_dict()
    future["schema_version"] = rules.RULE_SCHEMA_VERSION + 1
    assert rules.DiscoveredRule.from_mapping(future) is None
    assert len(rules.KnownRules([future])) == 0


def test_the_rule_ledger_dedupes_within_one_crawl():
    """A wizard revisits the same blocked step across branches; recording one
    rule per encounter would report the same discovery as several."""
    ledger = rules.RuleLedger()
    rule = rules.discover(url="https://app.test/x", blocked_label="Continue",
                          field_label="None")
    assert ledger.add(rule)
    assert not ledger.add(rule)
    assert len(ledger) == 1


def test_discovered_rules_reach_the_coverage_payload(tmp_path):
    """The producer side of the contract qe-central persists from."""
    port = ScriptedPort({"https://app.test/home": []}, "https://app.test/home")
    crawler = _crawler(port, tmp_path, known_rules=[rules.discover(
        url="https://app.test/home", blocked_label="Continue",
        field_label="None of these").as_dict()])

    summary = asyncio.run(crawler.run())

    assert "discovered_rules" in summary.coverage
    assert summary.coverage["rule_reuse"]["known"] == 1
    assert summary.discovered_rules == []       # this crawl proved none of its own
