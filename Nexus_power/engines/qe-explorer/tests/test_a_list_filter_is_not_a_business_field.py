"""A SEARCH BOX IS NOT A FIELD THE CLIENT HAS TO SUPPLY A VALUE FOR.

MEASURED (Dolibarr + Odoo journeys, 2026-08-29). Of 233 fields the crawl could
not fill, a large share were never business fields at all:

    "Search"
    "Third parties with sales representative"
    "Including products/services with the tag"
    "Cust./Prosp. tags/categories"

Those are the filter row above a list. Counting them as unfilled understates the
engine -- and, once an LLM rung exists, would send a model off inventing a
company name to type into a search box, burning tokens to narrow a list nobody
asked to narrow.

DECLARED SIGNALS ONLY, never vocabulary. "Search" is a word in one language and
this product crawls applications in many; a rule that reads labels would call a
German filter a business field and an English field named "Search Criteria" a
filter. Three signals the DOM declares:

  * ``input type=search``          -- the platform's own statement
  * an ancestor with ``role=search`` -- ARIA's own statement
  * the control sits in a table HEADER -- structural: a filter row lives in
    <thead>, the data lives in <tbody>

Anything else is a business field, which is the safe default: a filter wrongly
treated as a field costs one wasted fill, while a field wrongly treated as a
filter silently drops a question the client needed to answer.
"""
from __future__ import annotations

from app.inventory import is_filter_control


def test_a_search_input_is_a_filter():
    assert is_filter_control({"name": "Search", "kind": "text",
                              "input_type": "search"}) is True


def test_a_control_inside_a_search_landmark_is_a_filter():
    assert is_filter_control({"name": "Find", "kind": "text",
                              "landmark": {"role": "search", "name": ""}}) is True


def test_a_control_in_a_table_header_is_a_filter():
    """Dolibarr's list filters sit in the header row above the data."""
    assert is_filter_control({"name": "Ref", "kind": "text",
                              "filter_scope": "thead"}) is True


# ── the controls: a business field must never be dropped ───────────────────

def test_an_ordinary_text_field_is_a_business_field():
    assert is_filter_control({"name": "Company Name", "kind": "text",
                              "input_type": "text"}) is False


def test_a_field_merely_NAMED_search_is_not_a_filter():
    """THE CONTROL. Vocabulary must not decide this."""
    assert is_filter_control({"name": "Search Criteria", "kind": "text",
                              "input_type": "text"}) is False


def test_a_health_question_is_never_a_filter():
    assert is_filter_control({"name": "Yes", "kind": "radio",
                              "question_label": "Do you smoke?"}) is False


def test_a_control_in_a_table_BODY_is_not_a_filter():
    """Data cells are not filters; only the header row is."""
    assert is_filter_control({"name": "Ref", "kind": "text",
                              "filter_scope": "tbody"}) is False


def test_an_empty_record_is_a_business_field():
    """Fail toward asking: an unknown control is a field, not a filter."""
    assert is_filter_control({}) is False
