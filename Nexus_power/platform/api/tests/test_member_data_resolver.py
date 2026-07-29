"""Member-data resolution — the running member's values, or an honest refusal.

Phases 2 and 3 of MEMBER_DATA_RESOLVER_PLAN, tested together because shipping the
resolver without the refusal would swap one silent wrong answer for another.

The properties that matter:
  * a proven member-derived INPUT is redirected into the compiled spec's own
    override key space (a slug of the field label);
  * an unclassified value is left completely alone — first use of the feature
    cannot start blocking every run;
  * a member-derived value with NO answer for this member is reported missing, and
    never falls back to the value belonging to whoever the crawl ran as;
  * a member-derived ASSERTION can only block, never be quietly softened.

Pure — no DB, no compiler, no live stack.
"""
from __future__ import annotations

import re

from app.services.test_factory import member_data_resolver as mdr
from app.services.test_factory import persona_diff


def _data_key(label: str) -> str:
    """The compiled spec's override key: lowercase, non-alphanumerics to '-'.
    Mirrors compiler._data_key; the real one is injected in production."""
    return re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")


def _case(scenario_id: str, steps: list) -> dict:
    return {"test_id": scenario_id, "steps": steps}


def _step(n: int, label: str, value: str, expected: str = "") -> dict:
    return {"step_number": n, "expected_result": expected,
            "observed": {"verb": "type", "kind": "field", "label": label, "value": value}}


_SUITE = [_case("tc-1", [
    _step(1, "Member number", "8891234"),
    _step(2, "Coverage amount", "250000"),
])]

_MEMBER_DERIVED = {"class": persona_diff.CLASS_MEMBER_DERIVED}
_APP_CONSTANT = {"class": persona_diff.CLASS_APP_CONSTANT}


# ── resolving ────────────────────────────────────────────────────────────────

def test_a_member_derived_input_is_redirected_into_the_specs_key_space():
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": "5550001"},
        data_key=_data_key)

    assert plan["data_by_test"] == {"tc-1": {"member-number": "5550001"}}
    assert plan["missing"] == []
    assert plan["resolved"][0]["override_key"] == "member-number"


def test_a_shared_value_is_never_overridden():
    """An app-constant is the same for everyone — it must keep asserting the
    recorded value, so a genuine application change still fails red."""
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED,
                         "tc-1:2:observed_value": _APP_CONSTANT},
        answers={"tc-1:1:observed_value": "5550001",
                 "tc-1:2:observed_value": "999"},
        data_key=_data_key)

    assert "coverage-amount" not in plan["data_by_test"]["tc-1"]
    assert plan["data_by_test"] == {"tc-1": {"member-number": "5550001"}}


def test_an_unclassified_value_is_left_completely_alone():
    """Before any classification exists the feature must be inert — otherwise
    turning it on would block every run in the fleet on day one."""
    plan = mdr.plan_member_data(
        _SUITE, classifications={}, answers={}, data_key=_data_key)
    assert plan == {"data_by_test": {}, "resolved": [], "missing": [],
                    "member_derived_total": 0}


# ── refusing ─────────────────────────────────────────────────────────────────

def test_a_member_derived_value_with_no_answer_is_reported_missing():
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={},
        data_key=_data_key)

    assert plan["data_by_test"] == {}
    assert [m["value_key"] for m in plan["missing"]] == ["tc-1:1:observed_value"]
    assert plan["missing"][0]["label"] == "Member number"


def test_the_crawl_members_value_is_NEVER_used_as_a_fallback():
    """The defect this whole phase exists to remove: silently keeping the literal."""
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={},
        data_key=_data_key)
    assert "8891234" not in repr(plan["data_by_test"])
    assert plan["missing"], "a value with no answer must be surfaced, not defaulted"


def test_a_blank_answer_does_not_count_as_an_answer():
    """A member whose value is genuinely blank must be recorded blank on purpose,
    not inferred from an empty or whitespace row."""
    for blank in ("", "   ", None):
        plan = mdr.plan_member_data(
            _SUITE,
            classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
            answers={"tc-1:1:observed_value": blank},
            data_key=_data_key)
        assert plan["missing"], f"blank answer {blank!r} was accepted"


def test_a_classified_input_with_no_label_blocks_rather_than_running():
    """No label means the compiled spec has no override key, so the fill cannot be
    redirected — running anyway would type the other member's value."""
    suite = [_case("tc-2", [_step(1, "", "8891234")])]
    plan = mdr.plan_member_data(
        suite,
        classifications={"tc-2:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-2:1:observed_value": "5550001"},
        data_key=_data_key)

    assert plan["data_by_test"] == {}
    assert plan["missing"][0]["reason"] == "no_override_key"


# ── assertions cannot be softened ────────────────────────────────────────────

def test_a_member_derived_expectation_with_no_answer_blocks():
    """It is asserted, not typed, so it cannot be overridden — the only honest
    options are a correct answer or a refusal."""
    suite = [_case("tc-3", [_step(1, "Full name", "A Name", expected="'Full name' shows 'A Name'")])]
    plan = mdr.plan_member_data(
        suite,
        classifications={"tc-3:1:expected": _MEMBER_DERIVED},
        answers={},
        data_key=_data_key)

    assert [m["kind"] for m in plan["missing"]] == ["expected"]


