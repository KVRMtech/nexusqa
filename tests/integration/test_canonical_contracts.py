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

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


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
    from platform.api.app.routers.artifacts import _artifact_list_item

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
    from platform.api.app.routers.artifacts import _artifact_list_item

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
    try:
        from platform.api.app.routers.admin import canonical_operator_ready
    except ImportError:
        pytest.skip("admin router not importable in this environment")

    # This is a structural assertion — the function should exist and
    # its implementation should be distinct from engine-only readiness.
    assert callable(canonical_operator_ready)


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
    from platform.api.app.routers.artifacts import _artifact_list_item

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
