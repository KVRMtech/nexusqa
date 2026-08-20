"""M3.1 / T-VIS-06 — THE CANVAS PROVING GROUND, CRAWLED FOR REAL.

    canvas/WebGL control -> vision -> coordinate action -> R0 -> catalog

Every step below is real.  Real Chromium, the production
:class:`app.playwright_port.PlaywrightBrowserPort`, the production
:class:`app.crawler.Crawler`, the production capture JavaScript, the production
PII redaction, the production R0.  The assertions read the manifest and the
coverage payload the production emitter wrote to disk.

WHAT IS STUBBED, STATED PLAINLY
===============================
ONE thing: the multimodal model call.  A network LLM is not a function, so a
test that called one would measure the provider rather than this engine — the
same reason the characterization harness stubs its oracles.

In its place is :class:`PixelPerceiver`, which performs a REAL perception:
given the (already redacted) screenshot bytes and nothing else, it scans for
blocks of the fixture's button colour and returns their centres.  It never sees
the DOM, never sees the fixture source, and is handed no coordinates.  The
coordinates the crawl then clicks are therefore derived from pixels, which is
what makes this a proving ground rather than a rehearsal.

Two things it does that a model would also do, one of them on purpose:

  * it LABELS what it found from a small legend keyed by vertical order.  Real
    OCR is out of scope; this stands in for the model's reading of the text and
    is the only place a coordinate meets a name.
  * it HALLUCINATES one control — "Social Security Number", at a coordinate on
    the decorative crest where nothing is interactive.  This is the negative
    case the milestone requires, and it is chosen to be the worst realistic one:
    a perceived PII field that would have entered the catalogue as a question
    the application never asks.
"""
from __future__ import annotations

import json
import shutil
from io import BytesIO
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

pytest.importorskip("PIL", reason="Pillow is required for pixel perception")

CRAWL_OUT = H.HERE / "_crawl_out"

#: The fixture paints every operable control in this colour, and paints the
#: inert crest in another.  The perceiver knows the colour, not the geometry.
BUTTON_RGB = (0x1a, 0x4f, 0xd6)

#: The stand-in for OCR — see the module docstring.  Keyed by vertical order of
#: the blocks the perceiver finds, top first.
LEGEND = [("Annual Income", "textbox"), ("Recalculate", "button")]

#: The deliberately WRONG perception: a PII question at an inert coordinate.
HALLUCINATION = {"label": "Social Security Number", "role": "textbox",
                 "bbox": [700, 120, 120, 120], "click_x": 760, "click_y": 180}


# ── the perceiver: pixels in, candidate controls out ────────────────────────

