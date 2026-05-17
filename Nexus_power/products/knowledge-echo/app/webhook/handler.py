"""Webhook SurfaceHandler factory."""

from __future__ import annotations

from typing import Optional

import httpx

from ..surfaces import SurfaceHandler
from .composer import WebhookComposer
from .dispatcher import WebhookDispatcher
from .installation import WebhookInstallationLoader


def build_webhook_handler(
    *,
    installs: WebhookInstallationLoader,
    timeout_seconds: float = 15.0,
    max_retries: int = 3,
    client: Optional[httpx.AsyncClient] = None,
    composer: Optional[WebhookComposer] = None,
) -> SurfaceHandler:
    return SurfaceHandler(
        surface="webhook",
        composer=composer or WebhookComposer(),
        dispatcher=WebhookDispatcher(
            installs,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            client=client,
        ),
    )
