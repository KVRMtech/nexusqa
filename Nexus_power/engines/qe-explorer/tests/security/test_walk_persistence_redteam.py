"""M1.3 RED TEAM — every threat in the walk-persistence threat model, driven as
an ATTACK rather than as a unit test.

One test per attack path.  Each names the adversary's capability in its
docstring, performs the attack against the production objects, and asserts the
mitigation held.  A test here failing means an attacker succeeded — not that a
refactor moved a symbol.

Threats covered:
  R1  forged attestation                R6  mutation flooding
  R2  replay                            R7  budget exhaustion / starvation
  R3  stolen provisioning token         R8  audit tampering
  R4  privilege escalation              R9  production escape
  R5  tenant impersonation              R10 race conditions
"""
from __future__ import annotations

import base64
import json
import threading

import pytest

from app.attest import (AttestReason, ProofReplayGuard, TrustStore,
                        canonical_bytes, verify_provisioning_proof)
from app.config import Settings
from app.guard import GuardRule, Phase, load_refuse_pack
from app.guard_context import GuardContext
from app.walk_persist import (MutationAuditLog, WalkAuthorization, WalkReason,
                              verify_audit_chain)
from tests._attest_kit import (CRAWL_ID, TARGET_URL, TENANT_ID, Issuer, now_ms,
                               tampered)

ORIGIN = "https://app.char"
DRAFT = f"{ORIGIN}/api/application/draft"


@pytest.fixture(scope="module")
def pack():
    return load_refuse_pack(Settings().refuse_pack_path)


@pytest.fixture()
def issuer():
    return Issuer()


def _verify(payload, issuer, **over):
    kwargs = dict(trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=TENANT_ID,
                  target_url=TARGET_URL, now_epoch_ms=now_ms(),
                  replay_guard=ProofReplayGuard())
    kwargs.update(over)
    return verify_provisioning_proof(payload, **kwargs)


def _armed(issuer, pack, **proof_over):
    """A GuardContext with a live grant and an open actuation window — the most
    permissive state this system can ever be in.  Every attack below starts
    from here, so nothing passes because the crawl happened to be idle."""
    verdict = _verify({"proof": issuer.proof(**proof_over),
                       "revocations": issuer.revocations()}, issuer)
    assert verdict.authorized, verdict.reason
    auth = WalkAuthorization.from_verdict(verdict, workflow_id=CRAWL_ID,
                                          audit=MutationAuditLog())
    auth.begin_step(journey_id="j", step_index=0, step_fingerprint="fp",
                    now_ms=1000)
    auth.authorize_step(True)
    auth.open_window("Save Draft", 1000)
    return GuardContext(refuse_pack=pack, phase=Phase.WALK,
                        walk_authorization=auth), auth


# ═══ R1 · FORGED ATTESTATION ════════════════════════════════════════════════

def test_r1_attacker_who_controls_the_dispatch_body_cannot_mint_a_grant(issuer):
    """CAPABILITY: full control of the ``attestation`` field — the tenant, a
    compromised qe-central, or anything on the wire without the key.

    The attacker writes the most authoritative-looking object they can, and the
    only thing they cannot produce is a signature."""
    forgeries = [
        {"env_kind": "disposable"},
        {"env_kind": "disposable", "attested_by": "the platform",
         "expires_at_ms": now_ms() + 10**9},
        {"proof": {"claims": issuer.proof_claims(), "alg": "ed25519",
                   "kid": issuer.kid, "signature": base64.b64encode(b"\x00" * 64).decode()},
         "revocations": issuer.revocations()},
        {"proof": {"claims": issuer.proof_claims(), "alg": "none",
                   "kid": issuer.kid, "signature": ""},
         "revocations": issuer.revocations()},
        {"proof": {"claims": issuer.proof_claims(), "alg": "ed25519",
                   "kid": issuer.kid, "signature": "x" * 88},
         "revocations": issuer.revocations()},
    ]
    for forged in forgeries:
        verdict = _verify(forged, issuer)
        assert verdict.authorized is False, f"forgery accepted: {forged!r}"
        assert WalkAuthorization.from_verdict(verdict, workflow_id=CRAWL_ID) is None


