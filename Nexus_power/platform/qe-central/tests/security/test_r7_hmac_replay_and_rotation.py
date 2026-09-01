"""R7 — HMAC REPLAY, and the T-SEC-11 key-rotation seam.

ATTACK
======
The callback signature was ``HMAC-SHA256(secret, raw_body)``.  It proved the
sender held the fleet secret and nothing else — no time, no uniqueness, no
binding to a crawl.  So anyone who ever observed one valid ``/complete``
callback could POST the identical bytes again, forever: re-running the substrate
write, the auto-generate, and the autowalk dispatch cascade each time.  The same
signature also authenticated a call about a DIFFERENT crawl, because the crawl
id was not part of what was signed.

And because there was exactly one global secret with no key id, rotating it was
a flag day that rejected every callback already in flight.

EXPECTED
========
Replay rejected.  Stale rejected.  Future-dated rejected.  Modified body
rejected.  Wrong key rejected.  Re-scoped signature rejected.  A rotation
accepts both keys during the overlap and only the new one after it.
"""
from __future__ import annotations

import time

import pytest

from app.security import hmac_auth
from app.security.hmac_auth import KeyRing, NonceStore, SignatureError

K1 = "K1-fleet-secret-4a91c0de7f2b8365"
K2 = "K2-fleet-secret-19bd47ea0c635fd8"
BODY = b'{"crawl_id":"aaaa","status":"completed"}'
SCOPE = "complete:aaaa"


def _ring(**kw) -> KeyRing:
    return KeyRing(current=kw.pop("current", K1), **kw)


# ── R7: the replay itself ──────────────────────────────────────────────────

def test_r7_an_identical_valid_callback_is_accepted_once_and_only_once():
    """THE attack, end to end."""
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE)

    first = hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE)
    assert first["kid"] == hmac_auth.key_id(K1)

    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE)
    assert exc.value.reason == "nonce_replayed"


def test_r7_replay_is_refused_however_many_times_it_is_tried():
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE)
    hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE)
    for _ in range(20):
        with pytest.raises(SignatureError):
            hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE)


def test_r7_a_nonce_is_single_use_ACROSS_endpoints():
    """One store for the whole service: a nonce burned at ``/pick-advance``
    cannot be reused at ``/complete``.  A per-endpoint store would let an
    attacker replay each captured envelope once per route."""
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope="pick-advance:aaaa")
    hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces,
                     scope="pick-advance:aaaa")
    fields = hmac_auth.parse_envelope(header)
    # Re-sign the same nonce for another scope — an attacker controls the header.
    forged = hmac_auth.sign(BODY, keyring=ring, scope="complete:aaaa",
                            nonce=fields["nonce"])
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, forged, keyring=ring, nonces=nonces,
                         scope="complete:aaaa")
    assert exc.value.reason == "nonce_replayed"


# ── freshness (deterministic clocks) ───────────────────────────────────────

def test_r7_a_stale_timestamp_is_rejected():
    ring, nonces = _ring(), NonceStore()
    t0 = 1_800_000_000.0
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE, now=t0)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE,
                         skew_seconds=300, now=t0 + 301)
    assert exc.value.reason == "timestamp_expired"


def test_r7_a_future_timestamp_beyond_skew_is_rejected():
    """Not just old — a far-future stamp would otherwise buy an attacker an
    envelope that stays valid for as long as they chose."""
    ring, nonces = _ring(), NonceStore()
    t0 = 1_800_000_000.0
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE, now=t0 + 3_600)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE,
                         skew_seconds=300, now=t0)
    assert exc.value.reason == "timestamp_in_future"


@pytest.mark.parametrize("offset", [-299, -1, 0, 1, 299])
def test_r7_inside_the_skew_window_is_accepted(offset):
    """The boundary, both directions — a legitimate callback from a host with a
    slightly wrong clock must not be collateral damage."""
    ring, nonces = _ring(), NonceStore()
    t0 = 1_800_000_000.0
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE, now=t0 + offset)
    assert hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces,
                            scope=SCOPE, skew_seconds=300, now=t0)


# ── integrity + scope ──────────────────────────────────────────────────────

def test_r7_a_modified_body_is_rejected():
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY + b" ", header, keyring=ring, nonces=nonces,
                         scope=SCOPE)
    assert exc.value.reason == "bad_signature"


def test_r7_a_signature_for_one_crawl_cannot_authenticate_another():
    """Scope binding: this is what stops a captured envelope being re-pointed."""
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope="complete:aaaa")
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces,
                         scope="complete:bbbb")
    assert exc.value.reason == "bad_signature"


