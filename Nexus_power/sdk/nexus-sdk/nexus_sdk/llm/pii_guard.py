"""
Cloud-tier PII guard.

Before sending text to a cloud LLM provider (Tier 1 / Tier 2), this
module checks for high-confidence PII patterns. If the check fires:

  - `policy="block"`  → raise PIIGuardViolation; tier router falls
                        through to the next tier (or fails if no
                        on-prem tier is configured)
  - `policy="redact"` → return a redacted copy that the router sends
                        instead
  - `policy="warn"`   → log a warning, increment a metric, pass through

Tier 3 (on-prem) bypasses the guard — data never leaves the customer's
trust boundary, so the privacy concern doesn't apply.

Why a regex baseline (not Presidio):

  - Presidio is heavier (spaCy NER pipeline) and adds a startup cost
    every engine pays. Most engines only need to detect the highest-
    confidence patterns (email, phone, credit card, SSN, IBAN).
  - The interface is pluggable: customers running with strict privacy
    requirements can register a Presidio-backed detector via
    `register_pii_detector()` and the tier router will use it instead.
  - The regex set here is intentionally narrow — false positives are
    annoying, false negatives are dangerous. Customers who need higher
    recall should opt into Presidio.

Configuration (env, optional):
  NEXUS_LLM_PII_GUARD_POLICY  block | redact | warn | off  (default: block)
  NEXUS_LLM_PII_GUARD_TIERS   comma-separated tier values that should
                              be guarded; default "tier1,tier2"
                              (tier3 is never guarded — local data
                              never leaves the customer's premises)

This module is INDEPENDENT of the Shield engine. Shield handles long-
form workflow redaction (audio transcripts, video OCR). This guard is
a last-resort safety net on the LLM hot path so a misconfigured workflow
can't silently leak.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class PIIGuardViolation(Exception):
    """Raised when policy=block and a high-confidence PII pattern fires.

    The tier router catches this and falls through to the next tier
    (or fails the request if no further on-prem tier exists).
    """

    def __init__(self, pattern_name: str, masked_sample: str = "") -> None:
        super().__init__(
            f"PII pattern matched ({pattern_name}); refusing to send "
            f"prompt to cloud-tier LLM. Sample (masked): {masked_sample!r}"
        )
        self.pattern_name = pattern_name
        self.masked_sample = masked_sample


@dataclass
class PIIMatch:
    pattern_name: str
    start: int
    end: int
    masked: str    # the original text with the match replaced by ***


# ─── Detector interface ────────────────────────────────────────


class PIIDetector(Protocol):
    """Pluggable detector. Customers can swap regex for Presidio etc."""

    def scan(self, text: str) -> list[PIIMatch]:
        ...


# ─── Built-in regex detector ───────────────────────────────────


class RegexPIIDetector:
    """High-confidence patterns only. False-positive cost > false-negative."""

    _PATTERNS: dict[str, re.Pattern[str]] = {
        # RFC 5322 simplified — local-part@domain. Excludes weird forms.
        "email": re.compile(
            r"(?<![A-Za-z0-9._%+-])"
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
        # International phone: + then 7-15 digits, allowing common
        # separators. Avoid matching long numeric IDs without a +.
        "phone_e164": re.compile(
            r"(?<!\d)\+\d[\d\s\-().]{6,18}\d(?!\d)"
        ),
        # US SSN — 9 digits with optional dashes. Refuse 000-/666-/9xx-
        # which are reserved.
        "us_ssn": re.compile(
            r"(?<!\d)(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}(?!\d)"
        ),
        # Credit card: Luhn-validated 13-19 digits. Pre-filter by length
        # then call _is_luhn_valid below.
        "credit_card_candidate": re.compile(
            r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)"
        ),
        # IBAN — 2 letters, 2 digits, 11-30 alphanumerics.
        "iban": re.compile(
            r"(?<![A-Z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Z0-9])"
        ),
        # AWS access key id — distinctive prefix + 16-char body.
        "aws_access_key": re.compile(
            r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
        ),
        # GitHub personal access token (classic + fine-grained).
        "github_token": re.compile(
            r"(?<![A-Za-z0-9_])gh[ps]_[A-Za-z0-9]{36,}(?![A-Za-z0-9_])"
        ),
    }

    def scan(self, text: str) -> list[PIIMatch]:
        if not text:
            return []
        matches: list[PIIMatch] = []
        for name, pat in self._PATTERNS.items():
            for m in pat.finditer(text):
                if name == "credit_card_candidate":
                    digits = re.sub(r"\D", "", m.group(0))
                    if not _is_luhn_valid(digits):
                        continue
                    name_out = "credit_card"
                else:
                    name_out = name
                matches.append(PIIMatch(
                    pattern_name=name_out,
                    start=m.start(),
                    end=m.end(),
                    masked=_mask(text, m.start(), m.end()),
                ))
        return matches


def _is_luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum — rejects sequences that pass length but
    aren't real card numbers. Cuts the regex's false-positive rate."""
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask(text: str, start: int, end: int) -> str:
    """Replace [start:end) with '***'. Used in log lines so we don't
    log the original PII even when explaining the match."""
    return text[:start] + "***" + text[end:]


