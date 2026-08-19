"""A "CHOOSE AT LEAST ONE" RULE CANNOT BE WRITTEN IN HTML — so ask the app.

``required`` on a checkbox means *that one box* must be checked, which is never
what a multi-select question means. Every framework therefore puts the rule in
script (a zod ``.min(1)``, an Angular validator, a hand-written ``canAdvance()``)
where no crawler can read it. The fill correctly declined eight optional-looking
checkboxes; the application had disabled Continue precisely because none of them
were answered; the walk stopped one step short of the end of a five-step
application and named eight fields for a human to supply by hand.

The escape is not a better DOM read — there is nothing in the DOM to read. It is
an EXPERIMENT: answer one declined question, re-read the page, and let the
application render its own verdict on the forward control. The verdict is
evidence no static read can produce, and it is worth more than the unblocked
walk — "Continue is gated on Health Conditions" is the tacit business rule the
catalogue exists to capture, and the app just proved it.

These tests pin the four properties that make the experiment honest:
  * it answers the member that ASSERTS THE LEAST, so a synthetic applicant never
    acquires a fabricated medical history;
  * a failed experiment is UNDONE, so a change that bought nothing never reaches
    the recorded snapshot;
  * a success re-provenances the field and clears it from the human ask — the
    residue must never keep asking for something already answered;
  * it is bounded to one attempt, so it stays an experiment and not a search.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import vocab
from app import rules
from app.crawler import Crawler
from app.forms import PROV_UNBLOCK


# ─── the vocabulary: a full-string rule, never a substring one ───────────────

@pytest.mark.parametrize("label", [
    "None", "none", "N/A", "n/a", "Not Applicable", "Neither", "Nothing",
    "None of the above", "None of these", "No known allergies",
    "Prefer not to say", "Decline to answer", "No",
])
def test_a_negative_option_is_recognised(label):
    assert vocab.NEGATIVE_OPTION_RE.match(label), label


@pytest.mark.parametrize("label", [
    "Nonexistent condition", "Nodule", "Nonsmoker", "None of my business today",
    "Type 2 Diabetes", "Asthma", "Elevated BMI", "Known heart condition",
])
def test_a_positive_disclosure_is_never_read_as_a_denial(label):
    """A substring rule would find "None" inside "Nonexistent condition" and "no"
    inside "Nodule" — turning a disclosure into a denial, which is precisely the
    class of error that puts a value in the ledger the form never held."""
    assert not vocab.NEGATIVE_OPTION_RE.match(label), label


# ─── the experiment ──────────────────────────────────────────────────────────

def _checkbox(name: str, *, committed: str = "false") -> dict:
    return {"kind": "checkbox", "name": name, "disabled": False, "danger": "",
            "value_committed": committed, "role": "checkbox", "tag": "input"}


def _button(name: str, *, disabled: bool) -> dict:
    return {"kind": "button", "name": name, "disabled": disabled, "danger": ""}


#: The live shape: eight condition checkboxes, none required, none grouped, with
#: a Continue the application has disabled on a rule it never wrote down.
CONDITIONS = ["None", "Controlled Hypertension", "Type 2 Diabetes",
              "Elevated BMI", "Sleep Apnea (Treated)", "Asthma"]


class _Port:
    """Records what was asked of the browser, and answers as a real one would."""

    def __init__(self, *, accepts: bool = True):
        self.accepts = accepts
        self.calls: list[tuple[str, bool]] = []

    async def set_checked(self, control, checked):
        self.calls.append((str(control.get("name")), checked))
        return SimpleNamespace(intent_met=(True if self.accepts else False),
                               committed_value="true" if checked else "false")


def _crawler(port, *, unblocks: bool, url: str = "https://app/x",
             known_rules=()):
    """A stub carrying only what the method touches — so the code under test is
    the shipped code, not a re-implementation of it.

    M1.7 / T-GW-04 added two more things it touches: the rules earlier crawls
    PROVED about this app (``_known_rules``) and the ledger of what THIS crawl
    proves (``_rule_ledger``). ``known_rules`` defaults to empty, which is the
    pre-M1.7 behaviour exactly — every block runs the full experiment."""
    fields = [_checkbox(n) for n in CONDITIONS]

    async def _observe():
        # What the page looks like AFTER the answer: Continue's disabled state is
        # the application's own verdict on whether the answer was sufficient.
        return SimpleNamespace(
            raw_controls=[dict(c) for c in fields]
            + [_button("Continue", disabled=not unblocks)],
            url=url)

    return SimpleNamespace(
        _port=port, _observe=_observe, _refuse_pack=None,
        _advance_blocked=[{"url": url, "label": "Continue",
                           "reason": "advance_disabled_by_app_validation",
                           "missing_fields": list(CONDITIONS)}],
        _fields_unfilled=list(CONDITIONS),
        _fields_seed_detail=[{"label": n, "url": url} for n in CONDITIONS],
        _field_ledger=[{"label": n, "provenance": "needs_input", "filled": False,
                        "options": ["checked", "unchecked"]} for n in CONDITIONS],
        _known_rules=rules.KnownRules(known_rules),
        _rule_ledger=rules.RuleLedger(),
    )


def _controls():
    return [_checkbox(n) for n in CONDITIONS] + [_button("Continue", disabled=True)]


@pytest.mark.asyncio
async def test_the_least_assertive_answer_is_the_one_chosen(monkeypatch):
    """Every member would unblock the walk equally well. Only one of them invents
    nothing about the person the crawl is pretending to be."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    assert port.calls[0] == ("None", True), (
        "a condition was asserted about a synthetic applicant when a denial was "
        "on offer")


