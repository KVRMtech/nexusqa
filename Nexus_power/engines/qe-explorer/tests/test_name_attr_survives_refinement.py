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


def _name_attr(ctrl):
    """Where the value actually lives: the control's TOP LEVEL.

    Deliberately NOT the ``qec`` bucket. That bucket is serialised verbatim into
    every action record, so carrying it there moved all four characterization
    goldens for a field the evidence has no use for (see
    test_login_fields_without_an_accessible_name). Reading it here through one
    helper keeps every assertion in this file honest about the location, instead
    of an ``or`` fallback that passes whichever side happens to hold it.
    """
    return (ctrl.get("name_attr") if isinstance(ctrl, dict)
            else getattr(ctrl, "name_attr", "")) or ""


def _qec(ctrl):
    return (ctrl.get("qec") if isinstance(ctrl, dict)
            else getattr(ctrl, "qec", {})) or {}


def test_name_attr_reaches_the_port():
    """The seam that made the first fix inert."""
    ctrl = _find(_controls([_raw()]))
    carried = _name_attr(ctrl)
    assert not _qec(ctrl).get("name_attr"), (
        "name_attr must stay OUT of the serialised qec bucket, or every "
        "characterization golden moves for a field the evidence never reads")
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
    assert _name_attr(ctrl) == "username"
    accessible = ctrl.get("name") if isinstance(ctrl, dict) else getattr(ctrl, "name", "")
    assert accessible == "Login ID"


@pytest.mark.parametrize("tag", ["div", "span", "a", "button"])
def test_only_form_controls_carry_one(tag):
    """CONTROL — refinement must not INVENT a name_attr.

    Scoped honestly to what this layer can prove. build_inventory carries the
    field through; it does not gate on tag, so a button handed a name_attr in
    its raw record keeps it (measured). The tag gate is in the CAPTURE, and
    test_the_capture_gates_the_tag below pins it there rather than here.

    What this does pin: given a raw record with no name_attr, refinement must
    not manufacture one out of `name` — which would hand the port a rung that
    binds links and buttons by an attribute meaning something else on them.
    """
    ctrl = _find(_controls([_raw(tag=tag, name_attr="", kind="button", role="button",
                                 name="Log In")]))
    assert _name_attr(ctrl) == "", (
        "a link or button must not acquire a name_attr rung - on those "
        "elements name= means something else entirely")


def test_a_field_without_one_is_unchanged():
    """Most applications label their fields properly; they must not move."""
    ctrl = _find(_controls([_raw(name="Email address", name_attr="")]))
    assert _name_attr(ctrl) == ""
    assert (ctrl.get("name") if isinstance(ctrl, dict) else "") == "Email address"


def test_the_capture_gates_the_tag():
    """The tag restriction lives in the JS capture, so it is asserted there.

    Pinned at the source because the Python layer above cannot see it: by the
    time build_inventory runs, a non-field simply arrives with name_attr="" and
    an assertion here would pass whether the gate existed or not — the shape of
    blind verifier this repository keeps finding.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "inventory_js.py"
    text = src.read_text(encoding="utf-8")
    assert 'name_attr: lc(el.tagName) === "input"' in text, (
        "the capture no longer gates name_attr on the tag; a link or button "
        "would acquire a rung bound to an attribute that means something else")
    for tag in ('"select"', '"textarea"'):
        assert 'lc(el.tagName) === %s' % tag in text, (
            "%s must keep its name_attr - a nameless <%s> is exactly the case "
            "the ParaBank fix exists for" % (tag, tag.strip('"')))
