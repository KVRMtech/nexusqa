"""M3.1 / T-VIS-01 — A VISION PREDICTION IS NEVER CATALOG TRUTH.

The loop is exercised with a fake port and a fake oracle, so every rung —
capture, redaction, budget, perception, screening, coordinate action, R0 — runs
for real without a browser.

The assertion that matters is not "vision produced controls".  It is that
``promoted`` — the ONLY field a catalogue-feeding caller reads — contains
EXACTLY the controls the loop clicked and then measured the page responding to,
and that everything else is present in the ledger as refused and absent from
``promoted``.
"""
from __future__ import annotations

import asyncio
from io import BytesIO

import pytest

from app import vision_gate
from app.vision_loop import (
    R0_DOM,
    R0_PIXEL,
    REFUSED_NOT_ALLOWED,
    REFUSED_NO_COORDINATE,
    REFUSED_UNVERIFIED,
    SKIPPED_BUDGET,
    SKIPPED_NOT_JUSTIFIED,
    SKIPPED_NOTHING_PERCEIVED,
    SKIPPED_NO_SCREENSHOT,
    SKIPPED_REDACTION,
    VERIFIED,
    VisionEscalation,
)

pytest.importorskip("PIL", reason="Pillow is required to mask pixels")

CANVAS = [{"kind": "canvas", "label": "quote surface", "reason": "canvas app"}]
SPARSE: list = []


