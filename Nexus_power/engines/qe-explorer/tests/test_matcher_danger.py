"""Matcher registry + locale-safe danger gate — the extensibility keystone and the safety
gate for anything the crawler actuates.
"""
from app import danger_signals as ds
from app import matcher as m


# ── Matcher registry: control signature → interaction primitive ────────────────
def test_choice_with_options_reads_static_without_options_needs_open_probe():
    assert m.primitive_for({"kind": "select", "options": ["A", "B"]}) == m.READ_STATIC
    assert m.primitive_for({"kind": "select", "role": "combobox", "options": []}) == m.OPEN_READ
    assert m.needs_open_probe({"kind": "select", "role": "combobox", "options": []}) is True
    # native <select> is NOT open-probed (options static; native popup unreadable)
    assert m.needs_open_probe({"kind": "select", "tag": "select", "options": []}) is False


def test_radio_slider_button_map_to_expected_primitives():
    assert m.primitive_for({"kind": "radio"}) == m.GROUP_ASSEMBLE
    assert m.primitive_for({"kind": "slider"}) == m.RANGE_SET
    assert m.primitive_for({"kind": "checkbox"}) == m.READ_STATIC
    assert m.primitive_for({"kind": "text"}) == m.READ_STATIC
    assert m.primitive_for({"kind": "button"}) == m.NONE
    assert m.primitive_for({"kind": "link"}) == m.NONE


def test_unknown_interactive_control_is_unhandled_named_not_silent():
    # A control kind no rule covers → UNHANDLED (so the ledger names it), but only when it
    # carries a label (a nameless non-field is not a ledger row).
    assert m.primitive_for({"kind": "grid", "role": "grid"}) == m.UNHANDLED
    assert m.is_unhandled_field({"kind": "grid", "role": "grid", "name": "Data grid"}) is True
    assert m.is_unhandled_field({"kind": "grid", "role": "grid", "name": ""}) is False


def test_drag_drop_is_named_unhandled_not_silently_skipped():
    """R1 honesty leak: drag-drop had no primitive AND no ledger row. Now every
    drag-drop signal → UNHANDLED with a reason, even on a draggable button."""
    assert m.primitive_for({"kind": "button", "draggable": True}) == m.UNHANDLED
    assert m.primitive_for({"role": "listitem", "draggable": True}) == m.UNHANDLED
    assert m.primitive_for(
        {"role": "application", "roledescription": "drag handle"}) == m.UNHANDLED
    assert m.is_drag_drop({"draggable": True}) is True
    assert m.is_drag_drop({"roledescription": "sortable item"}) is True
    assert m.is_drag_drop({"kind": "button"}) is False
    assert "drag-drop" in m.unhandled_reason({"draggable": True})


def test_nameless_unhandled_is_still_ledgerable():
    """The old is_unhandled_field required a name, silently dropping nameless
    unsupported controls. is_unhandled() ledgers them (the crawler synthesizes a
    positional label) — never a silent skip."""
    nameless = {"kind": "grid", "role": "grid", "name": ""}
    assert m.is_unhandled_field(nameless) is False      # back-compat unchanged
    assert m.is_unhandled(nameless) is True             # but still ledgerable
    assert m.is_unhandled({"draggable": True, "name": ""}) is True
    assert m.is_unhandled({"kind": "text", "name": "x"}) is False  # handled field, not ledgered


def test_diff_driver_selection():
    assert m.is_diff_driver({"kind": "select", "options": ["A"]}) is True
    assert m.is_diff_driver({"kind": "radio"}) is True
    assert m.is_diff_driver({"kind": "toggle"}) is True
    # a choice whose own options we couldn't read can't drive; disabled/danger never drive.
    assert m.is_diff_driver({"kind": "select", "options": []}) is False
    assert m.is_diff_driver({"kind": "select", "options": ["A"], "danger": True}) is False
    assert m.is_diff_driver({"kind": "text"}) is False


# ── Danger gate: locale-tolerant destructive-intent + fail-closed ──────────────
def test_english_and_non_english_destructive_labels_are_consequential():
    for label in ["Delete account", "Remove item", "Pay now", "Transfer money",
                  "Löschen", "Eliminar", "Supprimer", "Deactivate", "删除", "支払", "결제"]:
        assert ds.is_consequential(label) is True, label


def test_plain_value_labels_are_not_consequential():
    for label in ["From Account", "Email", "Country", "Schedule for later", "First name"]:
        assert ds.is_consequential(label) is False, label


def test_safe_to_actuate_is_fail_closed():
    assert ds.safe_to_actuate({"name": "From Account"}) is True
    assert ds.safe_to_actuate({"name": "Delete account"}) is False   # destructive label
    assert ds.safe_to_actuate({"name": "Region", "danger": True}) is False  # base-guard danger
    assert ds.safe_to_actuate({"name": "Region", "disabled": True}) is False
