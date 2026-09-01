"""Conversational, GROUNDED test-case proposal for the Test Factory.

The user describes a case in plain English ("add a case for an invalid passport
number"); we PROPOSE a new ProductionTestCase — but the model may only *select*
from a catalog of pages/fields/buttons that were actually captured in the
recording.  Selectors are computed server-side (never by the model), and every
step is re-validated against the live catalog before it is persisted, so a
proposed case can never reference a screen, field, or control that was not
demonstrated.  Anti-hallucination by construction — same posture as the
read-only assistant, extended to *write* a grounded case.

Survival across regeneration: an added case is stored as a real
``FactoryTestCaseRow`` with ``generator_version="v1-human"`` (so neither the
demonstrated-prune, keyed on ``v1``, nor the category-prune, keyed on
``test_type``, removes it) AND mirrored into
``CanonicalArtifactRow.full_artifact_json["test_factory_added_cases"]`` so
``/generate`` can re-ground + re-upsert it (idempotent).  Additive — no schema
migration, outside the frozen capture pipeline.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm.attributes import flag_modified

from nexus_sdk.models import ProductionTestCase, ProductionTestStep
from nexus_sdk.db.models import FactoryTestCaseRow, CanonicalArtifactRow

from ..llm import CompletionRequest, FinishReason
from .generator import _is_real_value, _locator, _observed
from .service import _load_current_pages_and_actions
from ...database import tenant_scoped_session

_LLM_TASK = os.environ.get("LLM_TASK_ASSISTANT", "form_snapshot_extraction")
_MAX_PAGES = 40
_ADDED_KEY = "test_factory_added_cases"
_APPROVED_KEY = "test_factory_approved"
_GEN_VERSION = "v1-human"

_SYSTEM_PROMPT = (
    "You are Co-Architect inside Nexus Test Studio. The user wants to ADD a new "
    "test case to a suite that was generated from THEIR screen recording. You "
    "assemble that case using ONLY the pages, fields, and buttons in the CATALOG "
    "below (these are the only things the recording actually captured).\n\n"
    "BUILD A COMPLETE END-TO-END FLOW (this is the default — do not stop early):\n"
    "- Walk through the pages IN THE ORDER they appear in the CATALOG. For EACH "
    "page: fill that page's fields, then click that page's primary/advance button "
    "(e.g. Next / Continue / Submit) to move forward to the next page.\n"
    "- Continue all the way to the LAST page and submit it. A test case that "
    "covers only the first page is WRONG unless the user explicitly asked for a "
    "single page or a single field.\n"
    "- Apply the user's requested value(s) (e.g. a specific country) on the page "
    "whose field matches; fill every OTHER field with its demonstrated value or a "
    "captured option so the flow can actually complete.\n"
    "- If a page has no advance button in the catalog, still fill its fields, then "
    "continue with the next page.\n\n"
    "STRICT RULES — anti-hallucination:\n"
    "- Use ONLY page_key / field / button names that appear verbatim in the "
    "CATALOG. Never invent a screen, field, or control.\n"
    "- For a field, set value to one of its captured options, its demonstrated "
    "value, or — only for a free-text field (no options) — a realistic value the "
    "user describes.\n"
    "- If the user's request cannot be expressed with the catalog, return the "
    "closest grounded case you can and say what was missing in 'notes'.\n\n"
    "Output STRICT JSON only, no prose, in this exact shape:\n"
    '{"name": "<short title>", "notes": "<what you could/couldn\'t ground>", '
    '"steps": [{"page_key": "<from catalog>", "action": "type|select|click", '
    '"field_label": "<field or button label from catalog>", "value": "<value or empty for click>"}]}'
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _page_name(page_key: str) -> str:
    seg = (page_key or "").rstrip("/").rsplit("/", 1)[-1] or "home"
    seg = re.sub(r"[-_]+", " ", seg).strip()
    return seg.title() if seg else "page"


# ─── Catalog: the grounded menu the model is allowed to choose from ───────────

def build_catalog(
    visits: Sequence[Any],
    actions: Sequence[Any],
) -> dict[str, Any]:
    """Reduce the demonstrated flow to {pages:[{page_key,url,page_name,fields,buttons}]}.

    Only real, captured signals — field labels + values (form_snapshot), captured
    options + required (form_snapshot_signals), and click/press targets
    (page_actions) — ever enter the catalog.
    """
    actions_by_visit: dict[str, list[Any]] = {}
    for a in actions:
        actions_by_visit.setdefault(a.page_visit_id, []).append(a)

    pages: list[dict[str, Any]] = []
    for idx, v in enumerate(sorted(visits, key=lambda x: x.sequence_index)):
        signals: Mapping[str, Any] = getattr(v, "form_snapshot_signals", None) or {}
        fields: list[dict[str, Any]] = []
        seen: set[str] = set()

        for label, val in (v.form_snapshot or {}).items():
            if not _is_real_value(label, val):
                continue
            meta = signals.get(label) if isinstance(signals.get(label), Mapping) else {}
            options = list(dict.fromkeys(
                str(o).strip() for o in (meta.get("options") or []) if str(o).strip()
            ))
            fields.append({
                "label": str(label),
                "demonstrated_value": "" if val is None else str(val),
                "options": options,
                "required": bool(meta.get("required")),
            })
            seen.add(_norm(str(label)))

        # Fields seen only as captured option-domains (vision), not in the snapshot.
        for label, meta in signals.items():
            if _norm(str(label)) in seen or not isinstance(meta, Mapping):
                continue
            options = list(dict.fromkeys(
                str(o).strip() for o in (meta.get("options") or []) if str(o).strip()
            ))
            if not options:
                continue
            fields.append({
                "label": str(label),
                "demonstrated_value": str(meta.get("selected") or ""),
                "options": options,
                "required": bool(meta.get("required")),
            })
            seen.add(_norm(str(label)))

        buttons: list[dict[str, str]] = []
        bseen: set[str] = set()
        for a in sorted(actions_by_visit.get(v.page_visit_id, []), key=lambda x: x.subaction_index):
            if (a.verb or "").lower() in {"click", "press"} and (a.target_label or "").strip():
                key = _norm(a.target_label)
                if key in bseen:
                    continue
                bseen.add(key)
                buttons.append({"label": a.target_label.strip(), "kind": (a.target_kind or "button").strip() or "button"})

        if not (fields or buttons):
            continue

        page_key = (v.url_path or "").strip() or (v.location or "").strip() or f"page-{idx + 1}"
        host = (v.url_host or "").strip()
        path = (v.url_path or "").strip()
        url = (host + path) if host else (path or page_key)
        pages.append({
            "page_key": page_key,
            "url": url,
            "page_name": _page_name(page_key),
            "fields": fields,
            "buttons": buttons,
        })
        if len(pages) >= _MAX_PAGES:
            break

    return {"pages": pages}


def _render_catalog(catalog: dict[str, Any]) -> str:
    lines: list[str] = ["CATALOG (the ONLY pages/fields/buttons you may use):"]
    for p in catalog["pages"]:
        lines.append(f"- page_key: {p['page_key']}  ({p['page_name']})")
        for f in p["fields"]:
            opt = f" options=[{', '.join(f['options'][:12])}]" if f["options"] else ""
            req = " required" if f["required"] else ""
            dv = f" demonstrated='{f['demonstrated_value']}'" if f["demonstrated_value"] else ""
            lines.append(f"    field: {f['label']}{dv}{opt}{req}")
        for b in p["buttons"]:
            lines.append(f"    button: {b['label']}")
    return "\n".join(lines)


def _build_prompt(catalog: dict[str, Any], message: str, history: Sequence[dict]) -> str:
    parts = [_render_catalog(catalog), ""]
    recent = [h for h in history if isinstance(h, dict)][-6:]
    if recent:
        parts.append("CONVERSATION SO FAR:")
        for turn in recent:
            role = "User" if turn.get("role") == "user" else "Assistant"
            parts.append(f"{role}: {str(turn.get('content', ''))[:400]}")
        parts.append("")
    parts.append(f"User request: {message}")
    parts.append("Return the grounded case as STRICT JSON now.")
    return "\n".join(parts)


def _parse_json(text: str) -> dict[str, Any]:
    """Parse the model's JSON — tolerant of ```json fences AND truncation.

    Vision/text models can hit max_tokens mid-array; rather than lose the whole
    proposal we salvage every COMPLETE step object already emitted (balanced
    brace scan), so a truncated response still yields a usable grounded case."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t).rstrip("`").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i = t.find("{")
    if i == -1:
        return {}
    t2 = t[i:]
    # whole-object retry (handles trailing prose after a complete object)
    j = t2.rfind("}")
    if j > 0:
        try:
            return json.loads(t2[:j + 1])
        except Exception:
            pass
    # salvage: collect complete {...} step objects from the steps array
    m = re.search(r'"name"\s*:\s*"([^"]*)"', t2)
    name = m.group(1) if m else ""
    sidx = t2.find('"steps"')
    arr = t2.find("[", sidx) if sidx != -1 else -1
    objs: list[dict[str, Any]] = []
    if arr != -1:
        depth = 0
        start: int | None = None
        instr = False
        esc = False
        for k in range(arr + 1, len(t2)):
            ch = t2[k]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
                continue
            if ch == "{":
                if depth == 0:
                    start = k
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objs.append(json.loads(t2[start:k + 1]))
                    except Exception:
                        pass
                    start = None
            elif ch == "]" and depth == 0:
                break
    return {"name": name, "steps": objs}


# ─── The guardrail: compile raw steps against the catalog, drop the ungrounded ─

def _nav_step(n: int, page: dict[str, Any], verify: bool = False) -> ProductionTestStep:
    if verify:
        # Reached via a prior click (e.g. Next / Submit) — ASSERT the navigation
        # rather than forcing a goto() that can reset client-side wizard state and
        # mask a broken button. The compiler emits expect(page).toHaveURL(...) for
        # a navigate verb whose action does NOT start with "Open".
        path = page.get("page_key") or page["url"]
        return ProductionTestStep(
            step_number=n,
            action=f"Verify the application navigated to {path}",
            expected=f"URL path is {path} and the {page['page_name']} page is displayed",
            expected_result=f"URL path is {path} and the {page['page_name']} page is displayed",
            selector=f"url={path}",
            **_observed(verb="navigate", url=path, provenance="proposed"),
        )
    return ProductionTestStep(
        step_number=n,
        action=f"Open {page['url']}",
        expected=f"The {page['page_name']} page is displayed",
        expected_result=f"The {page['page_name']} page is displayed",
        selector=f"url={page['url']}",
        **_observed(verb="navigate", url=page["url"], provenance="proposed"),
    )


def validate_and_compile(
    catalog: dict[str, Any],
    raw_steps: Sequence[Mapping[str, Any]],
    *,
    default_name: str,
) -> tuple[ProductionTestCase, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (case, accepted_raw, dropped).  Every accepted step references a
    real page + field/button; selectors are computed here, never trusted from
    the model.  Steps that don't ground are dropped with a reason."""
    pages_by_key = {p["page_key"]: p for p in catalog["pages"]}
    # loose resolution: exact, then suffix/substring match on page_key
    def resolve_page(pk: str) -> dict[str, Any] | None:
        pk = (pk or "").strip()
        if pk in pages_by_key:
            return pages_by_key[pk]
        npk = _norm(pk)
        for p in catalog["pages"]:
            if _norm(p["page_key"]) == npk or npk and (npk in _norm(p["page_key"]) or _norm(p["page_key"]) in npk):
                return p
        return None

    steps: list[ProductionTestStep] = []
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    n = 0
    cur_key: str | None = None

    for raw in raw_steps:
        action = _norm(str(raw.get("action") or ""))
        label = str(raw.get("field_label") or raw.get("label") or "").strip()
        value = "" if raw.get("value") is None else str(raw.get("value"))
        page = resolve_page(str(raw.get("page_key") or raw.get("page") or ""))
        if page is None:
            dropped.append({"step": raw, "reason": "page_key not in recording"})
            continue

        # navigation onto a new page (mirrors the generator's nav step): the FIRST
        # page is a real Open (goto); a later page is reached via a prior click, so
        # ASSERT the transition (toHaveURL) instead of a state-resetting goto().
        if cur_key != page["page_key"]:
            n += 1
            steps.append(_nav_step(n, page, verify=(cur_key is not None)))
            cur_key = page["page_key"]

        if action in {"type", "select", "fill", "enter", "choose"}:
            fidx = {_norm(f["label"]): f for f in page["fields"]}
            f = fidx.get(_norm(label))
            if not f:
                dropped.append({"step": raw, "reason": f"field '{label}' not on page {page['page_key']}"})
                continue
            prov = "proposed"
            if f["options"] and value and value not in f["options"]:
                prov = "inferred"  # value not among captured options — honestly flagged
            n += 1
            is_select = action in {"select", "choose"} or bool(f["options"])
            verb = "select" if is_select else "type"
            steps.append(ProductionTestStep(
                step_number=n,
                action=(f"Select '{value}' for '{f['label']}'" if is_select else f"Enter '{value}' in the '{f['label']}' field"),
                expected=f"'{f['label']}' shows '{value}'",
                expected_result=f"'{f['label']}' shows '{value}'",
                selector=_locator(f["label"], "field"),
                data_ref=value,
                **_observed(verb=verb, label=f["label"], kind="field", value=value, provenance=prov),
            ))
            accepted.append({"page_key": page["page_key"], "field_label": f["label"], "action": ("select" if is_select else "type"), "value": value})

        elif action in {"click", "press", "submit", "tap"}:
            bidx = {_norm(b["label"]): b for b in page["buttons"]}
            b = bidx.get(_norm(label))
            if not b:
                dropped.append({"step": raw, "reason": f"button '{label}' not on page {page['page_key']}"})
                continue
            n += 1
            steps.append(ProductionTestStep(
                step_number=n,
                action=f"Click '{b['label']}'",
                expected=f"'{b['label']}' is activated",
                expected_result=f"'{b['label']}' is activated",
                selector=_locator(b["label"], b.get("kind") or "button"),
                **_observed(verb="click", label=b["label"], kind=(b.get("kind") or "button"), provenance="proposed"),
            ))
            accepted.append({"page_key": page["page_key"], "field_label": b["label"], "action": "click", "value": ""})

        elif action in {"navigate", "open", "goto", "go"}:
            continue  # nav already inserted on page change
        else:
            dropped.append({"step": raw, "reason": f"unsupported action '{action}'"})

    case = ProductionTestCase(
        test_id=str(uuid.uuid4()),
        name=(default_name or "Added test case")[:200],
        description="Human-requested, grounded in the recording's captured pages & controls.",
        type="functional",
        tags=["human-added", "proposed"],
        steps=steps,
        expected_outcome="The requested flow completes using only demonstrated controls.",
    )
    return case, accepted, dropped


# ─── Public: propose (no write) + add (persist) + reapply (survive regenerate) ─

async def propose(
    session,
    *,
    artifact_id: str,
    tenant_id: str,
    message: str,
    history: Sequence[dict],
    router,
) -> dict[str, Any]:
    visits, actions = await _load_current_pages_and_actions(session, artifact_id=artifact_id)
    catalog = build_catalog(visits, actions)
    if not catalog["pages"]:
        return {
            "error": True,
            "answer": "No grounded pages/fields are captured for this recording yet. "
                      "Click Generate (and Capture options) first, then ask me to add a case.",
            "grounded_on": {"pages": 0, "fields": 0},
        }

    req = CompletionRequest(
        system=_SYSTEM_PROMPT,
        prompt=_build_prompt(catalog, message, history),
        max_tokens=2400,
        temperature=0.2,
        correlation_id=f"{artifact_id}:propose",
        enable_prompt_cache=True,
        metadata={"task": _LLM_TASK},
    )
    response = await router.complete(task=_LLM_TASK, request=req)
    fields_total = sum(len(p["fields"]) for p in catalog["pages"])
    if response.finish_reason == FinishReason.ERROR or not (response.text or "").strip():
        return {
            "error": True,
            "answer": "I couldn't reach the model to draft the case. Try again.",
            "grounded_on": {"pages": len(catalog["pages"]), "fields": fields_total},
            "model": f"{response.provider}/{response.model}",
        }

    parsed = _parse_json(response.text)
    raw_steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(raw_steps, list) or not raw_steps:
        return {
            "error": True,
            "answer": "I couldn't ground that request to the captured pages. "
                      "Try describing it in terms of a page and its fields.",
            "grounded_on": {"pages": len(catalog["pages"]), "fields": fields_total},
            "model": f"{response.provider}/{response.model}",
        }

    name = (parsed.get("name") or message or "Added test case").strip()[:200]
    case, accepted, dropped = validate_and_compile(catalog, raw_steps, default_name=name)
    case_pages = len({s["page_key"] for s in accepted})
    return {
        "error": False,
        "proposed_case": case.model_dump(mode="json"),
        "grounded_steps": accepted,        # the normalised raw that survived grounding
        "dropped": dropped,
        "name": name,
        "notes": str(parsed.get("notes") or "")[:600],
        "case_pages": case_pages,          # pages the PROPOSED case actually covers
        "grounded_on": {"pages": len(catalog["pages"]), "fields": fields_total},
        "model": f"{response.provider}/{response.model}",
    }


def _row_values_for_case(
    case: ProductionTestCase,
    *,
    test_case_id: str,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    message: str,
    model: str,
) -> dict[str, Any]:
    return {
        "test_case_id": test_case_id,
        "artifact_id": artifact_id,
        "tenant_id": tenant_id,
        "session_id": session_id or "",
        "name": (case.name or "Added test case")[:500],
        "description": case.description or "",
        "priority": "P2_medium",
        "test_type": "functional",
        "confidence": "proposed",
        "status": "active",
        "step_count": len(case.steps),
        "tags": list(case.tags or []),
        "test_case": case.model_dump(mode="json"),
        "source_evidence": {"added": True, "request": message[:1000], "model": model},
        "generator_version": _GEN_VERSION,
    }


async def _upsert(session, values: dict[str, Any]) -> None:
    stmt = pg_insert(FactoryTestCaseRow).values(**values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[FactoryTestCaseRow.test_case_id],
        set_={k: values[k] for k in (
            "name", "description", "priority", "test_type", "confidence",
            "status", "step_count", "tags", "test_case", "source_evidence",
            "generator_version",
        )},
    ))


async def add_case(
    session,
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    name: str,
    steps: Sequence[Mapping[str, Any]],
    message: str,
    model: str = "",
) -> dict[str, Any]:
    """Persist a proposed case.  Re-validates EVERY step against a freshly-built
    catalog (so a tampered client payload can't inject ungrounded steps), stores
    it as a v1-human row, and mirrors it into the artifact json for survival."""
    visits, actions = await _load_current_pages_and_actions(session, artifact_id=artifact_id)
    catalog = build_catalog(visits, actions)
    case, accepted, dropped = validate_and_compile(catalog, steps, default_name=name)
    if not case.steps:
        return {"success": False, "error": "No grounded steps — nothing references the captured pages/fields.", "dropped": dropped}

    test_case_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"human:{artifact_id}:{_norm(name)}:{_norm(message)}"))[:64]
    values = _row_values_for_case(
        case, test_case_id=test_case_id, artifact_id=artifact_id, tenant_id=tenant_id,
        session_id=session_id, message=message, model=model,
    )
    await _upsert(session, values)

    art = (await session.execute(select(CanonicalArtifactRow).where(
        CanonicalArtifactRow.artifact_id == artifact_id,
        CanonicalArtifactRow.tenant_id == tenant_id,
    ))).scalar_one_or_none()
    if art is not None:
        j = dict(art.full_artifact_json or {})
        added = dict(j.get(_ADDED_KEY) or {})
        added[test_case_id] = {"name": case.name, "steps": accepted, "message": message[:1000], "model": model}
        j[_ADDED_KEY] = added
        art.full_artifact_json = j
        flag_modified(art, "full_artifact_json")
    await session.commit()

    return {"success": True, "test_case_id": test_case_id, "steps": len(case.steps), "dropped": dropped}


async def reapply_added_cases(artifact_id: str, tenant_id: str) -> int:
    """Re-ground + re-upsert every human-added case from the artifact json.
    Idempotent — runs after /generate so added cases survive a regenerate even
    if a future prune ever removed them."""
    async with tenant_scoped_session(tenant_id) as session:
        art = (await session.execute(select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        ))).scalar_one_or_none()
        added = dict((getattr(art, "full_artifact_json", None) or {}).get(_ADDED_KEY) or {}) if art else {}
        if not added:
            return 0
        session_id = (getattr(art, "session_id", "") or "")
        visits, actions = await _load_current_pages_and_actions(session, artifact_id=artifact_id)
        catalog = build_catalog(visits, actions)
        n = 0
        for tcid, rec in added.items():
            steps = rec.get("steps") or []
            case, accepted, _ = validate_and_compile(catalog, steps, default_name=rec.get("name") or "Added test case")
            if not case.steps:
                continue
            values = _row_values_for_case(
                case, test_case_id=tcid, artifact_id=artifact_id, tenant_id=tenant_id,
                session_id=session_id, message=str(rec.get("message") or ""), model=str(rec.get("model") or ""),
            )
            await _upsert(session, values)
            n += 1
        await session.commit()
        return n


async def reapply_approved(artifact_id: str, tenant_id: str) -> int:
    """Restore APPROVED case content after a regenerate (governance #4).

    Approval snapshots live in full_artifact_json[_APPROVED_KEY]; we re-upsert
    each so a Generate / Enrich / capture can NEVER silently overwrite a
    human-approved case. Runs LAST (after added + overrides) so approved content
    wins, and marks the row v1-human so future prunes skip it."""
    async with tenant_scoped_session(tenant_id) as session:
        art = (await session.execute(select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        ))).scalar_one_or_none()
        approved = dict((getattr(art, "full_artifact_json", None) or {}).get(_APPROVED_KEY) or {}) if art else {}
        if not approved:
            return 0
        session_id = (getattr(art, "session_id", "") or "")
        n = 0
        for cid, snap in approved.items():
            tc = snap.get("test_case") if isinstance(snap, dict) else None
            if not isinstance(tc, dict) or not tc.get("steps"):
                continue
            values = {
                "test_case_id": cid, "artifact_id": artifact_id, "tenant_id": tenant_id,
                "session_id": session_id,
                "name": (tc.get("name") or "Approved case")[:500],
                "description": tc.get("description") or "",
                "priority": tc.get("priority") or "P2_medium",
                "test_type": "functional",
                "confidence": "approved",
                "status": "active",
                "step_count": len(tc.get("steps") or []),
                "tags": list(tc.get("tags") or []),
                "test_case": tc,
                "source_evidence": {"approved": True, "approved_by": snap.get("approved_by", ""),
                                    "approved_at": snap.get("approved_at", "")},
                "generator_version": _GEN_VERSION,
            }
            await _upsert(session, values)
            n += 1
        await session.commit()
        return n
