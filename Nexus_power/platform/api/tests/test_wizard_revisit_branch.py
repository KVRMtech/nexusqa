"""Regression — a forward multi-step wizard on ONE path is NOT a revisit branch.

Root cause of venkata's near-total failure (2026-07-25): the app is a 4-step
wizard served entirely on ``/portal/apply``, advancing only its ``?step=N``
query. ``_split_revisit_branch`` keyed the revisit on host+path and IGNORED the
query, so all four steps hashed identical, the forward progression was
mis-detected as a "left and returned" revisit, and the middle steps (Coverage,
Health) were peeled into a branch case — leaving the PRIMARY flow jumping
Applicant -> Beneficiary with the Coverage/Health field-fills dropped. Every
combination built on that broken base then died at the first missing-page
field. The crawl substrate was COMPLETE; the defect was purely in generation.

These pin: (1) a query-advancing wizard yields NO branch (trunk keeps every
step); (2) a genuine same-URL revisit (path AND query identical) still splits.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_wizard_revisit_branch.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

# Load the generator by file path (pure logic; SDK models stubbed if absent).
try:
    from nexus_sdk.models import (  # noqa: F401
        Precondition, ProductionTestCase, ProductionTestStep,
    )
except Exception:
    class _B:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)
    _mod = types.ModuleType("nexus_sdk"); _models = types.ModuleType("nexus_sdk.models")
    _models.Precondition = _models.ProductionTestStep = _models.ProductionTestCase = _B
    _mod.models = _models
    sys.modules["nexus_sdk"] = _mod
    sys.modules["nexus_sdk.models"] = _models

_GEN = os.path.join(os.path.dirname(__file__), "..", "app", "services",
                    "test_factory", "generator.py")
_spec = importlib.util.spec_from_file_location("nexus_gen_revisit_ut", _GEN)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_gen_revisit_ut"] = gen
_spec.loader.exec_module(gen)


def _grp(path, query, host="app.example.com"):
    return gen._PageGroup(
        url_host=host, url_path=path, url_query=query,
        canonical_host="example.com", location=f"{host}{path}",
        frame_ref="", source="ground_truth", extraction_confidence=1.0,
    )


def test_forward_wizard_on_one_path_is_not_a_branch():
    """The exact venkata shape: 4 steps on /portal/apply, ?step= advancing."""
    groups = [
        _grp("/portal/apply", "step=1"),
        _grp("/portal/apply", "step=2&fname=Test"),
        _grp("/portal/apply", "step=3&fname=Test"),
        _grp("/portal/apply", "step=4&fname=Test"),
    ]
    trunk, branch = gen._split_revisit_branch(groups)
    # NOTHING is peeled — the primary flow keeps every wizard step.
    assert branch == []
    assert len(trunk) == 4
    assert [g.url_query for g in trunk] == \
        ["step=1", "step=2&fname=Test", "step=3&fname=Test", "step=4&fname=Test"]


def test_genuine_same_url_revisit_still_splits():
    """A real side-exploration that RETURNS to the same URL (path AND query)
    is still detected — the fix narrows, it does not disable, branch peeling."""
    groups = [
        _grp("/catalog", ""),
        _grp("/catalog/item-9", ""),
        _grp("/catalog", ""),        # returned to the SAME url as groups[0]
        _grp("/cart", ""),
    ]
    trunk, branch = gen._split_revisit_branch(groups)
    assert [g.url_path for g in branch] == ["/catalog", "/catalog/item-9", "/catalog"]
    assert [g.url_path for g in trunk] == ["/catalog", "/cart"]


def test_no_revisit_returns_all_as_trunk():
    groups = [_grp("/a", ""), _grp("/b", ""), _grp("/c", "")]
    trunk, branch = gen._split_revisit_branch(groups)
    assert branch == [] and len(trunk) == 3


def test_distinct_query_on_same_path_never_false_matches():
    """Two visits to the same path with DIFFERENT queries are distinct."""
    groups = [_grp("/s", "a=1"), _grp("/x", ""), _grp("/s", "a=2")]
    trunk, branch = gen._split_revisit_branch(groups)
    assert branch == []          # /s?a=1 != /s?a=2 -> not a revisit
    assert len(trunk) == 3
