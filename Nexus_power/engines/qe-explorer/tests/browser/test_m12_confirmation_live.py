"""M1.4 / T-CF-04 LIVE — the M1.2 confirmation recognizer, in a real browser.

The deterministic proof in ``tests/test_confirmation_terminal.py`` runs the real
crawler over a TRANSCRIBED model of this application and asserts the
transcription against the application's own source.  That is the gate for the
TERMINAL DECISION.  This module proves the other half — that the recognizer
those decisions rest on says the right thing about the REAL pages, rendered by
real Chromium and read through the production
:class:`app.playwright_port.PlaywrightBrowserPort`:

    every step of the nine-step application funnel  ->  NOT a confirmation
    the application's own confirmation page          ->  confirmation

Both halves matter, and the first one matters more.  A recognizer that fires on
the confirmation page is easy; one that does NOT fire on the eight pages before
it is the hard part, and it cannot be proved on a fixture, because a fixture
only contains the text its author remembered to put there.  These pages contain
whatever they actually contain — including a step indicator whose last label is
the literal word "Confirmation", a "Policy Number" field on the replacement
step, and a "Print Confirmation" BUTTON on the confirmation page itself.  Every
one of those matches the success vocabulary; none of them is a declaration that
anything happened.

WHAT THIS DELIBERATELY DOES NOT CLAIM.  It does not walk the funnel.  Reaching
the confirmation by traversal means clearing a cross-field business rule that
spans five pages — the typed signature on step nine must equal a name the
personal-info step wrote to sessionStorage on step two — which the fill engine
does not satisfy today.  That is an M1.2 traversal limitation, unchanged by
M1.4: a live crawl entered at the signature step stops honestly at
``submit_boundary``, and with a boundary approval crosses once and records
``outcome=error``, because the application rejects its own form.  Asserting
around that would be asserting on a defect belonging to another milestone.

TWO WAYS TO REACH THE APPLICATION, the same two the sibling proving-ground
module uses.  In CI the ``proving-ground`` workflow builds vkpower-life from its
own Dockerfile and publishes it, and this module reads
``QEC_PROVING_GROUND_URL``.  Locally it serves the static export::

    cd Nexus_power/proving-grounds/vkpower-life && npm install && npm run build

and skips loudly if that has not been built — a CI file that has never executed
is a plan, not a proving ground.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

_GROUND = H.SERVICE_ROOT.parent.parent / "proving-grounds" / "vkpower-life"
_EXPORT = _GROUND / "out"

#: The nine application steps, in funnel order, followed by the confirmation.
#: Order matters: the recognizer diffs against everything the journey has
#: already read, so reading them out of order would not be the same test.
FUNNEL = [
    "/life-insurance/apply/member-lookup/",
    "/life-insurance/apply/personal-info/",
    "/life-insurance/apply/replacement/",
    "/life-insurance/apply/health/",
    "/life-insurance/apply/lifestyle/",
    "/life-insurance/apply/decision/",
    "/life-insurance/apply/payment/",
    "/life-insurance/apply/beneficiary/",
    "/life-insurance/apply/signature/",
]
CONFIRMATION = "/life-insurance/apply/confirmation/"


#: Set by the ``proving-ground`` CI job. Honoured ONLY on the vkpower-life leg —
#: the other legs serve a different application entirely, and reading their URL
#: here would assert this funnel's pages against acme-life's markup.
_CI_URL = (os.environ.get("QEC_PROVING_GROUND_URL") or "").rstrip("/")
_CI_NAME = os.environ.get("QEC_PROVING_GROUND_NAME") or ""


@pytest.fixture(scope="module")
def origin() -> Any:
    if _CI_URL and _CI_NAME == "vkpower-life":
        yield _CI_URL
        return
    if _CI_URL:
        pytest.skip(
            f"QEC_PROVING_GROUND_URL is serving {_CI_NAME!r}, not vkpower-life")
    if not (_EXPORT / "index.html").is_file():
        pytest.skip(
            "vkpower-life static export not built. Run `npm install && npm run "
            f"build` in {_GROUND} — skipping rather than pretending to crawl it.")
    srv = H.FixtureServer(root=_EXPORT).start()
    try:
        yield srv.origin
    finally:
        srv.stop()


def _read_the_funnel(pw, origin: str):
    """Read every page in funnel order through the PRODUCTION port.

    Returns ``[(path, detail, rung, control_names)]`` — one verdict per page,
    each diffed against everything read before it, exactly as ``_walk_wizard``
    diffs a journey.
    """
    from app.boundary import confirmation_transition
    from app.playwright_port import PlaywrightBrowserPort

    ctx = pw.run(pw.fresh_context())
    try:
        page = pw.run(ctx.new_page())
        port = PlaywrightBrowserPort(page, ctx)
        history: list[str] = []
        out = []
        for path in FUNNEL + [CONFIRMATION]:
            pw.run(port.goto(origin + path))
            texts = pw.run(port.visible_texts())
            raw = pw.run(port.collect_controls())
            names = [str(c.get("name") or "") for c in raw if c.get("name")]
            detail, rung = confirmation_transition(
                history, texts, control_names=names)
            out.append((path, detail, rung, names))
            history.extend(texts)
        return out
    finally:
        pw.run(ctx.close())


@pytest.fixture(scope="module")
def verdicts(pw, origin):
    return _read_the_funnel(pw, origin)


def test_no_application_step_declares_itself_complete(verdicts):
    """THE HARD HALF. Nine real pages, none of which has completed anything.

    A false positive here would be worse than the bug M1.4 fixed: it would end a
    nine-step funnel at step three and report the truncation as a covered
    journey — green-wash arriving through the very mechanism built to stop it.
    """
    fired = [(path, detail, rung) for path, detail, rung, _n in verdicts
             if path != CONFIRMATION and rung]
    assert not fired, (
        "an application STEP was read as a confirmation: "
        + "; ".join(f"{p} -> {r}: {d!r}" for p, d, r in fired))


def test_the_success_vocabulary_really_is_present_on_those_pages():
    """The negative above is only worth having if it was under pressure.

    This pins that the funnel genuinely contains success-shaped text. Without
    it, the test above would pass just as well on an application that never says
    "confirmation" at all, and would quietly stop testing anything the day the
    pages changed.
    """
    from app.boundary import _SUCCESS_RE

    src = _GROUND / "src" / "app" / "life-insurance" / "apply"
    hits = {p.parent.name for p in src.rglob("page.tsx")
            if _SUCCESS_RE.search(p.read_text(encoding="utf-8"))}
    assert len(hits) >= 5, (
        f"only {sorted(hits)} carry success vocabulary — this proof has lost "
        "its teeth and the funnel model should be re-derived")


def test_the_real_confirmation_page_is_recognized(verdicts):
    """THE MILESTONE CLAIM, on the real page: the application's own words, on a
    named rung, reached through the production capture path."""
    from app.boundary import RUNG_TRANSITION_TEXT

    path, detail, rung, _names = verdicts[-1]
    assert path == CONFIRMATION
    assert rung == RUNG_TRANSITION_TEXT, (
        f"the real confirmation page was not recognized: rung={rung!r}")
    assert "submitted" in detail.lower()


def test_the_confirmation_is_the_banner_and_not_the_button(verdicts):
    """The page offers a ``Print Confirmation`` BUTTON, which carries the
    success vocabulary and declares nothing. The detail must be the banner."""
    _path, detail, _rung, names = verdicts[-1]
    assert any("Print Confirmation" in n for n in names), (
        "the confirmation page no longer offers the button this guard exists "
        "for — re-derive the proof rather than deleting the assertion")
    assert detail not in names


def test_the_recognized_landing_is_a_completing_terminal(verdicts):
    """The last link in the chain, closed on real evidence: what the browser
    observed on the real page satisfies the predicate the walk gates on, and the
    terminal that predicate selects is a COMPLETING one."""
    from app import flow_ledger
    from app.boundary import is_confirmation_landing

    _path, _detail, rung, _names = verdicts[-1]
    assert is_confirmation_landing(outcome="navigation", rung=rung, changed=True)
    terminal = flow_ledger.resolve_walk_terminal(confirmation=True)
    assert terminal == flow_ledger.TERMINAL_CONFIRMATION
    assert terminal in flow_ledger.COMPLETING_TERMINALS
