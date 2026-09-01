"""
Auth Service — Modular Sub-package Tests.

Tests the config, store (in-memory mode), models, security, and routes
that were refactored from the monolithic auth-service/main.py.
"""

import pytest
import sys
import os
import time
from unittest.mock import MagicMock, AsyncMock, patch


# ═══════════════════════════════════════════════════════════════
# Config Module
# ═══════════════════════════════════════════════════════════════


class TestAuthConfig:
    """Test AuthConfig from app.config."""

    def test_import(self):
        from app.config import AuthConfig
        assert AuthConfig is not None

    def test_defaults(self):
        from app.config import AuthConfig
        cfg = AuthConfig()
        assert cfg.engine_name == "auth"
        assert cfg.engine_port == 8000

    def test_admin_defaults(self):
        from app.config import AuthConfig
        cfg = AuthConfig()
        assert hasattr(cfg, "nexus_admin_email")
        assert hasattr(cfg, "nexus_admin_password")


# ═══════════════════════════════════════════════════════════════
# Security Module
# ═══════════════════════════════════════════════════════════════


class TestSecurity:
    """Test security helpers from app.security."""

    def test_import_all(self):
        from app.security import (
            hash_password,
            verify_password,
            role_permissions,
            check_brute_force,
            record_failed_attempt,
            is_insecure,
        )
        assert callable(hash_password)
        assert callable(verify_password)

    def test_hash_and_verify(self):
        from app.security import hash_password, verify_password
        pw = "MySecureP@ssw0rd!"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed) is True
        assert verify_password("WrongPassword", hashed) is False

    def test_hash_different_each_time(self):
        from app.security import hash_password
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        # Bcrypt salts differ
        assert h1 != h2

    def test_verify_invalid_hash(self):
        from app.security import verify_password
        assert verify_password("anything", "not-a-valid-hash") is False

    def test_role_permissions_admin(self):
        from app.security import role_permissions
        perms = role_permissions("admin")
        assert "*" in perms

    def test_role_permissions_manager(self):
        from app.security import role_permissions
        perms = role_permissions("manager")
        assert "sessions.read" in perms
        assert "sessions.create" in perms
        assert "tests.execute" in perms

    def test_role_permissions_viewer(self):
        from app.security import role_permissions
        perms = role_permissions("viewer")
        assert "sessions.read" in perms
        assert "sessions.create" not in perms

    def test_role_permissions_api(self):
        from app.security import role_permissions
        perms = role_permissions("api")
        assert "sessions.create" in perms
        assert "tests.execute" in perms

    def test_role_permissions_unknown(self):
        from app.security import role_permissions
        assert role_permissions("unknown_role") == []

    def test_is_insecure_patterns(self):
        from app.security import is_insecure
        assert is_insecure("change-me") is True
        assert is_insecure("admin123") is True
        assert is_insecure("password") is True
        assert is_insecure("dev-secret") is True
        assert is_insecure("short") is True  # < 16 chars

    def test_is_insecure_secure(self):
        from app.security import is_insecure
        assert is_insecure("a-very-long-and-secure-random-key-12345") is False

    def test_brute_force_allows_normal_usage(self):
        from app.security import check_brute_force, _login_attempts
        email = f"test-normal-{time.monotonic()}@example.com"
        _login_attempts.pop(email, None)
        # Should not raise for fresh email
        check_brute_force(email)

    def test_brute_force_blocks_after_max(self):
        from app.security import (
            check_brute_force,
            record_failed_attempt,
            MAX_LOGIN_ATTEMPTS,
            _login_attempts,
        )
        from fastapi import HTTPException

        email = f"test-blocked-{time.monotonic()}@example.com"
        _login_attempts.pop(email, None)

        for _ in range(MAX_LOGIN_ATTEMPTS):
            record_failed_attempt(email)

        with pytest.raises(HTTPException) as exc_info:
            check_brute_force(email)
        assert exc_info.value.status_code == 429

        # Cleanup
        _login_attempts.pop(email, None)


# ═══════════════════════════════════════════════════════════════
# Models Module
# ═══════════════════════════════════════════════════════════════


class TestAuthModels:
    """Test Pydantic request/response models from app.models."""

    def test_import_all(self):
        from app.models import (
            CreateTenantRequest,
            TenantResponse,
            LoginRequest,
            LoginResponse,
            CreateUserRequest,
            UserResponse,
        )
        assert CreateTenantRequest is not None

    def test_create_tenant_request(self):
        from app.models import CreateTenantRequest
        req = CreateTenantRequest(
            name="Acme Corp",
            domain="acme.com",
            admin_email="admin@acme.com",
        )
        assert req.name == "Acme Corp"
        assert req.domain == "acme.com"
        assert req.plan == "starter"

    def test_create_tenant_request_validation(self):
        from app.models import CreateTenantRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateTenantRequest(name="A", domain="x", admin_email="e")  # name too short

    def test_login_request(self):
        from app.models import LoginRequest
        req = LoginRequest(email="user@test.com", password="secret")
        assert req.email == "user@test.com"

    def test_login_response(self):
        from app.models import LoginResponse
        resp = LoginResponse(
            access_token="token123",
            user={"user_id": "u1", "email": "a@b.com"},
        )
        assert resp.token_type == "bearer"
        assert resp.access_token == "token123"

    def test_create_user_request_defaults(self):
        from app.models import CreateUserRequest
        req = CreateUserRequest(email="a@b.com", name="User", password="password123")
        assert req.role == "viewer"

    def test_tenant_response(self):
        from app.models import TenantResponse
        resp = TenantResponse(
            tenant_id="t1",
            name="Acme",
            domain="acme.com",
            plan="starter",
            created_at="2025-01-01T00:00:00Z",
            status="active",
        )
        assert resp.tenant_id == "t1"

    def test_user_response(self):
        from app.models import UserResponse
        resp = UserResponse(
            user_id="u1",
            tenant_id="t1",
            email="a@b.com",
            name="User",
            role="viewer",
            created_at="2025-01-01T00:00:00Z",
        )
        assert resp.user_id == "u1"


