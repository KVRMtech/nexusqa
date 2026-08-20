"""Gate 1 / T-WP-02 — THE ISSUER HALF OF THE WALK-PERSISTENCE ATTESTATION.

WHAT WAS MISSING, AND WHY THE FEATURE WAS INERT WITHOUT IT.

``qe-explorer``'s :mod:`app.attest` is a complete, fail-closed VERIFIER: it
refuses a walk mutation unless the dispatch carries an Ed25519-signed
provisioning proof from a configured issuer, plus a separately signed,
currently-valid revocation list.  The explorer holds only PUBLIC keys, on
purpose — a total compromise of a crawl worker yields no ability to mint a proof.

Nothing minted one.  The verifier's every path therefore ended in a DENY, and
M1.3 walk persistence has been shipped-but-unreachable since it was written.
This module is the missing half: the platform side that SIGNS, at the moment it
provisions a throwaway environment.

WHY THE ISSUER IS THE SMALLER HALF, and must stay that way.  Every security
property here is enforced by the verifier, not conceded by this file: proof
lifetime, mutation ceiling, origin binding, crawl binding, issuer identity and
replay are all re-checked on the far side against the FLEET's policy.  An issuer
that mints a ten-year proof still gets a proof the fleet refuses.  So the job
here is narrow and boring by design — produce claims the verifier will accept,
sign them, and never invent authority the verifier did not already grant.

THREE PROPERTIES THIS FILE OWNS OUTRIGHT.

**Determinism.**  Given the same claims, ``issue_provisioning_proof`` produces
byte-identical output.  Ed25519 is deterministic (RFC 8032) and the encoding is
canonical JSON, so re-running an issue with the same inputs re-derives the same
signature — which is what makes an attestation reproducible evidence rather than
a receipt you have to take on faith.  The clock is INJECTED for exactly this
reason: a function that reads ``time.time()`` internally cannot be re-run.

**Tamper-evidence.**  One flipped byte anywhere in the claims changes the
canonical encoding and fails verification.  There is no field a holder can edit
— not the crawl id, not the expiry, not the mutation budget — without
invalidating the whole proof.

**Revocation is mandatory.**  An expiry is not revocation: a proof stolen ten
minutes after issue stays valid for the rest of its life.  Every dispatch
therefore carries a freshly signed revocation list, and a missing, stale or
badly-signed list is a DENY for the whole attestation.  :func:`issue_attestation`
mints BOTH together so a caller cannot accidentally ship a proof without one.

CANONICAL ENCODING IS SHARED WITH THE VERIFIER BY VALUE, NOT BY IMPORT.  The two
services share no package.  :func:`app.services.signing.canonical_bytes` and
``app.attest.canonical_bytes`` are the same three lines written twice on purpose,
and :func:`self_check` below re-derives a signature the way the explorer does so
a divergence fails a test here rather than every genuine proof in production.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .signing import SIG_ALG, canonical_bytes, public_key_of, sign_payload

__all__ = [
    "CLAIMS_VERSION", "DISPOSABLE", "HARD_MAX_MUTATIONS_PER_STEP",
    "MAX_PROOF_LIFETIME_MS", "MIN_PLAUSIBLE_EPOCH_MS",
    "IssuerError", "ProvisioningGrant", "SignedEnvelope",
    "key_id", "normalize_origin", "new_proof_id",
    "issue_provisioning_proof", "issue_revocation_list", "issue_attestation",
    "self_check",
]

#: Claims schema version.  MUST equal ``app.attest.CLAIMS_VERSION``; the verifier
#: refuses an unknown version rather than interpreting it under this one's field
#: meanings, so bumping this is a two-sided change.
CLAIMS_VERSION = 1

#: The only ``env_kind`` that can authorise a walk mutation.  Mirrors
#: ``app.attest.DISPOSABLE`` and the explorer's ``MUTABLE_ENV_KIND``.
DISPOSABLE = "disposable"

#: Mirrors ``app.attest.HARD_MAX_MUTATIONS_PER_STEP``.  Requesting more than this
#: is refused HERE as well as there — an issuer that mints a claim its own
#: verifier will reject is an issuer that produces confusing outages.
HARD_MAX_MUTATIONS_PER_STEP = 10

#: Mirrors ``app.attest.DEFAULT_MAX_LIFETIME_MS``.  The verifier enforces its own
#: ceiling regardless; this refuses at mint time so the failure names the real
#: cause instead of surfacing as ``lifetime_too_long`` on a distant worker.
MAX_PROOF_LIFETIME_MS = 24 * 60 * 60 * 1000

#: Mirrors ``app.attest.MIN_PLAUSIBLE_EPOCH_MS`` — a "now" below this is a
#: monotonic since-start reading, a zeroed clock, or seconds mistaken for millis.
MIN_PLAUSIBLE_EPOCH_MS = 1_000_000_000_000

#: How long a revocation list is good for.  Short on purpose: the list is the
#: ONLY mechanism that can withdraw a proof early, so a long-lived list is a long
#: window in which a revocation has been decided and is not yet being enforced.
DEFAULT_REVOCATION_LIFETIME_MS = 10 * 60 * 1000


class IssuerError(ValueError):
    """A grant that cannot be honestly signed.  Raised at MINT time, never
    swallowed: an issuer that quietly emits a weaker proof than it was asked for
    is worse than one that fails loudly."""


# --- primitives shared with the verifier, by value ---------------------------


def key_id(public_key_b64: str) -> str:
    """Deterministic, non-secret id for a PUBLIC key (``sha256[:16]``).

    Byte-for-byte identical to ``app.attest.key_id``.  The explorer looks its
    trust anchor up by this value, so a divergence here makes every proof
    ``unknown_key_id``.
    """
    return hashlib.sha256(
        (public_key_b64 or "").strip().encode("utf-8")).hexdigest()[:16]


def normalize_origin(url: str) -> str:
    """``scheme://host[:port]`` with default ports elided, lower-cased.

    Identical to ``app.attest.normalize_origin``, including its fail-closed
    empty return: the verifier treats an empty origin on EITHER side as a
    mismatch rather than a wildcard, so minting one guarantees a refusal.
    """
    try:
        parts = urlsplit((url or "").strip())
    except (ValueError, TypeError):
        return ""
    scheme = (parts.scheme or "").lower()
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if not scheme or not host:
        return ""
    if port and not ((scheme == "https" and port == 443)
                     or (scheme == "http" and port == 80)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def new_proof_id() -> str:
    """A fresh, unguessable id for ONE provisioning event.

    The verifier's replay guard keys on this, so it must be unique per issue —
    which is why it is the one thing in this module that is NOT derived from the
    grant.  A caller wanting a reproducible proof passes its own ``proof_id``.
    """
    return secrets.token_hex(16)


# --- the grant ---------------------------------------------------------------


@dataclass(frozen=True)
class ProvisioningGrant:
    """WHAT THE PLATFORM IS ATTESTING TO, stated before anything is signed.

    A frozen record rather than a pile of keyword arguments so the thing that was
    signed can be logged, diffed and re-signed exactly.  Every field is
    load-bearing at the verifier:

    * ``crawl_id`` BINDS the proof to one crawl — a proof lifted from one
      dispatch and replayed on another is refused on the binding, not merely on
      replay;
    * ``target_origin`` binds it to one origin, so a proof for a throwaway
      staging host cannot authorise mutation against production;
    * ``tenant_id`` binds it to one tenant;
    * ``env_kind`` must be ``disposable`` — the verifier accepts nothing else.
    """

    environment_id: str
    tenant_id: str
    crawl_id: str
    target_url: str
    reset_procedure: str = ""
    env_kind: str = DISPOSABLE
    max_walk_mutations_per_step: int = 1
    proof_id: str = ""

    def claims(self, *, issuer: str, issued_at_ms: int,
               lifetime_ms: int) -> dict[str, Any]:
        """The exact object that gets signed.  Validated first, because a claim
        the verifier will refuse is not worth a signature."""
        issuer = (issuer or "").strip()
        if not issuer:
            raise IssuerError("issuer is required")
        origin = normalize_origin(self.target_url)
        if not origin:
            raise IssuerError(
                f"target_url {self.target_url!r} does not normalise to an "
                f"origin; the verifier treats an empty origin as a MISMATCH, so "
                f"this proof could never authorise anything")
        if self.env_kind != DISPOSABLE:
            raise IssuerError(
                f"env_kind={self.env_kind!r}: walk mutation is authorised for "
                f"{DISPOSABLE!r} environments only")
        for name, value in (("environment_id", self.environment_id),
                            ("tenant_id", self.tenant_id),
                            ("crawl_id", self.crawl_id)):
            if not str(value or "").strip():
                raise IssuerError(f"{name} is required")
        budget = int(self.max_walk_mutations_per_step)
        if not 0 <= budget <= HARD_MAX_MUTATIONS_PER_STEP:
            raise IssuerError(
                f"max_walk_mutations_per_step={budget} is outside "
                f"0..{HARD_MAX_MUTATIONS_PER_STEP}")
        if issued_at_ms < MIN_PLAUSIBLE_EPOCH_MS:
            raise IssuerError(
                f"issued_at_ms={issued_at_ms} is not an epoch-ms reading — the "
                f"verifier refuses rather than compare across clock domains")
        if lifetime_ms <= 0:
            raise IssuerError("lifetime_ms must be positive")
        if lifetime_ms > MAX_PROOF_LIFETIME_MS:
            raise IssuerError(
                f"lifetime_ms={lifetime_ms} exceeds the {MAX_PROOF_LIFETIME_MS}ms "
                f"ceiling the verifier enforces; minting it would produce a proof "
                f"refused as lifetime_too_long on the worker")

        proof_id = str(self.proof_id or "").strip() or new_proof_id()
        if not 16 <= len(proof_id) <= 128:
            raise IssuerError(
                f"proof_id must be 16..128 chars (got {len(proof_id)})")
        # Key order is irrelevant to the signature (the encoding sorts) and is
        # kept in the verifier's declared order anyway, so a human diffing a
        # proof against ``ProofClaims`` reads them in the same sequence.
        return {
            "v": CLAIMS_VERSION,
            "proof_id": proof_id,
            "issuer": issuer,
            "environment_id": str(self.environment_id).strip(),
            "env_kind": self.env_kind,
            "tenant_id": str(self.tenant_id).strip(),
            "crawl_id": str(self.crawl_id).strip(),
            "target_origin": origin,
            "reset_procedure": str(self.reset_procedure or "")[:512],
            "issued_at_ms": int(issued_at_ms),
            "expires_at_ms": int(issued_at_ms) + int(lifetime_ms),
            "max_walk_mutations_per_step": budget,
        }


@dataclass(frozen=True)
class SignedEnvelope:
    """A signed object in the shape the verifier destructures."""

    claims: Mapping[str, Any]
    alg: str
    kid: str
    signature: str

    def as_dict(self) -> dict[str, Any]:
        return {"claims": dict(self.claims), "alg": self.alg,
                "kid": self.kid, "signature": self.signature}


# --- issuing -----------------------------------------------------------------


def _sign(private_key_b64: str, claims: Mapping[str, Any]) -> SignedEnvelope:
    public = public_key_of(private_key_b64)
    return SignedEnvelope(
        claims=dict(claims), alg=SIG_ALG, kid=key_id(public),
        signature=sign_payload(private_key_b64, dict(claims)),
    )


def issue_provisioning_proof(
    grant: ProvisioningGrant,
    *,
    private_key_b64: str,
    issuer: str,
    issued_at_ms: int,
    lifetime_ms: int = 60 * 60 * 1000,
) -> dict[str, Any]:
    """Sign ONE provisioning event.

    ``issued_at_ms`` is a PARAMETER, not a clock read.  That is what makes an
    attestation reproducible: the same grant and the same timestamp re-derive the
    same bytes, so an auditor can re-issue a proof from the recorded inputs and
    compare it to the one on file rather than trusting that it was made properly.
    """
    claims = grant.claims(issuer=issuer, issued_at_ms=issued_at_ms,
                          lifetime_ms=lifetime_ms)
    return _sign(private_key_b64, claims).as_dict()


def issue_revocation_list(
    *,
    private_key_b64: str,
    issuer: str,
    issued_at_ms: int,
    revoked_proof_ids: Iterable[str] = (),
    revoked_environment_ids: Iterable[str] = (),
    lifetime_ms: int = DEFAULT_REVOCATION_LIFETIME_MS,
) -> dict[str, Any]:
    """Sign the issuer's CURRENT revocation state.

    AN EMPTY LIST IS NOT THE ABSENCE OF A LIST, and the difference is the whole
    mechanism.  "I have revoked nothing, signed, valid for ten minutes" is a
    positive statement the verifier can act on; no list at all is "revocation
    state unknown", which is a DENY.  So this is called on EVERY dispatch,
    including — especially — the ones with nothing revoked.

    Sorted and de-duplicated so the same revocation state signs to the same
    bytes regardless of the order the database returned it in.
    """
    issuer = (issuer or "").strip()
    if not issuer:
        raise IssuerError("issuer is required")
    if issued_at_ms < MIN_PLAUSIBLE_EPOCH_MS:
        raise IssuerError(
            f"issued_at_ms={issued_at_ms} is not an epoch-ms reading")
    if lifetime_ms <= 0:
        raise IssuerError("lifetime_ms must be positive")
    claims = {
        "v": CLAIMS_VERSION,
        "issuer": issuer,
        "issued_at_ms": int(issued_at_ms),
        "expires_at_ms": int(issued_at_ms) + int(lifetime_ms),
        "revoked_proof_ids": sorted({str(p).strip()
                                     for p in revoked_proof_ids if str(p).strip()}),
        "revoked_environment_ids": sorted({str(e).strip()
                                           for e in revoked_environment_ids
                                           if str(e).strip()}),
    }
    return _sign(private_key_b64, claims).as_dict()


def issue_attestation(
    grant: ProvisioningGrant,
    *,
    private_key_b64: str,
    issuer: str,
    issued_at_ms: int,
    proof_lifetime_ms: int = 60 * 60 * 1000,
    revocation_lifetime_ms: int = DEFAULT_REVOCATION_LIFETIME_MS,
    revoked_proof_ids: Iterable[str] = (),
    revoked_environment_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """The COMPLETE ``attestation`` object a dispatch carries.

    Both halves are minted together, deliberately.  The verifier requires a proof
    AND a currently-valid revocation list, and a caller that could obtain one
    without the other would eventually ship a dispatch that is refused for a
    reason the operator cannot see from the request they made.
    """
    return {
        "proof": issue_provisioning_proof(
            grant, private_key_b64=private_key_b64, issuer=issuer,
            issued_at_ms=issued_at_ms, lifetime_ms=proof_lifetime_ms),
        "revocations": issue_revocation_list(
            private_key_b64=private_key_b64, issuer=issuer,
            issued_at_ms=issued_at_ms,
            revoked_proof_ids=revoked_proof_ids,
            revoked_environment_ids=revoked_environment_ids,
            lifetime_ms=revocation_lifetime_ms),
    }


# --- the divergence check ----------------------------------------------------


def self_check(private_key_b64: str, claims: Mapping[str, Any]) -> bool:
    """Re-derive a signature THE WAY THE EXPLORER DOES and compare.

    The canonical encoding is duplicated across two services that share no
    package, so a change to either copy would silently reject every genuine
    proof in production — the worst possible failure shape, because it looks like
    a configuration problem and appears everywhere at once.  This makes that
    divergence a local, testable fact.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        import base64

        payload = dict(claims)
        signature = sign_payload(private_key_b64, payload)
        public = public_key_of(private_key_b64)
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public.encode("ascii"), validate=True)
        ).verify(
            base64.b64decode(signature.encode("ascii"), validate=True),
            canonical_bytes(payload),
        )
        return True
    except Exception:
        return False
