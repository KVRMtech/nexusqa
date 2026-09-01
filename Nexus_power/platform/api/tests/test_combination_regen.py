"""V3 regression — combination regeneration honesty rails.

The class (live incident, scenario 31d06925 step 9, 2026-07-25): a combination
sets an option-captured axis on a field ABSENT from the form state its other
values produce. Heal correctly refuses; the truthful repair is dropping the
absent axis — with the reduction LABELED. These tests pin the rails:

  * only ``available``-provenance steps may be dropped — NEVER demonstrated;
  * only combination-type cases are eligible;
  * steps renumber; the NAME loses the dropped fragment (no advertised
    coverage the case no longer has); description + tag record the drop and
    the open design-vs-application question.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_combination_regen.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_MOD = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory",
    "combination_regen.py")
_spec = importlib.util.spec_from_file_location("nexus_combo_regen_ut", _MOD)
cr = importlib.util.module_from_spec(_spec)
sys.modules["nexus_combo_regen_ut"] = cr
_spec.loader.exec_module(cr)


_NAME = ("Rank 1 — Verify user can complete the flow with State=NY, "
         "Relationship=Child, Product=Heritage Whole Life, Gender=Female")


def _case():
    return {
        "description": "Critical-combination variant of the demonstrated flow.",
        "tags": ["combination", "available"],
        "steps": [
            {"step_number": 1, "action": "Open the app",
             "provenance": "demonstrated", "observed": {"verb": "navigate"}},
            {"step_number": 2, "action": "Select 'NY'",
             "provenance": "available",
             "observed": {"verb": "select", "label": "State", "value": "NY"}},
            {"step_number": 3, "action": "Enter 'Child' in the 'Relationship' field",
             "provenance": "available",
             "observed": {"verb": "type", "label": "Relationship", "value": "Child"}},
            {"step_number": 4, "action": "Click 'Continue'",
             "provenance": "demonstrated",
             "observed": {"verb": "click", "label": "Continue"}},
        ],
    }


def test_drops_available_axis_renumbers_and_relabels():
    r = cr.drop_absent_axis(
        test_case=_case(), name=_NAME, test_type="combination",
        step_number=3, diagnosed_at="2026-07-25")
    assert r is not None
    # step gone + renumbered
    actions = [s["action"] for s in r.test_case["steps"]]
    assert actions == ["Open the app", "Select 'NY'", "Click 'Continue'"]
    assert [s["step_number"] for s in r.test_case["steps"]] == [1, 2, 3]
    assert r.step_count == 3
    # honest name: the dropped fragment is gone, the rest intact
    assert "Relationship=Child" not in r.name
    assert "State=NY" in r.name and "Product=Heritage Whole Life" in r.name
    assert ",," not in r.name and not r.name.rstrip().endswith(",")
    # the drop is labeled, and the open question is named
    assert "dropped by auto-recovery" in r.test_case["description"]
    assert "APPLICATION finding" in r.test_case["description"]
    assert r.tag.startswith("axis-dropped:relationship")
    assert r.tag in r.test_case["tags"]
    assert (r.dropped_label, r.dropped_value) == ("Relationship", "Child")


def test_never_drops_a_demonstrated_step():
    """A demonstrated step failing is a REAL signal — untouchable here."""
    assert cr.drop_absent_axis(
        test_case=_case(), name=_NAME, test_type="combination",
        step_number=4, diagnosed_at="2026-07-25") is None
    assert cr.drop_absent_axis(
        test_case=_case(), name=_NAME, test_type="combination",
        step_number=1, diagnosed_at="2026-07-25") is None


def test_only_combination_cases_are_eligible():
    for t in ("functional", "negative", "", None):
        assert cr.drop_absent_axis(
            test_case=_case(), name=_NAME, test_type=t,
            step_number=3, diagnosed_at="2026-07-25") is None


def test_out_of_range_step_refused():
    assert cr.drop_absent_axis(
        test_case=_case(), name=_NAME, test_type="combination",
        step_number=99, diagnosed_at="2026-07-25") is None
    assert cr.drop_absent_axis(
        test_case=_case(), name=_NAME, test_type="combination",
        step_number=0, diagnosed_at="2026-07-25") is None


def test_name_without_matching_fragment_still_transforms_steps():
    r = cr.drop_absent_axis(
        test_case=_case(), name="Rank 9 — Verify user can complete the flow",
        test_type="combination", step_number=2, diagnosed_at="2026-07-25")
    assert r is not None
    assert r.step_count == 3
    assert r.name == "Rank 9 — Verify user can complete the flow"


def test_driver_integration_is_wired():
    """Source contract: the auto-heal driver's honest-stop branches invoke the
    regeneration fallback and re-certify (the ring is closed in code)."""
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "test_factory.py"),
        encoding="utf-8").read()
    assert "drop_absent_axis" in src
    assert "_regenerate_combination" in src
    # regen re-proves through certification like every other repair
    seg = src.split("async def _regenerate_combination", 1)[-1]
    assert "_spawn_certification" in seg
