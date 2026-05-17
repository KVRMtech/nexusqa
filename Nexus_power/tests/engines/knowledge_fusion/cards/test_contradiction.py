"""HeuristicContradictionDetector — signal extraction without LLMs."""

from __future__ import annotations

from app.cards.contradiction import HeuristicContradictionDetector


def _det() -> HeuristicContradictionDetector:
    return HeuristicContradictionDetector()


def test_numeric_mismatch_on_shared_unit() -> None:
    sig = _det().detect(
        canonical="California cigar lookback is 24 months for tobacco.",
        candidate="California cigar lookback is 12 months for tobacco.",
    )
    assert sig is not None
    assert sig.kind == "numeric_mismatch"
    assert "24" in sig.rationale or "month" in sig.rationale


def test_matching_numbers_do_not_trigger() -> None:
    sig = _det().detect(
        canonical="California cigar lookback is 24 months for tobacco.",
        candidate="California uses a 24-month lookback for tobacco.",
    )
    assert sig is None


def test_temporal_supersession_detected() -> None:
    sig = _det().detect(
        canonical="ARPA suspends the 400% FPL cliff for ACA subsidies.",
        candidate="The 400% FPL cliff returned as of 2026 for ACA subsidies.",
    )
    assert sig is not None
    assert sig.kind in ("temporal_supersession", "numeric_mismatch")


def test_polarity_flip_detected_with_shared_anchor() -> None:
    sig = _det().detect(
        canonical="Tobacco classification requires wet signature on Form 8821-CA.",
        candidate="Tobacco classification does not require wet signature on Form 8821-CA.",
    )
    assert sig is not None
    assert sig.kind == "polarity_flip"


def test_unrelated_inputs_return_none() -> None:
    sig = _det().detect(
        canonical="California cigar lookback is 24 months.",
        candidate="Quarterly earnings beat consensus.",
    )
    assert sig is None


def test_low_overlap_ignores_negation() -> None:
    """Negation alone with no shared anchor isn't a contradiction signal."""
    sig = _det().detect(
        canonical="California cigar lookback is 24 months.",
        candidate="The customer's appointment is not on Friday.",
    )
    assert sig is None


def test_empty_inputs_return_none() -> None:
    assert _det().detect(canonical="", candidate="") is None
    assert _det().detect(canonical="x", candidate="") is None
    assert _det().detect(canonical="", candidate="y") is None