# ─── Module-level detector + policy ────────────────────────────


_active_detector: PIIDetector = RegexPIIDetector()


def register_pii_detector(detector: PIIDetector) -> None:
    """Swap the built-in detector for a customer-supplied one (e.g.
    Presidio). Call from engine startup; safe at runtime too."""
    global _active_detector
    _active_detector = detector
    logger.info(
        "llm.pii_guard.detector_registered",
        extra={"detector_type": type(detector).__name__},
    )


def _policy() -> str:
    """Read NEXUS_LLM_PII_GUARD_POLICY at call time so operators can
    flip it without a restart. Sane default: block."""
    raw = os.environ.get("NEXUS_LLM_PII_GUARD_POLICY", "block").strip().lower()
    if raw not in {"block", "redact", "warn", "off"}:
        logger.warning(
            "llm.pii_guard.unknown_policy",
            extra={"value": raw, "falling_back": "block"},
        )
        return "block"
    return raw


def _guarded_tiers() -> set[str]:
    raw = os.environ.get("NEXUS_LLM_PII_GUARD_TIERS", "tier1,tier2").strip()
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_tier_guarded(tier_value: str) -> bool:
    """Tier values are 'tier1', 'tier2', 'tier3' — string match."""
    return tier_value in _guarded_tiers()


# ─── Public entry point used by the tier router ────────────────


def check_text(text: str) -> list[PIIMatch]:
    """Scan a single string. Returns matches; doesn't decide policy."""
    return _active_detector.scan(text or "")


def enforce(
    system_prompt: str,
    user_prompt: str,
    *,
    tier_value: str,
    engine_name: str,
) -> tuple[str, str]:
    """Run the guard for one call. Returns (possibly-redacted) prompts.

    Raises PIIGuardViolation when policy=block and a match fires.

    The router calls this BEFORE dispatching to the tier provider.
    Tier 3 (on-prem) bypasses (controlled by NEXUS_LLM_PII_GUARD_TIERS).
    """
    if not is_tier_guarded(tier_value):
        return system_prompt, user_prompt

    policy = _policy()
    if policy == "off":
        return system_prompt, user_prompt

    matches: list[PIIMatch] = []
    for label, text in (("system_prompt", system_prompt), ("user_prompt", user_prompt)):
        for m in check_text(text):
            matches.append(m)

    if not matches:
        return system_prompt, user_prompt

    # Bump the Prometheus counter so SRE sees this happening.
    _record_violation(engine_name, tier_value, matches)

    if policy == "warn":
        logger.warning(
            "llm.pii_guard.matched",
            extra={
                "engine": engine_name, "tier": tier_value,
                "policy": "warn",
                "patterns": [m.pattern_name for m in matches],
            },
        )
        return system_prompt, user_prompt

    if policy == "redact":
        return (
            _redact_all(system_prompt),
            _redact_all(user_prompt),
        )

    # policy == "block"
    primary = matches[0]
    raise PIIGuardViolation(
        pattern_name=primary.pattern_name,
        masked_sample=primary.masked[:200],
    )


def _redact_all(text: str) -> str:
    """Pass through the detector and replace every match with a token."""
    if not text:
        return text
    spans = sorted(check_text(text), key=lambda m: m.start, reverse=True)
    out = text
    for m in spans:
        out = out[:m.start] + f"[REDACTED:{m.pattern_name.upper()}]" + out[m.end:]
    return out


# ─── Metrics ────────────────────────────────────────────────────


_prom_violations = None
_prom_registered = False


def _record_violation(
    engine_name: str, tier_value: str, matches: list[PIIMatch],
) -> None:
    global _prom_violations, _prom_registered
    if not _prom_registered:
        _prom_registered = True
        try:
            from nexus_sdk.observability.metrics import get_metrics
            m = get_metrics()
            if m:
                _prom_violations = m.custom_counter(
                    "nexus_llm_pii_guard_violations_total",
                    "PII guard pattern matches by engine/tier/pattern.",
                    labels=["engine", "tier", "pattern"],
                )
        except Exception:
            pass
    if _prom_violations is None:
        return
    for match in matches:
        try:
            _prom_violations.labels(
                service=engine_name, engine=engine_name,
                tier=tier_value, pattern=match.pattern_name,
            ).inc()
        except Exception:
            pass


__all__ = [
    "PIIGuardViolation",
    "PIIMatch",
    "PIIDetector",
    "RegexPIIDetector",
    "register_pii_detector",
    "check_text",
    "enforce",
    "is_tier_guarded",
]
