"""Demo Diff (P7a) — pure-function diff between two visual evidence graphs.

The diff identifies four classes of change:
  * Scenes added / removed / kept-but-changed
  * App instances added / removed
  * Controls added / removed / kept-but-renamed-or-moved (per scene)
  * Flow edges added / removed

Scene identity is established via ``page_key`` (from scene_state_summary)
when available, otherwise falling back to scene_index then OCR fingerprint.
This is the only way to correlate scenes across artifacts when scene_ids
are regenerated on each run (which they are — uuid5 of timing data).

Control identity inside a matched scene is established by
``label_text`` (case-insensitive, trimmed).  If two controls in the same
scene share a label they're matched in order of selector_confidence.

The diff is *purely structural* — it does not call an LLM, does not
compute pixel diffs, does not classify changes as "intentional" or
"regression".  Higher layers (UI + future heuristics) interpret severity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# ─── Data shapes ─────────────────────────────────────────────────────────


@dataclass
class ControlDiff:
    """One control's diff inside a matched scene."""
    label: str
    baseline_control_id: str
    current_control_id: str
    change_kind: str  # "added" | "removed" | "renamed" | "selector_changed" | "moved" | "automation_lost" | "automation_gained" | "unchanged"
    detail: str = ""


@dataclass
class SceneDiff:
    """One scene's diff. Either added / removed / matched-but-changed."""
    page_key: str
    baseline_scene_id: str  # "" if added
    current_scene_id: str   # "" if removed
    change_kind: str        # "added" | "removed" | "ocr_changed" | "controls_changed" | "unchanged"
    title: str = ""
    baseline_ocr_preview: str = ""
    current_ocr_preview: str = ""
    control_diffs: list[ControlDiff] = field(default_factory=list)


@dataclass
class EdgeDiff:
    """One flow edge's diff. Edge identity uses (from_page_key, to_page_key,
    action_kind) — robust across artifact regenerations."""
    from_page_key: str
    to_page_key: str
    action_kind: str
    baseline_edge_id: str
    current_edge_id: str
    change_kind: str  # "added" | "removed"


@dataclass
class DiffReport:
    baseline_artifact_id: str
    current_artifact_id: str
    scenes: list[SceneDiff] = field(default_factory=list)
    edges: list[EdgeDiff] = field(default_factory=list)
    app_instances_added: list[str] = field(default_factory=list)
    app_instances_removed: list[str] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "baseline_artifact_id": self.baseline_artifact_id,
            "current_artifact_id": self.current_artifact_id,
            "scenes": [_dc_to_dict(s) for s in self.scenes],
            "edges": [_dc_to_dict(e) for e in self.edges],
            "app_instances_added": list(self.app_instances_added),
            "app_instances_removed": list(self.app_instances_removed),
            "summary": dict(self.summary),
        }


def _dc_to_dict(obj: Any) -> dict:
    d = obj.__dict__.copy()
    if "control_diffs" in d:
        d["control_diffs"] = [_dc_to_dict(c) for c in d["control_diffs"]]
    return d


# ─── Identity keys ───────────────────────────────────────────────────────


def _scene_page_key(scene: dict) -> str:
    """Best stable identifier for a scene across artifact regenerations.

    Prefer the structured ``page_key`` produced by Phase 8 scene state
    summarisation.  Fall back to a lowercase screen title, then to
    ``scene-{index}``.  Empty string is never returned (we'd lose the
    scene from the diff).
    """
    state = scene.get("scene_state_summary") or {}
    candidate = (
        state.get("page_key")
        or state.get("screen_title")
        or scene.get("screen_name")
        or ""
    )
    candidate = (candidate or "").strip().lower()
    if not candidate:
        candidate = f"scene-{scene.get('scene_index', '?')}"
    return candidate


def _control_label(ctrl: dict) -> str:
    return (
        ctrl.get("label_text")
        or ctrl.get("display_label")
        or ctrl.get("element_type")
        or ""
    ).strip().lower()


