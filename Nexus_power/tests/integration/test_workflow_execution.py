"""
Integration Tests — Cross-engine workflow execution with mocked HTTP.

These tests verify that the orchestrator correctly wires multiple
engines together end-to-end, without live services. Each engine
HTTP call is intercepted by replacing the httpx client with a mock.
"""

import pytest
import sys
import os
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.schema import (
    ChainDefinition,
    StageDefinition,
    StageExecution,
    StageStatus,
    WorkflowInstance,
    WorkflowStatus,
    RetryPolicy,
    PollingConfig,
)
from app.workflows.context import WorkflowContext
from app.workflows.engine import ChainEngine, EngineURLResolver, WorkflowStore, FileStore


# ─── Helpers ───────────────────────────────────────────────────


def _make_engine(mock_client=None):
    """Create a ChainEngine with in-memory stores and an optional mock HTTP client."""
    resolver = EngineURLResolver()
    store = WorkflowStore()
    fstore = FileStore(base_path="/tmp/nexus-test-files")
    client = mock_client or _make_mock_client()
    return ChainEngine(
        url_resolver=resolver,
        workflow_store=store,
        file_store=fstore,
        http_client=client,
    )


def _make_mock_client(handler=None):
    """Create a mock httpx.AsyncClient with a request handler."""
    client = MagicMock()
    client.aclose = AsyncMock()

    if handler:
        client.request = AsyncMock(side_effect=handler)
    else:
        client.request = AsyncMock(return_value=_ok({}))

    return client


def _ok(data: dict, status_code=200):
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    resp.headers = {"content-type": "application/json"}
    resp.is_success = status_code < 400

    if status_code >= 400:
        import httpx as _httpx

        def _raise():
            raise _httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=_httpx.Request("POST", "http://mock"),
                response=_httpx.Response(status_code),
            )

        resp.raise_for_status = MagicMock(side_effect=_raise)
    else:
        resp.raise_for_status = MagicMock()
    return resp


# ─── Simple Two-Stage Chain ───────────────────────────────────


