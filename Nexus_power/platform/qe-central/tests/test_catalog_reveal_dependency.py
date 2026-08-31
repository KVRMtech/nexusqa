"""Tier 2 — the reveal->child join reaches the catalogue.

The branch walk proves trigger->child relationships (qec_011 ``reveals``) and
``journey_projector.rules_from_branches`` resolves them to question ids. Both
halves have worked since P1, and the CATALOGUE never read either: a question
that exists only because another was answered a particular way was published
with ``depends_on`` empty, indistinguishable from an unconditional one.

Every test here is pure — plain dicts, no DB, no browser — and the file is built
around one control: :func:`test_without_the_join_the_dependency_is_invisible`
runs the identical evidence through the identical builder with the join omitted
and requires ``depends_on`` to come back empty. Without it, every green below
would also be green if the join did nothing at all.
"""
from __future__ import annotations

from app.services import catalog
from app.services.catalog import (DEPENDS_ON_DECLARED, DEPENDS_ON_PROVEN,
                                  MAX_REVEALED_BY, question_id_for)
from app.services.journey_projector import rules_from_branches


# ─── The pure join ───────────────────────────────────────────────────────────

def test_a_proven_reveal_fills_an_empty_dependency():
    qs = [{"question_id": "q1", "name": "Do you use tobacco?"},
          {"question_id": "q2", "name": "Cigarettes per day"}]
    gained = catalog.apply_reveal_dependencies(
        qs, [{"question_id": "q1", "option": "yes",
              "reveals_question_ids": ["q2"]}])

    assert gained == 1
    child = qs[1]
    assert child["depends_on"] == "Do you use tobacco?"
    assert child["depends_on_source"] == DEPENDS_ON_PROVEN
    assert child["revealed_by"] == [
        {"question_id": "q1", "question": "Do you use tobacco?", "option": "yes"}]
    assert child["revealed_by_total"] == 1
    # The TRIGGER is not itself dependent on anything.
    assert not qs[0].get("depends_on")


def test_a_declared_dependency_is_never_overwritten_but_the_reveal_is_kept():
    """The two can disagree, and the disagreement is the part worth reading."""
    qs = [{"question_id": "q1", "name": "Do you use tobacco?"},
          {"question_id": "q2", "name": "Cigarettes per day",
           "depends_on": "Smoker status"}]
    gained = catalog.apply_reveal_dependencies(
        qs, [{"question_id": "q1", "option": "yes",
              "reveals_question_ids": ["q2"]}])

    assert gained == 0                     # nothing was upgraded
    child = qs[1]
    assert child["depends_on"] == "Smoker status"        # the page's own claim
    assert child["depends_on_source"] == DEPENDS_ON_DECLARED
    # ...and the observation survives beside it, naming a DIFFERENT question.
    assert child["revealed_by"][0]["question"] == "Do you use tobacco?"


def test_a_trigger_the_catalogue_does_not_hold_is_dropped_not_faked():
    qs = [{"question_id": "q2", "name": "Cigarettes per day"}]
    gained = catalog.apply_reveal_dependencies(
        qs, [{"question_id": "q-missing", "option": "yes",
              "reveals_question_ids": ["q2"]}])

    assert gained == 0
    assert not qs[0].get("depends_on")
    assert not qs[0].get("revealed_by")


def test_an_unnameable_trigger_records_the_reveal_but_writes_no_dependency():
    """An UNVERIFIED-name question can still reveal a child.

    The reveal was observed either way, so it is recorded — but writing an empty
    string into ``depends_on`` would say "depends on nothing", which is the
    opposite of what was proven.
    """
    qs = [{"question_id": "q1", "name": ""},
          {"question_id": "q2", "name": "Cigarettes per day"}]
    gained = catalog.apply_reveal_dependencies(
        qs, [{"question_id": "q1", "option": "yes",
              "reveals_question_ids": ["q2"]}])

    assert gained == 0
    assert not qs[1].get("depends_on")
    assert qs[1]["revealed_by"] == [
        {"question_id": "q1", "question": "", "option": "yes"}]


def test_several_triggers_accumulate_and_a_clipped_list_stays_visibly_clipped():
    qs = [{"question_id": "q%d" % i, "name": "Trigger %02d" % i}
          for i in range(MAX_REVEALED_BY + 4)]
    qs.append({"question_id": "child", "name": "Follow-up"})
    rules = [{"question_id": q["question_id"], "option": "yes",
              "reveals_question_ids": ["child"]} for q in qs[:-1]]

    catalog.apply_reveal_dependencies(qs, rules)
    child = qs[-1]

    assert len(child["revealed_by"]) == MAX_REVEALED_BY
    assert child["revealed_by_total"] == MAX_REVEALED_BY + 4
    assert child["depends_on_source"] == DEPENDS_ON_PROVEN


