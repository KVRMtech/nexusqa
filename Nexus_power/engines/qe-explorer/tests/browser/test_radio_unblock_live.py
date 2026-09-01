"""Gate 1 / T-RG-01 — THE RADIO UNBLOCK EXPERIMENT, RUN BY THE REAL CRAWLER.

WHY THIS MODULE HAD TO EXIST, stated as the gap it closes.

Two proofs of this feature already existed, and between them they proved
everything except the thing the feature claims:

* ``test_answer_to_unblock_radio.py`` drives ``_answer_to_unblock`` directly
  against dict fixtures — real code, no browser.  It proves the CHOICE.
* ``test_wizard_20_step.py`` drives fixture 27 in real Chromium — real browser,
  no crawler.  Its first test answers the radio with ``page.check()`` and
  watches Continue enable.  It proves the SHAPE is real and script-gated.
* ``test_m22_catalog_evidence.py`` runs a real Crawler and asserts a rule was
  discovered — but its gate (``I have reviewed the health questionnaire``) is a
  CHECKBOX.  It proves the checkbox path end to end.

Nothing ran the RADIO path through the real crawler in a real browser.  The
radio fixture was never crawled — ``Crawler(`` does not appear in the module
that owns it — so the sentence this milestone exists to support, "the crawl
answers a radio question the fill declined and the application lets it
through", was assembled from three tests that each proved a different third of
it.  That is precisely the shape of gap that let the original defect ship: each
part green, the join untested.

WHAT THIS ASSERTS is only what the run actually produced.  The fixture's own
behaviour is stated first, read off ``index.html`` by a human, so that a change
to the application fails this suite rather than silently re-baselining it.
"""
from __future__ import annotations

import json
import shutil
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

FIXTURE = "27-wizard-20-step-samefingerprint"

# ── What the application does, stated independently of the crawl ─────────────
#
# Read off fixture 27's index.html:
#   <button id="next" disabled>Continue</button>
#   <input type="radio" id="opt-yes" name="q01" value="yes"> Yes
#   <input type="radio" id="opt-no"  name="q01" value="no">  No
# and a script that enables #next only once one of them is checked.  ``required``
# appears on neither input, which is the whole point: the rule is invisible to
# any crawler that only reads markup.
GATE_CONTROL = "Continue"
#: The DOM's own name for the QUESTION — what ``_question_label`` recovers and
#: what makes the recorded rule name a question rather than only an answer.
GATE_QUESTION = "q01"
#: The answer ``_least_asserting`` must prefer: it matches NEGATIVE_OPTION_RE,
#: and on a health questionnaire it is the option that invents nothing.
GATE_ANSWER = "No"

CRAWL_OUT = H.HERE / "_crawl_out"


@pytest.fixture(scope="module")
def radio_crawl(pw, fixture_server) -> dict[str, Any]:
    """ONE real crawl of the radio-gated wizard, shared across this module."""
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        attestation=None,
        submit_flow_approved=False,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 12, "max_actions": 60, "max_requests": 400,
        "max_duration_ms": 300_000,
    })

    work_dir = CRAWL_OUT / "radio-unblock"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawl_id = "trg01-radio-unblock"
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id,
        tenant_id="proving-ground",
        target_url=fixture_server.url(FIXTURE),
        work_dir=str(work_dir),
        refuse_pack=pack,
        budget=budget,
        explorer_version=EXPLORER_VERSION,
        guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint="trg01-radio-unblock",
        guard_context=guard_ctx,
        identity_seed="qec-trg01-radio-unblock",
        observe_only=False,
    )
    result = pw.run(crawler.run())

    coverage = getattr(result, "coverage", None)
    if not isinstance(coverage, dict) or not coverage:
        coverage = (result or {}).get("coverage") if isinstance(result, dict) else {}
    assert coverage, "the crawl returned no coverage account"

    (work_dir / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True), encoding="utf-8")
    return {"coverage": coverage, "work_dir": work_dir}


# ─── 1. the crawl got through a gate no markup declares ─────────────────────

def test_the_crawl_advances_past_the_radio_gate(radio_crawl):
    """THE MILESTONE'S CLAIM, measured on a real run.

    Fixture 27 opens with ``#next`` disabled and nothing in the markup saying
    why.  A walk that cannot answer a radio question stops on step 1 — that is
    the production behaviour this work exists to end.  ``deepest_flow_steps``
    is the floor the flow ledger will vouch for, so > 1 is the whole proof.
    """
    flow = radio_crawl["coverage"].get("flow_summary") or {}
    assert flow.get("deepest_flow_steps", 0) > 1, (
        "the walk did not get past the radio-gated first step; the unblock "
        "experiment either did not run or did not clear the gate. flow=%s"
        % flow)
    assert (flow.get("advances_by_tier") or {}), (
        "the walk reported depth but no advance — depth without an advance is "
        "the collapse F1 was about")