def test_r1_a_single_flipped_claim_invalidates_a_genuine_proof(issuer):
    """CAPABILITY: intercept a REAL proof and edit one field.

    The highest-value edits are asserted individually, because "the signature
    covers the claims" is only worth as much as the claim you would most want to
    change."""
    genuine = issuer.envelope()
    assert _verify(genuine, issuer).authorized is True
    for field, value in (("env_kind", "prod"),
                         ("environment_id", "prod-eu-1"),
                         ("target_origin", "https://prod.example.com"),
                         ("crawl_id", "attacker-crawl"),
                         ("tenant_id", "tenant-victim"),
                         ("expires_at_ms", now_ms() + 10**11),
                         ("max_walk_mutations_per_step", 10)):
        verdict = _verify(tampered(genuine, **{field: value}), issuer)
        assert verdict.reason == AttestReason.BAD_SIGNATURE, field


def test_r1_a_self_signed_issuer_is_not_a_trusted_issuer(issuer):
    """CAPABILITY: generate a keypair and sign a perfect proof.

    Anyone can produce a valid Ed25519 signature; only the fleet's configured
    key is trusted, and key selection by ``kid`` is not trust."""
    attacker = Issuer(name=issuer.name)
    payload = {"proof": attacker.proof(), "revocations": attacker.revocations()}
    assert _verify(payload, issuer).reason == AttestReason.UNKNOWN_KEY_ID


