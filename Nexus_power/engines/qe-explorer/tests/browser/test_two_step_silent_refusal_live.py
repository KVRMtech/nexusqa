"""B1-S + B2 IN REAL CHROMIUM — the two-step silent refusal, end to end.

WHY THIS MODULE HAD TO EXIST, in the pattern test_radio_unblock_live.py set:
the step-back reader and the closed loop are proven against scripted ports and
against a TRANSCRIPTION of summit — real crawler, fake browser.  Nothing had
run them through real Chromium over a real DOM, where visibility, input
events, and the settle behaviour are the browser's own.  Fixture 32 is the
smallest application with the live failure shape: a two-step form whose
refusal is written into step 1's plain ``<p>`` — no ARIA, no error id — while
the commit lives on step 2 and nothing visible changes when it is refused.

TWO CRAWLS, ONE AXIS APART:

* ``reader_crawl`` runs with ``QEC_REFUSAL_RETRY_MAX=0`` — the loop OFF — and
  must NAME the refusal (field, the application's own mask sentence,
  ``steps_back=1``, the forward-walk text licence) while still reporting the
  journey honestly incomplete with the boundary spent exactly once.
* ``loop_crawl`` runs with the loop at its shipped default and must CROSS:
  one repair, one retry, the application's own confirmation banner observed,
  both attempts on the record.

The fixture's own behaviour is stated off its source (a human read
index.html), so a change to the application fails this suite rather than
silently re-baselining it.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright,
              pytest.mark.proving_ground]

FIXTURE = "32-two-step-silent-refusal"

# ── What the application does, read off index.html ─────────────────────────
COMMIT = "Submit Application"
FIELD = "Phone Number"
#: The exact sentence submit() writes into step 1's hidden message node.
RULE = "Phone Number must be (999) 999-9999"
#: The confirmation banner shown only when the phone satisfies the mask.
BANNER = "Application received. Confirmation #TS-204."

CRAWL_OUT = H.HERE / "_crawl_out"


def _crawl(pw, fixture_server, *, name: str, retry_max: str) -> dict[str, Any]:
    """ONE real crawl of fixture 32, boundary-granted and attested."""
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import Attestation, load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    url = fixture_server.url(FIXTURE)
    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        # The commit needs the same licence any crossing needs: a disposable
        # attestation plus the operator's named grant. Nothing is loosened for
        # the fixture — that is the point of driving the production path.
        attestation=Attestation(attested_by="browser-lane",
                                env_kind="disposable",
                                reset_procedure="rebuild",
                                expires_at_ms=4_102_444_800_000),
        submit_flow_approved=True,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 10, "max_actions": 80, "max_requests": 400,
        "max_duration_ms": 300_000,
    })
    work_dir = CRAWL_OUT / name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    prior = os.environ.get("QEC_REFUSAL_RETRY_MAX")
    os.environ["QEC_REFUSAL_RETRY_MAX"] = retry_max
    try:
        crawler = Crawler(
            PlaywrightBrowserPort(pw.page, pw.context),
            crawl_id=name,
            tenant_id="proving-ground",
            target_url=url,
            work_dir=str(work_dir),
            refuse_pack=pack,
            budget=budget,
            explorer_version=EXPLORER_VERSION,
            guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version,
            config_fingerprint=name,
            guard_context=guard_ctx,
            identity_seed="qec-%s" % name,
            observe_only=False,
            crawl_mode="e2e",
            wizard_enabled=True,
            e2e_wizard_steps=30,
            boundary_approvals=[{"control": COMMIT, "url": url,
                                 "max_crossings": 1,
                                 "approved_by": "browser-lane"}],
            submit_approvals=[],
        )
        result = pw.run(crawler.run())
    finally:
        if prior is None:
            os.environ.pop("QEC_REFUSAL_RETRY_MAX", None)
        else:
            os.environ["QEC_REFUSAL_RETRY_MAX"] = prior

    coverage = getattr(result, "coverage", None)
    if not isinstance(coverage, dict) or not coverage:
        coverage = (result or {}).get("coverage") if isinstance(result, dict) else {}
    assert coverage, "the crawl returned no coverage account"
    (work_dir / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True, default=str),
        encoding="utf-8")
    return coverage


@pytest.fixture(scope="module")
def reader_crawl(pw, fixture_server) -> dict[str, Any]:
    return _crawl(pw, fixture_server, name="trg32-reader", retry_max="0")


@pytest.fixture(scope="module")
def loop_crawl(pw, fixture_server) -> dict[str, Any]:
    return _crawl(pw, fixture_server, name="trg32-loop", retry_max="1")


# ── 1 · the reader, loop OFF: named, honest, exactly once ──────────────────

def test_the_silent_refusal_is_named_where_the_field_lives(reader_crawl):
    """B1-S's own done-when, in a real browser: the bundle carries a
    validation_rejections row naming the field the validator refused, read one
    step back, licensed by the forward walk's snapshot."""
    rows = [r for r in reader_crawl.get("validation_rejections", [])
            if r.get("field") == FIELD]
    assert rows, (
        "the refused field was not named; rejections=%r"
        % reader_crawl.get("validation_rejections"))
    row = rows[0]
    assert "(999) 999-9999" in row["rule"], "the app's own words were not kept"
    assert row["steps_back"] == 1
    assert row["anchored_by"] == "text_names_control", (
        "no ARIA exists in this fixture, so only the forward-walk text "
        "licence can carry the claim; got %r" % row["anchored_by"])


def test_with_the_loop_off_the_crossing_stays_exactly_once(reader_crawl):
    assert reader_crawl["boundaries_crossed"] == 1
    assert reader_crawl["journeys_completed"] == 0
    milestones = reader_crawl["outcome_milestones"]
    assert len(milestones) == 1 and milestones[0]["verified"] is False


# ── 2 · the loop ON: one repair, one retry, the app's own banner ───────────

def test_the_closed_loop_completes_the_journey_in_real_chromium(loop_crawl):
    assert loop_crawl["journeys_completed"] == 1, (
        "the repaired retry did not complete; milestones=%r"
        % [(m.get("outcome"), m.get("confirmation_rung"))
           for m in loop_crawl.get("outcome_milestones", [])])
    milestones = loop_crawl["outcome_milestones"]
    assert len(milestones) == 2, "both attempts must stay on the record"
    assert milestones[0]["verified"] is False
    assert milestones[1]["verified"] is True
    assert BANNER.split("#")[0].strip() in str(
        milestones[1].get("confirmation_detail") or ""), (
        "the confirmation must be the application's own banner")


def test_the_loop_left_the_evidence_joined_up(loop_crawl):
    """Named -> repaired -> retried, as three connected facts on one bundle."""
    rows = [r for r in loop_crawl.get("validation_rejections", [])
            if r.get("field") == FIELD]
    assert rows and rows[0].get("repaired") is True
    assert loop_crawl["boundaries_crossed"] == 2
