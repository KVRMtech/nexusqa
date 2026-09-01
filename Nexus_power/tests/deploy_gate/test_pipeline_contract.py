"""M0.4 — characterization tests over the deploy pipeline's SHELL surface.

``deploy.ps1`` and ``golden_crawl_gate.sh`` cannot be unit-tested the way the
Python modules can: one needs a GCP VM, the other a live postgres and a 40-minute
crawl. What CAN be pinned is their structure — and structure is exactly where
every defect in this milestone lived:

  * T-GT-01 was one variable name reused in two blocks.
  * T-GT-04 was one ``json.dump`` aimed at a tracked file.
  * T-GT-05 was one ``exit 1`` that a caller could not distinguish from another.
  * T-GT-07 was a call to a function that did not exist.

None of those are logic errors a behavioural test would find; they are all
"the code says X in a place where it must say Y". So these tests read the files
and assert the invariants directly. They are deliberately narrow — each one names
the defect it prevents from returning, so a future reader knows whether a failure
is a real regression or a rename they need to update.
"""
from __future__ import annotations

import os
import re

import pytest


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _code(text: str) -> str:
    """Drop whole-line comments.

    These files DOCUMENT the defects they fixed, so a naive substring search
    finds `$svcList` and `ok=1` in the very comments explaining why they are
    gone. The invariants are about code; the prose must not be able to fail
    them, and must not be able to satisfy them either."""
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


@pytest.fixture()
def deploy_ps1(repo_root) -> str:
    return _read(os.path.join(os.path.dirname(repo_root), "scripts", "deploy.ps1"))


@pytest.fixture()
def gate_sh(scripts_dir) -> str:
    return _read(os.path.join(scripts_dir, "golden_crawl_gate.sh"))


@pytest.fixture()
def rollback_sh(scripts_dir) -> str:
    return _read(os.path.join(scripts_dir, "gate_rollback.sh"))


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-01 — rollback targets are captured once, never re-derived
# ══════════════════════════════════════════════════════════════════════════
def test_the_clobbered_variable_is_gone(deploy_ps1):
    """`$svcList` was assigned in the qec block and again in the main block;
    Invoke-GateRollback read the survivor. The name is retired so a future edit
    cannot reintroduce the collision by muscle memory."""
    assert "$svcList" not in _code(deploy_ps1)


def test_the_deployment_inventory_is_assigned_exactly_once(deploy_ps1):
    assigns = re.findall(r"^\s*\$DeployInventory\s*=", deploy_ps1, re.MULTILINE)
    assert len(assigns) == 1, "the rollback inventory is written more than once"


def test_rollback_does_not_inline_its_own_docker_loop(deploy_ps1):
    """deploy.ps1 and the drill each carried a copy of the rollback loop, so the
    drill proved the copy worked while the original restored one service."""
    fn = deploy_ps1.split("function Invoke-GateRollback")[1].split("\n}")[0]
    assert "gate_rollback.sh" in fn
    assert "docker compose" not in fn, "the rollback loop is inlined again"


def test_build_blocks_use_distinct_service_variables(deploy_ps1):
    assert "$qecSvcList" in deploy_ps1 and "$mainSvcList" in deploy_ps1


def test_the_manifest_is_written_from_the_frozen_inventory(deploy_ps1):
    assert "gate_manifest.py build" in deploy_ps1
    assert "$manifestArgs = ($DeployInventory -join" in deploy_ps1


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-05 — host health never triggers rollback
# ══════════════════════════════════════════════════════════════════════════
def test_the_gate_gives_host_failure_its_own_exit_code(gate_sh):
    assert "EXIT_HOST_UNAVAILABLE=4" in gate_sh
    health = gate_sh.split("GATE ABORTED — the HOST is unhealthy")[1][:900]
    assert "finish HOST_UNAVAILABLE $EXIT_HOST_UNAVAILABLE" in health
    assert "NOT rolled back" in health


