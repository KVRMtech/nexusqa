"""T-HN-02 — real-Chromium EXECUTION lane, through the PRODUCTION injection path.

Every collection in this module goes through :class:`app.main.PlaywrightBrowserPort`
— the same adapter class ``app.main._run_job`` constructs for a live crawl. The
harness supplies no JavaScript, no locator strategy and no navigation logic of its
own: ``port.goto()`` is the production goto (429 backoff, hash-router nudge,
``_settle()`` quiescence wait) and ``port.collect_controls()`` is the production
``page.evaluate(INVENTORY_JS)``.

This lane is authoritative wherever jsdom is not: rendered accessible names
(`innerText`), layout-gated opaque detection (`getBoundingClientRect`),
`isContentEditable`, and true cross-origin isolation.
"""
from __future__ import annotations

import inspect

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]


# ─── The injection path itself ───────────────────────────────────────────────

def test_the_lane_uses_the_production_adapter() -> None:
    """This suite must exercise the SAME class the crawl entrypoint builds.

    Without this, the lane could drift into a test-local re-creation of the
    adapter and go on passing while production injection broke.
    """
    from app.main import PlaywrightBrowserPort, _run_job
    from app.browser import BrowserPort

    assert issubclass(PlaywrightBrowserPort, BrowserPort)

    entrypoint_src = inspect.getsource(_run_job)
    assert "PlaywrightBrowserPort(" in entrypoint_src, (
        "the crawl entrypoint no longer constructs PlaywrightBrowserPort — this "
        "lane is testing an adapter production does not use")

    collect_src = inspect.getsource(PlaywrightBrowserPort.collect_controls)
    assert "page.evaluate(INVENTORY_JS)" in collect_src.replace("self._", ""), (
        "collect_controls no longer evaluates INVENTORY_JS directly; the "
        "injection path under test has changed")

    # The harness must reach the page ONLY through the port. Checked on the
    # PARSED tree, not on the text, so a docstring that merely mentions
    # `page.evaluate(INVENTORY_JS)` while describing the production path does
    # not read as a violation.
    import ast
    tree = ast.parse(open(H.__file__, encoding="utf-8").read())
    offenders = [
        f"{H.__file__}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("evaluate", "evaluate_handle", "add_init_script")
    ]
    assert not offenders, (
        f"the harness calls evaluate() itself at {offenders} — the Playwright "
        f"lane must reach the page only through the production port")


def test_production_port_navigates_and_collects(pw, fixture_server) -> None:
    """DOM ready → script injected → inventory executed → controls collected."""
    from app.main import PlaywrightBrowserPort

    url = fixture_server.url("10-save-draft-wizard")

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        nav = await port.goto(url)
        assert nav.ok and nav.status == 200, f"navigation failed: {nav}"
        # DOM ready — asserted through the port's own title(), not a sleep.
        title = await port.title()
        assert "save-draft wizard" in title.lower()
        controls = await port.collect_controls()
        opaque = await port.collect_opaque()
        values = await port.collect_displayed_values()
        return nav, controls, opaque, values

    nav, controls, opaque, values = pw.run(_run())

    assert nav.url.endswith("/10-save-draft-wizard/index.html")
    assert len(controls) >= 8, f"inventory returned {len(controls)} controls"
    assert isinstance(opaque, list) and isinstance(values, list)
    names = {c["name"] for c in controls}
    assert {"Face Amount", "Save Draft", "Continue", "Cancel Application"} <= names


# ─── Execution across the whole fixture library ──────────────────────────────

def test_inventory_executes_in_chromium(pw, fixture_server, fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)
    if "playwright" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = pw.collect(fixture_server.url(fixture_name))
    assert isinstance(controls, list)
    assert len(controls) >= spec["min_controls"], (
        f"{fixture_name}: expected at least {spec['min_controls']} controls, "
        f"got {len(controls)}")


def test_expected_controls_in_chromium(pw, fixture_server, fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)
    if "playwright" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = pw.collect(fixture_server.url(fixture_name))
    ctx = f"[{fixture_name}/chromium] "

    asserted = 0
    for expected in spec.get("expect_controls", []):
        if not H.runs_in_lane(expected, "playwright"):
            continue
        H.assert_control(controls, expected, context=ctx)
        asserted += 1
    for forbidden in spec.get("forbid_controls", []):
        if not H.runs_in_lane(forbidden, "playwright"):
            continue
        H.assert_absent(controls, forbidden, context=ctx)
        asserted += 1

    assert asserted > 0, f"{ctx}every expectation was lane-excluded — vacuous pass"

    if "exact_control_count" in spec:
        assert len(controls) == spec["exact_control_count"], (
            f"{ctx}expected exactly {spec['exact_control_count']} controls, "
            f"got {len(controls)}")


def test_already_correct_rungs_in_chromium(pw, fixture_server, fixture_name) -> None:
    """The `already_correct` block: behaviour a sibling rung ALREADY gets right.

    Fixture 05 uses this to prove the accessible-name defect is localised to one
    helper rather than general — the same markup shape reached through
    `label[for]` produces the correct rendered name today.
    """
    spec = H.fixture_spec(fixture_name)
    cases = spec.get("already_correct")
    if not cases:
        pytest.skip("fixture declares no already-correct rungs")

    controls = pw.collect(fixture_server.url(fixture_name))
    for case in cases:
        H.assert_control(controls, case, context=f"[{fixture_name}/chromium] ")


