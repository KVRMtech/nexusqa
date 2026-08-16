"""Test bootstrap for the QE-Central unit suite.

Puts the service root (and the SDK checkout, for envelope imports) on
``sys.path`` and pins deterministic env values BEFORE ``app.config`` is
imported anywhere — the ``settings`` singleton reads the environment at
import time.

Also installs the M0.x **no-silent-skip** gate: when ``QEC_REQUIRE_DB`` is set
(CI does), a database-gated test that SKIPS for want of a DSN fails the whole
session. See :func:`pytest_sessionfinish`.
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
# modules. Put it on the path explicitly rather than relying on pytest's
# rootdir insertion order.
_CONTRACT_DIR = os.path.join(_TESTS_DIR, "contract")
for _p in (_SERVICE_ROOT, _SDK_PATH, _CONTRACT_DIR):
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


# ─── M0.x §6 — a database-gated test may never silently skip in CI ───────────
# The failure mode this closes: the CI Postgres service fails to start (or a DSN
# is misspelled), every DB test skips for want of its env var, and the job goes
# GREEN having proven nothing about the database. Under QEC_REQUIRE_DB those
# skips are collected and the session is failed with them listed by name.

def _db_required() -> bool:
    return os.environ.get("QEC_REQUIRE_DB", "").strip().lower() in ("1", "true", "yes", "on")


#: Substrings that identify a skip as "I had no database", as opposed to a
#: legitimate skip (an unsupported platform, an opt-in slow test, …).
_DB_SKIP_SIGNATURES = (
    "QEC_TEST_DATABASE_URL",
    "QEC_TEST_QEC_DATABASE_URL",
    "QEC_TEST_SUBSTRATE_DATABASE_URL",
    "QEC_TEST_ADMIN_DATABASE_URL",
    "QEC_TEST_REDIS_URL",
    "BYPASSES RLS",
)

#: …and substrings that EXEMPT a skip from the gate even though its reason also
#: names a DSN. QEC_REQUIRE_DB is a promise about the DATABASE services; it is
#: not a promise that a live platform-api HTTP server is running beside them.
#: The §2.4 factory HTTP honesty pins need one ("the live factory sharing the
#: same DB"), so under QEC_REQUIRE_DB alone they legitimately skip — treating
#: them as database failures would make the gate cry wolf, and a gate that cries
#: wolf gets switched off. Standing up that service is its own milestone; until
#: then this exemption is the honest declaration of the boundary.
_NON_DB_SKIP_SIGNATURES = (
    "QEC_TEST_PLATFORM_API_URL",
)

_db_skips: list[tuple[str, str]] = []


def pytest_runtest_logreport(report):
    """Record every skip whose reason names a database DSN."""
    if report.when != "setup" or report.outcome != "skipped":
        return
    reason = ""
    if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
        reason = str(report.longrepr[2])
    else:  # pragma: no cover — defensive; pytest may change the shape
        reason = str(report.longrepr)
    if any(sig in reason for sig in _NON_DB_SKIP_SIGNATURES):
        return
    if any(sig in reason for sig in _DB_SKIP_SIGNATURES):
        _db_skips.append((report.nodeid, reason.replace("Skipped: ", "", 1)))


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook shape
    """Fail the session when required DB tests could not execute."""
    if not _db_required() or not _db_skips:
        return
    lines = "\n".join(f"    {nodeid}\n      ↳ {reason}" for nodeid, reason in _db_skips)
    print(
        "\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"QEC_REQUIRE_DB is set, but {len(_db_skips)} database-gated test(s) SKIPPED.\n"
        "A skipped database test is NOT a passing database test. The services\n"
        "were declared mandatory for this run, so this is a FAILURE:\n"
        f"{lines}\n"
        "═══════════════════════════════════════════════════════════════════",
        file=sys.stderr,
    )
    session.exitstatus = 1
