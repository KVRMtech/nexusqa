"""
Auth Service — Password & Security Helpers.

Bcrypt hashing, verification, role→permission mapping,
and brute-force rate limiting.
"""
from __future__ import annotations

import time
import logging

import bcrypt
from fastapi import HTTPException

logger = logging.getLogger(__name__)


# ─── Password Hashing ─────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash password using bcrypt (industry standard, GPU-resistant)."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, stored: str) -> bool:
    """Verify password against bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except Exception:
        return False


# ─── Role → Permissions ───────────────────────────────────────

def role_permissions(role: str) -> list[str]:
    """Default permissions per role."""
    perms = {
        "admin": ["*"],
        "manager": [
            "sessions.read", "sessions.create",
            "knowledge.read", "knowledge.write",
            "tests.read", "tests.create", "tests.execute",
            "users.read",
        ],
        "viewer": [
            "sessions.read",
            "knowledge.read",
            "tests.read",
        ],
        "api": [
            "sessions.create",
            "tests.execute",
        ],
    }
    return perms.get(role, [])


# ─── Brute-force Protection ───────────────────────────────────

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes

_login_attempts: dict[str, list[float]] = {}


def check_brute_force(email: str) -> None:
    """Block login after too many failed attempts within the window."""
    now = time.monotonic()
    attempts = _login_attempts.get(email, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[email] = attempts
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        logger.warning("auth.brute_force_blocked", extra={"email": email, "attempts": len(attempts)})
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {LOGIN_WINDOW_SECONDS // 60} minutes.",
        )


def record_failed_attempt(email: str) -> None:
    """Record a failed login attempt."""
    if email not in _login_attempts:
        _login_attempts[email] = []
    _login_attempts[email].append(time.monotonic())


# ─── Insecure Secret Detection ────────────────────────────────

_INSECURE_PATTERNS = [
    "change-me", "change-this-password", "admin123", "password",
    "nexus-secret", "dev-secret", "dev-jwt-secret", "secret123",
]


def is_insecure(value: str) -> bool:
    """Check whether a secret value matches known-insecure patterns."""
    low = value.lower()
    return any(p in low for p in _INSECURE_PATTERNS) or len(value) < 16
