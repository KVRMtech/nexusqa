"""Value harvest — earn the classification that lets a suite run as another member.

Phase 1 of MEMBER_DATA_RESOLVER_PLAN. Until this exists the classifier has to be
hand-fed two observed maps, so in practice nothing is ever classified and every
value stays pinned to whoever the crawl ran as.

The properties that matter:
  * a harvest is keyed EXACTLY as a classification and an answer sheet are keyed,
    so no translation stands between observing a value and using it;
  * a verdict is EARNED by comparing two members — never inferred from what a
    field is called;
  * one member alone proves nothing, and a value seen for only one of the two is
    structural, never assumed shared;
  * a value that merely drifts over time is volatile, not identity.

Pure — no DB, no live stack.
"""
from __future__ import annotations

from app.services.test_factory import value_harvest as vh
from app.services.test_factory import persona_diff
from app.services.test_factory import member_data_resolver as mdr


def _step(n: int, label: str, value: str = "", text: str = "", expected: str = "") -> dict:
    observed: dict = {"kind": "field", "verb": "type", "label": label}
    if value:
        observed["value"] = value
    if text:
        observed["text"] = text
    return {"step_number": n, "expected_result": expected, "observed": observed}


def _case(scenario_id: str, steps: list) -> dict:
    return {"test_id": scenario_id, "steps": steps}


# ── harvesting ───────────────────────────────────────────────────────────────

def test_a_harvest_is_keyed_exactly_as_classifications_and_answers_are():
    """The whole point of the key shape: what we observe can be stored as a
    member's answers and compared with another member's, untranslated."""
    cases = [_case("tc-1", [_step(1, "Member number", value="8891234",
                                  expected="'Member number' shows '8891234'")])]
    got = vh.harvest(cases)
    assert got == {
        "tc-1:1:observed_value": "8891234",
        "tc-1:1:expected": "'Member number' shows '8891234'",
    }


def test_a_harvest_feeds_the_resolver_without_translation():
    """A harvested key must be the same key the resolver blocks and resolves on —
    if these two ever drift, classification silently stops applying."""
    cases = [_case("tc-1", [_step(1, "Member number", value="8891234")])]
    harvested = vh.harvest(cases)
    key = "tc-1:1:observed_value"
    assert key in harvested

    plan = mdr.plan_member_data(
        cases,
        classifications={key: {"class": persona_diff.CLASS_MEMBER_DERIVED}},
        answers={key: "5550001"},
        data_key=lambda s: s.lower().replace(" ", "-"))
    assert plan["data_by_test"] == {"tc-1": {"member-number": "5550001"}}


def test_page_text_is_harvested_as_its_own_kind():
    cases = [_case("tc-1", [_step(1, "Greeting", text="Welcome back, A Name")])]
    assert vh.harvest(cases) == {"tc-1:1:observed_text": "Welcome back, A Name"}


def test_blank_and_missing_values_are_not_harvested():
    cases = [_case("tc-1", [_step(1, "Nothing"), _step(2, "Blank", value="   ")])]
    assert vh.harvest(cases) == {}


def test_harvest_survives_malformed_input():
    for cases in ([], None, [{}], [{"steps": "not-a-list"}], [{"test_id": ""}]):
        assert vh.harvest(cases) == {}


# ── earning the verdict ──────────────────────────────────────────────────────

def test_a_value_that_differs_between_members_is_theirs():
    got = vh.compare(observed_a={"k:1:observed_value": "8891234"},
                     observed_b={"k:1:observed_value": "5550001"})
    assert got["k:1:observed_value"]["class"] == persona_diff.CLASS_MEMBER_DERIVED


def test_a_value_identical_for_both_members_belongs_to_the_application():
    got = vh.compare(observed_a={"k:1:observed_value": "Life cover"},
                     observed_b={"k:1:observed_value": "Life cover"})
    assert got["k:1:observed_value"]["class"] == persona_diff.CLASS_APP_CONSTANT


def test_a_value_seen_for_only_one_member_is_structural_not_shared():
    """Absence of evidence for the second member is not evidence they agree."""
    got = vh.compare(observed_a={"k:1:observed_value": "only-A"},
                     observed_b={"k:2:observed_value": "only-B"})
    assert got["k:1:observed_value"]["class"] == persona_diff.CLASS_STRUCTURAL
    assert got["k:2:observed_value"]["class"] == persona_diff.CLASS_STRUCTURAL


