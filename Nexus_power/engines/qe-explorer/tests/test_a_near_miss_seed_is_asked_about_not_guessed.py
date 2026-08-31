"""B4, LIMIT CLOSED THE HONEST WAY — A NEAR-MISS SEED IS ASKED ABOUT.

The re-scope's second measured pair, ``Last Physical Exam`` against ``Last Exam
Date``, differs by reordered and different words. No containment rule reaches
one from the other, and loosening the matcher until it did would put an
operator's value into a field it may not answer — the trade rung 2 refuses.

So the walk does the one honest thing left: it NAMES the near-miss on the
field's own ledger row, and coverage rolls it up into ``seed_near_misses`` —
the ask "did you mean this field?" — instead of guessing. Labels only; the
seed's value never travels.
"""
from __future__ import annotations

from app.coverage import _seed_near_misses
from app.forms import (AnswerKey, PROV_RECALLED, _seed_near_miss,
                       resolve_field)
from app.identity_pack import derive


def _resolve(name, recalled):
    ctl = {"name": name, "kind": "text", "question_label": name,
           "frame_origin": "", "value_committed": ""}
    return resolve_field(ctl, "text", name, AnswerKey(), derive("crawl-b4b"),
                         recalled=recalled)


# ── the measured pair: asked, not guessed ──────────────────────────────────

def test_the_second_measured_pair_is_named_as_an_ask_and_not_applied():
    """THE LIMIT, CLOSED HONESTLY. The seed is NOT applied (provenance is not
    recalled) and the near-miss IS on the row for the operator."""
    got = _resolve("Last Physical Exam", {"Last Exam Date": "2025-03-01"})
    assert got["entry"]["provenance"] != PROV_RECALLED
    assert got["entry"]["seed_near_miss"] == "Last Exam Date"
    assert "2025-03-01" not in repr(got["entry"]), "the VALUE never travels"


def test_a_true_match_is_applied_and_leaves_no_ask_behind():
    """FALSIFICATION CONTROL. A matcher that only ever asked would pass the test
    above and never fill anything — the real match must still be applied and
    must NOT also be recorded as a near-miss."""
    got = _resolve("Face Amount ($)", {"Face Amount": "750000"})
    assert got["entry"]["provenance"] == PROV_RECALLED
    assert "seed_near_miss" not in got["entry"]


def test_an_unrelated_seed_is_neither_applied_nor_asked_about():
    assert _seed_near_miss({"Occupation": "nurse"}, "Last Physical Exam") == ""


def test_two_close_seeds_are_two_questions_not_one_guess():
    """Naming ONE candidate when there are two would be a guess wearing an
    ask's clothes."""
    seeds = {"Last Exam Date": "a", "Physical Exam Date": "b"}
    assert _seed_near_miss(seeds, "Last Physical Exam") == ""


def test_a_signature_key_is_never_a_near_miss_candidate():
    assert _seed_near_miss({"a" * 32: "v"}, "Last Physical Exam") == ""


def test_one_shared_word_is_not_close():
    """"Date" alone links half the form; two significant tokens is the floor."""
    assert _seed_near_miss({"Policy Start Date": "x"}, "Last Exam Date") == ""


# ── the ask reaches the operator ───────────────────────────────────────────

def test_coverage_rolls_the_ask_up_once_per_field_and_page():
    ledger = [
        {"name": "Last Physical Exam", "url": "http://x/apply",
         "seed_near_miss": "Last Exam Date", "provenance": "synthesized"},
        {"name": "Last Physical Exam", "url": "http://x/apply",
         "seed_near_miss": "Last Exam Date", "provenance": "synthesized"},
        {"name": "Face Amount ($)", "url": "http://x/apply",
         "provenance": "recalled"},
    ]
    got = _seed_near_misses(ledger)
    assert got == [{"field": "Last Physical Exam", "seed": "Last Exam Date",
                    "url": "http://x/apply"}]


def test_the_roll_up_carries_labels_and_never_a_value():
    ledger = [{"name": "F", "url": "u", "seed_near_miss": "G",
               "value": "SECRET-1"}]
    assert "SECRET-1" not in repr(_seed_near_misses(ledger))


def test_an_empty_ledger_yields_an_empty_ask_rather_than_nothing():
    assert _seed_near_misses([]) == []
    assert _seed_near_misses(None) == []
