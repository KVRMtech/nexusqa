"""P0.2 regression — proven-oracle policy DEFAULT ON + soft misses RECORDED.

Founder-authorized policy (2026-07-24): prose-derived (best-effort) text
oracles are NON-FATAL by default — a fabricated description can never fail a
step whose grounded oracles (action, navigation, values) passed.  The flip
side of the doctrine: a soft miss must be VISIBLE, never silent — the compiled
spec records every miss via ``__nxSoftMiss(step, desc)`` (a test annotation
the bundled reporter ships into the step's ingest metadata).

Pins:
  * default (env unset)          → soft tails call __nxSoftMiss with the STEP
                                   NUMBER + a human description; the helper is
                                   injected; no bare silent .catch(() => {});
  * NEXUS_PROVEN_NAV_ORACLE=0    → legacy hard assertions, no helper injected
                                   (dead-scaffolding rule);
  * grounded oracles (toHaveURL) → HARD in both modes (never softened).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_soft_oracle_recording.py -q
"""
from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

_APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")

_svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
_sf = types.ModuleType("svc.script_factory")
_sf.__path__ = [os.path.join(_APP, "script_factory")]
sys.modules.setdefault("svc", _svc)
sys.modules.setdefault("svc.script_factory", _sf)
compiler = importlib.import_module("svc.script_factory.compiler")


_OBSERVED = {
    "kind": "button", "verb": "click", "label": "Continue",
    "after": "Order summary is shown",
    "next_url": "https://app.example/checkout/summary",
    "navigation_grounded": True,
}
_ER = "A confirmation summary appears after submitting"


def _lines(monkeypatch, policy: str | None, step_number: int = 7):
    if policy is None:
        monkeypatch.delenv("NEXUS_PROVEN_NAV_ORACLE", raising=False)
    else:
        monkeypatch.setenv("NEXUS_PROVEN_NAV_ORACLE", policy)
    return compiler._assertion_from_expected_result(
        _OBSERVED, _ER, nav_proven=True, step_number=step_number)


def test_default_policy_is_proven_only_soft(monkeypatch):
    joined = "\n".join(_lines(monkeypatch, None))
    # Best-effort text oracle present but NON-FATAL, miss RECORDED with the
    # step number and a human description.
    assert "__nxSoftMiss(7," in joined
    assert "not visible (best-effort hint)" in joined
    # No silent swallow anywhere.
    assert ".catch(() => {})" not in joined.replace(".catch(() => __nxSoftMiss", "")


def test_grounded_navigation_stays_hard_in_both_modes(monkeypatch):
    for policy in (None, "0", "1"):
        joined = "\n".join(_lines(monkeypatch, policy))
        assert "toHaveURL" in joined
        # The nav oracle line itself carries no .catch softener.
        nav_line = next(l for l in joined.splitlines() if "toHaveURL" in l)
        assert ".catch(" not in nav_line


def test_explicit_zero_restores_legacy_hard_assertions(monkeypatch):
    joined = "\n".join(_lines(monkeypatch, "0"))
    assert "__nxSoftMiss(" not in joined
    assert "toBeVisible(); // grounded" in joined


def test_soft_miss_descriptions_are_human_and_step_scoped(monkeypatch):
    joined = "\n".join(_lines(monkeypatch, None, step_number=3))
    assert "__nxSoftMiss(3," in joined
    assert "__nxSoftMiss(0," not in joined


def test_compiled_case_injects_helper_only_when_used(monkeypatch):
    """compile_case defines __nxSoftMiss iff the body calls it (auditor's
    dead-scaffolding rule) — checked at the full-spec level."""
    try:
        from nexus_sdk.models import ProductionTestCase, ProductionTestStep  # noqa: F401
    except Exception:
        class _B:  # minimal stand-ins accepted by compile_case's getattr use
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)
        ProductionTestStep = _B          # type: ignore[assignment]
        ProductionTestCase = _B          # type: ignore[assignment]

    step = ProductionTestStep(
        step_number=1,
        action="Click 'Continue'",
        expected=_ER, expected_result=_ER,
        selector="role=button|name=Continue",
        observed=dict(_OBSERVED),
        provenance="demonstrated", confidence="high",
    )
    tc = ProductionTestCase(
        test_id="softmiss-ut", name="Verify soft-miss recording",
        description="", steps=[step], preconditions=[],
        priority="P1_high", type="functional", tags=[],
    )
    monkeypatch.delenv("NEXUS_PROVEN_NAV_ORACLE", raising=False)
    spec = compiler.compile_case(tc)
    assert "__nxSoftMiss(" in spec and "function __nxSoftMiss" in spec

    monkeypatch.setenv("NEXUS_PROVEN_NAV_ORACLE", "0")
    spec_hard = compiler.compile_case(tc)
    assert "function __nxSoftMiss" not in spec_hard


def test_reporter_ships_soft_miss_annotations_into_step_metadata():
    """The bundled reporter template carries the annotation → step.metadata
    contract the ingest reads (structural pin over the template source)."""
    src = compiler._NEXUS_REPORTER_TS
    assert "nexus-soft-oracle-miss" in src
    assert "soft_oracle_misses" in src
    assert "metadata" in src


def test_soft_miss_hint_is_a_quoted_js_string_literal():
    """THE GUARD for the unquoted-hint escape (Release D-P live catch):
    ``js_str`` escapes but does NOT quote — every ``__nxSoftMiss`` call site
    must wrap the hint in quotes itself, or the emitted spec is a TypeScript
    syntax error ("No tests found", exit 1, zero steps executed)."""
    import re

    from app.services.script_factory import compiler

    src = open(compiler.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    for m in re.finditer(r"__nxSoftMiss\(\{_sn\}, \"\n\s*f\"(.)", src):
        assert m.group(1) == "'", (
            "an __nxSoftMiss hint is emitted without surrounding quotes — "
            "js_str does not add them")
    # And the compiled form itself: hint text always lands inside quotes.
    assert re.search(r"__nxSoftMiss\(\{_sn\}, \"\s*\n\s*f\"'", src) or \
        "__nxSoftMiss" not in src
