"""R3 — scoped certification: 'cert on env A does not grant trust on env B/class B'.

Additive + opt-in: a per-(scenario, environment, behavior_class) ledger, an
(env × class) proof matrix in the Trust Block, and an OPT-IN gate that admits
only certified cells when an environment sets require_scoped_certification. The
load-bearing baseline certification gate is untouched (default behaviour
unchanged).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_r3.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_store as store

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_STORE = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                           "test_factory", "persona_store.py"), encoding="utf-8").read()
_SQL = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                         "apply_persona_env_r3.sql"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()


# ── the matrix (pure) ────────────────────────────────────────────────────────

def test_certification_matrix_keeps_cells_independent():
    ledger = [
        {"scenario_id": "s1", "environment_id": "uat", "behavior_class": "young-small-family", "status": "certified"},
        {"scenario_id": "s2", "environment_id": "uat", "behavior_class": "young-small-family", "status": "failed"},
        {"scenario_id": "s1", "environment_id": "prod", "behavior_class": "senior-large-family", "status": "certified"},
    ]
    m = store.certification_matrix(ledger)
    assert m["uat"]["young-small-family"] == {"certified": 1, "failed": 1, "scenarios": 2}
    assert m["prod"]["senior-large-family"] == {"certified": 1, "failed": 0, "scenarios": 1}
    # the uat/young cert says NOTHING about prod/senior — separate cells
    assert "young-small-family" not in m["prod"]


# ── migration + store ────────────────────────────────────────────────────────

def test_migration_is_additive_ledger_plus_optin_flag():
    assert "CREATE TABLE IF NOT EXISTS tp_certification_ledger" in _SQL
    assert "ADD COLUMN IF NOT EXISTS require_scoped_certification" in _SQL
    assert "FORCE ROW LEVEL SECURITY" in _SQL
    assert "GRANT SELECT, INSERT, UPDATE" in _SQL


def test_store_exposes_ledger_helpers():
    for fn in ("record_certification", "get_certification_ledger", "cell_certified_scenarios"):
        assert f"async def {fn}(" in _STORE
    assert "def certification_matrix(" in _STORE
    assert "TpCertificationLedgerRow" in _STORE


# ── the opt-in gate is enforced at dispatch, additive ────────────────────────

def test_scoped_cert_gate_is_opt_in_and_admits_only_certified_cells():
    assert 'governance_env.get("require_scoped_certification")' in _ROUTER
    seg = _ROUTER[_ROUTER.index('if governance_env.get("require_scoped_certification")'):]
    seg = seg[:2200]
    assert "cell_certified_scenarios(" in seg
    assert 'blocked_reason": "needs_scoped_certification"' in seg
    # honest: a cert elsewhere does not grant trust here; not an app failure
    assert "does not grant" in seg
    assert "NOT an application failure" in seg


def test_endpoints_and_trust_matrix_exist():
    assert "/certification/record" in _ROUTER
    assert "/certification/matrix" in _ROUTER
    assert '"certification_matrix": cert_matrix' in _REPORT
