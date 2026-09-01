"""A14 — "CONSENT TO EVERY ONE OF THESE" IS A GATE THE EXPERIMENT CANNOT CLEAR.

The unblock experiment answers ONE declined question per blocked step, and its
own comment says that is enough for a multi-question gate::

    ONE question, and the FIRST one the page asks. A blocked step with five
    unanswered questions is blocked on all five, and answering them all would be
    a form-filling spree conducted on a guess. Answering the first tests the
    rule; the walk re-enters this function on the next observation if the block
    persists, so a genuinely multi-question gate is still cleared — one
    app-confirmed answer at a time, each with its own evidence.

**That is true for radios and false for checkboxes, and the reason is the undo.**

A radio group with no prior selection CANNOT be un-answered, so a failed attempt
leaves its answer standing (recorded honestly in ``_unblock_irreversible``). The
page has therefore MOVED, and the next re-entry meets a smaller declined set and
picks a different question. Incremental progress is real.

A checkbox CAN be un-checked, so a failed attempt is reverted — restoring the
page byte-for-byte. The next re-entry meets the identical state, computes the
identical least-asserting pick, and reverts it again. **The very property that
makes the checkbox path safe is what makes it unable to progress.** A gate that
needs N boxes is never cleared for any N > 1, however many times the walk
re-enters.

WHERE THIS BITES, and why it is not hypothetical: vkpower-life's electronic
signature step is six consent checkboxes plus a typed name in front of a
disabled "Sign & Submit Application". Driving those six directly enables the
control and lands on ``/apply/confirmation/`` — "Application Submitted" — so the
funnel's far side is real and this is the thing between a crawl and it.

WHY THIS FILE ONLY REPRODUCES, AND FIXES NOTHING
================================================
Repairing it means resolving a genuine design tension in a safety-sensitive
path: the experiment promises "a failed attempt is UNDONE, so a change that
bought nothing never reaches the recorded form snapshot", and incremental
progress requires exactly the opposite — keeping answers the app has not yet
accepted. The radio path already chose the other horn (keep it, record it as
irreversible residue), so a coherent answer probably exists. It is not one to
improvise at the end of a gate, so it is recorded here as an executable,
strict-xfail defect rather than a ticket someone has to remember.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import rules
from app.crawler import Crawler

#: Same escape hatch the browser reproductions use: with it set, these fail
#: outright instead of being recorded as xfail.
_STRICT = os.environ.get("QEC_BUG_REPRO_STRICT") == "1"


def _repro(reason: str):
    if _STRICT:
        return pytest.mark.usefixtures()
    return pytest.mark.xfail(strict=True, reason=reason)


def _checkbox(name: str, *, committed: str = "false") -> dict:
    return {"kind": "checkbox", "name": name, "disabled": False, "danger": "",
            "value_committed": committed, "role": "checkbox", "tag": "input"}


def _button(name: str, *, disabled: bool) -> dict:
    return {"kind": "button", "name": name, "disabled": disabled, "danger": ""}


#: The shape of an electronic-signature step: N acknowledgements, ALL required,
#: with the requirement living in a script validator that no markup declares.
CONSENTS = ["I authorize release of medical information",
            "I acknowledge the HIPAA privacy notice",
            "I understand the replacement comparison",
            "I consent to electronic delivery",
            "I understand the fraud warning"]


class _Port:
    """A browser that remembers which boxes are checked, because that is the
    whole question here — a port with no memory cannot show that a second
    attempt met the state the first one left."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.checked: set[str] = set()

    async def set_checked(self, control, checked):
        name = str(control.get("name"))
        self.calls.append((name, checked))
        if checked:
            self.checked.add(name)
        else:
            self.checked.discard(name)
        return SimpleNamespace(intent_met=True,
                               committed_value="true" if checked else "false")


