"""Phase R3 — audit-grade export: hash chain, signed manifest, offline verifier,
export governance (RBAC / redaction / watermark / audit event), e-signature.

Spec: EXECUTION_EVIDENCE_REPORT_SPEC.md §2.16, §2.17, §2.18.

Exit proof for this phase: **tamper one byte in an exported package and the
verifier fails loudly** — naming the file. Without that, "immutable audit
trail" is an adjective rather than a mechanism.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_evidence_report_r3.py -q
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

from app.services.test_factory import evidence_manifest as em
from app.services.test_factory import report_export as rx

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()


def _report() -> dict:
    step = {"step_number": 1, "status": "passed", "status_badge": "",
            "action": "Fill 'Password' with 'hunter2'", "target": "getByLabel('Password')",
            "expected": "Signed in", "actual": "as expected", "duration_ms": 5,
            "evidence_class": "PROVEN", "oracle_provenance": {},
            "evidence": {"screenshot_url": "", "trace_url": ""}, "analysis": None}
    bad = {"step_number": 2, "status": "defect_found", "status_badge": "application",
           "action": "Submit as user@example.com", "target": "getByRole('button')",
           "expected": "Validation shown", "actual": "accepted 4111111111111111",
           "duration_ms": 9, "evidence_class": "UNVERIFIED", "oracle_provenance": {},
           "evidence": {}, "analysis": {"category": "application_defect",
                                        "cause": "validation_missing", "detail": "d",
                                        "evidence_quoted": ["saw user@example.com"],
                                        "suggested": True}}
    case = {"test_case_id": "tc1", "name": "Verify user can complete the 'apply' flow",
            "description": "", "test_type": "functional", "priority": "P0",
            "status": "completed_with_defects", "executed": True, "steps_declared": 2,
            "steps_executed": 2, "counts": {"passed": 1, "defect_found": 1, "total": 2},
            "duration_ms": 14, "tags": [], "reproducibility": {}, "steps": [step, bad]}
    return {"report_version": "1.0", "generated_at": "2026-07-25T00:00:00+00:00",
            "run": {"run_id": "r1", "environment": "certification",
                    "ingested_totals": {"total_steps": 2}},
            "trust": {"certified": True, "certification_run": {"run_id": "c1"},
                      "quarantined_count": 0, "uncertified_exploratory_count": 0,
                      "statement": "s", "quarantined": [], "uncertified_exploratory": [],
                      "suite_size": 1, "oracle_scorecard": None},
            "summary": {"artifact_id": "a1", "total_flows": 1,
                        "total_cases_generated": 1, "total_cases_executed": 1,
                        "total_steps_executed": 2,
                        "case_counts": {"passed": 0}, "step_counts": {"passed": 1}},
            "flows": [{"flow_key": "apply", "flow_label": "apply", "cases": [case],
                       "case_count": 1, "duration_ms": 14, "pass_percentage": 0.0,
                       "defect_count": 1, "counts": {"passed": 0}}],
            "defects": {"unique_defects": 1, "total_occurrences": 2,
                        "by_lifecycle": {"open": 1}, "window_runs": 2,
                        "defects": [{"signature": "abc", "scenario_id": "tc1",
                                     "step_number": 2, "display_status": "defect_found",
                                     "cause": "validation_missing", "lifecycle": "open",
                                     "occurrence_count": 2, "fingerprint": "saw x",
                                     "occurrences": [{"error_excerpt": "user@example.com"}]}],
                        "note": "n"},
            "diff": {"available": False, "reason": "no earlier run"},
            "coverage": {"note": "n", "cases_not_executed": [],
                         "cases_not_executed_count": 0, "quarantined_count": 0,
                         "uncertified_exploratory_count": 0},
            "doctrine": {}}


# ── manifest + chain ─────────────────────────────────────────────────────────

def test_chain_root_changes_when_any_byte_changes():
    a = em.build_manifest({"x.txt": b"hello", "y.txt": b"world"})
    b = em.build_manifest({"x.txt": b"hellp", "y.txt": b"world"})
    assert a["chain_root"] != b["chain_root"]


def test_chain_root_is_independent_of_packing_order():
    a = em.build_manifest({"a": b"1", "b": b"2"})
    b = em.build_manifest({"b": b"2", "a": b"1"})
    assert a["chain_root"] == b["chain_root"]


def test_unsigned_manifest_says_so_rather_than_faking_a_signature():
    os.environ.pop("NEXUS_EVIDENCE_SIGNING_KEY", None)
    m = em.build_manifest({"a": b"1"})
    assert m["signed"] is False and m["signature"] == ""
    assert m["algorithm"]["signature"] == "none"
    assert "tamper-EVIDENT" in m["honesty_note"]


def test_signature_round_trip_when_a_key_is_configured():
    os.environ["NEXUS_EVIDENCE_SIGNING_KEY"] = "unit-test-key"
    try:
        m = em.build_manifest({"a": b"1"})
        assert m["signed"] is True and m["signature"]
        assert em.verify_root_signature(m["chain_root"], m["signature"]) is True
        assert em.verify_root_signature(m["chain_root"], "deadbeef") is False
    finally:
        os.environ.pop("NEXUS_EVIDENCE_SIGNING_KEY", None)


def test_verify_manifest_detects_modification_and_names_the_file():
    files = {"report.html": b"<html>ok</html>", "verdict.json": b"{}"}
    m = em.build_manifest(files)
    tampered = dict(files)
    tampered["report.html"] = b"<html>0k</html>"     # one byte
    res = em.verify_manifest(tampered, m)
    assert res["ok"] is False
    assert res["mismatched"] and res["mismatched"][0]["path"] == "report.html"
    assert res["chain_root_ok"] is False


def test_verify_manifest_detects_a_removed_file():
    files = {"a.txt": b"1", "b.txt": b"2"}
    m = em.build_manifest(files)
    res = em.verify_manifest({"a.txt": b"1"}, m)
    assert res["ok"] is False and "b.txt" in res["missing"]


def test_verify_manifest_passes_on_an_untouched_package():
    files = {"a.txt": b"1", "b.txt": b"2"}
    m = em.build_manifest(files)
    res = em.verify_manifest(files, m)
    assert res["ok"] is True and res["chain_root_ok"] is True


# ── THE R3 EXIT PROOF: offline verifier on a real ZIP ────────────────────────

def _extract(blob: bytes, dest: str) -> None:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(dest)


def _run_verifier(dirpath: str):
    return subprocess.run([sys.executable, os.path.join(dirpath, "verify_evidence.py"),
                           dirpath], capture_output=True, text=True)


def test_exported_package_verifies_then_fails_loudly_when_tampered():
    """THE exit proof: verify → tamper ONE byte → verify fails and names it."""
    blob, manifest = rx.build_zip(report=_report(), html="<html><body>hi</body></html>",
                                  exported_by="qa@example.com", artifact_id="a1",
                                  run_id="r1")
    with tempfile.TemporaryDirectory() as d:
        _extract(blob, d)
        assert os.path.exists(os.path.join(d, "verify_evidence.py"))
        assert os.path.exists(os.path.join(d, "manifest.json"))

        ok = _run_verifier(d)
        assert ok.returncode == 0, f"pristine package must verify: {ok.stdout}{ok.stderr}"
        assert "VERIFIED" in ok.stdout

        # tamper exactly one byte of the human-readable report
        target = os.path.join(d, "report.html")
        raw = open(target, "rb").read()
        open(target, "wb").write(raw[:-1] + bytes([raw[-1] ^ 0x01]))

        bad = _run_verifier(d)
        assert bad.returncode != 0, "a tampered package MUST NOT verify"
        assert "FAILED" in bad.stdout
        assert "report.html" in bad.stdout
        assert "CHAIN ROOT MISMATCH" in bad.stdout


def test_verifier_is_standard_library_only():
    """An auditor must be able to run it on an air-gapped machine, years on."""
    for banned in ("import requests", "pip install", "from cryptography",
                   "urllib.request", "http"):
        assert banned not in em.VERIFIER_SCRIPT


def test_verifier_fails_when_the_manifest_itself_is_missing():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "verify_evidence.py"), "w", encoding="utf-8").write(
            em.VERIFIER_SCRIPT)
        res = _run_verifier(d)
        assert res.returncode != 0 and "manifest.json not found" in res.stdout


# ── §2.16 export governance ──────────────────────────────────────────────────

def test_zip_contains_the_expected_members():
    blob, _ = rx.build_zip(report=_report(), html="<html><body>x</body></html>",
                           exported_by="q@e.com", artifact_id="a1", run_id="r1")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
    assert {"report.html", "report.json", "verdict.json", "README.txt",
            "manifest.json", "verify_evidence.py"} <= names


def test_export_redacts_credentials_and_sensitive_values():
    rep, stats = rx.redact_report(_report())
    blob = json.dumps(rep)
    assert "hunter2" not in blob, "a credential value must never leave in an export"
    assert "user@example.com" not in blob
    assert "4111111111111111" not in blob
    assert stats["credential_fields_masked"] >= 1


def test_redaction_marks_in_place_never_silently_empties():
    rep, _ = rx.redact_report(_report())
    step = rep["flows"][0]["cases"][0]["steps"][0]
    assert step["action"], "a redacted field must not become empty"
    assert "REDACTED" in step["action"]


def test_watermark_names_the_exporter_and_time():
    marker = "REPORT_BODY_CONTENT"
    out = rx.watermark_html(f"<html><body>{marker}</body></html>",
                            exported_by="qa@example.com",
                            at="2026-07-25T00:00:00Z", artifact_id="a1", run_id="r1",
                            chain_note="integrity: manifest")
    assert "qa@example.com" in out and "2026-07-25T00:00:00Z" in out
    # the provenance banner must precede the report content, so a screenshot of
    # the top of the document already carries who exported it
    assert out.index("qa@example.com") < out.index(marker)
    assert out.index("<body>") < out.index("qa@example.com")


def test_export_route_is_rbac_gated_and_audited():
    assert '@router.get("/api/v1/test-factory/{artifact_id}/report.zip")' in _ROUTER
    assert "_export_role_ok(user)" in _ROUTER
    assert 'event_type="evidence_exported"' in _ROUTER


# ── verdict JSON (machine-readable truth) ────────────────────────────────────

def test_verdict_json_carries_what_junit_cannot():
    v = rx.build_verdict_json(_report())
    assert v["schema"] == rx.VERDICT_SCHEMA_VERSION
    assert v["trust"]["certified"] is True
    assert v["defects"][0]["signature"] == "abc"
    f = v["cases"][0]["failures"][0]
    assert f["category"] == "application_defect" and f["evidence_class"] == "UNVERIFIED"
    assert f["suggested"] is True                      # D3 survives into the machine feed


def test_verdict_gate_hint_separates_our_faults_from_app_defects():
    v = rx.build_verdict_json(_report())
    assert "execution_error" in v["gate_hint"]["note"]


# ── §2.18 review workflow + e-signature ──────────────────────────────────────

def test_review_endpoint_exists_with_dispositions_and_signature():
    assert '@router.post("/api/v1/test-factory/{artifact_id}/report/review")' in _ROUTER
    for d in ("confirm_defect", "reclassify", "retest", "dismiss"):
        assert d in _ROUTER
    assert "signature_name" in _ROUTER
    assert 'event_type="review_disposition"' in _ROUTER


def test_audit_trail_endpoint_verifies_the_chain_live():
    assert '/report/audit-trail' in _ROUTER
    assert "verify_chain" in _ROUTER


# ── regressions found while verifying R3 against the LIVE system ─────────────

def test_part11_ledger_migration_ships_and_is_append_only():
    """The heal_events table did not exist in the deployed database, so every
    audit write degraded to a WARNING and 'immutable audit trail' had nothing
    behind it. The migration must ship AND enforce append-only at the grant."""
    sql = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "apply_heal_events.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS heal_events" in sql
    assert "GRANT SELECT, INSERT ON heal_events" in sql
    for forbidden in ("GRANT UPDATE", "GRANT DELETE", "GRANT ALL"):
        assert forbidden not in sql, "the ledger must never be updatable by the app role"
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "prev_hash" in sql and "row_hash" in sql


def test_audit_endpoint_isolates_sessions_so_one_failure_cannot_poison_the_other():
    """A failing statement aborts its whole Postgres transaction; sharing one
    session made a missing-table error cascade into a bogus 'chain verification
    unavailable', i.e. a second WRONG answer derived from the first failure."""
    i = _ROUTER.index("async def report_audit_trail")
    body = _ROUTER[i:i + 2200]
    assert body.count("async with tenant_scoped_session(tenant_id) as session:") >= 3, \
        "list, verify and the artifact check must each own their session"
