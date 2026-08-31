"""B4 — AN OPERATOR'S SEED REACHES THE FIELD INSTANCE THAT ACTUALLY BLOCKS.

MEASURED over four seeded rounds on summit-life-carrier
(PHASE_1_EXIT_RESCOPE.md §B4): the wizard renders ``Face Amount ($)`` while the
operator seeded ``Face Amount``, and ``Last Physical Exam`` against ``Last Exam
Date``. Those hash apart, so both wizard copies stayed ``synthesized`` while the
seeds landed — correctly — on same-named fields elsewhere in the application.
``fields_needing_seed`` converged 6 → 4 → 8 → 1 across those rounds while the
two values that actually failed never appeared on it once.

A seed may now ALSO be keyed by its LABEL. The signature is still tried first
and still wins, so every seed that resolves today resolves identically; the
label is the weaker fallback that reaches the near-duplicate. Matching follows
the client data library's rule — exact on normalised text, then whole-phrase
containment either way — and refuses an ambiguous pair for the same reason rung
2 refuses one: carrying the wrong value is worse than carrying none.
"""
from __future__ import annotations

from app.forms import (AnswerKey, PROV_RECALLED, _norm_seed_label,
                       _seed_by_label, resolve_field)
from app.identity_pack import derive

_SIG = "a" * 32


def _identity():
    return derive("crawl-b4")


def _control(name):
    return {"name": name, "kind": "text", "question_label": name,
            "frame_origin": "", "value_committed": ""}


def _resolve(name, recalled):
    ctl = _control(name)
    return resolve_field(ctl, "text", name, AnswerKey(), _identity(),
                         recalled=recalled)


# ── the measured case ──────────────────────────────────────────────────────

def test_a_face_amount_seed_reaches_the_wizard_s_face_amount_dollars():
    """THE ONE THAT MATTERS. This exact pair is why four rounds of seeding
    never reached the value that stopped the funnel."""
    got = _resolve("Face Amount ($)", {"Face Amount": "750000"})
    assert got["value"] == "750000"
    assert got["entry"]["provenance"] == PROV_RECALLED


def test_a_trailing_qualifier_is_bridged_too():
    """The same shape as the measured pair: the seed is the question, the form
    adds something for the reader."""
    got = _resolve("Annual Income (before tax)", {"Annual Income": "67000"})
    assert got["value"] == "67000"


def test_the_second_measured_pair_is_NOT_bridged_and_that_is_deliberate():
    """NOT CLAIMED, and the honest half of B4. The re-scope also names
    ``Last Physical Exam`` against ``Last Exam Date`` — those differ by
    REORDERED AND DIFFERENT WORDS, not by a unit a form appended, and no
    containment rule reaches one from the other. Loosening the matcher until it
    did would trade a missed seed (safe: it falls to the next rung) for a wrong
    seed (unsafe: an operator's value in a field it does not answer), which is
    the trade rung 2 already refuses. Bridging this needs the operator to be
    ASKED which field they meant, not guessed at."""
    got = _resolve("Last Physical Exam", {"Last Exam Date": "2025-03-01"})
    assert got["entry"]["provenance"] != PROV_RECALLED


def test_the_control_an_unseeded_field_is_untouched():
    """FALSIFICATION CONTROL. A fallback that answered EVERYTHING would satisfy
    the tests above and quietly put one operator value into every field."""
    got = _resolve("Occupation", {"Face Amount": "750000"})
    assert got["entry"]["provenance"] != PROV_RECALLED
    assert got["value"], "it must still be filled by a lower rung"


# ── the signature still wins ───────────────────────────────────────────────

def test_a_signature_keyed_seed_is_unchanged_and_still_preferred():
    """Every seed that resolves today must resolve identically: the label path
    is consulted ONLY after the signature misses."""
    ctl = _control("Face Amount ($)")
    sig = resolve_field(ctl, "text", ctl["name"], AnswerKey(), _identity(),
                        )["entry"]["signature"]
    got = _resolve("Face Amount ($)",
                   {sig: "FROM-SIGNATURE", "Face Amount": "FROM-LABEL"})
    assert got["value"] == "FROM-SIGNATURE"


