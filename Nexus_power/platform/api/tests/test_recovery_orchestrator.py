"""Recovery Orchestrator regression — the reflex arc's routing invariants.

Founder-approved FULL-AUTO (2026-07-25) with certification as the safety gate.
These pin the honesty rules autonomy stands on:

  * an APPLICATION-attributed failure is NEVER auto-repaired — it becomes the
    defect dossier (repairing it would be the ultimate green-wash);
  * no attribution → NO action (never invent a fix for an unproven cause);
  * recompile-class product defects auto-RECERTIFY — but never chained off a
    certification run (loop guard: cert → recert ping-pong is impossible);
  * every routed action lands as a durable store proposal so the whole story
    is visible in the Studio recovery view.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_recovery_orchestrator.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_MOD = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory",
    "recovery_orchestrator.py")
_spec = importlib.util.spec_from_file_location("nexus_recovery_orch_ut", _MOD)
ro = importlib.util.module_from_spec(_spec)
sys.modules["nexus_recovery_orch_ut"] = ro
_spec.loader.exec_module(ro)


def _attr(category, cause):
    return {"category": category, "cause": cause,
            "detail": "d", "evidence": ["e1"]}


# ── route_failure: the pure rule ─────────────────────────────────────────────

def test_recompile_class_product_defect_auto_recertifies_on_client_runs():
    for cause in ("url_as_text_oracle", "best_effort_text_oracle",
                  "ambiguous_locator", "url_string_text_oracle"):
        a = _attr("product_script_defect", cause)
        assert ro.route_failure(a, is_certification=False) == ro.ACTION_RECERTIFY


def test_loop_guard_cert_runs_never_retrigger_certification():
    """A failing certification must not re-certify itself into a ping-pong —
    the deliberate retry is POST /certify."""
    a = _attr("product_script_defect", "url_as_text_oracle")
    assert ro.route_failure(a, is_certification=True) != ro.ACTION_RECERTIFY


def test_application_failures_are_never_repaired_only_dossiered():
    a = _attr("application_defect", "grounded_navigation_broken")
    for is_cert in (False, True):
        assert ro.route_failure(a, is_certification=is_cert) == \
            ro.ACTION_DEFECT_DOSSIER


def test_locator_timeouts_route_to_heal():
    a = _attr("unknown", "action_locator_timeout")
    assert ro.route_failure(a, is_certification=False) == ro.ACTION_HEAL_CANDIDATE


def test_env_and_config_produce_operator_notices():
    assert ro.route_failure(_attr("environment", "target_unreachable"),
                            is_certification=False) == ro.ACTION_ENV_NOTICE
    assert ro.route_failure(_attr("configuration", "auth_wall"),
                            is_certification=False) == ro.ACTION_ENV_NOTICE


def test_unattributed_failures_get_no_invented_action():
    assert ro.route_failure(None, is_certification=False) == ro.ACTION_NONE
    assert ro.route_failure({}, is_certification=False) == ro.ACTION_NONE
    assert ro.route_failure(_attr("unknown", "novel"),
                            is_certification=False) == ro.ACTION_NONE


# ── plan_recovery: aggregation invariants ────────────────────────────────────

def _steps():
    return [
        {"scenario_id": "s1", "step_number": 7,
         "attribution": _attr("product_script_defect", "url_as_text_oracle"),
         "error_excerpt": "getByText(/https/i)"},
        {"scenario_id": "s2", "step_number": 9,
         "attribution": _attr("unknown", "action_locator_timeout"),
         "error_excerpt": "locator.selectOption timeout"},
        {"scenario_id": "s3", "step_number": 2,
         "attribution": _attr("application_defect", "grounded_navigation_broken"),
         "error_excerpt": "toHaveURL failed"},
        {"scenario_id": "s4", "step_number": 1,
         "attribution": None, "error_excerpt": "??"},
    ]


def test_plan_routes_every_step_and_sets_recertify():
    plan = ro.plan_recovery(_steps(), is_certification=False)
    assert plan.recertify is True
    got = {a["scenario_id"]: a["action"] for a in plan.actions}
    assert got == {
        "s1": ro.ACTION_RECERTIFY,
        "s2": ro.ACTION_HEAL_CANDIDATE,
        "s3": ro.ACTION_DEFECT_DOSSIER,
        "s4": ro.ACTION_NONE,
    }


def test_plan_persists_a_proposal_for_every_actionable_non_recertify_step():
    plan = ro.plan_recovery(_steps(), is_certification=False)
    kinds = sorted(p["kind"] for p in plan.proposals)
    assert kinds == [ro.ACTION_DEFECT_DOSSIER, ro.ACTION_HEAL_CANDIDATE]
    for p in plan.proposals:
        assert p["auto"] is True
        assert p["suggested_strategy"]
        assert p["scenario_id"] and p["step_number"]


def test_certification_run_plan_never_recertifies():
    plan = ro.plan_recovery(_steps(), is_certification=True)
    assert plan.recertify is False
    # the recompile-class step still leaves a durable trail (heal candidate)
    s1 = next(a for a in plan.actions if a["scenario_id"] == "s1")
    assert s1["action"] == ro.ACTION_HEAL_CANDIDATE


def test_summary_counts_are_honest():
    s = ro.plan_recovery(_steps(), is_certification=False).summary()
    assert s["actions"][ro.ACTION_RECERTIFY] == 1
    assert s["actions"][ro.ACTION_NONE] == 1
    assert s["proposals"] == 2 and s["recertify"] is True


# ── V2 (FULL-AUTO auto-heal): run_recovery drives one heal per scenario ──────

class _FakeScope:
    """Async-context session factory yielding a fake session whose execute()
    returns the prepared failed-step rows."""
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        rows = self._rows

        class _Res:
            def all(self):
                return rows

        class _Sess:
            async def execute(self, *_a, **_k):
                return _Res()

        class _Ctx:
            async def __aenter__(self):
                return _Sess()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _row(sid, n, attribution, err=""):
    return (sid, n, {"failure_attribution": attribution} if attribution else {}, err)


def test_run_recovery_spawns_one_heal_per_failing_scenario():
    import asyncio as _aio

    healed: list[tuple] = []
    certs: list[bool] = []
    rows = [
        _row("s2", 9, _attr("unknown", "action_locator_timeout")),
        _row("s2", 10, _attr("unknown", "action_locator_timeout")),  # same scenario
        _row("s5", 3, _attr("product_script_defect", "url_as_text_oracle")),
        _row("s6", 1, _attr("application_defect", "grounded_navigation_broken")),
    ]

    # recovery_store.persist_scan is fail-open by design: against the fake
    # session it swallows its own error and returns 0 — exactly the posture
    # run_recovery relies on (a store hiccup never blocks the reflex arc).
    summary = _aio.run(ro.run_recovery(
        artifact_id="a", tenant_id="t", run_id="r1",
        is_certification=False,
        session_scope=_FakeScope(rows),
        spawn_certification=lambda: certs.append(True),
        spawn_auto_heal=lambda sid, step, cause: healed.append((sid, step, cause)),
    ))

    # exactly ONE heal per failing scenario (s2 deduped), NEVER for the
    # application-attributed scenario (s6) — repairing it would be green-wash.
    assert healed == [("s2", 9, "action_locator_timeout")]
    assert certs == [True]                      # recompile-class → recertify
    assert summary["actions"][ro.ACTION_HEAL_CANDIDATE] == 2
    assert summary["actions"][ro.ACTION_DEFECT_DOSSIER] == 1