def test_the_join_is_deterministic_under_reordered_evidence():
    """Two reads of one database must not order this differently."""
    def build(order):
        qs = [{"question_id": "q1", "name": "Beta"},
              {"question_id": "q2", "name": "Alpha"},
              {"question_id": "child", "name": "Follow-up"}]
        rules = [{"question_id": "q1", "option": "yes",
                  "reveals_question_ids": ["child"]},
                 {"question_id": "q2", "option": "no",
                  "reveals_question_ids": ["child"]}]
        catalog.apply_reveal_dependencies(qs, [rules[i] for i in order])
        return qs[2]["revealed_by"]

    assert build([0, 1]) == build([1, 0])
    assert [r["question"] for r in build([0, 1])] == ["Alpha", "Beta"]


def test_a_self_reveal_never_makes_a_question_depend_on_itself():
    qs = [{"question_id": "q1", "name": "Do you use tobacco?"}]
    catalog.apply_reveal_dependencies(
        qs, [{"question_id": "q1", "option": "yes",
              "reveals_question_ids": ["q1"]}])
    assert not qs[0].get("depends_on")


def test_no_rules_at_all_leaves_every_question_untouched():
    qs = [{"question_id": "q1", "name": "A"}]
    assert catalog.apply_reveal_dependencies(qs, None) == 0
    assert catalog.apply_reveal_dependencies(qs, []) == 0
    assert "revealed_by" not in qs[0]


# ─── Through the real builder, over real branch rows ─────────────────────────

_NODES = [{"node_fp": "n1", "title": "Health", "controls": [
    {"name": "Cigarettes Per Day", "signature": "sig-cig", "type": "number"}]}]
_BRANCHES = [
    {"node_fp": "n1", "control_signature": "q:tobacco",
     "control_label_norm": "tobacco use", "option_label_norm": "yes",
     "reveals": ["input:cigarettes per day"]},
    {"node_fp": "n1", "control_signature": "q:tobacco",
     "control_label_norm": "tobacco use", "option_label_norm": "no"},
]


def _build(with_join: bool):
    fn = None
    if with_join:
        def fn(questions):                                  # noqa: F811
            return rules_from_branches(_BRANCHES, questions)
    return catalog.build_master_catalog(_NODES, branches=_BRANCHES,
                                        reveal_rules_fn=fn)


def test_the_builder_carries_a_proven_dependency_end_to_end():
    """The whole path: branch rows -> resolved rules -> catalogue row."""
    master = _build(with_join=True)
    by_id = {q["question_id"]: q for q in master["questions"]}
    child = by_id[question_id_for({"name": "Cigarettes Per Day",
                                   "signature": "sig-cig"})]

    assert child["depends_on"] == "tobacco use"
    assert child["depends_on_source"] == DEPENDS_ON_PROVEN
    assert child["revealed_by"] == [{
        "question_id": question_id_for({"signature": "q:tobacco",
                                        "name": "tobacco use"}),
        "question": "tobacco use",
        "option": "yes",
    }]


def test_without_the_join_the_dependency_is_invisible():
    """THE CONTROL. The same evidence through the same builder, join omitted.

    This is the defect exactly as it stood: the branch row carrying ``reveals``
    is present, the child question is catalogued, and the artifact still says
    the child hangs off nothing. If this test ever passes with a non-empty
    ``depends_on``, the joins above are proving something other than they claim.
    """
    master = _build(with_join=False)
    by_id = {q["question_id"]: q for q in master["questions"]}
    child = by_id[question_id_for({"name": "Cigarettes Per Day",
                                   "signature": "sig-cig"})]

    assert not child.get("depends_on")
    assert child["depends_on_source"] == ""
    assert child["revealed_by"] == []
    assert master["summary"]["with_proven_dependency"] == 0


def test_every_row_carries_the_provenance_field_even_with_nothing_to_say():
    """Present on some rows and absent on others is read as a bug."""
    for master in (_build(with_join=True), _build(with_join=False)):
        for q in master["questions"]:
            assert "depends_on_source" in q
            assert "revealed_by" in q
            assert "revealed_by_total" in q


def test_the_summary_counts_the_two_kinds_of_dependency_apart():
    master = _build(with_join=True)
    summary = master["summary"]

    assert summary["with_proven_dependency"] == 1
    assert summary["with_declared_dependency"] == 0
    assert summary["revealed_by_a_trigger"] == 1
    # The pre-existing total still counts BOTH, so no reader of it regresses.
    assert summary["with_dependency"] == 1


def test_a_failing_resolver_costs_the_dependency_and_not_the_catalogue():
    """The join is the last, additive step; a catalogue is not lost to it."""
    def _boom(_questions):
        raise RuntimeError("resolver exploded")

    master = catalog.build_master_catalog(_NODES, branches=_BRANCHES,
                                          reveal_rules_fn=_boom)
    assert len(master["questions"]) == len(_build(with_join=False)["questions"])
    assert master["summary"]["with_proven_dependency"] == 0
