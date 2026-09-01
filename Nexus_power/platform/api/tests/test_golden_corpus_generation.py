"""P2.7 — Golden-corpus product CI: the product grades its OWN output before
any deploy ships it.

Three realistic recorded-substrate corpora (an insurance-style wizard shaped
like the escaped 2026-07-24 incident, a storefront, an internal dotless-host
tool) run the FULL pipeline — generate → compile → audit — and every release
must hold these invariants:

  I1. generation yields runnable cases, every name business-intent;
  I2. NO compiled spec would be blocked by the auditor gate (a blocked spec
      shipping to a client is the defining product failure);
  I3. NO always-RED URL-as-text oracle anywhere (the escaped class);
  I4. NO raw URL in any client-visible Expected Result;
  I5. NO silently-swallowed text oracle: under the default proven-oracle
      policy every non-fatal text oracle records its miss (__nxSoftMiss);
  I6. every compiled spec is brace-balanced (syntax smoke — a parse-broken
      spec fails the whole run, the compiler-comment-newline incident class).

A verdict flip here fails OUR CI before the deploy — the structural defense
against efd0269-style regressions. Generic: corpora are synthetic-but-real
substrate shapes; nothing is keyed to a live host.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_golden_corpus_generation.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

import pytest

# ── SDK stub + module loading (same approach as test_upload_generation.py) ───
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
_spec = importlib.util.spec_from_file_location("nexus_generator_golden_ut", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_golden_ut"] = gen
_spec.loader.exec_module(gen)
PV, PA = gen.PageVisitInput, gen.PageActionInput

_svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
_sf = types.ModuleType("svc.script_factory")
_sf.__path__ = [os.path.join(_APP, "script_factory")]
sys.modules.setdefault("svc", _svc)
sys.modules.setdefault("svc.script_factory", _sf)
compiler = importlib.import_module("svc.script_factory.compiler")

_AUD_PATH = os.path.join(_APP, "test_factory", "playwright_auditor.py")
_aud_spec = importlib.util.spec_from_file_location("nexus_auditor_golden_ut", _AUD_PATH)
aud = importlib.util.module_from_spec(_aud_spec)
sys.modules["nexus_auditor_golden_ut"] = aud
_aud_spec.loader.exec_module(aud)


# ── The golden corpora (generic substrate shapes) ────────────────────────────

def _corpus_insurance_wizard():
    """The escaped-incident shape: multi-step wizard, URL-valued after_detail,
    proven navigation, typed values — on a public https host."""
    visits = [
        PV(page_visit_id="v1", sequence_index=0, location="Portal apply",
           url_host="life.example.com", url_path="/portal/apply", url_query="",
           canonical_host="life.example.com", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Apply step 2",
           url_host="life.example.com", url_path="/portal/apply/details", url_query="",
           canonical_host="life.example.com", source="ground_truth", form_snapshot={}),
    ]
    actions = [
        PA(page_visit_id="v1", subaction_index=0, verb="type",
           target_label="First name", target_kind="text_field", value="Test"),
        PA(page_visit_id="v1", subaction_index=1, verb="type",
           target_label="Date of birth", target_kind="text_field", value="1990-01-15"),
        PA(page_visit_id="v1", subaction_index=2, verb="click",
           target_label="Continue", target_kind="button", value=None,
           after_outcome="navigation", navigated=True,
           after_detail="https://life.example.com/portal/apply/details?step=2&fname=Test"),
        PA(page_visit_id="v2", subaction_index=0, verb="type",
           target_label="Coverage amount", target_kind="text_field", value="500000"),
    ]
    return visits, actions


def _corpus_storefront():
    """Region-word outcome text (real page text, not URLs) + a cart form."""
    visits = [
        PV(page_visit_id="s1", sequence_index=0, location="Catalog",
           url_host="shop.example.com", url_path="/products", url_query="",
           canonical_host="shop.example.com", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="s2", sequence_index=1, location="Cart",
           url_host="shop.example.com", url_path="/cart", url_query="",
           canonical_host="shop.example.com", source="ground_truth", form_snapshot={}),
    ]
    actions = [
        PA(page_visit_id="s1", subaction_index=0, verb="click",
           target_label="View cart", target_kind="link", value=None,
           after_outcome="navigation", navigated=True,
           after_detail="https://shop.example.com/cart"),
        PA(page_visit_id="s2", subaction_index=0, verb="type",
           target_label="Promo code", target_kind="text_field", value="SAVE10"),
        PA(page_visit_id="s2", subaction_index=1, verb="click",
           target_label="Apply promo", target_kind="button", value=None,
           after_outcome="content_change", after_detail="Order summary updated"),
    ]
    return visits, actions


def _corpus_internal_tool():
    """Dotless internal host (container-style) + a required form field."""
    visits = [
        PV(page_visit_id="t1", sequence_index=0, location="Requests",
           url_host="tooling", url_path="/requests/new", url_query="",
           canonical_host="tooling", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="t2", sequence_index=1, location="Submitted",
           url_host="tooling", url_path="/requests/submitted", url_query="",
           canonical_host="tooling", source="ground_truth", form_snapshot={}),
    ]
    actions = [
        PA(page_visit_id="t1", subaction_index=0, verb="type",
           target_label="Request title", target_kind="text_field", value="Access to reports"),
        PA(page_visit_id="t1", subaction_index=1, verb="click",
           target_label="Submit request", target_kind="button", value=None,
           after_outcome="navigation", navigated=True,
           after_detail="http://tooling/requests/submitted"),
    ]
    return visits, actions


_CORPORA = {
    "insurance_wizard": _corpus_insurance_wizard,
    "storefront": _corpus_storefront,
    "internal_tool": _corpus_internal_tool,
}


def _generate(name):
    visits, actions = _CORPORA[name]()
    res = gen.generate_demonstrated_test_cases(
        artifact_id=f"golden-{name}", page_visits=visits, page_actions=actions)
    assert res.test_cases, f"{name}: generation produced no cases"
    return res.test_cases


@pytest.fixture(autouse=True)
def _default_policy(monkeypatch):
    """The corpus runs under the SHIPPED default (proven-oracle policy ON)."""
    monkeypatch.delenv("NEXUS_PROVEN_NAV_ORACLE", raising=False)


@pytest.mark.parametrize("corpus", sorted(_CORPORA))
def test_golden_corpus_invariants(corpus):
    cases = _generate(corpus)

    for tc in cases:
        # I1 — business-intent names, no URLs/hosts/crawler jargon.
        name = getattr(tc, "name", "") or ""
        assert name.startswith("Verify ") or name.startswith("Rank "), (
            f"{corpus}: non-business name {name!r}")
        assert "http" not in name and "example.com" not in name, name

        # I4 — client-visible Expected Results carry no raw URL.
        for s in list(getattr(tc, "steps", []) or []):
            for field in ("expected", "expected_result"):
                text = getattr(s, field, "") or ""
                assert "https://" not in text and "http://" not in text, (
                    f"{corpus}: raw URL leaked into {field}: {text!r}")

        spec = compiler.compile_case(tc)

        # I3 — the escaped class can never compile again.
        assert "getByText(/https" not in spec
        assert "getByText(/http" not in spec
        assert "getByText(/www" not in spec

        # I5 — no silently-swallowed TEXT oracle: every non-fatal toBeVisible
        # text oracle records its miss. (Value-oracle catches are a separate,
        # pre-existing tolerance contract — out of scope here.)
        for line in spec.splitlines():
            if "toBeVisible().catch(() => {})" in line:
                raise AssertionError(
                    f"{corpus}: silently-swallowed text oracle: {line.strip()[:140]}")

        # I6 — brace-balance syntax smoke (a parse-broken spec reds the run).
        assert spec.count("{") == spec.count("}"), f"{corpus}: unbalanced braces"
        assert spec.count("(") == spec.count(")"), f"{corpus}: unbalanced parens"

        # I2 — the auditor gate must not block what we ship.
        report = aud.score_spec(spec, steps=list(getattr(tc, "steps", []) or []))
        g = aud.gate(report, blocking=True)
        assert g["would_block"] is False, (
            f"{corpus}: auditor would BLOCK generated spec for {tc.name!r}: "
            f"{g['block_reasons']}")
        assert report["decision"] != aud.DECISION_DEFECT


def test_incident_shape_regression_end_to_end():
    """The verbatim 2026-07-24 incident shape (URL-valued after_detail on the
    final proven click) must yield: business prose, a hard toHaveURL, no
    URL-text oracle, and an auditor pass — the full pipeline, one assertion
    chain, forever."""
    cases = _generate("insurance_wizard")
    primary = cases[0]
    nav_steps = [
        s for s in primary.steps
        if "proceeds to" in (getattr(s, "expected", "") or "")
    ]
    assert nav_steps, "expected a navigation-asserting step"
    spec = compiler.compile_case(primary)
    assert "toHaveURL" in spec
    assert "getByText(/https" not in spec
    g = aud.gate(aud.score_spec(spec, steps=list(primary.steps)), blocking=True)
    assert g["passed"] is True
