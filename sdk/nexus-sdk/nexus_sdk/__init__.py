"""
Nexus SDK — Shared foundation for all Nexus engines.

Every engine imports this SDK to get:
- Standard API setup (FastAPI + health checks + metrics)
- Event bus (publish/subscribe via Redis Streams)
- Authentication & authorization helpers
- Configuration management
- Standard request/response models
- Logging with structured output
- Plugin system for domain-specific extensions

Usage:
    from nexus_sdk import NexusEngine, EngineConfig
    from nexus_sdk.models import NexusRequest, NexusResponse
    from nexus_sdk.plugins import PluginRegistry, DomainPlugin
"""

from nexus_sdk.engine import NexusEngine
from nexus_sdk.config import EngineConfig, production_guard
from nexus_sdk.stores import JobStore

__version__ = "0.1.0"
__all__ = ["NexusEngine", "EngineConfig", "JobStore", "production_guard", "__version__"]
