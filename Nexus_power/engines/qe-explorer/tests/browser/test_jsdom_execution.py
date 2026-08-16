"""T-HN-01 — jsdom EXECUTION lane.

Every test in this module runs the REAL `app.inventory_js.INVENTORY_JS` inside a
real JavaScript engine against a real DOM.  Nothing asserts on the source text;
nothing re-implements the walker.  The contrast with the pre-existing suite is
the point: `test_custom_toggle_state.py` asserts
``'lc(attr(el, "aria-checked"))' in INVENTORY_JS`` — which passes whether or not
that expression is ever reached, whether or not it is inside dead code, and
whether or not the value it produces is correct.  These tests execute it.

Lane boundaries are declared per fixture in ``expected.json`` and enforced here,
so an assertion that jsdom cannot honestly adjudicate is SKIPPED with a reason
rather than silently weakened.
"""
from __future__ import annotations

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.jsdom]


# ─── The lane itself ─────────────────────────────────────────────────────────

def test_production_snippet_is_read_not_copied() -> None:
    """The harness must ship NO JavaScript of its own.

    Guards the single most important property of this milestone: if the harness
    ever grew a copy of the walker, every test below would be testing the copy.
    """
    js = H.production_snippet("INVENTORY_JS")
    from app.inventory_js import INVENTORY_JS
    assert js is INVENTORY_JS, "production_snippet() must return the live constant"

    runner = H.JSDOM_RUNNER.read_text(encoding="utf-8")
    for fingerprint in ("function walk(", "function accessibleName(", "var SELECTOR"):
        assert fingerprint not in runner, (
            f"the jsdom runner contains {fingerprint!r} — capture logic has been "
            f"duplicated into the harness")


def test_jsdom_capability_probe(jsdom, fixture_server) -> None:
    """PROVE which browser APIs jsdom provides, rather than assuming them.

    The capability set decides which assertions this lane is entitled to make.
    Pinning it here means a jsdom upgrade that ADDS `innerText` shows up as a
    failing test — an invitation to move assertions back from the Playwright
    lane — instead of silently leaving them stranded.
    """
    res = jsdom.run(fixture_server.url("01-shadow-open"))
    caps = res.capabilities

    # Present natively, and load-bearing for this lane.
    for required in ("attach_shadow", "get_root_node", "computed_style", "closest"):
        assert caps.get(required) is True, (
            f"jsdom no longer provides {required!r}; the jsdom lane cannot run "
            f"the fixtures that depend on it")

    # SUPPLIED BY THE HARNESS. jsdom 24 ships no `CSS` object at all, so the
    # walker's first accessible-name rung throws a ReferenceError into its own
    # try/catch and every labelled control comes back unnamed. The runner
    # installs the CSSOM-spec algorithm on the environment (never on the
    # production snippet) and declares that it did so.
    assert caps.get("css_escape") is True
    assert caps.get("css_escape_polyfilled") is True, (
        "jsdom now provides CSS.escape natively — drop CSS_ESCAPE_SHIM from "
        "browser_harness/jsdom_runner.js so this lane runs on the real API")

    # ABSENT, deliberately NOT supplied, and the reason several assertions are
    # Playwright-only: faking either would fabricate the behaviour under test.
    assert caps.get("innerText") is False, (
        "jsdom now implements innerText — the rendered-accessible-name "
        "assertions currently restricted to the Playwright lane should be moved "
        "back into this lane (fixtures 05, and accText coverage generally)")
    assert caps.get("layout_boxes") is False, (
        "jsdom now reports layout boxes — the OPAQUE_JS size-gated assertions "
        "currently restricted to the Playwright lane can move back here")
    assert caps.get("is_content_editable") is False, (
        "jsdom now implements isContentEditable — the contenteditable role and "
        "value assertions currently restricted to the Playwright lane can move "
        "back here")


def test_jsdom_runner_fails_loudly_on_a_broken_snippet(jsdom, fixture_server,
                                                       tmp_path) -> None:
    """A runner that swallowed errors would pass every `forbid` assertion.

    Negative control for the harness itself: feed it JavaScript that throws and
    require a hard failure, not an empty list.
    """
    broken = tmp_path / "broken.js"
    broken.write_text("(() => { throw new Error('deliberate'); })()", encoding="utf-8")
    with pytest.raises(RuntimeError, match="deliberate"):
        H.run_jsdom(fixture_server.url("01-shadow-open"), "INVENTORY_JS", broken)


# ─── Execution across the whole fixture library ──────────────────────────────

def test_inventory_executes_in_jsdom(jsdom, fixture_server, fixture_name) -> None:
    """The production walker RUNS in jsdom and returns a RawControl array."""
    spec = H.fixture_spec(fixture_name)
    if "jsdom" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only: "
                    f"{spec.get('lane_note', 'see expected.json')}")

    res = jsdom.run(fixture_server.url(fixture_name))
    controls = res.result

    assert isinstance(controls, list), (
        f"INVENTORY_JS returned {type(controls).__name__}, not an array")
    assert not [c for c in res.console if c.startswith("jsdomError")], (
        f"the fixture page raised errors, so the capture ran against a broken "
        f"DOM: {res.console}")
    assert len(controls) >= spec["min_controls"], (
        f"{fixture_name}: expected at least {spec['min_controls']} controls, "
        f"got {len(controls)}")

    # Shape contract: every RawControl carries the full field set the Python
    # refiner consumes. A silently dropped field would otherwise surface much
    # later as a KeyError-shaped default deep in build_inventory.
    required_fields = {
        "role", "name", "name_source", "best_effort", "kind", "tag", "input_type",
        "options", "options_total", "group_key", "required", "disabled",
        "frame_selector", "testid", "css_hint", "value_committed", "href",
        "haspopup", "expanded", "pressed", "aria_checked", "min", "max", "step",
        "pattern", "minlength", "maxlength", "draggable", "roledescription",
        "landmark",
    }
    missing = required_fields - set(controls[0])
    assert not missing, f"RawControl is missing fields: {sorted(missing)}"


