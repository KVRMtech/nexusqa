"""M0.x — THE CAPTURE COMPLETENESS CONTRACT.

Two defects are reproduced and then permanently guarded here. Both are of the
same family as the ones in :mod:`test_known_bugs`: the browser exposes the
information, capture drops it, and a downstream consumer silently degrades.

``BUG-CAPTURE-ATTRS`` (T-CAP-01)
    ``describe()`` never emits ``autocomplete`` / ``inputmode`` / ``placeholder``
    / ``id``.  :func:`app.field_semantics.classify` weights ``autocomplete``
    FIRST — it is the app's own W3C-standard declaration of what a field is for,
    at confidence 0.98 — and :func:`app.field_signature.compute` reads all four.
    With capture silent, rung 1 is unreachable code on every crawled control and
    every field falls through to the weakest name-token rung.

``BUG-OPTION-CEILING`` (T-CAP-05)
    Three different option ceilings on one path (300 → 60 → 48).  The captured
    answer set of a question is the test data for every scenario derived from it,
    so a prefix presented as the whole is a fabricated answer set.

WHY THE CLASSIFIER RUNS HERE RATHER THAN IN A UNIT TEST
    A unit test feeding a hand-authored dict to ``classify()`` passes today —
    that is precisely why this defect survived. The only assertion that can catch
    it starts at a real browser and ends at the classifier's verdict, so that is
    what these tests do: fixture → production port → INVENTORY_JS →
    ``build_inventory`` → ``field_signature.compute`` → ``field_semantics.classify``.

STATUS
    Both defects were written up here as ``xfail(strict=True)`` reproductions
    against ``inv-js-v9``, verified to fail (and to fail outright under
    ``QEC_BUG_REPRO_STRICT=1``), and then fixed in Capture. The strict markers
    turned the fixes into ``XPASS(strict)`` CI failures, which is the designed
    outcome — a fix cannot land without the test being promoted. They are now
    **permanent regression guards**; their assertions are byte-for-byte what they
    were as reproductions, and only the marker was removed.

    The recorded before-fix failures were::

        [BUG-CAPTURE-ATTRS/chromium] control {css_hint='input#email'}
          field 'autocomplete': expected 'email', actual None
        the signature carried no autocomplete: {'autocomplete': '',
          'tokens': ['field', 'a7'], ...}          # rung 1 unreachable
        the refiner kept only 60 of the 250 options capture preserved
        an option list is clipped by a bare number instead of the named
          ceiling: ['row.options = [str(o) for o in q["options"]][:48]']

    ``_repro`` is retained as the documented mechanism for the NEXT
    reproduction, matching the convention in :mod:`test_known_bugs`.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

import _harness as H

pytestmark = [pytest.mark.browser]

#: Same switch the sibling reproduction module uses: when set, the not-yet-fixed
#: assertions fail outright instead of being recorded xfail.
STRICT_MODE = os.environ.get("QEC_BUG_REPRO_STRICT", "").strip().lower() in ("1", "true", "yes")


def _repro(reason: str):
    if STRICT_MODE:
        return pytest.mark.usefixtures()
    return pytest.mark.xfail(strict=True, reason=reason)


_ATTRS_REASON = (
    "BUG-CAPTURE-ATTRS: describe() (inventory_js.py:561-631) emits no "
    "autocomplete/inputmode/placeholder/id, and build_control_record() "
    "(inventory.py:584-646) does not carry them, so classify() rung 1 is "
    "unreachable on every crawled control")


# ─── T-CAP-01 · the attributes reach the captured representation ─────────────

# PROMOTED to a permanent regression guard — formerly @_repro(_ATTRS_REASON).
@pytest.mark.playwright
def test_classifier_attributes_are_captured_in_chromium(pw, fixture_server) -> None:
    """Every declared attribute the classifier reads must survive capture."""
    controls = pw.collect(fixture_server.url("14-capture-attributes"))
    for case in H.fixture_spec("14-capture-attributes")["describes_correct_behaviour"]:
        H.assert_control(controls, case, context="[BUG-CAPTURE-ATTRS/chromium] ")


# PROMOTED to a permanent regression guard — formerly @_repro(_ATTRS_REASON).
@pytest.mark.jsdom
def test_classifier_attributes_are_captured_in_jsdom(jsdom, fixture_server) -> None:
    """The same contract in the second lane — these are plain attribute reads,
    so a runtime difference would mean the walker, not the runtime, is wrong."""
    controls = jsdom.run(fixture_server.url("14-capture-attributes")).result
    for case in H.fixture_spec("14-capture-attributes")["describes_correct_behaviour"]:
        H.assert_control(controls, case, context="[BUG-CAPTURE-ATTRS/jsdom] ")


# ─── T-CAP-01 · the acceptance: rung 1 actually fires ────────────────────────

def _classify_captured(controls, css_hint: str):
    """Run ONE captured control through the real refiner and the real classifier.

    This is the production chain: ``build_inventory`` produces the ControlRecord
    that ``app.forms.resolve_field`` hands to ``field_signature.compute``, whose
    signature ``field_semantics.classify`` then adjudicates.
    """
    from app.inventory import build_inventory
    from app import field_semantics, field_signature

    records = build_inventory(controls)
    # build_inventory preserves order, so the i-th record is the i-th raw control.
    hits = [i for i, c in enumerate(controls) if c.get("css_hint") == css_hint]
    assert len(hits) == 1, (
        f"{css_hint} matched {len(hits)} captured controls; the fixture must "
        f"identify exactly one")
    record = records[hits[0]]
    sig = field_signature.compute(record, kind=record.get("kind", ""))
    return record, sig, field_semantics.classify(sig)


# PROMOTED to a permanent regression guard — formerly @_repro(_ATTRS_REASON).
@pytest.mark.playwright
def test_rung_one_fires_on_a_captured_control(pw, fixture_server) -> None:
    """THE acceptance for T-CAP-01.

    Not "the attribute appears in a debug payload" — the classifier must reach a
    verdict ON the declaration. ``basis == "autocomplete"`` is the only proof
    that rung 1, and not a lucky label token, decided the field.
    """
    controls = pw.collect(fixture_server.url("14-capture-attributes"))

    # input#field-a7 is the decisive one: its label is "Field A7", whose tokens
    # classify to nothing at all. If the verdict is given-name, the ONLY thing
    # that could have decided it is the captured autocomplete attribute.
    _, sig, verdict = _classify_captured(controls, "input#field-a7")
    assert sig["autocomplete"] == "given-name", (
        f"the signature carried no autocomplete: {sig}")
    assert verdict["basis"] == "autocomplete", (
        f"rung 1 did not decide this field — classify() fell through to "
        f"{verdict['basis']!r} with verdict {verdict}")
    assert verdict["type"] == "given_name"
    assert verdict["confidence"] == 0.98

    # And across the declared vocabulary, including the odd-cased one.
    for hint, want in (("input#email", "email"),
                       ("input#zip", "postal_code"),
                       ("input#phone", "phone"),
                       ("textarea#street", "street_address"),
                       ("select#country", "country"),
                       ("input#pw", "password"),
                       ("input#card", "card_number")):
        _, sig, verdict = _classify_captured(controls, hint)
        assert verdict["basis"] == "autocomplete" and verdict["type"] == want, (
            f"{hint}: expected rung 1 -> {want}, got basis={verdict['basis']!r} "
            f"type={verdict['type']!r} (autocomplete captured as "
            f"{sig['autocomplete']!r})")


# PROMOTED to a permanent regression guard — formerly @_repro(_ATTRS_REASON).
@pytest.mark.playwright
def test_nameless_fields_classify_from_placeholder_and_id(pw, fixture_server) -> None:
    """The two fallbacks that are dead code until placeholder/id are captured.

    ``field_signature.compute`` falls back to placeholder tokens, then to id
    tokens, for a control with no accessible name. A crawl that captures neither
    can only ever produce an empty token set for such a field.
    """
    controls = pw.collect(fixture_server.url("14-capture-attributes"))

    _, sig, verdict = _classify_captured(controls, "input#x1")
    assert "social" in " ".join(sig["placeholder_tokens"]), (
        f"placeholder tokens were not carried: {sig}")
    assert verdict["type"] == "national_id", f"placeholder fallback did not classify: {verdict}"

    record, sig, verdict = _classify_captured(controls, "input#date-of-birth")
    assert record["name"] == "", "fixture control is supposed to be nameless"
    assert sig["tokens"], (
        "a nameless field with a semantic id produced NO tokens — the id "
        f"fallback never fired: {sig}")
    assert verdict["type"] == "date_of_birth", (
        f"id fallback did not classify the field: {verdict}")


# PROMOTED to a permanent regression guard — formerly @_repro(_ATTRS_REASON).
@pytest.mark.playwright
def test_absent_empty_and_off_never_fabricate_a_verdict(pw, fixture_server) -> None:
    """The negative half of the contract.

    Capturing an attribute must not make the classifier confident about a field
    the app said nothing about. ``autocomplete="off"`` in particular is an
    explicit REFUSAL to declare semantics and must never reach rung 1.
    """
    controls = pw.collect(fixture_server.url("14-capture-attributes"))

    for hint in ("input#nickname", "input#empty-ac", "input#opted-out"):
        record, sig, verdict = _classify_captured(controls, hint)
        assert verdict["basis"] != "autocomplete", (
            f"{hint}: rung 1 fired on a non-declaration "
            f"(autocomplete={sig['autocomplete']!r}) -> {verdict}")

    # Absent and present-but-empty must be INDISTINGUISHABLE, or the same field
    # authored two ways would learn two different signatures.
    a = _classify_captured(controls, "input#nickname")[1]
    b = _classify_captured(controls, "input#empty-ac")[1]
    assert a["autocomplete"] == b["autocomplete"] == ""
    assert a["inputmode"] == b["inputmode"] == ""


# ─── T-CAP-04 · the emitted selector must resolve, not merely look escaped ───

@pytest.mark.playwright
def test_every_emitted_frame_selector_resolves(pw, fixture_server) -> None:
    """THE acceptance for T-CAP-04, asserted as CONSEQUENCE.

    This test does not care how a selector is spelled. It hands each emitted
    ``frame_selector`` back to the browser and asks it to resolve — the exact
    operation a generated script performs at replay. That is deliberately not a
    string comparison: a selector can be beautifully escaped and still wrong, and
    escaping it by broadening to ``iframe`` would satisfy a string test while
    silently binding every control to the first frame on the page.
    """
    # collect_fresh, NOT collect: this test resolves selectors against
    # ``pw.page``, so the page must actually BE on this fixture. The memoised
    # ``collect`` returns a cached list without navigating, which would leave the
    # browser on whatever fixture ran last and silently resolve every selector
    # against the wrong document.
    controls = pw.collect_fresh(fixture_server.url("16-iframe-special-chars"))
    spec = H.fixture_spec("16-iframe-special-chars")["frame_selectors_must_resolve"]

    selectors = sorted({c["frame_selector"] for c in controls if c["frame_selector"]})
    assert len(selectors) == spec["expect_distinct"], (
        f"expected {spec['expect_distinct']} distinct frame selectors, got "
        f"{len(selectors)}: {selectors}")

    async def _count(sel: str) -> int:
        return await pw.page.locator(sel).count()

    broken = {}
    for sel in selectors:
        try:
            n = pw.run(_count(sel))
        except Exception as exc:
            broken[sel] = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        if n != 1:
            broken[sel] = f"resolved to {n} elements"
    assert not broken, (
        "emitted frame_selectors that do not resolve to exactly one frame — "
        f"every control recorded against them is unbindable at replay: {broken}")

    # And each frame's own control really was captured behind its own selector,
    # so the four selectors are not four spellings of the same frame.
    by_selector = {}
    for c in controls:
        if c["frame_selector"]:
            by_selector.setdefault(c["frame_selector"], set()).add(c["css_hint"])
    assert sorted(h for hints in by_selector.values() for h in hints) == [
        "input#field-a", "input#field-b", "input#field-c", "input#field-d"], (
        f"frames and controls do not line up 1:1: {by_selector}")


# ─── T-CAP-05 · one ceiling, honoured by every layer ─────────────────────────

_SERVICE_ROOT = Path(__file__).resolve().parents[2]          # …/engines/qe-explorer
_QE_CENTRAL = _SERVICE_ROOT.parents[1] / "platform" / "qe-central" / "app" / "services"


def _module_constant(path: Path, name: str):
    """Read a module-level int constant WITHOUT importing the module.

    qe-central is a different service with its own dependency tree; importing it
    from the explorer's test run would pull in SQLAlchemy and the ORM. The
    constant is a contract, and parsing it is enough to hold both sides to it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is no longer defined in {path}")


