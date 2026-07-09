"""QE-Central S4 — approval gate unit tests (pure; no DB, no network).

Pins the security core of :mod:`app.services.approval`:
  * hash-chain integrity — a tamper on ANY prior event (payload, action,
    signature, or a stored hash) is detected at the exact break index, and
    forward-linkage protects downstream events even if the attacker recomputes
    the edited event's own hash;
  * the signature gate — ``approve`` without a typed e-signature raises the
    422-mapped :class:`SignatureRequiredError`, at BOTH the pure builder and the
    async appender's fail-fast pre-check (no DB touched);
  * carry-forward is recorded but is NEVER a human touch.

The chain recipe is byte-for-byte verdict_events.py:66-124
(``sha256(prev + canonical sorted-JSON payload)``).
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import approval
from app.services.approval import (
    ACTION_APPROVE,
    ACTION_CARRY_FORWARD,
    ACTION_REJECT,
    ACTION_REOPEN,
    ACTION_SUBMIT,
    SUBJECT_SCENARIO,
    SUBJECT_UNIVERSE,
    InvalidActionError,
    SignatureRequiredError,
    build_approval_event,
    build_baseline_event,
    canonical_json,
    compute_chain_hash,
    is_human_touch,
    require_signature,
    verify_approval_chain,
    verify_baseline_chain,
)

TENANT = "tenant-A"
SUBJECT = "sc-transfer"


def _chain(events_specs):
    """Build a linked approval chain from ``(action, payload, signature)``
    specs; each event's prev_hash is the previous event's chain_hash."""
    chain = []
    prev = ""
    for action, payload, signature in events_specs:
        ev = build_approval_event(
            prev_hash=prev, tenant_id=TENANT, subject_kind=SUBJECT_SCENARIO,
            subject_id=SUBJECT, action=action, payload=payload,
            signature=signature, actor="jane@acme",
        )
        chain.append(ev)
        prev = ev["chain_hash"]
    return chain


# ── canonical + hash recipe ───────────────────────────────────────────────

class TestCanonicalRecipe:
    def test_canonical_json_is_sorted_and_tight(self):
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_chain_hash_is_sha256_of_prev_plus_canonical(self):
        import hashlib

        canon = canonical_json({"x": 1})
        expected = hashlib.sha256(("prev" + canon).encode()).hexdigest()
        assert compute_chain_hash("prev", canon) == expected

    def test_genesis_prev_hash_is_empty_string(self):
        chain = _chain([(ACTION_SUBMIT, {"n": 1}, "")])
        assert chain[0]["prev_hash"] == ""


# ── chain integrity / tamper evidence ─────────────────────────────────────

class TestChainIntegrity:
    def test_intact_chain_verifies(self):
        chain = _chain([
            (ACTION_SUBMIT, {"v": 1}, ""),
            (ACTION_APPROVE, {"v": 1}, "Jane Doe"),
            (ACTION_REOPEN, {"v": 2}, ""),
        ])
        result = verify_approval_chain(chain)
        assert result.ok is True
        assert result.break_index is None
        assert result.length == 3

    def test_tampering_a_prior_payload_breaks_the_chain(self):
        chain = _chain([
            (ACTION_SUBMIT, {"v": 1}, ""),
            (ACTION_APPROVE, {"v": 1}, "Jane Doe"),
            (ACTION_REOPEN, {"v": 2}, ""),
        ])
        # Silently edit event[1]'s payload without recomputing its hash.
        chain[1]["payload"] = {"v": 999}
        result = verify_approval_chain(chain)
        assert result.ok is False
        assert result.break_index == 1
        assert result.reason == "hash_mismatch"

    def test_tampering_a_signature_breaks_the_chain(self):
        chain = _chain([
            (ACTION_SUBMIT, {"v": 1}, ""),
            (ACTION_APPROVE, {"v": 1}, "Jane Doe"),
        ])
        chain[1]["signature"] = "Someone Else"
        result = verify_approval_chain(chain)
        assert result.ok is False
        assert result.break_index == 1
        assert result.reason == "hash_mismatch"

    def test_forward_linkage_catches_a_recomputed_edit(self):
        """Even if the attacker recomputes the EDITED event's own hash, the next
        event's prev_hash no longer matches → linkage break downstream."""
        chain = _chain([
            (ACTION_SUBMIT, {"v": 1}, ""),
            (ACTION_APPROVE, {"v": 1}, "Jane Doe"),
            (ACTION_REOPEN, {"v": 2}, ""),
        ])
        forged = build_approval_event(
            prev_hash=chain[1]["prev_hash"], tenant_id=TENANT,
            subject_kind=SUBJECT_SCENARIO, subject_id=SUBJECT,
            action=ACTION_APPROVE, payload={"v": 999}, signature="Jane Doe",
            actor="jane@acme", event_id=chain[1]["event_id"],
            created_at=chain[1]["created_at"],
        )
        chain[1] = forged  # self-consistent event, but chain_hash now differs
        result = verify_approval_chain(chain)
        assert result.ok is False
        assert result.break_index == 2          # event[2] still points at the old hash
        assert result.reason == "linkage_broken"

    def test_tampering_a_stored_chain_hash_is_detected(self):
        chain = _chain([(ACTION_SUBMIT, {"v": 1}, "")])
        chain[0]["chain_hash"] = "0" * 64
        result = verify_approval_chain(chain)
        assert result.ok is False
        assert result.break_index == 0
        assert result.reason == "hash_mismatch"

    def test_empty_chain_is_trivially_ok(self):
        result = verify_approval_chain([])
        assert result.ok is True
        assert result.length == 0


