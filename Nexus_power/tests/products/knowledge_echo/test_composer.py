"""Block Kit composer — structure, sanitisation, payload-hash stability."""

from __future__ import annotations

from app.matcher import MatchCandidate, MatchResult
from app.slack.composer import EchoCardComposer


def _candidate(
    *,
    text: str = "California cigar lookback is 24 months.",
    similarity: float = 0.91,
    speaker_id: str = "priya",
    speaker_role: str = "underwriting",
    start_ms: int = 31_000,
    end_ms: int = 42_000,
) -> MatchCandidate:
    return MatchCandidate(
        node_id="n1",
        node_type="TranscriptSegment",
        similarity=similarity,
        text=text,
        speaker_id=speaker_id,
        speaker_role=speaker_role,
        session_id="sess-2025-08-14",
        artifact_id="art-1",
        start_ms=start_ms,
        end_ms=end_ms,
        ordinal=3,
        product_ids=("lt5",),
        raw={},
    )


def _result(candidates: list[MatchCandidate]) -> MatchResult:
    if not candidates:
        return MatchResult(
            candidates=[], top_similarity=0.0, confidence_band="none"
        )
    sims = [c.similarity for c in candidates]
    top = max(sims)
    band = "high" if top >= 0.85 else "medium" if top >= 0.65 else "low"
    return MatchResult(
        candidates=candidates, top_similarity=top, confidence_band=band  # type: ignore[arg-type]
    )


def test_compose_produces_header_quote_actions() -> None:
    composer = EchoCardComposer()
    card = composer.compose(
        dispatch_id="d-1",
        question_text="What's the CA tobacco lookback?",
        match=_result([_candidate()]),
    )
    assert card is not None
    types = [b["type"] for b in card.blocks]
    assert types[0] == "header"
    assert "section" in types
    assert types[-1] == "actions"
    # Action ids carry the dispatch id so feedback can be routed.
    actions_block = card.blocks[-1]
    action_ids = [
        elem["action_id"] for elem in actions_block["elements"]
    ]
    assert any("thumbs_up" in a for a in action_ids)
    assert any("thumbs_down" in a for a in action_ids)
    assert any("ask_sme" in a for a in action_ids)
    assert all(elem["value"] == "d-1" or elem["action_id"].startswith("echo_ask_sme")
               for elem in actions_block["elements"])


def test_similarity_pct_in_header() -> None:
    composer = EchoCardComposer()
    card = composer.compose(
        dispatch_id="d-1",
        question_text="?",
        match=_result([_candidate(similarity=0.737)]),
    )
    assert card is not None
    header_text = card.blocks[0]["text"]["text"]
    assert "74% match" in header_text


def test_no_candidates_returns_none() -> None:
    composer = EchoCardComposer()
    assert composer.compose(
        dispatch_id="d-1", question_text="?", match=_result([])
    ) is None


def test_quote_is_sanitised() -> None:
    composer = EchoCardComposer()
    card = composer.compose(
        dispatch_id="d-1",
        question_text="ok",
        match=_result(
            [_candidate(text="A <b>html-ish</b> & dangerous quote")]
        ),
    )
    assert card is not None
    quote_block = card.blocks[1]
    assert "&lt;b&gt;" in quote_block["text"]["text"]
    assert "&amp;" in quote_block["text"]["text"]


def test_payload_hash_is_stable() -> None:
    composer = EchoCardComposer()
    a = composer.compose(
        dispatch_id="d-1",
        question_text="q",
        match=_result([_candidate()]),
    )
    b = composer.compose(
        dispatch_id="d-1",
        question_text="q",
        match=_result([_candidate()]),
    )
    assert a is not None and b is not None
    assert a.payload_hash == b.payload_hash


def test_payload_hash_differs_when_text_differs() -> None:
    composer = EchoCardComposer()
    a = composer.compose(
        dispatch_id="d-1",
        question_text="q",
        match=_result([_candidate(text="text one")]),
    )
    b = composer.compose(
        dispatch_id="d-1",
        question_text="q",
        match=_result([_candidate(text="text two")]),
    )
    assert a is not None and b is not None
    assert a.payload_hash != b.payload_hash


def test_long_question_truncates_in_text_field() -> None:
    composer = EchoCardComposer()
    long_q = "abcde " * 1000
    card = composer.compose(
        dispatch_id="d-1",
        question_text=long_q,
        match=_result([_candidate()]),
    )
    assert card is not None
    assert len(card.text) <= 3000
