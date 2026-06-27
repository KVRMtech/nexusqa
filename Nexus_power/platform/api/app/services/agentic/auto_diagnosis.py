"""Sentinel — Always-On Diagnosis.

Auto-runs the $0 deterministic diagnosis on every failed step (no click), routes it
through Triage, and (opt-in) enriches it with the Context cross-field reasoner. It REUSES
``self_heal.analyze_step`` — the exact same function the on-click path calls — so auto and
on-click can never diverge. It ATTACHES the result to the run timeline for rendering:
additive, read-only, fail-open. No schema change, no write, no migration.

Gated by the Governor: ``sentinel`` off => returns nothing and the timeline is unchanged.
"""
from __future__ import annotations

from . import governor
from . import triage as triage_mod
from . import semantic_diagnosis as context

# self_heal is the existing engine; we CALL it, we never modify it.
from ..diff_and_heal import self_heal

_FAIL = frozenset({"failed", "broken", "timed_out"})


def _unify(diag: dict, *, error_message: str, network=None, semantic=None) -> dict:
    """One diagnosis object: the deterministic diag + the Triage verdict + (optional) the
    Context enrichment. The deterministic cause + the oracle still LEAD; semantic only
    ENRICHES — it never changes ``auto_fixable`` and never flips a verdict to green."""
    out = dict(diag or {})
    t = triage_mod.triage(out, error_message=error_message, network=network)
    out["triage"] = t
    out["source"] = t["source"]
    out["route"] = t["route"]
    out["provenance"] = governor.stamp(
        out.get("recommended_action") or out.get("cause_label") or "",
        grounding=governor.G_DETERMINISTIC,
        facts=list(out.get("evidence") or []),
    )
    if semantic:
        out["semantic"] = semantic
        # surface the human-like line; the deterministic cause/auto_fixable stay unchanged.
        out["headline"] = (semantic.get("provenance") or {}).get("claim") or out.get("cause_label")
    else:
        out["headline"] = out.get("cause_label")
    return out


async def diagnose_failures(db, *, artifact_id: str, tenant_id: str,
                            scenario_ids: list[str] | None, timeline: dict,
                            prefs: dict | None = None, budget=None,
                            context_inputs=None, network_for=None) -> dict:
    """Diagnose the FIRST failure of each (selected) scenario. Returns
    ``{scenario_id: unified_diagnosis}``. ``sentinel`` off (Governor) or no failures => ``{}``.

    Optional hooks (provided by the wiring; both default to inert):
      * ``context_inputs(scenario_id, step_number) -> {nodes, sibling_fields,
        failing_label, failing_value}`` — when present AND ``context`` is enabled, the
        cross-field reasoner runs (budgeted, inert unless a live option set exists).
      * ``network_for(scenario_id, step_number) -> dict`` — per-step network signal for Triage.
    """
    if not governor.agent_enabled("sentinel", prefs):
        return {}
    try:
        failures = self_heal.first_failures(timeline, list(scenario_ids or
                    [sc.get("scenario_id") for sc in (timeline.get("scenarios") or [])]))
    except Exception:
        return {}
    ctx_on = governor.agent_enabled("context", prefs)
    out: dict = {}
    for f in failures:
        sid = f.get("scenario_id")
        sn = f.get("step_number")
        err = f.get("error_message", "") or ""
        try:
            diag = await self_heal.analyze_step(
                db, artifact_id=artifact_id, tenant_id=tenant_id,
                scenario_id=sid, step_number=sn)
        except Exception:
            continue
        if not diag or diag.get("found") is False:
            continue
        net = None
        if network_for is not None:
            try:
                net = network_for(sid, sn)
            except Exception:
                net = None
        semantic = None
        if ctx_on and context_inputs is not None:
            try:
                ci = context_inputs(sid, sn) or {}
                semantic = await context.analyze(
                    failing_label=ci.get("failing_label") or "",
                    failing_value=ci.get("failing_value") or "",
                    sibling_fields=ci.get("sibling_fields") or [],
                    nodes=ci.get("nodes") or [],
                    budget=budget)
            except Exception:
                semantic = None
        out[sid] = _unify(diag, error_message=err, network=net, semantic=semantic)
    return out


def attach_to_timeline(timeline: dict, diagnoses: dict) -> dict:
    """Additively attach each diagnosis to its scenario's FIRST failed step in the
    timeline (for rendering — no DB write). Returns the same ``timeline`` object so the
    caller can use it inline; fail-open (a missing diagnosis just isn't attached)."""
    if not diagnoses:
        return timeline
    for sc in (timeline.get("scenarios") or []):
        diag = diagnoses.get(sc.get("scenario_id"))
        if not diag:
            continue
        for st in (sc.get("steps") or []):
            if st.get("status") in _FAIL:
                st["diagnosis"] = diag
                break
    return timeline
