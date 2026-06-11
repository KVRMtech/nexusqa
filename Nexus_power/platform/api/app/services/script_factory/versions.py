"""Phase C — per-test Script/Data version store + helpers.

Additive: the deterministic compiler stays the source of truth; an *edited
version*, when present, overrides only that one test's compiled spec at run time.
Save is append-only (a new immutable ``version_no``); Restore copies a prior
version's content into a new highest version. The "active" version for a test is
simply its highest ``version_no``.

The ORM model is defined here in app code (not in the shared SDK) and binds the
SDK's declarative ``Base`` so it shares the metadata/registry. The table is
created out-of-band by an idempotent migration/SQL (see infrastructure SQL);
nothing here auto-creates it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base


def _new_id() -> str:
    return uuid.uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScriptVersionRow(Base):
    """One immutable, per-test edited Script/Data version (Phase C editor)."""

    __tablename__ = "script_versions"

    script_version_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    test_case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("factory_test_cases.test_case_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    spec_path: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    script_source: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    author: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False,
    )

    __table_args__ = (
        Index("ux_script_versions_test_version", "test_case_id", "version_no", unique=True),
        Index("ix_script_versions_artifact_version", "artifact_id", "version_no"),
        Index("ix_script_versions_tenant_test", "tenant_id", "test_case_id"),
    )


# ─── Helpers (tenant isolation enforced by RLS via tenant_scoped_session) ──────


async def list_script_versions(
    session: AsyncSession, *, artifact_id: str, test_case_id: str,
) -> list[dict[str, Any]]:
    """Version history for one test, newest first (no source bodies)."""
    rows = (
        await session.execute(
            select(ScriptVersionRow)
            .where(
                ScriptVersionRow.artifact_id == artifact_id,
                ScriptVersionRow.test_case_id == test_case_id,
            )
            .order_by(ScriptVersionRow.version_no.desc())
        )
    ).scalars().all()
    return [
        {
            "script_version_id": r.script_version_id,
            "version_no": r.version_no,
            "spec_path": r.spec_path,
            "author": r.author,
            "note": r.note,
            "has_data": bool(r.data_json),
            "pending_approval": _is_pending(r),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def _is_pending(row: ScriptVersionRow) -> bool:
    """A PROPOSED (auto-healed) version awaiting human approval — saved + visible in
    history, but NOT used by runs until approved. Carried in ``data_json`` so the
    gate needs NO schema change."""
    return bool((getattr(row, "data_json", None) or {}).get("pending_approval"))


async def _max_version_no(
    session: AsyncSession, *, artifact_id: str, test_case_id: str,
) -> int:
    """Highest version_no for a test (INCLUDING proposed) — for numbering only, so a
    new version never collides with a pending one."""
    v = (
        await session.execute(
            select(ScriptVersionRow.version_no)
            .where(
                ScriptVersionRow.artifact_id == artifact_id,
                ScriptVersionRow.test_case_id == test_case_id,
            )
            .order_by(ScriptVersionRow.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(v or 0)


async def get_active_version(
    session: AsyncSession, *, artifact_id: str, test_case_id: str,
) -> ScriptVersionRow | None:
    """The ACTIVE version for a test = highest APPROVED version_no, or None. A
    proposed (pending-approval) version is skipped — it is never what runs until a
    human approves it (the auto-persist gate)."""
    rows = (
        await session.execute(
            select(ScriptVersionRow)
            .where(
                ScriptVersionRow.artifact_id == artifact_id,
                ScriptVersionRow.test_case_id == test_case_id,
            )
            .order_by(ScriptVersionRow.version_no.desc())
        )
    ).scalars().all()
    for r in rows:
        if not _is_pending(r):
            return r
    return None


async def get_version(
    session: AsyncSession, *, artifact_id: str, test_case_id: str, version_no: int,
) -> ScriptVersionRow | None:
    return (
        await session.execute(
            select(ScriptVersionRow)
            .where(
                ScriptVersionRow.artifact_id == artifact_id,
                ScriptVersionRow.test_case_id == test_case_id,
                ScriptVersionRow.version_no == version_no,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_versions_for_artifact(
    session: AsyncSession, *, artifact_id: str,
) -> dict[str, ScriptVersionRow]:
    """test_id -> active (highest version_no) edited row for the whole artifact.
    Single query; used by the run path to override compiled specs in bulk."""
    rows = (
        await session.execute(
            select(ScriptVersionRow)
            .where(ScriptVersionRow.artifact_id == artifact_id)
            .order_by(ScriptVersionRow.test_case_id, ScriptVersionRow.version_no.desc())
        )
    ).scalars().all()
    out: dict[str, ScriptVersionRow] = {}
    for r in rows:  # first APPROVED per test_case_id wins (desc order)
        if _is_pending(r):
            continue  # a proposed/pending version never runs until approved
        out.setdefault(r.test_case_id, r)
    return out


async def save_new_version(
    session: AsyncSession, *, artifact_id: str, tenant_id: str, session_id: str,
    test_case_id: str, spec_path: str, script_source: str,
    data_json: dict, author: str, note: str = "", proposed: bool = False,
) -> ScriptVersionRow:
    """Append an immutable version = (current max version_no) + 1.

    ``proposed=True`` saves it PENDING HUMAN APPROVAL (carried in data_json): the
    version is stored + visible in history but is NOT active — runs keep using the
    prior approved version until a human approves it. Used by the TrueFix / Auto-Heal
    flow so a machine-written fix never silently becomes the source of truth.
    Numbering uses the true max (incl. proposed) so version_no never collides."""
    next_no = (await _max_version_no(
        session, artifact_id=artifact_id, test_case_id=test_case_id,
    )) + 1
    dj = dict(data_json or {})
    if proposed:
        dj["pending_approval"] = True
    row = ScriptVersionRow(
        script_version_id=_new_id(),
        test_case_id=test_case_id,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id or "",
        spec_path=spec_path or "",
        version_no=next_no,
        script_source=script_source or "",
        data_json=dj,
        author=author or "",
        note=note or "",
    )
    session.add(row)
    return row


async def approve_version(
    session: AsyncSession, *, artifact_id: str, test_case_id: str, version_no: int,
) -> ScriptVersionRow | None:
    """Approve a PROPOSED version → it becomes active (the data_json pending flag is
    cleared). The human gate for auto-healed versions. Returns the row, or None if
    not found. Idempotent. Caller commits."""
    row = await get_version(
        session, artifact_id=artifact_id, test_case_id=test_case_id, version_no=version_no,
    )
    if row is None:
        return None
    dj = dict(row.data_json or {})
    if dj.get("pending_approval"):
        dj.pop("pending_approval", None)
        dj["approved_at"] = _utc_now().isoformat()
        row.data_json = dj
    return row


async def batch_save_clean_run_version(
    session: AsyncSession, *, artifact_id: str, tenant_id: str,
    healed: list[dict], clean_run_session_id: str, n_healed: int,
) -> list[ScriptVersionRow]:
    """Persist a "Clean Run - V1" — one immutable, attributed version per healed
    test, all stamped with a shared ``clean_run_session_id`` (in ``data_json``) so
    they read as one verified set. Written by the Auto-Heal loop ONLY after the
    whole selected suite re-ran green. Each becomes the test's active version, so a
    later plain run picks it up. Append-only + reversible via /restore; the baseline
    recording is never mutated. No schema change.

    ``healed`` = [{test_case_id, spec_path, script_source}].
    """
    saved: list[ScriptVersionRow] = []
    for h in healed:
        tcid = (h or {}).get("test_case_id") or ""
        src = (h or {}).get("script_source") or ""
        if not tcid or not src:
            continue
        row = await save_new_version(
            session, artifact_id=artifact_id, tenant_id=tenant_id, session_id="",
            test_case_id=tcid, spec_path=(h.get("spec_path") or ""),
            script_source=src, data_json={
                "clean_run_session_id": clean_run_session_id,
                "auto_healed": True,
            },
            author="nexus-autoheal",
            note=f"Clean Run - V1 (auto-healed {n_healed} step(s), verified green)",
            proposed=True,  # human-gated: approve before it becomes the active source
        )
        saved.append(row)
    return saved
