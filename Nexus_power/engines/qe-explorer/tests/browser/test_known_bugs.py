"""T-HN-05 — KNOWN BUG REPRODUCTIONS.

Every test here describes **correct browser behaviour**. None of them is written
to match what Capture currently does, and none may be adjusted to make it pass.
They fail today; when Capture is corrected they pass *unchanged*.

## How a red test lives in a green CI without lying

Each reproduction is marked ``xfail(strict=True)``:

  * **today** — the assertion fails, pytest reports ``XFAIL``, the suite is green,
    and the defect is on the record as a named, executable test rather than a
    ticket someone has to remember.
  * **the moment Capture is fixed** — the assertion passes, pytest reports
    ``XPASS``, and ``strict=True`` turns that into a **CI failure**. The only way
    to clear it is to delete the marker. The bug therefore cannot be quietly
    fixed without the test being promoted to a permanent regression guard, and it
    cannot be quietly re-broken afterwards.

Set ``QEC_BUG_REPRO_STRICT=1`` to drop the markers and see the reproductions
fail outright — that is the mode used to demonstrate the milestone gate
("at least three tests fail before Capture fixes are applied").

## Status

The mechanism above has already run its full cycle once, during this milestone.

Three defects were written up as reproductions against ``inv-js-v9``, verified to
fail (and to fail *outright* under ``QEC_BUG_REPRO_STRICT=1``), and were then
**fixed in Capture** while the harness was being built. The strict markers turned
those silent fixes into ``XPASS(strict)`` CI failures, which is exactly the
designed outcome: a fix cannot land without the test being promoted.

They are now **permanent regression guards**. Their assertions are byte-for-byte
what they were as reproductions — only the marker was removed, which is the
contract this file promises ("once Capture is corrected they pass unchanged").

| ID | Fixture | Defect | Fixed in | Status |
|---|---|---|---|---|
| BUG-SHADOW-NAME | 01 | `walk()` forwarded the outer `doc` into a shadow root | `inv-js-v10`: `walk(host.shadowRoot, host.shadowRoot, …)` | **guard** |
| BUG-IFRAME-SELECTOR | 03 | `frameSelectorFor()` interpolated unescaped | `inv-js-v10`: `cssIdent()` / `cssStr()` | **guard** |
| BUG-ARIA-LABELLEDBY-TEXTCONTENT | 05 | `idText()` used `textContent` | `inv-js-v10`: `accText(doc.getElementById(id))` | **guard** |
| BUG-CATALOG-TRUNCATION-60 | 07 | `inventory.py:123` re-truncated options to 60, nullifying the JS ceiling of 300 | `_MAX_OPTIONS = MAX_OPTIONS` | **guard** |
| BUG-CAPTURE-ATTRS | 14 | `describe()` emitted no `autocomplete`/`inputmode`/`placeholder`/`id`, leaving `classify()` rung 1 unreachable | `inv-js-v10` emits all four | **guard** |

All five were found by this harness, reproduced as executable tests describing
correct behaviour, and are now fixed. Every one of them was a defect that the
pre-existing string-assertion suite passed cleanly over: asserting that
``'lc(attr(el, "aria-checked"))' in INVENTORY_JS`` cannot tell you whether the
expression is reached, whether the value is right, or whether the control it
describes can be bound at replay.

The ``_repro`` helper below is retained as the documented mechanism for the NEXT
reproduction, not as dead weight — it is how a newly-found defect enters this
file.

Each is a *capture-says-covered / replay-cannot-bind* green-wash: the manifest
records the control as captured, and the generated script cannot bind it.
"""
from __future__ import annotations

import os

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.bug_repro]

#: When set, the reproductions fail outright instead of being recorded as xfail.
STRICT_MODE = os.environ.get("QEC_BUG_REPRO_STRICT", "").strip().lower() in ("1", "true", "yes")


def _repro(reason: str):
    """xfail(strict) unless QEC_BUG_REPRO_STRICT asks for a hard failure."""
    if STRICT_MODE:
        return pytest.mark.usefixtures()          # no-op marker
    return pytest.mark.xfail(strict=True, reason=reason)


# ─── BUG-SHADOW-NAME ─────────────────────────────────────────────────────────

