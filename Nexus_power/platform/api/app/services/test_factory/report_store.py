"""Persisted Execution Evidence Report snapshots (spec AC-1).

The report is otherwise assembled ON DEMAND from live rows, which is honest but
has one audit weakness: cases get regenerated, edited and re-certified, so the
report you render next month for an old run is not necessarily the report that
described it at the time. A snapshot freezes the account of a run at the moment
it landed.

Two properties make this safe rather than a second source of truth:

  * the snapshot records its own ``chain_root`` (the same SHA-256 fold used by
    the export manifest), so a stored report can be checked for tampering;
  * a reader is always told which one they are looking at — ``source:
    "snapshot"`` versus ``source: "live"`` — and a snapshot that no longer
    matches the live data is a FINDING, not something to paper over.

Written fire-and-forget after ingest: a snapshot failure must never break a
run's ingest, but it is logged at WARNING so a gap in the record is visible.
The ORM model is declared here (binding the SDK ``Base``) with the table created
out-of-band by ``scripts/apply_run_reports.sql`` — the same pattern as
``auth_profiles``. Every helper degrades safe when the table is absent.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

logger = logging.getLogger(__name__)

#: A report for a very large suite is still small next to its evidence; this cap
#: stops a pathological run from bloating the table.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class E2ERunReportRow(Base):
    """One frozen report per (run, tenant)."""

    __tablename__ = "e2e_run_reports"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    chain_root: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False)


def snapshot_chain_root(report: dict) -> str:
    """The report's own integrity root, computed exactly like an export's."""
    from .evidence_manifest import chain_root, sha256_hex
    blob = json.dumps(report, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return chain_root([{"path": "report.json", "sha256": sha256_hex(blob)}])


async def save_snapshot(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                        run_id: str, environment: str, report: dict) -> str:
    """Freeze a report for a run. Returns the chain root. Caller commits."""
    blob = json.dumps(report, default=str)
    if len(blob.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("report snapshot too large")
    root = snapshot_chain_root(report)
    stmt = (
        pg_insert(E2ERunReportRow)
        .values(run_id=run_id, tenant_id=tenant_id, artifact_id=artifact_id,
                environment=(environment or "")[:64], report_json=blob,
                chain_root=root, byte_size=len(blob.encode("utf-8")),
                created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[E2ERunReportRow.run_id, E2ERunReportRow.tenant_id],
            set_={"report_json": blob, "chain_root": root,
                  "byte_size": len(blob.encode("utf-8")), "created_at": _utc_now()},
        )
    )
    await session.execute(stmt)
    return root


async def get_snapshot(session: AsyncSession, *, tenant_id: str,
                       run_id: str) -> dict | None:
    """The frozen report for a run, tagged ``source: "snapshot"`` plus a live
    integrity check of its own chain root. None when absent/pre-migration."""
    try:
        row = (await session.execute(
            select(E2ERunReportRow).where(
                E2ERunReportRow.run_id == run_id,
                E2ERunReportRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
    except Exception as exc:
        logger.debug("report_store.fetch_skipped run=%s err=%s", run_id, exc)
        return None
    if row is None:
        return None
    try:
        report = json.loads(row.report_json)
    except Exception:
        logger.warning("report_store.corrupt_snapshot run=%s", run_id)
        return None
    recomputed = snapshot_chain_root(report)
    report["source"] = "snapshot"
    report["snapshot"] = {
        "frozen_at": row.created_at.isoformat() if row.created_at else None,
        "chain_root": row.chain_root,
        "recomputed_chain_root": recomputed,
        "integrity_ok": bool(recomputed == row.chain_root),
        "byte_size": row.byte_size,
        "note": ("This is the report as it stood when the run landed. The live "
                 "report is recomputed from current data and may differ if cases "
                 "were regenerated since — that difference is a finding, not "
                 "something to reconcile silently."),
    }
    return report


async def list_snapshots(session: AsyncSession, *, tenant_id: str,
                         artifact_id: str, limit: int = 50) -> list[dict]:
    try:
        rows = (await session.execute(
            select(E2ERunReportRow)
            .where(E2ERunReportRow.artifact_id == artifact_id,
                   E2ERunReportRow.tenant_id == tenant_id)
            .order_by(E2ERunReportRow.created_at.desc())
            .limit(limit)
        )).scalars().all()
    except Exception:
        return []
    return [{"run_id": r.run_id, "environment": r.environment,
             "frozen_at": r.created_at.isoformat() if r.created_at else None,
             "chain_root": r.chain_root, "byte_size": r.byte_size}
            for r in rows]


__all__ = ["E2ERunReportRow", "MAX_SNAPSHOT_BYTES", "snapshot_chain_root",
           "save_snapshot", "get_snapshot", "list_snapshots"]