@pytest.mark.asyncio
async def test_a_success_records_the_rule_the_application_just_proved(monkeypatch):
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=True)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    rule = me._advance_blocked[0]
    assert rule["resolved_by_agent"] == "None"
    assert "Continue" in rule["business_rule"] and "None" in rule["business_rule"]
    assert "proven" in rule["business_rule"], (
        "the rule must say it was demonstrated, not inferred")


@pytest.mark.asyncio
async def test_an_answered_question_stops_being_asked_of_a_human(monkeypatch):
    """The residue is the operator's to-do list. Keeping a field on it after the
    agent answered it asks someone to do work that is already done — the exact
    default this product exists to remove."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=True)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    answered = [r for r in me._field_ledger if r["label"] == "None"][0]
    assert answered["provenance"] == PROV_UNBLOCK and answered["filled"] is True
    assert answered["choice"] == "checked"
    # THE WHOLE BLOCK IS RELEASED, not just the answered member. That list means
    # "the fields whose absence STOPPED THE FUNNEL", and the funnel is no longer
    # stopped — the app enabled its forward control with the other controls
    # exactly as they were. Dropping only the answered one would leave five
    # fields on an operator's to-do list under a heading the application itself
    # has just contradicted.
    assert me._fields_unfilled == []
    assert me._fields_seed_detail == []
    # The record still carries them, as evidence of the page as it stood.
    assert me._advance_blocked[0]["missing_fields"] == CONDITIONS


@pytest.mark.asyncio
async def test_the_steps_own_ledger_is_corrected_not_only_the_crawls(monkeypatch):
    """The crawl-wide ledger feeds the residue; the FILL's own ledger feeds this
    step's decision points. Updating only the first leaves the step reporting
    `needs_input` for a question the application just confirmed we answered."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=True)
    fill = SimpleNamespace(
        unfilled_fields=list(CONDITIONS),
        field_ledger=[{"label": n, "provenance": "needs_input", "filled": False,
                       "options": ["checked", "unchecked"]} for n in CONDITIONS])

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    row = [r for r in fill.field_ledger if r["label"] == "None"][0]
    assert row["provenance"] == PROV_UNBLOCK and row["filled"] is True
    assert "None" not in fill.unfilled_fields
    assert len(fill.unfilled_fields) == len(CONDITIONS) - 1
    assert me._last_unblock_field == "None"


def test_the_walk_asks_at_every_step_not_only_the_first():
    """Step 4 of a five-step application is only ever reached from INSIDE the
    walk loop. A hook on the outer form path alone sees step 1 and nothing after
    it — so it would never see the block that actually ends the journey, which is
    the entire failure this capability exists to fix."""
    import inspect
    src = inspect.getsource(Crawler._walk_wizard)
    assert src.count("_answer_to_unblock") >= 2, (
        "the walk must ask on its entry step AND on every step it reaches")
    assert "_last_unblock_field" in src, (
        "an answered question must correct the step's own counts")


