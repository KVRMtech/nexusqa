"""ZERO TESTS AND NO REASON IS ITS OWN STATE (B4).

270 crawls recorded ``generated: 0`` with an EMPTY reason. On the row that is
indistinguishable from an honest "this crawl found no coherent flow to build a
test from" — and the two need OPPOSITE responses:

  * an honest no-cases is a finding about the application (or about how much the
    crawl reached), and the reason says so;
  * zero-with-no-reason is a gap in OUR generator, and every hour spent
    investigating the app for it is an hour wasted.

Collapsing them cost this project two months of "the crawl produced nothing"
with nowhere to look. ``outcome`` now separates them, and the unexplained case
carries copy that points at us rather than at the client's app.
"""
from __future__ import annotations

import inspect

from app.routers import internal


def _generate_block() -> str:
    src = inspect.getsource(internal.complete_crawl)
    start = src.index("generate_result: dict")
    return src[start:src.index('stats_dict["generate"]', start)]


def test_the_three_outcomes_are_distinguished():
    block = _generate_block()
    for outcome in ('"generated"', '"no_cases"', '"unexplained"'):
        assert outcome in block, f"{outcome} is not a distinguishable outcome"


def test_zero_with_no_reason_is_named_unexplained_and_not_ok():
    """The swallowed state. It must not read as a successful generation."""
    block = _generate_block()
    assert 'outcome = "unexplained"' in block
    assert 'generate_result["ok"] = False' in block


def test_the_unexplained_case_points_at_us_not_at_the_client_app():
    """A client reading their verdict must not go hunting through their own
    application for a defect that is ours."""
    block = _generate_block()
    assert "gap in the generator" in block
    assert "not a finding about your application" in block


def test_a_transport_failure_is_its_own_outcome():
    block = _generate_block()
    assert '"outcome": "error"' in block


def test_an_honest_no_cases_keeps_its_reason():
    """The generator's own explanation is the most useful sentence on the row —
    it must survive, not be replaced by our copy."""
    block = _generate_block()
    assert 'elif reason:' in block
    assert 'outcome = "no_cases"' in block
