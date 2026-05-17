"""
Platform API — Modular Sub-package Tests.

Tests the config, database helpers, auth, middleware, and all routers
that were refactored from the monolithic platform/api/main.py.
"""

import pytest
import sys
import os
import uuid
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# Config Module
# ═══════════════════════════════════════════════════════════════


class TestPlatformAPIConfig:
    """Test PlatformAPIConfig from app.config."""

    def test_import(self):
        from app.config import PlatformAPIConfig
        assert PlatformAPIConfig is not None

    def test_defaults(self):
        from app.config import PlatformAPIConfig
        cfg = PlatformAPIConfig()
        assert cfg.port == 8091
        assert cfg.jwt_algorithm == "HS256"
        assert cfg.postgres_port == 5432
        assert cfg.redis_port == 6379
        assert cfg.ears_engine_url == "http://localhost:8002"
        assert cfg.eyes_engine_url == "http://localhost:8003"
        assert cfg.heart_engine_url == "http://localhost:8004"
        assert cfg.backbone_engine_url == "http://localhost:8005"
        assert cfg.shield_engine_url == "http://localhost:8001"
        assert cfg.nerves_engine_url == "http://localhost:8006"
        assert cfg.hands_engine_url == "http://localhost:8008"
        assert cfg.legs_engine_url == "http://localhost:8007"
        assert cfg.spine_engine_url == "http://localhost:8009"
        assert cfg.mouth_engine_url == "http://localhost:8010"

    def test_postgres_url_property(self):
        from app.config import PlatformAPIConfig
        cfg = PlatformAPIConfig()
        url = cfg.postgres_url
        assert "postgresql+asyncpg://" in url
        assert "nexus" in url
        assert "5432" in url

    def test_env_prefix(self):
        from app.config import PlatformAPIConfig
        # env_prefix was intentionally removed — aliased fields already
        # embed the correct env var names; prefix caused double-prefixing.
        assert PlatformAPIConfig.model_config.get("env_prefix", "") == ""
        assert PlatformAPIConfig.model_config["extra"] == "ignore"


# ═══════════════════════════════════════════════════════════════
# Database Module
# ═══════════════════════════════════════════════════════════════


class TestDatabaseHelpers:
    """Test database utility functions from app.database."""

    def test_import_all(self):
        from app.database import (
            init_db,
            close_db,
            get_session_factory,
            is_db_connected,
            require_db,
            new_id,
            utc_now,
            row_to_dict,
        )
        assert callable(init_db)
        assert callable(close_db)
        assert callable(get_session_factory)
        assert callable(is_db_connected)
        assert callable(require_db)
        assert callable(new_id)
        assert callable(utc_now)
        assert callable(row_to_dict)

    def test_new_id_returns_uuid(self):
        from app.database import new_id
        uid = new_id()
        # Should be a valid UUID string
        parsed = uuid.UUID(uid)
        assert str(parsed) == uid

    def test_new_id_unique(self):
        from app.database import new_id
        ids = {new_id() for _ in range(100)}
        assert len(ids) == 100

    def test_utc_now_returns_utc(self):
        from app.database import utc_now
        now = utc_now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None
        assert now.tzinfo == timezone.utc

    def test_row_to_dict(self):
        from app.database import row_to_dict
        from unittest.mock import MagicMock
        from datetime import datetime, timezone as tz

        # Create a mock ORM row
        mock_col1 = MagicMock()
        mock_col1.name = "id"
        mock_col2 = MagicMock()
        mock_col2.name = "created_at"
        mock_col3 = MagicMock()
        mock_col3.name = "name"

        mock_table = MagicMock()
        mock_table.columns = [mock_col1, mock_col2, mock_col3]

        row = MagicMock()
        row.__table__ = mock_table
        row.id = "abc-123"
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=tz.utc)
        row.created_at = ts
        row.name = "Test"

        result = row_to_dict(row)
        assert result["id"] == "abc-123"
        assert result["created_at"] == ts.isoformat()
        assert result["name"] == "Test"

    def test_is_db_connected_default(self):
        """Without calling init_db, the DB should report disconnected."""
        from app.database import is_db_connected
        # We don't call init_db so it depends on module-level state.
        # This at least verifies the function is callable.
        assert isinstance(is_db_connected(), bool)

    def test_require_db_raises_when_not_connected(self):
        """require_db raises 503 when no session factory is available."""
        from app.database import require_db
        import app.database as db_mod
        # Force _session_factory to None
        original = db_mod._session_factory
        db_mod._session_factory = None
        try:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                require_db()
            assert exc_info.value.status_code == 503
        finally:
            db_mod._session_factory = original


# ═══════════════════════════════════════════════════════════════
# Auth Module
# ═══════════════════════════════════════════════════════════════


