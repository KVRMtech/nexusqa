"""Test Factory service — bridges the frozen Pages & Forms ORM data to the
generator, and persists the generated cases into ``factory_test_cases``.

All reads are tenant-scoped (the caller opens a ``tenant_scoped_session`` so
Postgres RLS is active).  Persistence is idempotent: re-generating an artifact
UPSERTs by the generator's deterministic ``test_case_id`` and prunes active
rows that the new run no longer produces.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    CombinationReserveRow,
    FactoryTestCaseRow,
    PageActionRow,
    PageVisitRow,
)
from nexus_sdk.models import ProductionTestCase

from .combinations import generate_combination_cases
from .confidence import annotate as annotate_confidence, compute_ambiguous_labels
from .negatives import CATEGORY_GENERATORS
from .generator import (
    DemonstratedGenerationResult,
    PageActionInput,
    PageVisitInput,
    generate_demonstrated_test_cases,
)

GENERATOR_VERSION = "v1"
_RESERVE_NAMESPACE = "test-factory-reserve"


async def _latest_version(
    session: AsyncSession, model, artifact_id: str,
) -> str | None:
    """Return the extractor_version of the most recently written row.

    Avoids lexical ``max()`` pitfalls (``v9`` vs ``v11``) by ordering on
    ``created_at``.
    """
    res = await session.execute(
        select(model.extractor_version)
        .where(model.artifact_id == artifact_id)
        .order_by(model.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _load_current_pages_and_actions(
    session: AsyncSession, *, artifact_id: str,
) -> tuple[list[PageVisitInput], list[PageActionInput]]:
    """Load the CURRENT-version page_visits + page_actions as generator inputs."""
    visit_version = await _latest_version(session, PageVisitRow, artifact_id)
    if visit_version is None:
        return [], []

    visit_rows = (
        await session.execute(
            select(PageVisitRow)
            .where(
                PageVisitRow.artifact_id == artifact_id,
                PageVisitRow.extractor_version == visit_version,
            )
            .order_by(PageVisitRow.sequence_index.asc())
        )
    ).scalars().all()

    visits = [
        PageVisitInput(
            page_visit_id=v.page_visit_id,
            sequence_index=v.sequence_index,
            location=v.location or "",
            url_host=v.url_host or "",
            url_path=v.url_path or "",
            url_query=v.url_query or "",
            canonical_host=v.canonical_host or "",
            source=v.source or "",
            form_snapshot={
                str(k): ("" if val is None else str(val))
                for k, val in (v.form_snapshot or {}).items()
            },
            form_snapshot_signals=dict(v.form_snapshot_signals or {}),
            first_seen_ms=v.first_seen_ms or 0,
            duration_ms=v.duration_ms or 0,
        )
        for v in visit_rows
    ]

    visit_ids = [v.page_visit_id for v in visit_rows]
    actions: list[PageActionInput] = []
    if visit_ids:
        action_version = await _latest_version(session, PageActionRow, artifact_id)
        action_rows = (
            await session.execute(
                select(PageActionRow).where(
                    PageActionRow.page_visit_id.in_(visit_ids),
                    PageActionRow.extractor_version == action_version,
                )
            )
        ).scalars().all()
        actions = []
        for a in action_rows:
            sig = a.evidence_signals or {}
            anchor_sig = sig.get("anchor") or {}
            after_sig = sig.get("after") or {}
            actions.append(PageActionInput(
                page_visit_id=a.page_visit_id,
                subaction_index=a.subaction_index,
                verb=a.verb or "",
                target_label=a.target_label or "",
                target_kind=a.target_kind or "",
                value=a.value,
                anchor=str(anchor_sig.get("label") or ""),
                anchor_kind=str(anchor_sig.get("kind") or ""),
                after_outcome=str(after_sig.get("outcome") or ""),
                after_detail=str(after_sig.get("detail") or ""),
            ))

    return visits, actions


def _row_values(
    tc: ProductionTestCase, *, artifact_id: str, tenant_id: str, session_id: str,
    confidence: str, source_evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "test_case_id": tc.test_id,
        "artifact_id": artifact_id,
        "tenant_id": tenant_id,
        "session_id": session_id or "",
        "name": (tc.name or "")[:500],
        "description": tc.description or "",
        "priority": tc.priority or "P2_medium",
        "test_type": tc.type or "functional",
        "confidence": confidence,
        "status": "active",
        "step_count": len(tc.steps),
        "tags": list(tc.tags or []),
        "test_case": tc.model_dump(mode="json"),
        "source_evidence": source_evidence,
        "generator_version": GENERATOR_VERSION,
    }


async def _upsert_case(session: AsyncSession, values: dict[str, Any]) -> None:
    stmt = pg_insert(FactoryTestCaseRow).values(**values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[FactoryTestCaseRow.test_case_id],
        set_={
            k: values[k] for k in (
                "name", "description", "priority", "test_type", "confidence",
                "status", "step_count", "tags", "test_case", "source_evidence",
                "generator_version",
            )
        } | {"updated_at": func.now()},
    ))


def _dominant_host(visits: list[PageVisitInput]) -> str:
    for v in visits:
        if v.canonical_host:
            return v.canonical_host
    for v in visits:
        if v.url_host:
            return v.url_host
    return ""


async def generate_and_store(
    session: AsyncSession, *, artifact_id: str, tenant_id: str, session_id: str = "",
) -> dict[str, Any]:
    """Generate demonstrated + combination test cases and persist them.

    * Demonstrated functional E2E  → ``factory_test_cases`` (confidence=demonstrated)
    * Critical combinations (grounded available options) → ``factory_test_cases``
      (confidence=available) + the full spec into ``e2e_combination_reserve``.
    Idempotent: UPSERTs by ``test_case_id`` and prunes stale active rows.
    """
    visits, actions = await _load_current_pages_and_actions(
        session, artifact_id=artifact_id,
    )

    result = generate_demonstrated_test_cases(
        artifact_id=artifact_id, page_visits=visits, page_actions=actions,
    )
    ambiguous = compute_ambiguous_labels(actions)
    demonstrated_meta = {
        "page_groups": result.page_groups,
        "visits_total": result.visits_total,
        "visits_used": result.visits_used,
        "fields_demonstrated": result.fields_demonstrated,
        "excluded_placeholder_fields": result.excluded_placeholder_fields,
    }

    new_ids: list[str] = []
    for tc in result.test_cases:
        annotate_confidence(tc, ambiguous)
        values = _row_values(
            tc, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=session_id, confidence="demonstrated",
            source_evidence=demonstrated_meta,
        )
        new_ids.append(values["test_case_id"])
        await _upsert_case(session, values)

    # ── Phase 2 — critical combinations from captured option domains ──────
    base_case = result.test_cases[0] if result.test_cases else None
    combo = generate_combination_cases(
        artifact_id=artifact_id, base_case=base_case,
        page_visits=visits, host=_dominant_host(visits),
    )
    for tc in combo.active:
        annotate_confidence(tc, ambiguous)
        values = _row_values(
            tc, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=session_id, confidence="available",
            source_evidence={
                "combination": True,
                "base_test_id": combo.generation_spec.get("base_test_id"),
            },
        )
        new_ids.append(values["test_case_id"])
        await _upsert_case(session, values)

    # Prune active rows this run no longer produces.
    prune = delete(FactoryTestCaseRow).where(
        FactoryTestCaseRow.artifact_id == artifact_id,
        FactoryTestCaseRow.status == "active",
        FactoryTestCaseRow.generator_version == GENERATOR_VERSION,
    )
    if new_ids:
        prune = prune.where(FactoryTestCaseRow.test_case_id.notin_(new_ids))
    await session.execute(prune)

    # ── Reserve — store the full option space + spec (lossless) ───────────
    reserve_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{_RESERVE_NAMESPACE}:{artifact_id}:{GENERATOR_VERSION}",
    ))
    reserve_values = {
        "reserve_id": reserve_id,
        "artifact_id": artifact_id,
        "tenant_id": tenant_id,
        "session_id": session_id or "",
        "option_domains": combo.option_domains,
        "generation_spec": combo.generation_spec,
        "combination_count": combo.full_count,
        "selected_count": combo.selected_count,
        "status": "reserve",
        "generator_version": GENERATOR_VERSION,
    }
    res_stmt = pg_insert(CombinationReserveRow).values(**reserve_values)
    await session.execute(res_stmt.on_conflict_do_update(
        index_elements=[CombinationReserveRow.reserve_id],
        set_={
            k: reserve_values[k] for k in (
                "option_domains", "generation_spec", "combination_count",
                "selected_count", "status",
            )
        } | {"updated_at": func.now()},
    ))

    await session.commit()

    return {
        "artifact_id": artifact_id,
        "demonstrated": len(result.test_cases),
        "combinations_active": combo.selected_count,
        "combinations_full_space": combo.full_count,
        "option_domains": len(combo.option_domains),
        "generated": len(new_ids),
        **demonstrated_meta,
        "generator_version": GENERATOR_VERSION,
    }


async def generate_category(
    session: AsyncSession, *, artifact_id: str, tenant_id: str, category: str,
    session_id: str = "",
) -> dict[str, Any]:
    """On-demand generate a non-demonstrated category (negative|boundary|error_state).

    Grounded generators only; cases stored with confidence=inferred and
    test_type=<category>. Idempotent per category.
    """
    gen = CATEGORY_GENERATORS.get(category)
    if gen is None:
        raise ValueError(f"unknown category '{category}'")

    visits, actions = await _load_current_pages_and_actions(
        session, artifact_id=artifact_id,
    )
    demo = generate_demonstrated_test_cases(
        artifact_id=artifact_id, page_visits=visits, page_actions=actions,
    )
    base = demo.test_cases[0] if demo.test_cases else None
    cases = gen(
        artifact_id=artifact_id, base_case=base,
        page_visits=visits, host=_dominant_host(visits),
    )
    ambiguous = compute_ambiguous_labels(actions)

    new_ids: list[str] = []
    for tc in cases:
        annotate_confidence(tc, ambiguous)
        values = _row_values(
            tc, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=session_id, confidence="inferred",
            source_evidence={"category": category},
        )
        new_ids.append(values["test_case_id"])
        await _upsert_case(session, values)

    prune = delete(FactoryTestCaseRow).where(
        FactoryTestCaseRow.artifact_id == artifact_id,
        FactoryTestCaseRow.status == "active",
        FactoryTestCaseRow.test_type == category,
    )
    if new_ids:
        prune = prune.where(FactoryTestCaseRow.test_case_id.notin_(new_ids))
    await session.execute(prune)
    await session.commit()
    return {"category": category, "generated": len(cases)}


async def summarize(
    session: AsyncSession, *, artifact_id: str,
) -> dict[str, Any]:
    """Aggregate counts for the UI summary (small payload, never the full set)."""
    total = (
        await session.execute(
            select(func.count())
            .select_from(FactoryTestCaseRow)
            .where(FactoryTestCaseRow.artifact_id == artifact_id)
        )
    ).scalar_one()

    by_priority_rows = (
        await session.execute(
            select(FactoryTestCaseRow.priority, func.count())
            .where(
                FactoryTestCaseRow.artifact_id == artifact_id,
                FactoryTestCaseRow.status == "active",
            )
            .group_by(FactoryTestCaseRow.priority)
        )
    ).all()
    by_status_rows = (
        await session.execute(
            select(FactoryTestCaseRow.status, func.count())
            .where(FactoryTestCaseRow.artifact_id == artifact_id)
            .group_by(FactoryTestCaseRow.status)
        )
    ).all()
    by_type_rows = (
        await session.execute(
            select(FactoryTestCaseRow.test_type, func.count())
            .where(
                FactoryTestCaseRow.artifact_id == artifact_id,
                FactoryTestCaseRow.status == "active",
            )
            .group_by(FactoryTestCaseRow.test_type)
        )
    ).all()
    by_conf_rows = (
        await session.execute(
            select(FactoryTestCaseRow.confidence, func.count())
            .where(
                FactoryTestCaseRow.artifact_id == artifact_id,
                FactoryTestCaseRow.status == "active",
            )
            .group_by(FactoryTestCaseRow.confidence)
        )
    ).all()

    return {
        "artifact_id": artifact_id,
        "total": int(total),
        "active": sum(c for _s, c in by_status_rows if _s == "active"),
        "reserve": sum(c for _s, c in by_status_rows if _s == "reserve"),
        "by_priority": {p: int(c) for p, c in by_priority_rows},
        "by_status": {s: int(c) for s, c in by_status_rows},
        "by_type": {t: int(c) for t, c in by_type_rows},
        "by_confidence": {c0: int(c) for c0, c in by_conf_rows},
    }


async def list_paginated(
    session: AsyncSession, *, artifact_id: str, page: int, limit: int,
    status: str = "active", test_type: str | None = None,
) -> dict[str, Any]:
    """Server-side paginated listing — the UI fetches one page at a time.

    Optional ``test_type`` filter lets the UI fetch a small capped slice per
    category (e.g. 5 demonstrated) without pulling the whole suite.
    """
    base = [
        FactoryTestCaseRow.artifact_id == artifact_id,
        FactoryTestCaseRow.status == status,
    ]
    if test_type:
        base.append(FactoryTestCaseRow.test_type == test_type)
    base = tuple(base)
    total = (
        await session.execute(
            select(func.count()).select_from(FactoryTestCaseRow).where(*base)
        )
    ).scalar_one()

    offset = (page - 1) * limit
    rows = (
        await session.execute(
            select(FactoryTestCaseRow)
            .where(*base)
            .order_by(
                FactoryTestCaseRow.priority.asc(),
                FactoryTestCaseRow.created_at.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    items = [
        {
            "test_case_id": r.test_case_id,
            "name": r.name,
            "description": r.description,
            "priority": r.priority,
            "type": r.test_type,
            "confidence": r.confidence,
            "status": r.status,
            "step_count": r.step_count,
            "tags": r.tags,
            "test_case": r.test_case,
        }
        for r in rows
    ]

    return {
        "artifact_id": artifact_id,
        "page": page,
        "limit": limit,
        "total": int(total),
        "items": items,
    }


async def load_active_production_cases(
    session: AsyncSession, *, artifact_id: str,
) -> list[ProductionTestCase]:
    """Rehydrate stored active cases into ProductionTestCase objects (for export)."""
    rows = (
        await session.execute(
            select(FactoryTestCaseRow)
            .where(
                FactoryTestCaseRow.artifact_id == artifact_id,
                FactoryTestCaseRow.status == "active",
            )
            .order_by(
                FactoryTestCaseRow.priority.asc(),
                FactoryTestCaseRow.created_at.asc(),
            )
        )
    ).scalars().all()
    return [ProductionTestCase(**r.test_case) for r in rows if r.test_case]


async def get_reserve(
    session: AsyncSession, *, artifact_id: str,
) -> dict[str, Any] | None:
    """Return the stored combination reserve (full option space + spec)."""
    row = (
        await session.execute(
            select(CombinationReserveRow)
            .where(CombinationReserveRow.artifact_id == artifact_id)
            .order_by(CombinationReserveRow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "artifact_id": artifact_id,
        "option_domains": row.option_domains,
        "generation_spec": row.generation_spec,
        "combination_count": row.combination_count,
        "selected_count": row.selected_count,
        "status": row.status,
    }