def test_r1_the_explorer_holds_no_signing_key(issuer):
    """STRUCTURAL MITIGATION.  A compromised explorer cannot mint a proof
    because the private half does not exist in the service: the trust store is
    public keys only, and no module under ``app/`` imports a signing primitive."""
    store = issuer.trust()
    for pk in store.keys.values():
        assert len(base64.b64decode(pk)) == 32          # a PUBLIC key
    import pathlib
    app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = [p.name for p in app_dir.glob("*.py")
                 if "Ed25519PrivateKey" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"a signing primitive reached the service: {offenders}"


# ═══ R2 · REPLAY ════════════════════════════════════════════════════════════

def test_r2_a_captured_proof_cannot_be_replayed_against_another_crawl(issuer):
    """CAPABILITY: capture a complete, valid dispatch off the wire.

    The claims bind the proof to ONE crawl id, so the capture is worthless for
    any other crawl; and the process replay guard makes the first admission
    authoritative even if the binding somehow matched."""
    replay_guard = ProofReplayGuard()
    captured = issuer.envelope()
    assert _verify(captured, issuer, replay_guard=replay_guard).authorized is True
    assert _verify(captured, issuer, replay_guard=replay_guard,
                   crawl_id="victim-crawl").reason == AttestReason.CRAWL_BINDING_MISMATCH
    rebound = {"proof": issuer.proof(crawl_id="victim-crawl"),
               "revocations": issuer.revocations()}
    assert _verify(rebound, issuer, replay_guard=replay_guard,
                   crawl_id="victim-crawl").reason == AttestReason.PROOF_REPLAYED


def test_r2_an_old_revocation_list_cannot_un_revoke_a_proof(issuer):
    """CAPABILITY: keep a copy of yesterday's (empty) revocation list and
    present it alongside a proof that has since been revoked."""
    stale = now_ms() - 1_200_000
    payload = {"proof": issuer.proof(),
               "revocations": issuer.revocations(issued_at_ms=stale,
                                                 expires_at_ms=stale + 600_000)}
    assert _verify(payload, issuer).reason == AttestReason.REVOCATION_EXPIRED


def test_r2_dropping_the_revocation_list_does_not_help(issuer):
    """CAPABILITY: strip the revocation list so nothing can be checked against
    it.  Missing revocation state is UNKNOWN state, and unknown is not
    permission."""
    assert _verify({"proof": issuer.proof()}, issuer).reason == \
        AttestReason.NO_REVOCATION_LIST


# ═══ R3 · STOLEN PROVISIONING TOKEN ═════════════════════════════════════════

def test_r3_a_stolen_proof_is_useless_off_its_own_environment(issuer):
    """CAPABILITY: steal a genuine, unexpired proof for a real throwaway env.

    It still authorises nothing except: that crawl, that tenant, that origin —
    and only until it expires."""
    stolen = issuer.envelope()
    assert _verify(stolen, issuer, target_url="https://prod.example.com/x").reason \
        == AttestReason.ORIGIN_MISMATCH
    assert _verify(stolen, issuer, tenant_id="tenant-attacker").reason \
        == AttestReason.TENANT_MISMATCH
    assert _verify(stolen, issuer, crawl_id="attacker-crawl").reason \
        == AttestReason.CRAWL_BINDING_MISMATCH


def test_r3_the_verifier_bounds_the_theft_window_itself(issuer):
    """CAPABILITY: compromise the ISSUER and mint a long-lived proof.

    The verifier's own lifetime ceiling refuses it, so a stolen or maliciously
    minted proof cannot outlive this fleet's policy."""
    issued = now_ms()
    payload = {"proof": issuer.proof(issued_at_ms=issued,
                                     expires_at_ms=issued + 30 * 86_400_000),
               "revocations": issuer.revocations()}
    assert _verify(payload, issuer).reason == AttestReason.LIFETIME_TOO_LONG
    tight = _verify(payload, issuer,
                    trust=issuer.trust(max_lifetime_ms=30 * 86_400_000 + 1))
    assert tight.authorized is True, "the ceiling must be policy, not a hard-code"


# ═══ R4 · PRIVILEGE ESCALATION ══════════════════════════════════════════════

def test_r4_a_walk_grant_does_not_become_a_submit_grant(issuer, pack):
    """CAPABILITY: hold a perfectly valid walk proof and try to use it to
    SUBMIT.  The two tiers are independent; neither implies the other."""
    ctx, _auth = _armed(issuer, pack)
    ctx.phase = Phase.SUBMIT
    decision = ctx.decide("POST", f"{ORIGIN}/api/submit", now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.SUBMIT_NO_ATTESTATION


def test_r4_a_walk_grant_does_not_widen_explore(issuer, pack):
    ctx, _auth = _armed(issuer, pack)
    ctx.phase = Phase.EXPLORE
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        decision = ctx.decide(method, DRAFT, now_ms=1000)
        assert decision.allow is False
        assert decision.rule_id == GuardRule.EXPLORE_MUTATION_BLOCKED


def test_r4_irreversible_verbs_never_cross_in_walk(issuer, pack):
    """CAPABILITY: reach a genuinely attested disposable env and aim the walk at
    a destructive endpoint.  SUBMIT would allow this under a human approval;
    WALK, which has no human, never does."""
    ctx, auth = _armed(issuer, pack)
    for name, url in (("Cancel Application", f"{ORIGIN}/api/application/delete"),
                      ("Delete Account", f"{ORIGIN}/api/account/delete"),
                      ("", f"{ORIGIN}/api/application/destroy")):
        decision = ctx.decide("DELETE", url, now_ms=1000, action_button_name=name)
        assert decision.allow is False, f"{name!r} -> {url}"
    assert auth.budget.consumed == 0
    assert auth.audit.records == []


def test_r4_a_proof_cannot_widen_the_fleet_budget_ceiling(issuer):
    """CAPABILITY: mint (or compel the issuer to mint) a proof asking for the
    maximum budget.  The effective grant is the fleet's ceiling."""
    payload = {"proof": issuer.proof(max_walk_mutations_per_step=10),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer, trust=issuer.trust(max_mutations_per_step=1))
    assert verdict.max_mutations_per_step == 1


def test_r4_a_forged_authorization_object_is_not_a_grant(issuer, pack):
    """CAPABILITY: code execution inside the crawl process, constructing a
    look-alike authorisation.  ``walk_attested`` reads the VERDICT, so an object
    without an authorising verdict grants nothing."""
    class FakeAuth:
        verdict = None
        authorized = True
        step_authorized = True
        window_open = True

        def authorize_mutation(self, *a, **k):
            return True, "ok", "forged"

    ctx = GuardContext(refuse_pack=pack, phase=Phase.WALK,
                       walk_authorization=FakeAuth())
    assert ctx.walk_attested is False
    assert ctx.decide("POST", DRAFT, now_ms=1000).allow is False


# ═══ R5 · TENANT IMPERSONATION ══════════════════════════════════════════════

def test_r5_tenant_a_cannot_authorize_a_crawl_running_as_tenant_b(issuer):
    assert _verify(issuer.envelope(), issuer, tenant_id="tenant-victim").reason \
        == AttestReason.TENANT_MISMATCH


def test_r5_a_proof_cannot_be_re_pointed_at_another_customers_app(issuer):
    """The origin binding is what stops one customer's throwaway-env proof from
    authorising writes at another customer's application."""
    for victim in ("https://other-customer.example.com/apply",
                   "https://app.char.other-customer.net/apply"):
        assert _verify(issuer.envelope(), issuer, target_url=victim).reason \
            == AttestReason.ORIGIN_MISMATCH


# ═══ R6/R7 · MUTATION FLOODING AND BUDGET EXHAUSTION ════════════════════════

def test_r6_a_flood_cannot_exceed_the_per_step_budget(issuer, pack):
    """CAPABILITY: a hostile or buggy application firing a thousand writes while
    the actuation window is open."""
    ctx, auth = _armed(issuer, pack)
    allowed = sum(1 for _ in range(1000)
                  if ctx.decide("POST", DRAFT, now_ms=1000).allow)
    assert allowed == 3
    assert auth.budget.consumed == 3
    assert len(auth.audit.records) == 3


def test_r6_a_flood_cannot_win_by_waiting_for_the_next_step(issuer, pack):
    """Budgets reset PER STEP, so a flood spread across a long journey is
    bounded by (steps x budget) and each step is individually capped."""
    ctx, auth = _armed(issuer, pack)
    total = 0
    for step in range(10):
        auth.begin_step(journey_id="j", step_index=step,
                        step_fingerprint=f"fp{step}", now_ms=1000 + step)
        auth.authorize_step(True)
        auth.open_window("Save Draft", 1000 + step)
        total += sum(1 for _ in range(50)
                     if ctx.decide("POST", DRAFT, now_ms=1000 + step).allow)
    assert total == 30, "each of the ten steps must be capped at three"


def test_r7_a_blocked_request_cannot_starve_a_legitimate_one(issuer, pack):
    """CAPABILITY: fire requests the guard will refuse, hoping each costs a
    budget slot, so the real Save Draft finds nothing left."""
    ctx, auth = _armed(issuer, pack)
    for _ in range(50):
        ctx.decide("DELETE", f"{ORIGIN}/api/application/delete", now_ms=1000,
                   action_button_name="Cancel Application")
        ctx.decide("POST", "https://evil.example.com/x", now_ms=1000)
    assert auth.budget.remaining == 3
    assert ctx.decide("POST", DRAFT, now_ms=1000).allow is True


def test_r7_the_window_closes_on_time_even_with_budget_left(issuer, pack):
    ctx, auth = _armed(issuer, pack)
    auth.budget.window_ms = 500
    assert ctx.decide("POST", DRAFT, now_ms=1400).allow is True
    assert ctx.decide("POST", DRAFT, now_ms=1600).allow is False
    assert auth.budget.remaining == 2, "the time bound must not consume budget"


# ═══ R8 · AUDIT TAMPERING ═══════════════════════════════════════════════════

def test_r8_editing_deleting_or_reordering_the_ledger_is_detected(issuer, pack):
    ctx, auth = _armed(issuer, pack)
    for _ in range(3):
        ctx.decide("POST", DRAFT, now_ms=1000)
    records = auth.audit.records
    assert verify_audit_chain(records) == (True, "")

    edited = [dict(r) for r in records]
    edited[0]["endpoint"] = f"{ORIGIN}/api/harmless"
    assert verify_audit_chain(edited)[0] is False
    edited = [dict(r) for r in records]
    edited[2]["approval"] = dict(edited[2]["approval"], env_kind="prod")
    assert verify_audit_chain(edited)[0] is False
    assert verify_audit_chain(records[1:])[0] is False
    assert verify_audit_chain([records[1], records[0], records[2]])[0] is False


def test_r8_a_mutation_cannot_be_performed_off_the_record(issuer, pack):
    """CAPABILITY: break the evidence path (fill the disk, kill the sink) and
    keep mutating.  Evidence-or-nothing: the mutation is refused."""
    ctx, auth = _armed(issuer, pack)
    auth.audit.attach_sink(lambda _r: (_ for _ in ()).throw(IOError("disk full")))
    decision = ctx.decide("POST", DRAFT, now_ms=1000)
    assert decision.allow is False
    assert WalkReason.AUDIT_FAILED in decision.reason


def test_r8_the_ledger_records_no_user_values(issuer, pack):
    """A wizard persists an applicant's answers, and the ledger is immutable —
    so a query string must never reach it."""
    ctx, auth = _armed(issuer, pack)
    ctx.decide("POST", f"{DRAFT}?ssn=123-45-6789&dob=1980-01-01", now_ms=1000)
    blob = json.dumps(auth.audit.records)
    assert "123-45-6789" not in blob and "1980-01-01" not in blob
    assert "ssn" not in blob


# ═══ R9 · PRODUCTION ESCAPE ═════════════════════════════════════════════════

@pytest.mark.parametrize("env_kind", ["prod", "production", "staging", "uat",
                                      "production_test", "PROD", "unknown", "  "])
def test_r9_no_environment_but_disposable_can_ever_authorize(env_kind, issuer):
    """T-WP-06.  Correctly signed by the trusted issuer, unexpired, correctly
    bound — and still refused, because the environment is not throwaway."""
    payload = {"proof": issuer.proof(env_kind=env_kind),
               "revocations": issuer.revocations()}
    verdict = _verify(payload, issuer)
    assert verdict.authorized is False
    assert WalkAuthorization.from_verdict(verdict, workflow_id=CRAWL_ID) is None


def test_r9_an_unconfigured_fleet_authorizes_nothing(issuer):
    """THE SHIPPED DEFAULT.  A deployment that never sets the attestation keys
    performs zero walk mutations, whatever it is sent."""
    assert Settings().attestation_trust_store().configured is False
    verdict = _verify(issuer.envelope(), issuer, trust=TrustStore())
    assert verdict.reason == AttestReason.NO_TRUST_ANCHOR


def test_r9_there_is_no_configuration_switch_that_enables_walk_mutation(pack):
    """NO HIDDEN SWITCHES.  Every Settings field is set to its most permissive
    plausible value and walk mutation is still off, because the capability is
    switched on by cryptography and by nothing else."""
    permissive = Settings(
        QEC_WALK_MAX_MUTATIONS_PER_STEP=10,
        QEC_WALK_MUTATION_WINDOW_MS=10 ** 9,
        QEC_ATTESTATION_MAX_LIFETIME_MS=10 ** 12,
        QEC_ATTESTATION_SKEW_MS=10 ** 9,
    )
    assert permissive.attestation_trust_store().configured is False
    ctx = GuardContext(refuse_pack=pack, phase=Phase.WALK)
    assert ctx.walk_attested is False
    assert ctx.decide("POST", DRAFT, now_ms=1000).allow is False


def test_r9_a_mutation_aimed_off_the_attested_origin_is_refused(issuer, pack):
    """The last line before production: even inside a live, budgeted window, a
    write aimed anywhere but the attested origin is refused."""
    ctx, auth = _armed(issuer, pack)
    for url in ("https://prod.example.com/api/orders",
                "https://app.char.evil.net/api/draft",
                "http://app.char/api/draft",
                "https://app.char:8443/api/draft"):
        assert ctx.decide("POST", url, now_ms=1000).allow is False, url
    assert auth.budget.remaining == 3


# ═══ R10 · RACE CONDITIONS ══════════════════════════════════════════════════

def test_r10_concurrent_requests_cannot_both_take_the_last_slot(issuer, pack):
    ctx, auth = _armed(issuer, pack)
    ctx.decide("POST", DRAFT, now_ms=1000)
    ctx.decide("POST", DRAFT, now_ms=1000)
    assert auth.budget.remaining == 1

    wins: list = []
    barrier = threading.Barrier(24)

    def go() -> None:
        barrier.wait()
        if ctx.decide("POST", DRAFT, now_ms=1000).allow:
            wins.append(1)

    threads = [threading.Thread(target=go) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert verify_audit_chain(auth.audit.records) == (True, "")


def test_r10_the_ledger_stays_consistent_under_concurrent_writes(issuer, pack):
    """A hash chain written from many threads must still re-derive, and the
    sequence numbers must be dense — a lock that only mostly works produces a
    chain that mostly verifies."""
    ctx, auth = _armed(issuer, pack)
    auth.budget.max_mutations = 200
    barrier = threading.Barrier(20)

    def go() -> None:
        barrier.wait()
        for _ in range(10):
            ctx.decide("POST", DRAFT, now_ms=1000)

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = auth.audit.records
    assert len(records) == 200
    assert [r["sequence"] for r in records] == list(range(1, 201))
    assert verify_audit_chain(records) == (True, "")


def test_r10_the_window_cannot_be_left_open_by_a_raising_click(issuer, pack):
    """An exception mid-actuation must not leave the crawl standing in a phase
    that permits writes."""
    import asyncio

    from app.walker import WalkerMixin

    class _Clock:
        @staticmethod
        def now_ms() -> int:
            return 1000

    class Host(WalkerMixin):
        """The smallest object the window helper needs — the REAL mixin method
        is exercised, not a re-implementation of it."""

        _observe_only = False
        _clock = _Clock()

        def __init__(self, guard):
            self._guard = guard

    ctx, auth = _armed(issuer, pack)
    ctx.phase = Phase.EXPLORE
    host = Host(ctx)

    async def boom():
        async with host._walk_persistence_window("Save Draft") as opened:
            assert opened is True, "the window must actually have opened"
            assert host._guard.phase is Phase.WALK
            raise RuntimeError("click exploded")

    with pytest.raises(RuntimeError):
        asyncio.run(boom())
    assert ctx.phase is Phase.EXPLORE
    assert auth.window_open is False
    assert ctx.decide("POST", DRAFT, now_ms=1000).allow is False
