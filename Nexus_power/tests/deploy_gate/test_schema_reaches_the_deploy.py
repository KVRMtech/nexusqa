"""The deploy must carry the SCHEMA, not only the code.

MEASURED 2026-09-04. Production sat at ``qec_023`` while the repository shipped
``qec_025``. Two migrations had never run, so ``journeys.criticality_band`` and
``catalog_questions.revealed_by`` did not exist in the serving database while the
deployed CODE was written against them. The failure was SILENT — a crawl of a new
application captured its questions correctly and then wrote nothing:

    OrangeHRM    57 questions captured -> 0 catalog_questions, 0 journeys
    Summit Life  83 catalog_questions, 14 journeys   (written before the drift)

The golden app looked healthy the whole time because its rows predate the
divergence, so no gate went red and the catalogue was dead for weeks.

HOW IT WAS ABLE TO HAPPEN. ``alembic upgrade head`` appeared in this repository
exactly once: as a COMMENT in docker-compose.qec.yml describing a one-time
bootstrap. No deploy ran it. The code advanced on every deploy; the schema
advanced only when a human remembered.

WHY test_schema_drift DID NOT CATCH IT. That test compares the MODELS to the
MIGRATIONS — both were correct and consistent at qec_025. Nobody compared the
migrations to the database that is actually serving. Two green halves, one dead
product. That is the gap these tests close.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _code(text: str) -> str:
    """Drop whole-line comments — the file DOCUMENTS this defect at length, and
    prose must neither satisfy nor fail an invariant about code."""
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


@pytest.fixture()
def deploy_ps1(repo_root) -> str:
    return _read(os.path.join(os.path.dirname(repo_root), "scripts", "deploy.ps1"))


@pytest.fixture()
def schema_gate_path(scripts_dir) -> str:
    return os.path.join(scripts_dir, "gate_schema_deployed.sh")


# ══════════════════════════════════════════════════════════════════════════
#  The deploy runs migrations at all
# ══════════════════════════════════════════════════════════════════════════

def test_the_deploy_runs_alembic(deploy_ps1):
    code = _code(deploy_ps1)
    assert "alembic" in code and "upgrade head" in code, (
        "deploy.ps1 no longer applies migrations. Without this the schema "
        "advances only when a human remembers, which is how production reached "
        "qec_023 against a qec_025 codebase and the catalogue silently died."
    )


def test_migrations_run_BEFORE_the_swap(deploy_ps1):
    """Order is the whole point: schema first, then the code that reads it.

    Running the upgrade after ``up -d`` would leave a window — however short —
    in which the new code serves against the old schema. That is the exact
    failure being fixed, merely shortened, and a shortened silent failure is
    harder to find, not easier.
    """
    code = _code(deploy_ps1)
    upgrade_at = code.find("upgrade head")
    swap_at = code.find("up -d --force-recreate")
    assert upgrade_at != -1, "no alembic upgrade in deploy.ps1"
    assert swap_at != -1, "no service swap in deploy.ps1"
    assert upgrade_at < swap_at, (
        "the migration runs AFTER the swap (upgrade at %d, swap at %d): the new "
        "code would serve against the old schema" % (upgrade_at, swap_at)
    )


def test_the_deploy_proves_the_upgrade_landed(deploy_ps1):
    """CONTROL — a migration step that silently no-ops is the same defect.

    Running the command is not evidence it worked; the deploy must assert the
    serving database is at this checkout's head afterwards.
    """
    assert "gate_schema_deployed.sh" in _code(deploy_ps1), (
        "deploy.ps1 applies migrations but never checks they landed"
    )


# ══════════════════════════════════════════════════════════════════════════
#  The gate itself
# ══════════════════════════════════════════════════════════════════════════

def test_the_gate_exists_and_derives_the_head(schema_gate_path):
    assert os.path.exists(schema_gate_path), "gate_schema_deployed.sh is missing"
    body = _read(schema_gate_path)
    assert "alembic_version" in body, "the gate never reads the deployed revision"
    # A hard-coded head is a second thing to forget, and forgetting is the
    # entire failure mode. The head must be derived from the migrations present.
    assert "down_revision" in body, (
        "the gate does not derive the head from the migration graph — a "
        "hard-coded head silently rots the moment a migration is added"
    )


def _bash():
    """Prove an interpreter by running it, rather than trusting a PATH hit.

    WSL's bash.exe is on PATH on these machines and cannot execute a script at a
    Windows path — a test that assumed otherwise would pass every assertion
    while the script never ran, which this repository has already been bitten by.
    """
    exe = shutil.which("bash")
    if not exe:
        return None
    try:
        probe = subprocess.run([exe, "-c", "echo ok"], capture_output=True,
                               text=True, timeout=30)
    except Exception:
        return None
    return exe if probe.returncode == 0 and "ok" in probe.stdout else None


def test_an_unreadable_database_is_UNKNOWABLE_not_in_sync(schema_gate_path):
    """CONTROL — the blind-verifier guard, and the reason this file exists.

    If the gate cannot read the database it must say so. Reporting IN_SYNC when
    it read nothing would make it pass hardest exactly when the database is
    unreachable, which is the one moment a deploy most needs to stop.
    """
    exe = _bash()
    if exe is None:
        pytest.skip("no bash able to run a script at this path")
    with tempfile.TemporaryDirectory() as tmp:
        # A valid, single-headed migration graph, so the ONLY thing that can
        # fail is the database read.
        for rev, down in (("aaa", "None"), ("bbb", '"aaa"'), ("ccc", '"bbb"')):
            down_line = ("down_revision = None" if down == "None"
                         else 'down_revision = %s' % down)
            with open(os.path.join(tmp, "%s.py" % rev), "w", encoding="utf-8") as fh:
                fh.write('revision = "%s"\n%s\n' % (rev, down_line))
        env = dict(os.environ)
        env["QEC_VERSIONS_DIR"] = tmp
        env["QEC_PG_CONTAINER"] = "a-container-that-does-not-exist-12345"
        done = subprocess.run([exe, schema_gate_path], capture_output=True,
                              text=True, timeout=180, env=env)
    assert "SCHEMA_VERDICT=UNKNOWABLE" in done.stdout, (
        "a gate that cannot read the database must report UNKNOWABLE; got:\n%s"
        % done.stdout[-800:]
    )
    assert done.returncode == 2, (
        "UNKNOWABLE must be its own exit code (2), distinguishable from drift "
        "(1) and from sync (0) — a caller cannot roll back correctly otherwise; "
        "got %d" % done.returncode
    )
    assert "SCHEMA_VERDICT=IN_SYNC" not in done.stdout, (
        "the gate reported IN_SYNC without reading the database"
    )
