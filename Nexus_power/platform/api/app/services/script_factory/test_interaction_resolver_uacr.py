"""Standalone UACR unit test — run INSIDE the platform-api container:

    python -m pytest app/services/script_factory/test_interaction_resolver_uacr.py -q
    # or, with no pytest:
    python app/services/script_factory/test_interaction_resolver_uacr.py

Asserts (no network, no DB, no LLM):
  1. Each recorded (verb, kind, value) shape ROUTES to universal_control with the
     intended NON-GATING hint, and plain free-text fields route to None (the seam
     stays byte-identical for them).
  2. Each emit_* recipe (universal_control + the back-compat combobox / slider /
     switch / conditional) emits BOUNDED timeouts (no unbounded click/fill/expect)
     and >= 1 GROUNDED oracle (toBeVisible / toHaveValue / toBeChecked /
     toHaveText / toHaveAttribute), so a wrong recipe fails RED, never green.
  3. The whole Aegis 24-step control-kind set is covered by the pre-pass signature
     (has_control_kind_signature) for controls and excluded for plain text.
"""
import re
import types

from . import interaction_resolver as ir
from . import compiler  # ensures the lazy compiler helpers resolve in-package


def _step(verb, kind, label, value):
    return {"verb": verb, "kind": kind, "label": label, "value": value}


def _mkstep(observed):
    s = types.SimpleNamespace()
    s.observed = observed
    s.action = ""
    return s


# ── 1. ROUTING ───────────────────────────────────────────────────────────────

_ROUTING = [
    # (name, observed, expected_hint)
    ("combobox", _step("type", "field", "Plan tier", "Platinum — Concierge"), "custom_combobox"),
    ("slider", _step("type", "field", "Coverage amount", "$950,000"), "range_slider"),
    ("accordion", _step("type", "field", "Risk disclosure",
                        "Tobacco & hazardous activity disclosure"), "accordion"),
    ("switch_empty", _step("select", "toggle", "Auto-renew annually", ""), "switch"),
    ("switch_bool", _step("select", "toggle", "Add a beneficiary", "true"), "switch"),
    ("checkbox_certify", _step("select", "toggle",
                               "I certify I am a U.S. person...", ""), "switch"),
]

_PLAIN = [
    _step("type", "field", "First name", "REDDY"),
    _step("type", "field", "Last name", "KARNA"),
    _step("type", "field", "Tax ID / SSN", "123-45-6789"),
    _step("type", "field", "Date of birth", "03/25/1983"),
    _step("type", "field", "E-signature (type your full name)", "REDDY KARNA"),
    _step("type", "field", "Phone", "(555) 555-5555"),
]


def test_routing_controls_go_to_universal():
    for name, o, exp_hint in _ROUTING:
        intr = ir.detect_interaction(o, {}, "locator.fill: Timeout 6000ms exceeded")
        assert intr is not None, f"{name} routed to None"
        assert intr["kind"] == "universal_control", f"{name}: {intr['kind']}"
        assert intr["hint"] == exp_hint, f"{name}: hint {intr['hint']} != {exp_hint}"


def test_routing_plain_text_is_none():
    for o in _PLAIN:
        assert ir.detect_interaction(o, {}, "") is None, f"plain text routed: {o['label']}"


def test_field_meta_chooser_signal_upgrades_plain_label():
    # A label that LOOKS plain but field_meta marks as a chooser must still route.
    o = _step("type", "field", "Country", "United States")
    fm = {"country": {"control": "select", "options": ["United States", "Canada"]}}
    intr = ir.detect_interaction(o, fm, "")
    assert intr is not None and intr["kind"] == "universal_control"


def test_prepass_signature_matches_routing():
    for _, o, _h in _ROUTING:
        assert ir.has_control_kind_signature(o), o["label"]
    for o in _PLAIN:
        assert not ir.has_control_kind_signature(o), o["label"]


# ── 2. EMITTED JS IS BOUNDED + GROUNDED ───────────────────────────────────────

