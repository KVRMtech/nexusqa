"""QE-Central — service JWT minting (R-13).

QE-Central consumes the UNCHANGED VKPower factory over HTTP so every
interaction rides the same audited path a human uses.  The minted token
carries ``role='manager'`` — passes the admin|manager RBAC gate
(test_factory.py:114-125) with least privilege — and a distinguishable
service identity (``sub='svc-qe-central'``, ``email='qe-central@service'``)
so service mutations are separable from human admins in ``audit_log``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from .config import settings

SERVICE_SUBJECT = "svc-qe-central"
SERVICE_EMAIL = "qe-central@service"
SERVICE_ROLE = "manager"


def mint_service_jwt(tenant_id: str) -> str:
    """Mint a short-lived HS256 service JWT for ``tenant_id``.

    Claims (pinned by the Phase-0 shared conventions):
      ``{sub:'svc-qe-central', email:'qe-central@service', role:'manager',
         tenant_id, iat, exp}``

    Raises ``ValueError`` on a missing/empty tenant_id — a service token
    without tenant scope has no honest meaning (RLS everywhere).
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to mint a service token")

    now = datetime.now(timezone.utc)
    claims = {
        "sub": SERVICE_SUBJECT,
        "email": SERVICE_EMAIL,
        "role": SERVICE_ROLE,
        "tenant_id": tid,
        "iat": now,
        "exp": now + timedelta(seconds=settings.service_token_ttl_seconds),
    }
    return pyjwt.encode(
        claims, settings.nexus_jwt_secret, algorithm=settings.jwt_algorithm,
    )
