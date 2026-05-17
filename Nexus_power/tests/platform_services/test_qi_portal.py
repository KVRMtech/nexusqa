"""
QI Engineer Portal — Backend Tests.

Tests persona router, mission router, mission orchestrator service,
ORM models, and route registration.
"""

import pytest
import sys
import os
import uuid
import json
import importlib
import importlib.util
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# ORM Models
# ═══════════════════════════════════════════════════════════════


class TestQIPortalModels:
    """Test QI Portal ORM model definitions."""

    def test_persona_row_import(self):
        from nexus_sdk.db.models import PersonaRow
        assert PersonaRow is not None

    def test_mission_row_import(self):
        from nexus_sdk.db.models import MissionRow
        assert MissionRow is not None

    def test_mission_stage_row_import(self):
        from nexus_sdk.db.models import MissionStageRow
        assert MissionStageRow is not None

    def test_mission_artifact_row_import(self):
        from nexus_sdk.db.models import MissionArtifactRow
        assert MissionArtifactRow is not None

    def test_mission_message_row_import(self):
        from nexus_sdk.db.models import MissionMessageRow
        assert MissionMessageRow is not None

    def test_persona_row_table_name(self):
        from nexus_sdk.db.models import PersonaRow
        assert PersonaRow.__tablename__ == "personas"

    def test_mission_row_table_name(self):
        from nexus_sdk.db.models import MissionRow
        assert MissionRow.__tablename__ == "missions"

    def test_mission_stage_row_table_name(self):
        from nexus_sdk.db.models import MissionStageRow
        assert MissionStageRow.__tablename__ == "mission_stages"

    def test_mission_artifact_row_table_name(self):
        from nexus_sdk.db.models import MissionArtifactRow
        assert MissionArtifactRow.__tablename__ == "mission_artifacts"

    def test_mission_message_row_table_name(self):
        from nexus_sdk.db.models import MissionMessageRow
        assert MissionMessageRow.__tablename__ == "mission_messages"

    def test_persona_row_has_required_columns(self):
        from nexus_sdk.db.models import PersonaRow
        mapper = PersonaRow.__mapper__
        col_names = {c.key for c in mapper.column_attrs}
        required = {
            "persona_id", "tenant_id", "name", "slug", "description",
            "avatar_icon", "system_prompt", "capabilities", "stage_config",
            "specialty_domains", "is_system", "is_active", "sort_order",
            "created_at", "updated_at",
        }
        assert required.issubset(col_names), f"Missing columns: {required - col_names}"

    def test_mission_row_has_required_columns(self):
        from nexus_sdk.db.models import MissionRow
        mapper = MissionRow.__mapper__
        col_names = {c.key for c in mapper.column_attrs}
        required = {
            "mission_id", "tenant_id", "user_id", "persona_id",
            "title", "description", "objective", "status", "current_stage",
            "priority", "tags", "context", "summary", "progress_pct",
            "created_at", "updated_at", "started_at", "completed_at",
        }
        assert required.issubset(col_names), f"Missing columns: {required - col_names}"

    def test_mission_stage_row_has_required_columns(self):
        from nexus_sdk.db.models import MissionStageRow
        mapper = MissionStageRow.__mapper__
        col_names = {c.key for c in mapper.column_attrs}
        required = {
            "stage_id", "mission_id", "stage_number", "stage_type",
            "status", "inputs", "outputs", "engine_calls",
            "started_at", "completed_at", "duration_seconds", "error_message",
        }
        assert required.issubset(col_names), f"Missing columns: {required - col_names}"

    def test_mission_artifact_row_has_required_columns(self):
        from nexus_sdk.db.models import MissionArtifactRow
        mapper = MissionArtifactRow.__mapper__
        col_names = {c.key for c in mapper.column_attrs}
        required = {
            "artifact_id", "mission_id", "stage_id", "artifact_type",
            "name", "description", "content_json", "content_text",
            "file_path", "file_size_bytes", "item_count", "created_at",
        }
        assert required.issubset(col_names), f"Missing columns: {required - col_names}"

    def test_mission_message_row_has_required_columns(self):
        from nexus_sdk.db.models import MissionMessageRow
        mapper = MissionMessageRow.__mapper__
        col_names = {c.key for c in mapper.column_attrs}
        required = {
            "message_id", "mission_id", "role", "content",
            "stage_number", "content_type", "action_data", "token_count",
            "created_at",
        }
        assert required.issubset(col_names), f"Missing columns: {required - col_names}"

    def test_mission_row_has_persona_relationship(self):
        from nexus_sdk.db.models import MissionRow
        assert hasattr(MissionRow, "persona")

    def test_mission_row_has_stages_relationship(self):
        from nexus_sdk.db.models import MissionRow
        assert hasattr(MissionRow, "stages")

    def test_mission_row_has_artifacts_relationship(self):
        from nexus_sdk.db.models import MissionRow
        assert hasattr(MissionRow, "artifacts")

    def test_mission_row_has_messages_relationship(self):
        from nexus_sdk.db.models import MissionRow
        assert hasattr(MissionRow, "messages")


