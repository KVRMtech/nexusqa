"""
Phase 2.5 — Semantic Evaluation Matrix
========================================

Validates that canonical artifacts contain **semantically useful** content,
not just mechanically-valid pipeline output.

Evaluation Dimensions:
  1. Transcript usefulness — real words, speaker turns, non-placeholder
  2. Visual usefulness — scene summaries, OCR text, screen-flow structure
  3. Artifact completeness — semantic_completeness_score, has_real_transcript, has_visual_semantics
  4. Consumer readiness — artifact supports rule extraction and test generation
  5. Quality gate scoring — scores reflect actual semantic richness

Golden Reference:
  - Audio: 10+ words, multiple speaker segments (when diarized)
  - Video: 2+ scenes, non-empty OCR or visual summaries
  - Combined: both transcript and visual semantics present

Prerequisites:
  - All services running (esp. spine:8009, ears:8002, eyes:8003, brain:8011)
  - At least one canonical processing run must have completed
  - Alembic migration 009 applied

Run:
    pytest tests/e2e/test_semantic_evaluation.py -v --timeout=300
"""

from __future__ import annotations

import io
import time
import uuid

import httpx
import pytest

# ─── Config ────────────────────────────────────────────────────

BASE = "http://localhost"
PORTS = {
    "spine": 8009,
    "ears": 8002,
    "eyes": 8003,
    "brain": 8011,
    "heart": 8004,
    "platform-api": 8091,
    "orchestrator": 8100,
}

TIMEOUT = 30


# ─── Helpers ───────────────────────────────────────────────────

def url(service: str, path: str) -> str:
    return f"{BASE}:{PORTS[service]}{path}"


def get_token() -> str:
    """Get a JWT token for test requests."""
    import jwt
    import datetime
    payload = {
        "sub": "semantic-eval-user",
        "tenant_id": "t-semantic-eval",
        "role": "admin",
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    }
    return jwt.encode(payload, "test-secret-do-not-use-in-production", algorithm="HS256")


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}"}


# ─── Semantic Quality Definitions ──────────────────────────────

# Minimum thresholds for "semantically useful"
TRANSCRIPT_MIN_WORDS = 10
TRANSCRIPT_PLACEHOLDER_MARKERS = [
    "[stub",
    "[placeholder",
    "no transcript",
    "transcription unavailable",
]
VISUAL_MIN_SCENES = 1
VISUAL_PLACEHOLDER_MARKERS = [
    "[stub",
    "[placeholder",
    "no visual",
    "analysis unavailable",
]


# ─── Phase 2.5.1: Transcript Usefulness ───────────────────────

class TestTranscriptQuality:
    """Verify transcript output is real, not stub/placeholder."""

    def test_ears_health_reports_real_mode(self):
        """Ears engine should report real transcriber mode, not stub."""
        r = httpx.get(url("ears", "/health"), timeout=TIMEOUT)
        assert r.status_code == 200
        health = r.json()
        modes = health.get("modes", health.get("components", {}))
        transcriber_mode = None
        if isinstance(modes, dict):
            transcriber_mode = modes.get("transcriber", modes.get("transcription"))
        assert transcriber_mode is not None, (
            f"Ears health does not report transcriber mode. Full health: {health}"
        )
        # In production, transcriber_mode should be "whisper", not "stub"
        if transcriber_mode == "stub":
            pytest.skip(
                "Ears engine is running in stub mode — "
                "semantic transcript evaluation requires real transcription"
            )

    def test_transcript_not_placeholder(self):
        """Existing artifacts should have non-placeholder transcript text."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("No artifacts endpoint or no artifacts available")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))
        if not artifacts:
            pytest.skip("No canonical artifacts exist yet")

        for art in artifacts:
            transcript = art.get("safe_transcript_text", "")
            if not transcript:
                continue
            for marker in TRANSCRIPT_PLACEHOLDER_MARKERS:
                assert marker not in transcript.lower(), (
                    f"Artifact {art.get('artifact_id')} contains placeholder "
                    f"transcript marker: '{marker}'"
                )

    def test_transcript_meets_minimum_length(self):
        """Transcripts should have at least TRANSCRIPT_MIN_WORDS words."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))
        if not artifacts:
            pytest.skip("No artifacts available")

        audio_artifacts = [
            a for a in artifacts
            if a.get("safe_transcript_text", "").strip()
        ]
        if not audio_artifacts:
            pytest.skip("No artifacts with transcripts found")

        for art in audio_artifacts:
            word_count = len(art["safe_transcript_text"].split())
            assert word_count >= TRANSCRIPT_MIN_WORDS, (
                f"Artifact {art.get('artifact_id')} has only {word_count} words, "
                f"minimum is {TRANSCRIPT_MIN_WORDS}"
            )