def test_r7_a_signature_for_one_tenant_cannot_authenticate_another():
    """Team F R4: a fleet token is never cross-tenant callback authority."""
    ring, nonces = _ring(), NonceStore()
    scoped = hmac_auth.tenant_scope("complete", "tenant-a", "aaaa")
    header = hmac_auth.sign(BODY, keyring=ring, scope=scoped)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(
            BODY, header, keyring=ring, nonces=nonces,
            scope=hmac_auth.tenant_scope("complete", "tenant-b", "aaaa"),
        )
    assert exc.value.reason == "bad_signature"


def test_r7_a_wrong_key_is_rejected():
    nonces = NonceStore()
    header = hmac_auth.sign(BODY, keyring=KeyRing(current=K2), scope=SCOPE)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, header, keyring=KeyRing(current=K1),
                         nonces=nonces, scope=SCOPE)
    assert exc.value.reason == "unknown_key_id"


@pytest.mark.parametrize("header,reason", [
    ("", "missing_signature"),
    ("deadbeef" * 8, "unsupported_scheme"),                    # the v1 form
    ("v1;kid=x;ts=1;nonce=y;sig=z", "unsupported_scheme"),
    ("v2;kid=x", "malformed_signature"),
    ("v2;kid=x;ts=notanumber;nonce=" + "a" * 32 + ";sig=" + "b" * 64,
     "malformed_timestamp"),
    ("v2;kid=x;ts=1;nonce=short;sig=" + "b" * 64, "malformed_nonce"),
    ("v2;kid=x;ts=1;nonce=" + "a" * 32 + ";sig=tooshort", "malformed_signature"),
])
def test_r7_malformed_envelopes_are_rejected_by_category(header, reason):
    with pytest.raises(SignatureError) as exc:
        hmac_auth.parse_envelope(header)
    assert exc.value.reason == reason


def test_r7_the_legacy_v1_signature_no_longer_verifies():
    """A bare HMAC hex digest — the old format — must not be accepted.

    Accepting it "for compatibility" would leave the replay hole open behind a
    flag, which is not a fix."""
    import hashlib
    import hmac as _hmac

    legacy = _hmac.new(K1.encode(), BODY, hashlib.sha256).hexdigest()
    with pytest.raises(SignatureError):
        hmac_auth.verify(BODY, legacy, keyring=_ring(), nonces=NonceStore(),
                         scope=SCOPE)


def test_r7_an_unconfigured_keyring_verifies_nothing():
    with pytest.raises(SignatureError):
        hmac_auth.verify(BODY, "v2;kid=x;ts=1;nonce=" + "a" * 32 + ";sig=" + "b" * 64,
                         keyring=KeyRing(), nonces=NonceStore(), scope=SCOPE)
    with pytest.raises(SignatureError):
        hmac_auth.sign(BODY, keyring=KeyRing(), scope=SCOPE)


def test_r7_a_failed_verify_does_not_burn_a_legitimate_nonce():
    """Ordering matters: if the nonce were consumed BEFORE the signature check,
    an attacker could pre-burn nonces and DoS the real explorer."""
    ring, nonces = _ring(), NonceStore()
    header = hmac_auth.sign(BODY, keyring=ring, scope=SCOPE)
    with pytest.raises(SignatureError):
        hmac_auth.verify(b"tampered", header, keyring=ring, nonces=nonces,
                         scope=SCOPE)
    # the genuine callback still works
    assert hmac_auth.verify(BODY, header, keyring=ring, nonces=nonces, scope=SCOPE)


# ── T-SEC-11: rotation ─────────────────────────────────────────────────────

def test_rotation_sequence_k1_active_k2_introduced_k1_retired():
    """The full sequence the brief specifies, one assertion per step."""
    now = 1_800_000_000.0
    overlap_until = now + 600

    # 1. K1 active: a K1 callback is accepted.
    ring1 = KeyRing(current=K1)
    n = NonceStore()
    h1 = hmac_auth.sign(BODY, keyring=ring1, scope=SCOPE, now=now)
    assert hmac_auth.verify(BODY, h1, keyring=ring1, nonces=n, scope=SCOPE, now=now)

    # 2. K2 introduced, K1 in overlap.
    ring2 = KeyRing(current=K2, previous=K1, previous_expires_at=overlap_until)

    # 3. a K2 callback is accepted.
    h2 = hmac_auth.sign(BODY, keyring=ring2, scope=SCOPE, now=now)
    assert hmac_auth.verify(BODY, h2, keyring=ring2, nonces=n, scope=SCOPE, now=now)
    assert hmac_auth.parse_envelope(h2)["kid"] == hmac_auth.key_id(K2)

    # 4. an IN-FLIGHT K1 callback is still accepted during the overlap.
    h1b = hmac_auth.sign(BODY, keyring=KeyRing(current=K1), scope=SCOPE, now=now)
    assert hmac_auth.verify(BODY, h1b, keyring=ring2, nonces=n, scope=SCOPE,
                            now=now + 60)

    # 5. after the overlap closes, K1 is refused…
    h1c = hmac_auth.sign(BODY, keyring=KeyRing(current=K1), scope=SCOPE,
                         now=overlap_until + 1)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(BODY, h1c, keyring=ring2, nonces=n, scope=SCOPE,
                         now=overlap_until + 1)
    assert exc.value.reason == "unknown_key_id"

    # 6. …and K2 keeps working, so no valid in-flight callback was rejected
    #    merely because a rotation happened.
    h2b = hmac_auth.sign(BODY, keyring=ring2, scope=SCOPE, now=overlap_until + 1)
    assert hmac_auth.verify(BODY, h2b, keyring=ring2, nonces=n, scope=SCOPE,
                            now=overlap_until + 1)


