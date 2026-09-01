"""Demo Diff + Self-Healing services (P7).

Pure-function services over the visual evidence graph:

  diff_visual_graphs(baseline, current) -> DiffReport
      Structural diff between two visual evidence graphs (scenes,
      controls, flow edges, app instances).  Used by the Test Studio's
      "Compare with previous version" view.

  build_heal_plan(scenarios, live_graph) -> HealPlan
      For each scenario step whose evidence_control_id no longer exists
      in the live graph, search for the best replacement by label_text
      + bounding-box proximity.  Returns per-step HealSuggestion items.

  apply_heal_plan_to_scenarios(scenarios, plan) -> tuple[list, AuditLog]
      In-place mutation of scenario steps to swap stale control_ids for
      the suggested replacements.  Records the swap in an AuditLog so
      callers can persist it to the scenario lifecycle audit trail.
"""
from .diff import (
    diff_visual_graphs,
    DiffReport,
    SceneDiff,
    ControlDiff,
    EdgeDiff,
)
from .healing import (
    build_heal_plan,
    apply_heal_plan_to_scenarios,
    HealPlan,
    HealSuggestion,
    HealApplyResult,
)

__all__ = [
    "diff_visual_graphs",
    "DiffReport",
    "SceneDiff",
    "ControlDiff",
    "EdgeDiff",
    "build_heal_plan",
    "apply_heal_plan_to_scenarios",
    "HealPlan",
    "HealSuggestion",
    "HealApplyResult",
]
