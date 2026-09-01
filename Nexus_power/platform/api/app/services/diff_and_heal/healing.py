"""Self-Healing Selectors (P7b).

When a scenario step's ``evidence_control_id`` no longer exists in the
live visual evidence graph, search for a replacement using:

  1. Exact label_text match (case-insensitive, trimmed) in any scene.
     Preferred when the scene_id also still exists.
  2. Label match in a different scene → "moved" suggestion (lower confidence).
  3. Bounding-box proximity to the original control's bbox (when both
     have bboxes) — used to disambiguate when multiple controls share
     a label.

Output is a per-step :class:`HealSuggestion` — never auto-applied; users
review and click Apply in the Test Studio.

The healing logic is pure-function; the apply step is also pure
(scenario list in, scenario list out + audit log).  Persistence is the
caller's responsibility (router writes back to e2e_architect_cache).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# ─── Data shapes ─────────────────────────────────────────────────────────


@dataclass
class HealSuggestion:
    """One swap proposal for one stale step."""
    scenario_id: str
    step_number: int
    old_control_id: str
    old_label: str
    old_scene_id: str
    new_control_id: str
    new_label: str
    new_scene_id: str
    new_selector: str
    match_kind: str  # "exact_in_same_scene" | "label_match_other_scene" | "bbox_nearest" | "no_candidate"
    match_confidence: float
    reason: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class HealPlan:
    """A complete heal plan for a set of scenarios."""
    scenarios_scanned: int
    suggestions: list[HealSuggestion] = field(default_factory=list)
    unhealable_steps: list[dict] = field(default_factory=list)  # {scenario_id, step_number, reason}

    def to_dict(self) -> dict:
        return {
            "scenarios_scanned": self.scenarios_scanned,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "unhealable_steps": list(self.unhealable_steps),
        }


@dataclass
class HealApplyResult:
    """Outcome of applying a heal plan to a scenario list."""
    applied_count: int
    rejected_count: int
    audit_entries: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "applied_count": self.applied_count,
            "rejected_count": self.rejected_count,
            "audit_entries": list(self.audit_entries),
        }


# ─── Helpers ────────────────────────────────────────────────────────────


def _norm_label(text: str | None) -> str:
    return (text or "").strip().lower()


def _bbox_center(bbox: Any) -> tuple[float, float] | None:
    if not isinstance(bbox, dict):
        return None
    try:
        cx = float(bbox.get("x", 0)) + float(bbox.get("width", 0)) / 2
        cy = float(bbox.get("y", 0)) + float(bbox.get("height", 0)) / 2
        return (cx, cy)
    except (TypeError, ValueError):
        return None


def _bbox_distance(a: Any, b: Any) -> float | None:
    ca = _bbox_center(a)
    cb = _bbox_center(b)
    if ca is None or cb is None:
        return None
    return ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5


def _build_control_indexes(live_graph: dict) -> tuple[
    dict[str, dict],            # control_id -> control
    dict[str, list[dict]],      # label_norm -> list of controls (all scenes)
    dict[str, str],             # control_id -> scene_id
]:
    by_id: dict[str, dict] = {}
    by_label: dict[str, list[dict]] = {}
    scene_of: dict[str, str] = {}
    controls_by_scene = live_graph.get("controls_by_scene") or {}
    for scene_id, controls in controls_by_scene.items():
        for c in controls:
            cid = c.get("control_id", "")
            if not cid:
                continue
            by_id[cid] = c
            scene_of[cid] = scene_id
            label_norm = _norm_label(c.get("label_text") or c.get("display_label") or c.get("element_type"))
            if label_norm:
                by_label.setdefault(label_norm, []).append(c)
    return by_id, by_label, scene_of


# ─── Plan builder ───────────────────────────────────────────────────────


def build_heal_plan(
    scenarios: Iterable[dict],
    live_graph: dict,
    *,
    baseline_controls_by_id: dict[str, dict] | None = None,
) -> HealPlan:
    """Produce a heal plan for every step whose evidence_control_id is
    no longer in the live graph.

    ``baseline_controls_by_id`` (optional) is a dict of the *baseline*
    controls (from when the scenarios were created) keyed by their
    control_id.  When provided, we can use the baseline control's label
    and bounding box to find a better replacement.  When absent, we fall
    back to the step's existing ``target_element`` label only.
    """
    live_by_id, live_by_label, scene_of = _build_control_indexes(live_graph)
    baseline_controls_by_id = baseline_controls_by_id or {}

    scenarios_list = list(scenarios)
    suggestions: list[HealSuggestion] = []
    unhealable: list[dict] = []

    for sc in scenarios_list:
        scenario_id = sc.get("scenario_id") or ""
        for step in sc.get("steps") or []:
            old_control_id = step.get("evidence_control_id") or ""
            if not old_control_id:
                # Step never had a control reference — nothing to heal
                continue
            if old_control_id in live_by_id:
                # Still valid — no heal needed
                continue

            old_scene_id = step.get("evidence_scene_id") or ""
            baseline_ctrl = baseline_controls_by_id.get(old_control_id)
            old_label = (
                (baseline_ctrl or {}).get("label_text")
                or step.get("target_element")
                or ""
            )
            old_bbox = (baseline_ctrl or {}).get("bounding_box")
            normalized_old_label = _norm_label(old_label)
            step_number = int(step.get("step_number") or 0)

            if not normalized_old_label:
                # No label to search by — can't heal
                unhealable.append({
                    "scenario_id": scenario_id,
                    "step_number": step_number,
                    "reason": "stale control_id and no recoverable label to search by",
                })
                continue

            candidates = live_by_label.get(normalized_old_label, [])
            if not candidates:
                unhealable.append({
                    "scenario_id": scenario_id,
                    "step_number": step_number,
                    "reason": f"no live control has label {old_label!r}",
                })
                continue

            # Prefer candidates in the same scene as the step's old scene_id
            same_scene_candidates = [
                c for c in candidates if scene_of.get(c["control_id"]) == old_scene_id
            ]

            if same_scene_candidates:
                # Multiple in the same scene: pick highest selector_confidence,
                # then nearest bbox if old_bbox is known
                ranked = sorted(
                    same_scene_candidates,
                    key=lambda c: (
                        -(c.get("selector_confidence") or 0),
                        _bbox_distance(c.get("bounding_box"), old_bbox) or 1e9,
                    ),
                )
                winner = ranked[0]
                match_kind = "exact_in_same_scene"
                # Confidence: high if only one candidate, slightly lower if
                # multiple (ambiguity)
                base_conf = 0.92 if len(ranked) == 1 else 0.78
            else:
                # Label match in a different scene — UI moved
                ranked = sorted(
                    candidates,
                    key=lambda c: (
                        -(c.get("selector_confidence") or 0),
                        _bbox_distance(c.get("bounding_box"), old_bbox) or 1e9,
                    ),
                )
                winner = ranked[0]
                match_kind = "label_match_other_scene"
                base_conf = 0.55  # cross-scene moves are riskier

            # Cap match_confidence by the candidate's own selector_confidence
            cand_sel_conf = winner.get("selector_confidence") or 0
            match_confidence = round(min(base_conf, cand_sel_conf if cand_sel_conf else base_conf), 2)

            new_scene_id = scene_of.get(winner["control_id"], "")
            reason_parts = [f"matched label {old_label!r}"]
            if match_kind == "exact_in_same_scene":
                reason_parts.append("in same scene as before")
            else:
                reason_parts.append("in a different scene (UI may have moved)")
            if old_bbox and winner.get("bounding_box"):
                d = _bbox_distance(winner.get("bounding_box"), old_bbox)
                if d is not None:
                    reason_parts.append(f"{d:.0f}px from original position")

            suggestions.append(HealSuggestion(
                scenario_id=scenario_id,
                step_number=step_number,
                old_control_id=old_control_id,
                old_label=old_label,
                old_scene_id=old_scene_id,
                new_control_id=winner["control_id"],
                new_label=winner.get("label_text") or "",
                new_scene_id=new_scene_id,
                new_selector=winner.get("playwright_selector") or "",
                match_kind=match_kind,
                match_confidence=match_confidence,
                reason="; ".join(reason_parts),
            ))

    return HealPlan(
        scenarios_scanned=len(scenarios_list),
        suggestions=suggestions,
        unhealable_steps=unhealable,
    )


# ─── Apply ───────────────────────────────────────────────────────────────


def apply_heal_plan_to_scenarios(
    scenarios: list[dict],
    suggestions: Iterable[dict | HealSuggestion],
    *,
    live_graph: dict | None = None,
) -> HealApplyResult:
    """Mutate ``scenarios`` in-place — swap stale evidence IDs for the
    suggested replacements.  Optionally re-validate suggestions against
    the current ``live_graph`` before applying (rejects swaps whose new
    control_id no longer exists in the graph).

    Returns counts + an audit-entry list suitable for the lifecycle
    audit log.
    """
    live_by_id: dict[str, dict] = {}
    if live_graph:
        live_by_id, _, _ = _build_control_indexes(live_graph)

    # Normalize suggestions to dict form
    sugg_dicts: list[dict] = []
    for s in suggestions:
        if isinstance(s, HealSuggestion):
            sugg_dicts.append(s.to_dict())
        elif isinstance(s, dict):
            sugg_dicts.append(s)

    # Index suggestions by (scenario_id, step_number) for O(1) lookup
    sug_by_key: dict[tuple[str, int], dict] = {}
    for s in sugg_dicts:
        sug_by_key[(s.get("scenario_id") or "", int(s.get("step_number") or 0))] = s

    applied = 0
    rejected = 0
    audit: list[dict] = []

    for sc in scenarios:
        scenario_id = sc.get("scenario_id") or ""
        for step in sc.get("steps") or []:
            key = (scenario_id, int(step.get("step_number") or 0))
            sug = sug_by_key.get(key)
            if not sug:
                continue
            new_id = sug.get("new_control_id") or ""
            if not new_id:
                rejected += 1
                audit.append({
                    "scenario_id": scenario_id,
                    "step_number": key[1],
                    "outcome": "rejected",
                    "reason": "suggestion has no new_control_id",
                })
                continue
            if live_by_id and new_id not in live_by_id:
                rejected += 1
                audit.append({
                    "scenario_id": scenario_id,
                    "step_number": key[1],
                    "outcome": "rejected",
                    "reason": f"suggested control {new_id!r} no longer in live graph",
                })
                continue

            old_id = step.get("evidence_control_id") or ""
            old_scene = step.get("evidence_scene_id") or ""
            new_scene = sug.get("new_scene_id") or old_scene
            new_label = sug.get("new_label") or ""

            step["evidence_control_id"] = new_id
            if new_scene and new_scene != old_scene:
                step["evidence_scene_id"] = new_scene
            if new_label:
                step["target_element"] = new_label

            applied += 1
            audit.append({
                "scenario_id": scenario_id,
                "step_number": key[1],
                "outcome": "applied",
                "old_control_id": old_id,
                "new_control_id": new_id,
                "old_scene_id": old_scene,
                "new_scene_id": new_scene,
                "match_kind": sug.get("match_kind"),
                "match_confidence": sug.get("match_confidence"),
            })

    return HealApplyResult(
        applied_count=applied,
        rejected_count=rejected,
        audit_entries=audit,
    )
