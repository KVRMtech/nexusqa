"""A14 — A CHOICE RENDERED AS BUTTONS IS NOT ANSWERABLE BY THE EXPERIMENT.

``_UNBLOCK_MULTI_KINDS`` is ``("checkbox", "toggle")``, and radio groups are
handled by their own path. A choice rendered as BUTTONS — the card-style
selector every modern design system ships — matches neither, so the candidate
list comes back empty, the radio path finds nothing, and the experiment returns
the page untouched. The walk then ends at a forward control the application
disabled, having made no attempt at all.

THIS IS NOT A HYPOTHETICAL SHAPE, AND THE FILL ENGINE ALREADY SEES IT. Measured
on vkpower-life, the crawl's own ``advance_blocked`` ledger records:

    url    /life-insurance/quote/start/
    label  "Continue"      reason  advance_disabled_by_app_validation
    missing_fields
        "Term Life Insurance Affordable coverage for a specific period"
        "Whole Life Insurance Lifetime coverage with cash value accumulation"
        "Universal Life Insurance Flexible premiums and adjustable death benefit"
        "Variable Universal Life Investment options with life insurance protection"

Those four are ``<button>`` cards. The fill engine has already identified them as
fields it could not fill and named them for the operator — so the information the
experiment needs is present and correctly labelled. Only the KIND filter drops
them.

The same shape gates the payment step (``disabled={!method}``, two cards: "ACH
Bank Transfer …" and "Credit / Debit Card …"), which is where the live crawl
currently stops at depth 12 of a 15-step funnel.

WHY THIS FILE ONLY REPRODUCES
=============================
Widening the experiment to click buttons widens the ACTUATOR surface, which is a
different and larger decision than answering a form control — the existing
docstring is explicit that it acts on "a form control, never an actuator — the
same class of act as typing into a text field". A button card is a form control
in intent and an actuator in mechanism, and which of those governs is exactly the
judgement that should not be made at the end of a gate.

A clicked card also cannot be un-clicked, so the same undo tension the radio path
resolved (keep it, record it as irreversible residue) would have to be resolved
here too — see ``test_answer_to_unblock_consent_block.py``.

There is a second route: this is a decision no deterministic rule can make, which
is precisely what the tier-3 oracle exists for (A18). The right control IS in the
tier-3 candidate set on the live application. Either layer could own it, and that
choice is a design decision rather than a fix.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import rules
from app.crawler import Crawler

_STRICT = os.environ.get("QEC_BUG_REPRO_STRICT") == "1"


def _repro(reason: str):
    if _STRICT:
        return pytest.mark.usefixtures()
    return pytest.mark.xfail(strict=True, reason=reason)


def _card(name: str) -> dict:
    """A choice rendered as a button — a payment method, a product tier, a plan.

    Not danger, not disabled, not commit-worded: an ordinary option the user is
    expected to pick exactly one of.
    """
    return {"kind": "button", "name": name, "disabled": False, "danger": "",
            "value_committed": "", "role": "button", "tag": "button"}


def _button(name: str, *, disabled: bool) -> dict:
    return {"kind": "button", "name": name, "disabled": disabled, "danger": ""}


#: vkpower-life's payment step, verbatim in shape.
METHODS = ["ACH Bank Transfer Direct debit from checking or savings",
           "Credit / Debit Card Visa, Mastercard, Discover, Amex"]
ADVANCE = "Continue to Beneficiary Designation"


class _Port:
    """Records everything asked of the browser, by whichever verb."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.chosen: set[str] = set()

    async def set_checked(self, control, checked):
        name = str(control.get("name"))
        self.calls.append(("set_checked", name))
        # A checked box IS a chosen method in the checkbox variant below, so the
        # application's verdict responds to it exactly as it responds to a click.
        # Without this the discriminator would be measuring the fake rather than
        # the filter under test.
        if checked:
            self.chosen.add(name)
        else:
            self.chosen.discard(name)
        return SimpleNamespace(intent_met=True, committed_value="true")

    async def click(self, control):
        name = str(control.get("name"))
        self.calls.append(("click", name))
        self.chosen.add(name)
        return SimpleNamespace(intent_met=True, committed_value="true")