def _edge_key(edge: dict, scene_page_keys: dict[str, str]) -> tuple[str, str, str]:
    """Edge identity: (from_page_key, to_page_key, action_kind)."""
    from_pk = scene_page_keys.get(edge.get("from_scene_id", ""), "")
    to_pk = scene_page_keys.get(edge.get("to_scene_id", ""), "")
    action = (
        (edge.get("primary_action_summary") or {}).get("action_kind")
        or edge.get("action_type")
        or "transition"
    )
    return (from_pk, to_pk, str(action))


# ─── Diff core ───────────────────────────────────────────────────────────


def _control_change_kind(baseline: dict, current: dict) -> tuple[str, str]:
    """Return (change_kind, detail) for two matched controls.

    Detection order matters — we surface the most actionable change first.
    """
    base_label = (baseline.get("label_text") or "").strip()
    cur_label = (current.get("label_text") or "").strip()
    base_sel = baseline.get("playwright_selector") or ""
    cur_sel = current.get("playwright_selector") or ""
    base_ready = bool(baseline.get("automation_ready"))
    cur_ready = bool(current.get("automation_ready"))

    if base_label and cur_label and base_label.lower() != cur_label.lower():
        return ("renamed", f"label: {base_label!r} -> {cur_label!r}")
    if base_sel and cur_sel and base_sel != cur_sel:
        return ("selector_changed", f"selector: {base_sel!r} -> {cur_sel!r}")
    if base_ready and not cur_ready:
        return ("automation_lost", "automation_ready: True -> False")
    if not base_ready and cur_ready:
        return ("automation_gained", "automation_ready: False -> True")

    # Bounding-box movement check (only when both have bbox data)
    base_bbox = baseline.get("bounding_box")
    cur_bbox = current.get("bounding_box")
    if isinstance(base_bbox, dict) and isinstance(cur_bbox, dict):
        try:
            bcx = float(base_bbox.get("x", 0)) + float(base_bbox.get("width", 0)) / 2
            bcy = float(base_bbox.get("y", 0)) + float(base_bbox.get("height", 0)) / 2
            ccx = float(cur_bbox.get("x", 0)) + float(cur_bbox.get("width", 0)) / 2
            ccy = float(cur_bbox.get("y", 0)) + float(cur_bbox.get("height", 0)) / 2
            distance = ((bcx - ccx) ** 2 + (bcy - ccy) ** 2) ** 0.5
            if distance > 80.0:
                return ("moved", f"moved ~{distance:.0f}px on screen")
        except (TypeError, ValueError):
            pass

    return ("unchanged", "")


def _diff_controls_in_scene(
    baseline_controls: list[dict],
    current_controls: list[dict],
) -> list[ControlDiff]:
    """Match controls by lowercase label, then report add/remove/changed.

    Within a single scene, two controls with the same label are matched
    by descending selector_confidence so the "best" pair always lines up.
    """
    base_by_label: dict[str, list[dict]] = {}
    for c in baseline_controls:
        base_by_label.setdefault(_control_label(c), []).append(c)
    cur_by_label: dict[str, list[dict]] = {}
    for c in current_controls:
        cur_by_label.setdefault(_control_label(c), []).append(c)

    diffs: list[ControlDiff] = []
    all_labels = set(base_by_label) | set(cur_by_label)
    for label in sorted(all_labels):
        if not label:
            # Skip unlabeled controls — too noisy to diff usefully
            continue
        base_list = sorted(
            base_by_label.get(label, []),
            key=lambda c: -(c.get("selector_confidence") or 0),
        )
        cur_list = sorted(
            cur_by_label.get(label, []),
            key=lambda c: -(c.get("selector_confidence") or 0),
        )

        n_paired = min(len(base_list), len(cur_list))
        for i in range(n_paired):
            base = base_list[i]
            cur = cur_list[i]
            kind, detail = _control_change_kind(base, cur)
            diffs.append(ControlDiff(
                label=base.get("label_text") or cur.get("label_text") or label,
                baseline_control_id=base.get("control_id", ""),
                current_control_id=cur.get("control_id", ""),
                change_kind=kind,
                detail=detail,
            ))
        # Leftover baseline-only entries
        for base in base_list[n_paired:]:
            diffs.append(ControlDiff(
                label=base.get("label_text") or label,
                baseline_control_id=base.get("control_id", ""),
                current_control_id="",
                change_kind="removed",
                detail="control no longer in scene",
            ))
        # Leftover current-only entries
        for cur in cur_list[n_paired:]:
            diffs.append(ControlDiff(
                label=cur.get("label_text") or label,
                baseline_control_id="",
                current_control_id=cur.get("control_id", ""),
                change_kind="added",
                detail="new control in scene",
            ))
    return diffs


