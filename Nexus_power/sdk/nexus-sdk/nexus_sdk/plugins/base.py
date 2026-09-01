"""
Nexus Domain Plugin — Base class for all domain plugins.

A DomainPlugin packages domain-specific extensions that customize
engine behavior for a particular industry or use case.

The same 10 engines power different products by loading different plugins:
    Insurance QA Plugin  → engines behave as insurance testing tools
    Healthcare Plugin    → engines behave as clinical compliance tools
    Finance Plugin       → engines behave as financial audit tools

Each plugin provides up to 8 extensions (one per engine type),
plus optional workflow chain definitions.

Usage:
    class MyDomainPlugin(DomainPlugin):
        def __init__(self):
            super().__init__(manifest=PluginManifest(
                plugin_id="mycompany.my-domain",
                name="My Domain Plugin",
                domain="my_domain",
                version="1.0.0",
            ))

        def get_vocabulary(self) -> VocabularyExtension:
            return VocabularyExtension(domain="my_domain", terms=[...])

        # Override other get_* methods as needed
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from .extensions import (
    DataGeneratorExtension,
    DocumentTypeExtension,
    ExecutionExtension,
    GraphSchemaExtension,
    PIIExtension,
    ReasoningExtension,
    ReportExtension,
    VocabularyExtension,
)

logger = logging.getLogger(__name__)


class PluginManifest(BaseModel):
    """Metadata describing a domain plugin."""

    plugin_id: str = Field(
        ...,
        description="Globally unique plugin ID (e.g., 'nexus.insurance-qa')",
    )
    name: str = Field(..., description="Human-readable plugin name")
    domain: str = Field(
        ...,
        description="Domain identifier (e.g., 'insurance', 'healthcare', 'finance')",
    )
    version: str = Field(default="1.0.0")
    description: str = Field(default="")
    author: str = Field(default="")
    min_platform_version: str = Field(
        default="0.1.0",
        description="Minimum Nexus platform version required",
    )
    tags: list[str] = Field(default_factory=list)
    engines_extended: list[str] = Field(
        default_factory=list,
        description="Which engines this plugin extends (ears, shield, heart, etc.)",
    )


class ChainConfig(BaseModel):
    """
    Lightweight chain definition that plugins can provide.

    This is a transport format — the orchestrator converts it to its
    internal ChainDefinition at registration time.
    """

    chain_id: str = Field(..., description="Unique chain identifier (e.g., 'nexus.qa-testing')")
    name: str = Field(...)
    description: str = Field(default="")
    version: str = Field(default="1.0.0")
    tags: list[str] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Stage definitions as dicts (engine, endpoint, input_mapping, etc.)",
    )


class DomainPlugin:
    """
    Base class for domain plugins.

    Subclass and override the ``get_*`` methods to provide
    domain-specific extensions for each engine type.

    Lifecycle:
        1. Plugin is instantiated (``__init__``)
        2. ``on_load()`` called when registered in PluginRegistry
        3. Engines call ``get_*`` methods during startup
        4. ``on_unload()`` called when plugin is removed
    """

    def __init__(self, manifest: PluginManifest) -> None:
        self._manifest = manifest

    # ─── Read-only properties ─────────────────────────────────

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def plugin_id(self) -> str:
        return self._manifest.plugin_id

    @property
    def domain(self) -> str:
        return self._manifest.domain

    @property
    def version(self) -> str:
        return self._manifest.version

    # ─── Engine extension hooks (override in subclasses) ──────

    def get_vocabulary(self) -> Optional[VocabularyExtension]:
        """Return vocabulary extension for Ears engine, or None."""
        return None

    def get_pii_patterns(self) -> Optional[PIIExtension]:
        """Return PII extension for Shield engine, or None."""
        return None

    def get_graph_schema(self) -> Optional[GraphSchemaExtension]:
        """Return graph schema extension for Backbone engine, or None."""
        return None

    def get_reasoning(self) -> Optional[ReasoningExtension]:
        """Return reasoning extension for Heart engine, or None."""
        return None

    def get_document_types(self) -> Optional[DocumentTypeExtension]:
        """Return document type extension for Spine engine, or None."""
        return None

    def get_data_generators(self) -> Optional[DataGeneratorExtension]:
        """Return data generator extension for Hands engine, or None."""
        return None

    def get_reports(self) -> Optional[ReportExtension]:
        """Return report extension for Mouth engine, or None."""
        return None

    def get_execution(self) -> Optional[ExecutionExtension]:
        """Return execution extension for Legs engine, or None."""
        return None

    # ─── Chain definitions (optional) ─────────────────────────

    def get_chain_configs(self) -> list[ChainConfig]:
        """
        Return workflow chain configurations for the orchestrator.

        Override to provide product-specific orchestration chains.
        """
        return []

    # ─── Lifecycle hooks ──────────────────────────────────────

    def on_load(self) -> None:
        """Called when the plugin is registered in the PluginRegistry."""
        logger.info(
            "Plugin loaded: %s v%s (domain=%s)",
            self.plugin_id,
            self.version,
            self.domain,
        )

    def on_unload(self) -> None:
        """Called when the plugin is removed from the PluginRegistry."""
        logger.info("Plugin unloaded: %s", self.plugin_id)

    # ─── Introspection ────────────────────────────────────────

    def get_provided_extensions(self) -> list[str]:
        """Return list of extension types this plugin provides."""
        provided: list[str] = []
        if self.get_vocabulary() is not None:
            provided.append("vocabulary")
        if self.get_pii_patterns() is not None:
            provided.append("pii")
        if self.get_graph_schema() is not None:
            provided.append("graph_schema")
        if self.get_reasoning() is not None:
            provided.append("reasoning")
        if self.get_document_types() is not None:
            provided.append("document_types")
        if self.get_data_generators() is not None:
            provided.append("data_generators")
        if self.get_reports() is not None:
            provided.append("reports")
        if self.get_execution() is not None:
            provided.append("execution")
        return provided

    def __repr__(self) -> str:
        return (
            f"<DomainPlugin '{self._manifest.plugin_id}' "
            f"v{self._manifest.version} domain={self._manifest.domain}>"
        )
