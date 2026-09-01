"""
Nexus Plugin Extensions — Type-safe interfaces for domain customization.

Each extension defines what a domain plugin can contribute to a specific engine.
Extensions are Pydantic models: validated, serializable, documented.

Architecture:
    Engine (generic) loads Extension (domain-specific) at startup.
    Multiple plugins can contribute extensions — they get merged.

Extension Types:
    VocabularyExtension     → Ears Engine   (speech recognition boost words)
    PIIExtension            → Shield Engine (domain PII patterns)
    GraphSchemaExtension    → Backbone Engine (knowledge graph node/edge types)
    ReasoningExtension      → Heart Engine  (LLM prompt templates)
    DocumentTypeExtension   → Spine Engine  (document classification rules)
    DataGeneratorExtension  → Hands Engine  (synthetic data profiles)
    ReportExtension         → Mouth Engine  (report type definitions)
    ExecutionExtension      → Legs Engine   (execution target types)
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── Vocabulary Extension (Ears Engine) ──────────────────────────


class VocabularyTerm(BaseModel):
    """A domain-specific term for speech recognition accuracy."""

    term: str = Field(..., description="The domain term (e.g., 'nonforfeiture')")
    boost_weight: float = Field(
        default=1.5,
        ge=0.5,
        le=3.0,
        description="Whisper transcription boost weight (1.0=normal, 2.0=strong)",
    )
    pronunciation: Optional[str] = Field(
        default=None, description="IPA or phonetic pronunciation hint"
    )
    definition: Optional[str] = Field(
        default=None, description="Brief definition for context"
    )
    aliases: list[str] = Field(
        default_factory=list, description="Alternative spellings or abbreviations"
    )


class VocabularyExtension(BaseModel):
    """Extends Ears engine with domain-specific vocabulary."""

    domain: str = Field(..., description="Domain identifier")
    terms: list[VocabularyTerm] = Field(default_factory=list)
    boost_phrases: list[str] = Field(
        default_factory=list,
        description="Multi-word phrases to boost in transcription",
    )
    suppressed_phrases: list[str] = Field(
        default_factory=list,
        description="Common misrecognitions to suppress",
    )

    def get_boost_words(self) -> list[str]:
        """Return flat list of all terms + aliases + phrases for Whisper."""
        words: set[str] = set()
        for t in self.terms:
            words.add(t.term)
            words.update(t.aliases)
        words.update(self.boost_phrases)
        return sorted(words)

    def get_high_priority_terms(self, min_weight: float = 2.0) -> list[str]:
        """Return terms with boost weight at or above threshold."""
        return [t.term for t in self.terms if t.boost_weight >= min_weight]


# ─── PII Extension (Shield Engine) ──────────────────────────────


class PIIEntityDefinition(BaseModel):
    """A domain-specific PII entity type with detection pattern."""

    name: str = Field(..., description="Entity type name (e.g., POLICY_NUMBER)")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(default="")
    pattern: Optional[str] = Field(
        default=None, description="Regex pattern for detection"
    )
    risk_level: str = Field(
        default="medium", description="low, medium, high, critical"
    )
    redaction_format: str = Field(
        default="[{name}_{index}]",
        description="Format string for redacted placeholder",
    )
    examples: list[str] = Field(
        default_factory=list,
        description="Synthetic example values (never real PII)",
    )

    def compile_pattern(self) -> Optional[re.Pattern]:
        """Compile the regex pattern. Returns None if no pattern defined."""
        if self.pattern:
            return re.compile(self.pattern, re.IGNORECASE)
        return None


class PIIExtension(BaseModel):
    """Extends Shield engine with domain-specific PII detection."""

    domain: str
    entity_types: list[PIIEntityDefinition] = Field(default_factory=list)
    context_rules: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Context-aware detection rules",
    )

    def get_compiled_patterns(self) -> dict[str, re.Pattern]:
        """Return {entity_name: compiled_regex} for all entities with patterns."""
        result: dict[str, re.Pattern] = {}
        for entity in self.entity_types:
            compiled = entity.compile_pattern()
            if compiled is not None:
                result[entity.name] = compiled
        return result


# ─── Graph Schema Extension (Backbone Engine) ───────────────────


class NodeTypeDefinition(BaseModel):
    """A domain-specific node type for the knowledge graph."""

    name: str = Field(..., description="Node type name (PascalCase, e.g., 'RateTable')")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(default="")
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Property name → type (string, int, float, bool, datetime, list)",
    )
    required_properties: list[str] = Field(default_factory=list)
    icon: Optional[str] = Field(default=None, description="UI icon identifier")
    color: Optional[str] = Field(default=None, description="UI color hex code")


class RelationshipTypeDefinition(BaseModel):
    """A domain-specific relationship type for the knowledge graph."""

    name: str = Field(..., description="Relationship type (UPPER_SNAKE_CASE)")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(default="")
    from_node_types: list[str] = Field(
        default_factory=list, description="Allowed source node types"
    )
    to_node_types: list[str] = Field(
        default_factory=list, description="Allowed target node types"
    )
    properties: dict[str, str] = Field(default_factory=dict)


class GraphSchemaExtension(BaseModel):
    """Extends Backbone engine with domain-specific knowledge graph schema."""

    domain: str
    node_types: list[NodeTypeDefinition] = Field(default_factory=list)
    relationship_types: list[RelationshipTypeDefinition] = Field(
        default_factory=list
    )
    constraints: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Neo4j graph constraints and indexes",
    )

    def get_node_type_names(self) -> list[str]:
        """Return flat list of node type names."""
        return [n.name for n in self.node_types]

    def get_relationship_type_names(self) -> list[str]:
        """Return flat list of relationship type names."""
        return [r.name for r in self.relationship_types]

    def get_node_type(self, name: str) -> Optional[NodeTypeDefinition]:
        """Look up a node type definition by name."""
        for n in self.node_types:
            if n.name == name:
                return n
        return None


# ─── Reasoning Extension (Heart Engine) ──────────────────────────


class PromptTemplate(BaseModel):
    """A domain-specific prompt template for LLM reasoning."""

    name: str = Field(..., description="Template identifier (unique within domain)")
    task: str = Field(
        ...,
        description="Task type: extract_rules, generate_tests, detect_contradictions, "
        "explore_flows, analyze_risk, summarize, etc.",
    )
    system_prompt: str = Field(..., description="System prompt text")
    user_prompt_template: str = Field(
        ...,
        description="User prompt with {placeholders} for runtime substitution",
    )
    output_schema: Optional[dict[str, Any]] = Field(
        default=None, description="Expected JSON output schema for validation"
    )
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1)
    examples: list[dict[str, str]] = Field(
        default_factory=list, description="Few-shot examples [{input, output}]"
    )


class GuardrailRule(BaseModel):
    """A domain-specific validation rule for LLM outputs."""

    name: str = Field(..., description="Rule identifier")
    description: str = Field(default="")
    check_type: str = Field(
        ...,
        description="schema_check, graph_check, source_check, confidence_check, custom",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Rule-specific configuration",
    )
    severity: str = Field(
        default="warning", description="warning, error, block"
    )


class ReasoningExtension(BaseModel):
    """Extends Heart engine with domain-specific reasoning capabilities."""

    domain: str
    prompt_templates: list[PromptTemplate] = Field(default_factory=list)
    supported_tasks: list[str] = Field(
        default_factory=list,
        description="Tasks this domain supports (must match PromptTemplate.task values)",
    )
    guardrail_rules: list[GuardrailRule] = Field(default_factory=list)

    def get_template(self, task: str) -> Optional[PromptTemplate]:
        """Get the prompt template for a specific task."""
        for t in self.prompt_templates:
            if t.task == task:
                return t
        return None

    def get_guardrails(self, severity: Optional[str] = None) -> list[GuardrailRule]:
        """Get guardrail rules, optionally filtered by severity."""
        if severity is None:
            return list(self.guardrail_rules)
        return [g for g in self.guardrail_rules if g.severity == severity]


# ─── Document Type Extension (Spine Engine) ──────────────────────


class DocumentTypeDefinition(BaseModel):
    """A domain-specific document classification."""

    name: str = Field(..., description="Document type identifier (snake_case)")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(default="")
    keywords: list[str] = Field(
        default_factory=list,
        description="Content keywords that indicate this document type",
    )
    filename_patterns: list[str] = Field(
        default_factory=list,
        description="Regex patterns to match against filenames",
    )
    expected_formats: list[str] = Field(
        default_factory=list,
        description="Expected file formats (pdf, xlsx, docx, csv, etc.)",
    )
    chunking_strategy: str = Field(
        default="default",
        description="How to chunk this document type (default, table-aware, page-level)",
    )
    priority: int = Field(
        default=0,
        description="Classification priority (higher wins when multiple match)",
    )


class DocumentTypeExtension(BaseModel):
    """Extends Spine engine with domain-specific document classification."""

    domain: str
    document_types: list[DocumentTypeDefinition] = Field(default_factory=list)

    def classify(self, filename: str, content_preview: str) -> Optional[str]:
        """
        Classify a document based on filename and content.

        Returns the document type name with the highest match score,
        or None if no match.
        """
        best_match: Optional[str] = None
        best_score = 0

        fn_lower = filename.lower()
        content_lower = content_preview.lower()

        for dt in self.document_types:
            score = dt.priority

            for pattern in dt.filename_patterns:
                if re.search(pattern, fn_lower):
                    score += 10

            for kw in dt.keywords:
                if kw.lower() in content_lower:
                    score += 1

            if score > best_score:
                best_score = score
                best_match = dt.name

        return best_match if best_score > 0 else None


# ─── Data Generator Extension (Hands Engine) ────────────────────


class FieldDefinition(BaseModel):
    """A domain-specific synthetic data field."""

    name: str = Field(..., description="Field name")
    field_type: str = Field(
        ..., description="string, integer, float, date, enum, pattern, boolean"
    )
    description: str = Field(default="")
    required: bool = Field(default=True)
    enum_values: list[str] = Field(
        default_factory=list, description="Allowed values for enum fields"
    )
    pattern: Optional[str] = Field(
        default=None, description="Regex or format pattern for generation"
    )
    min_value: Optional[float] = Field(default=None)
    max_value: Optional[float] = Field(default=None)
    default_value: Optional[Any] = Field(default=None)
    synthetic_strategy: str = Field(
        default="random",
        description="random, sequential, boundary, realistic, weighted",
    )


class DataProfileDefinition(BaseModel):
    """A domain-specific synthetic data profile template."""

    name: str = Field(..., description="Profile type name (e.g., 'insurance_applicant')")
    display_name: str = Field(...)
    description: str = Field(default="")
    fields: list[FieldDefinition] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Cross-field constraints (e.g., 'if age < 18 then tobacco = never')",
    )
    generation_strategies: list[str] = Field(
        default_factory=lambda: ["random", "boundary", "equivalence"],
        description="Supported generation strategies for this profile",
    )


class IDPatternDefinition(BaseModel):
    """A domain-specific ID/number generation pattern."""

    name: str = Field(..., description="Pattern name (e.g., 'policy_number')")
    display_name: str = Field(...)
    format_template: str = Field(
        ..., description="Format template (e.g., 'PLY-{YYYY}-{SEQ:6}')"
    )
    description: str = Field(default="")
    prefix_options: list[str] = Field(default_factory=list)


class DataGeneratorExtension(BaseModel):
    """Extends Hands engine with domain-specific synthetic data generation."""

    domain: str
    profiles: list[DataProfileDefinition] = Field(default_factory=list)
    id_patterns: list[IDPatternDefinition] = Field(default_factory=list)

    def get_profile(self, name: str) -> Optional[DataProfileDefinition]:
        """Look up a data profile by name."""
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def get_id_pattern(self, name: str) -> Optional[IDPatternDefinition]:
        """Look up an ID pattern by name."""
        for p in self.id_patterns:
            if p.name == name:
                return p
        return None


# ─── Report Extension (Mouth Engine) ────────────────────────────


class ReportTypeDefinition(BaseModel):
    """A domain-specific report type."""

    name: str = Field(..., description="Report type identifier (snake_case)")
    display_name: str = Field(...)
    description: str = Field(default="")
    category: str = Field(
        default="general", description="Report category (compliance, testing, executive, audit)"
    )
    supported_formats: list[str] = Field(
        default_factory=lambda: ["html", "pdf", "json"]
    )
    template_name: Optional[str] = Field(
        default=None, description="Template file name (Jinja2)"
    )
    required_inputs: list[str] = Field(
        default_factory=list,
        description="Required input data keys for this report",
    )
    sections: list[str] = Field(
        default_factory=list,
        description="Report sections in rendering order",
    )


class ReportExtension(BaseModel):
    """Extends Mouth engine with domain-specific report generation."""

    domain: str
    report_types: list[ReportTypeDefinition] = Field(default_factory=list)
    branding: dict[str, str] = Field(
        default_factory=dict,
        description="Branding overrides (company_name, logo_path, accent_color, etc.)",
    )

    def get_report_type(self, name: str) -> Optional[ReportTypeDefinition]:
        """Look up a report type by name."""
        for r in self.report_types:
            if r.name == name:
                return r
        return None


# ─── Execution Extension (Legs Engine) ──────────────────────────


class ExecutionTargetDefinition(BaseModel):
    """A domain-specific execution target type."""

    name: str = Field(..., description="Target type identifier (snake_case)")
    display_name: str = Field(...)
    description: str = Field(default="")
    capabilities: list[str] = Field(
        default_factory=list,
        description="What this target can do (navigate, click, type, assert, etc.)",
    )
    required_config: list[str] = Field(
        default_factory=list,
        description="Required configuration keys (base_url, credentials, etc.)",
    )
    default_timeout_ms: int = Field(default=30000, ge=1000)


class ExecutionExtension(BaseModel):
    """Extends Legs engine with domain-specific execution capabilities."""

    domain: str
    target_types: list[ExecutionTargetDefinition] = Field(default_factory=list)
    self_healing_strategies: list[str] = Field(
        default_factory=list,
        description="Domain-specific self-healing approaches",
    )
    evidence_types: list[str] = Field(
        default_factory=lambda: ["screenshot", "video", "log", "har"],
        description="Types of evidence to capture during execution",
    )

    def get_target(self, name: str) -> Optional[ExecutionTargetDefinition]:
        """Look up a target type by name."""
        for t in self.target_types:
            if t.name == name:
                return t
        return None