def _scene_ocr_preview(scene: dict, limit: int = 200) -> str:
    return (scene.get("ocr_text") or "").replace("\n", " ")[:limit]


def diff_visual_graphs(
    baseline: dict,
    current: dict,
    *,
    baseline_artifact_id: str = "",
    current_artifact_id: str = "",
) -> DiffReport:
    """Compute a structural diff between two visual evidence graphs.

    Either graph may be empty (e.g. baseline missing) — the diff then
    reports everything from the non-empty side as added/removed.
    """
    baseline_scenes = baseline.get("scenes") or []
    current_scenes = current.get("scenes") or []
    baseline_controls_by_scene = baseline.get("controls_by_scene") or {}
    current_controls_by_scene = current.get("controls_by_scene") or {}
    baseline_edges = baseline.get("edges") or []
    current_edges = current.get("edges") or []
    baseline_apps = baseline.get("app_instances") or []
    current_apps = current.get("app_instances") or []

    # Build page_key indexes
    base_scenes_by_pk: dict[str, dict] = {}
    base_scene_pk_lookup: dict[str, str] = {}
    for s in baseline_scenes:
        pk = _scene_page_key(s)
        # Last-wins is fine; same page_key seen multiple times = same logical scene
        base_scenes_by_pk[pk] = s
        base_scene_pk_lookup[s.get("scene_id", "")] = pk

    cur_scenes_by_pk: dict[str, dict] = {}
    cur_scene_pk_lookup: dict[str, str] = {}
    for s in current_scenes:
        pk = _scene_page_key(s)
        cur_scenes_by_pk[pk] = s
        cur_scene_pk_lookup[s.get("scene_id", "")] = pk

    # ── Scene-level diff
    scene_diffs: list[SceneDiff] = []
    all_page_keys = sorted(set(base_scenes_by_pk) | set(cur_scenes_by_pk))
    counts = {"scenes_added": 0, "scenes_removed": 0, "scenes_changed": 0, "scenes_unchanged": 0}

    for pk in all_page_keys:
        baseline_scene = base_scenes_by_pk.get(pk)
        current_scene = cur_scenes_by_pk.get(pk)
        title = ""
        if current_scene:
            title = (
                (current_scene.get("scene_state_summary") or {}).get("screen_title")
                or current_scene.get("screen_name")
                or pk
            )
        elif baseline_scene:
            title = (
                (baseline_scene.get("scene_state_summary") or {}).get("screen_title")
                or baseline_scene.get("screen_name")
                or pk
            )

        if baseline_scene and not current_scene:
            scene_diffs.append(SceneDiff(
                page_key=pk,
                baseline_scene_id=baseline_scene.get("scene_id", ""),
                current_scene_id="",
                change_kind="removed",
                title=title,
                baseline_ocr_preview=_scene_ocr_preview(baseline_scene),
            ))
            counts["scenes_removed"] += 1
            continue
        if current_scene and not baseline_scene:
            scene_diffs.append(SceneDiff(
                page_key=pk,
                baseline_scene_id="",
                current_scene_id=current_scene.get("scene_id", ""),
                change_kind="added",
                title=title,
                current_ocr_preview=_scene_ocr_preview(current_scene),
            ))
            counts["scenes_added"] += 1
            continue

        # Both present — compare OCR + controls
        assert baseline_scene is not None and current_scene is not None
        base_id = baseline_scene.get("scene_id", "")
        cur_id = current_scene.get("scene_id", "")
        base_ocr = _scene_ocr_preview(baseline_scene)
        cur_ocr = _scene_ocr_preview(current_scene)
        ocr_changed = bool(base_ocr) and bool(cur_ocr) and base_ocr != cur_ocr

        ctrl_diffs = _diff_controls_in_scene(
            baseline_controls_by_scene.get(base_id, []) or [],
            current_controls_by_scene.get(cur_id, []) or [],
        )
        meaningful_ctrl_diffs = [c for c in ctrl_diffs if c.change_kind != "unchanged"]

        if ocr_changed or meaningful_ctrl_diffs:
            change_kind = "controls_changed" if meaningful_ctrl_diffs else "ocr_changed"
            scene_diffs.append(SceneDiff(
                page_key=pk,
                baseline_scene_id=base_id,
                current_scene_id=cur_id,
                change_kind=change_kind,
                title=title,
                baseline_ocr_preview=base_ocr,
                current_ocr_preview=cur_ocr,
                control_diffs=meaningful_ctrl_diffs,
            ))
            counts["scenes_changed"] += 1
        else:
            scene_diffs.append(SceneDiff(
                page_key=pk,
                baseline_scene_id=base_id,
                current_scene_id=cur_id,
                change_kind="unchanged",
                title=title,
                control_diffs=[],
            ))
            counts["scenes_unchanged"] += 1

    # ── Edge-level diff
    base_edges_by_key: dict[tuple[str, str, str], dict] = {
        _edge_key(e, base_scene_pk_lookup): e
        for e in baseline_edges
        if _edge_key(e, base_scene_pk_lookup)[0]  # require valid from/to
    }
    cur_edges_by_key: dict[tuple[str, str, str], dict] = {
        _edge_key(e, cur_scene_pk_lookup): e
        for e in current_edges
        if _edge_key(e, cur_scene_pk_lookup)[0]
    }
    edge_diffs: list[EdgeDiff] = []
    counts["edges_added"] = 0
    counts["edges_removed"] = 0
    for key in sorted(set(base_edges_by_key) | set(cur_edges_by_key)):
        from_pk, to_pk, action = key
        base = base_edges_by_key.get(key)
        cur = cur_edges_by_key.get(key)
        if base and not cur:
            edge_diffs.append(EdgeDiff(
                from_page_key=from_pk, to_page_key=to_pk, action_kind=action,
                baseline_edge_id=base.get("edge_id", ""), current_edge_id="",
                change_kind="removed",
            ))
            counts["edges_removed"] += 1
        elif cur and not base:
            edge_diffs.append(EdgeDiff(
                from_page_key=from_pk, to_page_key=to_pk, action_kind=action,
                baseline_edge_id="", current_edge_id=cur.get("edge_id", ""),
                change_kind="added",
            ))
            counts["edges_added"] += 1

    # ── App instance diff (identity = app_name + app_type)
    def _app_key(app: dict) -> str:
        return f"{(app.get('app_name') or '').lower()}|{(app.get('app_type') or '').lower()}"

    base_app_keys = {_app_key(a): a.get("app_name") or a.get("app_type") or "" for a in baseline_apps}
    cur_app_keys = {_app_key(a): a.get("app_name") or a.get("app_type") or "" for a in current_apps}
    apps_added = sorted([cur_app_keys[k] for k in (cur_app_keys.keys() - base_app_keys.keys()) if cur_app_keys[k]])
    apps_removed = sorted([base_app_keys[k] for k in (base_app_keys.keys() - cur_app_keys.keys()) if base_app_keys[k]])

    counts["apps_added"] = len(apps_added)
    counts["apps_removed"] = len(apps_removed)
    counts["controls_added"] = sum(
        1 for sd in scene_diffs for cd in sd.control_diffs if cd.change_kind == "added"
    )
    counts["controls_removed"] = sum(
        1 for sd in scene_diffs for cd in sd.control_diffs if cd.change_kind == "removed"
    )
    counts["controls_renamed"] = sum(
        1 for sd in scene_diffs for cd in sd.control_diffs if cd.change_kind == "renamed"
    )
    counts["controls_moved"] = sum(
        1 for sd in scene_diffs for cd in sd.control_diffs if cd.change_kind == "moved"
    )

    return DiffReport(
        baseline_artifact_id=baseline_artifact_id,
        current_artifact_id=current_artifact_id,
        scenes=scene_diffs,
        edges=edge_diffs,
        app_instances_added=apps_added,
        app_instances_removed=apps_removed,
        summary=counts,
    )