@pytest.mark.asyncio
async def test_a_failed_experiment_is_undone(monkeypatch):
    """If answering did not enable the control, the block was about something
    else. Leaving the box checked would put a change into the recorded snapshot
    that bought nothing and that no evidence supports."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=False)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    out = await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                           "https://app/x", fill)

    assert port.calls == [("None", True), ("None", False)], "the answer was not undone"
    assert me._fields_unfilled == CONDITIONS, "residue was corrected on a failure"
    assert "resolved_by_agent" not in me._advance_blocked[0]
    assert all(r["provenance"] == "needs_input" for r in me._field_ledger)
    assert out[-1]["name"] == "Continue", "the caller must keep the pre-attempt page"


@pytest.mark.asyncio
async def test_exactly_one_question_is_answered_per_block(monkeypatch):
    """One attempt keeps this an experiment. Answering a second, a third and a
    fourth would be a search through the app's validation, filling in a form the
    client never asked to have filled."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=False)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    assert [c for c in port.calls if c[1] is True] == [("None", True)]


@pytest.mark.asyncio
async def test_a_control_the_fill_already_answered_is_left_alone(monkeypatch):
    """Only DECLINED questions are candidates. Re-answering a field the fill
    committed would overwrite a grounded value with a structural guess."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True)
    fill = SimpleNamespace(unfilled_fields=[])          # nothing was declined

    out = await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                           "https://app/x", fill)

    assert port.calls == []
    assert out[-1]["name"] == "Continue" and out[-1]["disabled"] is True


@pytest.mark.asyncio
async def test_an_already_checked_box_is_not_offered_as_the_answer(monkeypatch):
    """It is on, and the app has plainly not accepted it as sufficient — checking
    it again changes nothing and would record a second answer to one question."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True)
    controls = [_checkbox("None", committed="true"),
                _checkbox("Asthma"), _button("Continue", disabled=True)]
    fill = SimpleNamespace(unfilled_fields=["None", "Asthma"])

    await Crawler._answer_to_unblock(me, controls, "Continue",
                                     "https://app/x", fill)

    assert port.calls[0] == ("Asthma", True)


@pytest.mark.asyncio
async def test_a_danger_control_is_never_the_answer(monkeypatch):
    """Unblocking a walk is never a reason to touch a control the refuse pack
    holds. A blocked funnel is a smaller cost than an irreversible act."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True)
    danger = _checkbox("None")
    danger["danger"] = "critical"
    controls = [danger, _button("Continue", disabled=True)]
    fill = SimpleNamespace(unfilled_fields=["None"])

    await Crawler._answer_to_unblock(me, controls, "Continue",
                                     "https://app/x", fill)

    assert port.calls == []


@pytest.mark.asyncio
async def test_a_browser_that_refuses_the_answer_leaves_the_block_standing(monkeypatch):
    """intent_met is False: the control did not take the answer. Recording an
    unblock here would claim an experiment that never ran."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port(accepts=False)
    me = _crawler(port, unblocks=True)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    out = await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                           "https://app/x", fill)

    assert "resolved_by_agent" not in me._advance_blocked[0]
    assert me._fields_unfilled == CONDITIONS
    assert out[-1]["disabled"] is True


# ═══════════════════════════════════════════════════════════════════════════
#  M1.7 / T-GW-04 — the rule outlives the crawl that proved it
# ═══════════════════════════════════════════════════════════════════════════


def _rule_for(field_label: str, url: str = "https://app/x"):
    return rules.discover(url=url, blocked_label="Continue",
                          field_label=field_label,
                          proof="proven on an earlier crawl").as_dict()


