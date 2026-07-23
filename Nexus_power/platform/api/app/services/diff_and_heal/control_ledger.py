"""Proven Control Ledger — app-level memo of oracle-VERIFIED heals (Phase 0 substrate).

A proven heal (a custom-combobox recipe, a renamed-control re-anchor, a wait/scope,
a control-kind correction) is a fact about a *control in an app*, not about the one
scenario that happened to discover it. Today that knowledge lives only in the
auto-heal loop's per-scenario ``overrides`` and is thrown away, so every other
scenario that touches the SAME control re-discovers the SAME fix from scratch.

This module is the durable, app-scoped store that lets a fix proven once be reused
everywhere — across scenarios AND across recordings of the same app. It is the
SUBSTRATE only (Phase 0): a stable control *fingerprint* + a tenant-scoped
read/write store. It is inert until the auto-heal loop is wired to WRITE proven
fixes (Phase 1) and SEED from them before a run (Phase 2). Reuse is ALWAYS
re-gated by the step's own grounded oracle + the 2x confirm, so a stale entry
fails RED and is re-healed — the ledger can never make a wrong test green.

GENERIC by construction: a fingerprint is derived ONLY from the recorded
accessibility signals every UI exposes (normalized accessible name + control kind
+ the page path it lives on) — never any domain-specific vocabulary, never a
hard-coded label. It works on any app.

Storage mirrors the ``e2e_run_screenshots`` / ``script_versions`` pattern exactly:
the ORM model binds the SDK ``Base`` (shared registry) but the table is created
out-of-band by an idempotent RLS migration (``scripts/apply_proven_control_ledger.sql``)
— nothing here auto-creates it. Every read/write DEGRADES SAFELY when the table
is absent (pre-migration): writes return False, reads return {} — so wiring this
in can never break the heal flow or a run.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

# Fix channels the auto-heal loop can prove green and reuse. Kept open (a free-text
# column, validated by the caller) so a new heal channel needs no schema change.
# nav/advance/nav_recover: the write-on-green path has always PASSED these kinds
# (test_factory.py proven-fix loop) but this gate silently dropped them
# (fail-open False, no trace) — so entry-URL corrections, wizard advances and
# nav recoveries were never memoized. Requirements-audit finding, R6.
FIX_KINDS = ("control_kind", "reanchor", "interaction", "wait",
             "nav", "advance", "nav_recover")

_FP_LEN = 40            # sha256 prefix — collision-safe, fits any VARCHAR comfortably
_MAX_LABEL = 400        # provenance label is stored readable; bound it defensively

# Phase 3 — INVALIDATION. A seed that fails its own first prove is "stale" (the app
# changed since the fix was proven). We bump ``stale_count`` and, once it reaches
# STALE_THRESHOLD *consecutive* misfires (a one-off flake should not nuke a trusted
# fix), QUARANTINE the row (``invalidated_at`` set) so it stops being SEEDED. The
# next from-scratch green re-proves the control and ``record_proven_fix`` REACTIVATES
# the row (resets stale_count + invalidated_at). Invalidation only ever REMOVES a
# seed — it makes the loop heal more from scratch, so it can NEVER green-wash.
STALE_THRESHOLD = 2

# Phase 4 (fuzzy, flag-gated) — minimum accessible-name similarity to fuzzy-reuse a
# reanchor across recordings whose labels drifted. HIGH on purpose: the seed loses
# resolve_reanchor's live role gate, and action_resolver._similarity's rename lift can
# reach ~0.90 on a fully-contained 2-token qualifier, so this is NEVER the sole barrier —
# fuzzy seeds also compile a NON-swallowed committed-value oracle (strict_oracle).
FUZZY_REANCHOR_THRESHOLD = 0.90


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _norm(s: str | None) -> str:
    """Normalize an accessible name/kind: trim + collapse whitespace + lowercase.
    The single normalization the rest of the heal stack already uses for labels."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def page_key(url: str | None) -> str:
    """The PATH portion of a URL (no scheme/host/query/hash), so the same control
    on the same page matches across environments and recordings. Empty when no URL
    is known (then the fingerprint is page-agnostic — still valid, just coarser)."""
    u = (url or "").strip()
    if not u:
        return ""
    after = u.split("://", 1)[-1]
    path = after.split("/", 1)[1] if "/" in after else ""
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    return "/" + path if path else "/"


