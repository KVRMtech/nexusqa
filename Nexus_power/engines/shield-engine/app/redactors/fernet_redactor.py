"""
Shield Engine — Fernet-encrypted Redaction, Storage & Audit.

Provides:
  - ``RedactionStore``: Redis-backed, Fernet-encrypted PII mapping persistence
  - ``PIIRedactor``: Token-based PII replacement with reversible de-redaction
  - ``ShieldAuditLog``: Compliance audit trail for every PII touch
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Default path for persisted Fernet key (inside container volume)
_DEFAULT_KEY_PATH = os.environ.get(
    "SHIELD_KEY_FILE",
    "/data/shield/.fernet_key",
)


# ─── Redaction Store (Redis-backed, encrypted) ────────────────

class RedactionStore:
    """
    Persistent encrypted store for PII redaction mappings.

    Primary: Redis hashes with Fernet-encrypted values and configurable TTL.
    Fallback: In-memory dict (logged warning on startup).
    """

    def __init__(self, config):
        self._config = config
        self._fallback: dict[str, dict[str, str]] = {}
        self._redis = None
        self._fernet: Optional[Fernet] = None

        # Derive or generate Fernet encryption key
        key = getattr(config, "shield_encryption_key", "")
        if key:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        else:
            # Attempt to load a previously persisted key from disk
            key_path = Path(
                getattr(config, "shield_key_file", _DEFAULT_KEY_PATH)
            )
            if key_path.exists():
                stored = key_path.read_text().strip()
                self._fernet = Fernet(stored.encode())
                logger.info(
                    "Loaded persisted Fernet key from %s", key_path
                )
            else:
                # Generate a new key and persist it
                generated = Fernet.generate_key()
                try:
                    key_path.parent.mkdir(parents=True, exist_ok=True)
                    key_path.write_bytes(generated)
                    # Restrict to owner-only read/write
                    key_path.chmod(0o600)
                    logger.warning(
                        "SHIELD_ENCRYPTION_KEY not set — generated and persisted key to %s. "
                        "Set SHIELD_ENCRYPTION_KEY for explicit control.",
                        key_path,
                    )
                except OSError as exc:
                    logger.warning(
                        "SHIELD_ENCRYPTION_KEY not set and could not persist key to %s: %s. "
                        "Key is EPHEMERAL — mappings will be unrecoverable after restart.",
                        key_path,
                        exc,
                    )
                self._fernet = Fernet(generated)

    async def connect(self):
        """Connect to Redis. Falls back to in-memory on failure."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.Redis(
                host=self._config.redis_host,
                port=self._config.redis_port,
                password=self._config.redis_password or None,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("Shield RedactionStore connected to Redis")
        except Exception as exc:
            logger.warning(
                "Redis unavailable for Shield redaction store — using in-memory fallback: %s",
                exc,
            )
            self._redis = None

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    async def save(self, mapping_id: str, mapping: dict[str, str]):
        """Persist a redaction mapping (encrypted)."""
        encrypted = {k: self._encrypt(v) for k, v in mapping.items()}
        if not encrypted:
            return  # nothing to persist — no PII entities found
        if self._redis:
            try:
                await self._redis.hset(f"shield:map:{mapping_id}", mapping=encrypted)
                ttl = getattr(self._config, "shield_mapping_ttl_seconds", 0)
                if ttl > 0:
                    await self._redis.expire(
                        f"shield:map:{mapping_id}", ttl,
                    )
                return
            except Exception as exc:
                logger.error("Redis save failed, falling back to memory: %s", exc)
        self._fallback[mapping_id] = encrypted

    async def load(self, mapping_id: str) -> Optional[dict[str, str]]:
        """Retrieve and decrypt a redaction mapping."""
        encrypted: Optional[dict] = None
        if self._redis:
            try:
                encrypted = await self._redis.hgetall(f"shield:map:{mapping_id}")
                if not encrypted:
                    encrypted = None
            except Exception as exc:
                logger.error("Redis load failed, trying fallback: %s", exc)
        if encrypted is None:
            encrypted = self._fallback.get(mapping_id)
        if encrypted is None:
            return None
        try:
            return {k: self._decrypt(v) for k, v in encrypted.items()}
        except Exception:
            return None


