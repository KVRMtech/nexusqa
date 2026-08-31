"""B1-S END TO END — THE SUMMIT SILENT REFUSAL, THROUGH THE REAL CRAWLER.

    Walk the five-step wizard -> reach the boundary -> cross it under the
    operator's grant -> the application refuses by schema and says nothing on
    the step it was clicked on -> step back to where the field lives -> name
    the field and the rule -> and STILL report the journey as not completed.

WHAT IS REAL HERE. The frontier, the budget, the guard, the refuse pack, the
inventory, the fingerprinter, the form engine, the wizard walker, the boundary
model, the approval registry, the crossing ledger, the milestone, and the
step-back reader are all the production objects. Only the BROWSER is scripted,
through the same :class:`app.browser.BrowserPort` the Playwright adapter
implements. This is the sibling of ``test_summit_life_crossing_e2e``; it shares
that file's transcription of the application and adds the one shape that file
does not model.

WHY THIS SHAPE. Measured live on summit-life-carrier (engine 768a3c8): the
commit crossed, ``outcome`` was ``none``, zero ``/api/v1/`` calls fired, and the
rejection reader — standing on the page it crossed from — named nothing. That
silence was verified in the application's own source and is TRUE: the wizard's
review step renders no message node at all, because it declares no field.
``handleSubmit`` runs a zod resolver over the whole schema before the submit
handler is reached, so every refusal belongs to a field on an earlier,
unmounted step.

    step 0  Applicant              <FormMessage/> x 7
    step 1  Address & Employment   <FormMessage/> x 7
    step 2  Coverage               <FormMessage/> x 4  <- the refusal lives here
    step 3  Health                 <FormMessage/> x 4
    step 4  Review & Submit        (none)              <- the reader stood here

``Face Amount ($)`` is not an arbitrary choice of field: the Phase-1 exit
re-scope names it as one of the two values that actually failed on the live
runs, and as one whose seed never reached the wizard copy.

THE CONTROL THAT MAKES THIS MEAN SOMETHING is ``…_without_a_back_control_…``
below: the identical application, the identical refusal, the identical
crossing, with the wizard's Back button removed. It must cross, refuse, and
name NOTHING — otherwise the naming in the positive test is coming from
somewhere other than the step-back and this file proves nothing.
"""
from __future__ import annotations

import json

import pytest

from tests.characterization.harness import (Fixture, ScriptedPage, control,
                                            disposable_attestation, run_fixture)
from tests.test_summit_life_crossing_e2e import (SUBMIT_LABEL, WIZARD_URL,
                                                 _REFUSE_PACK, _STEPS, _fields)
from app.crawler import GuardContext

#: The application's own words, from ``applicationSchema`` in page.tsx.
_RULE = "Face amount must be at least $50,000"
#: The field the live runs actually failed on.
_FIELD = "Face Amount ($)"
#: Which step declares it — "Coverage Details", index 2 of four field steps, so
#: the review step is TWO step-backs away from the refusal.
_REFUSED_STEP = 2


def _refusing_pages(*, with_back: bool = True) -> dict[str, ScriptedPage]:
    """The wizard, plus the world it enters after a schema-refused submit.

    Two page families over ONE url. The ``_refused`` family is byte-identical
    to the ordinary one except that the refused field carries the application's
    own annotation — which is exactly what react-hook-form does to the DOM when
    ``formState.errors`` is populated and the field's step is mounted.

    The review step's ``_refused`` twin is IDENTICAL to its ordinary self: the
    submit changed nothing observable there, which is the whole phenomenon.
    """
    pages: dict[str, ScriptedPage] = {}

    def _back(step_key: str) -> list[dict]:
        return ([control("button", "Back", tag="button")] if with_back else [])

    for index, (heading, spec) in enumerate(_STEPS):
        for suffix in ("", "_refused"):
            fields = _fields(spec)
            if suffix and index == _REFUSED_STEP:
                # THE APPLICATION'S OWN ANNOTATION. `aria-invalid` plus the
                # message node its `aria-describedby` points at is precisely
                # what shadcn's <FormControl>/<FormMessage> pair emits, and it
                # is what the production attribution ladder reads.
                fields = [
                    control("textbox", _FIELD, tag="input", kind="text",
                            input_type="number", aria_invalid="true",
                            error_text=_RULE)
                    if f["name"] == _FIELD else f
                    for f in fields
                ]
            transitions = {"Continue": f"step{index + 1}{suffix}"}
            if with_back and index:
                transitions["Back"] = f"step{index - 1}{suffix}"
            pages[f"step{index}{suffix}"] = ScriptedPage(
                url=WIZARD_URL, title="Submit New Application",
                controls=[*_back(f"step{index}{suffix}"), *fields,
                          control("button", "Continue", tag="button")],
                texts=[heading, "Submit the application to begin processing"],
                transitions=transitions,
            )

    review_controls = [*_back("step4"),
                       control("button", SUBMIT_LABEL, tag="button")]
    review_texts = ["Review & Submit",
                    "Review all information before submitting the application"]
    # Before the click: the commit leads into the refused world.
    pages["step4"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=review_controls, texts=review_texts,
        transitions={SUBMIT_LABEL: "step4_refused",
                     **({"Back": "step3"} if with_back else {})},
    )
    # After the click: THE SAME PAGE. Same url, same controls, same texts, no
    # banner, no status, no dialog, no navigation. The application has refused
    # and this step has no way of saying so.
    pages["step4_refused"] = ScriptedPage(
        url=WIZARD_URL, title="Submit New Application",
        controls=review_controls, texts=review_texts,
        transitions={"Back": "step3_refused"} if with_back else {},
    )
    return pages


