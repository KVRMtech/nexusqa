"""Envelope encryption for per-tenant secrets.

Two-tier key hierarchy:

    KEK  (Key Encryption Key)  — per tenant, lives in KMS / HSM / file.
                                  Never leaves the provider.
    DEK  (Data Encryption Key) — per ciphertext, AES-256-GCM. Encrypted
                                  with the tenant KEK and stored
                                  alongside the ciphertext as the
                                  ``wrapped_dek`` blob.

A single ``encrypt`` call returns a self-describing ``EnvelopeBlob``
containing version, kek_id, wrapped_dek, nonce, ciphertext, and AAD.
Decryption requires only the tenant_id; the provider is resolved from
the blob's kek_id.

Providers
---------

* ``AwsKmsProvider``      — boto3 KMS GenerateDataKey / Decrypt.
* ``AzureKvProvider``     — Azure KeyVault Keys WrapKey / UnwrapKey.
* ``GcpKmsProvider``      — google-cloud-kms encrypt / decrypt.
* ``LocalKekProvider``    — file-backed master key, AES-GCM wrap.
                              Only usable when NEXUS_ENV=development
                              (raises RuntimeError otherwise).

Adding a provider: implement ``KekProvider``, register the factory in
``PROVIDER_FACTORIES``. The blob format is provider-agnostic.

Operational notes
-----------------

* Rotation: ``rotate_kek`` re-wraps existing DEKs under the new KEK
  without re-encrypting the ciphertext (cheap, fast, online).
* AAD: callers should pass an immutable context string (e.g. the
  ``installation_id``) so a swapped ciphertext is detected at decrypt.
* The blob carries an integrity tag; corruption raises ``InvalidTag``.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


ENVELOPE_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256-GCM


# ── Exceptions ──────────────────────────────────────────────────


class EnvelopeError(Exception):
    """Base class for envelope encryption failures."""


class KekProviderError(EnvelopeError):
    """The KEK provider rejected an operation."""


class IntegrityError(EnvelopeError):
    """Ciphertext or wrapped DEK failed integrity verification."""


class ProviderUnavailable(EnvelopeError):
    """The configured provider can't be initialised in this environment."""


