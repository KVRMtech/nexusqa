"""
Tests for nexus_sdk.auth — JWT authentication with refresh tokens.

Verifies:
- Access token creation and validation
- Refresh token creation and validation
- Token pair creation
- JTI (JWT ID) is included for revocation
- Token type enforcement (access vs refresh)
- Expired token handling
- Backward compatibility (old tokens without type still work)
- init_auth and get_auth_service lifecycle
"""
import time
from datetime import datetime, timedelta, timezone

import pytest
import jwt

from nexus_sdk.auth import (
    AuthService,
    NexusUser,
    TokenPair,
    init_auth,
    get_auth_service,
    get_current_user,
)


@pytest.fixture
def user():
    """Sample user for token tests."""
    return NexusUser(
        user_id="user-123",
        tenant_id="tenant-abc",
        email="test@example.com",
        role="admin",
        permissions=["*"],
        name="Test User",
    )


@pytest.fixture
def auth_service():
    """AuthService instance with short expiry for testing."""
    return AuthService(
        jwt_secret="test-secret-key-for-testing-only",
        jwt_expiry_hours=1,
        refresh_expiry_days=30,
    )


class TestAccessToken:
    """Tests for access token creation and validation."""

    def test_create_token(self, auth_service, user):
        """Create a valid access token."""
        token = auth_service.create_token(user)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_validate_token(self, auth_service, user):
        """Validate a token and recover user data."""
        token = auth_service.create_token(user)
        validated = auth_service.validate_token(token)
        assert validated.user_id == user.user_id
        assert validated.tenant_id == user.tenant_id
        assert validated.email == user.email
        assert validated.role == user.role

    def test_token_has_jti(self, auth_service, user):
        """Access tokens include a JTI for revocation."""
        token = auth_service.create_token(user)
        payload = jwt.decode(token, "test-secret-key-for-testing-only", algorithms=["HS256"])
        assert "jti" in payload
        assert len(payload["jti"]) > 0

    def test_token_has_type(self, auth_service, user):
        """Access tokens are marked with type=access."""
        token = auth_service.create_token(user)
        payload = jwt.decode(token, "test-secret-key-for-testing-only", algorithms=["HS256"])
        assert payload["type"] == "access"

    def test_expired_token_rejected(self, user):
        """Expired tokens raise 401."""
        auth = AuthService(
            jwt_secret="test-secret",
            jwt_expiry_hours=0,  # Will effectively be expired immediately
        )
        # Manually create an expired token
        payload = {
            "sub": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "role": user.role,
            "permissions": user.permissions,
            "type": "access",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        token = jwt.encode(payload, "test-secret", algorithm="HS256")
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth.validate_token(token)
        assert exc_info.value.status_code == 401

    def test_invalid_token_rejected(self, auth_service):
        """Invalid tokens raise 401."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth_service.validate_token("invalid-token-string")
        assert exc_info.value.status_code == 401

    def test_refresh_token_rejected_as_access(self, auth_service, user):
        """Refresh tokens cannot be used as access tokens."""
        pair = auth_service.create_token_pair(user)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth_service.validate_token(pair.refresh_token)
        assert exc_info.value.status_code == 401
        assert "access token" in exc_info.value.detail.lower()


class TestRefreshToken:
    """Tests for refresh token creation and validation."""

    def test_create_token_pair(self, auth_service, user):
        """Token pair contains both access and refresh tokens."""
        pair = auth_service.create_token_pair(user)
        assert isinstance(pair, TokenPair)
        assert len(pair.access_token) > 0
        assert len(pair.refresh_token) > 0
        assert pair.token_type == "bearer"
        assert pair.expires_in == 3600

    def test_refresh_token_has_type(self, auth_service, user):
        """Refresh tokens are marked with type=refresh."""
        pair = auth_service.create_token_pair(user)
        payload = jwt.decode(
            pair.refresh_token, "test-secret-key-for-testing-only", algorithms=["HS256"]
        )
        assert payload["type"] == "refresh"

    def test_refresh_token_has_jti(self, auth_service, user):
        """Refresh tokens include a JTI."""
        pair = auth_service.create_token_pair(user)
        payload = jwt.decode(
            pair.refresh_token, "test-secret-key-for-testing-only", algorithms=["HS256"]
        )
        assert "jti" in payload

    def test_validate_refresh_token(self, auth_service, user):
        """Valid refresh tokens can be validated."""
        pair = auth_service.create_token_pair(user)
        payload = auth_service.validate_refresh_token(pair.refresh_token)
        assert payload["sub"] == user.user_id
        assert payload["tenant_id"] == user.tenant_id
        assert payload["type"] == "refresh"

    def test_access_token_rejected_as_refresh(self, auth_service, user):
        """Access tokens cannot be used as refresh tokens."""
        pair = auth_service.create_token_pair(user)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth_service.validate_refresh_token(pair.access_token)
        assert exc_info.value.status_code == 401

    def test_refresh_tokens_are_unique(self, auth_service, user):
        """Each token pair has unique JTIs."""
        pair1 = auth_service.create_token_pair(user)
        pair2 = auth_service.create_token_pair(user)
        assert pair1.refresh_token != pair2.refresh_token


class TestBackwardCompatibility:
    """Tests for backward compatibility with pre-refresh-token JWTs."""

    def test_old_token_without_type(self, auth_service, user):
        """Tokens without 'type' field are treated as access tokens."""
        # Create a "legacy" token without the type field
        payload = {
            "sub": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "role": user.role,
            "permissions": user.permissions,
            "name": user.name,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        token = jwt.encode(payload, "test-secret-key-for-testing-only", algorithm="HS256")
        validated = auth_service.validate_token(token)
        assert validated.user_id == user.user_id


class TestNexusUser:
    """Tests for NexusUser permission model."""

    def test_admin_has_all_permissions(self):
        """Admin role has all permissions."""
        user = NexusUser(
            user_id="u1", tenant_id="t1", email="a@b.com",
            role="admin", permissions=[],
        )
        assert user.has_permission("anything")

    def test_viewer_lacks_write(self):
        """Viewer role doesn't have write permission."""
        user = NexusUser(
            user_id="u1", tenant_id="t1", email="a@b.com",
            role="viewer", permissions=["sessions.read"],
        )
        assert user.has_permission("sessions.read")
        assert not user.has_permission("sessions.create")

    def test_require_permission_raises(self):
        """require_permission raises 403 when permission is missing."""
        user = NexusUser(
            user_id="u1", tenant_id="t1", email="a@b.com",
            role="viewer", permissions=[],
        )
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            user.require_permission("admin.delete")
        assert exc_info.value.status_code == 403


class TestInitAuth:
    """Tests for auth service initialization."""

    def test_init_and_get(self):
        """init_auth creates service, get_auth_service retrieves it."""
        svc = init_auth("test-secret", jwt_expiry_hours=2, refresh_expiry_days=7)
        assert svc is not None
        assert get_auth_service() is svc

    def test_init_with_custom_expiry(self, user):
        """Custom expiry settings are respected."""
        svc = init_auth("test-secret", jwt_expiry_hours=2, refresh_expiry_days=7)
        pair = svc.create_token_pair(user)
        assert pair.expires_in == 7200  # 2 hours in seconds
