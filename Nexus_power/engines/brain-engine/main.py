"""
Nexus Brain Engine v1.0.0 — The Intelligent Coordinator.

The Brain is the 11th engine of the Nexus QA platform. While the Heart
handles reasoning about individual tasks (rule extraction, test generation),
the Brain sits ABOVE all engines and provides:

1. **Cross-Engine Coordination** — Intelligent routing, result merging,
   and conflict resolution across all 10 engines
2. **Quality Gates** — Automated quality scoring of session outputs
   with pass/fail determination
3. **Multi-Tier Provider Management** — Monitors and configures the
   3-tier LLM provider system across all engines (Cloud → Hybrid → On-Prem)
4. **Session Intelligence** — Tracks session state across engines,
   identifies gaps, and recommends next actions
5. **Confidence-Based Escalation** — Flags low-confidence outputs
   for human/SME review

Architecture:
  Brain sits alongside the Orchestrator but serves a different purpose:
  - Orchestrator: Workflow execution (run stage A, then B, then C)
  - Brain: Intelligent decisions (SHOULD we run B? Is A's output good enough?)

The Brain uses its own tiered LLM for meta-reasoning:
  Tier 1: Claude Opus 4.5 (best reasoning for coordination)
  Tier 2: GPT-5 (fallback)
  Tier 3: Configurable local model via Ollama (default: llama3.2:1b)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.config import production_guard
from nexus_sdk.models import NexusRequest, NexusResponse
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.llm import LLMConfig, create_provider
from nexus_sdk.llm.base import LLMProvider
from nexus_sdk.llm.tiered import TieredProviderConfig, TieredLLMRouter

from app.coordinator.decision_engine import DecisionEngine, DecisionContext, DecisionType, Decision
from app.coordinator.quality_gate import QualityGate, QualityScore
from app.coordinator.session_reasoner import SessionReasoner, SessionState
from app.tier_manager.manager import TierManager, EngineTierStatus

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class BrainConfig(EngineConfig):
    engine_name: str = "brain"
    engine_port: int = 8011

    llm_backend: str = Field(
        default="ollama",
        description="LLM backend: 'ollama', 'vllm', or 'stub'",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        validation_alias=AliasChoices("BRAIN_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"),
    )
    ollama_model: str = Field(
        default="llama3.2:1b",
        validation_alias="BRAIN_OLLAMA_MODEL",
    )

    # Quality gate threshold
    quality_pass_threshold: float = Field(
        default=0.6, description="Minimum quality score to pass gate"
    )

    # Decision confidence threshold for human escalation
    escalation_threshold: float = Field(
        default=0.4, description="Below this confidence, escalate to human"
    )

    # LLM settings  
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096


# ─── Request/Response Models ──────────────────────────────────

class DecideRequest(NexusRequest):
    """Request the Brain to make a decision."""
    session_id: str = Field(..., description="QA session ID")
    decision_type: str = Field(
        ..., description="Type: route | quality_gate | confidence | merge | summarize"
    )
    engine_results: dict[str, Any] = Field(
        default_factory=dict, description="Results from engines"
    )
    rules: list[dict] = Field(default_factory=list)
    test_cases: list[dict] = Field(default_factory=list)
    confidence_scores: dict[str, float] = Field(default_factory=dict)
    user_query: str = Field(default="", description="Optional user question/context")
    constraints: dict[str, Any] = Field(default_factory=dict)


class DecideResponse(NexusResponse):
    """Brain decision response."""
    decision_id: str
    decision_type: str
    action: str
    reasoning: str
    confidence: float
    recommended_engines: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    requires_human: bool = False


class QualityGateRequest(NexusRequest):
    """Request quality gate evaluation."""
    session_id: str
    rules: list[dict] = Field(default_factory=list)
    test_cases: list[dict] = Field(default_factory=list)
    engine_results: dict[str, Any] = Field(default_factory=dict)
    confidence_scores: dict[str, float] = Field(default_factory=dict)
    pii_result: Optional[dict] = None


class QualityGateResponse(NexusResponse):
    """Quality gate evaluation result."""
    session_id: str
    overall_score: float
    level: str
    passed: bool
    rule_completeness: float
    test_coverage: float
    consistency: float
    confidence_avg: float
    pii_safety: float
    gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CanonicalQualityGateRequest(NexusRequest):
    """Request canonical artifact quality gate evaluation.

    Called by the canonical processing chain's Stage 7 to verify
    the raw artifact meets minimum quality before downstream chains.
    """
    session_id: str
    artifact_id: Optional[str] = None
    has_transcript: Optional[str] = Field(None, description="Transcript text from Ears (presence check)")
    has_visual: Optional[Any] = Field(None, description="Visual frames from Eyes (presence check)")
    scene_count: Optional[int] = Field(0, description="Number of analyzed frames")
    duration_seconds: Optional[float] = Field(0.0, description="Media duration from probe")
    # Shield-engine actual response fields
    entity_count: Optional[int] = Field(0, description="Number of PII entities found by Shield")
    entities_found: Optional[list] = Field(default_factory=list, description="PII entity labels from Shield")
    safe_text: Optional[str] = Field(None, description="PII-redacted transcript text from Shield")
    mapping_id: Optional[str] = Field(None, description="Shield PII mapping ID for de-identification audit")
    raw_transcript: Optional[str] = Field(None, description="Original transcript before PII redaction")
    scene_descriptions: Optional[list[str]] = Field(default_factory=list, description="Scene descriptions from Eyes for semantic depth analysis")
    scene_qualities: Optional[list[str]] = Field(default_factory=list, description="Per-scene quality flags ('strong'/'degraded'/'weak') from build_scenes")


class CanonicalQualityGateResponse(NexusResponse):
    """Canonical artifact quality gate result."""
    session_id: str
    artifact_id: Optional[str] = None
    overall_score: float
    level: str
    passed: bool
    transcript_quality: float
    visual_quality: float
    pii_safety: float
    duration_adequacy: float
    completeness: float
    gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    re_enrichment_needed: bool = False
    re_enrichment_recommendations: list[str] = Field(default_factory=list)


class SessionUpdateRequest(NexusRequest):
    """Update session state with engine results."""
    session_id: str
    engine_name: str
    result: dict[str, Any]


class SessionAnalysisResponse(NexusResponse):
    """Session gap analysis response."""
    session_id: str
    completeness: float
    engines_completed: list[str]
    gaps: list[str] = Field(default_factory=list)
    recommended_next: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TierStatusResponse(NexusResponse):
    """Multi-tier provider status across all engines."""
    overall_mode: str
    total_engines: int
    cloud_engines: list[str] = Field(default_factory=list)
    hybrid_engines: list[str] = Field(default_factory=list)
    onprem_engines: list[str] = Field(default_factory=list)
    engines: dict[str, Any] = Field(default_factory=dict)
    recommended_tiers: dict[str, Any] = Field(default_factory=dict)


class AskBrainRequest(NexusRequest):
    """Ask the Brain a free-form question about the QA process."""
    question: str
    session_id: Optional[str] = None
    context: Optional[str] = None


class AskBrainResponse(NexusResponse):
    """Brain answer response."""
    answer: str
    confidence: float
    sources: list[str] = Field(default_factory=list)


# ─── Process Oracle: Persona Draft Generation ────────────────

class EvidenceCitation(BaseModel):
    """A single piece of evidence grounding a persona claim."""
    text: str = Field(..., description="The quoted or paraphrased evidence")
    source_modality: str = Field(
        ..., description="Where this evidence came from: transcript | visual | graph | inferred"
    )
    timestamp_range: Optional[str] = Field(
        None, description="Approximate time range in source media (e.g. '02:15-03:40')"
    )
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="How confident the evidence supports the claim",
    )


class DomainActor(BaseModel):
    """An actor/role identified in the domain."""
    name: str
    role: str = Field(default="", description="What this actor does in the process")
    evidence: list[EvidenceCitation] = Field(default_factory=list)


class DomainSystem(BaseModel):
    """A system/application identified in the domain."""
    name: str
    purpose: str = Field(default="", description="What this system does")
    evidence: list[EvidenceCitation] = Field(default_factory=list)


class DomainWorkflow(BaseModel):
    """A workflow/process step identified in the domain."""
    step_number: int
    name: str
    description: str = ""
    actors: list[str] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list, description="Decision points at this step")
    evidence: list[EvidenceCitation] = Field(default_factory=list)


class DomainRisk(BaseModel):
    """A risk or unknown identified in the domain."""
    description: str
    severity: str = Field(default="medium", description="low | medium | high | critical")
    evidence: list[EvidenceCitation] = Field(default_factory=list)


class DomainMap(BaseModel):
    """Structured domain knowledge extracted from the canonical artifact."""
    actors: list[DomainActor] = Field(default_factory=list)
    systems: list[DomainSystem] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list, description="Key business entities/concepts")
    workflows: list[DomainWorkflow] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list, description="Key decision points")
    risks: list[DomainRisk] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list, description="Things that could not be determined")


class PersonaProfile(BaseModel):
    """The generated persona definition."""
    name: str = Field(..., description="e.g. 'Pharmacy Claims Process Expert'")
    description: str = Field(..., description="What this persona knows and can do")
    system_prompt: str = Field(..., description="LLM system prompt shaping persona behavior")
    capabilities: list[str] = Field(default_factory=list, description="Applicable capability IDs")
    specialty_domains: list[str] = Field(default_factory=list, description="Domain tags")
    avatar_icon: str = Field(default="user-circle", description="Lucide icon name")
    stage_config: dict = Field(default_factory=dict, description="Engine config per stage")


class PersonaDraftProvenance(BaseModel):
    """Tracks exactly how and from what this draft was generated."""
    artifact_id: str
    session_id: str = ""
    workflow_id: str = ""
    generated_at: str = ""
    model_used: str = ""
    model_backend: str = ""
    generation_time_ms: float = 0.0
    quality_score_threshold: float = 0.0
    artifact_quality_score: float = 0.0


class GeneratePersonaDraftRequest(NexusRequest):
    """Request to generate a Process Oracle persona draft from canonical artifact data.

    Brain receives pre-loaded artifact data (Platform is responsible for
    loading the artifact from DB and normalizing the payload).
    """
    artifact_id: str = Field(..., description="Canonical artifact ID")
    session_id: Optional[str] = None
    workflow_id: Optional[str] = None
    # Pre-loaded artifact data (Platform loads, Brain consumes)
    safe_transcript_text: str = Field(default="", description="PII-redacted transcript")
    visual_summary: str = Field(default="", description="Concatenated keyframe descriptions")
    application_types_seen: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(default=0.0)
    scene_count: int = Field(default=0)
    frame_count: int = Field(default=0)
    # Structured data from full_artifact_json
    transcript_segments: list[dict] = Field(default_factory=list, description="Transcript segments with speaker/timestamp")
    visual_graph_nodes: list[dict] = Field(default_factory=list, description="Knowledge graph nodes")
    visual_graph_edges: list[dict] = Field(default_factory=list, description="Knowledge graph edges")
    scene_descriptions: list[str] = Field(default_factory=list, description="Per-frame descriptions")
    quality_score: float = Field(default=0.0, description="Artifact brain_quality_score")


class GeneratePersonaDraftResponse(NexusResponse):
    """Response containing the generated persona draft with full grounding."""
    persona: PersonaProfile
    domain_map: DomainMap
    grounding_contract: dict = Field(
        default_factory=dict,
        description="Top-level grounding summary: total_evidence_count, modality_distribution, avg_confidence, open_questions",
    )
    provenance: PersonaDraftProvenance


# ── Test Architect Models ──────────────────────────────────────

class TestStep(BaseModel):
    """A single step within a test case."""
    step_number: int
    action: str = Field(..., description="What the tester does")
    input_data: str = Field(default="", description="Test data to use")
    expected_behavior: str = Field(..., description="What should happen")


class TestCase(BaseModel):
    """A single test case with full traceability."""
    case_id: str = Field(default="", description="e.g. TC-001")
    title: str = Field(..., description="Concise test case title")
    category: str = Field(default="happy_path", description="happy_path|negative|boundary|edge_case|security|performance")
    priority: str = Field(default="P2_medium", description="P0_critical|P1_high|P2_medium|P3_low")
    preconditions: list[str] = Field(default_factory=list)
    steps: list[TestStep] = Field(default_factory=list)
    expected_result: str = Field(default="", description="Overall expected outcome")
    test_data: dict = Field(default_factory=dict, description="Key test data values")
    tags: list[str] = Field(default_factory=list, description="smoke|regression|critical|sanity")
    evidence_trace: list[EvidenceCitation] = Field(default_factory=list, description="KT evidence this case is grounded in")


class TestScenario(BaseModel):
    """A group of related test cases for a workflow step."""
    scenario_id: str = Field(default="", description="e.g. TS-001")
    workflow_step_number: int = Field(..., description="Maps to domain_map workflow step")
    workflow_step_name: str = Field(default="")
    description: str = Field(default="")
    test_cases: list[TestCase] = Field(default_factory=list)


class TraceabilityEntry(BaseModel):
    """Maps a workflow step/requirement to its test coverage."""
    requirement: str = Field(..., description="Business requirement or step name")
    workflow_step_number: int = Field(default=0)
    test_case_ids: list[str] = Field(default_factory=list)
    coverage_status: str = Field(default="covered", description="covered|partial|gap")
    evidence_count: int = Field(default=0)


class CoverageBreakdown(BaseModel):
    """Test coverage statistics."""
    total_scenarios: int = 0
    total_cases: int = 0
    by_category: dict = Field(default_factory=dict, description="happy_path: N, negative: N, ...")
    by_priority: dict = Field(default_factory=dict, description="P0: N, P1: N, ...")
    coverage_percentage: float = Field(default=0.0, description="Steps with at least 1 test case")
    gap_areas: list[str] = Field(default_factory=list, description="Steps or areas without coverage")


class TestPlanSummary(BaseModel):
    """High-level test plan metadata."""
    name: str = Field(default="", description="e.g. 'USAA Life Insurance — Test Strategy'")
    objective: str = Field(default="")
    scope: str = Field(default="")
    approach: str = Field(default="risk-based", description="risk-based|exploratory|regression|comprehensive")
    source_persona: str = Field(default="", description="Name of the SME persona this derives from")
    source_artifact_id: str = Field(default="")


class TestStrategyProvenance(BaseModel):
    """Tracks how the test strategy was generated."""
    artifact_id: str
    session_id: str = ""
    persona_name: str = ""
    generated_at: str = ""
    model_used: str = ""
    model_backend: str = ""
    generation_time_ms: float = 0.0
    workflow_steps_analysed: int = 0
    risks_considered: int = 0
    source_persona_generated_at: str = ""
    source_persona_quality: str = ""


class GenerateTestStrategyRequest(NexusRequest):
    """Request to generate a test strategy from a persona draft's domain map."""
    artifact_id: str = Field(..., description="Canonical artifact ID")
    session_id: Optional[str] = None
    # Pre-loaded persona draft data (Platform extracts from cache)
    persona_name: str = Field(default="", description="Source persona name")
    persona_description: str = Field(default="", description="Source persona description")
    domain_map: dict = Field(default_factory=dict, description="Full domain_map from persona draft")
    grounding_contract: dict = Field(default_factory=dict, description="Grounding summary from persona draft")
    duration_seconds: float = Field(default=0.0)
    source_persona_generated_at: str = Field(default="", description="Persona draft generated_at timestamp for cache lineage")
    source_persona_quality: str = Field(default="", description="Persona draft quality: full or fallback")


class GenerateTestStrategyResponse(NexusResponse):
    """Response containing the full test strategy with traceability."""
    test_plan: TestPlanSummary
    test_scenarios: list[TestScenario] = Field(default_factory=list)
    coverage: CoverageBreakdown = Field(default_factory=CoverageBreakdown)
    traceability: list[TraceabilityEntry] = Field(default_factory=list)
    provenance: TestStrategyProvenance = Field(default_factory=TestStrategyProvenance)


# ── E2E Architect Models ──────────────────────────────────────

class E2EVariable(BaseModel):
    """A testable variable identified from multimodal evidence."""
    name: str = Field(..., description="Variable name (e.g. Gender, State)")
    type: str = Field(default="categorical", description="categorical | numeric | boolean")
    observed_values: list[str] = Field(default_factory=list, description="Values seen in the demo")
    inferred_values: list[str] = Field(default_factory=list, description="Values not shown but expected")
    source: str = Field(default="", description="Evidence source description")
    impacts: list[str] = Field(default_factory=list, description="What this variable affects")


class DecisionPoint(BaseModel):
    """A branching point where different values lead to different outcomes."""
    step_number: int = Field(default=0)
    condition: str = Field(..., description="The decision condition")
    observed_path: str = Field(default="", description="Path demonstrated in the recording")
    alternative_path: str = Field(default="", description="Path not shown but expected")
    source: str = Field(default="", description="Evidence source")


