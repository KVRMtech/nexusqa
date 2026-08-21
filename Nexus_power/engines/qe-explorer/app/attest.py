"""M1.3 / T-WP-02 — PLATFORM-ISSUED DISPOSABLE-ENVIRONMENT ATTESTATION.

THE HOLE THIS CLOSES
====================
:class:`app.guard.Attestation` is an unsigned dict.  It says::

    {"attested_by": "ops", "env_kind": "disposable", "expires_at_ms": ...}

and every gate downstream of it — the Phase-B submit tier, ``observe_only``,
the traversal posture — trusts it because it arrived.  That is tolerable only
while the thing it authorises is ALSO gated on a human per-flow approval, which
is exactly the case for SUBMIT.  It is NOT tolerable for walk persistence,
which by design fires without a human in the loop on every wizard step.  A
tenant — or anything that can shape a dispatch body — that can type the word
``disposable`` must never thereby acquire the right to POST at a target.

So walk mutation depends on a DIFFERENT object: a proof the platform ISSUED and
SIGNED at the moment it provisioned the throwaway environment.  The explorer
holds only PUBLIC keys — it cannot mint one even if wholly compromised.

THE PROOF
=========
An Ed25519 detached signature over the canonical JSON encoding of the claims
(the same primitive and encoding as ``qe-central`` ``app/services/signing.py``,
so one issuer implementation serves both)::

    {
      "claims": {
        "v": 1,
        "proof_id":        "...",   # unique per provisioning event
        "issuer":          "...",   # must equal the configured expected issuer
        "environment_id":  "...",   # the provisioned throwaway env
        "env_kind":        "disposable",
        "tenant_id":       "...",
        "crawl_id":        "...",   # BINDS the proof to ONE crawl
        "target_origin":   "https://host:port",
        "reset_procedure": "...",
        "issued_at_ms":     N,      # epoch ms
        "expires_at_ms":    N,      # epoch ms
        "max_walk_mutations_per_step": 3
      },
      "alg": "ed25519", "kid": "...", "signature": "base64"
    }

REVOCATION IS MANDATORY, NOT OPTIONAL
=====================================
A signature with an expiry is not revocation: a proof stolen ten minutes after
issue stays valid for the rest of its life.  So walk mutation additionally
requires a SEPARATELY SIGNED, currently-valid revocation list from the same
issuer.  No list, an expired list, or a list whose signature does not verify
=> REFUSED.

That costs the platform one extra signed object per dispatch and buys real
revocation.  It is not a regression for anybody: walk mutation does not exist
today, so nothing that works now stops working — the strictest possible default
IS the backward-compatible one.

FAIL-CLOSED ORDER
=================
Every check below returns a stable, log-safe reason code and STOPS.  No check is
skipped because an earlier one was ambiguous, and nothing here raises: a verifier
that throws is a verifier whose caller might catch and carry on.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: The ONLY signature algorithm this verifier will consider.  A proof naming
#: anything else — including "none" — is refused before a key is even looked up.
SIG_ALG = "ed25519"

#: Claims schema version.  A proof at an unknown version is refused rather than
#: interpreted under this version's field meanings.
CLAIMS_VERSION = 1

#: The ONLY env_kind that can authorise a walk mutation.  Mirrors
#: ``main.MUTABLE_ENV_KIND`` and ``guard.Attestation.is_submit_capable``.
DISPOSABLE = "disposable"

#: 2001-09-09T01:46:40Z in epoch millis.  Identical constant and identical
#: doctrine to ``guard._MIN_PLAUSIBLE_EPOCH_MS`` (M0.5 T-SEC-08): a "now" below
#: this is a monotonic since-start reading, a zeroed clock, or seconds mistaken
#: for millis.  Freshness REFUSES rather than compares across clock domains.
MIN_PLAUSIBLE_EPOCH_MS = 1_000_000_000_000

#: Hard ceiling on how long a proof may be valid, enforced by the VERIFIER and
#: not merely asserted by the issuer.  A compromised or buggy issuer that mints
#: a ten-year proof still gets a proof this fleet refuses: the window in which a
#: stolen proof is useful is bounded by the verifier's own policy.
DEFAULT_MAX_LIFETIME_MS = 24 * 60 * 60 * 1000

#: Allowed clock skew, both directions, between issuer and verifier.
DEFAULT_SKEW_MS = 300_000

#: Absolute ceiling on the per-step mutation budget any proof may request.  The
#: effective budget is ``min(claims, fleet ceiling)`` — least privilege, and an
#: issuer cannot widen this fleet's policy from the outside.
DEFAULT_MAX_MUTATIONS_PER_STEP = 3
HARD_MAX_MUTATIONS_PER_STEP = 10


# --- Stable refusal vocabulary ---------------------------------------------
# Every one of these is a DENY.  OK is the only authorising value, and it is
# returned from exactly one place in this module.

class AttestReason:
    OK = "ok"
    NO_TRUST_ANCHOR = "no_trust_anchor"
    NO_PROOF = "no_proof"
    MALFORMED_ENVELOPE = "malformed_envelope"
    UNSUPPORTED_ALG = "unsupported_alg"
    UNKNOWN_KEY_ID = "unknown_key_id"
    BAD_SIGNATURE = "bad_signature"
    MALFORMED_CLAIMS = "malformed_claims"
    UNSUPPORTED_VERSION = "unsupported_version"
    ISSUER_MISMATCH = "issuer_mismatch"
    NOT_DISPOSABLE = "not_disposable"
    CLOCK_DOMAIN_ERROR = "clock_domain_error"
    ISSUED_IN_FUTURE = "issued_in_future"
    EXPIRED = "expired"
    LIFETIME_TOO_LONG = "lifetime_too_long"
    CRAWL_BINDING_MISMATCH = "crawl_binding_mismatch"
    TENANT_MISMATCH = "tenant_mismatch"
    ORIGIN_MISMATCH = "origin_mismatch"
    REVOKED = "revoked"
    NO_REVOCATION_LIST = "no_revocation_list"
    REVOCATION_BAD_SIGNATURE = "revocation_bad_signature"
    REVOCATION_EXPIRED = "revocation_expired"
    REVOCATION_ISSUER_MISMATCH = "revocation_issuer_mismatch"
    PROOF_REPLAYED = "proof_replayed"
    VERIFIER_ERROR = "verifier_error"


# --- Canonical encoding + key ids (shared with the qe-central issuer) -------


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic encoding — sorted keys, no insignificant whitespace.

    The SAME bytes are signed and re-derived, so one differing byte anywhere in
    the claims fails verification.  Duplicated deliberately from qe-central's
    ``signing.canonical_bytes``: the two services share no package, and a
    divergence here would silently reject every genuine proof.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def key_id(public_key_b64: str) -> str:
    """Deterministic, non-secret id for a PUBLIC key (``sha256[:16]``)."""
    return hashlib.sha256((public_key_b64 or "").strip().encode("utf-8")).hexdigest()[:16]


def _b64d(value: str) -> bytes:
    return base64.b64decode((value or "").encode("ascii"), validate=True)


def _verify_ed25519(public_key_b64: str, payload: Any, signature_b64: str) -> bool:
    """True iff ``signature_b64`` is a valid Ed25519 signature by
    ``public_key_b64`` over :func:`canonical_bytes` of ``payload``.

    NEVER raises.  A malformed key, a malformed signature, a missing
    ``cryptography`` install — all are a verification FAILURE, which is the only
    safe reading of "I could not check this".
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(_b64d(public_key_b64))
        pub.verify(_b64d(signature_b64), canonical_bytes(payload))
        return True
    except Exception:
        return False


