"""QE-Central S4 — coverage / shrinkage / waiver unit tests (pure; no DB).

Pins the honesty spine of :mod:`app.services.coverage`:
  * the universe diff + shrinkage → a deleted approved atom yields EXACTLY one
    P0 ``possible_deletion`` gap and flips the verdict to
    ``blocked_on_p0_gaps``;
  * ``atoms_hash`` is order-independent and stable (baseline anchor);
  * gap-id determinism (idempotent re-runs);
  * the waiver lifecycle — a waiver ANNOTATES (never deletes), is clamped to
    ≤ 90 days, and STOPS applying once expired (verdict blocks again);
  * only ``possible_deletion`` / ``unclassified_fail_up`` P0 gaps block — a
    named ``uncovered_atom`` does not silently gate the suite.

Also pins the nine S4 ORM tables' column sets against qec_001_initial so any
migration/ORM drift fails loudly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import coverage
from app.services.coverage import (
    BAND_P0,
    BAND_P2,
    GAP_ADJUDICATED,
    GAP_OPEN,
    GAP_POSSIBLE_DELETION,
    GAP_UNCLASSIFIED,
    GAP_UNCOVERED_ATOM,
    GAP_WAIVED,
    MAX_WAIVER_DAYS,
    VERDICT_BLOCKED,
    VERDICT_OK,
    apply_waiver_to_gap,
    build_possible_deletion_gap,
    build_waiver,
    clamp_waiver_expiry,
    compute_atoms_hash,
    coverage_verdict,
    diff_universe,
    gaps_for_shrinkage,
    is_gap_blocking,
    normalize_keys,
    possible_deletion_gap_id,
    waiver_active,
)

TENANT = "tenant-A"
APP = "app-1"
BASELINE = "baseline-xyz"
NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


# ── universe hashing ──────────────────────────────────────────────────────

class TestUniverseHash:
    def test_atoms_hash_is_order_independent(self):
        assert compute_atoms_hash(["b", "a", "c"]) == compute_atoms_hash(["c", "a", "b"])

    def test_atoms_hash_dedupes_and_ignores_blank(self):
        assert compute_atoms_hash(["a", "a", "", "  ", "b"]) == compute_atoms_hash(["a", "b"])

    def test_atoms_hash_changes_when_membership_changes(self):
        assert compute_atoms_hash(["a", "b"]) != compute_atoms_hash(["a", "b", "c"])

    def test_normalize_keys_sorts_and_dedupes(self):
        assert normalize_keys([" b ", "a", "b", ""]) == ["a", "b"]


# ── diff + shrinkage → gaps ───────────────────────────────────────────────

class TestShrinkage:
    def test_diff_identifies_missing_added_retained(self):
        d = diff_universe(baseline_keys=["a", "b", "c"], fresh_keys=["a", "b", "d"])
        assert d.missing == ("c",)
        assert d.added == ("d",)
        assert d.retained == ("a", "b")

    def test_identical_universe_has_no_missing(self):
        d = diff_universe(baseline_keys=["a", "b"], fresh_keys=["b", "a"])
        assert d.missing == ()
        assert d.added == ()

    def test_deleting_one_approved_atom_makes_exactly_one_p0_gap(self):
        d = diff_universe(baseline_keys=["a", "b", "c"], fresh_keys=["a", "b"])
        gaps = gaps_for_shrinkage(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, missing_keys=d.missing,
        )
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["kind"] == GAP_POSSIBLE_DELETION
        assert gap["band"] == BAND_P0
        assert gap["status"] == GAP_OPEN
        assert gap["detail"]["canonical_key"] == "c"
        assert gap["detail"]["baseline_id"] == BASELINE

    def test_verdict_flips_to_blocked_on_a_possible_deletion(self):
        gaps = gaps_for_shrinkage(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, missing_keys=["c"],
        )
        assert coverage_verdict(gaps) == VERDICT_BLOCKED

    def test_no_gaps_means_ok(self):
        assert coverage_verdict([]) == VERDICT_OK

    def test_two_missing_atoms_make_two_distinct_gaps(self):
        gaps = gaps_for_shrinkage(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, missing_keys=["c", "d"],
        )
        assert len({g["gap_id"] for g in gaps}) == 2

    def test_gap_id_is_deterministic_and_key_specific(self):
        id1 = possible_deletion_gap_id(TENANT, APP, BASELINE, "c")
        id2 = possible_deletion_gap_id(TENANT, APP, BASELINE, "c")
        id3 = possible_deletion_gap_id(TENANT, APP, BASELINE, "d")
        assert id1 == id2          # idempotent re-run → same gap
        assert id1 != id3


# ── which gaps block the all-green verdict ────────────────────────────────

class TestBlockingRule:
    def test_open_possible_deletion_blocks(self):
        gap = build_possible_deletion_gap(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, canonical_key="c",
        )
        assert is_gap_blocking(gap, now=NOW) is True

    def test_uncovered_atom_does_not_block_even_at_p0(self):
        """A named uncovered atom is surfaced but must not silently gate the
        suite — only deletions and fail-ups block the all-green verdict."""
        gap = {"kind": GAP_UNCOVERED_ATOM, "band": BAND_P0, "status": GAP_OPEN}
        assert is_gap_blocking(gap, now=NOW) is False

    def test_unclassified_fail_up_blocks(self):
        gap = {"kind": GAP_UNCLASSIFIED, "band": BAND_P0, "status": GAP_OPEN}
        assert is_gap_blocking(gap, now=NOW) is True

    def test_adjudicated_gap_does_not_block(self):
        gap = build_possible_deletion_gap(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, canonical_key="c",
        )
        gap["status"] = GAP_ADJUDICATED
        assert is_gap_blocking(gap, now=NOW) is False

    def test_non_p0_gap_does_not_block(self):
        gap = {"kind": GAP_POSSIBLE_DELETION, "band": BAND_P2, "status": GAP_OPEN}
        assert is_gap_blocking(gap, now=NOW) is False


# ── waiver lifecycle: annotate, clamp, expire ─────────────────────────────

class TestWaiverLifecycle:
    def test_clamp_defaults_to_90_day_ceiling(self):
        assert clamp_waiver_expiry(now=NOW) == NOW + timedelta(days=MAX_WAIVER_DAYS)

    def test_clamp_caps_a_too_far_request(self):
        far = NOW + timedelta(days=365)
        assert clamp_waiver_expiry(now=NOW, requested=far) == NOW + timedelta(days=90)

    def test_clamp_keeps_a_near_request(self):
        near = NOW + timedelta(days=10)
        assert clamp_waiver_expiry(now=NOW, requested=near) == near

    def test_clamp_rejects_a_past_expiry(self):
        with pytest.raises(ValueError):
            clamp_waiver_expiry(now=NOW, requested=NOW - timedelta(days=1))

    def test_build_waiver_requires_reason_and_actor(self):
        with pytest.raises(ValueError):
            build_waiver(reason="", actor="jane", now=NOW)
        with pytest.raises(ValueError):
            build_waiver(reason="known flake", actor="", now=NOW)

    def test_waiver_annotates_gap_without_deleting_detail(self):
        gap = build_possible_deletion_gap(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, canonical_key="c",
        )
        waiver = build_waiver(reason="atom retired by product", actor="jane", now=NOW)
        waived = apply_waiver_to_gap(gap, waiver)
        assert waived["status"] == GAP_WAIVED
        assert waived["waiver"]["reason"] == "atom retired by product"
        # the original finding is preserved (annotate, never delete)
        assert waived["detail"]["canonical_key"] == "c"
        assert gap["status"] == GAP_OPEN  # source gap not mutated

    def test_active_waiver_unblocks_then_expiry_reblocks(self):
        gap = build_possible_deletion_gap(
            tenant_id=TENANT, app_id=APP, baseline_id=BASELINE, canonical_key="c",
        )
        waiver = build_waiver(
            reason="retired", actor="jane", now=NOW,
            requested_expires_at=NOW + timedelta(days=30),
        )
        waived = apply_waiver_to_gap(gap, waiver)
        # within the waiver window: not blocking, verdict ok
        assert is_gap_blocking(waived, now=NOW) is False
        assert coverage_verdict([waived], now=NOW) == VERDICT_OK
        # after expiry: blocking again, verdict blocked (never a silent green)
        after = NOW + timedelta(days=31)
        assert is_gap_blocking(waived, now=after) is True
        assert coverage_verdict([waived], now=after) == VERDICT_BLOCKED

    def test_waiver_active_helper(self):
        assert waiver_active({"expires_at": (NOW + timedelta(days=1)).isoformat()}, now=NOW) is True
        assert waiver_active({"expires_at": (NOW - timedelta(days=1)).isoformat()}, now=NOW) is False
        assert waiver_active(None, now=NOW) is False
        assert waiver_active({"reason": "x"}, now=NOW) is False  # no parseable expiry → inactive


# ── ORM ⇄ migration column-set pins (qec_001_initial) ─────────────────────

class TestOrmMatchesMigration:
    """The 9 S4 ORM tables must mirror the migration column-for-column — no new
    migration is written for S4, so the migration is the source of truth."""

    EXPECTED = {
        "qec_criticality_registry": {
            "registry_version", "tenant_id", "pack", "active", "created_by", "created_at",
        },
        "qec_scenarios": {
            "scenario_id", "tenant_id", "app_id", "source_artifact_id", "name",
            "journey", "criticality_band", "criticality_evidence", "registry_version",
            "fingerprint", "diff_state", "review", "approved_snapshot", "tier",
            "materialized_artifact_id", "status", "created_at", "updated_at",
        },
        "qec_approval_events": {
            "event_id", "tenant_id", "subject_kind", "subject_id", "action",
            "payload", "signature", "actor", "carry_forward", "prev_hash",
            "chain_hash", "created_at",
        },
        "qec_coverage_atoms": {
            "atom_id", "tenant_id", "app_id", "canonical_key", "kind", "source",
            "provenance", "evidence", "first_seen", "last_seen",
        },
        "qec_certified_invariants": {
            "invariant_id", "tenant_id", "app_id", "statement", "criticality_band",
            "signature", "signed_by", "signed_at", "requires_disposable_env",
            "linked_scenario_ids", "status", "created_at", "updated_at",
        },
        "qec_universe_baselines": {
            "baseline_id", "tenant_id", "app_id", "atoms_hash", "atom_count",
            "signature", "signed_by", "prev_hash", "chain_hash", "created_at",
        },
        "qec_coverage_gaps": {
            "gap_id", "tenant_id", "app_id", "kind", "band", "detail", "status",
            "waiver", "waiver_expires_at", "created_at", "updated_at",
        },
        "qec_case_tiers": {
            "tenant_id", "artifact_id", "test_id", "tier", "evidence", "computed_at",
        },
        "qec_touch_events": {
            "touch_id", "tenant_id", "app_id", "touch_type", "band", "cycle_id",
            "source", "source_ref", "actor", "created_at",
        },
    }

    def test_every_table_column_set_matches(self):
        from app.db import gov_models

        by_table = {}
        for obj in vars(gov_models).values():
            table = getattr(obj, "__tablename__", None)
            if table in self.EXPECTED:
                by_table[table] = {c.name for c in obj.__table__.columns}
        assert set(by_table) == set(self.EXPECTED), "missing or extra ORM tables"
        for table, cols in self.EXPECTED.items():
            assert by_table[table] == cols, f"{table} column drift"

    def test_composite_pk_on_case_tiers(self):
        from app.db.gov_models import CaseTierRow

        pk = {c.name for c in CaseTierRow.__table__.primary_key.columns}
        assert pk == {"tenant_id", "artifact_id", "test_id"}

    def test_approval_and_baseline_carry_both_hash_columns(self):
        from app.db.gov_models import ApprovalEventRow, UniverseBaselineRow

        for model in (ApprovalEventRow, UniverseBaselineRow):
            cols = {c.name for c in model.__table__.columns}
            assert {"prev_hash", "chain_hash"} <= cols
