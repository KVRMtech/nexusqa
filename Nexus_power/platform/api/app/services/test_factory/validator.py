"""LLM validate/repair for a generated test case (Phase 2 — opt-in via Enrich).

Deterministic-first: the grounded generator already assembled the case. This
pass asks an LLM to DOUBLE-CHECK it against the recorded evidence and propose
corrections — but with hard, code-enforced grounding so it can never hallucinate:

  * It CANNOT add a step — corrections are keyed to an existing ``step_number``
    (closed schema; there is no "new step" field).
  * It CANNOT invent a value — a "membership gate" re-verifies every corrected
    value/label against the set of strings ACTUALLY OBSERVED in the recording
    (captured options + form snapshots + action labels). The model's own
    ``grounded`` flag is never trusted; the gate is the authority.
  * Re-phrased Expected Results are accepted only if every quoted literal they
    contain is itself observed.

Anything that fails a gate is DISCARDED — the deterministic value stands — and
logged to the case's ``source_evidence`` audit. Text-only (reads the enriched
page data, not frames) and routed to the working gpt-4o tier.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from pydantic import BaseModel

from nexus_sdk.models import ProductionTestCase
from ..llm import CompletionRequest, FinishReason, LLMRouter, ToolDefinition
from ..script_factory.compiler import _norm, build_field_meta

logger = logging.getLogger(__name__)

# Default to the form-snapshot route, which maps to the working gpt-4o tier —
# NOT the (possibly dead) default tier. Same pattern as the enrich extractors.
_LLM_TASK = os.environ.get("LLM_TASK_VALIDATE_CASE", "form_snapshot_extraction")
_VALIDATOR_VERSION = "v1"
_QUOTED_RX = re.compile(r"'([^']{2,})'|\"([^\"]{2,})\"")


class StepCorrection(BaseModel):
    step_number: int
    grounded: bool = False
    evidence_ref: str = ""
    corrected_label: Optional[str] = None
    corrected_value: Optional[str] = None
    expected_result: Optional[str] = None
    drop_as_noise: bool = False
    note: str = ""


class CaseValidation(BaseModel):
    corrections: list[StepCorrection] = []


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="record_case_corrections",
        description=(
            "Record corrections to a generated E2E test case. You may ONLY correct a "
            "value or label to text that appears in the supplied recorded evidence — "
            "never invent. Corrections are keyed to an existing step_number; you "
            "cannot add a step. Set drop_as_noise=true for steps that are clearly not "
            "app interactions (typing a URL into the address bar, switching browser "
            "tabs, the desktop). Re-phrase expected_result into one observable, "
            "verifiable sentence. Every correction MUST set evidence_ref to the signal "
            "it came from."
        ),
        input_schema=CaseValidation.model_json_schema(),
    )


_SYSTEM = (
    "You are a senior QA engineer reviewing an auto-generated functional E2E test "
    "case against the EVIDENCE captured from a screen recording. The extraction can "
    "be noisy: some step labels/values are garbled OCR, and some steps are browser "
    "chrome or desktop noise rather than real app actions. Your job is to validate "
    "and correct it WITHOUT inventing anything.\n\n"
    "STRICT RULES:\n"
    "  * You may ONLY change a value/label to text that appears in the EVIDENCE below.\n"
    "  * For every correction, set evidence_ref to the exact signal you used "
    "(e.g. 'form_snapshot:Travel Class', 'options:Economy').\n"
    "  * Set drop_as_noise=true for steps that are not real app interactions.\n"
    "  * Re-phrase expected_result as one specific, observable statement; quote only "
    "values that appear in the evidence.\n"
    "  * If a step is already correct, do not emit a correction for it.\n"
    "Respond ONLY by calling record_case_corrections."
)


def _observed_set(visits, actions, field_meta: dict) -> set[str]:
    """The set of normalized strings ACTUALLY observed — the membership authority."""
    s: set[str] = set()

    def add(x):
        n = _norm(x or "")
        if n:
            s.add(n)

    for a in actions:
        add(getattr(a, "target_label", ""))
        add(getattr(a, "value", ""))
    for v in visits:
        for label, value in (getattr(v, "form_snapshot", None) or {}).items():
            add(label)
            add(value)
    for key, meta in (field_meta or {}).items():
        add(key)
        for opt in (meta.get("options") or []):
            add(opt)
    return s


def _in_observed(text: str, observed: set[str]) -> bool:
    n = _norm(text or "")
    if not n:
        return False
    if n in observed:
        return True
    # Tolerant: accept a substring match either direction ("Economy" vs
    # "economy class") — but still anchored to genuinely-observed strings.
    return any(n in m or m in n for m in observed)


def _expected_ok(expected: str, observed: set[str]) -> bool:
    """A re-phrased Expected Result is accepted only if every quoted literal in it
    is observed (so it can't name a page/value that never occurred)."""
    lits = [a or b for a, b in _QUOTED_RX.findall(expected or "")]
    return all(_in_observed(lit, observed) for lit in lits)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl > 0:
            t = t[nl + 1:]
    if t.endswith("```"):
        t = t[:-3].rstrip()
    return t


def _build_prompt(tc: ProductionTestCase, visits) -> str:
    lines: list[str] = ["GENERATED TEST CASE STEPS:"]
    for st in (tc.steps or []):
        obs = getattr(st, "observed", None) or {}
        lines.append(
            f"  step {getattr(st, 'step_number', '?')}: verb={obs.get('verb', '')!r} "
            f"label={obs.get('label', '')!r} value={obs.get('value', '')!r} "
            f"action={(getattr(st, 'action', '') or '')[:80]!r} "
            f"expected={(getattr(st, 'expected_result', '') or '')[:80]!r}"
        )
    lines.append("\nEVIDENCE (what was actually observed — you may only correct to this):")
    for v in visits:
        loc = (getattr(v, "location", "") or "")[:80]
        fs = getattr(v, "form_snapshot", None) or {}
        sig = getattr(v, "form_snapshot_signals", None) or {}
        opts = []
        for label, fld in (sig.items() if isinstance(sig, dict) else []):
            if isinstance(fld, dict) and fld.get("options"):
                opts.append(f"{label}={fld.get('options')[:8]}")
        lines.append(f"  page {loc!r}: fields={dict(list(fs.items())[:12])} options={opts[:8]}")
    return "\n".join(lines)


async def validate_and_repair_case(
    tc: ProductionTestCase, *, visits, actions, router: LLMRouter,
) -> dict:
    """Validate + repair ONE case IN PLACE (mutates tc.steps). Returns an audit dict.
    Never raises — on any failure the deterministic case stands unchanged."""
    field_meta = build_field_meta(visits)
    observed = _observed_set(visits, actions, field_meta)
    by_num = {getattr(s, "step_number", i + 1): s for i, s in enumerate(tc.steps or [])}

    tool = _tool()
    request = CompletionRequest(
        system=_SYSTEM,
        prompt=_build_prompt(tc, visits),
        max_tokens=1500,
        temperature=0.1,
        tools=(tool,),
        tool_choice=tool.name,
        enable_prompt_cache=True,
        metadata={"task": _LLM_TASK},
    )
    try:
        resp = await router.complete(task=_LLM_TASK, request=request)
    except Exception as exc:  # never let the validator break generation
        logger.warning("test_factory.validator.llm_error error=%s", str(exc)[:200])
        return {"validated": False, "error": str(exc)[:200]}

    validation: CaseValidation | None = None
    if resp.finish_reason != FinishReason.ERROR and resp.tool_calls:
        for call in resp.tool_calls:
            if call.name == tool.name:
                try:
                    validation = CaseValidation.model_validate(call.arguments)
                    break
                except Exception:
                    pass
    if validation is None and resp.text:
        try:
            data = json.loads(_strip_fence(resp.text))
            if isinstance(data, dict):
                validation = CaseValidation.model_validate(data)
        except Exception:
            pass
    if validation is None:
        return {"validated": True, "model": f"{resp.provider}/{resp.model}",
                "corrections_applied": 0, "note": "no parseable corrections"}

    applied: list[dict] = []
    discarded: list[dict] = []
    for c in validation.corrections:
        st = by_num.get(c.step_number)
        if st is None:
            discarded.append({"step": c.step_number, "reason": "unknown step_number"})
            continue
        if not (c.evidence_ref or "").strip():
            discarded.append({"step": c.step_number, "reason": "no evidence_ref"})
            continue
        obs = dict(getattr(st, "observed", None) or {})
        changed: list[str] = []
        if c.drop_as_noise:
            # Never delete — flag as review so the compiler skips it (UNPROVEN).
            setattr(st, "confidence", "review")
            setattr(st, "provenance", "inferred")
            changed.append("drop_as_noise")
        if c.corrected_value is not None and _in_observed(c.corrected_value, observed):
            obs["value"] = c.corrected_value
            setattr(st, "observed", obs)
            st.data_ref = c.corrected_value
            changed.append("value")
        if c.corrected_label is not None and _in_observed(c.corrected_label, observed):
            obs["label"] = c.corrected_label
            setattr(st, "observed", obs)
            changed.append("label")
        if c.expected_result is not None and _expected_ok(c.expected_result, observed):
            st.expected_result = c.expected_result
            st.expected = c.expected_result
            changed.append("expected_result")
        if changed:
            applied.append({"step": c.step_number, "fields": changed,
                            "evidence_ref": (c.evidence_ref or "")[:80]})
        else:
            discarded.append({"step": c.step_number, "reason": "failed membership gate",
                              "corrected_value": c.corrected_value,
                              "corrected_label": c.corrected_label})

    logger.info(
        "test_factory.validator.run model=%s applied=%d discarded=%d",
        f"{resp.provider}/{resp.model}", len(applied), len(discarded),
    )
    return {
        "validated": True,
        "validator_version": _VALIDATOR_VERSION,
        "model": f"{resp.provider}/{resp.model}",
        "corrections_applied": len(applied),
        "corrections_discarded": len(discarded),
        "applied": applied,
        "discarded": discarded,
    }


__all__ = ["validate_and_repair_case", "CaseValidation", "StepCorrection"]
