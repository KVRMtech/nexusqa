"""QE-Central Phase-7 — the PLATFORM SUPER-ADMIN scope + principal minting.

Provisioning a CLIENT TENANT is a FLEET-OPERATOR action, not a tenant action: a
tenant's own admin must NOT be able to onboard, suspend, or offboard OTHER
tenants.  The existing RBAC (:func:`app.auth.require_role`) gates on ROLE only —
a tenant admin has ``role='admin'`` — so it cannot draw that line.

This module adds the missing scope with ONE additional marker: a token is a
PLATFORM SUPER-ADMIN iff it has ``role='admin'`` AND a truthy ``platform_admin``
claim (:data:`PLATFORM_ADMIN_CLAIM`).  :func:`require_platform_admin` enforces
both.  A tenant's first-admin token (minted by onboarding) carries ``role='admin'``
but NEVER the ``platform_admin`` marker, so it is a full admin of its OWN tenant
and powerless over the fleet.

Security posture:
  * :func:`require_platform_admin` reuses the hardened :func:`app.auth.require_auth`
    for ALL security-critical validation (signature, expiry, audience, mandatory
    tenant) — it does not re-implement it — then reads the ``platform_admin``
    marker from the (already-accepted) token.  It NEVER modifies ``require_auth``'s
    public contract.
  * A super-admin token rides ONLY the Bearer header on the state-changing fleet
    routes; ``app.auth._token_from_request`` rejects a ``?token=`` query on any
    non-GET, so an operator token can never be replayed from a URL.

ZERO LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from ..auth import _bearer, _token_from_request, require_auth
from ..config import settings

logger = logging.getLogger(__name__)

#: The JWT claim marking a PLATFORM SUPER-ADMIN (fleet operator).  Present + truthy
#: ONLY on operator tokens; a tenant's own admin token never carries it.
PLATFORM_ADMIN_CLAIM = "platform_admin"

#: The role a full admin (tenant admin OR platform operator) carries.
ROLE_ADMIN = "admin"


def _truthy(value: Any) -> bool:
    """Strict truthiness for the ``platform_admin`` marker (guards a ``"false"`` string)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def is_platform_admin(claims: dict) -> bool:
    """True when ``claims`` names a PLATFORM SUPER-ADMIN (role admin + marker)."""
    return str(claims.get("role") or "") == ROLE_ADMIN and _truthy(claims.get(PLATFORM_ADMIN_CLAIM))


def _read_claims(token: str) -> dict:
    """Signature-verified read of ALL claims (audience already enforced upstream).

    :func:`require_platform_admin` calls this only AFTER :func:`require_auth` has
    fully accepted the token (signature, expiry, audience, tenant), so this
    re-verifies the signature + expiry cheaply and reads every claim WITHOUT the
    audience gate (which already passed) — it never widens what was accepted.
    """
    try:
        return pyjwt.decode(
            token, settings.nexus_jwt_secret,
            algorithms=[settings.jwt_algorithm], options={"verify_aud": False},
        )
    except Exception:
        # Unreachable in practice (require_auth accepted the same token first);
        # fail-closed regardless.
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def require_platform_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """FastAPI dependency: require a PLATFORM SUPER-ADMIN (fleet operator).

    Reuses :func:`app.auth.require_auth` for full token validation, then requires
    ``role='admin'`` AND a truthy ``platform_admin`` claim.  A tenant-scoped admin
    (no marker) is rejected 403 — it cannot provision/suspend/offboard tenants.
    Attaches ``platform_admin=True`` to the returned context (a SUPERSET of the
    require_auth context; the base ``{sub,tenant_id,email,role}`` keys are intact).
    """
    user = await require_auth(request, credentials)
    if str(user.get("role") or "") != ROLE_ADMIN:
        logger.warning(
            "qec.fleet.rbac.denied_not_admin",
            extra={"sub": user.get("sub", ""), "role": user.get("role", "")},
        )
        raise HTTPException(
            status_code=403, detail="platform super-admin required (role admin + platform marker)",
        )
    token = credentials.credentials if credentials is not None else _token_from_request(request)
    claims = _read_claims(token)
    if not _truthy(claims.get(PLATFORM_ADMIN_CLAIM)):
        logger.warning(
            "qec.fleet.rbac.denied_not_platform_admin",
            extra={"sub": user.get("sub", ""), "tenant_id": user.get("tenant_id", "")},
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "platform super-admin required — a tenant admin cannot manage "
                "other tenants (missing platform_admin scope)"
            ),
        )
    return {**user, PLATFORM_ADMIN_CLAIM: True}


