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
    #: M1.7 / T-GW-03 — the REACH KEY this item was deduped on, stamped by
    #: :meth:`Frontier.push`.  Callers never set it; it exists so a durable
    #: checkpoint can persist the queue AND the dedup set consistently.  Without
    #: it, a resume that restored the queue by URL would have to guess which key
    #: each item had been deduped under, and a guess that was wrong either
    #: re-walks a finished route or permanently blocks an unfinished one.
    key: str = ""


def _section_signature(url_template: str, mount: str = "") -> str:
    """The app SECTION an item belongs to — the first two path segments of its
    (id-collapsed) ``url_template`` (``/account/settings/*`` → ``account/settings``,
    ``/`` → ``""``).  The unit of novelty for the information-gain planner.

    A ``url_template`` carries no scheme, so the HOST is the first segment and
    this is really "host + first path segment". That is a real section only for
    an application whose top-level paths differ. For one mounted entirely under
    a single prefix it is a CONSTANT: every item lands in one section, novelty
    rank merely increments, and the information-gain planner silently does
    nothing whatsoever.

    Measured on parabank.parasoft.com 2026-09-02, whose whole app lives under
    /parabank/: marketing pages, Swagger docs and banking transactions ALL
    signed as "parabank.parasoft.com/parabank". Ordering degenerated to FIFO,
    the docs multiply per click and took 86 of 101 states, and the two real
    submits were then refused - "SUBMIT window closed, exceeded the
    request/time budget". /app/, /portal/ and /web/ share the shape.

    ``mount`` strips that application prefix so sections are relative to the
    app root ("api-docs", "overview.htm", "transfer.htm" - distinct). It
    defaults to "" and is then byte-for-byte the original function: this module
    decides traversal order, so the default must move no manifest.
    """
    path = urlsplit(url_template or "").path or ""
    if mount:
        stripped, whole = mount.strip("/"), path.strip("/")
        if stripped and (whole == stripped or whole.startswith(stripped + "/")):
            path = whole[len(stripped):]
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

    def __init__(self, plan_patterns: Sequence[tuple[str, int]] = (),
                 *, section_mount: str = "") -> None:
        self._heap: list[tuple[int, int, int, int, FrontierItem]] = []
        self._seq = 0
        self._enqueued_keys: set[str] = set()
        #: information-gain planner: items already queued per app section, so a
        #: newly-seen section outranks the Nth sibling of a saturated one.
        self._section_counts: dict[str, int] = {}
        #: Application mount prefix stripped before sectioning. Empty means the
        #: original signature exactly, so every existing crawl is unchanged.
        self._section_mount: str = str(section_mount or "")
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
        # Stamp the key ON the item so the queue can be snapshotted without the
        # snapshotter having to re-derive it (M1.7 / T-GW-03).
        item.key = key
        # Novelty rank = how many items are ALREADY queued from this item's section
        # (the reach key IS the url_template). 0 for the first, growing per sibling.
        section = _section_signature(key, self._section_mount)
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

    # -- durable checkpointing (M1.7 / T-GW-03) --------------------------------

    def snapshot_items(self) -> list[FrontierItem]:
        """Every item still QUEUED, in heap-array order.

        Deliberately NOT in pop order: draining the heap to sort it would destroy
        the queue, and a copy sorted by the ordering tuple would still have to be
        re-pushed one at a time on restore — which re-derives the same order
        anyway, because :meth:`push` recomputes novelty rank and plan priority
        from the key.  Order is therefore restored by the push, not by the
        snapshot, and the snapshot only has to be COMPLETE.
        """
        return [entry[-1] for entry in self._heap]

    def spent_keys(self) -> set[str]:
        """The push-time dedup set — every reach key this crawl has consumed."""
        return set(self._enqueued_keys)

    def mark_spent(self, keys) -> int:
        """Re-arm the dedup set from a durable checkpoint, WITHOUT queueing.

        These are routes an earlier run already dequeued and expanded.  Marking
        them keeps a resumed run from walking the app a second time inside one
        crawl id the moment a restored page re-discovers a link to one of them.
        Returns how many keys were newly marked.
        """
        added = 0
        for key in keys or ():
            key = str(key or "")
            if key and key not in self._enqueued_keys:
                self._enqueued_keys.add(key)
                added += 1
        return added


__all__ = ["Frontier", "FrontierItem", "_parse_plan_patterns", "_section_signature"]
