"""Visual graph → compact textual context for the Co-Architect LLM.

Produces a deterministic, dense summary of the visual evidence graph that
fits inside a system prompt.  Trim aggressively — OCR is the biggest
contributor, so we cap it per scene.

The structure is:

    APPS:
      app_id  app_name   scenes=N
    SCENES:
      scene_id  idx  title  type  ocr_preview
    CONTROLS (per scene):
      scene_id  ->  control_id  type  label  selector  conf  ready
    FLOW EDGES (action-confirmed only):
      edge_id  from->to  trigger=control_id  action=...  conf=...

The IDs are 8-char truncated for readability; the LLM should always
reference them with the truncated form, and the validator on the
``propose_scenario`` path expands them by prefix match.
"""
from __future__ import annotations

from typing import Any


_MAX_SCENES = 60
_MAX_CONTROLS_PER_SCENE = 8
_MAX_EDGES = 80
_OCR_PREVIEW_CHARS = 140


def _short(value: str | None, n: int = 8) -> str:
    return (value or "")[:n]


def _scene_label(scene: dict) -> str:
    state = scene.get("scene_state_summary") or {}
    return (
        state.get("screen_title")
        or scene.get("screen_name")
        or f"scene-{scene.get('scene_index', '?')}"
    )


def _control_summary(c: dict) -> str:
    label = (c.get("label_text") or c.get("display_label") or "").strip() or "(unlabeled)"
    sel = c.get("playwright_selector") or ""
    conf = c.get("selector_confidence")
    parts = [
        _short(c.get("control_id", "")),
        c.get("element_type", "") or "?",
        repr(label[:60]),
    ]
    if sel:
        parts.append(f"selector={sel[:80]!r}")
    if conf is not None:
        parts.append(f"conf={conf:.2f}")
    if c.get("automation_ready"):
        parts.append("ready=YES")
    else:
        parts.append("ready=no")
    return "  " + "  ".join(parts)


def build_graph_context(graph: dict[str, Any]) -> str:
    """Render the visual graph as a single string for the LLM system prompt.

    Returns an empty string if the graph has nothing renderable. The Heart
    chat endpoint should refuse to proceed in that case.
    """
    if not isinstance(graph, dict):
        return ""

    scenes: list[dict] = graph.get("scenes") or []
    controls_by_scene: dict[str, list[dict]] = graph.get("controls_by_scene") or {}
    edges: list[dict] = graph.get("edges") or []
    app_instances: list[dict] = graph.get("app_instances") or []

    if not scenes:
        return ""

    lines: list[str] = []
    lines.append("=== VISUAL EVIDENCE GRAPH ===")
    lines.append(
        f"Counts: scenes={len(scenes)}, controls={sum(len(v) for v in controls_by_scene.values())}, "
        f"edges={len(edges)}, apps={len(app_instances)}"
    )
    lines.append("")

    if app_instances:
        lines.append("APPS:")
        for app in app_instances:
            lines.append(
                f"  {_short(app.get('instance_id', ''))}  "
                f"{(app.get('app_name') or app.get('app_type') or 'App'):<30}  "
                f"scenes={app.get('scene_count', '?')}"
            )
        lines.append("")

    # Sort scenes by scene_index for stable, human-readable order
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_index", 0))
    truncated_scenes = sorted_scenes[:_MAX_SCENES]

    lines.append("SCENES:")
    for s in truncated_scenes:
        sid = _short(s.get("scene_id", ""))
        idx = s.get("scene_index", "?")
        title = _scene_label(s)
        screen_type = (s.get("scene_state_summary") or {}).get("screen_type", "")
        ocr = (s.get("ocr_text") or "").replace("\n", " ")[:_OCR_PREVIEW_CHARS]
        lines.append(
            f"  {sid}  #{idx:>3}  {title[:32]:<32}  type={screen_type or '?'}  ocr={ocr!r}"
        )
    if len(sorted_scenes) > _MAX_SCENES:
        lines.append(f"  ... and {len(sorted_scenes) - _MAX_SCENES} more scenes")
    lines.append("")

    lines.append("CONTROLS (per scene, automation_ready first):")
    for s in truncated_scenes:
        sid = s.get("scene_id", "")
        ctrls = list(controls_by_scene.get(sid, []) or [])
        if not ctrls:
            continue
        ctrls.sort(key=lambda c: (
            0 if c.get("automation_ready") else 1,
            -(c.get("selector_confidence") or 0),
        ))
        lines.append(f"  [scene {_short(sid)}]")
        for c in ctrls[:_MAX_CONTROLS_PER_SCENE]:
            lines.append(_control_summary(c))
        if len(ctrls) > _MAX_CONTROLS_PER_SCENE:
            lines.append(f"  ... {len(ctrls) - _MAX_CONTROLS_PER_SCENE} more")
    lines.append("")

    # Edges: prefer action-confirmed transitions; fall back to all if sparse
    confirmed = [e for e in edges if e.get("edge_type") == "action_confirmed_transition"]
    edge_pool = confirmed if confirmed else edges
    lines.append(f"FLOW EDGES (action_confirmed, top {_MAX_EDGES}):")
    for e in edge_pool[:_MAX_EDGES]:
        summary = e.get("primary_action_summary") or {}
        action_label = summary.get("action_label") or e.get("action_type") or "transition"
        conf = e.get("action_confidence") or e.get("evidence_confidence") or 0.0
        trigger = _short(e.get("trigger_control_id", "")) or "?"
        lines.append(
            f"  {_short(e.get('edge_id', ''))}  "
            f"{_short(e.get('from_scene_id', ''))} -> {_short(e.get('to_scene_id', ''))}  "
            f"trigger={trigger}  action={action_label[:40]!r}  conf={conf:.2f}"
        )
    if len(edge_pool) > _MAX_EDGES:
        lines.append(f"  ... {len(edge_pool) - _MAX_EDGES} more edges")

    return "\n".join(lines)
