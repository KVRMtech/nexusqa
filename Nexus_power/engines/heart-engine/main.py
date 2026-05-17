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
import re
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
from nexus_sdk.worker import GPUWorkerMixin

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

    # GPU concurrency (raise on multi-GPU / vLLM setups)
    gpu_concurrency: int = Field(
        default=1,
        description="Max concurrent LLM inferences per pod",
        validation_alias="HEART_GPU_CONCURRENCY",
    )


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
        self._gpu_semaphore: asyncio.Semaphore = asyncio.Semaphore(config.gpu_concurrency)

    async def load_model(self):
        """Initialize the LLM provider from environment config."""
        self._gpu_semaphore = asyncio.Semaphore(self.config.gpu_concurrency)

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
                    if os.getenv("NEXUS_ALLOW_DEGRADED_MODE", "false").lower() != "true":
                        raise RuntimeError(
                            "Heart LLM generation failed and degraded mode is disabled"
                        ) from e
                    return self._stub_generate(system_prompt, user_prompt)
            else:
                if os.getenv("NEXUS_ALLOW_DEGRADED_MODE", "false").lower() != "true":
                    raise RuntimeError(
                        "Heart LLM provider is unavailable and degraded mode is disabled"
                    )
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


# ─── Deterministic Visual-Strict Strategy Generators (P2) ────
# These are pure-Python graph algorithms — no LLM involvement.
# They consume the visual evidence graph and emit test_case dicts
# in the same shape as the LLM-driven generators.

_ERROR_OCR_RX = re.compile(
    r"\b(error|errors|invalid|failed|failure|denied|forbidden|"
    r"required|missing|cannot|unable|warning|alert|expired|"
    r"rejected|exceeded)\b",
    re.IGNORECASE,
)


def _pick_control_for_edge(edge: dict, ctrl_by_scene: dict) -> str:
    """Return the most relevant control_id for an edge — prefer the edge's
    own trigger_control_id, fall back to the highest-confidence
    automation-ready control in the from_scene.
    """
    trigger = edge.get("trigger_control_id")
    if trigger:
        return trigger
    from_id = edge.get("from_scene_id", "")
    ctrls = ctrl_by_scene.get(from_id, [])
    ready = sorted(
        (c for c in ctrls if c.get("automation_ready")),
        key=lambda c: -(c.get("selector_confidence") or 0),
    )
    if ready:
        return ready[0].get("control_id", "") or ""
    return ""


def _control_label(control_id: str, scene_id: str, ctrl_by_scene: dict) -> str:
    """Resolve a control_id back to its human-readable label within a scene."""
    if not control_id:
        return ""
    for c in ctrl_by_scene.get(scene_id, []):
        if c.get("control_id") == control_id:
            return (
                c.get("label_text")
                or c.get("display_label")
                or c.get("element_type")
                or ""
            )
    return ""


def _scene_label(scene: dict) -> str:
    """Best-effort human-readable name for a scene."""
    state = scene.get("scene_state_summary") or {}
    return (
        state.get("screen_title")
        or scene.get("screen_name")
        or f"Scene {scene.get('scene_index', '?')}"
    )


def _edge_confidence(edge: dict) -> float:
    """Numeric confidence we can place in an edge's action grounding."""
    return float(
        edge.get("action_confidence")
        or edge.get("evidence_confidence")
        or 0.7
    )


def _gen_state_explorer(
    *,
    scenes: list,
    edges: list,
    scene_by_id: dict,
    ctrl_by_scene: dict,
    max_paths: int = 5,
    max_depth: int = 8,
) -> list[dict]:
    """G10: Emit one test per distinct terminal scene reachable from an entry
    point, walking only ``action_confirmed_transition`` edges. Pure BFS — no
    LLM. Every step cites real scene_id / control_id / edge_id."""
    # Build adjacency for action-confirmed edges
    out_edges_by_scene: dict[str, list[dict]] = {}
    in_count: dict[str, int] = {}
    for e in edges:
        if e.get("edge_type") != "action_confirmed_transition":
            continue
        from_id = e.get("from_scene_id", "")
        to_id = e.get("to_scene_id", "")
        if not from_id or not to_id:
            continue
        out_edges_by_scene.setdefault(from_id, []).append(e)
        in_count[to_id] = in_count.get(to_id, 0) + 1
        in_count.setdefault(from_id, 0)

    if not out_edges_by_scene:
        return []

    # Entry scenes: have outgoing edges but no incoming confirmed edges
    entry_scenes = [
        s for s in scenes
        if s["scene_id"] in out_edges_by_scene
        and in_count.get(s["scene_id"], 0) == 0
    ]
    # Fallback for cyclic graphs: pick the lowest-scene_index node with outgoing
    if not entry_scenes:
        with_out = [s for s in scenes if s["scene_id"] in out_edges_by_scene]
        if not with_out:
            return []
        entry_scenes = [min(with_out, key=lambda s: s.get("scene_index", 0))]

    test_cases: list[dict] = []
    visited_terminals: set[str] = set()

    for entry in entry_scenes:
        # DFS up to max_depth; emit a test each time we hit a unique terminal
        stack: list[tuple[str, list[dict]]] = [(entry["scene_id"], [])]
        while stack and len(test_cases) < max_paths:
            scene_id, path = stack.pop()
            outgoing = out_edges_by_scene.get(scene_id, [])
            # Terminate if no outgoing or depth budget exhausted
            if not outgoing or len(path) >= max_depth:
                if not path:
                    continue
                terminal_id = path[-1].get("to_scene_id", "")
                if not terminal_id or terminal_id in visited_terminals:
                    continue
                visited_terminals.add(terminal_id)

                steps: list[dict] = []
                for step_num, edge in enumerate(path, start=1):
                    from_id = edge.get("from_scene_id", "")
                    ctrl_id = _pick_control_for_edge(edge, ctrl_by_scene)
                    ctrl_label = _control_label(ctrl_id, from_id, ctrl_by_scene)
                    to_scene = scene_by_id.get(edge.get("to_scene_id", ""), {})
                    expected = (to_scene.get("ocr_text") or "")[:160].strip()
                    summary = edge.get("primary_action_summary") or {}
                    action_label = (
                        summary.get("action_label")
                        or f"{summary.get('action_kind') or edge.get('action_type') or 'interact'} "
                        f"{ctrl_label}".strip()
                    )
                    steps.append({
                        "step_number": step_num,
                        "action": action_label,
                        "target_element": ctrl_label,
                        "expected_output": expected,
                        "input_data": "",
                        "evidence_scene_id": from_id,
                        "evidence_control_id": ctrl_id,
                        "evidence_edge_id": edge.get("edge_id", ""),
                        "proof_confidence": _edge_confidence(edge),
                    })

                terminal = scene_by_id.get(terminal_id, {})
                test_cases.append({
                    "title": f"Reach state: {_scene_label(terminal)}",
                    "strategy": "state_explorer",
                    "steps": steps,
                })
                continue

            # Continue BFS without revisiting edges (prevents infinite loops)
            for edge in outgoing:
                if any(p.get("edge_id") == edge.get("edge_id") for p in path):
                    continue
                stack.append((edge.get("to_scene_id", ""), path + [edge]))

    return test_cases


