"""
Nexus Security Headers Middleware — OWASP-compliant response headers.

Adds production-grade security headers to every HTTP response:
- Strict-Transport-Security (HSTS): Force HTTPS for 1 year
- X-Content-Type-Options: Prevent MIME-type sniffing
- X-Frame-Options: Prevent clickjacking
- X-XSS-Protection: Legacy XSS filter (still useful for older browsers)
- Content-Security-Policy: Restrict resource loading
- Referrer-Policy: Control referrer information leakage
- Permissions-Policy: Restrict browser feature access
- Cache-Control: Prevent sensitive data caching

Usage:
    # Automatically added by NexusEngine. For standalone services:
    from nexus_sdk.security.headers import SecurityHeadersMiddleware

    app.add_middleware(SecurityHeadersMiddleware)
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["SecurityHeadersMiddleware"]

# Default Content-Security-Policy for API services (no browser rendering)
_DEFAULT_CSP = "default-src 'none'; frame-ancestors 'none'"

# Default Permissions-Policy — disable all browser features for API
_DEFAULT_PERMISSIONS = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Attaches OWASP-recommended security headers to every response.

    Configurable per-service for different CSP needs:
    - API services use strict CSP that blocks everything
    - Frontend-serving services can relax CSP for script/style loading

    Args:
        app: ASGI application.
        csp: Content-Security-Policy header value. Set to None to omit.
        hsts_max_age: HSTS max-age in seconds (default: 1 year).
        include_hsts: Whether to include HSTS (disable for HTTP-only dev).
    """

    def __init__(
        self,
        app,
        csp: str | None = _DEFAULT_CSP,
        hsts_max_age: int = 31536000,
        include_hsts: bool = True,
        permissions_policy: str | None = _DEFAULT_PERMISSIONS,
    ):
        super().__init__(app)
        self.csp = csp
        self.hsts_max_age = hsts_max_age
        self.include_hsts = include_hsts
        self.permissions_policy = permissions_policy

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Prevent MIME-type sniffing attacks
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # Legacy XSS protection (still useful for IE/older Edge)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Control referrer leakage
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Prevent caching of API responses with sensitive data
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

        # HSTS — force HTTPS (skip in dev over HTTP)
        if self.include_hsts:
            response.headers["Strict-Transport-Security"] = (
                f"max-age={self.hsts_max_age}; includeSubDomains"
            )

        # Content-Security-Policy — restrict resource loading
        if self.csp:
            response.headers["Content-Security-Policy"] = self.csp

        # Permissions-Policy — restrict browser feature access
        if self.permissions_policy:
            response.headers["Permissions-Policy"] = self.permissions_policy

        return response
