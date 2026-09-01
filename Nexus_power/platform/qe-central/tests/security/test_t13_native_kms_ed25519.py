"""Team F T1.3 — KMS-native Ed25519 keeps the private key out of qe-central."""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.kms_ed25519 import GcpEd25519Signer
from app.services.signing import canonical_bytes, verify_signature


class _Response:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Kms:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.calls: list[dict] = []

    def get_public_key(self, *, name):
        self.calls.append({"get_public_key": name})
        pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return _Response(pem=pem)

    def asymmetric_sign(self, *, request):
        self.calls.append(request)
        return _Response(signature=self.private.sign(request["data"]))


def test_t13_kms_native_signature_verifies_with_the_existing_ed25519_verifier():
    client = _Kms()
    signer = GcpEd25519Signer("projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1",
                              client=client)
    payload = {"tenant_id": "tenant-a", "crawl_id": "c" * 32}
    signature = signer.sign(canonical_bytes(payload))
    assert verify_signature(signer.public_key_b64(), payload, signature)
    sign_call = next(call for call in client.calls if "data" in call)
    assert sign_call["data"] == canonical_bytes(payload)


def test_t13_native_signer_uses_a_crypto_key_version_not_a_private_key_value():
    signer = GcpEd25519Signer("projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/7",
                              client=_Kms())
    assert "private" not in vars(signer)
    assert signer.key_version.endswith("cryptoKeyVersions/7")
