"""M1.x / T-RG-01 — THE UNBLOCK EXPERIMENT ON A RADIO QUESTION.

WHY THIS FILE EXISTS SEPARATELY from ``test_answer_to_unblock.py``.

The checkbox suite next door pins a contract this one must never disturb: on a
page with a declined checkbox, the walk picks the same control, runs the same
single attempt and reverts the same way it did before radios existed.  Keeping
the radio cases in their own file makes that guarantee READABLE — a reviewer can
see that nothing in the checkbox file changed, and that every assertion here is
about a page the old code returned ``controls`` unchanged for.

WHAT MAKES A RADIO DIFFERENT, and what each test below therefore catches:

* a radio's ``name`` is the name of an ANSWER, so a page's residue holds
  ``["Yes", "No"]`` for ONE question — matching name-to-declined the way the
  checkbox path does would see two questions and answer both;
* answering one member answers the whole question, so the unit of choice is the
  GROUP;
* **a radio cannot be un-checked** — the revert this experiment promises is not
  available, and the code must say so instead of logging an undo it never did.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import rules
from app.crawler import Crawler
from app.forms import PROV_UNBLOCK


# ─── page fixtures ───────────────────────────────────────────────────────────

def _radio(name: str, *, group: str, committed: str = "false",
           disabled: bool = False, danger: str = "",
           group_key: str = "") -> dict:
    """One ANSWER to one question — which is what a radio record is.

    ``group_id`` is what ``build_inventory``'s GROUP_ASSEMBLE stamps on every
    member of a declared group; it, not the name, is the question's identity.
    """
    return {"kind": "radio", "name": name, "disabled": disabled,
            "danger": danger, "value_committed": committed, "role": "radio",
            "tag": "input", "group_id": group,
            "group_key": group_key or "name:form0:" + group}


def _checkbox(name: str, *, committed: str = "false") -> dict:
    return {"kind": "checkbox", "name": name, "disabled": False, "danger": "",
            "value_committed": committed, "role": "checkbox", "tag": "input"}


def _button(name: str, *, disabled: bool) -> dict:
    return {"kind": "button", "name": name, "disabled": disabled, "danger": ""}


#: The shape the target domain is actually made of: a required Yes/No health
#: question whose Continue is disabled by a script validator no markup declares.
def _tobacco() -> list[dict]:
    return [_radio("Yes", group="g_tobacco"), _radio("No", group="g_tobacco")]


class _Port:
    """Records what was asked of the browser, and answers as a real one would."""

    def __init__(self, *, accepts: bool = True):
        self.accepts = accepts
        self.calls: list[tuple[str, bool]] = []

    async def set_checked(self, control, checked):
        self.calls.append((str(control.get("name")), checked))
        return SimpleNamespace(intent_met=(True if self.accepts else False),
                               committed_value="true" if checked else "false")


def _crawler(port, *, unblocks: bool, controls, declined,
             url: str = "https://app/x", known_rules=()):
    """A stub carrying only what the method touches, so the code under test is
    the shipped code rather than a re-implementation of it."""

    async def _observe():
        # The page AFTER the answer.  Continue's disabled state is the
        # APPLICATION's verdict on whether the answer was sufficient — the whole
        # point of the experiment is that we do not decide this ourselves.
        return SimpleNamespace(
            raw_controls=[dict(c) for c in controls
                          if c.get("kind") != "button"]
            + [_button("Continue", disabled=not unblocks)],
            url=url)

    return SimpleNamespace(
        _port=port, _observe=_observe, _refuse_pack=None,
        _advance_blocked=[{"url": url, "label": "Continue",
                           "reason": "advance_disabled_by_app_validation",
                           "missing_fields": list(declined)}],
        _fields_unfilled=list(declined),
        _fields_seed_detail=[{"label": n, "url": url} for n in declined],
        _field_ledger=[{"label": n, "provenance": "needs_input", "filled": False}
                       for n in declined],
        _known_rules=rules.KnownRules(known_rules),
        _rule_ledger=rules.RuleLedger(),
        _unblock_irreversible=[],
    )


@pytest.fixture(autouse=True)
def _identity_inventory(monkeypatch):
    """``build_inventory`` is exercised by its own suite; here it would only
    obscure which control the walk chose."""
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))


async def _run(me, controls, fill, *, label="Continue", url="https://app/x"):
    return await Crawler._answer_to_unblock(me, controls, label, url, fill)


# ─── detection: a radio group is found, and found as ONE question ────────────

@pytest.mark.asyncio
async def test_a_declined_radio_question_is_answered_and_the_app_confirms_it():
    """The gap this closes.  Before radios were handled the walk read this page,
    found nothing it knew how to answer, and stopped one step short of the end of
    a twenty-question application."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Yes", "No"])
    fill = SimpleNamespace(unfilled_fields=["Yes", "No"])

    out = await _run(me, controls, fill)

    assert port.calls == [("No", True)], port.calls
    assert me._last_unblock_field == "No"
    assert out is not controls, "the refreshed page must be returned"


