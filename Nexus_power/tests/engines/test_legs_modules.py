"""
Legs Engine — Modular Sub-package Tests.

Tests the executors and explorer modules refactored from
the monolithic legs-engine/main.py.

All tests exercise stub mode (Playwright not installed).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "legs-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── WebExecutor ───────────────────────────────────────────────


class TestWebExecutor:
    """Test WebExecutor from app.executors (stub mode)."""

    def test_import(self):
        from app.executors import WebExecutor
        assert WebExecutor is not None

    def test_init(self):
        from main import LegsConfig
        from app.executors import WebExecutor
        cfg = LegsConfig()
        we = WebExecutor(cfg)
        assert we.browser is None
        assert we.playwright is None
        assert we._stub_fallback_count == 0

    def test_stub_execute(self):
        from main import LegsConfig, ExecutionStatus
        from app.executors import WebExecutor
        from nexus_sdk.models import TestCase, TestStep

        cfg = LegsConfig()
        we = WebExecutor(cfg)

        tc = TestCase(
            test_id="TC-001",
            tenant_id="t-test",
            title="Stub test",
            description="Testing stub execution",
            priority="high",
            steps=[
                TestStep(step_number=1, action="Click login", target_system="web", expected_output="Login page"),
                TestStep(step_number=2, action="Enter username", target_system="web", expected_output="Typed"),
            ],
        )
        result = we._stub_execute(tc, "/tmp/evidence")
        assert result.status == ExecutionStatus.SKIPPED
        assert result.total_steps == 2
        assert result.steps_passed == 0
        assert len(result.steps) == 2
        assert we._stub_fallback_count == 1

    def test_stub_execute_increments_count(self):
        from main import LegsConfig
        from app.executors import WebExecutor
        from nexus_sdk.models import TestCase, TestStep

        cfg = LegsConfig()
        we = WebExecutor(cfg)

        tc = TestCase(
            test_id="TC-002", tenant_id="t-test", title="T", description="D",
            priority="low", steps=[TestStep(step_number=1, action="X", target_system="web")],
        )
        we._stub_execute(tc, "/tmp")
        we._stub_execute(tc, "/tmp")
        we._stub_execute(tc, "/tmp")
        assert we._stub_fallback_count == 3

    def test_config_defaults(self):
        from main import LegsConfig
        cfg = LegsConfig()
        assert cfg.headless is True
        assert cfg.viewport_width == 1920
        assert cfg.viewport_height == 1080
        assert cfg.screenshot_on_failure is True


# ─── APIExecutor ───────────────────────────────────────────────


class TestAPIExecutor:
    """Test APIExecutor from app.executors."""

    def test_import(self):
        from app.executors import APIExecutor
        assert APIExecutor is not None

    def test_init(self):
        from main import LegsConfig
        from app.executors import APIExecutor
        cfg = LegsConfig()
        ae = APIExecutor(cfg)
        assert ae.config.api_timeout_seconds == 30


# ─── AutonomousExplorer ───────────────────────────────────────


class TestAutonomousExplorer:
    """Test AutonomousExplorer from app.explorer (stub mode)."""

    def test_import(self):
        from app.explorer import AutonomousExplorer
        assert AutonomousExplorer is not None

    def test_init(self):
        from main import LegsConfig
        from app.explorer import AutonomousExplorer
        cfg = LegsConfig()
        explorer = AutonomousExplorer(config=cfg, browser=None)
        assert explorer.browser is None
        assert explorer._stub_fallback_count == 0

    def test_stub_explore(self):
        from main import LegsConfig
        from app.explorer import AutonomousExplorer

        cfg = LegsConfig()
        explorer = AutonomousExplorer(config=cfg, browser=None)
        result = explorer._stub_explore()

        assert result.total_pages == 2
        assert result.total_interactions == 2
        assert len(result.pages_discovered) == 2
        assert len(result.forms_found) == 1
        assert len(result.links_followed) == 1
        assert len(result.errors_found) == 0
        assert explorer._stub_fallback_count == 1

    def test_stub_explore_increments(self):
        from main import LegsConfig
        from app.explorer import AutonomousExplorer

        cfg = LegsConfig()
        explorer = AutonomousExplorer(config=cfg, browser=None)
        explorer._stub_explore()
        explorer._stub_explore()
        assert explorer._stub_fallback_count == 2


# ─── Enums and Models ─────────────────────────────────────────


class TestLegsEnums:
    """Test execution enums from main.py."""

    def test_target_type(self):
        from main import TargetType
        assert TargetType.WEB_UI.value == "web_ui"
        assert TargetType.API.value == "api"
        assert TargetType.DATABASE.value == "database"
        assert TargetType.MAINFRAME.value == "mainframe"

    def test_execution_status(self):
        from main import ExecutionStatus
        assert ExecutionStatus.QUEUED.value == "queued"
        assert ExecutionStatus.RUNNING.value == "running"
        assert ExecutionStatus.PASSED.value == "passed"
        assert ExecutionStatus.FAILED.value == "failed"
        assert ExecutionStatus.ERROR.value == "error"
        assert ExecutionStatus.SKIPPED.value == "skipped"


class TestLegsModels:
    """Test response models from main.py."""

    def test_step_execution_detail(self):
        from main import StepExecutionDetail, ExecutionStatus
        s = StepExecutionDetail(
            step_number=1,
            action="Click login",
            expected="Login page",
            actual="Login page loaded",
            status=ExecutionStatus.PASSED,
        )
        assert s.step_number == 1
        assert s.self_healed is False
        assert s.element_found is True

    def test_test_execution_result(self):
        from main import TestExecutionResult, ExecutionStatus
        r = TestExecutionResult(
            test_id="TC-001",
            test_name="Login test",
            status=ExecutionStatus.PASSED,
            total_steps=3,
            steps_passed=3,
            steps_failed=0,
            duration_ms=1500.0,
            steps=[],
        )
        assert r.test_id == "TC-001"
        assert r.duration_ms == 1500.0

    def test_exploration_result(self):
        from main import ExplorationResult
        r = ExplorationResult(
            pages_discovered=[],
            forms_found=[],
            links_followed=[],
            errors_found=[],
            total_pages=0,
            total_interactions=0,
        )
        assert r.total_pages == 0


# ─── Integration: Main module imports from sub-packages ───────


class TestLegsMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import LegsEngine
        engine = LegsEngine()
        assert engine.version == "0.2.0"

    def test_main_imports_executors(self):
        from main import WebExecutor, APIExecutor
        assert WebExecutor is not None
        assert APIExecutor is not None

    def test_main_imports_explorer(self):
        from main import AutonomousExplorer
        assert AutonomousExplorer is not None
