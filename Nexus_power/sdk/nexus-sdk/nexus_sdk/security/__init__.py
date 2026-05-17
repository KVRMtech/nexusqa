"""
Nexus Security — Production security middleware and utilities.

Provides:
- Security headers middleware (OWASP-compliant)
- Input sanitization utilities
- Request body size limiting
- Path traversal protection

Every NexusEngine automatically enables security middleware.
Platform services (gateway, API) can use these modules directly.
"""

from nexus_sdk.security.headers import SecurityHeadersMiddleware
from nexus_sdk.security.sanitization import (
    sanitize_path,
    validate_request_size,
    RequestSizeLimitMiddleware,
)
from nexus_sdk.security.rate_limit import RateLimitMiddleware
from nexus_sdk.security.envelope import (
    AwsKmsProvider,
    EnvelopeBlob,
    EnvelopeError,
    EnvelopeService,
    IntegrityError,
    KekProvider,
    KekProviderError,
    LocalKekProvider,
    ProviderUnavailable,
)

__all__ = [
    "SecurityHeadersMiddleware",
    "sanitize_path",
    "validate_request_size",
    "RequestSizeLimitMiddleware",
    "RateLimitMiddleware",
    "AwsKmsProvider",
    "EnvelopeBlob",
    "EnvelopeError",
    "EnvelopeService",
    "IntegrityError",
    "KekProvider",
    "KekProviderError",
    "LocalKekProvider",
    "ProviderUnavailable",
]