def app_key_from_url(url: str | None) -> str:
    """Phase 4 — the HOST/origin of a URL (lowercased, leading ``www.`` and ``:port``
    stripped): a stable APP-level identity that the SAME app shares across recordings/
    artifacts. Pairs with :func:`page_key` (which keeps the PATH): a control's
    fingerprint is ALREADY host-agnostic, so app_key_from_url is the scope that lets a
    fix proven in one recording be reused in ANOTHER recording of the same app. Returns
    '' when no host is present (relative / host-less capture) — the caller then falls
    back to per-recording scope and cross-recording reuse simply no-ops (fail-open)."""
    after = (url or "").strip().split("://", 1)[-1]
    host = after.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split(":", 1)[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def control_fingerprint(observed: dict | None, *, page_path: str = "") -> str:
    """Stable, app-level identity for a control from its grounded observation.

    Derived ONLY from generic a11y signals — normalized accessible name + control
    kind + the page path — so two scenarios (or two recordings) that touch the SAME
    control on the SAME page yield the SAME fingerprint, and a heal proven once is
    reusable. Returns '' when there is no groundable label (the caller then skips
    the ledger for that step — no false key). A sha256 prefix keeps it fixed-length,
    separator-collision-free, and label-content-opaque in the key itself."""
    o = observed or {}
    label = _norm(o.get("label") or "")
    if not label:
        return ""
    kind = _norm(o.get("kind") or "")
    page = page_key(page_path or o.get("url") or o.get("next_url") or "")
    raw = "\n".join((page, label, kind))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_FP_LEN]


class ProvenControlLedgerRow(Base):
    """One oracle-PROVEN heal for a control, app- and tenant-scoped. Append-then-
    upsert: at most one row per (tenant, app_key, control_fp, fix_kind); a re-prove
    refreshes the payload + bumps ``confirmed_count`` (rising trust). ``app_key`` is
    the reuse scope chosen by the caller — a single artifact in Phase 1/2, the target
    app (so recordings share) in Phase 4. ``label``/``page_path`` are stored readable
    for provenance/debugging only; ``control_fp`` is the join key."""

    __tablename__ = "proven_control_ledger"

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    app_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    control_fp: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fix_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    label: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    page_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    proven_by_run: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    app_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # ── Phase 3 invalidation/provenance (added by apply_proven_control_ledger_p3.sql) ──
    stale_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    invalidated_reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    proven_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "app_key", "control_fp", "fix_kind",
                         name="uq_proven_control_ledger_control"),
    )


