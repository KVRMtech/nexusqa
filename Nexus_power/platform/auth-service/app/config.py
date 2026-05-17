"""
Auth Service — Configuration.

Centralises all environment-driven settings for the auth engine.
"""
from __future__ import annotations

from pydantic import Field
from nexus_sdk import EngineConfig


class AuthConfig(EngineConfig):
    """Auth service configuration loaded from environment."""

    engine_name: str = "auth"
    engine_port: int = 8000
    nexus_admin_email: str = Field(default="admin@nexus.local")
    nexus_admin_password: str = Field(default="change-this-password")
