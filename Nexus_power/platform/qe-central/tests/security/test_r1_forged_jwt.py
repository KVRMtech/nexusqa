"""R1 — FORGED JWT.

ATTACK
======
An attacker reads ``docker-compose.qec.yml`` in this repository, finds
``NEXUS_JWT_SECRET: ${NEXUS_JWT_SECRET:-test-secret-do-not-use-in-production}``,
signs ``{"role": "admin", "tenant_id": "<victim>"}`` with that value, and calls
the API as an administrator of any tenant they name.

That worked against every deployment that never set the variable — which is what
a fresh ``docker compose up`` is.

EXPECTED
========
Rejected.  In EVERY environment where the secret is a known development default
except an explicitly local one, and unconditionally when no secret is configured
at all.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import (
    DEV_DEFAULT_JWT_SECRETS,
    MIN_DEPLOYED_JWT_SECRET_LENGTH,
    jwt_secret_usable,
)

from harness import (
    SHIPPED_DEV_FLEET_TOKEN,
    SHIPPED_DEV_SECRET,
    STRONG_SECRET,
    forge_token,
)


def _decode(token: str):
    from app.auth import _decode_token

    return _decode_token(token)


# ── the exploit ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env", ["production", "staging"])
def test_forged_admin_token_under_the_shipped_secret_is_rejected(settings_env, env):
    """THE headline attack: the repository's own secret, an admin claim."""
    settings_env(nexus_jwt_secret=SHIPPED_DEV_SECRET, nexus_env=env)
    token = forge_token(secret=SHIPPED_DEV_SECRET, role="admin")
    with pytest.raises(HTTPException) as exc:
        _decode(token)
    assert exc.value.status_code == 401


def test_fresh_deployment_with_no_secret_authenticates_nobody(settings_env):
    """A deployment that never configured a secret must not fall back to one.

    Before M0.5 the field defaulted to ``dev-jwt-secret-change-me``, so "unset"
    silently meant "use the value in the source tree".

    The attacker signs with the secret that SHIPS IN THIS REPOSITORY, because
    that is the whole attack: against a deployment that configured nothing, the
    in-tree default was the working key. Signing with an empty string instead
    would prove nothing about the server — PyJWT refuses to mint such a token at
    all (``InvalidKeyError: HMAC key must not be empty``), so the assertion below
    was never reached and the test failed inside its own setup."""
    settings_env(nexus_jwt_secret="", nexus_env="development")
    with pytest.raises(HTTPException) as exc:
        _decode(forge_token(secret=SHIPPED_DEV_SECRET, role="admin"))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("dev_secret", sorted(s for s in DEV_DEFAULT_JWT_SECRETS if s))
def test_every_known_development_secret_is_refused_in_production(
    settings_env, dev_secret,
):
    """Not just the one that shipped — the whole known-weak set."""
    settings_env(nexus_jwt_secret=dev_secret, nexus_env="production")
    with pytest.raises(HTTPException):
        _decode(forge_token(secret=dev_secret, role="admin"))


def test_an_unknown_environment_label_does_not_inherit_dev_leniency(settings_env):
    """A typo'd NEXUS_ENV must fail CLOSED.

    ``DEPLOYED_ENVS`` and ``LOCAL_ENVS`` are deliberately not complements: an
    unrecognised value is neither, and the dev-secret exemption applies only to
    the explicit local list."""
    settings_env(nexus_jwt_secret=SHIPPED_DEV_SECRET, nexus_env="prodution")
    with pytest.raises(HTTPException):
        _decode(forge_token(secret=SHIPPED_DEV_SECRET, role="admin"))


def test_a_short_secret_is_refused_in_a_deployed_environment(settings_env):
    """An explicitly-set but brute-forceable secret is not a secret."""
    short = "x" * (MIN_DEPLOYED_JWT_SECRET_LENGTH - 1)
    settings_env(nexus_jwt_secret=short, nexus_env="production")
    with pytest.raises(HTTPException):
        _decode(forge_token(secret=short, role="admin"))


