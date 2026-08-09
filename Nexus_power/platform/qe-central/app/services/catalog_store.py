"""P2/P6 — Master Catalog persistence + regression store (the DB layer).

The pure catalog logic lives in ``catalog.py`` (build_master_catalog,
snapshot_catalog) and ``catalog_diff.py`` (diff_catalogs). This module is the
thin DB seam that:

  * ``build_app_master_catalog`` — aggregates the app's journey nodes/edges into
    the live Master Catalog for the read route (no persistence needed);
  * ``persist_catalog_version`` — at fold, upserts ``catalog_questions`` (the
    durable, deduped master) and writes a ``catalog_versions`` snapshot so a later
    crawl can diff against it (P6). Best-effort: never breaks the fold;
  * ``diff_latest_versions`` — loads the two most recent versions and diffs them.

All queries run inside ``tenant_scoped_qec_session`` (RLS-forced), so nothing
crosses a tenant boundary.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import (
    CatalogQuestionRow,
    CatalogVersionRow,
    JourneyBranchRow,
    JourneyEdgeRow,
    JourneyNodeRow,
)
from .catalog import build_master_catalog, snapshot_catalog
from .catalog_diff import diff_catalogs

logger = logging.getLogger(__name__)


def _sid(*parts: str) -> str:
    """Deterministic row id from a natural key — the upsert identity."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