def _png(screen=0, w=800, h=600) -> bytes:
    """A canvas SCREEN, drawn so that different screens really hash differently.

    Deliberately NOT a flat fill.  ``average_hash`` compares each cell against
    the frame mean, so every uniform image — of any colour — hashes to the same
    all-ones digest.  A fake built from flat fills would therefore "prove" the
    perceptual rung works when it was measuring nothing, which is precisely the
    class of green-wash this milestone is about.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # A different number of dark blocks in different places per screen.
    for i in range(1 + (screen % 4)):
        x0 = 40 + (i * 137 + screen * 61) % (w - 200)
        y0 = 30 + (i * 91 + screen * 53) % (h - 150)
        draw.rectangle([x0, y0, x0 + 160, y0 + 110], fill=(0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_the_fake_screens_really_are_perceptually_distinct():
    """A guard on the fixture itself.

    Every perceptual assertion below is worthless if two screens hash alike, so
    the fixture's own discriminating power is asserted rather than assumed.
    """
    from app.perception import perceptual_hash_png

    hashes = {perceptual_hash_png(_png(i)) for i in range(4)}
    assert len(hashes) == 4
    assert perceptual_hash_png(_png(1)) == perceptual_hash_png(_png(1))


class FakeObs:
    def __init__(self, intent_met=None, error_detail=""):
        self.intent_met = intent_met
        self.error_detail = error_detail
        self.url_before = self.url_after = "https://app/x"


class FakePort:
    """A canvas application whose response to a click is scripted.

    ``on_click`` returns ``(observation, new_colour_or_None)`` so a test can
    model the three real cases: the DOM responded, only the pixels responded,
    and nothing responded.
    """

    def __init__(self, *, regions_ok=True, regions=(), on_click=None,
                 animated=False, screen=0):
        self._regions_ok = regions_ok
        self._regions = list(regions)
        self._on_click = on_click or (lambda x, y: (FakeObs(), None))
        self._animated = animated
        self._screen = screen
        self._tick = 0
        self.clicks: list[tuple[int, int]] = []

    async def screenshot_png(self):
        if self._animated:
            # repaints on its OWN, with no click involved
            self._tick += 1
            return _png(self._tick)
        return _png(self._screen)

    async def collect_pii_regions(self):
        return {"ok": self._regions_ok, "regions": self._regions,
                "page_w": 800, "page_h": 600, "dpr": 1}

    async def click_at(self, x, y):
        self.clicks.append((x, y))
        obs, new_screen = self._on_click(x, y)
        if new_screen is not None:
            self._screen = new_screen
        return obs


class FakeOracle:
    def __init__(self, replies=None, raises=False):
        self._replies = replies if replies is not None else []
        self._raises = raises
        self.calls: list[dict] = []

    async def perceive(self, b64, ctx):
        self.calls.append({"b64": b64, "ctx": dict(ctx)})
        if self._raises:
            raise RuntimeError("provider down")
        if isinstance(self._replies, dict):
            return self._replies
        return self._replies.pop(0) if self._replies else {}


def _budget(**kw):
    gate = vision_gate.decide_gate(attested=True, tenant_enabled=True,
                                   rung=vision_gate.RUNG_SIGNED_PROOF)
    kw.setdefault("max_calls", 5)
    return vision_gate.VisionBudget(gate=gate, **kw)


def _control(label="Get Quote", x=120, y=200):
    return {"label": label, "role": "button", "bbox": [100, 180, 120, 40],
            "click_x": x, "click_y": y}


def _run(port, oracle, budget=None, **kw):
    esc = VisionEscalation(port=port, oracle=oracle,
                           budget=budget or _budget(), **kw)
    return asyncio.run(esc.run(url="https://app/x", controls=SPARSE,
                               opaque_surfaces=CANVAS))


# ── 1. THE LAW ──────────────────────────────────────────────────────────────

def test_a_verified_control_is_promoted_and_carries_its_proof():
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    res = _run(port, FakeOracle({"controls": [_control()]}))
    assert res.ran is True and res.perceived == 1
    assert [c["name"] for c in res.promoted] == ["Get Quote"]
    qec = res.promoted[0]["qec"]
    assert qec["r0_verified"] is True and qec["r0_rung"] == R0_DOM
    assert qec["capture_mode"] == "vision"
    assert port.clicks == [(120, 200)]


def test_an_UNVERIFIED_perception_is_refused_and_promotes_NOTHING():
    """The negative case, and the whole point of the milestone.

    The model named a control, the loop clicked exactly where it said, and the
    page did nothing. That is a wrong perception, and it must reach neither the
    catalogue nor a locator nor a journey step.
    """
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=None), None))
    res = _run(port, FakeOracle({"controls": [_control("Ghost Button")]}))
    assert res.promoted == []                     # nothing catalogued
    assert res.perceived == 1                     # …but it IS recorded
    assert [a.status for a in res.attempts] == [REFUSED_UNVERIFIED]
    assert res.attempts[0].label == "Ghost Button"
    assert "unverified" in res.attempts[0].reason


def test_R0_None_refuses_rather_than_passing():
    """``verify_intent`` returns ``None`` for "unverifiable", and elsewhere in
    this engine ``None`` preserves a fill because we cannot prove it failed.

    Here the polarity is inverted on purpose: a DOM fill has a read-back and a
    locator behind it; a vision control has a model's guess behind it.
    Unverifiable evidence from an unverifiable source is not evidence.
    """
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=None), None))
    assert _run(port, FakeOracle({"controls": [_control()]})).promoted == []


def test_an_action_error_is_refused_and_never_reaches_the_pixel_rung():
    port = FakePort(on_click=lambda x, y: (
        FakeObs(error_detail="action_error: detached"), 3))
    res = _run(port, FakeOracle({"controls": [_control()]}))
    assert res.promoted == []
    assert "action error" in res.attempts[0].reason


def test_the_verified_and_refused_controls_are_separated_on_one_state():
    def on_click(x, y):
        return (FakeObs(intent_met=True), None) if x == 120 else (FakeObs(), None)

    port = FakePort(on_click=on_click)
    res = _run(port, FakeOracle({"controls": [
        _control("Real Button", 120, 200),
        _control("Hallucinated", 400, 400),
    ]}), max_actions_per_state=4)
    assert [c["name"] for c in res.promoted] == ["Real Button"]
    assert {a.label: a.status for a in res.attempts} == {
        "Real Button": VERIFIED, "Hallucinated": REFUSED_UNVERIFIED}
    assert res.verified == 1 and res.refused == 1


# ── 2. the PERCEPTUAL R0 rung, and why it has to be earned ──────────────────

def test_a_still_surface_that_repaints_after_the_click_is_verified():
    """The rung that makes a WebGL control verifiable at all.

    A canvas app changes no DOM, so rung 1 can never fire on it. Rung 2 is a
    MEASUREMENT (the pixels moved), not a question to the model.
    """
    port = FakePort(screen=0,
                    on_click=lambda x, y: (FakeObs(intent_met=None), 2))
    res = _run(port, FakeOracle({"controls": [_control()]}))
    assert res.pixel_rung_admissible is True
    assert [c["qec"]["r0_rung"] for c in res.promoted] == [R0_PIXEL]


def test_an_ANIMATING_surface_cannot_verify_anything_by_pixels():
    """THE false-positive a naive "did the pixels change?" check would have.

    An animated canvas repaints regardless of the click, so every hallucinated
    control would verify. The surface is therefore proven STILL first, and when
    it is not, the rung is declared inadmissible and said so out loud.
    """
    port = FakePort(animated=True,
                    on_click=lambda x, y: (FakeObs(intent_met=None), None))
    res = _run(port, FakeOracle({"controls": [_control()]}))
    assert res.pixel_rung_admissible is False
    assert res.promoted == []
    assert "already repainting" in res.attempts[0].reason


def test_a_still_surface_that_does_NOT_repaint_is_refused():
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=None), None))
    res = _run(port, FakeOracle({"controls": [_control()]}))
    assert res.pixel_rung_admissible is True
    assert res.promoted == []
    assert "neither the DOM nor the pixels" in res.attempts[0].reason


# ── 3. screening: a canvas boundary is still a boundary ─────────────────────

def test_a_consequential_perceived_label_is_never_clicked():
    """A "Delete Policy" painted onto a canvas is exactly as irreversible as a
    marked-up one, and a boundary the crawl cannot see in the DOM is one to be
    MORE careful with, not less."""
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    res = _run(port, FakeOracle({"controls": [_control("Delete Policy")]}))
    assert port.clicks == []                      # never actuated
    assert res.promoted == []
    assert res.attempts[0].status == REFUSED_NOT_ALLOWED


def test_an_unnamed_perception_is_refused_outright():
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    res = _run(port, FakeOracle({"controls": [_control("")]}))
    assert port.clicks == []
    assert res.attempts[0].status == REFUSED_NOT_ALLOWED


def test_a_perception_with_no_click_point_is_refused():
    port = FakePort()
    res = _run(port, FakeOracle({"controls": [
        {"label": "Somewhere", "role": "button", "bbox": [0, 0, 0, 0],
         "click_x": None, "click_y": None}]}))
    assert port.clicks == []
    assert res.attempts[0].status == REFUSED_NO_COORDINATE


def test_the_per_state_action_budget_is_bounded():
    """A hallucinated 40-control perception must not become 40 clicks."""
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    res = _run(port, FakeOracle({"controls": [
        _control(f"Button {i}", 100 + i, 200) for i in range(6)]}),
        max_actions_per_state=2)
    assert len(port.clicks) == 2
    assert len(res.promoted) == 2
    assert sum(1 for a in res.attempts
               if "budget spent" in a.reason) == 4


# ── 4. the gate, the budget, and the redaction all refuse BEFORE the model ──

def test_a_page_the_DOM_explains_never_costs_a_vision_call():
    oracle = FakeOracle({"controls": [_control()]})
    esc = VisionEscalation(port=FakePort(), oracle=oracle, budget=_budget())
    res = asyncio.run(esc.run(url="u", controls=[], opaque_surfaces=[]))
    assert res.skipped_reason == SKIPPED_NOT_JUSTIFIED
    assert oracle.calls == []


def test_a_closed_gate_makes_no_vision_call():
    oracle = FakeOracle({"controls": [_control()]})
    res = _run(FakePort(), oracle, budget=vision_gate.closed_budget())
    assert res.skipped_reason.startswith(SKIPPED_BUDGET)
    assert oracle.calls == []
    assert res.promoted == []


def test_a_failed_redaction_refuses_BEFORE_the_budget_is_spent():
    """A capture that cannot be masked must not burn a model call — and, far
    more importantly, must not be sent."""
    budget = _budget()
    oracle = FakeOracle({"controls": [_control()]})
    res = _run(FakePort(regions_ok=False), oracle, budget=budget)
    assert res.skipped_reason == SKIPPED_REDACTION
    assert oracle.calls == []
    assert budget.calls == 0


def test_a_port_that_cannot_locate_pii_is_refused_not_degraded():
    """Every other optional verb on the port degrades to "nothing found".

    This one degrades to "do not send", because an evaluation error that
    silently published an unmasked screenshot of a real application is the exact
    outcome T-VIS-05 exists to prevent.
    """
    class NoRegions(FakePort):
        collect_pii_regions = None

    oracle = FakeOracle({"controls": [_control()]})
    res = _run(NoRegions(), oracle)
    assert res.skipped_reason == SKIPPED_REDACTION
    assert oracle.calls == []


def test_the_screenshot_that_reaches_the_oracle_is_the_MASKED_one():
    import base64

    from PIL import Image

    port = FakePort(regions=[{"x": 0, "y": 0, "w": 800, "h": 600}],
                    on_click=lambda x, y: (FakeObs(intent_met=True), None))
    oracle = FakeOracle({"controls": [_control()]})
    _run(port, oracle)
    sent = base64.b64decode(oracle.calls[0]["b64"])
    assert Image.open(BytesIO(sent)).convert("RGB").getpixel((400, 300)) == (0, 0, 0)
    # …and the receipt travels with it so the server can ENFORCE the masking
    # rather than trust that it happened.
    receipt = oracle.calls[0]["ctx"]["pixel_redaction"]
    assert receipt["applied"] is True and receipt["regions"] == 1
    assert len(receipt["image_sha256"]) == 64


def test_an_oracle_failure_charges_vision_s_own_breaker_only():
    budget = _budget(breaker_threshold=2)
    port = FakePort()
    for _ in range(2):
        res = _run(port, FakeOracle(raises=True), budget=budget)
        assert res.promoted == []
    assert budget.breaker_open is True
    # the next escalation cannot even reach the provider
    oracle = FakeOracle({"controls": [_control()]})
    assert _run(port, oracle, budget=budget).skipped_reason.startswith(SKIPPED_BUDGET)
    assert oracle.calls == []


def test_an_honest_empty_perception_is_a_SUCCESS_not_a_breaker_failure():
    """A provider that answers "I see nothing" is working.

    Counting it as a failure would open the breaker on exactly the pages vision
    is least useful on and then keep it shut for the ones it is useful on.
    """
    budget = _budget(breaker_threshold=2)
    port = FakePort()
    for _ in range(3):
        res = _run(port, FakeOracle({"controls": []}), budget=budget)
        assert res.skipped_reason == SKIPPED_NOTHING_PERCEIVED
        assert res.ran is True
    assert budget.breaker_open is False
    assert budget.calls == 3


# ── 5. observe-only ─────────────────────────────────────────────────────────

def test_observe_only_perceives_but_promotes_nothing():
    port = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    esc = VisionEscalation(port=port, oracle=FakeOracle({"controls": [_control()]}),
                           budget=_budget())
    res = asyncio.run(esc.run(url="u", controls=SPARSE,
                              opaque_surfaces=CANVAS, act=False))
    assert port.clicks == []
    assert res.promoted == []
    assert res.attempts[0].status == REFUSED_UNVERIFIED
    assert "observe-only" in res.attempts[0].reason


# ── 6. outcomes ride on the same proof as the controls ─────────────────────

def test_displayed_values_are_carried_only_when_something_was_verified():
    reply = {"controls": [_control()],
             "displayed_values": [{"label": "Premium", "text": "$42.10"}]}
    verified = FakePort(on_click=lambda x, y: (FakeObs(intent_met=True), None))
    assert _run(verified, FakeOracle(dict(reply))).outcomes == [
        {"label": "Premium", "selector": "", "text": "$42.10", "source": "vision"}]

    unverified = FakePort(on_click=lambda x, y: (FakeObs(intent_met=None), None))
    res = _run(unverified, FakeOracle(dict(reply)))
    assert res.outcomes == [], (
        "a page whose perception could not be proven has not earned the right "
        "to contribute outcome figures either")


# ── 7. the ledger is the audit trail ────────────────────────────────────────

def test_the_ledger_records_both_halves_with_reasons():
    def on_click(x, y):
        return (FakeObs(intent_met=True), None) if x == 120 else (FakeObs(), None)

    res = _run(FakePort(on_click=on_click), FakeOracle({"controls": [
        _control("Real", 120, 200), _control("Fake", 400, 400),
        _control("Delete Policy", 500, 500)]}), max_actions_per_state=4)
    led = res.as_ledger()
    assert (led["perceived"], led["verified"], led["refused"]) == (3, 1, 2)
    statuses = {a["label"]: a["status"] for a in led["attempts"]}
    assert statuses == {"Real": VERIFIED, "Fake": REFUSED_UNVERIFIED,
                        "Delete Policy": REFUSED_NOT_ALLOWED}
    assert all(a["reason"] for a in led["attempts"])


def test_a_browser_that_produced_no_image_is_distinguishable_from_a_refusal():
    """"No screenshot" and "we refused to send the screenshot" are different
    findings, and a crawl that made no vision call has to say which it hit."""
    class Blind(FakePort):
        async def screenshot_png(self):
            return b""

    oracle = FakeOracle({"controls": [_control()]})
    assert _run(Blind(), oracle).skipped_reason == SKIPPED_NO_SCREENSHOT
    assert _run(FakePort(regions_ok=False), oracle).skipped_reason == SKIPPED_REDACTION
    assert oracle.calls == []
