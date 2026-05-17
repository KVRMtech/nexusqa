"""
Nexus Plugin Registry — Thread-safe singleton for managing domain plugins.

The registry is the central point where engines discover domain extensions.
It supports loading plugins from Python modules or filesystem directories,
and provides merged extension views across all loaded plugins.

Usage:
    from nexus_sdk.plugins import PluginRegistry

    # Get singleton
    registry = PluginRegistry.instance()

    # Load a plugin
    registry.load_from_module("products.nexus_qa.plugin")

    # Engines query merged extensions
    vocab = registry.get_merged_vocabulary()
    pii = registry.get_merged_pii()
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from .base import ChainConfig, DomainPlugin, PluginManifest
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


class PluginRegistry:
    """
    Central registry for domain plugins.

    Thread-safe singleton. Engines query it at startup to load
    domain-specific extensions.

    Supports multiple plugins simultaneously — extensions from all
    plugins are merged when queried via ``get_merged_*`` methods.
    """

    _instance: Optional["PluginRegistry"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._plugins: dict[str, DomainPlugin] = {}
        self._load_lock = threading.Lock()

    # ─── Singleton ────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "PluginRegistry":
        """Get or create the singleton registry instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """
        Reset the singleton instance.

        Primarily for testing — unloads all plugins and clears state.
        """
        with cls._lock:
            if cls._instance is not None:
                for plugin in cls._instance._plugins.values():
                    try:
                        plugin.on_unload()
                    except Exception:
                        pass
                cls._instance._plugins.clear()
            cls._instance = None

    # ─── Plugin Management ────────────────────────────────────

    def register(self, plugin: DomainPlugin) -> None:
        """
        Register a domain plugin.

        If a plugin with the same ID is already registered, the old
        one is unloaded and replaced.
        """
        with self._load_lock:
            pid = plugin.plugin_id
            existing = self._plugins.get(pid)
            if existing is not None:
                logger.warning(
                    "Replacing already-registered plugin: %s", pid
                )
                try:
                    existing.on_unload()
                except Exception as exc:
                    logger.error("Error unloading plugin %s: %s", pid, exc)

            plugin.on_load()
            self._plugins[pid] = plugin
            logger.info(
                "Plugin registered: %s v%s (domain=%s, extensions=%s)",
                pid,
                plugin.version,
                plugin.domain,
                plugin.get_provided_extensions(),
            )

    def unregister(self, plugin_id: str) -> bool:
        """
        Unregister a plugin by ID.

        Returns True if the plugin was found and removed.
        """
        with self._load_lock:
            plugin = self._plugins.pop(plugin_id, None)
            if plugin is not None:
                try:
                    plugin.on_unload()
                except Exception as exc:
                    logger.error(
                        "Error unloading plugin %s: %s", plugin_id, exc
                    )
                logger.info("Plugin unregistered: %s", plugin_id)
                return True
            return False

    def get_plugin(self, plugin_id: str) -> Optional[DomainPlugin]:
        """Get a registered plugin by ID."""
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[PluginManifest]:
        """Return manifests of all registered plugins."""
        return [p.manifest for p in self._plugins.values()]

    def get_plugins_for_domain(self, domain: str) -> list[DomainPlugin]:
        """Return all plugins for a specific domain."""
        return [p for p in self._plugins.values() if p.domain == domain]

    @property
    def plugin_count(self) -> int:
        return len(self._plugins)

    @property
    def is_empty(self) -> bool:
        return len(self._plugins) == 0

    # ─── Plugin Loading ───────────────────────────────────────

    def load_from_module(self, module_path: str) -> DomainPlugin:
        """
        Load a plugin from a Python module path.

        The module must define a ``create_plugin()`` function
        that returns a ``DomainPlugin`` instance.

        Args:
            module_path: Dotted module path (e.g., "products.nexus_qa.plugin")

        Returns:
            The loaded and registered DomainPlugin.

        Raises:
            ValueError: If module cannot be imported or lacks ``create_plugin()``.
            TypeError: If ``create_plugin()`` returns wrong type.
        """
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ValueError(
                f"Cannot import plugin module '{module_path}': {exc}"
            ) from exc

        factory = getattr(module, "create_plugin", None)
        if factory is None:
            raise ValueError(
                f"Module '{module_path}' has no 'create_plugin()' function. "
                f"Every plugin module must define: "
                f"def create_plugin() -> DomainPlugin"
            )

        plugin = factory()
        if not isinstance(plugin, DomainPlugin):
            raise TypeError(
                f"create_plugin() in '{module_path}' returned "
                f"{type(plugin).__name__}, expected DomainPlugin subclass"
            )

        self.register(plugin)
        return plugin

    def load_from_file(self, file_path: str) -> DomainPlugin:
        """
        Load a plugin from a specific Python file.

        The file must define a ``create_plugin()`` function.

        Args:
            file_path: Filesystem path to a ``plugin.py`` file.

        Returns:
            The loaded and registered DomainPlugin.
        """
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Plugin file not found: {path}")

        # Derive a unique module name from the path
        module_name = f"nexus_plugin_{path.parent.name}"

        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot create module spec for: {path}")

        # Ensure parent directory is in sys.path for relative imports
        parent_dir = str(path.parent.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise ValueError(
                f"Error loading plugin from '{path}': {exc}"
            ) from exc

        factory = getattr(module, "create_plugin", None)
        if factory is None:
            sys.modules.pop(module_name, None)
            raise ValueError(
                f"File '{path}' has no 'create_plugin()' function."
            )

        plugin = factory()
        if not isinstance(plugin, DomainPlugin):
            sys.modules.pop(module_name, None)
            raise TypeError(
                f"create_plugin() in '{path}' returned "
                f"{type(plugin).__name__}, expected DomainPlugin subclass"
            )

        self.register(plugin)
        return plugin

    def load_from_directory(self, directory: str) -> list[DomainPlugin]:
        """
        Scan a directory for plugin packages and load them.

        Each subdirectory containing a ``plugin.py`` with
        ``create_plugin()`` is loaded.

        Args:
            directory: Path to scan for plugin packages.

        Returns:
            List of successfully loaded plugins.
        """
        dir_path = Path(directory).resolve()
        if not dir_path.is_dir():
            logger.warning("Plugin directory not found: %s", directory)
            return []

        loaded: list[DomainPlugin] = []

        for child in sorted(dir_path.iterdir()):
            if not child.is_dir():
                continue

            plugin_file = child / "plugin.py"
            if not plugin_file.exists():
                continue

            try:
                plugin = self.load_from_file(str(plugin_file))
                loaded.append(plugin)
                logger.info(
                    "Auto-loaded plugin from: %s", child.name
                )
            except Exception as exc:
                logger.error(
                    "Failed to load plugin from '%s': %s",
                    child.name,
                    exc,
                )

        return loaded

    # ─── Merged Extension Queries ─────────────────────────────
    #
    # These aggregate extensions from ALL loaded plugins.
    # Engines call these to get the combined domain configuration.

    def get_merged_vocabulary(self) -> VocabularyExtension:
        """Merge vocabulary extensions from all plugins."""
        merged = VocabularyExtension(
            domain="merged", terms=[], boost_phrases=[], suppressed_phrases=[]
        )
        for plugin in self._plugins.values():
            ext = plugin.get_vocabulary()
            if ext is not None:
                merged.terms.extend(ext.terms)
                merged.boost_phrases.extend(ext.boost_phrases)
                merged.suppressed_phrases.extend(ext.suppressed_phrases)
        return merged

    def get_merged_pii(self) -> PIIExtension:
        """Merge PII extensions from all plugins."""
        merged = PIIExtension(
            domain="merged", entity_types=[], context_rules=[]
        )
        for plugin in self._plugins.values():
            ext = plugin.get_pii_patterns()
            if ext is not None:
                merged.entity_types.extend(ext.entity_types)
                merged.context_rules.extend(ext.context_rules)
        return merged

    def get_merged_graph_schema(self) -> GraphSchemaExtension:
        """Merge graph schema extensions from all plugins."""
        merged = GraphSchemaExtension(
            domain="merged",
            node_types=[],
            relationship_types=[],
            constraints=[],
        )
        for plugin in self._plugins.values():
            ext = plugin.get_graph_schema()
            if ext is not None:
                merged.node_types.extend(ext.node_types)
                merged.relationship_types.extend(ext.relationship_types)
                merged.constraints.extend(ext.constraints)
        return merged

    def get_merged_reasoning(self) -> ReasoningExtension:
        """Merge reasoning extensions from all plugins."""
        merged = ReasoningExtension(
            domain="merged",
            prompt_templates=[],
            supported_tasks=[],
            guardrail_rules=[],
        )
        for plugin in self._plugins.values():
            ext = plugin.get_reasoning()
            if ext is not None:
                merged.prompt_templates.extend(ext.prompt_templates)
                merged.supported_tasks.extend(ext.supported_tasks)
                merged.guardrail_rules.extend(ext.guardrail_rules)
        return merged

    def get_merged_document_types(self) -> DocumentTypeExtension:
        """Merge document type extensions from all plugins."""
        merged = DocumentTypeExtension(
            domain="merged", document_types=[]
        )
        for plugin in self._plugins.values():
            ext = plugin.get_document_types()
            if ext is not None:
                merged.document_types.extend(ext.document_types)
        return merged

    def get_merged_data_generators(self) -> DataGeneratorExtension:
        """Merge data generator extensions from all plugins."""
        merged = DataGeneratorExtension(
            domain="merged", profiles=[], id_patterns=[]
        )
        for plugin in self._plugins.values():
            ext = plugin.get_data_generators()
            if ext is not None:
                merged.profiles.extend(ext.profiles)
                merged.id_patterns.extend(ext.id_patterns)
        return merged

    def get_merged_reports(self) -> ReportExtension:
        """Merge report extensions from all plugins."""
        merged = ReportExtension(
            domain="merged", report_types=[], branding={}
        )
        for plugin in self._plugins.values():
            ext = plugin.get_reports()
            if ext is not None:
                merged.report_types.extend(ext.report_types)
                merged.branding.update(ext.branding)
        return merged

    def get_merged_execution(self) -> ExecutionExtension:
        """Merge execution extensions from all plugins."""
        merged = ExecutionExtension(
            domain="merged",
            target_types=[],
            self_healing_strategies=[],
            evidence_types=[],
        )
        for plugin in self._plugins.values():
            ext = plugin.get_execution()
            if ext is not None:
                merged.target_types.extend(ext.target_types)
                merged.self_healing_strategies.extend(
                    ext.self_healing_strategies
                )
                merged.evidence_types.extend(ext.evidence_types)
        return merged

    def get_all_chain_configs(self) -> list[ChainConfig]:
        """Collect chain configurations from all plugins."""
        chains: list[ChainConfig] = []
        for plugin in self._plugins.values():
            chains.extend(plugin.get_chain_configs())
        return chains