def test_group_key_partition_in_chromium(pw, fixture_server, fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)
    partition = spec.get("group_key_partition")
    if not partition or "playwright" not in spec.get("lanes", []):
        pytest.skip("fixture declares no playwright-lane group_key partition")

    controls = pw.collect(fixture_server.url(fixture_name))
    keys = [c["group_key"] for c in controls if c.get("group_key")]
    distinct = sorted(set(keys))
    assert len(distinct) == partition["expect_distinct_nonempty"], (
        f"[{fixture_name}] expected {partition['expect_distinct_nonempty']} "
        f"distinct questions, found {len(distinct)}: {distinct}")
    if "expect_group_sizes" in partition:
        want = partition["expect_group_sizes"]
        wrong = {k: keys.count(k) for k in distinct if keys.count(k) != want}
        assert not wrong, f"[{fixture_name}] questions with wrong arity: {wrong}"


def test_href_absoluteness_in_chromium(pw, fixture_server, fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)
    rule = spec.get("href_absoluteness")
    if not rule:
        pytest.skip("fixture declares no href rule")

    controls = pw.collect(fixture_server.url(fixture_name))
    exempt = tuple(rule.get("exempt_schemes", []))
    offenders = [
        (c["css_hint"], c["href"]) for c in controls
        if c.get("tag") == "a" and c.get("href")
        and not c["href"].startswith(exempt)
        and not c["href"].startswith(("http://", "https://"))
    ]
    assert not offenders, f"[{fixture_name}] relative hrefs captured: {offenders}"


# ─── Opaque surfaces — Playwright only (layout-gated) ────────────────────────

def test_opaque_surfaces_in_chromium(pw, fixture_server, fixture_name) -> None:
    """OPAQUE_JS names the surfaces the walker cannot read.

    Layout-gated (`getBoundingClientRect`), so this can only run on a real
    engine — jsdom would report every rect as zero and detect nothing.
    """
    spec = H.fixture_spec(fixture_name)
    opaque_spec = spec.get("opaque")
    if not opaque_spec or "playwright" not in opaque_spec.get("lanes", []):
        pytest.skip("fixture declares no playwright-lane opaque expectations")

    pw.collect(fixture_server.url(fixture_name))          # production goto
    found = pw.run(H.collect_via_production_port(
        pw.page, pw.context, fixture_server.url(fixture_name), what="opaque"))

    for want in opaque_spec["expect"]:
        matches = [f for f in found
                   if f["kind"] == want["kind"]
                   and ("label" not in want or f["label"] == want["label"])]
        assert matches, (
            f"[{fixture_name}] no opaque surface matched {want}; the blind spot "
            f"would go unreported. found: {found}")

    for banned in opaque_spec.get("forbid_labels", []):
        offenders = [f for f in found if banned in f.get("label", "")]
        assert not offenders, (
            f"[{fixture_name}] {banned!r} is decorative chrome and must not be "
            f"ledgered: {offenders}")

    if "expect_count" in opaque_spec:
        assert len(found) == opaque_spec["expect_count"], (
            f"[{fixture_name}] expected {opaque_spec['expect_count']} opaque "
            f"surfaces, found {len(found)}: {found}")


def test_displayed_values_in_chromium(pw, fixture_server, fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)
    dv = spec.get("displayed_values")
    if not dv or "playwright" not in dv.get("lanes", []):
        pytest.skip("fixture declares no playwright-lane displayed-value expectations")

    values = pw.run(H.collect_via_production_port(
        pw.page, pw.context, fixture_server.url(fixture_name),
        what="displayed_values"))
    H.assert_displayed_values(values, dv, context=f"[{fixture_name}/chromium] ")


# ─── Cross-lane agreement + determinism ──────────────────────────────────────

def test_lanes_agree_on_structure(pw, jsdom, fixture_server) -> None:
    """Where both lanes are entitled to an opinion, they must agree.

    Compared on the STRUCTURAL fields only — the ones that do not depend on
    layout, `innerText` or `isContentEditable`. A disagreement here means one
    lane is lying about the walker rather than about its runtime.
    """
    structural = ("tag", "role", "input_type", "group_key", "required",
                  "disabled", "options_total", "frame_selector", "testid",
                  "min", "max", "step", "pattern", "minlength", "maxlength")

    for name in ("07-native-select-250", "08-radio-groups",
                 "09-questionnaire-20-samefingerprint", "12-download-step"):
        url = fixture_server.url(name)
        a = jsdom.run(url).result
        b = pw.collect(url)
        assert len(a) == len(b), (
            f"[{name}] jsdom captured {len(a)} controls, Chromium {len(b)} — "
            f"the lanes disagree about what exists on the page")
        for i, (ca, cb) in enumerate(zip(a, b)):
            for field in structural:
                assert ca.get(field) == cb.get(field), (
                    f"[{name}] control #{i} ({cb.get('css_hint')}) field {field!r}: "
                    f"jsdom={ca.get(field)!r} chromium={cb.get(field)!r}")


def test_chromium_execution_is_deterministic(pw, fixture_server) -> None:
    """Two identical production collections must be byte-identical."""
    url = fixture_server.url("07-native-select-250")
    first = pw.collect_fresh(url)            # bypasses the session memo cache
    second = pw.collect_fresh(url)
    assert H.canonical_json(first) == H.canonical_json(second)
