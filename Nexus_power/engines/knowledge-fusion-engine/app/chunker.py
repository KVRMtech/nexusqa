"""Transcript chunker — speaker-aware, structure-preserving.

The canonical artifact provides transcript text in one of two shapes:

1. ``full_artifact_json`` carries a structured list under
   ``transcript.segments`` (or ``ears.segments``) with per-utterance
   ``speaker``, ``text``, ``start_ms``, ``end_ms``, ``confidence``
   keys. This is the preferred input — speaker boundaries and
   timestamps are exact.
2. Plain text in ``safe_transcript_text``. We split into sentences via
   a punctuation regex and pack into target-size windows, assigning
   timestamps proportionally to the artifact duration. Speakers are
   left as None — a chunker can never invent diarization.

The chunker never spans a speaker boundary inside a chunk. When the
input is structured, each chunk carries a single speaker. When the
input is plain text, the resulting chunks have ``speaker_id=None``.

Output windows target ``chunk_target_chars`` (≈ 300–500 tokens) with
``chunk_overlap_chars`` of overlap to preserve context across chunks.
Trailing fragments below ``chunk_min_chars`` are merged into the
preceding chunk to avoid a long tail of tiny units.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ── Output model ────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """One transcript segment ready to persist."""

    segment_id: str
    ordinal: int
    text: str
    text_hash: str
    start_ms: int
    end_ms: int
    speaker_id: Optional[str]
    speaker_role: Optional[str]
    confidence: Optional[float]
    token_count: Optional[int]
    topic_label: Optional[str] = None
    product_ids: tuple[str, ...] = field(default_factory=tuple)


# ── Chunker ─────────────────────────────────────────────────────


_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\"'(\[])",
    re.UNICODE,
)


class ChunkerConfig:
    __slots__ = ("target_chars", "min_chars", "overlap_chars", "max_chunks")

    def __init__(
        self,
        target_chars: int = 1600,
        min_chars: int = 200,
        overlap_chars: int = 200,
        max_chunks: int = 5000,
    ) -> None:
        if target_chars < 64:
            raise ValueError("target_chars must be >= 64")
        if overlap_chars < 0 or overlap_chars >= target_chars:
            raise ValueError("0 <= overlap_chars < target_chars")
        if min_chars < 0 or min_chars > target_chars:
            raise ValueError("0 <= min_chars <= target_chars")
        if max_chunks < 1:
            raise ValueError("max_chunks must be >= 1")
        self.target_chars = target_chars
        self.min_chars = min_chars
        self.overlap_chars = overlap_chars
        self.max_chunks = max_chunks


class TranscriptChunker:
    """Speaker-aware, structure-preserving chunker."""

    def __init__(self, config: ChunkerConfig):
        self._cfg = config

    # ── Public API ───────────────────────────────────────────────

    def chunk_artifact(self, artifact: dict[str, Any]) -> list[Chunk]:
        """Produce ordered chunks for a canonical artifact dict.

        Picks the structured path when present; falls back to plain
        text. Either path is fully deterministic given identical input.
        """
        structured = _extract_structured_segments(artifact)
        if structured:
            return self._chunk_structured(structured)
        text = (artifact.get("safe_transcript_text") or "").strip()
        if not text:
            return []
        duration_ms = int(
            round(float(artifact.get("duration_seconds") or 0.0) * 1000)
        )
        return self._chunk_plain_text(text, duration_ms)

    # ── Structured path ──────────────────────────────────────────

    def _chunk_structured(self, utterances: list[dict[str, Any]]) -> list[Chunk]:
        out: list[Chunk] = []
        ordinal = 0
        for spk_group in _group_by_speaker(utterances):
            speaker = spk_group["speaker"]
            role = spk_group.get("role")
            confidences = [
                float(u.get("confidence", 0.0))
                for u in spk_group["utterances"]
                if u.get("confidence") is not None
            ]
            avg_conf = (sum(confidences) / len(confidences)) if confidences else None

            # Concatenate utterances inside the speaker group; track
            # the (start_ms, end_ms) of the slice that produced each
            # character window so chunk timestamps stay accurate.
            joined_text, char_to_ms = _join_with_ms_index(spk_group["utterances"])
            if not joined_text:
                continue

            for window in _windowed_text(
                joined_text,
                target_chars=self._cfg.target_chars,
                min_chars=self._cfg.min_chars,
                overlap_chars=self._cfg.overlap_chars,
            ):
                if len(out) >= self._cfg.max_chunks:
                    logger.warning(
                        "chunker.max_chunks_reached: truncating output"
                    )
                    return out
                start_ms = char_to_ms(window["start"])
                end_ms = char_to_ms(window["end"] - 1)
                # Guard against zero-length window in degenerate input.
                if end_ms < start_ms:
                    end_ms = start_ms
                text = window["text"]
                out.append(
                    Chunk(
                        segment_id=uuid.uuid4().hex,
                        ordinal=ordinal,
                        text=text,
                        text_hash=hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest(),
                        start_ms=start_ms,
                        end_ms=end_ms,
                        speaker_id=speaker,
                        speaker_role=role,
                        confidence=avg_conf,
                        token_count=_estimate_tokens(text),
                    )
                )
                ordinal += 1
        return out

    # ── Plain-text path ─────────────────────────────────────────

    def _chunk_plain_text(self, text: str, duration_ms: int) -> list[Chunk]:
        total_len = len(text)
        out: list[Chunk] = []
        ordinal = 0
        for window in _windowed_text(
            text,
            target_chars=self._cfg.target_chars,
            min_chars=self._cfg.min_chars,
            overlap_chars=self._cfg.overlap_chars,
        ):
            if len(out) >= self._cfg.max_chunks:
                logger.warning(
                    "chunker.max_chunks_reached: truncating output"
                )
                return out
            start_frac = window["start"] / total_len if total_len else 0.0
            end_frac = window["end"] / total_len if total_len else 1.0
            start_ms = int(round(start_frac * duration_ms))
            end_ms = int(round(end_frac * duration_ms))
            if end_ms < start_ms:
                end_ms = start_ms
            chunk_text = window["text"]
            out.append(
                Chunk(
                    segment_id=uuid.uuid4().hex,
                    ordinal=ordinal,
                    text=chunk_text,
                    text_hash=hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=None,
                    speaker_role=None,
                    confidence=None,
                    token_count=_estimate_tokens(chunk_text),
                )
            )
            ordinal += 1
        return out


# ── Helpers ─────────────────────────────────────────────────────


def _extract_structured_segments(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate the structured utterance list inside ``full_artifact_json``.

    Recognised paths (first match wins):

        full_artifact_json.transcript.segments
        full_artifact_json.ears.segments
        full_artifact_json.audio_transcription.segments
        full_artifact_json.audio_transcription.transcript.segments
    """
    full = artifact.get("full_artifact_json") or {}
    candidates: list[Any] = []
    for path in (
        ("transcript", "segments"),
        ("ears", "segments"),
        ("audio_transcription", "segments"),
        ("audio_transcription", "transcript", "segments"),
    ):
        node: Any = full
        ok = True
        for key in path:
            if not isinstance(node, dict) or key not in node:
                ok = False
                break
            node = node[key]
        if ok and isinstance(node, list) and node:
            candidates.extend(node)
            break

    cleaned: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "speaker": _stringify(item.get("speaker"))
                or _stringify(item.get("speaker_id")),
                "role": _stringify(item.get("speaker_role")),
                "text": text,
                "start_ms": _to_ms(item.get("start_ms"), item.get("start")),
                "end_ms": _to_ms(item.get("end_ms"), item.get("end")),
                "confidence": item.get("confidence"),
            }
        )
    return cleaned


