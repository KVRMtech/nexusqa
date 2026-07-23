"""R7 — ingest can never mint unverified green (requirements-audit P0 finding).

Two holes closed:
  1. a CI step with an ABSENT status ingested as 'passed';
  2. a declared run-level 'passed' could override failing step evidence.
"""
from app.services.test_runs import ingested_step_status


def test_absent_status_is_broken_never_passed():
    assert ingested_step_status({}) == "broken"
    assert ingested_step_status({"status": None}) == "broken"
    assert ingested_step_status({"status": ""}) == "broken"
    assert ingested_step_status({"status": "   "}) == "broken"


def test_unknown_status_is_broken():
    assert ingested_step_status({"status": "greenish"}) == "broken"
    assert ingested_step_status({"status": "ok"}) == "broken"


def test_explicit_statuses_pass_through():
    for s in ("passed", "failed", "skipped", "timed_out", "broken"):
        assert ingested_step_status({"status": s}) == s
    assert ingested_step_status({"status": "PASSED"}) == "passed"  # case-normalised


def test_declared_passed_cannot_override_failing_steps():
    """Source-level guard: the run-status derivation refuses a declared 'passed'
    when step evidence contains failures (evidence wins, declaration loses)."""
    import inspect
    from app.services import test_runs as tr
    src = inspect.getsource(tr)
    assert "declared_status == E2E_RUN_STATUS_PASSED and failed > 0" in src, \
        "the declared-passed override guard must stay in place"
