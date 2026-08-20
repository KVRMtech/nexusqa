"""M2.4 / T-GEN-06 — the whole generation path, proven by execution.

THE STOP CONDITION THIS FILE IS WRITTEN AGAINST, quoted so it cannot drift:

    Do not declare success because a Playwright file was generated.  The actual
    definition is: generated journey executes + network assertion executes +
    outcome assertion executes + real regression turns the test red.

So every claim below is made by RUNNING something.  The pipeline runs end to end
(discovered journey → criticality → Top-20 → compilation → lint → network
assertion → outcome assertion), the compiled spec runs in a real Chromium against
a real HTTP application, and then the application is BROKEN and the same spec is
run again and has to fail — for the right reason, named.

TWO REGRESSIONS, DELIBERATELY ORTHOGONAL.  A single regression could be caught by
luck.  These two cannot be caught by each other's oracle:

  * ``NETWORK_SILENT`` breaks only the BACKEND CALL.  The button still works, the
    navigation still happens, the result page still reads $42.50 — because the
    page now renders a constant instead of asking the API.  Every UI assertion in
    the spec passes.  Only the endpoint assertion can see it, which is exactly
    the claim T-GEN-03 makes.
  * ``OUTCOME_DRIFT`` breaks only the VALUE.  The endpoint is called, answers 200,
    and the page renders — with the wrong premium.  Only a hard outcome oracle
    can see it, which is exactly the claim T-GEN-04 makes.

If either oracle were decorative, the corresponding run would come back green and
this file would fail.

The proof also asserts the negative case that makes the positives meaningful: the
same spec is GREEN against the healthy application.  A suite that reds on a
working system has proven nothing about a broken one.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from m24_generation import crawl_evidence as ev          # noqa: E402
from m24_generation import fixture_app                   # noqa: E402
from m24_generation import pw_runner                     # noqa: E402
from m24_generation.service_import import load           # noqa: E402

pytestmark = pytest.mark.m24


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def quote_app():
    """The live application, started once and seeded per test.

    ONE server for the whole module on purpose: the baseline run and the
    regression runs then differ in exactly the seeded defect and in nothing else
    — not the port, not the process, not a restart's timing.
    """
    server = fixture_app.QuoteAppServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="module")
def pipeline(quote_app):
    """Run the ENTIRE M2.4 pipeline once, and hand the result to every test.

    discovered journeys → criticality → rank → Top-N → compile → lint.  Each
    stage is the production module, loaded one service at a time (three services
    here ship a package called ``app``; see :mod:`service_import`).  Every stage
    hands the next one plain data, which is what makes the switch safe.
    """
    origin = quote_app.origin

    # ── stage 1 · M2.5 endpoint inventory (explorer) ─────────────────────
    inventory = ev.endpoint_inventory(origin)

    # ── stage 2 · criticality, ranking and the compile payloads (qe-central)
    criticality = load("qe_central", "app.services.criticality")
    jcrit = load("qe_central", "app.services.journey_criticality")
    jspec = load("qe_central", "app.services.journey_spec")
    emap = load("qe_central", "app.services.endpoint_map")

    nodes_by_fp = ev.nodes(origin)
    edges = ev.edges()
    traversals = ev.traversals()

    entries = []
    for journey in ev.journeys(origin):
        traversal = traversals.get(journey["journey_id"])
        path_fps = list((traversal or {}).get("path_fps") or [])
        journey_nodes = [nodes_by_fp[fp] for fp in path_fps if fp in nodes_by_fp]
        edge_labels = [e["trigger_label_norm"] for e in edges
                       if e["from_fp"] in path_fps]
        banded = jcrit.evaluate_journey(
            journey, journey_nodes, edge_labels=edge_labels)
        entry = ev.rollup_for(journey, traversal)
        entry["criticality"] = banded
        entry["boundary_nodes"] = jcrit.boundary_node_count(journey_nodes)
        entry["endpoints_observed"] = jcrit.endpoint_count(journey_nodes)
        entry["_journey"] = journey
        entry["_traversal"] = traversal
        entries.append(entry)

    ranked = jcrit.rank(entries)
    top = jcrit.top_n(entries, jcrit.TOP_N_DEFAULT)

    payloads = []
    for entry in top:
        payload = jspec.build_journey_case(
            entry["_journey"], traversal=entry["_traversal"],
            nodes_by_fp=nodes_by_fp, edges=edges, tenant_id="m24-tenant",
            criticality=entry["criticality"], endpoint_inventory=inventory,
        )
        payload["rank"] = entry["rank"]
        payloads.append(payload)

    by_action = emap.inventory_by_action(inventory)

    # ── stage 3 · compilation + lint (the factory) ───────────────────────
    jc = load("factory", "app.services.script_factory.journey_compiler")
    compiled = jc.compile_top_n([p for p in payloads if p.get("compilable")])

    quote_payload = next(p for p in payloads
                         if p["journey_id"] == ev.JOURNEY_QUOTE)
    quote_result = next(r for r in compiled["results"]
                        if r.get("journey_id") == ev.JOURNEY_QUOTE)
    return {
        "origin": origin,
        "inventory": inventory,
        "by_action": by_action,
        "ranked": ranked,
        "top": top,
        "payloads": payloads,
        "compiled": compiled,
        "quote_payload": quote_payload,
        "quote_result": quote_result,
        "quote_spec": compiled["specs"][quote_result["spec_path"]],
        "criticality_classifier": criticality.CLASSIFIER,
    }


def _run(tmp_path, pipeline, label: str) -> pw_runner.RunResult:
    ok, why = pw_runner.available()
    if not ok:
        pytest.skip(f"playwright toolchain unavailable: {why}")
    project = tmp_path / label
    return pw_runner.run_spec(
        project, pipeline["quote_result"]["spec_path"], pipeline["quote_spec"])


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-01 · a journey compiles with no adopting case anywhere in sight
# ══════════════════════════════════════════════════════════════════════════

def test_journey_compiles_without_any_adopting_case(pipeline):
    """The acceptance criterion, literally: a journey with NO whole-artifact
    case still produces a valid Playwright spec.

    Nothing in this pipeline ever calls ``journey_case_linker``, lists an
    artifact's test cases, or consults an ``artifact_id`` — there is no artifact
    in this test at all.  The only inputs are the journey's own graph rows.
    """
    result = pipeline["quote_result"]
    assert result["compiled"] is True, result.get("reason")
    spec = pipeline["quote_spec"]
    assert spec.startswith("// GENERATED by VKPower Script Factory")
    assert "import { test, expect } from '@playwright/test';" in spec
    assert "test('Verify Term Life Quote end to end'" in spec
    # The steps are the journey's own walk: an entry navigation plus one click
    # per edge the traversal crossed.
    assert result["steps"] == 2
    assert pipeline["quote_payload"]["provenance"] == "journey_direct"


def test_an_unwalked_journey_is_refused_with_a_named_reason(pipeline):
    """A journey the crawl never finished must be REFUSED, not invented.

    It still appears in the ranking — it is a real journey and hiding it would
    misstate the application — but it compiles to nothing, with a sentence an
    operator can act on.
    """
    abandoned = next(p for p in pipeline["payloads"]
                     if p["journey_id"] == ev.JOURNEY_ABANDONED)
    assert abandoned["compilable"] is False
    assert "no completed walk" in abandoned["reason"]
    assert ev.JOURNEY_ABANDONED in {e["journey_id"] for e in pipeline["ranked"]}


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-02 · criticality is bound to the journey, and the ranking is total
# ══════════════════════════════════════════════════════════════════════════

def test_every_journey_carries_criticality_evidence(pipeline):
    """A band with no evidence is a number nobody can audit."""
    for entry in pipeline["ranked"]:
        band = entry["criticality"]
        assert band["band"] in ("P0", "P1", "P2", "P3")
        assert band["classifier"] == pipeline["criticality_classifier"]
        assert band["evidence"], f"{entry['journey_id']} banded with no evidence"
        for hit in band["evidence"]:
            assert hit["signal_id"]
            assert hit["rationale"]


def test_the_payment_funnel_outranks_the_quote_funnel(pipeline):
    """The BAND is what orders the list, and it is the registry's band.

    The payment journey fires the generic pack's money signals (a ``/checkout``
    route and a ``Pay Now`` control) and is P0; the quote funnel is banded by
    multi-page submit and is P1.  So payment must come first — not because this
    test says so, but because ``criticality.evaluate`` said so.
    """
    order = [e["journey_id"] for e in pipeline["ranked"]]
    bands = {e["journey_id"]: e["criticality"]["band"] for e in pipeline["ranked"]}
    assert bands[ev.JOURNEY_PAYMENT] == "P0"
    assert order.index(ev.JOURNEY_PAYMENT) < order.index(ev.JOURNEY_QUOTE)


def test_a_journey_no_signal_matches_fails_up(pipeline):
    """The registry's honest default survives the journey adapter.

    An unmatched journey must land on P1 with a named fail-up, never quietly on
    P2 or P3 — a low band is a claim that something does not matter, and no
    evidence was ever gathered to support it.
    """
    browse = next(e for e in pipeline["ranked"]
                  if e["journey_id"] == ev.JOURNEY_BROWSE)
    assert browse["criticality"]["band"] == "P1"
    assert any(h["signal_id"] == "fail_up_default"
               for h in browse["criticality"]["evidence"])


def test_the_ranking_is_deterministic_over_the_same_evidence(pipeline):
    """Same evidence in, same order out — the T-GEN-02 requirement.

    Re-ranked from a SHUFFLED input so the assertion is about the sort key and
    not about the order the rows happened to arrive in.
    """
    jcrit = load("qe_central", "app.services.journey_criticality")
    entries = [dict(e) for e in pipeline["ranked"]]
    first = [e["journey_id"] for e in jcrit.rank(entries)]
    second = [e["journey_id"] for e in jcrit.rank(list(reversed(entries)))]
    third = [e["journey_id"] for e in jcrit.rank(entries)]
    assert first == second == third
    assert [e["rank"] for e in jcrit.rank(entries)] == list(
        range(1, len(entries) + 1))


def test_top_n_ranks_the_whole_set_before_slicing(pipeline):
    """Entry N is rank N of the application, not rank N of a pre-cut list."""
    jcrit = load("qe_central", "app.services.journey_criticality")
    entries = [dict(e) for e in pipeline["ranked"]]
    head = jcrit.top_n(entries, 2)
    assert [e["rank"] for e in head] == [1, 2]
    assert len(head) == 2
    assert jcrit.TOP_N_DEFAULT == 20


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-03 · the network assertion is real, and joined to the right step
# ══════════════════════════════════════════════════════════════════════════

def test_the_endpoint_map_is_joined_to_the_triggering_step(pipeline):
    """The M2.5 stamp is READ, not guessed.

    ``POST /api/quote`` carries ``actions: [{label: "Get Quote"}]`` in the
    inventory the M2.5 aggregator produced, and the journey edge's trigger is
    ``get quote``.  The join is on that label, so the endpoint lands on the step
    that caused it — and the step says the attribution was ``recorded``.
    """
    by_action = pipeline["by_action"]
    assert ev.QUOTE_TRIGGER in by_action
    assert {(e["method"], e["path"]) for e in by_action[ev.QUOTE_TRIGGER]} == {
        ("POST", fixture_app.QUOTE_PATH)}

    steps = pipeline["quote_payload"]["steps"]
    click = steps[1]
    assert click["observed"]["label"] == ev.QUOTE_TRIGGER_RENDERED
    assert click["network_attribution"] == "recorded"
    assert [(e["method"], e["path"]) for e in click["network_expect"]] == [
        ("POST", fixture_app.QUOTE_PATH)]

    # And the entry step, which nothing clicked, is attributed STRUCTURALLY —
    # both rules exercised by one journey, each saying which it used.
    entry = steps[0]
    assert entry["network_attribution"] == "inferred"
    assert [(e["method"], e["path"]) for e in entry["network_expect"]] == [
        ("GET", fixture_app.CONFIG_PATH)]


def test_the_generated_spec_contains_a_real_network_assertion(pipeline):
    """Not a comment, not a log — an armed ``waitForResponse`` that can throw.

    The arming line must sit ABOVE the action and the await BELOW it: a
    subscription created after the click can miss a response that already
    arrived, which is a flake rather than an oracle.
    """
    spec = pipeline["quote_spec"]
    assert "page.waitForResponse(" in spec
    assert "__nxNet(page, 'POST', '/api/quote', 200" in spec
    assert "__nxNet(page, 'GET', '/api/config', 200" in spec

    arm = spec.index("const __net2 = [")
    click = spec.index("__nxClick(", arm)
    await_line = spec.index("await Promise.all(__net2);", click)
    assert arm < click < await_line

    # It asserts the OBSERVED behaviour — method, path and status — rather than
    # that the page did not crash.
    assert "r.request().method().toUpperCase() === method" in spec
    assert "r.status() === status" in spec
    # And it is never softened.  A ``.catch`` on this promise would turn the
    # whole oracle into an observation.
    assert ".catch" not in spec[arm:await_line + 40]


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-04 · a confirmed success criterion can fail the test
# ══════════════════════════════════════════════════════════════════════════

def test_a_confirmed_outcome_compiles_to_a_hard_oracle(pipeline):
    """The confirmed criterion is a THROW, not an annotation."""
    payload = pipeline["quote_payload"]
    assert payload["outcome_oracle"] == "hard"
    assert pipeline["quote_result"]["outcome_oracle"] == "hard"

    spec = pipeline["quote_spec"]
    assert "__nxNum(page.locator('#premium'), 42.5, 0.01)" in spec
    # The soft-miss recorder must not appear anywhere: its presence would mean
    # some criterion was compiled as a non-failing log.
    assert "__nxSoftMiss" not in spec


def test_an_unconfirmed_baseline_does_not_get_hard_authority(pipeline):
    """A captured baseline stays soft, and says why.

    The promotion is evidence-gated in both directions: an unapproved baseline
    must not quietly acquire the power to fail somebody's build.
    """
    jspec = load("qe_central", "app.services.journey_spec")
    journey = dict(next(j for j in ev.journeys(pipeline["origin"])
                        if j["journey_id"] == ev.JOURNEY_QUOTE))
    journey["baseline_status"] = "captured"
    payload = jspec.build_journey_case(
        journey, traversal=ev.traversals()[ev.JOURNEY_QUOTE],
        nodes_by_fp=ev.nodes(pipeline["origin"]), edges=ev.edges(),
        tenant_id="m24-tenant",
        endpoint_inventory=pipeline["inventory"])
    assert payload["outcome_oracle"] == "soft"
    assert "approved" in payload["outcome_oracle_reason"]


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-05 · the lint genuinely executes
# ══════════════════════════════════════════════════════════════════════════

def test_lint_executes_against_every_top_n_spec(pipeline):
    """``lint_status`` is written by the code path that ran the lint.

    An empty finding list and a lint that never ran are otherwise the same
    bytes, which is exactly how four reports claimed an API-policy audit that
    had never executed.
    """
    compiled = pipeline["compiled"]
    assert compiled["lint_status"] == "executed"
    assert compiled["lint_rules_version"]
    assert compiled["compiled"] >= 1
    for result in compiled["results"]:
        if not result.get("compiled"):
            continue
        assert result["lint_status"] == "executed"
        assert result["lint_rules_version"] == compiled["lint_rules_version"]
        assert result["lint_errors"] == 0, result["lint"]


def test_the_lint_is_a_real_analysis_and_not_a_rubber_stamp(pipeline):
    """The same lint, on a deliberately bad spec, must FIND things.

    Without this, "0 errors" is unfalsifiable: a lint that returns an empty list
    for every input would satisfy the test above forever.
    """
    auditor = load("factory", "app.services.test_factory.playwright_auditor")
    bad = (
        "import { test } from '@playwright/test';\n"
        "test('bad', async ({ page }) => {\n"
        "  await page.waitForTimeout(3000);\n"
        "  await page.click('#go');\n"
        "  const h = await page.$('#x');\n"
        "});\n"
    )
    findings = auditor.lint_spec(bad)
    assert [f for f in findings if f["severity"] == "error"], findings
    assert auditor.lint_spec(pipeline["quote_spec"]) == []


def test_the_generated_spec_passes_the_honest_ten_audit(pipeline):
    """A journey spec is held to the same rubric as every other spec."""
    audit = pipeline["quote_result"]["audit"]
    assert audit["overall_score"] == 10, audit
    assert audit["decision"] == "certified"


# ══════════════════════════════════════════════════════════════════════════
#  T-GEN-06 · EXECUTION — baseline green, then two seeded regressions RED
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_baseline_the_generated_spec_executes_and_passes(tmp_path, quote_app,
                                                         pipeline):
    """The generated journey EXECUTES against the healthy application, green.

    This is the negative control.  A suite that reds on a working system tells
    you nothing when it reds on a broken one, so the regressions below are only
    meaningful because this run is green.
    """
    quote_app.seed(fixture_app.BASELINE)
    run = _run(tmp_path, pipeline, "baseline")
    assert run.passed, run.stdout + run.stderr
    assert run.statuses() == ["passed"]

    # The generated STEPS ran — a compiled file that was never entered would
    # report none of these.
    titles = " | ".join(run.step_titles())
    assert "step 1: Open Quote Start" in titles
    assert "step 2: Click Get Quote" in titles

    # And the application confirms, independently of the spec, that the journey
    # really reached the backend.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) >= 1
    assert quote_app.calls_to("GET", fixture_app.CONFIG_PATH) >= 1


@pytest.mark.slow
def test_a_silent_api_regression_turns_the_generated_test_red(tmp_path,
                                                              quote_app,
                                                              pipeline):
    """THE MILESTONE'S CENTRAL CLAIM.

    The application is broken in the way a UI-only suite cannot see: the click
    stops calling ``POST /api/quote`` and renders the same premium from a
    constant.  The button works, the navigation happens, the result page shows
    $42.50 — every UI assertion in the spec still passes.

    The network assertion must fail, and it must be the thing that fails.
    """
    quote_app.seed(fixture_app.NETWORK_SILENT)
    run = _run(tmp_path, pipeline, "network-silent")

    assert run.failed, (
        "a silent API regression passed the generated test — the network "
        "assertion is decorative:\n" + run.stdout)
    failure = run.failure_text()
    # Named, so the failure is diagnosable rather than merely red.
    assert "waitForResponse" in failure, failure[:2000]

    # The application's own record confirms the regression was real: the commit
    # never reached the backend on this run.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) == 0
    # ...while the page WAS served, i.e. the UI genuinely still worked.
    assert quote_app.calls_to("GET", "/result.html") >= 1


@pytest.mark.slow
def test_an_outcome_regression_turns_the_generated_test_red(tmp_path,
                                                            quote_app,
                                                            pipeline):
    """The confirmed success criterion FAILS the test rather than logging.

    The endpoint is called and answers 200; navigation is correct; the page
    renders.  Only the premium is wrong.  If the outcome oracle were the
    informational annotation this milestone was written to remove, this run
    would be green.
    """
    quote_app.seed(fixture_app.OUTCOME_DRIFT)
    run = _run(tmp_path, pipeline, "outcome-drift")

    assert run.failed, (
        "a wrong business outcome passed the generated test — the confirmed "
        "criterion is an informational log:\n" + run.stdout)
    failure = run.failure_text()
    assert "value oracle" in failure, failure[:2000]
    assert fixture_app.DRIFTED_PREMIUM.split(".")[0] in failure, failure[:2000]

    # The backend WAS reached on this run — so the network oracle is not what
    # failed, and the two regressions are genuinely independent.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) >= 1


@pytest.mark.slow
def test_the_application_recovers_and_the_spec_goes_green_again(tmp_path,
                                                                quote_app,
                                                                pipeline):
    """Red for a reason, not red forever.

    An oracle that fails on everything is as useless as one that fails on
    nothing.  Removing the regression must restore green with no change to the
    generated spec.
    """
    quote_app.seed(fixture_app.BASELINE)
    run = _run(tmp_path, pipeline, "recovered")
    assert run.passed, run.stdout + run.stderr


# ══════════════════════════════════════════════════════════════════════════
#  The oracles are LOAD-BEARING — the mutation proof
# ══════════════════════════════════════════════════════════════════════════
# A red run only proves the suite noticed something.  It does not prove WHICH
# assertion noticed, and "some assertion failed" is not the claim this milestone
# makes.  These two tests remove ONE oracle each, re-compile, and run against the
# regression that oracle exists to catch — and require the mutated spec to PASS.
#
# A pass there is the load-bearing evidence: it shows the rest of the spec is
# genuinely blind to that regression, so the red in the tests above can only have
# come from the oracle that was removed.  Without these, both regression tests
# could be passing for an unrelated reason and nobody would know.


@pytest.mark.slow
def test_without_the_network_oracle_the_silent_regression_goes_unnoticed(
        tmp_path, quote_app, pipeline):
    """Strip the network assertions; the silent API regression becomes invisible.

    Everything else in the generated spec is untouched — the click, the
    navigation oracle, the outcome region, the value assertion.  All of it
    passes, because the page renders identically.  That is the whole reason
    T-GEN-03 exists, and this is the measurement of it.
    """
    jc = load("factory", "app.services.script_factory.journey_compiler")
    payload = json.loads(json.dumps(pipeline["quote_payload"]))
    for step in payload["steps"]:
        step["network_expect"] = []
    mutated = jc.compile_journey(payload)
    assert mutated["network_assertions"] == 0
    assert "__nxNet(" not in mutated["spec"]

    quote_app.seed(fixture_app.NETWORK_SILENT)
    ok, why = pw_runner.available()
    if not ok:
        pytest.skip(f"playwright toolchain unavailable: {why}")
    run = pw_runner.run_spec(tmp_path / "no-network-oracle",
                             mutated["spec_path"], mutated["spec"])

    assert run.passed, (
        "the UI-only spec failed for some OTHER reason, so the network "
        "regression test above cannot be attributed to the network oracle:\n"
        + run.failure_text()[:2000])
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) == 0


@pytest.mark.slow
def test_without_the_confirmed_outcome_oracle_the_drift_goes_unnoticed(
        tmp_path, quote_app, pipeline):
    """An UNAPPROVED baseline arms no value assertion, and the drift passes.

    Compiled from the same journey with ``baseline_status='captured'``: the
    criterion is grounded but not confirmed, so it is withheld and named rather
    than emitted.  The wrong premium then sails through — which is precisely
    what T-GEN-04 makes impossible once a human approves the baseline.
    """
    jspec = load("qe_central", "app.services.journey_spec")
    journey = dict(next(j for j in ev.journeys(pipeline["origin"])
                        if j["journey_id"] == ev.JOURNEY_QUOTE))
    journey["baseline_status"] = "captured"
    payload = jspec.build_journey_case(
        journey, traversal=ev.traversals()[ev.JOURNEY_QUOTE],
        nodes_by_fp=ev.nodes(pipeline["origin"]), edges=ev.edges(),
        tenant_id="m24-tenant", endpoint_inventory=pipeline["inventory"])
    assert payload["outcome_oracle"] == "soft"
    assert payload["value_assertions"] == []
    assert payload["unconfirmed_outcomes"] == ["Monthly Premium"]

    jc = load("factory", "app.services.script_factory.journey_compiler")
    mutated = jc.compile_journey(payload)
    assert "__nxNum(" not in mutated["spec"]

    quote_app.seed(fixture_app.OUTCOME_DRIFT)
    ok, why = pw_runner.available()
    if not ok:
        pytest.skip(f"playwright toolchain unavailable: {why}")
    run = pw_runner.run_spec(tmp_path / "no-outcome-oracle",
                             mutated["spec_path"], mutated["spec"])

    assert run.passed, (
        "the spec without a confirmed value oracle failed for some OTHER "
        "reason, so the outcome regression test above cannot be attributed to "
        "the outcome oracle:\n" + run.failure_text()[:2000])
    # The endpoint WAS reached, so nothing network-shaped is doing the work.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) >= 1


# ══════════════════════════════════════════════════════════════════════════
#  Evidence artifact
# ══════════════════════════════════════════════════════════════════════════

def test_write_evidence_bundle(tmp_path_factory, pipeline):
    """Emit the required evidence as one reviewable JSON artifact.

    Written to a path the run reports so the Top-20, the criticality evidence,
    the payload, the lint result and the generated spec can be read after the
    fact rather than reconstructed from assertions.
    """
    out_dir = tmp_path_factory.mktemp("m24-evidence")
    bundle = {
        "top_journeys": [
            {"rank": e["rank"], "journey_id": e["journey_id"],
             "business_name": e["business_name"],
             "band": e["criticality"]["band"],
             "criticality_evidence": e["criticality"]["evidence"],
             "boundary_nodes": e["boundary_nodes"],
             "endpoints_observed": e["endpoints_observed"]}
            for e in pipeline["top"]
        ],
        "endpoint_inventory": pipeline["inventory"]["endpoints"],
        "compile_payload": {k: v for k, v in pipeline["quote_payload"].items()
                            if k != "criticality"},
        "compile_result": pipeline["quote_result"],
        "lint": {
            "status": pipeline["compiled"]["lint_status"],
            "rules_version": pipeline["compiled"]["lint_rules_version"],
            "errors_total": pipeline["compiled"]["lint_errors_total"],
        },
    }
    path = out_dir / "m24_evidence.json"
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True, default=str),
                    encoding="utf-8")
    spec_path = out_dir / "generated.spec.ts"
    spec_path.write_text(pipeline["quote_spec"], encoding="utf-8", newline="\n")
    print(f"\nM2.4 evidence bundle: {path}\nM2.4 generated spec:   {spec_path}")
    assert path.is_file() and spec_path.is_file()
