"""Vertical-aware PII detector for OCR / transcript text.

Built on top of :mod:`nexus_sdk.evidence.vocabularies`.  Every call
composes the **default** baseline with the caller-supplied vertical
(banking, healthcare, insurance, telecom) so a healthcare tenant gets
both ``ssn_us`` AND ``mrn_labelled`` matches in one pass.

Why this lives in the SDK (and not the Shield engine):

  * The Shield engine's runtime PII filter is a request-time gate
    sitting in front of the transcript / artifact-store writers.
    It must be fast, single-threaded, and never block on disk I/O.
  * The Phase 4 detector is an *evidence-time* tool: it runs once
    per scene during the canonical pipeline, on already-stored OCR
    text, and produces structured hits the UI surfaces as warning
    badges.  Different latency budget, different consumers.
  * Keeping it in the SDK means the quality gate, the test-case
    generator, AND the orchestrator can all share one in-memory
    pattern set without a network round-trip to Shield.

Both layers stay defense-in-depth: Shield rejects raw PII at the
edge; this detector annotates already-redacted text with surviving
hits so the human reviewer sees what (if anything) leaked through.

Determinism: hits are returned in a stable order (severity, then
pattern_name, then span) so re-running the same input produces a
byte-identical evidence record — important for the artifact-level
deterministic hashes used by the diff/heal pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from .vocabularies import (
    PIIPattern,
    Vocabulary,
    get_vocabulary,
    iter_vocabularies,
)


@dataclass(frozen=True)
class PIIHit:
    """One PII detection event.

    ``span`` is a half-open ``(start, end)`` index pair into the
    *original* input string.  Callers that need to redact in place
    can do so by walking hits in reverse span order and replacing
    ``text[start:end]`` with ``redaction_placeholder``.
    """

    pattern_name: str
    severity: str
    vertical: str
    matched_text: str
    span: tuple[int, int]
    redaction_placeholder: str

    @property
    def length(self) -> int:
        return self.span[1] - self.span[0]


# Severity ordering for deterministic output.
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _hits_from_pattern(
    text: str,
    pattern: PIIPattern,
    vertical_name: str,
) -> Iterator[PIIHit]:
    for m in pattern.pattern.finditer(text):
        yield PIIHit(
            pattern_name=pattern.name,
            severity=pattern.severity,
            vertical=vertical_name,
            matched_text=m.group(0),
            span=(m.start(), m.end()),
            redaction_placeholder=pattern.redaction_placeholder,
        )


def detect_pii(
    text: str,
    vertical: Optional[str] = None,
    *,
    include_default: bool = True,
) -> list[PIIHit]:
    """Scan ``text`` for PII using the default + chosen vertical vocabularies.

    Args:
        text:             OCR / transcript text to scan.  Empty → [].
        vertical:         Tenant's industry vertical (banking, healthcare,
                          insurance, telecom).  ``None`` runs default only.
        include_default:  When True (the recommended setting), the default
                          vocabulary is always evaluated alongside the chosen
                          vertical.  Set False only in tests that pin to one
                          file's patterns.

    Returns:
        A sorted list of :class:`PIIHit` — high-severity first, then
        medium, then low; within a severity tier ties are broken by
        pattern_name then span.  Overlapping hits from different
        patterns are NOT merged — callers see every signal a vocab
        produced and decide on their own redaction strategy.
    """
    if not text:
        return []

    vocabs: list[Vocabulary] = []
    if include_default:
        baseline = get_vocabulary("default")
        if baseline is not None:
            vocabs.append(baseline)
    target = get_vocabulary(vertical)
    # Avoid scanning the default twice if the caller passed
    # ``vertical="default"`` explicitly.
    if target is not None and target.name != "default":
        vocabs.append(target)

    hits: list[PIIHit] = []
    for vocab in vocabs:
        for pattern in vocab.pii_patterns:
            hits.extend(_hits_from_pattern(text, pattern, vocab.name))

    hits.sort(key=lambda h: (
        _SEVERITY_RANK.get(h.severity, 99),
        h.pattern_name,
        h.span,
    ))
    return hits


def redact(
    text: str,
    hits: Iterable[PIIHit],
) -> str:
    """Return ``text`` with every hit replaced by its redaction placeholder.

    Hits are applied in *reverse span order* so earlier index pairs
    remain valid after later replacements.  Overlapping hits are
    handled by skipping any hit whose span is fully contained in a
    later-applied range (the later, longer match wins).
    """
    if not text or not hits:
        return text
    # Sort by span end descending so we can walk back through the
    # string applying replacements without invalidating earlier
    # offsets.  Stable sort keeps the deterministic order for
    # equal-end ties.
    sorted_hits = sorted(hits, key=lambda h: (h.span[1], -h.length), reverse=True)
    applied_ranges: list[tuple[int, int]] = []
    out = text
    for h in sorted_hits:
        start, end = h.span
        # Skip if a later (longer) hit already covered this span.
        if any(s <= start and end <= e for s, e in applied_ranges):
            continue
        out = out[:start] + h.redaction_placeholder + out[end:]
        applied_ranges.append((start, end))
    return out


def summarise(hits: Iterable[PIIHit]) -> dict[str, int]:
    """Return ``{pattern_name: count}`` for a hits collection.

    The summary is used by the quality-gate's PII signal: the gate
    weights ``high`` matches differently from ``low``, but raw counts
    per pattern are the simplest exchangeable representation.
    """
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.pattern_name] = counts.get(h.pattern_name, 0) + 1
    return counts


def patterns_for(vertical: Optional[str], *, include_default: bool = True) -> tuple[PIIPattern, ...]:
    """Return the compiled patterns that would be applied for ``vertical``.

    Used by the quality gate's static config snapshot and by the
    documentation generator.  No text scanning is performed.
    """
    out: list[PIIPattern] = []
    if include_default:
        b = get_vocabulary("default")
        if b is not None:
            out.extend(b.pii_patterns)
    t = get_vocabulary(vertical)
    if t is not None and t.name != "default":
        out.extend(t.pii_patterns)
    return tuple(out)


def known_verticals() -> tuple[str, ...]:
    """Return registered vertical names, excluding mainframe_codes
    (which carries no synonyms or PII patterns of its own).
    """
    return tuple(
        v.name for v in iter_vocabularies()
        if v.pii_patterns or v.synonyms
    )


__all__ = [
    "PIIHit",
    "detect_pii",
    "redact",
    "summarise",
    "patterns_for",
    "known_verticals",
]
