"""A FORM LAID OUT IN A TABLE STILL HAS LABELS — READ THEM.

MEASURED (Dolibarr third-party form, 2026-08-30). 94.3% of that form's fields
reached the semantic classifier with basis ``structural`` — the shape fallback,
confidence 0.5, "a shape we can fill safely without claiming meaning". The
diagnosis looked like a weak classifier and was not: the fields had no usable
NAME, and the page had the labels all along, in the first cell of each
control's own row.

    <tr><td>Country</td><td><select name="country_id">…   ->  name ""
    <tr><td>Status</td> <td><select name="status">…       ->  name ""

Everything downstream keys off that name. The classifier could not type the
field, so the generator worked blind, so the seed request could not say what it
was asking the client about. A JS-enhanced combobox makes it worse still by
presenting its CURRENT SELECTION as the widget's accessible name, which is how
the catalogue came to hold a field called "India (IN)".

The rung is DECLARED, not inferred: the label cell is the page's own wording in
the page's own structure, read only after every stronger rung has produced
nothing. It is marked ``best_effort`` alongside title/placeholder, because a
layout convention is weaker evidence than ``<label for>`` and the refiner
should keep saying so.

These tests pin the rule and, just as importantly, the four shapes it must NOT
claim a label from.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

from app.inventory_js import INVENTORY_JS  # noqa: E402


def _names(html: str) -> dict:
    """Run the REAL inventory JS over a page and map name -> name_source."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        raw = page.evaluate(INVENTORY_JS)
        browser.close()
    return {c.get("name", ""): c.get("name_source", "") for c in raw
            if c.get("tag") in ("select", "input", "textarea")}


# ── the measured shape ─────────────────────────────────────────────────────

def test_the_row_s_first_cell_names_the_control():
    got = _names("""
      <table>
        <tr><td>Country</td><td><select name="country_id">
          <option>India (IN)</option><option>France (FR)</option></select></td></tr>
        <tr><td>Status</td><td><select name="status">
          <option>Open</option><option>Closed</option></select></td></tr>
      </table>""")
    assert got.get("Country") == "row-label"
    assert got.get("Status") == "row-label"


def test_a_trailing_colon_or_asterisk_is_not_part_of_the_name():
    got = _names("""
      <table><tr><td>Postcode: *</td>
      <td><input name="zip"></td></tr></table>""")
    assert "Postcode" in got


# ── what it must NOT claim a label from ────────────────────────────────────

def test_a_declared_label_still_wins():
    """THE CONTROL. The rung is last; a real association must not be overridden."""
    got = _names("""
      <table><tr><td>Wrong</td>
      <td><label for="c">Country</label><input id="c" name="country"></td></tr></table>""")
    assert got.get("Country") == "label-for"
    assert "Wrong" not in got


def test_a_cell_holding_another_control_is_not_a_label():
    """A two-control row is a layout, not a labelled field — claiming the first
    control's text as the second's name would invent an association."""
    got = _names("""
      <table><tr><td><input name="from"></td>
      <td><input name="to"></td></tr></table>""")
    assert "row-label" not in got.values()


def test_a_control_in_the_first_cell_has_no_row_label():
    got = _names("""
      <table><tr><td><input name="alone"></td><td>some note</td></tr></table>""")
    assert "row-label" not in got.values()


def test_a_single_cell_row_names_nothing():
    got = _names("""
      <table><tr><td><input name="solo"></td></tr></table>""")
    assert "row-label" not in got.values()


def test_a_paragraph_of_prose_is_not_a_label():
    """A label is a few words. A cell holding a sentence is content."""
    long_text = "This is a long explanatory sentence " * 4
    got = _names(f"""
      <table><tr><td>{long_text}</td>
      <td><input name="x"></td></tr></table>""")
    assert "row-label" not in got.values()
