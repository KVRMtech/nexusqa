"""
Nexus Plugin System — Domain-agnostic extension framework.

Allows domain plugins (insurance, healthcare, finance, etc.) to extend
engine behavior without modifying engine source code.

Architecture:
    Engine (generic) + DomainPlugin → Product-specific behavior

    ┌──────────────────┐
    │ Engine (generic)  │
    │       ↓           │
    │ Plugin Socket     │  ← loads extensions at startup
    │       ↓           │
    │ Domain Plugin     │  ← insurance, healthcare, finance, etc.
    │ (vocabulary,      │
    │  PII patterns,    │
    │  graph schema,    │
    │  prompts, etc.)   │
    └──────────────────┘

Quick Start:
    from nexus_sdk.plugins import PluginRegistry, DomainPlugin, PluginManifest

    # Load plugins
    registry = PluginRegistry.instance()
    registry.load_from_directory("products/")

    # Engines query merged extensions
    vocab = registry.get_merged_vocabulary()
    pii = registry.get_merged_pii()
"""

from nexus_sdk.plugins.base import (
    ChainConfig,
    DomainPlugin,
    PluginManifest,
)
from nexus_sdk.plugins.extensions import (
    DataGeneratorExtension,
    DataProfileDefinition,
    DocumentTypeDefinition,
    DocumentTypeExtension,
    ExecutionExtension,
    ExecutionTargetDefinition,
    FieldDefinition,
    GraphSchemaExtension,
    GuardrailRule,
    IDPatternDefinition,
    NodeTypeDefinition,
    PIIEntityDefinition,
    PIIExtension,
    PromptTemplate,
    ReasoningExtension,
    RelationshipTypeDefinition,
    ReportExtension,
    ReportTypeDefinition,
    VocabularyExtension,
    VocabularyTerm,
)
from nexus_sdk.plugins.registry import PluginRegistry

__all__ = [
    # Core
    "DomainPlugin",
    "PluginManifest",
    "PluginRegistry",
    "ChainConfig",
    # Extension containers
    "VocabularyExtension",
    "PIIExtension",
    "GraphSchemaExtension",
    "ReasoningExtension",
    "DocumentTypeExtension",
    "DataGeneratorExtension",
    "ReportExtension",
    "ExecutionExtension",
    # Definition types
    "VocabularyTerm",
    "PIIEntityDefinition",
    "NodeTypeDefinition",
    "RelationshipTypeDefinition",
    "PromptTemplate",
    "GuardrailRule",
    "DocumentTypeDefinition",
    "FieldDefinition",
    "DataProfileDefinition",
    "IDPatternDefinition",
    "ReportTypeDefinition",
    "ExecutionTargetDefinition",
]
