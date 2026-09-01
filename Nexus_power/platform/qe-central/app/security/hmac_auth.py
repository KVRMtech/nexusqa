"""Shared explorer↔qe-central callback authentication (M0.5 T-SEC-06 / T-SEC-11).

THE PROBLEM THIS REPLACES
=========================
v1 was ``HMAC-SHA256(secret, raw_body)`` in ``X-QEC-Signature``.  It proved the
body came from a holder of the shared secret and NOTHING else:

  * a captured ``/complete`` callback could be REPLAYED verbatim, forever;
  * there was no key id, so rotating the fleet secret meant a flag day where
    every in-flight callback was rejected;
  * a signature had no binding to WHICH crawl it belonged to.

v2 SIGNATURE FORMAT
===================
``X-QEC-Signature: v2;kid=<kid>;ts=<epoch_ms>;nonce=<32 hex>;sig=<64 hex>``

with::

    sig = HMAC-SHA256(key, b"v2\\n{kid}\\n{ts}\\n{nonce}\\n{scope}\\n{sha256(body)}")

``scope`` binds the signature to the logical operation, tenant, and crawl.  A
valid signature for one tenant/crawl cannot authenticate a call about another.

VERIFICATION (all five, fail-closed, in order)
==============================================
  1. the header parses as v2 and names a KNOWN key id;
  2. ``ts`` is within ``skew_seconds`` — past OR future;
  3. ``nonce`` has not been consumed inside the freshness window;
  4. the signature matches (constant time) over the body HASH + scope;
  5. only then is the nonce consumed (so a failed verify cannot be used to
     burn a legitimate nonce).

KEY ROTATION (T-SEC-11)
=======================
A :class:`KeyRing` holds the CURRENT key plus an optional PREVIOUS key with an
explicit overlap deadline.  Signing always uses the current key; verification
accepts either while the overlap is open, and the previous key stops verifying
the moment its deadline passes.  Key ids are derived deterministically from the
secret (``sha256(secret)[:16]``) so no operator has to invent or distribute one,
and a kid never reveals the secret.

This module is PURE (apart from an injectable clock) and is duplicated
byte-for-byte in ``engines/qe-explorer/app/hmac_auth.py`` — the two services do
not share a package, and a divergence would reject every genuine callback.  The
security suite pins them identical.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Header carrying the v2 signature envelope.
SIGNATURE_HEADER = "X-QEC-Signature"
#: Header carrying the shared secret on dispatch (explorer-side inbound auth).
TOKEN_HEADER = "X-QEC-Token"

SCHEME = "v2"
#: Default allowed clock skew, both directions, in seconds.
DEFAULT_SKEW_SECONDS = 300
#: Upper bound on remembered nonces.  A nonce older than the skew window can
#: never verify anyway (rule 2 rejects it first), so the store only has to hold
#: one window's worth; the cap is a memory backstop against a flood.
MAX_NONCES = 20_000


class SignatureError(Exception):
    """A callback signature that did not verify.  ``reason`` is a stable,
    log-safe category — it NEVER contains a key, a nonce, or a body."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def key_id(secret: str) -> str:
    """Deterministic, non-secret key id for ``secret`` (``sha256[:16]``).

    Publishing this in the signature envelope reveals nothing: it is a one-way
    digest, and it lets a verifier pick the right key without trial decryption.
    """
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:16]


