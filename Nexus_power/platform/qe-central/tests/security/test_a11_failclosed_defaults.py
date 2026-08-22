"""A11.5 — THE FAIL-CLOSED DEFAULTS: the branches nothing ever reaches.

WHY THIS FILE EXISTS
====================
Five of the verifier's refusal codes had no test anywhere — not in
``test_a11_attestation_redteam.py``, not in the independent certification
harness:

    no_proof   clock_domain_error   issued_in_future
    unsupported_version   verifier_error

All five were probed when the gap was found and all five WORK. So nothing here
is a bug fix. It is regression protection for the branches that decide what
happens when something is absent, malformed, or impossible — and those are
precisely the branches that rot unnoticed, because no feature test ever reaches
them.

``no_proof`` is the answer for every uncertified crawl in the fleet, forever, so
it is exercised constantly in production and never in a test.
``clock_domain_error`` guards a defect class that ALREADY SHIPPED here: R8, in
which ``expires_at_ms`` (epoch millis) was compared against ``crawler.now_ms()``
(millis since the crawl started, a number in the thousands). ``5_000 <
1_760_000_000_000`` is true and stays true for about fifty thousand years, so
the freshness gate could not expire anything and a months-lapsed attestation
authorised an irreversible submit. The guard is the fix for that, and the fix
had no test.

EVERY DENIAL HERE IS PAIRED WITH A FALSIFICATION CONTROL
========================================================
An assertion that a verdict is DENIED is satisfied by ANY breakage — a typo in a
fixture, an unrelated exception, a trust store that authorises nothing. Six of
those landed in this repository in a single day (the "blind verifier" class). So
every test below also proves the SAME setup AUTHORISES once the one defect under
test is removed. Without that pairing these would be six more blind verifiers,
and a green suite would mean nothing.

Verified against the REAL SHIPPING verifier loaded from
``engines/qe-explorer/app/attest.py`` by path — see ``_a11_kit``.
"""
from __future__ import annotations

import pathlib

import pytest

from _a11_kit import (
    APP,
    ENV,
    TENANT,
    FakeSession,
    attest,
    bootstrap_issuer_key,
    make_environment_row,
    make_envelope_service,
    make_provisioning_record,
    now_ms,
    trust_store,
    verify,
)

from app.services import attestation_issuer as issuer
from app.services import attestation_keys as keys
from app.services import attestation_revocation as revocation


@pytest.fixture
def envelope(tmp_path):
    return make_envelope_service(tmp_path)


@pytest.fixture
def cache():
    return revocation.RevocationCache()


@pytest.fixture
def session():
    s = FakeSession()
    s.seed(make_provisioning_record())
    s.seed(make_environment_row())
    return s


@pytest.fixture
async def key(session, envelope):
    return await bootstrap_issuer_key(session, envelope)


@pytest.fixture
def trust(key):
    return trust_store([key])


async def _issue(session, envelope, cache, *, crawl_id="crawl-001", **kw):
    return await issuer.issue_for_crawl(
        session, envelope, tenant_id=TENANT, app_id=APP, environment_id=ENV,
        crawl_id=crawl_id, revocation_cache=cache, **kw)


async def _resign(session, envelope, claims):
    """Sign arbitrary claims with the REAL issuer key.

    Several guards below are reachable only by a proof the issuer REFUSES to
    mint — an unknown claims version, a timestamp in the future. The signature
    must still be genuine, or the verifier stops at ``bad_signature`` and the
    guard under test is never reached.
    """
    async with keys.active_signer(session, envelope) as signer:
        return signer.sign_claims(claims)


async def _proof_with(session, envelope, cache, **claim_overrides):
    """A genuine attestation whose PROOF claims carry ``claim_overrides``.

    The revocation half is left genuine and untouched, so a denial can only come
    from the proof — never from a revocation list that happened to be unusable
    for an unrelated reason.
    """
    issued = await _issue(session, envelope, cache)
    claims = dict(issued.attestation["proof"]["claims"])
    claims.update(claim_overrides)
    return {"proof": await _resign(session, envelope, claims),
            "revocations": issued.attestation["revocations"]}


