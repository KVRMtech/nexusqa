"""
Orchestrator — Write-through persistence hardening tests.

Validates that _write_through_spine:
  - Retries on failure for terminal workflow states
  - Stays best-effort (single attempt) for non-terminal states
  - Escalates logging for terminal failures
  - Refreshes auth headers between retries
"""

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.schema import (
    WorkflowInstance,
    WorkflowStatus,
    StageExecution,
    StageStatus,
)
from app.workflows.engine import ChainEngine, EngineURLResolver, WorkflowStore, FileStore


# ── Helpers ────────────────────────────────────────────────────

def _make_engine(http_mock: AsyncMock) -> ChainEngine:
    """Build a ChainEngine with mocked dependencies."""
    urls = EngineURLResolver({"spine": "http://spine:8009"})
    store = WorkflowStore()
    fstore = FileStore(base_path="/tmp/test-uploads")
    engine = ChainEngine(
        url_resolver=urls,
        workflow_store=store,
        file_store=fstore,
        http_client=http_mock,
        token_factory=lambda tid: f"test-token-{tid}",
    )
    return engine


def _make_instance(status: WorkflowStatus) -> WorkflowInstance:
    return WorkflowInstance(
        workflow_id="wf-test-001",
        chain_id="nexus.canonical-processing",
        chain_name="Canonical Media Processing",
        tenant_id="test-tenant",
        session_id="sess-001",
        status=status,
        stages={
            "media_probe": StageExecution(
                stage_id="media_probe",
                status=StageStatus.COMPLETED,
                output={"duration_seconds": 60},
            ),
        },
        timeline=[{"timestamp": "2026-04-06T00:00:00Z", "event": "test", "detail": "test"}],
    )


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True, "workflow_id": "wf-test-001"}
    resp.text = '{"success": true}'
    return resp


def _fail_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": False, "error": "database not available"}
    resp.text = '{"success": false, "error": "database not available"}'
    return resp


def _server_error_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 500
    resp.json.return_value = {"detail": "Internal Server Error"}
    resp.text = '{"detail": "Internal Server Error"}'
    return resp


# ══════════════════════════════════════════════════════════════
#  TERMINAL STATE RETRY TESTS
# ══════════════════════════════════════════════════════════════

