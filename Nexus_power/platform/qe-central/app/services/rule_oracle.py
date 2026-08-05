"""O1 — Client-rule oracle (first-crawl validation).

The ONLY oracle that validates on the *first* crawl.  Client rate tables /
eligibility rules → machine-checkable expectations the fold evaluates
against traversal outcome_values immediately, with no SME approval needed.

A rule is a client-authored expectation grounded in their rate tables /
eligibility logic — NOT an inference, NOT an LLM fabrication.  The FROZEN
reducer law applies: a rule that cannot be grounded is NEVER fabricated
as passing — it stays UNVERIFIED.

Rule format in ``answer_key.rules``::

    {"kind": "outcome_rule",
     "field": "monthly_premium",        # which outcome label to check
     "expected": 28.40,                  # the expected value
     "match": "numeric",                 # numeric | exact | contains
     "tolerance": 0.50,                  # numeric only (optional)
     "source": "rate_table_v3"}          # audit trail

Pure + dependency-free (unit-testable with plain dicts).  Tolerant:
unrecognised rule kinds are skipped (future expansion), malformed rules
are skipped with a reason.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dc_field
from typing import Any

logger = logging.getLogger(__name__)

RULE_KIND = "outcome_rule"
SCENARIO_KIND = "scenario"

MATCH_NUMERIC = "numeric"
MATCH_EXACT = "exact"
MATCH_CONTAINS = "contains"
MATCH_RANGE = "range"
VALID_MATCH_TYPES = frozenset({MATCH_NUMERIC, MATCH_EXACT, MATCH_CONTAINS, MATCH_RANGE})

STATUS_CONFIRMED = "confirmed"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_MALFORMED = "malformed"


@dataclass(frozen=True)
class RuleSpec:
    field: str
    expected: Any = None
    match: str = MATCH_NUMERIC
    tolerance: float | None = None
    source: str = ""
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class RuleResult:
    rule: RuleSpec
    status: str
    observed_value: str | None = None
    reason: str = ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[,\s$%€£]", "", text)
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else None


def normalize_rules(raw_rules: Sequence[Mapping[str, Any]] | None) -> list[RuleSpec]:
    """Parse raw answer_key rules into validated RuleSpec objects.

    Only ``outcome_rule`` kind is recognised.  Malformed entries are
    skipped with a warning — never crash, never fabricate.
    """
    if not raw_rules:
        return []
    specs: list[RuleSpec] = []
    for rule in raw_rules:
        if not isinstance(rule, Mapping):
            continue
        kind = str(rule.get("kind") or rule.get("type") or "").strip().lower()
        if kind != RULE_KIND:
            continue

        field = str(rule.get("field") or rule.get("name") or rule.get("label") or "").strip()
        if not field:
            logger.debug("rule_oracle.skip_no_field rule=%s", rule)
            continue

        match_type = str(rule.get("match") or "").strip().lower()
        if not match_type:
            expected_raw = rule.get("expected", rule.get("equals", rule.get("value")))
            if _as_number(expected_raw) is not None:
                match_type = MATCH_NUMERIC
            else:
                match_type = MATCH_EXACT
        if match_type not in VALID_MATCH_TYPES:
            logger.debug("rule_oracle.skip_bad_match match=%s rule=%s", match_type, rule)
            continue

        expected = rule.get("expected", rule.get("equals", rule.get("value")))
        tolerance = None
        low = None
        high = None

        if match_type == MATCH_NUMERIC:
            num = _as_number(expected)
            if num is None:
                logger.debug("rule_oracle.skip_non_numeric expected=%s", expected)
                continue
            expected = num
            tol = _as_number(rule.get("tolerance"))
            tolerance = tol

        elif match_type == MATCH_RANGE:
            low_v = _as_number(rule.get("low", rule.get("min")))
            high_v = _as_number(rule.get("high", rule.get("max")))
            if low_v is None and high_v is None:
                logger.debug("rule_oracle.skip_bad_range rule=%s", rule)
                continue
            low = low_v
            high = high_v

        elif match_type in (MATCH_EXACT, MATCH_CONTAINS):
            expected = str(expected or "").strip()
            if not expected:
                logger.debug("rule_oracle.skip_empty_expected rule=%s", rule)
                continue

        source = str(rule.get("source") or rule.get("source_hint") or "").strip()
        specs.append(RuleSpec(
            field=field, expected=expected, match=match_type,
            tolerance=tolerance, source=source, low=low, high=high))

    return specs


def _normalize_label(label: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(label or "").strip().lower()).strip()


def _match_numeric(expected: float, observed_str: str, tolerance: float | None) -> bool:
    observed = _as_number(observed_str)
    if observed is None:
        return False
    if tolerance is not None and tolerance >= 0:
        return abs(observed - expected) <= tolerance
    return observed == expected


def _match_exact(expected: str, observed: str) -> bool:
    return expected.strip().lower() == observed.strip().lower()


def _match_contains(expected: str, observed: str) -> bool:
    return expected.strip().lower() in observed.strip().lower()


def _match_range(low: float | None, high: float | None, observed_str: str) -> bool:
    observed = _as_number(observed_str)
    if observed is None:
        return False
    if low is not None and observed < low:
        return False
    if high is not None and observed > high:
        return False
    return True


def evaluate_rule(
    rule: RuleSpec,
    outcome_values: Sequence[Mapping[str, Any]],
) -> RuleResult:
    """Evaluate one rule against a set of traversal outcome_values.

    Returns CONFIRMED if the rule's expected value matches the observed
    outcome, NOT_APPLICABLE if the outcome field is not present (the
    traversal may not have reached the page that shows this value), or
    UNCONFIRMED if the field is present but the value does not match.
    """
    rule_field_norm = _normalize_label(rule.field)
    for outcome in (outcome_values or []):
        if not isinstance(outcome, Mapping):
            continue
        label = str(outcome.get("label") or "").strip()
        if not label:
            continue
        if _normalize_label(label) != rule_field_norm:
            continue

        observed = str(outcome.get("value") or "")

        if rule.match == MATCH_NUMERIC:
            if _match_numeric(rule.expected, observed, rule.tolerance):
                return RuleResult(rule=rule, status=STATUS_CONFIRMED,
                                  observed_value=observed,
                                  reason="numeric match")
            return RuleResult(rule=rule, status=STATUS_UNCONFIRMED,
                              observed_value=observed,
                              reason=f"expected {rule.expected} "
                                     f"(±{rule.tolerance}), observed {observed}")

        elif rule.match == MATCH_EXACT:
            if _match_exact(rule.expected, observed):
                return RuleResult(rule=rule, status=STATUS_CONFIRMED,
                                  observed_value=observed,
                                  reason="exact match")
            return RuleResult(rule=rule, status=STATUS_UNCONFIRMED,
                              observed_value=observed,
                              reason=f"expected '{rule.expected}', "
                                     f"observed '{observed}'")

        elif rule.match == MATCH_CONTAINS:
            if _match_contains(rule.expected, observed):
                return RuleResult(rule=rule, status=STATUS_CONFIRMED,
                                  observed_value=observed,
                                  reason="contains match")
            return RuleResult(rule=rule, status=STATUS_UNCONFIRMED,
                              observed_value=observed,
                              reason=f"expected '{rule.expected}' in "
                                     f"'{observed}'")

        elif rule.match == MATCH_RANGE:
            if _match_range(rule.low, rule.high, observed):
                return RuleResult(rule=rule, status=STATUS_CONFIRMED,
                                  observed_value=observed,
                                  reason="range match")
            return RuleResult(rule=rule, status=STATUS_UNCONFIRMED,
                              observed_value=observed,
                              reason=f"expected [{rule.low}, {rule.high}], "
                                     f"observed {observed}")

    return RuleResult(rule=rule, status=STATUS_NOT_APPLICABLE,
                      reason="outcome field not present in traversal")


def evaluate_rules(
    rules: list[RuleSpec],
    outcome_values: Sequence[Mapping[str, Any]],
) -> list[RuleResult]:
    """Evaluate all rules against a set of traversal outcome_values."""
    return [evaluate_rule(r, outcome_values) for r in rules]


def summarize_evaluation(results: list[RuleResult]) -> dict[str, Any]:
    """Summarize a rule evaluation for logging and the API response."""
    confirmed = [r for r in results if r.status == STATUS_CONFIRMED]
    unconfirmed = [r for r in results if r.status == STATUS_UNCONFIRMED]
    not_applicable = [r for r in results if r.status == STATUS_NOT_APPLICABLE]
    applicable = confirmed + unconfirmed
    all_applicable_pass = bool(applicable) and not unconfirmed
    return {
        "total_rules": len(results),
        "confirmed": len(confirmed),
        "unconfirmed": len(unconfirmed),
        "not_applicable": len(not_applicable),
        "all_applicable_pass": all_applicable_pass,
        "details": [
            {"field": r.rule.field, "status": r.status,
             "observed": r.observed_value, "reason": r.reason,
             "source": r.rule.source}
            for r in results
        ],
    }


def normalize_scenarios(
    raw_rules: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, str]]:
    """Extract must-walk scenarios from answer_key rules.

    A scenario rule declares a specific combination of choices that MUST
    be walked::

        {"kind": "scenario",
         "choices": {"product_type": "Term Life", "tobacco": "No"},
         "source": "underwriting_matrix"}

    The ``choices`` map keys are control signatures or label-normalized
    control names; values are the option to force.  Returns a list of
    ``{control_key: option}`` dicts suitable for ``pairwise.must_walk``.
    """
    if not raw_rules:
        return []
    scenarios: list[dict[str, str]] = []
    for rule in raw_rules:
        if not isinstance(rule, Mapping):
            continue
        kind = str(rule.get("kind") or rule.get("type") or "").strip().lower()
        if kind != SCENARIO_KIND:
            continue
        choices = rule.get("choices") or rule.get("overrides") or {}
        if not isinstance(choices, Mapping):
            continue
        valid: dict[str, str] = {}
        for k, v in choices.items():
            key = str(k or "").strip()
            val = str(v or "").strip()
            if key and val:
                valid[key] = val
        if len(valid) >= 2:
            scenarios.append(valid)
    return scenarios
