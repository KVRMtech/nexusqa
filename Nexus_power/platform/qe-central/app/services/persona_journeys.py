"""P3 — persona journey generation (the capstone).

Given ONE Master Catalog + the P1 trigger→child rules + a set of answers, produce
the concrete business journey those answers yield — which questions are executed,
which are dynamically activated, which are skipped — analytically, no crawl. This
is Generation mode: replay the catalog with a persona's answers.

``project_from_catalog`` is pure (catalog + rules + answers in, journey out) and
unit-tested; ``project_app_journey`` is the thin DB wrapper the route calls.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import catalog_store
from .journey_projector import _norm, project_traversal


def _resolve_answers(
    questions: list[Mapping[str, Any]], answers: Mapping[str, Any]
) -> dict[str, str]:
    """Accept answers keyed by ``question_id`` OR by (accessible) name; return a
    map keyed by ``question_id``. Unknown keys are dropped — never guessed onto a
    question."""
    ids = {str(q.get("question_id")) for q in questions if isinstance(q, Mapping)}
    by_name: dict[str, str] = {}
    for q in questions:
        if isinstance(q, Mapping):
            qid = str(q.get("question_id") or "")
            if qid:
                by_name.setdefault(_norm(q.get("name")), qid)
    out: dict[str, str] = {}
    for key, val in answers.items():
        ks = str(key)
        if ks in ids:
            out[ks] = str(val)
        else:
            qid = by_name.get(_norm(ks))
            if qid:
                out[qid] = str(val)
    return out


def project_from_catalog(
    master: Mapping[str, Any],
    rules: list[Mapping[str, Any]],
    answers: Mapping[str, Any],
) -> dict[str, Any]:
    """Pure: project one persona's journey and enrich it with question detail."""
    questions = list(master.get("questions") or [])
    by_id: dict[str, Mapping[str, Any]] = {
        str(q.get("question_id")): q for q in questions if isinstance(q, Mapping)}
    resolved = _resolve_answers(questions, answers)
    proj = project_traversal(questions, rules, resolved)

    def _detail(ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for qid in ids:
            q = by_id.get(qid, {})
            out.append({"question_id": qid, "name": q.get("name", ""),
                        "type": q.get("type", "")})
        return out

    return {
        "answered": len(resolved),
        "question_count": len(questions),
        "executed": _detail(proj["executed"]),
        "activated": _detail(proj["activated"]),
        "skipped": _detail(proj["skipped"]),
        "counts": {
            "executed": len(proj["executed"]),
            "activated": len(proj["activated"]),
            "skipped": len(proj["skipped"]),
            "on_path": len(proj["visible"]),
        },
    }


async def project_app_journey(
    tenant_id: str, app_id: str, answers: Mapping[str, Any]
) -> dict[str, Any]:
    """Load the app's Master Catalog + rules and project the journey for these
    answers. The read path for ``POST /apps/{app_id}/catalog/project``."""
    master, rules = await catalog_store.load_catalog_and_rules(tenant_id, app_id)
    result = project_from_catalog(master, rules, answers)
    result["app_id"] = app_id
    return result
