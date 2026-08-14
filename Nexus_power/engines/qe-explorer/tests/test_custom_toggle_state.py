"""A CUSTOM TOGGLE HOLDS ITS STATE IN ARIA (inv-js v9).

The same hole v8 closed for choice triggers, one widget class on. A
``<button role="radio">`` or ``role="checkbox"`` is not a form element, so
``valueCommitted`` fell through every branch and returned "" — the control's
state existed in the DOM and nothing captured reflected it.

The consequences are the ones v8 already demonstrated, and they are worth
restating because this class is even more common:

  * the form_snapshot reads back EMPTY for a control a human has plainly
    selected, so the snapshot lies about the state of the form;
  * an automated fill cannot be verified, so a correct selection is discarded.

Answered from the W3C ARIA specification (aria-checked / aria-pressed) rather
than from any one component library's markup — which is what makes it a fleet
capability instead of a patch. Radix, MUI, Headless UI, Ant and hand-rolled
widgets all set these attributes because assistive technology requires it.

The mirror to the native branch is deliberate: a custom toggle and an
``<input type="checkbox">`` must be indistinguishable downstream, because the
fill, the snapshot and the catalogue should not care which one an application
happened to use.
"""
from __future__ import annotations

import pytest

from app.inventory_js import INVENTORY_JS


def test_the_aria_state_branch_exists_and_mirrors_the_native_one():
    assert 'lc(attr(el, "aria-checked"))' in INVENTORY_JS
    assert 'lc(attr(el, "aria-pressed"))' in INVENTORY_JS


@pytest.mark.parametrize("role", ["radio", "checkbox", "switch",
                                  "menuitemcheckbox", "menuitemradio"])
def test_every_custom_toggle_role_is_covered(role):
    """A role list that omits one leaves that widget class silently unreadable —
    which is exactly how the combobox gap survived for so long."""
    assert f'role === "{role}"' in INVENTORY_JS


def test_a_tri_state_checkbox_is_not_forced_into_a_boolean():
    """``mixed`` is a real ARIA value. Reporting it as true or false would be a
    fabricated answer about a control the application deliberately left
    indeterminate."""
    assert 'ariaState === "mixed"' in INVENTORY_JS
    assert 'return "mixed"' in INVENTORY_JS


def test_an_absent_aria_state_reports_nothing_rather_than_false():
    """A button with no aria state is not an unchecked toggle — it is not a
    toggle at all, and saying "false" would invent a control that does not
    exist."""
    js = INVENTORY_JS
    branch = js[js.index("var ariaState"):js.index('if (role === "combobox"')]
    assert 'return "";' in branch, (
        "an absent aria state must fall through to no value, never to false")


def test_the_choice_trigger_branch_still_follows():
    """v8 must remain reachable: a combobox trigger is not a toggle, and the
    toggle branch must not swallow it."""
    js = INVENTORY_JS
    assert js.index("var ariaState") < js.index('if (role === "combobox"')
    assert 'data-placeholder' in js
