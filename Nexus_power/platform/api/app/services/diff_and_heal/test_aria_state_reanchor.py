"""Standalone L4 a11y-STATE deep-grounding unit test — run INSIDE the platform-api container:

    python -m pytest app/services/diff_and_heal/test_aria_state_reanchor.py -q
    # or, with no pytest:
    python app/services/diff_and_heal/test_aria_state_reanchor.py

No network, no DB, no LLM. Asserts:
  1. flatten_aria now copies ARIA STATE keys (expanded/checked/selected/disabled/
     level/...) through, and is BACKWARD-COMPATIBLE: a stateless node is still
     exactly {role, name}, and callers reading role+name are unaffected.
  2. recorded_state_intent derives a checked/expanded intent from the recorded
     observed dict, and returns {} for plain text (no false intent).
  3. resolve_reanchor uses the state bonus to DISAMBIGUATE two same-named, same-role
     renamed candidates by state (the one whose state matches the intent wins).
  4. NEVER-GREEN-WASH GUARD: the state bonus can never admit a node whose RAW name
     similarity is below min_confidence (the floor is checked pre-bonus), and a
     state-only signal can never override the name+role gate.
  5. With no recorded_state, resolve_reanchor is byte-identical to its prior behavior.
"""
from .action_resolver import (
    _ARIA_STATE_KEYS,
    _state_match_bonus,
    flatten_aria,
    recorded_state_intent,
    resolve_reanchor,
)


# ── 1. flatten_aria captures STATE, stays backward-compatible ────────────────

def test_flatten_captures_state():
    tree = {
        "role": "WebArea", "name": "Trip",
        "children": [
            {"role": "checkbox", "name": "Add insurance", "checked": True},
            {"role": "button", "name": "Continue", "disabled": True},
            {"role": "combobox", "name": "Cabin", "expanded": False, "editable": True},
            {"role": "heading", "name": "Section", "level": 2},
            {"role": "textbox", "name": "Email"},  # stateless -> still {role,name}
            {"role": "none", "name": ""},          # unnamed -> dropped, as before
        ],
    }
    flat = flatten_aria(tree)
    by_name = {n["name"]: n for n in flat}

    assert by_name["Add insurance"]["checked"] is True
    assert by_name["Continue"]["disabled"] is True
    assert by_name["Cabin"]["expanded"] is False and by_name["Cabin"]["editable"] is True
    assert by_name["Section"]["level"] == 2
    # backward-compatible: a stateless node is EXACTLY {role, name}
    assert by_name["Email"] == {"role": "textbox", "name": "Email"}
    # unnamed nodes are still dropped (no behavior change)
    assert "" not in by_name
    # never leaks a non-state property (e.g. children) into the flat node
    for n in flat:
        assert "children" not in n
        for k in n:
            assert k in ("role", "name") or k in _ARIA_STATE_KEYS


# ── 2. recorded_state_intent derivation ──────────────────────────────────────

def test_recorded_state_intent():
    assert recorded_state_intent({"verb": "check", "kind": "checkbox",
                                  "value": "on"}) == {"checked": True}
    assert recorded_state_intent({"verb": "uncheck", "kind": "checkbox",
                                  "value": "off"}) == {"checked": False}
    assert recorded_state_intent({"kind": "toggle", "value": "true"}) == {"checked": True}
    assert recorded_state_intent({"verb": "expand"}).get("expanded") is True
    assert recorded_state_intent({"verb": "collapse"}).get("expanded") is False
    # plain text fill -> NO state intent (no false signal)
    assert recorded_state_intent({"verb": "type", "kind": "field",
                                  "label": "Email", "value": "a@b.c"}) == {}
    assert recorded_state_intent({}) == {} and recorded_state_intent(None) == {}


# ── 3. STATE disambiguates two same-named renamed candidates ─────────────────

