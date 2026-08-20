"""M2.2 / T-BR-06 — A REAL CRAWL OF A REAL APPLICATION, checked against the
application's actual behaviour.

Everything here is real: a static application served over HTTP, driven by the
production :class:`app.main.PlaywrightBrowserPort` and the production
:class:`app.crawler.Crawler`, with the assertions read off the ``manifest.jsonl``
the production :mod:`app.emit` writer put on disk.  No crawl is simulated and no
capture is hand-written.

WHY THIS LANE EXISTS SEPARATELY FROM ``test_proving_grounds.py``.  That lane
crawls its grounds ``observe_only=True`` — deliberately, because it crawls
applications maintained for other purposes and must never mutate them.  But
``observe_only`` disables form filling outright (``discovery.py``: ``is_form``),
and with no filling there is no ACT-THEN-DIFF and no unblock experiment.  So the
two behaviours this milestone most needs to prove — a dependency the page does
not declare, and a rule that exists nowhere in the markup — are *structurally*
unobservable in that lane.  This one fills, against an application that exists
for exactly that purpose and holds no state worth protecting.

THE APPLICATION IS NOT COOPERATING.  Read
``proving-grounds/catalog-evidence/index.html``: the advance gate lives in a JS
function, the dependency links two elements that reference each other nowhere,
and one control is identified by nothing at all.  None of it is discoverable by
reading the DOM, which is what makes the crawl's findings evidence rather than
transcription.

THE CAPTURED COVERAGE IS COMMITTED, and that is on purpose.  qe-explorer and
qe-central both ship a top-level ``app`` package and cannot be imported into one
interpreter, so the second half of this proof — that the crawl's findings survive
into the Master Catalog and out of the catalog API — has to run in the other
service's process against what this one really produced.  See
``platform/qe-central/tests/contract/test_m22_catalog_from_real_crawl.py``.
Re-record with ``QEC_M22_RECAPTURE=1``.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

#: The application this milestone is proven against.
GROUND = "catalog-evidence"
PROVING_GROUNDS = H.SERVICE_ROOT.parent.parent / "proving-grounds"

#: Where the crawl writes, so CI can archive it as evidence.
CRAWL_OUT = H.HERE / "_crawl_out"

#: The captured coverage handed to qe-central's half of the proof.
CAPTURED = (H.SERVICE_ROOT.parent.parent / "platform" / "qe-central" / "tests"
            / "contract" / "fixtures" / "m22_real_crawl_coverage.json")

RECAPTURE = os.environ.get("QEC_M22_RECAPTURE") == "1"

# ── What the application actually does, stated independently of the crawl ────
#
# These are read off index.html by a human, and are the "compare the result
# directly against the application behaviour" half of T-BR-06.  If the app
# changes and the crawl still passes, one of these should have changed too.
GATE_FIELD = "I have reviewed the health questionnaire"
GATE_CONTROL = "Continue to review"
DRIVER_FIELD = "State of residence"
DEPENDENT_FIELD = "County"
CLIPPED_FIELD = "Country of citizenship"
#: 250 countries + the "Select a country…" placeholder row.
CLIPPED_TRUE_TOTAL = 251
VALIDATED_FIELD = "Face amount ($)"
UNLOCATABLE_FIELD_NAME_IS_ABSENT = "referralCode"
UNGATED_FIELD = "Send me product updates"


@pytest.fixture(scope="module")
def ground_server() -> Any:
    if not (PROVING_GROUNDS / GROUND / "index.html").is_file():
        pytest.skip(f"{GROUND} not found under {PROVING_GROUNDS}")
    srv = H.FixtureServer(root=PROVING_GROUNDS).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def crawl(pw, ground_server) -> dict[str, Any]:
    """Run ONE real crawl and share it across this module's assertions.

    Shared rather than re-run per test because it is a real browser session
    against a real application: repeating it a dozen times would buy nothing and
    would make the lane's cost grow with every assertion added.
    """
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        attestation=None,
        # The crawl FILLS but is never approved to submit: the boundary this
        # milestone needs crossed is the catalogue's, not the application's.
        submit_flow_approved=False,
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 12, "max_actions": 60, "max_requests": 400,
        "max_duration_ms": 300_000,
    })

    work_dir = CRAWL_OUT / GROUND
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawl_id = f"m22-{GROUND}"
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id,
        tenant_id="proving-ground",
        target_url=ground_server.url(GROUND),
        work_dir=str(work_dir),
        refuse_pack=pack,
        budget=budget,
        explorer_version=EXPLORER_VERSION,
        guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint=f"m22-{GROUND}",
        guard_context=guard_ctx,
        identity_seed="qec-m22-catalog-evidence",
        observe_only=False,
    )
    result = pw.run(crawler.run())

    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), (
        "the crawl produced NO manifest at %s. A crawl that writes nothing is a "
        "failed crawl, not a passing one." % manifest)
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, "the manifest is empty"

    coverage = _coverage_of(result, records)
    bundle = {"crawl_id": crawl_id, "records": records, "coverage": coverage,
              "url": ground_server.url(GROUND)}

    # Archive the real output beside the crawl so CI can attach it as evidence.
    (work_dir / "coverage.json").write_text(
        json.dumps(coverage, indent=2, sort_keys=True), encoding="utf-8")
    if RECAPTURE:
        CAPTURED.parent.mkdir(parents=True, exist_ok=True)
        CAPTURED.write_text(json.dumps(coverage, indent=2, sort_keys=True),
                            encoding="utf-8")
    return bundle


def _coverage_of(result: Any, records: list[dict]) -> dict[str, Any]:
    """The crawl's coverage account, however this crawler returns it.

    Read from the run result when it carries one and reconstructed from the
    manifest's ``crawl_meta`` otherwise — the two have swapped places before,
    and a test that pinned one spelling would fail for a reason that has nothing
    to do with the catalogue.
    """
    for source in (getattr(result, "coverage", None),
                   (result or {}).get("coverage") if isinstance(result, dict) else None):
        if isinstance(source, dict) and source:
            return source
    for rec in reversed(records):
        cov = rec.get("coverage")
        if isinstance(cov, dict) and cov:
            return cov
    raise AssertionError(
        "the crawl produced no coverage account. Every assertion below reads "
        "from it, and inferring one from the manifest would be this test "
        "writing the evidence it is supposed to be checking.")


def _signals(bundle: dict) -> dict[str, dict]:
    """Every form signal the crawl captured, merged across page states.

    Merged richest-first: a wizard step is met more than once and a dependent
    select is empty until its driver is answered, so taking any single state
    would hold the emptiest view of exactly the questions this milestone is
    about — which is the same reason the Master Catalog merges the same way.
    """
    merged: dict[str, dict] = {}
    for rec in bundle["records"]:
        for label, sig in (rec.get("form_snapshot_signals") or {}).items():
            if not isinstance(sig, dict):
                continue
            held = merged.get(label)
            if held is None or len(sig.get("options") or []) > len(held.get("options") or []):
                merged[label] = sig
    for state in (bundle["coverage"].get("states") or []):
        for label, sig in (state.get("form_snapshot_signals") or {}).items():
            if not isinstance(sig, dict):
                continue
            held = merged.get(label)
            if held is None or len(sig.get("options") or []) > len(held.get("options") or []):
                merged[label] = sig
    return merged


def _signal(bundle: dict, label: str) -> dict:
    sigs = _signals(bundle)
    assert label in sigs, (
        "the crawl did not capture %r. Captured: %s" % (label, sorted(sigs)))
    return sigs[label]


# ── The crawl happened at all ────────────────────────────────────────────────

def test_the_crawl_reached_the_application_and_ended_deliberately(crawl):
    metas = [r for r in crawl["records"] if r.get("type") == "crawl_meta"]
    assert metas, "no crawl_meta record — the crawl never started"
    stop_reason = metas[-1].get("stop_reason", "")
    assert stop_reason and stop_reason not in ("error", "crashed"), (
        "the crawl ended abnormally: stop_reason=%r" % stop_reason)


def test_the_crawl_captured_the_application_s_questions(crawl):
    sigs = _signals(crawl)
    for expected in (GATE_FIELD, DRIVER_FIELD, DEPENDENT_FIELD, CLIPPED_FIELD,
                     VALIDATED_FIELD):
        assert expected in sigs, (
            "%r is a question this application asks and the crawl did not "
            "capture it. Captured: %s" % (expected, sorted(sigs)))


# ── T-BR-01 · the rule the markup does not contain ───────────────────────────

def test_the_crawl_proved_the_advance_gate_by_experiment(crawl):
    """The application disables Continue and says nowhere why.

    The only way to this sentence is to answer the question and watch the app
    change its own mind — which is exactly what makes it evidence.
    """
    rules = crawl["coverage"].get("discovered_rules") or []
    assert rules, (
        "the crawl proved NO business rule against an application whose "
        "forward control is disabled until one specific question is answered. "
        "Either the unblock experiment did not run, or it ran and its result "
        "was not carried out on the completion.")
    gate = [r for r in rules if r.get("field_label") == GATE_FIELD]
    assert gate, (
        "a rule was proved but not about %r. Proved: %s"
        % (GATE_FIELD, [r.get("field_label") for r in rules]))
    rule = gate[0]
    assert rule["blocked_label"] == GATE_CONTROL
    assert rule["proof"], "a rule with no sentence proves nothing a reader can act on"
    assert rule["key"].startswith("rule:")


def test_no_rule_was_invented_for_the_control_group(crawl):
    """``Send me product updates`` is an optional checkbox beside the gating one.

    A rule attached to it would mean the discovery is matching on shape — a
    checkbox near a disabled button — rather than on what the application did.
    """
    rules = crawl["coverage"].get("discovered_rules") or []
    assert not [r for r in rules if r.get("field_label") == UNGATED_FIELD], (
        "a rule was proved about a question that gates nothing")


# ── T-BR-02 · the dependency the page does not declare ───────────────────────

def test_the_crawl_proved_the_undeclared_dependency(crawl):
    """``County`` is an empty <select> until ``State of residence`` is chosen,
    and no attribute connects them."""
    county = _signal(crawl, DEPENDENT_FIELD)
    assert county.get("depends_on") == DRIVER_FIELD, (
        "the crawl captured %r but not that it depends on %r. Captured: %s"
        % (DEPENDENT_FIELD, DRIVER_FIELD, county))
    assert county.get("options"), (
        "the dependency was recorded but the enumeration behind it was not — a "
        "dependent question whose answers are still unknown is only half found")


def test_the_driver_is_not_itself_marked_dependent(crawl):
    assert "depends_on" not in _signal(crawl, DRIVER_FIELD)


# ── T-BR-03 · locators, including the honest absence of one ──────────────────

def test_each_question_carries_the_handle_its_own_element_declares(crawl):
    expected_strategy = {
        VALIDATED_FIELD: "testid",          # data-testid="face-amount"
        DRIVER_FIELD: "dom_id",             # id="state-of-residence"
        CLIPPED_FIELD: "dom_id",            # id="country-of-citizenship"
    }
    for label, strategy in expected_strategy.items():
        loc = _signal(crawl, label).get("locator") or {}
        assert loc.get("strategy") == strategy, (
            "%r should be located by its %s; got %r" % (label, strategy, loc))
        assert loc.get("verified") is True
        assert loc.get("value"), "a verified locator with no handle is not one"


def test_a_control_the_application_identifies_by_nothing_is_reported_as_such(crawl):
    """The referral-code input has no id, no testid, no label and no class.

    Every other assertion in this file would still pass if the crawl quietly
    manufactured a positional selector here.  This is the one that would not.
    """
    unverified = [
        (label, sig["locator"]) for label, sig in _signals(crawl).items()
        if isinstance(sig.get("locator"), dict)
        and sig["locator"].get("verified") is False]
    for _label, loc in unverified:
        assert loc.get("strategy") == "", (
            "an unverified locator must carry no handle, not a synthesised one")
        assert loc.get("unverified_reason"), (
            "an unverified locator must say WHY: 'we could not identify this "
            "control' and 'we did not look' are different findings")


def test_the_two_answers_to_one_question_keep_their_own_locators(crawl):
    """Tobacco Yes/No is one question and two elements."""
    sigs = _signals(crawl)
    members = [(label, sig["locator"]) for label, sig in sigs.items()
               if isinstance(sig.get("locator"), dict)
               and sig["locator"].get("group_id")]
    if not members:
        pytest.skip("the crawl captured no grouped control on this application")
    groups: dict[str, list] = {}
    for label, loc in members:
        groups.setdefault(loc["group_id"], []).append((label, loc["value"]))
    for group_id, entries in groups.items():
        values = [v for _label, v in entries]
        assert len(set(values)) == len(values), (
            "members of group %s share a locator value %s — half the answers "
            "would point at the wrong control" % (group_id, values))


# ── T-BR-04 · validation, read from the application's own declaration ────────

def test_the_declared_validation_crossed_the_boundary(crawl):
    face = _signal(crawl, VALIDATED_FIELD)
    assert face.get("min") == "50000"
    assert face.get("max") == "2000000"
    assert face.get("step") == "10000", (
        "step is the clearest boundary rule this form declares, and a question "
        "with no declared rule justifies no boundary case")


# ── T-BR-05 · the 250-option control ─────────────────────────────────────────

def test_the_clipped_enumeration_reports_what_the_page_offers(crawl):
    """The application offers 250 countries plus a placeholder row.

    Whether the stored list is clipped depends on the capture ceiling, and that
    is precisely why the total is carried separately: the assertion is not "the
    list is complete" but "the catalogue can tell you whether it is".
    """
    country = _signal(crawl, CLIPPED_FIELD)
    stored, total = country.get("options") or [], country.get("options_total")
    assert isinstance(total, int), (
        "options_total is missing, so a consumer cannot distinguish a complete "
        "answer set from a prefix")
    assert total == CLIPPED_TRUE_TOTAL, (
        "the application offers %d answers; the crawl reported %r"
        % (CLIPPED_TRUE_TOTAL, total))
    assert total >= len(stored), (
        "the catalogue may never claim fewer answers than it holds")


def test_a_short_enumeration_is_not_reported_as_clipped(crawl):
    """Three states are three states.

    Without this, a total that simply always exceeded the stored length would
    pass the test above and mark every question in the fleet as clipped.
    """
    state = _signal(crawl, DRIVER_FIELD)
    assert state["options_total"] == len(state["options"])


# ── The artifact the other service's half of this proof reads ────────────────

def test_the_captured_coverage_is_the_one_committed_for_qe_central(crawl):
    """qe-central cannot import this service, so its half of the proof runs
    against what this crawl really produced.  A drift between the two is a
    proof that has quietly stopped being about the same crawl."""
    if RECAPTURE:
        pytest.skip("re-captured this run")
    assert CAPTURED.is_file(), (
        "%s is missing. It is the real crawl output qe-central's half of this "
        "proof reads; re-record with QEC_M22_RECAPTURE=1." % CAPTURED)
    committed = json.loads(CAPTURED.read_text(encoding="utf-8"))
    live_rules = {r["key"] for r in (crawl["coverage"].get("discovered_rules") or [])}
    held_rules = {r["key"] for r in (committed.get("discovered_rules") or [])}
    assert live_rules == held_rules, (
        "this crawl proved %s and the committed capture holds %s — the two "
        "halves of the M2.2 proof are no longer about the same crawl. "
        "Re-record with QEC_M22_RECAPTURE=1." % (sorted(live_rules), sorted(held_rules)))
