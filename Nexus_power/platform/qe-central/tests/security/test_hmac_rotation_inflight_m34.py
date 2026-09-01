"""M3.4 / T-RS-02 - ZERO LEGITIMATE CALLBACKS REJECTED BY A KEY ROTATION.

WHAT IS ALREADY PROVEN ELSEWHERE, and deliberately not repeated here.
``tests/security/test_r7_hmac_replay_and_rotation.py`` already gates the
primitive: replay, stale/future timestamps, modified bodies, wrong keys,
malformed envelopes, an unconfigured ring, the k1 -> k2 -> retire sequence, and
the half-configured overlap that fails closed.  The ``KeyRing`` overlap window
is BUILT, and this milestone does not rebuild it.

WHAT WAS NOT PROVEN, and is the actual T-RS-02 obligation.  Those tests check
acceptance at chosen INSTANTS.  A rotation in production is not an instant - it
is an interval during which a POPULATION of callbacks, signed at many different
moments under the outgoing key, arrives at a fleet that has already moved to the
incoming one.  The property an operator needs is a COUNT: across that whole
interval, how many legitimate callbacks were refused?  The answer must be zero,
and "zero" is a claim about a population, not about a sample of one.

So these proofs drive a fleet of workers across a rotation boundary and count
outcomes explicitly - successes, rejections, and the reason for every rejection.
A single unexplained refusal fails the gate.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.security import hmac_auth  # noqa: E402
from app.security.hmac_auth import KeyRing, NonceStore, SignatureError  # noqa: E402

K1 = "K1-fleet-secret-4a91c0de7f2b8365"
K2 = "K2-fleet-secret-19bd47ea0c635fd8"
K3 = "K3-fleet-secret-77c2f0aa9d1e4b6c"

T0 = 1_800_000_000.0
OVERLAP = 600.0


def _callback(i: int) -> tuple[bytes, str]:
    """One worker's completion callback: distinct body AND distinct scope.

    Distinct per crawl on purpose - a shared scope would let one nonce collision
    masquerade as a rotation failure, which is the confusion this gate exists to
    rule out.
    """
    crawl = "crawl%04d" % i
    return (b'{"crawl_id":"%s","status":"completed"}' % crawl.encode(),
            "complete:%s" % crawl)


def _drive_rotation(*, n: int, deliver_delay: float, overlap: float = OVERLAP):
    """Sign ``n`` callbacks under K1 BEFORE a rotation, deliver them AFTER it.

    Returns ``(accepted, rejections)`` where ``rejections`` is a list of
    ``(index, reason)`` so a failure names which callback died and why.
    """
    nonces = NonceStore()
    # Every callback is signed while K1 is still the fleet's current key.
    signed = []
    for i in range(n):
        body, scope = _callback(i)
        # Spread the signing times across the pre-rotation window, so the
        # population carries a realistic spread of ages rather than one instant.
        sign_at = T0 + (i % 60)
        header = hmac_auth.sign(body, keyring=KeyRing(current=K1),
                                scope=scope, now=sign_at)
        signed.append((i, body, scope, header, sign_at))

    # -- THE ROTATION: K2 becomes current, K1 enters its overlap window. ----
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + overlap)

    accepted = 0
    rejections: list[tuple[int, str]] = []
    for i, body, scope, header, sign_at in signed:
        deliver_at = sign_at + deliver_delay
        try:
            hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                             scope=scope, now=deliver_at)
            accepted += 1
        except SignatureError as exc:
            rejections.append((i, exc.reason))
    return accepted, rejections


# ── THE CORE OBLIGATION ────────────────────────────────────────────────────

def test_zero_legitimate_callbacks_are_rejected_by_a_normal_rotation():
    """250 in-flight callbacks cross the rotation. None may be refused."""
    accepted, rejections = _drive_rotation(n=250, deliver_delay=30.0)
    assert rejections == [], (
        "a normal rotation refused %d of 250 legitimate callbacks: %r"
        % (len(rejections), rejections[:10]))
    assert accepted == 250, "expected every callback to be accepted, got %d" % accepted


def test_the_count_is_zero_everywhere_inside_the_effective_window():
    """Delivery late in the window is the interesting case, not the early one.

    A rotation that only worked for callbacks delivered promptly would pass a
    single-instant test and still drop the slowest workers in the fleet - which
    are exactly the ones a long crawl produces.
    """
    skew = hmac_auth.DEFAULT_SKEW_SECONDS
    for delay in (1.0, 60.0, skew - 30.0, skew - 1.0):
        accepted, rejections = _drive_rotation(n=40, deliver_delay=delay)
        assert rejections == [], (
            "callbacks delivered %.0fs after signing were refused: %r"
            % (delay, rejections[:5]))
        assert accepted == 40


def test_the_skew_window_binds_the_overlap_and_the_ring_says_so():
    """MEASURED, NOT ASSUMED: a long overlap does NOT protect a slow callback.

    The overlap window and the signature skew window are independent, and the
    smaller one binds. A 600s overlap looks like ten minutes of drain time and
    delivers five, because rule 2 of `verify` rejects a stale TIMESTAMP before
    the key ring is ever consulted - so the refusals read `timestamp_expired`
    and an operator debugging a rotation sees no key error at all.

    This is an operational LIMIT, gated here so it stays known: the effective
    drain window is min(overlap, skew), and `effective_overlap_seconds` is the
    number to plan a rotation against.
    """
    skew = hmac_auth.DEFAULT_SKEW_SECONDS
    beyond = float(skew) + 60.0
    assert beyond < OVERLAP, "the fixture must deliver INSIDE the overlap window"

    accepted, rejections = _drive_rotation(n=20, deliver_delay=beyond)
    assert accepted == 0
    assert {reason for _, reason in rejections} == {"timestamp_expired"}, rejections

    # The ring reports the honest number rather than the configured one.
    ring = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + OVERLAP)
    assert ring.effective_overlap_seconds(now=T0) == float(skew)
    # Once the configured overlap is the smaller of the two, IT binds instead.
    assert ring.effective_overlap_seconds(now=T0 + OVERLAP - 10) == 10.0
    # And a retired key protects nothing.
    assert ring.effective_overlap_seconds(now=T0 + OVERLAP + 1) == 0.0


def test_a_second_rotation_during_the_first_overlap_still_loses_nothing_current():
    """Back-to-back rotations retire K1 - and that is CORRECT, not a defect.

    A ring holds one previous key. Rotating twice inside one overlap therefore
    drops K1, and any K1 callback still in flight WILL be refused. This is the
    honest limit of a two-key ring, and it is asserted here so it is a KNOWN
    operational constraint (never rotate twice inside one overlap) rather than a
    surprise discovered during an incident.
    """
    nonces = NonceStore()
    body, scope = _callback(1)
    k1_header = hmac_auth.sign(body, keyring=KeyRing(current=K1), scope=scope, now=T0)

    # Two rotations in quick succession: K1 -> K2 -> K3. K1 is now off the ring.
    twice = KeyRing(current=K3, previous=K2, previous_expires_at=T0 + OVERLAP)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(body, k1_header, keyring=twice, nonces=nonces,
                         scope=scope, now=T0 + 10)
    assert exc.value.reason == "unknown_key_id"

    # K2, the key of the immediately-previous generation, is still honoured.
    body2, scope2 = _callback(2)
    k2_header = hmac_auth.sign(body2, keyring=KeyRing(current=K2), scope=scope2, now=T0)
    assert hmac_auth.verify(body2, k2_header, keyring=twice, nonces=nonces,
                            scope=scope2, now=T0 + 10)


def test_after_the_overlap_closes_the_old_key_is_dead_and_the_new_one_lives():
    """Retirement must be real, or an overlap window is only advisory."""
    nonces = NonceStore()
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + OVERLAP)
    after = T0 + OVERLAP + 1

    body, scope = _callback(7)
    stale = hmac_auth.sign(body, keyring=KeyRing(current=K1), scope=scope, now=after)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(body, stale, keyring=rotated, nonces=nonces,
                         scope=scope, now=after)
    assert exc.value.reason == "unknown_key_id"

    body2, scope2 = _callback(8)
    fresh = hmac_auth.sign(body2, keyring=rotated, scope=scope2, now=after)
    assert hmac_auth.verify(body2, fresh, keyring=rotated, nonces=nonces,
                            scope=scope2, now=after)


# ── The adversarial population: nothing illegitimate slips through ─────────

def test_a_rotation_does_not_widen_the_door_for_anything_illegitimate():
    """The same population, corrupted four ways, must be refused four ways.

    Zero false rejections is only half the property. A rotation that accepted
    everything would also score zero, so the gate asserts the refusals too, with
    the reason for each - the counts must be complete AND correctly attributed.
    """
    nonces = NonceStore()
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + OVERLAP)
    now = T0 + 30
    refused: dict[str, str] = {}

    # 1. an unknown key, signed by someone who never held a fleet secret
    body, scope = _callback(11)
    forged = hmac_auth.sign(body, keyring=KeyRing(current="not-a-fleet-key"),
                            scope=scope, now=now)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(body, forged, keyring=rotated, nonces=nonces,
                         scope=scope, now=now)
    refused["unknown_key"] = exc.value.reason

    # 2. a body modified after signing
    body, scope = _callback(12)
    header = hmac_auth.sign(body, keyring=rotated, scope=scope, now=now)
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(body + b" ", header, keyring=rotated, nonces=nonces,
                         scope=scope, now=now)
    refused["modified_body"] = exc.value.reason

    # 3. a malformed envelope
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(b"{}", "this-is-not-an-envelope", keyring=rotated,
                         nonces=nonces, scope="complete:x", now=now)
    refused["malformed"] = exc.value.reason

    # 4. a replay of a callback that was already accepted DURING the rotation
    body, scope = _callback(13)
    header = hmac_auth.sign(body, keyring=KeyRing(current=K1), scope=scope, now=now)
    assert hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                            scope=scope, now=now), "precondition: first delivery"
    with pytest.raises(SignatureError) as exc:
        hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                         scope=scope, now=now)
    refused["replay"] = exc.value.reason

    assert set(refused) == {"unknown_key", "modified_body", "malformed", "replay"}
    assert all(reason for reason in refused.values()), refused
    # Replay protection must survive the rotation: an overlap window widens which
    # KEYS are accepted, never how many times one callback may be delivered.
    assert refused["replay"] == "nonce_replayed", refused


def test_a_rejected_forgery_does_not_burn_a_legitimate_callbacks_nonce():
    """A forged delivery during a rotation must not disable the real one.

    If a failed verify consumed the nonce, an attacker could deny-of-service a
    fleet mid-rotation by replaying garbage with guessed nonces, and the genuine
    callback behind each one would then be refused as a replay.
    """
    nonces = NonceStore()
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + OVERLAP)
    now = T0 + 5
    body, scope = _callback(21)
    genuine = hmac_auth.sign(body, keyring=KeyRing(current=K1), scope=scope, now=now)

    # Same envelope, wrong signature: the nonce is present but verification fails.
    env = hmac_auth.parse_envelope(genuine)
    tampered = genuine.replace(env["sig"], "0" * len(env["sig"]))
    with pytest.raises(SignatureError):
        hmac_auth.verify(body, tampered, keyring=rotated, nonces=nonces,
                         scope=scope, now=now)

    # The genuine callback, arriving second, is still accepted.
    assert hmac_auth.verify(body, genuine, keyring=rotated, nonces=nonces,
                            scope=scope, now=now)


# ── The operator-facing summary (the evidence this milestone must hand over) ──

def test_rotation_produces_a_reportable_success_failure_count():
    """The milestone asks for counts, so the gate produces them explicitly."""
    accepted, rejections = _drive_rotation(n=100, deliver_delay=45.0)
    report = {
        "delivered": accepted + len(rejections),
        "accepted": accepted,
        "rejected": len(rejections),
        "reasons": sorted({r for _, r in rejections}),
    }
    assert report == {"delivered": 100, "accepted": 100, "rejected": 0,
                      "reasons": []}, report
