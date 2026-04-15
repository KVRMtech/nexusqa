"""
Nexus Authentication — JWT-based auth for all engines.

Every API request must include a JWT token with:
- tenant_id: Which client is making the request
- user_id: Which user 
- role: admin, manager, viewer, api
- permissions: List of allowed actions

On-prem, the auth service issues tokens.
Each engine validates tokens using the shared JWT secret.

v2.0 — Refresh token support:
- Short-lived access tokens (configurable, default 1 hour)
- Long-lived refresh tokens (configurable, default 30 days)
- JTI (JWT ID) claim for token revocation
- Redis-backed token blacklist for compromised token invalidation
"""

from __future__ import annotations

import uuid
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

__all__ = [
    "NexusUser",
    "AuthService",
    "init_auth",
    "get_auth_service",
    "get_current_user",
    "security_scheme",
    "TokenPair",
]

security_scheme = HTTPBearer()


@dataclass
class NexusUser:
    """Authenticated user context available in every request."""
    
    user_id: str
    tenant_id: str
    email: str
    role: str  # admin, manager, viewer, api
    permissions: list[str]
    name: Optional[str] = None

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission."""
        if self.role == "admin":
            return True  # Admin has all permissions
        return permission in self.permissions

    def require_permission(self, permission: str) -> None:
        """Raise 403 if user lacks the permission."""
        if not self.has_permission(permission):
            raise HTTPException(
                status_code=403, 
                detail=f"Permission required: {permission}"
            )


@dataclass
class TokenPair:
    """Access + refresh token pair returned on login and token refresh."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 3600  # seconds until access token expires


class AuthService:
    """JWT token creation, validation, and refresh token lifecycle."""

    def __init__(
        self,
        jwt_secret: str,
        jwt_expiry_hours: int = 1,
        refresh_expiry_days: int = 30,
        redis_client=None,
    ):
        self._secret = jwt_secret
        self._expiry_hours = jwt_expiry_hours
        self._refresh_expiry_days = refresh_expiry_days
        self._algorithm = "HS256"
        self._redis = redis_client  # Optional: for token blacklist

    def create_token(self, user: NexusUser) -> str:
        """Create an access JWT token for a user (backward-compatible)."""
        jti = str(uuid.uuid4())
        payload = {
            "sub": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "role": user.role,
            "permissions": user.permissions,
            "name": user.name or "",
            "jti": jti,
            "type": "access",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=self._expiry_hours),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_token_pair(self, user: NexusUser) -> TokenPair:
        """Create an access + refresh token pair."""
        access_token = self.create_token(user)

        # Refresh token has longer expiry and type=refresh
        refresh_jti = str(uuid.uuid4())
        refresh_payload = {
            "sub": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "role": user.role,
            "jti": refresh_jti,
            "type": "refresh",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(days=self._refresh_expiry_days),
        }
        refresh_token = jwt.encode(refresh_payload, self._secret, algorithm=self._algorithm)

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._expiry_hours * 3600,
        )

    def validate_token(self, token: str) -> NexusUser:
        """Validate an access JWT token and return the user context."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])

            # Check token type (if present — backward compat with old tokens)
            token_type = payload.get("type", "access")
            if token_type != "access":
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token type. Use an access token.",
                )

            return NexusUser(
                user_id=payload["sub"],
                tenant_id=payload["tenant_id"],
                email=payload["email"],
                role=payload["role"],
                permissions=payload.get("permissions", []),
                name=payload.get("name"),
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    def validate_refresh_token(self, token: str) -> dict:
        """
        Validate a refresh token and return the payload.

        Returns the raw payload dict (not NexusUser) because refresh tokens
        don't carry permissions — the caller should look up the user.
        """
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])

            if payload.get("type") != "refresh":
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token type. Expected refresh token.",
                )

            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Refresh token expired. Please login again.")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

    async def revoke_token(self, jti: str, ttl_seconds: int = 86400 * 31) -> bool:
        """
        Add a JTI to the revocation blacklist.

        Uses Redis SET with TTL to auto-expire entries when the token would
        have expired anyway. Returns True if Redis was available.
        """
        if self._redis:
            try:
                await self._redis.setex(f"revoked:{jti}", ttl_seconds, "1")
                return True
            except Exception:
                return False
        return False

    async def is_token_revoked(self, jti: str) -> bool:
        """Check if a token's JTI has been revoked."""
        if self._redis:
            try:
                return await self._redis.exists(f"revoked:{jti}") > 0
            except Exception:
                return False  # Fail open — don't block if Redis is down
        return False


# ─── FastAPI Dependency ───────────────────────────────────────────

_auth_service: Optional[AuthService] = None


def init_auth(
    jwt_secret: str,
    jwt_expiry_hours: int = 1,
    refresh_expiry_days: int = 30,
    redis_client=None,
) -> AuthService:
    """Initialize the auth service (call once at engine startup)."""
    global _auth_service
    _auth_service = AuthService(
        jwt_secret,
        jwt_expiry_hours,
        refresh_expiry_days,
        redis_client,
    )
    return _auth_service


def get_auth_service() -> AuthService:
    """Get the initialized auth service."""
    if _auth_service is None:
        raise RuntimeError("Auth not initialized. Call init_auth() first.")
    return _auth_service


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme),
) -> NexusUser:
    """
    FastAPI dependency to extract and validate the current user from JWT.
    
    Usage in any engine endpoint:
        @app.get("/my-endpoint")
        async def my_endpoint(user: NexusUser = Depends(get_current_user)):
            print(user.tenant_id, user.role)
    """
    auth = get_auth_service()
    return auth.validate_token(credentials.credentials)
