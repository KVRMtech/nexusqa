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

    def test_load_all_returns_every_builtin_chain(self):
        """The four original chains must still load, alongside the later ones.

        Was `assert len(chains) == 4`. A bare count is a pin on the product's
        SIZE, which grows for good reasons (six chains have since been added),
        so it broke without anything being wrong. What actually matters is that
        no builtin chain silently disappears — asserted by name.
        """
        by_id = {c.chain_id: c for c in load_all_builtin_chains()}
        original_four = {
            "nexus.qa-testing",
            "nexus.compliance-audit",
            "nexus.knowledge-capture",
            "nexus.regression-suite",
        }
        assert original_four <= set(by_id), f"missing: {sorted(original_four - set(by_id))}"
        assert len(by_id) >= len(original_four)

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

    def test_stage_inventory(self):
        """Pin the stage IDs, not the count.

        The chain was restructured: raw-media ingestion (transcription /
        visual_analysis / security scanning) moved out to
        `nexus.canonical-processing`, and this chain now STARTS from the
        canonical artifact. Naming the stages says what the pipeline is; a
        count of 11 only said how long it was.
        """
        chain = build_qa_testing_chain()
        assert [s.stage_id for s in chain.stages] == [
            "fetch_artifact",
            "document_ingestion",
            "rule_extraction",
            "test_generation",
            "test_data_generation",
            "knowledge_storage",
            "test_execution",
            "report_generation",
            "notification",
        ]

    def test_dag_buildable(self, engine):
        chain = build_qa_testing_chain()
        plan = engine._build_execution_plan(chain.stages)
        assert len(plan) >= 3  # At least 3 levels

    def test_uses_the_qa_engines(self):
        chain = build_qa_testing_chain()
        assert {s.engine for s in chain.stages} == {
            "spine", "heart", "hands", "backbone", "legs", "mouth", "nerves",
        }

    def test_every_engine_is_exercised_by_some_builtin_chain(self):
        """The real coverage claim, relocated to where it is now true.

        This assertion used to live on qa-testing alone and demanded all ten
        engines there. After the decomposition, ears/eyes/shield belong to
        `nexus.canonical-processing` and `nexus.compliance-audit`. Asserting the
        UNION keeps the guarantee that no engine is orphaned — which is what the
        original test was really protecting — without freezing one chain's shape.
        """
        union = {
            s.engine
            for chain in load_all_builtin_chains()
            for s in chain.stages
            if getattr(s, "engine", "")
        }
        for engine_name in ("shield", "ears", "eyes", "heart", "backbone",
                            "nerves", "legs", "hands", "spine", "mouth"):
            assert engine_name in union, f"{engine_name} is not used by any builtin chain"

    def test_chain_id(self):
        chain = build_qa_testing_chain()
        assert chain.chain_id == "nexus.qa-testing"

    def test_first_level_starts_from_the_canonical_artifact(self, engine):
        """Level 0 is the artifact fetch.

        Previously asserted transcription / visual_analysis / document_ingestion
        ran in parallel here; those stages moved to `nexus.canonical-processing`
        when raw-media handling was split out, so this chain's entry point is now
        the fetch of the artifact that pipeline produced.
        """
        chain = build_qa_testing_chain()
        plan = engine._build_execution_plan(chain.stages)
        # Still genuinely parallel — two independent roots share level 0.
        assert {s.stage_id for s in plan[0]} == {"fetch_artifact", "document_ingestion"}


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

    def test_stage_inventory(self):
        """Same restructure as qa-testing: raw-media ingestion moved out to
        `nexus.canonical-processing`, so this chain starts from the artifact."""
        chain = build_knowledge_capture_chain()
        assert [s.stage_id for s in chain.stages] == [
            "fetch_artifact", "rule_extraction", "knowledge_storage",
        ]

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
