"""A28 — the R5 vision medic rung, and the coordinate arithmetic that is its
whole risk surface.

WHAT A28 FIXED
==============
``/internal/vision-operate`` was live, authenticated, flag-gated, tested on the
server side — and had NO caller in this engine. The interaction ladder's last
rung was the TEXT medic (``/internal/operate-control``); when that failed, a
DOM-opaque control became named residue and the endpoint was never reached.

These tests pin the rung that closes it, and they are deliberately weighted
towards the ways it can be wrong QUIETLY rather than towards the happy path. A
mis-aimed click does not raise: it lands on nothing, R0 honestly reports
``intent_met=False``, and the whole thing reads as "the model hallucinated".
M3.1 lost a milestone to exactly that, so the coordinate conversion is pinned
here in its own right.
"""
from __future__ import annotations

import pytest

from app.playwright_port import PlaywrightBrowserPort


class _Locator:
    def __init__(self, box):
        self._box = box

    async def bounding_box(self):
        return self._box


class _Page:
    """Just enough page for the rung: title, scroll offsets, mouse clicks."""

    def __init__(self, *, scroll=(0, 0), viewport=(1280, 720)):
        self.sx, self.sy = scroll
        self.iw, self.ih = viewport
        self.clicks: list[tuple[int, int]] = []
        self.url = "https://app.example/quote"
        self.mouse = self

    async def title(self):
        return "Quote"

    async def click(self, x, y):
        self.clicks.append((x, y))

    async def evaluate(self, script, arg=None):
        if "scrollX" in script and "innerWidth" in script:
            return {"sx": self.sx, "sy": self.sy, "iw": self.iw, "ih": self.ih}
        if "scrollX" in script:
            return {"sx": self.sx, "sy": self.sy}
        if "scrollTo" in script:
            return None
        return {}


def _port(page, oracle, *, box):
    """A port with the browser bits stubbed and the rung's real code intact.

    Only the things the rung READS from the outside world are replaced — the
    locator, the screenshot, the PII probe, and the observe helpers ``click_at``
    depends on. The coordinate arithmetic, the bbox guard, the status handling
    and the redaction refusal are all the production code paths.
    """
    p = PlaywrightBrowserPort.__new__(PlaywrightBrowserPort)
    p._active_page = page
    p._context = None
    p._vision_medic_oracle = oracle
    p._medic_oracle = None
    p._artifact_dir = ""

    class _Reg:
        def active(self_inner):
            return page
    p._registry = _Reg()
    p._locator = lambda control: _Locator(box)

    async def _shot():
        class _S:
            page_w, page_h = 1280, 3000

            def b64(self):
                return "UE5H"

            def receipt(self):
                return {"method": "boxes", "sha256": "deadbeef", "regions_masked": 2}
        return _S()
    p._redacted_screenshot = _shot
    p._set_action_context = lambda *a, **k: None
    p._safe_url = lambda: page.url

    async def _sig():
        return "sig"
    p._interactive_signature = _sig

    async def _settle():
        return None
    p._settle = _settle

    async def _errs():
        return []
    p.error_texts = _errs

    async def _dialogs():
        return []
    p.dialog_flags = _dialogs
    return p


def _oracle(reply, seen: list):
    async def call(**kwargs):
        seen.append(kwargs)
        return reply
    return call


CONTROL = {"name": "Coverage slider", "role": "slider", "selector": "#cvg"}
BOX = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 40.0}


# ══════════════════════════════════════════════════════════════════════════
# THE POINT OF THE MILESTONE — the endpoint is actually reached
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_rung_calls_the_oracle_and_clicks_where_it_says():
    seen: list = []
    page = _Page()
    port = _port(page, _oracle(
        {"status": "proposed", "action": "click", "click_x": 30, "click_y": 20},
        seen), box=BOX)

    obs = await port._vision_medic_rung(CONTROL, "slider", [{"rung": "fill"}])

    assert seen, "the vision medic oracle was never called — A28's whole point"
    assert obs is not None and obs.mechanic_used == "vision_medic:click"
    # bbox origin (100,200) + medic offset (30,20), no scroll ⇒ (130,220)
    assert page.clicks == [(130, 220)], (
        f"the click did not land at the medic's point: {page.clicks}")


@pytest.mark.asyncio
async def test_the_oracle_receives_the_redaction_receipt_as_a_field():
    """qe-central ENFORCES the receipt against the bytes it was handed.

    Sending it buried inside the free-form page context would make it a
    checkbox; as its own field it is a claim bound to the image.
    """
    seen: list = []
    port = _port(_Page(), _oracle({"status": "unavailable"}, seen), box=BOX)
    await port._vision_medic_rung(CONTROL, "slider", [])
    assert seen[0]["redaction"] == {"method": "boxes", "sha256": "deadbeef",
                                    "regions_masked": 2}
    assert seen[0]["screenshot_b64"] == "UE5H"
    assert seen[0]["bbox"]["width"] == 300.0


