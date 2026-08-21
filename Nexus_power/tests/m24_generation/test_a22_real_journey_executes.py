"""GATE 3 / A22 — the DISCOVERED journey EXECUTES, and a regression turns it red.

    real crawl -> coverage -> fold -> compile payload -> SPEC -> Chromium -> verdict

THE ONE THING M2.4 COULD NOT CLAIM. Its proof does everything below and does it
well — compiles, executes in real Chromium, goes red under two orthogonal seeded
regressions. What it reads is ``crawl_evidence.py``, which says in its own first
paragraph that its graph rows are FIXTURE: "what a crawl of the quote application
WOULD have recorded". So the pipeline has always been fed a hand-built account of
a crawl that never happened.

This file runs the identical harness — the same ``fixture_app``, the same
``pw_runner``, the same compiler — against the payload a REAL crawl produced. Side
by side with ``test_m24_generation_proof.py``, the only difference is where the
evidence came from, which is precisely the difference A22 exists to close.

WHY THE PAYLOAD ARRIVES AS A FILE RATHER THAN A FUNCTION CALL. Three services and
no single process can hold them: the crawl needs Chromium and the explorer's
``app`` package, the fold needs Postgres and qe-central's ``app`` package, the
compiler is ``platform/api``'s. Two of those ship a top-level ``app`` and cannot be
imported into one interpreter (M1.7 froze that boundary as data). So each stage
writes its evidence down and the next stage reads it — the same seam
``coverage.json`` already is. The producers:

    engines/qe-explorer  tests/browser/test_a22_generation_crawl.py   -> coverage.json
    platform/qe-central  tests/contract/test_a22_generation_from_real_crawl.py
                                                              -> compile_payload.json

THE ORIGIN IS RE-POINTED, AND THAT IS THE PRODUCT, NOT A FUDGE. The recorded
payload carries the origin the crawl ran against — an ephemeral loopback port that
died with it. Re-pointing a discovered journey at the environment under test is
crawl-once/run-many, the same operation an Environment Profile performs for every
customer run. It is done here by explicit substitution of that one origin, and the
test asserts the recorded origin is GONE from the compiled spec afterwards, so a
missed rewrite fails loudly instead of quietly executing against nothing.

WHAT IS AND IS NOT ASSERTED. The baseline must be green and the silent-API
regression must be red — A22's central claim, on discovered evidence. The OUTCOME
regression is deliberately NOT asserted red here: this journey's oracle is
``soft``, because T-GEN-04 keeps a crawl-derived outcome non-failing until a human
approves the baseline. Asserting it red would require promoting an oracle no one
confirmed, which is the green-wash the milestone forbids. It is asserted SOFT
instead, and the outcome drift is run to show what a soft oracle does and does not
catch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent                       # …/Nexus_power
if str(_REPO / "tests") not in sys.path:
    sys.path.insert(0, str(_REPO / "tests"))

from m24_generation import fixture_app                       # noqa: E402
from m24_generation import pw_runner                         # noqa: E402
from m24_generation.service_import import load               # noqa: E402

pytestmark = pytest.mark.m24

EVIDENCE = _REPO / "evidence" / "a22_generation"
PAYLOAD = EVIDENCE / "compile_payload.json"
PREMIUM_LABEL = "Your monthly premium"


def _recorded_origin(payload: dict) -> str:
    """The origin the crawl ran against, off the payload itself."""
    base = str(payload.get("base_url") or "").rstrip("/")
    assert base.startswith("http"), (
        f"the recorded payload has no usable base_url: {base!r}")
    return base


def _repoint(payload: dict, origin: str) -> dict:
    """Crawl-once/run-many: the same journey, aimed at a live instance."""
    old = _recorded_origin(payload)
    text = json.dumps(payload)
    assert old in text, "the recorded origin does not appear in its own payload"
    return json.loads(text.replace(old, origin.rstrip("/")))


@pytest.fixture(scope="module")
def payload() -> dict:
    if not PAYLOAD.is_file():
        pytest.skip(
            f"{PAYLOAD.name} has not been recorded. Produce it with:\n"
            f"  QEC_TEST_DATABASE_URL=... pytest "
            f"platform/qe-central/tests/contract/"
            f"test_a22_generation_from_real_crawl.py\n"
            f"There is deliberately no fixture fallback — a hand-built payload is "
            f"the thing A22 exists to stop reading.")
    return json.loads(PAYLOAD.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def quote_app():
    """ONE server for the module, seeded per test — the same choice the M2.4 proof
    makes and for the same reason: the baseline run and the regression runs then
    differ in the seeded defect and in nothing else, not the port and not a
    restart's timing."""
    server = fixture_app.QuoteAppServer().start()
    try:
        yield server
    finally:
        server.stop()


def _compile(payload: dict, origin: str):
    jc = load("factory", "app.services.script_factory.journey_compiler")
    repointed = _repoint(payload, origin)
    result = jc.compile_journey(repointed)
    spec = result.get("spec") or ""
    assert spec, f"the discovered journey did not compile to a spec: {result}"
    assert _recorded_origin(payload) not in spec, (
        "the compiled spec still contains the CRAWL-TIME origin, so it would "
        "execute against a port that no longer exists")
    assert origin.rstrip("/") in spec, (
        "the compiled spec does not contain the live origin — the re-point did "
        "not reach the generated text")
    return result, spec


