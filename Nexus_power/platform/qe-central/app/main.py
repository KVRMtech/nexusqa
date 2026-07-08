"""QE-Central service entrypoint (port 8093, design §3.1 service shape).

FastAPI app with:
  * lifespan startup/shutdown (NOT ``@app.on_event`` — ignored when a
    lifespan handler is set; see platform/api/main.py:223-228 precedent),
  * fail-closed JWT middleware on every ``/api/*`` route,
  * ``/health`` reporting BOTH database connections, the storage backend,
    and an RLS-GUC round-trip self-check — honest state, never forced-green,
  * the three Phase-0 routers (apps / explorations / harness),
  * a local EnvelopeService init (clone of the platform-api
    ``knowledge_foundation._kek_provider`` pattern) at
    ``app.state.envelope_service`` — None when unavailable, so credential
    writes refuse with 503 instead of falling back to plaintext.

Run: ``python -m app.main`` (container CMD) or uvicorn directly.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ─── Path setup (repo checkouts; the container has the SDK preinstalled) ──
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_SDK_PATH = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", "sdk", "nexus-sdk"))
for _p in (_SERVICE_ROOT, _SDK_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import structlog
from fastapi import FastAPI, Request

from app.auth import jwt_auth_middleware
from app.config import settings
from app.db import (
    close_db,
    guc_self_check,
    init_db,
    is_qec_connected,
    is_substrate_connected,
)
from app.routers.apps import router as apps_router
from app.routers.explorations import router as explorations_router
from app.routers.harness import router as harness_router

# ─── Structured logging ────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.qec_log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
)
logger = structlog.get_logger()


# ─── Envelope encryption (clone of knowledge_foundation.py:69-104) ────────

def _kek_provider():
    """Choose the KEK provider for the current deployment (env-driven)."""
    from nexus_sdk.security.envelope import (
        AwsKmsProvider,
        GcpKmsProvider,
        LocalKekProvider,
        ProviderUnavailable,
    )

    provider = os.environ.get("NEXUS_KEK_PROVIDER", "local").lower()
    if provider == "aws_kms":
        single_arn = os.environ.get("NEXUS_KEK_AWS_ARN")
        if not single_arn:
            raise ProviderUnavailable(
                "NEXUS_KEK_PROVIDER=aws_kms requires NEXUS_KEK_AWS_ARN until "
                "the per-tenant resolver service is wired."
            )

        async def _resolver(_tenant_id: str) -> str:
            return single_arn

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return AwsKmsProvider(kek_resolver=_resolver, region=region)

    if provider == "gcp_kms":
        key_name = os.environ.get("NEXUS_KEK_GCP_KEY")
        if not key_name:
            raise ProviderUnavailable(
                "NEXUS_KEK_PROVIDER=gcp_kms requires NEXUS_KEK_GCP_KEY (the "
                "CryptoKey resource name projects/P/locations/L/keyRings/R/"
                "cryptoKeys/K)."
            )

        async def _gcp_resolver(_tenant_id: str) -> str:
            return key_name

        return GcpKmsProvider(kek_resolver=_gcp_resolver)

    if provider == "local":
        master_key_path = os.environ.get(
            "NEXUS_LOCAL_KEK_PATH",
            str(Path.home() / ".nexus" / "kek" / "master.key"),
        )
        return LocalKekProvider(master_key_path=master_key_path)

    raise ProviderUnavailable(f"unsupported NEXUS_KEK_PROVIDER value: {provider!r}")


def _init_envelope_service():
    """Build the EnvelopeService or return None (routers then 503 on
    credential writes — refuse-plaintext, never a silent downgrade)."""
    from nexus_sdk.security.envelope import EnvelopeService, ProviderUnavailable

    try:
        return EnvelopeService(_kek_provider())
    except ProviderUnavailable as exc:
        logger.error("qe_central.envelope_init_failed", error=str(exc)[:300])
        return None
    except Exception as exc:  # unexpected — still refuse-closed, log loudly
        logger.error("qe_central.envelope_init_error", error=str(exc)[:300])
        return None


# ─── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    await init_db()
    application.state.envelope_service = _init_envelope_service()
    logger.info(
        "qe_central.started",
        port=settings.port,
        db_qec=is_qec_connected(),
        db_substrate=is_substrate_connected(),
        storage_backend=settings.nexus_storage_backend,
        harness_enabled=settings.qe_harness_enabled,
        envelope=(application.state.envelope_service is not None),
    )
    yield
    application.state.envelope_service = None
    await close_db()
    logger.info("qe_central.stopped")


# ─── Application ───────────────────────────────────────────────

app = FastAPI(
    title="Nexus QE-Central",
    description=(
        "Crawler-evidence substrate writer + Phase-0 REFUSE harness "
        "(QECentral IMPLEMENTATION_DESIGN §3.1)"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.state.settings = settings


async def security_headers_middleware(request: Request, call_next):
    """Production security headers on every response (platform-api parity)."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


# Registration order matters: middlewares run in reverse registration order,
# so the JWT gate (registered last) executes FIRST and rejected requests
# still get security headers attached on the way out.
app.middleware("http")(security_headers_middleware)
app.middleware("http")(jwt_auth_middleware)

app.include_router(apps_router)
app.include_router(explorations_router)
app.include_router(harness_router)


# ─── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    """Liveness + DB/GUC self-check + storage backend (design §3.1).

    Runs LIVE checks on every call — the GUC round-trip proves the RLS
    session discipline actually works on the qecentral engine, not just
    that TCP connects.  Status is 'healthy' only when everything passed.
    """
    await init_db()  # refresh connectivity flags honestly on every probe
    guc = await guc_self_check()
    db_qec = "connected" if is_qec_connected() else "disconnected"
    db_substrate = "connected" if is_substrate_connected() else "disconnected"
    healthy = (
        db_qec == "connected" and db_substrate == "connected" and bool(guc.get("ok"))
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "service": settings.qec_service_name,
        "db_qec": db_qec,
        "db_substrate": db_substrate,
        "storage_backend": settings.nexus_storage_backend,
        "guc": guc,
        "harness_enabled": settings.qe_harness_enabled,
    }


# ─── Entry Point ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
