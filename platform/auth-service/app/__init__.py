"""
Auth Service — App Sub-packages.

Modular components for the Nexus Auth Engine:
  - config    : AuthConfig
  - store     : AuthStore (PostgreSQL + in-memory fallback)
  - models    : Request / Response Pydantic models
  - security  : Password hashing, brute-force protection, role→permissions
  - routes    : Route registration
"""

from .config import AuthConfig
from .store import AuthStore
from .models import (
    CreateTenantRequest,
    TenantResponse,
    LoginRequest,
    LoginResponse,
    CreateUserRequest,
    UserResponse,
)
from .security import (
    hash_password,
    verify_password,
    role_permissions,
    check_brute_force,
    record_failed_attempt,
    is_insecure,
)
from .routes import register_routes

__all__ = [
    "AuthConfig",
    "AuthStore",
    "CreateTenantRequest",
    "TenantResponse",
    "LoginRequest",
    "LoginResponse",
    "CreateUserRequest",
    "UserResponse",
    "hash_password",
    "verify_password",
    "role_permissions",
    "check_brute_force",
    "record_failed_attempt",
    "is_insecure",
    "register_routes",
]