class TestWriteThroughTerminalRetry:
    """Terminal states (completed, failed, degraded) must retry on failure."""

    @pytest.mark.asyncio
    async def test_terminal_completed_retries_on_failure(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=[
            _fail_response(),
            _fail_response(),
            _ok_response(),
        ])
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._write_through_spine(instance)

        assert http.post.call_count == 3

    @pytest.mark.asyncio
    async def test_terminal_failed_retries_on_exception(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            _ok_response(),
        ])
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.FAILED)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._write_through_spine(instance)

        assert http.post.call_count == 3

    @pytest.mark.asyncio
    async def test_terminal_degraded_retries(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=[
            _server_error_response(),
            _ok_response(),
        ])
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.DEGRADED)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._write_through_spine(instance)

        assert http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_terminal_max_4_attempts(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_fail_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._write_through_spine(instance)

        assert http.post.call_count == 4

    @pytest.mark.asyncio
    async def test_terminal_stops_on_first_success(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        await engine._write_through_spine(instance)

        assert http.post.call_count == 1


# ══════════════════════════════════════════════════════════════
#  NON-TERMINAL STATE TESTS
# ══════════════════════════════════════════════════════════════

class TestWriteThroughNonTerminal:
    """Non-terminal states should remain single-attempt best-effort."""

    @pytest.mark.asyncio
    async def test_running_no_retry(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_fail_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.RUNNING)

        await engine._write_through_spine(instance)

        assert http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_created_no_retry(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.CREATED)

        await engine._write_through_spine(instance)

        assert http.post.call_count == 1


# ══════════════════════════════════════════════════════════════
#  AUTH HEADER REFRESH BETWEEN RETRIES
# ══════════════════════════════════════════════════════════════

class TestWriteThroughAuthRefresh:
    """Auth headers must be regenerated between retry attempts."""

    @pytest.mark.asyncio
    async def test_auth_headers_refreshed_on_retry(self):
        call_count = 0

        def counting_token_factory(tid):
            nonlocal call_count
            call_count += 1
            return f"token-{call_count}"

        http = AsyncMock()
        http.post = AsyncMock(side_effect=[
            _fail_response(),
            _ok_response(),
        ])

        urls = EngineURLResolver({"spine": "http://spine:8009"})
        engine = ChainEngine(
            url_resolver=urls,
            workflow_store=WorkflowStore(),
            file_store=FileStore(base_path="/tmp/test"),
            http_client=http,
            token_factory=counting_token_factory,
        )
        instance = _make_instance(WorkflowStatus.COMPLETED)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._write_through_spine(instance)

        # First call uses initial headers, second call should have refreshed token
        assert http.post.call_count == 2
        first_headers = http.post.call_args_list[0].kwargs.get("headers", {})
        second_headers = http.post.call_args_list[1].kwargs.get("headers", {})
        assert first_headers["Authorization"] != second_headers["Authorization"]


# ══════════════════════════════════════════════════════════════
#  LOGGING LEVEL TESTS
# ══════════════════════════════════════════════════════════════

class TestWriteThroughLogging:
    """Terminal failures must log at WARNING/ERROR, non-terminal at DEBUG."""

    @pytest.mark.asyncio
    async def test_terminal_exhausted_logs_error(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_fail_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.workflows.engine.logger") as mock_logger:
            await engine._write_through_spine(instance)

        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "FAILED" in error_msg
        assert "terminal" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_nonterminal_failure_logs_debug(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_fail_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.RUNNING)

        with patch("app.workflows.engine.logger") as mock_logger:
            await engine._write_through_spine(instance)

        mock_logger.debug.assert_called()
        mock_logger.error.assert_not_called()
        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminal_intermediate_failures_log_warning(self):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=[
            _fail_response(),
            _ok_response(),
        ])
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.workflows.engine.logger") as mock_logger:
            await engine._write_through_spine(instance)

        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()


# ══════════════════════════════════════════════════════════════
#  PAYLOAD CORRECTNESS
# ══════════════════════════════════════════════════════════════

class TestWriteThroughPayload:
    """Verify the persist-workflow payload is structurally correct."""

    @pytest.mark.asyncio
    async def test_payload_contains_required_fields(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)
        instance.completed_at = "2026-04-06T01:00:00Z"

        await engine._write_through_spine(instance)

        call_kwargs = http.post.call_args
        payload = call_kwargs.kwargs.get("json", call_kwargs[1].get("json"))

        assert payload["workflow_id"] == "wf-test-001"
        assert payload["chain_id"] == "nexus.canonical-processing"
        assert payload["chain_name"] == "Canonical Media Processing"
        assert payload["tenant_id"] == "test-tenant"
        assert payload["session_id"] == "sess-001"
        assert payload["status"] == "completed"
        assert payload["completed_at"] == "2026-04-06T01:00:00Z"
        assert "media_probe" in payload["stages"]
        assert len(payload["timeline"]) <= 20

    @pytest.mark.asyncio
    async def test_terminal_uses_longer_timeout(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.COMPLETED)

        await engine._write_through_spine(instance)

        call_kwargs = http.post.call_args
        timeout = call_kwargs.kwargs.get("timeout")
        assert timeout == 10.0

    @pytest.mark.asyncio
    async def test_nonterminal_uses_short_timeout(self):
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response())
        engine = _make_engine(http)
        instance = _make_instance(WorkflowStatus.RUNNING)

        await engine._write_through_spine(instance)

        call_kwargs = http.post.call_args
        timeout = call_kwargs.kwargs.get("timeout")
        assert timeout == 5.0
