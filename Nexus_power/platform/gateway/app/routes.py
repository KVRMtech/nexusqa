"""
API Gateway — Route table.

Maps path prefixes to upstream engine URLs.
"""
from __future__ import annotations

from .config import GatewayConfig


def build_route_table(cfg: GatewayConfig) -> dict[str, str]:
    """Build the prefix → upstream URL mapping from configuration."""
    return {
        # Engine routes
        "/api/v1/auth": cfg.auth_url,
        "/api/v1/shield": cfg.shield_url,
        "/api/v1/ears": cfg.ears_url,
        "/api/v1/eyes": cfg.eyes_url,
        "/api/v1/heart": cfg.heart_url,
        "/api/v1/backbone": cfg.backbone_url,
        "/api/v1/nerves": cfg.nerves_url,
        "/api/v1/legs": cfg.legs_url,
        "/api/v1/hands": cfg.hands_url,
        "/api/v1/spine": cfg.spine_url,
        "/api/v1/mouth": cfg.mouth_url,
        "/api/v1/brain": cfg.brain_url,
        # Orchestrator — generic workflow engine
        "/api/v1/orchestrator": cfg.orchestrator_url,
        "/api/v1/qa": cfg.orchestrator_url,
        # Phase 1 canonical-workflow plane lives in the orchestrator.
        # The /api/v1/workflows prefix is reserved for the legacy chain
        # read model on platform-api; the new surface uses its own
        # namespace so the two never collide.
        "/api/v1/canonical-workflows": cfg.orchestrator_url,
        "/api/v1/canonical-admin": cfg.orchestrator_url,
        # Platform API passthrough — artifacts & workflows (Phase 7)
        "/api/v1/artifacts": cfg.platform_api_url,
        "/api/v1/workflows": cfg.platform_api_url,
        # Platform API passthrough — QI Portal
        "/api/v1/personas": cfg.platform_api_url,
        "/api/v1/missions": cfg.platform_api_url,
        "/api/v1/test-strategy": cfg.platform_api_url,
        "/api/v1/e2e-architect": cfg.platform_api_url,
        # Test Factory — Pages & Forms → grounded test cases (additive)
        "/api/v1/test-factory": cfg.platform_api_url,
        # Run screenshots + heal-capture serve (the 'This run' actual frames the
        # browser <img> loads, and the failure-state capture). Reads go through
        # the gateway; without this prefix they 404 'No engine handles path'
        # even though the reporter uploads them direct to platform-api.
        "/api/v1/test-runs": cfg.platform_api_url,
        # Platform API passthrough — modules
        "/api/v1/sessions": cfg.platform_api_url,
        "/api/v1/sme": cfg.platform_api_url,
        "/api/v1/contradictions": cfg.platform_api_url,
        "/api/v1/guardrails": cfg.platform_api_url,
        "/api/v1/traceability": cfg.platform_api_url,
        "/api/v1/tests": cfg.platform_api_url,
        "/api/v1/test-cases": cfg.platform_api_url,
        "/api/v1/data-forge": cfg.platform_api_url,
        "/api/v1/compliance": cfg.platform_api_url,
        "/api/v1/insights": cfg.platform_api_url,
        "/api/v1/admin": cfg.platform_api_url,
    }
