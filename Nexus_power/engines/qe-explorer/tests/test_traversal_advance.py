"""TRAVERSAL POSTURE, part 2 — identifying the forward control.

A crawl can only catalogue a journey it can WALK, and it can only walk a journey
whose forward control it can IDENTIFY. Tier 1 recognises a control only when its
label carries a generic advance word, so an application whose steps read "Continue
to Payment", "Review Application" or "See My Quote" had no advance at all and every
journey was recorded one step deep — observed live on a carrier admin app: six
flows, every one ``steps: 1``.

Button wording is not something a test tool gets to standardise across a thousand
applications. On an environment the operator has ATTESTED, the crawl should use
every means it has to find the forward control — that is what the deeper tiers are
for, and they were previously reachable only by setting ``crawl_mode='e2e'``, a
SCOPE dial that most apps are (correctly) not onboarded with.

The load-bearing pin in this file is the LAST section: a deeper walk must not be a
laxer one. Widening how the forward control is IDENTIFIED must not widen what may
be CLICKED — the commit boundary and the refuse-pack danger gate hold in every
posture, and crossing them stays the separately-attested submit path.
"""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.crawler import (
    TRAVERSAL_FULL,
    TRAVERSAL_OBSERVE,
    TRAVERSAL_PROBE,
    Budget,
    Crawler,
    GuardContext,
)
from app.guard import load_refuse_pack

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)


def _crawler(tmp_path, **over) -> Crawler:
    kwargs = dict(
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/",
        work_dir=str(tmp_path), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE),
    )
    kwargs.update(over)
    return Crawler(None, **kwargs)


def _btn(name: str, **over) -> dict:
    c = {"kind": "button", "name": name, "disabled": False, "danger": False}
    c.update(over)
    return c


def _pick(crawler, controls):
    return asyncio.run(crawler._pick_advance(controls, "https://app.example/step", "S", "fp1"))


# ── Tier 2: a commit word in DESTINATION position ───────────────────────────

def test_probe_cannot_identify_continue_to_payment(tmp_path):
    """Baseline — the behaviour that produced one-step journeys. Tier 1 sees the
    commit word "Payment" and vetoes, and a probe has no second tier, so the
    funnel ends here with nothing to advance on."""
    assert _pick(_crawler(tmp_path), [_btn("Continue to Payment")]).control is None


def test_full_traversal_walks_continue_to_payment(tmp_path):
    """"Continue to Payment" navigates to the payment STEP; it does not pay. On an
    attested environment the walk follows it and the next page enters the
    catalogue."""
    d = _pick(_crawler(tmp_path, traversal=TRAVERSAL_FULL), [_btn("Continue to Payment")])
    assert d.control is not None and d.tier == 2


def test_full_traversal_still_refuses_a_conjunction_label(tmp_path):
    """"Continue & Place Order" says on its face that it commits, so no tier may
    take it — the destination rule is a SHAPE rule, not a keyword allowance."""
    assert _pick(_crawler(tmp_path, traversal=TRAVERSAL_FULL),
                 [_btn("Continue & Place Order")]).control is None


def test_a_plain_advance_word_is_walked_in_every_posture(tmp_path):
    """REGRESSION GUARD: the posture only ADDS tiers. What Tier 1 could already
    do must be untouched."""
    for posture in (TRAVERSAL_PROBE, TRAVERSAL_FULL):
        d = _pick(_crawler(tmp_path, traversal=posture), [_btn("Next")])
        assert d.control is not None and d.tier == 1, posture


# ── Tier 3: a label no regex can anticipate ─────────────────────────────────

def test_full_traversal_asks_the_oracle_when_regex_finds_nothing(tmp_path):
    """"Review Application" carries no advance word and no destination shape, so
    both regex tiers decline. It is still plainly the forward control to a human,
    and a thousand applications will each word this differently."""
    asked: list[int] = []

    async def oracle(candidates, page_title, page_url):
        asked.append(len(candidates or ()))
        return {"status": "picked", "index": 0, "signature": "sig-1"}

    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL, advance_oracle=oracle)
    d = _pick(c, [_btn("Review Application"), _btn("Back")])
    assert asked, "the oracle was never consulted"
    assert d.control is not None and d.tier == 3


def test_a_probe_never_calls_the_oracle(tmp_path):
    """Cost and blast radius both stay where the attestation is."""
    called = []

    async def oracle(candidates, page_title, page_url):
        called.append(1)
        return {"status": "picked", "index": 0, "signature": "sig-1"}

    _pick(_crawler(tmp_path, traversal=TRAVERSAL_PROBE, advance_oracle=oracle),
          [_btn("Review Application")])
    assert not called


# ══════════════════════════════════════════════════════════════════════════
# DEPTH IS NOT PERMISSION — the invariant this whole change rests on
# ══════════════════════════════════════════════════════════════════════════

def test_full_traversal_alone_never_crosses_a_submit(tmp_path):
    """THE LOAD-BEARING TEST.

    "Submit Application" is the commit boundary. Full traversal makes the crawl
    better at FINDING the way forward; it grants nothing. With no submit
    approvals and no attestation, this page is where the journey honestly ends —
    recorded as a submit boundary, not clicked.
    """
    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL)
    assert c._submit_enabled is False
    assert _pick(c, [_btn("Submit Application")]).control is None
    assert _pick(c, [_btn("Submit Application")]).submit_control is None


def test_the_oracle_is_never_offered_a_commit_control(tmp_path):
    """The oracle picks whatever a human would click, so anything a human must
    not be allowed to click for us is removed BEFORE it is asked — never filtered
    out of its answer afterwards."""
    seen: list[list[str]] = []

    async def oracle(candidates, page_title, page_url):
        seen.append([str(x.get("name")) for x in (candidates or ())])
        return {"status": "none"}

    c = _crawler(tmp_path, traversal=TRAVERSAL_FULL, advance_oracle=oracle)
    _pick(c, [_btn("Pay Now"), _btn("Place Order"), _btn("Review Application")])
    assert seen and seen[0] == ["Review Application"]


def test_a_danger_control_is_refused_in_every_posture(tmp_path):
    """The refuse pack is not on the traversal dial. "Start Over" wipes the
    application; no posture makes that a step forward."""
    for posture in (TRAVERSAL_PROBE, TRAVERSAL_FULL):
        d = _pick(_crawler(tmp_path, traversal=posture),
                  [_btn("Next", danger=True)])
        assert d.control is None, posture


def test_a_disabled_control_is_refused_in_every_posture(tmp_path):
    for posture in (TRAVERSAL_PROBE, TRAVERSAL_FULL):
        d = _pick(_crawler(tmp_path, traversal=posture),
                  [_btn("Continue", disabled=True)])
        assert d.control is None, posture


def test_observe_posture_identifies_no_advance_at_all(tmp_path):
    """Production is catalogued, never driven — so it must not even reach the
    deeper tiers, whatever the page offers."""
    c = _crawler(tmp_path, traversal=TRAVERSAL_OBSERVE)
    assert c._full_traversal is False
    assert _pick(c, [_btn("Continue to Payment")]).control is None
