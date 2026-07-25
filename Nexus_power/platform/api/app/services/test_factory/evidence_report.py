"""Execution Evidence Report — the audit-grade *Certificate of Execution*.

Phase R1 of ``QECentral/docs/EXECUTION_EVIDENCE_REPORT_SPEC.md``: assemble one
report spanning Crawl → User Flow → Test Case → Step, with the spec's status
state machine applied deterministically and every claim traceable to a row.

DOCTRINE (binding, from spec §0):
  * **D1 no fabricated precision** — a numeric confidence is emitted ONLY when
    it comes from a measured source. Everywhere else we emit the honest
    evidence class (PROVEN / INFERRED / UNVERIFIED) the platform already
    computes.
  * **D2 no unverifiable sentence** — every field here is derived from a DB row
    or a deterministic rule. ZERO LLM calls in this module. Failure prose comes
    from the Attribution Engine, which quotes the evidence it matched.
  * **D3 AI suggests, humans assert** — anything machine-suggested carries
    ``suggested: True`` so the UI can mark it pending confirmation.
  * **D4 no lone green badge** — every rollup emits the FULL count triplet via
    :func:`count_triplet`; a caller cannot render a single "PASSED" from this
    data without deliberately discarding the other six counts.

Never-green-wash: a step that did not execute is NEVER counted as green;
``skipped`` after a failure is BLOCKED (a consequence), and an unattributable
failure is NEEDS_REVIEW (fail-closed toward human attention).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from nexus_sdk.db.models import (
    CanonicalArtifactRow,
    E2ETestRunRow,
    E2ETestRunStepRow,
    FactoryTestCaseRow,
)

from . import attribution_engine

logger = logging.getLogger(__name__)

REPORT_VERSION = "1.0"

# ── Display statuses (spec §1) ───────────────────────────────────────────────
ST_PASSED = "passed"
ST_DEFECT = "defect_found"
ST_EXEC_ERROR = "execution_error"
ST_BLOCKED = "blocked"
ST_NEEDS_REVIEW = "needs_review"
ST_SKIPPED = "skipped"
ST_CANCELLED = "cancelled"

# Case-level only
ST_COMPLETED_WITH_DEFECTS = "completed_with_defects"
ST_DEFECT_HALTED = "defect_found_halted"
ST_NOT_EXECUTED = "not_executed"

#: The seven step-level buckets every rollup must carry (D4).
TRIPLET_KEYS = (ST_PASSED, ST_DEFECT, ST_EXEC_ERROR, ST_BLOCKED,
                ST_NEEDS_REVIEW, ST_SKIPPED, ST_CANCELLED)

#: Case statuses that mean "this case ran and the APPLICATION was found wanting"
#: — a success of our product, styled distinctly from an Execution Error.
DEFECT_CASE_STATUSES = (ST_COMPLETED_WITH_DEFECTS, ST_DEFECT_HALTED)

# Evidence classes (D1) — never a fabricated percentage.
EV_PROVEN = "PROVEN"
EV_INFERRED = "INFERRED"
EV_UNVERIFIED = "UNVERIFIED"

_DB_PASSED = "passed"
_DB_FAILED = "failed"
_DB_SKIPPED = "skipped"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


# ── §1.1 step-level state machine ────────────────────────────────────────────

def derive_step_status(
    db_status: str,
    attribution: dict | None,
    *,
    after_failure: bool = False,
) -> tuple[str, str]:
    """Map (DB status × attribution class) → (display status, sub-badge).

    Deterministic; no LLM. ``after_failure`` marks a step that was skipped
    BECAUSE an earlier step in the same case failed — that is BLOCKED (a
    consequence of the failure), not a benign SKIPPED.

    A ``failed`` step can only ever surface as Defect Found / Execution Error /
    Needs Review. There is no soft-fail, and no path from ``failed`` to green.
    """
    s = (db_status or "").strip().lower()
    if s == _DB_PASSED:
        return ST_PASSED, ""
    if s == _DB_SKIPPED:
        return (ST_BLOCKED, "precondition_failed") if after_failure else (ST_SKIPPED, "")
    if s != _DB_FAILED:
        # Unknown/absent status never counts as green (fail-closed).
        return ST_NEEDS_REVIEW, "unknown_status"

    category = str((attribution or {}).get("category") or "").strip()
    if not category or category == attribution_engine.CATEGORY_UNKNOWN:
        # Honest silence from the engine ⇒ a human decides. Never implicit blame.
        # Distinguish "nothing matched at all" from "we recognised the failure
        # SHAPE but cannot PROVE whose fault it is" — the second is a much more
        # actionable review item, and conflating them reads as vagueness.
        if (attribution or {}).get("cause"):
            return ST_NEEDS_REVIEW, "cause_known_blame_unproven"
        return ST_NEEDS_REVIEW, "unattributed"
    if category == attribution_engine.CATEGORY_APPLICATION:
        return ST_DEFECT, "application"
    if category == attribution_engine.CATEGORY_ENVIRONMENT:
        return ST_EXEC_ERROR, "environment"
    if category == attribution_engine.CATEGORY_CONFIG:
        return ST_EXEC_ERROR, "configuration"
    if category == attribution_engine.CATEGORY_DATA:
        return ST_EXEC_ERROR, "test_data"
    if category == attribution_engine.CATEGORY_PRODUCT:
        # OUR generated script is at fault — an automation issue, and by
        # standing doctrine NEVER reported as a defect in the customer's app.
        return ST_EXEC_ERROR, "script"
    return ST_NEEDS_REVIEW, "unattributed"


# ── §1.2 case-level rollup ───────────────────────────────────────────────────

def derive_case_status(
    step_statuses: list[str],
    *,
    executed: bool = True,
    reached_final_step: bool = True,
) -> str:
    """Roll step statuses up to one case status, first match wins (spec §1.2).

    ``executed=False`` (the case never ran in this execution) yields
    NOT_EXECUTED — which the report surfaces under Coverage Honesty rather
    than silently omitting or, worse, counting as green.
    """
    if not executed:
        return ST_NOT_EXECUTED
    if not step_statuses:
        return ST_CANCELLED
    if ST_DEFECT in step_statuses:
        return ST_COMPLETED_WITH_DEFECTS if reached_final_step else ST_DEFECT_HALTED
    if ST_EXEC_ERROR in step_statuses:
        return ST_EXEC_ERROR
    if ST_NEEDS_REVIEW in step_statuses:
        return ST_NEEDS_REVIEW
    if ST_BLOCKED in step_statuses:
        return ST_BLOCKED
    if all(s == ST_SKIPPED for s in step_statuses):
        return ST_SKIPPED
    if all(s == ST_PASSED for s in step_statuses):
        return ST_PASSED
    # Mixed passed+skipped with no failure: honest partial, never green.
    return ST_SKIPPED if ST_SKIPPED in step_statuses else ST_PASSED


def count_triplet(statuses: list[str]) -> dict:
    """The FULL count breakdown (D4). Every key is always present — a caller
    cannot accidentally render a lone green badge from this dict."""
    out = {k: 0 for k in TRIPLET_KEYS}
    for s in statuses:
        if s in out:
            out[s] += 1
        elif s == ST_COMPLETED_WITH_DEFECTS or s == ST_DEFECT_HALTED:
            out[ST_DEFECT] += 1
        elif s == ST_NOT_EXECUTED:
            out.setdefault(ST_NOT_EXECUTED, 0)
            out[ST_NOT_EXECUTED] += 1
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def evidence_class(step_def: dict | None, db_status: str) -> str:
    """The honest evidence class for a step's expected-result (D1) — derived
    from the recorded provenance, never a made-up confidence number."""
    prov = str((step_def or {}).get("provenance") or "").strip().lower()
    if (db_status or "").lower() == _DB_FAILED:
        return EV_UNVERIFIED
    if prov in ("demonstrated", "recorded", "observed"):
        return EV_PROVEN
    if prov:
        return EV_INFERRED
    return EV_UNVERIFIED


# ── Flow derivation (generic — works on ANY app) ─────────────────────────────

_QUOTED_RX = re.compile(r"'([^']{1,60})'")


def derive_flow(case_name: str, first_step: dict | None) -> tuple[str, str]:
    """(flow_key, flow_label) for a case — grounded, app-agnostic.

    Primary signal: the first quoted token in the generated case name (our
    generator names every case "… the 'apply' flow" / "… from 'apply' to
    'claims'"), which is itself derived from the crawled page. Fallback: the
    entry URL path from the first step's recorded observation. Both come from
    the substrate, so this carries no domain vocabulary.
    """
    m = _QUOTED_RX.search(case_name or "")
    if m:
        token = m.group(1).strip()
        if token:
            return token.lower(), token
    url = str(((first_step or {}).get("observed") or {}).get("url") or "")
    path = ""
    if url:
        path = re.sub(r"^[a-z]+://[^/]+", "", url, flags=re.IGNORECASE).split("?")[0]
    seg = [p for p in (path or "").split("/") if p]
    if seg:
        return seg[-1].lower(), seg[-1]
    return "unassigned", "Unassigned"


# ── Assembler ────────────────────────────────────────────────────────────────

async def _load_run(session, *, artifact_id: str, tenant_id: str,
                    run_id: str | None) -> Any:
    q = select(E2ETestRunRow).where(
        E2ETestRunRow.artifact_id == artifact_id,
        E2ETestRunRow.tenant_id == tenant_id,
    )
    if run_id:
        q = q.where(E2ETestRunRow.run_id == run_id)
    else:
        # Newest execution that a human would call "the run" — diagnosis runs
        # are internal capture probes and are excluded.
        q = q.where(E2ETestRunRow.environment != "diagnosis")
    q = q.order_by(E2ETestRunRow.started_at.desc()).limit(1)
    return (await session.execute(q)).scalar_one_or_none()


async def _load_steps(session, *, run_id: str, tenant_id: str) -> list[Any]:
    rows = (await session.execute(
        select(E2ETestRunStepRow)
        .where(E2ETestRunStepRow.run_id == run_id,
               E2ETestRunStepRow.tenant_id == tenant_id)
        .order_by(E2ETestRunStepRow.scenario_id, E2ETestRunStepRow.step_number)
    )).scalars().all()
    return list(rows)


def _case_step_def(case_json: dict, step_number: int) -> dict:
    for s in (case_json.get("steps") or []):
        if int(s.get("step_number") or 0) == int(step_number or 0):
            return s
    return {}


def _build_case(
    *, case_row: Any, case_json: dict, step_rows: list[Any],
    is_certification: bool, include_steps: bool,
) -> dict:
    """One test case with its steps, statuses, evidence links and provenance."""
    steps_out: list[dict] = []
    statuses: list[str] = []
    seen_failure = False
    declared = int(getattr(case_row, "step_count", 0) or len(case_json.get("steps") or []))

    for row in sorted(step_rows, key=lambda r: int(getattr(r, "step_number", 0) or 0)):
        db_status = str(getattr(row, "status", "") or "")
        n = int(getattr(row, "step_number", 0) or 0)
        step_def = _case_step_def(case_json, n)
        err = getattr(row, "error_message", "") or ""
        attribution = None
        if db_status.lower() == _DB_FAILED:
            try:
                attribution = attribution_engine.attribute_failure(
                    err, step_def=step_def, is_certification=is_certification)
            except Exception as exc:      # never let attribution break a report
                logger.warning("evidence_report.attribution_failed step=%s err=%s",
                               n, str(exc)[:200])
                attribution = None
        status, badge = derive_step_status(db_status, attribution,
                                           after_failure=seen_failure)
        if status in (ST_DEFECT, ST_EXEC_ERROR, ST_NEEDS_REVIEW):
            seen_failure = True
        statuses.append(status)
        if not include_steps:
            continue
        steps_out.append({
            "step_number": n,
            "status": status,
            "status_badge": badge,
            "db_status": db_status,
            "action": step_def.get("action") or "",
            "target": (getattr(row, "expected_selector", "") or ""
                       or step_def.get("selector") or ""),
            "resolved_selector": getattr(row, "resolved_selector", "") or "",
            "expected": step_def.get("expected_result") or step_def.get("expected") or "",
            "actual": err if err else ("as expected" if status == ST_PASSED else ""),
            "duration_ms": int(getattr(row, "duration_ms", 0) or 0),
            "executed_at": _iso(getattr(row, "created_at", None)),
            "evidence_class": evidence_class(step_def, db_status),
            # Oracle provenance — WHICH recorded demonstration grounds this
            # expectation (§2.4). Links straight back into the substrate.
            "oracle_provenance": {
                "scene_id": getattr(row, "evidence_scene_id", "") or "",
                "control_id": getattr(row, "evidence_control_id", "") or "",
                "edge_id": getattr(row, "evidence_edge_id", "") or "",
                "recorded_provenance": step_def.get("provenance") or "",
                "confidence_reason": step_def.get("confidence_reason") or "",
            },
            "evidence": {
                "screenshot_url": getattr(row, "screenshot_url", "") or "",
                "baseline_screenshot": step_def.get("screenshot") or "",
                "step_run_id": getattr(row, "step_run_id", "") or "",
                # Tier T2 — one trace.zip replays this failure step-by-step
                # (DOM + network + console + screencast). Empty when the test
                # passed (traces are retained on failure only) or pre-migration.
                "trace_url": str((getattr(row, "metadata_json", None) or {}).get("trace_url") or ""),
            },
            # D2: prose ONLY on non-passing steps, and only what the engine
            # PROVED, with the evidence it matched quoted verbatim.
            "analysis": ({
                "category": attribution.get("category"),
                "tier": attribution.get("tier"),
                "cause": attribution.get("cause"),
                "detail": attribution.get("detail"),
                "evidence_quoted": attribution.get("evidence") or [],
                "engine": attribution.get("engine"),
                "suggested": True,          # D3 — until a human confirms
            } if attribution else (
                {"category": None, "cause": "unattributed",
                 "detail": ("No rung of the attribution ladder could PROVE a cause "
                            "from the recorded evidence. Routed to human review — "
                            "never attributed to the application by default."),
                 "evidence_quoted": [], "suggested": True}
                if status == ST_NEEDS_REVIEW else None)),
        })

    executed = bool(step_rows)
    reached_final = bool(statuses) and (declared == 0 or len(statuses) >= declared)
    case_status = derive_case_status(statuses, executed=executed,
                                     reached_final_step=reached_final)
    durations = [int(getattr(r, "duration_ms", 0) or 0) for r in step_rows]
    starts = [getattr(r, "created_at", None) for r in step_rows if getattr(r, "created_at", None)]
    tags = list(getattr(case_row, "tags", None) or [])
    return {
        "test_case_id": getattr(case_row, "test_case_id", ""),
        "name": getattr(case_row, "name", "") or "",
        "description": getattr(case_row, "description", "") or "",
        "test_type": getattr(case_row, "test_type", "") or "",
        "priority": getattr(case_row, "priority", "") or "",
        "risk_level": case_json.get("risk_level") or "",
        "business_requirement": case_json.get("expected_outcome") or "",
        "generated_by": f"nexus-generator/{getattr(case_row, 'generator_version', '') or 'v1'}",
        "tags": tags,
        "status": case_status,
        "executed": executed,
        "steps_declared": declared,
        "steps_executed": len(step_rows),
        "counts": count_triplet(statuses),
        "duration_ms": sum(durations),
        "started_at": _iso(min(starts)) if starts else None,
        "ended_at": _iso(max(starts)) if starts else None,
        # §2.3 Reproducibility — everything needed to re-run this exact result.
        "reproducibility": {
            "generator_version": getattr(case_row, "generator_version", "") or "",
            "case_updated_at": _iso(getattr(case_row, "updated_at", None)),
            "case_status": getattr(case_row, "status", "") or "",
            "evidence_grade": next((t.split(":", 1)[1] for t in tags
                                    if str(t).startswith("evidence-grade:")), ""),
            "inferred_steps": next((t.split(":", 1)[1] for t in tags
                                    if str(t).startswith("inferred-steps:")), ""),
        },
        "steps": steps_out,
    }


async def build_report(
    session, *, artifact_id: str, tenant_id: str, run_id: str | None = None,
    include_steps: bool = True, include_cross_run: bool = True,
) -> dict:
    """Assemble the full Execution Evidence Report. Read-only, ZERO LLM.

    Every number is a count of rows; every sentence is either deterministic or
    an Attribution Engine verdict that quotes its evidence (D2).
    """
    from ..test_runs import (product_quarantined_scenarios,
                             uncertified_exploratory_scenarios)

    artifact = (await session.execute(
        select(CanonicalArtifactRow).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()

    run = await _load_run(session, artifact_id=artifact_id, tenant_id=tenant_id,
                          run_id=run_id)
    # The ROW (not the rehydrated ProductionTestCase) — the report needs the
    # stored metadata (generator version, tags, updated_at) for reproducibility.
    cases = list((await session.execute(
        select(FactoryTestCaseRow)
        .where(FactoryTestCaseRow.artifact_id == artifact_id,
               FactoryTestCaseRow.tenant_id == tenant_id,
               FactoryTestCaseRow.status == "active")
        .order_by(FactoryTestCaseRow.priority.asc(),
                  FactoryTestCaseRow.created_at.asc())
    )).scalars().all())

    step_rows: list[Any] = []
    if run is not None:
        step_rows = await _load_steps(session, run_id=run.run_id, tenant_id=tenant_id)
    by_scenario: dict[str, list[Any]] = {}
    for r in step_rows:
        by_scenario.setdefault(str(getattr(r, "scenario_id", "") or ""), []).append(r)

    is_cert = bool(run is not None and str(getattr(run, "environment", "")) == "certification")

    # ── gates (Trust Block inputs) ──────────────────────────────────────────
    exploratory_ids = {
        str(getattr(c, "test_case_id", "") or "") for c in cases
        if str(getattr(c, "test_type", "") or "").lower() == "combination"
    }
    exploratory_ids.discard("")
    try:
        quarantined = await product_quarantined_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id)
    except Exception:
        quarantined = {}
    try:
        ungated = await uncertified_exploratory_scenarios(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            exploratory_ids=exploratory_ids)
    except Exception:
        ungated = {}

    # ── flows → cases → steps ───────────────────────────────────────────────
    flows: dict[str, dict] = {}
    all_case_statuses: list[str] = []
    all_step_statuses: list[str] = []
    not_executed: list[dict] = []

    for c in cases:
        case_json = dict(getattr(c, "test_case", None) or {})
        if not case_json:
            case_json = {"steps": [], "name": getattr(c, "name", "")}
        tcid = str(getattr(c, "test_case_id", "") or "")
        rows = by_scenario.get(tcid, [])
        built = _build_case(case_row=c, case_json=case_json, step_rows=rows,
                            is_certification=is_cert, include_steps=include_steps)
        # Gate transparency: a case excluded by a run-gate did not "fail" — say
        # exactly why it did not execute (never silently absent, never green).
        if not built["executed"]:
            reason = ""
            if tcid in quarantined:
                reason = "quarantined — its last certification failed for a " \
                         "product-side or unproven cause; excluded until it re-certifies"
            elif tcid in ungated:
                reason = str(ungated.get(tcid) or "not yet certified (exploratory gate)")
            built["not_executed_reason"] = reason or "not selected for this execution"
            not_executed.append({"test_case_id": tcid, "name": built["name"],
                                 "reason": built["not_executed_reason"]})

        steps_src = case_json.get("steps") or []
        fkey, flabel = derive_flow(built["name"], steps_src[0] if steps_src else None)
        flow = flows.setdefault(fkey, {"flow_key": fkey, "flow_label": flabel,
                                       "cases": [], "duration_ms": 0})
        flow["cases"].append(built)
        flow["duration_ms"] += built["duration_ms"]
        all_case_statuses.append(built["status"])
        all_step_statuses.extend([s["status"] for s in built["steps"]] if include_steps
                                 else [])

    for f in flows.values():
        cstats = [c["status"] for c in f["cases"]]
        f["counts"] = count_triplet(cstats)
        f["case_count"] = len(f["cases"])
        executed_cases = [c for c in f["cases"] if c["executed"]]
        green = [c for c in executed_cases if c["status"] == ST_PASSED]
        f["pass_percentage"] = round(100.0 * len(green) / len(executed_cases), 1) \
            if executed_cases else None
        f["defect_count"] = sum(1 for c in f["cases"] if c["status"] in DEFECT_CASE_STATUSES)

    # ── §2.1 summary (D4 triplets, never a lone badge) ──────────────────────
    executed_cases = [s for s in all_case_statuses if s != ST_NOT_EXECUTED]
    summary = {
        "artifact_id": artifact_id,
        "tenant_id": tenant_id,
        "session_id": getattr(artifact, "session_id", "") if artifact else "",
        "source_type": getattr(artifact, "source_type", "") if artifact else "",
        "crawl_completed_at": _iso(getattr(artifact, "completed_at", None)) if artifact else None,
        "total_flows": len(flows),
        "total_cases_generated": len(cases),
        "total_cases_executed": len(executed_cases),
        "total_steps_executed": len(step_rows),
        "case_counts": count_triplet(all_case_statuses),
        "step_counts": count_triplet(all_step_statuses) if include_steps else None,
    }

    run_block = None
    if run is not None:
        run_block = {
            "run_id": run.run_id,
            "environment": getattr(run, "environment", "") or "",
            "run_status_db": getattr(run, "status", "") or "",
            "started_at": _iso(getattr(run, "started_at", None)),
            "completed_at": _iso(getattr(run, "completed_at", None)),
            "duration_ms": int(getattr(run, "duration_ms", 0) or 0),
            "ingested_totals": {
                "total_steps": int(getattr(run, "total_steps", 0) or 0),
                "passed_steps": int(getattr(run, "passed_steps", 0) or 0),
                "failed_steps": int(getattr(run, "failed_steps", 0) or 0),
                "skipped_steps": int(getattr(run, "skipped_steps", 0) or 0),
            },
            "ci_run_id": getattr(run, "ci_run_id", "") or "",
            "ci_commit_sha": getattr(run, "ci_commit_sha", "") or "",
            "is_certification": is_cert,
        }

    trust = await build_trust_block(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
        quarantined=quarantined, ungated=ungated, cases=cases)

    # ── cross-run derivations (§2.6 defect identity, §2.14 diff) ───────────
    defects = None
    diff = None
    if include_cross_run:
        from .defect_ledger import build_defect_ledger, build_run_diff
        case_names = {str(getattr(c, "test_case_id", "")): (getattr(c, "name", "") or "")
                      for c in cases}
        try:
            defects = await build_defect_ledger(
                session, artifact_id=artifact_id, tenant_id=tenant_id,
                case_names=case_names)
        except Exception as exc:
            logger.warning("evidence_report.defect_ledger_failed err=%s", str(exc)[:200])
        if run is not None:
            try:
                diff = await build_run_diff(
                    session, artifact_id=artifact_id, tenant_id=tenant_id,
                    current_run_id=run.run_id, case_names=case_names)
            except Exception as exc:
                logger.warning("evidence_report.run_diff_failed err=%s", str(exc)[:200])

    coverage = {
        "cases_not_executed": not_executed,
        "cases_not_executed_count": len(not_executed),
        "quarantined_count": len(quarantined),
        "uncertified_exploratory_count": len(ungated),
        "note": ("Cases listed here did NOT execute in this run. They are "
                 "reported explicitly rather than omitted — an un-run case is "
                 "never counted as a pass."),
    }

    return {
        "report_version": REPORT_VERSION,
        "generated_at": _utc_now_iso(),
        "doctrine": {
            "no_fabricated_confidence": True,
            "evidence_classes": [EV_PROVEN, EV_INFERRED, EV_UNVERIFIED],
            "skipped_never_counted_green": True,
            "execution_errors_are_never_application_defects": True,
        },
        "run": run_block,
        "trust": trust,
        "summary": summary,
        "flows": sorted(flows.values(), key=lambda f: (-f["case_count"], f["flow_key"])),
        "defects": defects,
        "diff": diff,
        "coverage": coverage,
    }


async def build_trust_block(
    session, *, artifact_id: str, tenant_id: str,
    quarantined: dict, ungated: dict, cases: list,
) -> dict:
    """§2.0 — the report's opening section and our category differentiator:
    proof that this suite EARNED the right to judge the application."""
    from ..oracle_scorecard import compute_artifact_scorecard

    cert = (await session.execute(
        select(E2ETestRunRow).where(
            E2ETestRunRow.artifact_id == artifact_id,
            E2ETestRunRow.tenant_id == tenant_id,
            E2ETestRunRow.environment == "certification",
        ).order_by(E2ETestRunRow.started_at.desc()).limit(1)
    )).scalar_one_or_none()

    cert_block = None
    if cert is not None:
        cert_block = {
            "run_id": cert.run_id,
            "started_at": _iso(getattr(cert, "started_at", None)),
            "status": getattr(cert, "status", "") or "",
            "total_steps": int(getattr(cert, "total_steps", 0) or 0),
            "passed_steps": int(getattr(cert, "passed_steps", 0) or 0),
            "failed_steps": int(getattr(cert, "failed_steps", 0) or 0),
            "skipped_steps": int(getattr(cert, "skipped_steps", 0) or 0),
        }

    scorecard = None
    try:
        sc = await compute_artifact_scorecard(session, artifact_id=artifact_id,
                                              tenant_id=tenant_id)
        scorecard = {"grounding": sc.get("grounding"), "has_runs": sc.get("has_runs"),
                     "total_scenarios": sc.get("total_scenarios"),
                     "false_heal_rate": sc.get("false_heal_rate")}
    except Exception as exc:
        logger.debug("evidence_report.scorecard_skipped err=%s", str(exc)[:200])

    return {
        "statement": (
            "This suite was certified against the application's own baseline "
            "BEFORE it was allowed to judge the application. Cases that failed "
            "certification for a product-side or unproven cause are quarantined "
            "and excluded from client-facing runs until they re-certify."
        ),
        "certification_run": cert_block,
        "certified": bool(cert_block and cert_block["failed_steps"] == 0
                          and cert_block["total_steps"] > 0),
        "quarantined": [
            {"test_case_id": k, "reason": (v or {}).get("reason") or
             (v or {}).get("cause") or "failed certification"}
            for k, v in list(quarantined.items())[:200]
        ],
        "quarantined_count": len(quarantined),
        "uncertified_exploratory": [
            {"test_case_id": k, "reason": v} for k, v in list(ungated.items())[:200]
        ],
        "uncertified_exploratory_count": len(ungated),
        "oracle_scorecard": scorecard,
        "suite_size": len(cases),
    }


__all__ = [
    "REPORT_VERSION", "TRIPLET_KEYS", "DEFECT_CASE_STATUSES",
    "ST_PASSED", "ST_DEFECT", "ST_EXEC_ERROR", "ST_BLOCKED", "ST_NEEDS_REVIEW",
    "ST_SKIPPED", "ST_CANCELLED", "ST_COMPLETED_WITH_DEFECTS",
    "ST_DEFECT_HALTED", "ST_NOT_EXECUTED",
    "EV_PROVEN", "EV_INFERRED", "EV_UNVERIFIED",
    "derive_step_status", "derive_case_status", "count_triplet",
    "evidence_class", "derive_flow", "build_report", "build_trust_block",
]
