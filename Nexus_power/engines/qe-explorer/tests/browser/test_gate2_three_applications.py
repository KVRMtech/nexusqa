"""GATE 2 - the three real applications, and what each one is PROVEN to do.

This is the blocking regression for A17. It runs the SAME instrument A14/A15/A16
are evidenced with (:mod:`gate2_journey`) against all three proving grounds and
asserts each one against a DECLARED expectation committed next to it.

WHY DECLARED EXPECTATIONS AND NOT "ALL THREE MUST CROSS"
========================================================
Two of the three do not complete a journey today, and a gate that asserted they
did would be red on the day it landed and would stay red -- which in practice
means disabled, which means no protection at all. A gate that instead SKIPPED
them would be worse: a lane that quietly does not run reads as a lane that
passed.

So each application declares what it currently does, and the gate asserts that
EXACTLY. It is a ratchet in both directions:

  * an application that stops crossing goes RED  -- the regression A17 exists to
    catch;
  * an application that STARTS crossing also goes RED, because its declaration
    is now understated. Someone must look at the new evidence and raise the
    declaration deliberately.

The second direction is the one that keeps this honest. A gate that only checks
a floor can be satisfied by a crawl that got luckier, and nobody ever finds out
the platform improved -- or that the declaration was wrong to begin with.

WHAT IS NOT ASSERTED HERE
=========================
Nothing about the live tier-3 oracle (A18): this lane runs the deterministic
stand-in so it is reproducible offline, and says so in every bundle it writes.
A18 needs qe-central and a model, and is a different lane.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

EXPLORER = H.SERVICE_ROOT
PROVING_GROUNDS = EXPLORER.parent.parent / "proving-grounds"

sys.path.insert(0, str(EXPLORER))

#: What each application is PROVEN to do today, measured on 2026-08-20 against
#: the image built from its own Dockerfile. Every field is a measurement, and
#: the note says what stands between the current result and a completed journey.
#:
#:   crossings        - boundary crossings with status="crossed"
#:   confirmation     - a milestone with outcome="confirmation" AND a rung
#:   journeys         - flow_summary.journeys_completed
DECLARED: dict[str, dict[str, Any]] = {
    "acme-life": {
        "population": {"flows": 3, "walked_depth": 3, "proven_depth": 3},
        "crossings": 2,
        "confirmation": True,
        "journeys": 1,
        "note": "Completes. Two crossings of 'Bind policy' at two URLs "
                "(#/review and #/quote) = two boundary_keys, each crossed "
                "once; the #/review one lands on a dialog confirmation.",
    },
    "vkpower-life": {
        "population": {"flows": 3, "walked_depth": 10, "proven_depth": 0},
        "crossings": 0,
        "confirmation": False,
        "journeys": 0,
        "note": "Walks the quote funnel and four apply steps, then stops at "
                "'Continue to Underwriting Decision' -- a router.push flagged "
                "DANGER because rp.verb.underwrite matches its button NAME. "
                "Granting it walks four steps further but asserts a navigation "
                "control commits, which makes OutcomeMilestone.verified accept "
                "a bare navigation and report a completed journey at step 6 of "
                "10 with an empty confirmation_detail. Measured, then reverted "
                "-- see gate2_journey.grants_for. The funnel needs a reviewed "
                "refuse-pack allow_overrides row, not a grant.",
    },
    "summit-life-carrier": {
        "population": {"flows": 5, "walked_depth": 1, "proven_depth": 1},
        "crossings": 0,
        "confirmation": False,
        "journeys": 0,
        "note": "Logs in (three login defects fixed -- see the A16 section of "
                "GATE_2_THREE_APPLICATIONS.md) and reaches the carrier "
                "platform, then walks five one-step flows and stops at their "
                "submit boundaries. It never reaches "
                "/underwriting/new-business/new-application, so the control it "
                "is authorised for is never offered and nothing is crossed. "
                "The grant is URL-scoped BECAUSE the dashboard nav carries a "
                "link with the same label, which an unscoped grant was spent "
                "on at /dashboard/overview with outcome=error.",
    },
}

#: Container port per image, read from each Dockerfile. Not guessed from the
#: framework -- browser-harness.yml guessed 3000 for the Next.js app whose
#: Dockerfile sets PORT=3002, and that leg never served.
PORTS = {"acme-life": 80, "vkpower-life": 80, "summit-life-carrier": 3002}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


class _Container:
    """One proving ground, built from its own Dockerfile and served."""

    def __init__(self, app: str) -> None:
        self.app = app
        self.name = "gate2-%s-%d" % (app, os.getpid())
        self.port = _free_port()

    def start(self) -> str:
        image = "gate2-%s:test" % self.app
        subprocess.run(
            ["docker", "build", "-t", image, str(PROVING_GROUNDS / self.app)],
            check=True, capture_output=True, timeout=1800)
        subprocess.run(["docker", "rm", "-f", self.name],
                       capture_output=True, timeout=60)
        subprocess.run(
            ["docker", "run", "-d", "--name", self.name,
             "-p", "%d:%d" % (self.port, PORTS[self.app]), image],
            check=True, capture_output=True, timeout=120)
        url = "http://127.0.0.1:%d/" % self.port
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                import urllib.request
                urllib.request.urlopen(url, timeout=3).read(1)
                return url
            except Exception:
                time.sleep(1)
        logs = subprocess.run(["docker", "logs", self.name],
                              capture_output=True, timeout=60)
        raise AssertionError(
            "%s never served on :%d (container port %d). This is the failure "
            "browser-harness.yml produced for summit-life-carrier for as long "
            "as it published 3000 against an image listening on 3002.\n%s"
            % (self.app, self.port, PORTS[self.app],
               logs.stdout.decode("utf-8", "replace")[-2000:]))

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name],
                       capture_output=True, timeout=60)


@pytest.fixture(scope="module", params=sorted(DECLARED))
def journey(request) -> dict[str, Any]:
    """Build, serve and CRAWL one application; hand back its verdict."""
    app = request.param
    if not PROVING_GROUNDS.is_dir():
        pytest.skip("proving-grounds not found at %s" % PROVING_GROUNDS)
    if not _docker_available():
        pytest.skip(
            "docker is not available. This lane deliberately does NOT fall "
            "back to serving the source directory: for vkpower-life those are "
            "two different applications, and a journey proven against the "
            "wrong one would not reproduce in CI.")

    import asyncio

    import gate2_journey as G

    container = _Container(app)
    try:
        url = container.start()
        out = H.HERE / "_crawl_out" / "gate2" / app
        out.mkdir(parents=True, exist_ok=True)
        # gate2_journey.run owns its own Playwright lifecycle (it is the same
        # entry point the A14/A15/A16 evidence was produced with), so this lane
        # deliberately does NOT take the shared `pw` fixture -- two Playwright
        # instances in one process is how the characterization lane once ended
        # up sharing a page between tests.
        result = asyncio.run(
            G.run(app, url, oracle_kind="stub", out_root=out,
                  max_states=60, max_duration_ms=900_000))
        verdict = G.verdict_of(app, url, result)
        (out / "journey.json").write_text(
            json.dumps(verdict, indent=2, default=str), encoding="utf-8")
        return verdict
    finally:
        container.stop()


def _diagnose(verdict: dict[str, Any]) -> str:
    declared = DECLARED[verdict["app"]]
    return (
        "\n  app                 : %s" % verdict["app"]
        + "\n  declared            : %s" % json.dumps(
            {k: declared[k] for k in ("crossings", "confirmation", "journeys")})
        + "\n  measured crossings  : %s" % [c.get("control_name")
                                            for c in verdict["crossings"]]
        + "\n  confirmation        : %s %s" % (verdict["confirmation_observed"],
                                               verdict["confirmation_rungs"])
        + "\n  telemetry           : %s" % json.dumps(verdict["telemetry"])
        + "\n  boundaries offered  : %s" % verdict["approvable_boundaries_seen"]
        + "\n  stop_reason         : %s" % verdict["stop_reason"]
        + "\n  declared note       : %s" % declared["note"])


def test_the_application_crosses_exactly_what_it_is_declared_to_cross(journey):
    """A change in either direction stops CI here and names both numbers."""
    declared = DECLARED[journey["app"]]
    assert journey["boundaries_crossed"] == declared["crossings"], (
        "%s crossed %d boundaries; its declaration says %d. If the platform "
        "improved, raise the declaration and commit the new evidence. If it "
        "regressed, this is the Phase-1 capability breaking."
        % (journey["app"], journey["boundaries_crossed"], declared["crossings"])
        + _diagnose(journey))


def test_every_crossing_carries_the_grant_that_authorised_it(journey):
    """An irreversible action with no approval behind it is the audit hole the
    crossing ledger exists to close -- asserted on EVERY application, including
    the ones that cross nothing (where it is vacuously true and costs nothing).
    """
    for crossing in journey["crossings"]:
        assert str(crossing.get("approval_id") or "").startswith("apr_"), (
            "a crossing was recorded with no approval id: %s" % crossing
            + _diagnose(journey))


def test_no_boundary_is_crossed_more_than_its_grant_allows(journey):
    """Exactly-once, per boundary. Every grant this lane issues names ONE
    control and allows ONE crossing, so a boundary_key appearing twice is a
    duplicate irreversible action -- the replay failure A14 asks about."""
    keys = [c.get("boundary_key") for c in journey["crossings"]]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, (
        "these boundaries were crossed more than once: %s. Each grant allows "
        "max_crossings=1, so this is a duplicate submission."
        % sorted(duplicates) + _diagnose(journey))


def test_the_confirmation_claim_matches_what_was_declared(journey):
    """Landing on a confirmation is the journey claim; crossing is not."""
    declared = DECLARED[journey["app"]]
    assert journey["confirmation_observed"] == declared["confirmation"], (
        "%s confirmation_observed=%s; its declaration says %s."
        % (journey["app"], journey["confirmation_observed"],
           declared["confirmation"]) + _diagnose(journey))


def test_depth_telemetry_is_reported_and_not_null(journey):
    """A19 -- every depth field must carry a value, on every application.

    Reported as a floor of ZERO, not as absent: a crawl that walked nothing has
    a depth of 0, and that is a measurement. ``None`` is not.
    """
    telemetry = journey["telemetry"]
    for field in ("flows_found", "flows_completed", "journeys_completed",
                  "deepest_flow_steps", "deepest_flow_proven_steps"):
        assert telemetry.get(field) is not None, (
            "%s is null for %s. Depth telemetry that reports nothing cannot "
            "distinguish a shallow application from a truncated traversal."
            % (field, journey["app"]) + _diagnose(journey))
    assert isinstance(telemetry.get("deepest_flow_capped"), bool), (
        "deepest_flow_capped must be a bool -- it is the field that says "
        "whether deepest_flow_steps is a measurement or a floor."
        + _diagnose(journey))


def test_a_completed_journey_is_never_claimed_without_a_confirmation(journey):
    """The anti-green-wash conjunct, asserted across all three applications.

    ``journeys_completed`` counts crossings whose FAR SIDE was observed to be a
    confirmation. If it is non-zero while no confirmation was recognised, the
    two halves of the account disagree and only one can be right.
    """
    completed = int(journey["telemetry"].get("journeys_completed") or 0)
    if completed and not journey["confirmation_observed"]:
        milestones = [m for m in journey["outcome_milestones"]
                      if str(m.get("outcome") or "") == "confirmation"]
        assert milestones, (
            "%s reports journeys_completed=%d but recognised no confirmation "
            "at all. A journey is completed when the application DECLARES it "
            "complete, not when a button was pressed."
            % (journey["app"], completed) + _diagnose(journey))


# -- the question every declaration above has to survive ---------------------


def test_the_claim_rests_on_a_population_big_enough_to_support_it(journey):
    """WOULD THIS STILL PASS IF THE CRAWL HAD OBSERVED NOTHING?

    For two of these three applications, every other assertion in this file
    answered YES until this one existed. summit-life-carrier and vkpower-life
    both declare crossings=0, confirmation=False, journeys=0 -- so a crawl that
    reached the application and captured NOT ONE PAGE satisfied all of them,
    and the lane would have reported six green tests over an empty account.

    The near-miss that forced this: an unscoped grant was spent on a navigation
    LINK that merely SHARED the commit button's label. That is not "the subject
    was absent" -- it is "the wrong subject matched", and it passes a check that
    only asks whether something was found. So the companion question has to be
    asked too, and NON-EMPTY is not enough to answer it: a two-state recording
    is non-empty and still proves nothing about a fifteen-route funnel.

    Hence a floor per application, at the scale ITS claim needs -- read from
    what that application actually is, not from what a crawl happened to do.
    """
    floor = DECLARED[journey["app"]]["population"]
    telemetry = journey["telemetry"]

    assert int(telemetry.get("flows_found") or 0) >= floor["flows"], (
        "%s walked %s flows; its claim needs at least %d. Every other assertion "
        "in this file is satisfied by an empty account, so this is the one that "
        "notices a crawl that reached the application and captured nothing."
        % (journey["app"], telemetry.get("flows_found"), floor["flows"])
        + _diagnose(journey))

    assert int(telemetry.get("deepest_flow_steps") or 0) >= floor["walked_depth"], (
        "%s walked to a depth of %s; its claim needs at least %d. This is the "
        "floor that bites for an application which proves nothing: vkpower-life "
        "walks ten steps and proves none of them, and a crawl that stalled at "
        "step one would satisfy every other assertion here."
        % (journey["app"], telemetry.get("deepest_flow_steps"),
           floor["walked_depth"]) + _diagnose(journey))

    assert int(telemetry.get("deepest_flow_proven_steps") or 0) >= floor["proven_depth"], (
        "%s proved a depth of %s; its claim needs at least %d. PROVEN depth, not "
        "walked depth: vkpower-life reports deepest_flow_steps=10 with proven=0 "
        "and capped=true, which looks like coverage and is a floor."
        % (journey["app"], telemetry.get("deepest_flow_proven_steps"),
           floor["proven_depth"]) + _diagnose(journey))


def test_the_crawl_got_past_the_front_door(journey):
    """A crawl stopped at a login wall has measured NOTHING about the
    application behind it, and must never be read as one that found nothing to
    report. This is the assertion that would have caught A16 on the day it
    appeared: summit-life-carrier crawled for weeks returning states=1 and
    stop_reason=auth_failed while every declaration it had was satisfied."""
    assert str(journey.get("stop_reason") or "") != "auth_failed", (
        "%s never got past its login wall. Nothing below this line describes "
        "the application." % journey["app"] + _diagnose(journey))
