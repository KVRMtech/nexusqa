"""ProductTagger — word-boundary, case-insensitive product detection."""

from __future__ import annotations

import pytest

from app.indexer import ProductTagger


def _products() -> list[dict]:
    return [
        {
            "product_id": "p_lt5",
            "name": "LT5 Term Life",
            "slug": "lt5",
            "aliases": ["LT-5", "LT_5", "Term Life 5"],
        },
        {
            "product_id": "p_wl3",
            "name": "WL3 Whole Life",
            "slug": "wl3",
            "aliases": ["Whole Life 3"],
        },
    ]


def test_matches_slug_case_insensitively() -> None:
    tagger = ProductTagger(_products())
    assert tagger.tag("The lt5 form requires tobacco answers.") == {"p_lt5"}
    assert tagger.tag("On LT5 the eligibility runs first.") == {"p_lt5"}


def test_respects_word_boundaries() -> None:
    tagger = ProductTagger(_products())
    # ALT54 contains lt5 as a substring but it's part of a larger word.
    assert tagger.tag("ALT54 is unrelated.") == set()
    assert tagger.tag("alphabetlt5x is unrelated.") == set()


def test_aliases_match() -> None:
    tagger = ProductTagger(_products())
    assert tagger.tag("Internally we call this LT-5.") == {"p_lt5"}
    assert tagger.tag("Term Life 5 is the marketing name.") == {"p_lt5"}


def test_multi_product_returns_all_matches() -> None:
    tagger = ProductTagger(_products())
    result = tagger.tag("Both LT5 and WL3 share the rate table layout.")
    assert result == {"p_lt5", "p_wl3"}


def test_empty_text_returns_empty() -> None:
    tagger = ProductTagger(_products())
    assert tagger.tag("") == set()


def test_no_products_returns_empty() -> None:
    tagger = ProductTagger([])
    assert tagger.tag("LT5 should not match anything.") == set()


def test_alias_dedup_normalised() -> None:
    """Duplicate aliases shouldn't produce duplicate matches."""
    products = [
        {
            "product_id": "p_lt5",
            "name": "LT5",
            "slug": "lt5",
            "aliases": ["lt5", "LT5", "lt5"],  # all duplicates of slug
        }
    ]
    tagger = ProductTagger(products)
    assert tagger.tag("Mentioning LT5 once.") == {"p_lt5"}
