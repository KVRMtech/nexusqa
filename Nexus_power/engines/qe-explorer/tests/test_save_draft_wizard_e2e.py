"""M1.3 / T-WP-05 + T-WP-06 — THE SAVE-DRAFT WIZARD, END TO END.

The unit suites prove the verifier, the guard and the ledger in isolation.  This
one proves the thing the milestone was actually asked for, through the REAL
``Crawler``, the REAL ``GuardContext`` and the REAL refuse pack:

    Walk -> Save Draft POST -> server persists state -> crawler advances ->
    journey continues

and its indispensable negative: the IDENTICAL workflow, with anything less than
a verified platform proof, terminates at the mutation guard.

WHY THE FIXTURE IS HONEST.  The scripted application does not merely record that
a request was made — it refuses to serve step 2 until the draft has actually
been persisted, and a request the guard BLOCKS persists nothing.  So a broken
guard cannot fake a pass here, and a guard that over-blocks cannot fake one
either: the walk depth is a direct measurement of what the network policy
permitted.
"""
from __future__ import annotations

import json

import pytest

from app.attest import AttestReason
from app.budget import Budget
from app.config import Settings
from app.guard import Phase, load_refuse_pack
from app.guard_context import GuardContext
from app.walk_persist import MutationAuditLog, WalkAuthorization, verify_audit_chain
from tests._attest_kit import TENANT_ID, Issuer, now_ms
from tests.characterization.harness import Fixture, ScriptedPage, control, run_fixture

# The characterization harness pins these two; the proof must be bound to them
# or the verifier refuses it — which is itself asserted below.
CRAWL_ID = "char-crawl"
CHAR_TENANT = "char-tenant"

ORIGIN = "https://app.char"
COVERAGE_URL = f"{ORIGIN}/apply/coverage"
BENEFICIARY_URL = f"{ORIGIN}/apply/beneficiary"
REVIEW_URL = f"{ORIGIN}/apply/review"

DRAFT_ENDPOINT = f"{ORIGIN}/api/application/draft"
STEP_ENDPOINT = f"{ORIGIN}/api/application/step"


# ─── The application under test ─────────────────────────────────────────────

def _save_draft_wizard() -> dict:
    """A three-step application whose每 step is server-persisted.

    Modelled on ``tests/browser/fixtures/10-save-draft-wizard``: the same four
    actuators with the same four meanings — Back reverses, Save Draft persists
    without advancing, Continue advances, Cancel Application is irreversible and
    must never be clicked.
    """
    def step(url: str, title: str, token: str, nxt: str) -> ScriptedPage:
        return ScriptedPage(
            url=url, title=title,
            controls=[
                control("textbox", "Face Amount", tag="input", input_type="number",
                        kind="text"),
                control("button", "Back", tag="button"),
                control("button", "Save Draft", tag="button"),
                control("button", "Continue", tag="button"),
                control("button", "Cancel Application", tag="button"),
            ],
            transitions={"Continue": nxt},
            # THE SERVER'S GATE: no persisted draft, no next step.
            requires_persisted=token,
            emits={
                "Save Draft": [{"method": "POST", "url": DRAFT_ENDPOINT}],
                "Continue": [{"method": "POST", "url": STEP_ENDPOINT}],
                # The irreversible control is wired to a real destructive
                # endpoint so that a walk which ever clicked it would be visible
                # here as an allowed DELETE. It never is.
                "Cancel Application": [{"method": "DELETE",
                                        "url": f"{ORIGIN}/api/application"}],
            },
            persists={"Save Draft": token},
        )

    return {
        "coverage": step(COVERAGE_URL, "Coverage", "draft-1", "beneficiary"),
        "beneficiary": step(BENEFICIARY_URL, "Beneficiary", "draft-2", "review"),
        "review": ScriptedPage(
            url=REVIEW_URL, title="Review",
            controls=[control("link", "Back Home", href="/apply/coverage")],
            displayed_values=[{"label": "Premium", "selector": "#p",
                               "text": "$118.40"}],
        ),
    }


# ─── Wiring ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pack():
    return load_refuse_pack(Settings().refuse_pack_path)