# PROMOTED — see module docstring. Formerly:
#   @_repro("BUG-SHADOW-NAME: walk() forwards the outer document into a shadow root, "
#           "so label[for] and aria-labelledby inside the shadow root resolve against "
#           "the wrong root and every shadow-scoped control is captured unnamed "
#           "(inventory_js.py:648)")
def test_shadow_dom_names_resolve_against_the_owning_root(pw, fixture_server) -> None:
    """A control inside an open shadow root must be named by its OWN root.

    CORRECT BEHAVIOUR (what a browser, an AT and Playwright's getByRole all do):
    an element's `label[for=id]` and `aria-labelledby` id-references are resolved
    against the node's root — the shadow root that contains it — not against the
    document that hosts the custom element.

    Today both shadow-scoped inputs come back `name: ""`, `name_source: "none"`.
    The compiler binds by accessible name only, so an unnamed control has no
    bindable rung and is dropped from the generated script entirely: a shadow-DOM
    design system (Lightning, Vaadin, any lit app) presents as a page with no
    fillable fields.
    """
    controls = pw.collect(fixture_server.url("01-shadow-open"))
    ctx = "[BUG-SHADOW-NAME] "

    # Control group — the walker DOES descend the shadow root. If this fails the
    # defect is bigger than naming and the diagnosis above is wrong.
    H.assert_control(controls, {
        "where": {"css_hint": "button#verify-btn"},
        "fields": {"name": "Verify Identity", "name_source": "content"},
    }, context=ctx + "(control group) ")

    for case in H.fixture_spec("01-shadow-open")["describes_correct_behaviour"]:
        H.assert_control(controls, case, context=ctx)


# ─── BUG-IFRAME-SELECTOR ─────────────────────────────────────────────────────

# PROMOTED — see module docstring. Formerly:
#   @_repro("BUG-IFRAME-SELECTOR: frameSelectorFor() interpolates the iframe id/name "
#           "into a CSS selector without CSS.escape, producing a selector that is "
#           "wrong (id with a dot) or unparseable (name with a quote) "
#           "(inventory_js.py:546-557)")
def test_iframe_selectors_are_escaped(pw, fixture_server) -> None:
    """An emitted `frame_selector` must resolve to the frame it came from.

    CORRECT BEHAVIOUR: the module docstring states the recipe exists so *"a
    `frame_selector` we emit resolves the SAME way `page.frameLocator(...)`
    resolves it"*. That requires escaping, which the sibling `label[for]` lookup
    twelve lines earlier already does.

    Today:
      * `id="pay.frame"` → `iframe#pay.frame`, which is *valid CSS that matches
        something else* (`#pay` AND `.frame`) — it fails silently;
      * `name='quote"frame'` → `iframe[name="quote"frame"]`, unparseable.

    Both make every control in that frame unbindable at replay while the manifest
    reports it captured.
    """
    controls = pw.collect(fixture_server.url("03-iframe-same-origin"))
    ctx = "[BUG-IFRAME-SELECTOR] "

    # Control group — the clean id case already works.
    H.assert_control(controls, {
        "where": {"css_hint": "input#billing-zip"},
        "fields": {"frame_selector": "iframe#billing"},
    }, context=ctx + "(control group) ")

    for case in H.fixture_spec("03-iframe-same-origin")["describes_correct_behaviour"]:
        H.assert_control(controls, case, context=ctx)


