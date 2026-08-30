"""WHERE RUNG 4 SITS IN THE LADDER, AND WHY EACH NEIGHBOUR IS WHERE IT IS.

A rung's VALUE is only half of it; its POSITION is the other half, and position
is what a docstring can claim without it being true. These tests drive
``resolve_field`` itself so the order is measured rather than asserted in prose.

    journey    ABOVE minted   this walk's own committed answer is truer than any
                              reference, because the application is validating
                              the funnel against exactly that value
    minted     ABOVE recalled a reference minted by THIS run minutes ago is live
                              by construction; one remembered from a crawl last
                              month has very likely been consumed or expired
    minted     ABOVE harvest  a harvested value was merely DISPLAYED and may
                              belong to somebody else's test run

Every ordering test carries a control that removes the higher rung, because
"the higher rung won" is satisfied just as well by a lower rung that never fires
at all.
"""
from __future__ import annotations

from app.forms import (PROV_HARVESTED, PROV_MINTED, PROV_RECALLED, AnswerKey,
                       resolve_field)
from app.harvest import HarvestPool
from app.identity_pack import derive
from app.minted import MintRegistry


def _identity():
    return derive("crawl-ladder-1")


def _control(name="Policy Number"):
    return {"name": name, "kind": "text", "question_label": name,
            "frame_origin": "", "value_committed": ""}


def _minted(label="Policy Number", value="POL-90011"):
    reg = MintRegistry()
    reg.mint([{"label": label, "value": value}])
    return reg


def _resolve(**kwargs):
    ctl = kwargs.pop("control", None) or _control()
    return resolve_field(ctl, "text", ctl["name"], AnswerKey({}), _identity(),
                         **kwargs)


# ── minted answers at all ──────────────────────────────────────────────────

def test_a_minted_reference_is_typed_into_the_field_that_asks_for_it():
    got = _resolve(minted=_minted())
    assert got["value"] == "POL-90011"
    assert got["entry"]["provenance"] == PROV_MINTED


def test_no_registry_leaves_the_ladder_exactly_as_it_was():
    """The rung is opt-in at the call site. Every existing caller passes none,
    and must be unaffected."""
    got = _resolve()
    assert got["entry"]["provenance"] != PROV_MINTED


# ── minted is ABOVE memory of previous crawls ──────────────────────────────

def test_a_reference_minted_this_run_beats_one_remembered_from_last_month():
    """THE ONE THAT MATTERS for this rung's position. A policy number recalled
    from a crawl weeks ago has very likely been consumed; the one this run just
    minted is live by construction."""
    ctl = _control()
    sig = resolve_field(ctl, "text", ctl["name"], AnswerKey({}), _identity(),
                        )["entry"]["signature"]
    got = _resolve(control=ctl, minted=_minted(),
                   recalled={sig: "POL-11033"})
    assert got["value"] == "POL-90011"
    assert got["entry"]["provenance"] == PROV_MINTED


def test_the_control_for_that_memory_still_answers_when_nothing_was_minted():
    """FALSIFICATION CONTROL. Without it, a ``recalled`` lookup that was simply
    BROKEN would satisfy the test above and look like correct precedence."""
    ctl = _control()
    sig = resolve_field(ctl, "text", ctl["name"], AnswerKey({}), _identity(),
                        )["entry"]["signature"]
    got = _resolve(control=ctl, recalled={sig: "POL-11033"})
    assert got["value"] == "POL-11033"
    assert got["entry"]["provenance"] == PROV_RECALLED


# ── minted is ABOVE harvest ────────────────────────────────────────────────

def _harvest_with(label, value):
    pool = HarvestPool()
    pool.ingest([{"entities": [{label: value, "Status": "Active"}]}])
    return pool


def test_a_minted_reference_beats_one_merely_displayed_on_a_list_page():
    """A harvested value was displayed and may belong to somebody else's test
    run; a minted one was created by this crawl."""
    got = _resolve(minted=_minted(),
                   harvest=_harvest_with("Policy Number", "POL-77022"))
    assert got["value"] == "POL-90011"
    assert got["entry"]["provenance"] == PROV_MINTED


def test_the_control_for_harvest_it_still_answers_when_nothing_was_minted():
    """FALSIFICATION CONTROL for the test above."""
    got = _resolve(harvest=_harvest_with("Policy Number", "POL-77022"))
    assert got["value"] == "POL-77022"
    assert got["entry"]["provenance"] == PROV_HARVESTED


# ── minted is BELOW this journey's own committed answer ────────────────────

def test_this_walk_s_own_committed_answer_still_wins():
    """The application is cross-validating the funnel against exactly the value
    this walk already committed. A reference must not overwrite it."""
    ctl = _control()
    sig = resolve_field(ctl, "text", ctl["name"], AnswerKey({}), _identity(),
                        )["entry"]["signature"]
    got = _resolve(control=ctl, minted=_minted(),
                   journey_values={sig: "POL-22044"})
    assert got["value"] == "POL-22044"


def test_the_client_s_own_answer_key_still_wins_over_everything():
    """The rung must never outrank what the client stated outright."""
    ctl = _control()
    got = resolve_field(ctl, "text", ctl["name"],
                        AnswerKey.from_payload({"exact": {"Policy Number": "POL-33055"}}),
                        _identity(), minted=_minted())
    assert got["value"] == "POL-33055"


# ── the rung declines rather than guessing ─────────────────────────────────

def test_a_field_no_minted_reference_matches_falls_through():
    got = _resolve(control=_control("First Name"), minted=_minted())
    assert got["entry"]["provenance"] != PROV_MINTED
    assert got["value"], "it must still be filled by a lower rung"


def test_a_toggle_is_state_rather_than_data_and_is_never_minted_into():
    """A checkbox's value is "true"/"false" state. Typing a policy number into
    one is the same category error harvest refuses."""
    ctl = {"name": "Policy Number", "kind": "checkbox",
           "question_label": "Policy Number", "frame_origin": ""}
    got = resolve_field(ctl, "checkbox", "Policy Number", AnswerKey({}),
                        _identity(), minted=_minted())
    assert got["entry"]["provenance"] != PROV_MINTED