# ── the positive half: a correctly-configured fleet still works ────────────

def test_a_properly_configured_secret_authenticates_a_real_principal(settings_env):
    settings_env(nexus_jwt_secret=STRONG_SECRET, nexus_env="production",
                 qec_jwt_audience="")
    ctx = _decode(forge_token(secret=STRONG_SECRET, role="manager",
                              tenant_id="tenant-real", sub="alice"))
    assert ctx["tenant_id"] == "tenant-real" and ctx["role"] == "manager"


def test_development_keeps_working_with_its_declared_test_secret(settings_env):
    """Local dev and the existing suite must not be collateral damage.

    The exemption is narrow: the secret must be EXPLICITLY configured AND the
    environment must be one of the declared local ones."""
    settings_env(nexus_jwt_secret="unit-test-secret-qe-central", nexus_env="test",
                 qec_jwt_audience="")
    ctx = _decode(forge_token(secret="unit-test-secret-qe-central", role="viewer",
                              tenant_id="tenant-dev"))
    assert ctx["tenant_id"] == "tenant-dev"


# ── the pure predicate, exhaustively ───────────────────────────────────────

@pytest.mark.parametrize("secret,env,expected", [
    ("", "development", False),
    ("", "production", False),
    (SHIPPED_DEV_SECRET, "development", True),      # explicit local dev
    (SHIPPED_DEV_SECRET, "test", True),
    (SHIPPED_DEV_SECRET, "staging", False),
    (SHIPPED_DEV_SECRET, "production", False),
    (SHIPPED_DEV_SECRET, "", False),                # unset env ⇒ not local
    (STRONG_SECRET, "development", True),
    (STRONG_SECRET, "staging", True),
    (STRONG_SECRET, "production", True),
])
def test_jwt_secret_usable_matrix(secret, env, expected):
    usable, reason = jwt_secret_usable(secret, env)
    assert usable is expected, reason


def test_minting_refuses_under_an_unsafe_secret(settings_env):
    """A credential this service would refuse must never be ISSUED either."""
    from app.service_token import mint_service_jwt

    settings_env(nexus_jwt_secret=SHIPPED_DEV_SECRET, nexus_env="production")
    with pytest.raises(ValueError):
        mint_service_jwt("tenant-a")


def test_the_shipped_compose_no_longer_carries_a_secret_default():
    """The file itself is part of the control: a default here IS the vulnerability.

    Asserted over the EXECUTABLE lines only — the comments deliberately name the
    old values so a reader understands what was removed and why, and a comment
    cannot be substituted into a container's environment.
    """
    import pathlib

    path = (pathlib.Path(__file__).resolve().parents[4] / "docker-compose.qec.yml")
    executable = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert SHIPPED_DEV_SECRET not in executable
    assert SHIPPED_DEV_FLEET_TOKEN not in executable
    # No ``:-`` DEFAULT-substitution form for either secret…
    assert "${NEXUS_JWT_SECRET:-" not in executable
    assert "${QEC_EXPLORER_TOKEN:-" not in executable
    # …and the ``:?`` form, which makes compose REFUSE to start when unset.
    assert executable.count("${NEXUS_JWT_SECRET:?") >= 2      # qe-central + repo-intel
    assert executable.count("${QEC_EXPLORER_TOKEN:?") >= 2    # qe-central + qe-explorer


def test_the_internal_seam_is_not_published_on_a_public_interface():
    """T-SEC-02 defence in depth: port 8093 binds to loopback by default."""
    import pathlib

    compose = (pathlib.Path(__file__).resolve().parents[4]
               / "docker-compose.qec.yml").read_text(encoding="utf-8")
    assert '- "8093:8093"' not in compose
    assert '${QEC_BIND_ADDRESS:-127.0.0.1}:8093:8093' in compose
