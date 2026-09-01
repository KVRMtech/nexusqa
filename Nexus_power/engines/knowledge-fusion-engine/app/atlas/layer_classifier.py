"""Assign a layer to an atlas-eligible node.

The classifier is a small, defensive component sitting between
ingestion (which has node_type + text + context) and the atlas
projection (which needs a definitive layer). Strategy:

    1. Strong type signal — if the source ``node_type`` maps
       deterministically onto a layer (e.g. ``BusinessRule`` → ``rule``),
       use it directly.
    2. Heuristic regex pass over the text — fast keyword-based
       disambiguation that catches obvious cases (``POST /api/...`` →
       ``application``, ``SELECT * FROM ...`` → ``data``).
    3. Optional LLM verifier — when both signals are weak and an
       LLM client implementing ``chat_json(model, messages, ...)`` is
       supplied, ask the model. ``messages`` must be a list of objects
       with ``role`` and ``content`` attributes; the classifier ships
       a ``ChatMessage`` dataclass that satisfies the contract.
    4. Default to ``rule`` (the safest place for an unknown business-
       text segment).

Confidence is reported per source so the operator can sort review
queues by uncertainty.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class ChatMessage:
    """Minimal LLM message envelope expected by ``chat_json`` clients."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .models import Layer

logger = logging.getLogger(__name__)


# ── DTO ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LayerVerdict:
    layer: Layer
    confidence: float
    rationale: str
    source: str  # 'type_map' | 'heuristic' | 'llm' | 'default'


# ── Type → layer map ───────────────────────────────────────────


_TYPE_LAYER_MAP: dict[str, Layer] = {
    "UIFlow": Layer.EXPERIENCE,
    "UIScreen": Layer.EXPERIENCE,
    "UIElement": Layer.EXPERIENCE,
    "MainframeScreen": Layer.EXPERIENCE,
    "APIEndpoint": Layer.APPLICATION,
    "DatabaseTable": Layer.DATA,
    "RateTable": Layer.DATA,
    "BusinessRule": Layer.RULE,
    "Coverage": Layer.RULE,
    "Form": Layer.RULE,
    "Endorsement": Layer.RULE,
    "Filing": Layer.RULE,
    "TestCase": Layer.TEST,
    "TestStep": Layer.TEST,
    "TestResult": Layer.TEST,
    "StateRegulation": Layer.COMPLIANCE,
    "TranscriptSegment": Layer.RULE,  # treat extracted segments as rule-flavoured by default
    "KnowledgeCard": Layer.RULE,
}


# ── Heuristic patterns ─────────────────────────────────────────


_EXPERIENCE_RE = re.compile(
    r"\b(?:button|click(?:ed|s)?|page|screen|tab|modal|form\s+field|"
    r"sidebar|tooltip|dropdown|navigate(?:s)?|UI|user\s+interface|"
    r"login\s+screen|wizard|step\s+\d+|dashboard)\b",
    re.IGNORECASE,
)

_APPLICATION_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[^\s]+|"
    r"\b(?:endpoint|service|microservice|REST\b|GraphQL|gRPC|"
    r"webhook|api\s+call|/api/v\d+/[\w/.-]+|connector|consumer|producer)\b",
    re.IGNORECASE,
)

_DATA_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b\s+|"
    r"\b(?:table|column|schema|index|view|materialized|"
    r"row|primary\s+key|foreign\s+key|sql\b|nosql|kafka\s+topic|"
    r"redis\s+(?:key|stream)|stored\s+proc(?:edure)?|rate_tables?|"
    r"lookup\s+table)\b",
    re.IGNORECASE,
)

_RULE_RE = re.compile(
    r"\b(?:rule|policy|requirement|must|shall|when\s+\w+\s+then|"
    r"if\s+\w+(?:\s+\w+){0,3}\s+(?:then|the)|eligible|eligibility|"
    r"threshold|lookback|premium|underwriting|classification)\b",
    re.IGNORECASE,
)

_TEST_RE = re.compile(
    r"\b(?:test\s+case|TC[-_]?\d+|scenario|given\s+\w+\s+when|"
    r"assert(?:ion)?|expected|under\s+test|fixture|regression|"
    r"smoke\s+test|integration\s+test)\b",
    re.IGNORECASE,
)

_OPS_RE = re.compile(
    r"\b(?:alert|incident|page(?:r)?duty|runbook|playbook|on[- ]call|"
    r"sla|p\d{1,3}\s+latency|kpi|dashboard|metric|grafana|"
    r"datadog|outage|degradation|sev\d)\b",
    re.IGNORECASE,
)

_COMPLIANCE_RE = re.compile(
    r"\b(?:HIPAA|GDPR|GLBA|SOX|FERPA|FedRAMP|PCI[- ]DSS|"
    r"regulation\b|regulator|jurisdiction|state\s+regulation|"
    r"NAIC|DOI\s+bulletin|insurance\s+code|"
    r"compliance\s+(?:officer|requirement))\b",
    re.IGNORECASE,
)


_HEURISTICS: list[tuple[re.Pattern[str], Layer, float]] = [
    (_COMPLIANCE_RE, Layer.COMPLIANCE, 0.78),
    (_OPS_RE, Layer.OPS, 0.78),
    (_TEST_RE, Layer.TEST, 0.78),
    (_APPLICATION_RE, Layer.APPLICATION, 0.80),
    (_DATA_RE, Layer.DATA, 0.78),
    (_EXPERIENCE_RE, Layer.EXPERIENCE, 0.75),
    (_RULE_RE, Layer.RULE, 0.72),
]


# ── LLM port ───────────────────────────────────────────────────


class _ChatMessage(Protocol):
    role: str
    content: str