@pytest.mark.asyncio
async def test_only_one_member_of_a_group_is_ever_set():
    """Four answers to one question are one decision, not four.  Setting more
    than one is meaningless on a radio and would be a form-filling spree on
    anything else."""
    grp = [_radio(n, group="g_freq") for n in ("Daily", "Weekly", "Never")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Daily", "Weekly", "Never"])
    fill = SimpleNamespace(unfilled_fields=["Daily", "Weekly", "Never"])

    await _run(me, controls, fill)

    assert len(port.calls) == 1, port.calls
    # "Never", not DOM order's "Daily" — see the frequency-scale tests below.
    assert port.calls[0] == ("Never", True)


@pytest.mark.asyncio
async def test_the_least_asserting_answer_is_chosen():
    """A "Yes" to a tobacco question fabricates a medical history for a synthetic
    person; "No" invents nothing.  Both unblock the walk equally well."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls[0][0] == "No"


@pytest.mark.asyncio
async def test_with_no_negative_option_dom_order_decides():
    """Determinism is the requirement, not cleverness: the same page must yield
    the same choice on every crawl, or two runs of one app disagree."""
    grp = [_radio(n, group="g_plan") for n in ("Gold", "Silver", "Bronze")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Gold", "Silver", "Bronze"])
    await _run(me, controls,
               SimpleNamespace(unfilled_fields=["Gold", "Silver", "Bronze"]))
    assert port.calls[0] == ("Gold", True)


# ─── the cases that must NOT be touched ──────────────────────────────────────

@pytest.mark.asyncio
async def test_an_already_answered_question_is_left_alone():
    """It is not a declined question.  Re-answering it would overwrite a real
    choice — possibly one an earlier step of this very walk made — with a
    speculative one."""
    grp = [_radio("Yes", group="g_tobacco", committed="true"),
           _radio("No", group="g_tobacco")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["No"]))
    assert port.calls == []
    assert out is controls


@pytest.mark.asyncio
async def test_a_group_whose_every_option_is_disabled_is_not_answerable():
    """The question is real and unanswered and there is nothing safe to answer it
    with.  Forcing a disabled control would be acting against the app's own
    statement that the option is unavailable."""
    grp = [_radio("Yes", group="g_x", disabled=True),
           _radio("No", group="g_x", disabled=True)]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls == []
    assert out is controls


@pytest.mark.asyncio
async def test_a_disabled_option_is_skipped_but_its_group_is_still_answered():
    """Dynamic visibility and conditional branching routinely disable ONE option.
    That disables an answer, not the question."""
    grp = [_radio("No", group="g_x", disabled=True), _radio("Yes", group="g_x")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls == [("Yes", True)], "the enabled option answers it"


@pytest.mark.asyncio
async def test_a_dangerous_option_is_never_chosen():
    grp = [_radio("No", group="g_x", danger="destructive"),
           _radio("Yes", group="g_x")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls == [("Yes", True)]


@pytest.mark.asyncio
async def test_an_ungrouped_radio_is_never_answered():
    """No ``group_id`` means the DOM never declared which question this answers.
    Answering it is a guess about the question, and a wrong guess is a fabricated
    answer on a real application."""
    lone = [dict(_radio("Yes", group="x"), group_id=""),
            dict(_radio("No", group="x"), group_id="")]
    controls = lone + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls == []
    assert out is controls


@pytest.mark.asyncio
async def test_a_group_the_fill_did_not_decline_is_left_alone():
    """The residue is the evidence that a question is open.  A group absent from
    it was answered by the fill, and this experiment has no business in it."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Nickname"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Nickname"]))
    assert port.calls == []
    assert out is controls


# ─── validation failure, and the undo a radio cannot perform ────────────────

@pytest.mark.asyncio
async def test_an_answer_that_buys_nothing_is_reported_as_irreversible():
    """THE HONEST-LOG TEST, and the reason radios needed their own revert path.

    HTML has no gesture that returns a group to "nothing selected".  So when the
    experiment fails, the answer STAYS — and the engine must record that rather
    than log the undo the checkbox path performs."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=False, controls=controls,
                  declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    assert port.calls == [("No", True)], "no uncheck was attempted"
    assert out is controls, "the block stands"
    assert me._unblock_irreversible == [{
        "url": "https://app/x", "advance": "Continue", "field": "No",
        "reason": "radio_group_has_no_unanswered_state"}]


@pytest.mark.asyncio
async def test_a_group_that_already_holds_an_answer_is_never_the_experiment():
    """A default selection must survive.  A group with a committed member is
    ANSWERED, so it is never chosen — which is also why the restore-the-prior
    branch exists only for a group whose answer was committed after this
    function read the page."""
    grp = [_radio("Gold", group="g_plan", committed="true"),
           _radio("Silver", group="g_plan"), _radio("Bronze", group="g_plan")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=False, controls=controls,
                  declined=["Silver", "Bronze"])
    await _run(me, controls,
               SimpleNamespace(unfilled_fields=["Silver", "Bronze"]))

    assert port.calls == []
    assert me._unblock_irreversible == []


@pytest.mark.asyncio
async def test_a_control_that_refuses_the_answer_stops_the_experiment():
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port(accepts=False)
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert out is controls
    assert me._unblock_irreversible == [], "nothing was committed to record"


# ─── mixed pages: the compatibility guarantee ───────────────────────────────

@pytest.mark.asyncio
async def test_on_a_mixed_page_the_checkbox_is_tried_first():
    """THE NO-REGRESSION TEST.  Every page that has a declined checkbox must pick
    exactly the control it picked before radios existed.  Radios are reached only
    where the old code found nothing at all."""
    controls = ([_checkbox("None"), _checkbox("Asthma")] + _tobacco()
                + [_button("Continue", disabled=True)])
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["None", "Asthma", "Yes", "No"])
    await _run(me, controls,
               SimpleNamespace(unfilled_fields=["None", "Asthma", "Yes", "No"]))
    assert port.calls == [("None", True)], "the checkbox path owns this page"


@pytest.mark.asyncio
async def test_radios_are_reached_when_no_checkbox_is_answerable():
    """The checkbox on this page is already checked, so the old code returned
    early and the walk stopped.  The question next to it was always answerable."""
    controls = ([_checkbox("Consent", committed="true")] + _tobacco()
                + [_button("Continue", disabled=True)])
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Consent", "Yes", "No"])
    await _run(me, controls,
               SimpleNamespace(unfilled_fields=["Consent", "Yes", "No"]))
    assert port.calls == [("No", True)]


# ─── what the successful experiment RECORDS ─────────────────────────────────

@pytest.mark.asyncio
async def test_the_siblings_are_released_from_the_residue_with_the_answer():
    """"Yes" and "No" are one question.  Leaving "Yes" on an operator's to-do
    list asks someone to supply an answer we have, to a question that can hold
    one."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    fill = SimpleNamespace(unfilled_fields=["Yes", "No"])
    await _run(me, controls, fill)

    assert fill.unfilled_fields == [], fill.unfilled_fields
    assert me._fields_unfilled == []
    assert me._fields_seed_detail == []


@pytest.mark.asyncio
async def test_the_business_rule_names_the_question_not_only_the_answer():
    """A rule reading "Continue requires an answer to 'No'" is unreadable.  The
    DOM declared which question these answers belong to; the record quotes it."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    blocked = me._advance_blocked[0]
    assert "g_tobacco = No" in blocked["business_rule"], blocked["business_rule"]
    assert blocked["resolved_by_agent"] == "No"
    assert blocked["rule_reused"] is False


@pytest.mark.asyncio
async def test_the_answered_field_carries_unblock_provenance():
    """PROV_UNBLOCK means "this crawl proved it just now" — the strongest
    evidence in the catalogue, and it must not be reported as an ordinary fill."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    row = next(r for r in me._field_ledger if r["label"] == "No")
    assert row["provenance"] == PROV_UNBLOCK
    assert row["filled"] is True


@pytest.mark.asyncio
async def test_the_rule_outlives_the_crawl():
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    minted = me._rule_ledger.as_list()
    assert len(minted) == 1
    assert minted[0]["field_label"] == "No"


# ─── a rule an earlier crawl proved ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_known_rule_selects_the_answer_without_experimenting():
    """The reuse path must reach radios too, or every crawl of the same
    questionnaire re-derives the same twenty rules by guessing."""
    controls = ([_radio(n, group="g_plan")
                 for n in ("Gold", "Silver", "Bronze")]
                + [_button("Continue", disabled=True)])
    port = _Port()
    known = [rules.discover(url="https://app/x", blocked_label="Continue",
                            field_label="Bronze",
                            proof="proved earlier").as_dict()]
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Gold", "Silver", "Bronze"], known_rules=known)
    await _run(me, controls,
               SimpleNamespace(unfilled_fields=["Gold", "Silver", "Bronze"]))

    # NOT "Gold", which DOM order would have chosen.
    assert port.calls == [("Bronze", True)]
    assert me._advance_blocked[0]["rule_reused"] is True


@pytest.mark.asyncio
async def test_a_stale_rule_falls_back_to_the_experiment():
    """The rule names an answer this branch does not offer.  Forcing a control
    that is not there would be worse than experimenting."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()
    known = [rules.discover(url="https://app/x", blocked_label="Continue",
                            field_label="Platinum",
                            proof="proved earlier").as_dict()]
    me = _crawler(port, unblocks=True, controls=controls,
                  declined=["Yes", "No"], known_rules=known)
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    assert port.calls == [("No", True)]
    assert me._advance_blocked[0]["rule_reused"] is False


# ─── two questions on one step ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_the_first_question_is_answered_per_attempt():
    """A step blocked on five questions is blocked on all five, and answering
    them all would be a spree conducted on a guess.  One app-confirmed answer per
    attempt, each with its own evidence; the walk re-enters on the next
    observation if the block persists."""
    controls = (_tobacco()
                + [_radio(n, group="g_alcohol") for n in ("Yes", "No")]
                + [_button("Continue", disabled=True)])
    port = _Port()
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert len(port.calls) == 1, port.calls


# ─── the frequency scale (Gate 1 / T-RG-02) ─────────────────────────────────

@pytest.mark.parametrize("label", ["Never", "never", "Not at all", "not at all"])
def test_an_outright_denial_on_a_frequency_scale_is_recognised(label):
    """A health questionnaire does not only ask yes/no.  "How often do you use
    tobacco?" offers Never / Rarely / Weekly / Daily, and until this vocabulary
    carried "Never" the group fell to DOM order and the walk could answer
    "Daily" — fabricating a habit for a synthetic person on an insurance
    application."""
    from app import vocab
    assert vocab.NEGATIVE_OPTION_RE.match(label), label


@pytest.mark.parametrize("label", ["Rarely", "Occasionally", "Sometimes"])
def test_an_infrequent_disclosure_is_not_a_denial(label):
    """"Rarely" ASSERTS that the person does the thing, just not often.  The rule
    is "the option that asserts the LEAST", so auto-selecting it would invent a
    smaller version of the same fabrication.  Only outright denials belong in the
    vocabulary."""
    from app import vocab
    assert not vocab.NEGATIVE_OPTION_RE.match(label), label


@pytest.mark.parametrize("label", [
    "Nevertheless", "Nonsmoker", "Nonexistent condition", "Nodule",
    "Not applicable today",
])
def test_widening_the_vocabulary_did_not_widen_it_into_a_substring_rule(label):
    """The property the checkbox suite has always guarded, re-asserted after the
    change that could have broken it: the pattern is FULL-STRING anchored, so
    "never" cannot match inside "Nevertheless" any more than "none" can match
    inside "Nonexistent condition"."""
    from app import vocab
    assert not vocab.NEGATIVE_OPTION_RE.match(label), label


@pytest.mark.asyncio
async def test_a_frequency_question_is_answered_with_its_denial():
    """The end-to-end shape: four options, DOM order would pick the first, and
    the walk picks the one that invents nothing."""
    grp = [_radio(n, group="g_freq")
           for n in ("Daily", "Weekly", "Rarely", "Never")]
    controls = grp + [_button("Continue", disabled=True)]
    port = _Port()
    declined = ["Daily", "Weekly", "Rarely", "Never"]
    me = _crawler(port, unblocks=True, controls=controls, declined=declined)
    await _run(me, controls, SimpleNamespace(unfilled_fields=list(declined)))

    assert port.calls == [("Never", True)]


# ─── the residue must LEAVE the crawl (T-RG-01 · audit) ──────────────────────
#
# THE HOLE THESE TWO PIN.  The irreversible-experiment ledger above is asserted
# in memory and nowhere else: `_unblock_irreversible` had exactly three code
# sites in the whole engine — declared on the crawler, appended to by the
# walker, and read by the test on line 282.  Nothing carried it into the
# coverage payload, so the one fact it exists to preserve — this test
# environment now holds a committed answer the crawl put there and could not
# take back — died with the Crawler object.
#
# That is the exact failure `advance_blocked` was built to avoid, six lines
# above it in the same constructor, and it escapes via coverage.py.  The
# comment on the ledger promises the residue is "auditable rather than merely
# absent"; until these tests passed, it was precisely absent.

def _real_crawler(tmp_path) -> Crawler:
    """A REAL Crawler, because the thing under test is the payload builder — a
    stub that returned a dict would be testing the test."""
    from app.config import Settings
    from app.crawler import Budget, GuardContext
    from app.guard import load_refuse_pack
    pack = load_refuse_pack(Settings().refuse_pack_path)
    return Crawler(
        None, crawl_id="c1", tenant_id="t1", target_url="https://app.example/",
        work_dir=str(tmp_path), refuse_pack=pack,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=pack.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=pack),
    )


def test_an_irreversible_experiment_is_reported_out_of_the_crawl(tmp_path):
    """An operator deciding whether the form snapshot reflects the app's state
    or ours cannot read a Python list that was garbage-collected."""
    c = _real_crawler(tmp_path)
    c._unblock_irreversible.append({
        "url": "https://app/x", "advance": "Continue", "field": "No",
        "reason": "radio_group_has_no_unanswered_state"})

    payload = c._build_coverage()

    assert "unblock_irreversible" in payload, (
        "the residue of an experiment that could not be undone must leave the "
        "crawl; in memory only is how it was lost")
    assert payload["unblock_irreversible"] == [{
        "url": "https://app/x", "advance": "Continue", "field": "No",
        "reason": "radio_group_has_no_unanswered_state"}]


def test_the_irreversible_ledger_is_declared_in_the_coverage_contract():
    """coverage.py states this law twice in its own Protocol: "a payload key
    with no contract entry is how the crawler and its ledger drift apart"."""
    from app.coverage import CoverageHost
    assert "_unblock_irreversible" in getattr(CoverageHost, "__annotations__", {}), (
        "the payload reads this attribute, so the Protocol must declare it")


# ─── the experiment must run on the page the block was recorded on ──────────
#
# THE DEFECT THIS PINS, measured on a live deployment (vkpowerlife).
#
# `_discover`'s click-pass follows links to reveal content, and one of the
# controls it clicks on the product-selection page is the site LOGO — an <a>
# whose accessible name is "V VKPower Life Insurance".  Clicking it navigates
# the live page to `/`.  `_walk_wizard` already knows this can happen and
# re-establishes the entry step; the OUTER form path in discovery.py does not.
#
# So `_answer_to_unblock` was handed `controls` and a `url` describing the quote
# page while the browser was sitting on the home page.  It picked the right
# radio, called set_checked, and Playwright waited 30 SECONDS for a control that
# does not exist on `/` before failing — after which the log announced "the
# control did not take the answer", blaming an application that had never been
# asked.  Measured: URLB/URLA/LIVE all `…sslip.io/`, `radios=0`, while the walk
# believed it was on `…/life-insurance/quote/start/`.
#
# Both halves are defects: the wrong page, and the misattribution.

class _NavPort(_Port):
    """A port that knows WHERE it is — which the real browser port does, and
    the stub above deliberately did not."""

    def __init__(self, *, at: str, accepts: bool = True, goto_lands: bool = True):
        super().__init__(accepts=accepts)
        self._at = at
        self._goto_lands = goto_lands
        self.navigations: list[str] = []

    async def current_url(self):
        return self._at

    async def goto(self, url):
        self.navigations.append(url)
        if self._goto_lands:
            self._at = url
        return SimpleNamespace(url=self._at, ok=self._goto_lands)


@pytest.mark.asyncio
async def test_the_experiment_refuses_to_act_on_a_page_the_browser_has_left():
    """No click is attempted while the browser is somewhere else.  The 30-second
    timeout was the cost of finding that out by trying."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _NavPort(at="https://app/somewhere-else", goto_lands=False)
    me = _crawler(port, unblocks=False, controls=controls, declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    assert port.calls == [], (
        "a control was clicked on a page that does not contain it; that is the "
        "30s timeout the live crawl spent before blaming the application")
    assert out is controls
    assert me._unblock_irreversible == [], "nothing was committed, so no residue"


@pytest.mark.asyncio
async def test_a_drifted_page_is_re_established_and_the_experiment_then_runs():
    """The fix that unblocks the funnel: go back to the page the block was
    recorded on, then run the experiment there."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _NavPort(at="https://app/somewhere-else")
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    out = await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    assert port.navigations == ["https://app/x"], (
        "the walk must return to the page whose block it is testing")
    assert port.calls == [("No", True)], "and only then answer the question"
    assert me._rule_ledger.as_list(), "the app confirmed it; a rule is owed"


@pytest.mark.asyncio
async def test_a_page_that_never_drifted_is_not_re_navigated():
    """THE NO-REGRESSION TEST.  The overwhelmingly common case is that the
    browser is already where the walk thinks it is, and that case must not
    acquire a navigation it never needed."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _NavPort(at="https://app/x")
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))

    assert port.navigations == [], "no drift, so no navigation"
    assert port.calls == [("No", True)]


@pytest.mark.asyncio
async def test_a_port_that_cannot_say_where_it_is_still_runs_the_experiment():
    """Backward compatibility with every existing caller and test double: a port
    with no ``current_url`` cannot be checked, and an unverifiable page is not
    the same claim as a wrong one."""
    controls = _tobacco() + [_button("Continue", disabled=True)]
    port = _Port()                      # no current_url / goto
    me = _crawler(port, unblocks=True, controls=controls, declined=["Yes", "No"])
    await _run(me, controls, SimpleNamespace(unfilled_fields=["Yes", "No"]))
    assert port.calls == [("No", True)]