def _entry_to_dict(row: ProvenControlLedgerRow) -> dict:
    return {
        "control_fp": row.control_fp,
        "fix_kind": row.fix_kind,
        "payload": dict(row.payload or {}),
        "label": row.label,
        "page_path": row.page_path,
        "proven_by_run": row.proven_by_run,
        "app_fingerprint": row.app_fingerprint,
        "confirmed_count": int(row.confirmed_count or 0),
        "stale_count": int(getattr(row, "stale_count", 0) or 0),
        "invalidated_at": row.invalidated_at.isoformat() if getattr(row, "invalidated_at", None) else None,
        "invalidated_reason": getattr(row, "invalidated_reason", "") or "",
        "proven_at": row.proven_at.isoformat() if row.proven_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def record_proven_fix(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_key: str,
    control_fp: str,
    fix_kind: str,
    payload: dict,
    label: str = "",
    page_path: str = "",
    proven_by_run: str = "",
    app_fingerprint: str = "",
) -> bool:
    """Upsert one PROVEN (oracle-verified) heal for a control. Insert when new;
    on a repeat, refresh the payload/provenance and bump ``confirmed_count``.

    BEST-EFFORT and FAIL-OPEN: returns False (never raises) when ``control_fp`` is
    empty, ``fix_kind`` is unknown, or the table is absent (pre-migration) / any DB
    error — so it can never break the heal flow. The caller commits (the tenant-
    scoped session commits on exit)."""
    if not (control_fp and fix_kind in FIX_KINDS and tenant_id):
        return False
    try:
        existing = (await session.execute(
            select(ProvenControlLedgerRow).where(
                ProvenControlLedgerRow.tenant_id == tenant_id,
                ProvenControlLedgerRow.app_key == (app_key or ""),
                ProvenControlLedgerRow.control_fp == control_fp,
                ProvenControlLedgerRow.fix_kind == fix_kind,
            ).limit(1)
        )).scalar_one_or_none()
        now = _utc_now()
        if existing is not None:
            existing.payload = dict(payload or {})
            existing.label = (label or existing.label or "")[:_MAX_LABEL]
            existing.page_path = (page_path or existing.page_path or "")[:512]
            existing.proven_by_run = proven_by_run or existing.proven_by_run
            if app_fingerprint:
                existing.app_fingerprint = app_fingerprint[:128]
            existing.confirmed_count = int(existing.confirmed_count or 0) + 1
            # Phase 3: a fresh green REACTIVATES the fix — clear any prior staleness/
            # quarantine so a re-proven control is seeded again with rising trust.
            existing.stale_count = 0
            existing.invalidated_at = None
            existing.invalidated_reason = ""
            existing.updated_at = now
        else:
            session.add(ProvenControlLedgerRow(
                ledger_id=_new_id(),
                tenant_id=tenant_id,
                app_key=(app_key or "")[:200],
                control_fp=control_fp[:64],
                fix_kind=fix_kind,
                payload=dict(payload or {}),
                label=(label or "")[:_MAX_LABEL],
                page_path=(page_path or "")[:512],
                proven_by_run=(proven_by_run or "")[:64],
                app_fingerprint=(app_fingerprint or "")[:128],
                confirmed_count=1,
                proven_at=now,
                updated_at=now,
            ))
        await session.flush()
        return True
    except Exception:  # table missing (pre-migration) / DB error — fail open
        try:
            await session.rollback()  # clear the aborted txn so a shared session stays usable
        except Exception:
            pass
        return False


async def get_proven_fixes(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_key: str,
    control_fps: list[str] | set[str] | None = None,
) -> dict[str, list[dict]]:
    """Proven fixes for an (tenant, app_key), grouped by ``control_fp``. When
    ``control_fps`` is given, only those are returned (the seed path looks up just
    the controls a scenario actually uses). BEST-EFFORT: returns {} when the table
    is absent (pre-migration) or on any DB error — so seeding never breaks a run."""
    if not tenant_id:
        return {}
    fps = {f for f in (control_fps or []) if f}
    try:
        q = select(ProvenControlLedgerRow).where(
            ProvenControlLedgerRow.tenant_id == tenant_id,
            ProvenControlLedgerRow.app_key == (app_key or ""),
            # Phase 3: never SEED a quarantined fix (a control that has drifted past
            # the stale threshold). A re-proven green reactivates it (invalidated_at→NULL).
            ProvenControlLedgerRow.invalidated_at.is_(None),
        )
        if fps:
            q = q.where(ProvenControlLedgerRow.control_fp.in_(fps))
        rows = (await session.execute(q)).scalars().all()
    except Exception:  # table missing (pre-migration) / DB error — fail open
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.control_fp, []).append(_entry_to_dict(r))
    return out


async def get_proven_fixes_by_app(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_fingerprint: str,
    control_fps: list[str] | set[str] | None = None,
) -> dict[str, list[dict]]:
    """Phase 4 — proven fixes for a (tenant, app_fingerprint=host) scope, grouped by
    ``control_fp``, so a fix proven in ONE recording is reusable in ANOTHER recording of
    the SAME app. ``control_fingerprint`` is already host-agnostic (``page_key`` keeps
    PATH only), so an UNCHANGED control yields a byte-identical fp across recordings —
    this read just widens the reuse scope from one artifact to the whole app (EXACT
    fingerprint, no fuzzy). Filters ``invalidated_at IS NULL`` (never seed a quarantined
    fix). BEST-EFFORT: returns {} when ``app_fingerprint`` is blank, the table is absent
    (pre-migration), or on any DB error — so cross-recording reuse never breaks a run."""
    if not (tenant_id and app_fingerprint):
        return {}
    fps = {f for f in (control_fps or []) if f}
    try:
        q = select(ProvenControlLedgerRow).where(
            ProvenControlLedgerRow.tenant_id == tenant_id,
            ProvenControlLedgerRow.app_fingerprint == app_fingerprint,
            ProvenControlLedgerRow.invalidated_at.is_(None),
        )
        if fps:
            q = q.where(ProvenControlLedgerRow.control_fp.in_(fps))
        rows = (await session.execute(q)).scalars().all()
    except Exception:  # table missing (pre-migration) / DB error — fail open
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.control_fp, []).append(_entry_to_dict(r))
    return out