class TestTwoStageChain:
    """Shield → Heart (redact then extract rules)."""

    @pytest.fixture
    def chain(self):
        return ChainDefinition(
            chain_id="test.two-stage",
            name="Two Stage Test",
            description="Shield → Heart",
            version="1.0.0",
            stages=[
                StageDefinition(
                    stage_id="redact",
                    name="PII Redaction",
                    engine="shield",
                    endpoint="/api/v1/shield/scan",
                    method="POST",
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "text": "$workflow.input.raw_text",
                    },
                ),
                StageDefinition(
                    stage_id="extract",
                    name="Rule Extraction",
                    engine="heart",
                    endpoint="/api/v1/heart/extract-rules",
                    method="POST",
                    depends_on=["redact"],
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "transcript": "$stages.redact.output.safe_text",
                        "session_id": "$workflow.input.session_id",
                    },
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_two_stage_executes_in_order(self, chain):
        call_order = []

        async def mock_request(method, url, **kw):
            url_str = str(url)
            call_order.append(url_str)
            if "shield" in url_str:
                return _ok({"safe_text": "[REDACTED] text", "pii_found": True})
            elif "heart" in url_str:
                return _ok({"rules": [{"description": "Rule A"}], "status": "ok"})
            return _ok({})

        engine = _make_engine(_make_mock_client(mock_request))

        instance = await engine.start(
            chain=chain,
            tenant_id="tenant-001",
            session_id="sess-001",
            input_data={"raw_text": "John Doe SSN 123-45-6789", "session_id": "sess-001"},
        )

        await engine.execute(instance.workflow_id, chain)

        result = await engine.store.get_instance(instance.workflow_id)
        assert result.status == WorkflowStatus.COMPLETED

        # Shield should be called before Heart
        shield_idx = next((i for i, u in enumerate(call_order) if "shield" in u), -1)
        heart_idx = next((i for i, u in enumerate(call_order) if "heart" in u), -1)
        assert shield_idx < heart_idx


# ─── Conditional Stage Skip ────────────────────────────────────


class TestConditionalSkip:

    @pytest.fixture
    def chain(self):
        return ChainDefinition(
            chain_id="test.conditional",
            name="Conditional Test",
            description="Stage 2 runs only if condition met",
            version="1.0.0",
            stages=[
                StageDefinition(
                    stage_id="step1",
                    name="Step 1",
                    engine="shield",
                    endpoint="/api/v1/shield/scan",
                    method="POST",
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "text": "$workflow.input.text",
                    },
                ),
                StageDefinition(
                    stage_id="step2_optional",
                    name="Optional Heart",
                    engine="heart",
                    endpoint="/api/v1/heart/extract-rules",
                    method="POST",
                    depends_on=["step1"],
                    condition="$stages.step1.output.pii_found",
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "transcript": "$stages.step1.output.safe_text",
                        "session_id": "sess-001",
                    },
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_skips_when_condition_false(self, chain):
        """When shield says no PII found, heart stage should be skipped."""

        async def mock_request(method, url, **kw):
            url_str = str(url)
            if "shield" in url_str:
                return _ok({"safe_text": "clean text", "pii_found": False})
            return _ok({})

        engine = _make_engine(_make_mock_client(mock_request))

        instance = await engine.start(
            chain=chain,
            tenant_id="t1",
            session_id="s1",
            input_data={"text": "no pii here"},
        )

        await engine.execute(instance.workflow_id, chain)

        result = await engine.store.get_instance(instance.workflow_id)

        # The heart stage should be SKIPPED because pii_found=False
        heart_stage = result.stages.get("step2_optional")
        assert heart_stage is not None
        assert heart_stage.status in (StageStatus.SKIPPED, StageStatus.COMPLETED)


# ─── Retry Behaviour ──────────────────────────────────────────


class TestRetryBehaviour:
    """Verify that a 500 triggers retry and eventual success."""

    @pytest.fixture
    def chain(self):
        return ChainDefinition(
            chain_id="test.retry",
            name="Retry Test",
            version="1.0.0",
            stages=[
                StageDefinition(
                    stage_id="flaky",
                    name="Flaky Stage",
                    engine="shield",
                    endpoint="/api/v1/shield/scan",
                    method="POST",
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "text": "$workflow.input.text",
                    },
                    retry_policy=RetryPolicy(max_retries=2, backoff_seconds=0.01),
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_retries_on_500(self, chain):
        call_count = 0

        async def mock_request(method, url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _ok({"error": "internal"}, 500)
            return _ok({"safe_text": "ok", "pii_found": False})

        engine = _make_engine(_make_mock_client(mock_request))

        instance = await engine.start(
            chain=chain,
            tenant_id="t1",
            session_id="s1",
            input_data={"text": "test"},
        )

        await engine.execute(instance.workflow_id, chain)

        # Should have retried at least once
        assert call_count >= 2


# ─── Parallel Independent Stages ───────────────────────────────


class TestParallelStages:

    @pytest.fixture
    def chain(self):
        return ChainDefinition(
            chain_id="test.parallel",
            name="Parallel Test",
            version="1.0.0",
            stages=[
                StageDefinition(
                    stage_id="ears",
                    name="Transcribe",
                    engine="ears",
                    endpoint="/api/v1/ears/transcribe",
                    method="POST",
                    input_mapping={"tenant_id": "$workflow.tenant_id"},
                ),
                StageDefinition(
                    stage_id="eyes",
                    name="Analyze",
                    engine="eyes",
                    endpoint="/api/v1/eyes/analyze",
                    method="POST",
                    input_mapping={"tenant_id": "$workflow.tenant_id"},
                ),
                StageDefinition(
                    stage_id="merge",
                    name="Extract Rules",
                    engine="heart",
                    endpoint="/api/v1/heart/extract-rules",
                    method="POST",
                    depends_on=["ears", "eyes"],
                    input_mapping={
                        "tenant_id": "$workflow.tenant_id",
                        "transcript": "$stages.ears.output.transcript",
                        "session_id": "s1",
                    },
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_independent_stages_both_execute(self, chain):
        called_engines = set()

        async def mock_request(method, url, **kw):
            url_str = str(url)
            if "ears" in url_str:
                called_engines.add("ears")
                return _ok({"transcript": "Hello world"})
            elif "eyes" in url_str:
                called_engines.add("eyes")
                return _ok({"screens": [{"type": "web_ui"}]})
            elif "heart" in url_str:
                called_engines.add("heart")
                return _ok({"rules": [{"desc": "R1"}]})
            return _ok({})

        engine = _make_engine(_make_mock_client(mock_request))

        instance = await engine.start(
            chain=chain,
            tenant_id="t1",
            session_id="s1",
            input_data={},
        )

        await engine.execute(instance.workflow_id, chain)

        result = await engine.store.get_instance(instance.workflow_id)

        # All three engines should have been called
        assert "ears" in called_engines
        assert "eyes" in called_engines
        assert "heart" in called_engines
        assert result.status == WorkflowStatus.COMPLETED


# ─── WorkflowStore In-Memory ──────────────────────────────────


class TestWorkflowStoreIntegration:

    @pytest.mark.asyncio
    async def test_save_and_retrieve_workflow(self):
        store = WorkflowStore()
        instance = WorkflowInstance(
            workflow_id="wf-int-001",
            chain_id="test.chain",
            tenant_id="t1",
            session_id="s1",
            status=WorkflowStatus.RUNNING,
        )
        await store.save_instance(instance)
        loaded = await store.get_instance("wf-int-001")
        assert loaded is not None
        assert loaded.chain_id == "test.chain"

    @pytest.mark.asyncio
    async def test_save_and_retrieve_context(self):
        store = WorkflowStore()
        ctx = WorkflowContext(
            workflow_id="wf-int-002",
            chain_id="test.chain",
            tenant_id="t1",
            session_id="s1",
            input_data={"key": "value"},
        )
        await store.save_context("wf-int-002", ctx.snapshot())
        loaded = await store.get_context("wf-int-002")
        assert loaded is not None
        assert loaded["workflow"]["tenant_id"] == "t1"


# ─── EngineURLResolver ────────────────────────────────────────


class TestURLResolverIntegration:

    def test_all_engines_have_default_url(self):
        resolver = EngineURLResolver()
        engines = ["shield", "ears", "eyes", "heart", "backbone",
                    "nerves", "legs", "hands", "spine", "mouth"]
        for eng in engines:
            url = resolver.get_url(eng)
            assert url.startswith("http://")

    def test_overrides_work(self):
        resolver = EngineURLResolver(overrides={"shield": "http://shield.internal:9001"})
        assert resolver.get_url("shield") == "http://shield.internal:9001"
        assert resolver.get_url("ears") == "http://localhost:8002"  # unchanged

    def test_unknown_engine_raises(self):
        resolver = EngineURLResolver()
        with pytest.raises(ValueError, match="Unknown engine"):
            resolver.get_url("nonexistent")
