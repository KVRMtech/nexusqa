"""Recovery Orchestrator — the product's reflex arc on every failed run.

Founder decision (2026-07-25): FULL-AUTO for product-side repairs, with
certification as the safety gate. The loop a human (or the founder's chat
session) used to run by hand — see the failure → attribute the cause → apply
the right fix → re-prove — now fires automatically the moment a failed run is
ingested, so the diagnosis and (where safe) the repair are DONE before anyone
files a ticket.

Routing (deterministic, evidence-first — the Attribution Engine's verdicts
drive everything; no attribution → no invented action):

  product / recompile-class causes   → AUTO-RECERTIFY: these defects are fixed
    (url-as-text oracle, best-effort   at COMPILE time by the shipped guards,
    text oracle, ambiguous locator)    so a fresh certification both re-proves
                                       the suite and refreshes every legacy
                                       spec. Loop-guarded: never chained off a
                                       certification run (a cert→recert cycle
                                       could ping-pong forever).
  product / other + locator timeouts → HEAL CANDIDATE: a durable dossier in the
                                       R5 recovery store routes the case into
                                       the heal pipeline with full evidence.
  application                        → DEFECT DOSSIER: the failure is the
                                       product WORKING — never repaired, always
                                       packaged (evidence + step + run) for the
                                       client's defect flow.
  environment / configuration        → ACTIONABLE NOTICE: infra/config outages
                                       produce an operator dossier, never case
                                       blame, never silent loss.
  no attribution                     → NO ACTION (honest silence — the UI
                                       already renders "cause under analysis").

Every action is persisted through the existing R5 recovery store (UPSERT,
human-terminal states respected) so the whole story is visible in the Studio
proposals view, and logged at WARNING so the trail survives the deployed log
level. Every automatic repair path re-proves through certification before the
client can meet the case again — autonomy without green-wash.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# ── Actions ──────────────────────────────────────────────────────────────────
ACTION_RECERTIFY = "auto_recertify"
ACTION_HEAL_CANDIDATE = "heal_candidate"
ACTION_DEFECT_DOSSIER = "application_defect_dossier"
ACTION_ENV_NOTICE = "environment_notice"
ACTION_NONE = "no_action"

# Product-side causes that the SHIPPED compiler/generator guards repair at the
# next compile — recompilation IS the fix, certification is the proof.
RECOMPILE_CAUSES = frozenset({
    "url_as_text_oracle",
    "url_string_text_oracle",
    "best_effort_text_oracle",
    "ambiguous_locator",
})


def route_failure(attribution: dict | None, *, is_certification: bool) -> str:
    """The PURE routing rule from one failed step's attribution to one action.

    Honesty invariants:
      * an APPLICATION verdict is never repaired — it becomes the dossier;
      * no attribution → no action (never invent a fix for an unproven cause);
      * auto-recertify only chains off CLIENT runs (loop guard: a failing
        certification must not re-trigger itself; POST /certify is the
        operator's deliberate retry there).
    """
    if not attribution:
        return ACTION_NONE
    category = str(attribution.get("category") or "")
    cause = str(attribution.get("cause") or "")
    if category == "product_script_defect":
        if cause in RECOMPILE_CAUSES and not is_certification:
            return ACTION_RECERTIFY
        if cause in RECOMPILE_CAUSES:
            return ACTION_HEAL_CANDIDATE   # cert-run repeat → dossier, no loop
        return ACTION_HEAL_CANDIDATE
    if category == "application_defect":
        return ACTION_DEFECT_DOSSIER
    if category in ("environment", "configuration"):
        return ACTION_ENV_NOTICE
    if category == "unknown" and cause == "action_locator_timeout":
        return ACTION_HEAL_CANDIDATE
    return ACTION_NONE


@dataclass
class RecoveryPlan:
    """The orchestrator's decision for one ingested failed run."""
    actions: list[dict] = field(default_factory=list)   # per failed step
    proposals: list[dict] = field(default_factory=list)  # durable store rows
    recertify: bool = False

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a["action"]] = counts.get(a["action"], 0) + 1
        return {"actions": counts, "recertify": self.recertify,
                "proposals": len(self.proposals)}


_STRATEGY = {
    ACTION_RECERTIFY: "recompile-class product defect — guards fix at compile; "
                      "certification dispatched to re-prove the suite",
    ACTION_HEAL_CANDIDATE: "route to the heal pipeline: grounded rebind / "
                           "anchor-scope / regenerate, then re-certify",
    ACTION_DEFECT_DOSSIER: "file with the application's defect flow — the "
                           "grounded oracle break is the client's signal",
    ACTION_ENV_NOTICE: "fix the environment/configuration and re-run; no case "
                       "or application blame",
}