def _gen_cross_app(
    *,
    scenes: list,
    edges: list,
    scene_by_id: dict,
    ctrl_by_scene: dict,
) -> list[dict]:
    """G11: Emit one test per distinct cross-app boundary. Detects edges
    marked ``edge_type=app_switch`` or with ``intra_app=False``. Each
    test asserts the destination app's first scene appears after the
    trigger action."""
    seen_boundaries: set[tuple[str, str]] = set()
    test_cases: list[dict] = []
    for edge in edges:
        is_cross = (
            edge.get("edge_type") == "app_switch"
            or edge.get("intra_app") is False
        )
        if not is_cross:
            continue
        from_scene = scene_by_id.get(edge.get("from_scene_id", ""), {})
        to_scene = scene_by_id.get(edge.get("to_scene_id", ""), {})
        from_app = from_scene.get("app_instance_id") or ""
        to_app = to_scene.get("app_instance_id") or ""
        if not from_app or not to_app or from_app == to_app:
            continue
        key = (from_app, to_app)
        if key in seen_boundaries:
            continue
        seen_boundaries.add(key)

        ctrl_id = _pick_control_for_edge(edge, ctrl_by_scene)
        ctrl_label = _control_label(ctrl_id, edge.get("from_scene_id", ""), ctrl_by_scene)
        expected = (to_scene.get("ocr_text") or "")[:160].strip()
        summary = edge.get("primary_action_summary") or {}
        action_label = (
            summary.get("action_label")
            or f"Trigger app switch via {ctrl_label or 'observed action'}"
        )
        test_cases.append({
            "title": f"Cross-app journey: {_scene_label(from_scene)} → {_scene_label(to_scene)}",
            "strategy": "cross_app",
            "steps": [{
                "step_number": 1,
                "action": action_label,
                "target_element": ctrl_label,
                "expected_output": expected,
                "input_data": "",
                "evidence_scene_id": edge.get("from_scene_id", ""),
                "evidence_control_id": ctrl_id,
                "evidence_edge_id": edge.get("edge_id", ""),
                "proof_confidence": _edge_confidence(edge),
            }],
        })
    return test_cases


def _derive_preconditions(first_scene: dict) -> list[str]:
    """G3: Derive preconditions from the first cited scene's state — login
    status, app context, URL/domain. No LLM, no stubs."""
    if not first_scene:
        return []
    pre: list[str] = []
    state = first_scene.get("scene_state_summary") or {}
    app_label = state.get("application_label") or first_scene.get("screen_name")
    if app_label:
        pre.append(f"User is on {app_label}")
    url = first_scene.get("detected_url")
    if url:
        pre.append(f"URL: {url}")
    domain = state.get("domain")
    if domain and domain not in (url or ""):
        pre.append(f"Domain: {domain}")
    # Login heuristic: presence of "Sign out", "Log out", "Welcome <name>" in OCR
    ocr = (first_scene.get("ocr_text") or "").lower()
    if any(kw in ocr for kw in ("sign out", "log out", "logout", "welcome,", "welcome back")):
        pre.append("User is authenticated")
    return pre


def _derive_expected_outcome(last_scene: dict) -> str:
    """G4: Derive expected_outcome from the last cited scene's OCR/state."""
    if not last_scene:
        return ""
    state = last_scene.get("scene_state_summary") or {}
    title = state.get("screen_title") or last_scene.get("screen_name") or ""
    screen_type = (state.get("screen_type") or "").strip()
    ocr = (last_scene.get("ocr_text") or "").strip()
    snippet = ocr[:140].replace("\n", " ").strip()
    parts: list[str] = []
    if title:
        parts.append(f'"{title}" screen visible')
    if screen_type and screen_type not in ("unknown", ""):
        parts.append(f"({screen_type})")
    if snippet:
        parts.append(f'— OCR contains: "{snippet}"')
    return " ".join(parts).strip() or ""


_CONTROL_RISK_MAP = {
    "text_field": "input validation",
    "input": "input validation",
    "textarea": "input validation",
    "password": "credential handling",
    "email": "email format validation",
    "dropdown": "selection state",
    "select": "selection state",
    "checkbox": "toggle state",
    "radio": "selection state",
    "date": "date boundary handling",
    "datetime": "date boundary handling",
    "number": "numeric range",
    "file": "file upload",
    "submit": "form submission",
    "button": "action triggering",
    "link": "navigation",
}


