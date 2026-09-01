"""M0.5 security-suite helpers — the attacker's toolkit.

Deliberately gives a test FULL control over every claim, header and signature
field, so an attack is written the way an attacker would perform it rather than
the way the product would.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

#: A strong, explicitly-configured secret — what a correctly-deployed fleet has.
STRONG_SECRET = "M05-strong-random-jwt-secret-0f3a91c7d24be586"
#: The exact value that shipped in docker-compose.qec.yml before M0.5.
SHIPPED_DEV_SECRET = "test-secret-do-not-use-in-production"
#: The exact fleet token that shipped in docker-compose.qec.yml before M0.5.
SHIPPED_DEV_FLEET_TOKEN = "dev-explorer-token-change-me"


def forge_token(
    *,
    secret: str,
    role: str = "admin",
    tenant_id: str | None = "tenant-attacker",
    sub: str = "mallory",
    aud: str | None = None,
    exp_delta: timedelta = timedelta(minutes=30),
) -> str:
    """Mint a token an ATTACKER would mint, with full control of every claim."""
    claims: dict = {
        "sub": sub,
        "email": "mallory@evil.example",
        "role": role,
        "exp": datetime.now(timezone.utc) + exp_delta,
    }
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if aud is not None:
        claims["aud"] = aud
    return pyjwt.encode(claims, secret, algorithm="HS256")


def canonical_body(**fields) -> bytes:
    """Serialise a callback body exactly as the explorer does (sorted, compact)."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
