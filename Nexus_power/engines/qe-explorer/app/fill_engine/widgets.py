"""WHAT KIND OF WIDGET IS THIS, AND HOW IS IT DRIVEN?

The old engine had two implicit widget classes — "things Playwright's ``fill``
works on" and "everything else" — and everything else was skipped.  That is why
a radio group and a portal-rendered combobox, between them the two most common
controls in enterprise software, needed an operator to change posture before the
crawl would answer them.

Making them ordinary means naming them.  A control is classified ONCE, from what
the application declared about it, and the class carries the two facts a caller
needs:

    ``primitive``  the browser verb that drives it (``fill`` / ``select_option``
                   / ``set_checked`` / ``open_and_pick``);
    ``answerable`` whether the crawl can answer it at all, so an UNHANDLED
                   widget is a named blind spot rather than a silent skip.

WHY CLASSIFICATION IS EVIDENCE-BASED AND NOT OPTIMISTIC.  ``_is_open_choice`` in
:mod:`app.forms` already learned this the hard way: treating an unknown tag as a
custom widget routed ordinary ``<select>`` elements into the open-and-pick path
and broke fills that had always worked.  So a custom widget must DECLARE itself —
an explicit non-``select`` tag, or an ARIA role that only a scripted widget
carries — and an unknown control keeps the native path.

PURE + DETERMINISTIC.  No browser, no I/O.  The driving itself lives in
:mod:`app.forms`; this module only says what to drive it with, which is what
makes the decision testable without a browser.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "WidgetClass", "classify_widget", "SUPPORTED_PRIMITIVES",
    "PRIM_FILL", "PRIM_SELECT_OPTION", "PRIM_SET_CHECKED", "PRIM_OPEN_AND_PICK",
    "PRIM_NONE",
]

PRIM_FILL = "fill"
PRIM_SELECT_OPTION = "select_option"
PRIM_SET_CHECKED = "set_checked"
PRIM_OPEN_AND_PICK = "open_and_pick"
PRIM_NONE = ""

SUPPORTED_PRIMITIVES = frozenset({
    PRIM_FILL, PRIM_SELECT_OPTION, PRIM_SET_CHECKED, PRIM_OPEN_AND_PICK,
})

#: ARIA roles that only a SCRIPTED choice widget carries.  A ``<select>`` never
#: reports these, so their presence is positive evidence that the browser's
#: ``selectOption`` primitive cannot drive this control and it must be opened.
_SCRIPTED_CHOICE_ROLES = frozenset({
    "combobox", "listbox", "menu", "menubar",
})
#: Roles that identify a widget as a scripted TOGGLE — driven by a click, with
#: state read from ``aria-checked``/``aria-pressed`` rather than ``.checked``.
_SCRIPTED_TOGGLE_ROLES = frozenset({"switch", "checkbox", "radio", "menuitemcheckbox",
                                    "menuitemradio"})

#: Widget names, so a metric can be reported per class and a gate can hold a
#: class at zero.  ``coverage`` in the quality report is counted over these.
W_TEXT = "text"
W_NATIVE_SELECT = "native_select"
W_RADIO_GROUP = "radio_group"
W_CHECKBOX_GROUP = "checkbox_group"
W_TOGGLE = "toggle"
W_ARIA_COMBOBOX = "aria_combobox"
W_ARIA_LISTBOX = "aria_listbox"
W_SEARCHABLE_SELECT = "searchable_select"
W_SLIDER = "slider"
W_COLOR = "color"
W_FILE = "file"
W_UNHANDLED = "unhandled"


@dataclass(frozen=True)
class WidgetClass:
    """What this control is, and the verb that operates it."""

    name: str
    primitive: str
    #: True when this widget's answer is a CHOICE among enumerated options — the
    #: fork in a business flow that the journey graph records as a decision.
    enumerable: bool = False
    #: True when the enumeration only exists while the widget is OPEN, so the
    #: options must be read at fill time rather than from the static inventory.
    options_on_open: bool = False
    #: True when the widget belongs to a GROUP that is one question with N
    #: answers, so exactly one member is filled and the rest are its siblings.
    grouped: bool = False
    #: Why this classification — carried so a skip can always be explained.
    basis: str = ""

    @property
    def answerable(self) -> bool:
        return self.primitive in SUPPORTED_PRIMITIVES

    def as_dict(self) -> dict[str, Any]:
        return {"widget": self.name, "primitive": self.primitive,
                "enumerable": self.enumerable,
                "options_on_open": self.options_on_open, "grouped": self.grouped}


def _norm(value: Any) -> str:
    return " ".join(("" if value is None else str(value)).split()).lower()


def _qec(control: Mapping[str, Any]) -> Mapping[str, Any]:
    q = control.get("qec")
    return q if isinstance(q, Mapping) else {}


def _attr(control: Mapping[str, Any], *keys: str) -> str:
    q = _qec(control)
    for key in keys:
        v = control.get(key)
        if v in (None, ""):
            v = q.get(key)
        if v not in (None, ""):
            return _norm(v)
    return ""


def classify_widget(control: Mapping[str, Any], *, kind: str = "") -> WidgetClass:
    """Decide the widget class and the primitive that drives it."""
    k = _norm(kind) or _norm(control.get("kind"))
    tag = _attr(control, "tag")
    role = _attr(control, "role")
    input_type = _attr(control, "input_type", "type")
    grouped = bool(_norm(control.get("group_id")))
    has_options = bool(control.get("options") or control.get("group_options"))

    if input_type == "file":
        return WidgetClass(W_FILE, PRIM_NONE, basis="input_type=file")

    if k == "radio":
        # A RADIO GROUP IS AN ORDINARY WIDGET.  It is one question with N
        # answers; the member that IS the answer is checked and the browser
        # enforces exclusivity over its siblings.
        return WidgetClass(W_RADIO_GROUP, PRIM_SET_CHECKED, enumerable=True,
                           grouped=True, basis="kind=radio")

    if k == "checkbox":
        if grouped:
            return WidgetClass(W_CHECKBOX_GROUP, PRIM_SET_CHECKED, enumerable=True,
                               grouped=True, basis="kind=checkbox + group_id")
        return WidgetClass(W_TOGGLE, PRIM_SET_CHECKED, enumerable=True,
                           basis="kind=checkbox, ungrouped")

    if k == "toggle":
        return WidgetClass(W_TOGGLE, PRIM_SET_CHECKED, enumerable=True,
                           basis=f"kind=toggle role={role or 'none'}")

    if k == "select":
        if tag == "select":
            return WidgetClass(W_NATIVE_SELECT, PRIM_SELECT_OPTION, enumerable=True,
                               basis="tag=select")
        # A DECLARED CUSTOM WIDGET.  Requires positive evidence — an explicit
        # non-select tag or a scripted-choice ARIA role — because assuming
        # "unknown means custom" routed ordinary selects into the open-pick path
        # and broke fills that had always worked.
        if role in _SCRIPTED_CHOICE_ROLES or (tag and tag != "select"):
            searchable = bool(_attr(control, "haspopup")) and input_type in ("text", "search")
            if searchable or (tag == "input" and role == "combobox"):
                # A combobox rendered as a text input filters its list as you
                # type: it is opened and picked like the others, but it will also
                # accept typed text, so the primitive is the same and the caller
                # may type first.
                return WidgetClass(W_SEARCHABLE_SELECT, PRIM_OPEN_AND_PICK,
                                   enumerable=True, options_on_open=True,
                                   basis=f"tag={tag} role={role} input_type={input_type}")
            name = W_ARIA_LISTBOX if role == "listbox" else W_ARIA_COMBOBOX
            return WidgetClass(name, PRIM_OPEN_AND_PICK, enumerable=True,
                               options_on_open=not has_options,
                               basis=f"tag={tag or 'none'} role={role or 'none'}")
        return WidgetClass(W_NATIVE_SELECT, PRIM_SELECT_OPTION, enumerable=True,
                           basis="kind=select, no custom-widget evidence")

    if k == "slider":
        if tag == "input" or input_type == "range":
            return WidgetClass(W_SLIDER, PRIM_FILL, basis="native range input")
        # A CUSTOM slider needs the keyboard set-range verb, which this crawl
        # does not have.  Named, not silently skipped.
        return WidgetClass(W_UNHANDLED, PRIM_NONE,
                           basis="custom slider needs a keyboard set-range verb")

    if k == "color":
        if tag == "input" or input_type == "color":
            return WidgetClass(W_COLOR, PRIM_FILL, basis="native color input")
        return WidgetClass(W_UNHANDLED, PRIM_NONE, basis="custom colour picker")

    if k in ("text", "date"):
        if role in _SCRIPTED_CHOICE_ROLES and tag != "input":
            return WidgetClass(W_ARIA_COMBOBOX, PRIM_OPEN_AND_PICK, enumerable=True,
                               options_on_open=not has_options,
                               basis=f"role={role} on a non-input tag")
        return WidgetClass(W_TEXT, PRIM_FILL, basis=f"kind={k}")

    if control.get("draggable"):
        return WidgetClass(W_UNHANDLED, PRIM_NONE,
                           basis="drag-and-drop needs a gesture verb")

    return WidgetClass(W_UNHANDLED, PRIM_NONE, basis=f"unclassified kind={k!r}")
