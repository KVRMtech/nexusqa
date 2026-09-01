"""QE-Central Phase-8 — compliance-adapter projection tests (no DB, no network).

Pins the load-bearing properties of the SEAM-E compliance projections:

  * an adapter projects a synthetic verdict/dossier/waiver/audit set into the
    framework-shaped report WITHOUT mutating the input or the bundle;
  * the projection is DETERMINISTIC (same bundle → identical dict) and HASH-
    VERIFIABLE (``report_digest`` recomputes from the report body);
  * it is TENANT-SCOPED — a tenant-A report can never surface tenant-B evidence,
    even when both tenants' rows are handed to the bundle builder together;
  * an unknown framework is a 404 (``UnknownFrameworkError`` → the router maps it);
  * the hash-chain re-derivation is genuinely tamper-EVIDENT (a rewritten verdict
    flips the chain to unverified and downgrades the integrity control);
  * operational controls are never green-washed to ``satisfied`` from evidence
    the projection cannot see; code-enforced controls hold at zero rows.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.compliance import (
    EUAIActAnnex22Adapter,
    EvidenceBundle,
    EvidenceWindow,
    NAICModelAuditAdapter,
    SOC2Adapter,
    UnknownFrameworkError,
    available_frameworks,
    build_report,
    compute_chain_hash,
    get_adapter,
    report_digest,
    verify_verdict_chains,
)
from app.compliance.adapter import (
    CHAIN_REGISTRY_VERSION,
    STATUS_NOT_EVIDENCED,
    STATUS_OPERATIONAL,
    STATUS_PARTIAL,
    STATUS_SATISFIED,
)

_BASE_TS = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── synthetic evidence builders ─────────────────────────────────────────────
def _make_chain(tenant: str, artifact: str, test: str, overalls: list[int]) -> list[dict]:
    """Build a correctly hash-chained verdict list for one (tenant, artifact, test)."""
    verdicts: list[dict] = []
    prev = ""
    for i, overall in enumerate(overalls):
        v = {
            "verdict_id": f"{artifact}-{test}-{i}",
            "tenant_id": tenant,
            "artifact_id": artifact,
            "test_id": test,
            "version": i + 1,
            "registry_version": CHAIN_REGISTRY_VERSION,
            "source": "verify",
            "actor": "tester@acme",
            "overall": overall,
            "decision": "ship" if overall >= 80 else "hold",
            "axes": {"grounding": overall, "assertions": overall - 5},
            "gaps": 0,
            "created_at": _BASE_TS + timedelta(seconds=i),
        }
        v["chain_hash"] = compute_chain_hash(prev, v)
        prev = v["chain_hash"]
        verdicts.append(v)
    return verdicts


def _dossier(tenant: str, artifact: str, test: str, n: int) -> dict:
    return {
        "verdict_id": f"{artifact}-{test}-{n}",
        "tenant_id": tenant,
        "artifact_id": artifact,
        "test_id": test,
        "chain_hash": f"chain-{artifact}-{n}",
        "created_at": _BASE_TS + timedelta(seconds=n),
    }


def _waiver(tenant: str, artifact: str, n: int) -> dict:
    return {
        "waiver_id": f"wv-{artifact}-{n}",
        "tenant_id": tenant,
        "artifact_id": artifact,
        "test_id": "t1",
        "finding_match": "flaky selector",
        "reason": "known third-party widget",
        "actor": "owner@acme",
        "expires_at": (_BASE_TS + timedelta(days=30)).isoformat(),
        "created_at": _BASE_TS + timedelta(seconds=n),
    }


def _audit(tenant: str, n: int) -> dict:
    return {
        "log_id": f"log-{tenant}-{n}",
        "tenant_id": tenant,
        "engine": "qe-central",
        "action": "app.updated",
        "entity_type": "client_app",
        "entity_id": f"app-{n}",
        "user_id": "admin@acme",
        "created_at": _BASE_TS + timedelta(seconds=n),
    }


def _bundle_a() -> EvidenceBundle:
    verdicts = _make_chain("tenant-a", "art-1", "t1", [70, 85, 90])
    return EvidenceBundle.build(
        tenant_id="tenant-a",
        window=EvidenceWindow(hours=None),
        verdicts=verdicts,
        dossiers=[_dossier("tenant-a", "art-1", "t1", 2)],
        waivers=[_waiver("tenant-a", "art-1", 1)],
        audit=[_audit("tenant-a", 1), _audit("tenant-a", 2)],
    )


# ══════════════════════════════════════════════════════════════════════════
#  Chain re-derivation (tamper-evidence)
# ══════════════════════════════════════════════════════════════════════════
class TestChainVerification:
    def test_valid_chain_verifies(self):
        chain = verify_verdict_chains(_make_chain("t", "a", "x", [50, 60, 70]))
        assert chain["verified"] is True
        assert chain["chains_checked"] == 1
        assert chain["events_checked"] == 3
        assert chain["breaks"] == []

    def test_empty_set_verifies_vacuously(self):
        chain = verify_verdict_chains([])
        assert chain["verified"] is True
        assert chain["chains_checked"] == 0

    def test_tampered_row_is_detected(self):
        verdicts = _make_chain("t", "a", "x", [50, 60, 70])
        # Rewrite an outcome WITHOUT recomputing the stored chain hash.
        verdicts[1]["overall"] = 99
        chain = verify_verdict_chains(verdicts)
        assert chain["verified"] is False
        assert len(chain["breaks"]) == 1
        brk = chain["breaks"][0]
        assert brk["verdict_id"] == "a-x-1"
        assert brk["reason"] == "hash_mismatch"

    def test_missing_hash_is_a_break(self):
        verdicts = _make_chain("t", "a", "x", [50])
        verdicts[0]["chain_hash"] = ""
        chain = verify_verdict_chains(verdicts)
        assert chain["verified"] is False
        assert chain["breaks"][0]["reason"] == "missing_chain_hash"


# ══════════════════════════════════════════════════════════════════════════
#  Projection: shape, determinism, hash-verifiability
# ══════════════════════════════════════════════════════════════════════════
class TestProjection:
    def test_projects_framework_shape(self):
        report = SOC2Adapter().project(_bundle_a())
        assert report["framework"] == "soc2"
        assert report["tenant_id"] == "tenant-a"
        assert report["evidence_summary"]["verdicts"] == 3
        assert report["evidence_summary"]["chain"]["verified"] is True
        assert report["controls"], "expected a non-empty control set"
        for c in report["controls"]:
            assert {"control_id", "criterion", "title", "implementation",
                    "enforcement", "status", "evidence"} <= set(c.keys())

    def test_deterministic(self):
        bundle = _bundle_a()
        a = SOC2Adapter().project(bundle)
        b = SOC2Adapter().project(bundle)
        assert a == b
        assert a["report_digest"] == b["report_digest"]

    def test_hash_verifiable(self):
        report = SOC2Adapter().project(_bundle_a())
        assert report["report_digest"].startswith("sha256:")
        # A verifier recomputes the digest from the report body → identical.
        assert report_digest(report) == report["report_digest"]

    def test_digest_changes_when_evidence_changes(self):
        base = SOC2Adapter().project(_bundle_a())
        more = SOC2Adapter().project(
            EvidenceBundle.build(
                tenant_id="tenant-a",
                verdicts=_make_chain("tenant-a", "art-1", "t1", [70, 85, 90, 95]),
            )
        )
        assert base["report_digest"] != more["report_digest"]

    def test_all_registered_frameworks_project(self):
        bundle = _bundle_a()
        for framework in ("soc2", "naic_mar", "eu_ai_act"):
            report = build_report(framework, bundle)
            assert report["framework"] == framework
            assert report_digest(report) == report["report_digest"]
            assert report["control_totals"]  # totals present + sum to controls
            assert sum(report["control_totals"].values()) == len(report["controls"])


# ══════════════════════════════════════════════════════════════════════════
#  Non-mutation
# ══════════════════════════════════════════════════════════════════════════
class TestNoMutation:
    def test_project_does_not_mutate_input_rows(self):
        verdicts = _make_chain("tenant-a", "art-1", "t1", [70, 85, 90])
        snapshot = copy.deepcopy(verdicts)
        bundle = EvidenceBundle.build(tenant_id="tenant-a", verdicts=verdicts)
        SOC2Adapter().project(bundle)
        SOC2Adapter().project(bundle)
        assert verdicts == snapshot, "the caller's evidence rows were mutated"

    def test_project_does_not_mutate_bundle(self):
        bundle = _bundle_a()
        before = json.dumps(
            [bundle.verdicts, bundle.dossiers, bundle.waivers, bundle.audit],
            sort_keys=True, default=str,
        )
        NAICModelAuditAdapter().project(bundle)
        after = json.dumps(
            [bundle.verdicts, bundle.dossiers, bundle.waivers, bundle.audit],
            sort_keys=True, default=str,
        )
        assert before == after


# ══════════════════════════════════════════════════════════════════════════
#  Tenant scoping (A's report excludes B's evidence)
# ══════════════════════════════════════════════════════════════════════════
class TestTenantScoping:
    def test_bundle_build_drops_foreign_tenant_rows(self):
        mixed = _make_chain("tenant-a", "art-1", "t1", [80]) + \
            _make_chain("tenant-b", "art-9", "t9", [80])
        bundle = EvidenceBundle.build(tenant_id="tenant-a", verdicts=mixed)
        assert len(bundle.verdicts) == 1
        assert all(v["tenant_id"] == "tenant-a" for v in bundle.verdicts)

    def test_report_excludes_other_tenant_evidence(self):
        a_verdicts = _make_chain("tenant-a", "art-1", "t1", [80, 90])
        b_verdicts = _make_chain("tenant-b", "art-9", "t9", [10, 20])
        b_hashes = {v["chain_hash"] for v in b_verdicts}

        bundle = EvidenceBundle.build(
            tenant_id="tenant-a",
            verdicts=a_verdicts + b_verdicts,
            audit=[_audit("tenant-a", 1), _audit("tenant-b", 1)],
        )
        report = SOC2Adapter().project(bundle)

        # No tenant-B chain hash appears anywhere in tenant-A's report.
        blob = json.dumps(report, default=str)
        for h in b_hashes:
            assert h not in blob
        assert "log-tenant-b-1" not in blob
        assert report["evidence_summary"]["verdicts"] == 2


# ══════════════════════════════════════════════════════════════════════════
#  Honesty: operational never green-washed, code-enforced holds at zero rows
# ══════════════════════════════════════════════════════════════════════════
class TestHonestyClassification:
    def _control(self, report: dict, control_id: str) -> dict:
        return next(c for c in report["controls"] if c["control_id"] == control_id)

    def test_operational_control_never_satisfied_from_evidence(self):
        report = SOC2Adapter().project(_bundle_a())
        backup = self._control(report, "A1.2")  # backup/restore = operational
        assert backup["enforcement"] == "operational"
        assert backup["status"] == STATUS_OPERATIONAL

    def test_code_enforced_control_holds_with_zero_rows(self):
        empty = EvidenceBundle.build(tenant_id="tenant-a")
        report = SOC2Adapter().project(empty)
        rls = self._control(report, "CC6.1")  # pure code invariant
        assert rls["enforcement"] == "code_enforced"
        assert rls["status"] == STATUS_SATISFIED

    def test_runtime_control_not_evidenced_when_no_rows(self):
        empty = EvidenceBundle.build(tenant_id="tenant-a")
        report = SOC2Adapter().project(empty)
        audit = self._control(report, "PI1.5")   # audit_log-backed
        integrity = self._control(report, "PI1.2")  # verdict-chain-backed
        assert audit["status"] == STATUS_NOT_EVIDENCED
        assert integrity["status"] == STATUS_NOT_EVIDENCED

    def test_waivers_satisfied_even_at_zero_count(self):
        empty = EvidenceBundle.build(tenant_id="tenant-a")
        report = SOC2Adapter().project(empty)
        waivers = self._control(report, "PI1.4")
        assert waivers["status"] == STATUS_SATISFIED
        assert waivers["evidence"]["count"] == 0

    def test_tampered_chain_downgrades_integrity_control(self):
        verdicts = _make_chain("tenant-a", "art-1", "t1", [80, 90, 95])
        verdicts[1]["overall"] = 5  # rewrite an outcome, keep the stale hash
        bundle = EvidenceBundle.build(tenant_id="tenant-a", verdicts=verdicts)
        report = SOC2Adapter().project(bundle)
        assert report["evidence_summary"]["chain"]["verified"] is False
        integrity = self._control(report, "PI1.2")
        assert integrity["status"] == STATUS_PARTIAL

    def test_attestations_split_code_vs_operational(self):
        report = SOC2Adapter().project(_bundle_a())
        att = report["attestations"]
        assert att["code_enforced"], "expected code-enforced controls"
        assert att["operational"], "expected operational controls"
        # A1.2 (backups) + CC7.4 (incident response) are operational.
        assert "A1.2" in att["operational"]
        assert "CC7.4" in att["operational"]


# ══════════════════════════════════════════════════════════════════════════
#  Registry + unknown framework => 404
# ══════════════════════════════════════════════════════════════════════════
class TestRegistry:
    def test_get_adapter_known(self):
        assert isinstance(get_adapter("soc2"), SOC2Adapter)
        assert isinstance(get_adapter("SOC2"), SOC2Adapter)  # case-insensitive
        assert isinstance(get_adapter("naic_mar"), NAICModelAuditAdapter)
        assert isinstance(get_adapter("eu_ai_act"), EUAIActAnnex22Adapter)

    def test_unknown_framework_raises(self):
        with pytest.raises(UnknownFrameworkError):
            get_adapter("does-not-exist")

    def test_available_frameworks_catalogue(self):
        cat = available_frameworks()
        keys = {f["framework"] for f in cat}
        assert keys == {"soc2", "naic_mar", "eu_ai_act"}
        for f in cat:
            assert f["control_count"] > 0

    async def test_router_maps_unknown_framework_to_404(self):
        from fastapi import HTTPException

        from app.routers.compliance import compliance_report

        with pytest.raises(HTTPException) as exc_info:
            await compliance_report(
                framework="not-a-framework",
                window_hours=None,
                user={"tenant_id": "tenant-a", "sub": "admin", "role": "admin"},
            )
        assert exc_info.value.status_code == 404

    async def test_router_lists_frameworks(self):
        from app.routers.compliance import list_compliance_frameworks

        result = await list_compliance_frameworks(
            user={"tenant_id": "tenant-a", "sub": "admin", "role": "admin"},
        )
        assert result["total"] == 3
        assert {f["framework"] for f in result["frameworks"]} == \
            {"soc2", "naic_mar", "eu_ai_act"}
