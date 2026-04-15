"""
Quality Gate — Automated quality scoring for QA session outputs.

Evaluates completeness, consistency, and confidence of extracted
rules, generated tests, and overall session quality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class QualityLevel(str, Enum):
    """Quality assessment levels."""
    EXCELLENT = "excellent"   # ≥ 0.9
    GOOD = "good"             # ≥ 0.75
    ACCEPTABLE = "acceptable" # ≥ 0.6
    NEEDS_REVIEW = "needs_review"  # ≥ 0.4
    POOR = "poor"             # < 0.4


@dataclass
class QualityScore:
    """Quality score for a QA session."""
    overall: float = 0.0                  # 0.0-1.0
    level: QualityLevel = QualityLevel.POOR
    rule_completeness: float = 0.0         # Are rules thorough?
    test_coverage: float = 0.0             # Do tests cover all rule types?
    consistency: float = 0.0               # No contradictions?
    confidence: float = 0.0                # Average confidence across all items
    pii_safety: float = 1.0               # PII properly handled?
    dimensions: dict[str, float] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: bool = False

    @staticmethod
    def level_for(score: float) -> QualityLevel:
        if score >= 0.9:
            return QualityLevel.EXCELLENT
        if score >= 0.75:
            return QualityLevel.GOOD
        if score >= 0.6:
            return QualityLevel.ACCEPTABLE
        if score >= 0.4:
            return QualityLevel.NEEDS_REVIEW
        return QualityLevel.POOR


class QualityGate:
    """
    Automated quality gate for QA session outputs.

    Evaluates multiple dimensions and produces a composite score.
    Can be used as a pass/fail gate in orchestrator workflows.
    """

    def __init__(self, pass_threshold: float = 0.6):
        self.pass_threshold = pass_threshold

    def evaluate(
        self,
        rules: list[dict],
        test_cases: list[dict],
        engine_results: dict[str, Any] | None = None,
        confidence_scores: dict[str, float] | None = None,
        pii_result: dict | None = None,
    ) -> QualityScore:
        """
        Evaluate session quality across all dimensions.

        Returns a QualityScore with pass/fail determination.
        """
        engine_results = engine_results or {}
        confidence_scores = confidence_scores or {}

        # Score each dimension
        rule_completeness = self._score_rule_completeness(rules)
        test_coverage = self._score_test_coverage(rules, test_cases)
        consistency = self._score_consistency(rules)
        confidence = self._score_confidence(rules, test_cases, confidence_scores)
        pii_safety = self._score_pii_safety(pii_result)

        # Weighted average
        weights = {
            "rule_completeness": 0.25,
            "test_coverage": 0.25,
            "consistency": 0.20,
            "confidence": 0.20,
            "pii_safety": 0.10,
        }
        scores = {
            "rule_completeness": rule_completeness,
            "test_coverage": test_coverage,
            "consistency": consistency,
            "confidence": confidence,
            "pii_safety": pii_safety,
        }
        overall = sum(scores[k] * weights[k] for k in weights)

        # Collect gaps
        gaps = []
        warnings = []

        if rule_completeness < 0.5:
            gaps.append("Few business rules extracted — session may be incomplete")
        if test_coverage < 0.5:
            gaps.append("Low test coverage — missing test types (boundary/negative/edge)")
        if consistency < 0.7:
            warnings.append("Potential contradictions detected in extracted rules")
        if confidence < 0.6:
            warnings.append("Low average confidence — results may need SME review")
        if pii_safety < 0.9:
            warnings.append("PII handling incomplete — some data may not be fully redacted")

        level = QualityScore.level_for(overall)
        passed = overall >= self.pass_threshold

        result = QualityScore(
            overall=round(overall, 3),
            level=level,
            rule_completeness=round(rule_completeness, 3),
            test_coverage=round(test_coverage, 3),
            consistency=round(consistency, 3),
            confidence=round(confidence, 3),
            pii_safety=round(pii_safety, 3),
            dimensions=scores,
            gaps=gaps,
            warnings=warnings,
            passed=passed,
        )

        logger.info(
            "brain.quality_gate.evaluated",
            extra={
                "overall": result.overall,
                "level": level.value,
                "passed": passed,
                "rules": len(rules),
                "tests": len(test_cases),
            },
        )
        return result

    def _score_rule_completeness(self, rules: list[dict]) -> float:
        """Score based on rule count and field completeness."""
        if not rules:
            return 0.0

        # Minimum viable: at least 3 rules for a useful session
        count_score = min(len(rules) / 5.0, 1.0)

        # Check field completeness
        required_fields = {"description", "condition", "expected_result"}
        filled = 0
        total = 0
        for rule in rules:
            for f in required_fields:
                total += 1
                val = rule.get(f)
                if val and str(val).strip():
                    filled += 1

        field_score = filled / total if total else 0.0
        return (count_score * 0.4) + (field_score * 0.6)

    def _score_test_coverage(self, rules: list[dict], tests: list[dict]) -> float:
        """Score based on test type diversity and test-to-rule ratio."""
        if not tests:
            return 0.0

        # Test type diversity
        test_types = set()
        for tc in tests:
            tt = tc.get("test_type", tc.get("type", ""))
            if tt:
                test_types.add(tt.lower())

        desired_types = {"happy_path", "boundary", "negative", "edge_case"}
        type_coverage = len(test_types & desired_types) / len(desired_types) if desired_types else 0

        # Ratio: at least 2 tests per rule
        rule_count = max(len(rules), 1)
        ratio_score = min(len(tests) / (rule_count * 2), 1.0)

        return (type_coverage * 0.6) + (ratio_score * 0.4)

    def _score_consistency(self, rules: list[dict]) -> float:
        """Score based on rule consistency (no contradictions)."""
        if not rules:
            return 1.0  # No rules = no contradictions

        # Simple heuristic: check for contradictory keywords
        contradiction_pairs = [
            ("must", "must not"),
            ("required", "optional"),
            ("always", "never"),
            ("allow", "block"),
        ]

        contradiction_count = 0
        descriptions = [str(r.get("description", "")).lower() for r in rules]

        for i, desc_a in enumerate(descriptions):
            for desc_b in descriptions[i + 1:]:
                for pos, neg in contradiction_pairs:
                    if pos in desc_a and neg in desc_b:
                        contradiction_count += 1
                    elif neg in desc_a and pos in desc_b:
                        contradiction_count += 1

        max_comparisons = max(len(rules) * (len(rules) - 1) / 2, 1)
        return max(1.0 - (contradiction_count / max_comparisons), 0.0)

    def _score_confidence(
        self,
        rules: list[dict],
        tests: list[dict],
        explicit_scores: dict[str, float],
    ) -> float:
        """Score based on average confidence across all items."""
        scores: list[float] = []

        for rule in rules:
            conf = rule.get("confidence")
            if conf is not None:
                scores.append(float(conf))

        for tc in tests:
            conf = tc.get("confidence")
            if conf is not None:
                scores.append(float(conf))

        for v in explicit_scores.values():
            scores.append(float(v))

        if not scores:
            return 0.5  # Default when no confidence data available

        return sum(scores) / len(scores)

    def _score_pii_safety(self, pii_result: dict | None) -> float:
        """Score based on PII handling completeness."""
        if pii_result is None:
            return 0.5  # Unknown — neutral score

        entity_count = pii_result.get("entity_count", 0)
        if entity_count == 0:
            return 1.0  # No PII found = safe

        # Check if redaction was performed
        redacted = pii_result.get("redacted", False)
        mapping_id = pii_result.get("mapping_id", "")

        if redacted and mapping_id:
            return 1.0
        elif redacted:
            return 0.9
        else:
            return 0.3  # PII found but not redacted

    # ── Canonical Artifact Quality Gate ────────────────────────

    def evaluate_canonical(
        self,
        has_transcript: Any = None,
        has_visual: Any = None,
        scene_count: int = 0,
        duration_seconds: float = 0.0,
        pii_result: dict | None = None,
        safe_transcript: str | None = None,
        raw_transcript: str | None = None,
        scene_descriptions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate canonical artifact quality after media processing.

        Unlike evaluate() which assesses post-consumer outputs (rules, tests),
        this evaluates the raw canonical artifact from the processing chain:
        transcript extraction, visual analysis, PII redaction, and media probe.

        Returns a dict with scores and pass/fail for the canonical stage.
        """
        gaps: list[str] = []
        warnings: list[str] = []

        # ── 1. Transcript quality ──────────────────────────────
        transcript_quality = self._score_transcript_quality(
            has_transcript, safe_transcript, raw_transcript, duration_seconds, gaps, warnings,
        )

        # ── 2. Visual quality ──────────────────────────────────
        visual_quality = self._score_visual_quality(
            has_visual, scene_count, duration_seconds, gaps, warnings,
            scene_descriptions=scene_descriptions,
        )

        # ── 3. PII safety ─────────────────────────────────────
        pii_safety = self._score_pii_safety(pii_result)
        if pii_safety < 0.7:
            warnings.append("PII safety below threshold — review redaction completeness")

        # ── 4. Duration adequacy ───────────────────────────────
        duration_adequacy = self._score_duration_adequacy(
            duration_seconds, gaps, warnings,
        )

        # ── 5. Completeness ───────────────────────────────────
        # At least one modality (audio OR visual) must be present
        has_any_transcript = bool(has_transcript or safe_transcript or raw_transcript)
        has_any_visual = bool(has_visual) or scene_count > 0
        if has_any_transcript and has_any_visual:
            completeness = 1.0
        elif has_any_transcript or has_any_visual:
            completeness = 0.7
            warnings.append(
                "Only one modality available — "
                + ("audio only, no visual" if has_any_transcript else "visual only, no audio")
            )
        else:
            completeness = 0.0
            gaps.append("No transcript and no visual data — artifact is empty")

        # ── Weighted composite ────────────────────────────────
        weights = {
            "transcript_quality": 0.30,
            "visual_quality": 0.20,
            "pii_safety": 0.15,
            "duration_adequacy": 0.10,
            "completeness": 0.25,
        }
        scores = {
            "transcript_quality": transcript_quality,
            "visual_quality": visual_quality,
            "pii_safety": pii_safety,
            "duration_adequacy": duration_adequacy,
            "completeness": completeness,
        }
        overall = round(sum(scores[k] * weights[k] for k in weights), 3)
        level = QualityScore.level_for(overall)
        passed = overall >= self.pass_threshold and completeness > 0.0

        logger.info(
            "brain.canonical_quality_gate.scored",
            extra={
                "overall": overall,
                "level": level.value,
                "passed": passed,
                "transcript_q": round(transcript_quality, 3),
                "visual_q": round(visual_quality, 3),
                "pii": round(pii_safety, 3),
                "duration": round(duration_adequacy, 3),
                "completeness": round(completeness, 3),
            },
        )

        return {
            "overall_score": overall,
            "level": level.value,
            "passed": passed,
            "transcript_quality": round(transcript_quality, 3),
            "visual_quality": round(visual_quality, 3),
            "pii_safety": round(pii_safety, 3),
            "duration_adequacy": round(duration_adequacy, 3),
            "completeness": round(completeness, 3),
            "gaps": gaps,
            "warnings": warnings,
        }

    def _score_transcript_quality(
        self,
        has_transcript: Any,
        safe_transcript: str | None,
        raw_transcript: str | None,
        duration_seconds: float,
        gaps: list[str],
        warnings: list[str],
    ) -> float:
        """Score transcript quality with semantic depth analysis.

        Combines structural checks (length, duration ratio) with linguistic
        signals (vocabulary diversity, sentence structure, domain keywords)
        to distinguish real speech transcripts from noise/garbage output.
        """
        transcript_text = safe_transcript or raw_transcript or ""
        if isinstance(has_transcript, str) and has_transcript.strip():
            transcript_text = transcript_text or has_transcript

        if not transcript_text or not transcript_text.strip():
            gaps.append("No transcript extracted — audio may be silent or unrecognizable")
            return 0.0

        text = transcript_text.strip()
        text_len = len(text)

        # Minimum viable transcript: at least 50 characters
        if text_len < 50:
            gaps.append(f"Transcript too short ({text_len} chars) — likely incomplete extraction")
            return 0.15

        score = 1.0  # start at max, deduct for issues

        # ── Length-duration ratio ──────────────────────────────
        if duration_seconds > 0:
            expected_min = duration_seconds * 5   # very slow speech
            expected_max = duration_seconds * 40  # very fast speech
            if text_len < expected_min * 0.3:
                warnings.append(
                    f"Transcript length ({text_len}) unusually short for {duration_seconds:.0f}s media"
                )
                score = min(score, 0.4)
            elif text_len > expected_max * 2:
                warnings.append(
                    f"Transcript length ({text_len}) unusually long — possible duplication"
                )
                score = min(score, 0.6)

        # ── Repetition detection ──────────────────────────────
        if text_len > 200:
            chunk = text[:100]
            repeat_count = text.count(chunk)
            if repeat_count > 2:
                warnings.append("Repeated text detected in transcript — possible extraction error")
                score = min(score, 0.3)

        # ── Vocabulary diversity (type-token ratio) ───────────
        words = text.lower().split()
        if len(words) >= 10:
            unique_words = set(words)
            # Cap TTR sample to first 500 words to avoid length bias
            sample = words[:500]
            ttr = len(set(sample)) / len(sample)

            if ttr < 0.15:
                # Extremely repetitive — garbled or loop output
                warnings.append(
                    f"Very low vocabulary diversity (TTR={ttr:.2f}) — "
                    "transcript may be noise or repetition"
                )
                score = min(score, 0.25)
            elif ttr < 0.30:
                warnings.append(
                    f"Low vocabulary diversity (TTR={ttr:.2f}) — limited content variety"
                )
                score = min(score, 0.55)

        # ── Sentence structure heuristic ──────────────────────
        # Real speech has sentence-ending punctuation; pure noise rarely does
        punctuation_chars = {'.', '?', '!', ',', ';', ':'}
        punct_count = sum(1 for c in text if c in punctuation_chars)
        if text_len > 100 and punct_count == 0:
            warnings.append("No punctuation in transcript — may be garbled or noise output")
            score = min(score, 0.45)

        # ── Length tier ───────────────────────────────────────
        if text_len < 200:
            length_factor = max(0.5, text_len / 200.0)
            score = min(score, length_factor)

        return round(score, 3)

    def _score_visual_quality(
        self,
        has_visual: Any,
        scene_count: int,
        duration_seconds: float,
        gaps: list[str],
        warnings: list[str],
        scene_descriptions: list[str] | None = None,
    ) -> float:
        """Score visual analysis quality based on frame coverage and description depth.

        Checks both the quantity of scenes extracted and, when available,
        the semantic depth of scene descriptions from the Eyes engine.
        """
        if not has_visual and scene_count == 0:
            # No visual analysis — may be audio-only media, which is acceptable
            return 0.3  # neutral-low, not a hard fail

        if scene_count == 0 and has_visual:
            warnings.append("Visual data present but no scenes analyzed")
            return 0.4

        score = 1.0

        # ── Scene count vs duration ───────────────────────────
        if duration_seconds > 0 and scene_count > 0:
            expected_min_scenes = max(1, int(duration_seconds / 60))
            if scene_count < expected_min_scenes:
                warnings.append(
                    f"Low scene count ({scene_count}) for {duration_seconds:.0f}s media "
                    f"(expected ≥ {expected_min_scenes})"
                )
                score = min(score, max(0.4, scene_count / expected_min_scenes))

        # ── Scene count tier ──────────────────────────────────
        if scene_count < 2:
            score = min(score, 0.6)
        elif scene_count < 5:
            score = min(score, 0.8)

        # ── Description depth (when available) ────────────────
        if scene_descriptions:
            total_desc_len = sum(len(d) for d in scene_descriptions if d)
            non_empty = sum(1 for d in scene_descriptions if d and d.strip())

            if non_empty == 0:
                warnings.append("All scene descriptions are empty — Eyes may have failed")
                score = min(score, 0.35)
            elif total_desc_len / max(non_empty, 1) < 20:
                # Average description < 20 chars — very superficial analysis
                warnings.append("Scene descriptions are very brief — limited visual semantics")
                score = min(score, 0.55)

        return round(score, 3)

    def _score_duration_adequacy(
        self,
        duration_seconds: float,
        gaps: list[str],
        warnings: list[str],
    ) -> float:
        """Score media duration for reasonableness."""
        if duration_seconds <= 0:
            gaps.append("No media duration detected — probe may have failed")
            return 0.0

        # Too short: < 5 seconds unlikely to contain useful content
        if duration_seconds < 5:
            gaps.append(f"Media too short ({duration_seconds:.1f}s) — unlikely to contain audit content")
            return 0.1

        # Short but possible: 5-30 seconds
        if duration_seconds < 30:
            warnings.append(f"Short media ({duration_seconds:.1f}s) — limited content expected")
            return 0.5

        # Very long: > 4 hours — possible ingestion error
        if duration_seconds > 14400:
            warnings.append(
                f"Unusually long media ({duration_seconds / 3600:.1f}h) — verify this is a single session"
            )
            return 0.7

        # Normal range: 30s to 4h
        return 1.0
