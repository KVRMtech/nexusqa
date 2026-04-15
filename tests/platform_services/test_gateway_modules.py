"""
API Gateway — Modular Sub-package Tests.

Tests the config, route table, rate limiter, and proxy logic
that were refactored from the monolithic gateway/main.py.
"""

import pytest
import sys
import os
import time
from unittest.mock import MagicMock, AsyncMock, patch


# ═══════════════════════════════════════════════════════════════
# Config Module
# ═══════════════════════════════════════════════════════════════


class TestGatewayConfig:
    """Test GatewayConfig from app.config."""

    def test_import(self):
        from app.config import GatewayConfig
        assert GatewayConfig is not None

    def test_defaults(self):
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        assert cfg.gateway_port == 8080
        assert cfg.rate_limit_per_minute == 600
        assert cfg.jwt_algorithm == "HS256"

    def test_engine_urls(self):
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        assert cfg.auth_url == "http://localhost:8000"
        assert cfg.shield_url == "http://localhost:8001"
        assert cfg.ears_url == "http://localhost:8002"
        assert cfg.eyes_url == "http://localhost:8003"
        assert cfg.heart_url == "http://localhost:8004"
        assert cfg.backbone_url == "http://localhost:8005"
        assert cfg.nerves_url == "http://localhost:8006"
        assert cfg.legs_url == "http://localhost:8007"
        assert cfg.hands_url == "http://localhost:8008"
        assert cfg.spine_url == "http://localhost:8009"
        assert cfg.mouth_url == "http://localhost:8010"
        assert cfg.orchestrator_url == "http://localhost:8100"
        assert cfg.platform_api_url == "http://localhost:8091"

    def test_cors_origins(self):
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        assert "localhost:3000" in cfg.cors_origins
        assert "localhost:5173" in cfg.cors_origins

    def test_public_paths(self):
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        assert "/api/v1/auth/login" in cfg.public_path_prefixes

    def test_env_prefix(self):
        from app.config import GatewayConfig
        # env_prefix was intentionally removed — aliased fields (e.g.
        # nexus_jwt_secret) already embed the correct env var name.
        assert GatewayConfig.model_config.get("env_prefix", "") == ""


# ═══════════════════════════════════════════════════════════════
# Route Table Module
# ═══════════════════════════════════════════════════════════════


class TestRouteTable:
    """Test route table builder from app.routes."""

    def test_import(self):
        from app.routes import build_route_table
        assert callable(build_route_table)

    def test_returns_dict(self):
        from app.routes import build_route_table
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        routes = build_route_table(cfg)
        assert isinstance(routes, dict)
        assert len(routes) > 0

    def test_all_engine_prefixes_present(self):
        from app.routes import build_route_table
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        routes = build_route_table(cfg)

        expected_prefixes = [
            "/api/v1/auth", "/api/v1/shield", "/api/v1/ears",
            "/api/v1/eyes", "/api/v1/heart", "/api/v1/backbone",
            "/api/v1/nerves", "/api/v1/legs", "/api/v1/hands",
            "/api/v1/spine", "/api/v1/mouth", "/api/v1/qa",
        ]
        for prefix in expected_prefixes:
            assert prefix in routes, f"Missing route prefix: {prefix}"

    def test_platform_api_prefixes_present(self):
        from app.routes import build_route_table
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        routes = build_route_table(cfg)

        platform_prefixes = [
            "/api/v1/sessions", "/api/v1/sme", "/api/v1/contradictions",
            "/api/v1/guardrails", "/api/v1/traceability", "/api/v1/tests",
            "/api/v1/test-cases", "/api/v1/data-forge", "/api/v1/compliance",
            "/api/v1/insights", "/api/v1/admin",
        ]
        for prefix in platform_prefixes:
            assert prefix in routes, f"Missing platform prefix: {prefix}"
            assert routes[prefix] == cfg.platform_api_url

    def test_route_count(self):
        from app.routes import build_route_table
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        routes = build_route_table(cfg)
        # 12 engines + 2 orchestrator aliases + 11 platform + 2 qi-portal = 27
        assert len(routes) == 27

    def test_routes_map_to_correct_urls(self):
        from app.routes import build_route_table
        from app.config import GatewayConfig
        cfg = GatewayConfig()
        routes = build_route_table(cfg)
        assert routes["/api/v1/auth"] == cfg.auth_url
        assert routes["/api/v1/shield"] == cfg.shield_url
        assert routes["/api/v1/qa"] == cfg.orchestrator_url
        assert routes["/api/v1/orchestrator"] == cfg.orchestrator_url
        assert routes["/api/v1/personas"] == cfg.platform_api_url
        assert routes["/api/v1/missions"] == cfg.platform_api_url


# ═══════════════════════════════════════════════════════════════
# Rate Limiter Module
# ═══════════════════════════════════════════════════════════════