# ─── Scenario-impact analysis ────────────────────────────────────────────


def affected_scenarios(
    scenarios: Iterable[dict],
    diff: DiffReport,
) -> list[dict]:
    """For each scenario, report which steps cite scenes/controls/edges
    that changed in the diff.  Returns a list of impact records:

        [{
          "scenario_id": ...,
          "title": ...,
          "affected_step_numbers": [1, 3],
          "reasons": ["control 'Submit' was renamed", "scene 'Login' was removed"],
        }, ...]

    Scenarios with no affected steps are NOT included.
    """
    # Build lookup: baseline IDs -> their change description
    removed_scenes: dict[str, str] = {
        sd.baseline_scene_id: sd.title or sd.page_key
        for sd in diff.scenes
        if sd.change_kind == "removed" and sd.baseline_scene_id
    }
    changed_scenes: dict[str, str] = {
        sd.baseline_scene_id: sd.title or sd.page_key
        for sd in diff.scenes
        if sd.change_kind in ("controls_changed", "ocr_changed") and sd.baseline_scene_id
    }
    changed_controls: dict[str, tuple[str, str]] = {}
    for sd in diff.scenes:
        for cd in sd.control_diffs:
            if cd.change_kind in ("removed", "renamed", "selector_changed", "moved", "automation_lost") and cd.baseline_control_id:
                changed_controls[cd.baseline_control_id] = (cd.change_kind, cd.label)
    removed_edges: dict[str, tuple[str, str]] = {
        ed.baseline_edge_id: (ed.from_page_key, ed.to_page_key)
        for ed in diff.edges
        if ed.change_kind == "removed" and ed.baseline_edge_id
    }

    out: list[dict] = []
    for sc in scenarios:
        affected_steps: list[int] = []
        reasons: list[str] = []
        for step in sc.get("steps") or []:
            scene_id = step.get("evidence_scene_id") or ""
            control_id = step.get("evidence_control_id") or ""
            edge_id = step.get("evidence_edge_id") or ""
            step_num = step.get("step_number")

            step_added = False
            if scene_id in removed_scenes:
                reasons.append(f"step {step_num}: scene {removed_scenes[scene_id]!r} was removed")
                if not step_added and step_num is not None:
                    affected_steps.append(step_num)
                    step_added = True
            elif scene_id in changed_scenes:
                reasons.append(f"step {step_num}: scene {changed_scenes[scene_id]!r} changed")
                if not step_added and step_num is not None:
                    affected_steps.append(step_num)
                    step_added = True
            if control_id in changed_controls:
                kind, label = changed_controls[control_id]
                reasons.append(f"step {step_num}: control {label!r} {kind}")
                if not step_added and step_num is not None:
                    affected_steps.append(step_num)
                    step_added = True
            if edge_id in removed_edges:
                frm, to = removed_edges[edge_id]
                reasons.append(f"step {step_num}: transition {frm!r} -> {to!r} no longer in graph")
                if not step_added and step_num is not None:
                    affected_steps.append(step_num)

        if affected_steps:
            out.append({
                "scenario_id": sc.get("scenario_id"),
                "title": sc.get("title"),
                "strategy": sc.get("strategy"),
                "state": sc.get("state"),
                "affected_step_numbers": sorted(set(affected_steps)),
                "reasons": reasons,
            })
    return out
