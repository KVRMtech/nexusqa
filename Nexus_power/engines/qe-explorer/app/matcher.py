"""Control → Interaction-Primitive MATCHER registry — the extensibility keystone.

Instead of reactive per-widget code scattered through the crawler, a control's accessibility
SIGNATURE (kind / role / tag / options) is run through an ORDERED list of pure rules, each
yielding the reversible PRIMITIVE that captures it. A NEW widget becomes ONE new rule mapping
its signature to an EXISTING primitive — the crawler control-flow never changes, and because
the rules key on the a11y tree, one rule covers that pattern across MUI / AntD / Vuetify /
Radix / Flutter-semantics at once. A control no rule covers resolves to UNHANDLED — NAMED in
the coverage ledger, never a silent skip.

Primitives (see the interaction-architecture master plan):
  READ_STATIC     a value already readable (text / number / date / checkbox / toggle /
                  native-select-with-options / custom-select whose options were captured)
  OPEN_READ       a custom choice whose options appear only on open (combobox open-probe)
  GROUP_ASSEMBLE  a radio / segmented group to assemble into one choice (roadmap P1.1)
  RANGE_SET       a slider / color to set + read (roadmap RANGE-SET-READ)
  NONE            not a value control (button / link / submit / tab)
  UNHANDLED       an interactive control no rule yet covers — named, on the roadmap
"""
from __future__ import annotations

from collections.abc import Mapping

READ_STATIC = "read-static"
OPEN_READ = "open-read-dismiss"
GROUP_ASSEMBLE = "group-assemble"
RANGE_SET = "range-set-read"
NONE = "none"
UNHANDLED = "unhandled"

_NON_FIELD_KINDS = frozenset({"button", "link"})
_NON_FIELD_ROLES = frozenset({"button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab"})
_VALUE_KINDS = frozenset({"text", "date", "email", "number", "tel", "url", "password", "textarea"})
# Kinds/roles that DRIVE a reactive form change (their act can reveal dependent options/fields).
_DRIVER_KINDS = frozenset({"select", "radio", "checkbox", "toggle"})


def _s(c: Mapping, key: str) -> str:
    return str(c.get(key) or "").strip().lower()


def is_drag_drop(control: Mapping) -> bool:
    """A drag-and-drop control (HTML5 ``draggable`` or an ARIA grab/drop role).
    No interaction primitive exists yet — we NAME it as a blind spot rather than
    silently skip it (the requirements-audit honesty leak)."""
    if bool(control.get("draggable")):
        return True
    role = _s(control, "role")
    rd = _s(control, "roledescription")
    return role in ("application",) and "drag" in rd or "drag" in rd or "sortable" in rd


def primitive_for(control: Mapping) -> str:
    """The interaction primitive that captures ``control`` — first matching rule wins.
    Adding a widget = insert one rule here mapping its signature to an existing primitive."""
    kind = _s(control, "kind")
    role = _s(control, "role")
    tag = _s(control, "tag")
    has_options = bool(control.get("options"))

    # 0) Drag-and-drop — no primitive yet; ALWAYS named UNHANDLED for the ledger
    # (a draggable button is still a blind spot, so this precedes the affordance
    # rule).
    if is_drag_drop(control):
        return UNHANDLED
    # 1) Non-value affordances — captured by name only, no field probe.
    if kind in _NON_FIELD_KINDS or role in _NON_FIELD_ROLES or tag in ("button", "a", "summary"):
        return NONE
    # 2) Radio / segmented group — assemble into one logical choice.
    if kind == "radio":
        return GROUP_ASSEMBLE
    # 3) Boolean — set + read back.
    if kind in ("checkbox", "toggle"):
        return READ_STATIC
    # 4) Slider / color — keyboard set + aria-value read.
    if kind in ("slider", "color", "range"):
        return RANGE_SET
    # 5) Choice controls.
    if kind == "select" or role in ("combobox", "listbox") or tag == "select":
        # options already captured (native <select>, or a custom combobox we open-probed)
        # read statically; a custom choice with no options still needs the open-probe.
        return READ_STATIC if has_options else OPEN_READ
    # 6) Recognised value fields.
    if kind in _VALUE_KINDS or role in ("textbox", "searchbox", "spinbutton") or tag == "textarea":
        return READ_STATIC
    # 7) An interactive control no rule covers — named for the ledger, not silently skipped.
    return UNHANDLED


def needs_open_probe(control: Mapping) -> bool:
    """A CUSTOM choice whose options must be read by OPENING it (the combobox open-probe).
    A native ``<select>`` is excluded — its options are read statically and its browser-native
    popup isn't DOM-readable."""
    if primitive_for(control) != OPEN_READ:
        return False
    return _s(control, "tag") != "select" and not control.get("disabled") and not control.get("danger")


def is_diff_driver(control: Mapping) -> bool:
    """A control whose act can reveal a DEPENDENT field/options (the ACT-THEN-DIFF drivers):
    a choice with options, a radio, or a boolean toggle — and only when safe/enabled."""
    kind = _s(control, "kind")
    if kind not in _DRIVER_KINDS or control.get("disabled") or control.get("danger"):
        return False
    if kind == "select" and not control.get("options"):
        return False  # can't drive from a choice whose own options we couldn't read
    return True


def is_unhandled_field(control: Mapping) -> bool:
    """An interactive/value-ish control the registry does NOT yet capture — surfaced to the
    coverage ledger as an honest UNHANDLED row rather than a silent gap. Excludes plain
    non-value affordances (buttons/links) which are not fields."""
    return primitive_for(control) == UNHANDLED and bool((control.get("name") or "").strip())


def is_unhandled(control: Mapping) -> bool:
    """Any UNHANDLED control — INCLUDING a nameless one. The old name-required
    gate silently dropped nameless unsupported controls from the ledger (a
    requirements-audit honesty leak); the crawler synthesizes a positional label
    for these so 'never a silent skip' actually holds."""
    return primitive_for(control) == UNHANDLED


def unhandled_reason(control: Mapping) -> str:
    """A short honest reason for the ledger row."""
    return "drag-drop (no interaction primitive yet)" if is_drag_drop(control) \
        else "interactive control kind not handled yet"
