"""Smoke test for P7 Demo Diff + Self-Healing primitives.

Pure-function tests:
  * diff_visual_graphs detects added/removed/renamed scenes and controls
  * Edge diff identifies added/removed transitions by (from_pk, to_pk, action)
  * affected_scenarios correctly flags scenarios whose steps reference
    changed scenes/controls
  * build_heal_plan finds exact-in-same-scene matches, label matches in
    other scenes, and correctly fails when no candidate exists
  * apply_heal_plan_to_scenarios swaps IDs in-place + writes audit
  * NO hardcoded fallback IDs leak — when nothing matches we report
    unhealable
"""
from __future__ import annotations

import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_API_ROOT))

# Load diff_and_heal modules directly by file path (skip app/__init__.py).
import importlib.util as _util  # noqa: E402

_PKG_DIR = _API_ROOT / "app" / "services" / "diff_and_heal"

def _load(name: str):
    spec = _util.spec_from_file_location(f"_d_and_h_{name}", _PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = _util.module_from_spec(spec)
    sys.modules[f"_d_and_h_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod

_diff = _load("diff")
_heal = _load("healing")
diff_visual_graphs = _diff.diff_visual_graphs
affected_scenarios = _diff.affected_scenarios
build_heal_plan = _heal.build_heal_plan
apply_heal_plan_to_scenarios = _heal.apply_heal_plan_to_scenarios


# ─── Test fixtures ───────────────────────────────────────────────────────


def _scene(scene_id: str, page_key: str, ocr: str, title: str = "", index: int = 0):
    return {
        "scene_id": scene_id,
        "scene_index": index,
        "screen_name": title or page_key,
        "ocr_text": ocr,
        "scene_state_summary": {
            "page_key": page_key,
            "screen_title": title or page_key,
            "screen_type": "form",
        },
    }


def _control(ctrl_id: str, scene_id: str, label: str, *, selector: str = "", confidence: float = 0.9,
             automation_ready: bool = True, bbox=None, element_type: str = "button"):
    c = {
        "control_id": ctrl_id,
        "scene_id": scene_id,
        "element_type": element_type,
        "label_text": label,
        "playwright_selector": selector or f"button:has-text('{label}')",
        "selector_confidence": confidence,
        "automation_ready": automation_ready,
    }
    if bbox is not None:
        c["bounding_box"] = bbox
    return c


def _edge(edge_id: str, from_sid: str, to_sid: str, action: str = "click",
          trigger_ctrl: str = "", edge_type: str = "action_confirmed_transition"):
    return {
        "edge_id": edge_id,
        "from_scene_id": from_sid,
        "to_scene_id": to_sid,
        "edge_type": edge_type,
        "trigger_control_id": trigger_ctrl,
        "primary_action_summary": {"action_kind": action, "action_label": action},
        "action_type": action,
    }


def _graph(scenes, controls_by_scene, edges, apps=None):
    return {
        "scenes": scenes,
        "controls_by_scene": controls_by_scene,
        "edges": edges,
        "app_instances": apps or [],
    }


# ─── Diff tests ──────────────────────────────────────────────────────────


def test_diff_detects_added_and_removed_scenes():
    baseline = _graph(
        scenes=[
            _scene("scA1", "login", "Sign in"),
            _scene("scA2", "form", "DOB Smoker Calculate"),
        ],
        controls_by_scene={},
        edges=[],
    )
    current = _graph(
        scenes=[
            _scene("scB1", "login", "Sign in"),
            _scene("scB3", "result", "Premium: $847/mo"),
        ],
        controls_by_scene={},
        edges=[],
    )
    report = diff_visual_graphs(baseline, current)
    kinds = {(sd.page_key, sd.change_kind) for sd in report.scenes}
    assert ("login", "unchanged") in kinds
    assert ("form", "removed") in kinds
    assert ("result", "added") in kinds
    assert report.summary["scenes_added"] == 1
    assert report.summary["scenes_removed"] == 1
    print("[OK] diff detects added + removed scenes by page_key")


def test_diff_detects_renamed_control():
    baseline = _graph(
        scenes=[_scene("scA", "form", "click Submit")],
        controls_by_scene={
            "scA": [_control("ctrl-a-1", "scA", "Submit")],
        },
        edges=[],
    )
    current = _graph(
        scenes=[_scene("scB", "form", "click Confirm Purchase")],
        controls_by_scene={
            "scB": [_control("ctrl-b-1", "scB", "Confirm Purchase")],
        },
        edges=[],
    )
    report = diff_visual_graphs(baseline, current)
    # Scene matched on page_key="form", but controls don't share a label
    # so each is reported as removed/added separately. That's correct
    # behavior for a button rename in a single-control scene.
    form_diff = next(sd for sd in report.scenes if sd.page_key == "form")
    kinds = {(cd.label.lower(), cd.change_kind) for cd in form_diff.control_diffs}
    assert ("submit", "removed") in kinds
    assert ("confirm purchase", "added") in kinds
    print("[OK] diff detects single-control scene rename as add+remove")


def test_diff_detects_selector_change_for_same_label():
    """Same label, different selector → 'selector_changed' (not add+remove)."""
    baseline = _graph(
        scenes=[_scene("scA", "form", "")],
        controls_by_scene={
            "scA": [_control("ctrl-a", "scA", "Submit", selector="button#submit")],
        },
        edges=[],
    )
    current = _graph(
        scenes=[_scene("scB", "form", "")],
        controls_by_scene={
            "scB": [_control("ctrl-b", "scB", "Submit", selector="button[data-testid=submit]")],
        },
        edges=[],
    )
    report = diff_visual_graphs(baseline, current)
    form_diff = next(sd for sd in report.scenes if sd.page_key == "form")
    assert any(cd.change_kind == "selector_changed" for cd in form_diff.control_diffs)
    print("[OK] diff detects selector change while preserving control identity by label")


def test_diff_detects_added_and_removed_edges():
    baseline = _graph(
        scenes=[_scene("sA", "login", ""), _scene("sB", "form", ""), _scene("sC", "result", "")],
        controls_by_scene={},
        edges=[
            _edge("e1", "sA", "sB", "click"),
            _edge("e2", "sB", "sC", "click"),
        ],
    )
    current = _graph(
        scenes=[_scene("sX", "login", ""), _scene("sY", "form", ""), _scene("sZ", "error", "")],
        controls_by_scene={},
        edges=[
            _edge("e3", "sX", "sY", "click"),
            _edge("e4", "sY", "sZ", "click"),  # new error path
        ],
    )
    report = diff_visual_graphs(baseline, current)
    edge_kinds = {(ed.from_page_key, ed.to_page_key, ed.change_kind) for ed in report.edges}
    assert ("form", "result", "removed") in edge_kinds
    assert ("form", "error", "added") in edge_kinds
    assert report.summary["edges_added"] == 1
    assert report.summary["edges_removed"] == 1
    print("[OK] diff detects added + removed edges by page-key triple")


def test_affected_scenarios_flags_steps_citing_changed_controls():
    baseline_form_ctrl = "ctrl-old-submit"
    baseline = _graph(
        scenes=[_scene("scOld", "form", "")],
        controls_by_scene={
            "scOld": [_control(baseline_form_ctrl, "scOld", "Submit")],
        },
        edges=[],
    )
    current = _graph(
        scenes=[_scene("scNew", "form", "")],
        controls_by_scene={
            # Renamed: Submit -> Confirm
            "scNew": [_control("ctrl-new-confirm", "scNew", "Confirm")],
        },
        edges=[],
    )
    report = diff_visual_graphs(baseline, current)
    scenarios = [
        {
            "scenario_id": "vs_001",
            "title": "Submit happy path",
            "state": "approved",
            "steps": [
                {
                    "step_number": 1,
                    "evidence_scene_id": "scOld",
                    "evidence_control_id": baseline_form_ctrl,
                    "evidence_edge_id": "",
                },
            ],
        },
        {
            "scenario_id": "vs_002",
            "title": "Unrelated",
            "state": "draft",
            "steps": [
                {
                    "step_number": 1,
                    "evidence_scene_id": "completely-other-scene",
                    "evidence_control_id": "completely-other-control",
                    "evidence_edge_id": "",
                },
            ],
        },
    ]
    impacted = affected_scenarios(scenarios, report)
    assert len(impacted) == 1
    assert impacted[0]["scenario_id"] == "vs_001"
    assert 1 in impacted[0]["affected_step_numbers"]
    print(f"[OK] affected_scenarios flags only impacted scenarios ({len(impacted)} of 2)")


# ─── Healing tests ───────────────────────────────────────────────────────


def test_heal_exact_same_scene_match():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [{
            "step_number": 1,
            "evidence_scene_id": "scene-1",
            "evidence_control_id": "old-ctrl-submit",  # gone
            "target_element": "Submit",
        }],
    }]
    live_graph = _graph(
        scenes=[_scene("scene-1", "form", "")],
        controls_by_scene={
            "scene-1": [_control("new-ctrl-submit", "scene-1", "Submit", confidence=0.95)],
        },
        edges=[],
    )
    plan = build_heal_plan(scenarios, live_graph)
    assert len(plan.suggestions) == 1
    s = plan.suggestions[0]
    assert s.old_control_id == "old-ctrl-submit"
    assert s.new_control_id == "new-ctrl-submit"
    assert s.match_kind == "exact_in_same_scene"
    assert s.match_confidence >= 0.85
    print(f"[OK] build_heal_plan finds exact_in_same_scene match (conf={s.match_confidence})")