class TestAuth:
    """Test auth module from app.auth."""

    def test_import(self):
        from app.auth import get_current_user, jwt_auth_middleware, PUBLIC_PATHS
        assert callable(get_current_user)
        assert callable(jwt_auth_middleware)
        assert isinstance(PUBLIC_PATHS, frozenset)

    def test_public_paths_contains_essentials(self):
        from app.auth import PUBLIC_PATHS
        assert "/" in PUBLIC_PATHS
        assert "/health" in PUBLIC_PATHS
        assert "/docs" in PUBLIC_PATHS
        assert "/openapi.json" in PUBLIC_PATHS

    @pytest.mark.asyncio
    async def test_get_current_user_no_credentials(self):
        """Should raise 401 when no credentials provided."""
        from app.auth import get_current_user
        from fastapi import HTTPException

        mock_request = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, credentials=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_valid_token(self):
        """Should decode JWT and set request.state.user."""
        from app.auth import get_current_user

        import jwt as pyjwt
        secret = "test-secret-key-that-is-long-enough"
        payload = {
            "sub": "user-123",
            "tenant_id": "tenant-abc",
            "email": "test@example.com",
            "role": "admin",
        }
        token = pyjwt.encode(payload, secret, algorithm="HS256")

        mock_creds = MagicMock()
        mock_creds.credentials = token

        mock_config = MagicMock()
        mock_config.jwt_secret = secret
        mock_config.jwt_algorithm = "HS256"

        mock_request = MagicMock()
        mock_request.app.state.config = mock_config

        user = await get_current_user(mock_request, credentials=mock_creds)
        assert user["user_id"] == "user-123"
        assert user["tenant_id"] == "tenant-abc"
        assert user["email"] == "test@example.com"
        assert user["role"] == "admin"

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_token(self):
        """Should raise 401 for an invalid token."""
        from app.auth import get_current_user
        from fastapi import HTTPException

        mock_creds = MagicMock()
        mock_creds.credentials = "invalid.token.value"

        mock_config = MagicMock()
        mock_config.jwt_secret = "test-secret"
        mock_config.jwt_algorithm = "HS256"

        mock_request = MagicMock()
        mock_request.app.state.config = mock_config

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, credentials=mock_creds)
        assert exc_info.value.status_code == 401


# ═══════════════════════════════════════════════════════════════
# Middleware Module
# ═══════════════════════════════════════════════════════════════


class TestSecurityHeaders:
    """Test security headers middleware from app.middleware."""

    def test_import(self):
        from app.middleware import security_headers_middleware
        assert callable(security_headers_middleware)

    @pytest.mark.asyncio
    async def test_adds_security_headers(self):
        from app.middleware import security_headers_middleware

        mock_request = MagicMock()
        mock_response = MagicMock()
        mock_response.headers = {}

        async def mock_call_next(req):
            return mock_response

        result = await security_headers_middleware(mock_request, mock_call_next)
        assert result.headers["X-Content-Type-Options"] == "nosniff"
        assert result.headers["X-Frame-Options"] == "DENY"
        assert result.headers["X-XSS-Protection"] == "1; mode=block"
        assert result.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "no-store" in result.headers["Cache-Control"]
        assert "no-cache" in result.headers["Cache-Control"]
        assert "must-revalidate" in result.headers["Cache-Control"]
        assert "max-age=31536000" in result.headers["Strict-Transport-Security"]
        assert result.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
        assert result.headers["Pragma"] == "no-cache"


# ═══════════════════════════════════════════════════════════════
# App Package __init__ Re-exports
# ═══════════════════════════════════════════════════════════════


class TestAppReExports:
    """Verify that the app package properly re-exports all public symbols."""

    def test_config_reexport(self):
        from app import PlatformAPIConfig
        assert PlatformAPIConfig is not None

    def test_database_reexports(self):
        from app import (
            init_db,
            close_db,
            get_session_factory,
            is_db_connected,
            require_db,
            new_id,
            utc_now,
            row_to_dict,
        )
        assert callable(init_db)
        assert callable(new_id)

    def test_auth_reexports(self):
        from app import get_current_user, jwt_auth_middleware, PUBLIC_PATHS
        assert callable(get_current_user)
        assert isinstance(PUBLIC_PATHS, frozenset)

    def test_middleware_reexport(self):
        from app import security_headers_middleware
        assert callable(security_headers_middleware)


# ═══════════════════════════════════════════════════════════════
# Router Modules — Import Tests
# ═══════════════════════════════════════════════════════════════


