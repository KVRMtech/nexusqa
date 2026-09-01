"""
Nexus Input Sanitization — Request validation and security utilities.

Provides:
- Path traversal protection for file operations
- Request body size limiting middleware
- String sanitization for user-supplied text

These utilities complement Pydantic model validation by catching
security-sensitive patterns that schema validation alone cannot detect.
"""

from __future__ import annotations

import os
import re

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

__all__ = [
    "sanitize_path",
    "validate_request_size",
    "RequestSizeLimitMiddleware",
    "sanitize_string",
]

# Maximum request body size (default 10 MB)
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024

# Patterns that indicate path traversal attempts
_PATH_TRAVERSAL_PATTERNS = re.compile(
    r"(\.\./|\.\.\\|%2e%2e%2f|%2e%2e/|\.\.%2f|%2e%2e%5c)",
    re.IGNORECASE,
)

# Characters not allowed in filenames
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def sanitize_path(
    user_path: str,
    base_dir: str | None = None,
) -> str:
    """
    Sanitize a user-supplied file path to prevent path traversal attacks.

    Args:
        user_path: The path provided by the user.
        base_dir: If provided, ensures the resolved path stays within this directory.

    Returns:
        Sanitized path string.

    Raises:
        HTTPException: If path traversal is detected.
    """
    # Check for obvious traversal patterns
    if _PATH_TRAVERSAL_PATTERNS.search(user_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid path: directory traversal not allowed",
        )

    # Normalize the path
    normalized = os.path.normpath(user_path)

    # After normalization, check again for parent directory references
    if ".." in normalized.split(os.sep):
        raise HTTPException(
            status_code=400,
            detail="Invalid path: directory traversal not allowed",
        )

    # If base_dir specified, ensure the path stays within it
    if base_dir:
        base = os.path.abspath(base_dir)
        full = os.path.abspath(os.path.join(base, normalized))
        if not full.startswith(base + os.sep) and full != base:
            raise HTTPException(
                status_code=400,
                detail="Invalid path: access outside allowed directory",
            )
        return full

    return normalized


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a user-supplied filename, removing unsafe characters.

    Args:
        filename: Original filename from user.

    Returns:
        Safe filename suitable for filesystem operations.

    Raises:
        HTTPException: If filename is empty or purely unsafe.
    """
    # Remove path components — only the filename
    name = os.path.basename(filename)

    # Remove unsafe characters
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)

    # Remove leading/trailing dots and spaces
    name = name.strip(". ")

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename",
        )

    return name


def sanitize_string(
    value: str,
    max_length: int = 10000,
    strip_html: bool = False,
) -> str:
    """
    Sanitize a user-supplied string.

    Args:
        value: Input string from user.
        max_length: Maximum allowed length.
        strip_html: If True, remove HTML tags.

    Returns:
        Sanitized string.
    """
    # Truncate to max length
    if len(value) > max_length:
        value = value[:max_length]

    # Strip null bytes
    value = value.replace("\x00", "")

    # Strip HTML if requested
    if strip_html:
        value = re.sub(r"<[^>]+>", "", value)

    return value


def validate_request_size(
    content_length: int | None,
    max_size: int = DEFAULT_MAX_BODY_SIZE,
) -> None:
    """
    Validate that a request body doesn't exceed the size limit.

    Args:
        content_length: Content-Length header value.
        max_size: Maximum allowed body size in bytes.

    Raises:
        HTTPException: If content length exceeds the limit.
    """
    if content_length is not None and content_length > max_size:
        raise HTTPException(
            status_code=413,
            detail=f"Request body too large. Maximum: {max_size // (1024 * 1024)} MB",
        )


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces maximum request body size.

    Rejects requests with Content-Length exceeding the configured maximum
    before the body is read, preventing memory exhaustion attacks.

    Args:
        app: ASGI application.
        max_body_size: Maximum request body size in bytes (default: 10 MB).
    """

    def __init__(self, app, max_body_size: int = DEFAULT_MAX_BODY_SIZE):
        super().__init__(app)
        self.max_body_size = max_body_size

    async def dispatch(self, request: Request, call_next) -> Response:
        # Check Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > self.max_body_size:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body too large. Maximum: {self.max_body_size // (1024 * 1024)} MB",
                        },
                    )
            except ValueError:
                pass

        return await call_next(request)