# ─── PII Redactor ──────────────────────────────────────────────

class PIIRedactor:
    """
    Replaces PII entities with safe tokens.
    Stores the mapping via RedactionStore for reversible de-redaction.
    """

    def __init__(self, store: RedactionStore):
        self._store = store

    async def redact(
        self,
        text: str,
        entities: list[dict],
    ) -> tuple[str, str, dict[str, str]]:
        """
        Replace PII entities with tokens.

        Returns
        -------
        tuple[str, str, dict]
            ``(safe_text, mapping_id, mapping)``
        """
        mapping_id = str(uuid.uuid4())
        mapping: dict[str, str] = {}
        counters: dict[str, int] = {}

        # Process from end to start so positions don't shift
        safe_text = text
        for entity in sorted(entities, key=lambda e: e["start"], reverse=True):
            pii_type = entity["type"]

            counters.setdefault(pii_type, 0)
            counters[pii_type] += 1
            token = f"[{pii_type}_{counters[pii_type]}]"

            mapping[token] = entity["value"]
            safe_text = (
                safe_text[: entity["start"]] + token + safe_text[entity["end"] :]
            )

        await self._store.save(mapping_id, mapping)
        return safe_text, mapping_id, mapping

    async def reveal(self, mapping_id: str) -> Optional[dict[str, str]]:
        """Retrieve the original PII mapping. Returns None if not found."""
        return await self._store.load(mapping_id)

    async def restore(self, safe_text: str, mapping_id: str) -> Optional[str]:
        """Restore original text from a redacted version."""
        mapping = await self._store.load(mapping_id)
        if not mapping:
            return None

        text = safe_text
        for token, original in mapping.items():
            text = text.replace(token, original)
        return text


# ─── Audit Logger ──────────────────────────────────────────────

class ShieldAuditLog:
    """
    Records every PII detection and redaction for compliance.

    Uses Redis list for persistence (compliance-critical data).
    Falls back to in-memory list if Redis is unavailable.
    """

    _log: list[dict] = []
    _redis = None
    _connected = False
    _key = "nexus:shield:audit_log"

    @classmethod
    async def connect(
        cls,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        redis_db: int = 2,
    ):
        """Connect to Redis for persistent audit storage."""
        try:
            import redis.asyncio as aioredis
            cls._redis = aioredis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password or None,
                db=redis_db,
                decode_responses=True,
            )
            await cls._redis.ping()
            cls._connected = True
        except Exception:
            cls._redis = None
            cls._connected = False

    @classmethod
    async def record(
        cls,
        action: str,
        tenant_id: str,
        user_id: Optional[str],
        mapping_id: Optional[str],
        entity_count: int,
        entity_types: list[str],
    ):
        """Record an audit entry."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "mapping_id": mapping_id,
            "entity_count": entity_count,
            "entity_types": entity_types,
        }
        if cls._connected and cls._redis:
            try:
                await cls._redis.lpush(cls._key, json.dumps(entry, default=str))
                await cls._redis.ltrim(cls._key, 0, 9999)
                return
            except Exception:
                pass
        cls._log.append(entry)

    @classmethod
    async def get_log(
        cls,
        tenant_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieve audit log entries, optionally filtered by tenant."""
        logs: list[dict] = []
        if cls._connected and cls._redis:
            try:
                raw_entries = await cls._redis.lrange(cls._key, 0, limit * 2 - 1)
                for raw in raw_entries:
                    entry = json.loads(raw)
                    if tenant_id and entry.get("tenant_id") != tenant_id:
                        continue
                    logs.append(entry)
                    if len(logs) >= limit:
                        break
                return logs
            except Exception:
                pass
        # Fallback to in-memory
        fallback = cls._log
        if tenant_id:
            fallback = [l for l in fallback if l["tenant_id"] == tenant_id]
        return fallback[-limit:]
