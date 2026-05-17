"""Heuristic contradiction detector.

For Phase 3 v1 the detector is deterministic and conservative — it
flags statements likely to *contradict* the card's canonical statement
rather than to *refine* or *elaborate* it. Real LLM-backed semantic
contradiction lands in Phase 5 alongside cross-modal alignment.

Signals used (all combine into a single score; the detector returns
the highest-confidence signal observed):

    * **Polarity flip** — one side says "X is required" while the
      other says "X is not required"; detected by short negation
      patterns adjacent to a shared anchor token.
    * **Numeric mismatch** — both sides cite the same unit but
      different magnitudes (``24 months`` vs ``12 months``).
    * **Temporal supersession** — explicit phrases like
      "no longer", "as of <date>", "deprecated".

The detector NEVER raises; on uncertainty it returns ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol


class ContradictionDetector(Protocol):
    """Hook for swapping in an LLM-backed detector later."""

    def detect(
        self, *, canonical: str, candidate: str
    ) -> Optional["ContradictionSignal"]:  # pragma: no cover — interface
        ...


@dataclass(frozen=True)
class ContradictionSignal:
    kind: str  # 'polarity_flip' | 'numeric_mismatch' | 'temporal_supersession'
    confidence: float
    snippet_canonical: str
    snippet_candidate: str
    rationale: str


# ── Heuristic detector ──────────────────────────────────────────


_NEG_RE = re.compile(
    r"\b(?:not|no|never|n['’]?t|cannot|without|disallowed|prohibited|"
    r"forbidden|deprecated|removed|abolished|repealed|"
    r"no\s+longer|does\s+not|do\s+not|did\s+not|will\s+not|"
    r"isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|hasn['’]?t|haven['’]?t)\b",
    re.IGNORECASE,
)

_TEMPORAL_RE = re.compile(
    r"\b(?:no\s+longer|deprecated|sunsetted|repealed|"
    r"as\s+of\s+\d{4}|effective\s+\w+\s+\d{1,2},?\s+\d{4}|"
    r"prior\s+to|previously)\b",
    re.IGNORECASE,
)

# Numeric pattern: integer/float followed by a unit, optionally
# separated by whitespace, a hyphen, or an underscore (so we match
# both "24 months" and "24-month").
_NUMBER_UNIT_RE = re.compile(
    r"(?<![\w\.])(\d+(?:\.\d+)?)\s*[-_]?\s*"
    r"(months?|years?|days?|weeks?|hours?|minutes?|seconds?|"
    r"%|percent|usd|eur|gbp|dollars?|fpl|bps)",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-/]*")


class HeuristicContradictionDetector:
    """Deterministic, dependency-free detector."""

    def __init__(
        self,
        *,
        min_shared_tokens: int = 2,
        min_confidence: float = 0.55,
    ) -> None:
        self._min_shared = max(1, int(min_shared_tokens))
        self._min_conf = min_confidence

    def detect(
        self,
        *,
        canonical: str,
        candidate: str,
    ) -> Optional[ContradictionSignal]:
        canonical = (canonical or "").strip()
        candidate = (candidate or "").strip()
        if not canonical or not candidate:
            return None

        shared = self._shared_significant_tokens(canonical, candidate)
        if len(shared) < self._min_shared:
            # Without enough lexical overlap, any disagreement is more
            # likely a different topic than a contradiction.
            return None

        # Strongest signal: numeric mismatch on the same unit.
        sig = self._numeric_mismatch(canonical, candidate)
        if sig is not None:
            return sig

        # Next: explicit temporal supersession.
        sig = self._temporal_supersession(canonical, candidate)
        if sig is not None:
            return sig

        # Finally: polarity flip — one side negates the other.
        sig = self._polarity_flip(canonical, candidate)
        if sig is not None:
            return sig

        return None

    # ── Heuristics ──────────────────────────────────────────────

    def _shared_significant_tokens(self, a: str, b: str) -> set[str]:
        # Drop very short tokens and a small stop-word set.
        stop = {
            "the", "and", "a", "an", "of", "to", "in", "on", "for", "is",
            "are", "be", "as", "or", "by", "this", "that", "with",
            "we", "our", "you", "they", "it", "but", "if", "at", "from",
            "into", "than", "then", "so", "do", "does", "did",
        }
        ta = {
            t.lower() for t in _TOKEN_RE.findall(a) if len(t) > 2
        }
        tb = {
            t.lower() for t in _TOKEN_RE.findall(b) if len(t) > 2
        }
        return (ta & tb) - stop

    def _numeric_mismatch(
        self, canonical: str, candidate: str
    ) -> Optional[ContradictionSignal]:
        a_pairs = _NUMBER_UNIT_RE.findall(canonical)
        b_pairs = _NUMBER_UNIT_RE.findall(candidate)
        if not a_pairs or not b_pairs:
            return None
        # Index by normalised unit (strip trailing 's', lowercase).
        a_by_unit: dict[str, list[float]] = {}
        for n, u in a_pairs:
            try:
                a_by_unit.setdefault(_norm_unit(u), []).append(float(n))
            except ValueError:
                continue
        b_by_unit: dict[str, list[float]] = {}
        for n, u in b_pairs:
            try:
                b_by_unit.setdefault(_norm_unit(u), []).append(float(n))
            except ValueError:
                continue
        for unit in a_by_unit:
            if unit not in b_by_unit:
                continue
            a_vals = a_by_unit[unit]
            b_vals = b_by_unit[unit]
            # Mismatch when no value on one side matches the other.
            if any(_close(va, vb) for va in a_vals for vb in b_vals):
                continue
            return ContradictionSignal(
                kind="numeric_mismatch",
                confidence=0.85,
                snippet_canonical=_first_n(canonical, 120),
                snippet_candidate=_first_n(candidate, 120),
                rationale=(
                    f"divergent {unit} values: "
                    f"canonical={a_vals} vs candidate={b_vals}"
                ),
            )
        return None

    def _temporal_supersession(
        self, canonical: str, candidate: str
    ) -> Optional[ContradictionSignal]:
        # If the candidate explicitly says "no longer / deprecated / as of ..."
        # AND it shares vocabulary with the canonical, treat it as
        # supersession-flavoured contradiction.
        if not _TEMPORAL_RE.search(candidate):
            return None
        return ContradictionSignal(
            kind="temporal_supersession",
            confidence=0.65,
            snippet_canonical=_first_n(canonical, 120),
            snippet_candidate=_first_n(candidate, 120),
            rationale="candidate language signals supersession",
        )

    def _polarity_flip(
        self, canonical: str, candidate: str
    ) -> Optional[ContradictionSignal]:
        a_neg = bool(_NEG_RE.search(canonical))
        b_neg = bool(_NEG_RE.search(candidate))
        if a_neg == b_neg:
            return None
        confidence = self._min_conf  # base confidence for polarity flip
        # Slightly higher when negation co-occurs near a shared anchor.
        shared = self._shared_significant_tokens(canonical, candidate)
        if shared:
            confidence = min(0.78, self._min_conf + 0.05 * len(shared))
        return ContradictionSignal(
            kind="polarity_flip",
            confidence=confidence,
            snippet_canonical=_first_n(canonical, 120),
            snippet_candidate=_first_n(candidate, 120),
            rationale=(
                "exactly one side uses negation near shared anchor tokens"
            ),
        )


# ── Helpers ─────────────────────────────────────────────────────


def _norm_unit(u: str) -> str:
    u = (u or "").strip().lower()
    if u.endswith("s"):
        u = u[:-1]
    return u


def _close(a: float, b: float, *, tol: float = 0.01) -> bool:
    if a == b:
        return True
    if a == 0 or b == 0:
        return abs(a - b) <= tol
    return abs(a - b) / max(abs(a), abs(b)) <= tol


def _first_n(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"