# ── 1. no_proof — the answer on every uncertified crawl, forever ───────────

@pytest.mark.parametrize("payload,shape", [
    (None, "None"),
    ({}, "empty dict"),
    ({"revocations": {}}, "revocations but no proof"),
    ({"proof": None}, "explicit null proof"),
    ({"proof": "a-string"}, "proof is not a mapping"),
    ("not-a-mapping", "payload is not a mapping"),
    ([], "payload is a list"),
])
async def test_a_dispatch_with_no_proof_is_denied(
        session, envelope, cache, key, trust, payload, shape):
    """THE DEFAULT-DENY THE WHOLE FLEET RESTS ON.

    Every crawl that is not walk-certified — almost all of them, and every
    production one — reaches exactly this branch. If it ever returned an
    authorising verdict, walk mutation would be ON BY DEFAULT and nothing
    downstream would notice, because nothing downstream re-checks.

    Seven shapes, because "absent" arrives in more than one form and a guard
    that only handles the tidy one is not a guard.
    """
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False, f"a dispatch with {shape} was AUTHORISED"
    assert verdict.reason == attest.AttestReason.NO_PROOF
    # A denied verdict hands out NO grant. A caller that reads the budget
    # without first checking `authorized` must still get nothing.
    assert verdict.max_mutations_per_step == 0
    assert verdict.proof_id == ""
    assert verdict.env_kind == ""


