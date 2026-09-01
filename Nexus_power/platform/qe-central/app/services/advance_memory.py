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
    An LLM guess is not knowledge.  PROOF, not PROVENANCE, is the gate: a
    deterministic tier-1/2 advance the walk carried forward is remembered on
    exactly the same terms as a tier-3 one (M2.6 / T-CAP-02).  It did not use
    to be, so on an ordinary "Next"/"Continue" application the learning layer
    stored nothing at all.
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


def _proven_advances(coverage: Any) -> list[tuple[str, str, bool]]:
    """(signature, label_norm, was_oracle) triples from a completion's flow
    evidence — EVERY tier, not only the oracle's.

    PROOF IS THE SAME FACT WHATEVER DECIDED IT. An ``advance`` entry exists on
    a step only when the walk genuinely advanced out of it (real effect + a new
    unseen state), so presence here IS the proof — and a deterministic tier-1
    "Continue" that carried the walk forward is exactly as proven as a tier-3
    pick.  This filtered on ``oracle`` and therefore remembered ONLY the LLM's
    picks: on a normal application, whose forward controls are named "Next" and
    "Continue", the crawl proved an advance at every step and stored none of
    them.  The learning layer only ever saw the rare case (M2.6 / T-CAP-02).

    The gate that remains is the SIGNATURE.  It is the value-free decision-point
    key both halves compute the same way (qe-explorer ``app.advance_signature``
    mirrors :func:`app.services.advance_agent.compute_signature`), and without
    it a remembered label has no decision point to be recalled at.  A step whose
    advance carries no signature is skipped rather than stored under a fabricated
    key.

    ``was_oracle`` is kept so the harvest can REPORT the split — a fleet whose
    advances are suddenly all tier-3 is a fleet whose deterministic tiers broke.
    """
    if not isinstance(coverage, Mapping):
        return []
    out: list[tuple[str, str, bool]] = []
    for flow in coverage.get("flows") or []:
        if not isinstance(flow, Mapping):
            continue
        for step in flow.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            adv = step.get("advance")
            if not isinstance(adv, Mapping):
                continue
            signature = str(adv.get("signature") or "")
            label = normalize_label(str(adv.get("control_name") or ""))
            if signature and label:
                out.append((signature, label, bool(adv.get("oracle"))))
    return out


async def harvest_completion(
    *, tenant_id: str, app_id: str, coverage: Any,
) -> dict[str, int]:
    """Fold a finished crawl's PROVEN advances — every tier — into memory (and,
    with consent, into the shared label pool).  Best-effort: never raises, and a
    failure never breaks the completion callback.

    ``oracle`` / ``deterministic`` split the proven set by WHO decided, so the
    fleet can see at a glance whether the deterministic tiers are still doing
    the work.  Before M2.6 the deterministic column did not exist because those
    advances were dropped on the floor."""
    triples = _proven_advances(coverage)
    if not triples:
        return {"proven": 0, "remembered": 0, "contributed": 0,
                "oracle": 0, "deterministic": 0}
    oracle_n = sum(1 for _s, _l, was_oracle in triples if was_oracle)
    base = {"proven": len(triples), "oracle": oracle_n,
            "deterministic": len(triples) - oracle_n}
    remembered = contributed = 0
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            consented = await _tenant_consented(session, tenant_id)
            seen: set[str] = set()
            for signature, label, _was_oracle in triples:
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
        return {**base, "remembered": 0, "contributed": 0}
    logger.warning(
        "qec.advance_memory.harvested proven=%d oracle=%d deterministic=%d "
        "remembered=%d contributed=%d tenant=%s app=%s",
        len(triples), oracle_n, len(triples) - oracle_n, remembered,
        contributed, tenant_id, app_id)
    return {**base, "remembered": remembered, "contributed": contributed}