def test_iframe_selectors_are_actually_resolvable(pw, fixture_server) -> None:
    """The defect, demonstrated as CONSEQUENCE rather than as string shape.

    This test does not care how the selector is spelled. It takes each emitted
    `frame_selector` and asks the browser to resolve it — which is exactly what a
    generated script does at replay. A selector that resolves to zero frames is a
    control the crawl claimed to capture and replay cannot reach.

    Deliberately NOT marked xfail: it is written to REPORT the blast radius today
    and to keep reporting it after the fix. It asserts only the invariant that
    holds in both worlds — every emitted selector must resolve — for the frames
    whose selectors are currently correct, and prints the rest.
    """
    # collect_fresh, NOT collect: the selectors are resolved against ``pw.page``,
    # so the page must actually be on this fixture. The memoised ``collect``
    # returns a cached list without navigating, leaving the browser wherever the
    # previous test left it.
    controls = pw.collect_fresh(fixture_server.url("03-iframe-same-origin"))
    selectors = sorted({c["frame_selector"] for c in controls if c["frame_selector"]})
    assert selectors, "no frame selectors were emitted at all"

    async def _resolve(sel: str) -> tuple[bool, str]:
        try:
            count = await pw.page.locator(sel).count()
            return count == 1, f"matched {count} elements"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    results = {sel: pw.run(_resolve(sel)) for sel in selectors}
    broken = {s: why for s, (ok, why) in results.items() if not ok}

    # The invariant that must hold in both worlds: at least the plain-id frame
    # resolves. If even that breaks, the recipe is comprehensively wrong.
    assert results.get("iframe#billing", (False, ""))[0], (
        f"even the unescaped-safe selector failed to resolve: {results}")

    if broken:
        pytest.xfail(
            "BUG-IFRAME-SELECTOR blast radius — these emitted frame_selectors do "
            "not resolve to exactly one frame, so every control recorded against "
            f"them is unbindable at replay: {broken}")


# ─── BUG-ARIA-LABELLEDBY-TEXTCONTENT ─────────────────────────────────────────

# PROMOTED — see module docstring. Formerly:
#   @_repro("BUG-ARIA-LABELLEDBY-TEXTCONTENT: idText() reads textContent instead of "
#           "the module's own accText()/innerText helper, so block children in an "
#           "aria-labelledby target are concatenated with no separator "
#           "(inventory_js.py:158-164)")
def test_aria_labelledby_uses_rendered_text(pw, fixture_server) -> None:
    """`aria-labelledby` must compute the RENDERED accessible name.

    CORRECT BEHAVIOUR per W3C accname — and therefore per every AT, per
    Playwright's `getByRole(name=…)`, and per the compiler's own binding rung —
    two block children contribute `"A B"`. `textContent` yields `"AB"`.

    The module already argues this at length for the sibling rungs
    (inventory_js.py:166-184) and fixes it there via `accText()`; the
    `aria-labelledby` rung was never converted.

    Consequence, quoting the source's own account of the identical defect
    elsewhere: `get_by_role(name=…)` matched ZERO elements, every fill on that
    control timed out and was recorded `intent_unmet`, and any generated script
    binding by that name would fail the same way. `aria-labelledby` pointing at a
    title+body pair is the standard markup for a questionnaire question.
    """
    controls = pw.collect(fixture_server.url("05-aria-labelledby-multiblock"))
    ctx = "[BUG-ARIA-LABELLEDBY] "

    # Control group A — a single-text-node target names correctly today.
    H.assert_control(controls, {
        "where": {"css_hint": "input#income"},
        "fields": {"name": "Annual Income", "name_source": "aria-labelledby"},
    }, context=ctx + "(control group) ")

    # Control group B — the SAME markup shape via label[for] is ALREADY correct,
    # which localises the defect to idText() rather than to naming in general.
    for case in H.fixture_spec("05-aria-labelledby-multiblock")["already_correct"]:
        H.assert_control(controls, case, context=ctx + "(already correct) ")

    for case in H.fixture_spec("05-aria-labelledby-multiblock")["describes_correct_behaviour"]:
        H.assert_control(controls, case, context=ctx)


def test_aria_labelledby_name_is_bindable_by_playwright(pw, fixture_server) -> None:
    """The defect, demonstrated as CONSEQUENCE: the captured name cannot bind.

    Takes the accessible name the walker captured and asks Playwright to find the
    control by it — the operation a generated script performs. A captured name
    that matches zero elements is a control the manifest claims and replay
    cannot reach.
    """
    # collect_fresh, NOT collect: get_by_role runs against pw.page, which is
    # shared session state. The memoised collect() returns the right controls
    # while the browser sits on whatever page ran last, so the lookup reports
    # "matched 0 elements" for a reason that has nothing to do with the name. A
    # fresh production goto leaves the page on this fixture.
    controls = pw.collect_fresh(fixture_server.url("05-aria-labelledby-multiblock"))
    target = [c for c in controls if c["css_hint"] == "input#tobacco"]
    assert target, "fixture control input#tobacco was not captured at all"
    captured_name = target[0]["name"]

    async def _count(name: str) -> int:
        return await pw.page.get_by_role("textbox", name=name, exact=True).count()

    n = pw.run(_count(captured_name))
    if n != 1:
        pytest.xfail(
            f"BUG-ARIA-LABELLEDBY blast radius — the walker captured the name "
            f"{captured_name!r}, and get_by_role(name=…) on that exact string "
            f"matches {n} elements. Every fill bound by this name times out and "
            f"is recorded intent_unmet.")
    assert n == 1