def test_host_unavailable_does_not_roll_back(deploy_ps1):
    branch = deploy_ps1.split('$gateVerdict -eq "HOST_UNAVAILABLE"')[1].split("elseif")[0]
    assert "Invoke-GateRollback" not in branch, \
        "an infrastructure failure still reverts a healthy deployment"
    assert "NOT rolling back" in branch


def test_a_missing_verdict_does_not_roll_back(deploy_ps1):
    """Network partition / dropped SSH / monitoring down. We learned nothing
    about the build, so we change nothing about the fleet."""
    tail = deploy_ps1.split("GATE UNREACHABLE")[1].split("Write-Host \"`nGolden")[0]
    assert "Invoke-GateRollback" not in tail
    assert "UNVERIFIED" in tail


@pytest.mark.parametrize("verdict", ["REGRESSION", "APP_UNHEALTHY"])
def test_deployment_correctness_failures_do_roll_back(deploy_ps1, verdict):
    branch = deploy_ps1.split(f'$gateVerdict -eq "{verdict}"')[1].split("elseif")[0]
    assert "Invoke-GateRollback" in branch


def test_the_preflight_runs_before_the_swap(deploy_ps1):
    """The cleanest way to honour 'infrastructure failure must not revert a
    healthy deployment' is to notice it before the fleet changes at all.

    Ordering is asserted on the two SSH invocations, not on where the command
    strings are built: $cmds is assembled early and executed late."""
    preflight = deploy_ps1.index('--command="$healthCmd"')
    swap = deploy_ps1.index('--command="$cmds"')
    assert preflight < swap, "the health preflight runs after the container swap"
    abort = deploy_ps1.split("DEPLOY ABORTED - the host cannot support")[1][:400]
    assert "NOTHING WAS SWAPPED" in abort


def test_the_gate_and_the_preflight_share_one_definition_of_healthy(gate_sh, scripts_dir):
    """A preflight that checked something subtly different would admit a deploy
    through a door the gate then refuses — the worst of both."""
    assert 'host_health.sh"' in gate_sh
    assert os.path.exists(os.path.join(scripts_dir, "host_health.sh"))


def test_every_gate_exit_carries_a_machine_readable_verdict(gate_sh):
    """A caller whose SSH dropped before the exit code arrived can still read the
    last line. Exit code and verdict are emitted by one statement, so they cannot
    drift apart."""
    assert "printf 'GATE_VERDICT=%s\\n' \"$1\"" in gate_sh
    bare = re.findall(r"^\s*exit [0-9]\s*$", gate_sh, re.MULTILINE)
    assert bare == [], f"gate exits without announcing a verdict: {bare}"


def test_deploy_reads_the_verdict_line_not_just_the_exit_code(deploy_ps1):
    assert "GATE_VERDICT=(\\w+)" in deploy_ps1


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-04 — a gate run never writes a tracked file
# ══════════════════════════════════════════════════════════════════════════
def test_the_gate_has_no_inline_baseline_writer(gate_sh):
    """The gap bookkeeping used to json.dump() straight into the git-tracked
    baseline on EVERY run, including read-only evaluations."""
    code = _code(gate_sh)
    assert "_gaps" not in code
    assert "$BASELINE','w'" not in code and '$BASELINE","w"' not in code
    # Stronger, and rename-proof: the baseline is only ever NAMED as an argument
    # to the ratchet module. Any other mention would be an inline writer.
    for line in code.splitlines():
        if "$BASELINE" in line and "BASELINE=" not in line:
            assert "$RATCHET" in line, f"the baseline is touched outside the ratchet: {line.strip()}"


def test_the_baseline_is_written_only_by_an_explicit_command(gate_sh):
    """Every invocation of the ratchet is one of four named subcommands, and the
    only two that write the tracked baseline are guarded by an operator flag."""
    code = _code(gate_sh)
    commands = set(re.findall(r'"\$RATCHET"\s+(\w[\w-]*)', code))
    assert commands and commands <= {"evaluate", "raise", "rebaseline", "gaps"}
    assert {"evaluate", "raise", "rebaseline", "gaps"} == commands

    raise_stmt = code.split('"$RATCHET" raise')
    assert len(raise_stmt) == 2, "the raise writer is gone or duplicated"
    guard = raise_stmt[0].splitlines()[-2:]
    assert any("UPDATE_BASELINE" in l for l in guard), \
        "the baseline can be raised without --update-baseline"
    rebase_guard = code.split('"$RATCHET" rebaseline')[0].splitlines()[-2:]
    assert any("REBASELINE_REASON" in l for l in rebase_guard)


