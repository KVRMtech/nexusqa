"""QE-Central — service JWT minting (R-13).

QE-Central consumes the UNCHANGED VKPower factory over HTTP so every
interaction rides the same audited path a human uses.  The minted token
carries ``role='manager'`` — passes the admin|manager RBAC gate
(test_factory.py:114-125) with least privilege — and a distinguishable
service identity (``sub='svc-qe-central'``, ``email='qe-central@service'``)
so service mutations are separable from human admins in ``audit_log``.

Phase-6 audience note (no VKPower<->Verdict token bleed): ``mint_service_jwt``
grows an OPT-IN ``audience`` parameter that stamps the JWT ``aud`` claim
(:data:`app.config.AUDIENCE` / ``QEC_JWT_AUDIENCE``).  It defaults to ``None``
(NO ``aud`` claim) ON PURPOSE — every current call site relays this token to the
UNCHANGED VKPower factory over HTTP, and VKPower's ``pyjwt.decode`` is called
WITHOUT an ``audience`` argument, so (PyJWT 2.x) it REJECTS any token that
carries an ``aud`` it was not told to expect.  Stamping ``aud`` by default would
therefore 401 every qe-central→VKPower call (a silent regression against a seam
we are forbidden to modify).  A Verdict-internal caller that wants an audience-
bound token passes ``audience=settings.qec_jwt_audience`` explicitly; the verify
side (:func:`app.auth._decode_token`) enforces the real property — a foreign
``aud`` is ALWAYS rejected, a missing ``aud`` is transition-accepted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from .config import jwt_secret_usable, settings

SERVICE_SUBJECT = "svc-qe-central"
SERVICE_EMAIL = "qe-central@service"
SERVICE_ROLE = "manager"


def mint_service_jwt(tenant_id: str, *, audience: str | None = None) -> str:
    """Mint a short-lived HS256 service JWT for ``tenant_id``.

    Claims (pinned by the Phase-0 shared conventions):
      ``{sub:'svc-qe-central', email:'qe-central@service', role:'manager',
         tenant_id, iat, exp}`` — plus ``aud`` ONLY when ``audience`` is given.

    Args:
        tenant_id: the RLS tenant scope (required — a service token without
            tenant scope has no honest meaning).
        audience: OPT-IN JWT ``aud`` claim.  ``None``/empty (the default) ⇒ NO
            ``aud`` is stamped, so the token stays acceptable to the UNCHANGED
            VKPower factory (see the module docstring).  Pass
            ``settings.qec_jwt_audience`` for a Verdict-audience-bound token.

    Raises:
        ValueError: on a missing/empty ``tenant_id`` (RLS everywhere).
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to mint a service token")

    # M0.5 T-SEC-01 — never MINT under a secret that could not VERIFY.  Minting
    # with an unconfigured or known-development secret would hand out a
    # credential this service will refuse and platform-api might not, which is
    # the worst of both: broken here, forged-signable there.
    usable, reason = jwt_secret_usable(settings.nexus_jwt_secret, settings.nexus_env)
    if not usable:
        raise ValueError(
            f"refusing to mint a service token: {reason} "
            "(configure NEXUS_JWT_SECRET with a strong random value)"
        )

    now = datetime.now(timezone.utc)
    claims = {
        "sub": SERVICE_SUBJECT,
        "email": SERVICE_EMAIL,
        "role": SERVICE_ROLE,
        "tenant_id": tid,
        "iat": now,
        "exp": now + timedelta(seconds=settings.service_token_ttl_seconds),
    }
    aud = (audience or "").strip()
    if aud:
        claims["aud"] = aud
    return pyjwt.encode(
        claims, settings.nexus_jwt_secret, algorithm=settings.jwt_algorithm,
    )