def test_rotation_signs_with_the_CURRENT_key_only():
    ring = KeyRing(current=K2, previous=K1, previous_expires_at=time.time() + 600)
    assert ring.signing_key()[1] == K2


def test_rotation_key_id_is_explicit_deterministic_and_not_the_secret():
    kid = hmac_auth.key_id(K1)
    assert kid == hmac_auth.key_id(K1) and len(kid) == 16
    assert K1 not in kid and kid not in K1
    assert hmac_auth.key_id(K1) != hmac_auth.key_id(K2)


def test_rotation_a_half_configured_overlap_fails_closed():
    """No deadline (or an unparseable one) retires the old key IMMEDIATELY.

    The dangerous failure mode is an operator who sets ``_PREVIOUS`` and forgets
    the deadline, leaving a superseded key valid forever."""
    ring = KeyRing(current=K2, previous=K1)     # previous_expires_at defaults to 0
    assert ring.resolve(hmac_auth.key_id(K1), now=time.time()) == ""

    env = {"QEC_EXPLORER_TOKEN": K2, "QEC_EXPLORER_TOKEN_PREVIOUS": K1,
           "QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT": "not-a-number"}
    assert KeyRing.from_env(env=env).previous_expires_at == 0.0


def test_rotation_is_wired_into_the_live_settings(monkeypatch):
    """Not just the primitive — the deployed object builds the same ring."""
    from app.clients.config import phase1_settings

    monkeypatch.setattr(phase1_settings, "explorer_token", K2)
    monkeypatch.setattr(phase1_settings, "explorer_token_previous", K1)
    monkeypatch.setattr(phase1_settings, "explorer_token_previous_expires_at",
                        time.time() + 600)
    ring = phase1_settings.keyring()
    assert ring.current == K2 and ring.previous == K1
    assert sorted(ring.known_key_ids(now=time.time())) == sorted(
        [hmac_auth.key_id(K2), hmac_auth.key_id(K1)])


# ── the settings-level verify path (what the endpoints actually call) ──────

def test_r7_the_live_verify_helper_refuses_a_replay(monkeypatch, fresh_nonces):
    from app.clients.config import phase1_settings

    monkeypatch.setattr(phase1_settings, "explorer_token", K1)
    monkeypatch.setattr(phase1_settings, "explorer_token_previous", "")
    header = phase1_settings.sign_payload(BODY, scope=SCOPE)
    assert phase1_settings.verify_signature(BODY, header, scope=SCOPE) is True
    assert phase1_settings.verify_signature(BODY, header, scope=SCOPE) is False


def test_r7_the_live_verify_helper_never_raises(monkeypatch, fresh_nonces):
    """The boolean form is used inside request handlers; a raise there would be
    a 500 where a 401 belongs."""
    from app.clients.config import phase1_settings

    monkeypatch.setattr(phase1_settings, "explorer_token", K1)
    for bad in ("", "garbage", "v2;kid=zz;ts=1;nonce=" + "a" * 32 + ";sig=" + "b" * 64):
        assert phase1_settings.verify_signature(BODY, bad, scope=SCOPE) is False


def test_r7_the_two_services_share_a_byte_identical_signing_module():
    """qe-central and qe-explorer do not share a package.

    A divergence between the two copies would reject every genuine callback, so
    the identity is pinned rather than assumed."""
    import hashlib
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[4]
    central = (root / "platform/qe-central/app/security/hmac_auth.py").read_bytes()
    explorer = (root / "engines/qe-explorer/app/hmac_auth.py").read_bytes()
    assert hashlib.sha256(central).hexdigest() == hashlib.sha256(explorer).hexdigest()


def test_r7_the_nonce_store_is_bounded():
    """Memory must not grow with traffic — including hostile traffic."""
    store = NonceStore(ttl_seconds=1000, max_entries=100)
    for i in range(1000):
        store.consume(f"nonce-{i}", now=1_800_000_000.0)
    assert len(store._seen) <= 100
