"""QE-Central Phase-8 — the read-only evidence loader (SEAM E, live side).

Reads the existing compliance evidence — the hash-chained ``verdict_events``,
the reproducible ``decision_dossiers``, the governed ``verification_waivers``,
and the immutable ``audit_log`` — out of the nexus (substrate) database into an
:class:`~app.compliance.adapter.EvidenceBundle` the framework adapters project.

STRICTLY READ-ONLY.  Every query is a ``SELECT`` issued through the least-
privilege ``qec_substrate`` role inside a tenant-scoped session (the
``nexus.current_tenant_id`` GUC is set, and every ``WHERE`` clause also filters
``tenant_id`` — defence in depth over RLS).  It writes nothing and captures
nothing new: the projection re-frames trust the evidence already carries.

BEST-EFFORT + HONEST DEGRADATION.  Each source is read in its OWN tenant-scoped
session so a failure in one (a missing out-of-band table, or a role that has not
yet been granted SELECT on the evidence tables) is ISOLATED — it degrades that
one source to ``[]`` with a loud warning and an ``unavailable`` marker the report
surfaces, never aborting the whole report or fabricating evidence.  A separate
session per source is required because one failed statement aborts a Postgres
transaction for every subsequent statement in it.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ..db import tenant_scoped_substrate_session
from .adapter import EvidenceBundle, EvidenceWindow

logger = logging.getLogger(__name__)

#: Hard cap on rows pulled per source — an audit report is a bounded projection,
#: not a bulk export; a pathological tenant can never exhaust memory here.
_ROW_CAP = 5000


def _maybe_json(value: Any) -> Any:
    """Coerce a JSONB column that arrived as raw text into a Python object.

    The SQLAlchemy asyncpg dialect usually deserialises JSON/JSONB already; when
    a driver hands back the raw JSON string instead, parse it so the verdict
    chain re-derivation (which hashes ``axes`` as a nested object) stays byte-
    compatible with how the value was written.  Unparseable text passes through
    unchanged rather than raising.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _window_clause(window: EvidenceWindow | None) -> tuple[str, dict]:
    """Build an optional ``created_at`` window predicate + its bound params."""
    if window is None:
        return "", {}
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if isinstance(window.start, datetime):
        clauses.append("created_at >= :win_start")
        params["win_start"] = window.start
    if isinstance(window.end, datetime):
        clauses.append("created_at <= :win_end")
        params["win_end"] = window.end
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


async def _read_rows(
    tenant_id: str, sql: str, params: Mapping[str, Any], source: str,
) -> tuple[list[dict], str]:
    """Run one tenant-scoped read-only SELECT, isolated + best-effort.

    Returns ``(rows, status)`` where status is ``"ok"`` or ``"unavailable"``.
    Any failure (missing table, permission denied, transient error) degrades to
    ``([], "unavailable")`` with a warning — never raises to the caller.
    """
    try:
        async with tenant_scoped_substrate_session(tenant_id) as session:
            result = await session.execute(text(sql), dict(params))
            rows = [dict(m) for m in result.mappings().all()]
        return rows, "ok"
    except Exception as exc:  # noqa: BLE001 — honest degradation, never a 500
        logger.warning(
            "qec.compliance.evidence_source_unavailable",
            extra={"tenant_id": tenant_id, "source": source, "error": str(exc)[:300]},
        )
        return [], "unavailable"


async def load_evidence(
    *, tenant_id: str, window: EvidenceWindow | None = None,
) -> tuple[EvidenceBundle, dict[str, str]]:
    """Load a tenant-scoped evidence snapshot + a per-source availability map.

    Returns ``(bundle, sources)``.  ``bundle`` is an :class:`EvidenceBundle`
    (already defensively tenant-filtered); ``sources`` maps each evidence source
    name to ``"ok"`` or ``"unavailable"`` so the report can honestly disclose
    which stores it could read.
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to load compliance evidence")

    win_sql, win_params = _window_clause(window)
    params = {"tenant_id": tid, "cap": _ROW_CAP, **win_params}

    verdict_rows, s_verdicts = await _read_rows(
        tid,
        "SELECT verdict_id, tenant_id, artifact_id, test_id, version, "
        "registry_version, source, actor, overall, decision, axes, gaps, "
        "chain_hash, created_at "
        "FROM verdict_events "
        f"WHERE tenant_id = :tenant_id{win_sql} "
        "ORDER BY created_at ASC, verdict_id ASC LIMIT :cap",
        params, "verdict_events",
    )
    for r in verdict_rows:
        r["axes"] = _maybe_json(r.get("axes")) or {}

    dossier_rows, s_dossiers = await _read_rows(
        tid,
        "SELECT verdict_id, tenant_id, artifact_id, test_id, chain_hash, "
        "created_at "
        "FROM decision_dossiers "
        f"WHERE tenant_id = :tenant_id{win_sql} "
        "ORDER BY created_at ASC, verdict_id ASC LIMIT :cap",
        params, "decision_dossiers",
    )

    waiver_rows, s_waivers = await _read_rows(
        tid,
        "SELECT waiver_id, tenant_id, artifact_id, test_id, finding_match, "
        "reason, actor, expires_at, created_at "
        "FROM verification_waivers "
        f"WHERE tenant_id = :tenant_id{win_sql} "
        "ORDER BY created_at ASC, waiver_id ASC LIMIT :cap",
        params, "verification_waivers",
    )

    audit_rows, s_audit = await _read_rows(
        tid,
        "SELECT log_id, tenant_id, engine, action, entity_type, entity_id, "
        "user_id, created_at "
        "FROM audit_log "
        f"WHERE tenant_id = :tenant_id{win_sql} "
        "ORDER BY created_at ASC, log_id ASC LIMIT :cap",
        params, "audit_log",
    )

    bundle = EvidenceBundle.build(
        tenant_id=tid,
        window=window or EvidenceWindow(),
        verdicts=verdict_rows,
        dossiers=dossier_rows,
        waivers=waiver_rows,
        audit=audit_rows,
    )
    sources = {
        "verdict_events": s_verdicts,
        "decision_dossiers": s_dossiers,
        "verification_waivers": s_waivers,
        "audit_log": s_audit,
    }
    return bundle, sources


__all__ = ["load_evidence"]