async def list_proven_controls(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_key: str | None = None,
    app_fingerprint: str | None = None,
    include_invalidated: bool = True,
    limit: int = 200,
) -> list[dict]:
    """Phase 5 — list ledger entries for the KB view (newest first), each with full
    provenance + lifecycle: label, page, fix_kind, confirmed_count (rising trust),
    stale_count, invalidated_at/reason (quarantine), app_fingerprint (cross-recording
    scope), proven_by_run. Scope by ``app_key`` (one recording) and/or
    ``app_fingerprint`` (an app across recordings); omit both for the whole tenant.
    Read-only + BEST-EFFORT: returns [] when the table is absent or on any DB error."""
    if not tenant_id:
        return []
    try:
        q = select(ProvenControlLedgerRow).where(ProvenControlLedgerRow.tenant_id == tenant_id)
        if app_key:
            q = q.where(ProvenControlLedgerRow.app_key == app_key)
        if app_fingerprint:
            q = q.where(ProvenControlLedgerRow.app_fingerprint == app_fingerprint)
        if not include_invalidated:
            q = q.where(ProvenControlLedgerRow.invalidated_at.is_(None))
        q = q.order_by(ProvenControlLedgerRow.updated_at.desc()).limit(max(1, int(limit)))
        rows = (await session.execute(q)).scalars().all()
    except Exception:  # table missing (pre-migration) / DB error — fail open
        return []
    return [{**_entry_to_dict(r), "app_key": r.app_key, "ledger_id": r.ledger_id} for r in rows]


async def mark_seed_stale(
    session: AsyncSession,
    *,
    tenant_id: str,
    app_key: str,
    control_fp: str,
    fix_kind: str,
    invalidated_by_run: str = "",
    reason: str = "seed_failed_first_prove",
) -> bool:
    """Phase 3: record that a SEEDED fix just failed its own first prove (it is stale —
    the app changed since it was proven). Bumps ``stale_count``; once it reaches
    ``STALE_THRESHOLD`` consecutive misfires the row is QUARANTINED (``invalidated_at``
    set) so it stops being seeded and the loop heals fresh instead. A later green
    re-prove reactivates it via :func:`record_proven_fix`.

    BEST-EFFORT and FAIL-OPEN: returns False (never raises) when the row/table is
    absent or on any DB error — invalidation is an optimization, never a gate, and
    it only ever REMOVES a seed, so a failure here can never make a test wrongly green.
    Caller commits (the tenant-scoped session commits on exit)."""
    if not (control_fp and fix_kind and tenant_id):
        return False
    try:
        existing = (await session.execute(
            select(ProvenControlLedgerRow).where(
                ProvenControlLedgerRow.tenant_id == tenant_id,
                ProvenControlLedgerRow.app_key == (app_key or ""),
                ProvenControlLedgerRow.control_fp == control_fp,
                ProvenControlLedgerRow.fix_kind == fix_kind,
            ).limit(1)
        )).scalar_one_or_none()
        if existing is None:
            return False
        now = _utc_now()
        existing.stale_count = int(existing.stale_count or 0) + 1
        if existing.stale_count >= STALE_THRESHOLD and existing.invalidated_at is None:
            existing.invalidated_at = now
            # provenance: keep proven_by_run (the proof) untouched; tag who quarantined it
            existing.invalidated_reason = (f"{reason}@{invalidated_by_run}" if invalidated_by_run
                                           else (reason or "stale"))[:200]
        existing.updated_at = now
        await session.flush()
        return True
    except Exception:  # table missing (pre-migration) / DB error — fail open
        try:
            await session.rollback()
        except Exception:
            pass
        return False


__all__ = [
    "FIX_KINDS",
    "STALE_THRESHOLD",
    "FUZZY_REANCHOR_THRESHOLD",
    "ProvenControlLedgerRow",
    "control_fingerprint",
    "page_key",
    "app_key_from_url",
    "record_proven_fix",
    "get_proven_fixes",
    "get_proven_fixes_by_app",
    "list_proven_controls",
    "mark_seed_stale",
]
