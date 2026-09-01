"""DEPLOY CI GATE — nothing reaches the fleet that CI has not passed (H1).

THE FINDING
===========
This repository has two remotes and, until 2026-08-31, no link between them::

    laptop --push--> mine (nexus-power-snapshot) --pull--> VM   # no Actions
    laptop --push--> origin (KVRMtech/nexusqa)                  # CI runs here

``deploy.ps1`` pushed to ``mine``; the VM pulled from ``mine``; nothing in the
deploy path ever asked ``origin`` whether the commit had built. ``gh run list``
appeared nowhere in the repository. Measured on trunk that day: **826 commits,
21 with a successful ``Nexus QA CI`` run** — so the overwhelmingly likely state
of any given deployed commit was "never compiled by anyone but its author".

The golden crawl gate is real, but it fires *after* the swap: it detects a bad
build by serving it. This gate fires before the push, so a commit CI has not
passed never reaches the deploy remote at all.

WHY THESE ARE TEXT ASSERTIONS
=============================
The gate's behaviour is proven by *running* it — three live refusals and a
positive control, transcribed in ``QECentral/docs/GATE_0_DURABILITY.md`` §9.4.
These tests exist for the other half: to make the gate hard to remove by
accident. They assert the properties whose loss would be silent — the ordering
against the push, the absence of a bypass, and the fail-closed branches — none
of which a green deploy would ever reveal as missing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
REPO = ROOT.parent

DEPLOY = REPO / "scripts/deploy.ps1"
GATE = REPO / "scripts/require_green_ci.ps1"

# The qe-central suite is also run from a container that mounts only the service
# directory, where the repository root genuinely is not present. That is a lane
# limitation, not a missing gate, and it is the documented cause of the two
# pre-existing failures in tests/security/test_deploy_environment.py under the
# runner lane. Stating it here keeps the skip legible rather than mysterious.
pytestmark = pytest.mark.skipif(
    not DEPLOY.exists(),
    reason=f"repository root not mounted in this lane: {DEPLOY} absent",
)


@pytest.fixture(scope="module")
def deploy_src() -> str:
    return DEPLOY.read_text(encoding="utf-8-sig")


@pytest.fixture(scope="module")
def gate_src() -> str:
    return GATE.read_text(encoding="utf-8-sig")


# -- the gate exists and the deploy calls it --------------------------------

def test_the_gate_script_is_present():
    assert GATE.exists(), (
        f"{GATE} is missing. deploy.ps1 refuses when it is absent (that is "
        "tested below), so losing this file does not ship a bad build - it "
        "stops every deploy."
    )


def test_the_deploy_invokes_the_gate(deploy_src: str):
    assert "require_green_ci.ps1" in deploy_src, (
        "deploy.ps1 no longer references the CI gate at all."
    )


def test_the_gate_runs_BEFORE_the_push_to_the_deploy_remote(deploy_src: str):
    """Ordering is the whole property.

    A gate after the push has already put an unverified sha on the remote the
    VM pulls from - one `git pull` from the fleet, and reachable by -PushOnly
    with no deploy step at all.
    """
    gate_at = deploy_src.index("require_green_ci.ps1")
    push_at = deploy_src.index("git push $REMOTE $BRANCH")
    assert gate_at < push_at, (
        "The CI gate is invoked AFTER the push to the deploy remote. An "
        "unverified commit is then already on `mine`, where the VM's `git pull` "
        "can reach it."
    )


# -- the fail-closed branches -----------------------------------------------

def test_a_missing_gate_script_refuses_the_deploy(deploy_src: str):
    """An absent check is indistinguishable from a passing one.

    This shipped broken once: the invocation path was corrupted, PowerShell
    raised CommandNotFound, and $LASTEXITCODE stayed 0 - so the deploy died on
    an unhandled exception while reporting SUCCESS to its caller.
    """
    assert "Test-Path $GateScript" in deploy_src, (
        "deploy.ps1 no longer checks that the gate script exists before "
        "calling it."
    )
    guard = deploy_src[deploy_src.index("Test-Path $GateScript"):]
    guard = guard[: guard.index("}")]
    assert "exit 3" in guard, (
        "the missing-gate branch does not exit non-zero, so a vanished gate "
        "would let the deploy continue"
    )


def test_the_gate_is_built_with_join_path_not_a_backslash_string(deploy_src: str):
    """CLAUDE.md section 3, in the deploy path.

    `"$PSScriptRoot\\require_green_ci.ps1"` shipped once as `$PSScriptRoot` +
    CR + `equire_green_ci.ps1`: a lone backslash read as an escape, then a
    text-mode read of this CRLF file promoted the CR into a real line break.
    Join-Path removes the backslash from the source, so the class cannot recur.
    """
    assert 'Join-Path $PSScriptRoot "require_green_ci.ps1"' in deploy_src
    assert '"$PSScriptRoot\\require_green_ci.ps1"' not in deploy_src


def test_the_gate_fails_closed_when_gh_is_unavailable(gate_src: str):
    """Missing tool is not evidence that the build is fine."""
    assert "Get-Command gh" in gate_src
    idx = gate_src.index("Get-Command gh")
    assert "exit 4" in gate_src[idx: idx + 900], (
        "a missing gh must refuse (exit 4), not fall through to a deploy"
    )


# -- the green-wash properties ----------------------------------------------

def test_success_is_the_only_passing_conclusion(gate_src: str):
    """`cancelled` must be red, not absent.

    ci.yml runs under cancel-in-progress, so the next push KILLS the suite. Of
    the last 100 runs on trunk, 53 were cancelled. A gate that treats a killed
    suite as anything but a refusal reports green on exactly the commits whose
    tests never finished.
    """
    assert '$latest.conclusion -eq "success"' in gate_src, (
        "the pass condition is no longer an exact match on 'success'"
    )
    for laundering in ('-ne "failure"', '-ne "failed"', 'conclusion -match'):
        assert laundering not in gate_src, (
            f"gate uses {laundering!r}: anything other than an exact 'success' "
            "test lets cancelled/skipped conclusions through as passes"
        )


def test_the_gate_requires_a_verdict_from_each_gating_workflow(gate_src: str):
    """Not 'some workflow on this commit succeeded'.

    Four workflows fire per push; two of them finish in under a minute. On
    36adb1f the security and attestation lanes were green while `Nexus QA CI`
    was CANCELLED - so a naive `gh run list | grep success` passes that commit.
    """
    assert "Nexus QA CI" in gate_src
    assert "M0.5 Security Gate" in gate_src
    assert "foreach ($wf in $GatingWorkflows)" in gate_src, (
        "the gate no longer adjudicates each gating workflow separately"
    )


def test_a_short_sha_cannot_be_mistaken_for_an_untested_commit(gate_src: str):
    """`gh run list --commit <short-sha>` returns [] and exit 0 - silently.

    Measured: `--commit d5130e4` -> [], `--commit d5130e4843ff...` -> 3 runs.
    Without the resolve step the gate refuses every legitimate deploy while
    saying the commit was never tested, which is a false statement about a
    green commit.
    """
    assert "^[0-9a-fA-F]{40}$" in gate_src, (
        "the gate no longer normalises the sha to full length before querying"
    )


# -- no escape hatch ---------------------------------------------------------

def test_there_is_no_switch_that_skips_the_ci_gate(deploy_src: str):
    """-NoGate skips the golden CRAWL gate and always has. It must not skip
    this one: the deploy somebody skips a gate on is always the urgent one,
    which is the reasoning already written above -NoGate in deploy.ps1."""
    gate_at = deploy_src.index("Join-Path $PSScriptRoot")
    block = deploy_src[gate_at: deploy_src.index("git push $REMOTE $BRANCH")]
    # Word-bounded: a bare substring test matched "$Gate" inside "$GateScript",
    # i.e. the gate's own filesystem guard, and failed the property it was
    # written to protect. A test that fires on its own subject is not a test.
    for switch in ("NoGate", "Gate", "PushOnly"):
        assert not re.search(r"\$" + switch + r"", block), (
            f"${switch} appears in the CI-gate block: a flag that can skip this "
            "gate makes 'nothing deploys without a green run' false by design"
        )
