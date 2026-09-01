"""
Heart Engine — Output Validation & Guardrails Module.

Validates LLM outputs against configurable rules: JSON parsability,
required-field presence, confidence thresholds, hallucination markers,
and source-reference requirements.
"""

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ─── Models ────────────────────────────────────────────────────

class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationResult(BaseModel):
    """Result of running output guardrails."""

    valid: bool = True
    severity: ValidationSeverity = ValidationSeverity.INFO
    issues: list[dict] = Field(default_factory=list)
    score: float = Field(
        default=1.0,
        description="Overall quality score 0…1, where 1 = clean output",
    )
    error_indices: list[int] = Field(
        default_factory=list,
        description="Indices of items with ERROR-severity issues (used for filtering)",
    )

# ─── Output Validator ─────────────────────────────────────────

class OutputValidator:
    """
    Validates Heart LLM outputs against guardrail rules.

    Parameters
    ----------
    min_confidence : float
        Minimum acceptable confidence value (0.0 – 1.0).
    require_source : bool
        Whether BusinessRules must include a ``source`` reference.
    max_rules : int
        Maximum rules per extraction (overflow = warning).
    hallucination_markers : list[str] | None
        Phrases that indicate the LLM may be hallucinating.
    """

    # Known phrases that suggest fabricated output
    DEFAULT_HALLUCINATION_MARKERS = [
        "as an ai",
        "i cannot",
        "i don't have access",
        "i'm not sure",
        "hypothetically",
        "generally speaking",
        "it is possible that",
        "i would assume",
    ]

    def __init__(
        self,
        min_confidence: float = 0.7,
        require_source: bool = True,
        max_rules: int = 50,
        hallucination_markers: list[str] | None = None,
    ):
        self.min_confidence = min_confidence
        self.require_source = require_source
        self.max_rules = max_rules
        self.hallucination_markers = (
            hallucination_markers
            if hallucination_markers is not None
            else self.DEFAULT_HALLUCINATION_MARKERS
        )

    # ── Public API ─────────────────────────────────────────────

    def validate_json_output(self, raw_text: str) -> ValidationResult:
        """Check if raw LLM output is valid JSON."""
        issues: list[dict] = []

        try:
            json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            issues.append({
                "check": "json_parsable",
                "severity": ValidationSeverity.ERROR.value,
                "message": f"Output is not valid JSON: {exc}",
            })
            return ValidationResult(
                valid=False,
                severity=ValidationSeverity.ERROR,
                issues=issues,
                score=0.0,
            )

        return ValidationResult(valid=True, issues=[], score=1.0)

    def validate_rules_output(self, parsed: dict) -> ValidationResult:
        """
        Validate extracted-rules JSON against guardrail rules.

        Checks
        ------
        - ``rules`` key exists and is a list
        - Each rule has ``description`` / ``rule_text``
        - Confidence values are above threshold
        - Source references present (if required)
        - Rule count does not exceed max
        - No hallucination markers
        """
        issues: list[dict] = []
        score = 1.0

        rules = parsed.get("rules")
        if rules is None:
            issues.append({
                "check": "rules_key",
                "severity": ValidationSeverity.ERROR.value,
                "message": "Missing 'rules' key in output",
            })
            return ValidationResult(
                valid=False,
                severity=ValidationSeverity.ERROR,
                issues=issues,
                score=0.0,
            )

        if not isinstance(rules, list):
            issues.append({
                "check": "rules_type",
                "severity": ValidationSeverity.ERROR.value,
                "message": "'rules' is not a list",
            })
            return ValidationResult(
                valid=False,
                severity=ValidationSeverity.ERROR,
                issues=issues,
                score=0.0,
            )

        # Per-rule checks
        error_indices: list[int] = []
        for idx, rule in enumerate(rules):
            prefix = f"rules[{idx}]"
            desc = rule.get("description", rule.get("rule_text", ""))
            has_rule_error = False

            if not desc:
                issues.append({
                    "check": "rule_description",
                    "severity": ValidationSeverity.ERROR.value,
                    "message": f"{prefix}: missing description / rule_text",
                })
                score -= 0.1
                has_rule_error = True

            # Confidence check
            conf_str = str(rule.get("confidence", "medium")).lower()
            conf_map = {"high": 0.9, "medium": 0.6, "low": 0.3}
            conf_val = conf_map.get(conf_str, 0.5)
            if conf_val < self.min_confidence:
                issues.append({
                    "check": "confidence",
                    "severity": ValidationSeverity.WARNING.value,
                    "message": (
                        f"{prefix}: confidence '{conf_str}' "
                        f"({conf_val}) < threshold ({self.min_confidence})"
                    ),
                })
                score -= 0.05

            # Hallucination check
            if desc:
                hallucination_hits = self._check_hallucination(desc)
                if hallucination_hits:
                    issues.append({
                        "check": "hallucination",
                        "severity": ValidationSeverity.WARNING.value,
                        "message": (
                            f"{prefix}: potential hallucination markers: "
                            f"{hallucination_hits}"
                        ),
                    })
                    score -= 0.1

            if has_rule_error:
                error_indices.append(idx)

        # Count check
        if len(rules) > self.max_rules:
            issues.append({
                "check": "rule_count",
                "severity": ValidationSeverity.WARNING.value,
                "message": (
                    f"Extracted {len(rules)} rules exceeds max ({self.max_rules})"
                ),
            })
            score -= 0.05

        score = max(score, 0.0)
        has_error = any(
            i["severity"] == ValidationSeverity.ERROR.value for i in issues
        )
        worst = (
            ValidationSeverity.ERROR
            if has_error
            else (
                ValidationSeverity.WARNING
                if issues
                else ValidationSeverity.INFO
            )
        )

        return ValidationResult(
            valid=not has_error,
            severity=worst,
            issues=issues,
            score=round(score, 4),
            error_indices=error_indices,
        )

    def validate_tests_output(self, parsed: dict) -> ValidationResult:
        """
        Validate generated-test-cases JSON.

        Checks
        ------
        - ``test_cases`` key exists and is a list
        - Each test has ``name`` / ``title`` and ``steps``
        - Each step has ``action``
        """
        issues: list[dict] = []
        score = 1.0

        tests = parsed.get("test_cases")
        if tests is None or not isinstance(tests, list):
            issues.append({
                "check": "test_cases_key",
                "severity": ValidationSeverity.ERROR.value,
                "message": "Missing or invalid 'test_cases' key",
            })
            return ValidationResult(
                valid=False,
                severity=ValidationSeverity.ERROR,
                issues=issues,
                score=0.0,
            )

        for idx, tc in enumerate(tests):
            prefix = f"test_cases[{idx}]"
            name = tc.get("name", tc.get("title", ""))
            if not name:
                issues.append({
                    "check": "test_name",
                    "severity": ValidationSeverity.WARNING.value,
                    "message": f"{prefix}: missing name/title",
                })
                score -= 0.05

            steps = tc.get("steps", [])
            if not steps:
                issues.append({
                    "check": "test_steps",
                    "severity": ValidationSeverity.WARNING.value,
                    "message": f"{prefix}: no steps defined",
                })
                score -= 0.1

            for si, step in enumerate(steps):
                if not step.get("action"):
                    issues.append({
                        "check": "step_action",
                        "severity": ValidationSeverity.WARNING.value,
                        "message": f"{prefix}.steps[{si}]: missing action",
                    })
                    score -= 0.02

        score = max(score, 0.0)
        has_error = any(
            i["severity"] == ValidationSeverity.ERROR.value for i in issues
        )
        worst = (
            ValidationSeverity.ERROR
            if has_error
            else (
                ValidationSeverity.WARNING
                if issues
                else ValidationSeverity.INFO
            )
        )

        return ValidationResult(
            valid=not has_error,
            severity=worst,
            issues=issues,
            score=round(score, 4),
        )

    # ── Internal helpers ───────────────────────────────────────

    def _check_hallucination(self, text: str) -> list[str]:
        """Return any hallucination markers found in *text*."""
        lower = text.lower()
        return [m for m in self.hallucination_markers if m in lower]
