"""Every control was being reported as drag-and-drop, so the crawler clicked nothing.

THE DEFECT (found on a client crawl of /portal/claims/new): the crawl landed on its
entry page, read the form, and stopped — one visit, and every nav link ledgered as
"drag-drop (no interaction primitive yet)". The client reported that none of the
flow had been recorded.

The cause is one absence test in the capture JS. `attr()` returns "" for a missing
attribute (`getAttribute(n) || ""`), so `!== null && !== undefined` was true for
EVERY element. That set draggable=true everywhere, and the matcher's drag rule runs
BEFORE the affordance rule — so links, buttons and fields all resolved to UNHANDLED
and the crawler refused to interact with any of them.
"""
import re
from pathlib import Path

from app.matcher import NONE, READ_STATIC, UNHANDLED, is_drag_drop, primitive_for

_JS = Path(__file__).resolve().parents[1] / "app" / "inventory_js.py"


# ── the capture: absence must be tested against "" ───────────────────────────

def test_the_absence_test_compares_against_empty_string_not_null():
    """attr() can never return null or undefined, so comparing against them is
    always true and marks every element draggable."""
    src = _JS.read_text(encoding="utf-8")
    m = re.search(r'draggable:\s*(.+?),\n\s*roledescription', src, re.S)
    assert m, "the draggable capture moved — re-check this test"
    expr = m.group(1)
    assert '!== null' not in expr
    assert '!== undefined' not in expr
    assert '!== ""' in expr


def test_attr_still_returns_empty_string_for_a_missing_attribute():
    """The premise of the fix. If attr() ever starts returning null, the comparison
    above has to change with it."""
    src = _JS.read_text(encoding="utf-8")
    assert 'return el.getAttribute(name) || "";' in src


def test_the_js_expression_is_false_for_an_ordinary_element():
    """Simulate attr()'s real behaviour for an element with neither attribute."""
    attr = lambda name: ""                      # noqa: E731 - mirrors the JS helper
    draggable = (attr("draggable") == "true") or (attr("aria-grabbed") != "")
    assert draggable is False

    # and still TRUE for controls that really are drag-and-drop
    for d, g in (("true", ""), ("", "false"), ("true", "true")):
        attr2 = lambda name, _d=d, _g=g: _d if name == "draggable" else _g  # noqa: E731
        assert (attr2("draggable") == "true") or (attr2("aria-grabbed") != "")


# ── the consequence: a nav link is clickable again ───────────────────────────

def test_a_plain_nav_link_is_a_normal_affordance():
    link = {"kind": "link", "name": "Dashboard", "tag": "a", "role": "link",
            "draggable": False}
    assert is_drag_drop(link) is False
    assert primitive_for(link) == NONE


def test_ordinary_controls_are_not_swallowed_by_the_drag_rule():
    for control, expected in (
        ({"kind": "text", "name": "Claimant name"}, READ_STATIC),
        ({"kind": "select", "name": "Claim type", "options": ["Auto"]}, READ_STATIC),
        ({"kind": "button", "name": "Submit claim"}, NONE),
        ({"kind": "link", "name": "My policies"}, NONE),
    ):
        assert primitive_for(dict(control, draggable=False)) == expected


def test_a_REAL_drag_control_is_still_named_unhandled():
    """The honesty rule this code exists for must survive the fix: a genuine
    drag-and-drop widget is NAMED in the ledger, never silently skipped."""
    assert primitive_for({"kind": "div", "name": "Reorder", "draggable": True}) == UNHANDLED
    assert primitive_for({"kind": "div", "name": "Row", "roledescription": "sortable"}) == UNHANDLED
    assert primitive_for({"kind": "div", "name": "Row", "roledescription": "drag handle"}) == UNHANDLED


def test_the_dead_role_clause_is_gone_but_behaviour_is_unchanged():
    """`role in (...) and X or X or Y` reduced to `X or Y` — the role check never
    ran. Same verdicts, stated honestly."""
    assert is_drag_drop({"roledescription": "drag handle"}) is True
    assert is_drag_drop({"role": "application", "roledescription": "drag handle"}) is True
    assert is_drag_drop({"role": "application", "roledescription": "plain"}) is False
    assert is_drag_drop({"roledescription": ""}) is False
