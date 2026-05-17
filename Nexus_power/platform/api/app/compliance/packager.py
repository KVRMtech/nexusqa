"""Compliance evidence packager.

Walks audit-relevant tenant-scoped tables for a given ``[period_start,
period_end)`` window and emits:

  * One NDJSON shard per scope (newline-delimited JSON, one row per line).
  * A top-level ``manifest.json`` recording counts + per-shard SHA-256.
  * A detached HMAC-SHA256 ``signature`` over the canonical manifest.

The packager is intentionally additive — it issues only SELECT
statements against existing tables and writes its output to a local
directory. No row is mutated.

Scope declaration
-----------------

``SCOPE_TABLES`` maps each logical scope label (``"echoes"``,
``"plugin_events"``, ``"scim"``, ``"atlas"``, ``"knowledge_cards"``)
to a tuple of (table_name, timestamp_column). Adding a new audit table
to the bundle is a one-line entry — the packager handles iteration,
counting, shard writing, and hashing uniformly.

The packager runs entirely within a single ``AsyncSession`` so that the
RLS policy (``current_setting('nexus.current_tenant_id')``) applies
to every row read.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── Scope declaration ─────────────────────────────────────────


@dataclass(frozen=True)
class ScopeDefinition:
    """Single audit slice — which table + which timestamp column."""

    label: str
    table: str
    timestamp_column: str
    # Optional fixed set of columns to project. When None, ``SELECT *``.
    columns: Optional[tuple[str, ...]] = None


SCOPE_TABLES: tuple[ScopeDefinition, ...] = (
    ScopeDefinition(
        label="echoes",
        table="echo_dispatches",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="echo_feedback",
        table="echo_feedback",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="plugin_events",
        table="action_invocations",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="scim_users",
        table="org_users",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="scim_groups",
        table="org_groups",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="atlas",
        table="atlas_nodes",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="knowledge_cards",
        table="knowledge_cards",
        timestamp_column="created_at",
    ),
    ScopeDefinition(
        label="knowledge_card_history",
        table="knowledge_card_history",
        timestamp_column="changed_at",
    ),
)


_SCOPE_BY_LABEL: dict[str, ScopeDefinition] = {s.label: s for s in SCOPE_TABLES}


# ── Configuration ─────────────────────────────────────────────


@dataclass(frozen=True)
class EvidencePackagerConfig:
    """Tuning knobs for the packager.

    * ``signing_key``: when present, the manifest is signed with
      HMAC-SHA256 and the signature included in the bundle. Production
      deployments should always set this from ``NEXUS_EVIDENCE_SIGNING_KEY``.
    * ``shard_max_rows``: cap rows-per-NDJSON-shard to keep individual
      files trivially diffable.
    * ``storage_dir``: directory where bundles are written. Each
      export_id gets its own subdirectory.
    * ``include_unknown_scopes``: when True, scopes not in
      ``SCOPE_TABLES`` are silently skipped; when False, an unknown
      scope raises.
    """

    signing_key: Optional[bytes] = None
    shard_max_rows: int = 5_000
    storage_dir: Optional[str] = None
    include_unknown_scopes: bool = False


# ── Result objects ────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceSlice:
    """One scope's contribution to the bundle."""

    scope: str
    table: str
    row_count: int
    shards: tuple[str, ...]
    shard_sha256: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceBundle:
    """Full result of a packager run."""

    tenant_id: str
    period_start: datetime
    period_end: datetime
    slices: tuple[EvidenceSlice, ...]
    manifest: dict[str, Any]
    manifest_sha256: str
    signature: Optional[str]
    storage_uri: Optional[str]

    def to_summary(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "slices": [
                {
                    "scope": s.scope,
                    "table": s.table,
                    "row_count": s.row_count,
                    "shard_count": len(s.shards),
                }
                for s in self.slices
            ],
            "manifest_sha256": self.manifest_sha256,
            "signature": self.signature,
            "storage_uri": self.storage_uri,
        }


# ── Errors ────────────────────────────────────────────────────


class EvidencePackagerError(Exception):
    """Top-level packager failure."""


class UnknownScopeError(EvidencePackagerError):
    """Scope label was requested but not declared in SCOPE_TABLES."""


# ── Packager ──────────────────────────────────────────────────


