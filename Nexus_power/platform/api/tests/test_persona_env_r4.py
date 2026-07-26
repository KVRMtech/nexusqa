"""R4 — cardinality-driven repetition is CONSUMED (one suite, any family size).

Pins that a repeated block's per-member count is persisted, computed into a
repetition plan via persona_governance (giving the pure fn a real consumer),
injected into the run env (NEXUS_REPETITION), surfaced in the report, and
readable by the compiled script via __nxRepeat — with the honest doctrine that an
unset block repeats once (never an invented number).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_persona_env_r4.py -q
"""
from __future__ import annotations

import os

from app.services.test_factory import persona_governance as pg

_ROUTER = open(os.path.join(os.path.dirname(__file__), "..", "app", "routers",
                            "test_factory.py"), encoding="utf-8").read()
_STORE = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                           "test_factory", "persona_store.py"), encoding="utf-8").read()
_COMPILER = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                              "script_factory", "compiler.py"), encoding="utf-8").read()
_REPORT = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "test_factory", "evidence_report.py"), encoding="utf-8").read()


# ── the pure plan (unchanged) still holds and is now CONSUMED ─────────────────

def test_repetition_plan_never_invents_a_count():
    plan = pg.repetition_plan([{"block": "beneficiary", "count_b": 5}],
                              {"p::beneficiary": 5}, "p")
    assert plan == [{"block": "beneficiary", "repeat": 5}]
    # an unknown block would fall back to the recorded shape (1), never invented
    assert pg.repetition_count({}, "dependents", "p", default=1) == 1


def test_dispatch_consumes_repetition_plan():
    assert "persona_governance.repetition_plan(" in _ROUTER
    assert "get_persona_cardinality(" in _ROUTER
    assert '"NEXUS_REPETITION": json.dumps(repetition)' in _ROUTER
    assert '"repetition_plan": repetition' in _ROUTER


def test_store_persists_per_persona_cardinality_additively():
    for fn in ("set_persona_cardinality", "get_persona_cardinality"):
        assert f"async def {fn}(" in _STORE
    assert "__cardinality__::" in _STORE  # reserved answer-sheet key, no migration


def test_cardinality_endpoints_exist():
    assert "/personas/{persona_id}/cardinality/{environment_id}" in _ROUTER
    assert "/personas/{persona_id}/repetition-plan/{environment_id}" in _ROUTER


def test_compiler_exposes_nxRepeat_reading_the_run_env():
    assert "function __nxRepeat(" in _COMPILER
    assert "process.env.NEXUS_REPETITION" in _COMPILER
    # an unset block repeats once, never an invented number
    seg = _COMPILER[_COMPILER.index("function __nxRepeat("):]
    seg = seg[:400]
    assert "Math.max(1, fallback)" in seg


def test_report_surfaces_repetition_in_identity():
    assert '"repetition": _rep' in _REPORT
    assert "repetition: str = \"\"," in _REPORT
