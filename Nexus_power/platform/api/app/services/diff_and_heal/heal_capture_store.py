"""Shared store for failure-state a11y captures (TrueFix re-anchor, P-B / T1.4).

The gated ``afterEach`` capture fixture (compiler ``_HEAL_CAPTURE_AFTEREACH``)
posts the live accessibility tree at the moment a step failed; the re-anchor
resolver reads it back within seconds (same heal flow).

WHY THIS IS SHARED (T1.4 fix)
-----------------------------
The original store was a per-worker, in-process ``OrderedDict``. Under multiple
uvicorn workers (or any multi-process deploy) a capture written by the worker
that ran the headed capture-run could be invisible to the worker that later runs
the resolve request → the resolver silently saw "no capture" → it silently
OVER-ESCALATED (a *healable* rename looked unhealable). That is a green-wash-
adjacent silent degradation, so the store now lives in a process-shared backend:

  * PRIMARY: a Postgres ``heal_capture`` UNLOGGED table, RLS-scoped per tenant,
    created idempotently on first use (no Alembic migration needed — see
    ``_ensure_table``). UNLOGGED = fast + crash-discardable, which is exactly the
    right durability class for a transient failure-state snapshot. Shared across
    every worker/process that talks to the same database.
  * FALLBACK: the original bounded in-memory ``OrderedDict`` — used only when the
    DB is not connected (e.g. unit tests, degraded boot). This keeps the legacy
    sync ``put``/``get``/``clear`` callers working with zero behavioural change in
    a single-process context.

OBSERVABILITY (T1.4)
--------------------
Silent degradation is the enemy. Every put/get is counted (``_metrics``) and
exposed via ``stats()`` and ``capture_success_rate()`` so the resolver path can
report "capture present on N% of resolve attempts" — a sudden drop is a visible
signal, not a silent over-escalation.

API
---
Async (preferred — process-shared, RLS-correct):  ``aput`` / ``aget`` / ``aclear``.
Sync (legacy, in-memory only):                     ``put``  / ``get``  / ``clear``.

The async functions transparently fall back to the in-memory map when the DB is
down, so a single signature works in every deployment posture.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 256
_TABLE = "heal_capture"

# ─── In-memory fallback (single-process / DB-down only) ──────────────────────
# key: (tenant_id, artifact_id, scenario_id) -> {"nodes": [...], "status": str}
_store: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()

# ─── Observability — make silent capture degradation impossible ──────────────
_metrics = {
    "puts": 0,            # captures written (any backend)
    "puts_db": 0,         # …of which landed in the shared DB
    "puts_memory": 0,     # …of which fell back to in-memory
    "get_attempts": 0,    # resolve-side reads
    "get_hits": 0,        # …that found a usable capture (>=1 node)
    "get_misses": 0,      # …that found nothing (silent over-escalation risk)
    "db_errors": 0,       # backend failures that triggered fallback
}

# Guards the one-time idempotent DDL so we don't issue CREATE on every call.
_table_ready = False


def _key(tenant_id: str, artifact_id: str, scenario_id: str) -> tuple[str, str, str]:
    return (tenant_id or "", artifact_id or "", scenario_id or "")


# ─── Shared (Postgres) backend ───────────────────────────────────────────────

async def _ensure_table(session) -> None:
    """Create the UNLOGGED capture table + its RLS policy idempotently.

    No Alembic migration: this table holds only transient, crash-discardable
    failure-state snapshots, so it is created lazily and is safe to (re)create
    with ``IF NOT EXISTS``. RLS mirrors the rest of the schema so a capture only
    resolves within its owning tenant. Runs at most once per process.

    IMPORTANT (audit T1.4): we DO NOT set ``_table_ready`` here. DDL is
    transactional in Postgres — if the surrounding ``aput`` INSERT (or ``aget``
    SELECT) later fails and the transaction rolls back, this ``CREATE`` rolls back
    with it. If we flipped ``_table_ready`` mid-transaction, a rollback would leave
    the flag True while the table no longer exists, so every later call would SKIP
    ``_ensure_table``, hit a missing table, throw, and silently revert to the
    per-worker in-memory map — re-introducing the exact silent over-escalation this
    store exists to kill. The caller (aput/aget/aclear) sets ``_table_ready=True``
    ONLY after its enclosing ``tenant_scoped_session`` block has COMMITTED, and
    resets it to False in its ``except`` so a rolled-back DDL is retried.
    """
    if _table_ready:
        return
    await session.execute(text(f"""
        CREATE UNLOGGED TABLE IF NOT EXISTS {_TABLE} (
            tenant_id   TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL DEFAULT '',
            nodes       JSONB NOT NULL DEFAULT '[]'::jsonb,
            status      TEXT NOT NULL DEFAULT '',
            node_count  INTEGER NOT NULL DEFAULT 0,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, artifact_id, scenario_id)
        );
    """))
    # Best-effort RLS — guarded so a re-run or a missing app role never errors.
    await session.execute(text(f"""
        DO $$
        BEGIN
            ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};
            CREATE POLICY tenant_isolation ON {_TABLE}
                USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO nexus_app;
            END IF;
        END$$;
    """))
    # NB: _table_ready is set by the CALLER after COMMIT (see _ensure_table docstring).


async def aput(*, tenant_id: str, artifact_id: str, scenario_id: str,
               nodes: list[dict], status: str = "") -> None:
    """Write a failure-state capture to the SHARED store (process-wide).

    Falls back to the in-memory map if the DB is not connected, so the same call
    works in tests / degraded boot. Best-effort: never raises into the heal flow.
    """
    global _table_ready
    nodes = list(nodes or [])
    status = status or ""
    _metrics["puts"] += 1

    from ...database import is_db_connected, tenant_scoped_session  # lazy: avoid cycle
    if is_db_connected() and (tenant_id or ""):
        try:
            async with tenant_scoped_session(tenant_id) as session:
                await _ensure_table(session)
                await session.execute(
                    text(f"""
                        INSERT INTO {_TABLE}
                            (tenant_id, artifact_id, scenario_id, nodes, status,
                             node_count, updated_at)
                        VALUES
                            (:t, :a, :s, CAST(:n AS jsonb), :st, :c, now())
                        ON CONFLICT (tenant_id, artifact_id, scenario_id)
                        DO UPDATE SET
                            nodes = EXCLUDED.nodes,
                            status = EXCLUDED.status,
                            node_count = EXCLUDED.node_count,
                            updated_at = now();
                    """),
                    {
                        "t": tenant_id or "", "a": artifact_id or "",
                        "s": scenario_id or "",
                        "n": _json_dumps(nodes), "st": status,
                        "c": len(nodes),
                    },
                )
            # The session block exited cleanly => the tx (incl. any CREATE issued by
            # _ensure_table) COMMITTED. Only NOW is it safe to skip the DDL next time.
            _table_ready = True
            _metrics["puts_db"] += 1
            return
        except Exception as exc:  # pragma: no cover - degraded path
            # The tx rolled back — a CREATE issued this call rolled back too, so the
            # table may not exist. Force the next call to re-run _ensure_table rather
            # than skipping it and silently degrading to in-memory forever.
            _table_ready = False
            _metrics["db_errors"] += 1
            logger.warning(
                "heal_capture_store.db_put_failed_fallback_memory",
                extra={"error": str(exc)[:200], "artifact_id": artifact_id},
            )
    # Fallback: in-memory.
    _put_memory(tenant_id, artifact_id, scenario_id, nodes, status)
    _metrics["puts_memory"] += 1


async def aget(*, tenant_id: str, artifact_id: str, scenario_id: str) -> Optional[dict]:
    """Read a capture from the SHARED store (visible across all workers).

    Returns ``{"nodes": [...], "status": str}`` or ``None``. Records a hit/miss
    so ``capture_success_rate()`` can surface silent degradation. Falls back to
    the in-memory map when the DB is down.
    """
    global _table_ready
    _metrics["get_attempts"] += 1

    from ...database import is_db_connected, tenant_scoped_session  # lazy
    if is_db_connected() and (tenant_id or ""):
        try:
            async with tenant_scoped_session(tenant_id) as session:
                await _ensure_table(session)
                row = (await session.execute(
                    text(f"""
                        SELECT nodes, status FROM {_TABLE}
                        WHERE tenant_id = :t AND artifact_id = :a
                          AND scenario_id = :s
                    """),
                    {"t": tenant_id or "", "a": artifact_id or "",
                     "s": scenario_id or ""},
                )).first()
            # Session block exited cleanly => the tx (incl. any _ensure_table CREATE)
            # COMMITTED; safe to skip the DDL next time.
            _table_ready = True
            if row is not None:
                nodes, status = row[0] or [], row[1] or ""
                rec = {"nodes": list(nodes), "status": status}
                _record_hit(rec)
                return rec
            _metrics["get_misses"] += 1
            return None
        except Exception as exc:  # pragma: no cover - degraded path
            # tx rolled back — re-run the DDL next time instead of silently degrading.
            _table_ready = False
            _metrics["db_errors"] += 1
            logger.warning(
                "heal_capture_store.db_get_failed_fallback_memory",
                extra={"error": str(exc)[:200], "artifact_id": artifact_id},
            )
    # Fallback: in-memory.
    rec = _store.get(_key(tenant_id, artifact_id, scenario_id))
    if rec and rec.get("nodes"):
        _record_hit(rec)
    else:
        _metrics["get_misses"] += 1
    return rec


async def aclear(*, tenant_id: str, artifact_id: str, scenario_id: str) -> None:
    """Delete a capture from the shared store (and the in-memory mirror)."""
    global _table_ready
    from ...database import is_db_connected, tenant_scoped_session  # lazy
    if is_db_connected() and (tenant_id or ""):
        try:
            async with tenant_scoped_session(tenant_id) as session:
                await _ensure_table(session)
                await session.execute(
                    text(f"""
                        DELETE FROM {_TABLE}
                        WHERE tenant_id = :t AND artifact_id = :a
                          AND scenario_id = :s
                    """),
                    {"t": tenant_id or "", "a": artifact_id or "",
                     "s": scenario_id or ""},
                )
            _table_ready = True  # committed cleanly
        except Exception as exc:  # pragma: no cover
            _table_ready = False  # tx rolled back — re-run DDL next time
            _metrics["db_errors"] += 1
            logger.warning("heal_capture_store.db_clear_failed",
                           extra={"error": str(exc)[:200]})
    _store.pop(_key(tenant_id, artifact_id, scenario_id), None)


# ─── In-memory primitives (legacy sync API + fallback) ───────────────────────

def _put_memory(tenant_id: str, artifact_id: str, scenario_id: str,
                nodes: list[dict], status: str) -> None:
    key = _key(tenant_id, artifact_id, scenario_id)
    _store[key] = {"nodes": list(nodes or []), "status": status or ""}
    _store.move_to_end(key)
    while len(_store) > _MAX_ENTRIES:
        _store.popitem(last=False)


def put(*, tenant_id: str, artifact_id: str, scenario_id: str,
        nodes: list[dict], status: str = "") -> None:
    """LEGACY sync write — in-memory only (single-process).

    Kept so any non-async caller continues to work unchanged. New code should
    prefer the shared ``aput``. Counts toward the same metrics for honesty.
    """
    _metrics["puts"] += 1
    _metrics["puts_memory"] += 1
    _put_memory(tenant_id, artifact_id, scenario_id, nodes, status)


def get(*, tenant_id: str, artifact_id: str, scenario_id: str) -> Optional[dict]:
    """LEGACY sync read — in-memory only.

    Prefer ``aget`` (process-shared). Retained so the deterministic re-anchor
    resolver can still read a capture in a single-process / test context.
    """
    _metrics["get_attempts"] += 1
    rec = _store.get(_key(tenant_id, artifact_id, scenario_id))
    if rec and rec.get("nodes"):
        _record_hit(rec)
    else:
        _metrics["get_misses"] += 1
    return rec


def clear(*, tenant_id: str, artifact_id: str, scenario_id: str) -> None:
    """LEGACY sync clear — in-memory only."""
    _store.pop(_key(tenant_id, artifact_id, scenario_id), None)


# ─── Observability helpers ───────────────────────────────────────────────────

def _record_hit(rec: dict) -> None:
    if rec and rec.get("nodes"):
        _metrics["get_hits"] += 1
    else:
        _metrics["get_misses"] += 1


def stats() -> dict:
    """Raw counters — safe to expose on a health/observability endpoint."""
    return dict(_metrics)


def capture_success_rate() -> Optional[float]:
    """Fraction of resolve-side reads that found a usable capture.

    ``None`` when there have been no reads yet. A drop from ~1.0 toward 0 is the
    early-warning signal that the capture write path is broken under the current
    worker topology (the bug this store was built to make impossible to hide).
    """
    attempts = _metrics["get_attempts"]
    if attempts <= 0:
        return None
    return round(_metrics["get_hits"] / attempts, 4)


def reset_metrics() -> None:
    """Test hook — zero the counters."""
    for k in _metrics:
        _metrics[k] = 0


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), default=str)


__all__ = [
    "aput", "aget", "aclear",          # shared (preferred)
    "put", "get", "clear",             # legacy sync (in-memory)
    "stats", "capture_success_rate", "reset_metrics",
]
