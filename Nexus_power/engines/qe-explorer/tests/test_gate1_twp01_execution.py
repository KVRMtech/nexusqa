"""Gate 1 / WP7 — T-WP-01 EXECUTED, from a real signed proof to a sealed ledger.

WHY THIS COULD NOT BE WRITTEN BEFORE, and what its existence proves.

M1.3 shipped :mod:`app.walk_persist` complete: the four conditions, the per-step
budget, the actuation window, the hash-chained audit ledger.  Its entry point is
``WalkAuthorization.from_verdict``, which returns ``None`` for any verdict that
is not authorising — and until Gate 1's WP6 nothing in the world could produce an
authorising one, because the issuer half of the attestation did not exist.

So every existing T-WP-01 test necessarily constructs its own
:class:`AttestationVerdict` by hand.  Those tests are correct and they prove the
budget arithmetic, but they cannot prove the thing an operator actually needs to
know: **that a proof the platform really minted really opens the window.**  A
renamed claim, a drifted canonical encoding or a mismatched key-id derivation
would leave all fifty of them green while the feature stayed unreachable in
production — which is exactly the state it has been in since it was written.

This module closes that loop.  It starts from the FROZEN GOLDEN ATTESTATION that
``qe-central``'s issuer minted (``contracts/gate1_walk_attestation_v1.json``),
runs it through the REAL verifier, hands the resulting verdict to the REAL
``from_verdict``, and drives a mutation through to a sealed audit record.

THE DEPENDENCY ORDER IS THE POINT.  WP7 was gated on WP6 for a reason that is
visible here in one file: without an issuer there is no verdict, without a
verdict there is no authorization, and without an authorization T-WP-01 is a
correct implementation of nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import attest
from app import guard
from app.config import Settings
from app.guard import load_refuse_pack
from app.walk_persist import (
    MutationAuditLog,
    WalkAuthorization,
    WalkReason,
    verify_audit_chain,
)


def _contract() -> dict:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "gate1_walk_attestation_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError("contracts/gate1_walk_attestation_v1.json not found")


CONTRACT = _contract()
GRANT = CONTRACT["grant"]
ISSUED_MS = int(CONTRACT["issued_at_ms"])
NOW_MS = ISSUED_MS + 60_000
TARGET = "https://staging.example.test:8443/apply"
_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


def _verdict() -> attest.AttestationVerdict:
    """THE REAL CHAIN: issuer output -> production verifier -> verdict."""
    return attest.verify_provisioning_proof(
        CONTRACT["attestation"],
        trust=attest.TrustStore.from_public_keys(
            [CONTRACT["test_keys"]["public_key_b64"]],
            issuer=CONTRACT["issuer"], max_mutations_per_step=3),
        crawl_id=GRANT["crawl_id"], tenant_id=GRANT["tenant_id"],
        target_url=GRANT["target_url"], now_epoch_ms=NOW_MS,
        replay_guard=attest.ProofReplayGuard())


def _authorization(**kw) -> WalkAuthorization:
    auth = WalkAuthorization.from_verdict(
        _verdict(), workflow_id="wf-gate1", audit=MutationAuditLog(),
        clock_ms=lambda: NOW_MS, wall_clock_ms=lambda: NOW_MS, **kw)
    assert auth is not None, (
        "from_verdict returned None for an authorising verdict — T-WP-01 is "
        "unreachable and this whole module is the proof that it is not")
    return auth


def _armed(**kw) -> WalkAuthorization:
    """A walk standing on an authorised step with its window open."""
    auth = _authorization(**kw)
    auth.begin_step(journey_id="j1", step_index=3, step_fingerprint="fp-3",
                    now_ms=NOW_MS)
    auth.authorize_step(True)
    auth.open_window("Save Draft", NOW_MS)
    return auth


# ─── dependency verification: WP7's stated prerequisite ─────────────────────

def test_the_issuer_produces_a_verdict_that_authorises():
    """WP7's prerequisite, asserted as a fact rather than assumed.

    This single assertion is what WP6 bought: before it, ``authorized`` was
    False on every path through the verifier that existed.
    """
    verdict = _verdict()
    assert verdict.authorized, verdict.reason
    assert verdict.reason == attest.AttestReason.OK
    assert verdict.env_kind == attest.DISPOSABLE
    assert verdict.max_mutations_per_step == 3


def test_from_verdict_returns_an_authorization_rather_than_none():
    """``None`` is the entire backward-compatibility story of M1.3: an
    unattested crawl behaves byte-identically to a pre-M1.3 one.  It is also
    what every crawl got, always, until now."""
    assert WalkAuthorization.from_verdict(
        _verdict(), workflow_id="wf-gate1") is not None


def test_a_denied_verdict_still_yields_no_authorization():
    """The backward-compatible path must survive the issuer existing.  A proof
    for a DIFFERENT crawl is refused, and a refusal must still be inert rather
    than partially enabled."""
    denied = attest.verify_provisioning_proof(
        CONTRACT["attestation"],
        trust=attest.TrustStore.from_public_keys(
            [CONTRACT["test_keys"]["public_key_b64"]],
            issuer=CONTRACT["issuer"]),
        crawl_id="crawl-someone-else", tenant_id=GRANT["tenant_id"],
        target_url=GRANT["target_url"], now_epoch_ms=NOW_MS,
        replay_guard=attest.ProofReplayGuard())

    assert not denied.authorized
    assert WalkAuthorization.from_verdict(denied, workflow_id="wf") is None


# ─── the four conditions, each denied independently ─────────────────────────

def test_all_four_conditions_together_permit_one_mutation():
    """THE END-TO-END ASSERTION.  Signed proof -> verified verdict -> authorised
    step -> open window -> a released request with an audit id."""
    auth = _armed()
    allowed, reason, request_id = auth.authorize_mutation(
        "POST", TARGET, now_ms=NOW_MS)

    assert allowed, reason
    assert request_id, "a released request is always identified in the ledger"


def test_an_unauthorised_step_is_refused_even_under_a_valid_proof():
    """Condition 4.  A step the walk merely OBSERVES never becomes mutable, no
    matter how good the cryptography is."""
    auth = _authorization()
    auth.begin_step(journey_id="j1", step_index=1, step_fingerprint="fp-1",
                    now_ms=NOW_MS)
    allowed, reason, _ = auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)

    assert not allowed
    assert reason == WalkReason.STEP_NOT_AUTHORIZED


def test_a_closed_window_is_refused_even_on_an_authorised_step():
    """What keeps background autosave, analytics beacons and co-located forms
    out of the grant: the window is around ONE actuation, not around the step."""
    auth = _armed()
    auth.close_window()
    allowed, reason, _ = auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)

    assert not allowed
    assert reason == WalkReason.WINDOW_CLOSED


def test_the_per_step_budget_is_the_one_the_signed_claims_asked_for():
    """The budget is carried INSIDE the signed claims, so it cannot be widened by
    anything a tenant writes into a dispatch body."""
    auth = _armed()
    results = [auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)[0]
               for _ in range(4)]

    assert results[:3] == [True, True, True], "three, as the claims asked"
    assert results[3] is False, "and not a fourth"


def test_a_new_step_gets_a_fresh_budget():
    auth = _armed()
    for _ in range(3):
        auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)
    assert not auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)[0]

    auth.end_step()
    auth.begin_step(journey_id="j1", step_index=4, step_fingerprint="fp-4",
                    now_ms=NOW_MS)
    auth.authorize_step(True)
    auth.open_window("Continue", NOW_MS)

    assert auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)[0]


# ─── the audit trail the grant is conditional on ────────────────────────────

def test_every_released_mutation_is_recorded_before_it_is_released():
    auth = _armed()
    allowed, _, request_id = auth.authorize_mutation("POST", TARGET,
                                                     now_ms=NOW_MS)
    assert allowed

    records = auth.audit.records
    assert len(records) == 1
    assert records[0]["request_id"] == request_id


def test_the_audit_chain_verifies():
    """A hash-chained ledger is the only reason a per-request grant with no human
    in the loop is acceptable at all."""
    auth = _armed()
    for _ in range(3):
        auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)

    ok, why = verify_audit_chain(auth.audit.records)
    assert ok, why


def test_editing_one_audit_record_breaks_the_chain():
    auth = _armed()
    for _ in range(3):
        auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)

    records = auth.audit.records
    records[1]["method"] = "DELETE"
    ok, why = verify_audit_chain(records)

    assert not ok
    assert why, "a broken chain must say where"


def test_the_audit_record_carries_the_proof_it_was_authorised_by():
    """Traceability: a mutation in the ledger can be tied back to the exact
    signed proof that permitted it, which is what makes the trail auditable by
    somebody who does not trust the process that wrote it."""
    auth = _armed()
    auth.authorize_mutation("POST", TARGET, now_ms=NOW_MS)
    record = auth.audit.records[0]

    blob = json.dumps(record)
    assert CONTRACT["attestation"]["proof"]["claims"]["proof_id"] in blob, (
        "the released mutation does not name the proof that authorised it")


def test_the_audit_record_carries_no_request_body():
    """Value-free by construction: the ledger is a durable, widely-read artifact
    and a request body is per-crawl evidence with a much tighter blast radius."""
    record = _armed()
    record.authorize_mutation("POST", TARGET + "?ssn=123-45-6789", now_ms=NOW_MS)
    blob = json.dumps(record.audit.records[0])

    assert "123-45-6789" not in blob, "a query value reached the audit trail"
    assert "body" not in blob


# ─── irreversible verbs are never in this grant ─────────────────────────────
#
# WHERE THIS GATE LIVES, and why these tests call a different module than the
# ones above.  ``walk_persist.authorize_mutation`` owns the BUDGET, the WINDOW
# and the ORIGIN; it does not inspect the request's meaning.  Legality of the
# ACT is the guard's, and it is decided on the SEMANTIC VERB — the action button
# and the URL — not on the HTTP method, because "Delete Account" and "Save Draft"
# are both a POST and only one of them may ever cross.
#
# Asserting this against ``authorize_mutation`` would therefore have tested the
# wrong layer and, worse, would have passed for the wrong reason once someone
# added a method check there.

def _walk_decision(action_button_name: str, url: str = TARGET,
                   *, attested: bool = True):
    """The production guard, called with its pinned positional signature."""
    return guard.classify_request(
        "POST", url, guard.Phase.WALK, _REFUSE_PACK, False,
        action_button_name, walk_attested=attested)


def test_an_irreversible_verb_is_refused_in_walk_even_when_attested():
    """The narrower-than-SUBMIT property, stated as a test.  SUBMIT may cross an
    irreversible boundary because a human named that specific flow; WALK fires on
    every wizard step the crawler picks for itself, so "Save Draft" earning the
    same rights as "Delete Account" is the escalation this feature would
    otherwise ship."""
    decision = _walk_decision("Delete Account")
    assert not decision.allow, "an irreversible verb crossed under a walk grant"
    assert decision.rule_id == guard.GuardRule.WALK_IRREVERSIBLE_BLOCKED


def test_a_persistence_verb_is_permitted_in_walk_when_attested():
    decision = _walk_decision("Save Draft")
    assert decision.allow, decision.reason
    assert decision.rule_id == guard.GuardRule.WALK_MUTATION_OK


def test_no_mutation_crosses_in_walk_without_an_attestation():
    """The condition every other one is downstream of."""
    decision = _walk_decision("Save Draft", attested=False)
    assert not decision.allow
    assert decision.rule_id == guard.GuardRule.WALK_NO_ATTESTATION


# ─── the origin binding survives into the mutation decision ────────────────

def test_a_mutation_at_another_origin_is_refused():
    """The signed claims bind ONE origin.  A proof for throwaway staging must
    not release a request at production even mid-window."""
    auth = _armed()
    allowed, reason, _ = auth.authorize_mutation(
        "POST", "https://production.example.test/apply", now_ms=NOW_MS)

    assert not allowed
    assert reason == WalkReason.OFF_ORIGIN
