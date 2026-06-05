"""Negative / boundary / error-state generators (the other half of "critical").

Grounded, never invented:
* **Negative** — for each field the recording shows as REQUIRED, a test that
  omits it on the demonstrated flow and expects a required-field validation
  error.  Grounded: the field's required flag is captured.  The exact error
  text is inferred (marked as such) unless an error state was observed.
* **Boundary** — for each demonstrated date/number field, min/empty/invalid
  variants.  Grounded: the field + its value type are captured.
* **Error-state** — only when the recording actually shows an error/validation
  screen (OCR contains 'required' / 'invalid' / 'error' / 'must'); reproduces
  the path to it.  On a clean happy-path recording this yields nothing — by
  design, no fabrication.

Each clones the demonstrated base ``ProductionTestCase`` and adjusts the
relevant step(s); confidence is ``inferred`` (clearly NOT demonstrated).
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable, Mapping

from nexus_sdk.models import ProductionTestCase, ProductionTestStep

from .generator import PageVisitInput, _norm, _strip_required

_TEST_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

_DATE_RX = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d\d|\d{1,2}[/-]\d{1,2})\b",
    re.IGNORECASE,
)
_ERROR_RX = re.compile(
    r"\b(required|invalid|error|must|please\s+(enter|select|provide)|cannot\s+be\s+empty)\b",
    re.IGNORECASE,
)


def _required_fields(page_visits: Iterable[PageVisitInput]) -> list[str]:
    """Captured required-field labels (from signals + '(required)' markers)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(label: str) -> None:
        clean = _strip_required(label)[0].strip()
        key = _norm(clean)
        if clean and key and key not in seen:
            seen.add(key)
            out.append(clean)

    for v in page_visits:
        signals = getattr(v, "form_snapshot_signals", None) or {}
        for label, meta in signals.items():
            if isinstance(meta, Mapping) and meta.get("required"):
                _add(label)
        for label in (v.form_snapshot or {}):
            if _strip_required(label)[1]:
                _add(label)
    return out


def _base(test_cases) -> ProductionTestCase | None:
    return test_cases[0] if test_cases else None


def _renumber(steps: list[ProductionTestStep]) -> list[ProductionTestStep]:
    for i, s in enumerate(steps, start=1):
        s.step_number = i
    return steps


def _clone_steps(base: ProductionTestCase) -> list[ProductionTestStep]:
    return [s.model_copy(deep=True) for s in (base.steps or [])]


def generate_negative_cases(
    *, artifact_id: str, base_case: ProductionTestCase | None,
    page_visits: Iterable[PageVisitInput], host: str = "",
) -> list[ProductionTestCase]:
    if base_case is None:
        return []
    cases: list[ProductionTestCase] = []
    for fieldname in _required_fields(page_visits):
        steps = _clone_steps(base_case)
        # Drop any fill step for this field; the negative LEAVES it empty.
        steps = [
            s for s in steps
            if f"in the '{fieldname}'" not in (s.action or "")
        ]
        steps.append(ProductionTestStep(
            step_number=0,
            action=f"Leave the required field '{fieldname}' empty and submit",
            data_ref="",
            expected=f"A validation error is shown for '{fieldname}' (required field)",
            expected_result=f"A validation error is shown for '{fieldname}' (required field)",
            selector=f"label={fieldname}",
        ))
        _renumber(steps)
        name = f"Negative: missing required '{fieldname}' ({host})"[:500]
        cases.append(ProductionTestCase(
            test_id=str(uuid.uuid5(_TEST_ID_NAMESPACE, f"{artifact_id}:negative:{_norm(fieldname)}")),
            name=name,
            description=(
                f"Negative test — omits the required field '{fieldname}' on the "
                f"demonstrated flow and expects a required-field validation error. "
                f"Grounded: '{fieldname}' is a captured required field; the exact "
                f"error text is inferred."
            ),
            steps=steps,
            priority="P1_high",
            type="negative",
            tags=["negative", "inferred", "e2e", "pages_and_forms"],
        ))
    return cases


