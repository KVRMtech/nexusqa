"""THE GOLDEN GATE: a real crawl must still cross a real irreversible boundary.

This is the regression that protects Phase 1's central claim. Everything in the
product -- the catalogue, the journey graph, the evidence report -- rests on the
crawl being able to walk a funnel to its end and cross the one control that
commits it. That capability was broken for the whole of Phase 1 by two defects
nothing in the suite could see, because every existing test stopped short of the
boundary:

  * a control's PRE-FILL validation message was read as a verdict on the value
    written after it, so every required field was rejected the moment it was
    correctly filled and the crawl stopped at the login page;
  * a named per-control grant for a refuse-pack irreversible verb was discarded
    by a label filter before the authorisation ladder ever saw it.

Both were found by running a crawl and looking, not by a unit test. So this gate
runs a crawl and looks.

WHAT MAKES THIS A GATE RATHER THAN A DEMO
=========================================
Nothing is mocked but the tier-3 oracle. The crawl runs through the PRODUCTION
:class:`app.crawler.Crawler` and the PRODUCTION
:class:`app.main.PlaywrightBrowserPort`, in real Chromium, against an
application served from the proving-ground tree. The assertions read the
crawl's own coverage account -- the same object the manifest is built from --
so a crossing that did not happen cannot be reported as one.

The authorisation is real too, and deliberately the NARROWEST form the system
offers: one `ApprovalGrant` naming exactly one control, plus a platform
provisioning proof verified through :mod:`app.attest`. If the gate could be
made to pass by widening authorisation it would be measuring the wrong thing.

WHY THE ORACLE IS A STUB, AND WHY THAT IS HONEST
================================================
Tier 3 is normally an LLM reached through qe-central. A gate that needed a live
model would be a gate that fails on network weather, so it is replaced by a
deterministic function over the candidate set the walker has ALREADY filtered.
It is not tuned to this application: it prefers a button over a link among
forward-shaped labels, which is the same reasoning a model applies and the same
rule ``measure_boundary_crossing.py`` uses.

That substitution is why this gate says "the crossing machinery works when tier
3 answers". It does not, and does not claim to, prove the live oracle picks
correctly -- that needs qe-central and is a different lane.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

PROVING_GROUNDS = H.SERVICE_ROOT.parent.parent / "proving-grounds"
CROSS_OUT = H.HERE / "_crawl_out" / "boundary_gate"

#: The application this gate is measured on, the control it must cross, and the
#: credentials it needs to get there. `acme-life` is a static single-page
#: application, so the gate runs on a developer machine and in CI identically --
#: a gate that only ever ran in one of those is a gate nobody trusts.
TARGET_APP = "acme-life"
BOUNDARY_CONTROL = "Bind policy"
CREDENTIALS = {"username": "qec.gate@example.test", "password": "Gate!Passw0rd"}

#: Forward-shaped label fragments the stub oracle recognises. Generic funnel
#: vocabulary, not this application's wording.
_FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start", "see")


async def _stub_advance_oracle(candidates: Sequence[Mapping[str, Any]],
                               page_title: str, page_url: str) -> dict[str, Any]:
    """Pick the control that advances this step -- deterministically.

    Prefers a BUTTON over a LINK among the forward-shaped candidates: site
    navigation renders as links, while the control that advances the step you
    are standing on is that step's own submit. The walker has already removed
    everything unsafe from this set (danger, disabled, nameless, commit-worded),
    so this only chooses among controls the crawl was already willing to click.
    """
    names = [str(c.get("name") or "") for c in candidates]
    for want_button in (True, False):
        for index, (name, control) in enumerate(zip(names, candidates)):
            if (str(control.get("kind") or "") == "button") is not want_button:
                continue
            if any(word in name.lower() for word in _FORWARD):
                return {"status": "picked", "index": index, "signature": "gate-forward"}
    return {"status": "none", "signature": "gate-none"}


def _walk_authorization(crawl_id: str, tenant_id: str, target_url: str):
    """A verified M1.3 provisioning proof, built the way the dispatch builds it.

    The proof is minted by the test issuer and then VERIFIED through the
    production :func:`app.attest.verify_provisioning_proof`, so the object the
    crawl holds is one the real verifier accepted -- not a hand-made stand-in.
    """
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    from app.walk_persist import MutationAuditLog, WalkAuthorization
    from _attest_kit import Issuer

    issuer = Issuer()
    scheme, _, rest = target_url.partition("//")
    origin = f"{scheme}//{rest.split('/')[0]}"
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=crawl_id, tenant_id=tenant_id,
                               target_origin=origin,
                               max_walk_mutations_per_step=8),
         "revocations": issuer.revocations()},
        trust=issuer.trust(), crawl_id=crawl_id, tenant_id=tenant_id,
        target_url=target_url, replay_guard=ProofReplayGuard())
    assert verdict.authorized, (
        f"the gate could not build a walk authorization: {verdict.reason}. "
        f"Without it the funnel's persistence steps cannot be actuated and the "
        f"crawl cannot reach the boundary at all.")
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=crawl_id, audit=MutationAuditLog())


@pytest.fixture(scope="module")
def ground_server() -> Any:
    if not PROVING_GROUNDS.is_dir():
        pytest.skip(f"proving-grounds not found at {PROVING_GROUNDS}")
    server = H.FixtureServer(root=PROVING_GROUNDS).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def crossing_coverage(pw, ground_server) -> dict[str, Any]:
    """Run ONE real crawl and hand back the coverage account it produced."""
    from app.auth import AuthWindow, Credentials
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from tests.characterization.harness import disposable_attestation

    # DELIBERATELY NOT `QEC_PROVING_GROUND_URL`. That variable names whichever
    # application the proving-ground matrix leg is currently serving, and this
    # gate is a statement about ONE application whose funnel is known to be
    # crossable. Pointed at a different one it would go red for reasons that say
    # nothing about the capability under test. `acme-life` is static, so serving
    # it here makes the gate run identically on a laptop and in CI.
    url = ground_server.url(TARGET_APP)
    crawl_id, tenant_id = "gate-boundary-crossing", "boundary-gate"

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        walk_authorization=_walk_authorization(crawl_id, tenant_id, url),
        idp_domains=frozenset(),
    )

    work_dir = CROSS_OUT
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id=tenant_id, target_url=url,
        work_dir=str(work_dir), refuse_pack=pack,
        budget=Budget.from_dict({"max_states": 40, "max_actions": 250,
                                 "max_requests": 4000, "max_duration_ms": 420_000}),
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version, config_fingerprint="boundary-gate",
        guard_context=guard_ctx, identity_seed="qec-boundary-gate",
        observe_only=False, traversal=TRAVERSAL_FULL,
        advance_oracle=_stub_advance_oracle,
        # THE NARROWEST AUTHORISATION THE SYSTEM OFFERS: one named control, once.
        # No "*", no submit_approvals label list.
        boundary_approvals=[{"control": BOUNDARY_CONTROL, "approved_by": "golden-gate",
                             "max_crossings": 1}],
        credentials=Credentials.from_payload(CREDENTIALS),
    )
    pw.run(crawler.run())
    return crawler._coverage.build()


def _crossings(coverage: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(coverage.get("boundary_crossings") or [])


def _diagnose(coverage: Mapping[str, Any]) -> str:
    """Everything a reader needs to see WHY the gate went red, in one block."""
    approvable = sorted({str(r.get("label") or "")
                         for r in (coverage.get("approvable_boundary") or [])})
    return (
        f"\n  boundaries_crossed : {coverage.get('boundaries_crossed')}"
        f"\n  crossings          : {json.dumps(_crossings(coverage), default=str)[:600]}"
        f"\n  milestones         : {json.dumps(coverage.get('outcome_milestones'), default=str)[:600]}"
        f"\n  boundaries SEEN    : {approvable}"
        f"\n\nReproduce with:  python measure_boundary_crossing.py {TARGET_APP}")


def test_the_crawl_crosses_an_approved_irreversible_boundary(crossing_coverage) -> None:
    """THE GATE. A build that cannot cross stops CI here.

    Asserted on the crawl's own account rather than on a log line, so the only
    way to make this pass is to actually cross.
    """
    crossed = [c for c in _crossings(crossing_coverage)
               if str(c.get("status") or "") == "crossed"]
    assert crossed, (
        f"THE CRAWL CROSSED NO BOUNDARY. It was given credentials, a verified "
        f"platform attestation and an explicit grant naming {BOUNDARY_CONTROL!r}, "
        f"and still never committed the funnel. Phase 1's central capability is "
        f"broken." + _diagnose(crossing_coverage))


def test_the_crossing_was_the_control_the_operator_named(crossing_coverage) -> None:
    """A crossing of something else is not this grant being honoured."""
    names = {str(c.get("control_name") or "").strip().lower()
             for c in _crossings(crossing_coverage)}
    assert BOUNDARY_CONTROL.lower() in names, (
        f"the crawl crossed {sorted(names)} but not {BOUNDARY_CONTROL!r}, which is "
        f"the only control it was authorised for." + _diagnose(crossing_coverage))


def test_the_crossing_carries_the_operators_approval_id(crossing_coverage) -> None:
    """Every crossing is attributable to the grant that authorised it.

    An irreversible action recorded without the approval behind it is exactly
    the audit hole the boundary ledger exists to close.
    """
    for crossing in _crossings(crossing_coverage):
        assert str(crossing.get("approval_id") or "").startswith("apr_"), (
            f"a crossing was recorded with no approval id: {crossing}"
            + _diagnose(crossing_coverage))


def test_the_journey_reached_a_verified_confirmation(crossing_coverage) -> None:
    """Crossing is not the claim -- LANDING on a confirmation is.

    A click that fires and lands nowhere provable is recorded honestly as
    ``verified=False``, and a gate that accepted it would be back to asserting
    that a button was pressed.
    """
    milestones = list(crossing_coverage.get("outcome_milestones") or [])
    assert milestones, (
        "the crawl crossed a boundary but recorded no outcome milestone, so "
        "nothing is known about what the crossing produced."
        + _diagnose(crossing_coverage))
    verified = [m for m in milestones
                if str(m.get("outcome") or "") == "confirmation"
                and str(m.get("confirmation_rung") or "")]
    assert verified, (
        "no crossing landed on a recognized confirmation. The funnel was "
        "committed but the application never declared it complete, so the "
        "journey is unproven." + _diagnose(crossing_coverage))


def test_a_crossing_is_never_recorded_without_its_evidence(crossing_coverage) -> None:
    """The milestone must carry what a reader needs to re-check it."""
    for milestone in (crossing_coverage.get("outcome_milestones") or []):
        for field in ("crossing_id", "control_name", "state_fingerprint_before",
                      "state_fingerprint_after", "attestation_env_kind"):
            assert str(milestone.get(field) or ""), (
                f"milestone is missing {field!r}, so the crossing cannot be "
                f"audited: {milestone}")