def _crawl(tmp_path, monkeypatch, *, with_back=True, step_back_max=None):
    if step_back_max is not None:
        monkeypatch.setenv("QEC_STEP_BACK_MAX", str(step_back_max))
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True, exist_ok=True)
    guard = GuardContext(refuse_pack=_REFUSE_PACK,
                         attestation=disposable_attestation())
    fixture = Fixture(
        name="summit_silent_refusal", pages=_refusing_pages(with_back=with_back),
        start="step0", target_url=WIZARD_URL,
        kwargs={"crawl_mode": "e2e", "wizard_enabled": True,
                "e2e_wizard_steps": 60, "guard_context": guard,
                "boundary_approvals": [{"control": SUBMIT_LABEL,
                                        "url": WIZARD_URL, "max_crossings": 1}],
                "submit_approvals": []},
    )
    text, digest = run_fixture(fixture, work, monkeypatch)
    body = text.split("===SUMMARY===")[0]
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    return records, digest["coverage"]


@pytest.fixture()
def refused(tmp_path, monkeypatch):
    return _crawl(tmp_path, monkeypatch)


# ── 1 · the shape is the live one ─────────────────────────────────────────

def test_the_boundary_is_crossed_and_the_far_side_confirms_nothing(refused):
    """The live shape, unchanged by anything this milestone added: the click
    happened, it is recorded, and no journey is claimed."""
    _records, cov = refused
    assert cov["boundaries_crossed"] == 1, "the click must still be recorded"
    assert cov["journeys_completed"] == 0
    milestone = cov["outcome_milestones"][0]
    assert milestone["verified"] is False
    assert milestone["confirmation_rung"] == ""


# ── 2 · THE CLOSURE: the reason is now on the record ──────────────────────

def test_the_silent_refusal_is_named_with_the_field_and_the_rule(refused):
    """WHAT WAS MISSING. Before B1-S this bundle carried a crossing, an empty
    outcome, and no reason anywhere — the most misleading shape a bundle can
    have, because it reads like a pass with a confirmation merely absent."""
    _records, cov = refused
    named = cov["validation_rejections"]
    assert named, "the crossing still owes a reason and does not give one"
    row = next((r for r in named if r.get("field") == _FIELD), None)
    assert row is not None, (
        "the refused field was not named; got %r"
        % [r.get("field") for r in named])
    assert _RULE in row["rule"], "the application's own words were not kept"
    assert row["rejected_on"] == "commit:%s" % SUBMIT_LABEL


def test_the_record_says_where_it_was_read(refused):
    """PROVENANCE. A message read two steps behind the commit is a weaker
    claim than one read where the commit happened, and the record says so
    rather than flattening the two."""
    _records, cov = refused
    row = next(r for r in cov["validation_rejections"]
               if r.get("field") == _FIELD)
    assert row["steps_back"] == 2, (
        "the reader must record how far it walked; got %r"
        % row.get("steps_back"))
    assert row["anchored_by"], (
        "a stepped-back record must still name the rung that anchored it")


# ── 3 · THE FALSIFICATION CONTROL ─────────────────────────────────────────

def test_without_a_back_control_the_identical_refusal_names_nothing(
        tmp_path, monkeypatch):
    """THE CONTROL THAT MAKES THIS FILE MEAN SOMETHING.

    Same application, same schema refusal on the same field, same approval,
    same crossing — only the wizard's Back button is gone. If this named the
    field anyway, the naming in the positive tests would be coming from
    somewhere other than the step-back, and every claim here would be
    unearned."""
    _records, cov = _crawl(tmp_path, monkeypatch, with_back=False)
    assert cov["boundaries_crossed"] == 1, (
        "the control must still reach and cross the boundary, or it is not a "
        "control for anything")
    assert not [r for r in cov["validation_rejections"]
                if r.get("field") == _FIELD], (
        "the refusal was named with no way to step back to it")


def test_with_the_mechanism_switched_off_the_identical_refusal_names_nothing(
        tmp_path, monkeypatch):
    """The second control: the Back button is present and reachable, and only
    ``QEC_STEP_BACK_MAX=0`` differs. This is the operator's off switch, proven
    to restore the previous behaviour rather than merely documented to."""
    _records, cov = _crawl(tmp_path, monkeypatch, step_back_max=0)
    assert cov["boundaries_crossed"] == 1
    assert not [r for r in cov["validation_rejections"]
                if r.get("field") == _FIELD]


# ── 4 · IT STILL REFUSES TO CALL THIS A JOURNEY ───────────────────────────

def test_naming_the_reason_does_not_turn_a_refusal_into_a_completion(refused):
    """THE PROPERTY MOST WORTH PROTECTING. A mechanism that explains a failure
    must never be mistaken for one that fixes it. Reading the rule changes what
    the operator knows and changes NOTHING about what the crawl claims."""
    _records, cov = refused
    assert cov["journeys_completed"] == 0
    assert cov["forms_confirmed"] == 0
    assert not any(f.get("journey_completed") for f in cov["flows"])
    assert cov["outcome_milestones"][0]["verified"] is False
