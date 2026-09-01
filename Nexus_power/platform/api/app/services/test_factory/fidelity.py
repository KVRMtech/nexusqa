"""Script ↔ Test Case fidelity — "is the generated Playwright correct for this
test case?" + an optional LLM semantic review.

Because the spec is COMPILED from the case, fidelity is mostly a deterministic
question, answered with zero LLM:
  * COVERAGE — does every step compile to a real action, or is it an UNPROVEN
    skip (provenance=inferred / confidence=review → the compiler emits test.skip)?
  * ASSERTIONS — how many real `expect(...)` oracles does the compiled spec carry
    (vs steps that just act and verify nothing)?
  * EXPECTED RESULTS — how many steps carry an Expected Result + the case-level one.
  * DRIFT — does the saved/edited version still match the current case's compile?

`compute_fidelity` is $0 + exact. `llm_faithfulness_review` adds a gpt-4o
semantic judgment ("does the script actually verify what the case says?") — a
READ-ONLY judge that flags gaps; it never mutates the case (that's the Phase-2
validator's job).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from pydantic import BaseModel

from ..llm import CompletionRequest, FinishReason, LLMRouter, ToolDefinition
from ..script_factory.compiler import compile_case, _confidence, _provenance

logger = logging.getLogger(__name__)

_LLM_TASK = os.environ.get("LLM_TASK_VALIDATE_CASE", "form_snapshot_extraction")


def _step_expected(step) -> str:
    return (getattr(step, "expected_result", "") or getattr(step, "expected", "") or "").strip()


def compute_fidelity(tc, field_meta: dict, *, active_source: str | None = None) -> dict:
    """Deterministic scorecard for one test case ($0 LLM). Compiles the case and
    measures coverage, assertions, expected-results, and drift vs a saved version."""
    field_meta = field_meta or {}
    steps = list(getattr(tc, "steps", None) or [])
    spec = compile_case(tc, field_meta, parametrize=True)

    per_step: list[dict] = []
    covered = 0
    for s in steps:
        unproven = _provenance(s) == "inferred" or _confidence(s) == "review"
        if not unproven:
            covered += 1
        per_step.append({
            "step_number": getattr(s, "step_number", None),
            "action": (getattr(s, "action", "") or "")[:120],
            "covered": (not unproven),
            "has_expected": bool(_step_expected(s)),
            "reason": "UNPROVEN (not directly observed)" if unproven else "",
        })

    total = len(steps)
    # Assertion QUALITY, not raw count: a tolerant `not.toHaveValue('')` (which
    # catches a no-op fill) is real but WEAKER than a grounded value/state/URL/
    # outcome oracle. Scoring on STRONG oracles stops a script of only not-empty
    # checks from reading as 100% verified. `not.toHaveValue('')` is the compiler's
    # only weak oracle, so the classification is exact (kept in sync with compiler).
    expect_lines = [ln for ln in spec.splitlines() if "await expect(" in ln]
    assertions = len(expect_lines)
    weak_assertions = sum(1 for ln in expect_lines if ".not.toHaveValue('')" in ln)
    strong_assertions = assertions - weak_assertions
    with_er = sum(1 for s in steps if _step_expected(s))
    case_expected = (getattr(tc, "expected_outcome", "") or "").strip()
    unproven = total - covered

    # Drift: a saved/edited version exists but no longer matches the current case.
    drift = False
    if active_source is not None:
        drift = active_source.strip() != spec.strip()

    coverage_pct = round(100.0 * covered / total) if total else 0
    # Measure STRONG oracles only — never the tolerant not-empty checks. When no
    # Expected Results are declared the fidelity is largely UNMEASURED, so credit
    # strong oracles against covered steps instead of awarding a free 100%.
    if with_er:
        assertion_pct = round(100.0 * min(strong_assertions, with_er) / with_er)
    elif covered:
        assertion_pct = round(100.0 * min(strong_assertions, covered) / covered)
    else:
        assertion_pct = 0
    # Overall: weight coverage + assertion fidelity equally.
    score = round((coverage_pct + assertion_pct) / 2)

    grade = "strong" if score >= 85 and unproven == 0 else ("partial" if score >= 50 else "weak")
    gaps: list[str] = []
    if unproven:
        gaps.append(f"{unproven} step(s) UNPROVEN — not directly observed, skipped in the script")
    if with_er and strong_assertions < with_er:
        gaps.append(f"{with_er - strong_assertions} expected result(s) without a strong (value/state/URL) assertion")
    if weak_assertions:
        gaps.append(f"{weak_assertions} tolerant not-empty check(s) — real, but weaker than a value/state oracle")
    if not with_er:
        gaps.append("no step-level Expected Results — assertion fidelity is largely unmeasured")
    if not case_expected:
        gaps.append("no case-level Expected Result")
    if drift:
        gaps.append("saved script is stale — the test case changed since it was last generated")

    return {
        "test_id": getattr(tc, "test_id", "") or "",
        "name": (getattr(tc, "name", "") or "")[:160],
        "steps": total,
        "covered": covered,
        "unproven": unproven,
        "assertions": assertions,
        "strong_assertions": strong_assertions,
        "weak_assertions": weak_assertions,
        "expected_results": with_er,
        "case_expected_outcome": bool(case_expected),
        "drift": drift,
        "coverage_pct": coverage_pct,
        "assertion_pct": assertion_pct,
        "score": score,
        "grade": grade,
        "gaps": gaps,
        "per_step": per_step,
    }


# ── LLM semantic faithfulness review (optional, gpt-4o, READ-ONLY judge) ───────


class FaithfulnessVerdict(BaseModel):
    faithful: bool = False
    coverage_assessment: str = ""
    gaps: list[str] = []
    confidence: float = 0.0


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_faithfulness",
        description=(
            "Judge whether the compiled Playwright script faithfully implements the "
            "test case AND verifies its Expected Results. List concrete gaps (a step "
            "not performed, an expected result not asserted, an assertion checking the "
            "wrong thing). Do NOT rewrite the script — only assess."
        ),
        input_schema=FaithfulnessVerdict.model_json_schema(),
    )


_SYSTEM = (
    "You are a senior SDET auditing whether a generated Playwright script faithfully "
    "implements a test case and VERIFIES its Expected Results. You are a read-only "
    "judge: assess coverage + assertion fidelity, list concrete gaps, and give a "
    "confidence. Do not rewrite anything. Respond only via record_faithfulness."
)


def _review_prompt(tc, spec: str, scorecard: dict) -> str:
    lines = [f"TEST CASE: {getattr(tc, 'name', '')!r}"]
    co = (getattr(tc, "expected_outcome", "") or "").strip()
    if co:
        lines.append(f"Case Expected Result: {co}")
    lines.append("Steps (action → expected result):")
    for s in (tc.steps or []):
        lines.append(f"  {getattr(s,'step_number','?')}. {(getattr(s,'action','') or '')[:90]}"
                     f"  →  {_step_expected(s)[:90]!r}")
    lines.append(f"\nDeterministic scorecard: {scorecard['covered']}/{scorecard['steps']} steps covered, "
                 f"{scorecard['assertions']} assertions for {scorecard['expected_results']} expected results, "
                 f"{scorecard['unproven']} UNPROVEN.")
    lines.append("\nCOMPILED PLAYWRIGHT:\n" + spec[:6000])
    return "\n".join(lines)


async def llm_faithfulness_review(tc, spec: str, scorecard: dict, *, router: LLMRouter) -> dict:
    """gpt-4o semantic judgment of script↔case faithfulness. Read-only; never raises."""
    tool = _tool()
    request = CompletionRequest(
        system=_SYSTEM, prompt=_review_prompt(tc, spec, scorecard),
        max_tokens=900, temperature=0.1, tools=(tool,), tool_choice=tool.name,
        enable_prompt_cache=True, metadata={"task": _LLM_TASK},
    )
    try:
        resp = await router.complete(task=_LLM_TASK, request=request)
    except Exception as exc:
        logger.warning("test_factory.fidelity.llm_error error=%s", str(exc)[:200])
        return {"reviewed": False, "error": str(exc)[:200]}
    verdict: FaithfulnessVerdict | None = None
    if resp.finish_reason != FinishReason.ERROR and resp.tool_calls:
        for call in resp.tool_calls:
            if call.name == tool.name:
                try:
                    verdict = FaithfulnessVerdict.model_validate(call.arguments); break
                except Exception:
                    pass
    if verdict is None and resp.text:
        try:
            verdict = FaithfulnessVerdict.model_validate(json.loads(resp.text))
        except Exception:
            pass
    if verdict is None:
        return {"reviewed": True, "model": f"{resp.provider}/{resp.model}", "note": "no parseable verdict"}
    return {
        "reviewed": True,
        "model": f"{resp.provider}/{resp.model}",
        "faithful": bool(verdict.faithful),
        "coverage_assessment": verdict.coverage_assessment[:600],
        "gaps": [g[:200] for g in (verdict.gaps or [])][:10],
        "confidence": round(float(verdict.confidence or 0.0), 2),
    }


__all__ = ["compute_fidelity", "llm_faithfulness_review"]
