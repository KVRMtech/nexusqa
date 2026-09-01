"""M1.3 / T-WP-01 + T-WP-03 + T-WP-04 — THE EXTENDED GUARD, THE PER-STEP BUDGET
AND THE IMMUTABLE LEDGER.

Structured as the SAFETY ARGUMENT for the guard change, in three parts:

  1. WHAT DID NOT CHANGE — the whole pre-M1.3 decision matrix, re-asserted with
     a walk authorisation present.  A feature that quietly widened EXPLORE or
     AUTH while nobody was looking would pass every new test and fail here.
  2. THE NEW PHASE — every rung of the WALK ladder, positive and negative.
  3. THE BUDGET AND THE LEDGER — bounded, deterministic, thread-safe, and
     evidence-or-nothing.

Pure: no browser, no network.
"""
from __future__ import annotations

import threading

import pytest

from app.attest import AttestReason
from app.config import Settings
from app.guard import (MUTATING_METHODS, READ_METHODS, Attestation, GuardRule,
                       Phase, classify_request, load_refuse_pack)
from app.guard_context import GuardContext
from app.walk_persist import (AUDIT_GENESIS, MutationAuditLog, StepMutationBudget,
                              WalkAuthorization, WalkReason, scrub_endpoint,
                              verify_audit_chain)
from tests._attest_kit import CRAWL_ID, TARGET_URL, TENANT_ID, Issuer, now_ms

ORIGIN = "https://app.char"
SAVE_DRAFT_URL = f"{ORIGIN}/api/application/draft"


@pytest.fixture(scope="module")
def pack():
    return load_refuse_pack(Settings().refuse_pack_path)


@pytest.fixture()
def issuer() -> Issuer:
    return Issuer()


def _verdict(issuer: Issuer, **over):
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    payload = {"proof": issuer.proof(**over), "revocations": issuer.revocations()}
    return verify_provisioning_proof(
        payload, trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=TENANT_ID,
        target_url=TARGET_URL, now_epoch_ms=now_ms(),
        replay_guard=ProofReplayGuard())


def _authorization(issuer: Issuer, *, sink=None, window_ms=15_000, **over):
    verdict = _verdict(issuer, **over)
    assert verdict.authorized, verdict.reason
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=CRAWL_ID, audit=MutationAuditLog(sink),
        window_ms=window_ms)


def _ctx(pack, auth=None, phase=Phase.WALK) -> GuardContext:
    return GuardContext(refuse_pack=pack, phase=phase, walk_authorization=auth)


def _open_step(auth, *, now_ms_=1000, control="Save Draft"):
    auth.begin_step(journey_id="flow-1", step_index=0,
                    step_fingerprint="fp-step-0", now_ms=now_ms_)
    auth.authorize_step(True)
    auth.open_window(control, now_ms_)


# ═══ PART 1 · WHAT DID NOT CHANGE ═══════════════════════════════════════════

