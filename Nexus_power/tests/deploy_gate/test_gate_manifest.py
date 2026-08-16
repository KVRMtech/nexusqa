"""M0.4 / T-GT-01 + T-GT-02 — the deployment inventory and rollback selection.

The defect these pin: ``deploy.ps1`` reused one ``$svcList`` variable across two
build blocks, and ``Invoke-GateRollback`` read whatever the LAST block wrote. On
the default 3-service deploy that is ``platform-api``, so a red gate restored one
service in three and reported success while two containers kept serving the
rejected build. Rollback targets are now DATA — captured once, validated, and
never re-derived at rollback time.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import gate_manifest as gm

ALL = ["qe-central", "qe-explorer", "platform-api"]


# ══════════════════════════════════════════════════════════════════════════
#  Building the inventory
# ══════════════════════════════════════════════════════════════════════════
def test_a_full_deploy_records_every_service():
    manifest = gm.build_manifest(ALL)
    assert [e["name"] for e in manifest["services"]] == ALL


def test_each_service_records_the_compose_file_that_owns_it():
    """Rollback must rebuild through the SAME file the deploy used. Deriving it a
    second time is how a rollback runs `docker compose -f the-wrong-file up` and
    reports success on a no-op."""
    by_name = {e["name"]: e["compose"] for e in gm.build_manifest(ALL)["services"]}
    assert by_name["qe-central"] == "docker-compose.qec.yml"
    assert by_name["qe-explorer"] == "docker-compose.qec.yml"
    assert by_name["platform-api"] == "docker-compose.yml"


def test_the_inventory_is_deterministic_regardless_of_request_order():
    a = gm.build_manifest(["platform-api", "qe-explorer", "qe-central"])
    b = gm.build_manifest(["qe-central", "platform-api", "qe-explorer"])
    assert a == b


def test_duplicates_collapse():
    manifest = gm.build_manifest(["qe-central", "qe-central", "platform-api"])
    assert [e["name"] for e in manifest["services"]] == ["qe-central", "platform-api"]


def test_a_partial_deploy_records_only_what_it_deployed():
    """Rollback must restore neither fewer NOR extra services."""
    manifest = gm.build_manifest(["qe-explorer"])
    assert [e["name"] for e in manifest["services"]] == ["qe-explorer"]
    assert [e["name"] for e in gm.rollback_plan(manifest)] == ["qe-explorer"]


def test_an_unknown_service_is_refused_at_deploy_time():
    """Fail loudly here, not during an incident: a bad inventory discovered at
    rollback time means restoring the wrong containers at the worst moment."""
    with pytest.raises(gm.ManifestError) as exc:
        gm.build_manifest(["qe-central", "not-a-service"])
    assert "not-a-service" in str(exc.value)


def test_an_empty_deploy_is_refused():
    with pytest.raises(gm.ManifestError):
        gm.build_manifest([])


# ══════════════════════════════════════════════════════════════════════════
#  Rollback selection — the acceptance criterion for T-GT-01
# ══════════════════════════════════════════════════════════════════════════
def test_rollback_restores_exactly_the_deployment_set():
    """Service A + B + C in, A + B + C out. 100% of the time."""
    manifest = gm.build_manifest(ALL)
    restored = {e["name"] for e in gm.rollback_plan(manifest)}
    assert restored == set(ALL)


def test_rollback_never_collapses_to_the_last_build_block():
    """The exact historical bug, stated as an assertion: the rollback set is not
    'whatever the main-compose block wrote last'."""
    plan = [e["name"] for e in gm.rollback_plan(gm.build_manifest(ALL))]
    assert plan != ["platform-api"]
    assert len(plan) == 3


def test_rollback_order_is_the_reverse_of_deploy_order():
    """LIFO: the last thing swapped in is the first thing swapped out, and the
    backend a service depends on is restored before its callers."""
    manifest = gm.build_manifest(ALL)
    deploy = [e["name"] for e in gm.deploy_plan(manifest)]
    rollback = [e["name"] for e in gm.rollback_plan(manifest)]
    assert rollback == list(reversed(deploy))
    assert rollback[0] == "platform-api"


def test_ordering_is_stable_across_a_write_read_cycle(tmp_path):
    path = str(tmp_path / "m.json")
    gm.write_manifest(path, gm.build_manifest(ALL))
    loaded = gm.load_manifest(path)
    assert [e["name"] for e in gm.rollback_plan(loaded)] == ["platform-api", "qe-explorer", "qe-central"]


# ══════════════════════════════════════════════════════════════════════════
#  Failure injection — a manifest we cannot trust must ABORT, never guess
# ══════════════════════════════════════════════════════════════════════════
def test_a_missing_manifest_refuses(tmp_path):
    with pytest.raises(gm.ManifestError) as exc:
        gm.load_manifest(str(tmp_path / "absent.json"))
    assert "no deployment manifest" in str(exc.value)


def test_a_corrupt_manifest_refuses(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{ truncated")
    with pytest.raises(gm.ManifestError):
        gm.load_manifest(str(path))


def test_a_future_manifest_version_refuses(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"manifest_version": 99, "services": [
        {"name": "qe-central", "compose": "docker-compose.qec.yml", "order": 1}]}))
    with pytest.raises(gm.ManifestError) as exc:
        gm.load_manifest(str(path))
    assert "version" in str(exc.value)


def test_a_manifest_with_no_services_refuses(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"manifest_version": gm.MANIFEST_VERSION, "services": []}))
    with pytest.raises(gm.ManifestError):
        gm.load_manifest(str(path))


def test_a_service_entry_missing_its_compose_file_refuses(tmp_path):
    """Silently defaulting the compose file would restore a service through a
    file that may not define it — a rollback that no-ops and reports success."""
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"manifest_version": gm.MANIFEST_VERSION,
                                "services": [{"name": "qe-central", "order": 1}]}))
    with pytest.raises(gm.ManifestError) as exc:
        gm.load_manifest(str(path))
    assert "compose" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════
#  CLI contract — this is what deploy.ps1 and gate_rollback.sh call
# ══════════════════════════════════════════════════════════════════════════
def _cli(scripts_dir, *args):
    return subprocess.run([sys.executable, f"{scripts_dir}/gate_manifest.py", *args],
                          capture_output=True, text=True)


def test_cli_build_then_rollback_plan(scripts_dir, tmp_path):
    out = str(tmp_path / "m.json")
    built = _cli(scripts_dir, "build", "--out", out, "--commit", "abc123", *ALL)
    assert built.returncode == 0
    plan = _cli(scripts_dir, "rollback-plan", "--manifest", out)
    assert plan.returncode == 0
    rows = [l.split("\t") for l in plan.stdout.strip().splitlines()]
    assert [r[0] for r in rows] == ["platform-api", "qe-explorer", "qe-central"]
    assert rows[0][1] == "docker-compose.yml"


def test_cli_build_refuses_an_unknown_service(scripts_dir, tmp_path):
    res = _cli(scripts_dir, "build", "--out", str(tmp_path / "m.json"), "redis")
    assert res.returncode == 2
    assert "MANIFEST ERROR" in res.stderr


def test_cli_rollback_plan_refuses_a_corrupt_manifest(scripts_dir, tmp_path):
    path = tmp_path / "m.json"
    path.write_text("nonsense")
    res = _cli(scripts_dir, "rollback-plan", "--manifest", str(path))
    assert res.returncode == 2
    assert res.stdout.strip() == "", "a corrupt manifest produced a rollback plan"


def test_the_manifest_file_is_byte_stable(tmp_path):
    """Two identical deploys produce identical manifests, so a diff in this file
    always means the deployment set actually changed."""
    a, b = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    gm.write_manifest(a, gm.build_manifest(ALL, commit="c1", deployed_at="t1"))
    gm.write_manifest(b, gm.build_manifest(ALL, commit="c1", deployed_at="t1"))
    assert open(a, "rb").read() == open(b, "rb").read()
