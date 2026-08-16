"""M0.4 / T-GT-01 + T-GT-02 — execute the REAL rollback script.

The unit tests prove the rollback PLAN is correct. These prove the executable
that consumes it — ``scripts/gate_rollback.sh``, the same file ``deploy.ps1``
invokes on a red gate — restores every service in it.

``docker`` and ``git`` are replaced with recording shims on ``$PATH``, so the
script's real control flow runs (argument parsing, manifest load, ordering,
per-service build/up, all-or-report) without a VM. The old drill could only be
run against production, which is why the single-service assumption baked into it
went unchallenged for so long.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

import gate_manifest as gm

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="bash unavailable on this host")

ALL = ["qe-central", "qe-explorer", "platform-api"]


def _fwd(path) -> str:
    """Forward-slash path: understood by Git Bash AND by native Python on
    Windows, which the shims invoke to read their config."""
    return str(path).replace("\\", "/")


@pytest.fixture()
def sandbox(tmp_path, scripts_dir):
    """A fake deployment: repo tree, green anchor, manifest, and shims.

    ``fail_for`` in the shim config makes a named service's ``build`` fail, which
    is how the partial-rollback path is exercised."""
    src = tmp_path / "nexus-src"
    (src / "Nexus_power" / "scripts").mkdir(parents=True)
    for f in ("gate_rollback.sh", "gate_manifest.py"):
        shutil.copy(os.path.join(scripts_dir, f), src / "Nexus_power" / "scripts" / f)
    (src / ".last_green_deploy").write_text("green0000000000000000000000000000000000\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    cfg = tmp_path / "shim.json"
    cfg.write_text(json.dumps({"fail_for": [], "unknown_service": []}))

    # A shim that RECORDS every invocation, so ordering is observable.
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
CFG={_fwd(cfg)}
echo "docker $*" >> {_fwd(log)}
if [ "${{1:-}}" = "compose" ]; then
  # `compose -f <file> <verb> <svc>`
  FILE="$3"; VERB="$4"; SVC="$5"
  case "$VERB" in
    config)
      # `config --services` lists what this compose file defines.
      SVC_UNKNOWN=$("$PYTHON" -c "import json;print(' '.join(json.load(open('$CFG'))['unknown_service']))")
      for s in qe-central qe-explorer platform-api; do
        case " $SVC_UNKNOWN " in *" $s "*) continue ;; esac
        echo "$s"
      done
      exit 0 ;;
    build|up)
      FAIL=$("$PYTHON" -c "import json;print(' '.join(json.load(open('$CFG'))['fail_for']))")
      for f in $FAIL; do [ "$f" = "$SVC" ] && exit 1; done
      exit 0 ;;
  esac
fi
exit 0
""")
    (bin_dir / "git").write_text(f"""#!/usr/bin/env bash
echo "git $*" >> {_fwd(log)}
exit 0
""")
    for f in ("docker", "git"):
        os.chmod(bin_dir / f, 0o755)

    manifest = tmp_path / "manifest.json"
    gm.write_manifest(str(manifest), gm.build_manifest(ALL, commit="deadbeef"))

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    # `python3` on this host is the Windows Store stub; point the scripts at the
    # interpreter running the tests.
    env["PYTHON"] = sys.executable

    class Sandbox:
        def __init__(self):
            self.src, self.manifest, self.log, self.cfg = src, manifest, log, cfg
            self.script = src / "Nexus_power" / "scripts" / "gate_rollback.sh"

        def configure(self, **kw):
            data = json.loads(self.cfg.read_text())
            data.update(kw)
            self.cfg.write_text(json.dumps(data))

        def run(self, *extra):
            return subprocess.run(
                [BASH, _fwd(self.script), "--src", _fwd(self.src),
                 "--manifest", _fwd(self.manifest), *extra],
                capture_output=True, text=True, env=env, cwd=str(tmp_path))

        def calls(self):
            return self.log.read_text().splitlines() if self.log.exists() else []

    return Sandbox()


# ══════════════════════════════════════════════════════════════════════════
#  The acceptance criterion for T-GT-01
# ══════════════════════════════════════════════════════════════════════════
def test_rollback_restores_every_deployed_service(sandbox):
    res = sandbox.run()
    assert res.returncode == 0, res.stdout + res.stderr
    for svc in ALL:
        assert f"OK   {svc} restored" in res.stdout, f"{svc} was not restored"
    assert "ROLLBACK COMPLETE" in res.stdout


