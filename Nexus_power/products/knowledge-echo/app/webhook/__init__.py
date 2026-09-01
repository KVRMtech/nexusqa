"""Generic Webhook surface — Source + Surface.

Inbound: signed HMAC-SHA256 POST containing ``{question, user_id, channel_id, …}``.
Outbound: signed HMAC-SHA256 POST to the tenant-configured destination
URL when an echo is produced.

This is the simplest surface and serves as the reference implementation
for the ``SurfaceHandler`` protocol — every other surface follows the
same shape.
"""

from __future__ import annotations

from .composer import WebhookComposer
from .dispatcher import WebhookDispatcher, WebhookDispatchError
from .installation import (
    WebhookInstallation,
    WebhookInstallationError,
    WebhookInstallationLoader,
)
from .parser import (
    WebhookInboundPayload,
    WebhookInboundError,
    parse_webhook_inbound,
)
from .signature import (
    WebhookSignatureError,
    sign_webhook_body,
    verify_webhook_signature,
)
from .handler import build_webhook_handler

__all__ = [
    "WebhookComposer",
    "WebhookDispatchError",
    "WebhookDispatcher",
    "WebhookInboundError",
    "WebhookInboundPayload",
    "WebhookInstallation",
    "WebhookInstallationError",
    "WebhookInstallationLoader",
    "WebhookSignatureError",
    "build_webhook_handler",
    "parse_webhook_inbound",
    "sign_webhook_body",
    "verify_webhook_signature",
]
