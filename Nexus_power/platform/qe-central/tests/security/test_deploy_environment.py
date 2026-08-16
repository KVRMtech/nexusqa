"""DEPLOY ENVIRONMENT — the variable that disarms the whole safety spine.

THE FINDING
===========
``docker-compose.qec.yml`` declares ``NEXUS_ENV: ${NEXUS_ENV:-development}``, and
``scripts/deploy.ps1`` ran every ``docker compose`` call with NO ``--env-file``.
So the fleet that actually serves clients booted as **development**, and three
independent controls silently went inert at once:

  * ``boot_validator.validate_boot_safety`` only REFUSES in staging/production —
    in development it warns and boots, on a dev KEK, with whatever secrets exist;
  * ``auth.assert_signing_key_usable`` permits a KNOWN development JWT secret
    when the environment says development — which is what the fleet claimed;
  * ``prod_guard._bypass_allowed`` honours ``fences.onboarding_test_bypass``
    ONLY in a development environment — so a real client app could be waved past
    attestation by a flag in its own row.

Every one of those is correct code. All three were disarmed by one unset
variable, which is why this is tested as its own control rather than as a
property of any of them.

``scripts/verdict_box_bootstrap.sh`` already GENERATES ``.env.production`` with
``NEXUS_ENV=production`` and strong secrets — it was simply never passed to the
deploys that followed. The deploy and the rollback now both carry it.
"""
from __future__ import annotations

import pathlib

import pytest

from app.config import DEPLOYED_ENVS, LOCAL_ENVS, jwt_secret_usable
from app.security import prod_guard
from app.security.boot_validator import BootSafetyError, validate_boot_safety

ROOT = pathlib.Path(__file__).resolve().parents[4]
REPO = ROOT.parent


def _dev_default_settings(env: str):
    """A deployment wearing every development default at once."""
    import types

    return types.SimpleNamespace(
        nexus_env=env,
        nexus_kek_provider="local",
        nexus_jwt_secret="test-secret-do-not-use-in-production",
        qec_explorer_token="dev-explorer-token-change-me",
        qec_database_url="postgresql+asyncpg://qec:qec-dev@postgres:5432/qecentral",
        nexus_database_url_substrate=(
            "postgresql+asyncpg://qec_substrate:qec-substrate-dev@postgres:5432/nexus"),
    )


# ── why the variable matters: three controls, one switch ───────────────────

@pytest.mark.parametrize("env", sorted(DEPLOYED_ENVS))
def test_the_boot_gate_refuses_a_dev_default_fleet_when_deployed(env):
    with pytest.raises(BootSafetyError):
        validate_boot_safety(_dev_default_settings(env))


def test_the_same_fleet_boots_happily_as_development():
    """The disarmed state, demonstrated rather than asserted.

    This is not a bug in the boot gate — it is the gate behaving exactly as
    designed for a developer's laptop, on a box that told it it WAS one."""
    violations = validate_boot_safety(_dev_default_settings("development"))
    assert violations, "expected the dev defaults to be reported"
    # …reported, but NOT fatal. That is the whole finding.


@pytest.mark.parametrize("env", sorted(DEPLOYED_ENVS))
def test_the_jwt_gate_also_depends_on_the_same_variable(env):
    secret = "test-secret-do-not-use-in-production"
    assert jwt_secret_usable(secret, env)[0] is False
    assert jwt_secret_usable(secret, "development")[0] is True


def test_the_onboarding_bypass_also_depends_on_the_same_variable():
    """A real client app can be waved past attestation in a dev environment."""
    row = {"fences": {"onboarding_test_bypass": True}, "env_attestation": {},
           "schedule": {}, "app_id": "a1"}
    # development: the bypass is honoured, so a non-attested app crawls.
    prod_guard.assert_crawlable(row, env="development")
    # production: it is not.
    with pytest.raises(prod_guard.OnboardingRefused):
        prod_guard.assert_crawlable(row, env="production")


def test_deployed_and_local_environments_do_not_overlap():
    """An unrecognised NEXUS_ENV must be neither — so a typo fails closed."""
    assert not (DEPLOYED_ENVS & LOCAL_ENVS)
    assert "prodution" not in DEPLOYED_ENVS and "prodution" not in LOCAL_ENVS


# ── the deploy path carries the environment ────────────────────────────────

def test_the_deploy_script_passes_the_production_env_file():
    """Every compose invocation in the deploy must carry --env-file.

    A single call without it re-opens the finding for the service it builds."""
    src = (REPO / "scripts/deploy.ps1").read_text(encoding="utf-8")
    compose_calls = [ln for ln in src.splitlines()
                     if "docker compose" in ln and ("build" in ln or "up -d" in ln)]
    assert compose_calls, "no compose build/up calls found - has deploy.ps1 moved?"
    for line in compose_calls:
        assert "--env-file" in line, f"compose call without --env-file: {line.strip()}"


def test_the_deploy_script_refuses_to_run_without_the_env_file():
    src = (REPO / "scripts/deploy.ps1").read_text(encoding="utf-8")
    assert '$ENV_FILE = ".env.production"' in src
    assert "if [ ! -f $ENV_FILE ]" in src
    # and it asserts the file actually names a deployed environment
    assert "NEXUS_ENV=(staging|production)" in src


def test_the_rollback_carries_the_same_environment():
    """An incident restore must not silently downgrade the fleet to development."""
    src = (ROOT / "scripts/gate_rollback.sh").read_text(encoding="utf-8")
    assert 'ENV_FILE="${ENV_FILE:-.env.production}"' in src
    for line in src.splitlines():
        if "docker compose" in line and ("build " in line or "up -d" in line
                                         or "config --services" in line):
            assert "$ENV_ARGS" in line, f"rollback compose call unguarded: {line.strip()}"


def test_the_bootstrap_still_generates_a_production_env_file():
    """The deploy now REQUIRES what the bootstrap already produced."""
    src = (ROOT / "scripts/verdict_box_bootstrap.sh").read_text(encoding="utf-8")
    assert "NEXUS_ENV=production" in src
    assert 'ENV_FILE="$REPO/.env.production"' in src
    assert "NEXUS_KEK_PROVIDER=gcp_kms" in src
