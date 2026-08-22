"""The crawl's terminal summary object.

Extracted from ``crawler.py`` (Gate 0 · task 12), which stood at 1294 LOC
against a <900 exit target. A PURE RELOCATION: not one character of logic,
ordering or naming changed, so the characterization goldens are byte-identical
by construction rather than by re-baselining.

Kept in its own module because ``main`` imports it, several tests import
it, and ``crawler`` re-exports it — so its identity is public API and does
not belong inside the driver it describes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CrawlSummary:
    crawl_id: str
    stop_reason: str
    states: int
    actions: int
    screenshots: int
    guard_blocks: int
    manifest_path: str
    storage_state: Optional[dict[str, Any]] = None
    detail: str = ""
    #: HOW FAR the crawl actually got: the greatest frontier depth it dequeued
    #: and expanded. Distinct from the max_depth BUDGET (what it was allowed) —
    #: the gap between the two is the difference between "stopped because it ran
    #: out of app" and "stopped because it ran out of permission".
    max_depth_reached: int = 0
    #: What the crawl found vs could fill/advance (forms_found, fields_inferred,
    #: fields_needing_seed, submit_candidates) — the coverage the operator sees.
    coverage: Optional[dict[str, Any]] = None
    #: M1.7 — the terminal DISPOSITION adjudicated from evidence
    #: (:mod:`app.completion`): ``completed`` / ``failed`` / ``incomplete``.
    #: ``stop_reason`` says WHAT happened; this says whether the crawl may be
    #: believed.  qe-central reads this rather than re-deriving the judgement
    #: from a string it would have to keep in sync.
    disposition: str = ""
    #: The evidence the disposition was adjudicated FROM, so the decision can be
    #: re-checked by a reader who does not trust the process that made it.
    evidence: Optional[dict[str, Any]] = None
    #: True when a completion CLAIM was refused for want of evidence — the
    #: milestone's "recovery must always be observable", applied to completion.
    downgraded: bool = False
    #: M1.7 / T-GW-04 — business rules this crawl PROVED, for qe-central to
    #: persist as durable, reusable knowledge.
    discovered_rules: Optional[list] = None