def test_there_is_exactly_one_option_ceiling() -> None:
    """THE cross-layer contract.

    Fails if a future engineer changes ONE layer's ceiling without the others —
    which is exactly how 300 → 60 → 48 came to exist.
    """
    from app.inventory import MAX_OPTIONS as REFINER_CEILING
    from app.inventory_js import MAX_OPTIONS as WALKER_CEILING

    assert WALKER_CEILING == REFINER_CEILING, (
        f"the walker captures up to {WALKER_CEILING} options and the refiner "
        f"keeps only {REFINER_CEILING} — the refiner silently discards what the "
        f"walker was raised to preserve")

    catalog_py = _QE_CENTRAL / "catalog.py"
    if not catalog_py.exists():                       # explorer checked out alone
        pytest.skip(f"qe-central not present at {catalog_py}")

    catalogue_ceiling = _module_constant(catalog_py, "MAX_CATALOG_OPTIONS")
    assert catalogue_ceiling == WALKER_CEILING, (
        f"the catalogue's ceiling ({catalogue_ceiling}) differs from capture's "
        f"({WALKER_CEILING}); the two services share no library, so they can "
        f"only be held together by this test")


def test_no_layer_reapplies_a_private_option_ceiling() -> None:
    """The 48 that started this: a bare literal slice on an option list.

    ``catalog_store`` clipped options to ``MAX_CATALOG_OPTIONS`` when INSERTING a
    question and to a hard-coded 48 when UPDATING one — so a question's answer
    set shrank the second time a crawl saw it. Guarded by shape, so any new bare
    numeric clip of an option list fails here rather than in production.
    """
    store_py = _QE_CENTRAL / "catalog_store.py"
    if not store_py.exists():
        pytest.skip(f"qe-central not present at {store_py}")

    src = store_py.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r'options.*\[:\s*\d+\s*\]', line)
    ]
    assert not offenders, (
        "an option list is clipped by a bare number instead of the named "
        f"ceiling: {offenders}")


