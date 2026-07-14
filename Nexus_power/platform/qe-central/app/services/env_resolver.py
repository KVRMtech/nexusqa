"""Resolve an Environment Profile → a run-time ``env_context`` (multi-env).

qe-central OWNS ``app_environments`` (crawl-once/run-many); the runner (platform-api)
owns NO env data — a bounded-context boundary. So qe-central RESOLVES a profile here
(decrypting per-env creds, resolving effective fences over the app + env) into a plain
``env_context`` dict that a crawl/run request carries; the runner only APPLIES it.

Secret material (login creds, HTTP basic-auth, secret cookies/headers) is sealed in
the env row's ``creds_blob`` (AAD=environment_id) and decrypted ONLY here, in the
tenant-scoped session — never echoed, never stored back in plaintext.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import or_, select

from ..db.models import ClientAppEnvironmentRow, ClientAppRow
from ..security.prod_guard import resolve_effective_fences

logger = logging.getLogger(__name__)


async def _decrypt_env_creds(envelope, tenant_id: str, env_id: str, creds_blob) -> dict:
    """Open the env's sealed creds dict (AAD=environment_id), or {} when absent /
    undecryptable (never raises into the caller)."""
    if not creds_blob or envelope is None:
        return {}
    try:
        from nexus_sdk.security.envelope import EnvelopeBlob

        pt = await envelope.decrypt(
            tenant_id, EnvelopeBlob.from_bytes(bytes(creds_blob)),
            expected_aad=env_id.encode("utf-8"),
        )
        d = json.loads(pt.decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception as exc:
        logger.warning("qec.env_resolver.decrypt_failed",
                       extra={"environment_id": env_id, "error": str(exc)[:200]})
        return {}


async def resolve_env_context(
    session, *, tenant_id: str, app_id: str, env: str, envelope: Any = None,
) -> dict | None:
    """Resolve one Environment Profile (by ``environment_id`` OR ``name``) into an
    ``env_context``, or ``None`` when the env is absent.

    ``env_context`` = ``{environment_id, name, base_url, cookies, headers,
    http_credentials, data_overrides, env_assertion, fences, env_kind}`` — the exact
    shape the runner's ``_configured_files(env_context=…)`` applies.  Non-secret
    routing cookies/headers come from the row's columns; secret ones + basic-auth +
    login creds are decrypted from ``creds_blob`` and merged.  Effective fences are
    resolved over the APP fences + this env (prod ⇒ allow_submit forced off).
    """
    row = (await session.execute(
        select(ClientAppEnvironmentRow).where(
            ClientAppEnvironmentRow.app_id == app_id,
            ClientAppEnvironmentRow.tenant_id == tenant_id,
            or_(
                ClientAppEnvironmentRow.environment_id == env,
                ClientAppEnvironmentRow.name == env,
            ),
        ).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None

    app = (await session.execute(
        select(ClientAppRow).where(
            ClientAppRow.app_id == app_id, ClientAppRow.tenant_id == tenant_id,
        ).limit(1)
    )).scalar_one_or_none()

    secrets = await _decrypt_env_creds(envelope, tenant_id, row.environment_id, row.creds_blob)
    cookies = list(row.cookies or []) + list(secrets.get("cookies") or [])
    headers = {**dict(row.headers or {}), **dict(secrets.get("headers") or {})}
    http_credentials = secrets.get("basic_auth") or secrets.get("http_credentials") or None
    env_kind = str((row.env_attestation or {}).get("env_kind") or "").strip().lower()
    fences = resolve_effective_fences(
        dict((app.fences if app else None) or {}), dict(row.fences or {}), env_kind=env_kind)

    return {
        "environment_id": row.environment_id,
        "name": row.name,
        "base_url": (row.base_url or "").strip() or (app.base_url if app else ""),
        "cookies": cookies,
        "headers": headers,
        "http_credentials": http_credentials,
        "data_overrides": dict(row.data_overrides or {}),
        "env_assertion": dict(row.env_assertion or {}),
        "fences": fences,
        "env_kind": env_kind,
    }


#: The non-secret subset safe to LOG / echo (never cookies/headers/creds values).
#: NOTE ``env_name`` (not ``name``): this dict is spread into logging ``extra=`` and
#: ``name`` is a RESERVED LogRecord attribute — using it raises KeyError at log time.
def env_context_log_safe(ctx: dict | None) -> dict:
    if not ctx:
        return {}
    return {
        "environment_id": ctx.get("environment_id"), "env_name": ctx.get("name"),
        "base_url": ctx.get("base_url"), "env_kind": ctx.get("env_kind"),
        "has_cookies": bool(ctx.get("cookies")), "has_headers": bool(ctx.get("headers")),
        "has_http_credentials": bool(ctx.get("http_credentials")),
        "allow_submit": bool((ctx.get("fences") or {}).get("allow_submit")),
    }


__all__ = ["resolve_env_context", "env_context_log_safe"]
