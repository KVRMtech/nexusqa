"""THE PRODUCER SIDE OF A CONTRACT NOTHING HAD EVER WRITTEN.

qe-central folds a crawl by looking each journey step's fingerprint up in
``build_states_index(coverage)``, which reads ``coverage.states``. The consumer
was written, shipped and unit-tested. The producer never existed, and nothing
failed loudly when it didn't: an empty index is indistinguishable from an
application with no form fields.

Measured consequence on a five-step insurance application whose walk completed
end to end, every step recorded and every edge folded:

  * ``journey_nodes.controls_inventory`` was empty on all five wizard nodes;
  * the Master Catalog could therefore only be fed by journey BRANCHES, which
    carry choices — so all 24 catalogued questions were enumerable (gender,
    product, premium mode, tobacco use, the health conditions) and not one text,
    date or number field was present;
  * ``faceAmount`` — a number input declaring ``step=10000``, the clearest
    boundary rule on the form — was absent, so no boundary scenario could ever
    be derived from it.

The walk was fine. The catalogue it fed was starved, and the catalogue is the
product's deliverable.

These tests pin the field NAMES as much as the behaviour: this is a contract
between two services that share no library, and a rename on either side is
silent in exactly the way the missing producer was.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.crawler import Crawler, _MAX_COVERAGE_STATES, _MAX_STATE_FIELDS


#: The keys qe-central's build_states_index / extract_controls read. Mirrored
#: here deliberately — the services share no library, so this list IS the
#: contract. Change both sides or neither.
CONTRACT_KEYS = ("ax_fingerprint", "location", "form_snapshot_signals")


def _me():
    return SimpleNamespace(_states={})


def _signals(*names):
    return {n: {"type": "text", "required": True} for n in names}


def test_a_state_is_recorded_under_the_fingerprint_the_graph_uses():
    """The journey graph keys nodes by fingerprint and looks them up by it. Any
    other key and the fold finds nothing — which is the failure being fixed."""
    me = _me()
    Crawler._note_state_signals(me, "fp1", "https://app/apply", _signals("Email"))
    out = Crawler._state_signals(me)
    assert len(out) == 1
    assert out[0]["ax_fingerprint"] == "fp1"
    assert out[0]["location"] == "https://app/apply"
    assert set(out[0]) == set(CONTRACT_KEYS)


def test_the_questions_are_carried_not_the_answers():
    """VALUE-FREE BY CONSTRUCTION. ``form_snapshot_signals`` is label→shape;
    its sibling ``form_snapshot`` is label→COMMITTED VALUE and must never cross
    this boundary. Shapes leave the tenant; answers never do."""
    me = _me()
    Crawler._note_state_signals(
        me, "fp1", "https://app/apply",
        {"Email": {"type": "text", "required": True, "options": []}})
    blob = repr(Crawler._state_signals(me))
    assert "form_snapshot_signals" in blob
    assert "form_snapshot\"" not in blob and "'form_snapshot'" not in blob
    assert "qa.autotest@example.com" not in blob


def test_the_richest_sighting_of_a_state_wins():
    """A wizard step is met on entry and again mid-walk, and a dependent question
    offers nothing until its driver is answered. Keeping the first sighting holds
    the emptiest view of exactly the questions hardest to enumerate."""
    me = _me()
    Crawler._note_state_signals(me, "fp1", "https://app/x", _signals("A"))
    Crawler._note_state_signals(me, "fp1", "https://app/x", _signals("A", "B", "C"))
    out = Crawler._state_signals(me)
    assert len(out) == 1
    assert set(out[0]["form_snapshot_signals"]) == {"A", "B", "C"}


def test_a_poorer_later_sighting_never_erodes_what_was_seen():
    me = _me()
    Crawler._note_state_signals(me, "fp1", "https://app/x", _signals("A", "B", "C"))
    Crawler._note_state_signals(me, "fp1", "https://app/x", _signals("A"))
    assert set(Crawler._state_signals(me)[0]["form_snapshot_signals"]) == {"A", "B", "C"}


def test_a_state_that_asked_nothing_is_not_recorded():
    """A page with no form fields contributes no questions. Recording it would
    put empty rows in a report whose job is to say what the app asks."""
    me = _me()
    Crawler._note_state_signals(me, "fp1", "https://app/x", {})
    Crawler._note_state_signals(me, "", "https://app/x", _signals("A"))
    assert Crawler._state_signals(me) == []


def test_the_index_is_bounded():
    """Coverage is a REPORT, not a second copy of the manifest. One pathological
    application must not turn the stats column into an evidence store."""
    me = _me()
    for i in range(_MAX_COVERAGE_STATES + 25):
        Crawler._note_state_signals(me, f"fp{i}", "https://app/x", _signals("A"))
    assert len(Crawler._state_signals(me)) == _MAX_COVERAGE_STATES

    wide = _me()
    Crawler._note_state_signals(
        wide, "fp1", "https://app/x", _signals(*[f"f{i}" for i in range(_MAX_STATE_FIELDS + 50)]))
    assert len(Crawler._state_signals(wide)[0]["form_snapshot_signals"]) == _MAX_STATE_FIELDS


def test_a_bounded_index_still_improves_the_states_it_holds():
    """Hitting the cap must not freeze the rows already there — a later, richer
    sighting of a state we are keeping is still the better record of it."""
    me = _me()
    for i in range(_MAX_COVERAGE_STATES):
        Crawler._note_state_signals(me, f"fp{i}", "https://app/x", _signals("A"))
    Crawler._note_state_signals(me, "fp0", "https://app/x", _signals("A", "B"))
    kept = [s for s in Crawler._state_signals(me) if s["ax_fingerprint"] == "fp0"][0]
    assert set(kept["form_snapshot_signals"]) == {"A", "B"}


def test_coverage_publishes_the_key_the_fold_reads():
    """The whole defect in one assertion: the consumer read `coverage.states`
    and no crawl ever wrote it."""
    src = inspect.getsource(Crawler._build_coverage)
    assert '"states": self._state_signals()' in src


def test_every_recorded_state_is_offered_to_the_index():
    """Hooked to the single point where a page_state is emitted, so a state that
    reaches the manifest cannot fail to reach the catalogue."""
    src = inspect.getsource(Crawler._record_state)
    assert "_note_state_signals(fingerprint, url, form_signals)" in src
    assert src.index("_note_state_signals") < src.index("emit.PageStateRecord")
