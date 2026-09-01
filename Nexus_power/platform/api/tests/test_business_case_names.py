"""F5 regression — business-intent test-case names (Issue 1).

Before: names described CRAWLER MECHANICS — "Branch — exploration from 'apply'
and back (sslip.io)" — meaningless to business users, QA, POs.  After: every
generated name says WHAT IS VERIFIED, in the app's own words, readable without
opening the steps:

    Verify user can complete the 'apply' journey to 'dashboard'
    Verify user can navigate from 'apply' to 'dashboard' and return
    Verify submission is blocked when required field 'First name' is left empty
    Verify user can complete the flow with age=65, tobacco=yes

Honesty rule: a name never claims more than the case's oracles prove (the
branch case's oracle is "returns to the entry page", so its name says
"…and return", not "…without losing data").

Generic: names are built from the app's own recorded words (page names, field
labels, CTA labels, values) — no domain vocabulary, no hosts, no crawler jargon.

Two layers:
  * dynamic — generate from a synthetic substrate, assert name properties;
  * static  — a source contract over generator.py + combinations.py so any
    FUTURE naming site must start with "Verify " too.

NO live stack / NO DB.  Run from Nexus_power/platform/api:
    python -m pytest tests/test_business_case_names.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

# ── SDK stub (same approach as test_upload_generation.py) ────────────────────
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
_COMBO_PATH = os.path.join(_APP, "test_factory", "combinations.py")
_spec = importlib.util.spec_from_file_location("nexus_generator_names_ut", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_names_ut"] = gen
_spec.loader.exec_module(gen)
PV, PA = gen.PageVisitInput, gen.PageActionInput

_LEGACY_JARGON = (
    "Branch — exploration", "Functional E2E:", "Variant —", "Negative —",
    "exploration from",
)


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
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Contact", target_kind="link", value=None,
           after_outcome="navigation", navigated=True,
           after_detail="https://app.example/contact"),
        PA(page_visit_id="v2", subaction_index=0, verb="type",
           target_label="Subject", target_kind="text_field", value="Warranty"),
    ]


def _cases():
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_visits(), page_actions=_actions())
    assert res.test_cases, "expected at least the primary demonstrated case"
    return res.test_cases


# ── dynamic: generated names ─────────────────────────────────────────────────

def test_primary_case_name_states_the_business_journey():
    name = _cases()[0].name
    assert name.startswith("Verify user can complete the '")
    assert "'contact'" in name          # the app's own page word
    assert "journey to" in name or "flow" in name


def test_no_generated_name_contains_host_url_or_crawler_jargon():
    for c in _cases():
        name = c.name or ""
        assert name.startswith("Verify "), f"non-business name: {name!r}"
        assert "http" not in name and "app.example" not in name, name
        for jargon in _LEGACY_JARGON:
            assert jargon not in name, f"crawler jargon in name: {name!r}"


# ── static: source contract over every naming site ───────────────────────────

def _name_literals(src: str) -> list[str]:
    """Every ``name=`` f-string literal prefix in the source (both quote
    styles).  The look-behind keeps ``dest_name=`` / ``src_name=`` and other
    suffixed identifiers out of scope — only the case-name kwarg counts."""
    out = []
    for m in re.finditer(r"(?<![A-Za-z0-9_])name\s*=\s*\(?\s*f?\"([A-Za-z][^\"{]*)", src):
        out.append(m.group(1))
    for m in re.finditer(r"(?<![A-Za-z0-9_])name\s*=\s*\(?\s*f?'([A-Za-z][^'{]*)", src):
        out.append(m.group(1))
    return out


def test_every_naming_site_in_source_is_business_intent():
    """Source contract over the NAME LITERALS only (comments/docstrings may
    legitimately mention the legacy formats when explaining the change)."""
    gen_src = open(_GEN_PATH, encoding="utf-8").read()
    combo_src = open(_COMBO_PATH, encoding="utf-8").read()
    literals = _name_literals(gen_src + combo_src)
    assert literals, "expected name= literals in the generator sources"
    for lit in literals:
        assert lit.startswith("Verify ") or lit.startswith("Rank "), (
            f"naming site does not state business intent: {lit!r} — every "
            "generated case name must start with 'Verify '")
        for jargon in _LEGACY_JARGON:
            assert jargon not in lit, f"legacy name format still present: {lit!r}"