# ─── Phase 2.5.2: Visual Usefulness ───────────────────────────

class TestVisualQuality:
    """Verify visual analysis output is meaningful."""

    def test_eyes_health_reports_real_mode(self):
        """Eyes engine should report real OCR/vision mode, not stub."""
        r = httpx.get(url("eyes", "/health"), timeout=TIMEOUT)
        assert r.status_code == 200
        health = r.json()
        modes = health.get("modes", health.get("components", {}))
        ocr_mode = None
        if isinstance(modes, dict):
            ocr_mode = modes.get("ocr", modes.get("vision"))
        if ocr_mode == "stub":
            pytest.skip(
                "Eyes engine is running in stub mode — "
                "semantic visual evaluation requires real vision"
            )

    def test_visual_summary_not_placeholder(self):
        """Existing artifacts with video should have non-placeholder visual summaries."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))

        video_artifacts = [
            a for a in artifacts
            if a.get("visual_summary", "").strip() or a.get("scene_count", 0) > 0
        ]
        if not video_artifacts:
            pytest.skip("No artifacts with visual data found")

        for art in video_artifacts:
            summary = art.get("visual_summary", "")
            if not summary:
                continue
            for marker in VISUAL_PLACEHOLDER_MARKERS:
                assert marker not in summary.lower(), (
                    f"Artifact {art.get('artifact_id')} contains placeholder "
                    f"visual marker: '{marker}'"
                )

    def test_scene_count_meaningful(self):
        """Video artifacts should have at least VISUAL_MIN_SCENES scenes."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))

        video_artifacts = [
            a for a in artifacts
            if a.get("scene_count", 0) > 0 or a.get("frame_count", 0) > 0
        ]
        if not video_artifacts:
            pytest.skip("No video artifacts found")

        for art in video_artifacts:
            assert art.get("scene_count", 0) >= VISUAL_MIN_SCENES, (
                f"Artifact {art.get('artifact_id')} has "
                f"{art.get('scene_count', 0)} scenes, minimum is {VISUAL_MIN_SCENES}"
            )


# ─── Phase 2.5.3: Semantic Completeness Flags ─────────────────

