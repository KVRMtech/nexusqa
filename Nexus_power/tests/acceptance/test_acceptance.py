"""Pytest entry-point for the acceptance suite.

Each fixture file under ``tests/acceptance/fixtures`` becomes one
parametrized test case.  Add a fixture by:

  1. Drop the recording in the operator-managed media volume (out of
     repo).  Set ``video_path`` in the YAML to a path the test runner
     can resolve, OR pre-process the recording and set ``artifact_id``
     to use the cached output.
  2. Annotate ground truth in the YAML's ``expected_actions`` block.
  3. Run ``pytest tests/acceptance``.

The default behaviour fetches actual steps from the live platform API
using the operator-supplied JWT in ``NEXUS_ACCEPTANCE_TOKEN``.  CI
environments without API access can override the fetcher via the
``acceptance_fetch_steps`` pytest fixture so the suite runs against
canned data.
"""
from __future__ import annotations

import json
import os
from typing import Callable

import pytest

from .harness import (
    FixtureManifest,
    FixtureResult,
    evaluate_fixture,
    load_all_fixtures,
)


_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ─── Fixture discovery ──────────────────────────────────────────────────────

def _discover_fixtures() -> list[FixtureManifest]:
    """Load every fixture YAML at collection time."""
    try:
        return load_all_fixtures(_FIXTURES_DIR)
    except ImportError:
        # PyYAML not installed — collect zero fixtures so the suite is
        # skipped rather than erroring at import time.
        return []
    except Exception as exc:  # pragma: no cover — config error path
        pytest.skip(f"acceptance fixtures unavailable: {exc}")
        return []


_FIXTURES = _discover_fixtures()


# ─── Step fetcher ───────────────────────────────────────────────────────────

def _default_fetch_steps(fixture: FixtureManifest) -> list[dict]:
    """Default step fetcher — calls the platform API.

    Requires:
      NEXUS_ACCEPTANCE_API_BASE   default: http://localhost:8091
      NEXUS_ACCEPTANCE_TOKEN      JWT bearer token

    Skips with a clear reason when neither API access nor a cached
    artifact_id is available.  The fixture's ``artifact_id`` field is
    used directly when set; otherwise a video_path-based re-process is
    out of scope for this default fetcher (operators should pre-process
    and supply artifact_id, or override the fixture below).
    """
    artifact_id = fixture.artifact_id
    if not artifact_id:
        pytest.skip(
            f"{fixture.name}: no artifact_id in fixture; pre-process the "
            "video and set artifact_id, or override acceptance_fetch_steps."
        )
        return []

    api_base = os.environ.get("NEXUS_ACCEPTANCE_API_BASE", "http://localhost:8091")
    token = os.environ.get("NEXUS_ACCEPTANCE_TOKEN", "")
    if not token:
        pytest.skip(
            f"{fixture.name}: NEXUS_ACCEPTANCE_TOKEN not set; "
            "supply a JWT or override acceptance_fetch_steps."
        )
        return []

    try:
        import httpx  # type: ignore
    except ImportError:
        pytest.skip(f"{fixture.name}: httpx not installed")
        return []

    url = f"{api_base.rstrip('/')}/api/v1/artifacts/{artifact_id}/evidence-steps"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            data = resp.json()
            return list(data.get("steps") or [])
    except Exception as exc:
        pytest.skip(f"{fixture.name}: API fetch failed: {exc}")
        return []


@pytest.fixture
def acceptance_fetch_steps() -> Callable[[FixtureManifest], list[dict]]:
    """Pytest fixture allowing a CI run to inject a custom step fetcher.

    Override in conftest::

        @pytest.fixture
        def acceptance_fetch_steps():
            return lambda fixture: load_canned_steps_for(fixture.name)
    """
    return _default_fetch_steps


# ─── Parametrized acceptance tests ──────────────────────────────────────────

if not _FIXTURES:
    @pytest.mark.skip(reason="no acceptance fixtures present yet")
    def test_acceptance_no_fixtures():
        """Placeholder — populated tests appear once fixtures are dropped in."""


@pytest.mark.parametrize(
    "fixture", _FIXTURES,
    ids=[f.name for f in _FIXTURES],
)
def test_fixture_meets_thresholds(
    fixture: FixtureManifest,
    acceptance_fetch_steps: Callable[[FixtureManifest], list[dict]],
):
    """One pytest case per fixture: assert all four thresholds pass."""
    actual_steps = acceptance_fetch_steps(fixture)
    result = evaluate_fixture(fixture, actual_steps)
    assert result.passed, _diagnostic_message(result)


def _diagnostic_message(result: FixtureResult) -> str:
    """Render an actionable diff when a fixture fails its thresholds."""
    lines = [result.summary_line(), ""]
    if result.missing_count:
        lines.append(f"Missing actions ({result.missing_count}):")
        for m in result.matches:
            if m.actual_step is None:
                e = m.expected
                lines.append(
                    f"  - {e.action_kind} @ [{e.timestamp_ms_min}-{e.timestamp_ms_max}] "
                    f"target~={e.target_label_contains!r}"
                )
        lines.append("")
    if result.spurious_steps:
        lines.append(f"Spurious steps ({len(result.spurious_steps)}):")
        for step in result.spurious_steps[:10]:
            lines.append(
                f"  - {step.get('action_kind')!r} @ {step.get('start_ms')} ms "
                f"target={step.get('target_label')!r}"
            )
    return "\n".join(lines)
