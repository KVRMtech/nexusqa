"""P3 — persona journey generation (the capstone).

Given ONE Master Catalog + the P1 trigger→child rules + a set of answers, produce
the concrete business journey those answers yield — which questions are executed,
which are dynamically activated, which are skipped — analytically, no crawl. This
is Generation mode: replay the catalog with a persona's answers.

``project_from_catalog`` is pure (catalog + rules + answers in, journey out) and
unit-tested; ``project_app_journey`` is the thin DB wrapper the route calls.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import PersonaJourneyRow, PersonaRow
from . import catalog_store
from .journey_projector import _norm, project_traversal


def _sid(*parts: str) -> str:
    """Deterministic row id from a natural key — the upsert identity."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


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


# ── stored personas → generated journeys (the 20-persona generation) ─────────────

def _ids(detail: list[Mapping[str, Any]]) -> list[str]:
    return [str(d.get("question_id")) for d in detail if d.get("question_id")]


def _path_hash(result: Mapping[str, Any]) -> str:
    keys = sorted(_ids(result.get("executed") or []) + _ids(result.get("activated") or []))
    return hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:64]


async def _persist_persona_journey(
    session: Any, tenant_id: str, app_id: str, persona_id: str,
    result: Mapping[str, Any],
) -> str:
    """Upsert the projected journey (one per persona, journey_id=""). Provenance
    stays ``inferred`` — a verifying crawl is what would make it live_confirmed."""
    executed, activated, skipped = (
        _ids(result.get("executed") or []),
        _ids(result.get("activated") or []),
        _ids(result.get("skipped") or []))
    ph = _path_hash(result)
    row = (await session.execute(select(PersonaJourneyRow).where(
        PersonaJourneyRow.tenant_id == tenant_id,
        PersonaJourneyRow.app_id == app_id,
        PersonaJourneyRow.persona_id == persona_id,
        PersonaJourneyRow.journey_id == "",
    ))).scalar_one_or_none()
    if row is None:
        session.add(PersonaJourneyRow(
            persona_journey_id=_sid("pjourney", tenant_id, app_id, persona_id),
            tenant_id=tenant_id, app_id=app_id, persona_id=persona_id,
            journey_id="", path_hash=ph, executed=executed, activated=activated,
            skipped=skipped, provenance="inferred", created_at=utc_now()))
    else:
        row.path_hash = ph
        row.executed, row.activated, row.skipped = executed, activated, skipped
        row.provenance = "inferred"
    return ph


async def register_persona(
    *, tenant_id: str, app_id: str, name: str, answers: Mapping[str, Any],
) -> dict[str, Any]:
    """Register (or update) a persona and its declared answers for an app.

    The persona's answers live IN qe-central — decision-level option labels the
    projector consumes — so generation needs no cross-service value egress.
    """
    pid = _sid("persona", tenant_id, app_id, str(name))
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(select(PersonaRow).where(
            PersonaRow.tenant_id == tenant_id, PersonaRow.app_id == app_id,
            PersonaRow.persona_id == pid))).scalar_one_or_none()
        if row is None:
            session.add(PersonaRow(
                persona_id=pid, tenant_id=tenant_id, app_id=app_id,
                name=str(name)[:200], answers=dict(answers or {})))
        else:
            row.name = str(name)[:200]
            row.answers = dict(answers or {})
    return {"persona_id": pid, "name": str(name)[:200]}


async def generate_persona_journey(
    *, tenant_id: str, app_id: str, persona_id: str,
) -> dict[str, Any]:
    """Project + persist one stored persona's journey."""
    master, rules = await catalog_store.load_catalog_and_rules(tenant_id, app_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        persona = (await session.execute(select(PersonaRow).where(
            PersonaRow.tenant_id == tenant_id, PersonaRow.app_id == app_id,
            PersonaRow.persona_id == persona_id))).scalar_one_or_none()
        if persona is None:
            raise ValueError(f"persona {persona_id} not found")
        result = project_from_catalog(master, rules, persona.answers or {})
        await _persist_persona_journey(session, tenant_id, app_id, persona_id, result)
        name = persona.name
    result["persona_id"] = persona_id
    result["name"] = name
    result["provenance"] = "inferred"
    return result


async def generate_all_journeys(*, tenant_id: str, app_id: str) -> dict[str, Any]:
    """Project + persist EVERY stored persona's journey from the ONE catalog — the
    20-journey generation. One catalog+rules load, N projections (no duplication)."""
    master, rules = await catalog_store.load_catalog_and_rules(tenant_id, app_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        personas = (await session.execute(select(PersonaRow).where(
            PersonaRow.tenant_id == tenant_id,
            PersonaRow.app_id == app_id))).scalars().all()
        out = []
        for p in personas:
            result = project_from_catalog(master, rules, p.answers or {})
            await _persist_persona_journey(session, tenant_id, app_id, p.persona_id, result)
            out.append({"persona_id": p.persona_id, "name": p.name,
                        "counts": result["counts"]})
    return {"generated": len(out),
            "question_count": len(master.get("questions") or []),
            "personas": out}


async def list_personas(*, tenant_id: str, app_id: str) -> dict[str, Any]:
    """Personas registered for an app + each one's latest projected journey."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        prows = (await session.execute(select(PersonaRow).where(
            PersonaRow.tenant_id == tenant_id,
            PersonaRow.app_id == app_id))).scalars().all()
        jrows = (await session.execute(select(PersonaJourneyRow).where(
            PersonaJourneyRow.tenant_id == tenant_id,
            PersonaJourneyRow.app_id == app_id))).scalars().all()
    j_by_pid = {j.persona_id: {
        "counts": {"executed": len(j.executed or []),
                   "activated": len(j.activated or []),
                   "skipped": len(j.skipped or [])},
        "provenance": j.provenance,
        "path_hash": j.path_hash,
        "verified_traversal_id": j.verified_traversal_id or "",
    } for j in jrows}
    return {"personas": [{
        "persona_id": p.persona_id, "name": p.name, "answers": p.answers or {},
        "journey": j_by_pid.get(p.persona_id),
    } for p in prows]}