def _derive_risk_areas(test_case: dict, ctrl_by_scene: dict) -> list[str]:
    """G6: Derive risk_areas_addressed by inspecting the element_types of
    every cited control."""
    risks: set[str] = set()
    for step in test_case.get("steps", []):
        cid = step.get("evidence_control_id")
        sid = step.get("evidence_scene_id")
        if not cid or not sid:
            continue
        for c in ctrl_by_scene.get(sid, []):
            if c.get("control_id") != cid:
                continue
            element_type = (c.get("element_type") or "").lower()
            risk = _CONTROL_RISK_MAP.get(element_type)
            if risk:
                risks.add(risk)
            break
    # Strategy-specific risks
    strategy = test_case.get("strategy", "")
    if strategy == "negative":
        risks.add("error path validation")
    elif strategy == "boundary":
        risks.add("boundary value handling")
    elif strategy == "cross_app":
        risks.add("cross-application state continuity")
    elif strategy == "error_state":
        risks.add("error UI surfacing")
    return sorted(risks)


def _derive_workflow_steps_covered(test_case: dict, scene_by_id: dict) -> list[int]:
    """G5: Unique 1-based scene_index list for every cited scene."""
    idxs: set[int] = set()
    for step in test_case.get("steps", []):
        sid = step.get("evidence_scene_id")
        if not sid:
            continue
        scene = scene_by_id.get(sid)
        if scene and scene.get("scene_index") is not None:
            idxs.add(int(scene["scene_index"]) + 1)
    return sorted(idxs)


def _enrich_test_case(
    test_case: dict,
    *,
    scene_by_id: dict,
    ctrl_by_scene: dict,
) -> dict:
    """Populate preconditions / expected_outcome / workflow_steps_covered /
    risk_areas_addressed for a test_case. All values are derived from the
    visual evidence graph — no stubs, no LLM.
    """
    steps = test_case.get("steps") or []
    first_scene = scene_by_id.get(
        (steps[0].get("evidence_scene_id") if steps else "") or "", {}
    )
    last_step_scene_id = ""
    for step in reversed(steps):
        sid = step.get("evidence_scene_id")
        if sid:
            last_step_scene_id = sid
            break
    last_scene = scene_by_id.get(last_step_scene_id, {})

    test_case["preconditions"] = _derive_preconditions(first_scene)
    test_case["expected_outcome"] = _derive_expected_outcome(last_scene)
    test_case["workflow_steps_covered"] = _derive_workflow_steps_covered(test_case, scene_by_id)
    test_case["risk_areas_addressed"] = _derive_risk_areas(test_case, ctrl_by_scene)
    return test_case


def _gen_error_state(
    *,
    scenes: list,
    edges: list,
    scene_by_id: dict,
    ctrl_by_scene: dict,
) -> list[dict]:
    """G12: Emit one test per scene whose OCR or screen_type marks it as an
    error/validation state, asserting the error appears after a grounded
    trigger action. Only emits when (a) the scene shows error content AND
    (b) at least one incoming confirmed edge exists to ground the trigger.
    """
    incoming_by_scene: dict[str, list[dict]] = {}
    for e in edges:
        if e.get("edge_type") != "action_confirmed_transition":
            continue
        incoming_by_scene.setdefault(e.get("to_scene_id", ""), []).append(e)

    test_cases: list[dict] = []
    for scene in scenes:
        sid = scene["scene_id"]
        state = scene.get("scene_state_summary") or {}
        screen_type = (state.get("screen_type") or "").lower()
        ocr = scene.get("ocr_text") or ""
        ocr_match = _ERROR_OCR_RX.search(ocr) if ocr else None
        if screen_type != "error" and not ocr_match:
            continue

        incoming = incoming_by_scene.get(sid, [])
        if not incoming:
            # Can't ground the trigger — skip rather than fabricate
            continue
        trigger = max(incoming, key=_edge_confidence)

        ctrl_id = _pick_control_for_edge(trigger, ctrl_by_scene)
        ctrl_label = _control_label(ctrl_id, trigger.get("from_scene_id", ""), ctrl_by_scene)

        if ocr_match:
            kw = ocr_match.group(0)
            start = max(0, ocr_match.start() - 20)
            end = min(len(ocr), ocr_match.end() + 80)
            excerpt = ocr[start:end].strip()
        else:
            kw = "error"
            excerpt = (ocr or "Error UI displayed")[:160].strip()

        test_cases.append({
            "title": f"Error state appears: '{kw}' on {_scene_label(scene)}",
            "strategy": "error_state",
            "steps": [{
                "step_number": 1,
                "action": f"Trigger via {ctrl_label or 'observed action'}",
                "target_element": ctrl_label,
                "expected_output": excerpt,
                "input_data": "",
                "evidence_scene_id": trigger.get("from_scene_id", ""),
                "evidence_control_id": ctrl_id,
                "evidence_edge_id": trigger.get("edge_id", ""),
                "proof_confidence": _edge_confidence(trigger),
            }],
        })
    return test_cases


# ─── The Heart Engine v0.2.0 ──────────────────────────────────

