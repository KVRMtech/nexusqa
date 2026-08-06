"""Journey Graph C0 — decision-point capture.

An enumerable control (select / radio / checkbox / toggle) is a fork in the
business flow. The fill ledger records the fork's enumeration (option labels —
product UI text, never user values) and WHICH option the committed fill took;
the crawler folds those into per-step ``decision_points`` on the flow, and the
ledger passes them through sanitized. A fork whose field went unanswered is
recorded WITHOUT a choice — discovered, not decided.
"""
from __future__ import annotations

import asyncio

from app import emit, flow_ledger
from app.browser import RawObservation
from app.crawler import _decision_points
from app.forms import (
    AnswerKey,
    _enumerable_options,
    fill_form_phase_a,
    normalize_option,
    resolve_field,
)
from app.identity_pack import derive as derive_identity

_IDENTITY = derive_identity("qec-test")


class FakeFormPort:
    async def fill(self, control, value):
        return RawObservation(url_before="u", url_after="u", committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before="u", url_after="u", committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before="u", url_after="u",
                              committed_value="true" if checked else "false")


def _fill(controls, answers=None):
    key = AnswerKey.from_payload({"exact": answers} if answers else None)
    return asyncio.run(fill_form_phase_a(
        FakeFormPort(), controls, key,
        emit.MonotonicClock(), state_id="fp1", identity=_IDENTITY))


# ── option enumeration ───────────────────────────────────────────────────

def test_normalize_option_collapses_case_and_whitespace():
    assert normalize_option("  Smoker  /  Tobacco ") == "smoker / tobacco"


def test_select_and_radio_enumerate_their_options():
    control = {"options": ["Non-smoker", "Smoker", ""]}
    assert _enumerable_options(control, "select") == ["non-smoker", "smoker"]
    assert _enumerable_options(control, "radio") == ["non-smoker", "smoker"]


def test_binary_kinds_enumerate_their_two_states():
    assert _enumerable_options({}, "checkbox") == ["checked", "unchecked"]
    assert _enumerable_options({}, "toggle") == ["checked", "unchecked"]


def test_resolve_field_records_options_only_for_enumerable_kinds():
    sel = {"name": "Tobacco use", "kind": "select",
           "options": ["Non-smoker", "Smoker"]}
    out = resolve_field(sel, "select", "Tobacco use", AnswerKey({}), _IDENTITY)
    assert out["entry"]["options"] == ["non-smoker", "smoker"]
    text = {"name": "First name", "kind": "text"}
    out = resolve_field(text, "text", "First name", AnswerKey({}), _IDENTITY)
    assert "options" not in out["entry"]


# ── choice stamping (committed fills only) ───────────────────────────────

def test_committed_select_fill_stamps_the_choice():
    result = _fill(
        [{"name": "Tobacco use", "kind": "select",
          "options": ["Non-smoker", "Smoker"]}],
        answers={"Tobacco use": "Smoker"})
    entry = next(e for e in result.field_ledger if e["name"] == "Tobacco use")
    assert entry["options"] == ["non-smoker", "smoker"]
    assert entry["choice"] == "smoker"
    assert entry["filled"] is True


def test_unanswered_enumerable_keeps_options_without_choice():
    """A radio group in user data-mode is the client's choice to make — the
    fork is DISCOVERED (options recorded) but not decided (no choice)."""
    result = _fill(
        [{"name": "Coverage tier", "kind": "radio",
          "options": ["Bronze", "Silver", "Gold"]}])
    entry = next(e for e in result.field_ledger if e["name"] == "Coverage tier")
    assert entry["options"] == ["bronze", "silver", "gold"]
    assert "choice" not in entry
    assert entry["filled"] is False


def test_checkbox_choice_normalizes_to_binary_states():
    result = _fill(
        [{"name": "I agree to the terms", "kind": "checkbox", "required": True}])
    entry = next(e for e in result.field_ledger
                 if e["name"] == "I agree to the terms")
    assert entry["options"] == ["checked", "unchecked"]
    assert entry["choice"] == "checked"


# ── crawler fold + ledger passthrough ────────────────────────────────────