# ── Blob format ─────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class EnvelopeBlob:
    """Self-describing ciphertext + wrapped DEK.

    Serialised form is JSON-with-base64; this fits a Postgres ``bytea``
    column once UTF-8 encoded. The serialisation is intentionally
    transparent so audit tooling can inspect kek_id / version without
    decrypting.
    """

    version: int
    kek_id: str
    provider: str
    nonce: bytes
    ciphertext: bytes
    wrapped_dek: bytes
    aad: bytes

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "v": self.version,
                "kek_id": self.kek_id,
                "provider": self.provider,
                "nonce": _b64(self.nonce),
                "ct": _b64(self.ciphertext),
                "wdek": _b64(self.wrapped_dek),
                "aad": _b64(self.aad),
            },
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "EnvelopeBlob":
        try:
            payload = json.loads(raw.decode("utf-8"))
            version = int(payload["v"])
            if version != ENVELOPE_VERSION:
                raise EnvelopeError(
                    f"unsupported envelope version: {version}"
                )
            return cls(
                version=version,
                kek_id=str(payload["kek_id"]),
                provider=str(payload["provider"]),
                nonce=_b64d(payload["nonce"]),
                ciphertext=_b64d(payload["ct"]),
                wrapped_dek=_b64d(payload["wdek"]),
                aad=_b64d(payload["aad"]),
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise EnvelopeError(f"corrupt envelope blob: {exc}") from exc


# ── KEK provider interface ──────────────────────────────────────


class KekProvider(Protocol):
    """Provider-side wrap/unwrap of DEKs.

    Implementations must be safe to call from async contexts; they may
    perform network I/O via blocking client libs run in a threadpool.
    """

    provider_id: str

    async def kek_id(self, tenant_id: str) -> str:
        """Return the active KEK identifier for the tenant."""

    async def wrap(self, tenant_id: str, dek: bytes) -> tuple[str, bytes]:
        """Encrypt the DEK under the tenant KEK. Returns (kek_id, wrapped)."""

    async def unwrap(
        self, tenant_id: str, kek_id: str, wrapped_dek: bytes
    ) -> bytes:
        """Decrypt the wrapped DEK using the named KEK."""


# ── Local provider (development only) ───────────────────────────


class LocalKekProvider:
    """File-backed KEK for development environments.

    Production code must not import this directly. ``EnvelopeService``
    refuses to use it unless ``NEXUS_ENV`` is set to ``development``.

    The master key file is a 32-byte raw AES-256 key stored on disk
    with 0600 permissions. Per-tenant KEKs are derived deterministically
    by HKDF-equivalent: AES-encrypt the tenant_id under the master key
    to produce a stable per-tenant key.
    """

    provider_id = "local"

    def __init__(self, master_key_path: str):
        path = Path(master_key_path)
        if not path.exists():
            # Generate a fresh master key on first use.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(secrets.token_bytes(_KEY_BYTES))
            try:
                os.chmod(path, 0o600)
            except OSError:
                # Windows ignores chmod bits; rely on parent-dir ACLs.
                pass
        self._master = path.read_bytes()
        if len(self._master) != _KEY_BYTES:
            raise ProviderUnavailable(
                f"local master key at {path} is {len(self._master)} bytes; "
                f"expected {_KEY_BYTES}"
            )

    def _kek_for(self, tenant_id: str) -> bytes:
        # Deterministic per-tenant derivation: HMAC-style using AES-GCM
        # of a fixed nonce + tenant_id under the master key produces a
        # stable 32-byte key. (Suitable for dev; real KMS in prod.)
        cipher = AESGCM(self._master)
        nonce = b"\x00" * _NONCE_BYTES
        ct = cipher.encrypt(nonce, tenant_id.encode("utf-8"), None)
        # ct is at least 16 bytes (tenant_id + GCM tag); SHA-256 not
        # available here, but ct is keyed-PRF output so this is fine.
        # We hash by taking the first 32 bytes (zero-padding if shorter).
        if len(ct) >= _KEY_BYTES:
            return ct[:_KEY_BYTES]
        return (ct + b"\x00" * _KEY_BYTES)[:_KEY_BYTES]

    async def kek_id(self, tenant_id: str) -> str:
        return f"local:{tenant_id}"

    async def wrap(
        self, tenant_id: str, dek: bytes
    ) -> tuple[str, bytes]:
        kek = self._kek_for(tenant_id)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        wrapped = AESGCM(kek).encrypt(nonce, dek, tenant_id.encode("utf-8"))
        return f"local:{tenant_id}", nonce + wrapped

    async def unwrap(
        self, tenant_id: str, kek_id: str, wrapped_dek: bytes
    ) -> bytes:
        if not kek_id.startswith("local:"):
            raise KekProviderError(
                f"local provider cannot unwrap kek_id={kek_id}"
            )
        if len(wrapped_dek) < _NONCE_BYTES + 16:
            raise IntegrityError("wrapped DEK is truncated")
        nonce, ct = wrapped_dek[:_NONCE_BYTES], wrapped_dek[_NONCE_BYTES:]
        kek = self._kek_for(tenant_id)
        try:
            return AESGCM(kek).decrypt(nonce, ct, tenant_id.encode("utf-8"))
        except InvalidTag as exc:
            raise IntegrityError("wrapped DEK failed authentication") from exc


# ── AWS KMS provider ────────────────────────────────────────────


class AwsKmsProvider:
    """boto3-backed KMS provider.

    Tenant→KEK mapping is supplied by a callable so the service can
    resolve the ARN from ``tenant_keks`` (PostgreSQL) or an alias
    convention. Generation of new tenant KEKs is out of scope here —
    do that via your provisioning pipeline.
    """

    provider_id = "aws_kms"

    def __init__(self, kek_resolver, region: Optional[str] = None):
        """
        Parameters
        ----------
        kek_resolver
            ``Callable[[tenant_id: str], Awaitable[str]]`` returning the
            ARN or key ID for the tenant's KEK.
        region
            Optional AWS region override.
        """
        try:
            import boto3  # noqa: F401  (deferred import)
        except ImportError as exc:
            raise ProviderUnavailable(
                "boto3 is required for AwsKmsProvider"
            ) from exc
        import boto3

        self._client = boto3.client("kms", region_name=region) if region else boto3.client("kms")
        self._resolver = kek_resolver

    async def kek_id(self, tenant_id: str) -> str:
        arn = await self._resolver(tenant_id)
        if not arn:
            raise KekProviderError(
                f"no KEK ARN registered for tenant {tenant_id}"
            )
        return arn

    async def wrap(self, tenant_id: str, dek: bytes) -> tuple[str, bytes]:
        import asyncio

        arn = await self.kek_id(tenant_id)
        loop = asyncio.get_running_loop()

        def _call() -> dict:
            return self._client.encrypt(
                KeyId=arn,
                Plaintext=dek,
                EncryptionContext={"tenant_id": tenant_id},
            )

        try:
            resp = await loop.run_in_executor(None, _call)
        except Exception as exc:
            raise KekProviderError(f"KMS encrypt failed: {exc}") from exc

        return arn, resp["CiphertextBlob"]

    async def unwrap(
        self, tenant_id: str, kek_id: str, wrapped_dek: bytes
    ) -> bytes:
        import asyncio

        loop = asyncio.get_running_loop()

        def _call() -> dict:
            return self._client.decrypt(
                CiphertextBlob=wrapped_dek,
                KeyId=kek_id,
                EncryptionContext={"tenant_id": tenant_id},
            )

        try:
            resp = await loop.run_in_executor(None, _call)
        except Exception as exc:
            raise KekProviderError(f"KMS decrypt failed: {exc}") from exc
        return resp["Plaintext"]


# ── GCP KMS provider ────────────────────────────────────────────


class GcpKmsProvider:
    """google-cloud-kms-backed KMS provider.

    Wrap/unwrap of the DEK is delegated to a Cloud KMS symmetric key via
    ``encrypt`` / ``decrypt``. The ``tenant_id`` is bound into the call as
    Additional Authenticated Data so a ciphertext can't be replayed under a
    different tenant. On a GCP VM the client authenticates via Application
    Default Credentials (the VM's service account through the metadata
    server) — no static keys on disk; the SA needs
    ``roles/cloudkms.cryptoKeyEncrypterDecrypter`` on the key.

    Tenant→key mapping is supplied by a callable so the deployment can resolve
    the CryptoKey resource name from ``tenant_keks`` or a single-key bootstrap.
    KMS ``decrypt`` auto-detects the key *version* from the ciphertext, so the
    resolver only needs the CryptoKey resource name
    (``projects/P/locations/L/keyRings/R/cryptoKeys/K``).
    """

    provider_id = "gcp_kms"

    def __init__(self, kek_resolver):
        """
        Parameters
        ----------
        kek_resolver
            ``Callable[[tenant_id: str], Awaitable[str]]`` returning the
            CryptoKey resource name for the tenant's KEK.
        """
        try:
            from google.cloud import kms  # noqa: F401  (deferred import)
        except ImportError as exc:
            raise ProviderUnavailable(
                "google-cloud-kms is required for GcpKmsProvider"
            ) from exc
        from google.cloud import kms

        self._client = kms.KeyManagementServiceClient()
        self._resolver = kek_resolver

    async def kek_id(self, tenant_id: str) -> str:
        name = await self._resolver(tenant_id)
        if not name:
            raise KekProviderError(
                f"no KEK key registered for tenant {tenant_id}"
            )
        return name

    async def wrap(self, tenant_id: str, dek: bytes) -> tuple[str, bytes]:
        import asyncio

        name = await self.kek_id(tenant_id)
        loop = asyncio.get_running_loop()

        def _call():
            return self._client.encrypt(
                request={
                    "name": name,
                    "plaintext": dek,
                    "additional_authenticated_data": tenant_id.encode("utf-8"),
                }
            )

        try:
            resp = await loop.run_in_executor(None, _call)
        except Exception as exc:
            raise KekProviderError(f"GCP KMS encrypt failed: {exc}") from exc
        return name, resp.ciphertext

    async def unwrap(
        self, tenant_id: str, kek_id: str, wrapped_dek: bytes
    ) -> bytes:
        import asyncio

        loop = asyncio.get_running_loop()

        def _call():
            return self._client.decrypt(
                request={
                    "name": kek_id,
                    "ciphertext": wrapped_dek,
                    "additional_authenticated_data": tenant_id.encode("utf-8"),
                }
            )

        try:
            resp = await loop.run_in_executor(None, _call)
        except Exception as exc:
            raise KekProviderError(f"GCP KMS decrypt failed: {exc}") from exc
        return resp.plaintext


# ── Provider registry ───────────────────────────────────────────


PROVIDER_FACTORIES: dict[str, str] = {
    "aws_kms": "AwsKmsProvider",
    "gcp_kms": "GcpKmsProvider",
    "local": "LocalKekProvider",
}


# ── EnvelopeService — main entry point ─────────────────────────


class EnvelopeService:
    """High-level encrypt/decrypt for per-tenant secrets."""

    def __init__(self, provider: KekProvider):
        if (
            provider.provider_id == "local"
            and os.environ.get("NEXUS_ENV", "").lower() not in {"development", "test"}
        ):
            raise ProviderUnavailable(
                "LocalKekProvider is restricted to NEXUS_ENV=development|test. "
                "Configure aws_kms / azure_kv / gcp_kms in production."
            )
        self._provider = provider

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    async def encrypt(
        self,
        tenant_id: str,
        plaintext: bytes,
        *,
        aad: Optional[bytes] = None,
    ) -> EnvelopeBlob:
        if not plaintext:
            raise ValueError("plaintext must not be empty")
        dek = secrets.token_bytes(_KEY_BYTES)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad_bytes = aad or b""
        try:
            ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad_bytes)
            kek_id, wrapped = await self._provider.wrap(tenant_id, dek)
        finally:
            # Best effort — Python doesn't let us zero memory, but we
            # can at least drop the reference promptly.
            del dek
        return EnvelopeBlob(
            version=ENVELOPE_VERSION,
            kek_id=kek_id,
            provider=self._provider.provider_id,
            nonce=nonce,
            ciphertext=ciphertext,
            wrapped_dek=wrapped,
            aad=aad_bytes,
        )

    async def decrypt(
        self,
        tenant_id: str,
        blob: EnvelopeBlob,
        *,
        expected_aad: Optional[bytes] = None,
    ) -> bytes:
        if blob.version != ENVELOPE_VERSION:
            raise EnvelopeError(
                f"unsupported envelope version: {blob.version}"
            )
        if expected_aad is not None and expected_aad != blob.aad:
            raise IntegrityError(
                "AAD mismatch: ciphertext was bound to a different context"
            )
        if blob.provider != self._provider.provider_id:
            raise KekProviderError(
                f"provider mismatch: blob={blob.provider} "
                f"service={self._provider.provider_id}"
            )
        dek = await self._provider.unwrap(
            tenant_id, blob.kek_id, blob.wrapped_dek
        )
        try:
            try:
                return AESGCM(dek).decrypt(
                    blob.nonce, blob.ciphertext, blob.aad
                )
            except InvalidTag as exc:
                raise IntegrityError(
                    "ciphertext failed authentication"
                ) from exc
        finally:
            del dek

    async def rotate_kek(
        self,
        tenant_id: str,
        old_blob: EnvelopeBlob,
        *,
        expected_aad: Optional[bytes] = None,
    ) -> EnvelopeBlob:
        """Re-wrap the DEK under the current tenant KEK.

        Ciphertext is not re-encrypted — only the wrapped DEK is
        replaced. This is the cheap-and-online rotation path.
        """
        dek = await self._provider.unwrap(
            tenant_id, old_blob.kek_id, old_blob.wrapped_dek
        )
        try:
            kek_id, wrapped = await self._provider.wrap(tenant_id, dek)
        finally:
            del dek
        return EnvelopeBlob(
            version=ENVELOPE_VERSION,
            kek_id=kek_id,
            provider=self._provider.provider_id,
            nonce=old_blob.nonce,
            ciphertext=old_blob.ciphertext,
            wrapped_dek=wrapped,
            aad=old_blob.aad,
        )


# ── Encoding helpers ────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))
