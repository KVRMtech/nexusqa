"""
Orchestrator — Schema validation unit tests.

Tests Pydantic model constraints, enum values, and defaults.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from pydantic import ValidationError

from app.workflows.schema import (
    ChainDefinition,
    ChainListItem,
    PollingConfig,
    RetryPolicy,
    StageDefinition,
    StageExecution,
    StageStatus,
    StartWorkflowRequest,
    WorkflowInstance,
    WorkflowStatus,
    WorkflowSummary,
)


# ══════════════════════════════════════════════════════════════
#  STAGE DEFINITION
# ══════════════════════════════════════════════════════════════

class TestStageDefinition:

    def test_minimal_valid(self):
        s = StageDefinition(
            stage_id="s1",
            name="Test Stage",
            engine="heart",
            endpoint="/api/v1/heart/ask",
        )
        assert s.method == "POST"
        assert s.request_type == "json"
        assert s.on_failure == "fail"
        assert s.timeout_seconds == 300
        assert s.depends_on == []
        assert s.for_each is None

    def test_default_retry_policy(self):
        s = StageDefinition(
            stage_id="s1", name="S", engine="heart",
            endpoint="/api/v1/heart/ask",
        )
        assert s.retry_policy.max_retries == 3
        assert s.retry_policy.backoff_seconds == 2.0
        assert s.retry_policy.backoff_multiplier == 2.0

    def test_invalid_method_rejected(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", method="INVALID",
            )

    def test_invalid_request_type_rejected(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", request_type="xml",
            )

    def test_invalid_on_failure_rejected(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", on_failure="retry",
            )

    def test_valid_methods(self):
        for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
            s = StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", method=method,
            )
            assert s.method == method

    def test_for_each_concurrency_bounds(self):
        # Lower bound
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", for_each_concurrency=0,
            )
        # Upper bound
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", for_each_concurrency=51,
            )

    def test_timeout_bounds(self):
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", timeout_seconds=0,
            )
        with pytest.raises(ValidationError):
            StageDefinition(
                stage_id="s1", name="S", engine="heart",
                endpoint="/test", timeout_seconds=7201,
            )


# ══════════════════════════════════════════════════════════════
#  RETRY POLICY
# ══════════════════════════════════════════════════════════════

class TestRetryPolicy:

    def test_defaults(self):
        rp = RetryPolicy()
        assert rp.max_retries == 3
        assert rp.retry_on_status == [500, 502, 503, 504]

    def test_max_retries_bounds(self):
        with pytest.raises(ValidationError):
            RetryPolicy(max_retries=-1)
        with pytest.raises(ValidationError):
            RetryPolicy(max_retries=11)

    def test_backoff_positive(self):
        with pytest.raises(ValidationError):
            RetryPolicy(backoff_seconds=0)
        with pytest.raises(ValidationError):
            RetryPolicy(backoff_seconds=-1)

    def test_multiplier_min(self):
        with pytest.raises(ValidationError):
            RetryPolicy(backoff_multiplier=0.5)


# ══════════════════════════════════════════════════════════════
#  POLLING CONFIG
# ══════════════════════════════════════════════════════════════

class TestPollingConfig:

    def test_defaults(self):
        pc = PollingConfig()
        assert pc.enabled is False
        assert pc.poll_interval_seconds == 5.0
        assert pc.max_poll_seconds == 600.0
        assert "completed" in pc.completion_statuses

    def test_min_interval(self):
        with pytest.raises(ValidationError):
            PollingConfig(poll_interval_seconds=0.5)

    def test_min_max_poll(self):
        with pytest.raises(ValidationError):
            PollingConfig(max_poll_seconds=5)


# ══════════════════════════════════════════════════════════════
#  CHAIN DEFINITION
# ══════════════════════════════════════════════════════════════

class TestChainDefinition:

    def test_requires_at_least_one_stage(self):
        with pytest.raises(ValidationError):
            ChainDefinition(
                chain_id="empty",
                name="Empty Chain",
                stages=[],
            )

    def test_auto_created_at(self):
        c = ChainDefinition(
            chain_id="test",
            name="Test",
            stages=[
                StageDefinition(
                    stage_id="s1", name="S", engine="heart",
                    endpoint="/test",
                ),
            ],
        )
        assert c.created_at  # should be auto-filled

    def test_default_version(self):
        c = ChainDefinition(
            chain_id="test", name="Test",
            stages=[
                StageDefinition(
                    stage_id="s1", name="S", engine="heart",
                    endpoint="/test",
                ),
            ],
        )
        assert c.version == "1.0.0"


# ══════════════════════════════════════════════════════════════
#  ENUMS
# ══════════════════════════════════════════════════════════════

class TestEnums:

    def test_stage_status_values(self):
        expected = {"pending", "waiting", "running", "polling", "completed",
                    "skipped", "failed", "retrying"}
        actual = {s.value for s in StageStatus}
        assert actual == expected

    def test_workflow_status_values(self):
        expected = {"created", "running", "paused", "completed",
                    "degraded", "needs_review", "failed",
                    "policy_blocked", "cancelled"}
        actual = {s.value for s in WorkflowStatus}
        assert actual == expected


# ══════════════════════════════════════════════════════════════
#  WORKFLOW INSTANCE
# ══════════════════════════════════════════════════════════════

class TestWorkflowInstance:

    def test_auto_workflow_id(self):
        wi = WorkflowInstance(chain_id="test", tenant_id="t1")
        assert wi.workflow_id  # auto UUID
        assert wi.status == WorkflowStatus.CREATED

    def test_stage_execution_defaults(self):
        se = StageExecution(stage_id="s1")
        assert se.status == StageStatus.PENDING
        assert se.retries == 0
        assert se.output is None
        assert se.error is None


# ══════════════════════════════════════════════════════════════
#  REQUEST / RESPONSE MODELS
# ══════════════════════════════════════════════════════════════

class TestStartWorkflowRequest:

    def test_auto_session_id(self):
        req = StartWorkflowRequest(chain_id="test", tenant_id="t1")
        assert req.session_id  # auto UUID

    def test_default_input_data(self):
        req = StartWorkflowRequest(chain_id="test", tenant_id="t1")
        assert req.input_data == {}