# ══════════════════════════════════════════════════════════════════════════
# THE COORDINATE SPACES — three of them, and M3.1 already lost to two
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_scrolled_page_still_clicks_the_right_pixel():
    """THE M3.1 DEFECT CLASS, pinned.

    ``bounding_box()`` is VIEWPORT-relative; ``click_at`` takes PAGE
    coordinates. On a scrolled page, forgetting to add the scroll offset aims
    every vision click short by exactly that offset — silently.

    Here the page is scrolled 500px down. The element's viewport box is y=200,
    so its PAGE y is 700; the medic's +20 makes the page point 720. click_at
    then converts back to the viewport for the actual mouse click, giving 220.
    Both numbers being right is what proves the round trip is consistent.
    """
    seen: list = []
    page = _Page(scroll=(0, 500))
    port = _port(page, _oracle(
        {"status": "proposed", "action": "click", "click_x": 30, "click_y": 20},
        seen), box=BOX)

    await port._vision_medic_rung(CONTROL, "slider", [])

    assert seen[0]["bbox"]["y"] == 700.0, (
        "the bbox sent to the medic was not converted to PAGE space, so the "
        "model was shown a full-page screenshot and told the wrong offset")
    assert page.clicks == [(130, 220)], (
        f"scrolled-page click landed at {page.clicks}, expected [(130, 220)]")


@pytest.mark.asyncio
async def test_a_point_outside_the_element_is_refused():
    """A mis-attributed actuation is worse than no actuation.

    The medic was asked where INSIDE this control to click. An offset beyond the
    box either is a hallucination or is in the wrong coordinate space; clicking
    it actuates some OTHER control while recording the result against this one.
    """
    seen: list = []
    page = _Page()
    port = _port(page, _oracle(
        {"status": "proposed", "action": "click", "click_x": 900, "click_y": 20},
        seen), box=BOX)

    obs = await port._vision_medic_rung(CONTROL, "slider", [])

    assert obs is None, "an out-of-bbox point was accepted"
    assert page.clicks == [], "a click was executed outside the control's box"


# ══════════════════════════════════════════════════════════════════════════
# DECLINING IS NOT FAILING — every path the ladder already handles
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unwired_oracle_declines_silently():
    page = _Page()
    port = _port(page, None, box=BOX)
    assert await port._vision_medic_rung(CONTROL, "slider", []) is None
    assert page.clicks == []


@pytest.mark.asyncio
async def test_no_bbox_means_no_call():
    """Without a box the medic's answer has no origin to be relative to."""
    seen: list = []
    page = _Page()
    port = _port(page, _oracle({"status": "proposed", "click_x": 1,
                                "click_y": 1, "action": "click"}, seen),
                 box=None)
    assert await port._vision_medic_rung(CONTROL, "slider", []) is None
    assert not seen, "the oracle was billed for a control with no bounding box"


@pytest.mark.asyncio
async def test_an_unmaskable_screenshot_is_never_sent():
    """T-VIS-05: a page whose sensitive pixels cannot be located is not
    photographed and sent. The refusal must happen BEFORE the network call."""
    seen: list = []
    page = _Page()
    port = _port(page, _oracle({"status": "proposed"}, seen), box=BOX)

    async def _refuse():
        return None
    port._redacted_screenshot = _refuse

    assert await port._vision_medic_rung(CONTROL, "slider", []) is None
    assert not seen, "an image was sent although redaction could not be proven"


@pytest.mark.asyncio
async def test_display_only_is_a_terminal_answer_not_a_failure():
    seen: list = []
    port = _port(_Page(), _oracle({"status": "display_only"}, seen), box=BOX)
    obs = await port._vision_medic_rung(CONTROL, "slider", [])
    assert obs is not None
    assert obs.mechanic_used == "vision_medic:display_only"
    assert obs.intent_met is None, "display_only must not read as a failed intent"


@pytest.mark.asyncio
async def test_an_oracle_exception_never_escapes_the_rung():
    """A vision outage must degrade to residue, never break the crawl."""
    async def boom(**kwargs):
        raise RuntimeError("provider down")
    page = _Page()
    port = _port(page, boom, box=BOX)
    assert await port._vision_medic_rung(CONTROL, "slider", []) is None
    assert page.clicks == []


@pytest.mark.asyncio
async def test_a_non_numeric_point_is_refused():
    seen: list = []
    page = _Page()
    port = _port(page, _oracle(
        {"status": "proposed", "action": "click",
         "click_x": "middle", "click_y": None}, seen), box=BOX)
    assert await port._vision_medic_rung(CONTROL, "slider", []) is None
    assert page.clicks == []


# ══════════════════════════════════════════════════════════════════════════
# THE CALLER EXISTS — the structural half of A28
# ══════════════════════════════════════════════════════════════════════════

def test_the_engine_names_the_endpoint():
    """The regression this milestone exists to prevent: the route going back to
    having no consumer. Grep-shaped on purpose — it fails if the wiring is
    deleted, regardless of how the rung is refactored."""
    from pathlib import Path
    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    src = main_py.read_text(encoding="utf-8")
    assert "/internal/vision-operate" in src, (
        "no module in this engine names /internal/vision-operate — the endpoint "
        "is orphaned again, which is the exact A28 defect")
    assert "_make_vision_medic_oracle" in src
    assert "vision_medic_oracle=" in src, (
        "the oracle is built but never handed to the browser port")


def test_the_new_oracle_kind_is_a_first_class_metric_label():
    """Folded into ``other``, the rung's cost would be unattributable."""
    from app import metrics
    assert "vision_medic" in metrics.ORACLE_KINDS
    assert metrics._enum("vision_medic", metrics.ORACLE_KINDS) == "vision_medic"
