"""
Nexus Correlation ID Middleware — Request tracing across service boundaries.

Generates a unique X-Request-ID for each incoming request (or uses the one
from upstream) and propagates it through:
1. Response headers (for client-side correlation)
2. Structlog context (for log correlation)
3. Downstream HTTP calls (via header forwarding)

This enables tracing a single user request across all 14 Nexus services
by searching logs for the same request_id.

Usage:
    # Automatically added by NexusEngine. For standalone services:
    from nexus_sdk.observability.correlation import CorrelationIdMiddleware

    app.add_middleware(CorrelationIdMiddleware, service_name="gateway")
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nexus_sdk.observability.logging import set_request_id, set_tenant_id

__all__ = ["CorrelationIdMiddleware"]

# Standard header names
REQUEST_ID_HEADER = "X-Request-ID"
TENANT_ID_HEADER = "X-Tenant-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware that manages correlation IDs for cross-service request tracing.

    For each request:
    1. Reads X-Request-ID from incoming headers (set by gateway or upstream)
    2. Generates a new UUID if no X-Request-ID is present
    3. Binds the request_id to structlog context vars for the request's lifetime
    4. Sets X-Request-ID in the response headers
    5. Extracts tenant_id from JWT claims or X-Tenant-ID header
    """

    def __init__(self, app, service_name: str = "unknown"):
        super().__init__(app)
        self.service_name = service_name

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract or generate request ID
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        # Extract tenant ID from header (gateway injects this after JWT validation)
        tenant_id = request.headers.get(TENANT_ID_HEADER)

        # Bind to structlog context for the duration of this request
        set_request_id(request_id)
        if tenant_id:
            set_tenant_id(tenant_id)

        # Store in request state for downstream access
        request.state.request_id = request_id
        if tenant_id:
            request.state.tenant_id = tenant_id

        # Process the request
        response = await call_next(request)

        # Set correlation headers in response
        response.headers[REQUEST_ID_HEADER] = request_id
        if tenant_id:
            response.headers[TENANT_ID_HEADER] = tenant_id

        return response
