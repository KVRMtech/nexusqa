"""Ed25519 signing primitives for governance (Phase 6).

Honest framing (do not overclaim): a signature made with a server-minted, KMS-sealed
key proves ATTRIBUTION and TAMPER-EVIDENCE — that the platform recorded this exact
payload under this principal. It is NOT vendor-independent non-repudiation, because the
key's custody is the vendor's; the vendor-independent guarantee is the hash-chain
RE-DERIVATION a regulator can run offline. True non-repudiation requires customer-held /
HSM keys (a supported option), where the private key never exists in vendor infra.

These are pure primitives over a canonical JSON encoding — no DB, no KMS here (the
envelope sealing of the private key lives in the persistence layer). Deterministic and
fully unit-testable.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIG_ALG = "ed25519"


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic canonical encoding — sorted keys, no insignificant whitespace.
    The SAME bytes are hashed/signed and re-derived, so a single differing field fails
    verification."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_key_b64, public_key_b64)`` as raw 32-byte Ed25519 keys, base64.
    The private key is what the persistence layer seals under the tenant KEK."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return _b64e(priv_raw), _b64e(pub_raw)


def public_key_of(private_key_b64: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(_b64d(private_key_b64))
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return _b64e(pub_raw)


def sign_payload(private_key_b64: str, payload: Any) -> str:
    """Detached signature (base64) over the canonical encoding of ``payload``."""
    priv = Ed25519PrivateKey.from_private_bytes(_b64d(private_key_b64))
    return _b64e(priv.sign(canonical_bytes(payload)))


def verify_signature(public_key_b64: str, payload: Any, signature_b64: str) -> bool:
    """True iff ``signature_b64`` is a valid Ed25519 signature by ``public_key_b64``
    over the canonical encoding of ``payload``. Never raises — a malformed key/sig
    returns False (fail-closed for a verifier)."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(_b64d(public_key_b64))
        pub.verify(_b64d(signature_b64), canonical_bytes(payload))
        return True
    except (InvalidSignature, ValueError, TypeError, Exception):
        return False
