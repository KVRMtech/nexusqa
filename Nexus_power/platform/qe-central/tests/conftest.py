"""Test bootstrap for the QE-Central unit suite.

Puts the service root (and the SDK checkout, for envelope imports) on
``sys.path`` and pins deterministic env values BEFORE ``app.config`` is
imported anywhere — the ``settings`` singleton reads the environment at
import time.

Also installs the A27.1 **no-silent-skip** gate: when a ``QEC_REQUIRE_*`` flag
declares an infrastructure category mandatory (CI sets ``QEC_REQUIRE_DB``,
``QEC_REQUIRE_REDIS`` and ``QEC_REQUIRE_S3``), any test that SKIPS for want of
that infrastructure fails the whole session. The categories and the hooks live in
:mod:`tests._infra_gate`.
"""
from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SDK_PATH = os.path.abspath(
    os.path.join(_SERVICE_ROOT, "..", "..", "sdk", "nexus-sdk")
)
# tests/contract holds `_dbgate`, the shared DB gate imported by the contract
# modules; tests/ itself holds `_infra_gate`, which this file imports below and
# which the canary loads as a bare plugin (`pytest -p _infra_gate`). Put both on
# the path explicitly rather than relying on pytest's rootdir insertion order.
_CONTRACT_DIR = os.path.join(_TESTS_DIR, "contract")
for _p in (_SERVICE_ROOT, _SDK_PATH, _CONTRACT_DIR, _TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Deterministic config for the singleton (set BEFORE any app import).
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_JWT_SECRET", "unit-test-secret-qe-central")
os.environ.setdefault("QEC_LOG_LEVEL", "WARNING")
# M0.5 T-SEC-01 — ``QEC_EXPLORER_TOKEN`` no longer has a shipped default (a
# development fleet secret baked into the image is a credential anyone with this
# repository holds). Empty now means fail-closed: nothing signs, nothing
# verifies, and the /internal boundary refuses every caller. The suite therefore
# has to state its own deterministic test secret, exactly as it does for the JWT.
os.environ.setdefault("QEC_EXPLORER_TOKEN", "unit-test-explorer-token")


# ─── A27.1 — NO INFRASTRUCTURE-GATED TEST MAY SILENTLY SKIP IN CI ───────────
# The failure mode this closes: a required service fails to start (or an env var
# is misspelled), every test that needs it skips, and the job goes GREEN having
# proven nothing about that service.
#
# This began as a DATABASE-only gate (M0.x §6) — and that narrowness was itself
# the next hole. T-FL-03's six object-storage tests skipped in every CI run ever
# made, and the database-shaped gate looked straight past them because their
# reason named QEC_TEST_S3_ENDPOINT rather than a DSN. The detection is now a
# REGISTRY of infrastructure categories (database, Redis, S3/MinIO, and whatever
# comes next) that lives in tests/_infra_gate.py, so registering a new dependency
# does not mean editing detection logic.
#
# The hooks are imported rather than re-declared so that ONE implementation
# guards the real suite here AND is the thing the canary drives directly with
# `pytest -p _infra_gate` — a canary that exercised a copy would prove only that
# the copy works.
from _infra_gate import (  # noqa: E402,F401 — hook names must be module attributes
    INFRA_CATEGORIES,
    pytest_runtest_logreport,
    pytest_sessionfinish,
)
