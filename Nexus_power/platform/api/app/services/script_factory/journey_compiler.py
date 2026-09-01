"""M2.4 — compile a DISCOVERED JOURNEY into a runnable Playwright specification.

THE PRIMITIVE THIS ADDS.  The factory has always compiled a ``ProductionTestCase``
— an object generated from a whole crawl artifact.  A journey could only be run
if one of those artifact-level cases happened to walk its pages in its order, at
which point ``journey_case_linker`` ADOPTED that case as the journey's runnable
form.  Adoption is a sound idea and a terrible sole entry point: a journey with
no spanning case is simply not runnable, and the honest message the API returns
for it ("no test case walks this journey's pages in order") describes a
limitation of the pipeline, not of the journey.

A walked journey already holds everything a specification needs.  This module
takes that evidence — assembled by ``qe-central``'s ``journey_spec`` — and
compiles it through the SAME ``compile_case`` every other spec goes through.  Not
a second compiler: a second FRONT DOOR to the one that exists, so a journey spec
and an artifact spec share every locator ladder, every honesty rule and every
audit the factory already enforces.

WHAT THIS MODULE ADDS ON TOP OF ``compile_case``, and why each is here:

  * **The lint actually runs (T-GEN-05).**  ``lint_spec`` is called on the
    compiled text and its result is RETURNED — findings, error count, rules
    version, and an explicit ``lint_status``.  The last of those exists because
    an empty finding list and a lint that never executed are indistinguishable
    from each other, and for months every delivered report claimed an API-policy
    audit on exactly that ambiguity.  ``executed`` is written by the code path
    that ran it.
  * **The audit runs.**  ``score_spec`` grades the compiled spec against its own
    steps, so a journey spec is held to the same HONEST-10 rubric.
  * **The outcome oracle is set from the evidence (T-GEN-04).**  A journey whose
    baseline a human approved on a COMPLETED walk compiles with hard outcome
    assertions; anything else keeps the non-failing default and says so.

Pure apart from the two auditor calls (both themselves pure): payload in,
result dict out.  No I/O, no clock, no database — the whole generation contract
is unit-testable, and the same payload always produces the same bytes.
"""
from __future__ import annotations

from typing import Any

from ..test_factory import playwright_auditor as _auditor
from . import compiler as _compiler
from .compiler import compile_case

#: Written on a result whose lint genuinely executed.  See the module docstring:
#: an empty ``lint`` list means "ran, found nothing" ONLY because this field
#: distinguishes it from "never ran".
LINT_STATUS_EXECUTED = "executed"

#: How many findings ride back on the result.  The full count is always
#: reported; the list is bounded so one pathological spec cannot dominate a
#: response.
MAX_LINT_FINDINGS = 40


class _Step:
    """One compiled step, in the attribute shape ``compile_case`` reads.

    A plain dict will not do: the compiler reaches for ``step.step_number``,
    ``step.action``, ``step.observed`` and ``step.confidence`` through
    ``getattr``, so a payload arriving as JSON has to be given attributes.  This
    class is that adapter and nothing more — it adds no defaults the payload did
    not state, because a default invented here would be a claim about the
    application that no crawl made.
    """

    __slots__ = ("step_number", "action", "expected_result", "observed",
                 "confidence", "provenance")

    def __init__(self, raw: Any, index: int) -> None:
        raw = raw if isinstance(raw, dict) else {}
        self.step_number = int(raw.get("step_number") or index)
        self.action = str(raw.get("action") or "")
        self.expected_result = str(raw.get("expected_result") or "")
        observed = dict(raw.get("observed") or {})
        # The network expectations travel INSIDE ``observed`` because that is
        # where the compiler's own reader looks, and because they are observed
        # evidence in exactly the sense every other key there is: the crawl saw
        # this step make these calls.
        if raw.get("network_expect"):
            observed["network_expect"] = list(raw["network_expect"])
        self.observed = observed
        self.confidence = str(raw.get("confidence") or "high")
        self.provenance = str(raw.get("provenance") or "observed")


class _JourneyCase:
    """The case object ``compile_case`` compiles, assembled from a journey."""

    __slots__ = ("test_id", "name", "description", "expected_outcome", "steps",
                 "value_assertions", "value_rules", "env_assertion")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.test_id = str(payload.get("test_id") or "")
        self.name = str(payload.get("name") or "Verify journey end to end")
        self.description = str(payload.get("description") or "")
        self.expected_outcome = str(payload.get("expected_outcome") or "")
        self.steps = [_Step(s, i) for i, s in
                      enumerate(payload.get("steps") or [], start=1)]
        self.value_assertions = list(payload.get("value_assertions") or [])
        self.value_rules = list(payload.get("value_rules") or [])
        self.env_assertion = payload.get("env_assertion")


def spec_path_for(payload: dict[str, Any]) -> str:
    """The path this journey's spec occupies in a generated project.

    Derived from the journey's own stable test id rather than from a sequence
    number, so a re-crawl (which mints a new artifact) does not renumber every
    file and turn an unchanged suite into a diff nobody can review.
    """
    test_id = str(payload.get("test_id") or "journey")
    return f"tests/{test_id}.spec.ts"


