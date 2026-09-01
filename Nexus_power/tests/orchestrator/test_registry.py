"""
Orchestrator — Chain registry and validation unit tests.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.schema import ChainDefinition, StageDefinition
from app.workflows.registry import ChainRegistry


def _chain(chain_id: str = "test.chain", stages: list | None = None, **kw) -> ChainDefinition:
    """Helper: minimal valid chain."""
    default_stages = [
        StageDefinition(
            stage_id="s1",
            name="Stage 1",
            engine="heart",
            endpoint="/api/v1/heart/extract-rules",
        ),
    ]
    return ChainDefinition(
        chain_id=chain_id,
        name="Test Chain",
        stages=stages or default_stages,
        **kw,
    )


def _stage(sid: str, depends_on: list[str] | None = None, **kw) -> StageDefinition:
    return StageDefinition(
        stage_id=sid,
        name=f"Stage {sid}",
        engine="heart",
        endpoint="/api/v1/heart/extract-rules",
        depends_on=depends_on or [],
        **kw,
    )


# ══════════════════════════════════════════════════════════════
#  CHAIN VALIDATION
# ══════════════════════════════════════════════════════════════

class TestValidateChain:
    """Test structural validation of chain definitions."""

    def test_valid_chain_no_errors(self):
        chain = _chain(stages=[
            _stage("a"),
            _stage("b", depends_on=["a"]),
            _stage("c", depends_on=["b"]),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert errors == []

    def test_duplicate_stage_ids(self):
        chain = _chain(stages=[
            _stage("dup"),
            _stage("dup"),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert any("Duplicate stage_id" in e for e in errors)

    def test_unknown_depends_on(self):
        chain = _chain(stages=[
            _stage("a"),
            _stage("b", depends_on=["nonexistent"]),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert any("unknown stage" in e for e in errors)

    def test_circular_dependency(self):
        chain = _chain(stages=[
            _stage("a", depends_on=["b"]),
            _stage("b", depends_on=["a"]),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert any("Circular" in e for e in errors)

    def test_for_each_without_temp_ref(self):
        chain = _chain(stages=[
            StageDefinition(
                stage_id="iter",
                name="Iterator",
                engine="heart",
                endpoint="/api/v1/heart/extract-rules",
                for_each="$stages.prev.output.items",
                for_each_item_key="item",
                input_mapping={"data": "static-value"},  # Missing $temp.item
            ),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert any("$temp.item" in e for e in errors)

    def test_for_each_with_temp_ref_is_valid(self):
        chain = _chain(stages=[
            StageDefinition(
                stage_id="iter",
                name="Iterator",
                engine="backbone",
                endpoint="/api/v1/backbone/rules",
                for_each="$stages.prev.output.rules",
                for_each_item_key="item",
                input_mapping={"rule": "$temp.item"},
            ),
        ])
        errors = ChainRegistry.validate_chain(chain)
        # Should have no for_each-related error
        for_each_errors = [e for e in errors if "$temp." in e]
        assert len(for_each_errors) == 0

    def test_valid_diamond_dag(self):
        chain = _chain(stages=[
            _stage("a"),
            _stage("b", depends_on=["a"]),
            _stage("c", depends_on=["a"]),
            _stage("d", depends_on=["b", "c"]),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert errors == []

    def test_multiple_errors_accumulated(self):
        chain = _chain(stages=[
            _stage("dup"),
            _stage("dup"),
            _stage("bad_dep", depends_on=["nonexistent"]),
        ])
        errors = ChainRegistry.validate_chain(chain)
        assert len(errors) >= 2


# ══════════════════════════════════════════════════════════════
#  REGISTRY CRUD (in-memory mode)
# ══════════════════════════════════════════════════════════════

class TestRegistryCRUD:
    """Test registry operations in no-Redis (in-memory) mode."""

    @pytest.fixture
    def registry(self):
        return ChainRegistry()

    @pytest.mark.asyncio
    async def test_register_and_get(self, registry):
        chain = _chain("my.chain")
        await registry.register(chain)
        fetched = await registry.get("my.chain")
        assert fetched is not None
        assert fetched.chain_id == "my.chain"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, registry):
        assert await registry.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_delete(self, registry):
        chain = _chain("to.delete")
        await registry.register(chain)
        deleted = await registry.delete("to.delete")
        assert deleted is True
        assert await registry.get("to.delete") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, registry):
        deleted = await registry.delete("nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_list_chains_all(self, registry):
        await registry.register(_chain("chain.a"))
        await registry.register(_chain("chain.b"))
        chains = await registry.list_chains()
        assert len(chains) == 2

    @pytest.mark.asyncio
    async def test_list_chains_tenant_filter(self, registry):
        sys_chain = _chain("sys.chain", tenant_id="")
        tenant_chain = _chain("tenant.chain", tenant_id="t-001")
        other_chain = _chain("other.chain", tenant_id="t-002")

        await registry.register(sys_chain)
        await registry.register(tenant_chain)
        await registry.register(other_chain)

        visible = await registry.list_chains(tenant_id="t-001")
        visible_ids = {c.chain_id for c in visible}
        # Should see system chain + own chain, not other tenant's
        assert "sys.chain" in visible_ids
        assert "tenant.chain" in visible_ids
        assert "other.chain" not in visible_ids

    @pytest.mark.asyncio
    async def test_register_builtins(self, registry):
        chains = [_chain("b.one"), _chain("b.two"), _chain("b.three")]
        await registry.register_builtins(chains)
        all_chains = await registry.list_chains()
        assert len(all_chains) == 3

    @pytest.mark.asyncio
    async def test_update_replaces(self, registry):
        await registry.register(_chain("upd.chain", version="1.0.0"))
        await registry.register(_chain("upd.chain", version="2.0.0"))
        fetched = await registry.get("upd.chain")
        assert fetched.version == "2.0.0"
