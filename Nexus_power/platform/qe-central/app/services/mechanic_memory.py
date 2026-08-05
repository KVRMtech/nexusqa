"""R4 — Mechanic memory: compounding interaction knowledge.

When the explorer verifies (R0 intent contract) that a specific ladder rung
operates a control, the proven mechanic is persisted here.  On the next crawl
the explorer tries the proven mechanic FIRST — bypassing the full ladder walk
and every medic call.

Two halves, same doctrine as advance_memory:

  * RECALL (tenant-private): ``control_mechanics`` keyed
    ``(tenant_id, control_sig)`` → the ladder-rung variant that was R0-proven
    to operate this control.  Only reads for this tenant.
  * WRITE-BACK ON PROOF ONLY: a mechanic enters memory only after the crawl
    observed ``intent_met != False`` through that rung.  An LLM guess is not
    knowledge; a ladder walk that fell through is not a proof.
  * CONSENT: contributing a proven mechanic to the shared pool
    (``mechanic_priors``) requires the tenant's explicit
    ``share_advance_priors`` opt-in (OFF by default).  Recall of the pool is
    open — it is value-free by construction (only rung variant names, never
    values or control labels).

Every function is best-effort and never raises: without memory a crawl still
runs exactly as before (full ladder walk).  DB failures degrade to "no recall",
never to a blocked fill or a failed completion callback.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.advance_models import MechanicMemoryRow, MechanicPriorRow
from ..db.fleet_models import TenantProvisioningRow
from .advance_memory import contributor_hash

logger = logging.getLogger(__name__)


async def recall_all(tenant_id: str, app_id: str) -> dict[str, str]:
    """Every proven mechanic for this tenant+app: ``{control_sig: mechanic}``.

    Called at dispatch time; the dict is small (one entry per distinct control
    the tenant has ever successfully interacted with) and travels on the
    dispatch request as ``proven_mechanics``.  Returning the whole dict avoids
    N+1 queries during the crawl and keeps the explorer stateless.
    """
    if not tenant_id:
        return {}
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            q = select(
                MechanicMemoryRow.control_sig,
                MechanicMemoryRow.mechanic,
            ).where(MechanicMemoryRow.tenant_id == tenant_id)
            if app_id:
                q = q.where(MechanicMemoryRow.app_id == app_id)
            rows = (await session.execute(q)).all()
            return {r.control_sig: r.mechanic for r in rows}
    except Exception as exc:
        logger.warning("qec.mechanic_memory.recall_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
        return {}


async def recall_priors(control_sigs: set[str]) -> dict[str, str]:
    """Cross-tenant pooled mechanics for a set of control signatures.

    Returns ``{control_sig: mechanic}`` — the highest-proof mechanic per sig
    that clears the minimum-proofs AND minimum-distinct-tenants thresholds.
    Value-free by construction (only rung variant names); consuming is open.
    """
    sigs = {s for s in control_sigs if s}
    if not sigs:
        return {}
    try:
        async with tenant_scoped_qec_session("__system__") as session:
            rows = (
                await session.execute(
                    select(MechanicPriorRow).where(
                        MechanicPriorRow.control_sig.in_(sorted(sigs)),
                        MechanicPriorRow.proof_count
                        >= settings.advance_prior_min_proofs,
                        MechanicPriorRow.distinct_tenants
                        >= settings.advance_prior_min_tenants,
                    )
                )
            ).scalars().all()
            if not rows:
                return {}
            best: dict[str, MechanicPriorRow] = {}
            for r in rows:
                prev = best.get(r.control_sig)
                if prev is None or (r.proof_count, r.distinct_tenants) > (
                    prev.proof_count, prev.distinct_tenants
                ):
                    best[r.control_sig] = r
            return {sig: row.mechanic for sig, row in best.items()}
    except Exception as exc:
        logger.warning("qec.mechanic_memory.prior_recall_failed",
                       extra={"error": str(exc)[:200]})
        return {}


def _proven_mechanics_from_coverage(
    coverage: Any,
) -> list[tuple[str, str]]:
    """Extract ``(control_sig, mechanic)`` pairs from a completion's field ledger.

    A field-ledger entry carries ``mechanic`` only when the fill was committed
    through a non-native ladder rung AND the R0 intent contract confirmed
    ``intent_met != False``.  Presence in the ledger IS the proof.
    """
    if not isinstance(coverage, Mapping):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in coverage.get("field_ledger") or []:
        if not isinstance(entry, Mapping):
            continue
        sig = str(entry.get("signature") or "")
        mechanic = str(entry.get("mechanic") or "")
        if not sig or not mechanic:
            continue
        if not entry.get("filled"):
            continue
        if sig in seen:
            continue
        seen.add(sig)
        out.append((sig, mechanic))
    return out


async def _tenant_consented(session, tenant_id: str) -> bool:
    row = (
        await session.execute(
            select(TenantProvisioningRow.share_advance_priors).where(
                TenantProvisioningRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    return bool(row)


async def _remember_proven(
    session, *, tenant_id: str, app_id: str,
    control_sig: str, mechanic: str,
) -> None:
    row = (
        await session.execute(
            select(MechanicMemoryRow).where(
                MechanicMemoryRow.tenant_id == tenant_id,
                MechanicMemoryRow.control_sig == control_sig,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(MechanicMemoryRow(
            tenant_id=tenant_id, control_sig=control_sig,
            mechanic=mechanic, app_id=app_id or "",
            proof_count=1, last_proven_at=utc_now()))
        return
    if row.mechanic == mechanic:
        row.proof_count += 1
    else:
        row.mechanic = mechanic
        row.proof_count = 1
    row.last_proven_at = utc_now()
    if app_id:
        row.app_id = app_id


async def _contribute_prior(
    session, *, tenant_id: str, control_sig: str, mechanic: str,
) -> None:
    marker = contributor_hash(tenant_id)
    row = (
        await session.execute(
            select(MechanicPriorRow).where(
                MechanicPriorRow.control_sig == control_sig,
                MechanicPriorRow.mechanic == mechanic,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(MechanicPriorRow(
            control_sig=control_sig, mechanic=mechanic,
            proof_count=1, distinct_tenants=1,
            contributor_hashes=[marker], last_proven_at=utc_now()))
        return
    hashes = list(row.contributor_hashes or [])
    if marker not in hashes:
        hashes.append(marker)
        row.contributor_hashes = hashes
        row.distinct_tenants = len(hashes)
    row.proof_count += 1
    row.last_proven_at = utc_now()


async def harvest_mechanics(
    *, tenant_id: str, app_id: str, coverage: Any,
) -> dict[str, int]:
    """Fold a finished crawl's PROVEN mechanic interactions into memory (and,
    with consent, into the shared mechanic pool).  Best-effort: never raises,
    and a failure never breaks the completion callback."""
    pairs = _proven_mechanics_from_coverage(coverage)
    if not pairs:
        return {"proven": 0, "remembered": 0, "contributed": 0}
    remembered = contributed = 0
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            consented = await _tenant_consented(session, tenant_id)
            for control_sig, mechanic in pairs:
                await _remember_proven(
                    session, tenant_id=tenant_id, app_id=app_id,
                    control_sig=control_sig, mechanic=mechanic)
                remembered += 1
                if consented:
                    await _contribute_prior(
                        session, tenant_id=tenant_id,
                        control_sig=control_sig, mechanic=mechanic)
                    contributed += 1
    except Exception as exc:
        logger.warning("qec.mechanic_memory.harvest_failed",
                       extra={"tenant_id": tenant_id, "app_id": app_id,
                              "error": str(exc)[:200]})
        return {"proven": len(pairs), "remembered": 0, "contributed": 0}
    logger.warning(
        "qec.mechanic_memory.harvested proven=%d remembered=%d contributed=%d "
        "tenant=%s app=%s", len(pairs), remembered, contributed, tenant_id, app_id)
    return {"proven": len(pairs), "remembered": remembered,
            "contributed": contributed}
