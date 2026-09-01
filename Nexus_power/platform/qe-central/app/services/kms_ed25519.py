"""Cloud KMS-native Ed25519 signing for attestation proofs.

The private half is created and retained by Cloud KMS.  This small adapter has
no database dependency and intentionally lazy-imports the Google client so unit
tests and non-GCP development environments do not acquire a cloud SDK merely by
importing the attestation service.
"""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class NativeKmsError(RuntimeError):
    """The KMS signing authority is unavailable or not an Ed25519 key."""


class GcpEd25519Signer:
    """A synchronous adapter for one ``EC_SIGN_ED25519`` CryptoKeyVersion.

    Cloud KMS's asymmetric-sign API accepts raw data for this algorithm and
    returns normal Ed25519 signature bytes.  The existing verifier therefore
    remains byte-for-byte unchanged.
    """

    def __init__(self, key_version: str, *, client: Any | None = None) -> None:
        self.key_version = str(key_version or "").strip()
        if not self.key_version:
            raise NativeKmsError("a Cloud KMS CryptoKeyVersion name is required")
        if client is None:
            try:
                from google.cloud import kms
                client = kms.KeyManagementServiceClient()
            except Exception as exc:  # no local fallback for a signing root
                raise NativeKmsError(
                    "google-cloud-kms client is unavailable for native Ed25519 signing"
                ) from exc
        self._client = client

    def public_key_b64(self) -> str:
        try:
            response = self._client.get_public_key(name=self.key_version)
            pem = bytes(response.pem, "utf-8") if isinstance(response.pem, str) else response.pem
            key = serialization.load_pem_public_key(pem)
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("KMS public key is not Ed25519")
            raw = key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        except NativeKmsError:
            raise
        except Exception as exc:
            raise NativeKmsError(
                "could not read the native Ed25519 KMS public key"
            ) from exc
        return base64.b64encode(raw).decode("ascii")

    def sign(self, payload: bytes) -> str:
        try:
            response = self._client.asymmetric_sign(
                request={"name": self.key_version, "data": bytes(payload)}
            )
            signature = bytes(response.signature)
        except Exception as exc:
            raise NativeKmsError("Cloud KMS refused the Ed25519 sign operation") from exc
        if len(signature) != 64:
            raise NativeKmsError("Cloud KMS returned a non-Ed25519 signature")
        return base64.b64encode(signature).decode("ascii")
