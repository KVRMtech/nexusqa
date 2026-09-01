"""
Auth Service — Request / Response Models.

Pydantic models for all auth endpoint payloads.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateTenantRequest(BaseModel):
    name: str = Field(..., description="Company name", min_length=2, max_length=200)
    domain: str = Field(..., description="Company domain", min_length=3)
    admin_email: str = Field(..., description="Admin user email")
    admin_name: str = Field(default="Admin", description="Admin display name")
    plan: str = Field(default="starter", description="Plan: starter, growth, enterprise")


class TenantResponse(BaseModel):
    tenant_id: str
    name: str
    domain: str
    plan: str
    created_at: str
    status: str


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    expires_in: int = 3600
    user: dict


class RefreshRequest(BaseModel):
    """Request body for token refresh."""
    refresh_token: str = Field(..., description="Valid refresh token")


class RefreshResponse(BaseModel):
    """Response with new token pair."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 3600


class CreateUserRequest(BaseModel):
    email: str = Field(..., max_length=255)
    name: str = Field(..., max_length=200)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(default="viewer", description="admin, manager, viewer, api")


class UserResponse(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    name: str
    role: str
    created_at: str


class SelfSignupRequest(BaseModel):
    """Public self-signup: creates a tenant and admin user in one step."""
    tenant_name: str = Field(..., description="Company name", min_length=2, max_length=200)
    email: str = Field(..., description="Admin user email", max_length=255)
    password: str = Field(..., description="Admin user password", min_length=8, max_length=128)
    name: str = Field(default="Admin", description="Admin display name", max_length=200)
    plan: str = Field(default="starter", description="Plan: starter, growth, enterprise")


class SelfSignupResponse(BaseModel):
    """Response for successful self-signup."""
    tenant_id: str
    user_id: str
    email: str
    name: str
    access_token: str
    refresh_token: str = ""
    expires_in: int = 3600
