"""Integration plugin framework — Slack, Teams, email, webhook, etc.

Distinct from ``nexus_sdk.plugins`` (DomainPlugin) which describes
in-process domain extensions (vocabulary, PII, graph schema). The
``integrations`` package describes out-of-process system connectors
that move events into and out of the platform.

Public surface:
    * ``IntegrationManifest``       — strict Pydantic model
    * ``Capability``, ``AuthMethod`` — enums
    * ``load_manifest``             — filesystem loader
    * ``json_schema``               — exported JSON Schema (draft 2020-12)
    * ``ManifestError``             — validation failures
"""

from __future__ import annotations

from .manifest import (
    AuthMethod,
    Capability,
    IntegrationManifest,
    ManifestError,
    SourceCapability,
    SurfaceCapability,
    SinkCapability,
    HealthCheck,
    RateLimit,
    AuthSpec,
    EventSubscription,
    Action,
    json_schema,
    load_manifest,
)

__all__ = [
    "Action",
    "AuthMethod",
    "AuthSpec",
    "Capability",
    "EventSubscription",
    "HealthCheck",
    "IntegrationManifest",
    "ManifestError",
    "RateLimit",
    "SinkCapability",
    "SourceCapability",
    "SurfaceCapability",
    "json_schema",
    "load_manifest",
]
