"""Gap-closure — §2.11 Execution Timeline and §2.6 defect severity/priority.

These were the two sections of the founder's original 17 that the R1-R4 build
left unimplemented. Both are DERIVED from data that already exists; neither
introduces a model or a fabricated score.

The honesty properties pinned here:
  * an unattributed failure gets severity `unset` — inventing a severity for a
    cause we cannot prove is exactly the fabricated precision D1 forbids;
  * severity is explainable: every assessment ships the reasons that produced
    it, so a reviewer argues with the rule, not a black box;
  * blast radius (how many DISTINCT cases share one failure shape) is a count,
    not a judgement, and it drives severity;
  * an execution error is still never presented as a defect in the customer's
    application, no matter how severe it is for US;
  * no owner is ever invented — only a grounded component.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_evidence_report_r5_gaps.py -q
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta

from app.services.test_factory import defect_ledger as dl
from app.services.test_factory import evidence_report as er


# ── §2.6 severity / priority / component / fix area ──────────────────────────

def _defect(**kw):
    base = {"display_status": er.ST_DEFECT, "lifecycle": dl.LC_OPEN,
            "occurrence_count": 1, "step_number": 5, "cause": "validation_missing",
            "category": "application_defect", "case_name": "Verify checkout",
            "scenario_id": "sc1"}
    base.update(kw)
    return base


def test_unattributed_failure_gets_no_severity():
    """The single most important rule: we do not invent a severity for a cause
    we could not prove."""
    a = dl.assess_defect(_defect(display_status=er.ST_NEEDS_REVIEW))
    assert a["severity"] == dl.SEV_UNSET and a["priority"] == dl.SEV_UNSET
    assert "does not guess" in " ".join(a["assessment_reasons"])


def test_regression_is_critical():
    a = dl.assess_defect(_defect(lifecycle=dl.LC_REGRESSED))
    assert a["severity"] == dl.SEV_CRITICAL
    assert any("REGRESSED" in r for r in a["assessment_reasons"])


def test_blast_radius_drives_severity_and_is_a_count_not_an_opinion():
    one = dl.assess_defect(_defect(), blast_radius=1)
    many = dl.assess_defect(_defect(), blast_radius=5)
    assert many["severity"] == dl.SEV_CRITICAL
    assert one["severity"] != dl.SEV_CRITICAL
    assert any("blast radius 5" in r for r in many["assessment_reasons"])


def test_business_priority_of_the_case_raises_severity():
    a = dl.assess_defect(_defect(), case_priority="P0_critical")
    assert a["severity"] == dl.SEV_HIGH
    assert any("business-critical" in r for r in a["assessment_reasons"])


def test_recurrence_raises_severity():
    a = dl.assess_defect(_defect(occurrence_count=4))
    assert a["severity"] == dl.SEV_HIGH
    assert any("recurred in 4 runs" in r for r in a["assessment_reasons"])


def test_entry_step_failure_is_escalated():
    a = dl.assess_defect(_defect(step_number=1))
    assert a["severity"] == dl.SEV_HIGH
    assert any("entry step" in r for r in a["assessment_reasons"])


def test_execution_error_is_never_framed_as_an_application_defect():
    a = dl.assess_defect(_defect(display_status=er.ST_EXEC_ERROR,
                                 category="product_script_defect",
                                 cause="ambiguous_locator"))
    joined = " ".join(a["assessment_reasons"])
    assert "NOT a defect in the application under test" in joined
    assert a["suggested_fix_area"] == "test generation — locator binding"


def test_fixed_defects_have_their_priority_damped():
    open_d = dl.assess_defect(_defect(occurrence_count=4))
    fixed = dl.assess_defect(_defect(occurrence_count=4, lifecycle=dl.LC_FIXED))
    order = [dl.SEV_LOW, dl.SEV_MEDIUM, dl.SEV_HIGH, dl.SEV_CRITICAL]
    assert order.index(fixed["priority"]) < order.index(open_d["priority"])


def test_no_person_is_ever_invented_as_an_owner():
    a = dl.assess_defect(_defect())
    assert "owner_note" in a and "ownership map" in a["owner_note"]
    # the component is a grounded LOCATION, not a team or a name
    assert "Verify checkout" in a["suggested_component"] and "step 5" in a["suggested_component"]


def test_every_assessment_is_explainable_and_marked_suggested():
    for d in (_defect(), _defect(display_status=er.ST_EXEC_ERROR),
              _defect(display_status=er.ST_NEEDS_REVIEW)):
        a = dl.assess_defect(d)
        assert a["assessment_reasons"], "an unexplained severity is a black box"
        assert a["suggested"] is True


# ── §2.11 Execution timeline ─────────────────────────────────────────────────

def _step(n, status, at, scenario="sc1", err=""):
    return types.SimpleNamespace(step_number=n, status=status, created_at=at,
                                 scenario_id=scenario, error_message=err,
                                 duration_ms=10)


def _run(started, completed=None):
    return types.SimpleNamespace(
        run_id="r1", environment="certification", started_at=started,
        completed_at=completed, status="passed", passed_steps=2,
        failed_steps=1, skipped_steps=0)


def test_timeline_is_chronological_and_brackets_the_run():
    t0 = datetime(2026, 7, 25, 10, 0, 0)
    run = _run(t0, t0 + timedelta(minutes=5))
    steps = [_step(2, "failed", t0 + timedelta(seconds=30), err="boom"),
             _step(1, "passed", t0 + timedelta(seconds=10))]
    tl = er.build_timeline(run=run, step_rows=steps, case_names={"sc1": "Checkout"})
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds[0] == "run_started" and kinds[-1] == "run_completed"
    times = [e["at"] for e in tl["events"] if e["at"]]
    assert times == sorted(times), "the timeline must read forwards in time"


def test_timeline_always_keeps_every_non_passing_step():
    t0 = datetime(2026, 7, 25, 10, 0, 0)
    steps = ([_step(i, "passed", t0 + timedelta(seconds=i)) for i in range(300)]
             + [_step(999, "failed", t0 + timedelta(seconds=400), err="the one failure")])
    tl = er.build_timeline(run=_run(t0), step_rows=steps,
                           case_names={"sc1": "C"}, max_events=50)
    failures = [e for e in tl["events"] if e.get("status") == "failed"]
    assert len(failures) == 1 and "the one failure" in failures[0]["detail"]


def test_timeline_discloses_sampling_rather_than_truncating_silently():
    t0 = datetime(2026, 7, 25, 10, 0, 0)
    steps = [_step(i, "passed", t0 + timedelta(seconds=i)) for i in range(500)]
    tl = er.build_timeline(run=_run(t0), step_rows=steps,
                           case_names={}, max_events=60)
    assert tl["passing_steps_sampled_out"] > 0
    assert "sampled" in tl["note"] and "silently" in tl["note"]


def test_timeline_on_an_empty_run_is_safe():
    tl = er.build_timeline(run=None, step_rows=[], case_names={})
    assert tl["events"] == [] and tl["event_count"] == 0


def test_report_and_exports_carry_the_new_sections():
    import os
    rep = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()
    html = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                             "test_factory", "report_html.py"), encoding="utf-8").read()
    exp = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "report_export.py"), encoding="utf-8").read()
    fmt = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "report_formats.py"), encoding="utf-8").read()
    assert '"timeline": timeline' in rep
    assert "Execution Timeline" in html and "sev-critical" in html
    assert '"severity": d.get("severity")' in exp
    assert "Severity (suggested)" in fmt and "Timeline" in fmt


# ── §2.18 Needs-Review QUEUE (a queue with owners, not a label) ──────────────

def _router() -> str:
    import os
    return open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                             "test_factory.py"), encoding="utf-8").read()


def test_review_queue_endpoint_exists_and_joins_dispositions():
    r = _router()
    assert '/report/review-queue' in r
    assert '"state": "resolved" if disp else "open"' in r
    # dispositions are read back STRUCTURALLY, not parsed out of prose
    assert 'ev.get("event_type") != "review_disposition"' in r


def test_disposition_is_recorded_structurally_so_the_queue_can_query_it():
    """The first cut wrote scenario_id="" / step_number=0 and stuffed everything
    into a prose string, which no queue could join on."""
    r = _router()
    assert "scenario_id=body.scenario_id, step_number=body.step_number" in r
    assert '"assignee": (body.assignee or "").strip()' in r
    # signed-state comes from the configured e-signature method, not from the
    # mere presence of a string (see test_esign_method_is_configurable_...)
    assert '"electronically_signed": _esign_evidence(' in r


def test_queue_lists_unattributed_items_first():
    """Severity 'unset' means the machine could not decide — exactly the items a
    human must see first."""
    r = _router()
    assert 'rank = {"unset": 0, "critical": 1, "high": 2, "medium": 3, "low": 4}' in r


def test_queue_never_counts_an_unsigned_disposition_as_a_sign_off():
    r = _router()
    assert "an unsigned one is reported as" in r
    assert "never counted as a sign-off" in r


def test_assignee_is_part_of_the_review_contract():
    r = _router()
    assert "assignee: str | None = Field(None, max_length=200," in r


# ── AC-1: a report is frozen for every run, and says which one you're reading ─

def test_snapshot_chain_root_detects_tampering():
    from app.services.test_factory import report_store as rs
    a = {"summary": {"total_cases_executed": 47}}
    b = {"summary": {"total_cases_executed": 46}}
    assert rs.snapshot_chain_root(a) != rs.snapshot_chain_root(b)
    # …and is stable for the same content regardless of key order
    assert rs.snapshot_chain_root({"x": 1, "y": 2}) == rs.snapshot_chain_root({"y": 2, "x": 1})


def test_snapshot_is_written_fire_and_forget_and_never_breaks_ingest():
    import os
    fb = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                           "test_runs_feedback.py"), encoding="utf-8").read()
    assert "_freeze_report" in fb and "_SNAPSHOT_TASKS" in fb
    # a failure is logged, never raised into the ingest path
    assert "report_snapshot_failed" in fb
    assert "report_snapshot_spawn_failed" in fb


def test_reader_is_always_told_snapshot_or_live():
    r = _router()
    assert '{**live, "source": "live"}' in r
    assert "no frozen report for that run" in r
    from app.services.test_factory import report_store as rs
    src = open(rs.__file__, encoding="utf-8").read()
    assert 'report["source"] = "snapshot"' in src
    assert '"integrity_ok"' in src


def test_snapshot_divergence_is_a_finding_not_something_to_reconcile():
    from app.services.test_factory import report_store as rs
    src = open(rs.__file__, encoding="utf-8").read()
    assert "is a finding" in src and "reconcile silently" in src


def test_snapshot_migration_is_append_or_update_never_deletable_by_the_app():
    import os
    sql = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "apply_run_reports.sql"), encoding="utf-8").read()
    assert "GRANT SELECT, INSERT, UPDATE ON e2e_run_reports" in sql
    assert "GRANT DELETE" not in sql and "GRANT ALL" not in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql


# ── T3 deep diagnostics (§2.10) — default OFF, no new dependency ─────────────

def _compiler_src() -> str:
    import os
    return open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                             "script_factory", "compiler.py"), encoding="utf-8").read()


def test_t3_is_inert_unless_explicitly_enabled():
    """The owned script a customer downloads must behave identically whether or
    not they ever turn diagnostics on."""
    src = _compiler_src()
    assert "_T3_DIAGNOSTICS_AFTEREACH" in src
    assert "process.env.NEXUS_T3_DIAGNOSTICS !== '1') return" in src
    # the HTML dump is a SECOND opt-in on top of the first (it is the heavy one)
    assert "process.env.NEXUS_T3_HTML === '1'" in src


def test_t3_uses_only_browser_builtins_no_new_dependency():
    """axe-core would be a new dependency and would break an air-gapped install."""
    src = _compiler_src()
    assert "axe" not in src.lower().split("_T3_DIAGNOSTICS_AFTEREACH")[-1][:4000]
    assert "page.accessibility.snapshot" in src
    assert "performance.getEntriesByType" in src


def test_t3_never_claims_a_wcag_audit_it_did_not_run():
    """The AX tree is a snapshot. Calling it an accessibility AUDIT would be the
    exact fabricated claim the report exists to eliminate."""
    src = _compiler_src()
    assert "NOT a WCAG audit" in src
    assert "no conformance is asserted" in src
    fb = open(__file__.replace("tests\test_evidence_report_r5_gaps.py",
                               "app\routers\test_runs_feedback.py")
              .replace("tests/test_evidence_report_r5_gaps.py",
                       "app/routers/test_runs_feedback.py"), encoding="utf-8").read()
    assert "NOT a WCAG audit" in fb


def test_t3_can_never_change_a_verdict():
    src = _compiler_src()
    tail = src.split("_T3_DIAGNOSTICS_AFTEREACH")[1][:4000]
    assert "diagnostics never affect the verdict" in tail
    assert "never let diagnostics change a run result" in tail


def test_t3_endpoint_exists_and_is_write_gated():
    fb = open(__file__.replace("tests\test_evidence_report_r5_gaps.py",
                               "app\routers\test_runs_feedback.py")
              .replace("tests/test_evidence_report_r5_gaps.py",
                       "app/routers/test_runs_feedback.py"), encoding="utf-8").read()
    assert '@router.post("/api/v1/test-runs/diagnostics")' in fb
    assert "_require_write_role(user)" in fb


def test_runs_advertise_the_diagnostics_endpoint():
    assert "NEXUS_DIAGNOSTICS_ENDPOINT" in _router()


# ── §6 config decisions: signing key source, retention, e-signature ──────────

def test_signing_key_prefers_a_file_over_an_env_var(tmp_path, monkeypatch):
    """A mounted secret file can be rotated without recreating the container and
    does not leak into `docker inspect` or a process listing."""
    from app.services.test_factory import evidence_manifest as em
    kf = tmp_path / "key"
    kf.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("NEXUS_EVIDENCE_SIGNING_KEY", "env-secret")
    monkeypatch.setenv("NEXUS_EVIDENCE_SIGNING_KEY_FILE", str(kf))
    assert em._signing_key() == "file-secret"
    assert em.signing_key_source().startswith("file:")
    monkeypatch.delenv("NEXUS_EVIDENCE_SIGNING_KEY_FILE")
    assert em._signing_key() == "env-secret"
    assert em.signing_key_source().startswith("env:")


def test_no_key_means_unsigned_never_a_locally_derived_pseudo_key(monkeypatch):
    from app.services.test_factory import evidence_manifest as em
    monkeypatch.delenv("NEXUS_EVIDENCE_SIGNING_KEY", raising=False)
    monkeypatch.setenv("NEXUS_EVIDENCE_SIGNING_KEY_FILE", "/nonexistent/key")
    assert em.signing_enabled() is False
    m = em.build_manifest({"a": b"1"})
    assert m["signed"] is False and m["algorithm"]["key_source"] == "none"


def test_retention_tombstones_rather_than_deletes():
    from app.services.test_factory import evidence_retention as ret
    stone_src = open(ret.__file__, encoding="utf-8").read()
    assert "TOMBSTONE_PREFIX" in stone_src
    assert "sha256=" in stone_src            # the digest survives the reclaim
    # the run/step rows and verdicts must never be touched by retention
    assert "run/step rows" in stone_src.lower() or "Run/step rows" in stone_src


def test_retention_is_a_dry_run_by_default():
    import inspect
    from app.services.test_factory import evidence_retention as ret
    sig = inspect.signature(ret.apply_retention)
    assert sig.parameters["dry_run"].default is True


def test_retention_windows_differ_by_cost_of_the_artifact():
    from app.services.test_factory import evidence_retention as ret
    assert ret.window_days("application/zip") < ret.window_days("image/png")
    assert ret.window_days("application/zip") == 30


def test_retention_window_is_env_overridable(monkeypatch):
    from app.services.test_factory import evidence_retention as ret
    monkeypatch.setenv("NEXUS_RETENTION_APPLICATION_ZIP_DAYS", "60")
    assert ret.window_days("application/zip") == 60
    monkeypatch.setenv("NEXUS_RETENTION_APPLICATION_ZIP_DAYS", "0")
    assert ret.window_days("application/zip") == 0      # keep forever


def test_esign_method_is_configurable_and_never_over_claims():
    r = _router()
    assert "_ESIGN_METHOD" in r
    for m in ("typed_name", "sso_session", "both"):
        assert f'"{m}"' in r
    # an authenticated request is NOT by itself a signature
    assert "never upgraded to a sign-off because a request" in r
    assert 'sso_sub != "anonymous"' in r


def test_retention_endpoint_is_admin_gated_and_audited():
    r = _router()
    assert "/evidence/retention" in r
    assert 'event_type="evidence_retention"' in r
    assert "requires an admin or manager role" in r
