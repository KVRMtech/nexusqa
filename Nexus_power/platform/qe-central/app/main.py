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
from app.security.boot_validator import (
    BootSafetyError,
    kek_health_status,
    validate_boot_safety,
)
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
from app.routers.internal import router as internal_router
from app.routers.scenario_gov import router as scenario_gov_router
from app.routers.cycles import router as cycles_router
from app.routers.cost import router as cost_router
from app.routers.webhooks import router as webhooks_router
from app.routers.compliance import router as compliance_router
from app.routers.fleet import router as fleet_router

# ── Phase-5.5 additive wiring (all OPT-IN; the default of every knob preserves
#    today's single-instance behavior — see app/config.py + the module docs) ──
from app.api_protect import (
    API_RATE_LIMITER,
    principal_rate_limit_middleware,
    unhandled_exception_handler,
)
from app.observability import CorrelationIdMiddleware, metrics_router

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
    # ── Phase-6 fail-closed BOOT GATE (runs FIRST, before any DB/KMS work) ──
    # Refuse to start a DEPLOYED process (NEXUS_ENV in {staging,production}) that
    # is still wearing development defaults: dev KEK, empty/default JWT or
    # explorer secrets, or a default DB password.  INERT in development/test
    # (the default NEXUS_ENV) — it only WARNS there, so local dev + the existing
    # test suite boot unchanged.  A fatal raises BootSafetyError, which we log
    # loudly and let propagate so the process EXITS rather than boot unsafe.
    try:
        validate_boot_safety(settings)
    except BootSafetyError as exc:
        logger.critical(
            "qe_central.boot_refused",
            nexus_env=settings.nexus_env,
            violations=exc.violations,
        )
        raise
    await init_db()
    application.state.envelope_service = _init_envelope_service()
    # Multi-env: share the process EnvelopeService with the in-process cycle daemon
    # so run_cycle can decrypt a bound Environment Profile's sealed creds (basic-auth).
    # Additive: when None, per-env secret decryption is skipped (honest degradation).
    try:
        from app.controlplane.cycle.driver import set_control_plane_envelope
        set_control_plane_envelope(application.state.envelope_service)
    except Exception as exc:  # never let this optional wiring break boot
        logger.warning("qe_central.control_plane_envelope_wire_failed", error=str(exc)[:200])
    # S5 (Phase-4) control plane: the cycle-driver daemon (design §3.5). Hosted
    # via asyncio.create_task in the lifespan (NOT @app.on_event) — the
    # sentinel_daemon pattern (qe_agents.py:136-156). It SELF-GATES on
    # QEC_CYCLE_TICK_SECONDS: when unset/<=0 it logs and idles, so a stack that
    # has not opted into automated cycles pays nothing. try/except inside the
    # loop means one cycle failure never kills the daemon.
    import asyncio

    from app.controlplane.cycle.driver import cycle_driver_daemon
    from app.controlplane.leader import build_leader_election

    # Phase-5.5 leader election wraps the daemon loop.  DEFAULT mode ``none`` ⇒
    # this instance is ALWAYS leader ⇒ byte-identical to today's single
    # ``create_task(cycle_driver_daemon())``.  In ``advisory_lock`` mode exactly
    # ONE replica across the fleet holds the Postgres advisory lock and runs the
    # loop; a follower idles (waking on ``driver_stop``) and takes over when the
    # leader dies (the session lock auto-releases on connection close).
    election = build_leader_election()
    driver_stop = asyncio.Event()
    driver_task = asyncio.create_task(
        election.run_as_leader(cycle_driver_daemon, stop_event=driver_stop),
        name="qec-cycle-driver",
    )
    application.state.cycle_driver_task = driver_task
    application.state.cycle_driver_stop = driver_stop
    application.state.leader_election = election
    logger.info(
        "qe_central.started",
        port=settings.port,
        db_qec=is_qec_connected(),
        db_substrate=is_substrate_connected(),
        storage_backend=settings.nexus_storage_backend,
        harness_enabled=settings.qe_harness_enabled,
        envelope=(application.state.envelope_service is not None),
        cycle_driver="launched",
        admission_backend=settings.qec_admission_backend,
        leader_election=election.mode,
    )
    yield
    # Clean shutdown: signal the loop to stop (wakes an idle follower promptly),
    # then cancel the task — the daemon returns on CancelledError and
    # ``run_as_leader``'s finally releases leadership (freeing the advisory lock
    # for another replica).
    driver_stop.set()
    driver_task.cancel()
    try:
        await driver_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - clean shutdown
        pass
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