class EvidencePackager:
    """Walks per-tenant audit tables and produces a signed bundle.

    Lifecycle (one bundle per call)::

        async with factory() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
                {"t": tenant_id},
            )
            packager = EvidencePackager(config)
            bundle = await packager.build(
                session,
                tenant_id=tenant_id,
                period_start=...,
                period_end=...,
                scopes=("echoes", "plugin_events", "scim_users"),
                export_id="...",
            )

    The caller is responsible for the surrounding session + RLS context;
    the packager performs only SELECT statements and (optionally) writes
    to the local filesystem.
    """

    def __init__(self, config: Optional[EvidencePackagerConfig] = None) -> None:
        self._config = config or EvidencePackagerConfig()

    async def build(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
        scopes: tuple[str, ...] | list[str],
        export_id: str,
    ) -> EvidenceBundle:
        if period_end <= period_start:
            raise EvidencePackagerError(
                "period_end must be strictly after period_start"
            )
        if not tenant_id:
            raise EvidencePackagerError("tenant_id is required")

        resolved = self._resolve_scopes(scopes)
        bundle_dir = self._bundle_dir(export_id)

        slices: list[EvidenceSlice] = []
        for scope_def in resolved:
            evidence_slice = await self._build_slice(
                session,
                scope_def=scope_def,
                tenant_id=tenant_id,
                period_start=period_start,
                period_end=period_end,
                bundle_dir=bundle_dir,
            )
            slices.append(evidence_slice)

        manifest = self._build_manifest(
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
            slices=slices,
            export_id=export_id,
        )
        canonical = self._canonical_json(manifest)
        manifest_sha = hashlib.sha256(canonical).hexdigest()
        signature = self._sign(canonical) if self._config.signing_key else None
        manifest["manifest_sha256"] = manifest_sha
        if signature:
            manifest["signature"] = signature

        storage_uri = None
        if bundle_dir:
            manifest_path = os.path.join(bundle_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write(self._canonical_json(manifest).decode("utf-8"))
            storage_uri = f"file://{os.path.abspath(bundle_dir)}"

        return EvidenceBundle(
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
            slices=tuple(slices),
            manifest=manifest,
            manifest_sha256=manifest_sha,
            signature=signature,
            storage_uri=storage_uri,
        )

    # ── Internals ──────────────────────────────────────────────

    def _resolve_scopes(
        self, scopes: tuple[str, ...] | list[str]
    ) -> tuple[ScopeDefinition, ...]:
        if not scopes:
            return SCOPE_TABLES
        resolved: list[ScopeDefinition] = []
        for label in scopes:
            scope_def = _SCOPE_BY_LABEL.get(label)
            if scope_def is None:
                if self._config.include_unknown_scopes:
                    logger.warning("evidence.unknown_scope_skipped", extra={"scope": label})
                    continue
                raise UnknownScopeError(f"unknown scope: {label!r}")
            resolved.append(scope_def)
        return tuple(resolved)

    def _bundle_dir(self, export_id: str) -> Optional[str]:
        if not self._config.storage_dir:
            return None
        path = os.path.join(self._config.storage_dir, export_id)
        os.makedirs(path, exist_ok=True)
        return path

    async def _build_slice(
        self,
        session: AsyncSession,
        *,
        scope_def: ScopeDefinition,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
        bundle_dir: Optional[str],
    ) -> EvidenceSlice:
        # Use a parameterised text query; the timestamp column is a
        # whitelisted identifier from SCOPE_TABLES so direct
        # interpolation is safe.
        ts_col = scope_def.timestamp_column
        column_list = (
            ", ".join(scope_def.columns)
            if scope_def.columns
            else "*"
        )
        stmt = sa.text(
            f"SELECT {column_list} FROM {scope_def.table} "
            f"WHERE tenant_id = :tid "
            f"AND {ts_col} >= :start "
            f"AND {ts_col} < :end "
            f"ORDER BY {ts_col} ASC"
        )

        shards: list[str] = []
        shard_hashes: list[str] = []
        row_count = 0
        rows_iter = await self._stream_rows(session, stmt, tenant_id, period_start, period_end)

        buffer: list[bytes] = []
        shard_index = 0
        async for row in rows_iter:
            row_count += 1
            buffer.append(_encode_row(row))
            if len(buffer) >= self._config.shard_max_rows:
                shard_name, shard_hash = self._flush_shard(
                    bundle_dir, scope_def.label, shard_index, buffer
                )
                shards.append(shard_name)
                shard_hashes.append(shard_hash)
                buffer = []
                shard_index += 1
        if buffer:
            shard_name, shard_hash = self._flush_shard(
                bundle_dir, scope_def.label, shard_index, buffer
            )
            shards.append(shard_name)
            shard_hashes.append(shard_hash)

        return EvidenceSlice(
            scope=scope_def.label,
            table=scope_def.table,
            row_count=row_count,
            shards=tuple(shards),
            shard_sha256=tuple(shard_hashes),
        )

    async def _stream_rows(
        self,
        session: AsyncSession,
        stmt: sa.TextClause,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each result row as a dict."""
        result = await session.execute(
            stmt,
            {
                "tid": tenant_id,
                "start": period_start,
                "end": period_end,
            },
        )
        rows = result.mappings().all()

        async def _gen():
            for row in rows:
                yield dict(row)

        return _gen()

    def _flush_shard(
        self,
        bundle_dir: Optional[str],
        scope_label: str,
        shard_index: int,
        rows: list[bytes],
    ) -> tuple[str, str]:
        name = f"{scope_label}.{shard_index:04d}.ndjson"
        payload = b"\n".join(rows) + b"\n"
        sha = hashlib.sha256(payload).hexdigest()
        if bundle_dir:
            with open(os.path.join(bundle_dir, name), "wb") as f:
                f.write(payload)
        return name, sha

    def _build_manifest(
        self,
        *,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
        slices: list[EvidenceSlice],
        export_id: str,
    ) -> dict[str, Any]:
        return {
            "schema": "nexus.compliance.evidence.v1",
            "export_id": export_id,
            "tenant_id": tenant_id,
            "period_start": period_start.astimezone(timezone.utc).isoformat(),
            "period_end": period_end.astimezone(timezone.utc).isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "slices": [
                {
                    "scope": s.scope,
                    "table": s.table,
                    "row_count": s.row_count,
                    "shards": [
                        {"file": f, "sha256": h}
                        for f, h in zip(s.shards, s.shard_sha256)
                    ],
                }
                for s in slices
            ],
            "totals": {
                "row_count": sum(s.row_count for s in slices),
                "shard_count": sum(len(s.shards) for s in slices),
            },
        }

    @staticmethod
    def _canonical_json(payload: dict[str, Any]) -> bytes:
        """Encode a manifest deterministically for hashing/signing."""
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_jsonable,
        ).encode("utf-8")

    def _sign(self, canonical: bytes) -> str:
        return hmac.new(
            self._config.signing_key, canonical, hashlib.sha256
        ).hexdigest()


# ── JSON helpers ──────────────────────────────────────────────


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError(f"{type(value)!r} is not JSON-serialisable")


def _encode_row(row: dict[str, Any]) -> bytes:
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        default=_jsonable,
    ).encode("utf-8")
