"""
Nexus Structured Logging — JSON-formatted, context-rich logging for production.

Configures structlog with:
- JSON output for machine parsing (Loki, ELK, CloudWatch)
- Human-readable console output in development
- Automatic context binding (service name, version, environment)
- Correlation ID injection from context vars
- Timestamp, log level, caller info in every log line
- Standard library logging bridge (so third-party libs also emit JSON)

Usage:
    from nexus_sdk.observability.logging import setup_logging, get_logger

    # Call once at startup
    setup_logging(service_name="ears", log_level="INFO", environment="production")

    # Use anywhere
    logger = get_logger()
    logger.info("transcription.complete", audio_id="abc123", duration_ms=1234)
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Optional

import structlog

# Context variables for cross-cutting concerns
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_tenant_id_ctx: ContextVar[Optional[str]] = ContextVar("tenant_id", default=None)
_service_name_ctx: ContextVar[Optional[str]] = ContextVar("service_name", default=None)

_logging_configured = False


def get_request_id() -> Optional[str]:
    """Get the current request's correlation ID."""
    return _request_id_ctx.get()


def set_request_id(request_id: str) -> None:
    """Set the correlation ID for the current async context."""
    _request_id_ctx.set(request_id)


def get_tenant_id() -> Optional[str]:
    """Get the current request's tenant ID."""
    return _tenant_id_ctx.get()


def set_tenant_id(tenant_id: str) -> None:
    """Set the tenant ID for the current async context."""
    _tenant_id_ctx.set(tenant_id)


def set_service_name(name: str) -> None:
    """Set the service name for the current context."""
    _service_name_ctx.set(name)


def _add_context_vars(
    logger: structlog.typing.WrappedLogger,
    method_name: str,
    event_dict: dict,
) -> dict:
    """Structlog processor: inject context vars into every log entry."""
    request_id = _request_id_ctx.get()
    if request_id:
        event_dict["request_id"] = request_id

    tenant_id = _tenant_id_ctx.get()
    if tenant_id:
        event_dict["tenant_id"] = tenant_id

    service = _service_name_ctx.get()
    if service:
        event_dict["service"] = service

    return event_dict


def setup_logging(
    service_name: str,
    log_level: str = "INFO",
    environment: str = "production",
    json_output: bool | None = None,
) -> None:
    """
    Configure structured logging for the entire process.

    Args:
        service_name: Name of this service (e.g., "ears", "gateway").
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        environment: "production" (JSON) or "development" (console-colored).
        json_output: Force JSON output. If None, auto-detect from environment.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    set_service_name(service_name)

    # Determine output format
    use_json = json_output if json_output is not None else (environment != "development")

    # Map string level to int
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors for structlog
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_context_vars,
    ]

    if use_json:
        # Production: JSON output for machine parsing
        renderer = structlog.processors.JSONRenderer()
    else:
        # Development: human-readable colored console output
        renderer = structlog.dev.ConsoleRenderer()

    # Configure structlog
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure standard library logging to use structlog formatter
    # This ensures third-party libraries (uvicorn, httpx, sqlalchemy) also emit structured logs
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Quiet down noisy loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    # Ensure uvicorn's own logger uses our handler
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.addHandler(handler)
    uvicorn_logger.setLevel(level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a structlog logger instance.

    Args:
        name: Logger name (typically __name__). If None, uses caller's module.

    Returns:
        Bound structlog logger with context injection.
    """
    return structlog.get_logger(name)


def reset_logging_state() -> None:
    """Reset logging configuration state. Used in tests."""
    global _logging_configured
    _logging_configured = False