def test_heal_label_match_other_scene_lower_confidence():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [{
            "step_number": 1,
            "evidence_scene_id": "scene-form",
            "evidence_control_id": "old-ctrl",
            "target_element": "Calculate",
        }],
    }]
    # Live graph has the Calculate button but in a different scene
    live_graph = _graph(
        scenes=[
            _scene("scene-form", "form", "DOB Smoker"),
            _scene("scene-other", "wizard", "Calculate"),
        ],
        controls_by_scene={
            "scene-other": [_control("new-ctrl-calc", "scene-other", "Calculate", confidence=0.9)],
        },
        edges=[],
    )
    plan = build_heal_plan(scenarios, live_graph)
    assert len(plan.suggestions) == 1
    s = plan.suggestions[0]
    assert s.match_kind == "label_match_other_scene"
    assert s.match_confidence < 0.7  # capped at 0.55 for cross-scene moves
    assert s.new_scene_id == "scene-other"
    print(f"[OK] build_heal_plan finds cross-scene label match (conf={s.match_confidence} < 0.7)")


def test_heal_no_candidate_reports_unhealable():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [{
            "step_number": 1,
            "evidence_scene_id": "scene-1",
            "evidence_control_id": "old-ctrl",
            "target_element": "Submit",
        }],
    }]
    live_graph = _graph(
        scenes=[_scene("scene-1", "form", "")],
        controls_by_scene={
            "scene-1": [_control("ctrl-other", "scene-1", "Cancel")],  # different label
        },
        edges=[],
    )
    plan = build_heal_plan(scenarios, live_graph)
    assert plan.suggestions == []
    assert len(plan.unhealable_steps) == 1
    assert "no live control has label" in plan.unhealable_steps[0]["reason"]
    print("[OK] build_heal_plan reports unhealable when no candidate exists (no fabrication)")