def _find_button_blocks(png: bytes) -> list[dict[str, Any]]:
    """Locate solid blocks of ``BUTTON_RGB`` in a PNG.  Pixels only.

    Deliberately crude — a coarse grid scan and a bounding box per connected
    run of hits.  It does not need to be a good vision system; it needs to be a
    REAL one, so that the coordinates it emits were derived from the image the
    crawl actually captured and from nothing else.
    """
    from PIL import Image

    img = Image.open(BytesIO(png)).convert("RGB")
    w, h = img.size
    step = 4
    hits: list[tuple[int, int]] = []
    for y in range(0, h, step):
        for x in range(0, w, step):
            px = img.getpixel((x, y))
            if all(abs(px[i] - BUTTON_RGB[i]) <= 8 for i in range(3)):
                hits.append((x, y))
    if not hits:
        return []
    # Group by contiguous rows — the fixture's controls are horizontally
    # separated bands, which is all this needs to resolve.
    bands: list[list[tuple[int, int]]] = []
    for x, y in sorted(hits, key=lambda p: (p[1], p[0])):
        if bands and y - bands[-1][-1][1] <= step * 3:
            bands[-1].append((x, y))
        else:
            bands.append([(x, y)])
    out = []
    for band in bands:
        xs = [p[0] for p in band]
        ys = [p[1] for p in band]
        bx, by = min(xs), min(ys)
        bw, bh = max(xs) - bx, max(ys) - by
        if bw < 40 or bh < 20:
            continue                        # noise, not a control
        out.append({"bbox": [bx, by, bw, bh],
                    "click_x": bx + bw // 2, "click_y": by + bh // 2})
    return out


class PixelPerceiver:
    """The vision oracle seam, satisfied by real pixel analysis + one lie."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        #: The EXACT payload each call was handed, kept so a test can re-derive
        #: the redaction digest from the bytes rather than from the claim.
        self.received: list[str] = []

    async def perceive(self, screenshot_b64: str, page_context: dict) -> dict:
        import base64

        png = base64.b64decode(screenshot_b64)
        self.received.append(screenshot_b64)
        self.calls.append({"bytes": len(png), "ctx": dict(page_context)})
        blocks = _find_button_blocks(png)
        controls = []
        for i, block in enumerate(blocks[:len(LEGEND)]):
            label, role = LEGEND[i]
            controls.append({**block, "label": label, "role": role})
        controls.append(dict(HALLUCINATION))
        return {"controls": controls,
                "displayed_values": [{"label": "Runs", "text": "0"}]}


# ── the crawl ───────────────────────────────────────────────────────────────

def _open_budget():
    from app import vision_gate

    gate = vision_gate.decide_gate(attested=True, tenant_enabled=True,
                                   rung=vision_gate.RUNG_SIGNED_PROOF)
    return vision_gate.VisionBudget(gate=gate, max_calls=6, timeout_s=20.0,
                                    breaker_threshold=3)


@pytest.fixture(scope="module")
def canvas_crawl(pw, fixture_server):
    """ONE real crawl of fixture 23, shared by every assertion below."""
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=50, window_ms=60_000),
        attestation=None, submit_flow_approved=False, idp_domains=frozenset())

    work_dir = CRAWL_OUT / "vis06-canvas"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    perceiver = PixelPerceiver()
    budget = _open_budget()
    crawl_id = "vis06-canvas"
    url = fixture_server.origin + "/23-canvas-app/index.html"

    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id="vis06", target_url=url,
        work_dir=str(work_dir), refuse_pack=pack,
        budget=Budget(max_states=4, max_depth=1, max_actions_per_state=6,
                      max_requests=60, rate_per_s=0),
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version, config_fingerprint="vis06",
        guard_context=guard_ctx, identity_seed="vis06",
        vision_oracle=perceiver.perceive, vision_budget=budget,
        # THREE, so the hallucination is actually CLICKED and genuinely fails
        # R0. At the default of two it would have been refused for running out
        # of action budget — a true refusal, but the wrong one: it would prove
        # the bound rather than the law.
        vision_max_actions_per_state=3,
    )
    summary = pw.run(crawler.run())
    # GROUND TRUTH — what the APPLICATION itself believes happened, read out of
    # its own state rather than out of the crawl's report of it. Nothing the
    # crawler reads consults this; it exists so "R0 said verified" can be checked
    # against "the app really did react".
    ground_truth = pw.run(pw.page.evaluate(
        "window.__canvasApp ? {clicks: window.__canvasApp.clicks.length,"
        " runs: window.__canvasApp.runs, focus: window.__canvasApp.focus} : {}"
    )) or {}

    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), "the crawl wrote no manifest"
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    # COVERAGE is not a manifest record — it rides on the CrawlSummary and is
    # POSTed to qe-central by the completion callback. Read from the summary, so
    # the assertions below are about the payload that actually crosses the
    # service boundary rather than about a local reconstruction of it.
    coverage = summary.coverage or {}
    assert coverage, "the crawl produced no coverage payload"
    # ARCHIVE THE EVIDENCE. `_crawl_out` is the directory CI archives, and this
    # is the milestone's primary artifact: the vision call trace, the gate, the
    # budget, both halves of the ledger and the catalogue payload the verified
    # control crossed on. It is also the INPUT to qe-central's half of the
    # proof — the two services cannot import each other, so the payload is
    # carried as data (the same device `contracts/m22_catalog_question_v1.json`
    # exists for).
    evidence = {
        "crawl_id": crawl_id,
        "target_url": url,
        "stop_reason": summary.stop_reason,
        "states": summary.states,
        "vision_gate": budget.gate.as_dict(),
        "vision_budget": budget.telemetry(),
        "vision_ledger": coverage.get("vision_ledger"),
        "vision_verified": coverage.get("vision_verified"),
        "vision_refused": coverage.get("vision_refused"),
        "opaque_surfaces": coverage.get("opaque_surfaces"),
        "states_index": coverage.get("states"),
        "perceive_calls": [
            {"bytes": c["bytes"], "pixel_redaction": c["ctx"].get("pixel_redaction")}
            for c in perceiver.calls
        ],
    }
    (work_dir / "vision_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "records": records,
        "summary": summary,
        "perceiver": perceiver,
        "budget": budget,
        "crawler": crawler,
        "coverage": coverage,
        "crawl_id": crawl_id,
        "work_dir": work_dir,
        "url": url,
        "evidence_path": work_dir / "vision_evidence.json",
        "ground_truth": ground_truth,
    }


def _vision_ledger(coverage) -> list[dict]:
    return list(coverage.get("vision_ledger") or [])


def _attempts(coverage) -> dict[str, dict]:
    out = {}
    for row in _vision_ledger(coverage):
        for a in row.get("attempts") or []:
            out[a.get("label", "")] = a
    return out


# ── 1. DOM capture cannot describe this target ──────────────────────────────

def test_the_dom_cannot_describe_this_application(pw, fixture_server):
    """The precondition for the whole milestone, measured rather than asserted.

    The production capture JavaScript reads this page and finds no interactive
    control, while the opaque detector names the canvas.  If this ever stops
    being true the fixture has drifted and every proof below is about a
    different application.
    """
    from app.perception import should_perceive

    url = fixture_server.origin + "/23-canvas-app/index.html"
    controls = pw.collect_fresh(url, what="controls")
    opaque = pw.run(H.collect_via_production_port(
        pw.page, pw.context, url, what="opaque"))

    assert controls == [] or all(
        (c.get("qec") or {}).get("name_confidence") not in ("high", "medium")
        for c in controls), (
        "fixture 23 must have no well-named DOM control before a coordinate "
        f"action earns one; got {controls!r}")
    assert any(s.get("kind") == "canvas" for s in opaque), (
        f"the canvas was not detected as an opaque surface: {opaque!r}")
    assert should_perceive(controls, opaque) is True


# ── 2. the escalation actually ran, under the gate and the budget ───────────

def test_should_perceive_activated_and_a_vision_call_was_made(canvas_crawl):
    assert canvas_crawl["perceiver"].calls, (
        "the crawl never escalated to vision on a page the DOM cannot read")
    assert canvas_crawl["budget"].calls >= 1


def test_the_feature_gate_and_the_budget_are_reported_as_evidence(canvas_crawl):
    """T-VIS-04 + T-VIS-03 evidence, carried in the crawl's own payload."""
    tel = canvas_crawl["coverage"]["vision_budget"]
    assert tel["gate"]["enabled"] is True
    assert tel["gate"]["attested"] is True
    assert tel["gate"]["tenant_enabled"] is True
    assert tel["gate"]["attestation_rung"] == "signed_provisioning_proof"
    assert tel["max_calls"] == 6 and tel["breaker_open"] is False
    assert tel["calls"] >= 1


# ── 3. the screenshot that left was REDACTED ────────────────────────────────

def test_the_image_handed_to_the_model_carried_a_matching_redaction_receipt(canvas_crawl):
    """T-VIS-05 on the live path: the receipt is bound to the bytes SENT.

    Re-derived from the payload the oracle actually received, through the same
    verifier qe-central runs — so one implementation is exercised at both ends
    of a wire the two services cannot share code across.
    """
    from app.pixel_redaction import verify_receipt

    perceiver = canvas_crawl["perceiver"]
    receipt = perceiver.calls[0]["ctx"]["pixel_redaction"]
    assert receipt["applied"] is True
    assert receipt["method"] == "dom-region-blackout-v1"
    assert len(receipt["image_sha256"]) == 64
    ok, why = verify_receipt(receipt, perceiver.received[0])
    assert ok is True, why


def test_the_visible_PII_was_MASKED_in_the_bytes_that_left(canvas_crawl, pw,
                                                           fixture_server):
    """T-VIS-05's acceptance, on the live path and read from the PIXELS.

    Fixture 23 prints an applicant's name, SSN, date of birth, account number
    and email onto the page as ordinary text. No input holds them, so no text
    scan of the prompt could ever see them — they travel in the image.

    The region the browser reports for that strip is compared against the image
    the perceiver actually received, and every sampled pixel inside it must be
    the mask colour. Asserting the receipt's ``regions`` count alone would be
    the class of check this whole milestone exists to reject.
    """
    import base64

    from PIL import Image

    from app.playwright_port import PlaywrightBrowserPort

    perceiver = canvas_crawl["perceiver"]
    receipt = perceiver.calls[0]["ctx"]["pixel_redaction"]
    assert receipt["regions"] >= 1, (
        "the page renders four distinct identifiers and the redaction pass "
        f"found nothing to mask: {receipt}")

    # Where the browser says that strip is, asked independently of the crawl.
    async def _strip_box():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(canvas_crawl["url"])
        return await pw.page.evaluate(
            "(() => { const r = document.getElementById('case-strip')"
            ".getBoundingClientRect();"
            " return {x: r.left + scrollX, y: r.top + scrollY,"
            "         w: r.width, h: r.height}; })()")

    box = pw.run(_strip_box())
    img = Image.open(BytesIO(base64.b64decode(perceiver.received[0]))).convert("RGB")
    scale = img.size[0] / float(receipt["page_w"] or img.size[0])

    samples = []
    for fx in (0.1, 0.3, 0.5, 0.7, 0.9):
        for fy in (0.3, 0.6):
            x = int((box["x"] + box["w"] * fx) * scale)
            y = int((box["y"] + box["h"] * fy) * scale)
            if 0 <= x < img.size[0] and 0 <= y < img.size[1]:
                samples.append(img.getpixel((x, y)))
    assert samples, "the PII strip fell outside the captured image"
    assert all(px == (0, 0, 0) for px in samples), (
        "an unmasked pixel of a rendered SSN / account number reached the "
        f"payload sent to the model: {samples}")


def test_the_live_receipt_cannot_be_reused_for_a_different_image(canvas_crawl):
    """The bytes and the claim travel together, so neither can be swapped.

    THE BYPASS THIS BINDING EXISTS FOR: redact one screenshot, keep its
    receipt, then send the unmasked original beside it. Exercised against the
    receipt this live crawl really produced, so the binding is proven on the
    path that carries it rather than only in a unit.
    """
    import base64

    from app.pixel_redaction import verify_receipt

    perceiver = canvas_crawl["perceiver"]
    receipt = perceiver.calls[0]["ctx"]["pixel_redaction"]
    assert verify_receipt(receipt, perceiver.received[0])[0] is True

    tampered = base64.b64encode(
        base64.b64decode(perceiver.received[0]) + b"\x00").decode("ascii")
    ok, why = verify_receipt(receipt, tampered)
    assert ok is False and "does not match" in why


# ── 4. THE CHAIN: coordinate action -> R0 -> catalog ────────────────────────

def test_a_perceived_control_became_a_coordinate_action(canvas_crawl):
    """The crawl clicked where the PIXELS said, and the application agrees."""
    attempts = _attempts(canvas_crawl["coverage"])
    assert "Recalculate" in attempts, f"perceived: {sorted(attempts)}"
    assert attempts["Recalculate"]["click_x"] is not None
    assert attempts["Recalculate"]["click_y"] is not None


def test_R0_verified_the_canvas_button_on_the_DOM_rung(canvas_crawl):
    a = _attempts(canvas_crawl["coverage"])["Recalculate"]
    assert a["status"] == "verified", a
    assert a["r0_rung"] == "dom", (
        "clicking Recalculate appends a real <button>, so the strongest rung "
        "must be the one that fires")


def test_R0_verified_the_canvas_TEXT_FIELD_on_the_PIXEL_rung(canvas_crawl):
    """The rung that makes a WebGL control verifiable at all.

    Focusing the canvas text field changes no element, no attribute and no URL —
    only pixels. Under rung 1 alone it could never be proven, and a canvas
    application would be permanently uncatalogueable.
    """
    a = _attempts(canvas_crawl["coverage"])["Annual Income"]
    assert a["status"] == "verified", a
    assert a["r0_rung"] == "pixel_stable_surface", a


def test_the_verified_control_entered_THE_CATALOGUE_PAYLOAD(canvas_crawl):
    """The end of the chain.

    ``coverage.states[].form_snapshot_signals`` is the ONE payload qe-central's
    ``catalog.extract_controls`` reads a question from (frozen in
    ``contracts/m22_catalog_question_v1.json``). A canvas-rendered text field
    that was clicked at a perceived coordinate and measured responding is
    present in it, carrying its provenance.
    """
    states = canvas_crawl["coverage"].get("states") or []
    signals: dict = {}
    for st in states:
        signals.update(st.get("form_snapshot_signals") or {})
    assert "Annual Income" in signals, (
        "a vision-verified canvas control did not reach the payload the "
        f"catalogue is built from; got {sorted(signals)}")
    assert signals["Annual Income"]["type"] == "text"


def test_the_UNVERIFIED_perception_entered_NOTHING(canvas_crawl):
    """THE LAW, stated as the assertion it has to be.

    "Social Security Number" was perceived with a coordinate, the crawl clicked
    exactly there, and the application did nothing. It must be absent from the
    catalogue payload, absent from the recorded control inventory, absent from
    every journey step — and PRESENT, as refused, in the vision ledger.
    """
    cov = canvas_crawl["coverage"]
    attempts = _attempts(cov)
    assert attempts["Social Security Number"]["status"].startswith("refused"), attempts

    # (a) ABSENT from the payload the catalogue is built from.
    for st in cov.get("states") or []:
        assert "Social Security Number" not in (st.get("form_snapshot_signals") or {})
        for group in st.get("question_groups") or []:
            assert group.get("label") != "Social Security Number"

    # (b) ABSENT from the recorded page states — so it is not a control, not a
    #     locator, and not a step any generated journey could bind to.
    for rec in canvas_crawl["records"]:
        if rec.get("type") != "page_state":
            continue
        assert "Social Security Number" not in (rec.get("form_snapshot_signals") or {})
        assert "Social Security Number" not in (rec.get("form_snapshot") or {})
        assert "Social Security Number" not in json.dumps(rec.get("actions") or [])

    # (c) ABSENT from every recorded flow / journey step.
    assert "Social Security Number" not in json.dumps(cov.get("flows") or [])

    # (d) …and PRESENT in the vision ledger, as refused, with a reason. A wrong
    #     perception that leaves no trace is indistinguishable from one that
    #     never happened, and the difference is the only way an operator learns
    #     to distrust a model.
    row = attempts["Social Security Number"]
    assert row["reason"], "a refusal with no stated reason is not evidence"
    assert row["click_x"] is not None, (
        "the ledger must record WHERE the crawl clicked, or the refusal cannot "
        "be re-checked")
    # (e) …and it was refused for the RIGHT reason: the crawl really clicked the
    #     perceived coordinate and the application really did nothing. A refusal
    #     for running out of action budget would prove the bound, not the law.
    assert "neither the DOM nor the pixels changed" in row["reason"], row


def test_the_applications_OWN_state_agrees_with_the_R0_verdicts(canvas_crawl):
    """Ground truth, read out of the application rather than out of the crawl.

    Three coordinate clicks were delivered; exactly ONE of them was on the
    control whose effect is a recalculation, and the inert crest changed
    nothing. If R0's verdicts and the application's own state ever disagreed,
    the R0 rungs would be measuring something other than what happened.
    """
    truth = canvas_crawl["ground_truth"]
    assert truth.get("clicks") >= 3, (
        f"the canvas did not receive every perceived coordinate: {truth}")
    assert truth.get("runs") == 1, (
        f"exactly one Recalculate should have been actuated: {truth}")


def test_the_ledger_counts_both_halves(canvas_crawl):
    cov = canvas_crawl["coverage"]
    assert cov["vision_verified"] >= 2
    assert cov["vision_refused"] >= 1


# ── 5. state identity distinguishes the canvas screens ─────────────────────

def test_the_canvas_screens_are_perceptually_distinct(pw, fixture_server):
    """T-VIS-02 on the live fixture.

    Focusing the field and running a recalculation are different screens with an
    IDENTICAL interactive DOM. Their perceptual hashes must differ, and a screen
    re-observed without interaction must hash the same — distinctness is earned,
    never manufactured.
    """
    from app.perception import perceptual_hash_png
    from app.playwright_port import PlaywrightBrowserPort

    url = fixture_server.origin + "/23-canvas-app/index.html"

    async def _run():
        port = PlaywrightBrowserPort(pw.page, pw.context)
        await port.goto(url)
        rects = await pw.page.evaluate("window.__canvasApp.rects")
        # The canvas's own PAGE origin, asked for rather than assumed: the
        # fixture has chrome above it and a hardcoded offset would silently
        # aim every click at empty space the moment that chrome changes.
        origin = await pw.page.evaluate(
            "(() => { const r = document.getElementById('app')"
            ".getBoundingClientRect();"
            " return {x: r.left + scrollX, y: r.top + scrollY}; })()")

        def page_point(rect):
            return (int(origin["x"] + rect["x"] + rect["w"] / 2),
                    int(origin["y"] + rect["y"] + rect["h"] / 2))

        entry = perceptual_hash_png(await port.screenshot_png())
        again = perceptual_hash_png(await port.screenshot_png())
        await port.click_at(*page_point(rects["income"]))
        focused = perceptual_hash_png(await port.screenshot_png())
        for _ in range(3):
            await port.click_at(*page_point(rects["recalc"]))
        recalculated = perceptual_hash_png(await port.screenshot_png())
        return entry, again, focused, recalculated

    entry, again, focused, recalculated = pw.run(_run())
    assert entry and entry == again, (
        "a re-observed canvas with no interaction changed its own hash — that "
        "would fragment every canvas state into an infinite frontier")
    assert len({entry, focused, recalculated}) == 3, (
        f"visually distinct canvas screens collapsed: "
        f"{entry!r} {focused!r} {recalculated!r}")
