"""A field's own name= attribute must survive capture → refinement → the port.

MEASURED on parabank.parasoft.com, 2026-09-02. Its login inputs declare no
accessible name of any kind:

    <p><b>Username</b></p>
    <div class="login"><input type="text" class="input" name="username"></div>

no id, no aria-label, no aria-labelledby, no <label for>. So the port's locator
ladder — get_by_role(role, name) → get_by_label(name) → get_by_text(name) →
css_hint — matched nothing on its first two rungs and bound get_by_text, which
matched the <b>Username</b> LABEL. fill() then failed with

    Locator.fill: Element is not an <input>, <textarea> or [contenteditable]

and the crawl never authenticated: 8 public pages, the whole banking
application unseen.

css_hint cannot rescue it. For BOTH inputs it is `input.input`, so `.first`
would type the password into the username box — a wrong fill is worse than a
failed one. `[name="username"]` is unique where css_hint is not.

WHY THIS FILE EXISTS RATHER THAN A LOCATOR TEST. The first attempt at the fix
added the capture and the port rung and changed NOTHING, because build_inventory
refines the raw record into a fixed shape and silently dropped the new field on
the way through. The port read `name_attr`, found "", and skipped its own rung.
Nothing failed; nothing improved either. This pins the seam that was invisible.
"""

from __future__ import annotations

import pytest

from app.inventory import build_inventory


def _raw(**over):
    """One raw capture record shaped like ParaBank's username input."""
    rec = {
        "kind": "text", "role": "textbox", "name": "", "tag": "input",
        "input_type": "text", "css_hint": "input.input", "testid": "",
        "name_attr": "username", "options": [], "required": False,
        "disabled": False, "frame_selector": "", "value_committed": "",
        "href": "", "haspopup": "", "expanded": "", "question_label": "",
        "question_label_source": "", "landmark": {}, "filter_scope": "",
    }
    rec.update(over)
    return rec


def _controls(records):
    out = build_inventory(records)
    return list(out) if not isinstance(out, dict) else list(out.get("controls") or [])


def _find(controls):
    assert controls, "build_inventory returned nothing for a valid record"
    return controls[0]


def test_name_attr_reaches_the_port():
    """The seam that made the first fix inert."""
    ctrl = _find(_controls([_raw()]))
    qec = ctrl.get("qec") if isinstance(ctrl, dict) else getattr(ctrl, "qec", {})
    carried = (qec or {}).get("name_attr") or (
        ctrl.get("name_attr") if isinstance(ctrl, dict) else "")
    assert carried == "username", (
        "name_attr was dropped in refinement. The port reads it as a locator "
        "rung for fields with no accessible name; dropped, the rung silently "
        "never fires and the fix appears to work while doing nothing. "
        "got %r" % (carried,)
    )


def test_it_is_not_confused_with_the_accessible_name():
    """`name` is what a USER sees; `name_attr` is what the APP calls the field.

    Conflating them would make the rung fire on the wrong element for every
    control whose visible label differs from its form name — which is most of
    them.
    """
    ctrl = _find(_controls([_raw(name="Login ID")]))
    qec = (ctrl.get("qec") if isinstance(ctrl, dict) else getattr(ctrl, "qec", {})) or {}
    assert qec.get("name_attr") == "username"
    accessible = ctrl.get("name") if isinstance(ctrl, dict) else getattr(ctrl, "name", "")
    assert accessible == "Login ID"


@pytest.mark.parametrize("tag", ["div", "span", "a", "button"])
def test_only_form_controls_carry_one(tag):
    """CONTROL — a non-field must not acquire a name_attr rung.

    Without this the guard could be satisfied by capturing `name` off anything,
    and the port would start binding links and buttons by an attribute that
    means something else on them.
    """
    ctrl = _find(_controls([_raw(tag=tag, name_attr="", kind="button", role="button",
                                 name="Log In")]))
    qec = (ctrl.get("qec") if isinstance(ctrl, dict) else getattr(ctrl, "qec", {})) or {}
    assert not qec.get("name_attr")


def test_a_field_without_one_is_unchanged():
    """Most applications label their fields properly; they must not move."""
    ctrl = _find(_controls([_raw(name="Email address", name_attr="")]))
    qec = (ctrl.get("qec") if isinstance(ctrl, dict) else getattr(ctrl, "qec", {})) or {}
    assert qec.get("name_attr", "") == ""
    assert (ctrl.get("name") if isinstance(ctrl, dict) else "") == "Email address"
