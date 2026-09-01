"""Test-only ISSUER for M1.3 platform provisioning proofs.

This file holds the ONLY private key material in the qe-explorer tree, and it
lives under ``tests/`` on purpose: the production service verifies with public
keys and has no signing path at all, so a test that wants a genuine proof has to
bring its own issuer.  That asymmetry is the security property being protected —
if this module could be imported from ``app/``, a compromised explorer could
mint its own authorisation.

``Issuer`` mirrors what ``qe-central``'s provisioner does at environment-
provisioning time (``app/services/signing.py``), so a proof built here is the
same object shape the platform will actually emit.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.attest import CLAIMS_VERSION, SIG_ALG, TrustStore, canonical_bytes, key_id

ISSUER = "nexus-platform-provisioner"
CRAWL_ID = "crawl-m13-0001"
TENANT_ID = "tenant-acme"
TARGET_URL = "https://app.char/apply/coverage"


def now_ms() -> int:
    return int(time.time() * 1000)


class Issuer:
    """A platform attestation issuer: holds the private key, mints proofs."""

    def __init__(self, name: str = ISSUER) -> None:
        self.name = name
        self._priv = Ed25519PrivateKey.generate()
        raw = self._priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        self.public_key_b64 = base64.b64encode(raw).decode("ascii")
        self.kid = key_id(self.public_key_b64)

    # -- signing --------------------------------------------------------------

    def _sign(self, claims: dict) -> dict:
        sig = base64.b64encode(self._priv.sign(canonical_bytes(claims))).decode("ascii")
        return {"claims": claims, "alg": SIG_ALG, "kid": self.kid, "signature": sig}

    def proof_claims(self, **over: Any) -> dict:
        issued = over.pop("issued_at_ms", None) or now_ms()
        claims = {
            "v": CLAIMS_VERSION,
            "proof_id": "prf-" + "0" * 12 + "abcd",
            "issuer": self.name,
            "environment_id": "env-disposable-7f2a",
            "env_kind": "disposable",
            "tenant_id": TENANT_ID,
            "crawl_id": CRAWL_ID,
            "target_origin": "https://app.char",
            "reset_procedure": "terraform destroy && terraform apply",
            "issued_at_ms": issued,
            "expires_at_ms": issued + 3_600_000,
            "max_walk_mutations_per_step": 3,
        }
        claims.update(over)
        return claims

    def proof(self, **over: Any) -> dict:
        return self._sign(self.proof_claims(**over))

    def revocations(self, *, revoked_proof_ids=(), revoked_environment_ids=(),
                    **over: Any) -> dict:
        issued = over.pop("issued_at_ms", None) or now_ms()
        claims = {
            "v": CLAIMS_VERSION,
            "issuer": self.name,
            "issued_at_ms": issued,
            "expires_at_ms": issued + 600_000,
            "revoked_proof_ids": list(revoked_proof_ids),
            "revoked_environment_ids": list(revoked_environment_ids),
        }
        claims.update(over)
        return self._sign(claims)

    def envelope(self, *, proof_over: Optional[dict] = None,
                 revocation_over: Optional[dict] = None) -> dict:
        """The complete ``attestation`` object a dispatch carries."""
        return {"proof": self.proof(**(proof_over or {})),
                "revocations": self.revocations(**(revocation_over or {}))}

    # -- the fleet side -------------------------------------------------------

    def trust(self, **policy: Any) -> TrustStore:
        """A TrustStore that trusts THIS issuer (public key only)."""
        return TrustStore.from_public_keys([self.public_key_b64],
                                           issuer=self.name, **policy)


def tampered(envelope: dict, **claim_over: Any) -> dict:
    """Edit the CLAIMS of a signed proof without re-signing it.

    This is the forgery every negative test needs: the envelope still carries a
    real signature by a real key, and the bytes it covers no longer match."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in envelope.items()}
    out["proof"] = dict(out["proof"])
    claims = dict(out["proof"]["claims"])
    claims.update(claim_over)
    out["proof"]["claims"] = claims
    return out