def test_the_runtime_state_file_is_gitignored(repo_root):
    with open(os.path.join(os.path.dirname(repo_root), ".gitignore"),
              "r", encoding="utf-8") as fh:
        ignored = fh.read()
    for name in (".gate_runtime_state.json", ".deploy_manifest.json"):
        assert name in ignored, f"{name} is runtime state and would be committed"


def test_the_shipped_baseline_carries_no_runtime_state(scripts_dir):
    """The committed artefact must be floors and justifications only."""
    import json
    with open(os.path.join(scripts_dir, "golden_crawl_baseline.json"),
              "r", encoding="utf-8") as fh:
        baseline = json.load(fh)
    assert "_gaps" not in baseline


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-03 + T-GT-06 — the gate measures what it ratchets
# ══════════════════════════════════════════════════════════════════════════
def test_the_gate_passes_every_ratcheted_metric_to_the_evaluator(gate_sh):
    import gate_baseline as gb
    payload = gate_sh.split("CURRENT_JSON=$(printf")[1].split(")\n")[0]
    for metric in gb.RATCHETED_METRICS:
        assert f'"{metric}"' in payload, f"{metric} is ratcheted but never measured"


def test_the_gate_reads_the_catalog_size(gate_sh):
    assert "FROM catalog_questions WHERE app_id=" in gate_sh


def test_an_uncountable_catalog_aborts_rather_than_reporting_zero(gate_sh):
    """Reporting an unanswered count as 0 would fake a total catalog collapse and
    roll back a healthy deploy on a missing table."""
    block = gate_sh.split('if [ -z "$CATALOG" ]')[1][:600]
    assert "HOST_UNAVAILABLE" in block


def test_missing_evidence_aborts_rather_than_reporting_a_collapse(gate_sh):
    """If postgres dies between the crawl and the read, every metric reads 0 and
    the gate announces a total funnel regression."""
    assert "is not readable" in gate_sh
    block = gate_sh.split("is not readable")[1][:600]
    assert "HOST_UNAVAILABLE" in block


# ══════════════════════════════════════════════════════════════════════════
#  T-GT-02 — the drill exercises the real path, multi-service
# ══════════════════════════════════════════════════════════════════════════
def test_the_drill_is_multi_service_and_calls_the_real_rollback(scripts_dir):
    drill = _read(os.path.join(scripts_dir, "gate_rollback_drill.sh"))
    assert "gate_rollback.sh" in _code(drill), "the drill still re-types the rollback loop"
    assert "SVC=qe-explorer" not in _code(drill), "the drill is still single-service"
    services = re.search(r'DRILL_SERVICES="\$\{DRILL_SERVICES:-([^}]+)\}"', drill)
    assert services and len(services.group(1).split()) >= 3
    for assertion in ("rollback set == deployment set",
                      "rollback order is reverse of deploy order",
                      "a partial rollback exits NON-zero",
                      "byte-identical"):
        assert assertion in drill, f"the drill no longer asserts: {assertion}"


# ══════════════════════════════════════════════════════════════════════════
#  Rollback executor invariants
# ══════════════════════════════════════════════════════════════════════════
def test_a_partial_restore_is_never_reported_as_success(rollback_sh):
    """The old loop set ok=1 if ANY service restored, so one-of-three exited 0."""
    code = _code(rollback_sh)
    assert "ok=1" not in code
    assert "ROLLBACK INCOMPLETE" in code
    assert "exit 1" in code.split("ROLLBACK INCOMPLETE")[1]


def test_rollback_refuses_without_a_trustworthy_inventory(rollback_sh):
    assert _code(rollback_sh).count("ROLLBACK IMPOSSIBLE") >= 3
