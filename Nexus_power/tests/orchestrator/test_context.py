"""
Orchestrator — WorkflowContext unit tests.

Tests $-path resolution, string interpolation, condition evaluation,
for_each isolation, and snapshot/restore.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))


from app.workflows.context import WorkflowContext


@pytest.fixture
def ctx():
    """Fresh workflow context with realistic data."""
    c = WorkflowContext(
        workflow_id="wf-001",
        chain_id="nexus.qa-testing",
        tenant_id="tenant-001",
        session_id="session-001",
        input_data={
            "audio_file_id": "file-abc",
            "video_file_id": "file-def",
            "language": "en",
            "documents": [
                {"name": "policy.pdf", "id": "doc-1"},
                {"name": "rates.xlsx", "id": "doc-2"},
            ],
            "sut_url": "https://app.example.com",
            "skip_execution": False,
            "notify": True,
        },
    )
    # Simulate completed stages
    c.set_stage_output("shield", {"safe_text": "Redacted text here", "entity_count": 3})
    c.set_stage_status("shield", "completed")
    c.set_stage_output("rule_extraction", {
        "rules": [
            {"rule_id": "r1", "text": "Rule one"},
            {"rule_id": "r2", "text": "Rule two"},
            {"rule_id": "r3", "text": "Rule three"},
        ]
    })
    c.set_stage_status("rule_extraction", "completed")
    return c


# ══════════════════════════════════════════════════════════════
#  PATH RESOLUTION
# ══════════════════════════════════════════════════════════════

class TestResolve:
    """Test $-path resolution."""

    def test_resolve_workflow_scalar(self, ctx):
        assert ctx.resolve("$workflow.tenant_id") == "tenant-001"

    def test_resolve_workflow_session(self, ctx):
        assert ctx.resolve("$workflow.session_id") == "session-001"

    def test_resolve_workflow_input(self, ctx):
        assert ctx.resolve("$workflow.input.audio_file_id") == "file-abc"

    def test_resolve_nested_input(self, ctx):
        assert ctx.resolve("$workflow.input.language") == "en"

    def test_resolve_list_index(self, ctx):
        doc = ctx.resolve("$workflow.input.documents.0")
        assert doc == {"name": "policy.pdf", "id": "doc-1"}

    def test_resolve_list_second_index(self, ctx):
        doc = ctx.resolve("$workflow.input.documents.1")
        assert doc == {"name": "rates.xlsx", "id": "doc-2"}

    def test_resolve_deep_through_list(self, ctx):
        name = ctx.resolve("$workflow.input.documents.0.name")
        assert name == "policy.pdf"

    def test_resolve_stage_output(self, ctx):
        safe = ctx.resolve("$stages.shield.output.safe_text")
        assert safe == "Redacted text here"

    def test_resolve_stage_output_int(self, ctx):
        count = ctx.resolve("$stages.shield.output.entity_count")
        assert count == 3

    def test_resolve_stage_status(self, ctx):
        status = ctx.resolve("$stages.shield.status")
        assert status == "completed"

    def test_resolve_stage_output_list(self, ctx):
        rules = ctx.resolve("$stages.rule_extraction.output.rules")
        assert isinstance(rules, list)
        assert len(rules) == 3

    def test_resolve_into_stage_list(self, ctx):
        first_rule = ctx.resolve("$stages.rule_extraction.output.rules.0")
        assert first_rule["rule_id"] == "r1"

    def test_resolve_temp(self, ctx):
        ctx.set_temp("item", {"key": "value"})
        assert ctx.resolve("$temp.item") == {"key": "value"}
        assert ctx.resolve("$temp.item.key") == "value"

    def test_resolve_nonexistent_returns_none(self, ctx):
        assert ctx.resolve("$stages.nonexistent.output") is None

    def test_resolve_deep_nonexistent_returns_none(self, ctx):
        assert ctx.resolve("$stages.shield.output.nonexistent.deep") is None

    def test_resolve_out_of_range_index_returns_none(self, ctx):
        assert ctx.resolve("$workflow.input.documents.99") is None

    def test_resolve_non_dollar_passthrough(self, ctx):
        assert ctx.resolve("literal string") == "literal string"
        assert ctx.resolve(42) == 42
        assert ctx.resolve(None) is None

    def test_resolve_boolean_preserved(self, ctx):
        val = ctx.resolve("$workflow.input.skip_execution")
        assert val is False
        assert isinstance(val, bool)

    def test_resolve_true_bool(self, ctx):
        assert ctx.resolve("$workflow.input.notify") is True


# ══════════════════════════════════════════════════════════════
#  RESOLVE MAPPING
# ══════════════════════════════════════════════════════════════

class TestResolveMapping:
    """Test recursive mapping resolution."""

    def test_simple_mapping(self, ctx):
        mapping = {
            "tenant_id": "$workflow.tenant_id",
            "text": "$stages.shield.output.safe_text",
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["tenant_id"] == "tenant-001"
        assert resolved["text"] == "Redacted text here"

    def test_literal_passthrough(self, ctx):
        mapping = {
            "count": 100,
            "flag": True,
            "label": "static-value",
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["count"] == 100
        assert resolved["flag"] is True
        assert resolved["label"] == "static-value"

    def test_nested_dict_resolution(self, ctx):
        mapping = {
            "outer": {
                "inner_ref": "$workflow.tenant_id",
                "inner_literal": "hello",
            }
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["outer"]["inner_ref"] == "tenant-001"
        assert resolved["outer"]["inner_literal"] == "hello"

    def test_list_resolution(self, ctx):
        mapping = {
            "items": ["$workflow.tenant_id", "static", "$workflow.session_id"],
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["items"] == ["tenant-001", "static", "session-001"]

    def test_string_interpolation(self, ctx):
        mapping = {
            "message": "Pipeline for session ${workflow.session_id}",
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["message"] == "Pipeline for session session-001"

    def test_mixed_interpolation_and_ref(self, ctx):
        mapping = {
            "ref": "$workflow.tenant_id",
            "interp": "Tenant: ${workflow.tenant_id} session: ${workflow.session_id}",
        }
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["ref"] == "tenant-001"
        assert resolved["interp"] == "Tenant: tenant-001 session: session-001"

    def test_none_resolution_in_interpolation(self, ctx):
        mapping = {"msg": "Val: ${stages.nonexistent.output}"}
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["msg"] == "Val: "

    def test_preserves_type_for_full_ref(self, ctx):
        """$-ref without interpolation preserves original type (int, list, etc)."""
        mapping = {"count": "$stages.shield.output.entity_count"}
        resolved = ctx.resolve_mapping(mapping)
        assert resolved["count"] == 3
        assert isinstance(resolved["count"], int)


# ══════════════════════════════════════════════════════════════
#  CONDITION EVALUATION
# ══════════════════════════════════════════════════════════════

class TestEvaluateCondition:
    """Test condition expressions."""

    def test_simple_truthy_path(self, ctx):
        assert ctx.evaluate_condition("$workflow.input.audio_file_id") is True

    def test_simple_falsy_path(self, ctx):
        assert ctx.evaluate_condition("$workflow.input.skip_execution") is False

    def test_none_path_is_false(self, ctx):
        assert ctx.evaluate_condition("$stages.nonexistent.output") is False

    def test_empty_condition_is_true(self, ctx):
        assert ctx.evaluate_condition("") is True
        assert ctx.evaluate_condition(None) is True
        assert ctx.evaluate_condition("  ") is True

    def test_len_comparison(self, ctx):
        assert ctx.evaluate_condition(
            "len($stages.rule_extraction.output.rules) > 0"
        ) is True

    def test_len_exact(self, ctx):
        assert ctx.evaluate_condition(
            "len($stages.rule_extraction.output.rules) == 3"
        ) is True

    def test_equality_to_false(self, ctx):
        assert ctx.evaluate_condition(
            "$workflow.input.skip_execution == false"
        ) is True

    def test_string_equality(self, ctx):
        assert ctx.evaluate_condition(
            "$workflow.input.language == 'en'"
        ) is True

    def test_complex_and(self, ctx):
        assert ctx.evaluate_condition(
            "$workflow.input.audio_file_id and len($stages.rule_extraction.output.rules) > 2"
        ) is True

    def test_eval_error_defaults_false(self, ctx):
        """On eval error, conditions default to False (fail-closed)."""
        result = ctx.evaluate_condition("this is not valid python !!@#$")
        assert result is False

    def test_builtins_restricted(self, ctx):
        """Dangerous builtins like __import__ are blocked."""
        # This should fail to eval (no __import__) and default to False
        result = ctx.evaluate_condition("__import__('os').system('whoami')")
        assert result is False  # fail-closed


# ══════════════════════════════════════════════════════════════
#  FOR_EACH ISOLATION
# ══════════════════════════════════════════════════════════════

class TestWithTemp:
    """Test concurrency-safe temp context."""

    def test_child_has_temp_data(self, ctx):
        child = ctx.with_temp({"item": "hello", "item_index": 0})
        assert child.resolve("$temp.item") == "hello"
        assert child.resolve("$temp.item_index") == 0

    def test_child_shares_workflow(self, ctx):
        child = ctx.with_temp({"item": "x"})
        assert child.resolve("$workflow.tenant_id") == "tenant-001"

    def test_child_shares_stages(self, ctx):
        child = ctx.with_temp({"item": "x"})
        assert child.resolve("$stages.shield.output.safe_text") == "Redacted text here"

    def test_children_have_isolated_temp(self, ctx):
        child_a = ctx.with_temp({"item": "a_item"})
        child_b = ctx.with_temp({"item": "b_item"})
        assert child_a.resolve("$temp.item") == "a_item"
        assert child_b.resolve("$temp.item") == "b_item"

    def test_parent_temp_unaffected(self, ctx):
        ctx.set_temp("original", "value")
        child = ctx.with_temp({"item": "child_val"})
        assert ctx.resolve("$temp.original") == "value"
        assert child.resolve("$temp.original") is None


# ══════════════════════════════════════════════════════════════
#  SNAPSHOT / RESTORE
# ══════════════════════════════════════════════════════════════

class TestSnapshot:
    """Test context serialisation for Redis persistence."""

    def test_snapshot_is_deep_copy(self, ctx):
        snap = ctx.snapshot()
        snap["workflow"]["tenant_id"] = "MODIFIED"
        assert ctx.resolve("$workflow.tenant_id") == "tenant-001"

    def test_from_snapshot_restores_values(self, ctx):
        snap = ctx.snapshot()
        restored = WorkflowContext.from_snapshot(snap)
        assert restored.resolve("$workflow.tenant_id") == "tenant-001"
        assert restored.resolve("$stages.shield.output.safe_text") == "Redacted text here"
        assert len(restored.resolve("$stages.rule_extraction.output.rules")) == 3

    def test_from_snapshot_is_independent(self, ctx):
        snap = ctx.snapshot()
        restored = WorkflowContext.from_snapshot(snap)
        restored.set_stage_output("shield", {"safe_text": "CHANGED"})
        assert ctx.resolve("$stages.shield.output.safe_text") == "Redacted text here"

    def test_roundtrip_preserves_temp(self, ctx):
        ctx.set_temp("mykey", [1, 2, 3])
        snap = ctx.snapshot()
        restored = WorkflowContext.from_snapshot(snap)
        assert restored.resolve("$temp.mykey") == [1, 2, 3]
