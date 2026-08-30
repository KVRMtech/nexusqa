"""THE DATA ACCOUNT — where every answered field's value CAME FROM.

The client's first question about an autonomous crawl is "what did you type into
my application, and who decided it?". The ladder already stamps a ``provenance``
on every field, but answering that question required reading 500 ledger rows, so
in practice nobody answered it and the claim went unmade.

``data_account`` is the roll-up, and it is what makes a sentence like "of 240
answered fields, 96 came from your own data and 12 were written by the model"
DERIVABLE from the bundle instead of asserted in a slide.

TWO PROPERTIES, each with a control:

  1. COUNTS, NEVER VALUES. A provenance name is a category; a value is the
     client's data. Only one of them belongs in evidence.
  2. THE FULL LEDGER, NOT THE TRUNCATED COPY. The bundle emits at most 500
     ledger rows because an operator will not read more. A cap on what a human
     reads must never silently become a cap on what the account COUNTS — that
     is how a report understates the model's contribution on exactly the large
     crawls where it matters most.
"""
from __future__ import annotations

from app.coverage import _provenance_counts


def _rows(*pairs):
    return [{"provenance": p, "name": n, "signature": f"sig{i}"}
            for i, (p, n) in enumerate(pairs)]


def test_each_rung_is_counted_under_its_own_name():
    got = _provenance_counts(_rows(
        ("provided", "Email"), ("provided", "Phone"),
        ("llm", "Occupation"), ("synthesized", "First Name"),
        ("harvested", "Customer Code"), ("minted", "Policy Number")))
    assert got == {"harvested": 1, "llm": 1, "minted": 1, "provided": 2,
                   "synthesized": 1}


def test_the_account_is_derivable_into_the_sentence_a_client_asks_for():
    """The point of the block, spelled out: the claim must be ARITHMETIC."""
    ledger = _rows(*([("provided", "x")] * 96 + [("llm", "y")] * 12
                     + [("synthesized", "z")] * 132))
    account = _provenance_counts(ledger)
    total = sum(account.values())
    assert total == 240
    assert account["provided"] == 96 and account["llm"] == 12


def test_a_rung_added_later_appears_without_this_code_changing():
    """Keyed on the ladder's OWN names rather than a list restated here. A
    hard-coded list is how an account silently under-reports the rung nobody
    remembered to add."""
    assert _provenance_counts(_rows(("some_future_rung", "x"))) == \
        {"some_future_rung": 1}


def test_no_value_can_reach_the_account():
    """PROPERTY 1. A provenance is a category; a value is the client's data."""
    ledger = [{"provenance": "provided", "name": "SSN",
               "value": "900-00-1234"}]
    account = _provenance_counts(ledger)
    assert account == {"provided": 1}
    assert "900-00-1234" not in repr(account)


def test_the_account_counts_the_full_ledger_not_the_emitted_slice():
    """PROPERTY 2, and the one that matters on a real crawl. The bundle emits
    500 rows; the account must count all of them."""
    ledger = _rows(*([("llm", "f")] * 700))
    assert _provenance_counts(ledger) == {"llm": 700}


def test_a_field_with_no_provenance_is_not_counted_as_a_rung():
    """An unstamped row is a defect in the ladder, not a category. Counting it
    under "" would invent a rung that answered nothing."""
    assert _provenance_counts([{"provenance": ""}, {"provenance": None},
                               {}]) == {}


def test_the_control_a_stamped_row_beside_them_is_still_counted():
    """FALSIFICATION CONTROL for the refusal above."""
    assert _provenance_counts([{"provenance": ""}, {"provenance": "llm"}]) == \
        {"llm": 1}


def test_an_empty_crawl_reports_an_empty_account_rather_than_nothing():
    """A crawl that filled no field must still SAY so — an absent key reads as
    "not measured", which is a different claim."""
    assert _provenance_counts([]) == {}
    assert _provenance_counts(None) == {}


def test_the_account_is_ordered_so_two_runs_compare_by_eye():
    got = _provenance_counts(_rows(("synthesized", "a"), ("llm", "b"),
                                   ("harvested", "c")))
    assert list(got) == sorted(got)


def test_the_bundle_carries_the_account_at_its_top_level():
    """THE WIRING. A roll-up nothing emits is a function nobody calls — this
    asserts the key is actually in the coverage account the bundle ships."""
    import inspect

    from app import coverage

    source = inspect.getsource(coverage)
    assert '"data_account": _provenance_counts(c._field_ledger)' in source, \
        "the account must be built from the FULL ledger, not field_ledger[:500]"