@pytest.mark.asyncio
async def test_a_proved_rule_is_recorded_for_persistence(monkeypatch):
    """THE PRODUCER. Before M1.7 the proof was written into a list on the
    crawler object, counted once by qe-central, and thrown away. It must now
    also be minted as a keyed, versioned rule that can be persisted."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=True)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    recorded = me._rule_ledger.as_list()
    assert len(recorded) == 1
    assert recorded[0]["blocked_label"] == "Continue"
    assert recorded[0]["field_label"] == "None"
    assert recorded[0]["schema_version"] == rules.RULE_SCHEMA_VERSION
    assert "proven" in recorded[0]["proof"]
    # And it is keyed on the URL TEMPLATE, so a second applicant is the same rule.
    assert recorded[0]["key"] == rules.rule_key(
        url="https://app/x", blocked_label="Continue", field_label="None")


@pytest.mark.asyncio
async def test_a_failed_experiment_proves_no_rule(monkeypatch):
    """A rule is a record of EVIDENCE. An experiment the application refused
    proved nothing, and storing it would poison every later crawl with a wrong
    answer that skips the search which would have found the right one."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=False)
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    assert me._rule_ledger.as_list() == []


@pytest.mark.asyncio
async def test_a_known_rule_picks_the_proved_field_not_the_heuristic(monkeypatch):
    """THE CONSUMER, and the whole point of persisting anything.

    With no knowledge the walk picks the LEAST-ASSERTING declined question
    ("None") and finds out what happens. On this application the answer that
    actually unblocks Continue is a different one — so without a rule the walk
    guesses, the guess is reverted, and the funnel stops one step short.

    A rule removes the guess: the field the application already proved is the
    one that gets answered.
    """
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True, known_rules=[_rule_for("Asthma")])
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    # The heuristic would have chosen "None" (it matches NEGATIVE_OPTION_RE and
    # comes first in DOM order); the rule chose the proved field instead.
    assert [name for name, _ in port.calls] == ["Asthma"]
    assert me._last_unblock_field == "Asthma"
    assert me._known_rules.stats() == {"known": 1, "lookups": 1, "hits": 1,
                                       "misses": 0, "reuse_rate": 1.0}


@pytest.mark.asyncio
async def test_a_reused_rule_is_provenanced_apart_from_a_fresh_proof(monkeypatch):
    """Inherited evidence must not be indistinguishable from evidence gathered
    on this run — that blur is the thing this milestone exists to remove."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    me = _crawler(_Port(), unblocks=True, known_rules=[_rule_for("Asthma")])
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    row = next(r for r in me._field_ledger if r["label"] == "Asthma")
    assert row["provenance"] == rules.PROV_KNOWN_RULE
    assert row["provenance"] != PROV_UNBLOCK
    assert me._advance_blocked[0]["rule_reused"] is True


@pytest.mark.asyncio
async def test_a_reused_rule_still_performs_and_confirms_the_action(monkeypatch):
    """WHAT REUSE MUST NOT SKIP. A rule is knowledge ABOUT an application, never
    a substitute for having done the thing. A walk that reported an advance it
    had not actually unblocked would be this milestone's own failure mode,
    rebuilt inside the feature meant to close it."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    # The app REFUSES to unblock, even though a rule says this field should work
    # (the application changed since the rule was proved).
    me = _crawler(port, unblocks=False, known_rules=[_rule_for("Asthma")])
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    # It ACTED (the control was set, then reverted when the app disagreed)...
    assert [name for name, _ in port.calls] == ["Asthma", "Asthma"]
    assert port.calls[-1][1] is False
    # ...and it claimed NOTHING the application did not confirm.
    assert "resolved_by_agent" not in me._advance_blocked[0]
    assert me._rule_ledger.as_list() == []


@pytest.mark.asyncio
async def test_a_stale_rule_falls_back_to_the_experiment(monkeypatch):
    """A rule naming a question this state no longer asks must not force a
    control that is not there — it degrades to the search, which is exactly the
    pre-M1.7 behaviour."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))
    port = _Port()
    me = _crawler(port, unblocks=True,
                  known_rules=[_rule_for("A Question This Page Does Not Ask")])
    fill = SimpleNamespace(unfilled_fields=list(CONDITIONS))

    await Crawler._answer_to_unblock(me, _controls(), "Continue",
                                     "https://app/x", fill)

    assert [name for name, _ in port.calls] == ["None"]     # the heuristic pick
    assert me._advance_blocked[0]["rule_reused"] is False