def _value_kind(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if _DATE_RX.search(v):
        return "date"
    if re.fullmatch(r"[$£€]?\d[\d,.]*\s*\w*", v) and any(c.isdigit() for c in v):
        return "number"
    return ""


def generate_boundary_cases(
    *, artifact_id: str, base_case: ProductionTestCase | None,
    page_visits: Iterable[PageVisitInput] = (), host: str = "",
) -> list[ProductionTestCase]:
    if base_case is None:
        return []
    cases: list[ProductionTestCase] = []
    # Find demonstrated date/number fields from the base fill steps.
    for s in (base_case.steps or []):
        action = s.action or ""
        m = re.search(r"in the '([^']+)' field", action)
        value = getattr(s, "data_ref", None) or ""
        kind = _value_kind(value)
        if not m or not kind:
            continue
        fieldname = m.group(1)
        if kind == "date":
            variants = [("a past date (2000-01-01)", "2000-01-01"), ("an empty date", "")]
        else:  # number
            variants = [("zero (0)", "0"), ("an out-of-range value (999999)", "999999")]
        for desc_v, val in variants:
            steps = _clone_steps(base_case)
            for st in steps:
                if f"in the '{fieldname}' field" in (st.action or ""):
                    st.action = f"Enter {desc_v} in the '{fieldname}' field"
                    st.data_ref = val
                    st.expected = f"'{fieldname}' is validated (accepted or rejected) for the boundary value"
                    st.expected_result = st.expected
            _renumber(steps)
            cases.append(ProductionTestCase(
                test_id=str(uuid.uuid5(
                    _TEST_ID_NAMESPACE, f"{artifact_id}:boundary:{_norm(fieldname)}:{_norm(val)}",
                )),
                name=f"Boundary: '{fieldname}' = {desc_v} ({host})"[:500],
                description=(
                    f"Boundary test — sets the demonstrated {kind} field "
                    f"'{fieldname}' to {desc_v}. Grounded: the field and its value "
                    f"type are captured; boundary behaviour is inferred."
                ),
                steps=steps,
                priority="P2_medium",
                type="boundary",
                tags=["boundary", "inferred", "e2e", "pages_and_forms"],
            ))
    return cases


def generate_error_state_cases(
    *, artifact_id: str, base_case: ProductionTestCase | None,
    page_visits: Iterable[PageVisitInput], host: str = "",
) -> list[ProductionTestCase]:
    """Only emits when the recording actually shows an error/validation screen."""
    if base_case is None:
        return []
    observed: list[str] = []
    seen: set[str] = set()
    for v in page_visits:
        text = " ".join(str(x) for x in (v.form_snapshot or {}).keys())
        text += " " + (v.location or "")
        for m in _ERROR_RX.finditer(text):
            ctx = text[max(0, m.start() - 30):m.end() + 30].strip()
            key = _norm(ctx)
            if key and key not in seen:
                seen.add(key)
                observed.append(ctx)
    cases: list[ProductionTestCase] = []
    for i, ctx in enumerate(observed[:10]):
        steps = _clone_steps(base_case)
        steps.append(ProductionTestStep(
            step_number=0,
            action="Reproduce the conditions that led to the observed error state",
            expected=f"The error/validation state is shown: '{ctx[:120]}'",
            expected_result=f"The error/validation state is shown: '{ctx[:120]}'",
        ))
        _renumber(steps)
        cases.append(ProductionTestCase(
            test_id=str(uuid.uuid5(_TEST_ID_NAMESPACE, f"{artifact_id}:error:{i}:{_norm(ctx)[:40]}")),
            name=f"Error-state: {ctx[:60]} ({host})"[:500],
            description=(
                f"Error-state test — the recording showed an error/validation cue "
                f"('{ctx[:100]}'); this reproduces the path to it. Grounded in the "
                f"observed text."
            ),
            steps=steps,
            priority="P1_high",
            type="error_state",
            tags=["error_state", "observed", "e2e", "pages_and_forms"],
        ))
    return cases


CATEGORY_GENERATORS = {
    "negative": generate_negative_cases,
    "boundary": generate_boundary_cases,
    "error_state": generate_error_state_cases,
}
