"""Shared fixtures for the M0.4 deploy-gate suite.

The gate's logic lives in ``Nexus_power/scripts`` — a scripts directory, not a
package — so it is put on ``sys.path`` here rather than imported through a
package path that does not exist. Keeping that in one place means a test file
never has to know where the repo root is."""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture()
def scripts_dir() -> str:
    return SCRIPTS_DIR


@pytest.fixture()
def repo_root() -> str:
    return REPO_ROOT


@pytest.fixture()
def baseline_path(tmp_path) -> str:
    """An isolated baseline. Never the real one: a test that writes the tracked
    baseline is itself the T-GT-04 defect."""
    return str(tmp_path / "golden_crawl_baseline.json")


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Point gap bookkeeping at a temp file for every test.

    Without this a test run would write the developer's real runtime state — the
    same class of leak this milestone exists to remove."""
    monkeypatch.setenv("GOLDEN_GATE_STATE", str(tmp_path / "runtime_state.json"))