def test_every_service_is_both_built_and_swapped(sandbox):
    """A rollback that rebuilds but never recreates leaves the old container
    serving — the failure mode is invisible from the exit code alone."""
    sandbox.run()
    calls = "\n".join(sandbox.calls())
    for svc in ALL:
        assert f"build {svc}" in calls
        assert f"up -d --force-recreate {svc}" in calls


def test_each_service_is_restored_through_its_own_compose_file(sandbox):
    """Also pins a CR-injection defect this test found: a CRLF plan left a
    trailing carriage return on the compose filename, so every `docker compose
    -f` ran against a name that LOOKED right in the log and did not exist."""
    sandbox.run()
    joined = "\n".join(sandbox.calls())
    assert "compose -f docker-compose.yml build platform-api" in joined
    assert "compose -f docker-compose.qec.yml build qe-central" in joined
    assert "compose -f docker-compose.qec.yml build qe-explorer" in joined
    assert "compose -f docker-compose.yml build qe-central" not in joined
    assert "\r" not in "".join(sandbox.calls())


def test_rollback_executes_in_reverse_deploy_order(sandbox):
    sandbox.run()
    built = [c.split()[-1] for c in sandbox.calls() if " build " in c]
    assert built == ["platform-api", "qe-explorer", "qe-central"]


def test_the_tree_is_checked_out_to_the_green_anchor(sandbox):
    sandbox.run()
    assert any("checkout" in c and "green0000" in c for c in sandbox.calls())


# ══════════════════════════════════════════════════════════════════════════
#  Failure injection
# ══════════════════════════════════════════════════════════════════════════
def test_a_partial_rollback_fails_and_names_the_survivors(sandbox):
    """The heart of the old bug's cover-up: the previous implementation set
    ok=1 if ANY service restored and exited 0, so a one-of-three rollback
    printed 'Fleet restored' and ended the investigation."""
    sandbox.configure(fail_for=["qe-central"])
    res = sandbox.run()
    assert res.returncode == 1
    assert "ROLLBACK INCOMPLETE" in res.stdout
    assert "qe-central" in res.stdout.split("still on the REJECTED build:")[1]
    # the others still restored — a partial rollback restores what it can
    assert "OK   platform-api restored" in res.stdout


def test_a_service_absent_from_the_green_commit_is_reported_not_skipped(sandbox):
    """Rolling back to an older tree can reach a commit that predates a service.
    Skipping it quietly means a container stays on the rejected build with
    nothing said."""
    sandbox.configure(unknown_service=["qe-explorer"])
    res = sandbox.run()
    assert res.returncode == 1
    assert "is not defined in docker-compose.qec.yml" in res.stdout
    assert "qe-explorer" in res.stdout.split("still on the REJECTED build:")[1]


def test_no_green_anchor_refuses_to_roll_back(sandbox):
    (sandbox.src / ".last_green_deploy").unlink()
    res = sandbox.run()
    assert res.returncode == 2
    assert "ROLLBACK IMPOSSIBLE" in res.stdout
    assert not any("checkout" in c for c in sandbox.calls()), \
        "it checked out something despite having no anchor"


def test_a_corrupt_manifest_refuses_rather_than_restoring_a_guess(sandbox):
    sandbox.manifest.write_text("{ truncated")
    res = sandbox.run()
    assert res.returncode == 2
    assert "ROLLBACK IMPOSSIBLE" in res.stdout
    assert not any("build" in c for c in sandbox.calls())


def test_a_missing_manifest_refuses(sandbox):
    sandbox.manifest.unlink()
    res = sandbox.run()
    assert res.returncode == 2
    assert "no deployment manifest" in res.stdout


def test_a_failed_checkout_aborts_before_touching_containers(sandbox, tmp_path):
    """If the source tree cannot be moved to the green commit, rebuilding would
    reinstall the REJECTED build under a 'rollback succeeded' banner."""
    bad_git = tmp_path / "bin" / "git"
    bad_git.write_text("#!/usr/bin/env bash\nexit 1\n")
    os.chmod(bad_git, 0o755)
    res = sandbox.run()
    assert res.returncode == 1
    assert "could not check out" in res.stdout
    assert not any(" build " in c for c in sandbox.calls())


def test_dry_run_reports_the_plan_and_changes_nothing(sandbox):
    res = sandbox.run("--dry-run")
    assert res.returncode == 0
    assert "platform-api" in res.stdout
    assert not any("build" in c or "checkout" in c for c in sandbox.calls())