def normalize_origin(url: str) -> str:
    """``scheme://host[:port]`` with default ports elided, lower-cased.

    Fail-closed: anything unparseable returns ``""``, and the caller treats an
    empty origin on either side as a MISMATCH rather than a wildcard.

    An IPv6 host is RE-BRACKETED (CERT-FINDING-2 / A11a).  ``urlsplit`` reports
    ``hostname`` without brackets, so reassembling ``host:port`` from it emitted
    ``https://::1:8443`` - a string THIS function cannot re-parse, so a second
    pass returned ``""``.  That is not cosmetic: the output is signed into
    ``claims.target_origin`` and the verifier re-normalises it before comparing,
    so a non-idempotent output is a cryptographically valid proof guaranteed to
    be refused as ``origin_mismatch`` on a correctly-provisioned environment.

    The test is ``":" in host`` and not a list of IPv6 shapes: the defect is the
    reassembly, so EVERY host containing a colon breaks.  Enumerating the forms
    that were reported would have left the class open.

    ``N(N(u)) == N(u)`` is asserted for the whole origin-vector suite in
    ``tests/test_walk_attestation.py``; the malformed-port vector still returns
    ``""``, because a repair that made an unparseable authority parseable would
    turn the verifier's mismatch sentinel into an origin.
    """
    try:
        parts = urlsplit((url or "").strip())
    except (ValueError, TypeError):
        return ""
    scheme = (parts.scheme or "").lower()
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:          # malformed port in the authority
        return ""
    if not scheme or not host:
        return ""
    if "[" in host or "]" in host:
        # A bracket SURVIVING the parse means the authority was malformed and
        # urlsplit split it somewhere we did not intend: 'https://[::1@evil]/x'
        # is read as userinfo '[::1' + host 'evil]'. Re-bracketing would emit an
        # unbalanced origin, so refuse instead. NEW-CERT-FINDING-4.
        return ""
    if ":" in host:             # IPv6 literal - urlsplit stripped its brackets
        host = f"[{host}]"
    if port and not ((scheme == "https" and port == 443)
                     or (scheme == "http" and port == 80)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


# --- Claim models (strict: an unexpected field REFUSES the proof) -----------


class ProofClaims(BaseModel):
    """The signed statement.  ``extra="forbid"`` is a security control, not
    tidiness: a proof carrying fields this verifier does not understand may be
    relying on them for its meaning, and interpreting it anyway is a guess."""

    model_config = ConfigDict(extra="forbid")

    v: int
    proof_id: str = Field(min_length=16, max_length=128)
    issuer: str = Field(min_length=1, max_length=128)
    environment_id: str = Field(min_length=1, max_length=128)
    env_kind: str = Field(min_length=1, max_length=32)
    tenant_id: str = Field(min_length=1, max_length=128)
    crawl_id: str = Field(min_length=1, max_length=128)
    target_origin: str = Field(min_length=1, max_length=512)
    reset_procedure: str = Field(default="", max_length=512)
    issued_at_ms: int
    expires_at_ms: int
    max_walk_mutations_per_step: int = Field(
        default=1, ge=0, le=HARD_MAX_MUTATIONS_PER_STEP)


class RevocationClaims(BaseModel):
    """A signed, time-boxed statement of what the issuer has revoked."""

    model_config = ConfigDict(extra="forbid")

    v: int
    issuer: str = Field(min_length=1, max_length=128)
    issued_at_ms: int
    expires_at_ms: int
    revoked_proof_ids: tuple[str, ...] = ()
    revoked_environment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrustStore:
    """The PUBLIC keys this fleet accepts, and the policy it applies to them.

    ``keys`` maps :func:`key_id` -> base64 raw Ed25519 public key.  There is no
    private key anywhere in this service: signing lives in the platform's
    provisioner, so a total compromise of the explorer yields no ability to mint
    a proof.  Empty => fail-closed (nothing verifies, walk mutation is off).
    """

    keys: Mapping[str, str] = field(default_factory=dict)
    issuer: str = ""
    max_lifetime_ms: int = DEFAULT_MAX_LIFETIME_MS
    skew_ms: int = DEFAULT_SKEW_MS
    max_mutations_per_step: int = DEFAULT_MAX_MUTATIONS_PER_STEP

    @property
    def configured(self) -> bool:
        return bool(self.keys) and bool((self.issuer or "").strip())

    @classmethod
    def from_public_keys(cls, public_keys: Any, *, issuer: str = "",
                         **policy: Any) -> "TrustStore":
        """Build a store from raw base64 public keys, dropping anything that is
        not a raw 32-byte Ed25519 key.  A malformed key is LOGGED and dropped
        rather than accepted: half-configured trust is no trust."""
        keys: dict[str, str] = {}
        for raw in public_keys or ():
            pk = str(raw or "").strip()
            if not pk:
                continue
            try:
                if len(_b64d(pk)) != 32:
                    raise ValueError("not a raw 32-byte ed25519 public key")
            except Exception as exc:
                logger.error("qec.attest.bad_public_key error=%s", str(exc)[:120])
                continue
            keys[key_id(pk)] = pk
        return cls(keys=keys, issuer=str(issuer or "").strip(), **policy)

    def resolve(self, kid: str) -> str:
        return self.keys.get(str(kid or "").strip(), "")


@dataclass(frozen=True)
class AttestationVerdict:
    """The verifier's answer.  ``authorized`` is the gate; ``reason`` is a stable
    code for the audit trail and the metrics; the remaining fields are the
    LEAST-PRIVILEGE grant a caller may act on — populated ONLY on an authorising
    verdict, so a denied verdict cannot hand anybody a budget."""

    authorized: bool
    reason: str
    detail: str = ""
    proof_id: str = ""
    environment_id: str = ""
    env_kind: str = ""
    tenant_id: str = ""
    crawl_id: str = ""
    target_origin: str = ""
    kid: str = ""
    expires_at_ms: int = 0
    max_mutations_per_step: int = 0
    claims_digest: str = ""

    def as_audit_dict(self) -> dict[str, Any]:
        """The approval context recorded on every permitted mutation (T-WP-03).
        Value-free: ids, a digest and a policy number — never a key, never a
        signature, never a request body."""
        return {
            "authorized": bool(self.authorized), "reason": self.reason,
            "proof_id": self.proof_id, "environment_id": self.environment_id,
            "env_kind": self.env_kind, "tenant_id": self.tenant_id,
            "kid": self.kid, "expires_at_ms": int(self.expires_at_ms),
            "max_mutations_per_step": int(self.max_mutations_per_step),
            "claims_digest": self.claims_digest,
        }


def _deny(reason: str, detail: str = "") -> AttestationVerdict:
    logger.warning("qec.attest.denied reason=%s detail=%s", reason, detail[:200])
    return AttestationVerdict(authorized=False, reason=reason, detail=detail[:300])


# --- Replay protection across crawls inside one worker process --------------


class ProofReplayGuard:
    """A ``proof_id`` may be admitted for exactly ONE ``crawl_id``.

    The crawl binding inside the claims already stops a proof being reused for a
    different crawl.  This closes the other half: it makes the FIRST admission
    authoritative, so a second dispatch quoting the same proof against a
    different crawl id is refused even if a signature somehow covered both.

    Thread-safe: one explorer process serves concurrent crawls.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._lock = threading.Lock()
        self._seen: "dict[str, str]" = {}
        self._max = max(1, int(max_entries))

    def admit(self, proof_id: str, crawl_id: str) -> bool:
        pid, cid = str(proof_id or ""), str(crawl_id or "")
        if not pid or not cid:
            return False
        with self._lock:
            bound = self._seen.get(pid)
            if bound is None:
                if len(self._seen) >= self._max:
                    # Oldest-insertion first (dicts preserve insertion order).
                    self._seen.pop(next(iter(self._seen)), None)
                    logger.warning("qec.attest.replay_guard_pressure size=%d",
                                   len(self._seen))
                self._seen[pid] = cid
                return True
            return bound == cid

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


#: Process-wide guard.  Injectable in tests; never reset in production.
_REPLAY_GUARD = ProofReplayGuard()


# --- Revocation ------------------------------------------------------------


@dataclass(frozen=True)
class RevocationSet:
    proof_ids: frozenset = frozenset()
    environment_ids: frozenset = frozenset()
    expires_at_ms: int = 0


def verify_revocation_list(payload: Any, *, trust: TrustStore,
                           now_epoch_ms: int):
    """``(RevocationSet, "")`` on success, ``(None, reason)`` on any failure.

    Never raises.  An unusable list is a DENY for the whole attestation — it is
    not "no revocations known", it is "revocation state unknown"."""
    if not isinstance(payload, Mapping):
        return None, AttestReason.NO_REVOCATION_LIST
    raw_claims = payload.get("claims")
    alg = str(payload.get("alg") or "").strip().lower()
    kid = str(payload.get("kid") or "").strip()
    sig = str(payload.get("signature") or "").strip()
    if not isinstance(raw_claims, Mapping) or not kid or not sig:
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    if alg != SIG_ALG:
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    public_key = trust.resolve(kid)
    if not public_key:
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    if not _verify_ed25519(public_key, dict(raw_claims), sig):
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    try:
        claims = RevocationClaims.model_validate(dict(raw_claims))
    except Exception:
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    if claims.v != CLAIMS_VERSION:
        return None, AttestReason.REVOCATION_BAD_SIGNATURE
    if claims.issuer != trust.issuer:
        return None, AttestReason.REVOCATION_ISSUER_MISMATCH
    if int(now_epoch_ms) >= int(claims.expires_at_ms):
        # A stale list proves nothing about what has been revoked SINCE.
        return None, AttestReason.REVOCATION_EXPIRED
    return RevocationSet(
        proof_ids=frozenset(str(p) for p in claims.revoked_proof_ids),
        environment_ids=frozenset(str(e) for e in claims.revoked_environment_ids),
        expires_at_ms=int(claims.expires_at_ms),
    ), ""


# --- The verifier ----------------------------------------------------------


def verify_provisioning_proof(
    payload: Any,
    *,
    trust: TrustStore,
    crawl_id: str,
    tenant_id: str,
    target_url: str,
    now_epoch_ms: Optional[int] = None,
    replay_guard: Optional[ProofReplayGuard] = None,
) -> AttestationVerdict:
    """Verify a platform-issued provisioning proof.  PURE apart from the wall
    clock and the process replay guard; NEVER raises; DENY is the default.

    ``payload`` is the untrusted ``attestation`` object from the dispatch: it
    must carry ``proof`` and ``revocations``.  Nothing a tenant can write
    reaches an authorising verdict without a signature this fleet's configured
    PUBLIC key verifies.
    """
    try:
        return _verify(payload, trust=trust, crawl_id=crawl_id, tenant_id=tenant_id,
                       target_url=target_url, now_epoch_ms=now_epoch_ms,
                       replay_guard=replay_guard or _REPLAY_GUARD)
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.exception("qec.attest.verifier_error")
        return _deny(AttestReason.VERIFIER_ERROR, str(exc)[:200])


def _verify(payload: Any, *, trust: TrustStore, crawl_id: str, tenant_id: str,
            target_url: str, now_epoch_ms: Optional[int],
            replay_guard: ProofReplayGuard) -> AttestationVerdict:
    # 0 - a fleet with no configured trust anchor can prove nothing.
    if not trust.configured:
        return _deny(AttestReason.NO_TRUST_ANCHOR,
                     "no attestation public key / issuer configured")

    # 1 - the clock.  Refuse rather than compare across clock domains.
    now = int(time.time() * 1000) if now_epoch_ms is None else int(now_epoch_ms)
    if now < MIN_PLAUSIBLE_EPOCH_MS:
        return _deny(AttestReason.CLOCK_DOMAIN_ERROR,
                     f"now_ms={now} is not an epoch-ms reading")

    # 2 - envelope shape.
    if not isinstance(payload, Mapping):
        return _deny(AttestReason.NO_PROOF, "no provisioning proof supplied")
    proof = payload.get("proof")
    if not isinstance(proof, Mapping):
        return _deny(AttestReason.NO_PROOF, "attestation carries no 'proof'")
    raw_claims = proof.get("claims")
    alg = str(proof.get("alg") or "").strip().lower()
    kid = str(proof.get("kid") or "").strip()
    signature = str(proof.get("signature") or "").strip()
    if not isinstance(raw_claims, Mapping) or not kid or not signature:
        return _deny(AttestReason.MALFORMED_ENVELOPE, "claims/kid/signature missing")
    if alg != SIG_ALG:
        return _deny(AttestReason.UNSUPPORTED_ALG, f"alg={alg!r}")

    # 3 - key SELECTION is not trust; the signature check below is.
    public_key = trust.resolve(kid)
    if not public_key:
        return _deny(AttestReason.UNKNOWN_KEY_ID, f"kid={kid}")

    # 4 - INTEGRITY, over the raw claims exactly as they arrived.  Verified
    #     BEFORE the typed parse, so no normalisation the parser performs can
    #     ever sit between the signed bytes and the checked bytes.
    claims_dict = dict(raw_claims)
    if not _verify_ed25519(public_key, claims_dict, signature):
        return _deny(AttestReason.BAD_SIGNATURE, f"kid={kid}")

    # 5 - schema.  Strict: an unknown field refuses.
    try:
        claims = ProofClaims.model_validate(claims_dict)
    except Exception as exc:
        return _deny(AttestReason.MALFORMED_CLAIMS, str(exc)[:200])
    if claims.v != CLAIMS_VERSION:
        return _deny(AttestReason.UNSUPPORTED_VERSION, f"v={claims.v}")

    # 6 - issuer.
    if claims.issuer != trust.issuer:
        return _deny(AttestReason.ISSUER_MISMATCH, f"issuer={claims.issuer!r}")

    # 7 - PRODUCTION ISOLATION.  Anything that is not the word 'disposable' —
    #     prod, staging, uat, blank, novel — is refused here, signature or not.
    if (claims.env_kind or "").strip().lower() != DISPOSABLE:
        return _deny(AttestReason.NOT_DISPOSABLE, f"env_kind={claims.env_kind!r}")

    # 8 - freshness + a verifier-enforced lifetime ceiling.
    if int(claims.issued_at_ms) < MIN_PLAUSIBLE_EPOCH_MS:
        return _deny(AttestReason.CLOCK_DOMAIN_ERROR,
                     f"issued_at_ms={claims.issued_at_ms}")
    if int(claims.issued_at_ms) - now > int(trust.skew_ms):
        return _deny(AttestReason.ISSUED_IN_FUTURE,
                     f"issued_at_ms={claims.issued_at_ms} now={now}")
    if now >= int(claims.expires_at_ms):
        return _deny(AttestReason.EXPIRED,
                     f"expires_at_ms={claims.expires_at_ms} now={now}")
    lifetime = int(claims.expires_at_ms) - int(claims.issued_at_ms)
    if lifetime <= 0 or lifetime > int(trust.max_lifetime_ms):
        return _deny(AttestReason.LIFETIME_TOO_LONG,
                     f"lifetime_ms={lifetime} ceiling={trust.max_lifetime_ms}")

    # 9 - BINDINGS.  A proof is for one crawl, one tenant, one origin.  Without
    #     these a genuine proof for a throwaway env is a bearer token that
    #     authorises mutation anywhere its holder points it.
    if claims.crawl_id != str(crawl_id or ""):
        return _deny(AttestReason.CRAWL_BINDING_MISMATCH,
                     f"proof_crawl={claims.crawl_id!r}")
    if claims.tenant_id != str(tenant_id or ""):
        return _deny(AttestReason.TENANT_MISMATCH, f"proof_tenant={claims.tenant_id!r}")
    want_origin = normalize_origin(target_url)
    got_origin = normalize_origin(claims.target_origin)
    if not want_origin or not got_origin or want_origin != got_origin:
        return _deny(AttestReason.ORIGIN_MISMATCH,
                     f"proof_origin={got_origin!r} target_origin={want_origin!r}")

    # 10 - REVOCATION: mandatory, signed, and unexpired.
    revocations, why = verify_revocation_list(
        payload.get("revocations"), trust=trust, now_epoch_ms=now)
    if revocations is None:
        return _deny(why or AttestReason.NO_REVOCATION_LIST,
                     "revocation list unusable")
    if claims.proof_id in revocations.proof_ids:
        return _deny(AttestReason.REVOKED, f"proof_id={claims.proof_id}")
    if claims.environment_id in revocations.environment_ids:
        return _deny(AttestReason.REVOKED, f"environment_id={claims.environment_id}")

    # 11 - one proof, one crawl, once.
    if not replay_guard.admit(claims.proof_id, claims.crawl_id):
        return _deny(AttestReason.PROOF_REPLAYED, f"proof_id={claims.proof_id}")

    # 12 - LEAST PRIVILEGE: the grant is the smaller of what the issuer asked
    #      for and what this fleet permits.  A proof cannot widen fleet policy.
    budget = min(int(claims.max_walk_mutations_per_step),
                 int(trust.max_mutations_per_step),
                 HARD_MAX_MUTATIONS_PER_STEP)

    verdict = AttestationVerdict(
        authorized=True, reason=AttestReason.OK,
        detail="platform provisioning proof verified",
        proof_id=claims.proof_id, environment_id=claims.environment_id,
        env_kind=DISPOSABLE, tenant_id=claims.tenant_id, crawl_id=claims.crawl_id,
        target_origin=want_origin, kid=kid,
        expires_at_ms=int(claims.expires_at_ms), max_mutations_per_step=budget,
        claims_digest=hashlib.sha256(canonical_bytes(claims_dict)).hexdigest()[:32],
    )
    logger.info("qec.attest.authorized proof_id=%s env=%s kid=%s budget=%d",
                verdict.proof_id, verdict.environment_id, kid, budget)
    return verdict


__all__ = [
    "CLAIMS_VERSION", "DEFAULT_MAX_LIFETIME_MS", "DEFAULT_MAX_MUTATIONS_PER_STEP",
    "DEFAULT_SKEW_MS", "DISPOSABLE", "HARD_MAX_MUTATIONS_PER_STEP",
    "MIN_PLAUSIBLE_EPOCH_MS", "SIG_ALG", "AttestReason", "AttestationVerdict",
    "ProofClaims", "ProofReplayGuard", "RevocationClaims", "RevocationSet",
    "TrustStore", "canonical_bytes", "key_id", "normalize_origin",
    "verify_provisioning_proof", "verify_revocation_list",
]
