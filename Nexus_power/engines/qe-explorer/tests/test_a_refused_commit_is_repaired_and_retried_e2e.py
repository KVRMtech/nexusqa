"""B2 END TO END — A NAMED REFUSAL DRIVES ONE REPAIR AND ONE RETRY.

    Walk the five-step wizard -> cross the commit under the operator's grant ->
    the schema refuses silently -> step back through EVERY step, naming the
    refused field with the application's own mask -> re-fill it where it lives,
    with a value reshaped to the mask -> walk forward to the commit -> retry it
    ONCE -> the application accepts -> confirmation observed, journey completed
    -> and both attempts stay on the record.

WHAT IS REAL HERE.  The frontier, the budget, the guard, the refuse pack, the
inventory, the fill engine (mask parse, tighten, reshape included), the wizard
walker, the boundary model, the crossing ledger WITH its refund arithmetic, the
step-back reader and both milestones are the production objects.  Only the
BROWSER is scripted — and one inch more of it than its siblings script: this
application REMEMBERS what was typed and judges the commit against it, because
a closed loop cannot be proven against a page that ignores the values.  That is
:class:`_SchemaBrowser`, the zod half of the transcription.

WHY THE PHONE FIELD AND WHY A MASK.  The refused field sits on step 0 — FOUR
steps behind the commit — so the sweep, the re-fill and the forward walk are
all exercised at full depth.  The rule is stated the way summit's schema states
its rules, as a format mask: the generator's phone is ten bare digits (measured:
``4455550110``), which genuinely fails ``(999) 999-9999``, and the repair's
reshaped value genuinely satisfies it.  Nothing in this file tells the engine
the answer; the application's own sentence is the only hint that exists.

THE CONTROLS THAT MAKE THIS MEAN SOMETHING flip exactly one axis each: the
operator's dial at 0 must leave the refusal standing with the boundary spent
exactly once; a mutating request in the crossing window must refuse the retry
outright.  If either control still crossed twice, the loop would be a spree
wearing a repair's clothes.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.characterization.harness import (Fixture, ScriptedBrowser,
                                            ScriptedPage, control,
                                            disposable_attestation, run_fixture)
from tests.test_summit_life_crossing_e2e import (SUBMIT_LABEL, SUCCESS_BANNER,
                                                 WIZARD_URL, _REFUSE_PACK,
                                                 _STEPS, _fields)
from app.crawler import GuardContext

#: The field the schema refuses, on step 0 — four step-backs from the commit.
_FIELD = "Phone Number"
#: The application's own words: a format mask, the way summit writes rules.
_RULE = "Phone must be (999) 999-9999"
#: What the mimic schema accepts — exactly the shape the mask draws.
_ACCEPT = re.compile(r"\(\d{3}\) \d{3}-\d{4}")
_REFUSED_STEP = 0


class _SchemaBrowser(ScriptedBrowser):
    """The scripted wizard, plus the one thing zod adds: judgement.

    Values are remembered as they are committed (the SPA keeps its form state
    across client-side steps), and the commit transition is decided by whether
    the refused field NOW satisfies the schema — refused world otherwise.  The
    conditional lives here and not in the page table because a value-dependent
    transition is precisely what ``ScriptedPage.transitions`` cannot express,
    and the whole subject under test is the value changing.
    """

    def __init__(self, pages, start):
        super().__init__(pages, start)
        self.committed: dict[str, str] = {}
        #: The value the refused field held at EACH commit click — the loop's
        #: own audit trail: exactly two entries on the happy path, exactly one
        #: under either control.
        self.commit_attempts: list[str] = []
        _PORTS.append(self)

    async def fill(self, control, value):
        self.committed[str(control.get("name") or "")] = str(value)
        return await super().fill(control, value)

    def _advance(self, name):
        if str(name or "") == SUBMIT_LABEL:
            held = self.committed.get(_FIELD, "")
            self.commit_attempts.append(held)
            if _ACCEPT.fullmatch(held or "") and "submitted" in self._pages:
                self._key = "submitted"
                return self._pages["submitted"].url
        return super()._advance(name)


#: run_fixture constructs the port internally; the factory stashes it so the
#: tests can read the schema's own audit trail.
_PORTS: list[_SchemaBrowser] = []


def _pages(*, mutation_in_window: bool = False) -> dict[str, ScriptedPage]:
    """The wizard and its refused twin, phone-refusing edition.

    Structure mirrors ``test_summit_silent_refusal_is_named_e2e`` — one URL,
    two page families — with the annotation on step 0 and, for the mutation
    control, one POST parked in the crossing window's network stream.
    """
    pages: dict[str, ScriptedPage] = {}
    for index, (heading, spec) in enumerate(_STEPS):
        for suffix in ("", "_refused"):
            fields = _fields(spec)
            if suffix and index == _REFUSED_STEP:
                fields = [
                    control("textbox", _FIELD, tag="input", kind="text",
                            aria_invalid="true", error_text=_RULE)
                    if f["name"] == _FIELD else f
                    for f in fields
                ]
            transitions = {"Continue": f"step{index + 1}{suffix}"}
            if index:
                transitions["Back"] = f"step{index - 1}{suffix}"
            pages[f"step{index}{suffix}"] = ScriptedPage(
                url=WIZARD_URL, title="Submit New Application",
                controls=[control("button", "Back", tag="button"), *fields,
                          control("button", "Continue", tag="button")],
                texts=[heading, "Submit the application to begin processing"],
                transitions=transitions,
            )
    review_controls = [control("button", "Back", tag="button"),
                       control("button", SUBMIT_LABEL, tag="button")]
    review_texts = ["Review & Submit",
                    "Review all information before submitting the application"]
    pages["step4"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=review_controls, texts=review_texts,
        transitions={SUBMIT_LABEL: "step4_refused", "Back": "step3"},
    )
    pages["step4_refused"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=review_controls, texts=review_texts,
        # The retry's refusal stays on this page; acceptance is decided by the
        # schema mimic, never by this table.
        transitions={SUBMIT_LABEL: "step4_refused", "Back": "step3_refused"},
        # THE MUTATION CONTROL's one difference: a POST observed in the window
        # between the click and the reader's drain.  Invariant 4 must refuse
        # the retry on exactly this evidence.
        network=([{"method": "POST", "url": WIZARD_URL + "/api/v1/applications",
                   "status": 400, "resource_type": "fetch"}]
                 if mutation_in_window else []),
    )
    pages["submitted"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=[control("button", "Back", tag="button")],
        texts=["Review & Submit", SUCCESS_BANNER,
               "29 API calls across 7 phases"],
    )
    return pages


def _crawl(tmp_path, monkeypatch, *, retry_max=None, mutation=False):
    if retry_max is not None:
        monkeypatch.setenv("QEC_REFUSAL_RETRY_MAX", str(retry_max))
    else:
        monkeypatch.delenv("QEC_REFUSAL_RETRY_MAX", raising=False)
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True, exist_ok=True)
    guard = GuardContext(refuse_pack=_REFUSE_PACK,
                         attestation=disposable_attestation())
    fixture = Fixture(
        name="refused_commit_repaired",
        pages=_pages(mutation_in_window=mutation),
        start="step0", target_url=WIZARD_URL,
        kwargs={"crawl_mode": "e2e", "wizard_enabled": True,
                "e2e_wizard_steps": 60, "guard_context": guard,
                "boundary_approvals": [{"control": SUBMIT_LABEL,
                                        "url": WIZARD_URL, "max_crossings": 1}],
                "submit_approvals": []},
        port_factory=_SchemaBrowser,
    )
    text, digest = run_fixture(fixture, work, monkeypatch)
    body = text.split("===SUMMARY===")[0]
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    return records, digest["coverage"], _PORTS[-1]


@pytest.fixture()
def repaired(tmp_path, monkeypatch):
    return _crawl(tmp_path, monkeypatch)


# ── 1 · THE CLOSED LOOP, END TO END ────────────────────────────────────────

def test_the_first_commit_is_refused_and_the_second_carries_the_apps_shape(
        repaired):
    """The headline: refusal -> named -> repaired -> retried -> confirmed."""
    _records, cov, port = repaired
    assert len(port.commit_attempts) == 2, (
        "one refusal, ONE retry — got %r" % port.commit_attempts)
    first, second = port.commit_attempts
    assert not _ACCEPT.fullmatch(first or ""), (
        "the first attempt must genuinely fail the schema, or this file "
        "proves a loop that was never needed")
    assert _ACCEPT.fullmatch(second or ""), (
        "the retry must carry the shape the application itself dictated")
    assert cov["journeys_completed"] == 1
    assert cov["forms_confirmed"] == 1


def test_both_attempts_stay_on_the_record(repaired):
    """A retry that erased its refused twin would be a green light with no
    history.  Two crossings, two milestones: the first unverified, the second
    verified with the application's own banner."""
    _records, cov, _port = repaired
    assert cov["boundaries_crossed"] == 2
    milestones = cov["outcome_milestones"]
    assert len(milestones) == 2
    assert milestones[0]["verified"] is False
    assert milestones[0]["confirmation_rung"] == ""
    assert milestones[1]["verified"] is True
    assert milestones[1]["confirmation_rung"], "the far side must be earned"
    assert "API calls completed successfully" in str(
        milestones[1].get("confirmation_detail") or "")


