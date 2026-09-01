"""Exploration PLANNER (the caged agent) — an LLM proposes WHERE to prioritize,
the deterministic crawler decides WHAT is reachable.

The quarantined explorer never talks to an LLM (design §1.1). Planning happens
HERE, in qe-central, and the plan travels to the explorer as pure ORDERING DATA:
a small set of grounded ``priority_patterns`` the explorer's frontier uses to
reorder what it would visit anyway.  A plan can NEVER:
  * add a state, cross the submit boundary, or leave the egress fence (the plan
    only lowers a frontier item's priority — the state machine still gates every
    action, submit, and egress the exact same way);
  * chase a hallucinated section (every pattern MUST be a substring of a REAL
    route the crawl already reached — ungrounded patterns are dropped).

Fail-open by construction: no LLM / bad output / no prior artifact ⇒ an EMPTY
plan ⇒ a byte-identical crawl.  This is the "agent inside the cage": the planner
adds intelligence to ordering; the cage keeps it safe.
"""
from __future__ import annotations

import json
import logging
import re

from ..clients import platform_api
from .synthesis import known_labels_for_artifact, known_routes_for_artifact

logger = logging.getLogger(__name__)

#: Hard bounds on a plan (a plan is an optimizer, never an attack surface).
_MAX_PATTERNS = 8
_MIN_WEIGHT, _MAX_WEIGHT = 1, 3
_MAX_PATTERN_LEN = 60
#: A pattern is a plain route/section token — letters, digits, and a few URL-path
#: characters ONLY (no regex metachars, no whitespace) so it can never be more
#: than a substring test on the explorer side.
_PATTERN_RX = re.compile(r"^[a-z0-9][a-z0-9/_.#-]{0,59}$")

PLANNER_SYSTEM = (
    "You are a test-exploration PLANNER for autonomous web crawling. You do NOT "
    "control the crawler; you only RANK which already-discovered app sections are "
    "the highest business value to explore first (quote/checkout/claim/apply "
    "funnels, money and policy paths, account management). Return STRICT JSON only."
)


def _build_prompt(base_url: str, routes: list[str], labels: list[str]) -> str:
    return (
        "Given a web application, propose which sections to prioritize during "
        "autonomous exploration so a finite crawl budget lands on the highest-"
        "business-value user journeys first.\n\n"
        f"App base URL: {base_url}\n"
        f"Routes already discovered (you MUST only reference these): {json.dumps(routes)}\n"
        f"Form field labels seen: {json.dumps(labels[:40])}\n\n"
        "Return STRICT JSON: {\"priority_patterns\": [{\"pattern\": <a substring of "
        "one of the discovered routes, e.g. \"quote\" or \"checkout\">, \"weight\": "
        "1-3 (3 = highest value), \"reason\": <short>}]}. Rank money/policy/"
        "application funnels highest. Do NOT invent routes not in the list. "
        f"At most {_MAX_PATTERNS} patterns."
    )


def _validate_plan(raw_text: str, routes: list[str]) -> dict:
    """Parse + GROUND + bound an LLM plan into ``{priority_patterns:[{pattern,
    weight,reason}]}``.  Every rule fails CLOSED to dropping the pattern — a plan
    that cannot be fully trusted simply carries fewer (or zero) patterns."""
    try:
        data = json.loads(_strip_fences(raw_text))
    except Exception:
        return {"priority_patterns": []}
    proposed = data.get("priority_patterns") if isinstance(data, dict) else None
    if not isinstance(proposed, list):
        return {"priority_patterns": []}

    route_blob = " ".join(routes).lower()
    out: list[dict] = []
    seen: set[str] = set()
    for item in proposed:
        if not isinstance(item, dict) or len(out) >= _MAX_PATTERNS:
            continue
        pattern = str(item.get("pattern") or "").strip().lower()[:_MAX_PATTERN_LEN]
        if not pattern or pattern in seen or not _PATTERN_RX.match(pattern):
            continue
        # GROUNDING: the pattern must actually occur in a discovered route.
        if pattern not in route_blob:
            logger.info("qec.planner.pattern_rejected_ungrounded pattern=%s", pattern[:40])
            continue
        try:
            weight = int(item.get("weight") or 1)
        except (TypeError, ValueError):
            weight = 1
        weight = max(_MIN_WEIGHT, min(_MAX_WEIGHT, weight))
        seen.add(pattern)
        out.append({"pattern": pattern, "weight": weight,
                    "reason": str(item.get("reason") or "").strip()[:200]})
    return {"priority_patterns": out}


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.lstrip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()


async def build_exploration_plan(
    tenant_id: str, app_id: str, artifact_id: str, base_url: str,
) -> dict:
    """Build a grounded, bounded exploration plan for a RE-crawl, or an empty plan.

    Empty (⇒ byte-identical crawl) when: there is no prior artifact to ground
    against (a first crawl cannot be planned — nothing has been seen yet), the LLM
    is unavailable, or nothing survives grounding.  Never raises."""
    try:
        if not artifact_id:
            return {"priority_patterns": []}
        routes = await known_routes_for_artifact(tenant_id, artifact_id)
        if not routes:
            return {"priority_patterns": []}
        labels = await known_labels_for_artifact(tenant_id, artifact_id)
        llm = await platform_api.complete_llm(
            tenant_id=tenant_id, prompt=_build_prompt(base_url, routes, labels),
            system=PLANNER_SYSTEM, task="exploration_plan", max_tokens=1200,
        )
        if not llm.ok:
            logger.info("qec.planner.llm_unavailable detail=%s", (llm.detail or "")[:120])
            return {"priority_patterns": []}
        plan = _validate_plan(llm.text, routes)
        logger.info("qec.planner.built app_id=%s patterns=%d",
                    app_id, len(plan["priority_patterns"]))
        return plan
    except Exception:  # a planner failure must NEVER block a crawl
        logger.warning("qec.planner.failed app_id=%s", app_id, exc_info=True)
        return {"priority_patterns": []}