def _attested_guard(pack, issuer: Issuer, **proof_over) -> GuardContext:
    """A GuardContext holding a VERIFIED platform proof, built through exactly
    the production verification path (``app.main._walk_authorization`` calls the
    same function with the same arguments)."""
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    payload = {"proof": issuer.proof(crawl_id=CRAWL_ID, tenant_id=CHAR_TENANT,
                                     **proof_over),
               "revocations": issuer.revocations()}
    verdict = verify_provisioning_proof(
        payload, trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=CHAR_TENANT,
        target_url=COVERAGE_URL, now_epoch_ms=now_ms(),
        replay_guard=ProofReplayGuard())
    auth = WalkAuthorization.from_verdict(
        verdict, workflow_id=CRAWL_ID, audit=MutationAuditLog(),
        window_ms=15_000)
    return GuardContext(refuse_pack=pack, walk_authorization=auth), verdict


def _crawl(tmp_path, monkeypatch, guard_ctx=None):
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True)
    kwargs = {"crawl_mode": "e2e", "wizard_enabled": True, "e2e_wizard_steps": 60,
              "budget": Budget(rate_per_s=0, max_depth=6)}
    if guard_ctx is not None:
        kwargs["guard_context"] = guard_ctx
    fixture = Fixture(name="save_draft_wizard", pages=_save_draft_wizard(),
                      start="coverage", target_url=COVERAGE_URL, kwargs=kwargs)
    text, digest = run_fixture(fixture, work, monkeypatch)
    body = text.split("===SUMMARY===")[0]
    records = [json.loads(line) for line in body.splitlines() if line.strip()]
    return records, digest


def _port_of(records):
    """The scripted port is not returned by run_fixture; the manifest carries
    everything these assertions need, so the network is read back from the
    audit + guard records instead of from the fake."""
    return records


def _walk_mutations(records):
    return [r for r in records
            if r.get("type") == "walk_mutation" and "method" in r]


def _flows(digest):
    return digest["coverage"]["flows"]


# ═══ T-WP-05 · THE POSITIVE CASE ════════════════════════════════════════════

def test_save_draft_advances_the_journey_on_a_verified_disposable_env(
        tmp_path, monkeypatch, pack):
    issuer = Issuer()
    guard_ctx, verdict = _attested_guard(pack, issuer)
    assert verdict.authorized and verdict.reason == AttestReason.OK

    records, digest = _crawl(tmp_path, monkeypatch, guard_ctx)

    # 1 · The journey CONTINUED. Three steps means both Save Draft POSTs were
    #     permitted and both servers persisted — the fixture cannot serve step 2
    #     or step 3 otherwise.
    flows = _flows(digest)
    assert flows, "no journey was recorded at all"
    deepest = max(flows, key=lambda f: f["step_count"])
    assert deepest["step_count"] >= 3, (
        f"the walk stopped at {deepest['step_count']} step(s); the save-draft "
        f"funnel is three steps deep — terminal={deepest['terminal']}")
    assert deepest["completed"] is True
    assert deepest["terminal"] in ("no_advance", "submit_boundary")

    # 2 · Every permitted mutation is in the ledger.
    mutations = _walk_mutations(records)
    assert mutations, "the walk advanced but recorded no mutation evidence"
    assert {m["method"] for m in mutations} == {"POST"}
    assert DRAFT_ENDPOINT in {m["endpoint"] for m in mutations}
    assert {m["triggering_control"] for m in mutations} <= {"Save Draft", "Continue"}

    # 3 · No mutation escaped its budget, and every one names its approval.
    for m in mutations:
        assert m["budget_consumed"] <= m["budget_max"] == 3
        assert m["approval"]["proof_id"] == verdict.proof_id
        assert m["approval"]["env_kind"] == "disposable"
        assert m["approval"]["kid"] == issuer.kid
        assert m["workflow_id"] == CRAWL_ID
        assert m["journey_id"] and m["step_fingerprint"]

    # 4 · The ledger re-derives.
    chain = [r for r in records if r.get("type") == "walk_mutation"]
    ok, why = verify_audit_chain(chain)
    assert ok, f"audit chain does not re-derive: {why}"

    # 5 · crawl_meta reports the grant honestly.
    meta = [r for r in records if r.get("type") == "crawl_meta"][-1]
    assert meta["walk_persistence"]["authorized"] is True
    assert meta["walk_persistence"]["proof_id"] == verdict.proof_id
    assert meta["walk_persistence"]["mutations"] == len(mutations)

    # 6 · THE IRREVERSIBLE CONTROL WAS NEVER CROSSED.
    assert not [m for m in mutations if m["method"] == "DELETE"]
    assert "Cancel Application" not in {m["triggering_control"] for m in mutations}