def test_state_disambiguates_same_name():
    # Recording wanted the "Add coverage" toggle left CHECKED. The page renamed it,
    # and there are now TWO equally-named, equally-roled, equally-similar candidates
    # ("Add coverage option") differing ONLY by state. Without state we'd refuse on
    # the ambiguity margin; the recorded intent (checked=True) breaks the tie.
    live = [
        {"role": "checkbox", "name": "Add coverage A", "checked": False},
        {"role": "checkbox", "name": "Add coverage A", "checked": True},
    ]
    ra = resolve_reanchor(
        recorded_label="Add coverage",
        recorded_kind="checkbox",
        live_nodes=live,
        recorded_state={"checked": True},
    )
    assert ra is not None, "state should have broken the tie -> a confident re-anchor"
    assert ra.name == "Add coverage A"
    assert ra.state.get("checked") is True
    # Same inputs but NO recorded_state -> a true tie -> refuse (ambiguity guard).
    assert resolve_reanchor(
        recorded_label="Add coverage", recorded_kind="checkbox",
        live_nodes=live, recorded_state=None,
    ) is None


# ── 3b. STATE must NEVER break a DIFFERENTLY-named ambiguity ──────────────────

def test_state_never_breaks_cross_name_ambiguity():
    # Two DISTINCT controls — tiered sibling coverages — with near-identical name
    # similarity to the recorded label but DIFFERENT accessible names. The recorded
    # intent (checked=True) matches the state of exactly one of them. State must NOT
    # be allowed to close the gap between two genuinely-different controls: this is a
    # real ambiguity and the resolver MUST refuse (never green-wash onto "Gold" just
    # because its state happens to match).
    live = [
        {"role": "checkbox", "name": "Travel Coverage Bronze", "checked": False},
        {"role": "checkbox", "name": "Travel Coverage Gold", "checked": True},
    ]
    ra = resolve_reanchor(
        recorded_label="Travel Coverage",
        recorded_kind="checkbox",
        live_nodes=live,
        recorded_state={"checked": True},
    )
    assert ra is None, (
        "state must never break ambiguity between DIFFERENTLY-named controls — "
        "two distinct tiered siblings remain genuinely ambiguous -> refuse"
    )


# ── 4. NEVER-GREEN-WASH: state can't admit a weak-name match ─────────────────

def test_state_cannot_greenwash_weak_name():
    # A node whose NAME is unrelated to the recorded label, but whose state matches
    # perfectly. The min_confidence floor is checked against RAW name similarity, so
    # the state bonus must NOT promote it into a confident heal -> refuse.
    live = [{"role": "checkbox", "name": "Totally Different Widget", "checked": True}]
    ra = resolve_reanchor(
        recorded_label="Add coverage",
        recorded_kind="checkbox",
        live_nodes=live,
        recorded_state={"checked": True},
    )
    assert ra is None, "state must never lift a weak-name node past the confidence floor"

    # And the bonus itself is bounded (just above the ambiguity margin so a clean
    # state match can break a same-name tie, but never large enough to flip a clear
    # name-similarity ordering or to lift a sub-floor RAW name match).
    assert 0.0 <= _state_match_bonus({"checked": True},
                                     {"checked": True}) <= 0.13 + 1e-9
    # A recorded key the node doesn't expose contributes 0 (sparse capture is safe).
    assert _state_match_bonus({"checked": True}, {"role": "x", "name": "y"}) == 0.0
    # Wrong state -> 0 bonus (never rewards a mismatch).
    assert _state_match_bonus({"checked": True}, {"checked": False}) == 0.0


# ── 5. No recorded_state -> byte-identical to prior behavior ─────────────────

def test_no_state_is_unchanged():
    live = [
        {"role": "textbox", "name": "Traveller email address"},
        {"role": "button", "name": "Continue"},
    ]
    # A clean rename "Email address" -> "Traveller email address" (token-contained
    # qualifier rename): resolves WITHOUT any state, exactly as before this change.
    ra = resolve_reanchor(
        recorded_label="Email address", recorded_kind="text", live_nodes=live,
    )
    assert ra is not None and ra.name == "Traveller email address"
    assert ra.state == {}  # no state captured -> empty, default-safe


def _run_all():
    import inspect
    import sys
    mod = sys.modules[__name__]
    fns = [f for n, f in inspect.getmembers(mod, inspect.isfunction)
           if n.startswith("test_")]
    failed = 0
    for f in fns:
        try:
            f()
            print(f"PASS {f.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {f.__name__}: {e}")
    if failed:
        print(f"\n{failed} FAILED")
        sys.exit(1)
    print(f"\nALL {len(fns)} PASSED")


if __name__ == "__main__":
    _run_all()