def test_expected_controls_in_jsdom(jsdom, fixture_server, fixture_name) -> None:
    """Each fixture's declared expectations, executed."""
    spec = H.fixture_spec(fixture_name)
    if "jsdom" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = jsdom.run(fixture_server.url(fixture_name)).result
    ctx = f"[{fixture_name}/jsdom] "

    asserted = 0
    for expected in spec.get("expect_controls", []):
        if not H.runs_in_lane(expected, "jsdom"):
            continue                      # declared playwright-only; see lane_note
        H.assert_control(controls, expected, context=ctx)
        asserted += 1
    for forbidden in spec.get("forbid_controls", []):
        if not H.runs_in_lane(forbidden, "jsdom"):
            continue
        H.assert_absent(controls, forbidden, context=ctx)
        asserted += 1

    assert asserted > 0, (
        f"{ctx}every expectation was lane-excluded — this fixture is declared "
        f"jsdom-capable but asserts nothing here, which is a vacuous pass")

    if "exact_control_count" in spec:
        assert len(controls) == spec["exact_control_count"], (
            f"{ctx}expected exactly {spec['exact_control_count']} controls, "
            f"got {len(controls)}")


def test_group_key_partition_in_jsdom(jsdom, fixture_server, fixture_name) -> None:
    """The SET of distinct group_keys is the set of questions the page asks.

    A per-control assertion cannot catch a collapse (two questions merged into
    one key) or a shatter (one question split across keys). The partition can.
    """
    spec = H.fixture_spec(fixture_name)
    partition = spec.get("group_key_partition")
    if not partition:
        pytest.skip("fixture declares no group_key partition")
    if "jsdom" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = jsdom.run(fixture_server.url(fixture_name)).result
    keys = [c["group_key"] for c in controls if c.get("group_key")]
    distinct = sorted(set(keys))

    assert len(distinct) == partition["expect_distinct_nonempty"], (
        f"[{fixture_name}] expected {partition['expect_distinct_nonempty']} distinct "
        f"questions, found {len(distinct)}: {distinct}")

    if "expect_group_sizes" in partition:
        want = partition["expect_group_sizes"]
        sizes = {k: keys.count(k) for k in distinct}
        wrong = {k: n for k, n in sizes.items() if n != want}
        assert not wrong, (
            f"[{fixture_name}] every question must hold {want} controls; "
            f"these do not: {wrong}")


def test_name_fingerprint_collapse_is_real(jsdom, fixture_server, fixture_name) -> None:
    """Fixture 09 only tests what it claims if the names really do collide."""
    spec = H.fixture_spec(fixture_name)
    collapse = spec.get("name_fingerprint_collapse")
    if not collapse:
        pytest.skip("fixture declares no fingerprint-collapse property")

    controls = jsdom.run(fixture_server.url(fixture_name)).result
    radios = [c for c in controls if c.get("role") == "radio"]
    pairs = {(c["role"], c["name"]) for c in radios}
    assert len(pairs) == collapse["expect_distinct_role_name_pairs_among_radios"], (
        f"[{fixture_name}] the fixture is supposed to present controls that are "
        f"indistinguishable by (role, name); it now has {len(pairs)} distinct "
        f"pairs {sorted(pairs)} and no longer tests the adversarial case")


def test_href_absoluteness_in_jsdom(jsdom, fixture_server, fixture_name) -> None:
    """Every captured anchor destination must be resolvable on its own."""
    spec = H.fixture_spec(fixture_name)
    rule = spec.get("href_absoluteness")
    if not rule:
        pytest.skip("fixture declares no href rule")

    controls = jsdom.run(fixture_server.url(fixture_name)).result
    exempt = tuple(rule.get("exempt_schemes", []))
    offenders = [
        (c["css_hint"], c["href"]) for c in controls
        if c.get("tag") == "a" and c.get("href")
        and not c["href"].startswith(exempt)
        and not c["href"].startswith(("http://", "https://"))
    ]
    assert not offenders, (
        f"[{fixture_name}] these hrefs came back relative — the crawler has no "
        f"origin to resolve them against and every follow dead-ends: {offenders}")


def test_displayed_values_in_jsdom(jsdom, fixture_server, fixture_name) -> None:
    """DISPLAYED_VALUES_JS grounds rendered value nodes the walker cannot see."""
    spec = H.fixture_spec(fixture_name)
    dv = spec.get("displayed_values")
    if not dv or "jsdom" not in dv.get("lanes", []):
        pytest.skip("fixture declares no jsdom-lane displayed-value expectations")

    values = jsdom.run(fixture_server.url(fixture_name), "DISPLAYED_VALUES_JS").result
    H.assert_displayed_values(values, dv, context=f"[{fixture_name}/jsdom] ")


# ─── Determinism ─────────────────────────────────────────────────────────────

def test_jsdom_execution_is_deterministic(jsdom, fixture_server) -> None:
    """Two identical runs must produce identical output.

    Run against the busiest fixture (250+320 options, four selects). A capture
    that depended on iteration order, a Set's traversal, or anything ambient
    would show up here rather than as an intermittent golden diff months later.
    """
    url = fixture_server.url("07-native-select-250")
    first = jsdom.fresh(url).result          # bypasses the session memo cache
    second = jsdom.fresh(url).result
    assert H.canonical_json(first) == H.canonical_json(second)
