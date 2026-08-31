"""M0.5 SECURITY SUITE (explorer side) — adversarial tests only.

Runs independently of the application suite:

    pytest engines/qe-explorer/tests/security -q
"""
from __future__ import annotations

import os
import sys

_SERVICE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)

os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("QEC_EXPLORER_TOKEN", "unit-test-explorer-token")
# Team A / Phase A — keep the fleet announcer off in the unit suite (see
# tests/conftest.py).
os.environ.setdefault("QEC_FLEET_REGISTER", "0")

import pytest  # noqa: E402


@pytest.fixture
def jobs():
    """A fresh JobManager per test — the slot is global state by design."""
    from app.main import JobManager

    return JobManager()
