"""
QA Orchestrator — Modular Sub-package Tests.

Tests the config, models, store (in-memory mode), and pipeline helpers
that were refactored from the monolithic qa-orchestrator/main.py.
"""

import pytest
import sys
import os
import json
from unittest.mock import MagicMock, AsyncMock, patch


# ═══════════════════════════════════════════════════════════════
# Config Module
# ═══════════════════════════════════════════════════════════════


class TestOrchestratorConfig:
    """Test OrchestratorConfig from app.config."""

    def test_import(self):
        from app.config import OrchestratorConfig
        assert OrchestratorConfig is not None

    def test_defaults(self):
        from app.config import OrchestratorConfig
        cfg = OrchestratorConfig()
        assert cfg.ears_url == "http://localhost:8002"
        assert cfg.eyes_url == "http://localhost:8003"
        assert cfg.heart_url == "http://localhost:8004"
        assert cfg.backbone_url == "http://localhost:8005"
        assert cfg.shield_url == "http://localhost:8001"
        assert cfg.nerves_url == "http://localhost:8006"
        assert cfg.legs_url == "http://localhost:8007"
        assert cfg.hands_url == "http://localhost:8008"
        assert cfg.spine_url == "http://localhost:8009"
        assert cfg.mouth_url == "http://localhost:8010"
        assert cfg.redis_url == "redis://localhost:6379/0"

    def test_jwt_secret_default(self):
        from app.config import OrchestratorConfig
        cfg = OrchestratorConfig()
        assert cfg.jwt_secret == "dev-jwt-secret-change-me"

    def test_custom_urls(self):
        from app.config import OrchestratorConfig
        cfg = OrchestratorConfig(
            ears_url="http://ears:8002",
            backbone_url="http://backbone:8005",
        )
        assert cfg.ears_url == "http://ears:8002"
        assert cfg.backbone_url == "http://backbone:8005"


# ═══════════════════════════════════════════════════════════════
# Models Module
# ═══════════════════════════════════════════════════════════════


class TestPipelineStage:
    """Test PipelineStage enum from app.models."""

    def test_import(self):
        from app.models import PipelineStage
        assert PipelineStage is not None

    def test_all_stages_present(self):
        from app.models import PipelineStage
        expected = [
            "UPLOADED", "INGESTING_DOCUMENTS", "SHIELDING", "TRANSCRIBING",
            "VISUAL_ANALYZING", "EXTRACTING_RULES", "GENERATING_TESTS",
            "GENERATING_TEST_DATA", "STORING_KNOWLEDGE", "EXECUTING_TESTS",
            "GENERATING_REPORTS", "NOTIFYING", "COMPLETED", "FAILED",
        ]
        for stage_name in expected:
            assert hasattr(PipelineStage, stage_name)

    def test_stage_values(self):
        from app.models import PipelineStage
        assert PipelineStage.UPLOADED.value == "uploaded"
        assert PipelineStage.COMPLETED.value == "completed"
        assert PipelineStage.FAILED.value == "failed"

    def test_stage_count(self):
        from app.models import PipelineStage
        assert len(PipelineStage) == 14


class TestKTSession:
    """Test KTSession model from app.models."""

    def test_import(self):
        from app.models import KTSession
        assert KTSession is not None

    def test_create_minimal(self):
        from app.models import KTSession
        session = KTSession(session_id="s-001", tenant_id="t-001")
        assert session.session_id == "s-001"
        assert session.tenant_id == "t-001"
        assert session.name == ""
        assert session.pipeline_stage.value == "uploaded"
        assert session.stages_completed == []

    def test_counters_default_zero(self):
        from app.models import KTSession
        session = KTSession(session_id="s-002", tenant_id="t-001")
        assert session.rules_extracted == 0
        assert session.tests_generated == 0
        assert session.tests_executed == 0
        assert session.tests_passed == 0
        assert session.tests_failed == 0
        assert session.documents_ingested == 0
        assert session.test_data_records == 0
        assert session.reports_generated == 0

    def test_set_pipeline_stage(self):
        from app.models import KTSession, PipelineStage
        session = KTSession(session_id="s-003", tenant_id="t-001")
        session.pipeline_stage = PipelineStage.EXTRACTING_RULES
        assert session.pipeline_stage == PipelineStage.EXTRACTING_RULES

    def test_optional_fields(self):
        from app.models import KTSession
        session = KTSession(session_id="s-004", tenant_id="t-001")
        assert session.audio_job_id is None
        assert session.video_job_id is None
        assert session.error is None

    def test_serialization(self):
        from app.models import KTSession
        session = KTSession(
            session_id="s-005",
            tenant_id="t-001",
            name="Test Session",
        )
        data = session.model_dump()
        assert data["session_id"] == "s-005"
        assert data["name"] == "Test Session"
        assert data["pipeline_stage"] == "uploaded"

    def test_json_roundtrip(self):
        from app.models import KTSession
        session = KTSession(
            session_id="s-006",
            tenant_id="t-001",
            name="Roundtrip",
            rules_extracted=5,
        )
        json_str = session.model_dump_json()
        restored = KTSession.model_validate_json(json_str)
        assert restored.session_id == "s-006"
        assert restored.rules_extracted == 5


class TestRequestModels:
    """Test request/response models from app.models."""

    def test_create_session_request(self):
        from app.models import CreateSessionRequest
        req = CreateSessionRequest(
            tenant_id="t-001",
            name="New Session",
        )
        assert req.tenant_id == "t-001"
        assert req.description == ""

    def test_run_pipeline_request(self):
        from app.models import RunPipelineRequest
        req = RunPipelineRequest(tenant_id="t-001")
        assert req.sut_url is None
        assert req.skip_test_execution is False
        assert req.notify_on_complete is True

    def test_session_summary(self):
        from app.models import SessionSummary, KTSession
        session = KTSession(session_id="s-007", tenant_id="t-001")
        summary = SessionSummary(session=session)
        assert summary.rules == []
        assert summary.test_cases == []
        assert summary.test_results == []
        assert summary.timeline == []