def test_a_member_derived_expectation_WITH_an_answer_does_not_block_and_does_not_override():
    suite = [_case("tc-3", [_step(1, "Full name", "A Name", expected="'Full name' shows 'A Name'")])]
    plan = mdr.plan_member_data(
        suite,
        classifications={"tc-3:1:expected": _MEMBER_DERIVED},
        answers={"tc-3:1:expected": "Another Name"},
        data_key=_data_key)

    assert plan["missing"] == []
    assert plan["data_by_test"] == {}          # assertions are not data-driven
    assert plan["resolved"][0]["override_key"] == ""


# ── shape / plumbing ─────────────────────────────────────────────────────────

def test_overrides_are_keyed_per_scenario_so_two_cases_do_not_collide():
    suite = [_case("tc-a", [_step(1, "Member number", "111")]),
             _case("tc-b", [_step(1, "Member number", "222")])]
    plan = mdr.plan_member_data(
        suite,
        classifications={"tc-a:1:observed_value": _MEMBER_DERIVED,
                         "tc-b:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-a:1:observed_value": "999", "tc-b:1:observed_value": "888"},
        data_key=_data_key)

    assert plan["data_by_test"] == {"tc-a": {"member-number": "999"},
                                    "tc-b": {"member-number": "888"}}


def test_member_derived_keys_ignores_every_other_class():
    classifications = {
        "k1": {"class": persona_diff.CLASS_MEMBER_DERIVED},
        "k2": {"class": persona_diff.CLASS_APP_CONSTANT},
        "k3": {"class": persona_diff.CLASS_VOLATILE},
        "k4": {"class": persona_diff.CLASS_STRUCTURAL},
        "k5": {"class": persona_diff.CLASS_UNKNOWN},
        "k6": "not-a-dict",
    }
    assert mdr.member_derived_keys(classifications) == {"k1"}


def test_the_total_accounts_for_every_member_derived_value():
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED,
                         "tc-1:2:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": "5550001"},
        data_key=_data_key)
    assert plan["member_derived_total"] == 2
    assert len(plan["resolved"]) == 1 and len(plan["missing"]) == 1


def test_empty_and_malformed_input_is_survivable():
    for cases in ([], None, [{}], [{"steps": "not-a-list"}]):
        plan = mdr.plan_member_data(cases, classifications={"x": _MEMBER_DERIVED},
                                    answers={}, data_key=_data_key)
        assert plan["data_by_test"] == {}


# ── the answer-sheet shape the store actually returns ────────────────────────

def test_the_real_answer_sheet_shape_from_the_store_is_understood():
    """REGRESSION. persona_store.get_expected_values returns
    {value_key: {"expected_value": ..., "source": ...}} — not a flat string map.
    Treating that dict as the value would type its repr into the field: a wrong
    value that still looks committed, so every guard downstream passes."""
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": {"expected_value": "5550001",
                                           "source": "diff_proven"}},
        data_key=_data_key)

    assert plan["data_by_test"] == {"tc-1": {"member-number": "5550001"}}
    assert plan["missing"] == []
    assert "expected_value" not in repr(plan["data_by_test"])


def test_both_answer_shapes_agree():
    """A flat map and the store's dict map must resolve identically."""
    flat = mdr.plan_member_data(
        _SUITE, classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": "5550001"}, data_key=_data_key)
    rich = mdr.plan_member_data(
        _SUITE, classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": {"expected_value": "5550001"}},
        data_key=_data_key)
    assert flat["data_by_test"] == rich["data_by_test"]


def test_a_store_row_with_an_empty_value_still_counts_as_no_answer():
    plan = mdr.plan_member_data(
        _SUITE,
        classifications={"tc-1:1:observed_value": _MEMBER_DERIVED},
        answers={"tc-1:1:observed_value": {"expected_value": "", "source": "x"}},
        data_key=_data_key)
    assert plan["data_by_test"] == {}
    assert plan["missing"], "an empty stored value must block, not resolve"


# ── the flag: off means the previous behaviour, exactly ──────────────────────

def test_the_resolver_is_off_unless_explicitly_enabled(monkeypatch):
    """Default OFF. An operator who has not opted in keeps today's behaviour, and a
    live box can be reverted by unsetting the variable — no rebuild."""
    from app.routers import test_factory as tf

    monkeypatch.delenv("NEXUS_MEMBER_DATA_RESOLVER", raising=False)
    assert tf._member_data_enabled() is False

    for off in ("", "0", "false", "no", "off", "  "):
        monkeypatch.setenv("NEXUS_MEMBER_DATA_RESOLVER", off)
        assert tf._member_data_enabled() is False, off

    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("NEXUS_MEMBER_DATA_RESOLVER", on)
        assert tf._member_data_enabled() is True, on


def test_the_override_key_bridge_matches_the_real_compiler():
    """The resolver writes into the compiled spec's key space. If the compiler's
    key derivation ever moves, this fails rather than the overrides silently
    landing under keys no spec reads."""
    from app.routers.test_factory import compiler_data_key

    for label in ("Member number", "Coverage amount", "Policy No.", "Date of Birth"):
        assert compiler_data_key(label) == _data_key(label), label