def test_heal_skips_steps_with_valid_control_id():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [
            {
                "step_number": 1,
                "evidence_scene_id": "scene-1",
                "evidence_control_id": "still-valid",  # exists in live graph
                "target_element": "Sign In",
            },
            {
                "step_number": 2,
                "evidence_scene_id": "scene-1",
                "evidence_control_id": "stale",  # missing
                "target_element": "Submit",
            },
        ],
    }]
    live_graph = _graph(
        scenes=[_scene("scene-1", "form", "")],
        controls_by_scene={
            "scene-1": [
                _control("still-valid", "scene-1", "Sign In"),
                _control("new-submit", "scene-1", "Submit"),
            ],
        },
        edges=[],
    )
    plan = build_heal_plan(scenarios, live_graph)
    # Only step 2 should appear in suggestions
    assert len(plan.suggestions) == 1
    assert plan.suggestions[0].step_number == 2
    print("[OK] build_heal_plan skips steps whose control_id still resolves")


def test_apply_heal_plan_mutates_scenarios_and_writes_audit():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [{
            "step_number": 1,
            "evidence_scene_id": "scene-1",
            "evidence_control_id": "stale",
            "target_element": "Submit",
        }],
    }]
    live_graph = _graph(
        scenes=[_scene("scene-1", "form", "")],
        controls_by_scene={
            "scene-1": [_control("fresh-submit", "scene-1", "Submit Order")],
        },
        edges=[],
    )
    suggestions = [{
        "scenario_id": "vs_001",
        "step_number": 1,
        "old_control_id": "stale",
        "new_control_id": "fresh-submit",
        "new_scene_id": "scene-1",
        "new_label": "Submit Order",
    }]
    result = apply_heal_plan_to_scenarios(scenarios, suggestions, live_graph=live_graph)
    assert result.applied_count == 1
    assert result.rejected_count == 0
    # In-place mutation
    assert scenarios[0]["steps"][0]["evidence_control_id"] == "fresh-submit"
    assert scenarios[0]["steps"][0]["target_element"] == "Submit Order"
    # Audit entry
    entry = result.audit_entries[0]
    assert entry["outcome"] == "applied"
    assert entry["old_control_id"] == "stale"
    assert entry["new_control_id"] == "fresh-submit"
    print("[OK] apply_heal_plan_to_scenarios mutates + audits applied swaps")


