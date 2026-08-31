"""B1-S HARDENING — THE FORWARD WALK LICENSES THE TEXT RUNG ON A STEPPED-BACK PAGE.

THE GAP THIS CLOSES, and why a07cb59 left it open on purpose.  The step-back
reader adopted ANCHORED rejections only: a plain-text refusal on a stepped-back
page had no before-snapshot to diff against, and an after-only read would score
a step's standing helper text as a verdict.  That was the right refusal — and
it leaves every application that renders per-field errors as bare ``<p>``
INSIDE a multi-step form unreadable.  vkpower renders exactly that way; summit
merely happens to annotate.

THE LICENCE THAT DOES EXIST: the walk stood on this very step on its way to the
commit and captured its texts then (``_note_step_texts``, keyed by the same
actionable-set signature the scan uses to tell steps apart).  Text present at
the stepped-back read that is ABSENT from that snapshot appeared as a result of
the refused commit — a real ACT-THEN-DIFF, with the act being the commit.

Every adoption below is paired with a control that removes exactly one licence
condition and requires the rung to stay withheld.
"""
from __future__ import annotations

from app.crawl_constants import _candidate_sig
from app.inventory import build_inventory
from tests.test_the_message_lives_where_the_field_lives import (
    _PACK, _URL, _field_step, _review_step, _WizardPort, _walker)

#: A rule that NAMES its control, so the strongest text rung is reachable.
_RULE = "Face Amount ($) must be at least $50,000"
#: A sentence that was on the step BEFORE the commit — helper text, not a
#: verdict, however rejection-shaped it reads.
_STANDING = "Face amount is required to quote this product"


def _sig_of(step_controls) -> str:
    """The signature the production writer would have recorded for this step —
    computed through the REAL inventory, so the key matches byte for byte."""
    return _candidate_sig(build_inventory(step_controls, _PACK, url=_URL))


def _plain_text_wizard():
    """Review step + one field step whose refusal is PLAIN TEXT: no
    aria-invalid, no error node, nothing the anchored ladder can read."""
    field_controls, _ = _field_step(anchored=False, plain_text=False)
    port = _WizardPort(
        [field_controls, _review_step()],
        form_texts_by_step=[[_RULE, _STANDING], []])
    return port, field_controls


# ── the adoption, and its controls ─────────────────────────────────────────

def test_a_snapshot_that_lacks_the_text_licenses_the_rung():
    """THE CLOSURE.  The walk's snapshot holds the step's ordinary text and
    not the rule; the rule therefore APPEARED after the commit, and the rung
    may speak — attributed to the control the sentence names, and labelled
    with how far back it was read."""
    port, field_controls = _plain_text_wizard()
    w = _walker(port)
    w._walk_step_texts = {_sig_of(field_controls): (_STANDING, "Coverage")}
    import asyncio
    named = asyncio.run(w._read_rejections_by_stepping_back(
        url=_URL, trigger="commit:Submit Application", max_steps=4))
    assert named == 1, "the licensed transition was not read"
    rec = w._validation_rejections[0]
    assert rec["rule"] == _RULE
    assert rec["field"] == "Face Amount ($)", (
        "the sentence names its control and the record must say so")
    assert rec["anchored_by"] == "text_names_control"
    assert rec["steps_back"] == 1, "the weaker claim must stay legible"


def test_control_text_already_in_the_snapshot_is_never_a_verdict():
    """Remove ONE condition: the rule was on the step BEFORE the commit.  A
    standing sentence is a fact about the step, not about the click, and
    adopting it would fail forms that never refused anything."""
    port, field_controls = _plain_text_wizard()
    w = _walker(port)
    w._walk_step_texts = {_sig_of(field_controls): (_STANDING, _RULE)}
    import asyncio
    named = asyncio.run(w._read_rejections_by_stepping_back(
        url=_URL, trigger="commit:Submit Application", max_steps=4))
    assert named == 0
    assert w._validation_rejections == []


def test_control_no_snapshot_keeps_the_a07cb59_refusal():
    """Remove the licence entirely: no snapshot, no diff, no claim — exactly
    the shipped behaviour, byte for byte.  (The anchored ladder above the rung
    still ran; it had nothing to anchor.)"""
    port, _field_controls = _plain_text_wizard()
    w = _walker(port)
    import asyncio
    named = asyncio.run(w._read_rejections_by_stepping_back(
        url=_URL, trigger="commit:Submit Application", max_steps=4))
    assert named == 0
    assert w._validation_rejections == []


def test_control_a_snapshot_for_a_different_step_licenses_nothing():
    """The key is the step's own actionable signature: another step's snapshot
    is another page's history, and diffing against it would attribute one
    step's furniture to another's verdict."""
    port, _field_controls = _plain_text_wizard()
    w = _walker(port)
    w._walk_step_texts = {_sig_of(_review_step()): (_STANDING,)}
    import asyncio
    named = asyncio.run(w._read_rejections_by_stepping_back(
        url=_URL, trigger="commit:Submit Application", max_steps=4))
    assert named == 0


def test_the_snapshot_store_is_bounded():
    """A sixty-step walk must not grow an unbounded dict: the store keeps the
    most recent :data:`_MAX_STEP_TEXT_SNAPSHOTS` steps and drops the oldest."""
    port, _f = _plain_text_wizard()
    w = _walker(port)
    for i in range(40):
        w._note_step_texts("sig-%d" % i, ["text-%d" % i])
    store = w._walk_step_texts
    assert len(store) == w._MAX_STEP_TEXT_SNAPSHOTS
    assert "sig-0" not in store and "sig-39" in store


def test_an_empty_signature_is_never_a_key():
    port, _f = _plain_text_wizard()
    w = _walker(port)
    w._note_step_texts("", ["whatever"])
    assert getattr(w, "_walk_step_texts", None) is None