class E2EScenario(BaseModel):
    """A critical end-to-end test scenario with data combinations."""
    scenario_id: str = Field(default="", description="e.g. E2E-001")
    title: str = Field(..., description="Scenario title")
    category: str = Field(default="observed", description="observed | inferred_high_risk | boundary_assumption")
    priority: str = Field(default="P1_high", description="P0_critical | P1_high | P2_medium")
    rationale: str = Field(default="", description="Why this combination is critical")
    evidence_sources: list[EvidenceCitation] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    steps: list[TestStep] = Field(default_factory=list)
    expected_outcome: str = Field(default="", description="Expected result for this combination")
    data_matrix: list[dict] = Field(default_factory=list, description="Data value sets for parameterisation")
    workflow_steps_covered: list[int] = Field(default_factory=list)
    risk_areas_addressed: list[str] = Field(default_factory=list)


class E2EArchitectOutput(BaseModel):
    """Complete E2E Architect analysis output."""
    variables: list[E2EVariable] = Field(default_factory=list)
    decision_points: list[DecisionPoint] = Field(default_factory=list)
    critical_combinations: list[E2EScenario] = Field(default_factory=list)
    coverage_analysis: dict = Field(default_factory=dict)


class GenerateE2EArchitectRequest(NexusRequest):
    """Request to generate critical E2E scenarios from multimodal evidence."""
    artifact_id: str = Field(..., description="Canonical artifact ID")
    session_id: str = ""
    tenant_id: str = ""
    # From canonical persona (read-only reference)
    persona_name: str = Field(default="", description="Source persona name")
    persona_description: str = Field(default="", description="Source persona description")
    domain_map: dict = Field(default_factory=dict, description="Domain map from persona draft")
    grounding_contract: dict = Field(default_factory=dict)
    # Existing test strategy (for deduplication)
    existing_test_scenarios: list[dict] = Field(default_factory=list)
    # Rich multimodal evidence (from canonical substrate via multimodal.py)
    visual_summary: str = ""
    scene_descriptions: list[str] = Field(default_factory=list)
    ui_element_inventory: dict = Field(default_factory=dict)
    multimodal_scenes: list[dict] = Field(default_factory=list)
    raw_ocr_evidence: list[dict] = Field(default_factory=list, description="Per-scene raw OCR text for evidence grounding")
    application_types_seen: list[str] = Field(default_factory=list)
    visual_graph_nodes: list[dict] = Field(default_factory=list)
    transcript_segments: list[dict] = Field(default_factory=list)
    # Processing metadata
    duration_seconds: float = 0.0
    frame_count: int = 0
    scene_count: int = 0


class GenerateE2EArchitectResponse(NexusResponse):
    """Response containing E2E Architect analysis with critical scenarios."""
    e2e_architect: E2EArchitectOutput = Field(default_factory=E2EArchitectOutput)
    provenance: TestStrategyProvenance = Field(default_factory=TestStrategyProvenance)


# ─── Brain LLM (Tiered) ──────────────────────────────────────

class BrainLLM:
    """
    Brain engine LLM adapter with multi-tier failover.

    Primary:   Claude Opus 4.5 (best reasoning)
    Secondary: GPT-5 (fallback)
    Local:     Llama 3.1 70B via Ollama (on-prem backup)

    Falls back to stub mode when no real LLM is available.
    """

    def __init__(self, config: BrainConfig, event_bus=None):
        self.config = config
        self._router: Optional[TieredLLMRouter] = None
        self._provider: Optional[LLMProvider] = None
        self._backend: str = "stub"
        self._model: str = "none"
        self._event_bus = event_bus

    async def initialize(self):
        """Initialize the LLM provider. Tries tiered first, then single."""
        backend = os.getenv("LLM_BACKEND", "").lower() or self.config.llm_backend.lower()

        if backend == "stub":
            self._backend = "stub"
            logger.info("brain: LLM backend set to STUB mode (development)")
            return

        # Try tiered provider system first
        try:
            tier_config = TieredProviderConfig.from_engine("brain")
            if len(tier_config.active_tiers) > 0:
                self._router = TieredLLMRouter(tier_config)
                await self._router.initialize()
                self._backend = "tiered"
                primary = tier_config.active_tiers[0]
                self._model = primary.model or f"{primary.provider}(default)"
                logger.info(
                    "brain: Tiered LLM router initialized",
                    extra={"tiers": [t.tier.value for t in tier_config.active_tiers]},
                )
                return
        except Exception as e:
            logger.warning("brain: Tiered router init failed: %s", e)

        # Fallback to single provider via SDK
        try:
            llm_config = LLMConfig()
            if backend == "ollama" and not os.getenv("LLM_PROVIDER"):
                llm_config.provider = "ollama"
                llm_config.api_base_url = self.config.ollama_base_url
                llm_config.ollama_base_url = self.config.ollama_base_url
                llm_config.model = self.config.ollama_model
                llm_config.ollama_model = self.config.ollama_model
            self._provider = create_provider(llm_config)
            await self._provider.initialize()
            self._backend = llm_config.provider
            self._model = llm_config.get_effective_model() or "unknown"
            logger.info(
                "brain: Single LLM provider initialized",
                extra={"provider": self._backend, "model": self._model},
            )
        except Exception as e:
            logger.warning("brain: LLM provider init failed, falling back to stub: %s", e)
            self._provider = None
            self._backend = "stub"

        production_guard("Brain LLM provider", available=(self._backend != "stub"))

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool | None = None,
        *,
        allow_stub_fallback: bool | None = None,
    ) -> str:
        """Generate text with automatic tier failover."""
        last_error: Exception | None = None
        if allow_stub_fallback is None:
            allow_stub_fallback = (
                os.getenv("NEXUS_ALLOW_DEGRADED_MODE", "false").lower() == "true"
            )

        if self._router and self._backend == "tiered":
            try:
                response = await self._router.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
                return response.content
            except Exception as e:
                last_error = e
                logger.warning("brain: Tiered router failed, trying single: %s", e)

        if self._provider and self._backend != "stub":
            try:
                response = await self._provider.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
                return response.content
            except Exception as e:
                last_error = e
                logger.warning("brain: LLM generate failed, falling back to stub: %s", e)

        if not allow_stub_fallback:
            if last_error is not None:
                raise RuntimeError(f"Brain LLM generation failed without stub fallback: {last_error}") from last_error
            raise RuntimeError("Brain LLM generation failed without stub fallback")

        return self._stub_generate(system_prompt, user_prompt)

    async def shutdown(self):
        """Clean up providers."""
        if self._router:
            await self._router.shutdown()
        if self._provider:
            try:
                await self._provider.shutdown()
            except Exception:
                pass

    def get_health(self) -> dict[str, Any]:
        """Return LLM provider health status."""
        if self._router:
            health = self._router.get_health()
            health.setdefault("semantic_scoring", "real")
            return health
        is_real = self._backend not in ("stub", "unknown", "")
        return {
            "engine": "brain",
            "backend": self._backend,
            "model": self._model,
            "initialized": is_real,
            "semantic_scoring": "real" if is_real else "degraded",
        }

    @staticmethod
    def _stub_generate(system_prompt: str, user_prompt: str) -> str:
        """Development stub returning structured JSON decisions."""
        if "quality" in system_prompt.lower() or "evaluate" in system_prompt.lower():
            return json.dumps({
                "action": "needs_review",
                "reasoning": "[Stub] Quality gate evaluation — insufficient data for production assessment",
                "confidence": 0.5,
                "gaps": ["Stub mode: no real LLM analysis performed"],
                "warnings": ["Running in stub/development mode"],
                "requires_human": True,
            })
        if "route" in system_prompt.lower() or "which engine" in system_prompt.lower():
            return json.dumps({
                "action": "route",
                "reasoning": "[Stub] Routing recommendation based on session state",
                "confidence": 0.5,
                "recommended_engines": ["heart", "shield"],
                "parameters": {},
                "warnings": ["Running in stub mode"],
                "requires_human": False,
            })
        if "merge" in system_prompt.lower() or "reconcile" in system_prompt.lower():
            return json.dumps({
                "action": "merged",
                "reasoning": "[Stub] Results merged without conflict analysis",
                "confidence": 0.5,
                "warnings": ["Running in stub mode — no real conflict resolution"],
                "requires_human": True,
            })
        if "contradiction" in user_prompt.lower() or "contradict" in user_prompt.lower():
            return json.dumps({
                "action": "contradictions_analyzed",
                "reasoning": "[Stub] Contradiction analysis — no real LLM comparison performed",
                "confidence": 0.5,
                "contradictions": [],
                "warnings": ["Running in stub/development mode — no real contradiction detection"],
                "requires_human": False,
            })
        return json.dumps({
            "action": "analyzed",
            "reasoning": f"[Stub] Analysis of: {user_prompt[:100]}...",
            "confidence": 0.5,
            "warnings": ["Running in stub/development mode"],
            "requires_human": False,
        })


# ─── Brain Engine ─────────────────────────────────────────────