def _crawler(port, controls, declined, *, needs_all: bool,
             url: str = "https://app/signature"):
    """The application's OWN verdict drives Continue, exactly as the real one
    does: it enables only when every acknowledgement is checked."""

    async def _observe():
        enabled = (port.checked >= set(declined)) if needs_all else bool(port.checked)
        live = []
        for c in controls:
            if c.get("kind") == "button":
                continue
            c = dict(c)
            if c.get("name") in port.checked:
                c["value_committed"] = "true"
            live.append(c)
        return SimpleNamespace(
            raw_controls=live + [_button("Sign & Submit Application",
                                         disabled=not enabled)],
            url=url)

    return SimpleNamespace(
        _port=port, _observe=_observe, _refuse_pack=None,
        _advance_blocked=[{"url": url, "label": "Sign & Submit Application",
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
        me, controls, "Sign & Submit Application", "https://app/signature",
        SimpleNamespace(unfilled_fields=list(declined)))


# ── what happens today, asserted so the mechanism is measured not argued ────

@pytest.mark.asyncio
async def test_today_one_box_is_checked_and_then_unchecked_again():
    """The CURRENT behaviour, pinned. Not a bug report — the evidence for one.

    One attempt is made, the application declines to enable its control, and the
    box is put back. Note the second call: ``(name, False)``. That revert is what
    makes the next re-entry identical to this one.
    """
    controls = [_checkbox(c) for c in CONSENTS] + [
        _button("Sign & Submit Application", disabled=True)]
    port = _Port()
    me = _crawler(port, controls, CONSENTS, needs_all=True)

    await _run(me, controls, CONSENTS)

    assert len(port.calls) == 2, port.calls
    assert port.calls[0][1] is True and port.calls[1][1] is False
    assert port.calls[0][0] == port.calls[1][0], "the same box, set then unset"
    assert port.checked == set(), "the page is byte-for-byte as the app left it"


@pytest.mark.asyncio
async def test_today_re_entry_repeats_the_identical_attempt_forever():
    """THE MECHANISM. The comment's "one app-confirmed answer at a time" needs
    the page to have MOVED between attempts. After a revert it has not, so the
    second attempt is not the next question — it is the same question again."""
    controls = [_checkbox(c) for c in CONSENTS] + [
        _button("Sign & Submit Application", disabled=True)]
    port = _Port()
    me = _crawler(port, controls, CONSENTS, needs_all=True)

    for _ in range(3):
        await _run(me, controls, CONSENTS)

    picked = {name for name, checked in port.calls if checked}
    assert len(picked) == 1, (
        "three re-entries tried %d distinct questions; the checkbox path cannot "
        "advance because each attempt is reverted, so every re-entry meets the "
        "state the last one restored: %s" % (len(picked), sorted(picked)))


# ── the defect ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@_repro("a consent block needing every box is never cleared: each attempt is "
        "reverted, so re-entry repeats it rather than progressing")
async def test_a_consent_block_is_eventually_cleared():
    """CORRECT behaviour, and the reason it matters.

    Six of these sit in front of vkpower-life's real commit control. The
    application enables it the moment all are acknowledged — verified by driving
    them directly, which lands on /apply/confirmation/, "Application Submitted".

    This test does not say HOW to fix it. The radio path resolved the same
    tension by keeping an answer it could not revert and recording the residue;
    whether the checkbox path should do likewise, or cap the number of answers,
    or ask the operator, is a design decision this reproduction deliberately
    leaves open.
    """
    controls = [_checkbox(c) for c in CONSENTS] + [
        _button("Sign & Submit Application", disabled=True)]
    port = _Port()
    me = _crawler(port, controls, CONSENTS, needs_all=True)

    for _ in range(len(CONSENTS) + 2):
        await _run(me, controls, CONSENTS)

    assert port.checked == set(CONSENTS), (
        "the block was never cleared: %d of %d acknowledgements are checked"
        % (len(port.checked), len(CONSENTS)))


@pytest.mark.asyncio
async def test_a_single_box_gate_still_clears_exactly_as_before():
    """The compatibility guarantee this defect must not be fixed at the expense
    of: where ONE answer is enough, the experiment runs one attempt, the app
    accepts it, and nothing is reverted."""
    controls = [_checkbox(c) for c in CONSENTS] + [
        _button("Sign & Submit Application", disabled=True)]
    port = _Port()
    me = _crawler(port, controls, CONSENTS, needs_all=False)

    await _run(me, controls, CONSENTS)

    assert [c for c in port.calls if c[1] is False] == [], port.calls
    assert len(port.checked) == 1
    assert me._last_unblock_field in CONSENTS