def _stringify(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_ms(primary: Any, fallback: Any) -> int:
    """Accept milliseconds (int) or seconds (float). Default 0."""
    for v in (primary, fallback):
        if v is None:
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        # Heuristic: anything < 10_000 we treat as seconds.
        if num < 10_000:
            return int(round(num * 1000))
        return int(round(num))
    return 0


def _group_by_speaker(utterances: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Adjacent same-speaker utterances merge into one group."""
    current: Optional[dict[str, Any]] = None
    for u in utterances:
        speaker = u.get("speaker")
        role = u.get("role")
        if current is not None and current["speaker"] == speaker and current.get("role") == role:
            current["utterances"].append(u)
            continue
        if current is not None:
            yield current
        current = {"speaker": speaker, "role": role, "utterances": [u]}
    if current is not None:
        yield current


def _join_with_ms_index(
    utterances: list[dict[str, Any]],
) -> tuple[str, "_MsLookup"]:
    """Concatenate utterance texts and build a position→ms lookup.

    Returns the joined text plus a callable that maps any character
    index in the joined text to the original utterance's ``start_ms``
    (or, for the last char in an utterance, ``end_ms``).
    """
    pieces: list[str] = []
    spans: list[tuple[int, int, int, int]] = []  # start_char, end_char, start_ms, end_ms
    cursor = 0
    for u in utterances:
        text = u["text"]
        start_char = cursor
        if pieces:
            pieces.append(" ")
            cursor += 1
        pieces.append(text)
        cursor += len(text)
        spans.append((start_char, cursor, int(u["start_ms"]), int(u["end_ms"])))
    return "".join(pieces), _MsLookup(spans)


class _MsLookup:
    __slots__ = ("_spans",)

    def __init__(self, spans: list[tuple[int, int, int, int]]):
        self._spans = spans

    def __call__(self, char_idx: int) -> int:
        if not self._spans:
            return 0
        if char_idx < 0:
            return self._spans[0][2]
        for start_char, end_char, start_ms, end_ms in self._spans:
            if char_idx < end_char:
                if end_char == start_char:
                    return start_ms
                fraction = (char_idx - start_char) / max(
                    1, (end_char - start_char)
                )
                return int(round(start_ms + fraction * (end_ms - start_ms)))
        # Past the last span — return final end_ms.
        return self._spans[-1][3]


def _windowed_text(
    text: str,
    *,
    target_chars: int,
    min_chars: int,
    overlap_chars: int,
) -> Iterable[dict[str, Any]]:
    """Yield {start, end, text} windows over ``text``.

    Boundaries snap to sentence endings whenever possible. The last
    window may be shorter than ``target_chars`` but is merged into
    the previous one if it falls below ``min_chars``.
    """
    if not text:
        return

    sentence_breaks = [m.start() for m in _SENTENCE_SPLIT_RE.finditer(text)]
    sentence_breaks.append(len(text))

    windows: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        ideal_end = start + target_chars
        if ideal_end >= len(text):
            windows.append((start, len(text)))
            break

        # Snap to the nearest sentence boundary at or after ideal_end,
        # but not past the next window's potential start.
        snap = next(
            (b for b in sentence_breaks if b >= ideal_end),
            len(text),
        )
        end = min(snap, len(text))
        windows.append((start, end))

        # Advance with overlap.
        next_start = max(start + 1, end - overlap_chars)
        if next_start <= start:
            next_start = end
        start = next_start

    # Merge a tiny trailing window into its predecessor.
    if (
        len(windows) >= 2
        and (windows[-1][1] - windows[-1][0]) < min_chars
    ):
        prev_start, _ = windows[-2]
        _, last_end = windows[-1]
        windows[-2] = (prev_start, last_end)
        windows.pop()

    for s, e in windows:
        yield {"start": s, "end": e, "text": text[s:e].strip()}


def _estimate_tokens(text: str) -> int:
    """Coarse token estimate (≈ 4 chars per token).

    This is a heuristic — the actual count depends on the tokenizer.
    We persist it for ranking and capacity planning, not billing.
    """
    return max(1, len(text) // 4)
