"""M3.2 — THE TWO OPAQUE SURFACES, TURNED INTO CATALOGUED EVIDENCE.

Two DOM-visibility gaps close here, and they fail in opposite directions, so
each is proved by a fixture the other cannot satisfy:

``T-FR-01`` cross-origin iframes
    ``INVENTORY_JS`` stops at an origin boundary because JavaScript running
    inside the page structurally cannot cross one, and must not try.  The PORT
    crosses it the supported way — ``content_frame()`` asks the browser for the
    frame's own execution context and the walker runs inside it under that
    frame's origin, exactly as the frame's own scripts do.  Nothing is injected
    across the boundary.

``T-FR-02`` closed shadow roots
    ``attachShadow({mode:"closed"})`` hands its root to the component and to
    nobody else, so the ONLY moment it is observable is the moment it is created.
    The capture hook is installed on the browser CONTEXT, before any page script
    runs.  A closed shadow root is an encapsulation convention, not a security
    boundary, and the DOM it hides is same-origin content the page already
    rendered — so this crosses nothing the browser is defending.

``T-FR-03`` selector correctness
    A frame selector must PARSE (escaping), identify exactly ONE frame
    (uniqueness), and be addressed from its own root rather than by a
    page-global ordinal (scoping).

THE STANDARD OF PROOF.  Every assertion below is a consequence, never a spelling:
the selectors this capture emits are handed back to a real browser and required
to bind the controls that were captured through them.  A selector can be
beautifully escaped, resolve to exactly one frame, and still be the wrong frame —
which is the failure that reads as coverage, and therefore the one worth testing
for.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


# ─── T-FR-01 · a cross-origin payment iframe is entered and catalogued ───────

def test_a_cross_origin_payment_iframe_is_entered_and_catalogued(
        pw, fixture_server) -> None:
    """THE T-FR-01 acceptance.

    The fields inside a foreign payment embed must arrive as ordinary catalogued
    controls — named, roled, and carrying a ``frame_selector`` that binds back to
    the frame they were read from. Anything less is "detected", which is what the
    ledger already did.
    """
    controls = pw.collect_fresh(fixture_server.url("04-iframe-cross-origin"))
    by_name = {c["name"]: c for c in controls}

    for field in ("Card Number", "CVC", "Pay Now"):
        assert field in by_name, (
            f"{field!r} was not catalogued. The cross-origin embed was not "
            f"entered, so the part of the checkout that takes the money is still "
            f"a blind spot. Captured: {sorted(by_name)}")
        assert by_name[field]["frame_selector"] == "iframe#card-entry", (
            f"{field!r} carries frame_selector "
            f"{by_name[field]['frame_selector']!r} — a control recorded against "
            f"no frame, or the wrong one, is unbindable at replay")

    # The main frame is untouched: entering an embed must not disturb the page.
    assert by_name["Amount Due"]["frame_selector"] == ""
    assert by_name["Amount Due"]["value_committed"] == "129.00"


def test_the_walker_itself_still_refuses_to_read_across_the_origin_boundary() -> None:
    """The honest skip is UNCHANGED, and this is the test that keeps it that way.

    The port crossing the boundary with Playwright is not a licence for the
    injected JavaScript to try. ``contentDocument`` on a foreign frame throws, and
    the walker's job is to catch it, skip that frame, and keep capturing the rest
    of the page — an exception that unwound the walk would take the MAIN frame's
    controls with it, which is how one embed loses a whole page.
    """
    from app.inventory_js import INVENTORY_JS

    assert "try { cdoc = ifr.contentDocument; } catch (e) { cdoc = null; }" in INVENTORY_JS, (
        "the walker no longer skips a foreign frame honestly — if it now reads "
        "contentDocument unguarded, one cross-origin embed throws away the whole "
        "page's capture")
    # And no injected snippet reaches for a way around it.
    for name in ("INVENTORY_JS", "OPAQUE_JS", "CAPTURE_HOOKS_JS"):
        source = H.production_snippet(name)
        for forbidden in ("document.domain", "postMessage", "contentWindow.eval"):
            assert forbidden not in source, (
                f"{name} contains {forbidden!r} — capture must not work AROUND "
                f"origin isolation; the port crosses it with Playwright's own "
                f"frame APIs instead")


def test_frame_entry_uses_playwright_frame_apis_and_injects_nothing_across() -> None:
    """Frame entry goes through supported APIs, and only through them."""
    from app.playwright_port import PlaywrightBrowserPort

    source = inspect.getsource(PlaywrightBrowserPort._enter_frame)
    assert "content_frame()" in source, (
        "frame entry no longer asks the browser for the frame's execution "
        "context — this is the supported crossing and the only one this "
        "milestone authorises")
    assert "frame.evaluate(INVENTORY_JS)" in source, (
        "the frame is entered but never observed with the PRODUCTION walker; a "
        "second capture recipe for frames would drift from the first")
    # add_init_script is a CONTEXT-level call. Doing it per-frame would be an
    # attempt to inject INTO a foreign origin, which is exactly what must not
    # happen — and would not work anyway.
    tree = ast.parse(textwrap.dedent(source))
    offenders = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("add_init_script", "add_script_tag", "expose_function")
    ]
    assert not offenders, (
        f"frame entry calls {offenders} — capture must not inject script into a "
        f"foreign frame")


def test_the_frame_ledger_records_entry_and_identity(pw, fixture_server) -> None:
    """Frame DISCOVERY and frame IDENTITY, as durable evidence.

    A crawl that read a payment frame and one that could not address it must not
    produce identical coverage — so the port keeps a row per frame met, entered
    or refused, and the row carries the frame's ORIGIN (never its URL: a vendor
    frame URL routinely carries a client secret in its query string).
    """
    from app.main import PlaywrightBrowserPort

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(fixture_server.url("04-iframe-cross-origin"))
        controls = await port.collect_controls()
        return controls, await port.collect_opaque(), await port.drain_frame_evidence()

    controls, opaque, frames = pw.run(_run())

    # DISCOVERY: the surface is still NAMED, and now carries what makes it
    # actionable — a deterministic selector.
    discovered = [s for s in opaque if s["kind"] == "cross_origin_iframe"]
    assert len(discovered) == 1, f"expected one cross_origin_iframe row, got {opaque}"
    assert discovered[0]["frame_selector"] == "iframe#card-entry"
    assert discovered[0]["frame_host"], "the row names no host"

    # IDENTITY + OUTCOME.
    entered = [f for f in frames if f["status"] == "entered"]
    assert len(entered) == 1, f"expected one entered frame, got {frames}"
    row = entered[0]
    assert row["selector"] == "iframe#card-entry"
    assert row["origin"].startswith("http://"), row
    assert "?" not in row["origin"] and row["origin"].count("/") == 2, (
        f"the frame identity carries more than an origin: {row['origin']!r} — a "
        f"vendor frame URL routinely carries a secret in its query string")
    assert row["controls"] == 3, row
    assert row["depth"] == 1

    # The ledger is DRAINED, like the network buffer: a second read must not
    # re-report the same frame onto the next state.
    assert pw.run(PlaywrightBrowserPort(pw.page, pw.context).drain_frame_evidence()) == []

    # And the frame's origin travels with each control read from inside it.
    card = next(c for c in controls if c["name"] == "Card Number")
    assert card["capture_scope"] == "cross_origin_frame"
    assert card["frame_origin"] == row["origin"]


def test_an_ambiguous_frame_is_refused_rather_than_guessed(pw, fixture_server) -> None:
    """A selector that resolves to several frames must NOT be entered.

    Binding to whichever frame came first and cataloguing its controls under the
    other frame's name is worse than reading nothing, because it reads as
    coverage. The refusal is recorded with the count, so it is legible.
    """
    from app.main import PlaywrightBrowserPort

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(fixture_server.url("24-iframe-ambiguous-attrs"))
        # Two frames share this title; the bare rung is deliberately ambiguous.
        got = await port._enter_frame(
            pw.page, 'iframe[title="payment \\"step\\" [1]"]',
            prefix="", depth=1, label="ambiguous")
        return got, await port.drain_frame_evidence()

    got, frames = pw.run(_run())
    assert got == [], "controls were captured through an ambiguous frame selector"
    refused = [f for f in frames if f["status"] == "not_entered"]
    assert refused and refused[-1]["resolved"] == 2, frames
    assert "not 1" in refused[-1]["reason"]


# ─── T-FR-02 · closed shadow roots ──────────────────────────────────────────

def test_a_closed_shadow_component_exposes_its_controls(pw, fixture_server) -> None:
    """THE T-FR-02 acceptance, on the fixture that used to assert the opposite."""
    controls = pw.collect_fresh(fixture_server.url("02-shadow-closed"))
    names = {c["name"] for c in controls}
    assert {"Coverage Amount", "Get Quote"} <= names, (
        f"the closed shadow root is still opaque: {sorted(names)}")
    # Same frame — a shadow root is not an iframe, and the compiler's
    # getByRole/getByLabel pierce shadow DOM.
    for c in controls:
        assert c["frame_selector"] == "", c
    # The name came from the SHADOW ROOT's own label[for]; resolving it against
    # the host document would have produced name:"" and dropped the control.
    coverage = next(c for c in controls if c["name"] == "Coverage Amount")
    assert coverage["name_source"] == "label-for"


def test_the_closed_shadow_surface_is_reported_as_observed_not_opaque(
        pw, fixture_server) -> None:
    """Coverage must not understate itself either.

    An opaque row for a surface we READ is as wrong as a clean scan over one we
    did not: both make the ledger disagree with the evidence beside it.
    """
    surfaces = pw.collect_fresh(fixture_server.url("02-shadow-closed"), what="opaque")
    kinds = {s["kind"] for s in surfaces}
    assert "closed_shadow_entered" in kinds, surfaces
    assert "closed_shadow" not in kinds, (
        f"a closed root we read is still reported as a blind spot: {surfaces}")
    row = next(s for s in surfaces if s["kind"] == "closed_shadow_entered")
    assert row["label"] == "closed-quote-widget"
    assert row["controls_observed"] == 2, row


def test_the_hooks_are_installed_at_context_creation_not_retrofitted() -> None:
    """The ORDERING is the capability, so it is asserted structurally.

    A hook installed after the page exists cannot work: the root it would need
    was created and captured before it arrived. This pins the call site — between
    ``new_context`` and the first page — because a refactor that moved it later
    would leave every closed shadow root uncatalogued while this file's browser
    tests still passed on a context the harness happened to configure correctly.
    """
    source = (_SERVICE_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    ctx_at = source.index("context = await browser.new_context(")
    hook_at = source.index("await install_capture_hooks(context)")
    page_at = source.index("await context.new_page()")
    assert ctx_at < hook_at < page_at, (
        "install_capture_hooks() is no longer called between new_context() and "
        "the first new_page(). Anywhere later is a retrofit, and a retrofit "
        "cannot observe a closed shadow root at all")


def test_a_retrofitted_hook_provably_cannot_see_a_closed_root(pw, fixture_server) -> None:
    """The negative control — why the placement above is not merely tidy.

    A context WITHOUT the init script sees nothing inside the closed root, and
    installing the hook afterwards does not recover it: the component has already
    run. This is the measurement that makes 'must be installed before the page'
    a fact rather than a claim, and it is the reason the fixture's assertions are
    not just an artefact of a well-configured harness.
    """
    from app.main import PlaywrightBrowserPort
    from app.playwright_port import context_defaults, install_capture_hooks

    async def _run():
        # No hooks at creation — deliberately NOT pw.fresh_context().
        ctx = await pw._browser.new_context(**context_defaults())
        ctx.set_default_timeout(15000)
        page = await ctx.new_page()
        port = PlaywrightBrowserPort(page, ctx)
        await port.goto(fixture_server.url("02-shadow-closed"))
        blind = {c["name"] for c in await port.collect_controls()}
        blind_opaque = await port.collect_opaque()
        # Retrofit: install now, re-read the SAME already-constructed page.
        await install_capture_hooks(ctx)
        retro = {c["name"] for c in await port.collect_controls()}
        await ctx.close()
        return blind, blind_opaque, retro

    blind, blind_opaque, retro = pw.run(_run())

    assert "ZIP Code" in blind, "the page was not observed at all"
    assert "Coverage Amount" not in blind, (
        "a context with no init script read inside a closed shadow root — then "
        "the hook is not what is doing the work, and this milestone's claim is "
        "not what it says it is")
    assert {s["kind"] for s in blind_opaque} == {"closed_shadow"}, blind_opaque
    assert "Coverage Amount" not in retro, (
        "installing the hook AFTER the component constructed itself recovered "
        "the root — which is impossible, so the test above is measuring "
        "something other than what it claims")


def test_the_hook_is_invisible_to_the_application(pw, fixture_server) -> None:
    """The page must not be able to tell.

    Capture that changes the application's own behaviour is not observation. The
    closed root must still read ``null`` from outside, and a framework that
    fingerprints ``attachShadow`` must see a native function.
    """
    async def _run():
        page = await (await pw.fresh_context()).new_page()
        await page.goto(fixture_server.url("02-shadow-closed"))
        return await page.evaluate("""() => ({
            closedRootStillHidden:
                document.getElementById("widget").shadowRoot === null,
            looksNative:
                Element.prototype.attachShadow.toString().indexOf("[native code]") >= 0,
            name: Element.prototype.attachShadow.name,
            enumerable: Object.keys(Element.prototype).indexOf("attachShadow") >= 0,
            hooksEnumerable: Object.keys(window).indexOf("__nxCaptureHooks") >= 0,
            openStillWorks: (() => {
                const el = document.createElement("div");
                return el.attachShadow({mode: "open"}) === el.shadowRoot;
            })(),
        })""")

    seen = pw.run(_run())
    assert seen["closedRootStillHidden"], (
        "the page can now read its own closed shadow root — capture changed the "
        "application's behaviour instead of observing it")
    assert seen["looksNative"] and seen["name"] == "attachShadow", seen
    assert not seen["enumerable"] and not seen["hooksEnumerable"], seen
    assert seen["openStillWorks"], "an OPEN shadow root no longer round-trips"


@pytest.mark.parametrize("page_name,expected", [
    ("01-shadow-open", {"Policy Number", "Security PIN", "Verify Identity"}),
    ("02-shadow-closed", {"ZIP Code", "Coverage Amount", "Get Quote"}),
    ("15-shadow-nested", {"Outer Account Number", "Inner Account Number",
                          "Deep Nested Field"}),
    ("14-capture-attributes", {"Email address"}),
])
def test_the_patch_does_not_break_ordinary_pages(
        pw, fixture_server, page_name, expected) -> None:
    """Open, closed, nested, and plain DOM — all four still read correctly.

    The hook runs on EVERY page of every crawl, so the risk it carries is not
    "does it work on the shadow fixture" but "does it quietly break the other
    99%". These four are the shapes it could plausibly disturb.
    """
    names = {c["name"] for c in pw.collect(fixture_server.url(page_name))}
    assert expected <= names, (
        f"{page_name}: {sorted(expected - names)} disappeared once the capture "
        f"hooks were installed")


# ─── T-FR-03 · selector correctness, asserted as consequence ────────────────

def test_ambiguous_and_shadow_nested_frame_selectors_bind_the_right_frame(
        pw, fixture_server) -> None:
    """THE T-FR-03 acceptance.

    Every emitted selector is handed back to the browser and must (a) resolve to
    exactly one frame and (b) find, inside that frame, the control that was
    captured through it. (b) is the half that matters: the defect class here is a
    selector that parses perfectly, resolves to exactly one frame, and names the
    wrong one — which is indistinguishable from coverage in every report.
    """
    controls = pw.collect_fresh(fixture_server.url("24-iframe-ambiguous-attrs"))
    spec = H.fixture_spec("24-iframe-ambiguous-attrs")["frame_selectors_must_resolve"]

    by_selector: dict[str, set[str]] = {}
    for c in controls:
        if c["frame_selector"]:
            by_selector.setdefault(c["frame_selector"], set()).add(c["css_hint"])
    assert len(by_selector) == spec["expect_distinct"], (
        f"expected {spec['expect_distinct']} distinct frame selectors, got "
        f"{sorted(by_selector)} — two frames sharing one selector means every "
        f"control in the second is recorded against the first")

    async def _check(sel: str, hints: set[str]) -> tuple[int, dict[str, int]]:
        resolved = await pw.page.locator(sel).count()
        scope = pw.page
        for segment in sel.split(" >>> "):
            scope = scope.frame_locator(segment)
        return resolved, {h: await scope.locator(h).count() for h in hints}

    broken = {}
    for sel, hints in sorted(by_selector.items()):
        try:
            resolved, bound = pw.run(_check(sel, hints))
        except Exception as exc:
            broken[sel] = f"{type(exc).__name__}: {str(exc)[:160]}"
            continue
        if resolved != 1:
            broken[sel] = f"resolved to {resolved} frames"
        elif any(n != 1 for n in bound.values()):
            broken[sel] = f"resolved to ONE frame, but the wrong one: {bound}"
    assert not broken, (
        "emitted frame_selectors that do not bind the control captured through "
        f"them: {broken}")

    # And the specific spellings the two defects produce.
    assert "div#shadow-host >> iframe >> nth=0" in by_selector, (
        f"the shadow-nested frame is not addressed through its host — a "
        f"page-global ordinal cannot name it: {sorted(by_selector)}")
    assert "iframe >> nth=2" in by_selector, (
        f"the light-DOM positional ordinal does not count the frame set "
        f"Playwright resolves against: {sorted(by_selector)}")
    # THE HOST HAS TO BE ADDRESSABLE TOO, and by id is the easy case — the one
    # every fixture happened to use until now.
    assert "aside >> iframe >> nth=0" in by_selector, (
        f"a shadow host with no id and a unique tag is not addressed by that "
        f"tag: {sorted(by_selector)}")
    assert "section >> nth=1 >> iframe >> nth=0" in by_selector, (
        f"a shadow host with no id and same-tag siblings is not addressed "
        f"positionally: {sorted(by_selector)}")


def test_special_characters_still_resolve_after_the_scoping_change(
        pw, fixture_server) -> None:
    """Fixture 16's escaping contract, re-asserted against the new recipe.

    T-FR-03 rewrote ``frameSelectorFor``; the escaping it inherited must survive
    that, or a fix for ambiguity would have reintroduced BUG-IFRAME-SELECTOR.
    """
    controls = pw.collect_fresh(fixture_server.url("16-iframe-special-chars"))
    selectors = {c["frame_selector"] for c in controls if c["frame_selector"]}
    assert len(selectors) == 4, sorted(selectors)
    for sel in selectors:
        assert pw.run(pw.page.locator(sel).count()) == 1, sel


# ─── T-FR-04 · the whole thing, on one page ─────────────────────────────────

def test_the_proving_ground_catalogues_both_surfaces(pw, fixture_server) -> None:
    """THE M3.2 STOP CONDITION.

    One page, both surfaces, ordinary DOM beside them. Every control on it
    becomes a catalogued record, the frame identity is retained, and nothing that
    was already captured moves.
    """
    name = "25-frame-shadow-proving-ground"
    controls = pw.collect_fresh(fixture_server.url(name))
    by_name = {c["name"]: c for c in controls}

    main_frame = {"Policy Holder Name", "Email Address", "Review Order"}
    closed_shadow = {"Signature Initials", "Accept Terms"}
    nested_in_closed = {"Disclosure Acknowledgement"}
    in_frame = {"Card Number", "Expiry (MM/YY)", "Billing Postcode", "Pay Now"}
    closed_shadow_in_frame = {"Security Code"}

    missing = (main_frame | closed_shadow | nested_in_closed | in_frame
               | closed_shadow_in_frame) - set(by_name)
    assert not missing, f"uncatalogued: {sorted(missing)}; captured {sorted(by_name)}"

    # PARENT / FRAME IDENTITY IS RETAINED — the whole page did not collapse into
    # one scope. Shadow roots are the same frame; the embed is not.
    for field in main_frame | closed_shadow | nested_in_closed:
        assert by_name[field]["frame_selector"] == "", by_name[field]
    for field in in_frame | closed_shadow_in_frame:
        assert by_name[field]["frame_selector"] == "iframe#payment-frame", by_name[field]
        assert by_name[field]["capture_scope"] == "cross_origin_frame"

    # The declared rules the app states about its own payment fields arrive too —
    # a catalogued question with no answer set is only half the evidence.
    assert by_name["Card Number"]["required"] is True
    assert by_name["Card Number"]["input_type"] == "text"
    assert by_name["Card Number"]["autocomplete"] == "cc-number"

    # THE CONTEXT-LEVEL INSTALL IS WHAT REACHES HERE. `Security Code` lives in a
    # closed shadow root INSIDE the foreign frame: a page-level hook could never
    # see it, and neither could anything injected from the parent origin.
    assert by_name["Security Code"]["name_source"] == "label-for"
    assert by_name["Security Code"]["shadow_scope"] == "closed_shadow", (
        "a control read through a closed shadow root does not say so — see "
        "test_a_closed_shadow_control_is_catalogued_but_declared_unbindable "
        "for why that flag is load-bearing")


def test_the_proving_ground_ledger_names_both_surfaces(pw, fixture_server) -> None:
    """Both surfaces are accounted for in the ledger, each as what it now is."""
    from app.main import PlaywrightBrowserPort

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(fixture_server.url("25-frame-shadow-proving-ground"))
        await port.collect_controls()
        return await port.collect_opaque(), await port.drain_frame_evidence()

    opaque, frames = pw.run(_run())
    kinds = {s["kind"] for s in opaque}
    assert "cross_origin_iframe" in kinds, opaque
    assert "closed_shadow_entered" in kinds, opaque
    assert "closed_shadow" not in kinds, (
        f"a closed root that was read is still reported as opaque: {opaque}")
    entered = [f for f in frames if f["status"] == "entered"]
    assert len(entered) == 1 and entered[0]["controls"] == 5, frames


def test_the_captured_payment_fields_are_actionable_not_merely_recorded(
        pw, fixture_server) -> None:
    """The stop condition's teeth: EVIDENCE, not a longer list.

    A catalogued control that no locator can bind is a claim, not evidence. The
    port's own ``_locator`` — the ladder every action goes through — is asked to
    build a locator for each field read inside the embed, and the browser is
    asked to confirm it hits exactly one element.
    """
    from app.main import PlaywrightBrowserPort
    from app.inventory import build_inventory

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        url = fixture_server.url("25-frame-shadow-proving-ground")
        await port.goto(url)
        records = build_inventory(await port.collect_controls(), None, url=url)
        out, flagged = {}, {}
        for rec in records:
            if rec["frame_selector"] != "iframe#payment-frame":
                continue
            # A control inside a CLOSED shadow root is excluded BY ITS OWN STAMP,
            # not by name: no selector engine reaches into a closed root, and the
            # test for that limit is right below this one. Excluding it silently
            # would be the thing this test exists to forbid.
            if rec["qec"]["shadow_scope"] == "closed_shadow":
                flagged[rec["name"]] = rec["qec"]["shadow_scope"]
                continue
            locator = port._locator(rec)
            out[rec["name"]] = await locator.count() if locator is not None else None
        return out, flagged

    bound, flagged = pw.run(_run())
    assert bound, "no control was captured inside the payment frame"
    unbindable = {k: v for k, v in bound.items() if v != 1}
    assert not unbindable, (
        f"catalogued payment fields that the production locator ladder cannot "
        f"bind: {unbindable}. A control the crawl claims and replay cannot reach "
        f"is exactly the green-wash this milestone exists to remove")
    assert set(bound) == {"Card Number", "Expiry (MM/YY)", "Billing Postcode",
                          "Pay Now"}, sorted(bound)
    # And the one that is NOT bindable was excluded because it says so.
    assert flagged == {"Security Code": "closed_shadow"}, flagged


def test_a_closed_shadow_control_is_catalogued_but_declared_unbindable(
        pw, fixture_server) -> None:
    """THE LIMIT OF T-FR-02, measured rather than assumed.

    The capture hook makes a closed shadow root OBSERVABLE. It does not, and
    cannot, make it BINDABLE: Playwright's selector engine pierces open shadow
    roots by reading ``element.shadowRoot``, which the spec keeps null for a
    closed one — for Playwright exactly as for the page. There is no supported
    selector that reaches inside, and manufacturing a crawler-only one would
    create the very asymmetry this harness exists to catch: a control the crawl
    can act on and a generated script cannot.

    So the control is catalogued — it is a real question the application asks,
    and leaving it out is the blind spot this milestone closes — and it is
    STAMPED, so nothing downstream can read "catalogued" as "replayable". This
    test measures both halves: the flag is set, and the binding really does fail.
    """
    from app.inventory import build_inventory
    from app.main import PlaywrightBrowserPort

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        url = fixture_server.url("02-shadow-closed")
        await port.goto(url)
        records = build_inventory(await port.collect_controls(), None, url=url)
        by_name = {r["name"]: r for r in records}
        counts = {}
        for name in ("ZIP Code", "Coverage Amount"):
            locator = port._locator(by_name[name])
            counts[name] = await locator.count() if locator is not None else None
        return by_name, counts

    by_name, counts = pw.run(_run())

    assert by_name["Coverage Amount"]["qec"]["shadow_scope"] == "closed_shadow", (
        "a control observed through a closed shadow root is catalogued with no "
        "mark on it — downstream cannot tell it apart from one a locator can "
        "reach, which is a capture-says-covered claim")
    assert by_name["ZIP Code"]["qec"]["shadow_scope"] == "", (
        "an ordinary light-DOM control was stamped as closed-shadow")

    # And the flag is TRUE, not decorative.
    assert counts["ZIP Code"] == 1
    assert counts["Coverage Amount"] in (0, None), (
        f"a standard locator now binds inside a closed shadow root "
        f"({counts['Coverage Amount']}) — if that has become possible the flag "
        f"is obsolete and this test should be replaced by one that binds it")


# ─── T-FR-04 · a REAL crawl, and what it leaves behind ──────────────────────

def test_a_real_crawl_of_the_proving_ground_catalogues_both_surfaces(
        pw, fixture_server, tmp_path) -> None:
    """THE MILESTONE'S STOP CONDITION, measured on a production crawl.

    Everything above reads the port directly. This drives the real
    :class:`app.crawler.Crawler` — real budget accounting, real fail-closed
    guard, real refuse pack, real manifest writer — and then reads the coverage
    ledger the crawl returns to qe-central. That is the artefact an operator
    actually sees, and it is where "opaque" either survives or does not.

    Three things have to be true at once, and each is a different failure if it
    is not:

      * the payment fields are in ``form_snapshot_signals`` — the shape
        qe-central builds catalogue questions from. Anywhere else and they are
        detected, not catalogued.
      * the frame appears as ``frame_entered`` with what was read from it, so a
        crawl that entered it is distinguishable from one that only named it.
      * the closed root appears as ``closed_shadow_entered`` rather than
        ``closed_shadow``, so coverage does not understate itself either.
    """
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(_SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    work_dir = tmp_path / "m32-crawl"
    work_dir.mkdir()

    async def _crawl():
        context = await pw.fresh_context()
        page = await context.new_page()
        crawler = Crawler(
            PlaywrightBrowserPort(page, context),
            crawl_id="m32-proving-ground", tenant_id="m32",
            target_url=fixture_server.url("25-frame-shadow-proving-ground"),
            work_dir=str(work_dir), refuse_pack=pack,
            budget=Budget.from_dict({"max_states": 1, "max_actions": 0,
                                     "max_requests": 200,
                                     "max_duration_ms": 120_000}),
            explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
            refuse_pack_version=pack.version, config_fingerprint="m32-fixed",
            guard_context=GuardContext(
                refuse_pack=pack,
                auth_window=AuthWindow(max_requests=50, window_ms=60_000),
                attestation=None, submit_flow_approved=False,
                idp_domains=frozenset()),
            identity_seed="qec-m32", observe_only=True,
        )
        try:
            return await crawler.run()
        finally:
            try:
                await context.close()
            except Exception:
                pass

    summary = pw.run(_crawl())
    assert summary.states == 1, summary
    coverage = summary.coverage

    # ── the ledger ──────────────────────────────────────────────────────────
    by_kind = {}
    for row in coverage["opaque_surfaces"]:
        by_kind.setdefault(row["kind"], []).append(row)

    assert "cross_origin_iframe" in by_kind, coverage["opaque_surfaces"]
    assert "frame_entered" in by_kind, (
        f"the crawl named the embed and never says whether it got in: "
        f"{coverage['opaque_surfaces']}")
    assert "5 control(s) catalogued" in by_kind["frame_entered"][0]["reason"], (
        by_kind["frame_entered"][0])
    assert "frame_not_entered" not in by_kind, by_kind
    assert "closed_shadow_entered" in by_kind, coverage["opaque_surfaces"]
    assert "closed_shadow" not in by_kind, (
        f"the closed root the crawl READ is still ledgered as a blind spot: "
        f"{coverage['opaque_surfaces']}")

    # ── the catalogue crossing ──────────────────────────────────────────────
    states = coverage["states"]
    assert len(states) == 1, states
    signals = states[0]["form_snapshot_signals"]

    for field in ("Card Number", "Expiry (MM/YY)", "Billing Postcode"):
        assert field in signals, (
            f"{field!r} never reached form_snapshot_signals — qe-central builds "
            f"catalogue questions from this, so a control that is not here was "
            f"detected, not catalogued. Present: {sorted(signals)}")
        assert signals[field]["locator"]["frame_selector"] == "iframe#payment-frame", (
            f"{field!r} lost the frame it lives in on the way to the catalogue: "
            f"{signals[field]['locator']}")

    for field in ("Signature Initials", "Disclosure Acknowledgement"):
        assert field in signals, (
            f"{field!r} (closed shadow root) never reached the catalogue: "
            f"{sorted(signals)}")

    # The DECLARED RULES survive the crossing too — a catalogued question with
    # no answer set is half the evidence.
    assert signals["Card Number"]["required"] is True
    assert signals["Card Number"]["maxlength"] == "19"
    assert signals["Expiry (MM/YY)"]["pattern"] == "[0-9]{2}/[0-9]{2}"

    # And the ordinary page is unchanged: 11 controls, not 3.
    assert states[0]["controls_total"] == 11, states[0]["controls_total"]
    assert {"Policy Holder Name", "Email Address"} <= set(signals)


# ─── Every rung, from the snippet that decides whether a frame is enterable ──

def test_every_frame_on_one_host_is_named_and_entered(pw, fixture_server) -> None:
    """``BUG-OPAQUE-FRAME-DEDUP`` — one host, four frames, four rows.

    ``OPAQUE_JS`` deduped its rows on ``kind|label``, and a cross-origin frame's
    label is its HOST. A checkout that embeds a card frame and a 3-D-Secure frame
    from the same vendor is two frames on one host, so the second was dropped
    before anything could enter it — named nowhere, and indistinguishable in the
    ledger from a page that only ever had one embed. Fixtures 04 and 25 embed a
    single frame each, so neither could see it.
    """
    from app.main import PlaywrightBrowserPort

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(fixture_server.url("26-opaque-surface-rungs"))
        controls = await port.collect_controls()
        return controls, await port.collect_opaque(), await port.drain_frame_evidence()

    controls, opaque, frames = pw.run(_run())

    named = [s for s in opaque if s["kind"] == "cross_origin_iframe"]
    assert len(named) == 5, (
        f"{len(named)} of 5 foreign embeds were named. They share one host, so a "
        f"host-keyed dedup silently collapses them: {named}")
    assert len({s["frame_selector"] for s in named}) == 5, named
    assert len({s["frame_host"] for s in named}) == 1, (
        "the fixture no longer puts every embed on ONE host, which is the whole "
        "point of it")

    entered = [f for f in frames if f["status"] == "entered"]
    assert len(entered) == 5, frames
    assert all(f["controls"] == 1 for f in entered), frames

    by_name = {c["name"]: c for c in controls}
    for step in ("Vendor Step A", "Vendor Step B", "Vendor Step C", "Vendor Step D",
                 "Vendor Step E"):
        assert step in by_name, sorted(by_name)
        assert by_name[step]["capture_scope"] == "cross_origin_frame"


def test_every_selector_rung_is_reached_from_the_opaque_snippet(
        pw, fixture_server) -> None:
    """The recipe is a chain, and only its first rung had ever run here.

    ``OPAQUE_JS`` decides whether a frame can be entered at all, so a rung that
    is never executed from it is a class of embed the crawl silently cannot
    reach. Each selector is also handed back to the browser and required to bind
    the control captured through it — a rung that fires and names the wrong frame
    is worse than one that never fires.
    """
    controls = pw.collect_fresh(fixture_server.url("26-opaque-surface-rungs"))
    by_selector: dict[str, set[str]] = {}
    for c in controls:
        if c["frame_selector"]:
            by_selector.setdefault(c["frame_selector"], set()).add(c["css_hint"])
    assert len(by_selector) == 5, sorted(by_selector)

    rungs = {
        "title, disambiguated": lambda s: s.startswith('iframe[title="vendor step"] >> nth='),
        "src": lambda s: s.startswith('iframe[src='),
        "host by ordinal": lambda s: s.startswith("div >> nth=1 >> iframe"),
        "host by unique tag": lambda s: s.startswith("aside >> iframe"),
    }
    unreached = [name for name, match in rungs.items()
                 if not any(match(s) for s in by_selector)]
    assert not unreached, f"rungs never reached from OPAQUE_JS: {unreached}; " \
                          f"emitted {sorted(by_selector)}"

    async def _check(sel: str, hints: set[str]) -> tuple[int, dict[str, int]]:
        resolved = await pw.page.locator(sel).count()
        scope = pw.page
        for segment in sel.split(" >>> "):
            scope = scope.frame_locator(segment)
        return resolved, {h: await scope.locator(h).count() for h in hints}

    broken = {}
    for sel, hints in sorted(by_selector.items()):
        resolved, bound = pw.run(_check(sel, hints))
        if resolved != 1:
            broken[sel] = f"resolved to {resolved} frames"
        elif any(n != 1 for n in bound.values()):
            broken[sel] = f"resolved to ONE frame, but the wrong one: {bound}"
    assert not broken, broken


def test_a_declarative_closed_shadow_root_stays_an_honest_blind_spot(
        pw, fixture_server) -> None:
    """THE STATED LIMIT OF T-FR-02, measured.

    ``<template shadowrootmode="closed">`` is attached by the HTML parser and
    never calls ``Element.prototype.attachShadow``, so wrapping that method
    cannot observe it. The docstring on ``CAPTURE_HOOKS_JS`` says so; this is
    what makes it a fact. The surface must stay a NAMED blind spot and capture
    nothing from inside it — if that ever changes, the limitation should be
    rewritten deliberately rather than discovered by someone trusting the prose.
    """
    url = fixture_server.url("26-opaque-surface-rungs")
    names = {c["name"] for c in pw.collect_fresh(url)}
    assert not ({"Legacy Acknowledgement", "Acknowledge"} & names), (
        f"a declarative closed shadow root was captured: {sorted(names)}. That "
        f"is not possible through the attachShadow hook, so either the hook has "
        f"grown a second mechanism or something is fabricating controls")

    surfaces = pw.collect_fresh(url, what="opaque")
    blind = [s for s in surfaces if s["kind"] == "closed_shadow"]
    assert len(blind) == 1 and blind[0]["label"] == "legacy-disclosure-panel", (
        f"the unobservable surface is not named as a blind spot: {surfaces}")
