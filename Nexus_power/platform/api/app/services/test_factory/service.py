"""Test Factory service — bridges the frozen Pages & Forms ORM data to the
generator, and persists the generated cases into ``factory_test_cases``.

All reads are tenant-scoped (the caller opens a ``tenant_scoped_session`` so
Postgres RLS is active).  Persistence is idempotent: re-generating an artifact
UPSERTs by the generator's deterministic ``test_case_id`` and prunes active
rows that the new run no longer produces.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    FactoryTestCaseRow,
    PageActionRow,
    PageVisitRow,
)
from nexus_sdk.models import ProductionTestCase

from .generator import (
    DemonstratedGenerationResult,
    PageActionInput,
    PageVisitInput,
    generate_demonstrated_test_cases,
)

GENERATOR_VERSION = "v1"


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
        actions = [
            PageActionInput(
                page_visit_id=a.page_visit_id,
                subaction_index=a.subaction_index,
                verb=a.verb or "",
                target_label=a.target_label or "",
                target_kind=a.target_kind or "",
                value=a.value,
            )
            for a in action_rows
        ]

    return visits, actions


def _row_values(
    tc: ProductionTestCase, *, artifact_id: str, tenant_id: str, session_id: str,
    result: DemonstratedGenerationResult,
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
        "confidence": "demonstrated",
        "status": "active",
        "step_count": len(tc.steps),
        "tags": list(tc.tags or []),
        "test_case": tc.model_dump(mode="json"),
        "source_evidence": {
            "page_groups": result.page_groups,
            "visits_total": result.visits_total,
            "visits_used": result.visits_used,
            "fields_demonstrated": result.fields_demonstrated,
            "excluded_placeholder_fields": result.excluded_placeholder_fields,
        },
        "generator_version": GENERATOR_VERSION,
    }


async def generate_and_store(
    session: AsyncSession, *, artifact_id: str, tenant_id: str, session_id: str = "",
) -> dict[str, Any]:
    """Generate demonstrated test cases for an artifact and persist them.

    Idempotent: UPSERTs by ``test_case_id`` and prunes stale active rows.
    Returns a summary dict.
    """
    visits, actions = await _load_current_pages_and_actions(
        session, artifact_id=artifact_id,
    )

    result = generate_demonstrated_test_cases(
        artifact_id=artifact_id, page_visits=visits, page_actions=actions,
    )

    new_ids: list[str] = []
    for tc in result.test_cases:
        values = _row_values(
            tc, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=session_id, result=result,
        )
        new_ids.append(values["test_case_id"])
        stmt = pg_insert(FactoryTestCaseRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FactoryTestCaseRow.test_case_id],
            set_={
                "name": values["name"],
                "description": values["description"],
                "priority": values["priority"],
                "test_type": values["test_type"],
                "confidence": values["confidence"],
                "status": values["status"],
                "step_count": values["step_count"],
                "tags": values["tags"],
                "test_case": values["test_case"],
                "source_evidence": values["source_evidence"],
                "generator_version": values["generator_version"],
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

    # Prune active demonstrated rows this run no longer produces.
    prune = delete(FactoryTestCaseRow).where(
        FactoryTestCaseRow.artifact_id == artifact_id,
        FactoryTestCaseRow.status == "active",
        FactoryTestCaseRow.generator_version == GENERATOR_VERSION,
    )
    if new_ids:
        prune = prune.where(FactoryTestCaseRow.test_case_id.notin_(new_ids))
    await session.execute(prune)
    await session.commit()

    return {
        "artifact_id": artifact_id,
        "generated": len(result.test_cases),
        "page_groups": result.page_groups,
        "visits_total": result.visits_total,
        "visits_used": result.visits_used,
        "fields_demonstrated": result.fields_demonstrated,
        "excluded_placeholder_fields": result.excluded_placeholder_fields,
        "generator_version": GENERATOR_VERSION,
    }


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

    return {
        "artifact_id": artifact_id,
        "total": int(total),
        "active": sum(c for _s, c in by_status_rows if _s == "active"),
        "reserve": sum(c for _s, c in by_status_rows if _s == "reserve"),
        "by_priority": {p: int(c) for p, c in by_priority_rows},
        "by_status": {s: int(c) for s, c in by_status_rows},
    }


async def list_paginated(
    session: AsyncSession, *, artifact_id: str, page: int, limit: int,
    status: str = "active",
) -> dict[str, Any]:
    """Server-side paginated listing — the UI fetches one page at a time."""
    base = (
        FactoryTestCaseRow.artifact_id == artifact_id,
        FactoryTestCaseRow.status == status,
    )
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
