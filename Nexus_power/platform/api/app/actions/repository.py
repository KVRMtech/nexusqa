"""Repository for action_invocations + synthesized_tours + impact_analyses."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


_md = sa.MetaData()


action_invocations = sa.Table(
    "action_invocations",
    _md,
    sa.Column("invocation_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("trigger_dispatch_id", sa.String(64)),
    sa.Column("trigger_user_id", sa.String(128)),
    sa.Column("trace_id", sa.String(64)),
    sa.Column("idempotency_key", sa.String(128)),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("result", JSONB, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("latency_ms", sa.Integer),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
)


synthesized_tours = sa.Table(
    "synthesized_tours",
    _md,
    sa.Column("tour_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("title", sa.String(256), nullable=False),
    sa.Column("persona", sa.String(64)),
    sa.Column("target_minutes", sa.Integer),
    sa.Column("playlist", JSONB, nullable=False),
    sa.Column("coverage", JSONB, nullable=False),
    sa.Column("atlas_node_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_by", sa.String(128)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


impact_analyses = sa.Table(
    "impact_analyses",
    _md,
    sa.Column("analysis_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("root_atlas_node_id", sa.String(64), nullable=False),
    sa.Column("change_description", sa.Text),
    sa.Column("downstream", JSONB, nullable=False),
    sa.Column("upstream", JSONB, nullable=False),
    sa.Column("layer_summary", JSONB, nullable=False),
    sa.Column("estimated_blast_radius", sa.Integer, nullable=False),
    sa.Column("requested_by", sa.String(128)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
)


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ActionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sf = session_factory

    # ── Invocations ─────────────────────────────────────────────

    async def open_invocation(
        self,
        *,
        tenant_id: str,
        kind: str,
        request: dict[str, Any],
        trigger_dispatch_id: Optional[str] = None,
        trigger_user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create an invocation row in 'queued' state.

        On idempotency-key collision returns the existing row so the
        caller can decide whether to wait or re-issue.
        """
        invocation_id = uuid.uuid4().hex
        now = _now()
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = pg_insert(action_invocations).values(
                invocation_id=invocation_id,
                tenant_id=tenant_id,
                kind=kind,
                trigger_dispatch_id=trigger_dispatch_id,
                trigger_user_id=trigger_user_id,
                trace_id=trace_id,
                idempotency_key=idempotency_key,
                request=request,
                result={},
                status="queued",
                created_at=now,
            )
            if idempotency_key:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=[
                        action_invocations.c.tenant_id,
                        action_invocations.c.kind,
                        action_invocations.c.idempotency_key,
                    ],
                )
            await session.execute(stmt)
            row = (
                await session.execute(
                    sa.select(action_invocations).where(
                        action_invocations.c.tenant_id == tenant_id,
                        sa.or_(
                            action_invocations.c.invocation_id == invocation_id,
                            sa.and_(
                                idempotency_key is not None,
                                action_invocations.c.kind == kind,
                                action_invocations.c.idempotency_key
                                == idempotency_key,
                            ),
                        )
                        if idempotency_key
                        else action_invocations.c.invocation_id == invocation_id,
                    ).order_by(action_invocations.c.created_at.asc())
                    .limit(1)
                )
            ).mappings().first()
            await session.commit()
        if row is None:
            raise RuntimeError("invocation insert produced no row")
        return dict(row)

    async def mark_running(self, *, tenant_id: str, invocation_id: str) -> None:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            await session.execute(
                sa.update(action_invocations)
                .where(
                    action_invocations.c.tenant_id == tenant_id,
                    action_invocations.c.invocation_id == invocation_id,
                )
                .values(status="running", started_at=_now())
            )
            await session.commit()

    async def mark_completed(
        self,
        *,
        tenant_id: str,
        invocation_id: str,
        status: str,
        result: dict[str, Any],
        error: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        if status not in ("succeeded", "failed", "cancelled"):
            raise ValueError(f"invalid terminal status: {status}")
        now = _now()
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            await session.execute(
                sa.update(action_invocations)
                .where(
                    action_invocations.c.tenant_id == tenant_id,
                    action_invocations.c.invocation_id == invocation_id,
                )
                .values(
                    status=status,
                    result=result,
                    error=(error[:8000] if error else None),
                    latency_ms=latency_ms,
                    completed_at=now,
                )
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(action_invocations).where(
                        action_invocations.c.tenant_id == tenant_id,
                        action_invocations.c.invocation_id == invocation_id,
                    )
                )
            ).mappings().first()
        if row is None:
            raise RuntimeError("invocation missing after completion")
        return dict(row)

    async def get_invocation(
        self, *, tenant_id: str, invocation_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(action_invocations).where(
                        action_invocations.c.tenant_id == tenant_id,
                        action_invocations.c.invocation_id == invocation_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_invocations(
        self,
        *,
        tenant_id: str,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(action_invocations).where(
                action_invocations.c.tenant_id == tenant_id
            )
            if kind:
                stmt = stmt.where(action_invocations.c.kind == kind)
            stmt = stmt.order_by(action_invocations.c.created_at.desc()).limit(
                max(1, min(500, int(limit)))
            )
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def quota_count_since(
        self,
        *,
        tenant_id: str,
        kind: str,
        since: datetime,
        only_running_or_succeeded: bool = True,
    ) -> int:
        """How many invocations of ``kind`` started since ``since``.

        Used by the sandbox runner to enforce per-tenant daily budgets.
        """
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(sa.func.count()).where(
                action_invocations.c.tenant_id == tenant_id,
                action_invocations.c.kind == kind,
                action_invocations.c.created_at >= since,
            )
            if only_running_or_succeeded:
                stmt = stmt.where(
                    action_invocations.c.status.in_(("running", "succeeded"))
                )
            return int((await session.execute(stmt)).scalar_one())

    # ── Tours ───────────────────────────────────────────────────

    async def save_tour(
        self,
        *,
        tenant_id: str,
        product_id: str,
        title: str,
        persona: Optional[str],
        target_minutes: Optional[int],
        playlist: list[dict[str, Any]],
        coverage: dict[str, Any],
        atlas_node_ids: list[str],
        status: str,
        created_by: Optional[str],
    ) -> dict[str, Any]:
        tour_id = uuid.uuid4().hex
        now = _now()
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            await session.execute(
                sa.insert(synthesized_tours).values(
                    tour_id=tour_id,
                    tenant_id=tenant_id,
                    product_id=product_id,
                    title=title[:256],
                    persona=(persona[:64] if persona else None),
                    target_minutes=target_minutes,
                    playlist=playlist,
                    coverage=coverage,
                    atlas_node_ids=list(atlas_node_ids),
                    status=status,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(synthesized_tours).where(
                        synthesized_tours.c.tour_id == tour_id,
                    )
                )
            ).mappings().first()
        if row is None:
            raise RuntimeError("tour insert produced no row")
        return dict(row)

    async def get_tour(
        self, *, tenant_id: str, tour_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(synthesized_tours).where(
                        synthesized_tours.c.tenant_id == tenant_id,
                        synthesized_tours.c.tour_id == tour_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_tours(
        self,
        *,
        tenant_id: str,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(synthesized_tours).where(
                synthesized_tours.c.tenant_id == tenant_id
            )
            if product_id:
                stmt = stmt.where(
                    synthesized_tours.c.product_id == product_id
                )
            if status:
                stmt = stmt.where(synthesized_tours.c.status == status)
            stmt = stmt.order_by(
                synthesized_tours.c.created_at.desc()
            ).limit(max(1, min(500, int(limit))))
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def update_tour_status(
        self, *, tenant_id: str, tour_id: str, status: str
    ) -> Optional[dict[str, Any]]:
        if status not in ("draft", "published", "archived"):
            raise ValueError(f"invalid tour status: {status}")
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            result = await session.execute(
                sa.update(synthesized_tours)
                .where(
                    synthesized_tours.c.tenant_id == tenant_id,
                    synthesized_tours.c.tour_id == tour_id,
                )
                .values(status=status)
            )
            if result.rowcount == 0:
                return None
            await session.commit()
            row = (
                await session.execute(
                    sa.select(synthesized_tours).where(
                        synthesized_tours.c.tour_id == tour_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    # ── Impact analyses ────────────────────────────────────────

    async def save_impact_analysis(
        self,
        *,
        tenant_id: str,
        product_id: str,
        root_atlas_node_id: str,
        change_description: Optional[str],
        downstream: list[dict[str, Any]],
        upstream: list[dict[str, Any]],
        layer_summary: dict[str, Any],
        estimated_blast_radius: int,
        requested_by: Optional[str],
        expires_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        analysis_id = uuid.uuid4().hex
        now = _now()
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            await session.execute(
                sa.insert(impact_analyses).values(
                    analysis_id=analysis_id,
                    tenant_id=tenant_id,
                    product_id=product_id,
                    root_atlas_node_id=root_atlas_node_id,
                    change_description=change_description,
                    downstream=downstream,
                    upstream=upstream,
                    layer_summary=layer_summary,
                    estimated_blast_radius=estimated_blast_radius,
                    requested_by=requested_by,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(impact_analyses).where(
                        impact_analyses.c.analysis_id == analysis_id,
                    )
                )
            ).mappings().first()
        if row is None:
            raise RuntimeError("impact insert produced no row")
        return dict(row)

    async def get_impact_analysis(
        self, *, tenant_id: str, analysis_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(impact_analyses).where(
                        impact_analyses.c.tenant_id == tenant_id,
                        impact_analyses.c.analysis_id == analysis_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_impact_analyses(
        self,
        *,
        tenant_id: str,
        product_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(impact_analyses).where(
                impact_analyses.c.tenant_id == tenant_id
            )
            if product_id:
                stmt = stmt.where(impact_analyses.c.product_id == product_id)
            stmt = stmt.order_by(impact_analyses.c.created_at.desc()).limit(
                max(1, min(200, int(limit)))
            )
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]
