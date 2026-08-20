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
from app.coverage import CoverageLedger
from app.state_identity import StateRecorder


def _note_state_signals(host, *args):
    """M0.3/T-DE-06: the states index moved from ``Crawler`` into
    :class:`app.state_identity.StateRecorder`.  Same code, same assertions —
    but the unit no longer needs a Crawler to exercise it, which is the
    testability the extraction was for."""
    StateRecorder(host).note_state_signals(*args)


def _state_signals(host):
    """Companion to :func:`_note_state_signals` — same move, same reason."""
    return StateRecorder(host).state_signals()


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
    _note_state_signals(me, "fp1", "https://app/apply", _signals("Email"))
    out = _state_signals(me)
    assert len(out) == 1
    assert out[0]["ax_fingerprint"] == "fp1"
    assert out[0]["location"] == "https://app/apply"
    # Superset, not equality: qe-central reads these and ignores the rest, so
    # the crawl may carry more (danger counts) without breaking the contract.
    assert set(CONTRACT_KEYS) <= set(out[0])


def test_the_questions_are_carried_not_the_answers():
    """VALUE-FREE BY CONSTRUCTION. ``form_snapshot_signals`` is label→shape;
    its sibling ``form_snapshot`` is label→COMMITTED VALUE and must never cross
    this boundary. Shapes leave the tenant; answers never do."""
    me = _me()
    _note_state_signals(
        me, "fp1", "https://app/apply",
        {"Email": {"type": "text", "required": True, "options": []}})
    blob = repr(_state_signals(me))
    assert "form_snapshot_signals" in blob
    assert "form_snapshot\"" not in blob and "'form_snapshot'" not in blob
    assert "qa.autotest@example.com" not in blob


def test_the_richest_sighting_of_a_state_wins():
    """A wizard step is met on entry and again mid-walk, and a dependent question
    offers nothing until its driver is answered. Keeping the first sighting holds
    the emptiest view of exactly the questions hardest to enumerate."""
    me = _me()
    _note_state_signals(me, "fp1", "https://app/x", _signals("A"))
    _note_state_signals(me, "fp1", "https://app/x", _signals("A", "B", "C"))
    out = _state_signals(me)
    assert len(out) == 1
    assert set(out[0]["form_snapshot_signals"]) == {"A", "B", "C"}


def test_a_poorer_later_sighting_never_erodes_what_was_seen():
    me = _me()
    _note_state_signals(me, "fp1", "https://app/x", _signals("A", "B", "C"))
    _note_state_signals(me, "fp1", "https://app/x", _signals("A"))
    assert set(_state_signals(me)[0]["form_snapshot_signals"]) == {"A", "B", "C"}


def test_a_state_with_neither_questions_nor_controls_is_not_recorded():
    me = _me()
    _note_state_signals(me, "fp1", "https://app/x", {})
    _note_state_signals(me, "", "https://app/x", _signals("A"))
    assert _state_signals(me) == []


def test_a_page_that_asks_nothing_can_still_refuse_everything():
    """The page that most needed the danger ratio — a hub whose only controls
    are links — has no form fields at all. Gating on questions alone would skip
    exactly the page where an over-broad refuse rule does its damage."""
    me = _me()
    _note_state_signals(
        me, "hub", "https://app/underwriting", {},
        [{"name": "New Application", "danger": ""},
         {"name": "Bind Coverage", "danger": "critical"}])
    out = _state_signals(me)
    assert len(out) == 1
    assert out[0]["controls_total"] == 2 and out[0]["danger_controls"] == 1


def test_the_danger_ratio_is_recorded_per_state():
    """A refuse rule that matches too widely does not fail — it quietly flags
    ordinary controls as dangerous, the walk skips them, and the funnel narrows
    for a reason no number reports. Live, a URL-scoped `underwrite` rule matched
    against the PAGE url took 20 of 35 hub controls critical, the wizard was
    never entered, and it cost an investigation. As a ratio it is an assertion."""
    me = _me()
    _note_state_signals(
        me, "fp1", "https://app/x", _signals("A"),
        [{"danger": "critical"}, {"danger": ""}, {"danger": ""}, {"danger": ""}])
    got = _state_signals(me)[0]
    assert got["controls_total"] == 4 and got["danger_controls"] == 1


def test_a_crawl_with_no_control_list_still_records_its_questions():
    """Back-compat: controls is optional, and a caller that omits it must not
    lose the states index it was already contributing."""
    me = _me()
    _note_state_signals(me, "fp1", "https://app/x", _signals("A"))
    got = _state_signals(me)[0]
    assert got["controls_total"] == 0 and got["danger_controls"] == 0


def test_the_index_is_bounded():
    """Coverage is a REPORT, not a second copy of the manifest. One pathological
    application must not turn the stats column into an evidence store."""
    me = _me()
    for i in range(_MAX_COVERAGE_STATES + 25):
        _note_state_signals(me, f"fp{i}", "https://app/x", _signals("A"))
    assert len(_state_signals(me)) == _MAX_COVERAGE_STATES

    wide = _me()
    _note_state_signals(
        wide, "fp1", "https://app/x", _signals(*[f"f{i}" for i in range(_MAX_STATE_FIELDS + 50)]))
    assert len(_state_signals(wide)[0]["form_snapshot_signals"]) == _MAX_STATE_FIELDS


def test_a_bounded_index_still_improves_the_states_it_holds():
    """Hitting the cap must not freeze the rows already there — a later, richer
    sighting of a state we are keeping is still the better record of it."""
    me = _me()
    for i in range(_MAX_COVERAGE_STATES):
        _note_state_signals(me, f"fp{i}", "https://app/x", _signals("A"))
    _note_state_signals(me, "fp0", "https://app/x", _signals("A", "B"))
    kept = [s for s in _state_signals(me) if s["ax_fingerprint"] == "fp0"][0]
    assert set(kept["form_snapshot_signals"]) == {"A", "B"}