class _LLMClient(Protocol):
    async def chat_json(
        self, *, model: str, messages: list, temperature: float = 0.0
    ) -> dict: ...


class LLMLayerPick(BaseModel):
    """Strict shape we require from the model."""

    model_config = ConfigDict(extra="forbid")

    layer: Layer
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=512)

    @field_validator("rationale")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "")[:512]


_LLM_SYSTEM_PROMPT = (
    "You assign one of the following layers to a knowledge fragment "
    "extracted from a recorded product demo or rule statement:\n"
    "  experience   - UI screens, buttons, agent journeys.\n"
    "  application  - API endpoints, services, integrations.\n"
    "  data         - tables, columns, queues, lookups.\n"
    "  rule         - business rules, policies, eligibility, classification.\n"
    "  test         - test cases, scenarios, assertions.\n"
    "  ops          - alerts, runbooks, dashboards, SLAs.\n"
    "  compliance   - regulations, jurisdictions, statutes.\n\n"
    "Return exactly one JSON object with the keys:\n"
    '  "layer": one of the seven values above,\n'
    '  "confidence": number in [0, 1],\n'
    '  "rationale": short audit note (no user-facing copy, max 200 chars).\n\n'
    "Hard rules: output JSON only; no prose; do not invent layers."
)


# ── Classifier protocol + heuristic implementation ─────────────


class LayerClassifier(Protocol):
    async def classify(
        self,
        *,
        node_type: str,
        text: str,
        layer_hint: Optional[str] = None,
    ) -> LayerVerdict: ...


class HeuristicLayerClassifier:
    """Deterministic-first classifier with an optional LLM upgrade.

    ``confidence_threshold_for_llm`` controls when the LLM is invoked:
    when both the type map and heuristics return below this confidence
    and an LLM client is configured, we ask the model.
    """

    def __init__(
        self,
        *,
        llm: Optional[_LLMClient] = None,
        llm_model: Optional[str] = None,
        confidence_threshold_for_llm: float = 0.7,
    ) -> None:
        self._llm = llm
        self._llm_model = llm_model
        self._llm_floor = max(0.0, min(1.0, float(confidence_threshold_for_llm)))

    async def classify(
        self,
        *,
        node_type: str,
        text: str,
        layer_hint: Optional[str] = None,
    ) -> LayerVerdict:
        # 1. Explicit hint wins when valid.
        if layer_hint:
            try:
                return LayerVerdict(
                    layer=Layer(layer_hint),
                    confidence=0.99,
                    rationale="explicit layer_hint",
                    source="type_map",
                )
            except ValueError:
                pass

        # 2. Type map.
        type_verdict = self._from_type(node_type)
        text = (text or "").strip()

        # 3. Heuristics.
        heuristic_verdict = self._from_heuristics(text) if text else None

        # 4. Decide which signal to trust.
        chosen: Optional[LayerVerdict] = None
        if (
            type_verdict is not None
            and (
                heuristic_verdict is None
                or type_verdict.confidence >= heuristic_verdict.confidence
            )
        ):
            chosen = type_verdict
        elif heuristic_verdict is not None:
            chosen = heuristic_verdict

        if chosen is not None and chosen.confidence >= self._llm_floor:
            return chosen

        # 5. Optional LLM upgrade.
        if self._llm is not None and self._llm_model and text:
            llm_verdict = await self._try_llm(node_type=node_type, text=text)
            if llm_verdict is not None:
                return llm_verdict

        if chosen is not None:
            return chosen

        return LayerVerdict(
            layer=Layer.RULE,
            confidence=0.4,
            rationale="default — no strong signal",
            source="default",
        )

    # ── Internals ───────────────────────────────────────────────

    @staticmethod
    def _from_type(node_type: str) -> Optional[LayerVerdict]:
        if not isinstance(node_type, str) or not node_type:
            return None
        layer = _TYPE_LAYER_MAP.get(node_type)
        if layer is None:
            return None
        # Confidence depends on how "leaf" the type is. UIScreen is
        # almost certainly experience; TranscriptSegment is rule-by-
        # default but only weakly so.
        ambiguous = {"TranscriptSegment", "KnowledgeCard"}
        confidence = 0.6 if node_type in ambiguous else 0.92
        return LayerVerdict(
            layer=layer,
            confidence=confidence,
            rationale=f"node_type={node_type} → {layer.value}",
            source="type_map",
        )

    @staticmethod
    def _from_heuristics(text: str) -> Optional[LayerVerdict]:
        if not text:
            return None
        for pattern, layer, conf in _HEURISTICS:
            if pattern.search(text):
                return LayerVerdict(
                    layer=layer,
                    confidence=conf,
                    rationale=f"heuristic match: {layer.value}",
                    source="heuristic",
                )
        return None

    async def _try_llm(
        self, *, node_type: str, text: str
    ) -> Optional[LayerVerdict]:
        assert self._llm is not None and self._llm_model is not None
        try:
            raw = await self._llm.chat_json(
                model=self._llm_model,
                messages=[
                    ChatMessage(role="system", content=_LLM_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {"node_type": node_type, "text": text[:1200]},
                            ensure_ascii=False,
                        ),
                    ),
                ],
                temperature=0.0,
            )
            verdict = LLMLayerPick.model_validate(raw)
            return LayerVerdict(
                layer=verdict.layer,
                confidence=verdict.confidence,
                rationale=verdict.rationale or "llm",
                source="llm",
            )
        except (ValidationError, ValueError, KeyError) as exc:
            logger.warning("atlas.layer_llm_invalid: %s", exc)
            return None
        except Exception as exc:  # transport / timeout
            logger.warning("atlas.layer_llm_failed: %s", exc)
            return None
