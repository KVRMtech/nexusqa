"""
Nexus Auth Engine v0.2.0 — Authentication, Users & Tenant Management.

v0.2.0 — Modular refactor:
  app.config    → AuthConfig
  app.store     → AuthStore (PostgreSQL + in-memory fallback)
  app.models    → Request/Response Pydantic models
  app.security  → Password hashing, brute-force, role→permissions
  app.routes    → Route registration

Provides:
  - JWT authentication (bcrypt password hashing)
  - Multi-tenant user management
  - Role-based access control (RBAC)
  - Brute-force login protection
  - Insecure-secret detection for production
"""

from __future__ import annotations

import os
import sys
import uuid
import logging
from datetime import datetime, timezone

from nexus_sdk import NexusEngine
from nexus_sdk.auth import init_auth
from nexus_sdk.db import Database, PostgresConfig

# ─── Path Setup ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk")
for p in [_THIS_DIR, _SDK_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ─── Modular sub-packages ─────────────────────────────────────
from app.config import AuthConfig
from app.store import AuthStore
from app.security import hash_password, is_insecure
from app.routes import register_routes

# Backward-compat re-exports
from app.models import (  # noqa: F401
    CreateTenantRequest,
    TenantResponse,
    LoginRequest,
    LoginResponse,
    CreateUserRequest,
    UserResponse,
)
from app.security import (  # noqa: F401
    verify_password as _verify_password,
    role_permissions as _role_permissions,
    check_brute_force as _check_brute_force,
    record_failed_attempt as _record_failed_attempt,
)

logger = logging.getLogger(__name__)

# Module-level store (shared by engine + routes)
_store = AuthStore()


class AuthEngine(NexusEngine):
    def __init__(self):
        self.cfg = AuthConfig()
        super().__init__(
            name="auth",
            version="0.2.0",
            config=self.cfg,
            description="Authentication, users, and tenant management",
        )

    async def on_startup(self):
        """Create the default platform admin on first boot."""

        # ── Security: Reject default / insecure secrets in production ──
        if self.cfg.nexus_env == "production":
            if is_insecure(self.cfg.nexus_jwt_secret):
                raise RuntimeError(
                    "FATAL: nexus_jwt_secret is insecure. "
                    "Set NEXUS_JWT_SECRET to a strong random secret (>= 32 chars)."
                )
            if is_insecure(self.cfg.nexus_admin_password):
                raise RuntimeError(
                    "FATAL: nexus_admin_password is insecure. "
                    "Set NEXUS_ADMIN_PASSWORD to a strong password (>= 16 chars)."
                )
            if is_insecure(self.cfg.nexus_secret_key):
                raise RuntimeError(
                    "FATAL: nexus_secret_key is insecure. "
                    "Set NEXUS_SECRET_KEY to a strong random key (>= 32 chars)."
                )
        else:
            if is_insecure(self.cfg.nexus_jwt_secret):
                logger.warning(
                    "auth.insecure_jwt_secret: JWT secret is insecure — acceptable only in development"
                )
            if is_insecure(self.cfg.nexus_admin_password):
                logger.warning(
                    "auth.insecure_admin_password: Admin password is insecure — change before production"
                )

        admin_id = str(uuid.uuid4())

        # Connect to PostgreSQL. In-memory fallback is opt-in only.
        await _store.connect(PostgresConfig())

        # Create default admin tenant
        platform_tenant_id = "nexus-platform"
        existing = await _store.get_tenant(platform_tenant_id)
        if not existing:
            await _store.save_tenant({
                "tenant_id": platform_tenant_id,
                "name": "Nexus Platform",
                "domain": "nexus.local",
                "plan": "platform",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
            })

        # Create default admin user (if not already present)
        existing_admin = await _store.get_user_by_email(self.cfg.nexus_admin_email)
        if not existing_admin:
            await _store.save_user({
                "user_id": admin_id,
                "tenant_id": platform_tenant_id,
                "email": self.cfg.nexus_admin_email,
                "name": "Platform Admin",
                "role": "admin",
                "permissions": ["*"],
                "password_hash": hash_password(self.cfg.nexus_admin_password),
                "created_at": datetime.now(timezone.utc),
            })

        if _store.using_postgres:
            self.health.add_check("postgres", _store._db.health_check)

        # Initialize auth service for token creation
        self._auth_svc = init_auth(self.cfg.nexus_jwt_secret, self.cfg.nexus_jwt_expiry_hours)

    def register_routes(self, app):
        register_routes(app, _store, self)


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = AuthEngine()
    engine.run()


if __name__ == "__main__":
    main()