@pytest.mark.playwright
def test_250_option_select_survives_every_layer(pw, fixture_server) -> None:
    """The 250-option native select, end to end.

    The browser has 250; capture must see 250; the refiner must keep 250; and the
    form signal the catalogue is built from must carry 250.
    """
    from app.inventory import build_inventory, form_signal_for

    controls = pw.collect(fixture_server.url("07-native-select-250"))
    raw = [c for c in controls if c["css_hint"] == "select#country"]
    assert raw, "fixture control select#country was not captured"

    # 1 · the browser and 2 · capture
    assert len(raw[0]["options"]) == 250, (
        f"capture read {len(raw[0]['options'])} of 250 options")
    assert raw[0]["options_total"] == 250

    # 3 · the refiner
    records = build_inventory(controls)
    country = [r for r in records if r["name"] == "Country of Residence"]
    assert country, (
        f"the refiner dropped the control: {[r['name'] for r in records]}")
    assert len(country[0]["options"]) == 250, (
        f"the refiner kept only {len(country[0]['options'])} of the 250 options "
        f"capture preserved — the answer set is truncated before anything "
        f"downstream can see it")
    assert country[0]["options_total"] == 250

    # 4 · the form signal, which is what the catalogue actually reads
    signal = form_signal_for(country[0])
    assert signal is not None and len(signal["options"]) == 250, (
        f"form_snapshot_signals carried "
        f"{len(signal['options']) if signal else 'no'} options")