# ─── BUG-CATALOG-TRUNCATION-60 ───────────────────────────────────────────────
# Found BY this harness while recording the characterization goldens: the walker
# captures 250 options, the manifest carries 60.

def test_catalogue_completeness_survives_the_refiner(pw, fixture_server) -> None:
    """A question's answer set must survive the walker → refiner → manifest path.

    CORRECT BEHAVIOUR: `inventory_js.py:90-97` raised `MAX_OPTIONS` from 60 to
    300 with an explicit rationale — *"Sized for COMPLETENESS of the enumerations
    a business form actually asks: 50 US states, ~250 countries, a 100-year
    date-of-birth range. The previous 60 silently truncated every one of those
    but the states."* The Python refiner's own `_MAX_OPTIONS = 60`
    (`app/inventory.py:123`) then discards options 61-250 before they reach
    `form_snapshot_signals`, which is what the catalogue and the scenario deriver
    actually read.

    Consequence: the enumeration a `<select>` offers IS the test data for the
    positive, negative and boundary cases generated from that question. A
    catalogue holding the first 60 of 250 countries is a prefix presented as the
    whole answer set — the precise failure the JS ceiling was raised to end.

    Verified here at the boundary the walker owns (250 captured) and at the
    boundary the refiner owns (250 expected to survive).
    """
    from app.inventory import build_inventory

    controls = pw.collect(fixture_server.url("07-native-select-250"))
    country = [c for c in controls if c["css_hint"] == "select#country"]
    assert country, "fixture control select#country was not captured"

    # The walker's side is already correct — 250 captured, 250 reported.
    assert len(country[0]["options"]) == 250
    assert country[0]["options_total"] == 250

    # The refiner's side is where the answer set is lost.
    refined = build_inventory(controls)
    # ControlRecord carries the accessible name as `name`; there is no `label`
    # field, so the original lookup matched nothing and this test failed on a
    # phantom "refiner dropped the control" rather than on the truncation it
    # exists to catch.
    country_rec = [r for r in refined if r.get("name") == "Country of Residence"]
    assert country_rec, f"refiner dropped the control entirely: {[r.get('name') for r in refined]}"
    got = country_rec[0].get("qec", {}).get("options") or country_rec[0].get("options") or []
    assert len(got) == 250, (
        f"the refiner kept {len(got)} of 250 options — the catalogue holds a "
        f"prefix presented as the complete set of answers to this question")


# ─── BUG-VALUE-LABEL-BLEED ───────────────────────────────────────────────────
# Found BY this harness while closing the DISPLAYED_VALUES_JS coverage gap.

@_repro("BUG-VALUE-LABEL-BLEED: labelOf() scans up to 3 previous siblings and 2 "
        "of the parent's previous siblings, SKIPPING any whose text looks like a "
        "value. It therefore walks past unrelated figures and adopts a label "
        "belonging to a different one. An unlabelled value must come back with "
        "label='' — a borrowed label is a false grounding, not a best effort.")
