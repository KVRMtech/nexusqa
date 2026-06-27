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