# ═══════════════════════════════════════════════════════════════
# Store Module (In-Memory Mode)
# ═══════════════════════════════════════════════════════════════


class TestAuthStoreInMemory:
    """Test AuthStore using in-memory fallback (no PostgreSQL)."""

    def test_import(self):
        from app.store import AuthStore
        assert AuthStore is not None

    def test_init(self):
        from app.store import AuthStore
        store = AuthStore()
        assert store._db is None
        assert store.using_postgres is False

    @pytest.mark.asyncio
    async def test_save_and_get_tenant(self):
        from app.store import AuthStore
        store = AuthStore()
        tenant = {
            "tenant_id": "t-001",
            "name": "Test Corp",
            "domain": "test.com",
            "plan": "starter",
            "status": "active",
            "created_at": "2025-01-01T00:00:00Z",
        }
        await store.save_tenant(tenant)
        result = await store.get_tenant("t-001")
        assert result is not None
        assert result["name"] == "Test Corp"

    @pytest.mark.asyncio
    async def test_get_tenant_not_found(self):
        from app.store import AuthStore
        store = AuthStore()
        result = await store.get_tenant("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_tenants(self):
        from app.store import AuthStore
        store = AuthStore()
        await store.save_tenant({"tenant_id": "t-a", "name": "A"})
        await store.save_tenant({"tenant_id": "t-b", "name": "B"})
        tenants = await store.list_tenants()
        assert len(tenants) >= 2

    @pytest.mark.asyncio
    async def test_list_tenants_filtered(self):
        from app.store import AuthStore
        store = AuthStore()
        await store.save_tenant({"tenant_id": "t-x", "name": "X"})
        await store.save_tenant({"tenant_id": "t-y", "name": "Y"})
        result = await store.list_tenants(filter_tenant_id="t-x")
        assert len(result) == 1
        assert result[0]["tenant_id"] == "t-x"

    @pytest.mark.asyncio
    async def test_save_and_get_user(self):
        from app.store import AuthStore
        store = AuthStore()
        user = {
            "email": "test@example.com",
            "user_id": "u-001",
            "tenant_id": "t-001",
            "name": "Test User",
            "role": "admin",
            "password_hash": "hashed",
        }
        await store.save_user(user)
        result = await store.get_user_by_email("test@example.com")
        assert result is not None
        assert result["user_id"] == "u-001"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self):
        from app.store import AuthStore
        store = AuthStore()
        result = await store.get_user_by_email("nobody@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_email_exists(self):
        from app.store import AuthStore
        store = AuthStore()
        await store.save_user({"email": "exists@example.com"})
        assert await store.email_exists("exists@example.com") is True
        assert await store.email_exists("no@example.com") is False


# ═══════════════════════════════════════════════════════════════
# Routes Module
# ═══════════════════════════════════════════════════════════════


class TestAuthRoutes:
    """Test routes module from app.routes."""

    def test_import(self):
        from app.routes import register_routes
        assert callable(register_routes)


# ═══════════════════════════════════════════════════════════════
# App Package Re-exports
# ═══════════════════════════════════════════════════════════════


class TestAuthAppReExports:
    """Verify app package re-exports all public symbols."""

    def test_config(self):
        from app import AuthConfig
        assert AuthConfig is not None

    def test_store(self):
        from app import AuthStore
        assert AuthStore is not None

    def test_security(self):
        from app import hash_password, verify_password, role_permissions, is_insecure
        assert callable(hash_password)

    def test_models(self):
        from app import (
            CreateTenantRequest,
            TenantResponse,
            LoginRequest,
            LoginResponse,
            CreateUserRequest,
            UserResponse,
        )
        assert CreateTenantRequest is not None

    def test_routes(self):
        from app import register_routes
        assert callable(register_routes)


# ═══════════════════════════════════════════════════════════════
# Main Entry-point
# ═══════════════════════════════════════════════════════════════


class TestAuthMainEntryPoint:
    """Test that main.py creates the AuthEngine properly."""

    def test_import(self):
        from main import AuthEngine
        assert AuthEngine is not None

    def test_engine_instantiation(self):
        from main import AuthEngine
        engine = AuthEngine()
        assert engine.config.engine_name == "auth"
        assert engine.config.engine_port == 8000
