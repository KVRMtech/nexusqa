"""Journey naming — business names for graph journeys (Release C2).

A journey's name is what a business user calls the work, not what the DOM
calls the page: "Get a life insurance quote", not "/quote/start". The agent
derives it from SHAPE ONLY — entry title, ordered step titles, outcome value
labels and types, terminal kind. NO URLs enter the prompt (F1/F2 doctrine),
and a proposal that comes back carrying URL-ish text is REJECTED, falling
back to the entry title.

Ownership law: ``name_source`` walks fallback → agent → operator. An
operator's rename is permanent — the agent updates a journey ONLY while
``name_source != 'operator'``, enforced in the UPDATE's WHERE, not in
application hope. Naming is best-effort and never blocks a fold.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import select

from ..clients import platform_api
from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import JourneyNodeRow, JourneyRow, JourneyTraversalRow

logger = logging.getLogger(__name__)

SYSTEM = (
    "You name business journeys in a web application for a business audience. "
    "You receive the journey's entry page title, the ordered titles of the "
    "steps a real walk visited, the outcomes the funnel displayed, and how "
    "the walk ended.  Reply with EXACTLY two lines:\n"
    "NAME: an imperative business name of at most six words (what a user is "
    "doing, e.g. a purchase, a quote, an update)\n"
    "DESCRIPTION: one plain sentence of what the journey does.\n"
    "Never include URLs, paths, domains, or technical identifiers."
)

#: Per-fold naming budget — a fold that discovered many journeys names the
#: first N now and the rest on the next fold (naming is idempotent-safe).
_MAX_NAMES_PER_FOLD = 20

_NAME_RE = re.compile(r"^NAME:\s*(.+)$", re.I | re.M)
_DESC_RE = re.compile(r"^DESCRIPTION:\s*(.+)$", re.I | re.M)
#: URL-ish text that must never appear in a business name (F2 / V_URL_TEXT
#: doctrine): schemes, hosts, path-y fragments, query fragments.
_URL_TEXT_RE = re.compile(
    r"(https?://|www\.|[a-z0-9-]+\.(com|net|org|io|co)\b|[/\\][a-z0-9_-]+|[?&=#{}])",
    re.I,
)


def looks_like_url_text(text: str) -> bool:
    return bool(_URL_TEXT_RE.search(str(text or "")))


def _clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip().strip('"')).strip()[:60]


def parse_proposal(text: str) -> Optional[tuple[str, str]]:
    """(name, description) from the agent's two-line reply, or ``None`` when
    unreadable or URL-tainted — an unreadable proposal is NOT a name."""
    name_m = _NAME_RE.search(text or "")
    if not name_m:
        return None
    name = _clean_name(name_m.group(1))
    if not name or looks_like_url_text(name):
        return None
    desc_m = _DESC_RE.search(text or "")
    desc = re.sub(r"\s+", " ", (desc_m.group(1).strip() if desc_m else ""))[:200]
    if looks_like_url_text(desc):
        desc = ""
    return name, desc


def build_prompt(
    *, entry_title: str, step_titles: list[str],
    outcomes: list[dict[str, Any]], terminal: str,
) -> str:
    """SHAPE ONLY — titles, outcome labels/types, terminal kind. NO URLs."""
    lines = [f"Entry page: {str(entry_title or '')[:200]}"]
    if step_titles:
        lines.append("Steps walked, in order:")
        for t in step_titles[:20]:
            lines.append(f"  - {str(t or '')[:200]}")
    if outcomes:
        lines.append("Outcomes the funnel displayed:")
        for o in outcomes[:8]:
            label = str(o.get("label") or "")[:120]
            vtype = str(o.get("value_type") or "")[:40]
            lines.append(f"  - {label} ({vtype})")
    lines.append(f"The walk ended at: {str(terminal or '')[:40]}")
    return "\n".join(lines)


async def propose_name(
    *, tenant_id: str, entry_title: str, step_titles: list[str],
    outcomes: list[dict[str, Any]], terminal: str,
) -> Optional[tuple[str, str]]:
    """One agent proposal, validated — or ``None`` (caller keeps fallback)."""
    prompt = build_prompt(entry_title=entry_title, step_titles=step_titles,
                          outcomes=outcomes, terminal=terminal)
    result = await platform_api.complete_llm(
        tenant_id=tenant_id, prompt=prompt, system=SYSTEM,
        task="name_journey", max_tokens=80, temperature=0.0,
    )
    if not result.ok:
        logger.warning("qec.journey_naming.llm_failed",
                       extra={"tenant_id": tenant_id,
                              "detail": (result.detail or "")[:200]})
        return None
    return parse_proposal(result.text or "")


async def name_unnamed_journeys(*, tenant_id: str, app_id: str) -> dict[str, int]:
    """Propose business names for this app's still-fallback journeys.

    Operator names are untouchable (WHERE name_source != 'operator'); agent
    re-proposals are allowed to refresh earlier agent names only when the
    journey is still on fallback (stable names beat churn). Best-effort."""
    named = failed = 0
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            rows = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.tenant_id == tenant_id,
                    JourneyRow.app_id == app_id,
                    JourneyRow.name_source == "fallback",
                ).limit(_MAX_NAMES_PER_FOLD))).scalars().all()
            for journey in rows:
                traversal = (await session.execute(
                    select(JourneyTraversalRow).where(
                        JourneyTraversalRow.tenant_id == tenant_id,
                        JourneyTraversalRow.app_id == app_id,
                        JourneyTraversalRow.journey_id == journey.journey_id,
                    ).order_by(JourneyTraversalRow.created_at.desc())
                    .limit(1))).scalar_one_or_none()
                step_titles: list[str] = []
                outcomes: list[dict[str, Any]] = []
                terminal = ""
                if traversal is not None:
                    terminal = traversal.terminal
                    outcomes = list(traversal.outcome_values or [])
                    for fp in list(traversal.path_fps or [])[:20]:
                        title = (await session.execute(
                            select(JourneyNodeRow.title).where(
                                JourneyNodeRow.tenant_id == tenant_id,
                                JourneyNodeRow.app_id == app_id,
                                JourneyNodeRow.fingerprint == str(fp),
                            ))).scalar_one_or_none()
                        if title:
                            step_titles.append(title)
                proposal = await propose_name(
                    tenant_id=tenant_id, entry_title=journey.entry_title,
                    step_titles=step_titles, outcomes=outcomes,
                    terminal=terminal)
                if proposal is None:
                    failed += 1
                    continue
                # Operator wins forever — re-check ownership at write time.
                if journey.name_source == "operator":
                    continue
                journey.business_name, journey.name_description = proposal
                journey.name_source = "agent"
                journey.updated_at = utc_now()
                named += 1
    except Exception as exc:
        logger.warning("qec.journey_naming.failed",
                       extra={"tenant_id": tenant_id, "app_id": app_id,
                              "error": str(exc)[:200]})
        return {"named": named, "failed": failed + 1}
    return {"named": named, "failed": failed}
