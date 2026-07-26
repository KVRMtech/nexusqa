"""P4 — environment governance: posture, production default-deny, behavior-class
scoping, cardinality-driven repetition, and the env health probe.

Pins the doctrine that a governance decision is NEVER an application failure:
  * production is default-deny — a mutating run is refused unless the environment
    carries an explicit, revocable write authorization (attribution env_policy);
  * a read_only/no_submit posture ENFORCES read-only — a mutating run is REFUSED
    (0 scripts, env_policy), never silently executed and reported as a pass;
  * a case a structure fork bound to a behavior class REFUSES an out-of-class
    persona (attribution test_scope) — skipped, never run-and-failed;
  * a cardinality-driven block repeats to THIS persona's proven count, not a
    hardcoded 5;
  * a known-bad environment (unreachable / login_failed / recipe_drift) is
    refused at dispatch, never blamed on the app.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_p4.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_governance as pg

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_STORE = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                           "test_factory", "persona_store.py"), encoding="utf-8").read()
_SQL = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "apply_persona_env_p4.sql"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()
_COMPILER = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                              "script_factory", "compiler.py"), encoding="utf-8").read()


# ── posture + effective posture ──────────────────────────────────────────────

def test_normalize_posture_defaults_to_read_write():
    assert pg.normalize_posture("garbage") == pg.POSTURE_READ_WRITE
    assert pg.normalize_posture(None) == pg.POSTURE_READ_WRITE
    assert pg.normalize_posture("READ_ONLY") == pg.POSTURE_READ_ONLY


def test_production_floors_posture_at_no_submit():
    # an UNAUTHORIZED production env configured read_write is floored — never bare read_write
    assert pg.effective_posture({"posture": "read_write", "is_production": True}) == pg.POSTURE_NO_SUBMIT
    # a stricter production posture is kept
    assert pg.effective_posture({"posture": "read_only", "is_production": True}) == pg.POSTURE_READ_ONLY
    # non-production keeps its declared posture
    assert pg.effective_posture({"posture": "read_write", "is_production": False}) == pg.POSTURE_READ_WRITE


def test_authorized_production_is_genuinely_read_write_not_a_no_submit_label():
    # once writes are authorized the posture is HONEST read_write — the run really
    # mutates, so we never label a mutating prod run as 'no_submit'.
    assert pg.effective_posture(
        {"posture": "read_write", "is_production": True, "write_authorized": True}
    ) == pg.POSTURE_READ_WRITE


# ── production default-deny ───────────────────────────────────────────────────

def test_production_mutating_run_denied_without_authorization():
    d = pg.gate_dispatch({"is_production": True, "posture": "read_write"}, mutating_requested=True)
    assert d["allowed"] is False
    assert d["attribution"] == pg.ATTR_ENV_POLICY
    assert "default-deny" in d["reasons"][0]


def test_production_mutating_run_allowed_with_authorization():
    d = pg.gate_dispatch(
        {"is_production": True, "posture": "read_write", "write_authorized": True},
        mutating_requested=True)
    assert d["allowed"] is True
    assert d["posture"] == pg.POSTURE_READ_WRITE   # honest: it really mutates


def test_production_read_only_run_allowed_without_authorization():
    # a NON-mutating verification suite is allowed even on locked production
    d = pg.gate_dispatch({"is_production": True, "posture": "read_only"}, mutating_requested=False)
    assert d["allowed"] is True


# ── posture is ENFORCED, not a label ─────────────────────────────────────────

def test_read_only_env_refuses_a_mutating_run_not_a_label():
    # the doctrine control: a mutating run on a read_only env is REFUSED with 0
    # scripts, never silently executed and reported green.
    d = pg.gate_dispatch({"posture": "read_only", "is_production": False}, mutating_requested=True)
    assert d["allowed"] is False
    assert d["attribution"] == pg.ATTR_ENV_POLICY
    assert "forbids mutation" in d["reasons"][0]


def test_no_submit_env_refuses_a_mutating_run():
    d = pg.gate_dispatch({"posture": "no_submit", "is_production": False}, mutating_requested=True)
    assert d["allowed"] is False
    assert d["attribution"] == pg.ATTR_ENV_POLICY


def test_read_only_env_allows_a_read_only_verification_run():
    d = pg.gate_dispatch({"posture": "read_only", "is_production": False}, mutating_requested=False)
    assert d["allowed"] is True


def test_unregistered_environment_is_allowed_default_read_write():
    # {} == today's behaviour: no governance row ⇒ ordinary run
    d = pg.gate_dispatch({}, mutating_requested=True)
    assert d["allowed"] is True
    assert d["posture"] == pg.POSTURE_READ_WRITE


def test_known_bad_environment_is_refused_never_app_blamed():
    for bad in ("unreachable", "login_failed", "recipe_drift"):
        d = pg.gate_dispatch({"health_status": bad, "health_detail": "x"}, mutating_requested=False)
        assert d["allowed"] is False
        assert d["attribution"] == pg.ATTR_ENV_POLICY


# ── behavior-class scoping ───────────────────────────────────────────────────

def test_unbound_case_applies_to_everyone():
    d = pg.scope_case("", "senior-large-family")
    assert d["in_scope"] is True


def test_in_class_persona_is_in_scope():
    d = pg.scope_case("senior-large-family", "senior-large-family")
    assert d["in_scope"] is True


def test_out_of_class_persona_is_refused_as_test_scope_not_failed():
    d = pg.scope_case("senior-large-family", "young-small-family")
    assert d["in_scope"] is False
    assert d["attribution"] == pg.ATTR_TEST_SCOPE
    assert "NOT failed" in d["reason"]


def test_partition_splits_a_suite_by_class():
    scopes = {"c1": "", "c2": "senior-large-family", "c3": "young-small-family"}
    part = pg.partition_suite(scopes, "young-small-family")
    assert set(part["in_scope"]) == {"c1", "c3"}
    assert part["out_of_scope"][0]["scenario_id"] == "c2"
    assert part["out_of_scope"][0]["attribution"] == pg.ATTR_TEST_SCOPE


# ── cardinality-driven repetition ────────────────────────────────────────────

def test_repetition_uses_proven_persona_count_over_default():
    counts = {"p_senior::beneficiary": 5, "beneficiary": 2}
    assert pg.repetition_count(counts, "beneficiary", "p_senior", default=1) == 5   # persona-specific wins
    assert pg.repetition_count(counts, "beneficiary", "p_other", default=1) == 2    # block fallback
    assert pg.repetition_count({}, "beneficiary", "p_x", default=1) == 1            # never invents


def test_repetition_plan_from_structure_diff():
    card = [{"block": "beneficiary", "count_a": 2, "count_b": 5}]
    plan = pg.repetition_plan(card, {"p_b::beneficiary": 5}, "p_b")
    assert plan == [{"block": "beneficiary", "repeat": 5}]


# ── SQL migration shape ──────────────────────────────────────────────────────

def test_migration_has_env_and_scope_tables_with_rls_and_no_delete():
    assert "CREATE TABLE IF NOT EXISTS tp_environments" in _SQL
    assert "CREATE TABLE IF NOT EXISTS tp_case_scopes" in _SQL
    assert "is_production" in _SQL and "write_authorized" in _SQL
    assert "FORCE ROW LEVEL SECURITY" in _SQL
    assert "GRANT SELECT, INSERT, UPDATE" in _SQL
    assert "DELETE" not in _SQL.split("GRANT SELECT, INSERT, UPDATE")[1]


# ── store surface ────────────────────────────────────────────────────────────

def test_store_exposes_environment_and_scope_helpers():
    for fn in ("save_environment", "get_environment", "list_environments",
               "stamp_env_health", "bind_case_scope", "get_case_scopes"):
        assert f"async def {fn}(" in _STORE
    assert "TpEnvironmentRow" in _STORE and "TpCaseScopeRow" in _STORE


def test_save_persona_returns_the_persisted_id_on_conflict():
    # a name conflict keeps the stored row's original id — save_persona must
    # RETURN that id, not a freshly minted phantom, or dispatch/scope by the
    # returned id silently resolves nothing.
    seg = _STORE[_STORE.index("async def save_persona("):]
    seg = seg[:seg.index("async def get_persona(")]
    assert ".returning(TpPersonaRow.persona_id)" in seg
    assert "real_pid" in seg


# ── dispatch wiring ──────────────────────────────────────────────────────────

def test_dispatch_gates_on_environment_and_scopes_before_running():
    assert "persona_governance.gate_dispatch(" in _ROUTER
    assert "persona_governance.partition_suite(" in _ROUTER
    # a governance refusal is attributed to the environment/scope, never the app
    assert "environment_policy" in _ROUTER or "ATTR_ENV_POLICY" in _ROUTER
    assert 'blocked_reason": persona_governance.ATTR_TEST_SCOPE' in _ROUTER
    # posture rides the run env for the report
    assert '"NEXUS_POSTURE": posture' in _ROUTER


def test_dispatch_default_deny_message_is_not_app_blame():
    seg = _ROUTER[_ROUTER.index("gate = persona_governance.gate_dispatch"):]
    seg = seg[:4000]
    # contiguous fragment within one string literal (no false app blame)
    assert "This is an environment decision" in seg


# ── endpoints ────────────────────────────────────────────────────────────────

def test_p4_endpoints_exist():
    assert "/environments/{environment_id}" in _ROUTER
    assert "/cases/{scenario_id}/behavior-class" in _ROUTER
    assert "/scopes/from-structure" in _ROUTER
    assert "/environments/{environment_id}/health" in _ROUTER


def test_health_probe_is_not_a_recrawl():
    seg = _ROUTER[_ROUTER.index("async def environment_health_endpoint"):]
    assert "NOT a re-crawl" in seg
    assert "stamp_env_health(" in seg


# ── report + compiler surface the posture ────────────────────────────────────

def test_report_surfaces_posture_in_identity():
    assert '"posture": posture or ""' in _REPORT
    assert "posture: str = \"\"," in _REPORT


def test_compiler_forwards_posture_metadata():
    assert "process.env.NEXUS_POSTURE" in _COMPILER
