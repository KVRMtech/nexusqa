"""P1.6 — the escape→guard law, mechanically enforced.

Process rule (founder-locked, 2026-07-24): every defect class that EVER
reaches a client must gain (a) a guard — a compiler/generator fix, an auditor
dimension, or an attribution rung — and (b) a regression test, BEFORE its fix
is considered done.  The ledger is ``ESCAPED_DEFECT_REGISTRY`` in
attribution_engine.py; this test makes the ledger unfalsifiable:

  * every registry entry must name at least one guard and one test;
  * every referenced test module must EXIST in tests/ (a registry entry can
    never point at a test that was deleted or never written);
  * the founding entry (url_as_text_oracle, the 2026-07-24 client-visible
    escape) can never be removed.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_escape_guard_registry.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_SVC = os.path.join(os.path.dirname(__file__), "..", "app", "services")
_TESTS_DIR = os.path.dirname(__file__)

_pkg = types.ModuleType("svc_tf_reg")
_pkg.__path__ = [os.path.join(_SVC, "test_factory")]
sys.modules.setdefault("svc_tf_reg", _pkg)
_fa_spec = importlib.util.spec_from_file_location(
    "svc_tf_reg.failure_attribution",
    os.path.join(_SVC, "test_factory", "failure_attribution.py"))
_fa = importlib.util.module_from_spec(_fa_spec)
sys.modules["svc_tf_reg.failure_attribution"] = _fa
_fa_spec.loader.exec_module(_fa)
_ae_spec = importlib.util.spec_from_file_location(
    "svc_tf_reg.attribution_engine",
    os.path.join(_SVC, "test_factory", "attribution_engine.py"))
ae = importlib.util.module_from_spec(_ae_spec)
sys.modules["svc_tf_reg.attribution_engine"] = ae
_ae_spec.loader.exec_module(ae)


def test_registry_is_nonempty_and_founding_entry_is_permanent():
    assert ae.ESCAPED_DEFECT_REGISTRY, "the escape ledger must never be empty"
    assert "url_as_text_oracle" in ae.ESCAPED_DEFECT_REGISTRY, (
        "the founding escape (2026-07-24 run 7c89de7e) is the permanent "
        "reminder — it must never be deleted from the ledger")


def test_every_entry_names_guards_and_existing_tests():
    for cls, entry in ae.ESCAPED_DEFECT_REGISTRY.items():
        assert entry.get("escaped"), f"{cls}: record WHEN/HOW it escaped"
        assert entry.get("guards"), f"{cls}: at least one guard is required"
        tests = entry.get("tests") or []
        assert tests, f"{cls}: at least one regression test is required"
        for t in tests:
            path = os.path.join(_TESTS_DIR, t)
            assert os.path.isfile(path), (
                f"{cls}: referenced regression test {t!r} does not exist — "
                "the escape→guard law forbids a ledger entry without its test")