def body_hash(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def tenant_scope(operation: str, tenant_id: str, crawl_id: str) -> str:
    """Return the one canonical tenant-bound callback scope.

    Tenant identity is already verified against the exploration row by the
    receiving service, but it must also be signed.  Otherwise a future endpoint
    that accidentally treats the body claim as authority could turn a fleet-wide
    token into cross-tenant authority.  Reject incomplete identities before a
    signature is emitted: an empty component is a weaker protocol, not a useful
    fallback.
    """
    values = tuple(str(value or "").strip() for value in
                   (operation, tenant_id, crawl_id))
    if not all(values):
        raise SignatureError("missing_tenant_scope_identity")
    if any("\n" in value or "\r" in value for value in values):
        raise SignatureError("invalid_tenant_scope_identity")
    return ":".join(values)


@dataclass(frozen=True)
class KeyRing:
    """The keys a fleet will ACCEPT right now.

    ``current`` signs and verifies.  ``previous`` verifies ONLY until
    ``previous_expires_at`` (epoch seconds); after that it is dead.  Both empty
    ⇒ fail-closed: nothing signs and nothing verifies.
    """

    current: str = ""
    previous: str = ""
    previous_expires_at: float = 0.0

    def signing_key(self) -> tuple[str, str]:
        """``(kid, secret)`` for signing.  Raises when unconfigured."""
        secret = (self.current or "").strip()
        if not secret:
            raise SignatureError("no_signing_key")
        return key_id(secret), secret

    def resolve(self, kid: str, *, now: float) -> str:
        """The secret for ``kid``, or ``""`` when unknown/retired.

        The previous key is resolvable ONLY while its overlap window is open —
        that is what makes retirement real rather than advisory.
        """
        cur = (self.current or "").strip()
        if cur and hmac.compare_digest(key_id(cur), kid):
            return cur
        prev = (self.previous or "").strip()
        if prev and hmac.compare_digest(key_id(prev), kid):
            if now <= float(self.previous_expires_at or 0.0):
                return prev
            logger.warning("qec.hmac.retired_key_rejected kid=%s", kid)
            return ""
        return ""

    def effective_overlap_seconds(self, *, now: float,
                                  skew_seconds: float = DEFAULT_SKEW_SECONDS) -> float:
        """How long an in-flight callback is ACTUALLY protected, in seconds.

        THE TRAP THIS NAMES (M3.4 / T-RS-02).  The overlap window and the
        signature SKEW window are independent, and the smaller one binds.  A
        callback signed under the outgoing key carries a timestamp, and rule 2
        of :func:`verify` rejects it once that timestamp is older than
        ``skew_seconds`` - whatever the key ring says.  So an operator who sets a
        24-hour overlap believing it drains a long-tailed fleet actually gets
        five minutes of protection, and every slower callback is refused as
        ``timestamp_expired`` with a key error nowhere in sight.

        Returns the MINIMUM of the two windows, which is the only number an
        operator planning a rotation can safely reason about.  Zero means the
        previous key is already retired.
        """
        remaining = float(self.previous_expires_at or 0.0) - float(now)
        if remaining <= 0:
            return 0.0
        return min(remaining, float(skew_seconds))

    def known_key_ids(self, *, now: float) -> list[str]:
        """Key ids that would currently resolve (diagnostics; never secrets)."""
        out = []
        if (self.current or "").strip():
            out.append(key_id(self.current.strip()))
        prev = (self.previous or "").strip()
        if prev and now <= float(self.previous_expires_at or 0.0):
            out.append(key_id(prev))
        return out

    @classmethod
    def from_env(
        cls,
        *,
        current_var: str = "QEC_EXPLORER_TOKEN",
        previous_var: str = "QEC_EXPLORER_TOKEN_PREVIOUS",
        overlap_var: str = "QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT",
        env: dict | None = None,
    ) -> "KeyRing":
        """Build a ring from the environment.

        ``QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT`` is epoch SECONDS.  An absent
        or unparseable value means the previous key is already retired (0.0),
        so a half-configured rotation fails CLOSED on the old key rather than
        accepting it indefinitely.
        """
        src = os.environ if env is None else env
        try:
            overlap = float(str(src.get(overlap_var, "") or "0").strip() or 0.0)
        except ValueError:
            overlap = 0.0
        ring = cls(
            current=str(src.get(current_var, "") or ""),
            previous=str(src.get(previous_var, "") or ""),
            previous_expires_at=overlap,
        )
        # An overlap longer than the skew window is not more protection - it is
        # FALSE CONFIDENCE. See `effective_overlap_seconds`. Warned at the point
        # of configuration so the operator learns it while planning a rotation
        # rather than while reading rejection logs during one.
        if ring.previous.strip() and overlap > 0:
            excess = overlap - time.time() - DEFAULT_SKEW_SECONDS
            if excess > 0:
                logger.warning(
                    "qec.hmac.overlap_exceeds_skew overlap_ends_in=%.0fs "
                    "skew=%ds - in-flight callbacks are protected for only the "
                    "SKEW window; anything slower is refused as "
                    "timestamp_expired regardless of the key",
                    overlap - time.time(), DEFAULT_SKEW_SECONDS)
        return ring


@dataclass
class NonceStore:
    """Bounded, TTL'd single-use nonce set (T-SEC-06 replay protection).

    In-process by design: a callback is delivered to ONE qe-central replica and
    the shared secret is per-fleet, so a per-process store is sufficient to make
    a replay fail.  ``ttl_seconds`` matches the signature skew window — outside
    it rule 2 already rejects, so forgetting is safe.
    """

    ttl_seconds: float = float(DEFAULT_SKEW_SECONDS)
    max_entries: int = MAX_NONCES
    _seen: dict[str, float] = field(default_factory=dict)

    def _evict(self, now: float) -> None:
        dead = [k for k, exp in self._seen.items() if exp <= now]
        for k in dead:
            self._seen.pop(k, None)
        if len(self._seen) > self.max_entries:
            # Oldest-expiry first; a flood can never push a LIVE nonce out
            # silently without also being visible in this log line.
            ordered = sorted(self._seen.items(), key=lambda kv: kv[1])
            for k, _ in ordered[: len(self._seen) - self.max_entries]:
                self._seen.pop(k, None)
            logger.warning("qec.hmac.nonce_store_pressure size=%d", len(self._seen))

    def seen(self, nonce: str, *, now: float) -> bool:
        self._evict(now)
        return nonce in self._seen

    def consume(self, nonce: str, *, now: float) -> bool:
        """Record ``nonce`` as used.  Returns False when it was ALREADY used."""
        self._evict(now)
        if nonce in self._seen:
            return False
        self._seen[nonce] = now + float(self.ttl_seconds)
        self._evict(now)          # trim AFTER inserting, so the cap is a real cap
        return True

    def clear(self) -> None:
        self._seen.clear()


def _canonical(kid: str, ts_ms: int, nonce: str, scope: str, digest: str) -> bytes:
    return "\n".join([SCHEME, kid, str(int(ts_ms)), nonce, scope, digest]).encode("utf-8")


def sign(
    body: bytes | None,
    *,
    keyring: KeyRing,
    scope: str = "",
    now: float | None = None,
    nonce: str | None = None,
) -> str:
    """Produce the ``X-QEC-Signature`` envelope for ``body``.

    ``scope`` binds the signature to one logical operation (the crawl id for a
    completion callback, the endpoint name for an oracle consultation).
    """
    kid, secret = keyring.signing_key()
    ts_ms = int((time.time() if now is None else now) * 1000)
    nonce_val = nonce or secrets.token_hex(16)
    digest = body_hash(body)
    mac = hmac.new(
        secret.encode("utf-8"),
        _canonical(kid, ts_ms, nonce_val, scope or "", digest),
        hashlib.sha256,
    ).hexdigest()
    return f"{SCHEME};kid={kid};ts={ts_ms};nonce={nonce_val};sig={mac}"


def parse_envelope(header: str) -> dict:
    """Parse a v2 signature header into its fields.  Raises on any malformation."""
    raw = (header or "").strip()
    if not raw:
        raise SignatureError("missing_signature")
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    if not parts or parts[0] != SCHEME:
        raise SignatureError("unsupported_scheme")
    fields: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            raise SignatureError("malformed_signature")
        key, _, value = part.partition("=")
        fields[key.strip()] = value.strip()
    for required in ("kid", "ts", "nonce", "sig"):
        if not fields.get(required):
            raise SignatureError("malformed_signature")
    try:
        fields["ts_ms"] = int(fields["ts"])
    except ValueError:
        raise SignatureError("malformed_timestamp")
    if len(fields["nonce"]) < 16 or len(fields["nonce"]) > 128:
        raise SignatureError("malformed_nonce")
    if len(fields["sig"]) != 64:
        raise SignatureError("malformed_signature")
    return fields


def verify(
    body: bytes | None,
    header: str,
    *,
    keyring: KeyRing,
    nonces: NonceStore,
    scope: str = "",
    skew_seconds: float = DEFAULT_SKEW_SECONDS,
    now: float | None = None,
) -> dict:
    """Verify a v2 signature.  Returns the parsed envelope, or RAISES.

    Fail-closed at every step; the nonce is consumed only AFTER the signature
    verifies, so an attacker cannot burn a legitimate nonce with a forged call.
    """
    clock = time.time() if now is None else float(now)
    fields = parse_envelope(header)

    secret = keyring.resolve(fields["kid"], now=clock)
    if not secret:
        raise SignatureError("unknown_key_id")

    delta = clock - (fields["ts_ms"] / 1000.0)
    if delta > float(skew_seconds):
        raise SignatureError("timestamp_expired")
    if delta < -float(skew_seconds):
        raise SignatureError("timestamp_in_future")

    if nonces.seen(fields["nonce"], now=clock):
        raise SignatureError("nonce_replayed")

    expected = hmac.new(
        secret.encode("utf-8"),
        _canonical(fields["kid"], fields["ts_ms"], fields["nonce"], scope or "",
                   body_hash(body)),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, fields["sig"]):
        raise SignatureError("bad_signature")

    if not nonces.consume(fields["nonce"], now=clock):
        # Lost a race against a concurrent replay of the same nonce.
        raise SignatureError("nonce_replayed")
    return fields


__all__ = [
    "DEFAULT_SKEW_SECONDS",
    "KeyRing",
    "MAX_NONCES",
    "NonceStore",
    "SCHEME",
    "SIGNATURE_HEADER",
    "SignatureError",
    "TOKEN_HEADER",
    "body_hash",
    "key_id",
    "parse_envelope",
    "sign",
    "tenant_scope",
    "verify",
]
