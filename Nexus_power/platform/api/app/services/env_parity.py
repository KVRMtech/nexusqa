"""Cross-environment PARITY — run the SAME flow against dev/test/uat/prod and report
where their grounded outputs DIVERGE (multi-env Phase 3, the differentiator).

The regulated-buyer fear is env drift causing prod surprises: "our UAT premium is
$75 but prod is $72." Verdict is the only tool that can report this HONESTLY, because
it has a grounded value oracle. This service re-derives verdicts through the SAME
frozen ``classify_failure`` reducer (never a new verdict path) and adds the
``environment`` dimension.

NEVER-GREEN-WASH (green-wash-proof by construction):
  * A **divergence** is reported ONLY when TWO environments each produced a
    PROVEN-grounded observed value (a value-oracle assertion that actually executed
    and read a real DOM value) and those values differ. Anything else —
    an ``// UNVERIFIED`` outcome, a value we couldn't capture, a run that didn't
    execute the oracle — is ``incomparable``, NEVER a divergence and NEVER a
    silent "match/no divergence."
  * HONEST GAP (v1): the reporter persists an observed value only on oracle FAILURE
    (embedded in ``error_message``); a PASSING value oracle records nothing. So a
    "$75 vs $72 while both PASS" is not computable until a structured observed-value
    channel lands (Phase 3 v2). v1 surfaces divergence from PROVEN failure evidence
    + differing verdict LABELS — and marks the rest ``incomparable``, not "match."
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: A value-oracle / invariant throw carries "... received <number>"; a Playwright
#: value-assertion mismatch carries 'Received string: <value>'. These are the ONLY
#: errors that embed a real, grounded OBSERVED value.
_RECEIVED_NUM_RE = re.compile(r"received\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RECEIVED_STR_RE = re.compile(r"received string:\s*\"?([^\"\n]+)", re.IGNORECASE)
_UNEXPECTED_VALUE = "unexpected value"


def observed_value_from_error(error_message: str) -> str:
    """The grounded OBSERVED value a value assertion actually read, or '' when the
    error is not a grounded value contradiction (→ not comparable, never a
    divergence).  Only ``__nxNum``/``__nxCmp`` throws ("unexpected value … received
    Y") and ``toHaveValue``/``toContainText`` mismatches ("Received string: Y")
    carry a real observed value."""
    if not error_message:
        return ""
    low = error_message.lower()
    m = _RECEIVED_NUM_RE.search(error_message)
    if m and _UNEXPECTED_VALUE in low:
        return m.group(1)
    m2 = _RECEIVED_STR_RE.search(error_message)
    if m2:
        return m2.group(1).strip()
    return ""


def _num(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def compute_parity(records: Iterable[Mapping[str, Any]], *, tolerance: float = 0.001) -> dict:
    """Pure parity over run records ``{test_id, scenario_id, environment, verdict,
    observed}`` (``observed`` = grounded value or '').  Groups by test/scenario and,
    for each group seen in ≥2 environments, reports:

      * ``diverged`` — two envs each have a PROVEN observed value and they differ;
      * ``verdict_diverged`` — no grounded values to compare, but the verdict LABELS
        differ across envs (e.g. dev PASS, prod real_regression);
      * ``incomparable`` — fewer than two grounded values AND labels agree (honest:
        we cannot assert parity, so we never claim "match").
    """
    groups: dict[tuple, list[dict]] = {}
    for r in records or ():
        if not isinstance(r, Mapping):
            continue
        env = str(r.get("environment") or "").strip()
        if not env:
            continue
        key = (str(r.get("test_id") or ""), str(r.get("scenario_id") or ""))
        groups.setdefault(key, []).append({
            "environment": env,
            "verdict": str(r.get("verdict") or ""),
            "observed": str(r.get("observed") or "").strip(),
        })

    tests: list[dict] = []
    diverged_n = 0
    for (test_id, scenario_id), rows in sorted(groups.items()):
        by_env: dict[str, dict] = {}
        for row in rows:  # latest wins if an env ran more than once
            by_env[row["environment"]] = row
        if len(by_env) < 2:
            continue  # not multi-env — nothing to compare
        grounded = {e: v["observed"] for e, v in by_env.items() if v["observed"]}
        labels = {e: v["verdict"] for e, v in by_env.items()}
        status = "incomparable"
        detail = ""
        if len(grounded) >= 2:
            vals = list(grounded.values())
            nums = [_num(x) for x in vals]
            if all(n is not None for n in nums):
                differ = max(nums) - min(nums) > tolerance
            else:
                differ = len(set(vals)) > 1
            if differ:
                status = "diverged"
                detail = "; ".join(f"{e}={grounded[e]}" for e in sorted(grounded))
            else:
                status = "match"
        elif len(set(labels.values())) > 1:
            status = "verdict_diverged"
            detail = "; ".join(f"{e}={labels[e]}" for e in sorted(labels))
        if status in ("diverged", "verdict_diverged"):
            diverged_n += 1
        tests.append({
            "test_id": test_id, "scenario_id": scenario_id, "status": status,
            "environments": sorted(by_env), "grounded_values": grounded,
            "verdicts": labels, "detail": detail,
        })

    return {
        "tests": tests,
        "environments": sorted({r["environment"] for rows in groups.values() for r in rows}),
        "diverged": diverged_n,
        "compared": len([t for t in tests if t["status"] != "incomparable"]),
        "incomparable": len([t for t in tests if t["status"] == "incomparable"]),
    }


async def compute_env_parity(db, *, artifact_id: str, tenant_id: str) -> dict:
    """DB-backed parity for an artifact's runs across environments.

    Reads test-run steps joined to their run (for the ``environment`` axis), extracts
    a grounded observed value from each PROVEN value-oracle failure, and feeds the
    pure :func:`compute_parity`.  Mirrors ``oracle_scorecard`` — same query shape, no
    new verdict logic, honest-by-construction.
    """
    from sqlalchemy import select
    from nexus_sdk.db.models import E2ETestRunRow, E2ETestRunStepRow
    from .test_runs import classify_failure

    rows = (await db.execute(
        select(E2ETestRunStepRow, E2ETestRunRow)
        .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
        .where(
            E2ETestRunStepRow.artifact_id == artifact_id,
            E2ETestRunStepRow.tenant_id == tenant_id,
        )
    )).all()

    records: list[dict] = []
    for step, run in rows:
        err = getattr(step, "error_message", "") or ""
        verdict = classify_failure(
            status=getattr(step, "status", ""),
            selector=getattr(step, "selector", "") or "",
            error_message=err,
        )
        env = (getattr(run, "environment", "") or "").strip()
        # a stronger per-profile axis when the run was tagged with one
        env_pid = str((getattr(run, "metadata_json", {}) or {}).get("env_profile_id") or "").strip()
        records.append({
            "test_id": getattr(step, "test_case_id", "") or "",
            "scenario_id": getattr(step, "scenario_id", "") or "",
            "environment": env_pid or env,
            "verdict": getattr(verdict, "label", "") or "",
            "observed": observed_value_from_error(err),
        })
    result = compute_parity(records)
    result["artifact_id"] = artifact_id
    return result


__all__ = ["compute_env_parity", "compute_parity", "observed_value_from_error"]
