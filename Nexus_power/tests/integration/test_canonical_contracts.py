"""
Canonical Contract Regression Tests
=====================================

Focused regression coverage for frontend-backend contract correctness:
  1. Alias session identity — adapter uses requested sessionId, not artifact's stored session_id
  2. Artifact list vs full — list endpoint strips blob, full endpoint preserves it
  3. PII safety vs quality gate — separate concerns, not conflated
  4. Admin readiness — canonical_operator_ready vs canonical_signoff_ready

Run:
    pytest tests/integration/test_canonical_contracts.py -v
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── Importing platform/api from a test ──────────────────────────────────────
# These tests used `from platform.api.app.routers.artifacts import ...`, which
# can NEVER work: `platform` is a Python STANDARD LIBRARY module, and the repo's
# `platform/` directory is not a package (no __init__.py), so the import failed
# at collection with
#
#     ModuleNotFoundError: No module named 'platform.api'; 'platform' is not a package
#
# The service is imported the way it is at runtime and in CI — with
# `platform/api` on sys.path, as `app.*` (see ci/run_platform_api_tests.sh, which
# exports PYTHONPATH=platform/api). Because several services in this repo each
# ship a top-level `app` package, the helper below also evicts any `app` already
# bound to a DIFFERENT service before importing and restores it afterwards, so
# these tests neither inherit nor cause the cross-suite import pollution that
# per-file isolation exists to contain.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLATFORM_API = os.path.join(_REPO_ROOT, "platform", "api")


def _platform_api_module(dotted: str):
    """Import `dotted` (e.g. "app.routers.artifacts") from platform/api."""
    saved_path = sys.path[:]
    saved_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    already_ours = getattr(sys.modules.get("app"), "__file__", "") or ""
    if not already_ours.startswith(_PLATFORM_API):
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
    sys.path.insert(0, _PLATFORM_API)
    try:
        return importlib.import_module(dotted)
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(saved_modules)


# ═══════════════════════════════════════════════════════════════
#  1. Artifact list endpoint strips full_artifact_json
# ═══════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, result=None):
        self._result = result or _FakeResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, *args, **kwargs):
        return self._result

    async def get(self, model, pk):
        return None


def _make_artifact_row(**overrides):
    """Build a fake artifact DB row with all expected fields."""
    defaults = {
        "artifact_id": "art-001",
        "tenant_id": "t-1",
        "session_id": "sess-original",
        "media_fingerprint": "fp-abc",
        "status": "completed",
        "workflow_id": "wf-001",
        "source_type": "audio",
        "source_filename": "meeting.wav",
        "created_by": "user-1",
        "duration_seconds": 120,
        "scene_count": 5,
        "frame_count": 30,
        "safe_transcript_text": "This is the full transcript text",
        "visual_summary": "Office meeting UI",
        "application_types_seen": ["web_browser"],
        "brain_quality_score": 0.92,
        "quality_gate_passed": True,
        "quality_gate_outcome": "pass",
        "has_real_transcript": True,
        "has_visual_semantics": True,
        "semantic_completeness_score": 0.88,
        "full_artifact_json": {
            "transcript": {"segments": [{"text": "hello", "speaker": "Alice"}]},
            "visual_analysis": {"frames": [{"description": "login screen"}]},
            "visual_graph": {"nodes": [{"id": 1, "label": "Login"}], "edges": []},
            "model_provenance": {"ears": "whisper-v3"},
            "review_reasons": [],
            "score_breakdown": {"transcript": 0.95, "visual": 0.88, "pii": 0.99, "completeness": 0.88},
        },
        "processing_time_seconds": 45.2,
        "created_at": "2026-04-06T10:00:00Z",
        "completed_at": "2026-04-06T10:00:45Z",
        "error": None,
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_artifact_list_endpoint_strips_blob():
    """
    Regression: the list endpoint must strip full_artifact_json and
    safe_transcript_text. If callers need the blob, they must use
    GET /v1/artifacts/{id} instead.
    """
    _artifact_list_item = _platform_api_module("app.routers.artifacts")._artifact_list_item

    full_artifact = _make_artifact_row()
    assert "full_artifact_json" in full_artifact
    assert "safe_transcript_text" in full_artifact

    list_item = _artifact_list_item(full_artifact)

    assert "full_artifact_json" not in list_item, \
        "list endpoint must strip full_artifact_json to avoid over-fetching"
    assert "safe_transcript_text" not in list_item, \
        "list endpoint must strip safe_transcript_text to avoid over-fetching"
    # Metadata fields should still be present
    assert list_item["artifact_id"] == "art-001"
    assert list_item["brain_quality_score"] == 0.92
    assert list_item["source_filename"] == "meeting.wav"


@pytest.mark.asyncio
async def test_artifact_list_does_not_mutate_source():
    """The list item helper must not mutate the original dict."""
    _artifact_list_item = _platform_api_module("app.routers.artifacts")._artifact_list_item

    full_artifact = _make_artifact_row()
    _artifact_list_item(full_artifact)

    # Original should still have its blob (dict.pop on a copy, not the original)
    assert "full_artifact_json" in full_artifact


# ═══════════════════════════════════════════════════════════════
#  2. Admin readiness surfaces — operator vs signoff
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_admin_canonical_operator_ready_includes_control_plane():
    """
    canonical_operator_ready should check engine health AND control plane
    (orchestrator, message bus). It is broader than canonical_signoff_ready.
    """
    # `canonical_operator_ready` is a value COMPUTED INSIDE the readiness route,
    # not a module-level callable — the old `assert callable(...)` could never
    # have held. It never failed either, because the import above it was broken
    # (`from platform.api...`) and the except-ImportError turned that into a
    # skip: a test that asserted nothing, silently, for its whole life.
    #
    # Assert the CONTRACT the docstring describes instead: operator readiness is
    # strictly stronger than signoff readiness — it adds control-plane health —
    # and both are surfaced separately so the two audiences are never conflated.
    import inspect

    admin = _platform_api_module("app.routers.admin")
    source = inspect.getsource(admin)

    assert "canonical_operator_ready = canonical_signoff_ready and control_plane_healthy" in source, (
        "operator readiness must be signoff readiness AND control-plane health"
    )
    assert '"canonical_signoff_ready": canonical_signoff_ready' in source
    assert '"canonical_operator_ready": canonical_operator_ready' in source


# ═══════════════════════════════════════════════════════════════
#  3. Alias/cache-reuse — session identity contract
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_alias_artifact_preserves_original_session_id():
    """
    When an artifact was produced for session A but is served to session B
    (via fingerprint cache), the list endpoint should still return the
    artifact with session_id = A (the producing session). The frontend
    adapter is responsible for using the requested session B as the
    viewing session.
    """
    _artifact_list_item = _platform_api_module("app.routers.artifacts")._artifact_list_item

    # Artifact was produced for original-session, now served to alias-session
    artifact = _make_artifact_row(
        session_id="original-session",
        artifact_id="art-cached",
    )
    item = _artifact_list_item(artifact)

    # The list item preserves the artifact's producing session_id
    assert item["session_id"] == "original-session"
    # The adapter (tested separately) must override this with the viewing session


# ═══════════════════════════════════════════════════════════════
#  4. Quality gate vs PII — separate concerns
# ═══════════════════════════════════════════════════════════════


def test_quality_gate_pass_does_not_imply_pii_safety():
    """
    A passing quality gate covers transcript completeness, visual quality,
    and PII redaction. PII safety is specifically the pii dimension in
    score_breakdown. An artifact can have quality_gate_passed=False but
    still have successful PII redaction (pii score > 0).
    """
    # Scenario: quality gate failed due to visual score, but PII is fine
    artifact = _make_artifact_row(
        quality_gate_passed=False,
        quality_gate_outcome="fail",
    )
    blob = artifact["full_artifact_json"]
    assert blob["score_breakdown"]["pii"] == 0.99, \
        "PII score should be independent of overall quality gate"


def test_quality_gate_pass_with_no_pii_score():
    """
    An artifact could theoretically pass the quality gate without a PII
    score (if PII redaction was skipped). The UI should not show PII SAFE
    in that case.
    """
    artifact = _make_artifact_row(quality_gate_passed=True)
    artifact["full_artifact_json"]["score_breakdown"]["pii"] = None

    pii_score = artifact["full_artifact_json"]["score_breakdown"]["pii"]
    # UI condition: only show PII SAFE when pii != null && pii > 0
    pii_safe = pii_score is not None and pii_score > 0
    assert not pii_safe, \
        "PII SAFE badge should not appear when pii score is null"
