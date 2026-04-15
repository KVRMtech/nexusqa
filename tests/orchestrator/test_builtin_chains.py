"""
Orchestrator — Built-in chain validation tests.

Verifies all 4 built-in chains are structurally valid
and match the expected DAG shapes.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.builtin import load_all_builtin_chains
from app.workflows.builtin.qa_testing import build_qa_testing_chain
from app.workflows.builtin.compliance_audit import build_compliance_audit_chain
from app.workflows.builtin.knowledge_capture import build_knowledge_capture_chain
from app.workflows.builtin.regression_suite import build_regression_suite_chain
from app.workflows.registry import ChainRegistry
from app.workflows.engine import ChainEngine, EngineURLResolver, WorkflowStore, FileStore

import httpx


@pytest.fixture
def engine():
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
#  LOAD ALL
# ══════════════════════════════════════════════════════════════

class TestLoadBuiltins:

    def test_load_all_returns_four(self):
        chains = load_all_builtin_chains()
        assert len(chains) == 4

    def test_all_have_unique_ids(self):
        chains = load_all_builtin_chains()
        ids = [c.chain_id for c in chains]
        assert len(ids) == len(set(ids))

    def test_all_are_system_level(self):
        """Built-in chains should have empty tenant_id."""
        for chain in load_all_builtin_chains():
            assert chain.tenant_id == "", f"{chain.chain_id} has tenant_id={chain.tenant_id!r}"


# ══════════════════════════════════════════════════════════════
#  PER-CHAIN VALIDATION
# ══════════════════════════════════════════════════════════════

class TestQATestingChain:

    def test_validates_clean(self):
        chain = build_qa_testing_chain()
        errors = ChainRegistry.validate_chain(chain)
        assert errors == [], f"Validation errors: {errors}"

    def test_has_11_stages(self):
        chain = build_qa_testing_chain()
        assert len(chain.stages) == 11

    def test_dag_buildable(self, engine):
        chain = build_qa_testing_chain()
        plan = engine._build_execution_plan(chain.stages)
        assert len(plan) >= 3  # At least 3 levels

    def test_uses_all_10_engines(self):
        chain = build_qa_testing_chain()
        engines_used = {s.engine for s in chain.stages}
        expected = {"shield", "ears", "eyes", "heart", "backbone",
                    "nerves", "legs", "hands", "spine", "mouth"}
        assert engines_used == expected

    def test_chain_id(self):
        chain = build_qa_testing_chain()
        assert chain.chain_id == "nexus.qa-testing"

    def test_first_level_is_parallel(self, engine):
        chain = build_qa_testing_chain()
        plan = engine._build_execution_plan(chain.stages)
        level0_ids = {s.stage_id for s in plan[0]}
        # transcription, visual_analysis, document_ingestion should be in level 0
        assert "transcription" in level0_ids
        assert "visual_analysis" in level0_ids
        assert "document_ingestion" in level0_ids


class TestComplianceAuditChain:

    def test_validates_clean(self):
        chain = build_compliance_audit_chain()
        errors = ChainRegistry.validate_chain(chain)
        assert errors == [], f"Validation errors: {errors}"

    def test_has_5_stages(self):
        chain = build_compliance_audit_chain()
        assert len(chain.stages) == 5

    def test_dag_buildable(self, engine):
        chain = build_compliance_audit_chain()
        plan = engine._build_execution_plan(chain.stages)
        assert len(plan) >= 2

    def test_chain_id(self):
        chain = build_compliance_audit_chain()
        assert chain.chain_id == "nexus.compliance-audit"


class TestKnowledgeCaptureChain:

    def test_validates_clean(self):
        chain = build_knowledge_capture_chain()
        errors = ChainRegistry.validate_chain(chain)
        assert errors == [], f"Validation errors: {errors}"

    def test_has_5_stages(self):
        chain = build_knowledge_capture_chain()
        assert len(chain.stages) == 5

    def test_dag_buildable(self, engine):
        chain = build_knowledge_capture_chain()
        plan = engine._build_execution_plan(chain.stages)
        assert len(plan) >= 3

    def test_chain_id(self):
        chain = build_knowledge_capture_chain()
        assert chain.chain_id == "nexus.knowledge-capture"

    def test_no_test_execution_stages(self):
        """Knowledge capture should not run tests."""
        chain = build_knowledge_capture_chain()
        engines_used = {s.engine for s in chain.stages}
        assert "legs" not in engines_used
        assert "hands" not in engines_used


class TestRegressionSuiteChain:

    def test_validates_clean(self):
        chain = build_regression_suite_chain()
        errors = ChainRegistry.validate_chain(chain)
        assert errors == [], f"Validation errors: {errors}"

    def test_has_6_stages(self):
        chain = build_regression_suite_chain()
        assert len(chain.stages) == 6

    def test_dag_buildable(self, engine):
        chain = build_regression_suite_chain()
        plan = engine._build_execution_plan(chain.stages)
        assert len(plan) >= 3

    def test_chain_id(self):
        chain = build_regression_suite_chain()
        assert chain.chain_id == "nexus.regression-suite"

    def test_no_transcription_stages(self):
        """Regression suite should not transcribe — it uses existing rules."""
        chain = build_regression_suite_chain()
        engines_used = {s.engine for s in chain.stages}
        assert "ears" not in engines_used
        assert "eyes" not in engines_used
