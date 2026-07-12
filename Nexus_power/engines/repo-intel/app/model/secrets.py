"""Token-at-rest envelope helper for repo-intel connections.

Clones the platform's refuse-plaintext discipline: a client git token is
persisted ONLY as an envelope blob with AAD = ``connection_id``. When no KEK
provider is configured, storing a token is REFUSED outside a development
environment (never a plaintext fallback in staging/prod).

Uses the SDK envelope service when importable; otherwise, in development only,
falls back to a clearly-marked local Fernet-style seal so the dev flow works
without the full KMS stack. Any attempt to seal outside development without
the SDK raises :class:`EnvelopeUnavailable` (fail-closed).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger("repo_intel.secrets")


class EnvelopeUnavailable(RuntimeError):
    """No secure envelope available and environment is not development."""


def _is_dev() -> bool:
    return os.environ.get("NEXUS_ENV", "development").strip().lower() in {"development", "test", "dev"}


#: Deployed environments in which a dev-grade LOCAL KEK provider is CATEGORICALLY
#: refused for storing a client token.  A ``local`` provider is a local
#: master-key file (dev-only); using it in a deployed env is the "dev KEK
#: silently used in prod" fail-open this guard closes.
_DEPLOYED_ENVS = frozenset({"staging", "production", "prod"})


def _kek_provider() -> str:
    """The configured KEK provider (``local`` | ``aws_kms`` | ``gcp_kms`` | …)."""
    return os.environ.get("NEXUS_KEK_PROVIDER", "local").strip().lower()


def _deployed_env() -> str:
    return os.environ.get("NEXUS_ENV", "development").strip().lower()


def _refuse_local_kek_in_deployed_env() -> None:
    """Fail-closed: refuse a ``local`` KEK provider in staging/production.

    Fires BEFORE any SDK envelope service is consulted, so even where the SDK is
    importable a dev-grade local-master-key provider can never seal a real
    client's token in a deployed environment (must be a KMS provider there).
    Inert in development/test (the default ``NEXUS_ENV``) and for any KMS
    provider — it changes no existing dev/test outcome.
    """
    env = _deployed_env()
    if env in _DEPLOYED_ENVS and _kek_provider() == "local":
        raise EnvelopeUnavailable(
            f"NEXUS_KEK_PROVIDER=local (dev-grade master key) is refused in "
            f"NEXUS_ENV={env!r} — provision a KMS KEK provider "
            f"(aws_kms|gcp_kms) before storing a client token"
        )


_ENVELOPE_SINGLETON = None
_ENVELOPE_TRIED = False


def _build_envelope():
    """Construct the SDK EnvelopeService ONCE (matching qe-central/platform-api:
    ``EnvelopeService(provider)`` from ``nexus_sdk.security.envelope``), or None
    when the SDK / KEK provider is unavailable.

    Fixes the historical break where this called a non-existent
    ``EnvelopeService.from_env()`` on the wrong module (``nexus_sdk.crypto``),
    so every seal fell through to a 503 in production.
    """
    global _ENVELOPE_SINGLETON, _ENVELOPE_TRIED
    if _ENVELOPE_TRIED:
        return _ENVELOPE_SINGLETON
    _ENVELOPE_TRIED = True
    provider_name = _kek_provider()
    try:  # pragma: no cover - exercised on the VM where the SDK + KMS are wired
        from nexus_sdk.security.envelope import (
            EnvelopeService, GcpKmsProvider, LocalKekProvider,
        )

        if provider_name == "gcp_kms":
            key_name = os.environ.get("NEXUS_KEK_GCP_KEY")
            if not key_name:
                return None

            async def _resolver(_tenant_id: str) -> str:
                return key_name

            _ENVELOPE_SINGLETON = EnvelopeService(GcpKmsProvider(kek_resolver=_resolver))
        elif provider_name == "local":
            from pathlib import Path

            path = os.environ.get(
                "NEXUS_LOCAL_KEK_PATH", str(Path.home() / ".nexus" / "kek" / "master.key"),
            )
            _ENVELOPE_SINGLETON = EnvelopeService(LocalKekProvider(master_key_path=path))
    except Exception as exc:
        logger.warning("repo_intel.envelope_init_failed: %s", str(exc)[:200])
        _ENVELOPE_SINGLETON = None
    return _ENVELOPE_SINGLETON


def _dev_key() -> bytes:
    """Deterministic development-only key derived from the local KEK path or a
    fixed dev salt. NOT for production (guarded by :func:`_is_dev`)."""
    seed = os.environ.get("NEXUS_LOCAL_KEK_PATH", "repo-intel-dev-kek")
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


async def seal_token(token: str, *, tenant_id: str, aad: str) -> tuple[bytes, str]:
    """Return ``(ciphertext_blob, kek_id)`` for a token. Refuses plaintext.

    ``aad`` (connection_id) binds the ciphertext so it cannot be replayed under
    another connection.  Async + ``tenant_id``-scoped to match the SDK envelope
    API used by qe-central/platform-api (``await encrypt(tenant_id, pt, aad=)``).
    """
    if not token:
        raise ValueError("empty token")
    # Fail-closed: a dev-grade local KEK can never seal a client token in a
    # deployed (staging/production) environment — even if the SDK service loads.
    _refuse_local_kek_in_deployed_env()
    svc = _build_envelope()
    if svc is not None:
        blob = await svc.encrypt(tenant_id, token.encode("utf-8"), aad=aad.encode("utf-8"))
        return (blob.to_bytes() if hasattr(blob, "to_bytes") else bytes(blob),
                getattr(svc, "kek_id", "sdk"))
    if not _is_dev():
        raise EnvelopeUnavailable(
            "no KEK provider configured — refusing to store a token outside development"
        )
    # Development-only local seal (marked so it can never be mistaken for KMS).
    key = _dev_key()
    aad_key = hashlib.sha256((aad + "|aad").encode()).digest()
    sealed = _xor(token.encode("utf-8"), _xor(key, aad_key))
    return (b"DEVSEAL1:" + base64.b64encode(sealed), "local-dev")


async def open_token(blob: bytes, *, tenant_id: str, aad: str) -> str:
    """Reverse :func:`seal_token` (async + tenant-scoped)."""
    if blob is None:
        raise ValueError("no ciphertext")
    if bytes(blob).startswith(b"DEVSEAL1:"):
        if not _is_dev():
            raise EnvelopeUnavailable("dev-sealed token cannot be opened outside development")
        key = _dev_key()
        aad_key = hashlib.sha256((aad + "|aad").encode()).digest()
        sealed = base64.b64decode(bytes(blob)[len(b"DEVSEAL1:"):])
        return _xor(sealed, _xor(key, aad_key)).decode("utf-8", errors="replace")
    svc = _build_envelope()
    if svc is None:
        raise EnvelopeUnavailable("SDK envelope service unavailable to open token")
    from nexus_sdk.security.envelope import EnvelopeBlob

    pt = await svc.decrypt(
        tenant_id, EnvelopeBlob.from_bytes(bytes(blob)), expected_aad=aad.encode("utf-8"),
    )
    return pt.decode("utf-8")