def _run(tmp_path, label: str, result, spec):
    ok, why = pw_runner.available()
    if not ok:
        pytest.skip(f"playwright toolchain unavailable: {why}")
    return pw_runner.run_spec(tmp_path / label,
                              result.get("spec_path") or "a22-journey.spec.ts",
                              spec)


# ══════════════════════════════════════════════════════════════════════════
#  The evidence is a real crawl's, and it is compilable
# ══════════════════════════════════════════════════════════════════════════

def test_the_payload_came_from_a_discovered_journey(payload) -> None:
    """Runs without node: a fixture payload is a problem either way."""
    assert payload.get("compilable") is not False, (
        f"the recorded payload is not compilable: {payload.get('reason')!r}")
    assert payload.get("provenance") == "journey_direct", (
        f"provenance is {payload.get('provenance')!r} — A22 requires the journey "
        f"to compile on its OWN evidence, not through an adopted artifact case")
    recorded = [e for s in (payload.get("steps") or [])
                for e in (s.get("network_expect") or [])
                if str(e.get("attribution") or "") == "recorded"]
    assert recorded, (
        "the payload carries no RECORDED network assertion, so the regression "
        "below could only be caught by luck")


@pytest.mark.slow
def test_baseline_the_discovered_journey_executes_and_passes(tmp_path, quote_app,
                                                             payload) -> None:
    """The negative control. A suite that reds on a working application tells you
    nothing when it reds on a broken one."""
    quote_app.seed(fixture_app.BASELINE)
    result, spec = _compile(payload, quote_app.origin)
    run = _run(tmp_path, "a22-baseline", result, spec)

    assert run.passed, run.stdout + run.stderr
    assert run.statuses() == ["passed"]

    # The application's OWN record — independent of the spec — confirms the
    # generated journey really drove the funnel through its backend.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) >= 1


@pytest.mark.slow
def test_a_silent_api_regression_turns_the_discovered_journey_red(
        tmp_path, quote_app, payload) -> None:
    """A22'S CENTRAL CLAIM, ON DISCOVERED EVIDENCE.

    The application breaks in the way a UI-only suite cannot see: the click stops
    calling ``POST /api/quote`` and renders the same premium from a constant. The
    button works, the navigation happens, the page shows the same number — every
    UI assertion still passes.

    The network assertion must fail, and it must be the thing that fails. That
    assertion exists only because a real crawl OBSERVED the call.
    """
    quote_app.seed(fixture_app.NETWORK_SILENT)
    result, spec = _compile(payload, quote_app.origin)
    run = _run(tmp_path, "a22-network-silent", result, spec)

    assert run.failed, (
        "a silent API regression passed a specification generated from a real "
        "crawl — the network assertion is decorative:\n" + run.stdout)
    failure = run.failure_text()
    assert "waitForResponse" in failure, failure[:2000]

    # The application confirms the regression was real: the commit never reached
    # the backend, while the page WAS served — i.e. the UI genuinely still worked.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) == 0
    assert quote_app.calls_to("GET", "/result.html") >= 1


@pytest.mark.slow
def test_the_outcome_oracle_is_soft_and_says_so(tmp_path, quote_app,
                                                payload) -> None:
    """AND THE ONE A22 DOES NOT GET TO CLAIM.

    The premium is grounded — the crawl captured its selector — but the oracle is
    SOFT, because T-GEN-04 keeps a crawl-derived outcome non-failing until a human
    approves the baseline. So an OUTCOME drift (the API is called correctly and
    returns a different number) does NOT turn this specification red, and this
    test asserts exactly that rather than hiding it.

    Promoting the oracle here is the single change that would make A22 look
    stronger, and it is the one the milestone forbids: a specification that fails
    on evidence nobody confirmed is asserting a number it was never told is right.
    """
    assert payload.get("outcome_oracle") == "soft", (
        f"outcome_oracle is {payload.get('outcome_oracle')!r}; this test documents "
        f"the SOFT case and must be revisited when a baseline is approved")
    assert PREMIUM_LABEL in (payload.get("unconfirmed_outcomes") or []), (
        "the premium is not carried as an unconfirmed outcome, so the soft oracle "
        "is not about the value this journey actually produced")

    quote_app.seed(fixture_app.OUTCOME_DRIFT)
    result, spec = _compile(payload, quote_app.origin)
    run = _run(tmp_path, "a22-outcome-drift", result, spec)

    # The network call still happens correctly under OUTCOME_DRIFT, so nothing
    # HARD is violated. The run is green, and that is the honest verdict for an
    # unconfirmed outcome — recorded here so the limitation is visible in CI
    # rather than discovered by a customer.
    assert quote_app.calls_to("POST", fixture_app.QUOTE_PATH) >= 1
    assert run.passed, (
        "the outcome drift turned the spec red. That is BETTER than the documented "
        "behaviour, but it means the oracle is no longer soft and this test's "
        "premise has changed — re-derive it:\n" + run.stdout)
