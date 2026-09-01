"""Resolve webhook installations.

Like Slack, webhook tenants store credentials in
``integration_installations``. The decrypted blob is a JSON object::

    {
      "inbound_secret":  "<shared secret for HMAC verification>",
      "outbound": {
        "destination_url": "https://customer.example.com/nexus-echoes",
        "outbound_secret": "<shared secret for signing our POSTs>",
        "headers":         { "X-Customer-Auth": "..." }
      }
    }

The outbound section is optional — a tenant can install the webhook
plugin as Source-only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import sqlalchemy as sa
from nexus_sdk.security.envelope import (
    EnvelopeBlob,
    EnvelopeError,
    EnvelopeService,
)

from ..db import Database, integration_installations

logger = logging.getLogger(__name__)


WEBHOOK_INTEGRATION_ID = "webhook"
_AAD = WEBHOOK_INTEGRATION_ID.encode("utf-8")


class WebhookInstallationError(Exception):
    """Unable to resolve / decrypt the webhook installation."""


@dataclass(frozen=True)
class WebhookOutbound:
    destination_url: str
    outbound_secret: str
    extra_headers: dict[str, str]


@dataclass(frozen=True)
class WebhookInstallation:
    tenant_id: str
    installation_id: str
    inbound_secret: str
    outbound: Optional[WebhookOutbound]
    status: str


class WebhookInstallationLoader:
    def __init__(
        self,
        db: Database,
        envelope: EnvelopeService,
        *,
        cache_ttl_seconds: int = 60,
    ):
        self._db = db
        self._envelope = envelope
        self._ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[WebhookInstallation, float]] = {}

    async def for_tenant(self, tenant_id: str) -> WebhookInstallation:
        import time

        entry = self._cache.get(tenant_id)
        if entry is not None and time.monotonic() < entry[1]:
            return entry[0]
        installation = await self._load(tenant_id)
        self._cache[tenant_id] = (
            installation,
            time.monotonic() + self._ttl,
        )
        return installation

    def invalidate(self, tenant_id: str) -> None:
        self._cache.pop(tenant_id, None)

    async def _load(self, tenant_id: str) -> WebhookInstallation:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.tenant_id == tenant_id,
                        integration_installations.c.integration_id
                        == WEBHOOK_INTEGRATION_ID,
                    )
                )
            ).mappings().first()
        if row is None:
            raise WebhookInstallationError(
                f"no Webhook installation for tenant_id={tenant_id}"
            )
        if row["status"] not in ("connected", "degraded"):
            raise WebhookInstallationError(
                f"Webhook installation in status={row['status']!r}"
            )
        cipher = row["encrypted_credentials"]
        if not cipher:
            raise WebhookInstallationError(
                "Webhook installation has no encrypted_credentials"
            )
        try:
            blob = EnvelopeBlob.from_bytes(bytes(cipher))
            plaintext = await self._envelope.decrypt(
                tenant_id, blob, expected_aad=_AAD
            )
        except EnvelopeError as exc:
            raise WebhookInstallationError(
                f"failed to decrypt Webhook credentials: {exc}"
            ) from exc
        try:
            credentials = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookInstallationError(
                f"credentials blob is not valid JSON: {exc}"
            ) from exc

        inbound_secret = credentials.get("inbound_secret")
        if not isinstance(inbound_secret, str) or not inbound_secret:
            raise WebhookInstallationError(
                "credentials missing 'inbound_secret'"
            )
        outbound = None
        out = credentials.get("outbound")
        if isinstance(out, dict):
            destination_url = out.get("destination_url")
            outbound_secret = out.get("outbound_secret")
            extra_headers = out.get("headers") or {}
            if not isinstance(destination_url, str) or not destination_url:
                raise WebhookInstallationError(
                    "outbound.destination_url missing"
                )
            if not isinstance(outbound_secret, str) or not outbound_secret:
                raise WebhookInstallationError(
                    "outbound.outbound_secret missing"
                )
            if not isinstance(extra_headers, dict):
                raise WebhookInstallationError(
                    "outbound.headers must be an object"
                )
            for k, v in extra_headers.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise WebhookInstallationError(
                        "outbound.headers entries must be strings"
                    )
            outbound = WebhookOutbound(
                destination_url=destination_url,
                outbound_secret=outbound_secret,
                extra_headers={str(k): str(v) for k, v in extra_headers.items()},
            )

        return WebhookInstallation(
            tenant_id=tenant_id,
            installation_id=row["installation_id"],
            inbound_secret=inbound_secret,
            outbound=outbound,
            status=row["status"],
        )
