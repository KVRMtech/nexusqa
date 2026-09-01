"""
Nexus Insurance QA Plugin — The first product on the AI Engine Factory.

This module defines the InsuranceQAPlugin class and the create_plugin()
factory function. The plugin aggregates all 8 domain extensions and
chain configurations for enterprise insurance QA testing.

Loading:
    from nexus_sdk.plugins import PluginRegistry
    registry = PluginRegistry.instance()
    registry.load_from_module("products.nexus_qa.plugin")

    # Or manually:
    from products.nexus_qa.plugin import create_plugin
    plugin = create_plugin()
    registry.register(plugin)
"""

from __future__ import annotations

import logging
from typing import Optional

from nexus_sdk.plugins.base import ChainConfig, DomainPlugin, PluginManifest
from nexus_sdk.plugins.extensions import (
    DataGeneratorExtension,
    DocumentTypeExtension,
    ExecutionExtension,
    GraphSchemaExtension,
    PIIExtension,
    ReasoningExtension,
    ReportExtension,
    VocabularyExtension,
)

from .domain import (
    build_data_generator_extension,
    build_document_type_extension,
    build_execution_extension,
    build_graph_schema_extension,
    build_pii_extension,
    build_reasoning_extension,
    build_report_extension,
    build_vocabulary_extension,
)

logger = logging.getLogger(__name__)


# ─── Plugin Manifest ──────────────────────────────────────────

_MANIFEST = PluginManifest(
    plugin_id="nexus.insurance-qa",
    name="Insurance QA",
    domain="insurance",
    version="1.0.0",
    description=(
        "Enterprise insurance QA product. Provides domain vocabulary, "
        "PII patterns, knowledge graph schema, LLM reasoning prompts, "
        "document classifiers, synthetic data generators, report templates, "
        "and execution targets for insurance QA testing."
    ),
    author="Nexus Platform Team",
    min_platform_version="0.1.0",
    tags=["insurance", "qa", "testing", "compliance", "life", "p&c", "annuity"],
    engines_extended=[
        "ears", "shield", "backbone", "heart",
        "spine", "hands", "mouth", "legs",
    ],
)


# ─── Chain Configurations ─────────────────────────────────────

_CHAIN_CONFIGS: list[ChainConfig] = [
    ChainConfig(
        chain_id="qa_testing",
        name="QA Testing Pipeline",
        description=(
            "End-to-end QA pipeline: transcribe → redact → extract rules → "
            "generate tests → produce data → execute tests → generate reports"
        ),
        stages=[
            "ears.transcribe",
            "shield.redact",
            "spine.ingest",
            "heart.extract_rules",
            "heart.generate_tests",
            "hands.generate_data",
            "legs.execute_tests",
            "mouth.generate_report",
        ],
        version="1.0.0",
        tags=["qa", "testing", "end-to-end"],
    ),
    ChainConfig(
        chain_id="knowledge_capture",
        name="Knowledge Capture Pipeline",
        description=(
            "Capture SME knowledge: transcribe → redact → store in graph → "
            "extract visual context → feed to reasoning"
        ),
        stages=[
            "ears.transcribe",
            "eyes.capture_screen",
            "shield.redact",
            "heart.extract_rules",
            "backbone.store_knowledge",
        ],
        version="1.0.0",
        tags=["knowledge", "capture", "kt"],
    ),
    ChainConfig(
        chain_id="compliance_audit",
        name="Compliance Audit Pipeline",
        description=(
            "Regulatory compliance verification: ingest documents → "
            "extract rules → analyze compliance → generate audit report"
        ),
        stages=[
            "spine.ingest",
            "heart.extract_rules",
            "heart.analyze_compliance",
            "mouth.generate_report",
        ],
        version="1.0.0",
        tags=["compliance", "audit", "regulatory"],
    ),
    ChainConfig(
        chain_id="regression_suite",
        name="Regression Test Suite",
        description=(
            "Regression cycle: load existing rules → generate tests → "
            "produce boundary data → execute → compare with baseline"
        ),
        stages=[
            "backbone.query_rules",
            "heart.generate_tests",
            "hands.generate_data",
            "legs.execute_tests",
            "mouth.generate_report",
        ],
        version="1.0.0",
        tags=["regression", "testing", "baseline"],
    ),
]


