"""A rollback must not disarm the deploy that carries the fix.

MEASURED, 2026-09-01. The golden-crawl gate rejected a build on verdict-box and
`gate_rollback.sh` restored the fleet correctly - every service back on the green
image, cleanly. Then the NEXT deploy, carrying the correction, died at step [3/4]:

    You are not currently on a branch.
    Please specify which branch you want to merge with.

`git checkout <sha>` leaves a DETACHED HEAD, and the VM's deploy step is a plain
`git pull`, which has nothing to merge into. The box had to be repaired by hand.

So the rollback did its literal job and still produced an outage no deploy could
end: the failure was not in restoring the fleet, it was in leaving the source
tree unable to RECEIVE the next change. That is the property under test here.

WHAT MAKES THIS TEST WORTH ANYTHING. The assertion is about a state that is
*restored*, and a test that only checked the happy path would pass just as well
against a script that never detached at all. Two things guard against that:

  * ``test_the_unfixed_script_strands_the_checkout`` runs the PRE-FIX script -
    read out of git history, not re-implemented here - through the identical
    harness and REQUIRES the detached head and the real `git pull` failure back.
    If the bug stops being reproducible, that control goes red and this file
    stops being evidence of anything.
  * The main assertion is parametrised onto the FAILING rollback path as well,
    because that is the case that needs the guarantee most: a mixed fleet is
    exactly when an operator is about to push a correction.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]
SCRIPTS = REPO / "Nexus_power" / "scripts"
ROLLBACK = SCRIPTS / "gate_rollback.sh"

def _usable_bash():
    """A bash that can actually EXECUTE a script at the path we hand it.

    `shutil.which("bash")` on a Windows checkout finds WSL's bash.exe, which
    cannot run a script named by a Windows path and fails with

        WSL (24) ERROR: CreateProcessCommon:559: execvpe(/bin/bash) failed

    on stderr while the rollback under test never runs at all. The first draft of
    this file did exactly that: the script was never executed, HEAD therefore
    never moved, and all three assertions about HEAD passed against an ABSENT
    subject. The control below is what caught it.

    So the interpreter is not looked up, it is PROVEN - each candidate has to run
    a throwaway script and hand back its token before it is trusted.
    """
    probe_dir = Path(tempfile.mkdtemp(prefix="qec-bash-probe-"))
    probe = probe_dir / "probe.sh"
    probe.write_text("echo QEC_BASH_OK\n", encoding="utf-8", newline="\n")
    candidates = [
        os.environ.get("QEC_BASH"),
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files/Git/usr/bin/bash.exe",
        shutil.which("bash"),
    ]
    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        try:
            done = subprocess.run([cand, str(probe)], capture_output=True,
                                  text=True, timeout=60)
        except OSError:
            continue
        if done.returncode == 0 and "QEC_BASH_OK" in done.stdout:
            return cand
    return None


BASH = _usable_bash()

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None,
    reason="needs a bash able to execute a script at a native path, plus git",
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout.strip()


def _fake_docker(bin_dir, build_succeeds):
    """A `docker` that answers `compose config --services` and can refuse `build`.

    Refusing the build is how we reach the ROLLBACK INCOMPLETE exit path without
    a container runtime - the point is to leave the script by its error door,
    not to simulate Docker.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    refuse = 0 if build_succeeds else 1
    body = (
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--services" ]; then echo qe-central; exit 0; fi\n'
        '  if [ "$a" = "build" ]; then exit %d; fi\n'
        "done\n"
        "exit 0\n" % refuse
    )
    path = bin_dir / "docker"
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _worktree(tmp_path, script_text):
    """A throwaway repo shaped like verdict-box: a branch, and an older green sha."""
    src = tmp_path / "src"
    (src / "Nexus_power" / "scripts").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "develop", str(src))
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")

    (src / "Nexus_power" / "docker-compose.yml").write_text(
        "services:\n  qe-central: {}\n", encoding="utf-8"
    )
    (src / "marker.txt").write_text("green\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "green")
    green = _git(src, "rev-parse", "HEAD")

    (src / "marker.txt").write_text("rejected\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "rejected build")

    # A REMOTE, because the failure being reproduced is a `git pull` failure.
    # Without an upstream, `git pull` on a detached HEAD complains about the
    # missing remote instead - a different error, which would let the control
    # "reproduce" a bug it was not actually reproducing.
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "-q", "-u", "origin", "develop")

    # The script under test is copied in as text, so the control below can supply
    # an OLDER revision of the very same file rather than a hand-written stand-in.
    under_test = src / "Nexus_power" / "scripts" / "gate_rollback.sh"
    under_test.write_text(script_text, encoding="utf-8", newline="\n")
    helper = src / "Nexus_power" / "scripts" / "gate_manifest.py"
    shutil.copy2(SCRIPTS / "gate_manifest.py", helper)

    manifest = src / ".deploy_manifest.json"
    subprocess.run(
        [sys.executable, str(helper), "build", "--out", str(manifest),
         "--commit", green, "qe-central"],
        check=True, capture_output=True,
    )
    assert json.loads(manifest.read_text(encoding="utf-8"))["services"]
    return src, green, under_test


def _run(src, script, green, bin_dir):
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["PYTHON"] = sys.executable
    done = subprocess.run(
        [BASH, str(script), "--src", str(src), "--green", green],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    # THE SCRIPT MUST HAVE RUN. Every assertion in this file is about a state the
    # rollback is supposed to leave behind, and an unstarted script leaves the
    # pristine state - which reads identically to success. This banner is printed
    # before the checkout, so its absence means nothing under test executed.
    assert "rollback target" in done.stdout, (
        "gate_rollback.sh produced no output - it did not run, so nothing below "
        "is evidence of anything.\nexit=%s\nstdout=%s\nstderr=%s"
        % (done.returncode, done.stdout[-800:], done.stderr[-800:])
    )
    return done


def _head_branch(src):
    done = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=str(src), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return done.stdout.strip() if done.returncode == 0 else None


def _pull_output(src):
    done = subprocess.run(
        ["git", "pull"], cwd=str(src), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return done.stdout + done.stderr


# -- the property -----------------------------------------------------------

@pytest.mark.parametrize(
    "build_succeeds", [True, False], ids=["rollback-completes", "rollback-INCOMPLETE"]
)
def test_the_rollback_leaves_the_checkout_on_its_branch(tmp_path, build_succeeds):
    """Both exit doors. The failing one matters more, not less."""
    src, green, script = _worktree(tmp_path, ROLLBACK.read_text(encoding="utf-8"))
    assert _head_branch(src) == "develop", "harness did not start on a branch"

    out = _run(src, script, green, _fake_docker(tmp_path / "bin", build_succeeds).parent)

    assert _head_branch(src) == "develop", (
        "the rollback stranded the checkout on a detached HEAD; the next "
        "`git pull` on the VM would refuse to run.\n" + out.stdout + out.stderr
    )
    assert "not currently on a branch" not in _pull_output(src)


def test_a_rollback_that_never_detached_is_left_alone(tmp_path):
    """Idempotence: if HEAD is already a branch on exit, do not move it."""
    src, green, script = _worktree(tmp_path, ROLLBACK.read_text(encoding="utf-8"))
    bin_dir = _fake_docker(tmp_path / "bin", True).parent

    _run(src, script, green, bin_dir)
    assert _head_branch(src) == "develop"
    tip = _git(src, "rev-parse", "HEAD")

    _run(src, script, green, bin_dir)
    assert _head_branch(src) == "develop"
    assert _git(src, "rev-parse", "HEAD") == tip


# -- the control: prove the bug was real, and that this test can see it ------

def test_the_unfixed_script_strands_the_checkout(tmp_path):
    """FALSIFICATION CONTROL - remove the fix, require the outage back.

    The pre-fix script is read from git history rather than written out here, so
    the control cannot quietly drift into agreeing with the implementation.
    """
    shown = subprocess.run(
        ["git", "show", "HEAD:Nexus_power/scripts/gate_rollback.sh"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if shown.returncode != 0 or "restore_branch" in shown.stdout:
        pytest.skip(
            "no pre-fix revision reachable at HEAD (the fix is committed there), "
            "so this control cannot remove the guard it exists to remove"
        )

    src, green, script = _worktree(tmp_path, shown.stdout)
    _run(src, script, green, _fake_docker(tmp_path / "bin", True).parent)

    assert _head_branch(src) is None, (
        "the control did not reproduce the detached HEAD - the harness is no "
        "longer exercising the failure this file claims to prevent"
    )
    assert "not currently on a branch" in _pull_output(src), (
        "the detached HEAD did not actually break `git pull`; the deploy-step "
        "failure this test is built around is not being reproduced"
    )