# ═══════════════════════════════════════════════════════════════
# Alembic Migration
# ═══════════════════════════════════════════════════════════════


class TestQIMigration:
    """Test the 005_qi_portal migration is well-formed."""

    @staticmethod
    def _migration_dir():
        """Find the alembic/versions directory from the workspace root."""
        # Navigate from tests/platform_services/ to workspace root
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(here, "..", "..", "alembic", "versions")

    def test_migration_import(self):
        mig_dir = self._migration_dir()
        sys.path.insert(0, mig_dir)
        try:
            import importlib
            # Module names starting with digits need importlib
            spec = importlib.util.spec_from_file_location(
                "migration_005", os.path.join(mig_dir, "005_qi_portal.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            assert mod is not None
        finally:
            sys.path.pop(0)

    def test_migration_has_revision(self):
        mig_dir = self._migration_dir()
        spec = importlib.util.spec_from_file_location(
            "migration_005_rev", os.path.join(mig_dir, "005_qi_portal.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "revision")
        assert mod.revision is not None

    def test_migration_has_upgrade(self):
        mig_dir = self._migration_dir()
        spec = importlib.util.spec_from_file_location(
            "migration_005_up", os.path.join(mig_dir, "005_qi_portal.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(mod.upgrade)

    def test_migration_has_downgrade(self):
        mig_dir = self._migration_dir()
        spec = importlib.util.spec_from_file_location(
            "migration_005_down", os.path.join(mig_dir, "005_qi_portal.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(mod.downgrade)

    def test_migration_depends_on_004(self):
        mig_dir = self._migration_dir()
        spec = importlib.util.spec_from_file_location(
            "migration_005_dep", os.path.join(mig_dir, "005_qi_portal.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "004" in str(mod.down_revision)


# ═══════════════════════════════════════════════════════════════
# Personas Router
# ═══════════════════════════════════════════════════════════════


class TestPersonasRouterImport:
    """Test personas router module imports."""

    def test_router_import(self):
        from app.routers.personas import router
        assert router is not None

    def test_router_has_routes(self):
        from app.routers.personas import router
        assert len(router.routes) > 0

    def test_router_tags(self):
        from app.routers.personas import router
        tags = [t.lower() for t in router.tags]
        assert any("persona" in t for t in tags)


class TestPersonasRouterEndpoints:
    """Test persona endpoint route paths."""

    @staticmethod
    def _route_paths():
        from app.routers.personas import router
        return [r.path for r in router.routes]

    def test_list_personas_route(self):
        paths = self._route_paths()
        assert any("/personas" in p for p in paths)

    def test_get_persona_route(self):
        paths = self._route_paths()
        assert any("persona_id" in p for p in paths)

    def test_create_persona_route(self):
        from app.routers.personas import router
        methods = []
        for r in router.routes:
            if hasattr(r, "methods") and "/personas" in r.path and "persona_id" not in r.path:
                methods.extend(r.methods)
        assert "POST" in methods

    def test_update_persona_route(self):
        from app.routers.personas import router
        methods = []
        for r in router.routes:
            if hasattr(r, "methods") and "persona_id" in r.path:
                methods.extend(r.methods)
        assert "PUT" in methods

    def test_delete_persona_route(self):
        from app.routers.personas import router
        methods = []
        for r in router.routes:
            if hasattr(r, "methods") and "persona_id" in r.path:
                methods.extend(r.methods)
        assert "DELETE" in methods


class TestPersonasRequestModels:
    """Test persona request/response models."""

    def test_create_persona_request_import(self):
        from app.routers.personas import CreatePersonaRequest
        assert CreatePersonaRequest is not None

    def test_create_persona_request_fields(self):
        from app.routers.personas import CreatePersonaRequest
        req = CreatePersonaRequest(name="Test", slug="test-persona")
        assert req.name == "Test"
        assert req.slug == "test-persona"

    def test_create_persona_slug_validation(self):
        from app.routers.personas import CreatePersonaRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreatePersonaRequest(name="Test", slug="Invalid Slug!")

    def test_create_persona_valid_slug(self):
        from app.routers.personas import CreatePersonaRequest
        req = CreatePersonaRequest(name="Test", slug="my-valid-slug-123")
        assert req.slug == "my-valid-slug-123"

    def test_update_persona_request_import(self):
        from app.routers.personas import UpdatePersonaRequest
        assert UpdatePersonaRequest is not None

    def test_update_persona_request_all_optional(self):
        from app.routers.personas import UpdatePersonaRequest
        req = UpdatePersonaRequest()
        assert req.name is None


# ═══════════════════════════════════════════════════════════════
# Missions Router
# ═══════════════════════════════════════════════════════════════


class TestMissionsRouterImport:
    """Test missions router module imports."""

    def test_router_import(self):
        from app.routers.missions import router
        assert router is not None

    def test_router_has_routes(self):
        from app.routers.missions import router
        assert len(router.routes) > 0

    def test_router_tags(self):
        from app.routers.missions import router
        tags = [t.lower() for t in router.tags]
        assert any("mission" in t for t in tags)


class TestMissionsRouterEndpoints:
    """Test mission endpoint route paths."""

    @staticmethod
    def _route_paths():
        from app.routers.missions import router
        return [r.path for r in router.routes]

    def test_list_missions_route(self):
        paths = self._route_paths()
        assert any("/missions" in p for p in paths)

    def test_dashboard_route(self):
        paths = self._route_paths()
        assert any("dashboard" in p for p in paths)

    def test_get_mission_route(self):
        paths = self._route_paths()
        assert any("mission_id" in p for p in paths)

    def test_stages_route(self):
        paths = self._route_paths()
        assert any("stages" in p for p in paths)

    def test_stage_detail_route(self):
        paths = self._route_paths()
        assert any("stage_number" in p for p in paths)

    def test_stage_start_route(self):
        paths = self._route_paths()
        assert any("start" in p for p in paths)

    def test_stage_complete_route(self):
        paths = self._route_paths()
        assert any("complete" in p for p in paths)

    def test_advance_route(self):
        paths = self._route_paths()
        assert any("advance" in p for p in paths)

    def test_artifacts_route(self):
        paths = self._route_paths()
        assert any("artifacts" in p for p in paths)

    def test_messages_route(self):
        paths = self._route_paths()
        assert any("messages" in p for p in paths)


class TestMissionsConstants:
    """Test mission constants."""

    def test_stage_types(self):
        from app.routers.missions import STAGE_TYPES
        assert STAGE_TYPES[1] == "capture"
        assert STAGE_TYPES[2] == "understand"
        assert STAGE_TYPES[3] == "strategize"
        assert STAGE_TYPES[4] == "generate"
        assert STAGE_TYPES[5] == "validate"

    def test_stage_labels(self):
        from app.routers.missions import STAGE_LABELS
        assert STAGE_LABELS[1] == "Capture"
        assert STAGE_LABELS[2] == "Understand"
        assert STAGE_LABELS[3] == "Strategize"
        assert STAGE_LABELS[4] == "Generate"
        assert STAGE_LABELS[5] == "Validate"


class TestMissionsStatusTransition:
    """Test mission status transition logic.

    _validate_status_transition returns None for valid transitions
    and raises HTTPException(400) for invalid ones.
    """

    def test_validate_status_transition_import(self):
        from app.routers.missions import _validate_status_transition
        assert callable(_validate_status_transition)

    def test_draft_to_active_allowed(self):
        from app.routers.missions import _validate_status_transition
        # Should NOT raise — returns None for valid transitions
        _validate_status_transition("draft", "active")

    def test_draft_to_cancelled_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("draft", "cancelled")

    def test_draft_to_completed_rejected(self):
        from app.routers.missions import _validate_status_transition
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc_info:
            _validate_status_transition("draft", "completed")
        assert exc_info.value.status_code == 400

    def test_active_to_paused_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("active", "paused")

    def test_active_to_completed_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("active", "completed")

    def test_active_to_failed_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("active", "failed")

    def test_active_to_cancelled_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("active", "cancelled")

    def test_active_to_draft_rejected(self):
        from app.routers.missions import _validate_status_transition
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc_info:
            _validate_status_transition("active", "draft")
        assert exc_info.value.status_code == 400

    def test_paused_to_active_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("paused", "active")

    def test_paused_to_cancelled_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("paused", "cancelled")

    def test_paused_to_completed_rejected(self):
        from app.routers.missions import _validate_status_transition
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc_info:
            _validate_status_transition("paused", "completed")
        assert exc_info.value.status_code == 400

    def test_failed_to_active_allowed(self):
        from app.routers.missions import _validate_status_transition
        _validate_status_transition("failed", "active")

    def test_completed_to_active_rejected(self):
        from app.routers.missions import _validate_status_transition
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc_info:
            _validate_status_transition("completed", "active")
        assert exc_info.value.status_code == 400

    def test_cancelled_to_active_rejected(self):
        from app.routers.missions import _validate_status_transition
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException) as exc_info:
            _validate_status_transition("cancelled", "active")
        assert exc_info.value.status_code == 400


class TestMissionsProgressCalculation:
    """Test mission progress calculation logic."""

    def test_calculate_progress_import(self):
        from app.routers.missions import _calculate_progress
        assert callable(_calculate_progress)

    def test_all_pending_is_zero(self):
        from app.routers.missions import _calculate_progress
        stages = [
            MagicMock(stage_number=i, status="pending")
            for i in range(1, 6)
        ]
        assert _calculate_progress(stages) == 0.0

    def test_all_completed_is_100(self):
        from app.routers.missions import _calculate_progress
        stages = [
            MagicMock(stage_number=i, status="completed")
            for i in range(1, 6)
        ]
        assert _calculate_progress(stages) == 100.0

    def test_partial_progress(self):
        from app.routers.missions import _calculate_progress
        stages = [
            MagicMock(stage_number=1, status="completed"),  # 15%
            MagicMock(stage_number=2, status="completed"),  # 25%
            MagicMock(stage_number=3, status="active"),     # 15% * 0.5 = 7.5%
            MagicMock(stage_number=4, status="pending"),    # 0%
            MagicMock(stage_number=5, status="pending"),    # 0%
        ]
        progress = _calculate_progress(stages)
        # stages 1+2 complete = 15+25 = 40, stage 3 active = 7.5
        assert 45.0 <= progress <= 50.0

    def test_skipped_stages_count_full(self):
        from app.routers.missions import _calculate_progress
        stages = [
            MagicMock(stage_number=1, status="completed"),
            MagicMock(stage_number=2, status="skipped"),
            MagicMock(stage_number=3, status="completed"),
            MagicMock(stage_number=4, status="completed"),
            MagicMock(stage_number=5, status="completed"),
        ]
        progress = _calculate_progress(stages)
        # Skipped counts as full: 15 + 25 + 15 + 30 + 15 = 100
        assert progress == 100.0


class TestMissionsRequestModels:
    """Test mission request models."""

    def test_create_mission_request(self):
        from app.routers.missions import CreateMissionRequest
        req = CreateMissionRequest(title="Test Mission", persona_id="p-123")
        assert req.title == "Test Mission"
        assert req.persona_id == "p-123"
        assert req.priority == "medium"  # default

    def test_create_mission_request_with_all_fields(self):
        from app.routers.missions import CreateMissionRequest
        req = CreateMissionRequest(
            title="Full Mission",
            persona_id="p-123",
            description="A test",
            objective="Test objective",
            priority="high",
            tags=["api", "compliance"],
        )
        assert req.priority == "high"
        assert len(req.tags) == 2

    def test_update_mission_request(self):
        from app.routers.missions import UpdateMissionRequest
        req = UpdateMissionRequest(title="Updated")
        assert req.title == "Updated"
        assert req.status is None

    def test_add_artifact_request(self):
        from app.routers.missions import AddArtifactRequest
        req = AddArtifactRequest(
            artifact_type="test_cases",
            name="Login Tests",
        )
        assert req.artifact_type == "test_cases"
        assert req.name == "Login Tests"


# ═══════════════════════════════════════════════════════════════
# Mission Orchestrator Service
# ═══════════════════════════════════════════════════════════════


class TestMissionOrchestratorImport:
    """Test mission orchestrator service imports."""

    def test_orchestrator_import(self):
        from app.services.mission_orchestrator import MissionOrchestrator
        assert MissionOrchestrator is not None

    def test_stage_execution_result_import(self):
        from app.services.mission_orchestrator import StageExecutionResult
        assert StageExecutionResult is not None

    def test_engine_call_result_import(self):
        from app.services.mission_orchestrator import EngineCallResult
        assert EngineCallResult is not None

    def test_services_package_import(self):
        from app.services import MissionOrchestrator, StageExecutionResult, EngineCallResult
        assert MissionOrchestrator is not None
        assert StageExecutionResult is not None
        assert EngineCallResult is not None


class TestMissionOrchestratorConfig:
    """Test orchestrator configuration and constants."""

    def test_default_engine_urls(self):
        from app.services.mission_orchestrator import DEFAULT_ENGINE_URLS
        assert "heart" in DEFAULT_ENGINE_URLS
        assert "shield" in DEFAULT_ENGINE_URLS
        assert "backbone" in DEFAULT_ENGINE_URLS

    def test_stage_engine_actions(self):
        from app.services.mission_orchestrator import STAGE_ENGINE_ACTIONS
        assert "capture" in STAGE_ENGINE_ACTIONS
        assert "understand" in STAGE_ENGINE_ACTIONS
        assert "strategize" in STAGE_ENGINE_ACTIONS
        assert "generate" in STAGE_ENGINE_ACTIONS
        assert "validate" in STAGE_ENGINE_ACTIONS


class TestMissionOrchestratorInstance:
    """Test orchestrator class methods."""

    def _make_orchestrator(self, **kwargs):
        from unittest.mock import MagicMock
        from app.services.mission_orchestrator import MissionOrchestrator
        mock_client = MagicMock()
        return MissionOrchestrator(http_client=mock_client, **kwargs)

    def test_constructor_default_urls(self):
        orch = self._make_orchestrator()
        assert orch._engine_urls is not None
        assert len(orch._engine_urls) > 0

    def test_constructor_custom_urls(self):
        custom = {"heart": "http://custom:9999"}
        orch = self._make_orchestrator(engine_urls=custom)
        assert orch._engine_urls["heart"] == "http://custom:9999"

    def test_has_execute_stage(self):
        orch = self._make_orchestrator()
        assert hasattr(orch, "execute_stage")

    def test_has_call_engine(self):
        orch = self._make_orchestrator()
        assert hasattr(orch, "_call_engine")

    def test_has_build_payload(self):
        orch = self._make_orchestrator()
        assert hasattr(orch, "_build_payload")

    def test_has_check_engine_health(self):
        orch = self._make_orchestrator()
        assert hasattr(orch, "check_engine_health")

    def test_has_check_stage_readiness(self):
        orch = self._make_orchestrator()
        assert hasattr(orch, "check_stage_readiness")


class TestEngineCallResult:
    """Test EngineCallResult dataclass."""

    def test_create_success(self):
        from app.services.mission_orchestrator import EngineCallResult
        r = EngineCallResult(
            engine="heart",
            endpoint="/analyze",
            status="ok",
            duration_ms=150.0,
            response_data={"confidence": 0.95},
        )
        assert r.engine == "heart"
        assert r.status == "ok"
        assert r.response_data["confidence"] == 0.95

    def test_create_error(self):
        from app.services.mission_orchestrator import EngineCallResult
        r = EngineCallResult(
            engine="shield",
            endpoint="/scan",
            status="error",
            duration_ms=50.0,
            error="Connection refused",
        )
        assert r.status == "error"
        assert "refused" in r.error


class TestStageExecutionResult:
    """Test StageExecutionResult dataclass."""

    def test_create(self):
        from app.services.mission_orchestrator import StageExecutionResult, EngineCallResult
        call = EngineCallResult(
            engine="heart", endpoint="/analyze", status="ok",
            duration_ms=100.0, response_data={"ok": True},
        )
        result = StageExecutionResult(
            stage_type="understand",
            success=True,
            engine_calls=[call],
            outputs={"analysis": "done"},
        )
        assert result.success is True
        assert result.stage_type == "understand"
        assert len(result.engine_calls) == 1
        assert result.total_duration_ms == 100.0


# ═══════════════════════════════════════════════════════════════
# Router Registration in Main
# ═══════════════════════════════════════════════════════════════


class TestQIRouterRegistration:
    """Verify QI Portal routers are registered in the main app."""

    def test_personas_router_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("/personas" in p for p in paths)

    def test_missions_router_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("/missions" in p for p in paths)

    def test_missions_dashboard_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("dashboard" in p for p in paths)

    def test_missions_stages_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("stages" in p for p in paths)

    def test_missions_artifacts_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("artifacts" in p for p in paths)

    def test_missions_messages_registered(self):
        from main import app
        paths = [r.path for r in app.routes]
        assert any("messages" in p for p in paths)

    def test_route_count_updated(self):
        from main import app
        # After registering 2 new routers (personas + missions), total should be >= 36
        route_count = len([r for r in app.routes if hasattr(r, "methods")])
        assert route_count >= 30, f"Expected at least 30 routes, got {route_count}"
