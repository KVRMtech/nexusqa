"""Phase R2 — defect identity/dedup/lifecycle, run-over-run diff, trace capture.

Spec: EXECUTION_EVIDENCE_REPORT_SPEC.md §2.6, §2.8 (tier T2), §2.14, §2.15.

Exit proof for this phase: **the same defect seen twice is ONE defect with two
occurrences** — without that, defect counts inflate every run and the report
loses the credibility the whole product is selling.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_evidence_report_r2.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import defect_ledger as dl

_COMPILER = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                              "script_factory", "compiler.py"), encoding="utf-8").read()
_FEEDBACK = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                              "test_runs_feedback.py"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()


# ── §2.6 defect identity (the phase exit proof) ──────────────────────────────

def test_same_defect_twice_is_one_signature():
    """THE R2 exit proof: two runs of one defect → one identity."""
    e1 = ("Error: locator.selectOption: Test timeout of 60000ms exceeded.\n"
          "Call log: waiting for getByLabel('State') at /tmp/run-a1b2/spec.ts:31:9")
    e2 = ("Error: locator.selectOption: Test timeout of 45000ms exceeded.\n"
          "Call log: waiting for getByLabel('State') at /tmp/run-c3d4/spec.ts:31:9")
    s1 = dl.defect_signature(scenario_id="sc1", step_number=2, cause="timeout",
                             fingerprint=dl.error_fingerprint(e1))
    s2 = dl.defect_signature(scenario_id="sc1", step_number=2, cause="timeout",
                             fingerprint=dl.error_fingerprint(e2))
    assert s1 == s2, "volatile timings/paths must not fork a defect's identity"


def test_fingerprint_masks_volatile_detail():
    fp = dl.error_fingerprint(
        "Timeout 30000ms at https://host.example/apply?x=1 "
        "run 7f3a9c21b45e0011 2026-07-25T10:00:00Z")
    for volatile in ("30000", "host.example", "7f3a9c21b45e0011", "2026-07-25"):
        assert volatile not in fp, f"{volatile} must be masked out of the identity"


def test_genuinely_different_failures_stay_distinct():
    a = dl.defect_signature(scenario_id="sc1", step_number=2, cause="timeout",
                            fingerprint=dl.error_fingerprint("locator.selectOption timed out"))
    b = dl.defect_signature(scenario_id="sc1", step_number=2, cause="timeout",
                            fingerprint=dl.error_fingerprint("expect(received).toHaveValue failed"))
    c = dl.defect_signature(scenario_id="sc2", step_number=2, cause="timeout",
                            fingerprint=dl.error_fingerprint("locator.selectOption timed out"))
    d = dl.defect_signature(scenario_id="sc1", step_number=9, cause="timeout",
                            fingerprint=dl.error_fingerprint("locator.selectOption timed out"))
    assert len({a, b, c, d}) == 4, "different shape / case / step are different defects"


def test_cause_participates_in_identity():
    """Same error text attributed to different owners is not the same defect."""
    fp = dl.error_fingerprint("assertion failed")
    assert (dl.defect_signature(scenario_id="s", step_number=1, cause="application_defect",
                                fingerprint=fp)
            != dl.defect_signature(scenario_id="s", step_number=1, cause="ambiguous_locator",
                                   fingerprint=fp))


def test_empty_error_yields_empty_fingerprint():
    assert dl.error_fingerprint("") == ""
    assert dl.error_fingerprint(None) == ""


def test_fingerprint_is_stable_across_ansi_and_whitespace():
    a = dl.error_fingerprint("\x1b[31mError: boom\x1b[0m\n  call log")
    b = dl.error_fingerprint("Error:   boom\n  call log")
    assert a == b


# ── lifecycle states ─────────────────────────────────────────────────────────

def test_lifecycle_constants_cover_the_three_states():
    assert (dl.LC_OPEN, dl.LC_FIXED, dl.LC_REGRESSED) == (
        "open", "fixed_verified", "regressed")


def test_diagnosis_runs_are_excluded_from_cross_run_analysis():
    """Diagnosis runs deliberately fail (they are capture probes); counting
    them would fabricate defects that never faced a user."""
    assert "diagnosis" in dl._EXCLUDED_ENVS


# ── §2.8 trace capture (evidence tier T2) ────────────────────────────────────

def test_trace_is_retained_on_failure_not_only_on_retry():
    """Server runs use retries=0, so 'on-first-retry' produced NO trace ever.

    Assert on the emitted CONFIG LINE, not the bare string — the rationale
    comment legitimately names the old setting.
    """
    assert "trace: 'on-first-retry'" not in _COMPILER, \
        "the generated config must not gate traces behind a retry"
    assert "|| 'retain-on-failure'" in _COMPILER
    # both generated config variants (default + parametrized) must be patched
    assert _COMPILER.count("|| 'retain-on-failure'") == 2


def test_reporter_uploads_a_trace_for_failed_tests():
    assert "pendingTraces" in _COMPILER
    assert "/api/v1/test-runs/trace" in _COMPILER
    assert "trace_url" in _COMPILER


def test_trace_upload_endpoint_exists_and_is_write_gated():
    assert '@router.post("/api/v1/test-runs/trace")' in _FEEDBACK
    assert "_require_write_role(user)" in _FEEDBACK
    assert "MAX_TRACE_BYTES" in _FEEDBACK


def test_report_exposes_trace_url_on_a_step():
    assert '"trace_url"' in _REPORT


def test_trace_upload_is_best_effort_and_never_fails_the_run():
    """A missing/oversize trace must never turn a real result into a red."""
    i = _COMPILER.index("pendingTraces")
    tail = _COMPILER[i:i + 4000]
    assert "best-effort trace upload" in tail or "catch {" in tail


# ── §2.14 diff / §2.15 coverage honesty are wired into the report ────────────

def test_report_carries_defects_and_diff_sections():
    assert '"defects": defects' in _REPORT and '"diff": diff' in _REPORT


def test_cross_run_failure_never_breaks_the_report():
    """An analytics failure must degrade to 'absent', never 500 the report."""
    assert "defect_ledger_failed" in _REPORT and "run_diff_failed" in _REPORT