def compile_journey(
    payload: Any, *, field_meta: dict | None = None,
    parametrize: bool = False, network_timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Compile one journey payload into a spec, then LINT and AUDIT it.

    Returns::

        {compiled, reason, test_id, journey_id, spec_path, spec,
         lint, lint_status, lint_errors, lint_warnings, lint_rules_version,
         audit: {overall_score, decision, dimension_scores, findings, gaps},
         outcome_oracle, network_assertions, steps}

    ``compiled`` is False — with a stated ``reason`` and no spec — when the
    payload says the journey is not compilable, or carries no steps.  Neither
    case is an error: a journey the crawl never walked to completion HAS no
    runnable form, and inventing one would be the exact green-wash this pipeline
    exists to prevent.
    """
    if not isinstance(payload, dict):
        return {"compiled": False, "reason": "payload is not an object"}
    if payload.get("compilable") is False:
        return {"compiled": False,
                "journey_id": str(payload.get("journey_id") or ""),
                "reason": str(payload.get("reason") or
                              "journey reported not compilable")}
    steps = payload.get("steps") or []
    if not steps:
        return {"compiled": False,
                "journey_id": str(payload.get("journey_id") or ""),
                "reason": "journey payload carries no steps"}

    case = _JourneyCase(payload)
    # T-GEN-04 — a CONFIRMED criterion compiles hard.  ``None`` (anything not
    # confirmed) leaves the env default in place, so an unapproved baseline
    # cannot quietly acquire the authority to fail a build.
    hard = str(payload.get("outcome_oracle") or "").strip().lower() == "hard"
    spec = compile_case(
        case, field_meta or {}, parametrize=parametrize,
        soft_outcome=False if hard else None,
        network_timeout_ms=network_timeout_ms,
    )

    # T-GEN-05 — THE LINT RUNS HERE, unwrapped.  ``lint_spec`` is pure and
    # total; it cannot raise, so it is deliberately NOT inside a try/except.
    # The except-swallow around this call is what let four reports claim an
    # API-policy audit that had never executed, and re-adding one would restore
    # the defect exactly.
    lint = _auditor.lint_spec(spec)
    lint_errors = [f for f in lint if f.get("severity") == "error"]
    lint_warnings = [f for f in lint if f.get("severity") != "error"]

    try:
        audit = _auditor.score_spec(spec, list(case.steps))
    except Exception as exc:  # the audit must never break generation
        audit = {"overall_score": 0, "decision": "audit_error",
                 "findings": [f"auditor crashed: {str(exc)[:120]}"],
                 "gaps": [], "dimension_scores": {}}

    # COUNTED FROM WHAT WAS COMPILED, not from what was requested.  Reading the
    # raw payload here would report an assertion for every expectation the
    # compiler REFUSED — a half-specified endpoint, or a non-2xx it will not
    # demand of an application — and a count that includes assertions the spec
    # does not contain is the same class of claim this milestone exists to
    # remove.
    network_assertions = sum(
        len(_compiler._network_expectations(step)) for step in case.steps)

    return {
        "compiled": True,
        "reason": "",
        "journey_id": str(payload.get("journey_id") or ""),
        "test_id": case.test_id,
        "name": case.name,
        "spec_path": spec_path_for(payload),
        "spec": spec,
        # ── the lint, as a fact about an execution rather than a shape ──
        "lint": lint[:MAX_LINT_FINDINGS],
        "lint_status": LINT_STATUS_EXECUTED,
        "lint_errors": len(lint_errors),
        "lint_warnings": len(lint_warnings),
        "lint_rules_version": _auditor.LINT_RULES_VERSION,
        "audit": {
            "overall_score": audit.get("overall_score"),
            "decision": audit.get("decision"),
            "dimension_scores": audit.get("dimension_scores"),
            "findings": list(audit.get("findings") or [])[:12],
            "gaps": list(audit.get("gaps") or [])[:12],
        },
        "outcome_oracle": "hard" if hard else "soft",
        "outcome_oracle_reason": str(payload.get("outcome_oracle_reason") or ""),
        "network_assertions": network_assertions,
        "value_assertions": len(case.value_assertions),
        "ungrounded_outcomes": list(payload.get("ungrounded_outcomes") or []),
        "steps": len(case.steps),
    }


def compile_top_n(
    payloads: Any, *, field_meta: dict | None = None,
    parametrize: bool = False, network_timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Compile a RANKED list of journey payloads, in rank order.

    Returns ``{"results": [...], "compiled", "refused", "lint_errors_total",
    "specs": {path: text}}``.  Refusals are carried in the SAME list as the
    successes rather than filtered out: a Top-20 that silently returns eleven
    entries has told the operator nothing about the nine, and "why can this one
    not be generated" is the most actionable output this pipeline produces.
    """
    results: list[dict[str, Any]] = []
    specs: dict[str, str] = {}
    for payload in (payloads or ()):
        result = compile_journey(
            payload, field_meta=field_meta, parametrize=parametrize,
            network_timeout_ms=network_timeout_ms)
        if result.get("compiled") and result.get("spec"):
            specs[result["spec_path"]] = result["spec"]
        results.append({k: v for k, v in result.items() if k != "spec"})
    return {
        "results": results,
        "compiled": sum(1 for r in results if r.get("compiled")),
        "refused": sum(1 for r in results if not r.get("compiled")),
        "lint_errors_total": sum(int(r.get("lint_errors") or 0) for r in results),
        "lint_status": LINT_STATUS_EXECUTED,
        "lint_rules_version": _auditor.LINT_RULES_VERSION,
        "specs": specs,
    }


__all__ = [
    "LINT_STATUS_EXECUTED",
    "spec_path_for",
    "compile_journey",
    "compile_top_n",
]
