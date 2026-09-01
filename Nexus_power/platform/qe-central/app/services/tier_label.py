"""QE-Central S4 — RENDERS-vs-BEHAVES tier labeller (the anti-green-wash axis).

HONEST-10 (the VKPower delivery auditor) scores five FIXED dimensions and has
NO behavioral axis (``playwright_auditor.py:37-43``), so a suite that only FILLS
fields can still score a green 10/10.  This module adds the missing axis WITHOUT
touching VKPower: it reads the read-only ``GET …/rtm`` traceability matrix
(``provenance.build_rtm`` — test_factory.py:3691-3712) and labels every case

  * ``behaves`` — iff ≥1 GROUNDED assertion whose ``oracle_kind`` proves the app
    actually *did something*: a real navigation (``toHaveURL`` → ``navigation``)
    or an observed outcome region (``getByText`` visibility → ``outcome-region``);
  * ``renders`` — otherwise (fail-DOWN): a fill-only / value-only / field-present
    suite proves the page RENDERED, never that it BEHAVED.

The SUITE label is the MIN tier (``renders`` < ``behaves``) — one un-behavioral
case drags the whole suite down, so a green delivery score can never launder a
fill-only suite into "behavioral".  Labels persist to ``qec_case_tiers`` (natural
PK ``(tenant_id, artifact_id, test_id)``), the table that exists precisely
because HONEST-10 has no behavioral axis and VKPower stays untouched.

ZERO LLM, deterministic, $0.  The pure ``label_case`` / ``suite_tier`` /
``label_rtm`` functions carry all the logic and are unit-tested with a fixture
``/rtm`` payload (no HTTP, no DB); the ``async`` helpers persist through the
tenant-scoped qecentral session (RLS on the table).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.gov_models import CaseTierRow

logger = logging.getLogger(__name__)

MODEL_VERSION = "qec-tier-v1"

# ── Tiers (renders is the LOWER / fail-down tier) ─────────────────────────
TIER_RENDERS = "renders"
TIER_BEHAVES = "behaves"
TIER_UNLABELED = "unlabeled"
TIERS: tuple[str, ...] = (TIER_RENDERS, TIER_BEHAVES)
#: Ordering for the MIN-tier suite roll-up (renders < behaves).
_TIER_ORDER = {TIER_RENDERS: 0, TIER_BEHAVES: 1}

#: The GROUNDED oracle kinds that prove BEHAVIOUR (not mere rendering/value).
#: A subset of the /rtm ``oracle_kind`` vocabulary (provenance.py:59-93):
#: ``navigation`` (toHaveURL) and ``outcome-region`` (observed getByText).
#: value-oracle / value-presence / state / a11y are grounded but only prove a
#: value was committed or a field is present — NOT that the app behaved.
BEHAVIORAL_ORACLE_KINDS = frozenset({"navigation", "outcome-region"})


# ── Pure labelling ─────────────────────────────────────────────────────────

def _behavioral_assertions(rtm_test: Mapping) -> list[dict]:
    """Collect the GROUNDED behavioural assertions that earn a BEHAVES label.

    Reads the exact ``/rtm`` row shape (``rows[].emitted_assertions[]`` each with
    ``code`` / ``oracle_kind`` / ``grounded`` — provenance.build_rtm).  An
    assertion counts iff it is ``grounded`` AND its ``oracle_kind`` is in
    :data:`BEHAVIORAL_ORACLE_KINDS`.  A step the compiler SKIPS emits no
    assertions (``unproven``), so it can never earn BEHAVES.
    """
    found: list[dict] = []
    for row in rtm_test.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        for a in row.get("emitted_assertions") or []:
            if not isinstance(a, Mapping):
                continue
            kind = str(a.get("oracle_kind") or "")
            if bool(a.get("grounded")) and kind in BEHAVIORAL_ORACLE_KINDS:
                found.append({
                    "step_number": row.get("step_number"),
                    "oracle_kind": kind,
                    "code": str(a.get("code") or "")[:500],
                })
    return found


def label_case(rtm_test: Mapping) -> dict:
    """Label ONE ``/rtm`` test case ``behaves`` | ``renders`` (pure).

    Args:
        rtm_test: one element of the ``/rtm`` response ``tests`` array —
            ``{test_id, name, rows:[{emitted_assertions:[{oracle_kind,
            grounded, code}], ...}], ...}``.

    Returns:
        ``{"test_id", "name", "tier", "evidence": {"behavioral_assertions",
        "oracle_kinds", "reason"}}``.  ``behaves`` iff ≥1 grounded
        navigation/outcome-region assertion; else ``renders`` (fail-down) with
        empty behavioural evidence and an honest reason.
    """
    behavioral = _behavioral_assertions(rtm_test)
    if behavioral:
        tier = TIER_BEHAVES
        reason = (
            f"{len(behavioral)} grounded behavioural assertion(s) prove the app "
            f"navigated / showed an observed outcome"
        )
    else:
        tier = TIER_RENDERS
        reason = (
            "no grounded navigation/outcome-region oracle — the suite proves the "
            "page RENDERED, not that it BEHAVED (fail-down)"
        )
    return {
        "test_id": str(rtm_test.get("test_id") or ""),
        "name": str(rtm_test.get("name") or ""),
        "tier": tier,
        "evidence": {
            "behavioral_assertions": behavioral,
            "oracle_kinds": sorted({b["oracle_kind"] for b in behavioral}),
            "reason": reason,
            "model_version": MODEL_VERSION,
        },
    }


def suite_tier(tiers: Sequence[str]) -> str:
    """The SUITE label = the MIN tier (``renders`` < ``behaves``); fail-down.

    An empty suite (no labelled cases) is :data:`TIER_UNLABELED` — an honest
    "nothing to judge", never a misleading green ``behaves``.
    """
    known = [t for t in tiers if t in _TIER_ORDER]
    if not known:
        return TIER_UNLABELED
    return min(known, key=lambda t: _TIER_ORDER[t])


def label_rtm(rtm_response: Mapping) -> dict:
    """Label every case in a full ``/rtm`` response (pure).

    Returns ``{artifact_id, cases:[label_case, ...], suite_tier, counts}`` where
    ``counts`` tallies ``behaves`` / ``renders`` and ``suite_tier`` is the MIN
    over all cases (:data:`TIER_UNLABELED` when there are no cases).
    """
    artifact_id = str(rtm_response.get("artifact_id") or "")
    tests = rtm_response.get("tests") or []
    cases = [label_case(t) for t in tests if isinstance(t, Mapping)]
    counts = {TIER_BEHAVES: 0, TIER_RENDERS: 0}
    for c in cases:
        counts[c["tier"]] = counts.get(c["tier"], 0) + 1
    counts["total"] = len(cases)
    return {
        "artifact_id": artifact_id,
        "cases": cases,
        "suite_tier": suite_tier([c["tier"] for c in cases]),
        "counts": counts,
        "model_version": MODEL_VERSION,
    }


# ── Async persistence (qecentral DB, tenant-scoped) ───────────────────────

def _row_to_dict(row: CaseTierRow) -> dict:
    return {
        "test_id": row.test_id,
        "tier": row.tier,
        "evidence": dict(row.evidence or {}),
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


async def persist_case_tiers(
    tenant_id: str, artifact_id: str, cases: Sequence[Mapping],
) -> dict:
    """Upsert the per-case tier labels into ``qec_case_tiers``.

    Idempotent by the natural PK ``(tenant_id, artifact_id, test_id)`` — a
    re-label overwrites tier/evidence/computed_at in place.  Cases with no
    ``test_id`` are skipped (an unkeyed label has no honest home).
    """
    now = utc_now()
    inserted = updated = skipped = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        for case in cases:
            test_id = str(case.get("test_id") or "").strip()
            if not test_id:
                skipped += 1
                continue
            tier = case.get("tier") or TIER_RENDERS
            row = (await session.execute(
                select(CaseTierRow).where(
                    CaseTierRow.tenant_id == tenant_id,
                    CaseTierRow.artifact_id == artifact_id,
                    CaseTierRow.test_id == test_id,
                )
            )).scalar_one_or_none()
            evidence = dict(case.get("evidence") or {})
            if row is None:
                session.add(CaseTierRow(
                    tenant_id=tenant_id, artifact_id=artifact_id,
                    test_id=test_id, tier=tier, evidence=evidence,
                    computed_at=now,
                ))
                inserted += 1
            else:
                row.tier = tier
                row.evidence = evidence
                row.computed_at = now
                updated += 1
        await session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": inserted + updated}


async def label_and_persist(
    tenant_id: str, artifact_id: str, rtm_response: Mapping, *, persist: bool = True,
) -> dict:
    """Label a full ``/rtm`` response and (optionally) persist the labels.

    ``rtm_response`` is fetched by the router from VKPower ``GET …/rtm`` over a
    service JWT (this function stays HTTP-free so it is unit-testable with a
    fixture payload).  Returns the :func:`label_rtm` result augmented with the
    persistence stats.
    """
    result = label_rtm(rtm_response)
    if persist:
        result["persisted"] = await persist_case_tiers(
            tenant_id, artifact_id, result["cases"],
        )
    logger.info(
        "qec.tier_label.labelled",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "suite_tier": result["suite_tier"], "counts": result["counts"],
               "persisted": persist},
    )
    return result


async def read_case_tiers(tenant_id: str, artifact_id: str) -> dict:
    """Read the persisted tier labels for an artifact + the MIN suite tier."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CaseTierRow)
            .where(
                CaseTierRow.tenant_id == tenant_id,
                CaseTierRow.artifact_id == artifact_id,
            )
            .order_by(CaseTierRow.test_id.asc())
        )).scalars().all()
    cases = [_row_to_dict(r) for r in rows]
    counts = {TIER_BEHAVES: 0, TIER_RENDERS: 0}
    for c in cases:
        counts[c["tier"]] = counts.get(c["tier"], 0) + 1
    counts["total"] = len(cases)
    return {
        "artifact_id": artifact_id,
        "cases": cases,
        "suite_tier": suite_tier([c["tier"] for c in cases]),
        "counts": counts,
        "model_version": MODEL_VERSION,
    }


__all__ = [
    "MODEL_VERSION",
    "TIER_RENDERS", "TIER_BEHAVES", "TIER_UNLABELED", "TIERS",
    "BEHAVIORAL_ORACLE_KINDS",
    "label_case",
    "suite_tier",
    "label_rtm",
    "persist_case_tiers",
    "label_and_persist",
    "read_case_tiers",
]