def _mint_jwt(
    *, tenant_id: str, email: str, role: str, ttl_seconds: int,
    audience: str | None, platform_admin: bool,
) -> tuple[str, datetime]:
    """Mint an HS256 principal JWT; return ``(token, expires_at)``.

    Raises:
        ValueError: on a missing/empty ``tenant_id`` (RLS everywhere).
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to mint a principal token")
    # M0.5 T-SEC-01 — a tenant's BOOTSTRAP admin credential is the highest-value
    # token this service issues. Never mint one under an unconfigured or known
    # development secret: it would be forgeable by anyone holding this repo.
    from ..config import jwt_secret_usable

    usable, reason = jwt_secret_usable(settings.nexus_jwt_secret, settings.nexus_env)
    if not usable:
        raise ValueError(
            f"refusing to mint a principal token: {reason} "
            "(configure NEXUS_JWT_SECRET with a strong random value)"
        )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
    claims: dict = {
        "sub": f"principal:{email or tid}",
        "email": str(email or ""),
        "role": str(role or "viewer"),
        "tenant_id": tid,
        "iat": now,
        "exp": expires_at,
    }
    aud = (audience or "").strip()
    if aud:
        claims["aud"] = aud
    if platform_admin:
        claims[PLATFORM_ADMIN_CLAIM] = True
    token = pyjwt.encode(claims, settings.nexus_jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def mint_tenant_principal_jwt(
    tenant_id: str, email: str, *, role: str = ROLE_ADMIN, ttl_seconds: int | None = None,
) -> tuple[str, datetime]:
    """Mint a TENANT principal token (the onboarding first-admin credential).

    Audience-bound to ``settings.qec_jwt_audience`` (Verdict), ``role='admin'`` by
    default, and NEVER carries the ``platform_admin`` marker — so the client's
    first admin governs its OWN tenant but has no fleet authority.  TTL defaults to
    ``QEC_ONBOARDING_TOKEN_TTL_SECONDS``.  Returns ``(token, expires_at)``.
    """
    ttl = settings.qec_onboarding_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    return _mint_jwt(
        tenant_id=tenant_id, email=email, role=role, ttl_seconds=ttl,
        audience=settings.qec_jwt_audience, platform_admin=False,
    )


def mint_platform_admin_jwt(
    email: str, *, tenant_id: str | None = None, ttl_seconds: int | None = None,
) -> tuple[str, datetime]:
    """Mint a PLATFORM SUPER-ADMIN (operator) token for the fleet routes.

    ``role='admin'`` + ``platform_admin=True`` + the Verdict audience.  Carries a
    reserved tenant scope (``QEC_PLATFORM_TENANT_ID``) so it satisfies the
    mandatory-tenant JWT gate while operating cross-tenant.  Returns
    ``(token, expires_at)``.
    """
    ttl = settings.qec_onboarding_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    return _mint_jwt(
        tenant_id=(tenant_id or settings.qec_platform_tenant_id),
        email=email, role=ROLE_ADMIN, ttl_seconds=ttl,
        audience=settings.qec_jwt_audience, platform_admin=True,
    )


__all__ = [
    "PLATFORM_ADMIN_CLAIM",
    "ROLE_ADMIN",
    "is_platform_admin",
    "require_platform_admin",
    "mint_tenant_principal_jwt",
    "mint_platform_admin_jwt",
]
