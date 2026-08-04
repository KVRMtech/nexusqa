"""Advance memory — the learning layer under the E2E advance oracle.

Converts the crawl-time LLM tax into a compounding asset, mirroring the
field-learning doctrine:

  * RECALL (tenant-private): a decision-point signature the tenant has already
    PROVEN answers instantly from ``advance_memory`` — no LLM call.
  * TIER 2.5 (pooled): a candidate whose normalized label matches a
    high-confidence, value-free cross-tenant prior advances without an LLM
    call.  Confidence thresholds come from settings, never literals.
  * WRITE-BACK ON PROOF ONLY: the harvest runs at completion-callback time
    over the flow steps' advance evidence — a pick enters memory only after
    the crawler observed a genuine advance (real effect + new unseen state).
    An LLM guess is not knowledge.
  * CONSENT: contributing label patterns to the shared pool requires the
    tenant's explicit ``share_advance_priors`` opt-in (OFF by default).
    Recall of the pool is open — it is value-free by construction.

Every function is best-effort and never raises: without memory a crawl still
runs exactly as before (the LLM answers).  DB failures degrade to "no recall",
never to a blocked pick or a failed completion callback.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import select

from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.advance_models import AdvanceLabelPriorRow, AdvanceMemoryRow
from ..db.fleet_models import TenantProvisioningRow

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def normalize_label(name: str) -> str:
    """The one normalization every store/recall path shares: lowercased,
    whitespace-collapsed, stripped, bounded."""
    return _WS_RE.sub(" ", str(name or "").strip().lower())[:200]


def contributor_hash(tenant_id: str) -> str:
    """Pseudonymous contributor marker — enough to count distinct tenants in
    the shared pool, never enough to name one."""
    return hashlib.sha256(f"advance-prior::{tenant_id}".encode("utf-8")).hexdigest()[:16]


async def recall(tenant_id: str, signature: str) -> Optional[str]:
    """The tenant's own proven answer for this decision point, or ``None``."""
    if not tenant_id or not signature:
        return None
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(AdvanceMemoryRow).where(
                        AdvanceMemoryRow.tenant_id == tenant_id,
                        AdvanceMemoryRow.signature == signature,
                    )
                )
            ).scalar_one_or_none()
            return row.chosen_label_norm if row is not None else None
    except Exception as exc:
        logger.warning("qec.advance_memory.recall_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
        return None


async def recall_prior(
    tenant_id: str, candidate_labels: set[str],
) -> Optional[str]:
    """The highest-confidence pooled label among ``candidate_labels`` that
    clears BOTH thresholds (proofs and distinct tenants), or ``None``.

    The pool is value-free product UI text; consuming it is open to every
    tenant — only CONTRIBUTING is consent-gated."""
    labels = {normalize_label(l) for l in candidate_labels if normalize_label(l)}
    if not tenant_id or not labels:
        return None
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(AdvanceLabelPriorRow).where(
                        AdvanceLabelPriorRow.label_norm.in_(sorted(labels)),
                        AdvanceLabelPriorRow.proof_count
                        >= settings.advance_prior_min_proofs,
                        AdvanceLabelPriorRow.distinct_tenants
                        >= settings.advance_prior_min_tenants,
                    )
                )
            ).scalars().all()
            if not rows:
                return None
            best = max(rows, key=lambda r: (r.proof_count, r.distinct_tenants))
            return best.label_norm
    except Exception as exc:
        logger.warning("qec.advance_memory.prior_recall_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
        return None


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
    session, *, tenant_id: str, app_id: str, signature: str, label_norm: str,
) -> None:
    row = (
        await session.execute(
            select(AdvanceMemoryRow).where(
                AdvanceMemoryRow.tenant_id == tenant_id,
                AdvanceMemoryRow.signature == signature,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(AdvanceMemoryRow(
            tenant_id=tenant_id, signature=signature,
            chosen_label_norm=label_norm, app_id=app_id or "",
            proof_count=1, last_proven_at=utc_now()))
        return
    # The same shape proven again — reinforce; a DIFFERENT proven label for
    # the same shape replaces (the newest proof wins, count restarts honest).
    if row.chosen_label_norm == label_norm:
        row.proof_count += 1
    else:
        row.chosen_label_norm = label_norm
        row.proof_count = 1
    row.last_proven_at = utc_now()
    if app_id:
        row.app_id = app_id


async def _contribute_prior(session, *, tenant_id: str, label_norm: str) -> None:
    marker = contributor_hash(tenant_id)
    row = (
        await session.execute(
            select(AdvanceLabelPriorRow).where(
                AdvanceLabelPriorRow.label_norm == label_norm,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(AdvanceLabelPriorRow(
            label_norm=label_norm, proof_count=1, distinct_tenants=1,
            contributor_hashes=[marker], last_proven_at=utc_now()))
        return
    hashes = list(row.contributor_hashes or [])
    if marker not in hashes:
        hashes.append(marker)
        row.contributor_hashes = hashes
        row.distinct_tenants = len(hashes)
    row.proof_count += 1
    row.last_proven_at = utc_now()


def _proven_oracle_advances(coverage: Any) -> list[tuple[str, str]]:
    """(signature, label_norm) pairs from a completion's flow evidence.

    Only tier-3 (oracle) advances carry a signature, and an ``advance`` entry
    exists on a step ONLY when the walk genuinely advanced out of it — so
    presence here IS the proof."""
    if not isinstance(coverage, Mapping):
        return []
    out: list[tuple[str, str]] = []
    for flow in coverage.get("flows") or []:
        if not isinstance(flow, Mapping):
            continue
        for step in flow.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            adv = step.get("advance")
            if not isinstance(adv, Mapping) or not adv.get("oracle"):
                continue
            signature = str(adv.get("signature") or "")
            label = normalize_label(str(adv.get("control_name") or ""))
            if signature and label:
                out.append((signature, label))
    return out


async def harvest_completion(
    *, tenant_id: str, app_id: str, coverage: Any,
) -> dict[str, int]:
    """Fold a finished crawl's PROVEN oracle advances into memory (and, with
    consent, into the shared label pool).  Best-effort: never raises, and a
    failure never breaks the completion callback."""
    pairs = _proven_oracle_advances(coverage)
    if not pairs:
        return {"proven": 0, "remembered": 0, "contributed": 0}
    remembered = contributed = 0
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            consented = await _tenant_consented(session, tenant_id)
            seen: set[str] = set()
            for signature, label in pairs:
                if signature in seen:
                    continue
                seen.add(signature)
                await _remember_proven(
                    session, tenant_id=tenant_id, app_id=app_id,
                    signature=signature, label_norm=label)
                remembered += 1
                if consented:
                    await _contribute_prior(
                        session, tenant_id=tenant_id, label_norm=label)
                    contributed += 1
    except Exception as exc:
        logger.warning("qec.advance_memory.harvest_failed",
                       extra={"tenant_id": tenant_id, "app_id": app_id,
                              "error": str(exc)[:200]})
        return {"proven": len(pairs), "remembered": 0, "contributed": 0}
    logger.warning(
        "qec.advance_memory.harvested proven=%d remembered=%d contributed=%d "
        "tenant=%s app=%s", len(pairs), remembered, contributed, tenant_id, app_id)
    return {"proven": len(pairs), "remembered": remembered,
            "contributed": contributed}
