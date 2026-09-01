"""Regression guards for the Phase-3 deterministic fixes (proven against the harness).

Each fix must MEASURABLY improve the score, and none may hardcode an app-under-test.
Run:  cd platform/api && python tests/accuracy/test_fixes.py   (or pytest)
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from baseline import AEGIS, AEGIS_LABEL, SAUCE, SAUCE_LABEL, extraction_doc  # noqa: E402
import fixes  # noqa: E402
from fixes import (apply_all, decompose_repeated_prefix_labels,  # noqa: E402
                   drop_recording_chrome_pages, kill_low_confidence_fabricated_navigations)
from harness import score  # noqa: E402


def _f1(doc):
    return score(doc, SAUCE_LABEL)["completeness"]["actions"]["f1"]


def test_disambiguator_lifts_action_f1():
    before = _f1(extraction_doc(SAUCE))
    after = _f1(decompose_repeated_prefix_labels(extraction_doc(SAUCE)))
    assert after > before + 0.15, (before, after)  # the 3 over-qualified add-to-carts recovered


def test_chrome_drop_lifts_node_f1():
    b = score(extraction_doc(SAUCE), SAUCE_LABEL)["page_graph"]["node"]["f1"]
    a = score(drop_recording_chrome_pages(extraction_doc(SAUCE)), SAUCE_LABEL)["page_graph"]["node"]["f1"]
    assert a > b, (b, a)


def test_fabrication_kill_drops_low_conf_navigates():
    b = len(extraction_doc(SAUCE).actions)
    a = len(kill_low_confidence_fabricated_navigations(extraction_doc(SAUCE)).actions)
    assert a < b, (b, a)  # the navigate@0.55 rows are removed


def test_apply_all_cuts_fabrication_rate():
    b = score(extraction_doc(SAUCE), SAUCE_LABEL)["faithfulness"]["rate"]
    a = score(apply_all(extraction_doc(SAUCE)), SAUCE_LABEL)["faithfulness"]["rate"]
    assert a < b, (b, a)


def test_apply_all_improves_aggregate_f1_both_apps():
    for aid, label in [(SAUCE, SAUCE_LABEL), (AEGIS, AEGIS_LABEL)]:
        b = score(extraction_doc(aid), label)["completeness"]["actions"]["f1"]
        a = score(apply_all(extraction_doc(aid)), label)["completeness"]["actions"]["f1"]
        assert a >= b, (aid, b, a)  # never regresses F1 on either app


def test_fixes_have_no_app_under_test_hardcoding():
    src = inspect.getsource(fixes).lower()
    for banned in ["saucedemo", "sauce labs", "onesie", "bolt", "fleece", "aegis",
                   "reddy", "karna", "/apply", "/inventory", "/checkout", "swag labs"]:
        assert banned not in src, f"hardcoded app-under-test token: {banned}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nALL {len(tests)} FIX REGRESSION TESTS PASS")