def test_coverage_publishes_the_key_the_fold_reads():
    """The whole defect in one assertion: the consumer read `coverage.states`
    and no crawl ever wrote it."""
    # M0.3/T-DE-07: the account moved into CoverageLedger.build. Same wiring,
    # same guarantee — the key the fold reads is still published from the
    # states-index producer rather than from anywhere else.
    src = inspect.getsource(CoverageLedger.build)
    assert '"states": c._state_signals()' in src


def test_every_recorded_state_is_offered_to_the_index():
    """Hooked to the single point where a page_state is emitted, so a state that
    reaches the manifest cannot fail to reach the catalogue."""
    # M0.3/T-DE-06: the emit point moved into StateRecorder.record_state. The
    # invariant is unchanged — the index is still fed from the SAME single place
    # a page_state is built, and still strictly before the record is assembled.
    #
    # M2.4: the guard no longer pins the ARGUMENT LIST. It used to require the
    # exact string ``note_state_signals(fingerprint, url, form_signals,
    # controls)``, so adding a fifth argument — the network calls, which the
    # endpoint map is built from — failed a test whose stated invariant was
    # untouched. A guard that reds on a change it does not describe teaches the
    # next reader to edit the guard rather than to think about it. What is
    # actually load-bearing is asserted instead: the call happens HERE, it is fed
    # THIS state's own fingerprint and signals, and it happens BEFORE the record
    # is assembled.
    src = inspect.getsource(StateRecorder.record_state)
    assert "note_state_signals(" in src
    call = src[src.index("note_state_signals("):]
    args = call[len("note_state_signals("): call.index(")")]
    for required in ("fingerprint", "url", "form_signals", "controls"):
        assert required in args, f"{required} is no longer fed to the index"
    assert src.index("note_state_signals") < src.index("emit.PageStateRecord")


# ─── the declared rule reaches the catalogue (Track 1.3) ─────────────────────

def test_a_declared_constraint_reaches_the_form_signal():
    """THE BOUNDARY SCENARIO'S ONLY INPUT. The browser extractor has always
    captured min/max/step, the control record has always held them, and
    form_signal_for — the boundary qe-central reads validation from — dropped
    every one. Live that left `validation` NULL on all 24 catalogued questions,
    including a Face Amount input declaring step=10000: the clearest boundary
    rule on the form, and no boundary case could be derived because the
    catalogue never learned it."""
    from app.inventory import form_signal_for
    sig = form_signal_for({
        "kind": "text", "options": [], "required": True,
        "min": "10000", "max": "5000000", "step": "10000",
        "pattern": r"\d+", "minlength": "1", "maxlength": "9",
    })
    assert sig["min"] == "10000" and sig["max"] == "5000000"
    assert sig["step"] == "10000" and sig["pattern"] == r"\d+"
    assert sig["minlength"] == "1" and sig["maxlength"] == "9"


def test_an_undeclared_constraint_is_absent_not_empty():
    """An empty string is a claim that the app declared a blank rule. qe-central
    treats any non-empty value as a rule, so silence must stay silent."""
    from app.inventory import form_signal_for
    sig = form_signal_for({"kind": "text", "options": [], "required": False,
                           "min": "", "max": "", "step": ""})
    assert not any(k in sig for k in ("min", "max", "step", "pattern"))


def test_the_validation_contract_matches_the_consumer():
    """Mirrored across services that share no library — the same pin the advance
    vocabulary carries. A rename on either side silently loses the rule."""
    from app.inventory import _VALIDATION_KEYS
    assert _VALIDATION_KEYS == ("pattern", "minlength", "maxlength",
                                "min", "max", "step")


def test_the_extractor_captures_every_key_the_contract_names():
    from app.inventory_js import INVENTORY_JS
    for key in ("pattern", "minlength", "maxlength", "min", "max", "step"):
        assert f'{key}: attr(el, "{key}")' in INVENTORY_JS, key


# ─── a submit that fired is not a submit that worked ─────────────────────────

def test_confirmed_is_tracked_separately_from_submitted():
    """`submitted` is set whatever the application answered; `confirmed` is the
    separate fact that it answered with a navigation or a success. The crawl
    computed the distinction and dropped it, so nine submits that all errored
    scored exactly like nine completed business transactions — in the counter,
    in the gate floor, and in the weekly yield. This is the one boundary where
    the product claims something HAPPENED, so it is the last place a count may
    be generous."""
    import inspect
    # M0.3/T-DE-11: the submit path moved into app.submit.SubmitMixin. The
    # guarantee is unchanged — only the APPLICATION's own confirmation may
    # increment forms_confirmed, and it is still read off the submit result.
    from app.submit import SubmitMixin
    src = inspect.getsource(SubmitMixin)
    assert "self._forms_confirmed += 1" in src
    assert 'getattr(result, "confirmed", False)' in src
    cov = inspect.getsource(CoverageLedger.build)  # M0.3/T-DE-07
    assert '"forms_confirmed": c._forms_confirmed' in cov
    assert '"forms_submitted": c._forms_submitted' in cov


def test_a_submit_result_still_carries_both_facts():
    """Both must survive: an attempted-but-unconfirmed submit is real evidence
    (the app was reached and refused), not something to hide by counting only
    successes."""
    from app.forms import SubmitResult
    fields = SubmitResult.__dataclass_fields__
    assert "submitted" in fields and "confirmed" in fields
