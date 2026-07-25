"""repo-intel Phase-6 SAFETY SPINE — token-seal fail-closed guard tests.

Pins that the dev-grade LOCAL KEK provider can NEVER seal (store) a client git
token outside development:

  * in staging/production with ``NEXUS_KEK_PROVIDER=local`` → ``seal_token``
    REFUSES (``EnvelopeUnavailable``), even though the SDK service may load —
    closing the "dev KEK silently used in prod" fail-open;
  * in development (the default ``NEXUS_ENV``) the local DEVSEAL path still works
    (unchanged) and round-trips through ``open_token`` — no existing outcome moves;
  * a KMS provider name in a deployed env does not trip the local-KEK guard.

Pure — no network, no database; env is monkeypatched per test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.model.secrets import (  # noqa: E402
    EnvelopeUnavailable,
    open_token,
    seal_token,
)


@pytest.fixture(autouse=True)
def _clean_kek_env(monkeypatch):
    """Ensure each test starts from a known KEK/env baseline."""
    monkeypatch.delenv("NEXUS_ENV", raising=False)
    monkeypatch.delenv("NEXUS_KEK_PROVIDER", raising=False)
    monkeypatch.delenv("NEXUS_LOCAL_KEK_PATH", raising=False)
    yield


# NOTE (first-ever CI execution, 2026-07-25): this suite never ran in qec-ci —
# a sibling collection error interrupted the whole repo-intel session — and the
# seal/open API had since become ASYNC + tenant-scoped
# (``await seal_token(token, *, tenant_id, aad)``, secrets.py:122/150). The
# calls below follow the current contract; the properties pinned are unchanged.

@pytest.mark.parametrize("env", ["staging", "production", "prod"])
async def test_local_kek_refused_in_deployed_env(monkeypatch, env):
    monkeypatch.setenv("NEXUS_ENV", env)
    monkeypatch.setenv("NEXUS_KEK_PROVIDER", "local")
    with pytest.raises(EnvelopeUnavailable) as exc:
        await seal_token("ghp_realclienttoken", tenant_id="t-guard", aad="conn-123")
    msg = str(exc.value).lower()
    assert "local" in msg and env in msg


async def test_development_local_seal_still_works_and_round_trips(monkeypatch):
    # Default env is development; the local DEVSEAL path is unchanged.
    monkeypatch.setenv("NEXUS_KEK_PROVIDER", "local")
    blob, kek_id = await seal_token("ghp_devtoken", tenant_id="t-dev", aad="conn-abc")
    assert kek_id == "local-dev"
    assert bytes(blob).startswith(b"DEVSEAL1:")
    assert await open_token(blob, tenant_id="t-dev", aad="conn-abc") == "ghp_devtoken"


async def test_test_env_local_seal_still_works(monkeypatch):
    monkeypatch.setenv("NEXUS_ENV", "test")
    monkeypatch.setenv("NEXUS_KEK_PROVIDER", "local")
    blob, kek_id = await seal_token("ghp_testtoken", tenant_id="t-test", aad="conn-t")
    assert kek_id == "local-dev"
    assert await open_token(blob, tenant_id="t-test", aad="conn-t") == "ghp_testtoken"


async def test_default_provider_is_local_and_refused_in_prod(monkeypatch):
    # NEXUS_KEK_PROVIDER unset → defaults to 'local' → still refused in prod.
    monkeypatch.setenv("NEXUS_ENV", "production")
    with pytest.raises(EnvelopeUnavailable):
        await seal_token("ghp_realclienttoken", tenant_id="t-guard", aad="conn-xyz")


async def test_kms_provider_name_does_not_trip_the_local_guard(monkeypatch):
    # A KMS provider in prod is NOT refused by the local-KEK guard.  (Without the
    # SDK installed it still refuses via the pre-existing no-provider rule, but
    # NOT with the local-KEK message — proving the new guard is provider-specific.)
    monkeypatch.setenv("NEXUS_ENV", "production")
    monkeypatch.setenv("NEXUS_KEK_PROVIDER", "aws_kms")
    try:
        await seal_token("ghp_realclienttoken", tenant_id="t-guard", aad="conn-kms")
    except EnvelopeUnavailable as exc:
        assert "local" not in str(exc).lower()