def _crawler(port, controls, declined, url: str = "https://app/payment"):
    """The application enables its advance once ONE method has been chosen."""

    async def _observe():
        enabled = bool(port.chosen)
        live = [dict(c) for c in controls if c.get("name") != ADVANCE]
        return SimpleNamespace(
            raw_controls=live + [_button(ADVANCE, disabled=not enabled)],
            url=url)

    return SimpleNamespace(
        _port=port, _observe=_observe, _refuse_pack=None,
        _advance_blocked=[{"url": url, "label": ADVANCE,
                           "reason": "advance_disabled_by_app_validation",
                           "missing_fields": list(declined)}],
        _fields_unfilled=list(declined),
        _fields_seed_detail=[{"label": n, "url": url} for n in declined],
        _field_ledger=[{"label": n, "provenance": "needs_input", "filled": False}
                       for n in declined],
        _known_rules=rules.KnownRules(()),
        _rule_ledger=rules.RuleLedger(),
        _unblock_irreversible=[],
    )


@pytest.fixture(autouse=True)
def _identity_inventory(monkeypatch):
    monkeypatch.setattr("app.crawler.build_inventory",
                        lambda raw, pack, url="": list(raw))


async def _run(me, controls, declined):
    return await Crawler._answer_to_unblock(
        me, controls, ADVANCE, "https://app/payment",
        SimpleNamespace(unfilled_fields=list(declined)))


# ── what happens today ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_today_no_attempt_is_made_at_all():
    """The CURRENT behaviour, pinned as evidence.

    Not "the experiment tried and the app refused" — the experiment never runs.
    The two shapes it knows are checkboxes and radio groups; a button matches
    neither, so the candidate list is empty before any page is touched.
    """
    controls = [_card(m) for m in METHODS] + [_button(ADVANCE, disabled=True)]
    port = _Port()
    me = _crawler(port, controls, METHODS)

    out = await _run(me, controls, METHODS)

    assert port.calls == [], (
        "the experiment acted on a button-shaped choice: %s" % port.calls)
    assert out is controls, (
        "returning the SAME list is how this path says 'nothing to try' — a "
        "refreshed page would mean an attempt was made")


@pytest.mark.asyncio
async def test_today_the_same_page_with_checkboxes_is_answered():
    """The discriminator: identical page, identical declined labels, one
    difference — the choice is rendered as checkboxes. It is answered.

    So nothing about the page, the labels or the application's validation is
    what stops the button version. Only the KIND filter is.
    """
    boxes = [{"kind": "checkbox", "name": m, "disabled": False, "danger": "",
              "value_committed": "false", "role": "checkbox", "tag": "input"}
             for m in METHODS]
    controls = boxes + [_button(ADVANCE, disabled=True)]
    port = _Port()
    me = _crawler(port, controls, METHODS)

    await _run(me, controls, METHODS)

    assert [verb for verb, _ in port.calls] == ["set_checked"], port.calls


# ── the defect ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@_repro("a choice rendered as buttons matches neither _UNBLOCK_MULTI_KINDS nor "
        "the radio path, so the experiment never runs and the funnel ends at a "
        "control the application disabled")
async def test_a_button_shaped_choice_is_eventually_answered():
    """CORRECT behaviour, and what it is worth.

    This is the shape standing between the live vkpower-life crawl and its
    confirmation page: depth 12 of 15, stopped at ``disabled={!method}``.

    Deliberately silent on HOW. Widening the experiment to click buttons widens
    the actuator surface; the alternative is that tier 3 owns this decision,
    which is what A18 exists for and where the right control already appears in
    the candidate set. This test asserts the OUTCOME the funnel needs and leaves
    the layer to whoever takes that decision.
    """
    controls = [_card(m) for m in METHODS] + [_button(ADVANCE, disabled=True)]
    port = _Port()
    me = _crawler(port, controls, METHODS)

    await _run(me, controls, METHODS)

    assert port.chosen, (
        "no method was chosen, so the application's forward control stays "
        "disabled and the journey ends one step into its payment step")


@pytest.mark.asyncio
async def test_a_dangerous_card_is_never_chosen():
    """Whatever resolves this must keep the refusal that already holds
    everywhere else: a card the refuse pack flagged is not an option, even
    when choosing it would unblock the funnel."""
    controls = [dict(_card(METHODS[0]), danger="rp.verb.transfer"),
                _card(METHODS[1]), _button(ADVANCE, disabled=True)]
    port = _Port()
    me = _crawler(port, controls, METHODS)

    await _run(me, controls, METHODS)

    assert METHODS[0] not in port.chosen, (
        "a refuse-pack-flagged card was chosen: %s" % sorted(port.chosen))
