"""M0.5 SECURITY SUITE — shared attack harness (T-SEC-10).

This package holds ADVERSARIAL tests only.  Every test here states an attack,
performs it against the real control, and asserts it FAILS.  A test that only
proves the happy path does not belong in this directory.

It runs independently of the application suite:

    pytest platform/qe-central/tests/security -q
    pytest engines/qe-explorer/tests/security -q

The CI gate (``.github/workflows/security-m05.yml``) runs both and fails the
build on any red.
"""
from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICE_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SDK_PATH = os.path.abspath(
    os.path.join(_SERVICE_ROOT, "..", "..", "sdk", "nexus-sdk")
)
for _p in (_SERVICE_ROOT, _SDK_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_JWT_SECRET", "unit-test-secret-qe-central")
os.environ.setdefault("QEC_LOG_LEVEL", "WARNING")
os.environ.setdefault("QEC_EXPLORER_TOKEN", "unit-test-explorer-token")

import pytest  # noqa: E402


@pytest.fixture
def settings_env(monkeypatch):
    """Set ``settings`` fields for one test and restore them afterwards.

    ``app.config.settings`` is a module singleton read live by the auth path, so
    an attack scenario ("this deployment is in production with the shipped
    secret") is expressed by flipping fields on it.
    """
    from app.config import settings

    def _apply(**fields):
        for key, value in fields.items():
            monkeypatch.setattr(settings, key, value, raising=False)
        return settings

    return _apply


@pytest.fixture
def fresh_nonces():
    """Clear the process-wide callback nonce store around a test.

    Replay protection is stateful by construction; a leaked nonce from one test
    would make the next one pass for the wrong reason.
    """
    from app.clients.config import CALLBACK_NONCES

    CALLBACK_NONCES.clear()
    yield CALLBACK_NONCES
    CALLBACK_NONCES.clear()
