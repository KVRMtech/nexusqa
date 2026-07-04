"""Pages & Forms extraction-accuracy regression tests (Road-A / Phase 0).

Pins the four Road-A fixes that lift the audited corpus:
  1. the path-only OCR-recovery regex admits '.', so file extensions (.html)
     survive instead of being truncated to a bare path;
  2. the DISPLAY host preserves the observed www/subdomain while canonicalisation
     still rewrites ghost hosts;
  3. recording-tool / conferencing OVERLAY chrome is quarantined (never a page);
  4. provenance confidence is honest (GROUND_TRUTH=1.0 PROVEN, MISSING_PAGE low).

These import the real helpers, so they run in CI / the container where the app +
nexus_sdk are available (the page_visit_extractor pulls in sqlalchemy/httpx/pydantic):
    python -m pytest tests/test_pages_forms_accuracy.py -q
"""
from __future__ import annotations

import re

import pytest

from app.services.storyboard import page_visit_extractor as pve
from app.services.storyboard.config import PageVisitExtractorConfig
from app.services.storyboard.page_schemas import PageVisitSource


class _Frame:
    """Minimal stand-in for VisualFrameRow (the helper reads via getattr)."""
    def __init__(self, extracted_text="", page_title="", url_or_path=""):
        self.extracted_text = extracted_text
        self.page_title = page_title
        self.url_or_path = url_or_path


def test_path_regex_preserves_file_extension():
    # OCR destroyed the host but the deep path survived, ending in '.html'.
    url_pattern = re.compile(PageVisitExtractorConfig.url_regex_pattern, re.IGNORECASE)
    got = pve._first_url_match("... /checkout/step-one.html ...", url_pattern)
    assert got.endswith(".html"), got
    assert "/checkout/step-one.html" in got


def test_display_host_preserves_www_but_rewrites_ghost():
    # www variant of the canonical host → preserved verbatim.
    assert pve._display_host("www.saucedemo.com", "saucedemo.com") == "www.saucedemo.com"
    # exact match → canonical.
    assert pve._display_host("saucedemo.com", "saucedemo.com") == "saucedemo.com"
    # ghost host rewritten by canonicalisation → trust the canonical, not the ghost.
    assert pve._display_host("msdd.com", "usaa.com") == "usaa.com"


def test_recording_chrome_is_quarantined():
    pat = re.compile(PageVisitExtractorConfig.recording_chrome_pattern, re.IGNORECASE)
    # A conferencing 'Main View' frame with NO app URL → quarantined.
    chrome = _Frame(extracted_text="Video Conferencing — Screen Share / Main View")
    assert pve._is_recording_chrome(chrome, pat) is True
    # The same overlay phrase but the frame ALSO shows a real app URL → NOT chrome.
    app_over_share = _Frame(extracted_text="Main View", url_or_path="/inventory.html")
    assert pve._is_recording_chrome(app_over_share, pat) is False
    # A normal app frame → not chrome.
    normal = _Frame(extracted_text="Add to cart  Checkout", url_or_path="/inventory")
    assert pve._is_recording_chrome(normal, pat) is False
    # Quarantine disabled (pattern None) → never fires.
    assert pve._is_recording_chrome(chrome, None) is False


def test_provenance_confidence_is_honest():
    c = pve._confidence_for_source
    assert c(PageVisitSource.GROUND_TRUTH) == 1.0     # instrumented → PROVEN
    assert c(PageVisitSource.URL_REGEX) == 1.0
    assert c(PageVisitSource.MISSING_PAGE) <= 0.3     # honest low-confidence gap
    assert c(PageVisitSource.SCREEN_NAME_OCR) <= 0.6


def test_missing_page_source_is_low_confidence_and_distinct():
    # The placeholder must never read as a real, asserted page.
    assert PageVisitSource.MISSING_PAGE.value == "missing_page"
    assert pve._confidence_for_source(PageVisitSource.MISSING_PAGE) < \
        pve._confidence_for_source(PageVisitSource.URL_REGEX)


