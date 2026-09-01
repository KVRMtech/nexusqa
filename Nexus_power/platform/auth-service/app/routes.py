"""
Auth Service — Route Registration.

Registers all auth endpoints (login, tenants, users) on the FastAPI app.
"""
from __future__ import annotations

import uuid
import secrets
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

from .models import (
    CreateTenantRequest,
    TenantResponse,
    LoginRequest,
    LoginResponse,
    CreateUserRequest,
    UserResponse,
    RefreshRequest,
    RefreshResponse,
    SelfSignupRequest,
    SelfSignupResponse,
)
from .security import (
    hash_password,
    verify_password,
    role_permissions,
    check_brute_force,
    record_failed_attempt,
)
from .store import AuthStore

logger = logging.getLogger(__name__)


def register_routes(app: FastAPI, store: AuthStore, auth_engine) -> None:
    """Register all auth routes on the given FastAPI app."""

    # ── Login ─────────────────────────────────────────────

    @app.post("/api/v1/auth/login", response_model=LoginResponse)
    async def login(req: LoginRequest):
        """Authenticate and receive access + refresh token pair."""
        check_brute_force(req.email)

        user_data = await store.get_user_by_email(req.email)
        if not user_data:
            record_failed_attempt(req.email)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not verify_password(req.password, user_data["password_hash"]):
            record_failed_attempt(req.email)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        user = NexusUser(
            user_id=user_data["user_id"],
            tenant_id=user_data["tenant_id"],
            email=user_data["email"],
            role=user_data["role"],
            permissions=user_data.get("permissions", []),
            name=user_data.get("name"),
        )

        if auth_engine._auth_svc is None:
            raise HTTPException(status_code=500, detail="Auth service not initialized")

        # Issue access + refresh token pair
        token_pair = auth_engine._auth_svc.create_token_pair(user)
        await store.update_last_login(req.email)

        logger.info("auth.login_success", extra={"email": req.email, "user_id": user.user_id})

        return LoginResponse(
            access_token=token_pair.access_token,
            refresh_token=token_pair.refresh_token,
            expires_in=token_pair.expires_in,
            user={
                "user_id": user.user_id,
                "tenant_id": user.tenant_id,
                "email": user.email,
                "name": user.name,
                "role": user.role,
            },
        )

    # ── Token Refresh ─────────────────────────────────────

    @app.post("/api/v1/auth/refresh", response_model=RefreshResponse)
    async def refresh_token(req: RefreshRequest):
        """Exchange a valid refresh token for a new access + refresh token pair."""
        if auth_engine._auth_svc is None:
            raise HTTPException(status_code=500, detail="Auth service not initialized")

        # Validate the refresh token
        payload = auth_engine._auth_svc.validate_refresh_token(req.refresh_token)

        # Check if token has been revoked
        old_jti = payload.get("jti")
        if old_jti and await auth_engine._auth_svc.is_token_revoked(old_jti):
            raise HTTPException(status_code=401, detail="Refresh token has been revoked")

        # Look up current user data (permissions may have changed)
        user_data = await store.get_user_by_email(payload["email"])
        if not user_data:
            raise HTTPException(status_code=401, detail="User no longer exists")

        user = NexusUser(
            user_id=user_data["user_id"],
            tenant_id=user_data["tenant_id"],
            email=user_data["email"],
            role=user_data["role"],
            permissions=user_data.get("permissions", []),
            name=user_data.get("name"),
        )

        # Revoke old refresh token (rotation: a refresh token can only be used once)
        if old_jti:
            await auth_engine._auth_svc.revoke_token(old_jti)

        # Issue fresh token pair
        new_pair = auth_engine._auth_svc.create_token_pair(user)

        logger.info("auth.token_refreshed", extra={"email": user.email, "user_id": user.user_id})

        return RefreshResponse(
            access_token=new_pair.access_token,
            refresh_token=new_pair.refresh_token,
            expires_in=new_pair.expires_in,
        )

    # ── Token Revocation ──────────────────────────────────

    @app.post("/api/v1/auth/logout")
    async def logout(user: NexusUser = Depends(get_current_user)):
        """Revoke the current user's tokens (if Redis is available)."""
        # Note: Full implementation requires the JTI from the current token
        # This endpoint signals intent; client should discard tokens locally
        logger.info("auth.logout", extra={"email": user.email, "user_id": user.user_id})
        return {"status": "logged_out"}

    # ── Self-Signup (Public) ──────────────────────────────

    @app.post("/api/v1/auth/self-signup", response_model=SelfSignupResponse)
    async def self_signup(req: SelfSignupRequest):
        """
        Public self-signup: create a tenant and admin user in one step.

        No authentication required. This is the primary registration flow.
        Creates a new tenant, provisions an admin user, and returns an
        access + refresh token pair so the user is immediately logged in.
        """
        import os
        if os.getenv("NEXUS_DISABLE_SELF_SIGNUP", "false").lower() in ("true", "1"):
            raise HTTPException(status_code=403, detail="Self-signup is disabled")

        # Check if email already exists
        if await store.email_exists(req.email):
            raise HTTPException(status_code=409, detail="Email already registered")

        # Create tenant
        tenant_id = str(uuid.uuid4())[:8]
        domain = req.tenant_name.lower().replace(" ", "-").replace("/", "-")[:50]
        now = datetime.now(timezone.utc)

        tenant_data = {
            "tenant_id": tenant_id,
            "name": req.tenant_name,
            "domain": domain,
            "plan": req.plan,
            "status": "active",
            "created_at": now,
        }
        try:
            await store.save_tenant(tenant_data)
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Organization already registered")
            raise

        # Create admin user
        user_id = str(uuid.uuid4())
        await store.save_user({
            "user_id": user_id,
            "tenant_id": tenant_id,
            "email": req.email,
            "name": req.name,
            "role": "admin",
            "permissions": ["*"],
            "password_hash": hash_password(req.password),
            "created_at": now,
        })

        # Issue tokens so user is immediately logged in
        user = NexusUser(
            user_id=user_id,
            tenant_id=tenant_id,
            email=req.email,
            role="admin",
            permissions=["*"],
            name=req.name,
        )

        access_token = ""
        refresh_token = ""
        expires_in = 3600
        if auth_engine._auth_svc:
            token_pair = auth_engine._auth_svc.create_token_pair(user)
            access_token = token_pair.access_token
            refresh_token = token_pair.refresh_token
            expires_in = token_pair.expires_in

        # Publish signup event
        if auth_engine.event_bus:
            await auth_engine.event_bus.publish(NexusEvent(
                event_type="auth.self_signup",
                tenant_id=tenant_id,
                trace_id=str(uuid.uuid4()),
                engine="auth",
                data={
                    "tenant_id": tenant_id,
                    "tenant_name": req.tenant_name,
                    "admin_email": req.email,
                },
            ))

        logger.info(
            "auth.self_signup_success",
            extra={"email": req.email, "tenant_id": tenant_id, "user_id": user_id},
        )

        return SelfSignupResponse(
            tenant_id=tenant_id,
            user_id=user_id,
            email=req.email,
            name=req.name,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

    # ── Tenants ───────────────────────────────────────────

    @app.post("/api/v1/auth/tenants", response_model=TenantResponse)
    async def create_tenant(
        req: CreateTenantRequest,
        user: NexusUser = Depends(get_current_user),
    ):
        """Provision a new client tenant. Admin only."""
        user.require_permission("tenants.create")

        tenant_id = str(uuid.uuid4())[:8]
        tenant_data = {
            "tenant_id": tenant_id,
            "name": req.name,
            "domain": req.domain,
            "plan": req.plan,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await store.save_tenant(tenant_data)

        # Create admin user for this tenant
        admin_user_id = str(uuid.uuid4())
        temp_password = secrets.token_urlsafe(16)

        await store.save_user({
            "user_id": admin_user_id,
            "tenant_id": tenant_id,
            "email": req.admin_email,
            "name": req.admin_name,
            "role": "admin",
            "permissions": ["*"],
            "password_hash": hash_password(temp_password),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        # Publish tenant.created event
        if auth_engine.event_bus:
            await auth_engine.event_bus.publish(NexusEvent(
                event_type="auth.tenant.created",
                tenant_id=tenant_id,
                trace_id=str(uuid.uuid4()),
                engine="auth",
                data={
                    "tenant_id": tenant_id,
                    "name": req.name,
                    "admin_email": req.admin_email,
                },
            ))

        return TenantResponse(**tenant_data)

    @app.get("/api/v1/auth/tenants", response_model=list[TenantResponse])
    async def list_tenants(user: NexusUser = Depends(get_current_user)):
        """List all tenants. Platform admin sees all; tenant admin sees own."""
        if user.tenant_id == "nexus-platform":
            tenants = await store.list_tenants()
        else:
            tenants = await store.list_tenants(filter_tenant_id=user.tenant_id)
        return [TenantResponse(**t) for t in tenants]

    @app.get("/api/v1/auth/tenants/{tenant_id}", response_model=TenantResponse)
    async def get_tenant(tenant_id: str, user: NexusUser = Depends(get_current_user)):
        """Get tenant details."""
        if user.tenant_id != "nexus-platform" and user.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        tenant = await store.get_tenant(tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return TenantResponse(**tenant)

    # ── Users ─────────────────────────────────────────────

    @app.post("/api/v1/auth/users", response_model=UserResponse)
    async def create_user(
        req: CreateUserRequest,
        user: NexusUser = Depends(get_current_user),
    ):
        """Create a user within the current tenant. Admin/manager only."""
        user.require_permission("users.create")

        if await store.email_exists(req.email):
            raise HTTPException(status_code=409, detail="Email already registered")

        user_id = str(uuid.uuid4())
        now_str = datetime.now(timezone.utc).isoformat()

        await store.save_user({
            "user_id": user_id,
            "tenant_id": user.tenant_id,
            "email": req.email,
            "name": req.name,
            "role": req.role,
            "permissions": role_permissions(req.role),
            "password_hash": hash_password(req.password),
            "created_at": now_str,
        })

        return UserResponse(
            user_id=user_id,
            tenant_id=user.tenant_id,
            email=req.email,
            name=req.name,
            role=req.role,
            created_at=now_str,
        )

    @app.get("/api/v1/auth/users/me")
    async def get_me(user: NexusUser = Depends(get_current_user)):
        """Get the current authenticated user."""
        return {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "permissions": user.permissions,
        }