class TestRouterImports:
    """Verify all router modules import without error."""

    def test_sessions_router(self):
        from app.routers.sessions import router
        assert router is not None

    def test_sme_router(self):
        from app.routers.sme import router
        assert router is not None

    def test_contradictions_router(self):
        from app.routers.contradictions import router
        assert router is not None

    def test_guardrails_router(self):
        from app.routers.guardrails import router
        assert router is not None

    def test_traceability_router(self):
        from app.routers.traceability import router
        assert router is not None

    def test_tests_router(self):
        from app.routers.tests import router
        assert router is not None

    def test_data_forge_router(self):
        from app.routers.data_forge import router
        assert router is not None

    def test_compliance_router(self):
        from app.routers.compliance import router
        assert router is not None

    def test_insights_router(self):
        from app.routers.insights import router
        assert router is not None

    def test_admin_router(self):
        from app.routers.admin import router
        assert router is not None


# ═══════════════════════════════════════════════════════════════
# Router Modules — Route Presence Tests
# ═══════════════════════════════════════════════════════════════


class TestRouterEndpoints:
    """Verify each router defines the expected endpoints."""

    @staticmethod
    def _route_paths(router_mod):
        """Extract path strings from an APIRouter."""
        return [r.path for r in router_mod.router.routes]

    def test_sessions_routes(self):
        from app.routers import sessions
        paths = self._route_paths(sessions)
        assert any("/sessions" in p for p in paths)
        assert any("/sessions/{session_id}" in p for p in paths)

    def test_sme_routes(self):
        from app.routers import sme
        paths = self._route_paths(sme)
        assert any("/sme/profiles" in p for p in paths)

    def test_contradictions_routes(self):
        from app.routers import contradictions
        paths = self._route_paths(contradictions)
        assert any("/contradictions" in p for p in paths)

    def test_guardrails_routes(self):
        from app.routers import guardrails
        paths = self._route_paths(guardrails)
        assert any("/guardrails/pipeline" in p for p in paths)

    def test_traceability_routes(self):
        from app.routers import traceability
        paths = self._route_paths(traceability)
        assert any("/traceability" in p for p in paths)

    def test_tests_routes(self):
        from app.routers import tests
        paths = self._route_paths(tests)
        assert any("/test-suites" in p or "/tests" in p for p in paths)

    def test_data_forge_routes(self):
        from app.routers import data_forge
        paths = self._route_paths(data_forge)
        assert any("/data-forge" in p for p in paths)

    def test_compliance_routes(self):
        from app.routers import compliance
        paths = self._route_paths(compliance)
        assert any("/compliance" in p for p in paths)

    def test_insights_routes(self):
        from app.routers import insights
        paths = self._route_paths(insights)
        assert any("/insights" in p for p in paths)

    def test_admin_routes(self):
        from app.routers import admin
        paths = self._route_paths(admin)
        assert any("/admin" in p for p in paths)


# ═══════════════════════════════════════════════════════════════
# Insights / Admin — Init Functions
# ═══════════════════════════════════════════════════════════════


class TestInsightsInit:
    """Test insights module init wiring."""

    def test_init_insights_callable(self):
        from app.routers.insights import init_insights
        assert callable(init_insights)

    def test_init_insights_sets_module_state(self):
        from app.routers import insights
        mock_client = MagicMock()
        mock_cache = MagicMock()
        mock_config = MagicMock()

        insights.init_insights(mock_client, mock_cache, mock_config)
        assert insights._http is mock_client
        assert insights._cache is mock_cache
        assert insights._config is mock_config


class TestAdminInit:
    """Test admin module init wiring."""

    def test_init_admin_callable(self):
        from app.routers.admin import init_admin
        assert callable(init_admin)

    def test_init_admin_sets_module_state(self):
        from app.routers import admin
        mock_client = MagicMock()
        mock_cache = MagicMock()
        mock_config = MagicMock()

        admin.init_admin(mock_client, mock_cache, mock_config)
        assert admin._http is mock_client
        assert admin._cache is mock_cache
        assert admin._config is mock_config


# ═══════════════════════════════════════════════════════════════
# Main Entry-point
# ═══════════════════════════════════════════════════════════════


class TestMainEntryPoint:
    """Test that main.py properly creates the FastAPI app."""

    def test_app_import(self):
        from main import app
        assert app is not None

    def test_app_title(self):
        from main import app
        assert "nexus" in app.title.lower() or "platform" in app.title.lower()

    def test_app_version(self):
        from main import app
        assert app.version == "0.3.0"

    def test_backward_compat_reexports(self):
        """Main.py should re-export key symbols for backward compatibility."""
        from main import (
            PlatformAPIConfig,
            init_db,
            close_db,
            is_db_connected,
            get_current_user,
            row_to_dict,
            new_id,
            utc_now,
        )
        assert PlatformAPIConfig is not None
        assert callable(init_db)
        assert callable(new_id)
