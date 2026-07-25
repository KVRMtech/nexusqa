"""Regression — bare 1/0 are numeric text values, not booleans; and one
un-bindable control never skips a whole flow.

venkata's complete-apply flow (2026-07-25): the field "Benefit share (%)" was
demonstrated as a TEXT field, verb=type, value="1" (meaning 1%). The generator
had "1" in its boolean TRUE tokens, so _is_boolean("1") was True -> the field
was reclassified as a checkbox turned ON, dropping its value AND its text-field
kind. The compiler's toggle path then found no interactive checkbox at runtime
and called test.skip() -> which skips the WHOLE test, wiping all 21
already-good steps. Two defects, both fixed:

  * generator: "1"/"0" removed from the boolean tokens (numeric, not boolean);
  * compiler: an un-bindable toggle fails THIS step RED, never whole-test skip.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_numeric_not_boolean.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

# ── generator (_is_boolean, _is_true) by file path ───────────────────────────
_GEN = os.path.join(os.path.dirname(__file__), "..", "app", "services",
                    "test_factory", "generator.py")
_spec = importlib.util.spec_from_file_location("nexus_gen_numbool_ut", _GEN)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_gen_numbool_ut"] = gen
_spec.loader.exec_module(gen)


def test_bare_one_and_zero_are_not_boolean():
    assert gen._is_boolean("1") is False
    assert gen._is_boolean("0") is False
    assert gen._is_true("1") is False


def test_explicit_boolean_words_still_boolean():
    for t in ("true", "yes", "on", "checked", "selected"):
        assert gen._is_boolean(t) is True and gen._is_true(t) is True
    for f in ("false", "no", "off", "unchecked"):
        assert gen._is_boolean(f) is True and gen._is_true(f) is False


def test_percentage_text_field_stays_a_text_value():
    """A field the user typed '1' into is a real fill, not a toggle turned ON."""
    grp = types.SimpleNamespace(
        field_candidates=[("Benefit share (%)", "1")], required_labels=[])
    text_fields, enabled_toggles, _ = gen._resolve_fields(grp)
    labels = [lbl for lbl, _v in text_fields]
    assert "Benefit share (%)" in labels
    assert ("Benefit share (%)", "1") in text_fields
    assert "Benefit share (%)" not in enabled_toggles   # NOT a toggle


# ── compiler: un-bindable toggle fails THIS step, never whole-test skip ──────

def test_compiler_unbindable_toggle_does_not_skip_whole_test():
    _APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")
    _svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
    _sf = types.ModuleType("svc.script_factory")
    _sf.__path__ = [os.path.join(_APP, "script_factory")]
    sys.modules.setdefault("svc", _svc)
    sys.modules.setdefault("svc.script_factory", _sf)
    compiler = importlib.import_module("svc.script_factory.compiler")
    src = compiler.__file__ and open(compiler.__file__, encoding="utf-8").read()
    # the un-bindable-toggle branch must NOT emit a whole-test skip …
    assert "test.skip(true, 'UNPROVEN toggle" not in src
    # … it fails just this step with a clear, honest cause.
    assert "UNVERIFIABLE control" in src