def test_one_member_alone_classifies_nothing():
    """Guessing identity from a single observation is the heuristic this design
    exists to refuse."""
    assert vh.compare(observed_a={"k:1:observed_value": "8891234"}, observed_b={}) == {}
    assert vh.compare(observed_a={}, observed_b={"k:1:observed_value": "x"}) == {}
    assert vh.compare(observed_a=None, observed_b=None) == {}


def test_the_store_shape_is_accepted_on_both_sides():
    """Harvests come back from the answer sheet as {expected_value, source}."""
    rich = {"k:1:observed_value": {"expected_value": "8891234", "source": "harvest"}}
    flat = {"k:1:observed_value": "5550001"}
    got = vh.compare(observed_a=rich, observed_b=flat)
    assert got["k:1:observed_value"]["class"] == persona_diff.CLASS_MEMBER_DERIVED


def test_an_identity_echo_is_member_derived_even_when_the_diff_is_unsure():
    """A page echoing the member's own identifier back is theirs by construction."""
    got = vh.compare(observed_a={"k:1:observed_text": "Signed in as 8891234"},
                     observed_b={"k:1:observed_text": "Signed in as 8891234"},
                     identity_values={"8891234"})
    assert got["k:1:observed_text"]["class"] == persona_diff.CLASS_MEMBER_DERIVED


def test_nothing_is_classified_from_a_field_name():
    """GENERIC. Two members seeing the SAME value are app-constant no matter how
    identity-flavoured the field is called; two members seeing DIFFERENT values
    are member-derived no matter how mundane it is called."""
    same = vh.compare(observed_a={"tc:1:observed_value": "X"},
                      observed_b={"tc:1:observed_value": "X"})
    assert same["tc:1:observed_value"]["class"] == persona_diff.CLASS_APP_CONSTANT

    differs = vh.compare(observed_a={"tc:2:observed_value": "blue"},
                         observed_b={"tc:2:observed_value": "green"})
    assert differs["tc:2:observed_value"]["class"] == persona_diff.CLASS_MEMBER_DERIVED


def test_a_value_that_drifts_against_a_members_own_control_run_is_volatile():
    """Time-drift must not be mistaken for identity: compare A against A."""
    got = vh.compare(
        observed_a={"k:1:observed_text": "12:01:00"},
        observed_b={"k:1:observed_text": "12:04:31"},
        control_a={"k:1:observed_text": "12:00:02"})
    assert got["k:1:observed_text"]["class"] == persona_diff.CLASS_VOLATILE


# ── the loop closes ──────────────────────────────────────────────────────────

def test_two_harvests_classify_and_the_result_drives_the_resolver():
    """End to end, pure: observe two members -> classify -> the resolver then
    redirects the member-derived field and leaves the shared one alone."""
    suite_a = [_case("tc-1", [_step(1, "Member number", value="8891234"),
                              _step(2, "Product", value="Life cover")])]
    suite_b = [_case("tc-1", [_step(1, "Member number", value="5550001"),
                              _step(2, "Product", value="Life cover")])]

    classifications = vh.compare(observed_a=vh.harvest(suite_a),
                                 observed_b=vh.harvest(suite_b))
    assert classifications["tc-1:1:observed_value"]["class"] == persona_diff.CLASS_MEMBER_DERIVED
    assert classifications["tc-1:2:observed_value"]["class"] == persona_diff.CLASS_APP_CONSTANT

    plan = mdr.plan_member_data(
        suite_a, classifications=classifications,
        answers={"tc-1:1:observed_value": "5550001"},
        data_key=lambda s: s.lower().replace(" ", "-"))

    assert plan["data_by_test"] == {"tc-1": {"member-number": "5550001"}}
    assert plan["missing"] == []          # the shared value needs no answer


def test_the_same_loop_blocks_when_the_member_has_no_answer():
    suite_a = [_case("tc-1", [_step(1, "Member number", value="8891234")])]
    suite_b = [_case("tc-1", [_step(1, "Member number", value="5550001")])]
    classifications = vh.compare(observed_a=vh.harvest(suite_a),
                                 observed_b=vh.harvest(suite_b))
    plan = mdr.plan_member_data(
        suite_a, classifications=classifications, answers={},
        data_key=lambda s: s.lower().replace(" ", "-"))
    assert plan["data_by_test"] == {}
    assert [m["value_key"] for m in plan["missing"]] == ["tc-1:1:observed_value"]
