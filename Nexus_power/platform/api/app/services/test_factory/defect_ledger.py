"""Defect identity, dedup + lifecycle, and run-over-run diff (spec §2.6, §2.14).

Two cross-run derivations over the SAME rows, so they share one loader.

**Why identity matters.** Without a stable signature, the same defect seen in
five runs is reported as five defects — counts inflate and credibility
deflates. Here one signature = ONE defect with N occurrences, carrying
first-seen / last-seen and a lifecycle state (open → fixed_verified →
regressed).

**Derived, not stored.** The ledger is computed from the run history that
already exists, so there is no new table, no migration, and no risk of the
ledger disagreeing with the runs it summarises. It is therefore always exactly
as truthful as the underlying evidence.

ZERO LLM. Every field is a count, a timestamp, or a normalized quote of a
stored error string.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from nexus_sdk.db.models import E2ETestRunRow, E2ETestRunStepRow

from . import attribution_engine
from .evidence_report import (
    ST_DEFECT, ST_EXEC_ERROR, ST_NEEDS_REVIEW, ST_PASSED, derive_step_status,
)

logger = logging.getLogger(__name__)

# Lifecycle states
LC_OPEN = "open"                 # last execution of this step still failing
LC_FIXED = "fixed_verified"      # later execution of the same step PASSED
LC_REGRESSED = "regressed"       # passed, then failed again

#: Runs that represent an execution a human would reason about. Diagnosis runs
#: are internal capture probes (they deliberately fail) and would pollute both
#: the defect ledger and the diff with noise.
_EXCLUDED_ENVS = ("diagnosis",)

# Volatile fragments that must NOT change a defect's identity: timings, ids,
# ports, hashes, line/col markers. Two runs of the same defect differ in these.
_VOLATILE = (
    (re.compile(r"\b\d+(?:\.\d+)?\s*ms\b", re.I), "<ms>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*s\b(?!\w)", re.I), "<s>"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (re.compile(r":\d+:\d+\b"), ":<pos>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"https?://[^\s'\"]+"), "<url>"),
    (re.compile(r"\b\d+\b"), "<n>"),
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def error_fingerprint(error_message: str | None, *, max_chars: int = 220) -> str:
    """A stable, human-readable shape of a failure — volatile parts masked.

    Two occurrences of the SAME defect produce the same fingerprint even though
    their raw errors differ in timings, ids and URLs.
    """
    err = _ANSI.sub("", str(error_message or "")).strip()
    if not err:
        return ""
    # First meaningful line: that is the assertion/exception itself; the call
    # log below it is context, and it varies far more between runs.
    head = ""
    for line in err.splitlines():
        s = line.strip()
        if s:
            head = s
            break
    for rx, repl in _VOLATILE:
        head = rx.sub(repl, head)
    return re.sub(r"\s+", " ", head)[:max_chars].strip()


def defect_signature(*, scenario_id: str, step_number: int, cause: str,
                     fingerprint: str) -> str:
    """The stable identity of a defect: WHERE it happens (scenario+step) and
    WHAT shape it takes (attributed cause + normalized error)."""
    raw = f"{scenario_id}|{int(step_number or 0)}|{cause or ''}|{fingerprint or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _load_recent(session, *, artifact_id: str, tenant_id: str,
                       window_runs: int) -> tuple[list[Any], dict[str, list[Any]]]:
    """The last N reportable runs (newest first) and their steps by run_id."""
    runs = list((await session.execute(
        select(E2ETestRunRow)
        .where(E2ETestRunRow.artifact_id == artifact_id,
               E2ETestRunRow.tenant_id == tenant_id,
               E2ETestRunRow.environment.notin_(_EXCLUDED_ENVS))
        .order_by(E2ETestRunRow.started_at.desc())
        .limit(max(1, int(window_runs)))
    )).scalars().all())
    if not runs:
        return [], {}
    ids = [r.run_id for r in runs]
    steps = list((await session.execute(
        select(E2ETestRunStepRow)
        .where(E2ETestRunStepRow.run_id.in_(ids),
               E2ETestRunStepRow.tenant_id == tenant_id)
    )).scalars().all())
    by_run: dict[str, list[Any]] = {rid: [] for rid in ids}
    for s in steps:
        by_run.setdefault(str(getattr(s, "run_id", "")), []).append(s)
    return runs, by_run


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _classify(step: Any, *, is_cert: bool) -> tuple[str, dict | None]:
    err = getattr(step, "error_message", "") or ""
    attribution = None
    if str(getattr(step, "status", "")).lower() == "failed":
        try:
            attribution = attribution_engine.attribute_failure(
                err, step_def=None, is_certification=is_cert)
        except Exception:
            attribution = None
    status, _ = derive_step_status(getattr(step, "status", ""), attribution)
    return status, attribution


# ── §2.6 severity / priority / component / fix area ──────────────────────────
#
# DERIVED, never guessed. Every field below comes from signals we can point at:
# the attribution category, how often it recurred, whether it regressed, how
# many DIFFERENT cases share the same root cause (blast radius), and the
# business priority the case already carries. There is no model and no
# fabricated score — and every assessment ships the reasons that produced it,
# so a reviewer can disagree with the rule rather than with a black box.
#
# D3: all four are marked SUGGESTED until a human confirms them via the review
# endpoint. We never assert severity on the customer's behalf.

SEV_CRITICAL, SEV_HIGH, SEV_MEDIUM, SEV_LOW = "critical", "high", "medium", "low"
SEV_UNSET = "unset"          # honest: an unattributed failure has no severity yet

_P0 = ("p0", "critical", "blocker")
_P1 = ("p1", "high")


def assess_defect(defect: dict, *, case_priority: str = "", blast_radius: int = 1) -> dict:
    """Severity / priority / component / fix-area for one deduplicated defect.

    ``blast_radius`` = how many DISTINCT cases share this exact failure shape.
    A root cause that breaks nine journeys is more severe than the same shape
    breaking one, and that is a countable fact rather than a judgement call.
    """
    cls = str(defect.get("display_status") or "")
    lifecycle = str(defect.get("lifecycle") or "")
    occurrences = int(defect.get("occurrence_count") or 0)
    step = int(defect.get("step_number") or 0)
    prio = str(case_priority or "").strip().lower()
    reasons: list[str] = []

    # An unattributed failure gets NO severity — inventing one would be exactly
    # the fabricated precision the report exists to eliminate.
    if cls == ST_NEEDS_REVIEW:
        return {
            "severity": SEV_UNSET,
            "priority": SEV_UNSET,
            "suggested_component": _component_of(defect),
            "suggested_fix_area": "attribution — cause not yet proven",
            "assessment_reasons": [
                "No rung of the attribution ladder could prove a cause, so no "
                "severity is asserted. A human decides; the report does not guess."],
            "owner_note": _OWNER_NOTE,
            "suggested": True,
        }

    if cls == ST_DEFECT:                      # the APPLICATION is at fault
        sev = SEV_MEDIUM
        reasons.append("classified as an application defect by the attribution engine")
        if lifecycle == LC_REGRESSED:
            sev = SEV_CRITICAL
            reasons.append("REGRESSED — it had previously passed, so this is new breakage")
        elif blast_radius >= 3:
            sev = SEV_CRITICAL
            reasons.append(f"blast radius {blast_radius}: the same failure shape breaks "
                           f"{blast_radius} different test cases")
        elif any(p in prio for p in _P0):
            sev = SEV_HIGH
            reasons.append(f"the affected case is business-critical (priority {case_priority})")
        elif occurrences >= 3:
            sev = SEV_HIGH
            reasons.append(f"recurred in {occurrences} runs")
        elif any(p in prio for p in _P1):
            sev = SEV_HIGH
            reasons.append(f"the affected case is high priority ({case_priority})")
        if step <= 1 and sev in (SEV_MEDIUM, SEV_HIGH):
            reasons.append("fails at the entry step, so nothing downstream is exercised")
            sev = SEV_HIGH if sev == SEV_MEDIUM else sev
        fix_area = "application under test"
    else:                                      # OUR automation / environment
        # Severity here describes OUR fix urgency, and is never presented as a
        # defect in the customer's product.
        sev = SEV_MEDIUM
        reasons.append("execution error — our automation or environment, "
                       "NOT a defect in the application under test")
        if blast_radius >= 3:
            sev = SEV_HIGH
            reasons.append(f"blast radius {blast_radius}: it blocks "
                           f"{blast_radius} different cases from running")
        elif lifecycle == LC_REGRESSED:
            sev = SEV_HIGH
            reasons.append("REGRESSED — this step used to run cleanly")
        if step <= 1:
            reasons.append("fails at the entry step, so the whole case is blocked")
            sev = SEV_HIGH if sev == SEV_MEDIUM else sev
        fix_area = _fix_area_of(defect)

    # Priority follows severity, bumped once when the defect is actively getting
    # worse (regressed) and damped when it is already verified fixed.
    order = [SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL]
    idx = order.index(sev)
    if lifecycle == LC_FIXED:
        idx = max(0, idx - 1)
        reasons.append("already verified fixed in a later run — priority damped")
    prio_out = order[min(len(order) - 1, idx)]

    return {
        "severity": sev,
        "priority": prio_out,
        "suggested_component": _component_of(defect),
        "suggested_fix_area": fix_area,
        "assessment_reasons": reasons,
        "owner_note": _OWNER_NOTE,
        "suggested": True,
    }


#: We refuse to invent a person. An owner is only ever a mapping the customer
#: supplies; until then we name the grounded COMPONENT and stop there.
_OWNER_NOTE = ("No owner is suggested: assigning a person requires an ownership "
               "map this deployment has not been given. The component below is "
               "derived from where the failure actually occurred.")

_FIX_AREA_BY_CAUSE = {
    "ambiguous_locator": "test generation — locator binding",
    "action_locator_timeout": "test generation — locator binding / wait strategy",
    "target_unreachable": "environment — target reachability",
    "auth_wall": "environment — authentication/session",
    "url_as_text_oracle": "test generation — oracle construction",
}


def _fix_area_of(defect: dict) -> str:
    cause = str(defect.get("cause") or "")
    for key, area in _FIX_AREA_BY_CAUSE.items():
        if key in cause:
            return area
    cat = str(defect.get("category") or "")
    if cat == attribution_engine.CATEGORY_ENVIRONMENT:
        return "environment"
    if cat == attribution_engine.CATEGORY_CONFIG:
        return "run configuration"
    if cat == attribution_engine.CATEGORY_DATA:
        return "test data"
    return "test automation"


def _component_of(defect: dict) -> str:
    """The grounded location of the failure — the case and step it happened in.
    Not a guess about which team owns it."""
    name = str(defect.get("case_name") or defect.get("scenario_id") or "")
    step = defect.get("step_number")
    return f"{name} · step {step}" if name else f"step {step}"


async def build_defect_ledger(
    session, *, artifact_id: str, tenant_id: str, window_runs: int = 20,
    case_names: dict[str, str] | None = None,
    case_priorities: dict[str, str] | None = None,
) -> dict:
    """Deduplicated defects across the last ``window_runs`` runs, each with its
    occurrences and lifecycle state.

    A "defect" here is any non-passing outcome that needs an owner — an
    application defect, an execution error, or an unattributed failure. They
    stay CLASSIFIED (never merged into one flat "failure"), because an
    automation fault must never be reported as the customer's defect.
    """
    runs, by_run = await _load_recent(session, artifact_id=artifact_id,
                                      tenant_id=tenant_id, window_runs=window_runs)
    names = case_names or {}
    run_meta = {r.run_id: r for r in runs}
    # Oldest → newest so lifecycle transitions read forwards.
    ordered = sorted(runs, key=lambda r: (getattr(r, "started_at", None) or datetime.min))

    defects: dict[str, dict] = {}
    # (scenario, step) → chronological list of (run_id, at, display_status)
    step_history: dict[tuple[str, int], list[tuple[str, Any, str]]] = {}

    for run in ordered:
        is_cert = str(getattr(run, "environment", "")) == "certification"
        for step in by_run.get(run.run_id, []):
            sid = str(getattr(step, "scenario_id", "") or "")
            num = int(getattr(step, "step_number", 0) or 0)
            status, attribution = _classify(step, is_cert=is_cert)
            step_history.setdefault((sid, num), []).append(
                (run.run_id, getattr(run, "started_at", None), status))
            if status == ST_PASSED:
                continue
            if status not in (ST_DEFECT, ST_EXEC_ERROR, ST_NEEDS_REVIEW):
                continue        # blocked/skipped are consequences, not defects
            err = getattr(step, "error_message", "") or ""
            fp = error_fingerprint(err)
            cause = str((attribution or {}).get("cause") or "")
            sig = defect_signature(scenario_id=sid, step_number=num,
                                   cause=cause, fingerprint=fp)
            d = defects.get(sig)
            if d is None:
                d = defects[sig] = {
                    "signature": sig,
                    "scenario_id": sid,
                    "case_name": names.get(sid, ""),
                    "step_number": num,
                    "display_status": status,
                    "category": (attribution or {}).get("category"),
                    "tier": (attribution or {}).get("tier"),
                    "cause": cause or "unattributed",
                    "detail": (attribution or {}).get("detail") or "",
                    "evidence_quoted": (attribution or {}).get("evidence") or [],
                    "fingerprint": fp,
                    "first_seen": _iso(getattr(run, "started_at", None)),
                    "first_seen_run": run.run_id,
                    "last_seen": None,
                    "last_seen_run": "",
                    "occurrences": [],
                    "occurrence_count": 0,
                    "suggested": True,            # D3 — severity/owner unconfirmed
                }
            d["last_seen"] = _iso(getattr(run, "started_at", None))
            d["last_seen_run"] = run.run_id
            d["occurrence_count"] += 1
            if len(d["occurrences"]) < 50:
                d["occurrences"].append({
                    "run_id": run.run_id,
                    "at": _iso(getattr(run, "started_at", None)),
                    "environment": getattr(run, "environment", "") or "",
                    "error_excerpt": err[:300],
                })

    # ── lifecycle from the step's own later history ─────────────────────────
    for d in defects.values():
        hist = step_history.get((d["scenario_id"], d["step_number"]), [])
        after = [h for h in hist
                 if (h[1] or datetime.min) > _parse(d["last_seen"])] if d["last_seen"] else []
        passed_after = any(h[2] == ST_PASSED for h in after)
        if passed_after:
            d["lifecycle"] = LC_FIXED
        else:
            # regressed = it had PASSED at some point before this occurrence
            first = _parse(d["first_seen"])
            passed_before = any(h[2] == ST_PASSED and (h[1] or datetime.min) < first
                                for h in hist)
            d["lifecycle"] = LC_REGRESSED if passed_before else LC_OPEN

    # ── §2.6 assessment ─────────────────────────────────────────────────────
    # Blast radius first: how many DISTINCT cases share one failure shape. A
    # root cause that breaks nine journeys is more severe than the same shape
    # breaking one — and that is a count, not an opinion.
    radius: dict[str, set] = {}
    for d in defects.values():
        radius.setdefault(d["fingerprint"], set()).add(d["scenario_id"])
    prios = case_priorities or {}
    for d in defects.values():
        d["blast_radius"] = len(radius.get(d["fingerprint"], {d["scenario_id"]}))
        d.update(assess_defect(d, case_priority=prios.get(d["scenario_id"], ""),
                               blast_radius=d["blast_radius"]))

    items = sorted(defects.values(),
                   key=lambda d: (-d["occurrence_count"], d["scenario_id"], d["step_number"]))
    by_lifecycle = {LC_OPEN: 0, LC_FIXED: 0, LC_REGRESSED: 0}
    for d in items:
        by_lifecycle[d["lifecycle"]] = by_lifecycle.get(d["lifecycle"], 0) + 1
    by_class: dict[str, int] = {}
    for d in items:
        by_class[d["display_status"]] = by_class.get(d["display_status"], 0) + 1
    by_severity: dict[str, int] = {}
    for d in items:
        by_severity[d["severity"]] = by_severity.get(d["severity"], 0) + 1

    return {
        "window_runs": len(runs),
        "runs_considered": [
            {"run_id": r.run_id, "environment": getattr(r, "environment", ""),
             "started_at": _iso(getattr(r, "started_at", None))} for r in ordered
        ],
        "unique_defects": len(items),
        "total_occurrences": sum(d["occurrence_count"] for d in items),
        "by_lifecycle": by_lifecycle,
        "by_class": by_class,
        "by_severity": by_severity,
        "defects": items,
        "note": ("One signature = ONE defect with N occurrences. Signatures mask "
                 "volatile detail (timings, ids, URLs) so the same defect in two "
                 "runs is not double-counted. Automation faults stay classified as "
                 "Execution Errors and are never reported as application defects. "
                 "Severity and priority are DERIVED from countable signals "
                 "(attribution class, recurrence, regression, blast radius, the "
                 "case's own business priority) and every assessment carries the "
                 "reasons that produced it — they are SUGGESTED until a human "
                 "confirms them."),
    }


def _parse(iso: str | None) -> datetime:
    if not iso:
        return datetime.min
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return datetime.min


async def build_run_diff(
    session, *, artifact_id: str, tenant_id: str, current_run_id: str,
    previous_run_id: str | None = None, case_names: dict[str, str] | None = None,
) -> dict:
    """§2.14 — what CHANGED since the previous execution.

    Executives and auditors read the delta, not 900 steps. Coverage delta is
    part of the diff: a case that silently stopped being executed is a
    regression in coverage, and hiding it would be a green-wash of scope.
    """
    runs, by_run = await _load_recent(session, artifact_id=artifact_id,
                                      tenant_id=tenant_id, window_runs=40)
    names = case_names or {}
    ordered = sorted(runs, key=lambda r: (getattr(r, "started_at", None) or datetime.min),
                     reverse=True)
    cur = next((r for r in ordered if r.run_id == current_run_id), None)
    if cur is None:
        return {"available": False, "reason": "current run not found in the recent window"}
    if previous_run_id:
        prev = next((r for r in ordered if r.run_id == previous_run_id), None)
    else:
        cur_at = getattr(cur, "started_at", None) or datetime.min
        prev = next((r for r in ordered
                     if r.run_id != cur.run_id
                     and (getattr(r, "started_at", None) or datetime.min) < cur_at), None)
    if prev is None:
        return {"available": False, "reason": "no earlier run to compare against",
                "current_run_id": current_run_id}

    def case_status(run) -> dict[str, str]:
        is_cert = str(getattr(run, "environment", "")) == "certification"
        per: dict[str, list[str]] = {}
        for st in by_run.get(run.run_id, []):
            sid = str(getattr(st, "scenario_id", "") or "")
            status, _ = _classify(st, is_cert=is_cert)
            per.setdefault(sid, []).append(status)
        out = {}
        for sid, sts in per.items():
            if ST_DEFECT in sts:
                out[sid] = ST_DEFECT
            elif ST_EXEC_ERROR in sts:
                out[sid] = ST_EXEC_ERROR
            elif ST_NEEDS_REVIEW in sts:
                out[sid] = ST_NEEDS_REVIEW
            elif all(s == ST_PASSED for s in sts):
                out[sid] = ST_PASSED
            else:
                out[sid] = sts[0]
        return out

    now, before = case_status(cur), case_status(prev)
    bad = (ST_DEFECT, ST_EXEC_ERROR, ST_NEEDS_REVIEW)

    def item(sid, frm, to):
        return {"test_case_id": sid, "case_name": names.get(sid, ""),
                "from": frm, "to": to}

    newly_failing = [item(s, before.get(s), now[s]) for s in now
                     if now[s] in bad and before.get(s) == ST_PASSED]
    fixed = [item(s, before.get(s), now[s]) for s in now
             if now[s] == ST_PASSED and before.get(s) in bad]
    still_failing = [item(s, before.get(s), now[s]) for s in now
                     if now[s] in bad and before.get(s) in bad]
    newly_covered = [item(s, None, now[s]) for s in now if s not in before]
    lost_coverage = [item(s, before[s], None) for s in before if s not in now]

    return {
        "available": True,
        "current_run_id": cur.run_id,
        "current_started_at": _iso(getattr(cur, "started_at", None)),
        "previous_run_id": prev.run_id,
        "previous_started_at": _iso(getattr(prev, "started_at", None)),
        "newly_failing": newly_failing,
        "newly_failing_count": len(newly_failing),
        "fixed": fixed,
        "fixed_count": len(fixed),
        "still_failing": still_failing,
        "still_failing_count": len(still_failing),
        "coverage_gained": newly_covered,
        "coverage_gained_count": len(newly_covered),
        "coverage_lost": lost_coverage,
        "coverage_lost_count": len(lost_coverage),
        "note": ("Coverage lost means a case that ran previously did NOT run now. "
                 "It is reported as a delta, never absorbed silently into a pass rate."),
    }


__all__ = [
    "LC_OPEN", "LC_FIXED", "LC_REGRESSED",
    "error_fingerprint", "defect_signature",
    "build_defect_ledger", "build_run_diff",
]
