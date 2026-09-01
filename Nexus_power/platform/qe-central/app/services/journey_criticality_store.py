"""Tier 2 — the criticality band, made durable at fold time (qec_025).

``journey_criticality`` bands a journey and ``routers/journeys`` ranks the
result on every read.  Both are correct and neither leaves a trace: a band that
exists only for the duration of a request cannot be compared with anything, so
"did this journey's criticality change with the last crawl?" had no answer, and
neither did "which bands did the new signal pack leave stale?".

This module writes the band the fold's own evidence produces.

READ TIME REMAINS AUTHORITATIVE, and that is the whole design.  Nothing here is
a cache: ``_rank_journeys`` still evaluates live against the tenant's ACTIVE
pack, because a stored band served as current would outlive both the evidence
and the pack that produced it.  What is stored is a dated, attributed record —
band, the markers that fired, the pack version, and when — so a reader can tell
what was said, on what basis, and whether it still holds.

THE BAND IS NOT RE-DERIVED HERE.  It comes from ``evaluate_journey``, which
calls the registry, whose evidence list is carried through verbatim.  A second
opinion computed in a store would be a second classifier nobody declared.

BEST-EFFORT, LIKE THE CATALOGUE REFRESH BESIDE IT.  A fold that could not band
its journeys must not fail — the graph, the traversals and the catalogue are all
already committed, and the band is an annotation on top of them.  What it must
not do is write a WRONG band: a journey whose evaluation raises is left with the
band it already had, not stamped with a default.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import JourneyRow
from . import criticality, journey_criticality, journey_evidence

logger = logging.getLogger(__name__)

#: Most journeys banded in one fold.  An application with more journeys than
#: this has a discovery problem that a banding pass is not the place to notice,
#: and an unbounded per-journey graph read inside a fold is how a fold stops
#: finishing.  The ranked surface reads live and is unaffected by this bound.
MAX_JOURNEYS_PER_FOLD = 500

#: Markers kept per journey.  The registry lists what fired; a row is a record,
#: not a log, and the first few are what a reviewer reads.
MAX_EVIDENCE_ITEMS = 12


def _evidence_list(banded: Any) -> list:
    raw = (banded or {}).get("evidence")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item)[:200] for item in raw][:MAX_EVIDENCE_ITEMS]


async def persist_criticality_bands(
    tenant_id: str, app_id: str, *, crawl_ref: str = "",
) -> dict[str, int]:
    """Band every journey of one app and store the verdict.

    Returns ``{"banded": n, "changed": n}`` — ``changed`` counts the journeys
    whose band is DIFFERENT from the one already stored, which is the number an
    operator actually wants after a crawl.  A first fold reports every journey as
    changed, because every journey has genuinely acquired a band it did not have.
    """
    report = {"banded": 0, "changed": 0}
    if not tenant_id or not app_id:
        return report
    signals, registry_version = await criticality.load_active_pack(tenant_id)
    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        journeys = (await session.execute(
            select(JourneyRow)
            .where(JourneyRow.tenant_id == tenant_id,
                   JourneyRow.app_id == app_id)
            # Ordered so a capped run takes the SAME journeys every time. An
            # arbitrary 500 of 700 would band a different subset per fold and
            # the stored bands would flicker for a reason nothing records.
            .order_by(JourneyRow.journey_id)
            .limit(MAX_JOURNEYS_PER_FOLD)
        )).scalars().all()

        for journey in journeys:
            try:
                evidence = await journey_evidence.journey_evidence(
                    session, tenant_id, app_id, journey)
                banded = journey_criticality.evaluate_journey(
                    journey, evidence["nodes"],
                    edge_labels=evidence["edge_labels"],
                    pack={"signals": signals},
                    registry_version=registry_version)
            except Exception as exc:
                # LEFT AS IT WAS, never defaulted. A journey that could not be
                # evaluated keeps the band an earlier fold proved; stamping it
                # with a fail-up default would present a failure to band as a
                # banding, which is the one thing this record must not do.
                logger.warning(
                    "qec.criticality.band_failed journey=%s err=%s",
                    journey.journey_id, str(exc)[:200])
                continue
            band = str(banded.get("band") or "")[:8]
            if not band:
                continue
            if journey.criticality_band != band:
                report["changed"] += 1
            journey.criticality_band = band
            journey.criticality_registry_version = str(
                banded.get("registry_version") or registry_version or "")[:64]
            journey.criticality_evidence = _evidence_list(banded)
            journey.criticality_banded_at = now
            report["banded"] += 1

        await session.commit()

    logger.info(
        "qec.criticality.banded",
        extra={"tenant_id": tenant_id, "app_id": app_id, "crawl_ref": crawl_ref,
               "banded": report["banded"], "changed": report["changed"],
               "registry_version": registry_version},
    )
    return report


__all__ = ["MAX_EVIDENCE_ITEMS", "MAX_JOURNEYS_PER_FOLD",
           "persist_criticality_bands"]
