"""Resolve Slack installation credentials for a tenant.

Reads ``integration_installations`` rows, decrypts ``encrypted_credentials``
via the platform's ``EnvelopeService``, and returns a minimal
``SlackInstallation`` for the orchestrator and outbound client.

The decrypted bot token never persists; it lives in memory only for
the duration of a request handler.
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


SLACK_INTEGRATION_ID = "slack"
_AAD = SLACK_INTEGRATION_ID.encode("utf-8")


class SlackInstallationError(Exception):
    """Unable to resolve / decrypt a Slack installation."""


@dataclass(frozen=True)
class SlackInstallation:
    tenant_id: str
    installation_id: str
    team_id: str
    bot_token: str
    signing_secret: str
    default_channel: Optional[str]
    status: str


class SlackInstallationLoader:
    """Caches resolved installations briefly to avoid re-decrypt churn."""

    def __init__(
        self,
        db: Database,
        envelope: EnvelopeService,
        *,
        cache_ttl_seconds: int = 60,
    ) -> None:
        self._db = db
        self._envelope = envelope
        self._cache: dict[str, tuple[SlackInstallation, float]] = {}
        self._ttl = cache_ttl_seconds

    # ── Public API ──────────────────────────────────────────────

    async def for_tenant(self, tenant_id: str) -> SlackInstallation:
        installation = self._cached(tenant_id)
        if installation is not None:
            return installation
        installation = await self._load(tenant_id)
        self._cache_put(tenant_id, installation)
        return installation

    async def for_team_id(self, team_id: str) -> SlackInstallation:
        """Reverse lookup: Slack team_id → tenant installation.

        Slack webhooks arrive with team_id, not tenant_id. We resolve
        by scanning ``integration_installations`` for a row whose
        ``config.team_id`` matches.
        """
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.integration_id
                        == SLACK_INTEGRATION_ID,
                        integration_installations.c.status.in_(
                            ("connected", "degraded")
                        ),
                        integration_installations.c.config["team_id"].astext == team_id,
                    )
                )
            ).mappings().all()
        if not rows:
            raise SlackInstallationError(
                f"no Slack installation found for team_id={team_id}"
            )
        if len(rows) > 1:
            logger.warning(
                "slack.multiple_installations_for_team team_id=%s count=%d "
                "— using first",
                team_id,
                len(rows),
            )
        row = rows[0]
        return await self._materialise(row)

    def invalidate(self, tenant_id: str) -> None:
        self._cache.pop(tenant_id, None)

    # ── Internals ───────────────────────────────────────────────

    def _cached(self, tenant_id: str) -> Optional[SlackInstallation]:
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        installation, expires_at = entry
        import time

        if time.monotonic() >= expires_at:
            self._cache.pop(tenant_id, None)
            return None
        return installation

    def _cache_put(
        self, tenant_id: str, installation: SlackInstallation
    ) -> None:
        import time

        self._cache[tenant_id] = (
            installation,
            time.monotonic() + self._ttl,
        )

    async def _load(self, tenant_id: str) -> SlackInstallation:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.tenant_id == tenant_id,
                        integration_installations.c.integration_id
                        == SLACK_INTEGRATION_ID,
                    )
                )
            ).mappings().first()
        if row is None:
            raise SlackInstallationError(
                f"no Slack installation for tenant_id={tenant_id}"
            )
        return await self._materialise(row)

    async def _materialise(self, row) -> SlackInstallation:
        if row["status"] not in ("connected", "degraded"):
            raise SlackInstallationError(
                f"Slack installation in status={row['status']!r}"
            )
        cipher = row["encrypted_credentials"]
        if not cipher:
            raise SlackInstallationError(
                "Slack installation has no encrypted_credentials"
            )
        try:
            blob = EnvelopeBlob.from_bytes(bytes(cipher))
            plaintext = await self._envelope.decrypt(
                row["tenant_id"], blob, expected_aad=_AAD
            )
        except EnvelopeError as exc:
            raise SlackInstallationError(
                f"failed to decrypt Slack credentials: {exc}"
            ) from exc

        try:
            credentials = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SlackInstallationError(
                f"credentials blob is not valid JSON: {exc}"
            ) from exc

        bot_token = credentials.get("bot_token") or credentials.get("access_token")
        signing_secret = credentials.get("signing_secret")
        if not isinstance(bot_token, str) or not bot_token:
            raise SlackInstallationError(
                "credentials missing 'bot_token' / 'access_token'"
            )
        if not isinstance(signing_secret, str) or not signing_secret:
            raise SlackInstallationError(
                "credentials missing 'signing_secret'"
            )

        config = row["config"] or {}
        team_id = ""
        if isinstance(config, dict):
            team_id = str(config.get("team_id") or "")
        default_channel = None
        if isinstance(config, dict):
            dc = config.get("default_channel")
            if isinstance(dc, str) and dc:
                default_channel = dc

        return SlackInstallation(
            tenant_id=row["tenant_id"],
            installation_id=row["installation_id"],
            team_id=team_id,
            bot_token=bot_token,
            signing_secret=signing_secret,
            default_channel=default_channel,
            status=row["status"],
        )