# ── signature gate (422) ──────────────────────────────────────────────────

class TestSignatureGate:
    def test_require_signature_blocks_unsigned_approve(self):
        with pytest.raises(SignatureRequiredError) as exc:
            require_signature(ACTION_APPROVE, "")
        assert exc.value.http_status == 422

    def test_require_signature_allows_signed_approve(self):
        assert require_signature(ACTION_APPROVE, "  Jane Doe  ") == "Jane Doe"

    def test_non_approve_actions_never_need_a_signature(self):
        for action in (ACTION_SUBMIT, ACTION_REJECT, ACTION_REOPEN, ACTION_CARRY_FORWARD):
            assert require_signature(action, "") == ""

    def test_build_event_refuses_unsigned_approve(self):
        with pytest.raises(SignatureRequiredError):
            build_approval_event(
                prev_hash="", tenant_id=TENANT, subject_kind=SUBJECT_SCENARIO,
                subject_id=SUBJECT, action=ACTION_APPROVE, signature="",
            )

    def test_build_event_rejects_unknown_action(self):
        with pytest.raises(InvalidActionError):
            build_approval_event(
                prev_hash="", tenant_id=TENANT, subject_kind=SUBJECT_SCENARIO,
                subject_id=SUBJECT, action="delete",
            )

    def test_append_event_fails_fast_on_unsigned_approve_without_db(self):
        """The async appender validates the signature BEFORE opening any
        session, so a refused approve never persists a row (and needs no DB)."""
        with pytest.raises(SignatureRequiredError) as exc:
            asyncio.run(approval.append_event(
                tenant_id=TENANT, subject_kind=SUBJECT_SCENARIO,
                subject_id=SUBJECT, action=ACTION_APPROVE, signature="",
            ))
        assert exc.value.http_status == 422

    def test_append_baseline_fails_fast_on_unsigned_approve_without_db(self):
        with pytest.raises(SignatureRequiredError):
            asyncio.run(approval.append_universe_baseline(
                tenant_id=TENANT, app_id="a1", atoms_hash="deadbeef",
                atom_count=3, signature="",
            ))

    def test_build_baseline_refuses_unsigned(self):
        with pytest.raises(SignatureRequiredError):
            build_baseline_event(
                prev_hash="", tenant_id=TENANT, app_id="a1",
                atoms_hash="deadbeef", atom_count=3, signature="",
            )


# ── carry-forward is not a human touch ────────────────────────────────────

class TestHumanTouch:
    def test_signed_approve_is_a_touch(self):
        assert is_human_touch(ACTION_APPROVE, carry_forward=False) is True

    def test_reject_and_reopen_are_touches(self):
        assert is_human_touch(ACTION_REJECT, carry_forward=False) is True
        assert is_human_touch(ACTION_REOPEN, carry_forward=False) is True

    def test_submit_is_not_a_touch(self):
        assert is_human_touch(ACTION_SUBMIT, carry_forward=False) is False

    def test_carry_forward_is_never_a_touch(self):
        assert is_human_touch(ACTION_CARRY_FORWARD, carry_forward=True) is False
        # even an 'approve' auto-carry (UNCHANGED scenario) is zero touch
        assert is_human_touch(ACTION_APPROVE, carry_forward=True) is False

    def test_built_event_exposes_is_touch(self):
        approve = build_approval_event(
            prev_hash="", tenant_id=TENANT, subject_kind=SUBJECT_SCENARIO,
            subject_id=SUBJECT, action=ACTION_APPROVE, signature="Jane Doe",
        )
        assert approve["is_touch"] is True
        carry = build_approval_event(
            prev_hash=approve["chain_hash"], tenant_id=TENANT,
            subject_kind=SUBJECT_SCENARIO, subject_id="sc-unchanged",
            action=ACTION_CARRY_FORWARD, carry_forward=True,
        )
        assert carry["is_touch"] is False


# ── baseline chain uses the same recipe with its own projection ───────────

class TestBaselineChain:
    def test_baseline_chain_verifies_and_detects_tamper(self):
        prev = ""
        chain = []
        for h, n in (("h1", 3), ("h2", 4), ("h3", 4)):
            ev = build_baseline_event(
                prev_hash=prev, tenant_id=TENANT, app_id="a1",
                atoms_hash=h, atom_count=n, signature="Jane Doe", signed_by="jane",
            )
            chain.append(ev)
            prev = ev["chain_hash"]
        assert verify_baseline_chain(chain).ok is True
        # tamper the approved atoms_hash of a prior baseline
        chain[1]["atoms_hash"] = "forged"
        broken = verify_baseline_chain(chain)
        assert broken.ok is False
        assert broken.break_index == 1
