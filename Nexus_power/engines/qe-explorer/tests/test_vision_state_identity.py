"""M3.1 / T-VIS-02 — VISION-AWARE STATE IDENTITY.

THE HOLE
========
Rung 4 of the identity ladder — the perceptual hash — has existed since M1.1,
and until this milestone only the WALK ever supplied one.  DISCOVERY, which is
the path that records states and feeds the catalogue, called
``StateFingerprinter.fingerprint`` with no ``perceptual_hash`` at all.

For a canvas application that is fatal rather than cosmetic.  Its screens share
one URL, one (empty) control inventory and one dialog set, so every screen
hashed to the SAME digest, and every screen after the first was silently dropped
by ``_visited_fingerprints``.  A twelve-screen WebGL quote flow was recorded as
one state.

THE FIX, AND ITS PRECISE SCOPE
==============================
The hash is admitted on exactly one condition — the state is DOM-opaque AND
DOM-sparse, i.e. exactly the states ``should_perceive`` escalates.  Both halves
of that are asserted here, because the fix is only safe if the second half
holds: a page the DOM explains must still hash byte-for-byte as it always did,
or a cosmetic repaint anywhere in the fleet starts fragmenting states.
"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.crawler import Budget, Crawler, GuardContext
from app.guard import load_refuse_pack
from app.perception import perceptual_hash_png, should_perceive
from app.state_identity import StateFingerprinter

pytest.importorskip("PIL")

CANVAS_SURFACE = [{"kind": "canvas", "label": "quote canvas",
                   "reason": "a canvas-rendered surface"}]


def _screen_png(n: int, w: int = 400, h: int = 300) -> bytes:
    """A distinct canvas SCREEN.  Structured, not a flat fill — ``average_hash``
    compares cells against the frame mean, so any uniform image hashes to the
    same all-ones digest whatever its colour."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i in range(1 + (n % 3)):
        x = 20 + (i * 90 + n * 47) % (w - 120)
        y = 20 + (i * 60 + n * 31) % (h - 90)
        d.rectangle([x, y, x + 100, y + 70], fill=(0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 0. the fixture's own discriminating power ───────────────────────────────

def test_the_screens_used_below_really_are_perceptually_distinct():
    assert len({perceptual_hash_png(_screen_png(i)) for i in range(3)}) == 3
    assert perceptual_hash_png(_screen_png(1)) == perceptual_hash_png(_screen_png(1))


# ── 1. the identity ladder, at the hasher ───────────────────────────────────

def test_two_visually_distinct_canvas_states_do_not_collapse():
    """T-VIS-02's acceptance, first half."""
    fp = StateFingerprinter()
    a = fp.fingerprint(url="https://app/quote", controls=[],
                       perceptual_hash=perceptual_hash_png(_screen_png(0)))
    b = fp.fingerprint(url="https://app/quote", controls=[],
                       perceptual_hash=perceptual_hash_png(_screen_png(1)))
    assert a != b


def test_visually_identical_states_do_not_explode_into_two():
    """T-VIS-02's acceptance, second half — the one that keeps this honest.

    Distinctness has to be EARNED by an observed difference.  A canvas that
    repaints to the same picture is the same state, and minting a new identity
    for it would make ``TERMINAL_LOOP`` unreachable on exactly the applications
    that need it most.
    """
    fp = StateFingerprinter()
    h = perceptual_hash_png(_screen_png(2))
    assert (fp.fingerprint(url="https://app/quote", controls=[], perceptual_hash=h)
            == fp.fingerprint(url="https://app/quote", controls=[],
                              perceptual_hash=h))


def test_a_coarse_hash_absorbs_a_cosmetic_repaint():
    """aHash is 8x8 by design: a blinking cursor or a one-pixel nudge must not
    be a new state, or a canvas app becomes an infinite frontier."""
    base = _screen_png(0)
    from PIL import Image, ImageDraw

    img = Image.open(BytesIO(base)).convert("RGB")
    ImageDraw.Draw(img).point((1, 1), fill=(0, 0, 0))   # one pixel
    buf = BytesIO()
    img.save(buf, format="PNG")
    assert perceptual_hash_png(base) == perceptual_hash_png(buf.getvalue())


# ── 2. WHEN the hash is admitted (the discovery rule) ───────────────────────

def test_the_admission_rule_is_opaque_AND_sparse():
    rich = [{"name": "Continue", "kind": "button",
             "qec": {"name_confidence": "high"}} for _ in range(5)]
    assert should_perceive([], CANVAS_SURFACE) is True
    assert should_perceive(rich, CANVAS_SURFACE) is False    # DOM explains it
    assert should_perceive([], []) is False                  # nothing opaque


def test_a_dom_explained_page_is_fingerprinted_exactly_as_before():
    """The compatibility half.  Supplying no hash must be byte-identical to the
    behaviour that existed before rung 4 reached discovery — every state ever
    recorded still hashes to the value it hashed then."""
    fp = StateFingerprinter()
    controls = [{"role": "button", "name": "Continue", "kind": "button"}]
    assert (fp.fingerprint(url="https://app/x", controls=controls)
            == fp.fingerprint(url="https://app/x", controls=controls,
                              perceptual_hash=""))


# ── 3. the DISCOVERY path, end to end ───────────────────────────────────────

class CanvasApp:
    """A minimal canvas application: one URL, no readable controls, and a
    DIFFERENT picture on each visit.

    This is the shape that collapsed.  Only ``screenshot_png`` distinguishes the
    two states; every DOM signal the fingerprinter has ever used is identical.
    """

    def __init__(self, screens: list[int]) -> None:
        self._screens = screens
        self._visit = -1
        self.opaque_calls = 0

    # -- navigation
    async def goto(self, url: str):
        from app.browser import NavResult

        self._visit += 1
        return NavResult(ok=True, url="https://app/quote", status=200)

    async def current_url(self) -> str:
        return "https://app/quote"

    async def title(self) -> str:
        return "Quote"

    # -- inventory: a canvas app reads as EMPTY
    async def collect_controls(self) -> list[dict[str, Any]]:
        return []

    async def collect_displayed_values(self) -> list[dict[str, Any]]:
        return []

    async def collect_opaque(self) -> list[dict[str, Any]]:
        self.opaque_calls += 1
        return [dict(s) for s in CANVAS_SURFACE]

    async def dialog_flags(self) -> list[str]:
        return []

    async def error_texts(self) -> list[str]:
        return []

    async def status_texts(self) -> list[str]:
        return []

    async def visible_texts(self) -> list[str]:
        return []

    async def screenshot_png(self) -> bytes:
        idx = min(max(self._visit, 0), len(self._screens) - 1)
        return _screen_png(self._screens[idx])

    async def storage_state(self) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        return None


def _crawl(app: CanvasApp, tmp_path: Path):
    pack = load_refuse_pack(Settings().refuse_pack_path)
    crawler = Crawler(
        app, crawl_id="vis02", tenant_id="t", target_url="https://app/quote",
        work_dir=str(tmp_path), refuse_pack=pack,
        budget=Budget(max_states=6, max_depth=2, max_actions_per_state=4,
                      max_requests=40, rate_per_s=0),
        explorer_version="t", guard_version="t", refuse_pack_version=pack.version,
        config_fingerprint="fp", guard_context=GuardContext(refuse_pack=pack),
        sleep=_no_sleep,
    )
    return crawler, asyncio.run(crawler.run())


async def _no_sleep(_seconds: float) -> None:
    return None


def test_discovery_admits_the_perceptual_hash_on_a_canvas_state(tmp_path):
    """The producer half: the state a canvas app is recorded under is derived
    from its PIXELS, because nothing else about it differs."""
    a = CanvasApp([0])
    crawler_a, _ = _crawl(a, tmp_path / "a")
    b = CanvasApp([1])
    crawler_b, _ = _crawl(b, tmp_path / "b")

    fps_a = set(crawler_a._visited_fingerprints)
    fps_b = set(crawler_b._visited_fingerprints)
    assert fps_a and fps_b
    assert a.opaque_calls >= 1                     # the surface WAS detected
    assert fps_a.isdisjoint(fps_b), (
        "two canvas applications showing different pictures at the same URL "
        "with the same (empty) DOM collapsed to the same state identity")


def test_the_same_picture_twice_is_one_state(tmp_path):
    """…and the guard against manufacturing distinctness."""
    a = CanvasApp([3])
    crawler_a, _ = _crawl(a, tmp_path / "a")
    b = CanvasApp([3])
    crawler_b, _ = _crawl(b, tmp_path / "b")
    assert set(crawler_a._visited_fingerprints) == set(crawler_b._visited_fingerprints)


def test_a_port_without_opaque_detection_changes_no_fingerprint(tmp_path):
    """A fake or an older adapter with no ``collect_opaque`` must behave exactly
    as it did before this milestone: no probe, no hash, historical digest."""
    class NoOpaque(CanvasApp):
        collect_opaque = None

    a = NoOpaque([0])
    crawler_a, _ = _crawl(a, tmp_path / "a")
    b = NoOpaque([1])
    crawler_b, _ = _crawl(b, tmp_path / "b")
    assert set(crawler_a._visited_fingerprints) == set(crawler_b._visited_fingerprints)