# ─── Plugin Class ─────────────────────────────────────────────

class InsuranceQAPlugin(DomainPlugin):
    """
    Insurance QA domain plugin.

    Extends all 8 Nexus engines with insurance-specific behavior:
      - Ears:     Insurance vocabulary for accurate transcription
      - Shield:   Insurance PII patterns (policy numbers, NPNs, etc.)
      - Backbone: Insurance knowledge graph schema (products, coverages)
      - Heart:    Insurance reasoning prompts (rule extraction, test gen)
      - Spine:    Insurance document classifiers (rate filings, BRDs)
      - Hands:    Insurance data generators (applicant profiles, riders)
      - Mouth:    QA report types (traceability, compliance, coverage)
      - Legs:     QA execution targets (web UI, API, mainframe, DB)
    """

    def __init__(self) -> None:
        super().__init__(manifest=_MANIFEST)
        self._vocabulary: Optional[VocabularyExtension] = None
        self._pii: Optional[PIIExtension] = None
        self._graph_schema: Optional[GraphSchemaExtension] = None
        self._reasoning: Optional[ReasoningExtension] = None
        self._document_types: Optional[DocumentTypeExtension] = None
        self._data_generators: Optional[DataGeneratorExtension] = None
        self._reports: Optional[ReportExtension] = None
        self._execution: Optional[ExecutionExtension] = None

    # ─── Lifecycle ────────────────────────────────────────────

    def on_load(self) -> None:
        """Pre-build all extensions on plugin load for fast access."""
        logger.info(
            "Loading Insurance QA plugin v%s — extending engines: %s",
            self.manifest.version,
            ", ".join(self.manifest.engines_extended),
        )
        self._vocabulary = build_vocabulary_extension()
        self._pii = build_pii_extension()
        self._graph_schema = build_graph_schema_extension()
        self._reasoning = build_reasoning_extension()
        self._document_types = build_document_type_extension()
        self._data_generators = build_data_generator_extension()
        self._reports = build_report_extension()
        self._execution = build_execution_extension()
        logger.info("Insurance QA plugin loaded: 8 extensions ready")

    def on_unload(self) -> None:
        """Release cached extensions."""
        logger.info("Unloading Insurance QA plugin")
        self._vocabulary = None
        self._pii = None
        self._graph_schema = None
        self._reasoning = None
        self._document_types = None
        self._data_generators = None
        self._reports = None
        self._execution = None

    # ─── Extension Providers ──────────────────────────────────

    def get_vocabulary(self) -> Optional[VocabularyExtension]:
        if self._vocabulary is None:
            self._vocabulary = build_vocabulary_extension()
        return self._vocabulary

    def get_pii_patterns(self) -> Optional[PIIExtension]:
        if self._pii is None:
            self._pii = build_pii_extension()
        return self._pii

    def get_graph_schema(self) -> Optional[GraphSchemaExtension]:
        if self._graph_schema is None:
            self._graph_schema = build_graph_schema_extension()
        return self._graph_schema

    def get_reasoning(self) -> Optional[ReasoningExtension]:
        if self._reasoning is None:
            self._reasoning = build_reasoning_extension()
        return self._reasoning

    def get_document_types(self) -> Optional[DocumentTypeExtension]:
        if self._document_types is None:
            self._document_types = build_document_type_extension()
        return self._document_types

    def get_data_generators(self) -> Optional[DataGeneratorExtension]:
        if self._data_generators is None:
            self._data_generators = build_data_generator_extension()
        return self._data_generators

    def get_reports(self) -> Optional[ReportExtension]:
        if self._reports is None:
            self._reports = build_report_extension()
        return self._reports

    def get_execution(self) -> Optional[ExecutionExtension]:
        if self._execution is None:
            self._execution = build_execution_extension()
        return self._execution

    def get_chain_configs(self) -> list[ChainConfig]:
        return list(_CHAIN_CONFIGS)


# ─── Factory Function (required by PluginRegistry) ────────────


def create_plugin() -> InsuranceQAPlugin:
    """
    Create and return the Insurance QA plugin instance.

    This is the entry point called by PluginRegistry.load_from_module().
    """
    return InsuranceQAPlugin()
