"""Regression tests for the Track-1 Playwright-quality generator fixes.

These pin the auditor's 2/10 failure mode shut: the deterministic generator must
NEVER assert a navigation a click is not shown to cause (the impossible
"Add to cart -> /checkout-step-one" transition), MUST restore fills that live only
in the action stream (verb=type), and MUST keep a genuinely-grounded navigation
(no over-suppression). Acceptance tests AT-0/AT-1/AT-2 from the 10/10 plan.

NO live stack / NO DB / NO SDK: nexus_sdk lives in the separate nexus-base image,
so we stub nexus_sdk.models and load the generator by file path (same approach as
test_induced_drift_benchmark.py). Run from Nexus_power/platform/api:
    python -m pytest tests/test_generator_navigation_grounding.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

# Prefer the REAL SDK models when available (CI / container); fall back to a
# minimal stub so the generator's pure logic still loads standalone. Trying the
# real import FIRST means this test never poisons sys.modules['nexus_sdk'] for
# sibling tests that need the real package.
try:
    from nexus_sdk.models import (  # noqa: F401
        Precondition,
        ProductionTestCase,
        ProductionTestStep,
    )
except Exception:
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Precondition(_Base):
        pass

    class ProductionTestStep(_Base):
        def __init__(self, **kw):
            self.observed = {}
            self.provenance = ""
            self.screenshot = ""
            self.data_ref = None
            super().__init__(**kw)

    class ProductionTestCase(_Base):
        pass

    _mod = types.ModuleType("nexus_sdk")
    _models = types.ModuleType("nexus_sdk.models")
    _models.Precondition = Precondition
    _models.ProductionTestStep = ProductionTestStep
    _models.ProductionTestCase = ProductionTestCase
    _mod.models = _models
    sys.modules["nexus_sdk"] = _mod
    sys.modules["nexus_sdk.models"] = _models

_GEN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory", "generator.py"
)
_spec = importlib.util.spec_from_file_location("nexus_generator_under_test", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_under_test"] = gen  # dataclass processing needs this
_spec.loader.exec_module(gen)

PV, PA = gen.PageVisitInput, gen.PageActionInput


def _visits():
    return [
        PV(page_visit_id="v1", sequence_index=0, location="Inventory",
           url_host="saucedemo.com", url_path="/inventory", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Checkout: Your Information",
           url_host="saucedemo.com", url_path="/checkout-step-one", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
    ]


def _actions(addcart_navigated: bool):
    """A click that stays on the page (Add to cart) + two typed fields whose values
    live ONLY in the action stream (empty form_snapshot above)."""
    return [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Add to cart (Sauce Labs Backpack)", target_kind="button",
           value=None,
           after_outcome=("navigation" if addcart_navigated else "content_appeared"),
           after_detail=("" if addcart_navigated else "button now reads Remove; cart shows 1 item"),
           navigated=addcart_navigated),
        PA(page_visit_id="v2", subaction_index=0, verb="type", target_label="First Name",
           target_kind="text_field", value="Venkata"),
        PA(page_visit_id="v2", subaction_index=1, verb="type", target_label="Last Name",
           target_kind="text_field", value="karnam"),
    ]


def _steps(addcart_navigated: bool):
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_visits(), page_actions=_actions(addcart_navigated))
    assert res.test_cases, "expected a demonstrated test case (>=2 page groups)"
    return res.test_cases[0].steps


def _find(steps, needle):
    return [s for s in steps if needle.lower() in (getattr(s, "action", "") or "").lower()]


def test_non_navigating_click_does_not_assert_navigation():
    """AT-0: 'Add to cart' (stays on page) must NOT pin next_url -> no impossible toHaveURL."""
    steps = _steps(addcart_navigated=False)
    addcart = _find(steps, "Add to cart")[0]
    assert "next_url" not in (addcart.observed or {})


def test_uncaptured_boundary_is_demoted_to_inferred():
    """AT-0: the un-grounded inventory->checkout-step-one boundary must be UNPROVEN
    (provenance='inferred'), not a hard nav assertion that can only fail RED."""
    steps = _steps(addcart_navigated=False)
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred"


def test_typed_fills_restored_from_action_stream():
    """AT-2: typed values (verb=type) with an EMPTY form_snapshot must still become
    grounded fill steps (otherwise the data file / value-token helper go dead)."""
    steps = _steps(addcart_navigated=False)
    firsts = _find(steps, "First Name")
    lasts = _find(steps, "Last Name")
    assert any((s.observed or {}).get("value") == "Venkata" for s in firsts)
    assert any((s.observed or {}).get("value") == "karnam" for s in lasts)


def test_inferred_page_url_is_not_asserted():
    """Phase 1 (honest contract): a vision/title-INFERRED page URL must NOT be
    asserted as fact — its nav-verify is UNPROVEN even when the navigation itself
    was grounded. A PROVEN (url_regex / ground_truth) page still asserts."""
    visits = [
        PV(page_visit_id="v1", sequence_index=0, location="Inventory",
           url_host="saucedemo.com", url_path="/inventory", url_query="",
           canonical_host="saucedemo.com", source="url_regex", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Checkout",
           url_host="saucedemo.com", url_path="/checkout-step-one", url_query="",
           canonical_host="saucedemo.com", source="llm_inferred", form_snapshot={},
           extraction_confidence=0.85),
    ]
    acts = [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Continue", target_kind="button", value=None,
           after_outcome="navigation", navigated=True),
    ]
    res = gen.generate_demonstrated_test_cases(artifact_id="t", page_visits=visits, page_actions=acts)
    steps = res.test_cases[0].steps
    navverify = _find(steps, "Verify the application navigated")[0]
    assert getattr(navverify, "provenance", "") == "inferred"  # vision URL → UNPROVEN


def test_grounded_navigation_is_kept():
    """AT-1: a click the recording PROVES navigated keeps its assertion (no over-suppression)."""
    steps = _steps(addcart_navigated=True)
    addcart = _find(steps, "Add to cart")[0]
    navverify = _find(steps, "Verify the application navigated")[0]
    assert (addcart.observed or {}).get("next_url")
    assert (addcart.observed or {}).get("navigation_grounded") is True
    assert getattr(navverify, "provenance", "") == "demonstrated"


if __name__ == "__main__":  # allow running without pytest
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
