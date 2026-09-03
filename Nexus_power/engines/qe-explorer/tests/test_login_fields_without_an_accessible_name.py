"""A login field with no accessible name must still be findable and fillable.

MEASURED on parabank.parasoft.com, 2026-09-02 — the first third-party
application crawled here. Its login inputs declare no accessible name at all:

    <p><b>Username</b></p>
    <div class="login"><input type="text" class="input" name="username"></div>
    <p><b>Password</b></p>
    <div class="login"><input type="password" class="input" name="password"></div>

no id, no aria-label, no aria-labelledby, no <label for>. The crawl reported

    qec.auth.login_attempt success=False reason=login_unverified
    ... no login form driven at the entry; exploring UNAUTHENTICATED

and saw 8 public marketing pages. The entire banking application — accounts,
transfers, bill pay, loan requests — went unseen, with valid credentials
configured and the form sitting on the entry page.

THE RULE THAT CAUSED IT IS A GOOD ONE. A control with no accessible name is
skipped everywhere in this crawler (``qec.forms.skip_nameless_field``), because
a nameless field would be catalogued as a question nobody can read. The login
form is the one place that is fatal, and the PASSWORD field masked it: type=
"password" is a structural signal, so half the form was always found and the
failure looked like "the login just doesn't work here".

So a form control's own name= attribute counts as identity in the AUTH path,
and only there. It is what the application calls the field, it is stable, and it
is what the port now binds its locator on. The question catalogue is untouched.

WHY THE LAST TEST MATTERS MOST. name_attr is deliberately NOT in the ``qec``
bucket: that bucket rides verbatim into every action record, so carrying it
there added a key to the manifest and moved all four characterization goldens —
for a field the evidence has no use for. Worse, those goldens were dirty with a
colleague's uncommitted work, so re-recording them would have shipped their
changes under this one's name.
"""

from __future__ import annotations

import pytest

from app.auth import (DEFAULT_USERNAME_HINTS, _auth_identifiable,
                      _match_password_control, _match_username_control,
                      _text_fields)


def _field(kind="text", name="", name_attr="", input_type="text"):
    return {"kind": kind, "name": name, "name_attr": name_attr, "role": "textbox",
            "qec": {"input_type": input_type, "role": "textbox"}}


PARABANK = [
    _field(name_attr="username"),
    _field(name_attr="password", input_type="password"),
]
LABELLED = [
    _field(name="Username"),
    _field(name="Password", input_type="password"),
]


def test_a_nameless_field_with_a_name_attribute_is_findable():
    """The measured failure: this returned None and the crawl never logged in."""
    hit = _match_username_control(PARABANK, DEFAULT_USERNAME_HINTS)
    assert hit is not None, (
        "a login field with name='username' and no accessible name must be "
        "findable, or an application with old-style markup can never be "
        "authenticated into"
    )
    assert hit.get("name_attr") == "username"


def test_the_hint_still_picks_the_right_one():
    """Not a positional guess: 'username' identifies it as the username field."""
    controls = [_field(name_attr="csrf_token"), _field(name_attr="username")]
    assert _match_username_control(controls, DEFAULT_USERNAME_HINTS).get(
        "name_attr") == "username"


def test_the_fill_branch_accepts_it():
    """Matching it is not enough — the fill branch refused it separately.

    Both gates had to open: _text_fields admitted the field, and then the branch
    guard still required an accessible name. Fixing only one changed nothing,
    which is exactly what the first two attempts at this looked like.
    """
    assert _auth_identifiable(_field(name_attr="username")) is True
    assert _auth_identifiable(_field(name="Username")) is True
    assert _auth_identifiable(_field()) is False
    assert _auth_identifiable(None) is False


def test_a_field_with_neither_is_still_skipped():
    """CONTROL — the nameless-field rule must survive for fields that ARE nameless.

    Without this the change reads as "fill anything", and the honesty rule it
    carves an exception out of would be gone rather than narrowed.
    """
    assert _text_fields([_field()]) == []
    assert _match_username_control([_field()], DEFAULT_USERNAME_HINTS) is None


def test_a_properly_labelled_form_is_unaffected():
    """Most applications label their fields; none of them may move."""
    assert _match_username_control(LABELLED, DEFAULT_USERNAME_HINTS).get("name") == "Username"
    assert _match_password_control(LABELLED) is not None


def test_name_attr_is_not_in_the_serialised_bucket():
    """It must never reach the manifest.

    `qec` rides verbatim into every action record. Carrying name_attr there
    moved all four characterization goldens for a field the evidence has no use
    for — and those goldens were dirty with someone else's uncommitted work, so
    re-recording them would have committed their changes under this fix's name.
    """
    from app.inventory import build_inventory
    raw = {"kind": "text", "role": "textbox", "name": "", "tag": "input",
           "input_type": "text", "css_hint": "input.input", "testid": "",
           "name_attr": "username", "options": [], "required": False,
           "disabled": False, "frame_selector": "", "value_committed": "",
           "href": "", "haspopup": "", "expanded": "", "question_label": "",
           "question_label_source": "", "landmark": {}, "filter_scope": ""}
    out = build_inventory([raw])
    controls = list(out) if not isinstance(out, dict) else list(out.get("controls") or [])
    assert controls, "build_inventory returned nothing"
    c = controls[0]
    assert (c.get("qec") or {}).get("name_attr") in (None, ""), (
        "name_attr must NOT be in the qec bucket — it would be serialised into "
        "every action record and move every golden"
    )
    assert c.get("name_attr") == "username", "the port reads it from the top level"