class HeartEngine(NexusEngine, GPUWorkerMixin):
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

        # ── Register engine-specific Prometheus metrics ──
        from nexus_sdk.observability.metrics import get_metrics
        m = get_metrics()
        if m:
            self._m_llm_requests = m.custom_counter(
                "heart_llm_requests_total",
                "Total LLM inference requests",
                labels=["tier", "provider", "status"],
            )
            self._m_llm_latency = m.custom_histogram(
                "heart_llm_latency_seconds",
                "LLM inference latency",
                labels=["tier", "provider"],
                buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
            )
            self._m_tier_failovers = m.custom_counter(
                "heart_tier_failover_total",
                "Number of LLM tier failovers",
                labels=["from_tier", "to_tier"],
            )
        else:
            self._m_llm_requests = None
            self._m_llm_latency = None
            self._m_tier_failovers = None

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

        # ── GPU Job Queue (Redis Streams) ──────────────────────
        # Heart supports worker mode: when ENGINE_MODE=worker or hybrid,
        # starts a background loop that consumes jobs from Redis Streams.
        await self.init_worker_queue()
        if self._job_queue and self._job_queue.is_connected:
            self.register_queue_routes(self.app)

        # Start worker loop in worker/hybrid mode for async job processing
        if self.is_worker_mode:
            self.start_worker_loop(
                self._process_queued_job,
                gpu_semaphore=self.llm._gpu_semaphore,
            )

        # P2 Fix: Warmup inference — prime LLM connection so the first
        # real request doesn't pay cold-start latency.
        if self.llm._backend not in ("stub", "unknown", ""):
            try:
                await asyncio.wait_for(
                    self.llm.generate(
                        system_prompt="You are a health check.",
                        user_prompt="Respond with: OK",
                        max_tokens=4,
                        temperature=0.0,
                    ),
                    timeout=30.0,
                )
                logger.info("heart.warmup.ok")
            except Exception as e:
                logger.warning("heart.warmup.failed: %s (non-blocking)", e)

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

    async def _process_queued_job(self, job: dict):
        """
        Process a single job from the Redis Streams queue.

        Job payload contains:
            job_type: "extract_rules" | "generate_tests" | "explore_flows"
            request:  The original request data (serialised dict)
            tenant_id, trace_id, session_id: Provenance fields
        """
        job_id = job.get("job_id", "unknown")
        payload = job.get("payload", job)
        job_type = payload.get("job_type", "unknown")
        request_data = payload.get("request", {})

        logger.info("heart.worker: processing job=%s type=%s", job_id, job_type)

        try:
            if job_type == "extract_rules":
                result = await self.extractor.extract_rules(
                    transcript=request_data["transcript"],
                    session_id=request_data.get("session_id", ""),
                    tenant_id=request_data.get("tenant_id", ""),
                    visual_context=request_data.get("visual_context"),
                )
                # Serialize BusinessRule objects for storage
                result["rules"] = [
                    r.model_dump(mode="json") if hasattr(r, "model_dump") else r
                    for r in result.get("rules", [])
                ]
                await self.job_store.update_job(
                    job_id, status="completed", result=result,
                )

            elif job_type == "generate_tests":
                # Reconstruct BusinessRule objects from dicts
                rules = [
                    BusinessRule(**r) if isinstance(r, dict) else r
                    for r in request_data.get("rules", [])
                ]
                result = await self.generator.generate_tests(
                    rules=rules,
                    tenant_id=request_data.get("tenant_id", ""),
                    coverage_targets=request_data.get("coverage_targets",
                        ["happy_path", "boundary", "negative", "edge_case"]),
                    context=request_data.get("context"),
                )
                result["test_cases"] = [
                    tc.model_dump(mode="json") if hasattr(tc, "model_dump") else tc
                    for tc in result.get("test_cases", [])
                ]
                await self.job_store.update_job(
                    job_id, status="completed", result=result,
                )

            elif job_type == "explore_flows":
                result = await self.explorer.explore(
                    demonstrated_flow=request_data["demonstrated_flow"],
                    ui_screens=request_data.get("ui_screens"),
                    known_rules=request_data.get("known_rules"),
                )
                await self.job_store.update_job(
                    job_id, status="completed", result=result,
                )

            else:
                raise ValueError(f"Unknown job type: {job_type}")

            logger.info("heart.worker: job=%s completed", job_id)

        except Exception as exc:
            logger.error("heart.worker: job=%s failed: %s", job_id, exc, exc_info=True)
            await self.job_store.update_job(
                job_id, status="failed", error=str(exc),
            )
            raise  # Re-raise so worker loop can nack

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

        # ── Async Job Submission Endpoints ─────────────────────
        # These endpoints enqueue work to Redis Streams and return a
        # job_id immediately.  Workers consume the queue in the
        # background.  Callers poll GET /api/v1/heart/jobs/{job_id}
        # for results.

        @app.post("/api/v1/heart/extract-rules/async", response_model=JobResponse)
        async def extract_rules_async(
            req: ExtractRulesRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Enqueue rule extraction for async processing."""
            job_id = str(uuid.uuid4())
            payload = {
                "job_type": "extract_rules",
                "request": req.model_dump(mode="json"),
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            }

            await self.job_store.set_job(job_id, {
                "status": "queued",
                "job_type": "extract_rules",
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            })

            enqueued = await self.enqueue_gpu_job(job_id, payload)
            if not enqueued:
                raise HTTPException(
                    status_code=503,
                    detail="Job queue unavailable — use the sync endpoint instead",
                )

            return JobResponse(
                job_id=job_id,
                status=JobStatus.QUEUED,
                engine="heart",
            )

        @app.post("/api/v1/heart/generate-tests/async", response_model=JobResponse)
        async def generate_tests_async(
            req: GenerateTestsRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Enqueue test generation for async processing."""
            job_id = str(uuid.uuid4())
            payload = {
                "job_type": "generate_tests",
                "request": req.model_dump(mode="json"),
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            }

            await self.job_store.set_job(job_id, {
                "status": "queued",
                "job_type": "generate_tests",
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            })

            enqueued = await self.enqueue_gpu_job(job_id, payload)
            if not enqueued:
                raise HTTPException(
                    status_code=503,
                    detail="Job queue unavailable — use the sync endpoint instead",
                )

            return JobResponse(
                job_id=job_id,
                status=JobStatus.QUEUED,
                engine="heart",
            )

        @app.post("/api/v1/heart/explore-flows/async", response_model=JobResponse)
        async def explore_flows_async(
            req: ExploreFlowsRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Enqueue flow exploration for async processing."""
            job_id = str(uuid.uuid4())
            payload = {
                "job_type": "explore_flows",
                "request": req.model_dump(mode="json"),
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            }

            await self.job_store.set_job(job_id, {
                "status": "queued",
                "job_type": "explore_flows",
                "tenant_id": req.tenant_id,
                "trace_id": req.trace_id,
            })

            enqueued = await self.enqueue_gpu_job(job_id, payload)
            if not enqueued:
                raise HTTPException(
                    status_code=503,
                    detail="Job queue unavailable — use the sync endpoint instead",
                )

            return JobResponse(
                job_id=job_id,
                status=JobStatus.QUEUED,
                engine="heart",
            )

        # ── Generate Tests from Visual Evidence (Phase 9) ─────

        @app.post("/api/v1/heart/generate-tests-from-visual")
        async def generate_tests_from_visual(
            req: dict,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate E2E test scenarios grounded in visual evidence (Phase 9).

            Two operating modes:
            - ``visual_strict``: Inputs are only scenes, controls, and flow
              edges from the visual evidence pipeline.  The Brain's
              match_visual_to_workflows() step is bypassed.  Every generated
              scenario step cites an EvidenceControl or VisualFlowEdge.
            - ``multimodal`` (default): Uses all available signals.

            Phase 9 / P2 — multi-strategy generation:
            The ``strategies`` parameter selects which generators run.  Each
            strategy produces 1-3 grounded test cases tagged with its name.

            Available strategies (visual_strict):
              - ``happy_path``  — follow the observed flow as-demoed
              - ``variant``     — same flow with different input values
                                  (vary text content into observed input fields)
              - ``negative``    — skip a required field; expect validation error
              - ``boundary``    — exercise min/max/empty for numeric/date fields

            Request schema:
                artifact_id:       str   — canonical artifact id
                session_id:        str   — session scope
                evidence_mode:     str   — "visual_strict" | "multimodal"
                approved_edge_ids: list  — optional whitelist of approved edge_ids
                strategies:        list  — subset of available strategies
                                           (default: ["happy_path"])

            Returns:
                {"success", "artifact_id", "session_id", "evidence_mode",
                 "test_cases" (each tagged with "strategy"),
                 "unproven_steps", "coverage_report", "processing_time_ms"}
            """
            start = time.monotonic()

            artifact_id = req.get("artifact_id", "")
            session_id = req.get("session_id", "")
            evidence_mode = req.get("evidence_mode", "multimodal")
            approved_edge_ids: list[str] = req.get("approved_edge_ids") or []
            # P2: which strategies to run. Default = happy_path only (back-compat).
            requested_strategies: list[str] = req.get("strategies") or ["happy_path"]
            # Restrict to known strategies and dedupe while preserving order.
            # LLM-driven: happy_path, variant, negative, boundary
            # Deterministic: state_explorer, cross_app, error_state
            _llm_strategies = {"happy_path", "variant", "negative", "boundary"}
            _deterministic_strategies = {"state_explorer", "cross_app", "error_state"}
            _known_strategies = _llm_strategies | _deterministic_strategies
            strategies: list[str] = []
            for s in requested_strategies:
                if s in _known_strategies and s not in strategies:
                    strategies.append(s)
            if not strategies:
                strategies = ["happy_path"]
            llm_strategies_to_run = [s for s in strategies if s in _llm_strategies]
            deterministic_strategies_to_run = [s for s in strategies if s in _deterministic_strategies]

            if not artifact_id:
                raise HTTPException(status_code=422, detail="artifact_id is required")

            # Fetch visual evidence graph from Platform API
            _platform_url = os.environ.get("PLATFORM_API_URL", "http://localhost:8000")
            graph: dict = {}
            try:
                import httpx as _httpx
                auth_header = (
                    f"Bearer {user.token}"
                    if hasattr(user, "token") and user.token
                    else ""
                )
                async with _httpx.AsyncClient(timeout=15.0) as http:
                    resp = await http.get(
                        f"{_platform_url}/api/v1/artifacts/{artifact_id}/visual-evidence-graph",
                        headers={"Authorization": auth_header} if auth_header else {},
                    )
                    if resp.status_code == 200:
                        graph = resp.json()
            except Exception as exc:
                import logging
                logging.getLogger("heart").warning(
                    f"heart.visual_graph_fetch_failed: {exc}"
                )

            scenes: list[dict] = graph.get("scenes", [])
            # Phase 8+: API returns controls_by_scene dict (scene_id → list);
            # flatten to a list for digest building while keeping the indexed form.
            controls_by_scene_raw: dict[str, list[dict]] = graph.get("controls_by_scene", {})
            controls: list[dict] = [
                ctrl
                for ctrl_list in controls_by_scene_raw.values()
                for ctrl in ctrl_list
            ]
            edges: list[dict] = graph.get("edges", [])

            if approved_edge_ids:
                edges = [e for e in edges if e.get("edge_id") in approved_edge_ids]

            scene_by_id = {s["scene_id"]: s for s in scenes}
            # Build per-scene index from the already-indexed dict (fall back to flat list)
            ctrl_by_scene: dict[str, list[dict]] = {
                sid: list(clist) for sid, clist in controls_by_scene_raw.items()
            } if controls_by_scene_raw else {}
            for ctrl in controls:
                sid = ctrl.get("scene_id", "")
                if sid and sid not in ctrl_by_scene:
                    ctrl_by_scene.setdefault(sid, []).append(ctrl)

            if evidence_mode == "visual_strict":
                working_edges = [
                    e for e in edges
                    if e.get("edge_type") == "action_confirmed_transition"
                ]
                unproven_edges = [
                    e for e in edges
                    if e.get("edge_type") != "action_confirmed_transition"
                ]
            else:
                working_edges = edges
                unproven_edges = []

            if not working_edges and not scenes:
                elapsed_ms = (time.monotonic() - start) * 1000
                return {
                    "success": True,
                    "artifact_id": artifact_id,
                    "session_id": session_id,
                    "evidence_mode": evidence_mode,
                    "test_cases": [],
                    "unproven_steps": [],
                    "coverage_report": {
                        "total_edges": len(edges),
                        "proven_steps": 0,
                        "unproven_steps": 0,
                        "automation_ready_pct": 0.0,
                    },
                    "processing_time_ms": round(elapsed_ms, 2),
                    "warning": "No visual transitions available",
                }

            # ── Build LLM prompt (Phase 9 visual_strict prompt) ──────────
            auto_ready_count = sum(1 for c in controls if c.get("automation_ready"))

            if evidence_mode == "visual_strict":
                # P2: per-strategy guidance — ONLY for LLM-driven strategies.
                # Deterministic strategies (state_explorer, cross_app, error_state)
                # run as separate generators after the LLM call.
                strategy_guidance: list[str] = []
                if "happy_path" in llm_strategies_to_run:
                    strategy_guidance.append(
                        "  happy_path: 1 test case following the observed flow end-to-end. "
                        "Use the input values observed in OCR (no variations)."
                    )
                if "variant" in llm_strategies_to_run:
                    strategy_guidance.append(
                        "  variant: 1-2 test cases that follow the same flow as happy_path "
                        "but substitute different valid input values for text fields. "
                        "Vary only fields whose control element_type is 'text_field' or 'input'. "
                        "For each variant, include the substituted value in the step's "
                        "'input_data' field. Keep all selectors and assertions visual-graph-grounded."
                    )
                if "negative" in llm_strategies_to_run:
                    strategy_guidance.append(
                        "  negative: 1 test case that intentionally skips a required form "
                        "field (or submits with empty value) and asserts the resulting "
                        "validation error scene. ONLY emit if the visual graph contains a "
                        "scene whose OCR shows error/validation text ('required', "
                        "'must', '*' marker, red text). Otherwise emit nothing for negative."
                    )
                if "boundary" in llm_strategies_to_run:
                    strategy_guidance.append(
                        "  boundary: 1 test case using min/max/empty values for the "
                        "first numeric or date control observed in the flow. Cite the "
                        "scene where the field appears AND any scene whose OCR shows "
                        "the resulting validation or success state."
                    )

                system_prompt = (
                    "You are a QA automation engineer producing grounded Playwright test cases. "
                    "Generate test cases using these strategies — and ONLY these strategies:\n"
                    + "\n".join(strategy_guidance)
                    + "\n\nUNIVERSAL RULES:\n"
                    "- Each test step MUST reference a scene_id from the evidence graph.\n"
                    "- Each action step MUST reference a control_id with automation_ready=true.\n"
                    "- Expected results MUST quote OCR-confirmed text from the linked to_scene.\n"
                    "- Tag every test_case with its strategy ('happy_path' | 'variant' | "
                    "'negative' | 'boundary').\n"
                    "- Steps without a grounded scene_id AND control_id go to unproven_steps with explicit reason.\n"
                    "- Do NOT infer steps from context. Only generate steps for which evidence exists.\n"
                    "- If a strategy has no evidence to support it, emit zero test_cases for it (do not invent).\n"
                    "Return ONLY valid JSON: "
                    "{\"test_cases\": [{\"title\": str, \"strategy\": str, \"steps\": ["
                    "{\"action\": str, \"target_element\": str, \"expected_output\": str, "
                    "\"input_data\": str, "
                    "\"evidence_scene_id\": str, \"evidence_control_id\": str, "
                    "\"evidence_edge_id\": str, \"proof_confidence\": float}]}], "
                    "\"unproven_steps\": [{\"description\": str, \"reason\": str}]}"
                )
            else:
                system_prompt = (
                    "You are an expert QA automation engineer. "
                    "Generate 3–8 E2E test cases from the visual evidence below. "
                    "Each test case must: (1) state a title, (2) list step-by-step "
                    "actions with screen names and control labels, (3) include the Playwright "
                    "selector for automated steps, (4) specify the expected outcome. "
                    "Return ONLY valid JSON: "
                    "{\"test_cases\": [{\"title\": str, "
                    "\"steps\": [{\"action\": str, \"target_element\": str, "
                    "\"expected_output\": str, \"proof_confidence\": float}]}], "
                    "\"unproven_steps\": []}"
                )

            # Build evidence digest for the LLM
            digest_lines: list[str] = [
                f"Visual evidence artifact {artifact_id[:8]}.",
                f"Scenes: {len(scenes)}, confirmed transitions: {len(working_edges)}, "
                f"automation-ready controls: {auto_ready_count}.",
                "",
            ]
            for i, edge in enumerate(working_edges[:30], 1):
                from_s = scene_by_id.get(edge.get("from_scene_id", ""), {})
                to_s = scene_by_id.get(edge.get("to_scene_id", ""), {})
                label = "confirmed" if edge.get("edge_type") == "action_confirmed_transition" else "observed"
                digest_lines.append(
                    f"  {i}. [{label}] edge_id={edge.get('edge_id', '')[:8]} "
                    f"{from_s.get('screen_name') or 'screen'} "
                    f"—[{edge.get('action_type') or 'navigate'} {edge.get('action_value') or ''}]→ "
                    f"{to_s.get('screen_name') or 'screen'} "
                    f"(from_scene_id={edge.get('from_scene_id', '')[:8]})"
                )
                ready_ctrls = [
                    c for c in ctrl_by_scene.get(edge.get("from_scene_id", ""), [])
                    if c.get("automation_ready")
                ]
                if ready_ctrls:
                    ctrl_desc = ", ".join(
                        f"{c.get('label_text') or c.get('element_type')} "
                        f"sel={c.get('playwright_selector')} "
                        f"control_id={c.get('control_id', '')[:8]}"
                        for c in ready_ctrls[:3]
                    )
                    digest_lines.append(f"     Controls: {ctrl_desc}")
                # OCR delta hint
                from_ocr = (from_s.get("ocr_text") or "")[:80]
                to_ocr = (to_s.get("ocr_text") or "")[:80]
                if to_ocr and to_ocr != from_ocr:
                    digest_lines.append(f"     OCR after: {to_ocr!r}")

            # Add observed (unproven) transitions as context
            if unproven_edges:
                digest_lines.append(f"\n  Observed (unconfirmed) transitions: {len(unproven_edges)}")

            digest = "\n".join(digest_lines)

            # ── LLM-driven strategies ─────────────────────────────────
            test_cases: list[dict] = []
            llm_unproven: list[dict] = []
            llm_error: str | None = None
            if llm_strategies_to_run:
                try:
                    response_text = await self.llm.generate(system_prompt, digest)
                    import json as _json
                    parsed = _json.loads(response_text)
                    test_cases = parsed.get("test_cases", []) or []
                    llm_unproven = parsed.get("unproven_steps", []) or []
                except Exception as exc:
                    # G8: NO stub test fallback. Production behavior: surface
                    # the LLM failure honestly. Deterministic strategies still
                    # run so the response isn't empty when state_explorer etc.
                    # were requested alongside.
                    llm_error = f"LLM strategy generation failed: {exc.__class__.__name__}: {exc}"
                    import logging as _logging
                    _logging.getLogger("heart").error(
                        "heart.visual_strict.llm_failed: %s", exc, exc_info=True
                    )

            # P2: every LLM-emitted test_case carries a strategy tag.
            # Default to the first requested LLM strategy if missing.
            _default_strategy = llm_strategies_to_run[0] if llm_strategies_to_run else "happy_path"
            for tc in test_cases:
                tag = tc.get("strategy")
                if not tag or tag not in _llm_strategies:
                    tc["strategy"] = _default_strategy

            # ── Deterministic strategies ──────────────────────────────
            # G10/G11/G12: state_explorer, cross_app, error_state.
            # These do NOT depend on the LLM — they're pure graph algorithms.
            if "state_explorer" in deterministic_strategies_to_run:
                test_cases.extend(_gen_state_explorer(
                    scenes=scenes,
                    edges=working_edges,
                    scene_by_id=scene_by_id,
                    ctrl_by_scene=ctrl_by_scene,
                    max_paths=5,
                    max_depth=8,
                ))
            if "cross_app" in deterministic_strategies_to_run:
                test_cases.extend(_gen_cross_app(
                    scenes=scenes,
                    edges=edges,  # use all edges, including app_switch
                    scene_by_id=scene_by_id,
                    ctrl_by_scene=ctrl_by_scene,
                ))
            if "error_state" in deterministic_strategies_to_run:
                test_cases.extend(_gen_error_state(
                    scenes=scenes,
                    edges=working_edges,
                    scene_by_id=scene_by_id,
                    ctrl_by_scene=ctrl_by_scene,
                ))

            # G3/G4/G5/G6: Enrich every test_case with derived
            # preconditions, expected_outcome, workflow_steps_covered, and
            # risk_areas_addressed — sourced from the visual graph, never
            # stubbed.
            for tc in test_cases:
                _enrich_test_case(
                    tc,
                    scene_by_id=scene_by_id,
                    ctrl_by_scene=ctrl_by_scene,
                )

            # G7: Populate data_matrix for variant-strategy tests by
            # collecting the substituted input_data values from each
            # variant test's steps. The matrix is a list of dicts where
            # keys are control labels and values are the substituted text.
            for tc in test_cases:
                if tc.get("strategy") != "variant":
                    continue
                row: dict[str, str] = {}
                for step in tc.get("steps", []):
                    val = (step.get("input_data") or "").strip()
                    label = (step.get("target_element") or "").strip()
                    if val and label:
                        row[label] = val
                if row:
                    tc["data_matrix"] = [row]

            # G9: per-strategy verification — record which strategies produced
            # zero output so the response can explain it instead of silently
            # returning fewer tests than requested.
            strategy_counts = {s: 0 for s in strategies}
            for tc in test_cases:
                tag = tc.get("strategy")
                if tag in strategy_counts:
                    strategy_counts[tag] += 1
            strategies_empty = [s for s, n in strategy_counts.items() if n == 0]

            # Build unproven_steps: LLM output + observed edges in visual_strict mode
            unproven_steps = list(llm_unproven)
            for e in unproven_edges:
                from_s = scene_by_id.get(e.get("from_scene_id", ""), {})
                to_s = scene_by_id.get(e.get("to_scene_id", ""), {})
                unproven_steps.append({
                    "description": (
                        f"{from_s.get('screen_name') or 'scene'} → "
                        f"{to_s.get('screen_name') or 'scene'}"
                    ),
                    "reason": "transition observed but action not confirmed",
                    "edge_id": e.get("edge_id", ""),
                    "evidence_confidence": e.get("evidence_confidence", 0.0),
                })

            proven_steps = sum(
                1 for tc in test_cases
                for step in tc.get("steps", [])
                if step.get("evidence_scene_id") and step.get("evidence_control_id")
            )
            total_steps = sum(len(tc.get("steps", [])) for tc in test_cases)
            automation_ready_pct = (
                round(auto_ready_count / max(len(controls), 1) * 100, 1)
                if controls else 0.0
            )

            # G8/G9: surface honest diagnostics. If the LLM failed AND
            # nothing else produced output, this is a real error condition
            # and we propagate as a 502 rather than returning {test_cases: []}.
            if not test_cases and llm_error and not deterministic_strategies_to_run:
                raise HTTPException(status_code=502, detail=llm_error)

            elapsed_ms = (time.monotonic() - start) * 1000
            return {
                "success": True,
                "artifact_id": artifact_id,
                "session_id": session_id,
                "evidence_mode": evidence_mode,
                "test_cases": test_cases,
                "unproven_steps": unproven_steps,
                "coverage_report": {
                    "total_edges": len(edges),
                    "proven_steps": proven_steps,
                    "total_steps": total_steps,
                    "unproven_steps": len(unproven_steps),
                    "automation_ready_pct": automation_ready_pct,
                    "strategy_counts": strategy_counts,
                    "strategies_empty": strategies_empty,
                    "llm_error": llm_error,
                },
                "processing_time_ms": round(elapsed_ms, 2),
            }

        # ── Co-Architect Chat (P4) ──────────────────────────────────────

        @app.post("/api/v1/heart/co-architect/chat")
        async def co_architect_chat(
            req: dict,
            user: NexusUser = Depends(get_current_user),
        ):
            """Stateless chat turn with the visual-graph-constrained agent.

            Request schema:
                artifact_id:        str    — canonical artifact id
                session_id:         str    — session scope (optional)
                messages:           list   — [{role: 'user'|'assistant', content: str}, ...]
                propose_scenarios:  bool   — when True, the agent returns
                                             structured JSON with grounded
                                             scenario proposals
                evidence_mode:      str    — 'visual_strict' (default)

            Returns:
                {success, response, proposed_scenarios, parse_warning,
                 model_used, latency_ms, graph_summary}

            All scenario steps are guaranteed to cite a non-empty
            evidence_scene_id and evidence_control_id. The Platform API
            'commit proposals' endpoint re-validates these against the
            graph before persisting.
            """
            from app.co_architect import (
                build_system_prompt,
                encode_conversation,
                parse_chat_response,
            )

            start = time.monotonic()

            artifact_id = (req.get("artifact_id") or "").strip()
            session_id = (req.get("session_id") or "").strip()
            messages = req.get("messages") or []
            propose_scenarios = bool(req.get("propose_scenarios", False))

            if not artifact_id:
                raise HTTPException(422, "artifact_id is required")
            if not isinstance(messages, list) or not messages:
                raise HTTPException(422, "messages must be a non-empty list")

            # ── Fetch visual evidence graph (same pattern as
            # generate-tests-from-visual)
            _platform_url = os.environ.get(
                "PLATFORM_API_URL", "http://localhost:8000",
            )
            graph: dict = {}
            try:
                import httpx as _httpx
                auth_header = (
                    f"Bearer {user.token}"
                    if hasattr(user, "token") and user.token
                    else ""
                )
                async with _httpx.AsyncClient(timeout=15.0) as http:
                    resp = await http.get(
                        f"{_platform_url}/api/v1/artifacts/{artifact_id}"
                        "/visual-evidence-graph",
                        headers=(
                            {"Authorization": auth_header} if auth_header else {}
                        ),
                    )
                    if resp.status_code == 200:
                        graph = resp.json()
            except Exception as exc:
                import logging as _logging
                _logging.getLogger("heart").warning(
                    "co_architect.graph_fetch_failed: %s", exc,
                )

            if not (graph.get("scenes") or []):
                raise HTTPException(422, detail={
                    "error": "no_visual_evidence",
                    "message": (
                        "No visual evidence graph available for this artifact. "
                        "Run the visual pipeline first."
                    ),
                })

            # ── Build prompts
            system_prompt = build_system_prompt(
                graph, propose_scenarios=propose_scenarios,
            )
            user_prompt = encode_conversation(messages)

            # ── LLM call
            llm_error: str | None = None
            response_text = ""
            model_used = ""
            try:
                response_text = await self.llm.generate(
                    system_prompt, user_prompt,
                )
                # The LLM adapter doesn't expose the resolved provider; record
                # via the configured tier label when known.
                model_used = (
                    os.environ.get("HEART_TIER1_MODEL")
                    or os.environ.get("LLM_MODEL")
                    or "heart-llm"
                )
            except Exception as exc:
                import logging as _logging
                _logging.getLogger("heart").error(
                    "co_architect.llm_failed: %s", exc, exc_info=True,
                )
                llm_error = f"{exc.__class__.__name__}: {exc}"

            if llm_error and not response_text:
                raise HTTPException(502, detail={
                    "error": "llm_failed",
                    "message": llm_error,
                })

            # ── Parse (and repair JSON once if needed)
            turn = parse_chat_response(
                response_text, propose_scenarios=propose_scenarios,
            )

            if (
                propose_scenarios
                and turn.parse_warning
                and "plain text" in turn.parse_warning
            ):
                # One-shot JSON repair pass. Heart's RuleExtractor uses the
                # same trick when the model forgets to return JSON.
                try:
                    repair_prompt = (
                        "You returned plain prose when valid JSON was required. "
                        "Below is what you wrote. Rewrite it as the JSON object "
                        "described in the system prompt, with no commentary, "
                        "no markdown fences, and only the schema fields. "
                        "If you have no scenarios to propose, "
                        "set proposed_scenarios to [].\n\nORIGINAL OUTPUT:\n"
                        + response_text
                    )
                    repaired = await self.llm.generate(
                        system_prompt, repair_prompt,
                    )
                    turn = parse_chat_response(
                        repaired, propose_scenarios=True,
                    )
                except Exception:
                    pass  # keep the original prose response

            elapsed_ms = (time.monotonic() - start) * 1000

            payload = turn.to_dict()
            payload.update({
                "success": True,
                "artifact_id": artifact_id,
                "session_id": session_id,
                "model_used": model_used,
                "latency_ms": round(elapsed_ms, 2),
                "graph_summary": {
                    "scene_count": len(graph.get("scenes") or []),
                    "control_count": sum(
                        len(v) for v in (graph.get("controls_by_scene") or {}).values()
                    ),
                    "edge_count": len(graph.get("edges") or []),
                },
            })
            return payload


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = HeartEngine()
    engine.run()


if __name__ == "__main__":
    main()
