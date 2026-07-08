"""QE-Central — PII redaction at source (design §2.1 / REFUSE-matrix R6).

Every value QE-Central persists into the substrate (``page_actions.value``,
``page_visits.form_snapshot`` values, ``ground_truth_events.value``) passes
through this module FIRST — "Form VALUES are PII-redacted AT SOURCE before
persistence, never raw PII at rest" (nexus_sdk.db.models GroundTruthEventRow
docstring, models.py:2519-2520).

Detection reuses the SDK's deterministic detector
(``nexus_sdk.evidence.pii_detector`` + the vertical vocabularies) so
QE-Central and the video pipeline agree on what counts as PII; the SDK's
per-pattern placeholders are REPLACED with the uniform QE-Central classes
``[REDACTED:{ssn|card|email|phone|dob}]`` so downstream consumers (and the
R6 harness rule) match one stable shape.  Passwords are not a regex class —
they are flagged by the capture signals (``form_snapshot_signals[label].type
== 'password'`` / ``qec.input_type == 'password'``) and redacted whole via
:func:`redact_secret`.

Failure posture is FAIL-OPEN by design (per the E1 ``_redact_value``
precedent): a detector error lets the value pass UNREDACTED with a loud
warning and an ``errors`` count the writer surfaces in ``WriteStats`` —
a broken detector must never silently drop evidence, and the unredacted
count keeps the failure visible instead of masked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The QE-Central PII class set (design §3.1 redact module).
PII_CLASSES: tuple[str, ...] = ("ssn", "card", "password", "email", "phone", "dob")

#: SDK detector pattern-name → QE-Central class (default vocabulary,
#: sdk/nexus-sdk/nexus_sdk/evidence/vocabularies/default.yaml).
_CLASS_BY_PATTERN: dict[str, str] = {
    "ssn_us": "ssn",
    "credit_card_4x4": "card",
    "credit_card_amex": "card",
    "email_address": "email",
    "phone_us": "phone",
    "dob_labelled": "dob",
}


def placeholder(pii_class: str) -> str:
    """The uniform replacement token for one PII class."""
    return f"[REDACTED:{pii_class}]"


@dataclass(frozen=True)
class RedactionResult:
    """Outcome of redacting ONE value.

    ``counts`` maps PII class → number of replacements; ``errors`` counts
    detector failures where the value passed through UNREDACTED (fail-open,
    surfaced — never silent).
    """

    value: str
    counts: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact_secret(value: str | None, pii_class: str = "password") -> RedactionResult:
    """Redact a signal-flagged secret (password field) WHOLE.

    Regex detection cannot recognise a password; the capture signals can.
    A non-empty value becomes exactly one ``[REDACTED:password]`` token;
    empty/None values stay empty (nothing leaked, nothing to count).
    """
    if pii_class not in PII_CLASSES:
        raise ValueError(f"unknown PII class {pii_class!r}; expected one of {PII_CLASSES}")
    if not value:
        return RedactionResult(value="" if value is None else value)
    return RedactionResult(value=placeholder(pii_class), counts={pii_class: 1})


def redact_value(value: str | None) -> RedactionResult:
    """Redact every detected PII span in ``value`` → ``[REDACTED:class]``.

    Deterministic (the SDK detector returns stable-ordered hits); spans are
    replaced in reverse order so earlier offsets stay valid, and a span fully
    covered by an already-applied replacement is skipped (the longer match
    wins — mirrors ``nexus_sdk.evidence.pii_detector.redact``).
    """
    if not value:
        return RedactionResult(value="" if value is None else value)
    try:
        from nexus_sdk.evidence.pii_detector import detect_pii

        hits = detect_pii(value)
    except Exception as exc:  # fail-open: pass through, loudly, counted
        logger.warning(
            "qec.redact.detector_failed value passes UNREDACTED",
            extra={"error": str(exc)[:300]},
        )
        return RedactionResult(value=value, errors=1)

    counts: dict[str, int] = {}
    applied: list[tuple[int, int]] = []
    out = value
    for hit in sorted(hits, key=lambda h: (h.span[1], -(h.span[1] - h.span[0])),
                      reverse=True):
        pii_class = _CLASS_BY_PATTERN.get(hit.pattern_name)
        if pii_class is None:
            continue  # vertical-specific pattern outside the v1 class set
        start, end = hit.span
        if any(s <= start and end <= e for s, e in applied):
            continue
        out = out[:start] + placeholder(pii_class) + out[end:]
        applied.append((start, end))
        counts[pii_class] = counts.get(pii_class, 0) + 1
    return RedactionResult(value=out, counts=counts)


def merge_counts(totals: dict[str, int], result: RedactionResult) -> None:
    """Fold one :class:`RedactionResult`'s counts into a running total."""
    for pii_class, n in result.counts.items():
        totals[pii_class] = totals.get(pii_class, 0) + n
