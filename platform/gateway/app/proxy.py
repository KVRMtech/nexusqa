"""
API Gateway — Proxy Logic.

Reverse-proxy handler that routes requests to the correct engine,
performs JWT validation at the edge, enforces rate limits,
and propagates correlation IDs for cross-service tracing.
"""
from __future__ import annotations

import time
import uuid
import logging

import httpx
from fastapi import HTTPException, Request, Response
from starlette.responses import StreamingResponse

from .config import GatewayConfig
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Correlation ID header name (consistent with SDK)
REQUEST_ID_HEADER = "X-Request-ID"
TENANT_ID_HEADER = "X-Tenant-ID"


async def proxy_request(
    request: Request,
    path: str,
    *,
    routes: dict[str, str],
    config: GatewayConfig,
    client: httpx.AsyncClient,
    rate_limiter: RateLimiter,
) -> Response:
    """
    Reverse proxy — routes requests to the correct engine.

    Handles JWT validation, rate limiting, and upstream forwarding.
    """
    full_path = f"/api/v1/{path}"

    # ── Public paths skip auth ──────────────────────────────
    _public_prefixes = [p.strip() for p in config.public_path_prefixes.split(",") if p.strip()]
    is_public = any(full_path.startswith(p) for p in _public_prefixes)

    # ── JWT validation at the edge ──────────────────────────
    tenant_id = "anonymous"
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            import jwt as _jwt
            payload = _jwt.decode(
                auth_header[7:],
                config.nexus_jwt_secret,
                algorithms=[config.jwt_algorithm],
            )
            tenant_id = payload.get("tenant_id", "anonymous")
        except Exception as e:
            if not is_public:
                logger.warning("gateway.jwt_invalid", extra={"error": str(e), "path": full_path})
                raise HTTPException(
                    status_code=401,
                    detail="Invalid or expired authentication token",
                )
    elif not is_public:
        logger.debug("gateway.no_auth_token", extra={"path": full_path})

    # Health endpoints are cheap read-only probes — exempt from rate limiting
    is_health = "/health" in full_path
    if not is_health and not rate_limiter.allow(tenant_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again shortly.",
            headers={"Retry-After": "60"},
        )

    # Find the matching upstream
    upstream_url = None
    for prefix, url in routes.items():
        if full_path.startswith(prefix):
            upstream_url = url
            break

    if not upstream_url:
        raise HTTPException(status_code=404, detail=f"No engine handles path: {full_path}")

    # Detect SSE streaming requests
    is_sse = (
        full_path.endswith("/stream")
        or "text/event-stream" in request.headers.get("accept", "")
    )

    # Build upstream request
    for prefix in routes:
        if full_path.startswith(prefix):
            suffix = full_path[len(prefix):]
            if suffix.startswith("/health"):
                target_url = f"{upstream_url}{suffix}"
            else:
                target_url = f"{upstream_url}{full_path}"
            break
    else:
        target_url = f"{upstream_url}{full_path}"

    start = time.monotonic()

    # ── Generate / propagate correlation ID ──────────────
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

    try:
        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        # Inject correlation headers for downstream engines
        headers[REQUEST_ID_HEADER] = request_id
        headers[TENANT_ID_HEADER] = tenant_id

        # ── SSE streaming proxy ────────────────────────────
        if is_sse:
            logger.info(
                "gateway.sse_proxy",
                extra={
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "path": full_path,
                    "upstream": upstream_url,
                },
            )

            async def _stream_sse():
                async with client.stream(
                    method=request.method,
                    url=target_url,
                    content=body if body else None,
                    headers=headers,
                    params=dict(request.query_params),
                ) as upstream_resp:
                    async for chunk in upstream_resp.aiter_bytes():
                        yield chunk

            return StreamingResponse(
                _stream_sse(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    REQUEST_ID_HEADER: request_id,
                },
            )

        # ── Standard buffered proxy ────────────────────────
        resp = await client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )

        elapsed_ms = (time.monotonic() - start) * 1000

        logger.info(
            "gateway.proxy",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "method": request.method,
                "path": full_path,
                "upstream": upstream_url,
                "status": resp.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )

        # Build response with correlation ID
        response_headers = dict(resp.headers)
        response_headers[REQUEST_ID_HEADER] = request_id

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
        )

    except httpx.ConnectError:
        engine_name = full_path.split("/")[3] if len(full_path.split("/")) > 3 else "unknown"
        raise HTTPException(
            status_code=503,
            detail=f"Engine '{engine_name}' is not available. Is it running?",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Engine request timed out",
        )