# ── Middleware registration (Phase-5.5) ─────────────────────────────────────
# Starlette runs http middleware in REVERSE registration order (the LAST
# registered is OUTERMOST / executes FIRST), so we register innermost-first.
# Desired EXECUTION order (outer → inner):
#     CorrelationId → security_headers → jwt_auth → [rate_limit] → route
#   * CorrelationId is OUTERMOST so EVERY response — including a JWT 401 or a
#     rate-limit 429 — carries X-Request-ID and every log line (incl. the JWT
#     gate's) is tagged with the correlation id.
#   * security_headers is OUTER to the JWT gate + rate limiter so a REJECTED
#     request STILL gets the security headers attached on the way out.
#   * the API rate limiter runs INNERMOST (after jwt_auth) so it reads the
#     authenticated principal from request.state.user; it is mounted ONLY when
#     QEC_API_RATE_LIMIT > 0 (OFF by default ⇒ no added middleware, no behavior
#     change).
if API_RATE_LIMITER.enabled:
    app.middleware("http")(principal_rate_limit_middleware)
app.middleware("http")(jwt_auth_middleware)
app.middleware("http")(security_headers_middleware)
app.add_middleware(CorrelationIdMiddleware)

# Phase-5.5 global exception handler: any UNHANDLED exception → a clean 500 with
# the correlation id, never leaking internal detail/stack.  HTTPException keeps
# its own status (Starlette's dedicated handler), so intentional 4xx are intact.
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(apps_router)
app.include_router(explorations_router)
app.include_router(harness_router)
# S4 (Phase-3) scenario governance — criticality / synthesis / coverage /
# approval / tier-label / touch-meter (design §3.4). Additive; VKPower untouched.
app.include_router(scenario_gov_router)
# S5 (Phase-4) control plane — app registry cycles, GitLab webhooks, cost/budget
# (design §3.5). Additive; drives the UNCHANGED VKPower factory over HTTP.
app.include_router(cycles_router)
app.include_router(cost_router)
app.include_router(webhooks_router)
# Phase-8 (SEAM E) compliance — READ-ONLY framework projections (SOC2 / NAIC
# MAR / EU AI-Act) over the existing verdict chains / dossiers / waivers /
# audit_log. Additive; captures nothing new; admin-only, tenant-scoped.
app.include_router(compliance_router)
# Phase-7 (fleet) tenant provisioning + lifecycle — onboard/suspend/resume/
# offboard a CLIENT in one call. Admin-only via a NEW PLATFORM SUPER-ADMIN scope
# (role admin + platform_admin marker), so a tenant admin can NOT manage other
# tenants. Additive; a tenant with no control record behaves exactly as today.
app.include_router(fleet_router)
# Phase-1: the HMAC-authenticated explorer completion callback. Lives OUTSIDE
# the /api/* prefix (the explorer holds no JWT — only the HMAC token), so the
# fail-closed JWT middleware skips it; internal.py verifies the HMAC itself.
app.include_router(internal_router)
# Phase-5.5 metrics exposition — mounted OUTSIDE the /api/* prefix (default path
# /metrics; QEC_METRICS_PATH) so the fail-closed JWT gate skips it, the same
# public posture as /health.  The payload is a single empty-scrape comment when
# prometheus_client is absent or QEC_METRICS_ENABLED=0.
app.include_router(metrics_router)


# ─── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request) -> dict:
    """Liveness + DB/GUC self-check + storage backend + KEK canary (design §3.1).

    Runs LIVE checks on every call — the GUC round-trip proves the RLS
    session discipline actually works on the qecentral engine, not just
    that TCP connects.  Status is 'healthy' only when everything passed.

    Phase-6 KEK canary: reports the envelope-encryption posture WITHOUT ever
    leaking a secret (provider name + booleans only).  In a DEPLOYED env a
    ``local`` (development) KEK, or an envelope service that failed to init,
    DEGRADES the whole service — credential writes would refuse (503) or run
    without KMS backing — so ``/health`` says 'degraded' honestly.
    """
    await init_db()  # refresh connectivity flags honestly on every probe
    guc = await guc_self_check()
    db_qec = "connected" if is_qec_connected() else "disconnected"
    db_substrate = "connected" if is_substrate_connected() else "disconnected"

    envelope_ready = getattr(request.app.state, "envelope_service", None) is not None
    kek, kek_degraded = kek_health_status(
        provider=settings.nexus_kek_provider,
        envelope_ready=envelope_ready,
        is_deployed=settings.is_deployed_env,
    )

    core_ok = (
        db_qec == "connected" and db_substrate == "connected" and bool(guc.get("ok"))
    )
    healthy = core_ok and not kek_degraded
    return {
        "status": "healthy" if healthy else "degraded",
        "service": settings.qec_service_name,
        "db_qec": db_qec,
        "db_substrate": db_substrate,
        "storage_backend": settings.nexus_storage_backend,
        "guc": guc,
        "kek": kek,
        "harness_enabled": settings.qe_harness_enabled,
    }


# ─── Entry Point ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