def test_the_walk_never_treats_save_draft_as_an_advance(tmp_path, monkeypatch, pack):
    """Fixture 10's central warning: a walk that counts Save Draft as an advance
    records a funnel step that never happened.  Every recorded step must be a
    distinct page, and the advance evidence must name Continue."""
    guard_ctx, _ = _attested_guard(pack, Issuer())
    records, digest = _crawl(tmp_path, monkeypatch, guard_ctx)

    deepest = max(_flows(digest), key=lambda f: f["step_count"])
    urls = [s["url"] for s in deepest["steps"]]
    assert len(set(urls)) == len(urls), f"a step was recorded twice: {urls}"
    advances = [s["advance"]["control_name"] for s in deepest["steps"]
                if s.get("advance")]
    assert advances and all(a == "Continue" for a in advances), advances


# ═══ T-WP-05 / T-WP-06 · THE NEGATIVE CASES ═════════════════════════════════

def test_the_identical_workflow_terminates_at_the_guard_without_attestation(
        tmp_path, monkeypatch):
    """The same application, the same crawler, no proof.  The Save Draft POST is
    blocked, the server never persists, and the wizard genuinely cannot
    advance."""
    records, digest = _crawl(tmp_path, monkeypatch, guard_ctx=None)

    assert _walk_mutations(records) == []
    flows = _flows(digest)
    deepest = max(flows, key=lambda f: f["step_count"]) if flows else {"step_count": 0}
    assert deepest["step_count"] <= 1, (
        "the walk advanced past a server-persisted step with no attestation")

    meta = [r for r in records if r.get("type") == "crawl_meta"][-1]
    assert meta["walk_persistence"] == {
        "authorized": False, "reason": "not_attested",
        "max_mutations_per_step": 0, "mutations": 0}


@pytest.mark.parametrize("proof_over,expected", [
    ({"env_kind": "prod"}, AttestReason.NOT_DISPOSABLE),
    ({"env_kind": "staging"}, AttestReason.NOT_DISPOSABLE),
    ({"env_kind": "uat"}, AttestReason.NOT_DISPOSABLE),
    ({"env_kind": ""}, AttestReason.MALFORMED_CLAIMS),
    ({"issued_at_ms": now_ms() - 7_200_000,
      "expires_at_ms": now_ms() - 3_600_000}, AttestReason.EXPIRED),
    ({"issuer": "not-the-platform"}, AttestReason.ISSUER_MISMATCH),
    ({"crawl_id": "some-other-crawl"}, AttestReason.CRAWL_BINDING_MISMATCH),
    ({"tenant_id": "tenant-victim"}, AttestReason.TENANT_MISMATCH),
    ({"target_origin": "https://prod.example.com"}, AttestReason.ORIGIN_MISMATCH),
], ids=["prod", "staging", "uat", "blank-env", "expired", "wrong-issuer",
        "wrong-crawl", "wrong-tenant", "prod-origin"])