async def test_the_no_proof_control_authorises(session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL for the seven above.

    Same verifier, same trust store, same crawl id — with a proof present. If
    this fails, those seven denials prove nothing: they would be describing a
    verifier that refuses everything.
    """
    issued = await _issue(session, envelope, cache)
    assert verify(issued.attestation, trust=trust,
                  crawl_id="crawl-001").authorized is True


# ── 2. clock_domain_error — the R8 defect class, guarded on both sides ─────

@pytest.mark.parametrize("now_reading,what", [
    (5_000, "a crawl's monotonic since-start reading"),
    (0, "a zeroed clock"),
    (1_760_000_000, "epoch SECONDS mistaken for millis"),
])
async def test_a_non_epoch_now_is_refused_rather_than_compared(
        session, envelope, cache, key, trust, now_reading, what):
    """R8, GUARDED. The verifier REFUSES rather than compare across clock
    domains.

    This is not hypothetical — see the module docstring. The fix was to refuse a
    "now" below the plausible epoch floor instead of comparing it, and that
    refusal is what is under test.
    """
    issued = await _issue(session, envelope, cache)
    verdict = verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                     now_epoch_ms=now_reading)
    assert verdict.authorized is False, f"{what} was accepted as a clock"
    assert verdict.reason == attest.AttestReason.CLOCK_DOMAIN_ERROR


async def test_a_claims_timestamp_below_the_epoch_floor_is_refused(
        session, envelope, cache, key, trust):
    """The same doctrine on the OTHER side of the comparison.

    The test above rejects a bad ``now``. This rejects a bad ``issued_at_ms``
    inside the SIGNED claims — reachable only by re-signing, because the issuer
    refuses to mint it (``IssuerError``), which is itself the belt to this
    braces.
    """
    payload = await _proof_with(session, envelope, cache,
                                issued_at_ms=5_000, expires_at_ms=605_000)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.CLOCK_DOMAIN_ERROR


async def test_the_clock_domain_control_authorises(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: a real epoch-ms ``now`` on the same attestation
    authorises, so the refusals above are about the clock and nothing else."""
    issued = await _issue(session, envelope, cache)
    assert verify(issued.attestation, trust=trust, crawl_id="crawl-001",
                  now_epoch_ms=now_ms()).authorized is True


# ── 3. issued_in_future — bounded clock skew, not unbounded trust ──────────

async def test_a_proof_issued_beyond_the_skew_window_is_refused(
        session, envelope, cache, key, trust):
    """A proof stamped in the future extends its own usable life.

    Every freshness check measures from ``issued_at_ms``, so without this bound
    an issuer with a fast clock — or an attacker able to influence one — mints
    proofs that outlive the fleet's lifetime ceiling.
    """
    future = now_ms() + 3_600_000          # an hour ahead; skew allows 5 minutes
    payload = await _proof_with(session, envelope, cache,
                                issued_at_ms=future,
                                expires_at_ms=future + 600_000)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.ISSUED_IN_FUTURE


async def test_a_proof_inside_the_skew_window_is_accepted(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL, and the reason the window is not zero.

    Issuer and verifier are different machines. A proof stamped slightly ahead
    is ordinary clock skew, not an attack, and refusing it would make dispatch
    intermittently flaky in a way that looks like a fleet fault.
    """
    slightly = now_ms() + 60_000           # 1 minute ahead, inside the 5 min skew
    payload = await _proof_with(session, envelope, cache,
                                issued_at_ms=slightly,
                                expires_at_ms=slightly + 600_000)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is True, verdict.reason


# ── 4. unsupported_version — refuse, never reinterpret ─────────────────────

@pytest.mark.parametrize("version", [0, 2, 99, -1])
async def test_an_unknown_claims_version_is_refused_not_reinterpreted(
        session, envelope, cache, key, trust, version):
    """A v2 proof read under v1 field meanings is a silent misinterpretation.

    The danger is not that v2 is unknown; it is that its fields would LOOK
    familiar. If a future version redefined ``max_walk_mutations_per_step`` as a
    per-crawl total rather than per-step, a v1 reader grants far more than the
    issuer intended — with a valid signature over it. Refusing an unknown
    version is the only safe reading, and it must happen before any field is
    acted on.
    """
    payload = await _proof_with(session, envelope, cache, v=version)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.UNSUPPORTED_VERSION


async def test_the_current_version_control_authorises(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: re-signing with the CURRENT version through the
    same helper authorises — so the four refusals above are about the version
    number, not about the re-signing path being broken."""
    payload = await _proof_with(session, envelope, cache,
                                v=attest.CLAIMS_VERSION)
    assert verify(payload, trust=trust, crawl_id="crawl-001").authorized is True


# ── 5. verifier_error — a verifier that throws is one whose caller may swallow ─

async def test_an_internal_error_denies_instead_of_raising(
        session, envelope, cache, key, trust):
    """THE CATCH-ALL, AND WHY IT IS A SECURITY CONTROL.

    ``verify_provisioning_proof`` must never raise. A verifier that throws is a
    verifier whose caller might catch and carry on — and the ``except`` block
    that swallows it will be somewhere far away, written by someone who did not
    know a denial was being discarded.

    Proven by injecting a fault from inside the payload, which is the one input
    the verifier does not control. Note the test would ERROR rather than fail if
    the exception escaped, which is itself the assertion.
    """
    class _Exploding(dict):
        def get(self, key, default=None):
            raise RuntimeError("injected fault from an untrusted payload")

    verdict = verify(_Exploding(), trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.VERIFIER_ERROR
    assert verdict.max_mutations_per_step == 0


async def test_the_verifier_error_control_authorises(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: an ordinary dict through the same call path
    authorises, so the denial above is caused by the injected fault and not by
    the verifier refusing every mapping it is handed."""
    issued = await _issue(session, envelope, cache)
    assert verify(dict(issued.attestation), trust=trust,
                  crawl_id="crawl-001").authorized is True


# ══════════════════════════════════════════════════════════════════════════
#  FOUR MORE, FOUND BY THE GATE AT THE BOTTOM OF THIS FILE ON ITS FIRST RUN
#
#  The gap was originally measured as five codes. The standing gate below is
#  stricter than the grep that produced that number — it requires an assertion
#  on ``AttestReason.<NAME>`` specifically, in a test module, not merely the
#  string appearing somewhere in the harness or in an issuer-side constant with
#  the same name.
#
#  That immediately surfaced four more. Each was "covered" only in a way that
#  did not touch the verifier branch at all:
#
#    not_disposable             asserted as issuer.IssuanceReason.NOT_DISPOSABLE
#                               — the ISSUER's refusal to mint. The VERIFIER's
#                               own refusal of a signed non-disposable proof was
#                               never exercised, and it is the one that matters:
#                               it is what survives a compromised issuer.
#    proof_replayed             asserted via guard.admit() returning False —
#                               the guard's own API, never the verdict.
#    malformed_claims           no test.
#    revocation_issuer_mismatch no test.
#
#  Kept as a separate section because the discovery is the point: a stricter
#  gate found real gaps in work that had already been through nine
#  certification rounds.
# ══════════════════════════════════════════════════════════════════════════


async def test_a_signed_non_disposable_proof_is_refused_by_the_verifier(
        session, envelope, cache, key, trust):
    """PRODUCTION ISOLATION, enforced where it survives a bad issuer.

    The issuer refuses to mint a non-disposable grant, and that is tested
    elsewhere. This is the other half, and the more important one: a proof that
    genuinely IS signed by the platform key, saying ``env_kind: prod``, must
    still be refused. Anything but the word ``disposable`` — prod, staging, uat,
    blank, novel — is refused here regardless of signature.

    Reachable only by re-signing, because the issuer will not produce it. That
    is exactly the scenario: an issuer that has been compromised or has a bug.

    NOTE THE TWO DIFFERENT REFUSALS, and why the distinction is kept rather
    than smoothed over with "assert not authorized". A BLANK ``env_kind`` never
    reaches the disposable check at all — ``ProofClaims`` declares
    ``min_length=1``, so the strict parse (step 5) rejects it as
    ``malformed_claims`` two guards earlier. Asserting ``not_disposable`` for
    the blank case would have passed for the wrong reason and quietly claimed
    coverage of a branch this test never reached.
    """
    for kind in ("prod", "staging", "uat", "dev", "test",
                 "DISPOSABLE_BUT_NOT_QUITE", "disposable-ish", "disposabl"):
        payload = await _proof_with(session, envelope, cache, env_kind=kind)
        verdict = verify(payload, trust=trust, crawl_id="crawl-001")
        assert verdict.authorized is False, f"env_kind={kind!r} was AUTHORISED"
        assert verdict.reason == attest.AttestReason.NOT_DISPOSABLE, (
            f"env_kind={kind!r} was refused as {verdict.reason}, not by the "
            f"disposable gate")

    # The blank case: still refused, by the strict schema rather than the
    # disposable gate. Both are denials; only one of them is this guard.
    blank = await _proof_with(session, envelope, cache, env_kind="")
    blank_verdict = verify(blank, trust=trust, crawl_id="crawl-001")
    assert blank_verdict.authorized is False
    assert blank_verdict.reason == attest.AttestReason.MALFORMED_CLAIMS


@pytest.mark.parametrize("variant", [
    " disposable", "disposable ", "disposable\n", "DISPOSABLE", "Disposable",
])
async def test_whitespace_and_case_variants_of_disposable_are_NORMALISED(
        session, envelope, cache, key, trust, variant):
    """DOCUMENTING A DELIBERATE LENIENCY, because an undocumented one is a trap.

    The verifier compares ``(claims.env_kind or "").strip().lower()``, so
    ``" disposable"`` and ``"DISPOSABLE"`` are ACCEPTED. That surprised this
    test on first write, so it is pinned here as intended behaviour rather than
    left for the next person to rediscover as a suspected hole.

    Why it is not one: ``env_kind`` in a proof originates from
    ``env_provisioning_records.env_kind``, which the database constrains with a
    CHECK to exactly ``disposable|staging|uat|test|dev|prod``. No legitimate
    path can produce a variant, and a tenant cannot write that column at all.
    The normalisation is therefore defence against an operator's stray
    whitespace, not an authorisation surface.

    Recorded, NOT changed: ``attest.py`` is inside the certification's pinned
    set, so tightening this to an exact match would de-certify A11 and re-block
    A12 — for a case no live path can reach. If it is ever tightened, it belongs
    in the same re-certified change as any other pinned-file edit.
    """
    payload = await _proof_with(session, envelope, cache, env_kind=variant)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is True, (
        f"env_kind={variant!r} was refused as {verdict.reason} — the "
        f"normalisation this test documents has changed, which is a "
        f"pinned-file change and needs re-certification")
    # The GRANT still reports the canonical value, never the variant.
    assert verdict.env_kind == attest.DISPOSABLE


async def test_the_disposable_control_authorises(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: the same re-signing path with the correct
    ``env_kind`` authorises, so the refusals above are about the value and not
    about re-signed proofs being rejected wholesale."""
    payload = await _proof_with(session, envelope, cache,
                                env_kind=attest.DISPOSABLE)
    assert verify(payload, trust=trust, crawl_id="crawl-001").authorized is True


async def test_a_proof_id_admitted_for_one_crawl_is_refused_for_another(
        session, envelope, cache, key, trust):
    """``proof_replayed``, asserted on the VERDICT rather than on the guard.

    The existing replay test calls ``ProofReplayGuard.admit`` directly and
    checks it returns False. That tests the guard; it does not test that the
    verifier consults it and converts the refusal into a denial. Those are
    different failures, and only the second one is what production depends on.

    Setup: one proof_id, admitted for crawl-A, then re-signed onto crawl-B so
    the crawl BINDING passes and the replay guard is the only thing left to
    catch it.
    """
    guard = attest.ProofReplayGuard()
    first = await _issue(session, envelope, cache, crawl_id="crawl-A")
    assert verify(first.attestation, trust=trust, crawl_id="crawl-A",
                  replay_guard=guard).authorized is True

    # Same proof_id, different crawl, genuinely re-signed.
    replayed = await _proof_with(session, envelope, cache,
                                 proof_id=first.proof_id, crawl_id="crawl-B")
    verdict = verify(replayed, trust=trust, crawl_id="crawl-B",
                     replay_guard=guard)
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.PROOF_REPLAYED


async def test_a_fresh_proof_id_on_a_second_crawl_is_admitted(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: the same shared guard admits a DIFFERENT proof_id
    on crawl-B, so the refusal above is about the reused id and not about the
    guard rejecting every second call."""
    guard = attest.ProofReplayGuard()
    first = await _issue(session, envelope, cache, crawl_id="crawl-A")
    assert verify(first.attestation, trust=trust, crawl_id="crawl-A",
                  replay_guard=guard).authorized is True
    second = await _issue(session, envelope, cache, crawl_id="crawl-B")
    assert second.proof_id != first.proof_id
    assert verify(second.attestation, trust=trust, crawl_id="crawl-B",
                  replay_guard=guard).authorized is True


@pytest.mark.parametrize("override,why", [
    ({"proof_id": "tooshort"}, "proof_id below the 16-char minimum"),
    ({"tenant_id": ""}, "empty tenant_id"),
    ({"environment_id": ""}, "empty environment_id"),
    ({"max_walk_mutations_per_step": 999}, "budget above the hard ceiling"),
    ({"target_origin": ""}, "empty target_origin"),
])
async def test_claims_that_fail_the_strict_schema_are_refused(
        session, envelope, cache, key, trust, override, why):
    """``malformed_claims`` — the STRICT parse, after the signature check.

    The signature is genuine in every case here, so this is not tamper
    detection. It is the verifier refusing to act on a correctly-signed
    statement whose shape it does not accept — a bound the issuer cannot widen
    by signing something unusual.
    """
    payload = await _proof_with(session, envelope, cache, **override)
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False, f"{why} was AUTHORISED"
    assert verdict.reason == attest.AttestReason.MALFORMED_CLAIMS


async def test_a_revocation_list_from_another_issuer_is_refused(
        session, envelope, cache, key, trust):
    """``revocation_issuer_mismatch`` — the list must come from OUR issuer.

    Signed by the correct key, so the signature verifies; the ``issuer`` field
    inside names somebody else. Without this check, a revocation list minted by
    a different platform — one that has revoked nothing of ours — would satisfy
    the mandatory-list requirement while suppressing every revocation we have
    actually made.
    """
    issued = await _issue(session, envelope, cache)
    rev_claims = dict(issued.attestation["revocations"]["claims"])
    rev_claims["issuer"] = "some-other-platform"
    payload = {"proof": issued.attestation["proof"],
               "revocations": await _resign(session, envelope, rev_claims)}
    verdict = verify(payload, trust=trust, crawl_id="crawl-001")
    assert verdict.authorized is False
    assert verdict.reason == attest.AttestReason.REVOCATION_ISSUER_MISMATCH


async def test_the_revocation_issuer_control_authorises(
        session, envelope, cache, key, trust):
    """FALSIFICATION CONTROL: re-signing the revocation claims UNCHANGED through
    the same path authorises, so the refusal above is about the issuer name and
    not about the re-signed list being rejected."""
    issued = await _issue(session, envelope, cache)
    rev_claims = dict(issued.attestation["revocations"]["claims"])
    payload = {"proof": issued.attestation["proof"],
               "revocations": await _resign(session, envelope, rev_claims)}
    assert verify(payload, trust=trust, crawl_id="crawl-001").authorized is True


# ── The gap itself, pinned so it cannot silently reopen ────────────────────

def test_every_verifier_refusal_code_is_exercised_somewhere_in_this_suite():
    """A STANDING, SELF-DISCOVERING GATE ON THE GAP THAT PRODUCED THIS FILE.

    Five refusal codes had no test anywhere, and the omission was invisible
    because every guard worked. A new ``AttestReason`` added tomorrow would land
    in exactly the same blind spot.

    So the verifier's own vocabulary is checked against the whole security
    suite: add a refusal code, and this fails until something asserts it.
    Derived from the source rather than a hand-maintained list, for the same
    reason the RLS coverage gate is — a list someone must remember to update is
    a list that goes stale, and it fails OPEN.
    """
    suite = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted(pathlib.Path(__file__).parent.glob("test_a11_*.py"))
    )
    codes = {name: value for name, value in vars(attest.AttestReason).items()
             if name.isupper() and isinstance(value, str) and name != "OK"}
    assert codes, "no refusal codes discovered — the gate would pass vacuously"

    unexercised = sorted(value for name, value in codes.items()
                         if f"AttestReason.{name}" not in suite)
    assert unexercised == [], (
        f"these verifier refusal codes are asserted by no test: {unexercised}. "
        f"Every DENY branch needs one — they decide what happens when something "
        f"is absent or impossible, and they rot unnoticed precisely because no "
        f"feature test ever reaches them.")


def test_the_coverage_gate_has_teeth():
    """THE CANARY. A self-discovering gate that discovered nothing would report
    green forever — which is precisely how the original gap survived nine
    certification rounds.

    Two ways this gate could be vacuous, both checked:

      1. the discovery query finds no codes (a renamed class, a changed shape),
         so the "unexercised" set is empty because the *candidate* set is;
      2. the membership test always passes, so a genuinely absent code would be
         reported as covered.

    The second check builds its sentinel name at RUNTIME. Writing the literal
    would put the string into the very file the gate scans, and the canary would
    pass by finding itself — which is the same self-satisfying-assertion trap
    the gate exists to catch, and which this test did on its first run.
    """
    codes = {name for name, value in vars(attest.AttestReason).items()
             if name.isupper() and isinstance(value, str) and name != "OK"}
    assert len(codes) >= 20, (
        f"only {len(codes)} refusal codes discovered; the verifier defines far "
        f"more, so the discovery query is broken and the gate above is vacuous")

    suite = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted(pathlib.Path(__file__).parent.glob("test_a11_*.py")))
    sentinel = "AttestReason." + "".join(("SENTINEL", "_", "ABSENT", "_CODE"))
    assert sentinel not in suite, (
        "the sentinel leaked into the suite text; the membership test can no "
        "longer distinguish present from absent")
    # Re-run the gate's own logic with the sentinel injected as a candidate: it
    # MUST come back unexercised, or the gate cannot fail for a real omission.
    injected = {**{n: v for n, v in vars(attest.AttestReason).items()
                   if n.isupper() and isinstance(v, str) and n != "OK"},
                sentinel.split(".", 1)[1]: "sentinel_absent_code"}
    unexercised = [v for n, v in injected.items()
                   if f"AttestReason.{n}" not in suite]
    assert "sentinel_absent_code" in unexercised, (
        "the coverage gate did not flag a code that appears in no test — it "
        "would report green on a real omission")
