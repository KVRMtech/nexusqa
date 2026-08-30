"""RUNG 2 ENTERS THE LADDER BENEATH EVERYTHING THE CLIENT STATED DIRECTLY.

A client's test environment answering for itself is only an improvement while it
stays UNDER the client's own words. A value somebody typed into this app is a
more specific and more recent instruction than a fixture endpoint's standing
answer, and an overlay that could overwrite it would quietly replace a
deliberate choice with a default — the kind of defect nobody reports because the
crawl still goes green.

The second thing pinned here is that the overlay is genuinely OPTIONAL: without
one, the projection is byte-identical to what every existing caller already
gets. That is what keeps the characterization bundles unchanged, and it is
asserted directly rather than assumed.
"""
from __future__ import annotations

from app.services.answer_key import explorer_fill_contract


def test_without_an_overlay_nothing_about_the_projection_changes():
    """THE COMPATIBILITY GUARANTEE. Every existing caller passes one argument;
    if adding a rung moved a single byte here, the characterization goldens
    would be measuring a different product than the one that was certified."""
    ak = {"exact": {"Email": "a@example.com"},
          "semantic": {"phone": "555-0101"},
          "fill": {"coverage": "100000"},
          "regex_rules": [{"pattern": "^Zip", "value": "78701"}]}
    assert explorer_fill_contract(ak) == explorer_fill_contract(ak, None)
    assert explorer_fill_contract(ak) == explorer_fill_contract(ak, {})


def test_an_environment_answer_reaches_the_explorer_as_a_semantic_match():
    got = explorer_fill_contract({}, {"Member ID": "M-1001"})
    assert got["semantic"]["Member ID"] == "M-1001"
    assert got["exact"] == {}, "the environment never states an exact match"


def test_a_value_the_client_typed_wins_over_the_environment_s_answer():
    """THE ONE THAT MATTERS. Overwriting a deliberate choice with a fixture
    default is the defect nobody reports, because the crawl still goes green."""
    got = explorer_fill_contract({"exact": {"Member ID": "TYPED-BY-CLIENT"}},
                                 {"Member ID": "M-FROM-ENV"})
    assert got["exact"]["Member ID"] == "TYPED-BY-CLIENT"
    assert "Member ID" not in got["semantic"], \
        "an exact key must never be shadowed by a semantic one"


def test_the_data_tab_also_wins_over_the_environment():
    got = explorer_fill_contract({"fill": {"Member ID": "FROM-DATA-TAB"}},
                                 {"Member ID": "M-FROM-ENV"})
    assert got["semantic"]["Member ID"] == "FROM-DATA-TAB"


def test_a_semantic_key_the_client_set_also_wins():
    got = explorer_fill_contract({"semantic": {"Member ID": "CLIENT-SEMANTIC"}},
                                 {"Member ID": "M-FROM-ENV"})
    assert got["semantic"]["Member ID"] == "CLIENT-SEMANTIC"


def test_the_control_for_precedence_the_environment_answers_what_nobody_claimed():
    """FALSIFICATION CONTROL for all four precedence tests above. Without it, an
    overlay that was silently DISCARDED — a typo in the loop, a wrong key —
    would satisfy every one of them and look like correct precedence."""
    got = explorer_fill_contract({"exact": {"Email": "a@example.com"}},
                                 {"Member ID": "M-FROM-ENV"})
    assert got["semantic"]["Member ID"] == "M-FROM-ENV"
    assert got["exact"]["Email"] == "a@example.com"


def test_the_overlay_adds_alongside_rather_than_replacing_the_map():
    got = explorer_fill_contract({"fill": {"coverage": "100000"}},
                                 {"Member ID": "M-1001"})
    assert got["semantic"] == {"coverage": "100000", "Member ID": "M-1001"}


def test_a_blank_key_from_an_environment_is_not_a_field():
    got = explorer_fill_contract({}, {"": "orphan", "   ": "orphan"})
    assert got["semantic"] == {}


def test_an_overlay_that_is_not_a_mapping_is_ignored_rather_than_fatal():
    """A misconfigured environment must never fail a dispatch."""
    for bad in ("a string", ["a", "list"], 7):
        assert explorer_fill_contract({}, bad)["semantic"] == {}