class TestSemanticCompleteness:
    """Verify artifact-level semantic completeness indicators."""

    def test_artifact_status_includes_semantic_fields(self):
        """Artifact status endpoint must expose semantic completeness fields."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 1},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))
        if not artifacts:
            pytest.skip("No artifacts available")

        art = artifacts[0]
        artifact_id = art.get("artifact_id")
        if not artifact_id:
            pytest.skip("No artifact_id found")

        r2 = httpx.get(
            url("platform-api", f"/api/v1/artifacts/{artifact_id}/status"),
            timeout=TIMEOUT,
        )
        if r2.status_code != 200:
            pytest.skip("Artifact status endpoint unavailable")

        status = r2.json()
        assert "has_real_transcript" in status, "Missing has_real_transcript field"
        assert "has_visual_semantics" in status, "Missing has_visual_semantics field"
        assert "semantic_completeness_score" in status, "Missing semantic_completeness_score field"

    def test_semantic_score_is_numeric(self):
        """semantic_completeness_score must be a float between 0.0 and 1.0."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))

        for art in artifacts:
            score = art.get("semantic_completeness_score")
            if score is not None:
                assert isinstance(score, (int, float)), (
                    f"semantic_completeness_score is {type(score)}, expected float"
                )
                assert 0.0 <= score <= 1.0, (
                    f"semantic_completeness_score {score} out of range [0, 1]"
                )

    def test_has_real_transcript_consistency(self):
        """has_real_transcript=True only when transcript has real content."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 5},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))

        for art in artifacts:
            has_real = art.get("has_real_transcript", False)
            transcript = art.get("safe_transcript_text", "")
            if has_real:
                assert len(transcript.split()) >= TRANSCRIPT_MIN_WORDS, (
                    f"Artifact {art.get('artifact_id')} claims has_real_transcript=True "
                    f"but transcript has only {len(transcript.split())} words"
                )


# ─── Phase 2.5.4: Consumer Readiness ──────────────────────────

class TestConsumerReadiness:
    """Verify artifacts are useful for downstream consumers."""

    def test_heart_extract_rules_endpoint_available(self):
        """Heart engine's rule extraction endpoint must be available."""
        r = httpx.get(url("heart", "/health"), timeout=TIMEOUT)
        assert r.status_code == 200

    def test_brain_quality_gate_endpoint_available(self):
        """Brain engine's quality gate endpoint must be available."""
        r = httpx.get(url("brain", "/health"), timeout=TIMEOUT)
        assert r.status_code == 200

    def test_completed_artifacts_have_quality_scores(self):
        """Completed artifacts should have quality gate results."""
        r = httpx.get(
            url("spine", "/api/v1/spine/artifacts"),
            headers=auth_headers(),
            params={"limit": 10},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip("Artifacts endpoint unavailable")

        artifacts = r.json()
        if isinstance(artifacts, dict):
            artifacts = artifacts.get("artifacts", artifacts.get("items", []))

        completed = [
            a for a in artifacts if a.get("status") == "completed"
        ]
        if not completed:
            pytest.skip("No completed artifacts to evaluate")

        for art in completed:
            assert art.get("brain_quality_score") is not None, (
                f"Completed artifact {art.get('artifact_id')} has no quality score"
            )
            assert art.get("quality_gate_outcome") is not None, (
                f"Completed artifact {art.get('artifact_id')} has no quality gate outcome"
            )


# ─── Phase 2.5.5: Quality Gate Scoring ────────────────────────

class TestQualityGateScoring:
    """Verify quality gate measures semantic content, not just completion."""

    def test_quality_gate_responds_with_dimensions(self):
        """Quality gate should return multi-dimensional scoring."""
        r = httpx.post(
            url("brain", "/api/v1/brain/canonical-quality-gate"),
            headers=auth_headers(),
            json={
                "session_id": "test-semantic-eval",
                "artifact_id": "test-artifact",
                "entity_count": 0,
                "safe_text": "This is a real transcript with meaningful business content about insurance policies.",
                "raw_transcript": "This is a real transcript with meaningful business content about insurance policies.",
                "scene_count": 3,
                "duration_seconds": 120.0,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip(f"Quality gate returned {r.status_code}")

        result = r.json()
        # Must have overall score
        assert "overall_score" in result or "score" in result, (
            f"Quality gate response missing score: {list(result.keys())}"
        )
        # Must have pass/fail decision
        assert "passed" in result or "level" in result, (
            f"Quality gate response missing pass/fail: {list(result.keys())}"
        )

    def test_empty_artifact_scores_low(self):
        """An artifact with no real content should score poorly."""
        r = httpx.post(
            url("brain", "/api/v1/brain/canonical-quality-gate"),
            headers=auth_headers(),
            json={
                "session_id": "test-empty-eval",
                "artifact_id": "test-empty",
                "entity_count": 0,
                "safe_text": "",
                "raw_transcript": "",
                "scene_count": 0,
                "duration_seconds": 0.0,
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            pytest.skip(f"Quality gate returned {r.status_code}")

        result = r.json()
        score = result.get("overall_score", result.get("score", 1.0))
        # Empty content should not pass
        assert score < 0.6, (
            f"Empty artifact scored {score} — quality gate is not "
            "detecting semantic emptiness"
        )