def plan_recovery(
    failed_steps: list[dict], *, is_certification: bool,
) -> RecoveryPlan:
    """Build the full recovery plan for one run (PURE — thoroughly unit-tested).

    ``failed_steps``: [{scenario_id, step_number, attribution, error_excerpt}].
    """
    plan = RecoveryPlan()
    for s in failed_steps:
        attribution = s.get("attribution")
        action = route_failure(attribution, is_certification=is_certification)
        entry = {
            "scenario_id": str(s.get("scenario_id") or ""),
            "step_number": int(s.get("step_number") or 0),
            "action": action,
            "category": str((attribution or {}).get("category") or ""),
            "cause": str((attribution or {}).get("cause") or ""),
        }
        plan.actions.append(entry)
        if action == ACTION_RECERTIFY:
            plan.recertify = True
        if action in (ACTION_HEAL_CANDIDATE, ACTION_DEFECT_DOSSIER,
                      ACTION_ENV_NOTICE):
            plan.proposals.append({
                "scenario_id": entry["scenario_id"],
                "step_number": entry["step_number"],
                "cause": entry["cause"] or entry["action"],
                "kind": action,
                "suggested_strategy": _STRATEGY[action],
                "category": entry["category"],
                "evidence": list((attribution or {}).get("evidence") or [])[:3],
                "detail": str((attribution or {}).get("detail") or "")[:500],
                "error_excerpt": str(s.get("error_excerpt") or "")[:400],
                "auto": True,   # the orchestrator authored this, not a human scan
            })
    return plan


async def run_recovery(
    *,
    artifact_id: str,
    tenant_id: str,
    run_id: str,
    is_certification: bool,
    session_scope: Callable[[], Any],
    spawn_certification: Callable[[], None] | None,
    spawn_auto_heal: Callable[[str, int, str], None] | None = None,
) -> dict:
    """The reflex arc: load the run's failed steps, plan, act, persist, log.

    ``session_scope``  — zero-arg factory returning an async session context
                         manager (the router injects its tenant-scoped one).
    ``spawn_certification`` — zero-arg dispatcher for a certification run
                         (already resilient: scaled timeout + retries). None
                         disables recertify (certification-run ingests pass
                         None as a second-layer loop guard).
    Never raises — a recovery failure must never break ingest.
    """
    try:
        from sqlalchemy import select

        from nexus_sdk.db.models import E2ETestRunStepRow
        try:
            from ..agentic import recovery_store
        except ImportError:  # file-path-loaded (unit tests) — absolute fallback
            from app.services.agentic import recovery_store

        async with session_scope() as session:
            rows = (await session.execute(
                select(
                    E2ETestRunStepRow.scenario_id,
                    E2ETestRunStepRow.step_number,
                    E2ETestRunStepRow.metadata_json,
                    E2ETestRunStepRow.error_message,
                )
                .where(
                    E2ETestRunStepRow.run_id == run_id,
                    E2ETestRunStepRow.tenant_id == tenant_id,
                    E2ETestRunStepRow.status.notin_(("passed", "skipped")),
                )
            )).all()
        failed_steps = [
            {
                "scenario_id": sid,
                "step_number": n,
                "attribution": (meta or {}).get("failure_attribution"),
                "error_excerpt": (err or "")[:400],
            }
            for sid, n, meta, err in rows
        ]
        if not failed_steps:
            return {"actions": {}, "recertify": False, "proposals": 0}

        plan = plan_recovery(failed_steps, is_certification=is_certification)

        if plan.proposals:
            async with session_scope() as session:
                await recovery_store.persist_scan(
                    session, tenant_id=tenant_id, artifact_id=artifact_id,
                    run_id=run_id, proposals=plan.proposals,
                )

        if plan.recertify and spawn_certification is not None:
            spawn_certification()

        # V2 (founder-approved FULL-AUTO): drive one unattended heal per failing
        # scenario — capture → grounded candidate → verify → activate on the
        # double proof → re-certify. Deduped per scenario (first failing step);
        # the driver's own guards enforce one attempt + never-touch-oracles.
        if spawn_auto_heal is not None:
            seen: set[str] = set()
            for a in plan.actions:
                if a["action"] != ACTION_HEAL_CANDIDATE:
                    continue
                sid = a["scenario_id"]
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                spawn_auto_heal(sid, a["step_number"], a["cause"] or a["action"])

        summary = plan.summary()
        logger.warning(
            "test_factory.recovery.completed artifact=%s run=%s cert_run=%s %s",
            artifact_id, run_id, is_certification, summary,
        )
        return summary
    except Exception:
        logger.exception(
            "test_factory.recovery.failed artifact=%s run=%s (ingest unaffected)",
            artifact_id, run_id,
        )
        return {"actions": {}, "recertify": False, "proposals": 0, "error": True}
