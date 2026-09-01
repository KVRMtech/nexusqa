"""Compliance evidence packager — in-memory session that pattern-matches
the SCOPE_TABLES queries.

The packager only issues SELECT statements (one per scope) plus optional
``set_config`` text. The fake session inspects the SQL string for each
scope's table name and returns the matching rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest


# ── In-memory async session fake ──────────────────────────────


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, by_table: dict[str, list[dict[str, Any]]]):
        self._by_table = by_table

    async def execute(self, stmt, *args, **kwargs):  # noqa: ARG002
        text = str(stmt).lower()
        for table, rows in self._by_table.items():
            if table in text:
                return _Result(rows)
        return _Result([])


def _ev(table: str = "x", **extra) -> dict[str, Any]:
    base = {
        "id": "1",
        "tenant_id": "t1",
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    }
    base.update(extra)
    return base


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_packager_walks_declared_scopes(tmp_path):
    from app.compliance import EvidencePackager, EvidencePackagerConfig

    by_table = {
        "echo_dispatches": [
            _ev(id="d1", payload={"q": "a"}),
            _ev(id="d2", payload={"q": "b"}),
        ],
        "action_invocations": [_ev(id="i1", action="log_echo")],
        "atlas_nodes": [],
    }
    session = _FakeSession(by_table)
    packager = EvidencePackager(
        EvidencePackagerConfig(storage_dir=str(tmp_path))
    )
    bundle = await packager.build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        scopes=("echoes", "plugin_events", "atlas"),
        export_id="exp-001",
    )
    by_scope = {s.scope: s for s in bundle.slices}
    assert set(by_scope.keys()) == {"echoes", "plugin_events", "atlas"}
    assert by_scope["echoes"].row_count == 2
    assert by_scope["plugin_events"].row_count == 1
    assert by_scope["atlas"].row_count == 0
    # Two NDJSON shards (one for echoes, one for plugin_events). Empty
    # slices produce no shards.
    assert by_scope["echoes"].shards
    assert by_scope["atlas"].shards == ()


@pytest.mark.asyncio
async def test_packager_manifest_sha_deterministic(tmp_path):
    from app.compliance import EvidencePackager, EvidencePackagerConfig

    session = _FakeSession({"echo_dispatches": [_ev(id="d1")]})
    packager = EvidencePackager(
        EvidencePackagerConfig(storage_dir=str(tmp_path / "a"))
    )
    b1 = await packager.build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        scopes=("echoes",),
        export_id="exp-fixed",
    )
    # The packager records ``generated_at`` in the manifest, so two
    # builds of the same data still differ. We exercise determinism
    # by hashing the manifest *without* the timestamp field.
    fixed = {k: v for k, v in b1.manifest.items() if k != "generated_at"}
    import hashlib, json
    canonical = json.dumps(fixed, sort_keys=True, separators=(",", ":"))
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        and b1.manifest_sha256
    )
    # The slice's shard sha is stable for the same payload.
    sha_first = b1.slices[0].shard_sha256
    b2 = await packager.build(
        _FakeSession({"echo_dispatches": [_ev(id="d1")]}),
        tenant_id="t1",
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        scopes=("echoes",),
        export_id="exp-fixed-2",
    )
    assert b2.slices[0].shard_sha256 == sha_first


@pytest.mark.asyncio
async def test_packager_signing_key_produces_signature(tmp_path):
    from app.compliance import EvidencePackager, EvidencePackagerConfig

    session = _FakeSession({"echo_dispatches": [_ev(id="d1")]})
    cfg = EvidencePackagerConfig(
        signing_key=b"a-very-secret-key",
        storage_dir=str(tmp_path),
    )
    bundle = await EvidencePackager(cfg).build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        scopes=("echoes",),
        export_id="exp-signed",
    )
    assert bundle.signature is not None
    assert len(bundle.signature) == 64  # hex SHA-256


@pytest.mark.asyncio
async def test_packager_rejects_unknown_scope(tmp_path):
    from app.compliance import EvidencePackager, UnknownScopeError

    session = _FakeSession({})
    with pytest.raises(UnknownScopeError):
        await EvidencePackager().build(
            session,
            tenant_id="t1",
            period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
            scopes=("not_a_real_scope",),
            export_id="x",
        )


@pytest.mark.asyncio
async def test_packager_rejects_inverted_period():
    from app.compliance import EvidencePackager, EvidencePackagerError

    session = _FakeSession({})
    with pytest.raises(EvidencePackagerError):
        await EvidencePackager().build(
            session,
            tenant_id="t1",
            period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
            scopes=("echoes",),
            export_id="x",
        )


@pytest.mark.asyncio
async def test_packager_chunks_shards_at_max_rows(tmp_path):
    from app.compliance import EvidencePackager, EvidencePackagerConfig

    rows = [_ev(id=f"d{i}") for i in range(7)]
    session = _FakeSession({"echo_dispatches": rows})
    packager = EvidencePackager(
        EvidencePackagerConfig(shard_max_rows=3, storage_dir=str(tmp_path))
    )
    bundle = await packager.build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
        scopes=("echoes",),
        export_id="exp-shards",
    )
    slice_ = bundle.slices[0]
    # 7 rows / 3 per shard → 3 shards (3 + 3 + 1)
    assert len(slice_.shards) == 3
    assert slice_.row_count == 7


@pytest.mark.asyncio
async def test_packager_writes_files_when_storage_dir_set(tmp_path):
    import os
    from app.compliance import EvidencePackager, EvidencePackagerConfig

    session = _FakeSession(
        {"echo_dispatches": [_ev(id="d1"), _ev(id="d2")]}
    )
    cfg = EvidencePackagerConfig(storage_dir=str(tmp_path))
    bundle = await EvidencePackager(cfg).build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
        scopes=("echoes",),
        export_id="exp-fs",
    )
    bundle_dir = tmp_path / "exp-fs"
    assert (bundle_dir / "manifest.json").exists()
    shard_files = list(bundle_dir.glob("echoes.*.ndjson"))
    assert len(shard_files) == 1
    body = shard_files[0].read_text(encoding="utf-8").splitlines()
    assert len(body) == 2  # two NDJSON rows
    assert bundle.storage_uri and bundle.storage_uri.startswith("file://")


@pytest.mark.asyncio
async def test_packager_default_scopes_when_empty(tmp_path):
    from app.compliance import EvidencePackager, EvidencePackagerConfig, SCOPE_TABLES

    by_table = {s.table: [] for s in SCOPE_TABLES}
    session = _FakeSession(by_table)
    bundle = await EvidencePackager(EvidencePackagerConfig()).build(
        session,
        tenant_id="t1",
        period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        scopes=(),
        export_id="exp-all",
    )
    assert len(bundle.slices) == len(SCOPE_TABLES)
