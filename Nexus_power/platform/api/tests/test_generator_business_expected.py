"""F2 regression — business-language Expected Results (no raw URLs in prose).

Root cause (run 7c89de7e step 7): the step builder wrote the recorded
destination URL straight into the Expected Result ("The application proceeds
to https://…; https://…?step=2&fname=…") and the crawl-substrate ``after_detail``
(also a URL) was appended as if it were observed page text.  Unreadable for
business users AND the raw material for the ``getByText(/https/i)`` false-RED
oracle (guarded compile-side by F1 / test_compiler_url_text_oracle.py).

F2 fixes the source: destinations are phrased from the app's own URL structure
— "the 'apply' page (/portal/apply)" — while the machine-side URL still rides
``observed.next_url`` into the compiler's hard toHaveURL oracle.  The
"proceeds to" keyphrase is preserved (confidence.py / recording_quality.py key
on it to recognise navigation-asserting steps).

Generic: phrasing is built from URL structure + the app's own path words —
no domain vocabulary.  NO live stack / NO DB.  Run from Nexus_power/platform/api:
    python -m pytest tests/test_generator_business_expected.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

# Prefer the REAL SDK models; fall back to a minimal stub (same approach as
# test_upload_generation.py — never poisons sys.modules for sibling tests).
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
            kw.setdefault("observed", {})
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

_APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")
_GEN_PATH = os.path.join(_APP, "test_factory", "generator.py")
_spec = importlib.util.spec_from_file_location("nexus_generator_bizexp_ut", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_bizexp_ut"] = gen
_spec.loader.exec_module(gen)
PV, PA = gen.PageVisitInput, gen.PageActionInput


# ── _dest_phrase unit level ──────────────────────────────────────────────────

def test_dest_phrase_full_url_names_the_page():
    assert gen._dest_phrase(
        "https://vkpowerlife.35-186-147-245.sslip.io/portal/apply?step=2&fname=Test"
    ) == "the 'apply' page (/portal/apply)"


def test_dest_phrase_path_only_input():
    assert gen._dest_phrase("/category/power-tools") == \
        "the 'power tools' page (/category/power-tools)"


def test_dest_phrase_never_leaks_url():
    for raw in ("https://a.b/c/d?e=f#g", "www.example.com/x/yz-page", "/plain/path"):
        phrase = gen._dest_phrase(raw)
        assert "https://" not in phrase and "www." not in phrase
        assert "?" not in phrase and "#" not in phrase


# ── generated-case level (the client-visible Expected Results) ───────────────

def _visits():
    return [
        PV(page_visit_id="v1", sequence_index=0, location="Home",
           url_host="app.example", url_path="/", url_query="",
           canonical_host="app.example", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Contact",
           url_host="app.example", url_path="/contact", url_query="",
           canonical_host="app.example", source="ground_truth", form_snapshot={}),
    ]


def _actions():
    return [
        # The URL-shaped after_detail mirrors the crawl-substrate capture that
        # produced the run-7c89de7e prose ("proceeds to https://…; https://…").
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Contact", target_kind="link", value=None,
           after_outcome="navigation", navigated=True,
           after_detail="https://app.example/contact?src=nav"),
        PA(page_visit_id="v2", subaction_index=0, verb="type",
           target_label="Subject", target_kind="text_field", value="Warranty"),
    ]


def _steps():
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_visits(), page_actions=_actions())
    assert res.test_cases, "expected a demonstrated case"
    return res.test_cases[0].steps


def test_no_step_expected_contains_a_raw_url():
    """THE F2 acceptance: no Expected Result ever shows a raw URL again."""
    for s in _steps():
        for field in ("expected", "expected_result"):
            text = (getattr(s, field, "") or "")
            assert "https://" not in text and "http://" not in text, (
                f"raw URL leaked into {field!r}: {text!r}")


def test_navigation_expected_is_business_phrased_and_keyphrase_kept():
    nav = [s for s in _steps() if "proceeds to" in (getattr(s, "expected", "") or "")]
    assert nav, "expected a navigation-asserting step (keyphrase must survive F2)"
    exp = nav[0].expected
    # The reader sees the page NAME + path — the app's own words, no URL.
    assert "the 'contact' page (/contact)" in exp
    # The machine-side oracle input is untouched: the raw URL still rides
    # observed.next_url into the compiler's hard toHaveURL.
    obs = (nav[0].observed or {})
    assert obs.get("next_url", "").startswith("http") or obs.get("next_url", "").startswith("/")
