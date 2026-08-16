"""The priority frontier — what to visit next, and in what order (T-DE-04).

Extracted VERBATIM from :mod:`app.crawler`.  ``_section_signature`` and
``_parse_plan_patterns`` travel with :class:`Frontier` because they exist only
to serve its ordering; they are the frontier's private machinery, not shared
crawler utilities.  :mod:`app.crawler` re-exports the public names.

ORDERING IS BEHAVIOUR.  This module decides traversal order, so any change to
it changes every crawl's manifest.  It is moved here byte-for-byte and must
stay that way unless a milestone explicitly owns traversal order.

This module has NO dependency on any other ``app`` module.
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence
from urllib.parse import urlsplit


@dataclass
class FrontierItem:
    """A state to visit, described by how to REACH it (a URL to goto in Phase 1)
    plus its BFS depth and (Phase-2 seed) priority."""

    url: str
    depth: int = 0
    priority: int = 0
    discovered_via: str = ""
    parent_fingerprint: str = ""


def _section_signature(url_template: str) -> str:
    """The app SECTION an item belongs to — the first two path segments of its
    (id-collapsed) ``url_template`` (``/account/settings/*`` → ``account/settings``,
    ``/`` → ``""``).  The unit of novelty for the information-gain planner."""
    path = urlsplit(url_template or "").path or ""
    segs = [s for s in path.split("/") if s][:2]
    return "/".join(segs)


#: The explorer RE-BOUNDS a plan from qe-central (defense in depth — a plan is
#: ordering data, never an attack surface): a safe substring pattern only, weight
#: clamped 1..3, at most 8 patterns.  Mirrors the qe-central planner validation.
_PLAN_MAX_PATTERNS = 8
_PLAN_PATTERN_RX = re.compile(r"^[a-z0-9][a-z0-9/_.#-]{0,59}$")


def _parse_plan_patterns(plan: Optional[dict[str, Any]]) -> list[tuple[str, int]]:
    """Project a dispatch ``plan`` dict onto bounded ``[(pattern, weight)]`` the
    frontier can apply.  Fully defensive: any malformed/oversized/unsafe entry is
    dropped, an empty result ⇒ a byte-identical crawl."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in ((plan or {}).get("priority_patterns") or ()):
        if not isinstance(item, dict) or len(out) >= _PLAN_MAX_PATTERNS:
            continue
        pattern = str(item.get("pattern") or "").strip().lower()[:60]
        if not pattern or pattern in seen or not _PLAN_PATTERN_RX.match(pattern):
            continue
        try:
            weight = max(1, min(3, int(item.get("weight") or 1)))
        except (TypeError, ValueError):
            weight = 1
        seen.add(pattern)
        out.append((pattern, weight))
    return out


class Frontier:
    """A min-priority queue of :class:`FrontierItem` deduped by reach key.

    Ordering is ``(priority, novelty_rank, depth, insertion)``:

      * ``priority``     — an explicit Phase-2 seed can still raise a critical
        route ahead of everything (unchanged);
      * ``novelty_rank`` — the INFORMATION-GAIN planner (#3): the Nth item queued
        from a given app SECTION gets rank N-1, so the FIRST item of every section
        is visited before any section's second item.  Under a finite state budget
        this spends the budget on breadth-of-app-regions (maximal new information)
        instead of draining one link-heavy section before touching the rest;
      * ``depth`` then ``insertion`` — breadth-first / FIFO within a novelty tier.

    Push-time dedup on the reach key (``url_template``) keeps the queue finite; the
    crawler additionally dedups on the full state fingerprint at expand time so
    distinct URLs that render the SAME state are visited once.
    """

    def __init__(self, plan_patterns: Sequence[tuple[str, int]] = ()) -> None:
        self._heap: list[tuple[int, int, int, int, FrontierItem]] = []
        self._seq = 0
        self._enqueued_keys: set[str] = set()
        #: information-gain planner: items already queued per app section, so a
        #: newly-seen section outranks the Nth sibling of a saturated one.
        self._section_counts: dict[str, int] = {}
        #: CAGED-PLANNER priorities: (lowercased substring, weight 1..3) grounded +
        #: validated in qe-central. A frontier item whose reach key contains a
        #: pattern gets priority -weight (min-heap ⇒ visited earlier). This ONLY
        #: reorders; it can never add a state or change what is reachable.
        self._plan_patterns: list[tuple[str, int]] = [
            (str(p).lower(), int(w)) for p, w in (plan_patterns or ()) if str(p).strip()
        ]

    def _plan_priority(self, key: str) -> int:
        """The most-negative plan weight among patterns occurring in ``key`` (a
        url_template), or 0 when the plan does not touch this route."""
        if not self._plan_patterns:
            return 0
        kl = key.lower()
        best = 0
        for pattern, weight in self._plan_patterns:
            if pattern in kl:
                best = min(best, -weight)
        return best

    def push(self, item: FrontierItem, *, key: str) -> bool:
        if key in self._enqueued_keys:
            return False
        self._enqueued_keys.add(key)
        # Novelty rank = how many items are ALREADY queued from this item's section
        # (the reach key IS the url_template). 0 for the first, growing per sibling.
        section = _section_signature(key)
        novelty_rank = self._section_counts.get(section, 0)
        self._section_counts[section] = novelty_rank + 1
        # An EXPLICIT caller priority (a Phase-2 seed) wins; otherwise the caged
        # planner may raise a high-value section ahead of the rest. Ordering-only.
        priority = item.priority if item.priority != 0 else self._plan_priority(key)
        heapq.heappush(self._heap, (priority, novelty_rank, item.depth, self._seq, item))
        self._seq += 1
        return True

    def pop(self) -> Optional[FrontierItem]:
        if not self._heap:
            return None
        return heapq.heappop(self._heap)[-1]

    def release(self, key: str) -> bool:
        """Un-spend a reach key whose popped item never actually REACHED its URL.

        Push-time dedup marks a key used forever — correct for a visited page,
        wrong for one the expansion never observed. Live: the operator-onboarded
        entry (`/underwriting/new-business/new-application`) was popped first, the
        app's per-page-load logout landed the crawl on the dashboard instead, and
        the entry's key stayed spent — so when the queue page later surfaced a
        real clickable path to that route, it could never be enqueued again. The
        one page the client explicitly asked for became permanently unreachable
        at t=0.

        Only ever called for an item already POPPED (never one still queued), so
        releasing cannot double-queue. The caller bounds it to once per key.
        """
        if key in self._enqueued_keys:
            self._enqueued_keys.discard(key)
            return True
        return False

    def __len__(self) -> int:
        return len(self._heap)


__all__ = ["Frontier", "FrontierItem", "_parse_plan_patterns", "_section_signature"]
