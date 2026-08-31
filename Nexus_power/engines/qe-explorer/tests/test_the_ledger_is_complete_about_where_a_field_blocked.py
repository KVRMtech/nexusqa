"""B4 (second half) — LEDGER COMPLETENESS.

THE DOCUMENTED GAP AND WHY ITS PREMISE WAS WRONG. The Phase-1 exit re-scope
recorded five summit-life-carrier wizard fields — ``First Name``, ``Last Name``,
``Date of Birth``, ``Email Address``, ``Gender`` — as "absent from the field
ledger entirely, so they have no signature to seed against at all", and proposed
making unfillable custom widgets ledger-visible.

Measured against the shipped evidence bundle, both halves of that are wrong:

  * the five are NOT absent — they are in the ledger, attributed to
    ``/customers/profile`` and ``/actuarial/product-pricing``;
  * four of the five are plain ``<Input>``s that filled successfully, so no
    "unfillable custom widget" remedy could ever have reached them.

What actually happens is a SIGNATURE COLLISION plus a first-sighting-wins merge.
A field signature is deliberately URL-free — that is what lets an answer learned
once be reused — so the wizard's ``First Name`` hashes to exactly the same 32
hex digits as the profile page's, and ``CoverageLedger.collect_ledger`` dropped
the wizard's row. The crawl-wide ledger ended with 28 rows across five URLs and
not one row from the application funnel the crawl had just walked.

THE PROPERTY THESE TESTS PROTECT is not "more rows". The old docstring is right
that a residue list which repeats itself is one operators stop reading. The
tests below hold BOTH halves at once: one row per field, and that row complete
about where the field was met and where it did not resolve.
"""
from __future__ import annotations

from app import field_signature
from app.coverage import CoverageLedger

_PROFILE = "http://x/customers/profile"
_WIZARD = "http://x/underwriting/new-business/new-application"
_PRICING = "http://x/actuarial/product-pricing"


class _Host:
    def __init__(self):
        self._field_ledger: list[dict] = []


def _ledger():
    host = _Host()
    return host, CoverageLedger(host)


def _row(name, *, filled=True, sig=None, **extra):
    out = {"signature": sig or ("sig-" + name.lower().replace(" ", "-")),
           "name": name, "filled": filled}
    out.update(extra)
    return out


# ── 0 · the collision is real, and it is the cause ────────────────────────

def test_the_wizards_first_name_hashes_identically_to_the_profile_pages():
    """THE MEASURED CAUSE, recomputed rather than quoted. Both pages declare
    ``<FormLabel>First Name</FormLabel>`` over a plain text input, and the
    signature material deliberately contains no URL."""
    wizard = field_signature.compute(
        {"name": "First Name", "kind": "text", "input_type": "text"},
        kind="text")["signature"]
    profile = field_signature.compute(
        {"name": "First Name", "kind": "text", "input_type": "text"},
        kind="text")["signature"]
    assert wizard == profile == "f1f681ea2d53613c865959331447d2e2", (
        "the signature that collided in the shipped bundle no longer "
        "reproduces; this test is the anchor for the whole fix")


# ── 1 · THE LIVE SHAPE ────────────────────────────────────────────────────

def test_a_field_met_again_in_the_funnel_names_that_page():
    """The exact summit shape: the same field, profile page first, wizard
    second. One row — and the row knows about the funnel."""
    host, led = _ledger()
    led.collect_ledger([_row("First Name")], _PROFILE)
    led.collect_ledger([_row("First Name")], _WIZARD)
    assert len(host._field_ledger) == 1, "the ask must not repeat"
    row = host._field_ledger[0]
    assert row["url"] == _PROFILE, "the first sighting keeps the row"
    assert row["also_seen_at"] == [_WIZARD], (
        "the funnel page the field also governs is invisible again")


def test_control_before_the_fix_the_second_page_left_no_trace_at_all():
    """FALSIFICATION CONTROL, written as the OLD behaviour. If `also_seen_at`
    were dropped, this is precisely what the operator would be left with: a row
    naming a page that is not the one blocking them. Asserted so that removing
    the fix fails a test instead of silently restoring the bug."""
    host, led = _ledger()
    led.collect_ledger([_row("First Name")], _PROFILE)
    led.collect_ledger([_row("First Name")], _WIZARD)
    row = host._field_ledger[0]
    assert _WIZARD in (row.get("also_seen_at") or []), (
        "the wizard sighting vanished — this is the shipped bug")