@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
def test_explore_still_blocks_every_mutation_with_a_walk_grant_present(
        method, pack, issuer):
    """The single most important regression in this milestone.  A crawl holding
    a fully valid platform proof must STILL be refused in EXPLORE — the grant is
    scoped to the WALK phase and to an open actuation window, not to the crawl."""
    ctx = _ctx(pack, _authorization(issuer), phase=Phase.EXPLORE)
    _open_step(ctx.walk_authorization)
    decision = ctx.decide(method, SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.EXPLORE_MUTATION_BLOCKED


@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
def test_auth_phase_is_unchanged_by_a_walk_grant(method, pack, issuer):
    ctx = _ctx(pack, _authorization(issuer), phase=Phase.AUTH)
    _open_step(ctx.walk_authorization)
    decision = ctx.decide(method, f"{ORIGIN}/api/orders", now_ms=1000)
    assert decision.allow is False


def test_submit_phase_still_needs_its_own_attestation_and_approval(pack, issuer):
    """A walk grant is not a submit grant.  Nothing about M1.3 lets a crawl
    submit without the operator attestation + per-flow approval it always
    needed."""
    ctx = _ctx(pack, _authorization(issuer), phase=Phase.SUBMIT)
    _open_step(ctx.walk_authorization)
    decision = ctx.decide("POST", f"{ORIGIN}/api/submit", now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.SUBMIT_NO_ATTESTATION


def test_classify_request_default_refuses_walk_mutation(pack):
    """The pure function's DEFAULT is refusal: a caller that forgets the new
    keyword cannot accidentally authorise a write."""
    decision = classify_request("POST", SAVE_DRAFT_URL, Phase.WALK, pack, False, "")
    assert decision.allow is False
    assert decision.rule_id == GuardRule.WALK_NO_ATTESTATION


def test_unknown_phase_still_fails_closed(pack):
    assert classify_request("POST", SAVE_DRAFT_URL, "wallk", pack, False, "").allow is False


# ═══ PART 2 · THE WALK LADDER ═══════════════════════════════════════════════

@pytest.mark.parametrize("method", sorted(READ_METHODS))
def test_walk_reads_are_allowed(method, pack, issuer):
    ctx = _ctx(pack, _authorization(issuer))
    decision = ctx.decide(method, f"{ORIGIN}/apply/step-2", now_ms=1000)
    assert decision.allow is True
    assert decision.rule_id == GuardRule.WALK_READ_OK


def test_walk_mutation_signal_get_still_aborts(pack, issuer):
    """A logout / action=delete GET aborts in WALK exactly as in every other
    phase — the read allowance is not a hole."""
    ctx = _ctx(pack, _authorization(issuer))
    decision = ctx.decide("GET", f"{ORIGIN}/account/logout", now_ms=1000)
    assert decision.allow is False


@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
def test_walk_mutation_allowed_under_a_verified_proof(method, pack, issuer):
    """T-WP-04 case 1."""
    ctx = _ctx(pack, _authorization(issuer))
    _open_step(ctx.walk_authorization)
    decision = ctx.decide(method, SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is True, decision.reason
    assert decision.rule_id == GuardRule.WALK_MUTATION_OK


def test_walk_mutation_blocked_without_a_proof(pack):
    """T-WP-04 case 2 — a NON-ATTESTED environment."""
    ctx = _ctx(pack, None)
    decision = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.WALK_NO_ATTESTATION


def test_walk_attested_is_derived_from_the_verdict_not_a_flag(pack, issuer):
    """There is no boolean to set.  Assigning a look-alike object with
    ``authorized=False`` does not turn the capability on."""
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    assert ctx.walk_attested is True
    object.__setattr__(auth.verdict, "authorized", False)
    assert ctx.walk_attested is False
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000).allow is False


def test_walk_never_crosses_an_irreversible_verb(pack, issuer):
    """LEAST PRIVILEGE, and the sharpest difference from SUBMIT.  SUBMIT lets an
    irreversible verb through on a disposable env under a human approval; WALK
    has no human, so it never does."""
    ctx = _ctx(pack, _authorization(issuer))
    _open_step(ctx.walk_authorization, control="Cancel Application")
    decision = ctx.decide("POST", f"{ORIGIN}/api/application/delete",
                          now_ms=1000, action_button_name="Cancel Application")
    assert decision.allow is False
    assert decision.rule_id == GuardRule.WALK_IRREVERSIBLE_BLOCKED


def test_a_refused_irreversible_verb_does_not_consume_budget(pack, issuer):
    """Ordering proof: the pure rules decide before the budget is charged, so a
    refused danger control cannot starve a legitimate Save Draft."""
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    _open_step(auth, control="Cancel Application")
    ctx.decide("POST", f"{ORIGIN}/api/application/delete", now_ms=1000,
               action_button_name="Cancel Application")
    assert auth.budget.consumed == 0
    assert auth.audit.records == []


def test_walk_mutation_blocked_outside_an_actuation_window(pack, issuer):
    """The background-autosave case: attested, step-authorized, in budget — and
    fired when no click is in flight."""
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    auth.begin_step(journey_id="f", step_index=0, step_fingerprint="fp",
                    now_ms=1000)
    auth.authorize_step(True)          # no open_window
    decision = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert WalkReason.WINDOW_CLOSED in decision.reason


def test_walk_mutation_blocked_on_a_step_that_did_not_authorize(pack, issuer):
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    auth.begin_step(journey_id="f", step_index=0, step_fingerprint="fp",
                    now_ms=1000)
    decision = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert WalkReason.STEP_NOT_AUTHORIZED in decision.reason


def test_walk_mutation_blocked_before_any_step_is_open(pack, issuer):
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    auth.authorize_step(True)
    auth.open_window("Save Draft", 1000)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000).allow is False


def test_open_window_is_inert_on_an_unauthorized_step(issuer):
    auth = _authorization(issuer)
    auth.begin_step(journey_id="f", step_index=0, step_fingerprint="fp", now_ms=0)
    auth.open_window("Save Draft", 0)
    assert auth.window_open is False


@pytest.mark.parametrize("url", [
    "https://evil.example.com/api/draft",
    "https://app.char.evil.net/api/draft",
    "http://app.char/api/draft",
    "https://app.char:8443/api/draft",
])
def test_walk_mutation_blocked_off_the_attested_origin(url, pack, issuer):
    """T-WP-06.  The grant names ONE origin; a write anywhere else is outside
    the environment the platform attested as throwaway."""
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    _open_step(auth)
    decision = ctx.decide("POST", url, now_ms=1000)
    assert decision.allow is False
    assert WalkReason.OFF_ORIGIN in decision.reason


def test_authorization_error_fails_closed(pack, issuer):
    """A raising authority is a refusing authority."""
    auth = _authorization(issuer)
    ctx = _ctx(pack, auth)
    _open_step(auth)

    def boom(*a, **k):
        raise RuntimeError("authority exploded")

    auth.authorize_mutation = boom          # type: ignore[assignment]
    decision = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.WALK_NOT_AUTHORIZED


# ═══ PART 3 · THE BUDGET ════════════════════════════════════════════════════

def test_budget_cannot_be_exceeded(pack, issuer):
    """T-WP-04 case 6."""
    auth = _authorization(issuer, max_walk_mutations_per_step=2)
    ctx = _ctx(pack, auth)
    _open_step(auth)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000).allow is True
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1001).allow is True
    third = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1002)
    assert third.allow is False
    assert third.rule_id == GuardRule.WALK_BUDGET_EXCEEDED
    assert auth.budget.consumed == 2


