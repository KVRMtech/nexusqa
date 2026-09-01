"""Resolve Teams installations.

Credential blob shape::

    {
      "ms_app_id":     "<bot's MS app/client id>",
      "ms_app_password": "<bot's MS app secret>",
      "expected_tenant_id": "<aad tenant id>",      // optional pin
      "channel_auth_tenant": "common"               // optional, default 'botframework.com'
    }

config (per-installation public values) may carry:

    { "aad_tenant_id": "<aad tenant id>" }
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


TEAMS_INTEGRATION_ID = "teams"
_AAD = TEAMS_INTEGRATION_ID.encode("utf-8")


class TeamsInstallationError(Exception):
    """Unable to resolve / decrypt the teams installation."""


@dataclass(frozen=True)
class TeamsInstallation:
    tenant_id: str
    installation_id: str
    aad_tenant_id: Optional[str]
    ms_app_id: str
    ms_app_password: str
    channel_auth_tenant: str
    status: str


class TeamsInstallationLoader:
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
        self._cache: dict[str, tuple[TeamsInstallation, float]] = {}

    async def for_tenant(self, tenant_id: str) -> TeamsInstallation:
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

    async def for_aad_tenant(self, aad_tenant_id: str) -> TeamsInstallation:
        """Reverse lookup: AAD tenant ID → Nexus tenant installation."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.integration_id
                        == TEAMS_INTEGRATION_ID,
                        integration_installations.c.status.in_(
                            ("connected", "degraded")
                        ),
                        integration_installations.c.config["aad_tenant_id"].astext == aad_tenant_id,
                    )
                )
            ).mappings().all()
        if not rows:
            raise TeamsInstallationError(
                f"no Teams installation for aad_tenant_id={aad_tenant_id}"
            )
        if len(rows) > 1:
            logger.warning(
                "teams.multiple_installations_for_aad count=%d", len(rows)
            )
        return await self._materialise(rows[0])

    def invalidate(self, tenant_id: str) -> None:
        self._cache.pop(tenant_id, None)

    async def _load(self, tenant_id: str) -> TeamsInstallation:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.tenant_id == tenant_id,
                        integration_installations.c.integration_id
                        == TEAMS_INTEGRATION_ID,
                    )
                )
            ).mappings().first()
        if row is None:
            raise TeamsInstallationError(
                f"no Teams installation for tenant_id={tenant_id}"
            )
        return await self._materialise(row)

    async def _materialise(self, row) -> TeamsInstallation:
        if row["status"] not in ("connected", "degraded"):
            raise TeamsInstallationError(
                f"Teams installation in status={row['status']!r}"
            )
        cipher = row["encrypted_credentials"]
        if not cipher:
            raise TeamsInstallationError(
                "Teams installation has no encrypted_credentials"
            )
        try:
            blob = EnvelopeBlob.from_bytes(bytes(cipher))
            plaintext = await self._envelope.decrypt(
                row["tenant_id"], blob, expected_aad=_AAD
            )
        except EnvelopeError as exc:
            raise TeamsInstallationError(
                f"failed to decrypt Teams credentials: {exc}"
            ) from exc

        try:
            creds = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TeamsInstallationError(
                f"credentials blob is not valid JSON: {exc}"
            ) from exc

        ms_app_id = creds.get("ms_app_id")
        ms_app_password = creds.get("ms_app_password")
        if not isinstance(ms_app_id, str) or not ms_app_id:
            raise TeamsInstallationError(
                "credentials missing 'ms_app_id'"
            )
        if not isinstance(ms_app_password, str) or not ms_app_password:
            raise TeamsInstallationError(
                "credentials missing 'ms_app_password'"
            )

        channel_auth_tenant = creds.get("channel_auth_tenant") or "botframework.com"
        config = row["config"] or {}
        aad_tenant_id = (
            config.get("aad_tenant_id") if isinstance(config, dict) else None
        )

        return TeamsInstallation(
            tenant_id=row["tenant_id"],
            installation_id=row["installation_id"],
            aad_tenant_id=aad_tenant_id,
            ms_app_id=ms_app_id,
            ms_app_password=ms_app_password,
            channel_auth_tenant=str(channel_auth_tenant),
            status=row["status"],
        )
