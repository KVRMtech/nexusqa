"""ProductResolver — entity resolution against the tenant catalog."""

from __future__ import annotations

import pytest

from app.atlas.product_resolver import ProductCatalogEntry, ProductResolver


def _catalog() -> list[ProductCatalogEntry]:
    return [
        ProductCatalogEntry(
            product_id="p_lt5",
            name="LT5 Term Life",
            slug="lt5",
            aliases=("LT-5", "Term Life 5"),
        ),
        ProductCatalogEntry(
            product_id="p_wl3",
            name="WL3 Whole Life",
            slug="wl3",
            aliases=("Whole Life 3",),
        ),
    ]


def test_resolves_primary_for_single_product_text() -> None:
    r = ProductResolver(_catalog())
    v = r.resolve("LT5 has a 24-month tobacco lookback.")
    assert v.primary == "p_lt5"
    assert v.matches[0].product_id == "p_lt5"
    assert v.confidence > 0.5


def test_multi_term_match_outweighs_single_term() -> None:
    r = ProductResolver(_catalog())
    v = r.resolve("In the LT-5 and LT5 flow we also reference WL3.")
    # LT5 matches the slug + alias (2 distinct terms); WL3 matches only slug.
    assert v.primary == "p_lt5"
    assert {m.product_id for m in v.matches} == {"p_lt5", "p_wl3"}


def test_no_match_returns_empty_verdict() -> None:
    r = ProductResolver(_catalog())
    v = r.resolve("This text mentions no products.")
    assert v.primary is None
    assert v.matches == ()
    assert v.confidence == 0.0


def test_word_boundary_required() -> None:
    r = ProductResolver(_catalog())
    v = r.resolve("ALT5X is not a product.")
    assert v.primary is None


def test_min_confidence_gate() -> None:
    r = ProductResolver(_catalog(), min_confidence=0.99)
    v = r.resolve("LT5 mentioned.")
    # Single match below the gate.
    assert v.primary is None


def test_max_results_limits_output() -> None:
    catalog = _catalog() + [
        ProductCatalogEntry(
            product_id="p_lt6",
            name="LT6 Next-Gen Term",
            slug="lt6",
            aliases=("LT-6",),
        ),
    ]
    r = ProductResolver(catalog, max_results=2)
    v = r.resolve("LT5 and WL3 and LT6 all in one sentence.")
    assert len(v.matches) == 2


def test_empty_text_returns_empty() -> None:
    r = ProductResolver(_catalog())
    v = r.resolve("")
    assert v.primary is None


def test_empty_catalog_returns_empty() -> None:
    r = ProductResolver([])
    assert r.is_empty
    v = r.resolve("LT5")
    assert v.primary is None