class BrainEngine(NexusEngine):
    """
    The 11th Nexus engine — Intelligent Coordinator.

    Provides cross-engine decision-making, quality gates,
    session intelligence, and multi-tier provider management.
    """

    def __init__(self):
        config = BrainConfig()
        super().__init__(
            name="brain",
            version="1.0.0",
            config=config,
            description="Intelligent coordinator — cross-engine reasoning, quality gates, tier management",
        )
        self.brain_config = config
        self.llm = BrainLLM(config)
        self.decision_engine: Optional[DecisionEngine] = None
        self.quality_gate = QualityGate(pass_threshold=config.quality_pass_threshold)
        self.session_reasoner = SessionReasoner()
        self.tier_manager = TierManager()

    async def on_startup(self):
        """Initialize Brain engine components."""
        await self.llm.initialize()
        self.decision_engine = DecisionEngine(llm_generate_fn=self.llm.generate)

        # ── Register engine-specific Prometheus metrics ──
        from nexus_sdk.observability.metrics import get_metrics
        m = get_metrics()
        if m:
            self._m_llm_requests = m.custom_counter(
                "brain_llm_requests_total",
                "Total LLM inference requests",
                labels=["tier", "provider", "status"],
            )
            self._m_llm_latency = m.custom_histogram(
                "brain_llm_latency_seconds",
                "LLM inference latency",
                labels=["tier", "provider"],
                buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
            )
            self._m_tier_failovers = m.custom_counter(
                "brain_tier_failover_total",
                "Number of LLM tier failovers",
                labels=["from_tier", "to_tier"],
            )
        else:
            self._m_llm_requests = None
            self._m_llm_latency = None
            self._m_tier_failovers = None

        # P0: Connect SessionReasoner to Redis for durable session state
        redis_url = os.getenv("REDIS_URL", os.getenv("BRAIN_REDIS_URL", ""))
        if redis_url:
            connected = await self.session_reasoner.connect_redis(redis_url)
            if connected:
                loaded = await self.session_reasoner.load_from_redis()
                logger.info("brain.startup: Loaded %d persisted sessions from Redis", loaded)
        else:
            logger.warning(
                "brain.startup: No REDIS_URL configured — session state is in-memory only "
                "(not durable across restarts)"
            )
            from nexus_sdk.config import production_guard
            production_guard("Redis (brain-session-state)", available=False)

        # Detect current tier deployment
        self.tier_manager.detect_active_tiers()

        # Expose semantic mode in /health for deployment validation
        is_real = self.llm._backend not in ("stub", "unknown", "")
        self.health.set_mode("llm", self.llm._backend)
        self.health.set_mode("llm_model", self.llm._model)
        self.health.set_mode("semantic_scoring", "real" if is_real else "degraded")

        logger.info(
            "brain.engine.ready",
            extra={
                "llm_backend": self.llm._backend,
                "llm_model": self.llm._model,
                "semantic_scoring": "real" if is_real else "degraded",
                "quality_threshold": self.brain_config.quality_pass_threshold,
                "session_persistence": "redis" if self.session_reasoner._redis else "in-memory",
            },
        )

        # P2 Fix: Warmup inference — prime LLM connection + JIT kernels
        # with a trivial prompt so the first real request isn't penalized.
        if is_real:
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
                logger.info("brain.warmup.ok")
            except Exception as e:
                logger.warning("brain.warmup.failed: %s (non-blocking)", e)

    async def on_shutdown(self):
        """Cleanup Brain engine."""
        await self.llm.shutdown()

    async def _assess_canonical_quality_llm(
        self,
        transcript_text: str,
        scene_count: int,
        duration_seconds: float,
        scene_descriptions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Use Brain LLM to semantically assess canonical artifact quality.

        Returns dict with optional adjusted_score, additional_gaps, additional_warnings.
        """
        visual_context = ""
        if scene_descriptions:
            sample_descs = scene_descriptions[:5]
            visual_context = (
                f"\nVisual scene descriptions ({len(scene_descriptions)} total, showing first 5):\n"
                + "\n".join(f"  - {d[:200]}" for d in sample_descs if d)
                + "\n"
            )

        prompt = (
            "You are an automated QA quality assessor for knowledge transfer recordings.\n"
            "Assess the following canonical artifact and respond ONLY with valid JSON.\n\n"
            f"Transcript excerpt ({len(transcript_text)} chars):\n"
            f"---\n{transcript_text}\n---\n\n"
            f"Visual frames analyzed: {scene_count}\n"
            f"Media duration: {duration_seconds:.1f} seconds\n"
            f"{visual_context}\n"
            "Evaluate:\n"
            "1. Is the transcript coherent and meaningful (not garbled/noise)?\n"
            "2. Is the content length proportional to the duration?\n"
            "3. Are there obvious quality issues (repeated text, encoding errors)?\n"
            "4. Does the vocabulary suggest real domain-relevant speech?\n"
            "5. Do the visual descriptions (if any) describe meaningful UI or content?\n\n"
            "Respond with JSON: {\"adjusted_score\": 0.0-1.0, "
            "\"additional_gaps\": [\"...\"], \"additional_warnings\": [\"...\"]}"
        )

        raw = await self.llm.generate(prompt, max_tokens=512, temperature=0.0)
        if not raw:
            return {}

        # Extract JSON from LLM response
        import json as _json
        text = raw.strip()
        # Handle markdown code blocks
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            result = _json.loads(text)
            # Validate and clamp score
            if "adjusted_score" in result:
                result["adjusted_score"] = max(0.0, min(1.0, float(result["adjusted_score"])))
            return result
        except (_json.JSONDecodeError, ValueError, TypeError):
            logger.debug("brain.canonical_qg.llm_parse_failed: %s", text[:200])
            return {}

    def register_routes(self, app):
        """Register Brain Engine API routes."""

        # ── Decision Making ────────────────────────────────────

        @app.post("/api/v1/brain/decide", response_model=DecideResponse)
        async def decide(
            req: DecideRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Ask the Brain to make an intelligent decision.

            Decision types:
            - route: Which engines to invoke next
            - quality_gate: Pass/fail quality evaluation
            - confidence: Flag low-confidence items
            - merge: Merge results from multiple engines
            - summarize: Cross-engine session summary
            """
            start = time.monotonic()

            try:
                dt = DecisionType(req.decision_type)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid decision_type: {req.decision_type}. "
                           f"Valid: {[t.value for t in DecisionType]}",
                )

            context = DecisionContext(
                session_id=req.session_id,
                tenant_id=req.tenant_id,
                decision_type=dt,
                engine_results=req.engine_results,
                rules_extracted=req.rules,
                test_cases=req.test_cases,
                confidence_scores=req.confidence_scores,
                user_query=req.user_query,
                constraints=req.constraints,
            )

            decision = await self.decision_engine.decide(context)
            elapsed_ms = (time.monotonic() - start) * 1000

            return DecideResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                decision_id=decision.decision_id,
                decision_type=decision.decision_type.value,
                action=decision.action,
                reasoning=decision.reasoning,
                confidence=decision.confidence,
                recommended_engines=decision.recommended_engines,
                parameters=decision.parameters,
                warnings=decision.warnings,
                requires_human=decision.requires_human,
            )

        # ── Quality Gate ───────────────────────────────────────

        @app.post("/api/v1/brain/quality-gate", response_model=QualityGateResponse)
        async def quality_gate(
            req: QualityGateRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Evaluate the quality of a QA session's outputs.

            Returns a composite quality score across multiple dimensions:
            rule completeness, test coverage, consistency, confidence, PII safety.
            """
            start = time.monotonic()

            score = self.quality_gate.evaluate(
                rules=req.rules,
                test_cases=req.test_cases,
                engine_results=req.engine_results,
                confidence_scores=req.confidence_scores,
                pii_result=req.pii_result,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return QualityGateResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                session_id=req.session_id,
                overall_score=score.overall,
                level=score.level.value,
                passed=score.passed,
                rule_completeness=score.rule_completeness,
                test_coverage=score.test_coverage,
                consistency=score.consistency,
                confidence_avg=score.confidence,
                pii_safety=score.pii_safety,
                gaps=score.gaps,
                warnings=score.warnings,
            )

        # ── Canonical Artifact Quality Gate ──────────────────────

        @app.post("/api/v1/brain/canonical-quality-gate",
                  response_model=CanonicalQualityGateResponse)
        async def canonical_quality_gate(
            req: CanonicalQualityGateRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Evaluate canonical artifact quality before downstream consumption.

            Called as the final stage of canonical media processing.
            Validates transcript completeness, visual coverage, PII safety,
            and media duration. If confidence is below threshold, recommends
            selective re-enrichment.
            """
            start = time.monotonic()

            # Reconstruct pii_result dict for the quality gate scorer
            pii_result = {
                "entity_count": req.entity_count or 0,
                "entities_found": req.entities_found or [],
                "mapping_id": req.mapping_id or "",
                "redacted": bool(req.safe_text),
            }

            score = self.quality_gate.evaluate_canonical(
                has_transcript=req.has_transcript,
                has_visual=req.has_visual,
                scene_count=req.scene_count or 0,
                duration_seconds=req.duration_seconds or 0.0,
                pii_result=pii_result,
                safe_transcript=req.safe_text,
                raw_transcript=req.raw_transcript,
                scene_descriptions=req.scene_descriptions or None,
                # Per-scene quality flags from the eyes engine's own
                # assessment, threaded through the spine persistence stage.
                # Without these the gate scores purely on scene count and
                # description length and routinely returns "excellent" on
                # artifacts where 9/12 scenes are flagged "weak".
                scene_qualities=req.scene_qualities or None,
            )

            # LLM-powered semantic assessment when available and transcript exists
            llm_assessment: dict[str, Any] = {}
            transcript_text = req.safe_text or req.has_transcript or ""
            if self.llm._backend != "stub" and transcript_text and len(transcript_text) > 50:
                try:
                    llm_assessment = await self._assess_canonical_quality_llm(
                        transcript_text=transcript_text[:4000],
                        scene_count=req.scene_count or 0,
                        duration_seconds=req.duration_seconds or 0.0,
                        scene_descriptions=req.scene_descriptions or None,
                    )
                    # Blend LLM assessment into score
                    if llm_assessment.get("adjusted_score") is not None:
                        llm_weight = 0.5
                        score["overall_score"] = round(
                            score["overall_score"] * (1 - llm_weight)
                            + llm_assessment["adjusted_score"] * llm_weight,
                            3,
                        )
                        score["passed"] = score["overall_score"] >= self.brain_config.quality_pass_threshold
                        score["level"] = QualityScore.level_for(score["overall_score"]).value
                    if llm_assessment.get("additional_gaps"):
                        score["gaps"].extend(llm_assessment["additional_gaps"])
                    if llm_assessment.get("additional_warnings"):
                        score["warnings"].extend(llm_assessment["additional_warnings"])
                except Exception as exc:
                    logger.warning("brain.canonical_qg.llm_assessment_failed: %s", exc)

            # Determine re-enrichment needs
            re_enrichment_needed = not score["passed"] and score["overall_score"] < 0.5
            re_enrichment_recs: list[str] = []
            if re_enrichment_needed:
                if score["transcript_quality"] < 0.5:
                    re_enrichment_recs.append(
                        "Re-transcribe audio with higher quality settings or manual review"
                    )
                if score["visual_quality"] < 0.3:
                    re_enrichment_recs.append(
                        "Re-analyze video with denser frame sampling"
                    )
                if score["pii_safety"] < 0.7:
                    re_enrichment_recs.append(
                        "Re-run PII detection with stricter sensitivity"
                    )

            elapsed_ms = (time.monotonic() - start) * 1000

            logger.info(
                "brain.canonical_quality_gate.evaluated",
                extra={
                    "session_id": req.session_id,
                    "artifact_id": req.artifact_id,
                    "overall": score["overall_score"],
                    "passed": score["passed"],
                    "re_enrichment": re_enrichment_needed,
                    "llm_assessed": bool(llm_assessment),
                    "duration_ms": round(elapsed_ms, 2),
                },
            )

            return CanonicalQualityGateResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                session_id=req.session_id,
                artifact_id=req.artifact_id,
                overall_score=score["overall_score"],
                level=score["level"],
                passed=score["passed"],
                transcript_quality=score["transcript_quality"],
                visual_quality=score["visual_quality"],
                pii_safety=score["pii_safety"],
                duration_adequacy=score["duration_adequacy"],
                completeness=score["completeness"],
                gaps=score["gaps"],
                warnings=score["warnings"],
                re_enrichment_needed=re_enrichment_needed,
                re_enrichment_recommendations=re_enrichment_recs,
            )

        # ── Session Intelligence ───────────────────────────────

        @app.post("/api/v1/brain/sessions/{session_id}/update")
        async def update_session(
            session_id: str,
            req: SessionUpdateRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Update Brain's session state with results from an engine."""
            state = self.session_reasoner.get_or_create(session_id, req.tenant_id)
            updated = await self.session_reasoner.update_from_engine_durable(
                session_id=session_id,
                engine_name=req.engine_name,
                result=req.result,
            )
            return {
                "success": True,
                "session_id": session_id,
                "completeness": updated.completeness(),
                "engines_completed": updated.engines_completed,
            }

        @app.get("/api/v1/brain/sessions/{session_id}/analyze",
                 response_model=SessionAnalysisResponse)
        async def analyze_session(
            session_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Analyze gaps and recommend next actions for a session."""
            start = time.monotonic()

            analysis = self.session_reasoner.analyze_gaps(session_id)
            if "error" in analysis:
                raise HTTPException(status_code=404, detail=analysis["error"])

            elapsed_ms = (time.monotonic() - start) * 1000

            return SessionAnalysisResponse(
                success=True,
                trace_id=str(uuid.uuid4()),
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                session_id=session_id,
                completeness=analysis["completeness"],
                engines_completed=analysis["engines_completed"],
                gaps=analysis["gaps"],
                recommended_next=analysis["recommended_next"],
                warnings=analysis["warnings"],
            )

        @app.get("/api/v1/brain/sessions")
        async def list_sessions(
            user: NexusUser = Depends(get_current_user),
        ):
            """List all tracked QA sessions."""
            return {
                "success": True,
                "sessions": self.session_reasoner.list_sessions(),
            }

        # ── Tier Management ────────────────────────────────────

        @app.get("/api/v1/brain/tiers", response_model=TierStatusResponse)
        async def get_tier_status(
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Get the multi-tier provider configuration across all engines.

            Shows:
            - Overall deployment mode (on-prem / cloud / hybrid)
            - Per-engine tier configuration (active provider per tier)
            - Recommended tier mapping
            """
            start = time.monotonic()

            summary = self.tier_manager.get_deployment_summary()
            recommended = self.tier_manager.get_recommended_tiers()
            elapsed_ms = (time.monotonic() - start) * 1000

            return TierStatusResponse(
                success=True,
                trace_id=str(uuid.uuid4()),
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                overall_mode=summary["overall_mode"],
                total_engines=summary["total_engines"],
                cloud_engines=summary["cloud_engines"],
                hybrid_engines=summary["hybrid_engines"],
                onprem_engines=summary["onprem_engines"],
                engines=summary["engines"],
                recommended_tiers=recommended,
            )

        @app.get("/api/v1/brain/tiers/{engine_name}")
        async def get_engine_tiers(
            engine_name: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get tier configuration for a specific engine."""
            tiers = self.tier_manager.get_engine_tiers(engine_name)
            if not tiers:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown engine: {engine_name}",
                )
            return {
                "success": True,
                "engine": engine_name,
                "tiers": tiers,
            }

        # ── Ask Brain (Free-Form Q&A) ─────────────────────────

        @app.post("/api/v1/brain/ask", response_model=AskBrainResponse)
        async def ask_brain(
            req: AskBrainRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Ask the Brain a free-form question about QA processes.

            The Brain reasons using its LLM and cross-engine session
            context to provide intelligent answers.
            """
            start = time.monotonic()

            # Build context from session if available
            session_context = ""
            if req.session_id:
                state = self.session_reasoner.get_session(req.session_id)
                if state:
                    session_context = (
                        f"\nSession {req.session_id}: "
                        f"{len(state.rules)} rules, {len(state.test_cases)} tests, "
                        f"engines completed: {', '.join(state.engines_completed)}"
                    )

            system = (
                "You are the Brain of the Nexus QA platform — an expert AI coordinator "
                "for quality assurance workflows. You have knowledge of all 11 engines "
                "(Brain, Heart, Shield, Ears, Eyes, Backbone, Nerves, Legs, Hands, Spine, Mouth) "
                "and their multi-tier provider configurations. "
                "Answer questions about QA processes, platform capabilities, and session status."
            )

            user_prompt = req.question
            if req.context:
                user_prompt = f"Context: {req.context}\n\n{user_prompt}"
            if session_context:
                user_prompt = f"{session_context}\n\n{user_prompt}"

            raw = await self.llm.generate(system, user_prompt)

            try:
                parsed = json.loads(raw)
                answer = parsed.get("answer", raw)
                confidence = parsed.get("confidence", 0.5)
            except (json.JSONDecodeError, AttributeError):
                answer = raw
                confidence = 0.5

            elapsed_ms = (time.monotonic() - start) * 1000

            return AskBrainResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                answer=answer,
                confidence=confidence,
            )

        # ── Contradiction Detection ────────────────────────────

        @app.post("/api/v1/brain/detect-contradictions")
        async def detect_contradictions(
            req: dict,
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Analyse business rules for cross-session contradictions.

            Phase 2.5 — Compares rules from the current session against
            rules from other sessions in the same tenant. Uses LLM to
            determine if pairs are truly contradictory and assigns severity.

            Input:
                tenant_id, session_id, current_rules[], candidate_rules[]
            Output:
                contradictions[], warnings[], requires_human
            """
            start = time.monotonic()

            current_rules = req.get("current_rules", [])
            candidate_rules = req.get("candidate_rules", [])
            session_id = req.get("session_id", "")
            tenant_id = req.get("tenant_id", "")

            if not current_rules or not candidate_rules:
                return {
                    "success": True,
                    "contradictions": [],
                    "reasoning": "No rule pairs to compare.",
                    "requires_human": False,
                    "processing_time_ms": 0.0,
                }

            context = DecisionContext(
                session_id=session_id,
                tenant_id=tenant_id,
                decision_type=DecisionType.CONTRADICTION,
                rules_extracted=current_rules,
                engine_results={"candidate_rules": candidate_rules},
            )

            decision = await self.decision_engine.decide(context)
            elapsed_ms = (time.monotonic() - start) * 1000

            # Extract structured contradictions from decision output.
            # The LLM response is parsed by DecisionEngine into parameters,
            # but contradictions may land at different nesting levels.
            contradictions = decision.parameters.get("contradictions", [])
            if not contradictions and isinstance(decision.reasoning, str):
                # Fallback: try to parse contradictions from the reasoning JSON
                try:
                    import json as _json
                    parsed = _json.loads(decision.reasoning)
                    contradictions = parsed.get("contradictions", [])
                except (ValueError, TypeError, AttributeError):
                    pass

            return {
                "success": True,
                "decision_id": decision.decision_id,
                "contradictions": contradictions,
                "reasoning": decision.reasoning,
                "confidence": decision.confidence,
                "warnings": decision.warnings,
                "requires_human": decision.requires_human,
                "processing_time_ms": round(elapsed_ms, 2),
            }

        # ── Shared JSON repair helper ──────────────────────────

        def _repair_truncated_json(text: str) -> str:
            """Attempt to close truncated JSON by balancing braces/brackets."""
            open_braces = text.count('{') - text.count('}')
            open_brackets = text.count('[') - text.count(']')
            # Strip trailing comma or partial tokens
            text = text.rstrip()
            if text.endswith(','):
                text = text[:-1]
            # Remove incomplete trailing key-value
            last_brace = max(text.rfind('}'), text.rfind(']'), text.rfind('"'))
            if last_brace > 0:
                text = text[:last_brace + 1]
                # Recount after trim
                open_braces = text.count('{') - text.count('}')
                open_brackets = text.count('[') - text.count(']')
            text += ']' * max(0, open_brackets)
            text += '}' * max(0, open_braces)
            return text

        def _pairwise_combinations(variables: list[E2EVariable], max_pairs: int = 30) -> list[dict]:
            """Generate pairwise (all-pairs) test combinations from extracted variables.

            Uses a greedy algorithm: for each uncovered pair of (var_a=val_x, var_b=val_y),
            extend an existing combination or create a new one.  The result is a compact
            set of value-dicts that covers every 2-way interaction at least once.

            Returns a list of dicts like [{"Gender": "Male", "State": "Texas"}, ...].
            """
            if len(variables) < 2:
                # Single variable — just enumerate its values
                if variables:
                    v = variables[0]
                    all_vals = (v.observed_values + v.inferred_values)[:10]
                    return [{v.name: val} for val in all_vals] if all_vals else []
                return []

            # Build value lists per variable (observed first, then inferred — cap per var)
            var_names: list[str] = []
            var_values: list[list[str]] = []
            for v in variables:
                vals = list(dict.fromkeys(v.observed_values + v.inferred_values))[:8]
                if vals:
                    var_names.append(v.name)
                    var_values.append(vals)

            if len(var_names) < 2:
                return []

            # Collect all pairs that must be covered
            uncovered: set[tuple[int, str, int, str]] = set()
            for i in range(len(var_names)):
                for j in range(i + 1, len(var_names)):
                    for vi in var_values[i]:
                        for vj in var_values[j]:
                            uncovered.add((i, vi, j, vj))

            combos: list[dict[str, str]] = []

            while uncovered and len(combos) < max_pairs:
                # Greedy: pick the pair that appears most in uncovered
                # then extend the best existing combo or create new
                best_combo_idx = -1
                best_pair = None
                best_score = -1

                # Sample up to 200 uncovered pairs to keep this fast
                sample = list(uncovered)[:200]
                for pair in sample:
                    i, vi, j, vj = pair
                    # Check which existing combos could absorb this pair
                    for idx, combo in enumerate(combos):
                        ci = combo.get(var_names[i])
                        cj = combo.get(var_names[j])
                        if (ci is None or ci == vi) and (cj is None or cj == vj):
                            # Count how many additional uncovered pairs this would cover
                            score = 0
                            test_combo = dict(combo)
                            test_combo[var_names[i]] = vi
                            test_combo[var_names[j]] = vj
                            for p2 in sample[:50]:
                                i2, vi2, j2, vj2 = p2
                                if test_combo.get(var_names[i2]) == vi2 and test_combo.get(var_names[j2]) == vj2:
                                    score += 1
                            if score > best_score:
                                best_score = score
                                best_combo_idx = idx
                                best_pair = pair

                if best_pair is None:
                    # No existing combo can absorb — pick first uncovered and make new
                    best_pair = sample[0]

                i, vi, j, vj = best_pair

                if best_combo_idx >= 0 and best_score > 0:
                    combos[best_combo_idx][var_names[i]] = vi
                    combos[best_combo_idx][var_names[j]] = vj
                    combo = combos[best_combo_idx]
                else:
                    combo = {var_names[i]: vi, var_names[j]: vj}
                    combos.append(combo)

                # Remove all pairs now covered by this combo
                newly_covered = set()
                for p in list(uncovered):
                    pi, pvi, pj, pvj = p
                    if combo.get(var_names[pi]) == pvi and combo.get(var_names[pj]) == pvj:
                        newly_covered.add(p)
                uncovered -= newly_covered

            # Fill any missing variable slots with first observed value
            for combo in combos:
                for k, name in enumerate(var_names):
                    if name not in combo and var_values[k]:
                        combo[name] = var_values[k][0]

            return combos

        def _deduplicate_e2e_scenarios(
            new_scenarios: list[E2EScenario],
            existing_scenarios: list[dict],
        ) -> list[E2EScenario]:
            """Remove E2E scenarios that substantially overlap with existing test scenarios.

            Overlap is detected via:
              1. workflow_steps_covered intersection (>= 80% overlap)
              2. title similarity (normalised keyword overlap >= 0.6)
              3. data_matrix value overlap (>= 80% same values)

            Only removes if TWO or more signals fire.
            """
            if not existing_scenarios:
                return new_scenarios

            # Pre-process existing scenarios into comparable forms
            existing_steps: list[set[int]] = []
            existing_titles: list[set[str]] = []
            existing_data_vals: list[set[str]] = []
            _STOP_WORDS = {"the", "a", "an", "for", "with", "and", "or", "to", "of", "in", "is", "on", "at"}

            for es in existing_scenarios:
                if not isinstance(es, dict):
                    continue
                # Steps covered
                ws = es.get("workflow_step_number")
                step_set: set[int] = set()
                if isinstance(ws, int):
                    step_set.add(ws)
                # Also check nested test_cases for step data
                for tc in (es.get("test_cases") or []):
                    if isinstance(tc, dict):
                        for s in (tc.get("steps") or []):
                            if isinstance(s, dict) and s.get("step_number"):
                                step_set.add(s["step_number"])
                existing_steps.append(step_set)

                # Title keywords
                title = (es.get("description", "") or es.get("title", "")).lower()
                words = {w for w in title.split() if len(w) > 2 and w not in _STOP_WORDS}
                existing_titles.append(words)

                # Data values from test cases
                vals: set[str] = set()
                for tc in (es.get("test_cases") or []):
                    if isinstance(tc, dict):
                        for s in (tc.get("steps") or []):
                            if isinstance(s, dict):
                                inp = (s.get("input_data") or "").strip()
                                if inp and inp.lower() not in ("n/a", "none", ""):
                                    vals.add(inp.lower())
                existing_data_vals.append(vals)

            kept: list[E2EScenario] = []
            for sc in new_scenarios:
                signals = 0

                # Signal 1: workflow step overlap
                new_steps = set(sc.workflow_steps_covered)
                if new_steps:
                    for es_steps in existing_steps:
                        if es_steps:
                            overlap = len(new_steps & es_steps) / len(new_steps)
                            if overlap >= 0.8:
                                signals += 1
                                break

                # Signal 2: title keyword overlap
                new_words = {w for w in sc.title.lower().split() if len(w) > 2 and w not in _STOP_WORDS}
                if new_words:
                    for es_words in existing_titles:
                        if es_words:
                            common = len(new_words & es_words)
                            sim = common / max(len(new_words), 1)
                            if sim >= 0.6:
                                signals += 1
                                break

                # Signal 3: data matrix value overlap
                new_vals: set[str] = set()
                for dm_entry in sc.data_matrix:
                    if isinstance(dm_entry, dict):
                        for v in dm_entry.values():
                            if isinstance(v, str) and v.lower() not in ("n/a", "none", ""):
                                new_vals.add(v.lower())
                if new_vals:
                    for es_vals in existing_data_vals:
                        if es_vals:
                            overlap = len(new_vals & es_vals) / max(len(new_vals), 1)
                            if overlap >= 0.8:
                                signals += 1
                                break

                if signals >= 2:
                    logger.info(
                        "E2E dedup: dropping '%s' (%d overlap signals with existing scenarios)",
                        sc.title, signals,
                    )
                else:
                    kept.append(sc)

            if len(kept) < len(new_scenarios):
                logger.info(
                    "E2E dedup: kept %d / %d scenarios (removed %d duplicates)",
                    len(kept), len(new_scenarios), len(new_scenarios) - len(kept),
                )
            return kept

        def _synthesize_e2e_scenarios(
            req: GenerateE2EArchitectRequest,
            variables: list[E2EVariable],
            decision_points: list[DecisionPoint],
            pairwise_combos: list[dict],
        ) -> list[E2EScenario]:
            """Build grounded E2E scenarios when compact local models omit them."""

            workflows = [w for w in ((req.domain_map or {}).get("workflows", []) or []) if isinstance(w, dict)]
            workflow_steps = [int(w.get("step_number") or 0) for w in workflows if int(w.get("step_number") or 0) > 0]
            workflow_labels = [str(w.get("name") or f"Step {index + 1}") for index, w in enumerate(workflows[:4])]
            app_label = ", ".join(req.application_types_seen) if req.application_types_seen else "captured application"

            evidence_sources: list[EvidenceCitation] = []
            for variable in variables[:3]:
                if variable.source:
                    evidence_sources.append(EvidenceCitation(
                        text=variable.source,
                        source_modality="visual",
                        confidence=0.75,
                    ))
            for decision_point in decision_points[:2]:
                if decision_point.source:
                    evidence_sources.append(EvidenceCitation(
                        text=decision_point.source,
                        source_modality="visual",
                        confidence=0.75,
                    ))
            if not evidence_sources and req.visual_summary:
                evidence_sources.append(EvidenceCitation(
                    text=req.visual_summary[:200],
                    source_modality="visual",
                    confidence=0.6,
                ))

            observed_combo: dict[str, str] = {}
            for variable in variables:
                values = [value for value in (variable.observed_values or []) if value]
                if not values:
                    values = [value for value in (variable.inferred_values or []) if value]
                if values:
                    observed_combo[variable.name] = values[0]

            risk_areas = []
            for variable in variables:
                risk_areas.extend(variable.impacts or [])
            for decision_point in decision_points:
                if decision_point.condition:
                    risk_areas.append(decision_point.condition)
            risk_areas = list(dict.fromkeys(risk_areas))[:6]

            def _build_steps(combo: dict[str, str], observed: bool) -> list[TestStep]:
                steps: list[TestStep] = []
                input_summary = ", ".join(f"{key}={value}" for key, value in list(combo.items())[:3])
                for step_number, step_label in enumerate(workflow_labels, start=1):
                    steps.append(TestStep(
                        step_number=step_number,
                        action=f"Execute {step_label}",
                        input_data=input_summary,
                        expected_behavior=(
                            "Observed workflow path completes successfully"
                            if observed else
                            "The system applies the selected values and routes correctly"
                        ),
                    ))
                if not steps:
                    steps.append(TestStep(
                        step_number=1,
                        action=f"Exercise {app_label}",
                        input_data=input_summary,
                        expected_behavior="The workflow accepts the supplied values without validation errors",
                    ))
                return steps

            scenarios: list[E2EScenario] = []
            if observed_combo:
                scenarios.append(E2EScenario(
                    scenario_id="E2E-001",
                    title="Observed workflow with demonstrated values",
                    category="observed",
                    priority="P0_critical",
                    rationale="Anchors the generated suite to the exact variable path demonstrated in the recording.",
                    evidence_sources=evidence_sources[:3],
                    preconditions=[f"User can access {app_label}", "Required upstream systems are available"],
                    steps=_build_steps(observed_combo, observed=True),
                    expected_outcome="The demonstrated workflow path completes successfully with the observed values.",
                    data_matrix=[observed_combo],
                    workflow_steps_covered=workflow_steps[:4],
                    risk_areas_addressed=risk_areas[:4],
                ))

            variant_combos: list[dict] = []
            for combo in pairwise_combos:
                if not isinstance(combo, dict) or not combo or combo == observed_combo:
                    continue
                variant_combos.append(combo)
                if len(variant_combos) >= 2:
                    break

            for index, combo in enumerate(variant_combos, start=len(scenarios) + 1):
                title_suffix = ", ".join(f"{key}={value}" for key, value in list(combo.items())[:2])
                scenarios.append(E2EScenario(
                    scenario_id=f"E2E-{index:03d}",
                    title=f"High-risk variant covering {title_suffix}",
                    category="inferred_high_risk",
                    priority="P1_high",
                    rationale="Covers a real pairwise combination extracted from the workflow when the compact local model omits structured scenarios.",
                    evidence_sources=evidence_sources[:2],
                    preconditions=[f"User can access {app_label}"],
                    steps=_build_steps(combo, observed=False),
                    expected_outcome="The workflow handles this variable combination without breaking downstream decision logic.",
                    data_matrix=[combo],
                    workflow_steps_covered=workflow_steps[:4],
                    risk_areas_addressed=risk_areas[:4],
                ))

            return scenarios

        def _synthesize_test_steps(w: dict) -> list:
            """Generate realistic test steps from workflow metadata.

            Parses the workflow step name to identify action types (Enter, Navigate,
            Select, Review, Click, etc.) and builds proper QA test step tuples using
            the available systems and description context.
            """
            name = w.get("name", "")
            desc = w.get("description", "")[:300]
            w_systems = w.get("systems", [])
            decisions = w.get("decisions", [])

            name_lower = name.lower().strip()
            system_name = w_systems[0] if w_systems else "the application"

            steps = []
            step_num = 1

            # Parse action type from step name
            if any(name_lower.startswith(kw) for kw in ("type ", "enter ", "input ")):
                # Extract the value being typed
                for prefix in ("type ", "enter ", "input "):
                    if name_lower.startswith(prefix):
                        value = name[len(prefix):].strip()
                        break
                steps.append({
                    "step_number": step_num,
                    "action": f'Enter "{value}" in {system_name}',
                    "input_data": value,
                    "expected_behavior": f"{system_name} accepts the input and processes it successfully",
                })
            elif any(name_lower.startswith(kw) for kw in ("launch ", "navigate ", "open ", "go to ")):
                for prefix in ("launch ", "navigate to ", "navigate ", "open ", "go to "):
                    if name_lower.startswith(prefix):
                        target = name[len(prefix):].strip()
                        break
                steps.append({
                    "step_number": step_num,
                    "action": f"Navigate to {target}",
                    "input_data": f"URL: {target}" if any(kw in name_lower for kw in ("website", "page", "site", "portal")) else target,
                    "expected_behavior": f"{target} loads successfully and is accessible",
                })
            elif any(name_lower.startswith(kw) for kw in ("select ", "choose ", "pick ")):
                for prefix in ("select ", "choose ", "pick "):
                    if name_lower.startswith(prefix):
                        option = name[len(prefix):].strip()
                        break
                steps.append({
                    "step_number": step_num,
                    "action": f"Select {option} from the available options",
                    "input_data": option,
                    "expected_behavior": f"Selected {option} is applied and reflected in {system_name}",
                })
            elif any(name_lower.startswith(kw) for kw in ("review ", "verify ", "check ", "validate ")):
                for prefix in ("review ", "verify ", "check ", "validate "):
                    if name_lower.startswith(prefix):
                        target = name[len(prefix):].strip()
                        break
                steps.append({
                    "step_number": step_num,
                    "action": f"Review {target} displayed on screen",
                    "input_data": "N/A",
                    "expected_behavior": f"{target} information is displayed correctly and matches expected values",
                })
            elif any(name_lower.startswith(kw) for kw in ("click ", "press ", "submit ", "confirm ", "tap ")):
                for prefix in ("click ", "press ", "submit ", "confirm ", "tap "):
                    if name_lower.startswith(prefix):
                        target = name[len(prefix):].strip()
                        break
                steps.append({
                    "step_number": step_num,
                    "action": f"Click on {target}",
                    "input_data": "N/A",
                    "expected_behavior": f"{target} action is processed and {system_name} responds appropriately",
                })
            elif name_lower.startswith("answer "):
                target = name[7:].strip()
                steps.append({
                    "step_number": step_num,
                    "action": f"Answer {target} as presented on screen",
                    "input_data": "Required answers/responses",
                    "expected_behavior": "System accepts the answers and proceeds to the next step",
                })
            else:
                # Generic fallback — still better than raw descriptions
                steps.append({
                    "step_number": step_num,
                    "action": f'Perform "{name}" in {system_name}',
                    "input_data": name,
                    "expected_behavior": f"{name} completes successfully in {system_name}",
                })

            # Add verification step
            step_num = len(steps) + 1
            steps.append({
                "step_number": step_num,
                "action": f"Verify {name} completed successfully",
                "input_data": "N/A",
                "expected_behavior": f"{system_name} confirms {name} was processed and shows expected state",
            })

            return steps

        # ── Process Oracle: Generate Persona Draft ─────────────

        @app.post("/api/v1/brain/generate-persona-draft", response_model=GeneratePersonaDraftResponse)
        async def generate_persona_draft(
            req: GeneratePersonaDraftRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate a Process Oracle persona draft from canonical artifact data.

            Brain analyses the transcript, visual analysis, and knowledge graph
            to produce a structured persona profile, domain map, and grounding
            contract.  Every claim is backed by evidence citations with source
            modality and confidence.

            Platform is responsible for loading the artifact and calling this
            endpoint with the normalised payload.  Brain owns generation logic
            and scoring only — not persistence or lifecycle.
            """
            start = time.monotonic()

            # ── Build the analysis prompt ──────────────────────
            # Truncation budget: keep total prompt under ~2000 tokens
            # for small models (1B). Larger models get more context.
            is_small_model = "1b" in (self.llm._model or "").lower() or "3b" in (self.llm._model or "").lower()
            transcript_limit = 1500 if is_small_model else 3000
            visual_limit = 800 if is_small_model else 1500
            segment_limit = 8 if is_small_model else 15
            scene_limit = 5 if is_small_model else 10
            max_gen_tokens = 1536 if is_small_model else 2048

            transcript = req.safe_transcript_text[:transcript_limit] if req.safe_transcript_text else ""
            visual = req.visual_summary[:visual_limit] if req.visual_summary else ""
            apps = ", ".join(req.application_types_seen) if req.application_types_seen else "none detected"
            scenes = "\n".join(f"- {d}" for d in (req.scene_descriptions or [])[:scene_limit])
            segments_sample = ""
            if req.transcript_segments:
                for seg in req.transcript_segments[:segment_limit]:
                    speaker = seg.get("speaker", "?")
                    text = seg.get("text", "").strip()
                    ts = seg.get("start", "")
                    if text:
                        segments_sample += f"[{ts}s] {speaker}: {text}\n"

            graph_summary = ""
            if req.visual_graph_nodes:
                node_types = {}
                for n in req.visual_graph_nodes[:50]:
                    t = n.get("type", "unknown")
                    node_types[t] = node_types.get(t, 0) + 1
                graph_summary = "Knowledge graph: " + ", ".join(f"{k}({v})" for k, v in node_types.items())

            # Compact prompt for small models; full schema for large models
            if is_small_model:
                system_prompt = (
                    "You are a process analyst. Return ONLY valid JSON.\n"
                    "Extract a domain expert persona from the recording transcript.\n"
                    "IMPORTANT: Use REAL names and terms from the transcript. Never output placeholder text.\n\n"
                    "EXAMPLE output for a banking walkthrough by John:\n"
                    '{"persona":{"name":"Banking Transaction Expert","description":"Expert in banking workflows demonstrated by John.",'
                    '"system_prompt":"Expert in banking transaction workflows.",'
                    '"capabilities":["rule_extraction","test_generation","knowledge_graph","compliance_check","data_generation"],'
                    '"specialty_domains":["banking","transactions"],'
                    '"avatar_icon":"shield-check"},'
                    '"domain_map":{"actors":[{"name":"John","role":"Tester demonstrating banking workflow","evidence":[{"text":"Hi I am John walking through banking app","source_modality":"transcript","timestamp_range":"0:00-0:05","confidence":0.95}]}],'
                    '"systems":[{"name":"Banking Portal","purpose":"Web app for transactions","evidence":[{"text":"open banking portal","source_modality":"transcript","confidence":0.9}]}],'
                    '"entities":["account","transaction"],'
                    '"workflows":['
                    '{"step_number":1,"name":"Open Portal","description":"Navigate to site","actors":["John"],"systems":["Banking Portal"],"decisions":[],"evidence":[{"text":"type bank.com","source_modality":"transcript","confidence":0.9}]},'
                    '{"step_number":2,"name":"Select Account","description":"Choose account type","actors":["John"],"systems":["Banking Portal"],"decisions":["Account type"],"evidence":[{"text":"click checking","source_modality":"transcript","confidence":0.85}]}],'
                    '"decisions":["Account type"],'
                    '"risks":[{"description":"No validation for limits","severity":"medium","evidence":[{"text":"enter amount and submit","source_modality":"transcript","confidence":0.7}]}],'
                    '"unknowns":["Authentication method"]},'
                    '"grounding_summary":{"total_evidence_count":5,"modality_distribution":{"transcript":5,"visual":0,"graph":0,"inferred":0},"avg_confidence":0.87,"open_questions":["What auth is used?"]}}\n\n'
                    "NOW generate for the transcript below. Extract ALL walkthrough steps (5-8 steps minimum).\n"
                    "Use REAL quotes from the transcript as evidence. Name persona after the specific domain.\n"
                    "avatar_icon: brain, shield-check, flask-conical, database, book-open, target, microscope, compass, lightbulb\n"
                )
            else:
                system_prompt = (
                    "You are a knowledge analyst. Analyse this recording and return a JSON Process Oracle persona.\n\n"
                    "CRITICAL: Use REAL names, systems, and workflow steps from the transcript. Do NOT use placeholder values.\n"
                    "Name the persona after the specific process/domain discussed in the recording.\n\n"
                    "Return ONLY valid JSON with this structure:\n"
                    "{\n"
                    '  "persona": {"name": "Domain-Specific Expert Title", "description": "2 detailed sentences about the expert", '
                    '"system_prompt": "Detailed prompt (50+ words) referencing domain terms from the recording", '
                    '"capabilities": ["rule_extraction",...], "specialty_domains": ["specific domain 1",...], '
                    '"avatar_icon": "brain|shield-check|flask-conical|database|book-open|target|microscope|compass|lightbulb"},\n'
                    '  "domain_map": {\n'
                    '    "actors": [{"name": "Actual Person Name", "role": "Their actual role from recording", "evidence": [{"text": "exact quote from transcript", "source_modality": "transcript|visual|graph|inferred", "timestamp_range": "M:SS-M:SS", "confidence": 0.0-1.0}]}],\n'
                    '    "systems": [{"name": "Actual System Name", "purpose": "What it does in the workflow", "evidence": [{"text": "exact quote mentioning system", "source_modality": "transcript", "confidence": 0.9}]}],\n'
                    '    "entities": ["actual concept from recording",...],\n'
                    '    "workflows": [{"step_number": 1, "name": "Actual Step Name", "description": "What happens in this step", "actors": ["Person Name"], "systems": ["System Name"], "decisions": ["Decision made"], "evidence": [{"text": "exact quote describing step", "source_modality": "transcript", "confidence": 0.85}]}],\n'
                    '    "decisions": ["Actual decision point from walkthrough",...],\n'
                    '    "risks": [{"description": "Actual risk identified from the process", "severity": "low|medium|high|critical", "evidence": [{"text": "quote showing the risk", "source_modality": "transcript", "confidence": 0.7}]}],\n'
                    '    "unknowns": ["Actual gap or unclear area from recording",...]\n'
                    "  },\n"
                    '  "grounding_summary": {"total_evidence_count": int, "modality_distribution": {"transcript": int, "visual": int, "graph": int, "inferred": int}, "avg_confidence": float, "open_questions": ["Specific question about the process"]}\n'
                    "}\n\n"
                    "RULES:\n"
                    "1. Every actor/system/workflow/risk MUST have evidence citations with REAL quoted text from the transcript.\n"
                    "2. Extract ALL workflow steps end-to-end (aim for 5-10 steps).\n"
                    "3. Capabilities from: rule_extraction, test_generation, knowledge_graph, contradiction_detection, compliance_check, data_generation, report_generation, test_execution.\n"
                    "4. confidence 0.8+ only for direct quotes. source_modality: transcript|visual|graph|inferred.\n"
                    "5. Use ACTUAL names and terms from the transcript - never use generic placeholders.\n"
                )

            user_prompt = "Analyse this knowledge transfer recording and generate the Process Oracle persona:\n\n"
            if transcript:
                user_prompt += f"## TRANSCRIPT\n{transcript}\n\n"
            if segments_sample:
                user_prompt += f"## SPEAKERS\n{segments_sample}\n\n"
            if visual:
                user_prompt += f"## VISUAL\n{visual}\n\n"
            if scenes:
                user_prompt += f"## SCENES\n{scenes}\n\n"
            if apps != "none detected":
                user_prompt += f"## APPS\n{apps}\n\n"
            if graph_summary:
                user_prompt += f"## {graph_summary}\n\n"
            user_prompt += (
                f"Duration: {req.duration_seconds:.0f}s | "
                f"Scenes: {req.scene_count} | "
                f"Frames: {req.frame_count}\n"
            )

            # ── Call LLM ───────────────────────────────────────
            llm_start = time.monotonic()
            try:
                raw = await self.llm.generate(
                    system_prompt,
                    user_prompt,
                    temperature=0.1,
                    max_tokens=max_gen_tokens,
                    json_mode=True,
                )
            except (RuntimeError, Exception) as llm_err:
                logger.error(
                    "brain.persona_draft.llm_failed: %s", llm_err,
                    extra={"artifact_id": req.artifact_id},
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"LLM generation unavailable: {llm_err}",
                )
            llm_elapsed_ms = (time.monotonic() - llm_start) * 1000

            # ── Parse response ─────────────────────────────────
            # Handle markdown code blocks
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()

            logger.info(
                "Raw LLM output for artifact=%s [%d chars]: %.500s",
                req.artifact_id, len(raw), raw[:500],
            )

            try:
                parsed = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError) as parse_err:
                parsed = None
                # Try to find and extract JSON
                json_start = cleaned.find('{')
                json_end = cleaned.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    try:
                        parsed = json.loads(cleaned[json_start:json_end + 1])
                        logger.info("Recovered JSON from within LLM output (offset %d-%d)", json_start, json_end)
                    except (json.JSONDecodeError, ValueError):
                        pass

                # Try repairing truncated JSON (output cut off by max_tokens)
                if parsed is None and json_start >= 0:
                    repaired = _repair_truncated_json(cleaned[json_start:])
                    try:
                        parsed = json.loads(repaired)
                        logger.info("Recovered JSON via truncation repair (%d chars)", len(repaired))
                    except (json.JSONDecodeError, ValueError):
                        pass

                if parsed is None:
                    # Fallback: build minimal draft from transcript
                    logger.warning(
                        "LLM returned unparseable persona draft for artifact=%s: %s — first 200 chars: %.200s",
                        req.artifact_id, parse_err, cleaned[:200],
                    )
                    parsed = {
                        "persona": {
                            "name": "Process Expert",
                            "description": f"Domain expert generated from a {req.duration_seconds:.0f}s recording with {req.scene_count} scenes.",
                            "system_prompt": f"You are a process expert. Your knowledge comes from a recorded walkthrough. Transcript: {transcript[:500]}",
                            "capabilities": ["rule_extraction", "knowledge_graph"],
                            "specialty_domains": [],
                            "avatar_icon": "user-circle",
                        },
                        "domain_map": {"actors": [], "systems": [], "entities": [], "workflows": [], "decisions": [], "risks": [], "unknowns": ["LLM failed to parse recording — manual review needed"]},
                        "grounding_summary": {"total_evidence_count": 0, "modality_distribution": {"transcript": 0, "visual": 0, "graph": 0, "inferred": 0}, "avg_confidence": 0.0, "open_questions": ["LLM parsing failed"]},
                    }

            # ── Post-process: fix LLM output quality issues ──────
            p = parsed.get("persona", {})
            dm = parsed.get("domain_map", {})
            gs = parsed.get("grounding_summary", {})

            # Fix avatar_icon: model may concatenate all options
            VALID_ICONS = {"brain", "shield-check", "flask-conical", "database",
                           "book-open", "target", "microscope", "compass", "lightbulb"}
            raw_icon = p.get("avatar_icon", "brain")
            if raw_icon not in VALID_ICONS:
                # Try to extract first valid icon from comma-separated string
                for candidate in raw_icon.replace(",", " ").split():
                    candidate = candidate.strip().lower()
                    if candidate in VALID_ICONS:
                        raw_icon = candidate
                        break
                else:
                    raw_icon = "brain"
            p["avatar_icon"] = raw_icon

            # Fix capabilities: validate against allowed list
            VALID_CAPS = {"rule_extraction", "test_generation", "knowledge_graph",
                          "contradiction_detection", "compliance_check", "data_generation",
                          "report_generation", "test_execution"}
            raw_caps = p.get("capabilities", [])
            fixed_caps = [c for c in raw_caps if c in VALID_CAPS]
            if len(fixed_caps) < 3:
                fixed_caps = ["rule_extraction", "test_generation", "knowledge_graph",
                              "compliance_check", "data_generation"]
            p["capabilities"] = fixed_caps

            # Extract walkthrough steps from transcript if LLM under-generated
            existing_workflows = [w for w in dm.get("workflows", []) if isinstance(w, dict)]
            if len(existing_workflows) < 3 and transcript:
                import re as _re
                # Split transcript into sentences and detect action phrases
                action_patterns = _re.compile(
                    r'\b(click|select|choose|type|enter|navigate|go to|open|launch|submit|'
                    r'scroll|check|verify|fill|input|press|tap|drag|search|log\s*in|sign\s*in|'
                    r'first step|next step|now|then|after that)\b',
                    _re.IGNORECASE,
                )
                # Split on sentence boundaries
                sentences = _re.split(r'[.!?]+', transcript)
                steps = []
                for sent in sentences:
                    sent = sent.strip()
                    if not sent or len(sent) < 15:
                        continue
                    if action_patterns.search(sent):
                        steps.append(sent)

                if steps:
                    # Build workflow steps from extracted sentences
                    extracted_workflows = []
                    # Detect actor name from first sentence or LLM output
                    actor_name = ""
                    llm_actors = dm.get("actors", [])
                    if llm_actors and isinstance(llm_actors[0], dict):
                        actor_name = llm_actors[0].get("name", "")
                    if not actor_name:
                        # Try to detect from transcript intro
                        intro_match = _re.search(r'(?:myself is|my name is|I am|this is)\s+(\w+)', transcript, _re.IGNORECASE)
                        if intro_match:
                            actor_name = intro_match.group(1)

                    # Detect system name from LLM output
                    system_name = ""
                    llm_systems = dm.get("systems", [])
                    if llm_systems and isinstance(llm_systems[0], dict):
                        system_name = llm_systems[0].get("name", "")

                    def _clean_step_name(text: str, original_text: str = "") -> str:
                        """Extract a short, professional step name from conversational transcript text."""
                        orig = original_text or text
                        t = text.lower().strip()
                        # Strip filler phrases
                        t = _re.sub(
                            r'^(okay |so |now |and |um |uh |like |I hope you can able to see,?\s*'
                            r'|what are the |first step is,?\s*|you have to |I am |and now )',
                            '', t, flags=_re.IGNORECASE,
                        )
                        # Try to extract "verb + object" patterns — use FIRST good match
                        verb_pattern = _re.compile(
                            r'(type|click(?:ing)?|select(?:ing)?|enter(?:ing)?|choos(?:e|ing)|navigat(?:e|ing)|'
                            r'launch(?:ing)?|open(?:ing)?|submit(?:ting)?|go(?:ing)? to|get(?:ting)?)\s+'
                            r'(?:on |to |the |it with |a |either |that |like |drop )*'
                            r'([\w][\w\s\.]{2,}?)(?:\s*,|\s*\.|$)',
                            _re.IGNORECASE,
                        )
                        for m in verb_pattern.finditer(t):
                            verb_raw = m.group(1).strip().lower()
                            obj = m.group(2).strip()
                            obj = _re.sub(r'\s+(you|I|we|it|the|and|is|are|was|from|so|like|how|many|okay)\s*$', '', obj).strip()
                            if not obj or len(obj) <= 2:
                                continue
                            # Normalize verb to base form
                            verb_base = _re.sub(r'(ing|ting)$', '', verb_raw)
                            if verb_base in ('click', 'select', 'enter', 'choos', 'navigat', 'launch',
                                             'open', 'submit', 'go', 'get', 'typ'):
                                _VB_MAP = {'choos': 'Choose', 'navigat': 'Navigate', 'go': 'Go To',
                                           'typ': 'Type', 'get': 'Get'}
                                verb = _VB_MAP.get(verb_base, verb_base.capitalize())
                            else:
                                verb = verb_raw.capitalize()
                            name = f"{verb} {_title_preserve_acronyms(obj, orig)}"
                            if len(name) > 45:
                                name = name[:42].rsplit(" ", 1)[0] + "..."
                            return name
                        # Try domain-keyword extraction — broad set of nouns commonly found in walkthroughs
                        domain = _re.search(
                            r'(premium|insurance|quote|state|gender|age|year|question|validate'
                            r'|sharing|coverage|amount|application|website|height|weight|button'
                            r'|account|balance|transaction|transfer|payment|order|invoice|claim'
                            r'|patient|record|report|form|profile|settings|dashboard|login'
                            r'|search|filter|cart|checkout|product|service|ticket|request'
                            r'|document|file|folder|message|notification|approval|review)',
                            t, _re.IGNORECASE,
                        )
                        if domain:
                            keyword = domain.group(1).title()
                            # Try to find an associated verb
                            pre = t[:domain.start()].strip()
                            vb = _re.search(
                                r'(see|review|view|check|answer|stop|start|complete|verify|enter(?:ing)?|'
                                r'select(?:ing)?|click(?:ing)?|collect(?:ing)?)\s*$',
                                pre, _re.IGNORECASE,
                            )
                            if vb:
                                vb_word = _re.sub(r'ing$', '', vb.group(1)).capitalize()
                                return f"{vb_word} {keyword} Options" if keyword == "Premium" else f"{vb_word} {keyword}"
                            # If keyword is itself an action verb or has a natural pairing
                            _KW_ACTIONS = {
                                "validate": "Validate Results", "sharing": "Stop Sharing",
                                "question": "Answer Questions", "premium": "Review Premium Options",
                                "login": "Login To System", "checkout": "Complete Checkout",
                                "search": "Perform Search", "approval": "Submit For Approval",
                                "notification": "Check Notifications", "filter": "Apply Filters",
                            }
                            if keyword.lower() in _KW_ACTIONS:
                                return _KW_ACTIONS[keyword.lower()]
                            return f"Review {keyword}"
                        # Fallback: first 6 meaningful words
                        words = [w for w in t.split() if len(w) > 1][:6]
                        return _title_preserve_acronyms(" ".join(words).strip(), orig) if words else f"Step {i + 1}"

                    def _title_preserve_acronyms(s: str, original: str = "") -> str:
                        """Title-case but preserve acronyms (all-caps words >=2 chars, known tech terms)."""
                        _KNOWN = {"url", "api", "ui", "sql", "crm", "erp", "hr", "qa", "kt",
                                  "id", "uat", "sso", "pdf", "csv", "http", "json", "xml",
                                  "aws", "gcp", "saas", "ehr", "hipaa", "pci", "gdpr"}
                        # Build dynamic acronyms from original text (words that appear upper in source)
                        if original:
                            for ow in original.split():
                                cleaned = _re.sub(r'[^a-zA-Z]', '', ow)
                                if cleaned.isupper() and 2 <= len(cleaned) <= 6:
                                    _KNOWN.add(cleaned.lower())
                        words = s.split()
                        result = []
                        for w in words:
                            wl = w.lower()
                            # Preserve known acronyms
                            if wl in _KNOWN:
                                result.append(w.upper())
                            # Preserve words that look like acronyms (all-uppercase, 2-5 chars)
                            elif w.isupper() and 2 <= len(w) <= 5:
                                result.append(w)
                            # Preserve .com/.org domains
                            elif '.' in wl and any(wl.endswith(ext) for ext in ('.com', '.org', '.net', '.io')):
                                result.append(w)
                            else:
                                result.append(w.capitalize())
                        return " ".join(result)

                    # Pre-compute segment time estimates for when timestamps are missing
                    total_duration = 0
                    has_real_timestamps = False
                    if req.transcript_segments:
                        for seg in req.transcript_segments:
                            if seg.get("start", 0) > 0 or seg.get("end", 0) > 0:
                                has_real_timestamps = True
                                break
                        if not has_real_timestamps:
                            # Estimate: assume even distribution. Use transcript metadata if available
                            total_duration = getattr(req, "duration_seconds", 0) or len(req.transcript_segments) * 6.0

                    for i, step_text in enumerate(steps[:10]):
                        step_name = _clean_step_name(step_text)

                        # Find best-matching timestamp from segments
                        ts_range = None
                        if req.transcript_segments:
                            step_words = set(step_text.lower().split())
                            best_overlap = 0
                            best_seg_idx = -1
                            for seg_idx, seg in enumerate(req.transcript_segments):
                                seg_text = seg.get("text", "")
                                seg_words = set(seg_text.lower().split())
                                overlap = len(step_words & seg_words)
                                if overlap > best_overlap and overlap >= 3:
                                    best_overlap = overlap
                                    best_seg_idx = seg_idx
                            if best_seg_idx >= 0:
                                seg = req.transcript_segments[best_seg_idx]
                                start_s = seg.get("start", 0)
                                end_s = seg.get("end", 0)
                                if has_real_timestamps and (start_s > 0 or end_s > 0):
                                    ts_range = f"{int(start_s//60)}:{int(start_s%60):02d}-{int(end_s//60)}:{int(end_s%60):02d}"
                                elif total_duration > 0:
                                    # Estimate based on segment position
                                    n_segs = len(req.transcript_segments)
                                    est_start = (best_seg_idx / n_segs) * total_duration
                                    est_end = ((best_seg_idx + 1) / n_segs) * total_duration
                                    ts_range = f"{int(est_start//60)}:{int(est_start%60):02d}-{int(est_end//60)}:{int(est_end%60):02d}"

                        extracted_workflows.append({
                            "step_number": i + 1,
                            "name": step_name,
                            "description": step_text,
                            "actors": [actor_name] if actor_name else [],
                            "systems": [system_name] if system_name else [],
                            "decisions": [],
                            "evidence": [{
                                "text": step_text,
                                "source_modality": "transcript",
                                "timestamp_range": ts_range,
                                "confidence": 0.85,
                            }],
                        })

                    # Use extracted steps (they're more complete than LLM's 1 step)
                    if len(extracted_workflows) > len(existing_workflows):
                        dm["workflows"] = extracted_workflows
                        logger.info(
                            "Enriched domain_map: extracted %d workflow steps from transcript (LLM generated %d)",
                            len(extracted_workflows), len(existing_workflows),
                        )

            # Enrich grounding summary counts from actual evidence
            all_evidence = []
            for actor in dm.get("actors", []):
                if isinstance(actor, dict):
                    all_evidence.extend(actor.get("evidence", []))
            for sys in dm.get("systems", []):
                if isinstance(sys, dict):
                    all_evidence.extend(sys.get("evidence", []))
            for wf in dm.get("workflows", []):
                if isinstance(wf, dict):
                    all_evidence.extend(wf.get("evidence", []))
            for risk in dm.get("risks", []):
                if isinstance(risk, dict):
                    all_evidence.extend(risk.get("evidence", []))

            if all_evidence:
                transcript_count = sum(1 for e in all_evidence if isinstance(e, dict) and e.get("source_modality") == "transcript")
                visual_count = sum(1 for e in all_evidence if isinstance(e, dict) and e.get("source_modality") == "visual")
                gs["total_evidence_count"] = len(all_evidence)
                gs["modality_distribution"] = {
                    "transcript": transcript_count,
                    "visual": visual_count,
                    "graph": sum(1 for e in all_evidence if isinstance(e, dict) and e.get("source_modality") == "graph"),
                    "inferred": sum(1 for e in all_evidence if isinstance(e, dict) and e.get("source_modality") == "inferred"),
                }
                confs = [e.get("confidence", 0.5) for e in all_evidence if isinstance(e, dict)]
                gs["avg_confidence"] = round(sum(confs) / len(confs), 2) if confs else 0.0

            def _parse_evidence(ev_list):
                result = []
                for ev in (ev_list or []):
                    if isinstance(ev, dict):
                        result.append(EvidenceCitation(
                            text=ev.get("text", ""),
                            source_modality=ev.get("source_modality", "inferred"),
                            timestamp_range=ev.get("timestamp_range"),
                            confidence=min(max(float(ev.get("confidence", 0.5)), 0.0), 1.0),
                        ))
                return result

            persona = PersonaProfile(
                name=p.get("name", "Process Expert"),
                description=p.get("description", ""),
                system_prompt=p.get("system_prompt", ""),
                capabilities=p.get("capabilities", []),
                specialty_domains=p.get("specialty_domains", []),
                avatar_icon=p.get("avatar_icon", "user-circle"),
                stage_config=p.get("stage_config", {
                    "1_capture": {"engines": ["ears", "eyes", "spine", "shield"], "auto_advance": False},
                    "2_understand": {"engines": ["heart", "backbone", "nerves"], "auto_advance": False},
                    "3_strategize": {"engines": ["heart", "nerves"], "auto_advance": False},
                    "4_generate": {"engines": ["legs", "hands", "mouth"], "auto_advance": False},
                    "5_validate": {"engines": ["legs", "nerves"], "auto_advance": False},
                }),
            )

            def _to_str_list(items):
                """Normalise a list that might contain dicts to a list of strings."""
                result = []
                for item in (items or []):
                    if isinstance(item, str):
                        result.append(item)
                    elif isinstance(item, dict):
                        # Prefer name, then description, then str(dict)
                        result.append(item.get("name") or item.get("description") or str(item))
                    else:
                        result.append(str(item))
                return result

            domain_map = DomainMap(
                actors=[DomainActor(name=a.get("name", ""), role=a.get("role", ""), evidence=_parse_evidence(a.get("evidence"))) for a in dm.get("actors", []) if isinstance(a, dict)],
                systems=[DomainSystem(name=s.get("name", ""), purpose=s.get("purpose", ""), evidence=_parse_evidence(s.get("evidence"))) for s in dm.get("systems", []) if isinstance(s, dict)],
                entities=_to_str_list(dm.get("entities", [])),
                workflows=[
                    DomainWorkflow(
                        step_number=w.get("step_number", i + 1),
                        name=w.get("name", ""),
                        description=w.get("description", ""),
                        actors=_to_str_list(w.get("actors", [])),
                        systems=_to_str_list(w.get("systems", [])),
                        decisions=_to_str_list(w.get("decisions", [])),
                        evidence=_parse_evidence(w.get("evidence")),
                    )
                    for i, w in enumerate(dm.get("workflows", []))
                    if isinstance(w, dict)
                ],
                decisions=_to_str_list(dm.get("decisions", [])),
                risks=[DomainRisk(description=r.get("description", ""), severity=r.get("severity", "medium"), evidence=_parse_evidence(r.get("evidence"))) for r in dm.get("risks", []) if isinstance(r, dict)],
                unknowns=_to_str_list(dm.get("unknowns", [])),
            )

            grounding = {
                "total_evidence_count": gs.get("total_evidence_count", 0),
                "modality_distribution": gs.get("modality_distribution", {}),
                "avg_confidence": gs.get("avg_confidence", 0.0),
                "open_questions": gs.get("open_questions", []),
            }

            provenance = PersonaDraftProvenance(
                artifact_id=req.artifact_id,
                session_id=req.session_id or "",
                workflow_id=req.workflow_id or "",
                generated_at=datetime.now(timezone.utc).isoformat(),
                model_used=self.llm._model,
                model_backend=self.llm._backend,
                generation_time_ms=round(llm_elapsed_ms, 2),
                quality_score_threshold=self.config.quality_pass_threshold,
                artifact_quality_score=req.quality_score,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return GeneratePersonaDraftResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                persona=persona,
                domain_map=domain_map,
                grounding_contract=grounding,
                provenance=provenance,
            )

        # ── Test Architect: Generate Test Strategy ──────────────

        @app.post("/api/v1/brain/generate-test-strategy", response_model=GenerateTestStrategyResponse)
        async def generate_test_strategy(
            req: GenerateTestStrategyRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate a complete test strategy from a persona draft's domain map.

            Takes the SME persona's extracted domain knowledge (workflow steps,
            risks, actors, systems) and generates structured test scenarios with
            full traceability back to the original KT recording evidence.

            Each test case links to specific evidence from the recording
            transcript — so QA engineers can verify WHY each test exists.
            """
            start = time.monotonic()

            dm = req.domain_map
            workflows = dm.get("workflows", [])
            risks = dm.get("risks", [])
            actors = dm.get("actors", [])
            systems = dm.get("systems", [])
            entities = dm.get("entities", [])

            # ── Build compact domain summary for LLM prompt ────
            workflow_text = ""
            for w in workflows:
                if isinstance(w, dict):
                    sn = w.get("step_number", "?")
                    nm = w.get("name", "?")
                    desc = w.get("description", "")[:200]
                    acts = ", ".join(w.get("actors", []))
                    syss = ", ".join(w.get("systems", []))
                    decs = ", ".join(w.get("decisions", []))
                    evs = w.get("evidence", [])
                    # Pass ALL evidence items, not just the first
                    ev_lines = []
                    for ev_item in (evs if isinstance(evs, list) else []):
                        if isinstance(ev_item, dict):
                            ev_t = ev_item.get("text", "")[:200]
                            ev_mod = ev_item.get("source_modality", "")
                            if ev_t:
                                ev_lines.append(f'"{ev_t}" [{ev_mod}]' if ev_mod else f'"{ev_t}"')
                    workflow_text += f"Step {sn}: {nm} — {desc}\n"
                    if acts:
                        workflow_text += f"  Actors: {acts}\n"
                    if syss:
                        workflow_text += f"  Systems: {syss}\n"
                    if decs:
                        workflow_text += f"  Decisions: {decs}\n"
                    if ev_lines:
                        workflow_text += f"  Evidence: {'; '.join(ev_lines)}\n"

            risk_text = ""
            for r in risks:
                if isinstance(r, dict):
                    risk_text += f"- [{r.get('severity','medium').upper()}] {r.get('description','')[:120]}\n"

            system_text = ", ".join(s.get("name", "") for s in systems if isinstance(s, dict)) or "Unknown"
            entity_text = ", ".join(entities[:15]) if entities else "none extracted"

            is_small_model = "1b" in (self.llm._model or "").lower() or "3b" in (self.llm._model or "").lower()
            max_gen_tokens = 1536 if is_small_model else 2048

            system_prompt = (
                "You are a Senior Test Architect. Return ONLY valid JSON.\n"
                "Analyse the domain knowledge below and generate a comprehensive test strategy.\n"
                "For EACH workflow step, generate test scenarios with test cases.\n\n"
                "CATEGORIES: happy_path, negative, boundary, edge_case\n"
                "PRIORITIES: P0_critical (blocks release), P1_high (important), P2_medium (standard), P3_low (nice-to-have)\n"
                "TAGS: smoke, regression, critical, sanity, integration, e2e\n\n"
                "Return JSON:\n"
                '{"test_plan":{"name":"<Domain> Test Strategy","objective":"Validate...","scope":"Covers N steps...","approach":"risk-based"},'
                '"test_scenarios":['
                '{"scenario_id":"TS-001","workflow_step_number":1,"workflow_step_name":"Step Name","description":"Covers...",'
                '"test_cases":['
                '{"case_id":"TC-001","title":"Verify happy path for step","category":"happy_path","priority":"P1_high",'
                '"preconditions":["User is logged in to the application"],'
                '"steps":[{"step_number":1,"action":"Enter \\"USAA\\" in the search field","input_data":"USAA","expected_behavior":"System accepts the input and displays search results"},'
                '{"step_number":2,"action":"Verify search results are displayed","input_data":"N/A","expected_behavior":"Search results page loads with relevant USAA options"}],'
                '"expected_result":"Step completes successfully",'
                '"tags":["smoke","regression"],'
                '"evidence_trace":[{"text":"exact quote from KT","source_modality":"transcript","confidence":0.9}]}'
                "]}]}\n\n"
                "RULES:\n"
                "1. Generate 2-4 test cases per workflow step (happy + negative minimum)\n"
                "2. P0/P1 for steps with risks or decisions. P2/P3 for straightforward steps\n"
                "3. evidence_trace: Use REAL quotes from the evidence provided — link each test to the KT recording\n"
                "4. Include boundary/edge cases for steps with data entry or validation\n"
                "5. preconditions must reference actual systems and states from the domain\n"
                "6. CRITICAL — steps.action MUST be proper software test actions like 'Enter \"USAA\" in the search field', "
                "'Click on Life Insurance link', 'Select state from dropdown', 'Navigate to Premium Options page'. "
                "steps.input_data MUST be the actual data value like 'USAA', 'Male', '25', 'Texas' — NOT generic descriptions. "
                "NEVER use raw transcript text, 'Execute workflow action', or 'As per standard operating procedure'.\n"
                "7. steps.input_data should contain specific test data examples where applicable\n"
                "8. steps.expected_behavior should describe the expected UI/system response\n"
            )

            user_prompt = f"Generate test strategy for this domain:\n\n"
            user_prompt += f"## PERSONA: {req.persona_name}\n{req.persona_description}\n\n"
            user_prompt += f"## SYSTEMS: {system_text}\n"
            user_prompt += f"## ENTITIES: {entity_text}\n\n"
            user_prompt += f"## WORKFLOW STEPS\n{workflow_text}\n"
            if risk_text:
                user_prompt += f"## IDENTIFIED RISKS\n{risk_text}\n"
            user_prompt += f"\nDuration: {req.duration_seconds:.0f}s | Steps: {len(workflows)} | Risks: {len(risks)}\n"
            user_prompt += "Generate complete test scenarios for ALL workflow steps.\n"

            # ── Call LLM ───────────────────────────────────────
            llm_start = time.monotonic()
            try:
                raw = await self.llm.generate(
                    system_prompt,
                    user_prompt,
                    temperature=0.15,
                    max_tokens=max_gen_tokens,
                    json_mode=True,
                )
            except (RuntimeError, Exception) as llm_err:
                logger.error(
                    "brain.test_strategy.llm_failed: %s", llm_err,
                    extra={"artifact_id": req.artifact_id},
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"LLM generation unavailable: {llm_err}",
                )
            llm_elapsed_ms = (time.monotonic() - llm_start) * 1000

            # ── Parse response ─────────────────────────────────
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()

            logger.info(
                "Raw test-strategy LLM output for artifact=%s [%d chars]: %.500s",
                req.artifact_id, len(raw), raw[:500],
            )

            try:
                parsed = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                parsed = None
                json_start = cleaned.find('{')
                json_end = cleaned.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    try:
                        parsed = json.loads(cleaned[json_start:json_end + 1])
                    except (json.JSONDecodeError, ValueError):
                        pass
                # Try truncation repair
                if parsed is None and json_start >= 0:
                    repaired = _repair_truncated_json(cleaned[json_start:])
                    try:
                        parsed = json.loads(repaired)
                        logger.info("Recovered test-strategy JSON via truncation repair")
                    except (json.JSONDecodeError, ValueError):
                        pass
                if parsed is None:
                    logger.warning("LLM returned unparseable test strategy — building fallback")
                    parsed = {"test_plan": {}, "test_scenarios": []}

            # ── Post-process: build structured response ────────
            raw_plan = parsed.get("test_plan", {})
            raw_scenarios = parsed.get("test_scenarios", [])
            # Guard: LLM may return a dict instead of a list
            if isinstance(raw_scenarios, dict):
                raw_scenarios = [raw_scenarios]
            elif not isinstance(raw_scenarios, list):
                raw_scenarios = []

            # Ensure all workflow steps have at least one scenario
            covered_steps = {s.get("workflow_step_number") for s in raw_scenarios if isinstance(s, dict)}
            # Build lookup of step names for precondition references
            step_name_map = {}
            for w in workflows:
                if isinstance(w, dict):
                    step_name_map[w.get("step_number", 0)] = w.get("name", f"Step {w.get('step_number', '?')}")

            for w in workflows:
                if isinstance(w, dict):
                    sn = w.get("step_number", 0)
                    if sn not in covered_steps:
                        # Generate a minimal happy-path case for uncovered steps
                        ev = w.get("evidence", [])
                        ev_trace = []
                        for ev_item in (ev if isinstance(ev, list) else []):
                            if isinstance(ev_item, dict) and ev_item.get("text"):
                                ev_trace.append({"text": ev_item["text"], "source_modality": ev_item.get("source_modality", "transcript"), "confidence": 0.8})
                        # Use real precondition from prior step
                        preconditions = []
                        if sn > 1 and (sn - 1) in step_name_map:
                            preconditions.append(f"{step_name_map[sn - 1]} completed successfully")
                        w_actors = w.get("actors", [])
                        w_systems = w.get("systems", [])
                        if w_actors:
                            preconditions.append(f"Actor: {w_actors[0]} is available")
                        if w_systems:
                            preconditions.append(f"System: {w_systems[0]} is accessible")
                        if not preconditions:
                            preconditions = ["System is in ready state"]
                        raw_scenarios.append({
                            "scenario_id": f"TS-{sn:03d}",
                            "workflow_step_number": sn,
                            "workflow_step_name": w.get("name", f"Step {sn}"),
                            "description": f"Test coverage for {w.get('name', f'Step {sn}')}",
                            "test_cases": [{
                                "case_id": f"TC-{sn:03d}-01",
                                "title": f"Verify {w.get('name', f'Step {sn}')} happy path",
                                "category": "happy_path",
                                "priority": "P2_medium",
                                "preconditions": preconditions,
                                "steps": _synthesize_test_steps(w),
                                "expected_result": f"{w.get('name', 'Step')} completes successfully",
                                "tags": ["regression"],
                                "evidence_trace": ev_trace,
                            }],
                        })

            # Build structured scenarios
            test_scenarios = []
            tc_counter = 0
            # Build workflow lookup for step-quality fallback
            _wf_by_step = {}
            for w in workflows:
                if isinstance(w, dict):
                    _wf_by_step[w.get("step_number", 0)] = w
                    _wf_by_step[w.get("name", "").lower().strip()] = w

            _BAD_ACTION_MARKERS = (
                "execute", "workflow action", "as per standard",
                "standard operating procedure", "perform action",
            )

            def _steps_are_generic(steps: list) -> bool:
                """Return True if LLM-generated steps are too generic."""
                for s in steps:
                    act = (s.get("action", "") if isinstance(s, dict) else "").lower()
                    inp = (s.get("input_data", "") if isinstance(s, dict) else "").lower()
                    if any(m in act for m in _BAD_ACTION_MARKERS):
                        return True
                    if any(m in inp for m in _BAD_ACTION_MARKERS):
                        return True
                return False

            for raw_sc in raw_scenarios:
                if not isinstance(raw_sc, dict):
                    continue
                # Skip garbage scenarios with no test_cases (from LLM dict output)
                if not raw_sc.get("test_cases"):
                    continue
                sc_cases = []
                for raw_tc in raw_sc.get("test_cases", []):
                    if not isinstance(raw_tc, dict):
                        continue
                    tc_counter += 1
                    case_id = raw_tc.get("case_id", f"TC-{tc_counter:03d}")
                    category = raw_tc.get("category", "happy_path")
                    if category not in ("happy_path", "negative", "boundary", "edge_case", "security", "performance", "e2e"):
                        category = "happy_path"
                    priority = raw_tc.get("priority", "P2_medium")
                    if priority not in ("P0_critical", "P1_high", "P2_medium", "P3_low"):
                        priority = "P2_medium"

                    # Step quality guard: replace generic LLM steps with synthesized ones
                    raw_steps = raw_tc.get("steps", [])
                    if not raw_steps or _steps_are_generic(raw_steps):
                        wsn = raw_sc.get("workflow_step_number")
                        wsname = (raw_sc.get("workflow_step_name", "") or "").lower().strip()
                        wf = _wf_by_step.get(wsn) or _wf_by_step.get(wsname)
                        if wf:
                            raw_steps = _synthesize_test_steps(wf)

                    tc_steps = []
                    for raw_step in raw_steps:
                        if isinstance(raw_step, dict):
                            tc_steps.append(TestStep(
                                step_number=raw_step.get("step_number", len(tc_steps) + 1),
                                action=raw_step.get("action", ""),
                                input_data=raw_step.get("input_data", ""),
                                expected_behavior=raw_step.get("expected_behavior", ""),
                            ))

                    ev_traces = []
                    for raw_ev in raw_tc.get("evidence_trace", []):
                        if isinstance(raw_ev, dict):
                            ev_traces.append(EvidenceCitation(
                                text=raw_ev.get("text", ""),
                                source_modality=raw_ev.get("source_modality", "transcript"),
                                timestamp_range=raw_ev.get("timestamp_range"),
                                confidence=min(max(float(raw_ev.get("confidence", 0.7)), 0.0), 1.0),
                            ))

                    sc_cases.append(TestCase(
                        case_id=case_id,
                        title=raw_tc.get("title", f"Test Case {tc_counter}"),
                        category=category,
                        priority=priority,
                        preconditions=raw_tc.get("preconditions", []),
                        steps=tc_steps,
                        expected_result=raw_tc.get("expected_result", ""),
                        test_data=raw_tc.get("test_data", {}),
                        tags=raw_tc.get("tags", []),
                        evidence_trace=ev_traces,
                    ))

                # Only add scenarios that have test cases (skip LLM garbage)
                if sc_cases:
                    test_scenarios.append(TestScenario(
                        scenario_id=raw_sc.get("scenario_id", f"TS-{len(test_scenarios)+1:03d}"),
                        workflow_step_number=raw_sc.get("workflow_step_number", len(test_scenarios) + 1),
                        workflow_step_name=raw_sc.get("workflow_step_name", ""),
                        description=raw_sc.get("description", ""),
                        test_cases=sc_cases,
                    ))

            # Sort by workflow step number
            test_scenarios.sort(key=lambda s: s.workflow_step_number)

            # ── Synthesize E2E scenario ───────────────────────
            # Chain happy-path steps from all workflow steps into one E2E test case
            if len(test_scenarios) >= 2:
                e2e_steps = []
                e2e_evidence = []
                step_counter = 0
                for sc in test_scenarios:
                    # Take happy-path case from each scenario (first case)
                    hp_case = None
                    for tc in sc.test_cases:
                        if tc.category == "happy_path":
                            hp_case = tc
                            break
                    if not hp_case and sc.test_cases:
                        hp_case = sc.test_cases[0]
                    if hp_case:
                        for s in hp_case.steps:
                            step_counter += 1
                            e2e_steps.append(TestStep(
                                step_number=step_counter,
                                action=s.action,
                                input_data=s.input_data,
                                expected_behavior=s.expected_behavior,
                            ))
                        e2e_evidence.extend(hp_case.evidence_trace[:2])  # limit evidence per step
                if e2e_steps:
                    e2e_case = TestCase(
                        case_id="TC-E2E-001",
                        title="End-to-End: Full Workflow Happy Path",
                        category="e2e",
                        priority="P0_critical",
                        preconditions=["All systems accessible", "Test environment ready", "Valid user credentials available"],
                        steps=e2e_steps,
                        expected_result="Complete workflow executes successfully from first step to last step without errors",
                        test_data={},
                        tags=["e2e", "regression", "smoke"],
                        evidence_trace=e2e_evidence[:8],  # cap total evidence
                    )
                    e2e_scenario = TestScenario(
                        scenario_id="TS-E2E",
                        workflow_step_number=9999,
                        workflow_step_name="End-to-End Flow",
                        description=f"Chains all {len(test_scenarios)} workflow steps into a single end-to-end test case",
                        test_cases=[e2e_case],
                    )
                    test_scenarios.append(e2e_scenario)

            # ── Build coverage breakdown ──────────────────────
            total_cases = sum(len(s.test_cases) for s in test_scenarios)
            by_cat: dict[str, int] = {}
            by_pri: dict[str, int] = {}
            for sc in test_scenarios:
                for tc in sc.test_cases:
                    by_cat[tc.category] = by_cat.get(tc.category, 0) + 1
                    by_pri[tc.priority] = by_pri.get(tc.priority, 0) + 1

            covered_step_nums = {s.workflow_step_number for s in test_scenarios if s.workflow_step_number != 9999}
            total_steps = len(workflows)
            gap_areas = []
            for w in workflows:
                if isinstance(w, dict) and w.get("step_number") not in covered_step_nums:
                    gap_areas.append(w.get("name", f"Step {w.get('step_number', '?')}"))

            coverage_pct = (len(covered_step_nums) / total_steps * 100) if total_steps > 0 else 0.0

            coverage = CoverageBreakdown(
                total_scenarios=len(test_scenarios),
                total_cases=total_cases,
                by_category=by_cat,
                by_priority=by_pri,
                coverage_percentage=round(coverage_pct, 1),
                gap_areas=gap_areas,
            )

            # ── Build traceability matrix ─────────────────────
            traceability = []
            for w in workflows:
                if not isinstance(w, dict):
                    continue
                sn = w.get("step_number", 0)
                name = w.get("name", f"Step {sn}")
                matching_ids = []
                ev_count = 0
                for sc in test_scenarios:
                    if sc.workflow_step_number == sn:
                        for tc in sc.test_cases:
                            matching_ids.append(tc.case_id)
                            ev_count += len(tc.evidence_trace)
                status = "covered" if matching_ids else "gap"
                if matching_ids and len(matching_ids) < 2:
                    status = "partial"
                traceability.append(TraceabilityEntry(
                    requirement=name,
                    workflow_step_number=sn,
                    test_case_ids=matching_ids,
                    coverage_status=status,
                    evidence_count=ev_count,
                ))

            # ── Build test plan summary ────────────────────────
            plan_name = f"{req.persona_name} — Test Strategy" if req.persona_name else "Test Strategy"
            test_plan = TestPlanSummary(
                name=raw_plan.get("name", plan_name),
                objective=raw_plan.get("objective", f"Validate all {total_steps} workflow steps from KT recording"),
                scope=raw_plan.get("scope", f"Covers {total_steps} steps, {len(risks)} risks, {total_cases} test cases"),
                approach=raw_plan.get("approach", "risk-based"),
                source_persona=req.persona_name,
                source_artifact_id=req.artifact_id,
            )

            prov = TestStrategyProvenance(
                artifact_id=req.artifact_id,
                session_id=req.session_id or "",
                persona_name=req.persona_name,
                generated_at=datetime.now(timezone.utc).isoformat(),
                model_used=self.llm._model,
                model_backend=self.llm._backend,
                generation_time_ms=round(llm_elapsed_ms, 2),
                workflow_steps_analysed=total_steps,
                risks_considered=len(risks),
                source_persona_generated_at=req.source_persona_generated_at,
                source_persona_quality=req.source_persona_quality,
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            return GenerateTestStrategyResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                test_plan=test_plan,
                test_scenarios=test_scenarios,
                coverage=coverage,
                traceability=traceability,
                provenance=prov,
            )

        # ── E2E Test Architect ─────────────────────────────────

        @app.post("/api/v1/brain/generate-e2e-architect", response_model=GenerateE2EArchitectResponse)
        async def generate_e2e_architect(
            req: GenerateE2EArchitectRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Generate critical E2E test scenarios from multimodal evidence.

            Two-pass LLM strategy:
              Pass 1 — Variable extraction: Identify testable variables and
                       decision points from visual + transcript evidence.
              Pass 2 — Scenario generation: Produce critical E2E scenarios
                       that cover high-risk variable combinations.

            This endpoint is independent from generate-test-strategy.
            It consumes the multimodal utility layer output and generates
            a separate E2E Architect analysis.
            """
            start = time.monotonic()

            model_name = (self.llm._model or "").lower()
            is_tiny_model = "1b" in model_name
            is_small_model = is_tiny_model or "3b" in model_name

            # ── Budget limits based on model size ──────────────
            scene_limit = 4 if is_tiny_model else 5 if is_small_model else 10
            segment_limit = 4 if is_tiny_model else 6 if is_small_model else 12
            max_gen_tokens_p1 = 640 if is_tiny_model else 1024 if is_small_model else 1536
            max_gen_tokens_p2 = 768 if is_tiny_model else 1280 if is_small_model else 2048

            # ── Build evidence context strings ─────────────────
            visual_summary = (req.visual_summary or "")[:800 if is_tiny_model else 1200 if is_small_model else 2000]

            scene_text = ""
            for i, desc in enumerate((req.scene_descriptions or [])[:scene_limit]):
                scene_text += f"  Scene {i+1}: {str(desc)[:200]}\n"

            ui_inv = req.ui_element_inventory or {}
            ui_text = ""
            for elem_type in ("buttons", "dropdowns", "text_fields", "checkboxes", "radios", "links", "tabs"):
                items = ui_inv.get(elem_type, [])
                if items:
                    labels = [str(it.get("label", it) if isinstance(it, dict) else it)[:60] for it in items[:15]]
                    ui_text += f"  {elem_type}: {', '.join(labels)}\n"
            total_ui = ui_inv.get("total_elements", 0)
            if total_ui:
                ui_text += f"  Total UI elements: {total_ui}\n"

            multimodal_text = ""
            for ms in (req.multimodal_scenes or [])[:scene_limit]:
                if isinstance(ms, dict):
                    ts_range = ms.get("timestamp_range", "")
                    vis_desc = str(ms.get("visual_description", ""))[:100 if is_tiny_model else 150]
                    transcript_excerpt = str(ms.get("transcript_excerpt", ""))[:120 if is_tiny_model else 200]
                    speaker = ms.get("speaker", "")
                    ui_els = ms.get("ui_elements", [])
                    el_limit = 5 if is_tiny_model else 8
                    el_summary = ", ".join(str(e.get("label", e) if isinstance(e, dict) else e)[:40] for e in ui_els[:el_limit])
                    multimodal_text += f"  [{ts_range}] {vis_desc}"
                    if el_summary:
                        multimodal_text += f" | UI: {el_summary}"
                    if transcript_excerpt:
                        multimodal_text += f"\n    Said{(' (' + speaker + ')') if speaker else ''}: \"{transcript_excerpt}\""
                    multimodal_text += "\n"

            transcript_text = ""
            for seg in (req.transcript_segments or [])[:segment_limit]:
                if isinstance(seg, dict):
                    t = seg.get("timestamp", "")
                    spk = seg.get("speaker", "")
                    txt = str(seg.get("text", ""))[:120 if is_tiny_model else 200]
                    transcript_text += f"  [{t}]{(' ' + spk + ':') if spk else ''} {txt}\n"

            existing_scenarios_text = ""
            for es in (req.existing_test_scenarios or [])[:10 if is_tiny_model else 20]:
                if isinstance(es, dict):
                    existing_scenarios_text += f"  - {es.get('scenario_id', '?')}: {es.get('title', es.get('description', ''))[:100]}\n"

            domain_map_text = ""
            dm = req.domain_map or {}
            for w in (dm.get("workflows", []) or [])[:10 if is_tiny_model else 15]:
                if isinstance(w, dict):
                    sn = w.get("step_number", "?")
                    nm = w.get("name", "?")
                    decs = ", ".join(w.get("decisions", []))
                    domain_map_text += f"  Step {sn}: {nm}"
                    if decs:
                        domain_map_text += f" [decisions: {decs}]"
                    domain_map_text += "\n"

            apps_text = ", ".join(req.application_types_seen) if req.application_types_seen else "unknown"

            # ── Visual graph context ───────────────────────────
            visual_graph_text = ""
            if req.visual_graph_nodes:
                node_types: dict[str, int] = {}
                node_labels: list[str] = []
                graph_node_limit = 20 if is_tiny_model else 50
                graph_label_limit = 10 if is_tiny_model else 15
                for n in req.visual_graph_nodes[:graph_node_limit]:
                    if isinstance(n, dict):
                        t = n.get("type", "unknown")
                        node_types[t] = node_types.get(t, 0) + 1
                        label = n.get("label", "")
                        if label and len(node_labels) < graph_label_limit:
                            node_labels.append(f"{label} [{t}]")
                type_summary = ", ".join(f"{k}({v})" for k, v in node_types.items())
                visual_graph_text = f"  Knowledge graph: {type_summary}\n"
                if node_labels:
                    visual_graph_text += f"  Key nodes: {', '.join(node_labels[:graph_label_limit])}\n"

            # ── Raw OCR evidence (form labels, field text) ─────
            ocr_limit = 3 if is_tiny_model else 5 if is_small_model else 8
            raw_ocr_text = ""
            for ocr_entry in (req.raw_ocr_evidence or [])[:ocr_limit]:
                if isinstance(ocr_entry, dict):
                    scene_idx = ocr_entry.get("scene_idx", "?")
                    ts = ocr_entry.get("timestamp", 0)
                    text = str(ocr_entry.get("ocr_text", ""))[:300 if is_tiny_model else 500]
                    if text.strip():
                        raw_ocr_text += f"  Scene {scene_idx} ({ts:.0f}s): {text}\n"

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # PASS 1 — Variable & Decision Point Extraction
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            p1_system = (
                "You are the E2E Test Architect. Analyse the multimodal evidence below and extract:\n"
                "1. TESTABLE VARIABLES — data fields, dropdowns, toggles, user choices seen or implied.\n"
                "2. DECISION POINTS — branching conditions where different values lead to different outcomes.\n\n"
                "Return ONLY valid JSON:\n"
                '{"variables":[\n'
                '  {"name":"Gender","type":"categorical","observed_values":["Male"],'
                '   "inferred_values":["Female","Non-binary"],'
                '   "source":"Dropdown visible at 0:45 showing Male selected",'
                '   "impacts":["Premium calculation","Eligibility rules"]}\n'
                '],\n'
                '"decision_points":[\n'
                '  {"step_number":3,"condition":"State selection determines tax rules",'
                '   "observed_path":"Texas selected — standard rate",'
                '   "alternative_path":"New York — higher rate expected",'
                '   "source":"State dropdown at step 3"}\n'
                ']}\n\n'
                "RULES:\n"
                "1. observed_values: Only values you can SEE in the evidence.\n"
                "2. inferred_values: Reasonable values NOT shown but expected (e.g., if 'Male' seen, infer 'Female').\n"
                "3. Every variable must have a source citing visual or transcript evidence.\n"
                "4. Decision points must reference specific workflow steps.\n"
                "5. Focus on variables that AFFECT test outcomes — skip cosmetic/display-only fields.\n"
            )

            p1_user = "Extract testable variables and decision points from this multimodal evidence:\n\n"
            p1_user += f"## APPLICATION: {apps_text}\n"
            p1_user += f"## PERSONA: {req.persona_name}\n{req.persona_description[:300] if req.persona_description else ''}\n\n"
            if domain_map_text:
                p1_user += f"## WORKFLOW STEPS\n{domain_map_text}\n"
            if visual_summary:
                p1_user += f"## VISUAL SUMMARY\n{visual_summary}\n\n"
            if scene_text:
                p1_user += f"## SCENES\n{scene_text}\n"
            if ui_text:
                p1_user += f"## UI ELEMENTS\n{ui_text}\n"
            if multimodal_text:
                p1_user += f"## TIME-ALIGNED MULTIMODAL SCENES\n{multimodal_text}\n"
            if visual_graph_text:
                p1_user += f"## VISUAL KNOWLEDGE GRAPH\n{visual_graph_text}\n"
            if raw_ocr_text:
                p1_user += f"## RAW OCR TEXT (form labels, field values from screen)\n{raw_ocr_text}\n"
            if transcript_text:
                p1_user += f"## TRANSCRIPT\n{transcript_text}\n"
            p1_user += f"\nRecording: {req.duration_seconds:.0f}s | Frames: {req.frame_count} | Scenes: {req.scene_count}\n"

            # ── LLM Call: Pass 1 ───────────────────────────────
            llm_start_p1 = time.monotonic()
            try:
                raw_p1 = await self.llm.generate(
                    p1_system,
                    p1_user,
                    temperature=0.1,
                    max_tokens=max_gen_tokens_p1,
                    json_mode=True,
                    allow_stub_fallback=False,
                )
            except (RuntimeError, Exception) as llm_err:
                logger.error(
                    "brain.e2e_architect.pass1_llm_failed: %s", llm_err,
                    extra={"artifact_id": req.artifact_id},
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"LLM generation unavailable (E2E Pass 1): {llm_err}",
                )
            llm_p1_ms = (time.monotonic() - llm_start_p1) * 1000

            # ── Parse Pass 1 ──────────────────────────────────
            cleaned_p1 = raw_p1.strip()
            if cleaned_p1.startswith("```"):
                cleaned_p1 = cleaned_p1.split("\n", 1)[-1] if "\n" in cleaned_p1 else cleaned_p1[3:]
            if cleaned_p1.endswith("```"):
                cleaned_p1 = cleaned_p1[:-3].strip()
            if cleaned_p1.startswith("json"):
                cleaned_p1 = cleaned_p1[4:].strip()

            logger.info(
                "E2E Architect Pass 1 output for artifact=%s [%d chars]: %.500s",
                req.artifact_id, len(raw_p1), raw_p1[:500],
            )

            parsed_p1 = None
            try:
                parsed_p1 = json.loads(cleaned_p1)
            except (json.JSONDecodeError, ValueError):
                json_start = cleaned_p1.find('{')
                json_end = cleaned_p1.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    try:
                        parsed_p1 = json.loads(cleaned_p1[json_start:json_end + 1])
                    except (json.JSONDecodeError, ValueError):
                        pass
                if parsed_p1 is None and json_start >= 0:
                    repaired = _repair_truncated_json(cleaned_p1[json_start:])
                    try:
                        parsed_p1 = json.loads(repaired)
                        logger.info("Recovered E2E Architect Pass 1 JSON via truncation repair")
                    except (json.JSONDecodeError, ValueError):
                        pass
            if parsed_p1 is None:
                logger.warning("E2E Architect Pass 1 unparseable — using empty extraction")
                parsed_p1 = {"variables": [], "decision_points": []}

            # ── Structure Pass 1 results ───────────────────────
            variables = []
            for rv in (parsed_p1.get("variables") or []):
                if isinstance(rv, dict):
                    variables.append(E2EVariable(
                        name=rv.get("name") or "unknown",
                        type=rv.get("type") or "categorical",
                        observed_values=rv.get("observed_values") or [],
                        inferred_values=rv.get("inferred_values") or [],
                        source=rv.get("source") or "",
                        impacts=rv.get("impacts") or [],
                    ))

            decision_points = []
            for rd in (parsed_p1.get("decision_points") or []):
                if isinstance(rd, dict):
                    decision_points.append(DecisionPoint(
                        step_number=int(rd.get("step_number") or 0),
                        condition=rd.get("condition") or "",
                        observed_path=rd.get("observed_path") or "",
                        alternative_path=rd.get("alternative_path") or "",
                        source=rd.get("source") or "",
                    ))

            # ── Pairwise combinations ──────────────────────────
            pairwise_combos = _pairwise_combinations(variables)
            logger.info(
                "E2E Architect pairwise: %d variables → %d combinations",
                len(variables), len(pairwise_combos),
            )

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # PASS 2 — Critical E2E Scenario Generation
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            var_summary = ""
            for v in variables:
                all_vals = v.observed_values + v.inferred_values
                var_summary += f"  - {v.name} ({v.type}): {', '.join(all_vals[:8])} | impacts: {', '.join(v.impacts[:3])}\n"

            dp_summary = ""
            for dp in decision_points:
                dp_summary += f"  - Step {dp.step_number}: {dp.condition} (seen: {dp.observed_path[:80]})\n"

            pairwise_text = ""
            pairwise_limit = 8 if is_tiny_model else 15
            for idx, combo in enumerate(pairwise_combos[:pairwise_limit]):
                entries = ", ".join(f"{k}={v}" for k, v in combo.items())
                pairwise_text += f"  Combo {idx+1}: {entries}\n"

            p2_system = (
                "You are the E2E Test Architect. Using the extracted variables and decision points below,\n"
                "generate CRITICAL end-to-end test scenarios that cover high-risk combinations.\n\n"
                "Return ONLY valid JSON:\n"
                '{"scenarios":[\n'
                '  {"scenario_id":"E2E-001","title":"Happy path with observed data",'
                '   "category":"observed","priority":"P0_critical",'
                '   "rationale":"Covers the exact path demonstrated in the recording",'
                '   "evidence_sources":[{"text":"quote from evidence","source_modality":"visual","confidence":0.9}],'
                '   "preconditions":["User logged in","System accessible"],'
                '   "steps":[{"step_number":1,"action":"Select Male from Gender dropdown","input_data":"Male","expected_behavior":"Gender set to Male"}],'
                '   "expected_outcome":"Policy created successfully with standard rate",'
                '   "data_matrix":[{"Gender":"Male","State":"Texas","Age":"30"}],'
                '   "workflow_steps_covered":[1,2,3],'
                '   "risk_areas_addressed":["Premium calculation","State tax rules"]}\n'
                ']}\n\n'
                "CATEGORIES:\n"
                "  observed — Exact path demonstrated in the recording (P0)\n"
                "  inferred_high_risk — Unseen combination with high business risk (P0/P1)\n"
                "  boundary_assumption — Edge values / limits not tested (P1/P2)\n\n"
                "RULES:\n"
                "1. First scenario MUST be 'observed' — the exact path from the demo.\n"
                "2. Generate inferred_high_risk scenarios for variable combinations NOT shown in demo.\n"
                "3. data_matrix: Each entry is a set of variable values for parameterised execution.\n"
                "4. steps.action MUST be concrete UI actions ('Click Submit', 'Select Texas from State dropdown').\n"
                "5. evidence_sources: cite visual or transcript evidence for each scenario.\n"
                "6. workflow_steps_covered: list step numbers this scenario exercises.\n"
            )

            if existing_scenarios_text:
                p2_system += (
                    "7. DEDUPLICATION: The following test scenarios ALREADY EXIST. "
                    "Do NOT regenerate them. Focus on scenarios that cover variable COMBINATIONS "
                    "and cross-step flows NOT covered by existing tests.\n"
                )

            p2_user = "Generate critical E2E scenarios from these extracted variables:\n\n"
            p2_user += f"## VARIABLES ({len(variables)} extracted)\n{var_summary}\n"
            p2_user += f"## DECISION POINTS ({len(decision_points)} found)\n{dp_summary}\n"
            if pairwise_text:
                p2_user += f"## PAIRWISE COMBINATIONS TO COVER ({len(pairwise_combos)} combos)\n"
                p2_user += "Each combo below is a set of variable values that must be tested together.\n"
                p2_user += "Use these as the data_matrix entries for your scenarios.\n"
                p2_user += f"{pairwise_text}\n"
            if domain_map_text:
                p2_user += f"## WORKFLOW STEPS\n{domain_map_text}\n"
            if existing_scenarios_text:
                p2_user += f"## EXISTING TEST SCENARIOS (do NOT duplicate)\n{existing_scenarios_text}\n"
            if multimodal_text:
                p2_user += f"## MULTIMODAL EVIDENCE\n{multimodal_text}\n"
            if visual_graph_text:
                p2_user += f"## VISUAL KNOWLEDGE GRAPH\n{visual_graph_text}\n"
            if raw_ocr_text:
                p2_user += f"## RAW OCR TEXT (form labels, field values from screen)\n{raw_ocr_text}\n"
            p2_user += f"\nApplication: {apps_text} | Duration: {req.duration_seconds:.0f}s\n"
            p2_user += (
                "Generate 3-5 critical E2E scenarios covering observed + inferred high-risk combinations.\n"
                if is_tiny_model else
                "Generate 3-8 critical E2E scenarios covering observed + inferred high-risk combinations.\n"
            )
            if pairwise_combos:
                p2_user += "Distribute the pairwise combinations across your scenarios' data_matrix fields.\n"

            # ── LLM Call: Pass 2 ───────────────────────────────
            llm_start_p2 = time.monotonic()
            try:
                raw_p2 = await self.llm.generate(
                    p2_system,
                    p2_user,
                    temperature=0.15,
                    max_tokens=max_gen_tokens_p2,
                    json_mode=True,
                    allow_stub_fallback=False,
                )
            except (RuntimeError, Exception) as llm_err:
                logger.error(
                    "brain.e2e_architect.pass2_llm_failed: %s", llm_err,
                    extra={"artifact_id": req.artifact_id},
                )
                raise HTTPException(
                    status_code=503,
                    detail=f"LLM generation unavailable (E2E Pass 2): {llm_err}",
                )
            llm_p2_ms = (time.monotonic() - llm_start_p2) * 1000

            # ── Parse Pass 2 ──────────────────────────────────
            cleaned_p2 = raw_p2.strip()
            if cleaned_p2.startswith("```"):
                cleaned_p2 = cleaned_p2.split("\n", 1)[-1] if "\n" in cleaned_p2 else cleaned_p2[3:]
            if cleaned_p2.endswith("```"):
                cleaned_p2 = cleaned_p2[:-3].strip()
            if cleaned_p2.startswith("json"):
                cleaned_p2 = cleaned_p2[4:].strip()

            logger.info(
                "E2E Architect Pass 2 output for artifact=%s [%d chars]: %.500s",
                req.artifact_id, len(raw_p2), raw_p2[:500],
            )

            parsed_p2 = None
            try:
                parsed_p2 = json.loads(cleaned_p2)
            except (json.JSONDecodeError, ValueError):
                json_start = cleaned_p2.find('{')
                json_end = cleaned_p2.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    try:
                        parsed_p2 = json.loads(cleaned_p2[json_start:json_end + 1])
                    except (json.JSONDecodeError, ValueError):
                        pass
                if parsed_p2 is None and json_start >= 0:
                    repaired = _repair_truncated_json(cleaned_p2[json_start:])
                    try:
                        parsed_p2 = json.loads(repaired)
                        logger.info("Recovered E2E Architect Pass 2 JSON via truncation repair")
                    except (json.JSONDecodeError, ValueError):
                        pass
            if parsed_p2 is None:
                logger.warning("E2E Architect Pass 2 unparseable — using empty scenarios")
                parsed_p2 = {"scenarios": []}

            # ── Structure Pass 2 results ───────────────────────
            raw_scenarios_e2e = parsed_p2.get("scenarios", [])
            if isinstance(raw_scenarios_e2e, dict):
                raw_scenarios_e2e = [raw_scenarios_e2e]
            elif not isinstance(raw_scenarios_e2e, list):
                raw_scenarios_e2e = []

            scenarios = []
            for rs in raw_scenarios_e2e:
                if not isinstance(rs, dict):
                    continue

                # Parse evidence citations
                ev_sources = []
                for rev in (rs.get("evidence_sources") or []):
                    if isinstance(rev, dict):
                        ev_sources.append(EvidenceCitation(
                            text=rev.get("text") or "",
                            source_modality=rev.get("source_modality") or "visual",
                            timestamp_range=rev.get("timestamp_range"),
                            confidence=min(max(float(rev.get("confidence") or 0.7), 0.0), 1.0),
                        ))

                # Parse test steps
                sc_steps = []
                for raw_step in (rs.get("steps") or []):
                    if isinstance(raw_step, dict):
                        sc_steps.append(TestStep(
                            step_number=raw_step.get("step_number") or (len(sc_steps) + 1),
                            action=raw_step.get("action") or "",
                            input_data=raw_step.get("input_data") or "",
                            expected_behavior=raw_step.get("expected_behavior") or "",
                        ))

                category = rs.get("category") or "observed"
                if category not in ("observed", "inferred_high_risk", "boundary_assumption"):
                    category = "observed"
                priority = rs.get("priority", "P1_high")
                if priority not in ("P0_critical", "P1_high", "P2_medium", "P3_low"):
                    priority = "P1_high"

                scenarios.append(E2EScenario(
                    scenario_id=rs.get("scenario_id") or f"E2E-{len(scenarios)+1:03d}",
                    title=rs.get("title") or f"E2E Scenario {len(scenarios)+1}",
                    category=category,
                    priority=priority,
                    rationale=rs.get("rationale") or "",
                    evidence_sources=ev_sources,
                    preconditions=rs.get("preconditions") or [],
                    steps=sc_steps,
                    expected_outcome=rs.get("expected_outcome") or "",
                    data_matrix=rs.get("data_matrix") or [],
                    workflow_steps_covered=rs.get("workflow_steps_covered") or [],
                    risk_areas_addressed=rs.get("risk_areas_addressed") or [],
                ))

            if not scenarios:
                logger.warning(
                    "E2E Architect Pass 2 returned no usable scenarios — synthesizing grounded fallback scenarios"
                )
                scenarios = _synthesize_e2e_scenarios(req, variables, decision_points, pairwise_combos)

            # ── Deduplication ──────────────────────────────────
            pre_dedup_count = len(scenarios)
            scenarios = _deduplicate_e2e_scenarios(scenarios, req.existing_test_scenarios or [])

            # ── Backfill pairwise combos into data_matrix ──────
            # If LLM didn't populate data_matrix, assign pairwise combos round-robin
            unfilled = [sc for sc in scenarios if not sc.data_matrix and pairwise_combos]
            if unfilled and pairwise_combos:
                combo_idx = 0
                for sc in unfilled:
                    # Assign 1-3 combos per scenario
                    n_assign = max(1, len(pairwise_combos) // max(len(unfilled), 1))
                    assigned = []
                    for _ in range(min(n_assign, 3)):
                        if combo_idx < len(pairwise_combos):
                            assigned.append(pairwise_combos[combo_idx])
                            combo_idx += 1
                    sc.data_matrix = assigned

            # ── Coverage analysis ──────────────────────────────
            all_steps_covered = set()
            all_risks_addressed = set()
            for sc in scenarios:
                all_steps_covered.update(sc.workflow_steps_covered)
                all_risks_addressed.update(sc.risk_areas_addressed)

            total_workflow_steps = len((req.domain_map or {}).get("workflows", []))
            coverage_analysis = {
                "total_scenarios": len(scenarios),
                "scenarios_before_dedup": pre_dedup_count,
                "duplicates_removed": pre_dedup_count - len(scenarios),
                "by_category": {},
                "by_priority": {},
                "workflow_steps_covered": sorted(all_steps_covered),
                "workflow_coverage_pct": round(
                    len(all_steps_covered) / total_workflow_steps * 100, 1
                ) if total_workflow_steps > 0 else 0.0,
                "variables_tested": len(variables),
                "decision_points_found": len(decision_points),
                "pairwise_combinations_generated": len(pairwise_combos),
                "pairwise_coverage_pct": round(
                    sum(1 for sc in scenarios if sc.data_matrix) / max(len(scenarios), 1) * 100, 1
                ),
                "risk_areas_addressed": sorted(all_risks_addressed),
            }
            for sc in scenarios:
                coverage_analysis["by_category"][sc.category] = coverage_analysis["by_category"].get(sc.category, 0) + 1
                coverage_analysis["by_priority"][sc.priority] = coverage_analysis["by_priority"].get(sc.priority, 0) + 1

            # ── Assemble output ─────────────────────────────────
            e2e_output = E2EArchitectOutput(
                variables=variables,
                decision_points=decision_points,
                critical_combinations=scenarios,
                coverage_analysis=coverage_analysis,
            )

            total_llm_ms = llm_p1_ms + llm_p2_ms
            prov = TestStrategyProvenance(
                artifact_id=req.artifact_id,
                session_id=req.session_id or "",
                persona_name=req.persona_name,
                generated_at=datetime.now(timezone.utc).isoformat(),
                model_used=self.llm._model,
                model_backend=self.llm._backend,
                generation_time_ms=round(total_llm_ms, 2),
                workflow_steps_analysed=total_workflow_steps,
                risks_considered=len((req.domain_map or {}).get("risks", [])),
            )

            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "E2E Architect complete for artifact=%s: %d variables, %d decision_points, "
                "%d scenarios (%d pre-dedup), %d pairwise combos [%.0fms, LLM %.0fms]",
                req.artifact_id, len(variables), len(decision_points), len(scenarios),
                pre_dedup_count, len(pairwise_combos), elapsed_ms, total_llm_ms,
            )

            return GenerateE2EArchitectResponse(
                success=True,
                trace_id=req.trace_id,
                engine="brain",
                engine_version="1.0.0",
                processing_time_ms=round(elapsed_ms, 2),
                e2e_architect=e2e_output,
                provenance=prov,
            )

        # ── LLM Health ─────────────────────────────────────────

        @app.get("/api/v1/brain/llm-health")
        async def llm_health(
            user: NexusUser = Depends(get_current_user),
        ):
            """Get the Brain's LLM provider health status."""
            return {
                "success": True,
                "engine": "brain",
                "llm": self.llm.get_health(),
            }

        # ── P1: Tier Health Probing ────────────────────────────

        @app.get("/api/v1/brain/tiers/health")
        async def probe_tier_health(
            user: NexusUser = Depends(get_current_user),
        ):
            """
            Probe live health for all LLM engine tiers.

            Performs real connectivity checks against configured providers
            (HTTP health for local, API key check for cloud).
            """
            start = time.monotonic()
            results = await self.tier_manager.probe_all_health()
            elapsed_ms = (time.monotonic() - start) * 1000
            return {
                "success": True,
                "engine": "brain",
                "processing_time_ms": round(elapsed_ms, 2),
                "health": results,
            }

        @app.get("/api/v1/brain/tiers/{engine_name}/health")
        async def probe_engine_tier_health(
            engine_name: str,
            user: NexusUser = Depends(get_current_user),
        ):
            """Probe live health for a specific engine's tier providers."""
            result = await self.tier_manager.probe_tier_health(engine_name)
            return {"success": True, **result}


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = BrainEngine()
    engine.run()


if __name__ == "__main__":
    main()
