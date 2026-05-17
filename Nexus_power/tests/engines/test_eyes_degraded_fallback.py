"""
Architect P0 #5 regression test.

The eyes engine's `_handle_analyze_scenes` step has a graceful-degradation
branch that synthesizes a FrameAnalysis from OCR text when the vision
model (LLaVA / Ollama) fails. The pydantic schema is strict — a single
wrong field name (e.g. `timestamp` vs `timestamp_seconds`) crashes the
fallback and the workflow quarantines instead of degrading cleanly. This
was the failure mode that quarantined live workflows for hours before
the architect review caught it.

This test pins the fallback's contract:
  - When `_stage_analyze_scenes` raises, `_handle_analyze_scenes`
    returns `success=True` (degraded — not quarantined).
  - The checkpoint contains `degraded_stages=["analyze_scenes"]`.
  - `degraded_reasons["analyze_scenes"]` starts with "vision_model_failed:".
  - Each synthesised FrameAnalysis has valid field values (passes
    pydantic validation).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "engines" / "eyes-engine"))
sys.path.insert(0, str(_REPO / "sdk" / "nexus-sdk"))

# Allow stub OCR so module import doesn't fail in CI without easyocr.
os.environ.setdefault("NEXUS_ALLOW_DEGRADED_MODE", "true")


@pytest.mark.asyncio
async def test_analyze_scenes_degrades_to_success_on_vision_failure():
    """When vision model raises, handler returns success=True with
    degraded_stages set — not a fatal quarantine."""
    from app.workflow_handlers import EyesWorkflowHandlers
    from nexus_sdk.workflows import JobEnvelope
    from nexus_sdk.media.models import FrameAnalysis, ApplicationType

    # Build a minimal handler — bypass __init__ so we don't need a
    # full eyes engine. The handler only touches _engine and
    # _download_manifest / _materialize_frames_to_disk.
    handler = EyesWorkflowHandlers.__new__(EyesWorkflowHandlers)
    handler._engine = MagicMock()

    # Force the vision stage to raise — this is the failure mode the
    # production guard must absorb.
    handler._engine._stage_analyze_scenes = AsyncMock(
        side_effect=RuntimeError("ollama unreachable: connection refused"),
    )
    # Artifact store upload is also async — mock to avoid a real S3 hit.
    handler._engine._artifacts = MagicMock()
    handler._engine._artifacts.upload_bytes = AsyncMock(return_value=None)
    # Same for the upload helper if the handler invokes it directly.
    handler._upload_manifest = AsyncMock(return_value="enriched_key")

    # Fake manifests + frames so the handler reaches the analyze step.
    fake_frame_manifest = MagicMock()
    fake_scene_manifest = MagicMock(enrichment_scene_ids=[])
    fake_ocr_manifest = MagicMock()
    handler._download_manifest = AsyncMock(
        side_effect=[fake_frame_manifest, fake_scene_manifest, fake_ocr_manifest],
    )
    handler._materialize_frames_to_disk = AsyncMock(return_value=[
        {"frame_index": 0, "timestamp": 0.0, "frame_path": "/tmp/f0.png"},
        {"frame_index": 1, "timestamp": 1.5, "frame_path": "/tmp/f1.png"},
    ])

    # Patch the manifest→legacy translators to return predictable shape.
    with patch(
        "app.workflow_handlers._scenes_manifest_to_legacy",
        return_value=[
            {
                "scene_id": "s0",
                "representative_idx": 0,
                "representative_frame": {"timestamp": 0.0, "frame_path": "/tmp/f0.png"},
                "representative_ocr": ("Login page", [], 0.9),
                "merged_ocr_text": "Login page",
            },
            {
                "scene_id": "s1",
                "representative_idx": 1,
                "representative_frame": {"timestamp": 1.5, "frame_path": "/tmp/f1.png"},
                "representative_ocr": ("", [], 0.0),
                "merged_ocr_text": "",
            },
        ],
    ), patch(
        "app.workflow_handlers._ocr_manifest_to_legacy",
        return_value=[("Login page", [], 0.9), ("", [], 0.0)],
    ):
        env = JobEnvelope(
            workflow_id="wf-test",
            step_name="eyes.analyze_scenes",
            step_index=4,
            attempt=1,
            tenant_id="t1",
            session_id="s1",
            deadline_at_epoch=9999999999.0,
            heartbeat_seconds=30,
            params={"profile": "fast"},
            checkpoint={
                "frames_manifest_key": "k_frames",
                "scenes_manifest_key": "k_scenes",
                "ocr_manifest_key": "k_ocr",
                "eyes_job_id": "job-1",
            },
        )
        result = await handler._handle_analyze_scenes(env)

    # ─── Pin the contract ───────────────────────────────────────
    assert result.success is True, (
        f"vision-model failure must degrade to success, got success={result.success} "
        f"error={result.error!r}"
    )
    assert result.fatal is False
    ckpt = result.checkpoint
    assert "analyze_scenes" in (ckpt.get("degraded_stages") or []), (
        f"expected analyze_scenes in degraded_stages, got {ckpt.get('degraded_stages')}"
    )
    reason = (ckpt.get("degraded_reasons") or {}).get("analyze_scenes", "")
    assert reason.startswith("vision_model_failed:"), (
        f"expected reason to start with 'vision_model_failed:', got {reason!r}"
    )


def test_frame_analysis_fallback_fields_match_pydantic_schema():
    """The synthetic FrameAnalysis must pass pydantic validation. This is
    a unit-level smoke test for the field names the fallback uses (the
    architect-flagged bug was using `timestamp`/`scene_description` which
    raise ValidationError)."""
    from nexus_sdk.media.models import FrameAnalysis, ApplicationType

    # Same construction shape used by the fallback in workflow_handlers.py.
    fa = FrameAnalysis(
        frame_index=0,
        timestamp_seconds=1.5,
        application_type=ApplicationType.UNKNOWN,
        extracted_text="some ocr text",
        description="(visual analysis unavailable — OCR text: some ocr text)",
        ui_elements=[],
    )
    assert fa.frame_index == 0
    assert fa.timestamp_seconds == 1.5
    assert fa.application_type == ApplicationType.UNKNOWN
    assert "OCR text" in fa.description
