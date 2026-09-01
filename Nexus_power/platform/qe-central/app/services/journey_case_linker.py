"""Journey ⇄ test-case linker (Release D0/D1).

The factory already compiles the crawl substrate into runnable cases; this
module makes the relationship a STORED FACT: which cases exercise which
journey, and which ONE case is the journey's end-to-end runnable form.

Matching is deterministic — no LLM. A case's steps carry ``observed.url``
(the page each step acted on); a journey's traversal carries the ordered
node URLs it walked. The score is the percentage of the journey's walked
paths the case's steps cover. Adoption law (``kind='journey_e2e'``): the
highest-scoring case that covers BOTH the journey's entry and terminal
paths — a case that spans the funnel, not one that samples it. When none
spans it, the journey stays linked-only and the API says honestly why it
is not runnable.

The adopted case gets a business-named display OVERLAY ("Verify <name>
end to end" — F5 law). The frozen factory's own case rows are never edited.

Links are re-derived on every completion for the CURRENT artifact
(re-crawls mint new artifacts). Idempotent; best-effort at the callback
seam — a linker failure never costs a crawl.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select

from ..clients import factory
from ..config import settings
from ..db import tenant_scoped_qec_session
from ..db.journey_models import (
    JourneyEdgeRow, JourneyNodeRow, JourneyRow, JourneyTraversalRow)
from ..db.journey_run_models import JourneyCaseRow

logger = logging.getLogger(__name__)

KIND_LINKED = "linked"
KIND_JOURNEY_E2E = "journey_e2e"

_WS_RE = re.compile(r"\s+")


def _sid(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


def norm_path(url: str) -> str:
    """One normalization for every URL comparison: the PATH component only,
    lowercased, trailing-slash-stripped ('' → '/'). Hosts and query strings
    never participate — an env swap must not break the match."""
    path = (urlsplit(str(url or "")).path or "/").strip().lower()
    return path.rstrip("/") or "/"


def case_paths(case_json: Any) -> list[str]:
    """The ordered, de-duplicated page paths a case's steps acted on
    (``steps[].observed.url`` — the generator's grounded page pointer)."""
    if not isinstance(case_json, dict):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for step in case_json.get("steps") or []:
        if not isinstance(step, dict):
            continue
        observed = step.get("observed")
        url = (observed or {}).get("url") if isinstance(observed, dict) else ""
        if not url:
            url = step.get("next_url") or ""
        p = norm_path(url) if url else ""
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def case_labels(case_json: Any) -> list[str]:
    """The ordered control labels a case ACTS on (``steps[].observed.label``).

    The load-bearing signal for multi-step forms and SPAs, where several
    distinct states share ONE url (observed live: VKPower's login flow walks
    two states, both at ``/login/``). What the user DID identifies the
    journey; where the URL went does not."""
    if not isinstance(case_json, dict):
        return []
    out: list[str] = []
    for step in case_json.get("steps") or []:
        if not isinstance(step, dict):
            continue
        observed = step.get("observed")
        if not isinstance(observed, dict):
            continue
        label = normalize_option_label(str(observed.get("label") or ""))
        if label:
            out.append(label)
    return out


def normalize_option_label(text: str) -> str:
    """One normalization for every label comparison."""
    return _WS_RE.sub(" ", str(text or "").strip().lower())[:200]


def walks_journey(
    journey_paths: list[str], journey_labels: list[str],
    case_path_list: list[str], case_label_list: list[str],
) -> bool:
    """Does this case WALK the journey?

    The journey's ADVANCE LABELS (the controls its walk clicked, in order)
    must appear as an ordered subsequence of the labels the case acts on,
    AND the journey's entry page must be among the case's pages. Labels are
    the primary key because a multi-step form advances without changing the
    URL; the entry page keeps a label-coincidence in a different part of the
    app from matching.

    A journey that never advanced (no labels) has no path to re-prove: it
    falls back to the page rule, which needs ``MIN_JOURNEY_PAGES``."""
    labels = [l for l in journey_labels if l]
    if not labels:
        return spans_journey(journey_paths, case_path_list)
    it = iter(case_label_list)
    if not all(any(cl == l for cl in it) for l in labels):
        return False
    entry = next((p for p in journey_paths if p), "")
    return (not entry) or entry in set(case_path_list)


def coverage_score(journey_paths: list[str], case_path_list: list[str]) -> int:
    """Percent (0-100) of the journey's walked paths the case covers."""
    jp = [p for p in journey_paths if p]
    if not jp:
        return 0
    cp = set(case_path_list)
    covered = sum(1 for p in jp if p in cp)
    return int(round(100 * covered / len(jp)))


#: A journey needs at least this many DISTINCT pages before an "end to end"
#: case can be claimed for it. One page is not a journey — it is a page, and
#: adopting a script for it would put a business name on a claim the walk
#: never made (observed live: a single-page journey adopted a 3-step
#: navigation case that had nothing to do with it).
MIN_JOURNEY_PAGES = 2


def spans_journey(journey_paths: list[str], case_path_list: list[str]) -> bool:
    """A case SPANS a journey when it WALKS the journey's pages IN ORDER.

    Set intersection is not enough: a case that merely touches the same pages
    in a different order is a different business path. The journey's page
    sequence must appear as an ordered subsequence of the case's own page
    sequence — that is what "this script re-proves that journey" means.

    Journeys shallower than :data:`MIN_JOURNEY_PAGES` never span: a one-page
    journey would be spanned by almost any case, which is how a quote journey
    ended up adopting a home→start navigation script."""
    jp = [p for p in journey_paths if p]
    if len(set(jp)) < MIN_JOURNEY_PAGES:
        return False
    it = iter(case_path_list)
    return all(any(cp == p for cp in it) for p in jp)


def extraneous_steps(journey_paths: list[str], case_path_list: list[str]) -> int:
    """How far a case WANDERS beyond the journey it claims to cover.

    A case may span the journey and still continue into unrelated territory
    (observed live: a quote-journey case that walked on to a 'Member Sign In'
    click and failed there). Such a case would red-flag the journey for a
    reason the journey never claimed, so among spanning candidates the
    TIGHTEST fit wins — same coverage, fewest foreign pages."""
    jp = set(p for p in journey_paths if p)
    return sum(1 for p in case_path_list if p not in jp)


def display_name_for(business_name: str, entry_title: str) -> str:
    name = _WS_RE.sub(" ", str(business_name or entry_title or "").strip())
    return (f"Verify {name} end to end" if name else "Verify journey end to end")[:300]


async def journey_walked_paths(
    session, *, tenant_id: str, app_id: str,
    journey: JourneyRow | None = None, journey_id: str = "",
) -> list[str]:
    """The ordered node paths of the journey's most recent COMPLETED
    traversal ([] when it never completed — such a journey is not runnable
    and links stay informational)."""
    jid = journey.journey_id if journey is not None else journey_id
    traversal = (await session.execute(
        select(JourneyTraversalRow).where(
            JourneyTraversalRow.tenant_id == tenant_id,
            JourneyTraversalRow.app_id == app_id,
            JourneyTraversalRow.journey_id == jid,
            JourneyTraversalRow.completed.is_(True),
        ).order_by(JourneyTraversalRow.created_at.desc())
        .limit(1))).scalar_one_or_none()
    if traversal is None:
        return []
    paths: list[str] = []
    for fp in list(traversal.path_fps or []):
        url = (await session.execute(
            select(JourneyNodeRow.url).where(
                JourneyNodeRow.tenant_id == tenant_id,
                JourneyNodeRow.app_id == app_id,
                JourneyNodeRow.fingerprint == str(fp),
            ))).scalar_one_or_none()
        p = norm_path(url) if url else ""
        if p and (not paths or paths[-1] != p):
            paths.append(p)
    return paths


async def journey_walk_signature(
    session, *, tenant_id: str, app_id: str,
    journey: JourneyRow | None = None, journey_id: str = "",
) -> tuple[list[str], list[str]]:
    """(page paths, advance labels) of the journey's most recent COMPLETED
    walk — the two signals a case must reproduce to claim it re-proves this
    journey. Labels come from the edges the walk traversed, in order."""
    jid = journey.journey_id if journey is not None else journey_id
    traversal = (await session.execute(
        select(JourneyTraversalRow).where(
            JourneyTraversalRow.tenant_id == tenant_id,
            JourneyTraversalRow.app_id == app_id,
            JourneyTraversalRow.journey_id == jid,
            JourneyTraversalRow.completed.is_(True),
        ).order_by(JourneyTraversalRow.created_at.desc())
        .limit(1))).scalar_one_or_none()
    if traversal is None:
        return [], []
    fps = [str(fp) for fp in (traversal.path_fps or [])]
    paths: list[str] = []
    for fp in fps:
        url = (await session.execute(
            select(JourneyNodeRow.url).where(
                JourneyNodeRow.tenant_id == tenant_id,
                JourneyNodeRow.app_id == app_id,
                JourneyNodeRow.fingerprint == fp,
            ))).scalar_one_or_none()
        p = norm_path(url) if url else ""
        if p and (not paths or paths[-1] != p):
            paths.append(p)
    labels: list[str] = []
    for a, b in zip(fps, fps[1:]):
        trigger = (await session.execute(
            select(JourneyEdgeRow.trigger_label_norm).where(
                JourneyEdgeRow.tenant_id == tenant_id,
                JourneyEdgeRow.app_id == app_id,
                JourneyEdgeRow.from_fp == a,
                JourneyEdgeRow.to_fp == b,
            ).limit(1))).scalar_one_or_none()
        label = normalize_option_label(str(trigger or ""))
        if label:
            labels.append(label)
    return paths, labels


async def link_app_journeys(
    *, tenant_id: str, app_id: str, artifact_id: str,
) -> dict[str, int]:
    """Re-derive every journey⇄case link for one app + artifact.

    Returns counts. Raises only on DB-session failure (the callback wrapper
    catches); factory unavailability degrades to a logged no-op — the next
    completion re-links."""
    report = {"journeys": 0, "linked": 0, "adopted": 0}
    if not artifact_id:
        return report
    try:
        cases = await factory.list_test_cases(
            tenant_id=tenant_id, artifact_id=artifact_id)
    except Exception as exc:
        logger.warning(
            "qec.journey_linker.cases_unavailable tenant=%s artifact=%s err=%s",
            tenant_id, artifact_id, str(exc)[:200])
        return report
    scored_cases = [
        {"test_case_id": str(c.get("test_case_id") or ""),
         "name": str(c.get("name") or "")[:300],
         "paths": case_paths(c.get("test_case")),
         "labels": case_labels(c.get("test_case"))}
        for c in cases if str(c.get("test_case_id") or "")
    ]

    async with tenant_scoped_qec_session(tenant_id) as session:
        journeys = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
            ))).scalars().all()
        for journey in journeys:
            report["journeys"] += 1
            walked, walk_labels = await journey_walk_signature(
                session, tenant_id=tenant_id, app_id=app_id, journey=journey)
            if not walked:
                continue
            matches = []
            for c in scored_cases:
                score = coverage_score(walked, c["paths"])
                if score >= settings.journey_link_min_score:
                    matches.append((
                        score,
                        walks_journey(walked, walk_labels, c["paths"],
                                      c["labels"]),
                        extraneous_steps(walked, c["paths"]), c))
            if not matches:
                continue
            # Spanning (walks the journey's pages IN ORDER) first, then
            # highest coverage, then the TIGHTEST fit (fewest foreign pages) —
            # a case that wanders past the journey's terminal must not
            # red-flag a claim the journey never made.
            matches.sort(key=lambda m: (-int(m[1]), -m[0], m[2]))
            spanning = [m for m in matches if m[1]]
            adopted_id = spanning[0][3]["test_case_id"] if spanning else ""

            for score, _spans, _extra, c in matches:
                is_adopted = c["test_case_id"] == adopted_id
                link_id = _sid("jcase", tenant_id, journey.journey_id,
                               artifact_id, c["test_case_id"])
                row = (await session.execute(
                    select(JourneyCaseRow).where(
                        JourneyCaseRow.link_id == link_id,
                    ))).scalar_one_or_none()
                kind = KIND_JOURNEY_E2E if is_adopted else KIND_LINKED
                display = (display_name_for(journey.business_name,
                                            journey.entry_title)
                           if is_adopted else "")
                if row is None:
                    session.add(JourneyCaseRow(
                        link_id=link_id, tenant_id=tenant_id, app_id=app_id,
                        journey_id=journey.journey_id,
                        artifact_id=artifact_id,
                        test_case_id=c["test_case_id"],
                        case_name=c["name"], kind=kind,
                        display_name=display, coverage_score=score))
                    report["linked"] += 1
                else:
                    row.case_name = c["name"]
                    row.kind = kind
                    row.display_name = display
                    row.coverage_score = score
                if is_adopted:
                    report["adopted"] += 1
    logger.warning(
        "qec.journey_linker.linked tenant=%s app=%s artifact=%s "
        "journeys=%d linked=%d adopted=%d",
        tenant_id, app_id, artifact_id, report["journeys"],
        report["linked"], report["adopted"])
    return report


async def runnable_case(
    session, *, tenant_id: str, app_id: str, journey_id: str, artifact_id: str,
) -> Optional[JourneyCaseRow]:
    """The journey's adopted end-to-end case for the CURRENT artifact, or
    ``None`` (the caller states the honest not-runnable reason)."""
    if not artifact_id:
        return None
    return (await session.execute(
        select(JourneyCaseRow).where(
            JourneyCaseRow.tenant_id == tenant_id,
            JourneyCaseRow.app_id == app_id,
            JourneyCaseRow.journey_id == journey_id,
            JourneyCaseRow.artifact_id == artifact_id,
            JourneyCaseRow.kind == KIND_JOURNEY_E2E,
        ))).scalar_one_or_none()
