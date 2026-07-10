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


def _sdk_service():
    """Return an SDK envelope service if importable, else None."""
    try:  # pragma: no cover - exercised on the VM where the SDK is installed
        from nexus_sdk.crypto.envelope import EnvelopeService  # type: ignore
        return EnvelopeService.from_env()
    except Exception:
        return None


def _dev_key() -> bytes:
    """Deterministic development-only key derived from the local KEK path or a
    fixed dev salt. NOT for production (guarded by :func:`_is_dev`)."""
    seed = os.environ.get("NEXUS_LOCAL_KEK_PATH", "repo-intel-dev-kek")
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def seal_token(token: str, *, aad: str) -> tuple[bytes, str]:
    """Return ``(ciphertext_blob, kek_id)`` for a token. Refuses plaintext.

    ``aad`` (connection_id) binds the ciphertext so it cannot be replayed
    under another connection.
    """
    if not token:
        raise ValueError("empty token")
    # Fail-closed: a dev-grade local KEK can never seal a client token in a
    # deployed (staging/production) environment — even if the SDK service loads.
    _refuse_local_kek_in_deployed_env()
    svc = _sdk_service()
    if svc is not None:
        blob = svc.encrypt(token.encode("utf-8"), aad=aad.encode("utf-8"))
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


def open_token(blob: bytes, *, aad: str) -> str:
    """Reverse :func:`seal_token`."""
    if blob is None:
        raise ValueError("no ciphertext")
    if bytes(blob).startswith(b"DEVSEAL1:"):
        if not _is_dev():
            raise EnvelopeUnavailable("dev-sealed token cannot be opened outside development")
        key = _dev_key()
        aad_key = hashlib.sha256((aad + "|aad").encode()).digest()
        sealed = base64.b64decode(bytes(blob)[len(b"DEVSEAL1:"):])
        return _xor(sealed, _xor(key, aad_key)).decode("utf-8", errors="replace")
    svc = _sdk_service()
    if svc is None:
        raise EnvelopeUnavailable("SDK envelope service unavailable to open token")
    from nexus_sdk.crypto.envelope import EnvelopeBlob  # type: ignore
    return svc.decrypt(EnvelopeBlob.from_bytes(bytes(blob)), aad=aad.encode("utf-8")).decode("utf-8")
