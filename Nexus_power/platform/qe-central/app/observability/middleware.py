"""QE-Central — correlation-id (request-id) middleware + propagation helpers.

Phase-5.5 observability.  Every request gets a stable correlation id so a single
customer action can be traced end-to-end: from the inbound QE-Central request,
through the structured logs of the cycle it drives, out to the UNCHANGED VKPower
factory calls it makes, and back on the response header.

WHY A PURE-ASGI MIDDLEWARE (not ``BaseHTTPMiddleware``)
======================================================
Starlette's ``BaseHTTPMiddleware`` runs the downstream app in a SEPARATE anyio
task, so a ``ContextVar`` bound in its ``dispatch`` before ``call_next`` is not
reliably visible inside the route handler (or its logs).  A pure-ASGI middleware
awaits the downstream app INLINE, in the SAME context, so the correlation id we
bind here is visible to the endpoint, the cycle it drives, and every structlog
line emitted while the request is in flight.

WHAT IT DOES
============
:class:`CorrelationIdMiddleware`, on each HTTP request:

  1. reads the inbound id from the configured header (default ``X-Request-ID``);
  2. SANITISES it — an id that is empty, too long, or contains anything outside
     ``[A-Za-z0-9._-]`` is rejected and a fresh ``uuid4`` hex id is minted, so an
     attacker-controlled header can never inject bytes into logs or the echoed
     response header (security-first);
  3. binds the id into a process ``contextvars.ContextVar`` (the source of truth
     read by :func:`current_request_id`) AND into ``structlog`` contextvars (so
     every log line emitted while handling the request carries ``request_id``);
  4. exposes it on ``request.state.request_id`` for handlers/audit hooks;
  5. echoes it on the response under the same header;
  6. always UNBINDS on the way out (``finally``) so no id leaks across requests.

:func:`outbound_request_id_headers` merges the current id onto an httpx headers
dict so the factory client can PROPAGATE it downstream — one correlation id
spans QE-Central and VKPower.

Backward-compatible + additive: mounting the middleware is opt-in (the Wire
agent adds it); when it is NOT mounted, :func:`current_request_id` returns ``""``
and the propagation helper adds no header — today's behaviour is intact.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import uuid
from typing import Callable, Mapping, Optional

from starlette.datastructures import Headers

logger = logging.getLogger(__name__)


# ── Env knobs (config-driven) ───────────────────────────────────────────────
ENV_REQUEST_ID_HEADER = "QEC_REQUEST_ID_HEADER"
DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"

#: The structlog contextvars key the id is bound under (log field name).
LOG_FIELD_REQUEST_ID = "request_id"

#: Upper bound on an accepted inbound id — a defence against log/header abuse.
MAX_REQUEST_ID_LEN = 128
#: Only these characters are allowed in an inbound id (else it is re-minted).
_REQUEST_ID_ALLOWED = re.compile(r"^[A-Za-z0-9._\-]+$")


# ── Process-local source of truth (per async task / request) ────────────────
_REQUEST_ID_CTX: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "qec_request_id", default="",
)


def current_request_id() -> str:
    """Return the correlation id bound to the current context, or ``""``.

    Safe to call anywhere (handlers, the cycle driver, the factory client); when
    no middleware has run in this context it returns ``""``.
    """
    return _REQUEST_ID_CTX.get("")


def _sanitize_request_id(value: Optional[str]) -> str:
    """Return ``value`` if it is a safe correlation id, else ``""`` (re-mint).

    Fail-safe: any id that is empty, over :data:`MAX_REQUEST_ID_LEN`, or contains
    a character outside ``[A-Za-z0-9._-]`` is rejected — we never echo unbounded
    attacker-controlled bytes into logs or the response header.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LEN:
        return ""
    if not _REQUEST_ID_ALLOWED.match(candidate):
        return ""
    return candidate


def mint_request_id() -> str:
    """Mint a fresh correlation id (uuid4 hex — url/log-safe, 32 chars)."""
    return uuid.uuid4().hex


def _bind_structlog(request_id: str) -> None:
    """Bind ``request_id`` into structlog contextvars (best-effort, guarded)."""
    try:
        import structlog

        structlog.contextvars.bind_contextvars(**{LOG_FIELD_REQUEST_ID: request_id})
    except Exception:  # structlog absent/old — the ContextVar is still the SoT.
        logger.debug("qec.request_id.structlog_bind_skipped", exc_info=True)


def _unbind_structlog() -> None:
    """Unbind the ``request_id`` structlog contextvar (best-effort, guarded)."""
    try:
        import structlog

        structlog.contextvars.unbind_contextvars(LOG_FIELD_REQUEST_ID)
    except Exception:
        logger.debug("qec.request_id.structlog_unbind_skipped", exc_info=True)


class CorrelationIdMiddleware:
    """Pure-ASGI middleware that binds a correlation id to every HTTP request.

    Mount OUTERMOST (add it last, or first in an explicit middleware list) so
    even auth-rejected responses still carry a correlation id and every log line
    for the request — including the JWT gate's — is tagged.  Registered by the
    Wire agent via ``app.add_middleware(CorrelationIdMiddleware)``.
    """

    def __init__(
        self,
        app,
        *,
        header_name: Optional[str] = None,
        generator: Optional[Callable[[], str]] = None,
    ) -> None:
        self.app = app
        self.header_name = (
            header_name
            or os.getenv(ENV_REQUEST_ID_HEADER, DEFAULT_REQUEST_ID_HEADER)
            or DEFAULT_REQUEST_ID_HEADER
        )
        self._generator = generator or mint_request_id
        # ASGI header names are compared/emitted lowercased (spec §Headers).
        self._header_key = self.header_name.lower().encode("latin-1")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header_name)
        request_id = _sanitize_request_id(inbound) or self._generator()

        token = _REQUEST_ID_CTX.set(request_id)
        _bind_structlog(request_id)
        # Expose on request.state (Starlette backs request.state with scope['state']).
        scope.setdefault("state", {})["request_id"] = request_id

        id_bytes = request_id.encode("latin-1")

        async def _send(message) -> None:
            if message.get("type") == "http.response.start":
                # Echo the id, replacing any pre-existing occurrence (idempotent).
                headers = [
                    (k, v)
                    for (k, v) in message.get("headers", [])
                    if k.lower() != self._header_key
                ]
                headers.append((self._header_key, id_bytes))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            _unbind_structlog()
            _REQUEST_ID_CTX.reset(token)


def outbound_request_id_headers(
    headers: Optional[Mapping[str, str]] = None,
    *,
    header_name: Optional[str] = None,
) -> dict:
    """Return ``headers`` merged with the current correlation id header.

    The factory client calls this to PROPAGATE the id downstream to VKPower, so
    one correlation id spans QE-Central and the factory.  When no id is bound in
    the current context the input headers are returned UNCHANGED (a background
    cycle with no inbound request adds no header) — never a fabricated id.
    """
    merged: dict = dict(headers or {})
    request_id = current_request_id()
    if request_id:
        name = header_name or os.getenv(ENV_REQUEST_ID_HEADER, DEFAULT_REQUEST_ID_HEADER)
        merged[name or DEFAULT_REQUEST_ID_HEADER] = request_id
    return merged


__all__ = [
    "CorrelationIdMiddleware",
    "current_request_id",
    "mint_request_id",
    "outbound_request_id_headers",
    "DEFAULT_REQUEST_ID_HEADER",
    "ENV_REQUEST_ID_HEADER",
    "LOG_FIELD_REQUEST_ID",
    "MAX_REQUEST_ID_LEN",
]
