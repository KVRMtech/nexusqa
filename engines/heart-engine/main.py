"""
Nexus Heart Engine v0.2.0 — AI Reasoning & Intelligence.

The brain of Nexus. Takes redacted transcripts + visual data
from Backbone and:
1. Extracts business rules from conversations
2. Identifies edge cases and contradictions
3. Generates test cases (the "autonomous exploration")
4. Provides confidence scoring and guardrails

Uses Llama 3.1 70B (quantized) on-prem for reasoning.
NO data leaves the datacenter.

This is where "SME shows 1 flow → Nexus explores ALL flows" happens.

v0.2.0 — Modular refactor:
  app.extractors  → RuleExtractor, prompt templates, document analysis
  app.generators  → TestGenerator, FlowExplorer
  app.guardrails  → OutputValidator, ValidationResult
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
import json
import time
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from fastapi import Depends, HTTPException, BackgroundTasks
from pydantic import AliasChoices, BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.config import production_guard
from nexus_sdk.models import (
    NexusRequest, NexusResponse, JobResponse, JobStatus,
    BusinessRule, TestCase, TestStep, SourceReference, Confidence,
)
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent, fire_stub_alert
from nexus_sdk.llm import LLMConfig, LLMResponse, create_provider
from nexus_sdk.llm.base import LLMProvider
from nexus_sdk.llm.tiered import TieredProviderConfig, TieredLLMRouter

# ── Modular sub-packages ───────────────────────────────────────
from app.extractors import (
    RuleExtractor,
    RULE_EXTRACTION_SYSTEM,
    RULE_EXTRACTION_USER,
    DOCUMENT_ANALYSIS_SYSTEM,
)
from app.generators import (
    TestGenerator,
    TEST_GENERATION_SYSTEM,
    TEST_GENERATION_USER,
    FlowExplorer,
    EXPLORE_FLOWS_SYSTEM,
    EXPLORE_FLOWS_USER,
)
from app.guardrails import (
    OutputValidator,
    ValidationResult,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────

class HeartConfig(EngineConfig):
    engine_name: str = "heart"
    engine_port: int = 8004

    # LLM Backend: "ollama", "vllm", or "stub"
    llm_backend: str = Field(
        default="ollama",
        description="LLM backend: 'ollama' (recommended), 'vllm' (GPU), or 'stub' (development)",
    )

    # Ollama settings (works on CPU — recommended for on-prem)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama API URL",
        validation_alias=AliasChoices("HEART_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"),
    )
    ollama_model: str = Field(
        default="llama3.2:1b",
        description="Ollama model name (e.g., llama3.1:70b)",
        validation_alias="HEART_OLLAMA_MODEL",
    )

    # vLLM settings (GPU-only, high throughput)
    llm_model: str = "llama-3.1-70b-instruct"
    llm_model_path: str = "./models/llama-3.1-70b-q4"
    llm_device: str = "cuda"
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.1   # Low temp for deterministic rule extraction
    llm_context_window: int = 32768

    # Guardrails
    max_rules_per_extraction: int = 50
    min_confidence_threshold: float = 0.7
    require_source_reference: bool = True

    # Exploration settings
    max_edge_cases_per_rule: int = 10
    max_test_cases_per_rule: int = 20


# ─── Request/Response Models ──────────────────────────────────

class ExtractRulesRequest(NexusRequest):
    transcript: str = Field(..., description="Redacted transcript text")
    session_id: str = Field(..., description="KT session ID for source tracking")
    visual_context: Optional[dict] = Field(
        default=None, description="Visual context from Eyes engine"
    )
    existing_rules: list[str] = Field(
        default_factory=list,
        description="IDs of already-known rules to avoid duplicates",
    )


class ExtractRulesResponse(NexusResponse):
    rules: list[BusinessRule]
    edge_cases: list[dict] = Field(default_factory=list)
    contradictions: list[dict] = Field(default_factory=list)
    questions_for_sme: list[str] = Field(default_factory=list)
    guardrails: Optional[dict] = Field(
        default=None,
        description="Output validation results from guardrail checks",
    )


class GenerateTestsRequest(NexusRequest):
    rules: list[BusinessRule] = Field(..., description="Business rules to generate tests for")
    context: Optional[dict] = Field(default=None, description="Additional context (UI flows, etc.)")
    coverage_targets: list[str] = Field(
        default_factory=lambda: ["happy_path", "boundary", "negative", "edge_case"],
        description="Types of test coverage to generate",
    )


class GenerateTestsResponse(NexusResponse):
    test_cases: list[TestCase]
    coverage_summary: dict = Field(default_factory=dict)
    guardrails: Optional[dict] = Field(
        default=None,
        description="Output validation results from guardrail checks",
    )


class ExploreFlowsRequest(NexusRequest):
    """
    THE KEY CAPABILITY: Given a single demonstrated flow,
    autonomously explore all possible paths.
    """
    demonstrated_flow: dict = Field(..., description="The flow the SME showed")
    ui_screens: list[dict] = Field(default_factory=list, description="Known UI screens from Eyes")
    known_rules: list[str] = Field(default_factory=list, description="Known business rule descriptions")


class ExploreFlowsResponse(NexusResponse):
    explored_flows: list[dict] = Field(default_factory=list)
    new_paths_found: int = 0
    questions: list[str] = Field(default_factory=list)


class AskHeartRequest(NexusRequest):
    """Free-form question to Heart's reasoning engine."""
    question: str
    context: Optional[str] = None


