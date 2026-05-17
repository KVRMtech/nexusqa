"""Question classifier — decides whether an inbound chat message is a
question worth echoing, and emits structured hints downstream.

Implementation is LLM-backed with strict Pydantic output validation.
A Redis cache keyed by ``(text_hash, classifier_version)`` makes
repeated identical messages free.

The classifier never invents content: when the LLM declines or fails
schema validation, we degrade to a deterministic fallback that
classifies anything with a ``?`` as a low-confidence question. This
guarantees the pipeline always has something to act on while
preserving production safety.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .llm import ChatMessage, LLMError, LLMTimeout, OllamaJsonClient

logger = logging.getLogger(__name__)


CLASSIFIER_VERSION = "v1"


# ── Output schema ───────────────────────────────────────────────


class ClassifierOutput(BaseModel):
    """Strict shape we require from the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    is_question: bool
    confidence: float = Field(ge=0.0, le=1.0)
    question_type: Literal[
        "factual", "how-to", "policy", "troubleshoot", "comparison", "other"
    ] = "other"
    domain_hints: list[str] = Field(default_factory=list, max_length=8)
    product_hints: list[str] = Field(default_factory=list, max_length=8)
    jurisdiction_hints: list[str] = Field(default_factory=list, max_length=8)
    urgency: Literal["low", "medium", "high"] = "low"
    rationale_short: str = Field(default="", max_length=512)

    @field_validator("domain_hints", "product_hints", "jurisdiction_hints")
    @classmethod
    def _trim_and_dedup(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in v:
            s = (raw or "").strip()
            if not s or len(s) > 128:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out


# ── Cache port ──────────────────────────────────────────────────


class ClassifierCache(Protocol):
    async def get(self, key: str) -> Optional[bytes]: ...
    async def setex(self, key: str, ttl: int, value: bytes) -> None: ...


class _NullCache:
    async def get(self, key: str) -> Optional[bytes]:  # noqa: ARG002
        return None

    async def setex(self, key: str, ttl: int, value: bytes) -> None:  # noqa: ARG002
        return None


# ── Sender context (passed alongside the text) ──────────────────


@dataclass(frozen=True)
class SenderContext:
    role: Optional[str] = None
    tenure_days: Optional[int] = None
    channel_topic: Optional[str] = None
    surface: str = "slack"


# ── Classifier ──────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a strict JSON classifier inside an enterprise knowledge "
    "platform. Given a chat message and minimal sender context, decide "
    "whether the message is a question that the platform should attempt "
    "to answer using prior recorded knowledge.\n\n"
    "Return exactly one JSON object with these keys (and no others):\n"
    '  "is_question": boolean,\n'
    '  "confidence": number between 0 and 1,\n'
    '  "question_type": one of factual|how-to|policy|troubleshoot|comparison|other,\n'
    '  "domain_hints": array of <=8 short topical strings (e.g. "underwriting/tobacco"),\n'
    '  "product_hints": array of <=8 product names or codes mentioned (e.g. "LT5"),\n'
    '  "jurisdiction_hints": array of <=8 jurisdictional tokens (e.g. "CA", "EU"),\n'
    '  "urgency": one of low|medium|high,\n'
    '  "rationale_short": a brief audit note <=200 chars (no user-facing copy).\n\n'
    "Hard rules:\n"
    "- Output JSON ONLY. No prose, no markdown, no comments.\n"
    "- If the message is a greeting, status update, or non-question, "
    'set "is_question": false and "confidence" reflecting your certainty.\n'
    "- Confidence reflects HOW SURE you are about is_question, not "
    "answerability.\n"
    "- Hints must be drawn from the message; do not invent products "
    "or jurisdictions."
)


class QuestionClassifier:
    """Tiered classifier: LLM with cache + deterministic fallback."""

    def __init__(
        self,
        *,
        llm: OllamaJsonClient,
        model: str,
        cache: Optional[ClassifierCache] = None,
        cache_ttl_seconds: int = 86400,
        cache_namespace: str = "echo_cls",
    ) -> None:
        self._llm = llm
        self._model = model
        self._cache = cache or _NullCache()
        self._cache_ttl = cache_ttl_seconds
        self._cache_ns = cache_namespace

    async def classify(
        self, *, text: str, sender: SenderContext
    ) -> ClassifierOutput:
        text = (text or "").strip()
        if not text:
            return ClassifierOutput(
                is_question=False,
                confidence=1.0,
                question_type="other",
                rationale_short="empty input",
            )

        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cache_key = f"{self._cache_ns}:{CLASSIFIER_VERSION}:{text_hash}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            output = await self._classify_via_llm(text, sender)
        except (LLMTimeout, LLMError, ValidationError, ValueError) as exc:
            logger.warning(
                "classifier.llm_failed falling_back err=%s", exc
            )
            output = _heuristic_fallback(text)

        await self._cache_set(cache_key, output)
        return output

    # ── Internals ───────────────────────────────────────────────

    async def _classify_via_llm(
        self, text: str, sender: SenderContext
    ) -> ClassifierOutput:
        user_payload = {
            "message": text,
            "sender_role": sender.role,
            "sender_tenure_days": sender.tenure_days,
            "channel_topic": sender.channel_topic,
            "surface": sender.surface,
        }
        # Filter out None so the prompt is compact.
        user_payload = {k: v for k, v in user_payload.items() if v not in (None, "")}
        messages = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=json.dumps(user_payload, ensure_ascii=False),
            ),
        ]
        raw = await self._llm.chat_json(
            model=self._model, messages=messages, temperature=0.0
        )
        return ClassifierOutput.model_validate(raw)

    async def _cache_get(self, key: str) -> Optional[ClassifierOutput]:
        try:
            raw = await self._cache.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return ClassifierOutput.model_validate_json(raw)
        except Exception:
            return None

    async def _cache_set(
        self, key: str, output: ClassifierOutput
    ) -> None:
        try:
            await self._cache.setex(
                key, self._cache_ttl, output.model_dump_json().encode("utf-8")
            )
        except Exception as exc:
            logger.debug("classifier.cache_set_failed: %s", exc)


# ── Deterministic fallback ──────────────────────────────────────


_QUESTION_WORDS = (
    "who", "what", "where", "when", "why", "how",
    "can", "could", "should", "would", "is", "are", "do", "does", "did",
)


def _heuristic_fallback(text: str) -> ClassifierOutput:
    """Cheap fallback used when the LLM is unavailable or invalid.

    Conservative: never produces a high-confidence positive. Designed
    so the orchestrator can still progress without a model — usually
    into DM-only / shadow mode where suppression is cheap.
    """
    lowered = text.lower().strip()
    starts_with_q = any(
        lowered.startswith(w + " ") or lowered.startswith(w + "'")
        for w in _QUESTION_WORDS
    )
    ends_with_q = lowered.endswith("?")
    if ends_with_q and starts_with_q:
        confidence = 0.7
        is_q = True
    elif ends_with_q or starts_with_q:
        confidence = 0.55
        is_q = True
    else:
        confidence = 0.6
        is_q = False
    return ClassifierOutput(
        is_question=is_q,
        confidence=confidence,
        question_type="other",
        rationale_short="heuristic fallback (LLM unavailable)",
    )
