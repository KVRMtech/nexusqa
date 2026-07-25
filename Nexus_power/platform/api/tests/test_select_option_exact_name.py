"""Regression — a <select>'s option-content fallback rung must match the option
name EXACTLY, or a short option collides with a sibling select.

venkata cert run e2bde351 (2026-07-25): the State combination cases with
State=MA went RED at step 2 —

    locator.selectOption: Test timeout of 60000ms exceeded.
    waiting for getByLabel('State').or(getByRole('combobox',{name:'State'}))
      .or(locator('select').filter({has: getByRole('option',{name:'MA'})})).first()
    - locator resolved to <select id="gender" ...>

`getByRole('option', {name})` defaults to a SUBSTRING, case-insensitive match,
so name:'MA' also matched the 'Male' option of the Gender select. The .or()
union then contained BOTH selects and .first() bound Gender (DOM order), so
selectOption('MA') waited forever for an 'MA' option that Gender never has.
Exact match keys the rung to the one select that truly holds the option.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_select_option_exact_name.py -q
"""
from __future__ import annotations

import importlib
import os
import sys
import types


def _compiler():
    _APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")
    _svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
    _sf = types.ModuleType("svc.script_factory")
    _sf.__path__ = [os.path.join(_APP, "script_factory")]
    sys.modules.setdefault("svc", _svc)
    sys.modules.setdefault("svc.script_factory", _sf)
    return importlib.import_module("svc.script_factory.compiler")


def test_select_option_fallback_rung_is_exact():
    """The option-content rung must carry exact:true so 'MA' != 'Male'."""
    compiler = _compiler()
    rungs = compiler._ladder_rungs(
        {"label": "State", "value": "MA"}, "select")
    option_rung = [r for r in rungs if "getByRole('option'" in r]
    assert option_rung, "expected an option-content fallback rung for a <select>"
    assert "exact: true" in option_rung[0], (
        "option-name match must be EXACT — a substring match binds 'MA' to "
        "the 'Male' option of a sibling <select>")


def test_source_pins_the_exact_flag_on_option_filter():
    compiler = _compiler()
    src = open(compiler.__file__, encoding="utf-8").read()
    # the emitted filter must request an exact option name
    assert "getByRole('option', "
    assert "'{_optval}', exact: true" in src or "exact: true }}) }})" in src