def test_zero_walk_mutations_for_every_non_disposable_attestation(
        proof_over, expected, tmp_path, monkeypatch, pack):
    """T-WP-06.  Production, unknown, missing, expired, revoked or unverifiable
    — every one of them yields ZERO walk mutations and a walk that stops at the
    first server-persisted step."""
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    issuer = Issuer()
    over = dict(proof_over)
    over.setdefault("crawl_id", CRAWL_ID)
    over.setdefault("tenant_id", CHAR_TENANT)
    payload = {"proof": issuer.proof(**over), "revocations": issuer.revocations()}
    verdict = verify_provisioning_proof(
        payload, trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=CHAR_TENANT,
        target_url=COVERAGE_URL, now_epoch_ms=now_ms(),
        replay_guard=ProofReplayGuard())
    assert verdict.authorized is False
    assert verdict.reason == expected

    auth = WalkAuthorization.from_verdict(verdict, workflow_id=CRAWL_ID)
    assert auth is None, "a denied verdict must never yield an authorization"

    guard_ctx = GuardContext(refuse_pack=pack, walk_authorization=auth,
                             walk_denied_reason=verdict.reason)
    records, digest = _crawl(tmp_path, monkeypatch, guard_ctx)

    assert _walk_mutations(records) == []
    flows = _flows(digest)
    deepest = max(flows, key=lambda f: f["step_count"]) if flows else {"step_count": 0}
    assert deepest["step_count"] <= 1
    meta = [r for r in records if r.get("type") == "crawl_meta"][-1]
    assert meta["walk_persistence"]["authorized"] is False
    assert meta["walk_persistence"]["reason"] == expected


def test_a_revoked_proof_yields_zero_walk_mutations(tmp_path, monkeypatch, pack):
    """T-WP-06 · revocation, driven end to end."""
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    issuer = Issuer()
    proof = issuer.proof(crawl_id=CRAWL_ID, tenant_id=CHAR_TENANT)
    payload = {"proof": proof,
               "revocations": issuer.revocations(
                   revoked_proof_ids=[proof["claims"]["proof_id"]])}
    verdict = verify_provisioning_proof(
        payload, trust=issuer.trust(), crawl_id=CRAWL_ID, tenant_id=CHAR_TENANT,
        target_url=COVERAGE_URL, now_epoch_ms=now_ms(),
        replay_guard=ProofReplayGuard())
    assert verdict.reason == AttestReason.REVOKED

    guard_ctx = GuardContext(refuse_pack=pack, walk_denied_reason=verdict.reason)
    records, digest = _crawl(tmp_path, monkeypatch, guard_ctx)
    assert _walk_mutations(records) == []
    flows = _flows(digest)
    deepest = max(flows, key=lambda f: f["step_count"]) if flows else {"step_count": 0}
    assert deepest["step_count"] <= 1


def test_observe_only_posture_overrides_a_valid_proof(tmp_path, monkeypatch, pack):
    """Defence in depth.  Even with a verified proof in hand, an observe-only
    crawl performs ZERO walk mutations — the two gates are independent and the
    more restrictive one wins."""
    guard_ctx, _ = _attested_guard(pack, Issuer())
    work = tmp_path / "qec_char_work"
    work.mkdir(parents=True)
    fixture = Fixture(
        name="save_draft_observe", pages=_save_draft_wizard(), start="coverage",
        target_url=COVERAGE_URL,
        kwargs={"crawl_mode": "e2e", "wizard_enabled": True,
                "e2e_wizard_steps": 60, "observe_only": True,
                "budget": Budget(rate_per_s=0), "guard_context": guard_ctx})
    text, _digest = run_fixture(fixture, work, monkeypatch)
    records = [json.loads(line) for line in
               text.split("===SUMMARY===")[0].splitlines() if line.strip()]
    assert _walk_mutations(records) == []


# ═══ The phase never leaks ══════════════════════════════════════════════════

def test_the_crawl_never_ends_standing_in_the_walk_phase(
        tmp_path, monkeypatch, pack):
    """The actuation window is opened and closed around ONE click.  A crawl left
    in WALK would keep permitting writes after the walk finished."""
    guard_ctx, _ = _attested_guard(pack, Issuer())
    _crawl(tmp_path, monkeypatch, guard_ctx)
    assert guard_ctx.phase is Phase.EXPLORE
    assert guard_ctx.walk_authorization.window_open is False
    assert guard_ctx.walk_authorization.step_authorized is False
    # And a post-crawl POST is refused, whatever the phase is set to.
    assert guard_ctx.decide("POST", DRAFT_ENDPOINT, now_ms=10**9).allow is False