class AskHeartResponse(NexusResponse):
    answer: str
    confidence: float
    sources: list[dict] = Field(default_factory=list)


class AnalyzeDocumentRequest(NexusRequest):
    """Analyze a document for rules, risks, and compliance concerns."""
    content: str
    document_type: Optional[str] = None


class AnalyzeDocumentResponse(NexusResponse):
    summary: str
    rules_found: int
    risks: list[dict] = Field(default_factory=list)
    compliance_flags: list[dict] = Field(default_factory=list)
    key_entities: list[str] = Field(default_factory=list)


# ─── LLM Wrapper — Pluggable Provider via SDK ─────────────────

class HeartLLM:
    """
    Heart engine LLM adapter.

    Uses the pluggable nexus_sdk.llm provider system. Any backend
    (OpenAI, Anthropic, Azure, Ollama, vLLM, custom) can be switched
    via LLM_PROVIDER / LLM_MODEL / LLM_API_KEY env vars.

    P1: Now supports TieredLLMRouter (Tier 1 → 2 → 3 failover)
    when per-engine tier env vars are set.  Falls back to single
    provider or stub mode when no tiers are configured.
    """

    def __init__(self, config: HeartConfig, event_bus=None):
        self.config = config
        self._router: Optional[TieredLLMRouter] = None
        self._provider: Optional[LLMProvider] = None
        self._backend: str = "stub"
        self._stub_fallback_count: int = 0
        self._event_bus = event_bus
        self._gpu_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)

    async def load_model(self):
        """Initialize the LLM provider from environment config."""
        self._gpu_semaphore = asyncio.Semaphore(1)

        # Resolve backend: env var LLM_BACKEND (legacy) or LLM_PROVIDER (new SDK)
        backend = os.getenv("LLM_BACKEND", "").lower() or self.config.llm_backend.lower()

        if backend == "stub":
            self._backend = "stub"
            logger.info("heart: LLM backend set to STUB mode (development)")
            return

        # P1: Try tiered provider system first (HEART_TIER1_PROVIDER, etc.)
        try:
            tier_config = TieredProviderConfig.from_engine("heart")
            if len(tier_config.active_tiers) > 0:
                self._router = TieredLLMRouter(tier_config)
                await self._router.initialize()
                self._backend = "tiered"
                logger.info(
                    "heart: Tiered LLM router initialized",
                    extra={"tiers": [t.tier.value for t in tier_config.active_tiers]},
                )
                production_guard("Heart LLM provider", available=True)
                return
        except Exception as e:
            logger.warning("heart: Tiered router init failed, trying single: %s", e)

        try:
            llm_config = LLMConfig()

            # Legacy bridge: if old LLM_BACKEND env is set, map it to new config
            if backend == "ollama" and not os.getenv("LLM_PROVIDER"):
                llm_config.provider = "ollama"
                llm_config.api_base_url = self.config.ollama_base_url
                llm_config.ollama_base_url = self.config.ollama_base_url
                llm_config.model = self.config.ollama_model
                llm_config.ollama_model = self.config.ollama_model
            elif backend == "vllm" and not os.getenv("LLM_PROVIDER"):
                llm_config.provider = "vllm"

            # Create and initialize the SDK provider
            self._provider = create_provider(llm_config)
            await self._provider.initialize()
            self._backend = llm_config.provider

            logger.info(
                "heart: LLM provider initialized via SDK",
                extra={
                    "provider": self._backend,
                    "model": llm_config.get_effective_model(),
                },
            )

        except Exception as e:
            logger.warning(
                "heart: LLM provider init failed, falling back to stub: %s", e,
            )
            self._provider = None
            self._backend = "stub"

        # Production guard: refuse stub mode in production environments
        production_guard(
            "Heart LLM provider",
            available=(self._backend != "stub"),
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate: ~4 characters per token for English text."""
        return max(1, len(text) // 4)

    def _enforce_context_budget(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None,
    ) -> str:
        """
        Truncate user_prompt if the combined prompt + output reservation
        exceeds the model's context window.  Returns (possibly shortened)
        user_prompt.
        """
        output_reserve = max_tokens or self.config.llm_max_tokens
        budget = self.config.llm_context_window - output_reserve

        system_tokens = self._estimate_tokens(system_prompt)
        user_tokens = self._estimate_tokens(user_prompt)
        total = system_tokens + user_tokens

        if total <= budget:
            return user_prompt

        allowed_user_tokens = max(budget - system_tokens, 256)
        # Convert back to characters (×4) — leave small margin
        allowed_chars = allowed_user_tokens * 4
        truncated = user_prompt[:allowed_chars]

        logger.warning(
            "heart.context_window_guard: prompt truncated "
            "(%d estimated tokens → %d budget, context_window=%d, output_reserve=%d)",
            total,
            budget,
            self.config.llm_context_window,
            output_reserve,
            extra={"original_chars": len(user_prompt), "truncated_chars": len(truncated)},
        )
        return truncated

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = None,
        max_tokens: int = None,
    ) -> str:
        """Generate text from the LLM. Returns raw string content."""
        user_prompt = self._enforce_context_budget(system_prompt, user_prompt, max_tokens)
        async with self._gpu_semaphore:
            # P1: Try tiered router first (multi-tier failover)
            if self._router and self._backend == "tiered":
                try:
                    response = await self._router.generate(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    if response.is_truncated():
                        logger.warning(
                            "heart.llm.truncated_response: output hit max_tokens limit "
                            "(finish_reason=%s, tokens=%d).",
                            response.finish_reason, response.total_tokens,
                        )
                    return response.content
                except Exception as e:
                    logger.warning("heart: Tiered router failed, trying single: %s", e)

            if self._provider and self._backend not in ("stub", "tiered"):
                try:
                    response: LLMResponse = await self._provider.generate(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    if response.is_truncated():
                        logger.warning(
                            "heart.llm.truncated_response: output hit max_tokens limit "
                            "(finish_reason=%s, tokens=%d). "
                            "Downstream JSON parsing may fail.",
                            response.finish_reason,
                            response.total_tokens,
                            extra={
                                "content_tail": response.content[-120:]
                                if response.content
                                else "",
                            },
                        )
                    return response.content
                except Exception as e:
                    logger.warning(
                        "heart: LLM provider generate failed, falling back to stub: %s", e,
                    )
                    return self._stub_generate(system_prompt, user_prompt)
            else:
                return self._stub_generate(system_prompt, user_prompt)

    async def shutdown(self):
        """Clean up provider resources."""
        if self._router:
            await self._router.shutdown()
        if self._provider:
            try:
                await self._provider.shutdown()
            except Exception:
                pass

    def _stub_generate(self, system_prompt: str, user_prompt: str) -> str:
        """Development stub returning structured JSON."""
        self._stub_fallback_count += 1
        logger.warning(
            "heart: stub fallback #%d (LLM backend=%s)",
            self._stub_fallback_count, self._backend,
        )
        fire_stub_alert(
            self._event_bus, "heart", "llm",
            fallback_count=self._stub_fallback_count,
            reason=f"LLM backend '{self._backend}' not available",
        )
        if "extract" in system_prompt.lower() and "rule" in system_prompt.lower():
            return json.dumps({
                "rules": [
                    {
                        "description": "[Stub] Premium calculation depends on age band and tobacco status",
                        "condition": "IF applicant_age >= 35 AND applicant_age <= 40 AND tobacco_status == 'smoker'",
                        "expected_result": "THEN premium_rate = base_rate * 1.75 (smoker surcharge)",
                        "domain": "underwriting",
                        "priority": "high",
                        "confidence": "high",
                    },
                    {
                        "description": "[Stub] Non-resident aliens require additional documentation",
                        "condition": "IF residency_status == 'non_resident_alien'",
                        "expected_result": "THEN require_forms = ['I-94', 'W-8BEN'] AND underwriting_level = 'full'",
                        "domain": "compliance",
                        "priority": "critical",
                        "confidence": "medium",
                    },
                ],
                "edge_cases": [
                    {
                        "description": "[Stub] What happens when applicant turns 35 mid-policy?",
                        "related_rule": "age band premium calculation",
                    }
                ],
                "contradictions": [],
                "questions_for_sme": [
                    "[Stub] Does the 1.75x smoker surcharge apply to e-cigarette users?",
                    "[Stub] Is the age band calculated from issue date or effective date?",
                ],
            })
        elif "test" in system_prompt.lower():
            return json.dumps({
                "test_cases": [
                    {
                        "name": "[Stub] Happy path: Standard 35yo non-smoker term life quote",
                        "description": "Verify premium calculation for standard profile",
                        "steps": [
                            {"action": "Navigate to new quote", "expected": "Quote form displayed"},
                            {"action": "Enter age: 35", "expected": "Age accepted"},
                            {"action": "Select Non-Smoker", "expected": "Rate updated"},
                            {"action": "Click Calculate", "expected": "Premium = base_rate * 1.0"},
                        ],
                        "priority": "high",
                        "type": "happy_path",
                    },
                    {
                        "name": "[Stub] Boundary: Age 34→35 band transition",
                        "description": "Verify rate changes at age band boundary",
                        "steps": [
                            {"action": "Enter age: 34", "expected": "Uses 30-34 rate band"},
                            {"action": "Change age to 35", "expected": "Uses 35-40 rate band"},
                            {"action": "Verify premium increased", "expected": "Premium reflects new band"},
                        ],
                        "priority": "high",
                        "type": "boundary",
                    },
                ],
            })
        elif "explore" in system_prompt.lower():
            return json.dumps({
                "explored_flows": [
                    {
                        "flow_name": "[Stub] Alternate path: Decline flow",
                        "description": "What happens when underwriting declines the application",
                        "steps": ["Submit application", "Underwriter reviews", "Decline decision", "Decline letter generated"],
                    },
                    {
                        "flow_name": "[Stub] Alternate path: Counter-offer",
                        "description": "When the insurer offers modified terms",
                        "steps": ["Submit application", "Underwriter reviews", "Counter-offer generated", "Applicant accepts/rejects"],
                    },
                ],
                "new_paths_found": 2,
                "questions": [
                    "[Stub] Is there a re-application path after decline?",
                    "[Stub] What's the maximum number of counter-offers allowed?",
                ],
            })
        else:
            return json.dumps({
                "answer": "[Stub] LLM model not loaded. Install vllm for real inference.",
                "confidence": 0.0,
            })


# ─── The Heart Engine v0.2.0 ──────────────────────────────────

class HeartEngine(NexusEngine):
    def __init__(self):
        self.cfg = HeartConfig()
        super().__init__(
            name="heart",
            version="0.2.0",
            config=self.cfg,
            description="AI Reasoning & Intelligence Engine",
        )
        self.llm = HeartLLM(self.cfg, event_bus=self.event_bus)
        # Sub-modules wired after startup (need prompt overrides)
        self.extractor: Optional[RuleExtractor] = None
        self.generator: Optional[TestGenerator] = None
        self.explorer: Optional[FlowExplorer] = None
        self.validator = OutputValidator(
            min_confidence=self.cfg.min_confidence_threshold,
            require_source=self.cfg.require_source_reference,
            max_rules=self.cfg.max_rules_per_extraction,
        )

    async def on_startup(self):
        """Load LLM model and reasoning prompts from plugins."""
        # Wire event bus into LLM after it's initialized
        self.llm._event_bus = self.event_bus
        await self.llm.load_model()

        # Report LLM mode to health endpoint
        self.health.set_mode("llm", self.llm._backend or "stub")

        # Load reasoning extensions from domain plugins
        self._prompt_overrides: dict[str, str] = {}
        try:
            reasoning_ext = self.plugin_registry.get_merged_reasoning()
            if reasoning_ext and reasoning_ext.prompt_templates:
                for pt in reasoning_ext.prompt_templates:
                    self._prompt_overrides[pt.template_id] = pt.system_prompt
                    if pt.user_prompt_template:
                        self._prompt_overrides[f"{pt.template_id}_user"] = pt.user_prompt_template
        except Exception:
            logger.debug("Plugin prompt overrides not available, using hardcoded prompts", exc_info=True)

        # Wire sub-modules with loaded prompt overrides
        self.extractor = RuleExtractor(
            self.llm,
            max_rules=self.cfg.max_rules_per_extraction,
            prompt_overrides=self._prompt_overrides,
        )
        self.generator = TestGenerator(
            self.llm, prompt_overrides=self._prompt_overrides,
        )
        self.explorer = FlowExplorer(
            self.llm, prompt_overrides=self._prompt_overrides,
        )

        if self.event_bus:
            await self.event_bus.subscribe(
                "shield.redaction.completed", self._handle_redacted_transcript
            )

    async def _handle_redacted_transcript(self, event: NexusEvent):
        """Auto-extract rules from newly redacted transcripts."""
        safe_text = event.data.get("safe_text", "")
        if not safe_text:
            return

        result = await self.extractor.extract_rules(
            transcript=safe_text,
            session_id=event.session_id or "",
            tenant_id=event.tenant_id,
        )

        if self.event_bus and result["rules"]:
            await self.event_bus.publish(NexusEvent(
                event_type="heart.rules.extracted",
                tenant_id=event.tenant_id,
                trace_id=event.trace_id,
                engine="heart",
                session_id=event.session_id,
                data={
                    "rules": [r.model_dump(mode="json") for r in result["rules"]],
                    "edge_cases": result["edge_cases"],
                    "rule_count": len(result["rules"]),
                },
            ))

    def register_routes(self, app):

        # ── Extract Business Rules ─────────────────────────────

        @app.post("/api/v1/heart/extract-rules", response_model=ExtractRulesResponse)
        async def extract_rules(
            req: ExtractRulesRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Extract business rules from a redacted transcript."""
            start = time.monotonic()

            result = await self.extractor.extract_rules(
                transcript=req.transcript,
                session_id=req.session_id,
                tenant_id=req.tenant_id,
                visual_context=req.visual_context,
            )

            # Run guardrails validation on extracted rules
            guardrails_result = None
            if result["rules"]:
                validation = self.validator.validate_rules_output({
                    "rules": [r.model_dump(mode="json") for r in result["rules"]],
                })
                guardrails_result = {
                    "valid": validation.valid,
                    "score": validation.score,
                    "severity": validation.severity.value,
                    "issues": validation.issues,
                }
                if validation.error_indices:
                    # Filter out rules that failed ERROR-level checks
                    filtered_count = len(validation.error_indices)
                    result["rules"] = [
                        r for i, r in enumerate(result["rules"])
                        if i not in set(validation.error_indices)
                    ]
                    logger.warning(
                        "heart.guardrails.rules_filtered",
                        extra={
                            "filtered_count": filtered_count,
                            "remaining": len(result["rules"]),
                            "issues": [i["message"] for i in validation.issues],
                        },
                    )
                    guardrails_result["filtered_count"] = filtered_count
                elif not validation.valid:
                    logger.warning(
                        "heart.guardrails.rules_validation_warning",
                        extra={
                            "issues": [i["message"] for i in validation.issues],
                            "score": validation.score,
                        },
                    )

            elapsed_ms = (time.monotonic() - start) * 1000

            # Emit event
            if self.event_bus and result["rules"]:
                await self.event_bus.publish(NexusEvent(
                    event_type="heart.rules.extracted",
                    tenant_id=req.tenant_id,
                    trace_id=req.trace_id,
                    engine="heart",
                    session_id=req.session_id,
                    data={
                        "rules": [r.model_dump(mode="json") for r in result["rules"]],
                        "edge_cases": result["edge_cases"],
                        "rule_count": len(result["rules"]),
                    },
                ))

            return ExtractRulesResponse(
                success=True,
                trace_id=req.trace_id,
                engine="heart",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                rules=result["rules"],
                edge_cases=result["edge_cases"] or [],
                contradictions=result["contradictions"] or [],
                questions_for_sme=[
                    q if isinstance(q, str) else q.get("question", str(q))
                    for q in (result["questions_for_sme"] or [])
                ],
                guardrails=guardrails_result,
            )

        # ── Generate Test Cases ────────────────────────────────

        @app.post("/api/v1/heart/generate-tests", response_model=GenerateTestsResponse)
        async def generate_tests(
            req: GenerateTestsRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate test cases from business rules."""
            start = time.monotonic()

            result = await self.generator.generate_tests(
                rules=req.rules,
                tenant_id=req.tenant_id,
                coverage_targets=req.coverage_targets,
                context=req.context,
            )

            # Run guardrails validation on generated test cases
            guardrails_result = None
            if result["test_cases"]:
                validation = self.validator.validate_tests_output({
                    "test_cases": [tc.model_dump(mode="json") for tc in result["test_cases"]],
                })
                guardrails_result = {
                    "valid": validation.valid,
                    "score": validation.score,
                    "severity": validation.severity.value,
                    "issues": validation.issues,
                }
                if not validation.valid:
                    logger.warning(
                        "heart.guardrails.tests_validation_warning",
                        extra={
                            "issues": [i["message"] for i in validation.issues],
                            "score": validation.score,
                        },
                    )

            elapsed_ms = (time.monotonic() - start) * 1000

            return GenerateTestsResponse(
                success=True,
                trace_id=req.trace_id,
                engine="heart",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                test_cases=result["test_cases"],
                coverage_summary=result["coverage_summary"],
                guardrails=guardrails_result,
            )

        # ── Explore All Flows (THE KEY ENDPOINT) ───────────────

        @app.post("/api/v1/heart/explore-flows", response_model=ExploreFlowsResponse)
        async def explore_flows(
            req: ExploreFlowsRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Given one demonstrated flow, autonomously explore
            ALL possible paths through the system.
            """
            start = time.monotonic()

            result = await self.explorer.explore(
                demonstrated_flow=req.demonstrated_flow,
                ui_screens=req.ui_screens or None,
                known_rules=req.known_rules or None,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return ExploreFlowsResponse(
                success=True,
                trace_id=req.trace_id,
                engine="heart",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                explored_flows=result["explored_flows"],
                new_paths_found=result["new_paths_found"],
                questions=result["questions"],
            )

        # ── Free-Form Question ─────────────────────────────────

        @app.post("/api/v1/heart/ask", response_model=AskHeartResponse)
        async def ask(
            req: AskHeartRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Ask Heart a free-form question using LLM reasoning."""
            start = time.monotonic()

            system = "You are an expert insurance QA analyst. Answer precisely and cite your reasoning."
            user_prompt = req.question
            if req.context:
                user_prompt = f"Context:\n{req.context}\n\nQuestion: {req.question}"

            response = await self.llm.generate(system, user_prompt)

            try:
                parsed = json.loads(response)
                answer = parsed.get("answer", response)
                confidence = parsed.get("confidence", 0.5)
            except json.JSONDecodeError:
                answer = response
                confidence = 0.5

            elapsed_ms = (time.monotonic() - start) * 1000

            return AskHeartResponse(
                success=True,
                trace_id=req.trace_id,
                engine="heart",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                answer=answer,
                confidence=confidence,
            )

        # ── Document Analysis ──────────────────────────────────

        @app.post("/api/v1/heart/analyze", response_model=AnalyzeDocumentResponse)
        async def analyze_document(
            req: AnalyzeDocumentRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Analyze a document for rules, risks, and compliance."""
            start = time.monotonic()

            result = await self.extractor.analyze_document(
                content=req.content,
                document_type=req.document_type,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return AnalyzeDocumentResponse(
                success=True,
                trace_id=req.trace_id,
                engine="heart",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                summary=result["summary"],
                rules_found=result["rules_found"],
                risks=result["risks"],
                compliance_flags=result["compliance_flags"],
                key_entities=result["key_entities"],
            )


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = HeartEngine()
    engine.run()


if __name__ == "__main__":
    main()
