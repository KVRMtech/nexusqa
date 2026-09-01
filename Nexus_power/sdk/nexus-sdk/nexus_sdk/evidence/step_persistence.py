"""Persistence helpers for the evidence_steps + cursor_events tables.

Bridges the in-memory output of :class:`TriangulatedClassifier` and
:class:`CursorTracker` to dict shapes ready for SQLAlchemy bulk-insert
against the schema introduced in Alembic 017.

Usage from a service-layer caller (e.g. spine engine)::

    from nexus_sdk.evidence.step_persistence import (
        triangulated_actions_to_step_rows,
        cursor_events_to_db_rows,
    )

    step_rows = triangulated_actions_to_step_rows(
        actions=actions,
        artifact_id=artifact_id,
        scene_id=scene_id,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    await db.execute(
        EvidenceStepRow.__table__.insert(),
        step_rows,
    )

These helpers are deliberately ORM-agnostic — they return plain dicts so
the caller can use SQLAlchemy Core, the async dialect's ``insert``, or a
raw SQL builder.  The dict keys match the column names in
:class:`nexus_sdk.db.models.EvidenceStepRow` /
:class:`nexus_sdk.db.models.CursorEventRow`.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from .triangulator import TriangulatedAction


def triangulated_actions_to_step_rows(
    *,
    actions: Iterable[TriangulatedAction],
    artifact_id: str,
    scene_id: str,
    tenant_id: str,
    session_id: str,
) -> list[dict]:
    """Convert :class:`TriangulatedAction` objects into ``evidence_steps`` rows.

    The caller is responsible for ensuring ``scene_id`` matches the scene
    these actions belong to — the triangulator does not know its scene.

    ``step_id`` is taken from the action's metadata when present
    (the triangulator allocates a UUID at classification time so the
    same TriangulatedAction can be referenced before and after persistence)
    and otherwise generated fresh.  This makes the persist step
    idempotent under retries: repeating the call with the same actions
    inserts the same step_ids and the database's primary-key constraint
    short-circuits duplicates.
    """
    rows: list[dict] = []
    for action in actions:
        step_id = action.metadata.get("step_id") if action.metadata else None
        rows.append({
            "step_id": str(step_id or uuid.uuid4()),
            "artifact_id": artifact_id,
            "scene_id": scene_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "step_index": int(action.step_index),
            "action_kind": str(action.action_kind),
            "target_label": str(action.target_label or "")[:500],
            "observed_value": str(action.observed_value or "")[:2000],
            "trigger_control_id": action.trigger_control_id,
            "before_frame_id": action.before_frame_id or None,
            "after_frame_id": action.after_frame_id or None,
            "start_ms": int(action.start_ms or 0),
            "end_ms": int(action.end_ms or 0),
            "confidence": float(action.confidence or 0.0),
            "agreement_score": float(action.agreement_score or 0.0),
            "evidence_signals": list(action.evidence_signals or []),
            "cursor_x": (
                int(action.cursor_x) if action.cursor_x is not None else None
            ),
            "cursor_y": (
                int(action.cursor_y) if action.cursor_y is not None else None
            ),
            "audio_intent_text": str(action.audio_intent_text or ""),
            "audio_intent_ts_ms": (
                int(action.audio_intent_ts_ms)
                if action.audio_intent_ts_ms is not None
                else None
            ),
            # metadata_json holds anything the triangulator didn't put in a
            # first-class column — drop the step_id we already promoted so
            # we don't store it twice.
            "metadata_json": _strip_step_id(action.metadata),
        })
    return rows


def cursor_events_to_db_rows(
    *,
    events: Iterable,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    frame_id_lookup: dict[int, str] | None = None,
) -> list[dict]:
    """Convert :class:`CursorEvent` objects into ``cursor_events`` rows.

    ``frame_id_lookup`` maps ``frame_index → frame_id`` so the persistence
    layer can resolve the FK before insert.  When the lookup is missing
    or does not contain the frame_index, ``frame_id`` is left None — the
    column is nullable for exactly this case.
    """
    rows: list[dict] = []
    lookup = dict(frame_id_lookup or {})
    for event in events:
        # Allow either CursorEvent dataclass instances or plain dicts.
        if hasattr(event, "__dataclass_fields__"):
            data = {f: getattr(event, f) for f in event.__dataclass_fields__}
        elif isinstance(event, dict):
            data = dict(event)
        else:  # pragma: no cover — defensive
            raise TypeError(f"unsupported cursor event type: {type(event)!r}")

        frame_idx = int(data.get("frame_index", 0))
        rows.append({
            "event_id": str(data.get("event_id") or uuid.uuid4()),
            "artifact_id": artifact_id,
            "frame_id": data.get("frame_id") or lookup.get(frame_idx),
            "tenant_id": tenant_id,
            "session_id": session_id,
            "frame_index": frame_idx,
            "timestamp_ms": int(data.get("timestamp_ms", 0)),
            "cursor_x": int(data.get("cursor_x", 0)),
            "cursor_y": int(data.get("cursor_y", 0)),
            "velocity": float(data.get("velocity", 0.0)),
            "is_click": bool(data.get("is_click", False)),
            "is_drag": bool(data.get("is_drag", False)),
            "detection_method": str(data.get("detection_method", "motion"))[:32],
            "confidence": float(data.get("confidence", 0.0)),
            "metadata_json": dict(data.get("metadata") or {}),
        })
    return rows


def _strip_step_id(meta: dict | None) -> dict:
    if not meta:
        return {}
    return {k: v for k, v in meta.items() if k != "step_id"}


# ─── Test-case step generation (Phase C.3) ───────────────────────────────────

# Action_kind → human-readable verb template.  ``{target}`` and ``{value}``
# are substituted with the step's target_label and observed_value.  The
# templates intentionally read like manual test-script lines so a reviewer
# can validate them before committing to an automation suite.
_ACTION_TEMPLATES: dict[str, dict[str, str]] = {
    "click_cta": {
        "action": "Click on {target}",
        "expected": "{target} is activated and the next state loads",
    },
    "enter_text": {
        "action": "Enter '{value}' in {target}",
        "expected": "Value '{value}' appears in {target}",
    },
    "select_option": {
        "action": "Select '{value}' from {target}",
        "expected": "Option '{value}' is selected in {target}",
    },
    "submit_form": {
        "action": "Submit the form",
        "expected": "Form submission succeeds and the next page is shown",
    },
    "navigate": {
        "action": "Navigate to {target}",
        "expected": "{target} loads successfully",
    },
    "review": {
        "action": "Review {target}",
        "expected": "{target} content is verified",
    },
    "scroll": {
        "action": "Scroll to {target}",
        "expected": "{target} is in view",
    },
    "open_overlay": {
        "action": "Open {target}",
        "expected": "{target} is displayed",
    },
    "close_overlay": {
        "action": "Close {target}",
        "expected": "{target} is dismissed",
    },
    "ui_interaction": {
        "action": "Interact with {target}",
        "expected": "UI state advances",
    },
}


def evidence_steps_to_test_case_step_rows(
    *,
    evidence_steps: Iterable[dict],
    test_case_id: str,
    starting_step_number: int = 1,
) -> list[dict]:
    """Convert ``evidence_steps`` rows into ``test_case_steps`` rows.

    Output dicts match :class:`nexus_sdk.db.models.TestCaseStepRow` columns
    so the caller can do a SQLAlchemy bulk-insert without further mapping.

    The action and expected_result strings are built from the canonical
    action_kind taxonomy.  We never fabricate a target/value — when an
    evidence step has no target_label we use a neutral placeholder ("the
    UI") rather than inventing a button name; reviewers can spot the
    placeholder and add a real label if needed.

    Each test_case_step carries a back-reference to the source evidence
    step in metadata_json so an automation engineer can click through
    from the generated test case to the proof in the evidence pipeline.
    """
    rows: list[dict] = []
    for offset, ev in enumerate(evidence_steps):
        action_kind = str(ev.get("action_kind") or "ui_interaction")
        template = _ACTION_TEMPLATES.get(
            action_kind, _ACTION_TEMPLATES["ui_interaction"],
        )
        target = (ev.get("target_label") or "").strip() or "the UI"
        value = (ev.get("observed_value") or "").strip()

        action_text = template["action"].format(target=target, value=value)
        expected = template["expected"].format(target=target, value=value)

        # Trim the now-unused ``{value}`` placeholder when the action has
        # no observed_value (avoids printing "Enter '' in field" verbatim).
        if not value:
            action_text = action_text.replace(" ''", "").replace(" '' ", " ")
            expected = expected.replace(" ''", "").replace(" '' ", " ")

        rows.append({
            "step_id": str(uuid.uuid4()),
            "test_case_id": test_case_id,
            "step_number": starting_step_number + offset,
            "action": action_text,
            "expected_result": expected,
            "target_system": "web",
            "target_element": (ev.get("target_label") or "")[:500],
            "input_data_refs": [],
            "verification": _verification_for_kind(action_kind, target, value),
            "screenshot_required": action_kind in ("review", "submit_form"),
            "metadata_json": {
                "evidence_step_id": ev.get("step_id"),
                "evidence_signals": list(ev.get("evidence_signals") or []),
                "agreement_score": float(ev.get("agreement_score") or 0.0),
                "audio_intent_text": ev.get("audio_intent_text") or "",
            },
            "evidence_scene_id": ev.get("scene_id"),
            "evidence_control_id": ev.get("trigger_control_id"),
            # evidence_edge_id is set by the orchestrator if it joins this
            # step to a flow edge during persistence; left None here.
            "evidence_edge_id": None,
            "proof_confidence": float(ev.get("confidence") or 0.0),
            "evidence_mode": _evidence_mode_label(list(ev.get("evidence_signals") or [])),
        })
    return rows


def _verification_for_kind(action_kind: str, target: str, value: str) -> str:
    """Generate a brief verification statement matching the action_kind."""
    if action_kind == "enter_text" and value:
        return f"Verify the field shows '{value}' after entry."
    if action_kind == "select_option" and value:
        return f"Verify '{value}' is the selected option."
    if action_kind == "click_cta":
        return f"Verify the resulting screen state after clicking {target}."
    if action_kind == "submit_form":
        return "Verify the next page loads without form errors."
    if action_kind == "navigate":
        return f"Verify {target} URL/title is now active."
    if action_kind == "review":
        return f"Verify all expected content is visible on {target}."
    return "Verify the UI state advances as expected."


def _evidence_mode_label(signals: list[str]) -> str:
    """Map the set of evidence signals to a coarse evidence-mode tag.

    Used by the test harness to choose proof-collection strategy at run
    time.  ``multimodal`` when 2+ signals contributed; ``ocr`` when only
    OCR; ``cursor`` when only cursor; etc.  Defaults to ``multimodal``.
    """
    canon = {s.split("_", 1)[0] if s in ("cursor_click", "cursor_hover") else s for s in signals}
    canon.discard("")
    if len(canon) >= 2:
        return "multimodal"
    if "audio_intent" in canon:
        return "audio"
    if "cursor" in canon:
        return "cursor"
    if "ocr_diff" in canon:
        return "ocr"
    if "llava_delta" in canon:
        return "llava"
    return "multimodal"