def test_budget_resets_per_logical_step(pack, issuer):
    auth = _authorization(issuer, max_walk_mutations_per_step=1)
    ctx = _ctx(pack, auth)
    _open_step(auth)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000).allow is True
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1001).allow is False

    auth.begin_step(journey_id="flow-1", step_index=1,
                    step_fingerprint="fp-step-1", now_ms=2000)
    auth.authorize_step(True)
    auth.open_window("Save Draft", 2000)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=2000).allow is True


def test_re_entering_the_same_step_does_not_refill_the_budget(issuer):
    """A step that re-identifies (a revealed question changes the page) is still
    the same step, and does not buy itself a second allowance."""
    auth = _authorization(issuer, max_walk_mutations_per_step=1)
    _open_step(auth)
    auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000)
    auth.begin_step(journey_id="flow-1", step_index=0,
                    step_fingerprint="fp-step-0-CHANGED", now_ms=1500)
    assert auth.budget.consumed == 1
    auth.authorize_step(True)
    auth.open_window("Save Draft", 1500)
    ok, why, _ = auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1500)
    assert ok is False and why == WalkReason.BUDGET_EXCEEDED


def test_budget_time_bound_closes_the_window(pack, issuer):
    auth = _authorization(issuer, max_walk_mutations_per_step=5)
    auth.budget.window_ms = 1_000
    ctx = _ctx(pack, auth)
    _open_step(auth, now_ms_=1000)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1500).allow is True
    late = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=9999)
    assert late.allow is False
    assert late.rule_id == GuardRule.WALK_BUDGET_EXCEEDED


def test_a_zero_budget_proof_grants_no_writes(pack, issuer):
    auth = _authorization(issuer, max_walk_mutations_per_step=0)
    ctx = _ctx(pack, auth)
    _open_step(auth)
    assert ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000).allow is False


def test_budget_is_atomic_under_concurrency(issuer):
    """RACE CONDITIONS.  Sixteen threads contend for a budget of three; exactly
    three may win, or the "impossible to exceed" claim is a wish."""
    auth = _authorization(issuer, max_walk_mutations_per_step=3)
    _open_step(auth)
    wins: list = []
    barrier = threading.Barrier(16)

    def go() -> None:
        barrier.wait()
        ok, _why, _rid = auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000)
        if ok:
            wins.append(1)

    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 3
    assert auth.budget.consumed == 3
    assert len(auth.audit.records) == 3


def test_step_budget_is_fail_closed_before_it_is_opened():
    budget = StepMutationBudget(max_mutations=5)
    assert budget.would_allow(0) == (False, WalkReason.NO_STEP)


# ═══ PART 3b · THE IMMUTABLE LEDGER (T-WP-03) ═══════════════════════════════

def test_every_permitted_mutation_is_audited_with_the_required_fields(issuer):
    auth = _authorization(issuer)
    _open_step(auth, control="Save Draft")
    ok, _why, request_id = auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert ok is True
    (record,) = auth.audit.records
    for required in ("timestamp_ms", "wall_clock_ms", "workflow_id", "journey_id",
                     "step_index", "step_fingerprint", "triggering_control",
                     "method", "endpoint", "request_id", "approval",
                     "budget_consumed", "budget_max", "entry_hash", "prev_hash"):
        assert required in record, f"audit record is missing {required}"
    assert record["request_id"] == request_id
    assert record["method"] == "POST"
    assert record["triggering_control"] == "Save Draft"
    assert record["workflow_id"] == CRAWL_ID
    assert record["journey_id"] == "flow-1"
    assert record["step_index"] == 0
    assert record["budget_consumed"] == 1 and record["budget_max"] == 3
    assert record["approval"]["proof_id"] and record["approval"]["kid"] == issuer.kid