async def _load_graph(
    session: Any, tenant_id: str, app_id: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Every node (with its control inventory), edge, and branch for the app —
    branches carry the questionnaire questions the Master Catalog folds in."""
    node_rows = (await session.execute(
        select(JourneyNodeRow).where(
            JourneyNodeRow.tenant_id == tenant_id,
            JourneyNodeRow.app_id == app_id,
        ))).scalars().all()
    edge_rows = (await session.execute(
        select(JourneyEdgeRow).where(
            JourneyEdgeRow.tenant_id == tenant_id,
            JourneyEdgeRow.app_id == app_id,
        ))).scalars().all()
    branch_rows = (await session.execute(
        select(JourneyBranchRow).where(
            JourneyBranchRow.tenant_id == tenant_id,
            JourneyBranchRow.app_id == app_id,
        ))).scalars().all()
    nodes = [{
        "node_fp": n.fingerprint,
        "url": n.url,
        "title": n.title,
        "controls_inventory": list(n.controls_inventory or []),
    } for n in node_rows]
    edges = [{"from_fp": e.from_fp, "to_fp": e.to_fp} for e in edge_rows]
    branches = [{
        "node_fp": b.node_fp,
        "control_signature": b.control_signature,
        "control_label_norm": b.control_label_norm,
        "option_label_norm": b.option_label_norm,
    } for b in branch_rows]
    return nodes, edges, branches


async def build_app_master_catalog(tenant_id: str, app_id: str) -> dict[str, Any]:
    """The live app-scoped Master Catalog, aggregated from the journey graph.

    Read path for ``GET /apps/{app_id}/catalog`` — deduped by stable question_id
    across every journey/node, so the 400 questions appear once.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        nodes, edges, branches = await _load_graph(session, tenant_id, app_id)
    return build_master_catalog(nodes, edges=edges, branches=branches)


async def persist_catalog_version(
    tenant_id: str, app_id: str, artifact_id: str
) -> dict[str, Any]:
    """Upsert ``catalog_questions`` and snapshot a ``catalog_versions`` row.

    Called once at the end of a fold so the Master Catalog is durable and a later
    crawl can diff against this version. Idempotent per (tenant, app, question_id)
    and (tenant, app, artifact_id). Returns a small report.
    """
    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        nodes, edges, branches = await _load_graph(session, tenant_id, app_id)
        master = build_master_catalog(nodes, edges=edges, branches=branches)
        questions = master.get("questions") or []

        upserted = 0
        for q in questions:
            qid = str(q.get("question_id") or "")
            if not qid:
                continue
            row = (await session.execute(
                select(CatalogQuestionRow).where(
                    CatalogQuestionRow.tenant_id == tenant_id,
                    CatalogQuestionRow.app_id == app_id,
                    CatalogQuestionRow.question_id == qid,
                ))).scalar_one_or_none()
            pages = [str(p) for p in (q.get("pages") or [])][:64]
            validation = q.get("validation") if isinstance(q.get("validation"), dict) else None
            if row is None:
                session.add(CatalogQuestionRow(
                    cq_id=_sid("cq", tenant_id, app_id, qid),
                    tenant_id=tenant_id, app_id=app_id, question_id=qid,
                    name=str(q.get("name") or "")[:300],
                    answer_type=str(q.get("type") or q.get("answer_type") or "text")[:40],
                    required=bool(q.get("required")),
                    options=[str(o) for o in (q.get("options") or [])][:48],
                    validation=validation,
                    business_rule=str(q.get("business_rule") or "")[:500],
                    expected_next_page=str(q.get("expected_next_page") or "")[:200],
                    semantic_type=str(q.get("semantic_type") or "")[:80],
                    provenance=str(q.get("provenance") or "observed")[:24],
                    pages=pages,
                    first_seen_artifact=artifact_id[:64],
                    last_seen_artifact=artifact_id[:64],
                    first_seen_at=now, last_seen_at=now,
                ))
                upserted += 1
            else:
                # Keep the richest: required sticky, fill options/validation, merge pages.
                row.required = bool(row.required) or bool(q.get("required"))
                if not row.options and q.get("options"):
                    row.options = [str(o) for o in q["options"]][:48]
                if row.validation is None and validation:
                    row.validation = validation
                if q.get("expected_next_page"):
                    row.expected_next_page = str(q["expected_next_page"])[:200]
                merged_pages = list(row.pages or [])
                for p in pages:
                    if p not in merged_pages:
                        merged_pages.append(p)
                row.pages = merged_pages[:64]
                row.last_seen_artifact = artifact_id[:64]
                row.last_seen_at = now

        snap = snapshot_catalog(master, artifact_id=artifact_id)
        version = (await session.execute(
            select(CatalogVersionRow).where(
                CatalogVersionRow.tenant_id == tenant_id,
                CatalogVersionRow.app_id == app_id,
                CatalogVersionRow.artifact_id == artifact_id,
            ))).scalar_one_or_none()
        if version is None:
            session.add(CatalogVersionRow(
                version_id=_sid("catver", tenant_id, app_id, artifact_id),
                tenant_id=tenant_id, app_id=app_id, artifact_id=artifact_id[:64],
                snapshot_hash=snap["snapshot_hash"],
                question_count=snap["question_count"],
                snapshot=snap, created_at=now,
            ))
        else:
            version.snapshot_hash = snap["snapshot_hash"]
            version.question_count = snap["question_count"]
            version.snapshot = snap

    return {"questions_upserted": upserted, "question_count": len(questions),
            "snapshot_hash": snap["snapshot_hash"]}


async def diff_latest_versions(
    tenant_id: str, app_id: str, limit_older: int = 2
) -> dict[str, Any]:
    """Diff the two most recent catalog versions (P6 regression).

    Returns ``{from, to, diff}`` — ``diff`` from ``catalog_diff.diff_catalogs``.
    With fewer than two versions there is nothing to diff yet.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CatalogVersionRow).where(
                CatalogVersionRow.tenant_id == tenant_id,
                CatalogVersionRow.app_id == app_id,
            ).order_by(CatalogVersionRow.created_at.desc()).limit(limit_older)
        )).scalars().all()
    if len(rows) < 2:
        return {"from": None, "to": None, "diff": None,
                "reason": "need at least two catalog versions to diff"}
    newer, older = rows[0], rows[1]
    diff = diff_catalogs(older.snapshot or {}, newer.snapshot or {})
    return {
        "from": {"artifact_id": older.artifact_id, "hash": older.snapshot_hash,
                 "question_count": older.question_count},
        "to": {"artifact_id": newer.artifact_id, "hash": newer.snapshot_hash,
               "question_count": newer.question_count},
        "diff": diff,
    }
