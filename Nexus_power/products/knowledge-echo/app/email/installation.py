"""Resolve email installations.

Credential blob shape::

    {
      "sns_topic_arn":        "arn:aws:sns:...",   // optional pin
      "from_address":         "echoes@nexus.example.com",
      "reply_to":             "noreply@nexus.example.com",
      "ses_configuration_set": "nexus-echo",        // optional
      "aws_region":           "us-east-1",
      "aws_access_key_id":    "AKIA...",            // optional if IAM role used
      "aws_secret_access_key":"...",                // optional if IAM role used
      "aws_session_token":    "...",                // optional
      "outbound_signing_secret": "..."              // optional, for the
                                                    //   reply-vote handler
    }
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


EMAIL_INTEGRATION_ID = "email"
_AAD = EMAIL_INTEGRATION_ID.encode("utf-8")


class EmailInstallationError(Exception):
    """Unable to resolve / decrypt the email installation."""


@dataclass(frozen=True)
class EmailInstallation:
    tenant_id: str
    installation_id: str
    sns_topic_arn: Optional[str]
    from_address: str
    reply_to: Optional[str]
    ses_configuration_set: Optional[str]
    aws_region: str
    aws_access_key_id: Optional[str]
    aws_secret_access_key: Optional[str]
    aws_session_token: Optional[str]
    outbound_signing_secret: Optional[str]
    status: str


class EmailInstallationLoader:
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
        self._cache: dict[str, tuple[EmailInstallation, float]] = {}

    async def for_tenant(self, tenant_id: str) -> EmailInstallation:
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

    async def for_topic_arn(self, topic_arn: str) -> EmailInstallation:
        """Reverse lookup: SNS topic → tenant.

        The SNS handler receives a topic_arn before it knows the tenant;
        this resolver scans installations for a match. RLS does not
        scope us here (no current tenant context), so we filter by
        integration_id + status + JSON config key.
        """
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.integration_id
                        == EMAIL_INTEGRATION_ID,
                        integration_installations.c.status.in_(
                            ("connected", "degraded")
                        ),
                        integration_installations.c.config["sns_topic_arn"].astext == topic_arn,
                    )
                )
            ).mappings().all()
        if not rows:
            raise EmailInstallationError(
                f"no email installation for sns_topic_arn={topic_arn}"
            )
        if len(rows) > 1:
            logger.warning(
                "email.multiple_installations_for_topic count=%d", len(rows)
            )
        return await self._materialise(rows[0])

    def invalidate(self, tenant_id: str) -> None:
        self._cache.pop(tenant_id, None)

    async def _load(self, tenant_id: str) -> EmailInstallation:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(integration_installations).where(
                        integration_installations.c.tenant_id == tenant_id,
                        integration_installations.c.integration_id
                        == EMAIL_INTEGRATION_ID,
                    )
                )
            ).mappings().first()
        if row is None:
            raise EmailInstallationError(
                f"no email installation for tenant_id={tenant_id}"
            )
        return await self._materialise(row)

    async def _materialise(self, row) -> EmailInstallation:
        if row["status"] not in ("connected", "degraded"):
            raise EmailInstallationError(
                f"email installation in status={row['status']!r}"
            )
        cipher = row["encrypted_credentials"]
        if not cipher:
            raise EmailInstallationError(
                "email installation has no encrypted_credentials"
            )
        try:
            blob = EnvelopeBlob.from_bytes(bytes(cipher))
            plaintext = await self._envelope.decrypt(
                row["tenant_id"], blob, expected_aad=_AAD
            )
        except EnvelopeError as exc:
            raise EmailInstallationError(
                f"failed to decrypt email credentials: {exc}"
            ) from exc

        try:
            creds = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise EmailInstallationError(
                f"credentials blob is not valid JSON: {exc}"
            ) from exc

        from_address = creds.get("from_address")
        if not isinstance(from_address, str) or "@" not in from_address:
            raise EmailInstallationError(
                "credentials missing 'from_address'"
            )
        aws_region = creds.get("aws_region")
        if not isinstance(aws_region, str) or not aws_region:
            raise EmailInstallationError(
                "credentials missing 'aws_region'"
            )

        config = row["config"] or {}
        sns_topic_arn = (
            config.get("sns_topic_arn")
            if isinstance(config, dict)
            else None
        )

        return EmailInstallation(
            tenant_id=row["tenant_id"],
            installation_id=row["installation_id"],
            sns_topic_arn=sns_topic_arn,
            from_address=from_address,
            reply_to=creds.get("reply_to"),
            ses_configuration_set=creds.get("ses_configuration_set"),
            aws_region=aws_region,
            aws_access_key_id=creds.get("aws_access_key_id"),
            aws_secret_access_key=creds.get("aws_secret_access_key"),
            aws_session_token=creds.get("aws_session_token"),
            outbound_signing_secret=creds.get("outbound_signing_secret"),
            status=row["status"],
        )
