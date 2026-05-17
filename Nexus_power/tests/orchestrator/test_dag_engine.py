"""
Orchestrator — DAG execution plan unit tests.

Tests Kahn's algorithm, level grouping, cycle detection,
and unknown-dependency validation.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.schema import StageDefinition, RetryPolicy
from app.workflows.engine import ChainEngine, EngineURLResolver, WorkflowStore, FileStore

import httpx


def _stage(sid: str, depends_on: list[str] | None = None, **kw) -> StageDefinition:
    """Helper to build minimal StageDefinitions."""
    return StageDefinition(
        stage_id=sid,
        name=f"Stage {sid}",
        engine="heart",
        endpoint="/api/v1/heart/extract-rules",
        depends_on=depends_on or [],
        **kw,
    )


@pytest.fixture
def engine():
    """ChainEngine with minimal dependencies (only DAG builder needs no I/O)."""
    resolver = EngineURLResolver()
    store = WorkflowStore()
    fstore = FileStore(base_path="/tmp/nexus-test-files")
    client = httpx.AsyncClient()
    return ChainEngine(
        url_resolver=resolver,
        workflow_store=store,
        file_store=fstore,
        http_client=client,
    )


# ══════════════════════════════════════════════════════════════
#  BASIC DAG STRUCTURES
# ══════════════════════════════════════════════════════════════

class TestBuildExecutionPlan:
    """Test DAG topological sort with level grouping."""

    def test_single_stage(self, engine):
        stages = [_stage("a")]
        plan = engine._build_execution_plan(stages)
        assert len(plan) == 1
        assert plan[0][0].stage_id == "a"

    def test_two_independent_stages(self, engine):
        """Two stages with no deps → both in level 0."""
        stages = [_stage("a"), _stage("b")]
        plan = engine._build_execution_plan(stages)
        assert len(plan) == 1  # one level
        ids = {s.stage_id for s in plan[0]}
        assert ids == {"a", "b"}

    def test_linear_chain(self, engine):
        """A → B → C should produce 3 levels."""
        stages = [
            _stage("a"),
            _stage("b", depends_on=["a"]),
            _stage("c", depends_on=["b"]),
        ]
        plan = engine._build_execution_plan(stages)
        assert len(plan) == 3
        assert plan[0][0].stage_id == "a"
        assert plan[1][0].stage_id == "b"
        assert plan[2][0].stage_id == "c"

    def test_diamond_dag(self, engine):
        """
        A → B
        A → C
        B, C → D

        Should produce: Level 0=[A], Level 1=[B,C], Level 2=[D]
        """
        stages = [
            _stage("a"),
            _stage("b", depends_on=["a"]),
            _stage("c", depends_on=["a"]),
            _stage("d", depends_on=["b", "c"]),
        ]
        plan = engine._build_execution_plan(stages)
        assert len(plan) == 3
        level0_ids = {s.stage_id for s in plan[0]}
        level1_ids = {s.stage_id for s in plan[1]}
        level2_ids = {s.stage_id for s in plan[2]}
        assert level0_ids == {"a"}
        assert level1_ids == {"b", "c"}
        assert level2_ids == {"d"}

    def test_wide_parallel(self, engine):
        """5 independent stages → all in one level."""
        stages = [_stage(f"s{i}") for i in range(5)]
        plan = engine._build_execution_plan(stages)
        assert len(plan) == 1
        assert len(plan[0]) == 5

    def test_complex_dag(self, engine):
        """
        Mirrors QA-testing chain structure:
        transcription ─┐
                        ├─→ pii_redaction ─┐
        visual_analysis ───────────────────┤
        document_ingestion ────────────────┤
                                           ├─→ rule_extraction → test_gen
        """
        stages = [
            _stage("transcription"),
            _stage("visual_analysis"),
            _stage("document_ingestion"),
            _stage("pii_redaction", depends_on=["transcription"]),
            _stage("rule_extraction", depends_on=["pii_redaction", "visual_analysis", "document_ingestion"]),
            _stage("test_gen", depends_on=["rule_extraction"]),
        ]
        plan = engine._build_execution_plan(stages)

        # Level 0: transcription, visual_analysis, document_ingestion
        level0_ids = {s.stage_id for s in plan[0]}
        assert level0_ids == {"transcription", "visual_analysis", "document_ingestion"}

        # Level 1: pii_redaction
        level1_ids = {s.stage_id for s in plan[1]}
        assert level1_ids == {"pii_redaction"}

        # Level 2: rule_extraction
        level2_ids = {s.stage_id for s in plan[2]}
        assert level2_ids == {"rule_extraction"}

        # Level 3: test_gen
        level3_ids = {s.stage_id for s in plan[3]}
        assert level3_ids == {"test_gen"}


# ══════════════════════════════════════════════════════════════
#  ERROR DETECTION
# ══════════════════════════════════════════════════════════════

class TestDagValidation:
    """Test cycle detection and unknown dependency errors."""

    def test_circular_two_nodes(self, engine):
        stages = [
            _stage("a", depends_on=["b"]),
            _stage("b", depends_on=["a"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            engine._build_execution_plan(stages)

    def test_circular_three_nodes(self, engine):
        stages = [
            _stage("a", depends_on=["c"]),
            _stage("b", depends_on=["a"]),
            _stage("c", depends_on=["b"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            engine._build_execution_plan(stages)

    def test_self_dependency(self, engine):
        stages = [_stage("a", depends_on=["a"])]
        with pytest.raises(ValueError, match="Circular dependency"):
            engine._build_execution_plan(stages)

    def test_unknown_dependency(self, engine):
        stages = [
            _stage("a"),
            _stage("b", depends_on=["nonexistent"]),
        ]
        with pytest.raises(ValueError, match="unknown stage"):
            engine._build_execution_plan(stages)

    def test_partial_cycle_with_valid_nodes(self, engine):
        """Only the cycle members should appear in the error."""
        stages = [
            _stage("ok1"),
            _stage("ok2", depends_on=["ok1"]),
            _stage("cyc_a", depends_on=["cyc_b"]),
            _stage("cyc_b", depends_on=["cyc_a"]),
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            engine._build_execution_plan(stages)


# ══════════════════════════════════════════════════════════════
#  OUTPUT TRANSFORM
# ══════════════════════════════════════════════════════════════

class TestApplyTransform:
    """Test output_transform expressions."""

    def test_identity(self, engine):
        result = {"key": "value"}
        assert engine._apply_transform(result, "result") == result

    def test_extract_list(self, engine):
        result = {"data": [1, 2, 3]}
        transformed = engine._apply_transform(result, "result['data']")
        assert transformed == [1, 2, 3]

    def test_list_comprehension(self, engine):
        result = {"results": [
            {"properties": {"name": "a"}},
            {"properties": {"name": "b"}},
        ]}
        transformed = engine._apply_transform(
            result,
            "[r['properties'] for r in result.get('results', [])]",
        )
        assert transformed == [{"name": "a"}, {"name": "b"}]

    def test_len(self, engine):
        result = [1, 2, 3, 4, 5]
        assert engine._apply_transform(result, "len(result)") == 5

    def test_filter(self, engine):
        result = [
            {"status": "pass", "name": "t1"},
            {"status": "fail", "name": "t2"},
            {"status": "pass", "name": "t3"},
        ]
        transformed = engine._apply_transform(
            result,
            "[r for r in result if r['status'] == 'pass']",
        )
        assert len(transformed) == 2

    def test_invalid_transform_returns_raw(self, engine):
        """Bad transform should not crash — returns raw result."""
        result = {"key": "value"}
        transformed = engine._apply_transform(result, "nonexistent_var")
        assert transformed == result

    def test_sorted_transform(self, engine):
        result = [3, 1, 4, 1, 5]
        assert engine._apply_transform(result, "sorted(result)") == [1, 1, 3, 4, 5]


# ══════════════════════════════════════════════════════════════
#  EXTRACT PATH (dot-separated dict/list access)
# ══════════════════════════════════════════════════════════════

class TestExtractPath:
    """Test _extract_path helper."""

    def test_simple_key(self, engine):
        assert engine._extract_path({"job_id": "abc"}, "job_id") == "abc"

    def test_nested_key(self, engine):
        data = {"result": {"status": "done"}}
        assert engine._extract_path(data, "result.status") == "done"

    def test_list_index(self, engine):
        data = {"items": [10, 20, 30]}
        assert engine._extract_path(data, "items.1") == 20

    def test_missing_key_returns_none(self, engine):
        assert engine._extract_path({"a": 1}, "b") is None

    def test_deep_missing_returns_none(self, engine):
        assert engine._extract_path({"a": {"b": 1}}, "a.c.d") is None

    def test_out_of_range_index(self, engine):
        assert engine._extract_path({"items": [1]}, "items.99") is None


class TestStallThresholds:
    def test_long_polling_stage_uses_stage_budget(self, engine):
        stage = _stage(
            "audio_transcription",
            timeout_seconds=3600,
            polling={
                "enabled": True,
                "job_id_path": "job_id",
                "poll_endpoint": "/api/v1/ears/jobs/{job_id}",
                "poll_interval_seconds": 5.0,
                "max_poll_seconds": 3600.0,
                "completion_statuses": ["completed"],
                "failure_statuses": ["failed"],
                "result_path": "result",
                "status_path": "status",
            },
        )
        warn_secs, degrade_secs = engine._compute_stall_thresholds(stage, stage.polling)
        assert warn_secs == 900.0
        assert degrade_secs == 2700.0

    def test_short_polling_stage_never_warns_after_degrade(self, engine):
        stage = _stage(
            "short_async",
            timeout_seconds=300,
            polling={
                "enabled": True,
                "job_id_path": "job_id",
                "poll_endpoint": "/api/v1/heart/jobs/{job_id}",
                "poll_interval_seconds": 5.0,
                "max_poll_seconds": 300.0,
                "completion_statuses": ["completed"],
                "failure_statuses": ["failed"],
                "result_path": "result",
                "status_path": "status",
            },
        )
        warn_secs, degrade_secs = engine._compute_stall_thresholds(stage, stage.polling)
        assert warn_secs < degrade_secs
        assert degrade_secs == 300.0


# ══════════════════════════════════════════════════════════════
#  ENGINE URL RESOLVER
# ══════════════════════════════════════════════════════════════

class TestEngineURLResolver:

    def test_default_urls(self):
        resolver = EngineURLResolver()
        assert resolver.get_url("shield") == "http://localhost:8001"
        assert resolver.get_url("mouth") == "http://localhost:8010"

    def test_overrides(self):
        resolver = EngineURLResolver(overrides={"shield": "http://shield:9001"})
        assert resolver.get_url("shield") == "http://shield:9001"
        # Others still default
        assert resolver.get_url("ears") == "http://localhost:8002"

    def test_unknown_engine_raises(self):
        resolver = EngineURLResolver()
        with pytest.raises(ValueError, match="Unknown engine"):
            resolver.get_url("nonexistent")

    def test_trailing_slash_stripped(self):
        resolver = EngineURLResolver(overrides={"shield": "http://shield:9001/"})
        assert resolver.get_url("shield") == "http://shield:9001"
