"""THE CLIENT COVERAGE REPORT — every number derived, none typed (E2).

The report exists so a client does not have to learn this system's vocabulary to
find out what happened. That only helps if the prose is a FUNCTION of the
evidence, so the tests below are mostly about one property: change the bundle
and the sentence must change with it.

The fixtures are the three bundles this repository already committed from live
crawls — ``Nexus_power/evidence/gate2/phaseB_*`` — rather than a hand-written
sample. A report tested only against a fixture its author shaped proves the
author can write a fixture.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.services.coverage_report import (ALL_PROVENANCES, CHOSEN_BY_US,
                                          FROM_APPLICATION, FROM_CLIENT,
                                          UNANSWERED, build_report)

_EVIDENCE = pathlib.Path(__file__).resolve().parents[3] / "evidence" / "gate2"
_BUNDLES = sorted(_EVIDENCE.glob("phaseB_*/coverage.json"))


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_live_bundles_are_actually_found():
    """FALSIFICATION CONTROL. Every parametrised test below iterates this glob;
    if it broke they would all pass over nothing and read as a clean suite."""
    assert _BUNDLES, f"no phaseB bundles under {_EVIDENCE}"
    assert len(_BUNDLES) >= 3, [p.parent.name for p in _BUNDLES]


# ── the arithmetic holds on real evidence ───────────────────────────────────

@pytest.mark.parametrize("bundle_path", _BUNDLES, ids=lambda p: p.parent.name)
def test_the_parts_sum_to_the_total(bundle_path):
    r = build_report(_load(bundle_path))
    assert (r.from_client.value + r.from_application.value
            + r.chosen_by_us.value + r.unanswered.value) == r.fields_total.value


@pytest.mark.parametrize("bundle_path", _BUNDLES, ids=lambda p: p.parent.name)
def test_no_provenance_is_silently_dropped(bundle_path):
    """A provenance the fill grows that the grouping does not know would vanish
    from every total and understate the work. It is surfaced, not dropped."""
    r = build_report(_load(bundle_path))
    assert r.unknown_provenances == [], (
        "these provenances are in the bundle but in no group, so their fields "
        f"are missing from the report's totals: {r.unknown_provenances}")


@pytest.mark.parametrize("bundle_path", _BUNDLES, ids=lambda p: p.parent.name)
def test_every_figure_names_the_bundle_key_it_came_from(bundle_path):
    """A figure with no source is a figure somebody typed."""
    for name, fig in build_report(_load(bundle_path)).as_dict()["figures"].items():
        assert fig["source"], f"{name} has no source"


def test_a_real_bundle_produces_the_sentence_the_client_asked_for():
    """vkpower's committed bundle, read off the evidence rather than asserted.

    Its `data_account` is
    {"answered_to_unblock": 7, "harvested": 4, "intent_unmet": 1,
     "needs_input": 3, "synthesized": 41} — so the honest sentence says ZERO
    came from the client, which is the single most useful thing this report can
    tell somebody who believes their data was exercised.
    """
    path = next(p for p in _BUNDLES if "vkpower" in p.parent.name)
    r = build_report(_load(path))
    assert r.from_client.value == 0
    assert r.from_application.value == 4      # harvested
    assert r.chosen_by_us.value == 48         # 7 unblock + 41 synthesized
    assert r.unanswered.value == 4            # 1 intent_unmet + 3 needs_input
    assert r.fields_total.value == 56
    assert r.headline() == (
        "56 fields. 0 came from your data, 4 the application supplied itself, "
        "48 we chose, and 4 still need an answer from you.")


# ── derived, not typed ──────────────────────────────────────────────────────

def test_the_sentence_follows_the_bundle():
    """THE POINT OF THE WHOLE MODULE. Same code, different evidence, different
    sentence — so the prose cannot be a constant that happens to look right."""
    a = build_report({"data_account": {"provided": 2, "synthesized": 1}})
    b = build_report({"data_account": {"provided": 9, "synthesized": 4,
                                       "needs_input": 7}})
    assert a.headline() != b.headline()
    assert a.headline().startswith("3 fields. 2 came from your data")
    assert b.headline().startswith("20 fields. 9 came from your data")


def test_an_empty_bundle_says_nothing_rather_than_guessing():
    """A crawl that stopped early has no data_account. The report must be total
    and must not invent a number to fill the sentence."""
    r = build_report({})
    assert r.fields_total.value == 0
    assert r.headline().startswith("0 fields. 0 came from your data")
    assert r.questions_needing_you == []


def test_none_is_survivable():
    assert build_report(None).fields_total.value == 0


# ── the taxonomy itself ─────────────────────────────────────────────────────

def test_the_groups_do_not_overlap():
    """A provenance in two groups would be counted twice and the parts would
    stop summing to the total — silently, on some bundles and not others."""
    groups = [FROM_CLIENT, FROM_APPLICATION, CHOSEN_BY_US, UNANSWERED]
    flat = [p for g in groups for p in g]
    assert len(flat) == len(set(flat)), "a provenance appears in two groups"


def test_a_group_sibling_is_neither_a_gap_nor_an_answer():
    """It must not inflate 'we chose' and must not be asked of the client: its
    question was answered by the member that IS the answer."""
    r = build_report({"data_account": {"provided": 1, "group_sibling": 5}})
    assert r.fields_total.value == 1
    assert r.chosen_by_us.value == 0
    assert r.unanswered.value == 0
    assert r.unknown_provenances == []


def test_an_unknown_provenance_is_reported_not_swallowed():
    """The drift guard, stated as a test rather than as a comment."""
    r = build_report({"data_account": {"provided": 1, "teleported": 3}})
    assert r.unknown_provenances == ["teleported"]
    assert "teleported" not in ALL_PROVENANCES


# ── the lists a client acts on ──────────────────────────────────────────────

def test_the_questions_needing_you_come_from_the_ledger_with_their_reason():
    r = build_report({"field_ledger": [
        {"name": "Security PIN", "provenance": "needs_input",
         "reason": "secret_must_not_be_invented", "url": "u"},
        {"name": "First name", "provenance": "synthesized"},
    ]})
    assert [q["name"] for q in r.questions_needing_you] == ["Security PIN"]
    assert r.questions_needing_you[0]["reason"] == "secret_must_not_be_invented"


def test_a_malformed_bundle_does_not_crash_the_report():
    """Bundles are written by a crawl that may have stopped anywhere."""
    r = build_report({"data_account": "not-a-mapping", "field_ledger": 7,
                      "validation_rejections": None, "flow_summary": []})
    assert r.fields_total.value == 0
    assert r.rejections == []