def test_decision_points_maps_ledger_entries():
    ledger = [
        {"name": "Tobacco use", "signature": "sig-t", "provenance": "provided",
         "options": ["non-smoker", "smoker"], "choice": "smoker"},
        {"name": "Coverage tier", "signature": "sig-c", "provenance": "needs_input",
         "options": ["bronze", "silver", "gold"]},
        {"name": "First name", "signature": "sig-f", "provenance": "provided"},
    ]
    dps = _decision_points(ledger)
    assert [d["control_label"] for d in dps] == ["Tobacco use", "Coverage tier"]
    assert dps[0]["choice"] == "smoker"
    assert dps[0]["control_signature"] == "sig-t"
    assert "choice" not in dps[1]
    assert dps[1]["options"] == ["bronze", "silver", "gold"]


def test_flow_ledger_passes_decision_points_through_sanitized():
    f = flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url="u", entry_title="Quote",
        steps=[{
            "fingerprint": "f1", "url": "u1", "title": "Health",
            "fields_filled": 2, "fields_unfilled": 1,
            "decision_points": [
                {"control_signature": "sig-t", "control_label": "Tobacco use",
                 "options": ["non-smoker", "smoker"], "choice": "smoker",
                 "provenance": "provided"},
            ],
        }],
        terminal=flow_ledger.TERMINAL_SUBMIT_BOUNDARY, max_steps=20)
    dp = f["steps"][0]["decision_points"][0]
    assert dp == {"control_signature": "sig-t", "control_label": "Tobacco use",
                  "options": ["non-smoker", "smoker"], "provenance": "provided",
                  "choice": "smoker"}
    assert f["steps"][0]["fields_filled"] == 2


def test_flow_ledger_steps_without_decision_points_are_unchanged():
    f = flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url="u", entry_title="Quote",
        steps=[{"fingerprint": "f1", "url": "u1", "title": "Plain",
                "fields_filled": 1, "fields_unfilled": 0}],
        terminal=flow_ledger.TERMINAL_NO_ADVANCE, max_steps=6)
    assert "decision_points" not in f["steps"][0]


# ── Branch-walk choice overrides (Journey Graph C4, rung 0) ──────────────

def _sig_of(control, kind):
    from app import field_signature
    return field_signature.compute(control, kind=kind)["signature"]


def test_override_forces_the_enumerated_option_and_outranks_the_answer_key():
    control = {"name": "Tobacco use", "kind": "select",
               "options": ["Non-smoker", "Smoker"]}
    sig = _sig_of(control, "select")
    key = AnswerKey.from_payload({"exact": {"Tobacco use": "Non-smoker"}})
    out = resolve_field(control, "select", "Tobacco use", key, _IDENTITY,
                        choice_overrides={sig: "smoker"})
    assert out["value"] == "Smoker"          # the control's ORIGINAL text
    assert out["entry"]["provenance"] == "planned"
    assert out["entry"]["filled"] is True


def test_override_never_injects_free_text():
    """A forced option the control does not offer FAILS CLOSED to the normal
    rungs — an override can choose among enumerated options, never invent."""
    control = {"name": "Tobacco use", "kind": "select",
               "options": ["Non-smoker", "Smoker"]}
    sig = _sig_of(control, "select")
    key = AnswerKey.from_payload({"exact": {"Tobacco use": "Non-smoker"}})
    out = resolve_field(control, "select", "Tobacco use", key, _IDENTITY,
                        choice_overrides={sig: "platinum plan"})
    assert out["value"] == "Non-smoker"      # the answer key resumed control
    assert out["entry"]["provenance"] == "provided"


def test_override_ignores_non_enumerable_kinds():
    control = {"name": "First name", "kind": "text"}
    sig = _sig_of(control, "text")
    out = resolve_field(control, "text", "First name",
                        AnswerKey.from_payload({"exact": {"First name": "Ana"}}),
                        _IDENTITY, choice_overrides={sig: "smoker"})
    assert out["value"] == "Ana"
    assert out["entry"]["provenance"] == "provided"


def test_override_binary_states_map_to_checkbox_values():
    control = {"name": "Paperless billing", "kind": "toggle"}
    sig = _sig_of(control, "toggle")
    out = resolve_field(control, "toggle", "Paperless billing",
                        AnswerKey.from_payload(None), _IDENTITY,
                        choice_overrides={sig: "checked"})
    assert out["value"] == "true"
    assert out["entry"]["provenance"] == "planned"
    out2 = resolve_field(control, "toggle", "Paperless billing",
                         AnswerKey.from_payload(None), _IDENTITY,
                         choice_overrides={sig: "unchecked"})
    assert out2["value"] == "false"


