"""
Legs Engine — Unit tests.

Tests enums, Pydantic models, and configuration.
(WebExecutor / APIExecutor require live browser/server — tested at integration level.)
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "legs-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ────────────────────────────────────────────────────


class TestTargetType:

    def test_values(self):
        from main import TargetType
        assert TargetType.WEB_UI == "web_ui"
        assert TargetType.API == "api"
        assert TargetType.DATABASE == "database"
        assert TargetType.MAINFRAME == "mainframe"

    def test_all_members(self):
        from main import TargetType
        assert len(TargetType) == 4


class TestExecutionStatus:

    def test_values(self):
        from main import ExecutionStatus
        assert ExecutionStatus.QUEUED == "queued"
        assert ExecutionStatus.RUNNING == "running"
        assert ExecutionStatus.PASSED == "passed"
        assert ExecutionStatus.FAILED == "failed"
        assert ExecutionStatus.ERROR == "error"
        assert ExecutionStatus.SKIPPED == "skipped"

    def test_all_members(self):
        from main import ExecutionStatus
        assert len(ExecutionStatus) == 6


# ─── Configuration ────────────────────────────────────────────


class TestLegsConfig:

    def test_defaults(self):
        from main import LegsConfig
        cfg = LegsConfig()
        assert cfg.engine_name == "legs"
        assert cfg.engine_port == 8007
        assert cfg.browser_type == "chromium"
        assert cfg.headless is True
        assert cfg.viewport_width == 1920
        assert cfg.viewport_height == 1080
        assert cfg.default_timeout_ms == 30000

    def test_execution_defaults(self):
        from main import LegsConfig
        cfg = LegsConfig()
        assert cfg.max_concurrent_tests == 5
        assert cfg.screenshot_on_failure is True
        assert cfg.screenshot_on_step is True

    def test_exploration_defaults(self):
        from main import LegsConfig
        cfg = LegsConfig()
        assert cfg.max_exploration_depth == 10
        assert cfg.max_exploration_branches == 20


# ─── Pydantic Models ──────────────────────────────────────────


class TestStepExecutionDetail:

    def test_create(self):
        from main import StepExecutionDetail, ExecutionStatus
        step = StepExecutionDetail(
            step_number=1,
            action="Click Login",
            expected="Login form displayed",
            actual="Login form displayed",
            status=ExecutionStatus.PASSED,
            duration_ms=150.5,
        )
        assert step.step_number == 1
        assert step.status == ExecutionStatus.PASSED
        assert step.element_found is True
        assert step.self_healed is False

    def test_failed_step(self):
        from main import StepExecutionDetail, ExecutionStatus
        step = StepExecutionDetail(
            step_number=2,
            action="Click Submit",
            expected="Success message",
            actual="Error: timeout",
            status=ExecutionStatus.FAILED,
            error_message="Element not found after 30s",
            element_found=False,
        )
        assert step.status == ExecutionStatus.FAILED
        assert step.element_found is False
        assert step.error_message is not None

    def test_self_healed_step(self):
        from main import StepExecutionDetail, ExecutionStatus
        step = StepExecutionDetail(
            step_number=3,
            action="Click Save",
            expected="Saved",
            actual="Saved",
            status=ExecutionStatus.PASSED,
            self_healed=True,
            heal_details="Primary selector #save-btn failed, fell back to [data-action='save']",
        )
        assert step.self_healed is True
        assert step.heal_details is not None


class TestTestExecutionResult:

    def test_create_passed(self):
        from main import TestExecutionResult, ExecutionStatus
        result = TestExecutionResult(
            test_id="tc-001",
            test_name="Login Test",
            status=ExecutionStatus.PASSED,
            total_steps=5,
            steps_passed=5,
            steps_failed=0,
            duration_ms=3200.0,
            steps=[],
        )
        assert result.total_steps == 5
        assert result.steps_passed == 5
        assert result.evidence_path is None
        assert result.explored_paths == []

    def test_create_with_failures(self):
        from main import TestExecutionResult, ExecutionStatus
        result = TestExecutionResult(
            test_id="tc-002",
            test_name="Quote Flow",
            status=ExecutionStatus.FAILED,
            total_steps=10,
            steps_passed=7,
            steps_failed=3,
            duration_ms=12500.0,
            steps=[],
            evidence_path="/evidence/tc-002",
        )
        assert result.steps_failed == 3
        assert result.evidence_path is not None


class TestExplorationResult:

    def test_create(self):
        from main import ExplorationResult
        result = ExplorationResult(
            pages_discovered=[{"url": "/quote"}],
            forms_found=[{"id": "form1"}],
            links_followed=[{"href": "/apply"}],
            errors_found=[],
            total_pages=1,
            total_interactions=15,
        )
        assert result.total_pages == 1
        assert result.total_interactions == 15
        assert len(result.pages_discovered) == 1
        assert result.exploration_tree == {}


# ─── Request Models ───────────────────────────────────────────


class TestExecuteTestRequest:

    def test_defaults(self):
        from main import ExecuteTestRequest, TargetType
        from nexus_sdk.models import TestCase
        tc = TestCase(test_id="t1", tenant_id="tenant-1", title="Test", description="Desc")
        req = ExecuteTestRequest(
            tenant_id="tenant-1",
            test_case=tc,
            base_url="https://app.example.com",
        )
        assert req.target_type == TargetType.WEB_UI
        assert req.credentials is None
        assert req.variables == {}


class TestExploreRequest:

    def test_defaults_and_bounds(self):
        from main import ExploreRequest, TargetType
        req = ExploreRequest(
            tenant_id="t1",
            start_url="https://app.example.com/dashboard",
        )
        assert req.target_type == TargetType.WEB_UI
        assert req.max_depth == 5
        assert req.focus_areas == []
        assert req.credentials is None

    def test_max_depth_validation(self):
        from main import ExploreRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ExploreRequest(
                tenant_id="t1",
                start_url="https://app.example.com",
                max_depth=25,  # exceeds le=20
            )
