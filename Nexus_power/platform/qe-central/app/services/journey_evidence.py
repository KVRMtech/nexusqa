"""The graph rows one journey is banded from and compiled from — ONE reader.

WHY THIS MODULE EXISTS. ``_journey_evidence`` was a private helper inside
``routers/journeys``, which was the only place that needed it: the criticality
band and the compiled specification are both produced while serving a request.
Persisting the band at FOLD time gave it a second caller in a place a router
must not be imported from, and the alternative — a second traversal→nodes→edges
query in the store — is exactly the drift this repository keeps paying for. Two
readers of one graph WILL eventually disagree about which traversal is the
journey's, and the disagreement would be invisible: both would return a
plausible band.

So the read lives here, once, and both callers go through it.

COMPLETED TRAVERSALS ONLY, and that is load-bearing rather than tidy. A
specification generated from a walk the crawl abandoned would assert a path
nobody has ever finished, and a criticality band derived from one would rank a
dead end above a working funnel.

WALK ORDER, NOT QUERY ORDER. The criticality projection and the compiled steps
are both sequences. A set-ordered node list would make both non-deterministic
for a reason no reader could ever see in the output.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..db.journey_models import (JourneyEdgeRow, JourneyNodeRow,
                                 JourneyTraversalRow)

#: The shape every caller gets, including when the journey has no completed
#: traversal at all. Returned rather than ``None`` so a caller cannot forget to
#: handle the empty case: an unwalked journey bands from no evidence, which is a
#: legitimate answer, and one that must not arrive as an AttributeError.
EMPTY: dict[str, Any] = {"nodes": [], "edges": [], "traversal": None,
                         "edge_labels": [], "path_fps": []}


def _empty(traversal: Any = None) -> dict[str, Any]:
    return {"nodes": [], "edges": [], "traversal": traversal,
            "edge_labels": [], "path_fps": []}


def edge_labels_along(path_fps: list[str], edges_by_pair: dict) -> list[str]:
    """The advance triggers the walk actually clicked, in walk order.

    Read from CONSECUTIVE pairs on the path rather than from every edge that
    leaves a node on it: a node may have edges to branches the walk never took,
    and folding those labels into the subject would band a journey on advances
    it never made.
    """
    return [
        edges_by_pair[(path_fps[i], path_fps[i + 1])].trigger_label_norm
        for i in range(len(path_fps) - 1)
        if (path_fps[i], path_fps[i + 1]) in edges_by_pair
    ]


async def journey_evidence(
    session, tenant_id: str, app_id: str, journey: Any,
) -> dict[str, Any]:
    """The graph rows one journey compiles and is banded from.

    ``{nodes, edges, traversal, edge_labels, path_fps}`` for the journey's most
    recent COMPLETED traversal, or empty collections when it has none.
    """
    journey_id = getattr(journey, "journey_id", None) or str(journey or "")
    traversal = (await session.execute(
        select(JourneyTraversalRow).where(
            JourneyTraversalRow.tenant_id == tenant_id,
            JourneyTraversalRow.app_id == app_id,
            JourneyTraversalRow.journey_id == journey_id,
            JourneyTraversalRow.completed.is_(True),
        ).order_by(JourneyTraversalRow.created_at.desc())
        .limit(1))).scalar_one_or_none()
    path_fps = [str(fp) for fp in ((traversal.path_fps if traversal else []) or [])]
    if not path_fps:
        return _empty(traversal)
    rows = (await session.execute(
        select(JourneyNodeRow).where(
            JourneyNodeRow.tenant_id == tenant_id,
            JourneyNodeRow.app_id == app_id,
            JourneyNodeRow.fingerprint.in_(set(path_fps)),
        ))).scalars().all()
    by_fp = {r.fingerprint: r for r in rows}
    nodes = [by_fp[fp] for fp in path_fps if fp in by_fp]
    edges = (await session.execute(
        select(JourneyEdgeRow).where(
            JourneyEdgeRow.tenant_id == tenant_id,
            JourneyEdgeRow.app_id == app_id,
            JourneyEdgeRow.from_fp.in_(set(path_fps)),
        ))).scalars().all()
    walked = {(e.from_fp, e.to_fp): e for e in edges}
    return {"nodes": nodes, "edges": list(edges), "traversal": traversal,
            "edge_labels": edge_labels_along(path_fps, walked),
            "path_fps": path_fps}


__all__ = ["EMPTY", "edge_labels_along", "journey_evidence"]