def test_a_signature_key_is_never_read_as_a_label():
    """32 hex characters is a signature, not a question. Reading one as a label
    would let a stale signature answer an unrelated field."""
    assert _seed_by_label({_SIG: "v"}, "Anything") is None


# ── ambiguity is refused, as rung 2 refuses it ─────────────────────────────

def test_two_seeds_that_both_claim_one_field_are_refused():
    got = _seed_by_label({"Face Amount": "1", "Amount": "2"}, "Face Amount ($)")
    assert got is None


def test_the_control_for_ambiguity_one_seed_alone_answers():
    """FALSIFICATION CONTROL for the refusal above."""
    assert _seed_by_label({"Face Amount": "1"}, "Face Amount ($)") == "1"


def test_a_short_key_never_matches_by_containment():
    """"ID" inside "Valid ID Number" is a substring, not the same question.
    Containment needs four characters, the same floor harvest uses."""
    assert _seed_by_label({"ID": "x"}, "Valid Identification Number") is None


def test_an_empty_seed_value_is_not_an_answer():
    assert _seed_by_label({"Face Amount": ""}, "Face Amount ($)") is None
    assert _seed_by_label({}, "Face Amount") is None
    assert _seed_by_label({"Face Amount": "1"}, "") is None


# ── the normalisation, stated ──────────────────────────────────────────────

def test_the_units_a_form_adds_for_the_reader_are_not_the_question():
    for label in ("Face Amount ($)", "Face Amount (USD)", "Face  Amount",
                  "FACE AMOUNT", "Face Amount:"):
        assert _norm_seed_label(label) == "face amount", label


def test_a_parenthetical_that_is_the_whole_label_does_not_vanish_silently():
    """Normalising to nothing must not then match everything."""
    assert _norm_seed_label("($)") == ""
    assert _seed_by_label({"($)": "x"}, "Face Amount") is None


# ── B4 (second half) · EVERY STEP-0 QUESTION HAS A ROW THE WIZARD CAN SEE ──
# MEASURED LIVE 2026-08-31 (summit run on the a07cb59+ ledger): the five
# fields the Phase-1 exit re-scope recorded as "absent from the field ledger
# entirely" each carry exactly one row, filed under the page that met them
# first, with the wizard named in also_seen_at. This pins that live shape as
# a unit over the same CoverageLedger the crawl uses, so "zero questions with
# no row" is a property a test holds rather than a sentence a bundle implies.

def test_the_five_summit_fields_each_have_a_row_the_wizard_can_claim():
    from app.coverage import CoverageLedger

    class _Host:
        def __init__(self):
            self._field_ledger = []

    profile = "http://x/customers/profile"
    wizard = "http://x/underwriting/new-business/new-application"
    five = ["First Name", "Last Name", "Date of Birth", "Email Address",
            "Gender"]
    host = _Host()
    led = CoverageLedger(host)
    # The live order: the profile page is crawled first and the URL-free
    # signatures collide, which is exactly what used to drop the wizard rows.
    led.collect_ledger(
        [{"signature": "sig-" + n.lower(), "name": n, "filled": True}
         for n in five], profile)
    led.collect_ledger(
        [{"signature": "sig-" + n.lower(), "name": n, "filled": True}
         for n in five], wizard)
    rows = {r["name"]: r for r in host._field_ledger}
    missing = [n for n in five if n not in rows]
    assert not missing, "questions with NO row at all: %r" % missing
    invisible = [n for n in five
                 if wizard not in (rows[n].get("also_seen_at") or [])
                 and rows[n].get("url") != wizard]
    assert not invisible, (
        "rows the wizard cannot claim as its own: %r — this is the shipped "
        "collision bug come back" % invisible)
    assert len(host._field_ledger) == len(five), "the ask must never repeat"