def test_committed_override_fill_stamps_planned_choice_in_ledger():
    control = {"name": "Tobacco use", "kind": "select",
               "options": ["Non-smoker", "Smoker"]}
    sig = _sig_of(control, "select")
    result = asyncio.run(fill_form_phase_a(
        FakeFormPort(), [control], AnswerKey.from_payload(None),
        emit.MonotonicClock(), state_id="fp1", identity=_IDENTITY,
        choice_overrides={sig: "smoker"}))
    entry = next(e for e in result.field_ledger if e["name"] == "Tobacco use")
    assert entry["provenance"] == "planned"
    assert entry["choice"] == "smoker"


# ── radio GROUPS: one question, N elements, exactly one checked ──────────────

_GROUP = "g" * 32


def _member(label: str) -> dict:
    """One member of a declared radio group, as build_inventory stamps it."""
    return {"name": label, "kind": "radio", "role": "radio", "tag": "input",
            "input_type": "radio", "options": [],
            "group_id": _GROUP,
            "group_options": ["Term Life", "Whole Life", "Universal Life"],
            "group_size": 3}


def test_radio_group_enumerates_the_groups_answers_not_the_elements():
    """A single <input type=radio> has no options of its own — without the group
    the decision point enumerates [] and vanishes from the journey graph."""
    entry = resolve_field(_member("Term Life"), "radio", "Term Life",
                          AnswerKey({}), _IDENTITY)["entry"]
    assert entry["options"] == ["term life", "whole life", "universal life"]
    assert entry["group_id"] == _GROUP


def test_planned_walk_checks_only_the_forced_member_of_the_group():
    """The override is keyed by the QUESTION. The member that IS the forced
    answer checks itself; its siblings must be left untouched, because checking
    a second member would silently overturn the planned walk."""
    overrides = {_GROUP: "whole life"}
    chosen = resolve_field(_member("Whole Life"), "radio", "Whole Life",
                           AnswerKey({}), _IDENTITY, choice_overrides=overrides)
    assert chosen["value"] == "Whole Life"
    assert chosen["entry"]["provenance"] == "planned"
    assert chosen["entry"]["filled"] is True

    for sibling in ("Term Life", "Universal Life"):
        out = resolve_field(_member(sibling), "radio", sibling, AnswerKey({}),
                            _IDENTITY, choice_overrides=overrides)
        assert out["value"] is None, f"{sibling} must not be checked"
        assert out["entry"]["filled"] is False


def test_answer_key_cannot_overturn_a_planned_group_choice():
    """A sibling must not fall through to the answer key while a walk is forcing
    a different option — two members of one group selected is not a state the
    DOM can even hold, and the last write would decide the business path."""
    key = AnswerKey({"Term Life": "yes"})
    out = resolve_field(_member("Term Life"), "radio", "Term Life", key,
                        _IDENTITY, choice_overrides={_GROUP: "whole life"})
    assert out["value"] is None


def test_ungrouped_radio_keeps_the_pre_group_safety_rule():
    """No declared group ⇒ unchanged behaviour: USER mode never picks a radio,
    because choosing a product is a business decision, not a crawl decision."""
    solo = {"name": "Term Life", "kind": "radio", "role": "radio",
            "tag": "input", "input_type": "radio", "options": []}
    out = resolve_field(solo, "radio", "Term Life", AnswerKey({}), _IDENTITY)
    assert out["value"] is None
    assert "group_id" not in out["entry"]


def test_group_id_survives_the_ledger_sanitizer():
    """Dropped here, the fold cannot tell four members of one choice from four
    independent choices — the N×N phantom-branch bug."""
    dps = _decision_points([
        {"name": "Whole Life", "signature": "s1", "options": ["term life", "whole life"],
         "provenance": "planned", "choice": "whole life", "group_id": _GROUP},
    ])
    assert dps[0]["group_id"] == _GROUP
    f = flow_ledger.build_flow(
        entry_fingerprint="fpG", entry_url="u", entry_title="Quote",
        steps=[{"fingerprint": "f1", "url": "u1", "title": "Product",
                "fields_filled": 1, "fields_unfilled": 0,
                "decision_points": dps}],
        terminal=flow_ledger.TERMINAL_SUBMIT_BOUNDARY, max_steps=20)
    assert f["steps"][0]["decision_points"][0]["group_id"] == _GROUP