# ═══════════════════════════════════════════════════════════════
# Store Module (In-Memory Mode)
# ═══════════════════════════════════════════════════════════════


class TestRedisSessionStore:
    """Test RedisSessionStore using in-memory fallback (no Redis)."""

    def test_import(self):
        from app.store import RedisSessionStore
        assert RedisSessionStore is not None

    def test_init(self):
        from app.store import RedisSessionStore
        store = RedisSessionStore()
        assert store._redis is None

    @pytest.mark.asyncio
    async def test_save_and_get_session(self):
        from app.store import RedisSessionStore
        from app.models import KTSession
        store = RedisSessionStore()

        session = KTSession(session_id="s-100", tenant_id="t-001", name="Test")
        await store.save_session(session)

        result = await store.get_session("s-100")
        assert result is not None
        assert result.session_id == "s-100"
        assert result.name == "Test"

    @pytest.mark.asyncio
    async def test_get_session_not_found(self):
        from app.store import RedisSessionStore
        store = RedisSessionStore()
        result = await store.get_session("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_sessions_by_tenant(self):
        from app.store import RedisSessionStore
        from app.models import KTSession
        store = RedisSessionStore()

        await store.save_session(KTSession(session_id="s-201", tenant_id="t-a"))
        await store.save_session(KTSession(session_id="s-202", tenant_id="t-a"))
        await store.save_session(KTSession(session_id="s-203", tenant_id="t-b"))

        results = await store.list_sessions("t-a")
        assert len(results) == 2
        assert all(s.tenant_id == "t-a" for s in results)

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self):
        from app.store import RedisSessionStore
        store = RedisSessionStore()
        results = await store.list_sessions("nonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_save_and_get_data(self):
        from app.store import RedisSessionStore
        store = RedisSessionStore()

        data = {"rules": ["rule1", "rule2"], "count": 42}
        await store.save_data("s-300", data)

        result = await store.get_data("s-300")
        assert result["count"] == 42
        assert len(result["rules"]) == 2

    @pytest.mark.asyncio
    async def test_get_data_not_found(self):
        from app.store import RedisSessionStore
        store = RedisSessionStore()
        result = await store.get_data("nonexistent")
        assert result == {}

    @pytest.mark.asyncio
    async def test_overwrite_session(self):
        from app.store import RedisSessionStore
        from app.models import KTSession, PipelineStage
        store = RedisSessionStore()

        session = KTSession(session_id="s-400", tenant_id="t-001", name="Original")
        await store.save_session(session)

        session.name = "Updated"
        session.pipeline_stage = PipelineStage.COMPLETED
        await store.save_session(session)

        result = await store.get_session("s-400")
        assert result.name == "Updated"
        assert result.pipeline_stage == PipelineStage.COMPLETED


# ═══════════════════════════════════════════════════════════════
# Pipeline Module
# ═══════════════════════════════════════════════════════════════


class TestPipeline:
    """Test pipeline utilities from app.pipeline."""

    def test_import(self):
        from app.pipeline import run_full_pipeline, _poll_ears_job, _poll_legs_job, _log_timeline
        assert callable(run_full_pipeline)
        assert callable(_poll_ears_job)
        assert callable(_poll_legs_job)
        assert callable(_log_timeline)

    @pytest.mark.asyncio
    async def test_log_timeline(self):
        """_log_timeline should add an entry to the data dict's timeline."""
        from app.pipeline import _log_timeline
        from app.store import RedisSessionStore
        store = RedisSessionStore()

        await store.save_data("s-500", {"timeline": []})
        await _log_timeline(store, "s-500", "test_stage", "Test detail message")

        data = await store.get_data("s-500")
        assert len(data["timeline"]) == 1
        entry = data["timeline"][0]
        assert entry["event"] == "test_stage"
        assert entry["detail"] == "Test detail message"
        assert "timestamp" in entry


# ═══════════════════════════════════════════════════════════════
# App Package Re-exports
# ═══════════════════════════════════════════════════════════════


class TestOrchestratorAppReExports:
    """Verify app package re-exports all public symbols."""

    def test_config(self):
        from app import OrchestratorConfig
        assert OrchestratorConfig is not None

    def test_models(self):
        from app import PipelineStage, KTSession, CreateSessionRequest, RunPipelineRequest, SessionSummary
        assert PipelineStage is not None
        assert KTSession is not None

    def test_store(self):
        from app import RedisSessionStore
        assert RedisSessionStore is not None

    def test_pipeline(self):
        from app import run_full_pipeline
        assert callable(run_full_pipeline)


# ═══════════════════════════════════════════════════════════════
# Main Entry-point
# ═══════════════════════════════════════════════════════════════


class TestOrchestratorMainEntryPoint:
    """Test that main.py properly creates the FastAPI app."""

    def test_app_import(self):
        from main import app
        assert app is not None

    def test_app_title(self):
        from main import app
        assert "orchestrator" in app.title.lower() or "nexus" in app.title.lower()

    def test_app_version(self):
        from main import app
        assert app.version == "0.2.0"

    def test_backward_compat_reexports(self):
        """Main.py should re-export pipeline helpers for backward compat."""
        from main import _poll_ears_job, _poll_legs_job, _log_timeline
        assert callable(_poll_ears_job)
        assert callable(_poll_legs_job)
        assert callable(_log_timeline)