@pytest.mark.playwright
def test_clipping_is_deterministic_and_reports_the_true_size(pw, fixture_server) -> None:
    """Above the ceiling, the read is a PREFIX and says so.

    ``birth-year`` offers 320 options against a 300 ceiling. The contract is not
    "never clip" — it is "clip at one documented number, and never present the
    prefix as the whole", which ``options_total`` is what makes possible.
    """
    from app.inventory import MAX_OPTIONS, build_inventory

    controls = pw.collect(fixture_server.url("07-native-select-250"))
    records = build_inventory(controls)
    year = [r for r in records if r["name"] == "Year of Birth"]
    assert year, "fixture control select#birth-year was not captured"

    assert len(year[0]["options"]) == MAX_OPTIONS
    assert year[0]["options_total"] == 320, (
        "the TRUE size must survive clipping, or a 300-of-320 prefix is "
        "indistinguishable from a complete answer set")
    assert year[0]["options_total"] > len(year[0]["options"])


@pytest.mark.parametrize("count", [0, 1, 10, 48, 49, 60, 61, 250, 299, 300, 301])
def test_refiner_option_counts_are_exact_up_to_the_ceiling(count: int) -> None:
    """The refiner boundary, swept.

    Supplemental to the browser proof above — this is the layer that held the
    stale 60, and 48/49/60/61 are the counts at which the three historical
    ceilings would each have shown themselves.
    """
    from app.inventory import MAX_OPTIONS, build_control_record

    raw = {
        "role": "combobox", "tag": "select", "name": "Question",
        "options": [f"Option {i:03d}" for i in range(count)],
        "options_total": count,
    }
    record = build_control_record(raw)
    assert len(record["options"]) == min(count, MAX_OPTIONS)
    assert record["options_total"] == count
    if count:
        assert record["options"][0] == "Option 000"
        assert record["options"][-1] == f"Option {min(count, MAX_OPTIONS) - 1:03d}", (
            "clipping must take a deterministic PREFIX, not an arbitrary subset")
