"""Regression — background recovery runs share ONE runner budget + cert debounce.

The recovery automation (certification on every regenerate + an auto-heal per
failing scenario) shares the single runner with client runs. Un-budgeted, it
thrashed the runner: client runs 409'd and clean measurement was impossible
(2026-07-25). The budget:
  * ONE background recovery run holds the runner at a time (a shared lock that
    BOTH certification and auto-heal acquire, each released in a finally);
  * a per-artifact certification cooldown so rapid regenerates coalesce;
  * bounded lock waits so a wedged job never blocks forever.

Client-initiated runs (playwright_run / run_live / heal_step) must NOT take the
lock — they always get the runner ahead of background recovery.

Source-level pins (the router imports heavy deps; assert on its text).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_recovery_concurrency_budget.py -q
"""
from __future__ import annotations

import os
import re

_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "test_factory.py"),
    encoding="utf-8").read()


def _fn(name: str) -> str:
    """Return the source of one top-level async/def function body."""
    m = re.search(rf"\n(?:async )?def {re.escape(name)}\(.*?\n(?=\n(?:async )?def |\nclass |\Z)",
                  _SRC, re.S)
    assert m, f"{name} not found"
    return m.group(0)


def test_shared_recovery_lock_and_bounded_wait_exist():
    assert "_RECOVERY_RUNNER_LOCK = asyncio.Lock()" in _SRC
    assert "_RECOVERY_LOCK_WAIT_S" in _SRC and "_CERT_COOLDOWN_S" in _SRC


def test_lock_acquires_are_balanced_by_releases():
    assert _SRC.count("_RECOVERY_RUNNER_LOCK.acquire") == 2
    assert _SRC.count("_RECOVERY_RUNNER_LOCK.release") == 2
    # every release sits in a finally (survives return/exception)
    assert _SRC.count("finally:\n            _RECOVERY_RUNNER_LOCK.release()") \
        + _SRC.count("finally:\n        _RECOVERY_RUNNER_LOCK.release()") == 2


def test_certification_holds_the_budget_lock_and_bounds_the_wait():
    cert = _fn("_certify_generated_suite")
    assert "_RECOVERY_RUNNER_LOCK.acquire()" in cert
    assert "asyncio.wait_for" in cert and "_RECOVERY_LOCK_WAIT_S" in cert
    assert "_RECOVERY_RUNNER_LOCK.release()" in cert


def test_autoheal_holds_the_same_budget_lock():
    heal = _fn("_auto_heal_scenario")
    assert "_RECOVERY_RUNNER_LOCK.acquire()" in heal
    assert "recovery_runner_busy" in heal    # honest skip when the slot is held


def test_spawn_certification_is_debounced_per_artifact():
    spawn = _fn("_spawn_certification")
    assert "_CERT_LAST_DISPATCH" in spawn
    assert "_CERT_COOLDOWN_S" in spawn
    assert "debounced" in spawn


def test_client_run_paths_do_not_take_the_recovery_lock():
    """The whole point: a human clicking Run is never queued behind recovery."""
    for fn in ("playwright_run", "playwright_run_live", "heal_step"):
        body = _fn(fn)
        assert "_RECOVERY_RUNNER_LOCK" not in body, (
            f"{fn} must not take the background recovery lock")