def test_the_rejection_record_says_it_was_read_far_back_and_acted_on(repaired):
    """The evidence joins up: the field, the application's own sentence, HOW
    far back it was read, and that the repair acted on it."""
    _records, cov, _port = repaired
    row = next((r for r in cov["validation_rejections"]
                if r.get("field") == _FIELD), None)
    assert row is not None, (
        "the refused field was not named; got %r"
        % [r.get("field") for r in cov["validation_rejections"]])
    assert "(999) 999-9999" in row["rule"]
    assert row["steps_back"] == 4, "the field lives four steps behind the commit"
    assert row.get("repaired") is True


# ── 2 · THE CONTROLS ───────────────────────────────────────────────────────

def test_control_with_the_dial_at_zero_the_refusal_stands_exactly_once(
        tmp_path, monkeypatch):
    """Flip ONE axis: QEC_REFUSAL_RETRY_MAX=0.  Identical application,
    identical refusal — the boundary must stay spent exactly once and the
    named refusal must stand un-acted-on.  If this still crossed twice, the
    dial is decoration and the loop is unbounded."""
    _records, cov, port = _crawl(tmp_path, monkeypatch, retry_max=0)
    assert len(port.commit_attempts) == 1
    assert cov["boundaries_crossed"] == 1
    assert cov["journeys_completed"] == 0
    row = next((r for r in cov["validation_rejections"]
                if r.get("field") == _FIELD), None)
    assert row is not None, "the reader itself must still name the field"
    assert "repaired" not in row


def test_control_a_mutating_call_in_the_window_refuses_the_retry(
        tmp_path, monkeypatch):
    """Flip ONE axis: a POST the guard allowed sits in the crossing window.
    Whether the application acted on it is unknowable, so the retry could be a
    second submission — invariant 4 must refuse it on this evidence alone,
    with the repair machinery otherwise ready and willing."""
    _records, cov, port = _crawl(tmp_path, monkeypatch, mutation=True)
    assert len(port.commit_attempts) == 1, (
        "a second click after an observed mutation is the exact double-submit "
        "risk the exactly-once ledger exists to prevent")
    assert cov["boundaries_crossed"] == 1
    assert cov["journeys_completed"] == 0


def test_naming_and_repairing_never_fabricate_a_completion(repaired):
    """The retry's confirmation is the APPLICATION's, not the loop's: strike
    the accepted world and nothing may claim completion.  Held here by the
    dial-at-zero control above; held for the happy path by the milestone rung
    assertions — this asserts the flow ledger agrees end to end."""
    _records, cov, _port = repaired
    flows = [f for f in cov["flows"] if f.get("journey_completed")]
    assert len(flows) == 1
    assert flows[0]["terminal"] == "submit_crossed"
