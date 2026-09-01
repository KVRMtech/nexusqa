"""
Orchestrator — WorkflowStore and FileStore unit tests.
"""

import pytest
import sys
import os
import tempfile
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "products", "nexus-qa-orchestrator"))

from app.workflows.schema import WorkflowInstance, WorkflowStatus, StageExecution, StageStatus
from app.workflows.engine import WorkflowStore, FileStore


# ══════════════════════════════════════════════════════════════
#  WORKFLOW STORE (in-memory mode)
# ══════════════════════════════════════════════════════════════

class TestWorkflowStore:

    @pytest.fixture
    def store(self):
        return WorkflowStore()

    def _instance(self, wf_id: str = "wf-001", tenant: str = "t-001", **kw) -> WorkflowInstance:
        defaults = dict(
            workflow_id=wf_id,
            chain_id="nexus.qa-testing",
            chain_name="QA Testing",
            tenant_id=tenant,
            session_id="sess-001",
        )
        defaults.update(kw)
        return WorkflowInstance(**defaults)

    @pytest.mark.asyncio
    async def test_save_and_get(self, store):
        inst = self._instance()
        await store.save_instance(inst)
        fetched = await store.get_instance("wf-001")
        assert fetched is not None
        assert fetched.workflow_id == "wf-001"
        assert fetched.chain_id == "nexus.qa-testing"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        assert await store.get_instance("nope") is None

    @pytest.mark.asyncio
    async def test_list_by_tenant(self, store):
        await store.save_instance(self._instance("wf-1", "t-001"))
        await store.save_instance(self._instance("wf-2", "t-001"))
        await store.save_instance(self._instance("wf-3", "t-002"))

        t1 = await store.list_instances("t-001")
        assert len(t1) == 2
        t2 = await store.list_instances("t-002")
        assert len(t2) == 1

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, store):
        for i in range(10):
            await store.save_instance(self._instance(f"wf-{i}", "t-001"))
        result = await store.list_instances("t-001", limit=3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_save_and_get_context(self, store):
        ctx_data = {
            "workflow": {"tenant_id": "t-001"},
            "stages": {"s1": {"output": {"key": "val"}}},
            "temp": {},
        }
        await store.save_context("wf-001", ctx_data)
        loaded = await store.get_context("wf-001")
        assert loaded is not None
        assert loaded["workflow"]["tenant_id"] == "t-001"

    @pytest.mark.asyncio
    async def test_get_context_nonexistent(self, store):
        assert await store.get_context("nope") is None

    @pytest.mark.asyncio
    async def test_update_overwrites(self, store):
        inst = self._instance("wf-001")
        await store.save_instance(inst)

        inst.status = WorkflowStatus.COMPLETED
        await store.save_instance(inst)

        fetched = await store.get_instance("wf-001")
        assert fetched.status == WorkflowStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_list_by_session_id(self, store):
        await store.save_instance(self._instance("wf-1", "t-001", session_id="sess-A"))
        await store.save_instance(self._instance("wf-2", "t-001", session_id="sess-A"))
        await store.save_instance(self._instance("wf-3", "t-001", session_id="sess-B"))

        result = await store.list_instances("t-001", session_id="sess-A")
        assert len(result) == 2
        assert all(i.session_id == "sess-A" for i in result)

    @pytest.mark.asyncio
    async def test_list_session_id_none_returns_all(self, store):
        await store.save_instance(self._instance("wf-1", "t-001", session_id="sess-A"))
        await store.save_instance(self._instance("wf-2", "t-001", session_id="sess-B"))

        result = await store.list_instances("t-001", session_id=None)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_session_id_no_match(self, store):
        await store.save_instance(self._instance("wf-1", "t-001", session_id="sess-A"))

        result = await store.list_instances("t-001", session_id="sess-NONE")
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_list_session_id_cross_tenant_isolation(self, store):
        await store.save_instance(self._instance("wf-1", "t-001", session_id="sess-A"))
        await store.save_instance(self._instance("wf-2", "t-002", session_id="sess-A"))

        result = await store.list_instances("t-001", session_id="sess-A")
        assert len(result) == 1
        assert result[0].tenant_id == "t-001"


# ══════════════════════════════════════════════════════════════
#  FILE STORE (in-memory mode)
# ══════════════════════════════════════════════════════════════

class TestFileStore:

    @pytest.fixture
    def fstore(self, tmp_path):
        return FileStore(base_path=str(tmp_path / "uploads"))

    @pytest.mark.asyncio
    async def test_store_and_get(self, fstore):
        meta = await fstore.store(
            filename="test.pdf",
            content=b"fake pdf content",
            content_type="application/pdf",
            tenant_id="t-001",
        )
        assert meta["filename"] == "test.pdf"
        assert meta["size_bytes"] == 16
        assert meta["content_type"] == "application/pdf"
        assert "file_id" in meta

        # Retrieve by file_id
        loaded = fstore.get(meta["file_id"])
        assert loaded is not None
        assert loaded["filename"] == "test.pdf"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, fstore):
        assert fstore.get("nonexistent-id") is None

    @pytest.mark.asyncio
    async def test_file_actually_written(self, fstore):
        meta = await fstore.store(
            filename="data.csv",
            content=b"col1,col2\na,b",
            content_type="text/csv",
            tenant_id="t-001",
        )
        from pathlib import Path
        file_path = Path(meta["path"])
        assert file_path.exists()
        assert file_path.read_bytes() == b"col1,col2\na,b"

    @pytest.mark.asyncio
    async def test_creates_tenant_subdirectory(self, fstore):
        meta = await fstore.store(
            filename="x.txt", content=b"hi", content_type="text/plain",
            tenant_id="tenant-abc",
        )
        assert "tenant-abc" in meta["path"]

    @pytest.mark.asyncio
    async def test_multiple_files(self, fstore):
        m1 = await fstore.store("a.txt", b"a", "text/plain", "t-001")
        m2 = await fstore.store("b.txt", b"b", "text/plain", "t-001")
        assert m1["file_id"] != m2["file_id"]
        assert fstore.get(m1["file_id"]) is not None
        assert fstore.get(m2["file_id"]) is not None