def test_ground_truth_overlay_recovers_missing_page_and_overrides_url():
    """Phase 5 (Road B): instrumented navigation events (a) OVERRIDE the nearest
    frame with the exact URL at source=GROUND_TRUTH, and (b) INJECT a page that no
    frame captured (the missing cart.html) so it can never vanish."""
    F = pve._FrameLocation
    frames = [
        F(frame_id="f1", frame_index=0, timestamp_ms=1000, frame_asset_path="a1",
          scene_id="s1", raw_location="https://saucedemo.com/inventory",
          source=pve.PageVisitSource.URL_REGEX, url_host="saucedemo.com", url_path="/inventory"),
        F(frame_id="f2", frame_index=1, timestamp_ms=24000, frame_asset_path="a2",
          scene_id="s2", raw_location="https://saucedemo.com/checkout-step-one",
          source=pve.PageVisitSource.URL_REGEX, url_host="saucedemo.com", url_path="/checkout-step-one"),
    ]
    GE = pve.GroundTruthEvent
    events = [
        GE(timestamp_ms=1000, kind="navigate", url="https://www.saucedemo.com/inventory.html",
           url_host="www.saucedemo.com", url_path="/inventory.html"),
        # cart.html — a fast transition NO frame captured.
        GE(timestamp_ms=12000, kind="navigate", url="https://www.saucedemo.com/cart.html",
           url_host="www.saucedemo.com", url_path="/cart.html"),
        GE(timestamp_ms=24000, kind="navigate", url="https://www.saucedemo.com/checkout-step-one.html",
           url_host="www.saucedemo.com", url_path="/checkout-step-one.html"),
    ]
    merged, applied = pve._apply_ground_truth_overlay(frames, events)
    assert applied == 3
    paths = [m.url_path for m in merged]
    assert "/cart.html" in paths                                   # missing page recovered
    inv = [m for m in merged if m.url_path == "/inventory.html"][0]
    assert inv.source == pve.PageVisitSource.GROUND_TRUTH          # overridden + exact URL
    cart = [m for m in merged if m.url_path == "/cart.html"][0]
    assert cart.source == pve.PageVisitSource.GROUND_TRUTH
    assert paths.index("/inventory.html") < paths.index("/cart.html") < paths.index("/checkout-step-one.html")


def test_ground_truth_overlay_noop_without_events():
    # Absent a sidecar, the video path is byte-identical (fail-open).
    F = pve._FrameLocation
    frames = [F(frame_id="f1", frame_index=0, timestamp_ms=1000, frame_asset_path="a1",
                scene_id="s1", raw_location="x", source=pve.PageVisitSource.URL_REGEX)]
    merged, applied = pve._apply_ground_truth_overlay(frames, [])
    assert applied == 0 and merged is frames


# ── Item 3: demote guessed-host path-only reads off the PROVEN tier ───────────


class _ResolveFrame:
    """Richer VisualFrameRow stand-in for _resolve_frame_location (getattr)."""
    def __init__(self, extracted_text="", page_title="", url_or_path=""):
        self.frame_id = "f0"
        self.frame_index = 0
        self.timestamp_seconds = 1.0
        self.frame_asset_path = "a0"
        self.scene_id = "s0"
        self.extracted_text = extracted_text
        self.page_title = page_title
        self.url_or_path = url_or_path


def test_repair_html_extension_recovers_dropped_dot():
    """OCR drops the dot before 'html' (inventory.html -> 'inventory html' or
    'checkout-step-two.html' -> 'checkout-step-twohtml'). The repair recovers
    the extension — deterministically when merged, OCR-evidenced when dropped —
    and NEVER fabricates one without evidence."""
    r = pve._repair_html_extension
    # Case 1 — merged 'twohtml' (deterministic, no OCR needed).
    assert r("/checkout-step-twohtml", "") == "/checkout-step-two.html"
    assert r("/checkout-step-tohtml", "") == "/checkout-step-to.html"
    assert r("/a/b/foohtml", "") == "/a/b/foo.html"
    # Case 2 — extension dropped entirely; recovered ONLY with OCR evidence.
    assert r("/inventory", "saucedemo com/inventory html aclion") == "/inventory.html"
    assert r("/checkout-complete", "com/checkout-complete html") == "/checkout-complete.html"
    # NEVER fabricate: no OCR evidence of 'html' -> unchanged.
    assert r("/inventory", "swag labs products list") == "/inventory"
    # Already has the extension, or is root -> untouched.
    assert r("/cart.html", "cart html") == "/cart.html"
    assert r("/", "anything html") == "/"


