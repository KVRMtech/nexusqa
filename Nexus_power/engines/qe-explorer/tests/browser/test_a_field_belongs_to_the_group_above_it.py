"""A field's SECTION is the group label a person reads above it.

MEASURED 2026-09-05, across every crawl this system has ever recorded:

    controls with a NON-EMPTY section:  0  of  19,838

Zero. For every application. ``sectionOf`` accepted only the name of the nearest
ARIA LANDMARK ancestor, and real applications group form fields in plain divs —
so the ``Section:`` line the value prompt has always supported was never once
filled, and a model asked to answer a field was told only the field's own label.

THE COST, measured on orangehrm the same day. Its candidate list has a date
range whose two inputs are labelled "From" and "To" under a heading that says
what they are. With no section, the model answered them:

    From  ->  "John Smith"
    To    ->  an email address

Fluent, confident, and impossible to type into a date input — so a submit built
on that data could never have been valid. The grouping was in the markup the
whole time; nothing read it.

So the rule now reads the three ways HTML actually says "these belong together":
a ``<legend>``, an ``aria-label``led ``role=group``, and the nearest heading
ABOVE the field. Product UI text only, never a value — the same discipline a
label is held to.

WHY THE STOP RULE IS THE LOAD-BEARING PART. Scanning backwards for a heading
walks past OTHER groups if nothing stops it, and hands a field the heading of a
section it is not in. WRONG context is worse than none: the model cannot doubt
it, and it looks right in the evidence. The scan therefore stops at the first
sibling CONTAINER that owns controls. It must not stop at a bare peer control —
doing so stranded "To" while "From" resolved, which is half a pair answered
blind and the more dangerous failure of the two.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

from app.inventory_js import INVENTORY_JS  # noqa: E402


def _sections(html: str) -> dict:
    """Run the REAL capture JS over a page and map field name -> section."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        raw = page.evaluate(INVENTORY_JS)
        browser.close()
    rows = raw if isinstance(raw, list) else (raw or {}).get("controls") or []
    out = {}
    for r in rows:
        nm = (r.get("name") or r.get("question_label") or "").strip()
        if nm:
            out[nm] = (r.get("section") or "").strip()
    return out


#: The measured orangehrm shape: a heading, then a row holding the pair.
DATE_RANGE = """
<html><body>
  <h2>Date of Application</h2>
  <div class="row">
    <label for="f1">From</label><input id="f1" type="text">
    <label for="f2">To</label><input id="f2" type="text">
  </div>
</body></html>
"""


def test_the_measured_date_range_gets_its_heading():
    """Both halves of the pair, or the fix is worse than the defect."""
    s = _sections(DATE_RANGE)
    assert s.get("From") == "Date of Application"
    assert s.get("To") == "Date of Application", (
        "'To' was stranded while 'From' resolved — half a pair answered blind, "
        "got %r" % s.get("To")
    )


def test_a_fieldset_is_named_by_its_legend():
    s = _sections("""
      <html><body><fieldset><legend>Beneficiary details</legend>
        <label for="a">Full Name</label><input id="a" type="text">
      </fieldset></body></html>""")
    assert s.get("Full Name") == "Beneficiary details", (
        "expected the legend alone; the whole fieldset's text would glue the "
        "field's own label on ('Beneficiary details Full Name'), got %r"
        % s.get("Full Name")
    )


def test_an_aria_labelled_group_is_read():
    s = _sections("""
      <html><body><div role="group" aria-label="Contact preferences">
        <label for="a">Email</label><input id="a" type="text">
      </div></body></html>""")
    assert s.get("Email") == "Contact preferences"


# ══════════════════════════════════════════════════════════════════════════
#  CONTROLS — wrong context is worse than none
# ══════════════════════════════════════════════════════════════════════════

def test_a_field_outside_every_group_gets_no_section():
    """CONTROL — the over-capture this rule is one careless hop away from.

    Measured while building it: a trailing ungrouped input inherited
    "Date of Application" from two groups away, because the backward scan had
    nothing to stop it.
    """
    s = _sections("""
      <html><body>
        <h2>Date of Application</h2>
        <div class="row"><label for="a">From</label><input id="a" type="text"></div>
        <div role="group" aria-label="Contact preferences">
          <label for="b">Email</label><input id="b" type="text"></div>
        <label for="c">Loose Field</label><input id="c" type="text">
      </body></html>""")
    assert s.get("From") == "Date of Application"
    assert s.get("Email") == "Contact preferences"
    assert s.get("Loose Field") == "", (
        "an ungrouped field inherited a section from a group it is not in: %r"
        % s.get("Loose Field")
    )


def test_a_page_with_no_grouping_at_all_yields_no_sections():
    """CONTROL — silence is a valid answer, and the honest one."""
    s = _sections("""
      <html><body>
        <label for="a">Alpha</label><input id="a" type="text">
        <label for="b">Beta</label><input id="b" type="text">
      </body></html>""")
    assert s.get("Alpha") == ""
    assert s.get("Beta") == ""


def test_a_later_group_does_not_inherit_an_earlier_heading():
    """CONTROL — two groups in sequence must not bleed into each other."""
    s = _sections("""
      <html><body>
        <h3>Applicant</h3>
        <div><label for="a">Given Name</label><input id="a" type="text"></div>
        <h3>Employer</h3>
        <div><label for="b">Company</label><input id="b" type="text"></div>
      </body></html>""")
    assert s.get("Given Name") == "Applicant"
    assert s.get("Company") == "Employer", (
        "the second group inherited the first group's heading: %r" % s.get("Company")
    )