def test_apply_heal_plan_rejects_suggestions_for_missing_live_control():
    scenarios = [{
        "scenario_id": "vs_001",
        "steps": [{
            "step_number": 1,
            "evidence_scene_id": "scene-1",
            "evidence_control_id": "stale",
            "target_element": "x",
        }],
    }]
    live_graph = _graph(
        scenes=[_scene("scene-1", "form", "")],
        controls_by_scene={
            "scene-1": [_control("really-fresh", "scene-1", "Other")],
        },
        edges=[],
    )
    suggestions = [{
        "scenario_id": "vs_001",
        "step_number": 1,
        "old_control_id": "stale",
        "new_control_id": "this-id-not-in-live-graph",  # invalid
    }]
    result = apply_heal_plan_to_scenarios(scenarios, suggestions, live_graph=live_graph)
    assert result.applied_count == 0
    assert result.rejected_count == 1
    assert "no longer in live graph" in result.audit_entries[0]["reason"]
    # Original step UNCHANGED
    assert scenarios[0]["steps"][0]["evidence_control_id"] == "stale"
    print("[OK] apply_heal_plan_to_scenarios rejects suggestions for stale live IDs")


if __name__ == "__main__":
    test_diff_detects_added_and_removed_scenes()
    test_diff_detects_renamed_control()
    test_diff_detects_selector_change_for_same_label()
    test_diff_detects_added_and_removed_edges()
    test_affected_scenarios_flags_steps_citing_changed_controls()
    test_heal_exact_same_scene_match()
    test_heal_label_match_other_scene_lower_confidence()
    test_heal_no_candidate_reports_unhealable()
    test_heal_skips_steps_with_valid_control_id()
    test_apply_heal_plan_mutates_scenarios_and_writes_audit()
    test_apply_heal_plan_rejects_suggestions_for_missing_live_control()
    print("\nAll P7 diff + heal smoke tests passed.")