_GROUNDED = ("toBeVisible", "toHaveValue", "toBeChecked", "toHaveText", "toHaveAttribute")
# An action that MUST carry an explicit timeout (a bare one would risk a 60s hang).
_ACTION_CALL = re.compile(r"\.(click|fill|check|uncheck|selectOption|press|waitFor)\(")


def _assert_bounded_and_grounded(lines, name):
    js = "\n".join(lines)
    # >= 1 grounded oracle
    assert any(g in js for g in _GROUNDED), f"{name}: no grounded oracle"
    assert "expect(" in js, f"{name}: no expect()"
    # every action-ish call that could block carries an explicit timeout on its line
    for ln in lines:
        if ln.strip().startswith("//"):
            continue
        for m in _ACTION_CALL.finditer(ln):
            # the call (or its immediate options) must mention a timeout, OR be a
            # deliberately tolerant readiness probe (isEditable 1200 / .catch).
            assert ("timeout:" in ln) or (".catch(" in ln), \
                f"{name}: unbounded action on line: {ln.strip()}"
    # a hard expect(...) oracle should be bounded too (no infinite default wait)
    for ln in lines:
        if "expect(" in ln and (".toBeVisible" in ln or ".toHaveValue" in ln
                                or ".toBeChecked" in ln or ".toHaveAttribute" in ln):
            assert "timeout:" in ln or "toHaveValue(" in ln, \
                f"{name}: unbounded expect oracle: {ln.strip()}"


def test_universal_emits_bounded_grounded():
    samples = [o for _, o, _h in _ROUTING]
    # plus a conditional reveal field (carries reveal_label)
    cond = _step("type", "field", "Beneficiary full name", "SECOND TEST")
    for o in samples:
        intr = ir.detect_interaction(o, {}, "fill timeout")
        lines = ir.emit_universal_control_lines(o, {}, True, intr)
        _assert_bounded_and_grounded(lines, "universal:" + o["label"])
    intr = {"kind": "universal_control", "value": "SECOND TEST",
            "label": cond["label"], "hint": "custom_combobox",
            "reveal_label": "Add a beneficiary"}
    lines = ir.emit_universal_control_lines(cond, {}, True, intr)
    _assert_bounded_and_grounded(lines, "universal:conditional")
    # the conditional branch must use a grounded toHaveValue oracle helper
    assert "toHaveValue(_grounded_token" in "\n".join(lines)


def test_backcompat_recipes_bounded_grounded():
    combo = _step("type", "field", "Plan tier", "Platinum — Concierge")
    slider = _step("type", "field", "Coverage amount", "$950,000")
    sw = _step("select", "toggle", "Auto-renew annually", "")
    cond = _step("type", "field", "Beneficiary full name", "SECOND TEST")

    _assert_bounded_and_grounded(
        ir.emit_combobox_lines(combo, {}, True, {"value": combo["value"]}), "combobox")
    _assert_bounded_and_grounded(
        ir.emit_range_slider_lines(slider, {}, True, {"value": slider["value"]}), "slider")
    _assert_bounded_and_grounded(
        ir.emit_toggle_switch_lines(sw, {}, True, {"value": sw["value"]}), "switch")
    _assert_bounded_and_grounded(
        ir.emit_conditional_text_lines(
            cond, {}, True, {"value": cond["value"], "reveal_label": "Add a beneficiary"}),
        "conditional")


def test_registry_covers_every_routed_kind():
    assert "universal_control" in ir.INTERACTION_RECIPES
    for k in ("custom_combobox", "range_slider", "toggle_switch", "conditional_text"):
        assert k in ir.INTERACTION_RECIPES, k
    # every routed kind must be callable
    for k, fn in ir.INTERACTION_RECIPES.items():
        assert callable(fn), k


def test_seam_byte_identical_for_plain_text():
    # A plain text step must produce the SAME compiler output with and without an
    # (absent) interaction — i.e. detect_interaction returns None so no recipe runs.
    o = _step("type", "field", "First name", "REDDY")
    st = _mkstep(o)
    base = compiler._action_lines(st, {}, parametrize=True)
    # interaction None -> identical
    same = compiler._action_lines(st, {}, parametrize=True, interaction=None)
    assert base == same


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
