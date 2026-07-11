"""Auto-Heal Reliability SLO — read-only aggregation over the Part-11 heal
evidence ledger (``heal_evidence.HealEventRow``).

No migration and no writes: this is the "does healing behave, at scale?" dashboard
feed. On a fresh system with no recorded heals it returns honest zeros / ``None``
rates (status ``no_data``) rather than fabricating a green SLO — consistent with
the never-green-wash posture. Includes the tamper-evident chain check.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import heal_evidence
from .heal_evidence import HealEventRow

#: Published reliability target — fewer than 1% of applied heals are false.
FALSE_HEAL_SLO_TARGET = 0.01

#: One artifact accumulating this many heals in the window looks like a heal-storm
#: (a deploy that broke many locators at once → escalate, not silently absorb).
_HEAL_STORM_THRESHOLD = 10


def _empty(tenant_id: str, artifact_id: str | None, *, status: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "artifact_id": artifact_id,
        "window": "all",
        "heals_total": 0,
        "heals_succeeded": 0,
        "heals_refused": 0,
        "success_rate": None,
        "false_heal_slo_target": FALSE_HEAL_SLO_TARGET,
        "flow_churn": {},
        "heal_storm_detected": False,
        "chain_verified": True,
        "status": status,
    }


async def heal_slo(
    session: AsyncSession, *, tenant_id: str, artifact_id: str | None = None
) -> dict:
    """Aggregate heal-reliability metrics for a tenant (optionally one artifact).

    Read-only over the heal evidence ledger. Returns honest zeros / ``None`` rates
    when nothing has been recorded yet; never raises for a missing table (degrades
    to ``ledger_unavailable``). Includes the ``verify_chain`` tamper-evident check.
    """
    q = select(HealEventRow).where(HealEventRow.tenant_id == tenant_id)
    if artifact_id:
        q = q.where(HealEventRow.artifact_id == artifact_id)
    try:
        rows = (await session.execute(q)).scalars().all()
    except Exception:
        return _empty(tenant_id, artifact_id, status="ledger_unavailable")

    total = len(rows)
    if total == 0:
        return _empty(tenant_id, artifact_id, status="no_data")

    # A heal that produced a verified-green run is a success; the rest were
    # refused / quarantined rather than green-washed.
    succeeded = sum(1 for r in rows if bool(getattr(r, "verified_green", False)))
    refused = total - succeeded

    churn: dict[str, int] = {}
    for r in rows:
        key = getattr(r, "artifact_id", "") or ""
        churn[key] = churn.get(key, 0) + 1
    heal_storm = any(v >= _HEAL_STORM_THRESHOLD for v in churn.values())

    try:
        chain = await heal_evidence.verify_chain(session, tenant_id=tenant_id)
    except Exception:
        chain = {"ok": None, "count": total}

    return {
        "tenant_id": tenant_id,
        "artifact_id": artifact_id,
        "window": "all",
        "heals_total": total,
        "heals_succeeded": succeeded,
        "heals_refused": refused,
        "success_rate": (succeeded / total) if total else None,
        "false_heal_slo_target": FALSE_HEAL_SLO_TARGET,
        "flow_churn": churn,
        "heal_storm_detected": heal_storm,
        "chain_verified": chain.get("ok"),
        "chain": chain,
        "status": "ok",
    }