def test_path_only_ocr_match_is_not_proven_url_regex():
    """A bare /path OCR match whose host is GUESSED from the dominant host must
    be stamped url_ocr_path_only (sub-1.0), NEVER url_regex/1.0 — closing the
    current green-wash where a guessed host read as PROVEN."""
    url_pattern = re.compile(PageVisitExtractorConfig.url_regex_pattern, re.IGNORECASE)
    cfg = PageVisitExtractorConfig()

    frame = _ResolveFrame(extracted_text="... /personal-info/coverage.html ...")
    loc = pve._resolve_frame_location(
        frame, scene=None, app_instance=None,
        url_pattern=url_pattern, config=cfg, dominant_host="usaa.com",
    )
    assert loc.source == PageVisitSource.URL_OCR_PATH_ONLY     # NOT url_regex
    assert loc.url_host == "usaa.com"                          # host was GUESSED
    assert loc.url_path == "/personal-info/coverage.html"
    # honesty: sub-1.0, strictly below a full url_regex read, and NOT proven.
    assert pve._confidence_for_source(loc.source) == 0.7
    assert pve._confidence_for_source(loc.source) < \
        pve._confidence_for_source(PageVisitSource.URL_REGEX)
    assert loc.source.value not in {"ground_truth", "url_regex"}

    # A FULL URL OCR'd off the address bar stays PROVEN url_regex/1.0.
    frame2 = _ResolveFrame(extracted_text="open https://www.usaa.com/quote/start now")
    loc2 = pve._resolve_frame_location(
        frame2, scene=None, app_instance=None,
        url_pattern=url_pattern, config=cfg, dominant_host="usaa.com",
    )
    assert loc2.source == PageVisitSource.URL_REGEX
    assert pve._confidence_for_source(loc2.source) == 1.0


# ── Item 1: dwell-weighted frame selection for the vision identity pass ───────


def test_dwell_weighted_selection_covers_more_distinct_pages_than_even():
    """The vision budget must land on the pages the presenter paused on. With
    one long-dwell page + several short distinct pages and a tight cap, the
    dwell-weighted selector covers EVERY distinct page (one midpoint frame
    each) while plain even-sampling clusters on the long page and misses some."""
    F = pve._FrameLocation
    locs: list = []
    idx = 0

    def add(path: str, ts_ms: int) -> None:
        nonlocal idx
        locs.append(F(
            frame_id=f"f{idx}", frame_index=idx, timestamp_ms=ts_ms,
            frame_asset_path=f"a{idx}", scene_id="s",
            raw_location=f"https://x.com{path}",
            source=pve.PageVisitSource.URL_REGEX,
            url_host="x.com", url_path=path,
        ))
        idx += 1

    for t in range(12):           # /a — long dwell, 12 frames (ts 0..11000)
        add("/a", t * 1000)
    for p in ("/b", "/c", "/d", "/e"):   # 4 short distinct pages, 2 frames each
        base = 12000 + (ord(p[1]) - ord("b")) * 2000
        add(p, base)
        add(p, base + 1000)

    cap = 5
    dw = pve._dwell_weighted_frame_indices(locs, cap)
    dw_paths = {locs[i].url_path for i in dw}
    assert dw_paths == {"/a", "/b", "/c", "/d", "/e"}    # all 5 covered
    assert len(dw) <= cap                                # within budget

    n = len(locs)
    step = (n - 1) / max(1, cap - 1)
    even = sorted({min(n - 1, int(round(i * step))) for i in range(cap)})
    even_paths = {locs[i].url_path for i in even}
    assert len(even_paths) < len(dw_paths)               # even-sampling misses pages


def test_dwell_weighted_floor_one_frame_per_run():
    """Every distinct run gets at least one frame when runs <= cap."""
    F = pve._FrameLocation
    locs = [
        F(frame_id=f"f{i}", frame_index=i, timestamp_ms=i * 1000,
          frame_asset_path=f"a{i}", scene_id="s",
          raw_location=f"https://x.com/p{i // 2}",
          source=pve.PageVisitSource.URL_REGEX,
          url_host="x.com", url_path=f"/p{i // 2}")
        for i in range(6)            # 3 runs (/p0,/p1,/p2), 2 frames each
    ]
    dw = pve._dwell_weighted_frame_indices(locs, cap=10)
    assert {locs[i].url_path for i in dw} == {"/p0", "/p1", "/p2"}


# ── Item 2: high-res address-bar crop (sharper URL reading) ───────────────────


def test_crop_address_bar_returns_top_strip_and_fails_open():
    """The crop is the top strip (full width, top_fraction height); any failure
    fails OPEN to the SAME object so a frame is never lost."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    import io

    buf = io.BytesIO()
    Image.new("RGB", (1200, 1000), (10, 20, 30)).save(buf, format="PNG")
    data = buf.getvalue()

    out = pve._crop_address_bar(data, 0.08)
    assert out is not data                                # a real crop happened
    with Image.open(io.BytesIO(out)) as im:
        w, h = im.size
    assert w == 1200 and h == int(1000 * 0.08)            # full width, 8% strip

    # fail-open: non-image bytes return the SAME object, no exception raised.
    junk = b"not-an-image"
    assert pve._crop_address_bar(junk, 0.08) is junk
    # non-positive fraction → no crop (same object).
    assert pve._crop_address_bar(data, 0.0) is data