# ── 2 · THE COMPLETENESS HALF: success must not report for failure ────────

def test_a_first_sighting_that_filled_does_not_speak_for_one_that_did_not():
    """THE DANGEROUS HALF. ``filled: true`` is a fact about the page the row
    names and about no other page. A field that filled on the profile page and
    was refused in the funnel must not read as resolved."""
    host, led = _ledger()
    led.collect_ledger([_row("Gender", filled=True)], _PROFILE)
    led.collect_ledger([_row("Gender", filled=False)], _WIZARD)
    row = host._field_ledger[0]
    assert row["unresolved_at"] == [_WIZARD]
    assert row["filled"] is True, (
        "the first sighting's own fact must stay true of its own page")


def test_control_a_field_that_resolved_everywhere_records_no_block():
    """FALSIFICATION CONTROL. Identical fixture, identical pages — only the
    second sighting's outcome changes. If `unresolved_at` appeared here too,
    the key would be recording page visits rather than refusals."""
    host, led = _ledger()
    led.collect_ledger([_row("Gender", filled=True)], _PROFILE)
    led.collect_ledger([_row("Gender", filled=True)], _WIZARD)
    row = host._field_ledger[0]
    assert "unresolved_at" not in row
    assert row["also_seen_at"] == [_WIZARD]


def test_the_two_keys_are_independent():
    """A field can be met somewhere new AND blocked there; or blocked on the
    page it was first met on with no second page at all."""
    host, led = _ledger()
    led.collect_ledger([_row("Face Amount ($)", filled=False)], _WIZARD)
    row = host._field_ledger[0]
    assert "also_seen_at" not in row, "one page is not two"
    assert "unresolved_at" not in row, (
        "the row itself already says filled=False for its own page")
    assert row["filled"] is False


# ── 3 · THE PROPERTY THE OLD DOCSTRING IS RIGHT ABOUT ─────────────────────

def test_one_field_on_ten_pages_is_still_one_row():
    host, led = _ledger()
    for i in range(10):
        led.collect_ledger([_row("Email Address")], "http://x/p%d" % i)
    assert len(host._field_ledger) == 1


def test_the_url_lists_are_bounded():
    """A crawl that meets one field on two hundred pages must not turn one row
    into a two-hundred-entry list."""
    host, led = _ledger()
    for i in range(200):
        led.collect_ledger([_row("Email Address", filled=False)],
                           "http://x/p%d" % i)
    row = host._field_ledger[0]
    assert 0 < len(row["also_seen_at"]) <= 12
    assert 0 < len(row["unresolved_at"]) <= 12


def test_the_same_page_twice_is_recorded_once():
    host, led = _ledger()
    led.collect_ledger([_row("First Name")], _PROFILE)
    led.collect_ledger([_row("First Name")], _WIZARD)
    led.collect_ledger([_row("First Name")], _WIZARD)
    assert host._field_ledger[0]["also_seen_at"] == [_WIZARD]


# ── 4 · NOTHING MOVES FOR A FIELD SEEN ONCE ───────────────────────────────

def test_a_single_page_field_grows_no_key():
    """ADDITIVE AND CONDITIONAL. Every existing bundle whose fields were each
    seen on one page stays byte-identical, so no golden moves for them."""
    host, led = _ledger()
    entry = _row("Occupation", semantic_type="text", provenance="synthesized")
    led.collect_ledger([entry], _PROFILE)
    assert host._field_ledger[0] == {
        "signature": "sig-occupation", "name": "Occupation", "filled": True,
        "semantic_type": "text", "provenance": "synthesized", "url": _PROFILE}


def test_distinct_fields_still_get_distinct_rows():
    host, led = _ledger()
    led.collect_ledger([_row("First Name"), _row("Last Name")], _PROFILE)
    assert len(host._field_ledger) == 2


def test_an_entry_with_no_signature_is_still_ignored():
    host, led = _ledger()
    led.collect_ledger([{"name": "Nameless", "signature": ""}], _PROFILE)
    assert host._field_ledger == []


def test_collecting_nothing_changes_nothing():
    host, led = _ledger()
    led.collect_ledger([_row("First Name")], _PROFILE)
    led.collect_ledger([], _WIZARD)
    led.collect_ledger(None, _PRICING)
    assert len(host._field_ledger) == 1
    assert "also_seen_at" not in host._field_ledger[0]