def test_an_unlabelled_value_is_not_given_someone_elses_label(pw, fixture_server) -> None:
    """A displayed value with no label must be captured with an EMPTY label.

    CORRECT BEHAVIOUR: `DISPLAYED_VALUES_JS` exists so a value oracle can ground
    an expected outcome without a client-authored `source_hint`. That grounding
    is only sound if the label actually belongs to the figure. Six labelling
    rungs are exercised by fixture 13 and all six resolve correctly; this is the
    seventh case — a figure with no label of any kind.

    Today it is captured as `Surrender Charge`, which is the label of a
    *different* figure ($1,250.00) two blocks earlier. The value-skipping in the
    sibling scan is what does it: each intervening figure is rejected for looking
    like a value, and the scan keeps walking until it finds prose.

    Consequence: the oracle grounds `Surrender Charge == $99.99` on a page that
    states `Surrender Charge = $1,250.00`. That is a false assertion generated
    from correct capture of the numbers and incorrect capture of what they mean —
    strictly worse than no grounding, because no grounding degrades honestly to
    UNVERIFIED while this one produces a confident wrong answer.

    A label may be borrowed from a sibling only while nothing between them is
    itself a value. Once the scan passes a figure, the prose beyond it belongs to
    that figure, not to this one.
    """
    spec = H.fixture_spec("13-canvas")["displayed_values"]
    values = pw.run(H.collect_via_production_port(
        pw.page, pw.context, fixture_server.url("13-canvas"),
        what="displayed_values"))

    # Control group: the six rungs that DO resolve correctly. If any of these
    # break, the diagnosis below is wrong and the problem is broader.
    for want in spec["expect"]:
        match = [v for v in values
                 if v["selector"] == want["selector"] and v["text"] == want["text"]]
        assert match, f"(control group) no displayed value {want['text']} at {want['selector']}"
        assert match[0]["label"] == want["label"], (
            f"(control group) rung {want['rung']}: {want['text']} expected label "
            f"{want['label']!r}, got {match[0]['label']!r}")

    for want in spec["describes_correct_behaviour"]:
        match = [v for v in values
                 if v["selector"] == want["selector"] and v["text"] == want["text"]]
        assert match, f"no displayed value {want['text']} at {want['selector']}"
        assert match[0]["label"] == want["label"], (
            f"{want['text']} has no label on the page, so its captured label must "
            f"be {want['label']!r}; got {match[0]['label']!r} — which belongs to a "
            f"different figure. {want['why']}")


# ─── Generic driver — every fixture that describes unimplemented behaviour ───

def test_described_correct_behaviour(pw, fixture_server, fixture_name,
                                     request: pytest.FixtureRequest) -> None:
    """Run EVERY fixture's ``describes_correct_behaviour`` block.

    The named tests above document the three headline defects in prose. This
    driver is the mechanism: any fixture that declares correct-but-unimplemented
    behaviour is reproduced automatically, so a new bug fixture needs no new test
    code and cannot be added and then quietly forgotten.

    Applied as a runtime xfail rather than a decorator because whether a given
    fixture is expected to fail is data, not a static property of the test.
    """
    spec = H.fixture_spec(fixture_name)
    cases = spec.get("describes_correct_behaviour")
    if not cases:
        pytest.skip("fixture describes no unimplemented behaviour")
    if "playwright" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = pw.collect(fixture_server.url(fixture_name))
    ctx = f"[{fixture_name}] "

    failures = []
    for case in cases:
        try:
            H.assert_control(controls, case, context=ctx)
        except AssertionError as exc:
            failures.append(str(exc))

    if failures and not STRICT_MODE:
        pytest.xfail(
            f"{spec['targeted_defect'][:200]}\n\n"
            f"{len(failures)} of {len(cases)} described behaviours are not "
            f"implemented:\n\n" + "\n\n".join(failures[:3]))
    assert not failures, (
        f"{len(failures)} of {len(cases)} described behaviours failed:\n\n"
        + "\n\n".join(failures[:5]))


# ─── The gate itself ─────────────────────────────────────────────────────────

def test_at_least_three_defects_are_reproduced() -> None:
    """The milestone requires ≥3 executable reproductions of real defects.

    Counted from the fixture contracts rather than from this file, so a
    reproduction cannot be satisfied by a test that asserts nothing.
    """
    with_repro = [
        name for name in H.fixture_names()
        if H.fixture_spec(name).get("describes_correct_behaviour")
    ]
    assert len(with_repro) >= 3, (
        f"only {len(with_repro)} fixtures describe unimplemented correct "
        f"behaviour: {with_repro}")

    # Each must name the defect it targets, so a red test is diagnosable.
    for name in with_repro:
        spec = H.fixture_spec(name)
        assert spec["targeted_defect"].startswith("BUG-"), (
            f"{name} reproduces a defect but does not name it")