# ─── 2. the app's own verdict was recorded as a rule ────────────────────────

def test_the_radio_gate_was_proved_and_recorded_as_a_rule(radio_crawl):
    """A6.4.  The rule is the deliverable; the advance is only how it was won."""
    rules = radio_crawl["coverage"].get("discovered_rules") or []
    assert rules, (
        "the crawl advanced past a gate but proved NO rule — the experiment "
        "ran and its evidence was not carried out on the completion")
    gate = [r for r in rules if r.get("blocked_label") == GATE_CONTROL]
    assert gate, ("no rule about %r. Proved: %s"
                  % (GATE_CONTROL, [r.get("blocked_label") for r in rules]))
    rule = gate[0]
    assert rule["field_label"] == GATE_ANSWER, (
        "the engine must answer with the option that asserts least; it chose %r"
        % rule["field_label"])
    assert rule["key"].startswith("rule:")
    assert rule["proof"], "a rule with no sentence proves nothing a reader can act on"


def test_the_rule_names_the_question_and_not_only_the_answer(radio_crawl):
    """A radio's label is the name of an ANSWER.  A rule that said only "No"
    would be unreadable: the reader needs to know *what* was answered No."""
    rules = radio_crawl["coverage"].get("discovered_rules") or []
    proofs = [r.get("proof") or "" for r in rules]
    assert any(GATE_QUESTION in p for p in proofs), (
        "no proof names the question %r the DOM itself declared. Proofs: %s"
        % (GATE_QUESTION, proofs))


# ─── 3. the residue of an experiment that could not be undone ───────────────

def test_the_irreversible_residue_is_accounted_for_on_the_payload(radio_crawl):
    """T-RG-01 audit.  The key must be PRESENT whether or not it has entries:
    an operator asking "did this crawl leave anything behind?" must be able to
    read the answer, and a missing key is not the answer "no".

    Empty here is the correct value and is itself a claim — every experiment
    this run made was confirmed by the application, so none had to be undone.
    """
    cov = radio_crawl["coverage"]
    assert "unblock_irreversible" in cov, (
        "the payload cannot say whether the crawl left a committed answer "
        "behind; the ledger existed in memory and never left the crawl")
    assert cov["unblock_irreversible"] == [], (
        "this run's experiments were all confirmed by the app, so nothing "
        "should have needed an undo: %s" % cov["unblock_irreversible"])


# ─── 4. the defect this run exposed, on the record as an executable test ────

@pytest.mark.xfail(strict=True, reason=(
    "T-RG-02 — rule identity uses the ANSWER label, so a SPA wizard whose every "
    "question is answered 'No' behind one 'Continue' at one URL mints ONE rule "
    "for all of them. Measured on this fixture: 6 tier-1 advances across 7 "
    "steps, questions q01..q07 experimented, 1 rule recorded."))
def test_each_distinct_question_proves_its_own_rule(radio_crawl):
    """CORRECT behaviour, written to fail until the identity is fixed.

    ``rule_key`` hashes (kind, url_template, blocked_label, field_label).  On a
    single-page wizard the first three are constant across all twenty steps, and
    ``field_label`` is the ANSWER — "No" — which ``_least_asserting`` picks every
    time.  So twenty distinct proven rules collapse onto one key and nineteen
    are deduped away.

    This matters exactly where the product is aimed: a health questionnaire IS a
    SPA with N required Yes/No groups behind one Continue, so the durable-learning
    store (M1.7 / T-GW-04) inherits one rule out of N for the application shape
    this milestone was built to walk.

    The engine already recovers the question — ``_question_label`` puts "q01" in
    the proof sentence.  The identity simply does not use it.
    """
    cov = radio_crawl["coverage"]
    advances = sum((cov.get("flow_summary") or {}).get("advances_by_tier", {}).values())
    rules = cov.get("discovered_rules") or []
    assert len(rules) >= 2, (
        "%d advances were won by answering distinct questions, but only %d "
        "rule(s) survived — the rest collided on a key that does not include "
        "the question" % (advances, len(rules)))
