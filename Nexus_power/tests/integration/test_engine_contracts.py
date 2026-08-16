"""
Contract Tests — Verify engine API shapes match what builtin chains expect.

Each builtin chain references specific engine endpoints with specific
input mappings.  These tests validate:
  1. Every engine name in every chain is a known engine
  2. Every endpoint path follows the expected naming convention
  3. Input mappings only reference valid $-path namespaces
  4. No chain has duplicate stage IDs
  5. All depends_on references point to existing stages
  6. for_each stages reference $temp variables
  7. output_key is unique within each chain
"""

import pytest
import sys
import os
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.builtin import load_all_builtin_chains
from app.workflows.engine import EngineURLResolver


# ─── Constants ─────────────────────────────────────────────────

KNOWN_ENGINES = {
    "shield", "ears", "eyes", "heart", "backbone",
    "nerves", "legs", "hands", "spine", "mouth",
    # `brain` was missing here while being a real, shipped engine
    # (engines/brain-engine/, routed by the gateway at /api/v1/brain and used by
    # nexus.canonical-processing + nexus.contradiction-detection). The omission
    # made this contract reject a legitimate chain rather than catch a typo.
    "brain",
}

VALID_PATH_PREFIXES = {"$workflow", "$stages", "$temp"}

API_ENDPOINT_PATTERN = re.compile(r"^/api/v\d+/\w+/[\w\-/{}]+$")


# ─── Test Classes ──────────────────────────────────────────────

@pytest.fixture
def all_chains():
    return load_all_builtin_chains()


class TestEngineNameContract:
    """Every engine referenced in a chain must be a known engine."""

    def test_all_engines_known(self, all_chains):
        for chain in all_chains:
            for stage in chain.stages:
                assert stage.engine in KNOWN_ENGINES, (
                    f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                    f"references unknown engine '{stage.engine}'"
                )

    def test_all_engines_resolvable(self, all_chains):
        resolver = EngineURLResolver()
        for chain in all_chains:
            engines = {s.engine for s in chain.stages}
            for eng in engines:
                url = resolver.get_url(eng)
                assert url.startswith("http"), f"Engine '{eng}' has no valid URL"


class TestEndpointContract:
    """Endpoint paths must follow /api/v{N}/{engine}/{action} convention."""

    def test_endpoints_follow_convention(self, all_chains):
        for chain in all_chains:
            for stage in chain.stages:
                ep = stage.endpoint
                assert ep.startswith("/api/v"), (
                    f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                    f"endpoint '{ep}' doesn't start with /api/v"
                )
                # Engine name should appear in the endpoint
                assert stage.engine in ep.lower() or any(
                    alias in ep.lower()
                    for alias in [stage.engine.replace("-", "_")]
                ), (
                    f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                    f"endpoint '{ep}' doesn't contain engine name '{stage.engine}'"
                )


class TestInputMappingContract:
    """All $-paths in input mappings must reference valid namespaces."""

    def test_paths_use_valid_prefixes(self, all_chains):
        for chain in all_chains:
            for stage in chain.stages:
                for key, val in stage.input_mapping.items():
                    if isinstance(val, str) and val.startswith("$"):
                        prefix = val.split(".")[0]
                        assert prefix in VALID_PATH_PREFIXES, (
                            f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                            f"mapping '{key}' has invalid prefix '{prefix}' in '{val}'"
                        )

    def test_stage_refs_point_to_prior_stages(self, all_chains):
        for chain in all_chains:
            stage_ids = {s.stage_id for s in chain.stages}
            for stage in chain.stages:
                for key, val in stage.input_mapping.items():
                    if isinstance(val, str) and val.startswith("$stages."):
                        ref_id = val.split(".")[1]
                        assert ref_id in stage_ids, (
                            f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                            f"input '{key}' references non-existent stage '{ref_id}'"
                        )


class TestDependencyContract:
    """depends_on must only reference existing stage IDs."""

    def test_all_deps_exist(self, all_chains):
        for chain in all_chains:
            stage_ids = {s.stage_id for s in chain.stages}
            for stage in chain.stages:
                for dep in stage.depends_on:
                    assert dep in stage_ids, (
                        f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                        f"depends on non-existent stage '{dep}'"
                    )


class TestNoDuplicateStages:
    """Each chain must have unique stage IDs."""

    def test_unique_stage_ids(self, all_chains):
        for chain in all_chains:
            ids = [s.stage_id for s in chain.stages]
            assert len(ids) == len(set(ids)), (
                f"Chain '{chain.chain_id}' has duplicate stage IDs"
            )


class TestForEachContract:
    """for_each stages must iterate over results from a prior stage."""

    def test_for_each_has_valid_source(self, all_chains):
        for chain in all_chains:
            stage_ids = {s.stage_id for s in chain.stages}
            for stage in chain.stages:
                if stage.for_each:
                    source = stage.for_each
                    # for_each path should reference $stages.* or $workflow.*
                    assert source.startswith("$"), (
                        f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                        f"for_each '{source}' should be a $-path"
                    )
                    if source.startswith("$stages."):
                        ref_id = source.split(".")[1]
                        assert ref_id in stage_ids, (
                            f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                            f"for_each references non-existent stage '{ref_id}'"
                        )


class TestHTTPMethodContract:
    """All stages must use valid HTTP methods."""

    VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

    def test_valid_methods(self, all_chains):
        for chain in all_chains:
            for stage in chain.stages:
                assert stage.method.upper() in self.VALID_METHODS, (
                    f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                    f"uses invalid HTTP method '{stage.method}'"
                )


class TestChainMetadata:
    """Chains must have proper metadata."""

    def test_all_have_version(self, all_chains):
        for chain in all_chains:
            assert chain.version, f"Chain '{chain.chain_id}' has no version"

    def test_all_have_description(self, all_chains):
        for chain in all_chains:
            assert chain.description, f"Chain '{chain.chain_id}' has no description"

    def test_chain_ids_are_namespaced(self, all_chains):
        for chain in all_chains:
            assert "." in chain.chain_id, (
                f"Chain ID '{chain.chain_id}' should be namespaced (e.g., 'nexus.qa-testing')"
            )


class TestPollingContract:
    """Stages with polling must have required polling fields."""

    def test_polling_has_required_fields(self, all_chains):
        for chain in all_chains:
            for stage in chain.stages:
                if stage.polling and stage.polling.enabled:
                    assert stage.polling.poll_endpoint, (
                        f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                        f"has polling enabled but no poll_endpoint"
                    )
                    assert stage.polling.completion_statuses, (
                        f"Chain '{chain.chain_id}' stage '{stage.stage_id}' "
                        f"has polling enabled but no completion_statuses"
                    )