def test_no_mutation_is_permitted_without_evidence(issuer):
    """EVIDENCE OR NOTHING.  An audit sink that cannot write turns the mutation
    into a refusal — it does not log a warning and let the write through."""
    def refuse(_record):
        raise IOError("disk full")

    auth = _authorization(issuer, sink=refuse)
    _open_step(auth)
    ok, why, _rid = auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert ok is False and why == WalkReason.AUDIT_FAILED
    assert auth.audit.records == []


def test_a_failed_audit_write_does_not_fork_the_chain(issuer):
    """The sink fails, then succeeds.  Sequence numbers and the chain head must
    be exactly as if the failure had never been attempted."""
    state = {"fail": True}
    written: list = []

    def flaky(record):
        if state["fail"]:
            raise IOError("transient")
        written.append(record)

    auth = _authorization(issuer, sink=flaky)
    _open_step(auth)
    auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert auth.audit.head == AUDIT_GENESIS
    state["fail"] = False
    ok, _why, _rid = auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1001)
    assert ok is True
    assert [r["sequence"] for r in written] == [1]
    assert verify_audit_chain(auth.audit.records) == (True, "")


def test_audit_chain_detects_tampering(issuer):
    """AUDIT TAMPERING.  Editing, deleting or reordering an entry breaks
    re-derivation, and the check needs no key and no service."""
    auth = _authorization(issuer)
    _open_step(auth)
    for i in range(3):
        auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000 + i)
    records = auth.audit.records
    assert verify_audit_chain(records) == (True, "")

    edited = [dict(r) for r in records]
    edited[1]["endpoint"] = "https://app.char/api/harmless"
    assert verify_audit_chain(edited)[0] is False

    assert verify_audit_chain(records[:1] + records[2:])[0] is False   # deletion
    assert verify_audit_chain(list(reversed(records)))[0] is False     # reorder


def test_response_status_is_a_linked_record_not_an_edit(issuer):
    auth = _authorization(issuer)
    _open_step(auth)
    _ok, _why, request_id = auth.authorize_mutation("POST", SAVE_DRAFT_URL,
                                                    now_ms=1000)
    auth.note_response(request_id, 201)
    records = auth.audit.records
    assert len(records) == 2
    assert "response_status" not in records[0]
    assert records[1]["kind"] == "response"
    assert records[1]["request_id"] == request_id
    assert records[1]["response_status"] == 201
    assert verify_audit_chain(records) == (True, "")


def test_request_ids_are_deterministic(issuer):
    """DETERMINISTIC REPLAY.  Two identical runs produce identical ids, so an
    evidence reviewer can diff them."""
    def ids():
        auth = _authorization(issuer)
        _open_step(auth)
        return [auth.authorize_mutation("POST", SAVE_DRAFT_URL, now_ms=1000 + i)[2]
                for i in range(3)]

    assert ids() == ids()
    assert len(set(ids())) == 3, "ids must still be unique within a step"


def test_endpoint_is_recorded_without_its_query_string():
    """A persistence endpoint's query routinely carries the applicant's answers,
    and an immutable ledger is the last place to write them."""
    scrubbed = scrub_endpoint(
        "https://app.char/api/draft?ssn=123-45-6789&dob=1980-01-01")
    assert scrubbed == "https://app.char/api/draft"
    assert "ssn" not in scrubbed and "1980" not in scrubbed


def test_audit_log_starts_at_genesis():
    assert MutationAuditLog().head == AUDIT_GENESIS
    assert verify_audit_chain([]) == (True, "")


# ═══ Construction: an authorisation cannot be conjured ══════════════════════

def test_from_verdict_returns_none_for_a_denied_verdict():
    from app.attest import AttestationVerdict
    denied = AttestationVerdict(authorized=False, reason=AttestReason.EXPIRED)
    assert WalkAuthorization.from_verdict(denied, workflow_id=CRAWL_ID) is None


def test_from_verdict_returns_none_for_no_verdict():
    assert WalkAuthorization.from_verdict(None, workflow_id=CRAWL_ID) is None


def test_legacy_submit_attestation_cannot_authorize_a_walk(pack):
    """The unsigned operator attestation is exactly what M1.3 refuses to trust
    for this purpose.  Even a perfectly valid one grants no walk mutation."""
    legacy = Attestation(attested_by="ops", env_kind="disposable",
                         expires_at_ms=now_ms() + 3_600_000)
    assert legacy.is_submit_capable() is True
    ctx = GuardContext(refuse_pack=pack, phase=Phase.WALK, attestation=legacy)
    assert ctx.walk_attested is False
    decision = ctx.decide("POST", SAVE_DRAFT_URL, now_ms=1000)
    assert decision.allow is False
    assert decision.rule_id == GuardRule.WALK_NO_ATTESTATION