class TestRateLimiter:
    """Test sliding-window rate limiter from app.rate_limiter."""

    def test_import(self):
        from app.rate_limiter import RateLimiter
        assert RateLimiter is not None

    def test_allows_requests_below_limit(self):
        from app.rate_limiter import RateLimiter
        rl = RateLimiter(max_per_minute=10)
        for _ in range(10):
            assert rl.allow("tenant-1") is True

    def test_blocks_requests_at_limit(self):
        from app.rate_limiter import RateLimiter
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            rl.allow("tenant-2")
        assert rl.allow("tenant-2") is False

    def test_per_tenant_isolation(self):
        from app.rate_limiter import RateLimiter
        rl = RateLimiter(max_per_minute=3)
        for _ in range(3):
            rl.allow("tenant-a")
        # tenant-a is exhausted
        assert rl.allow("tenant-a") is False
        # tenant-b is fresh
        assert rl.allow("tenant-b") is True

    def test_zero_limit_allows_all(self):
        from app.rate_limiter import RateLimiter
        rl = RateLimiter(max_per_minute=0)
        for _ in range(100):
            assert rl.allow("any") is True

    def test_thread_safety_init(self):
        from app.rate_limiter import RateLimiter
        import threading
        rl = RateLimiter(max_per_minute=100)
        assert isinstance(rl._lock, type(threading.Lock()))


# ═══════════════════════════════════════════════════════════════
# Proxy Module
# ═══════════════════════════════════════════════════════════════


class TestProxy:
    """Test proxy logic from app.proxy."""

    def test_import(self):
        from app.proxy import proxy_request
        assert callable(proxy_request)

    @pytest.mark.asyncio
    async def test_proxy_no_matching_route(self):
        """Should return 404 when no route matches."""
        from app.proxy import proxy_request
        from app.config import GatewayConfig
        from app.rate_limiter import RateLimiter
        from fastapi import HTTPException

        cfg = GatewayConfig()
        cfg_dict = {}  # empty routes

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_client = AsyncMock()
        rl = RateLimiter(max_per_minute=600)

        with pytest.raises(HTTPException) as exc_info:
            await proxy_request(
                mock_request,
                "unknown/path",
                routes={},
                config=cfg,
                client=mock_client,
                rate_limiter=rl,
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_proxy_rate_limit_exceeded(self):
        """Should return 429 when rate limit is exceeded."""
        from app.proxy import proxy_request
        from app.config import GatewayConfig
        from app.rate_limiter import RateLimiter
        from fastapi import HTTPException

        cfg = GatewayConfig()
        rl = RateLimiter(max_per_minute=1)

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_client = AsyncMock()

        routes = {"/api/v1/test": "http://localhost:9999"}

        # Exhaust rate limit
        rl.allow("anonymous")

        with pytest.raises(HTTPException) as exc_info:
            await proxy_request(
                mock_request,
                "test/something",
                routes=routes,
                config=cfg,
                client=mock_client,
                rate_limiter=rl,
            )
        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_proxy_invalid_jwt_non_public(self):
        """Should return 401 for invalid JWT on non-public path."""
        from app.proxy import proxy_request
        from app.config import GatewayConfig
        from app.rate_limiter import RateLimiter
        from fastapi import HTTPException

        cfg = GatewayConfig()
        rl = RateLimiter(max_per_minute=600)

        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer invalid.jwt.token"}
        mock_client = AsyncMock()

        routes = {"/api/v1/test": "http://localhost:9999"}

        with pytest.raises(HTTPException) as exc_info:
            await proxy_request(
                mock_request,
                "test/something",
                routes=routes,
                config=cfg,
                client=mock_client,
                rate_limiter=rl,
            )
        assert exc_info.value.status_code == 401


# ═══════════════════════════════════════════════════════════════
# App Package Re-exports
# ═══════════════════════════════════════════════════════════════


class TestGatewayAppReExports:
    """Verify app package re-exports all public symbols."""

    def test_config(self):
        from app import GatewayConfig
        assert GatewayConfig is not None

    def test_route_table(self):
        from app import build_route_table
        assert callable(build_route_table)

    def test_rate_limiter(self):
        from app import RateLimiter
        assert RateLimiter is not None

    def test_proxy(self):
        from app import proxy_request
        assert callable(proxy_request)


# ═══════════════════════════════════════════════════════════════
# Main Entry-point
# ═══════════════════════════════════════════════════════════════


class TestGatewayMainEntryPoint:
    """Test that main.py creates the FastAPI app properly."""

    def test_app_import(self):
        from main import app
        assert app is not None

    def test_app_title(self):
        from main import app
        assert "gateway" in app.title.lower() or "nexus" in app.title.lower()

    def test_app_version(self):
        from main import app
        assert app.version == "0.2.0"
