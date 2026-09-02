"""A failed navigation is not a state of the application under test.

MEASURED, 2026-09-02, crawling parabank.parasoft.com — the first third-party
application put through this crawler. Two navigations failed. Chromium reports
the location of a failed navigation as ``chrome-error://chromewebdata/``, the
crawler recorded that as a page state twice, and qe-central then REFUSED THE
WHOLE CRAWL at ingest:

    substrate/schema._require_http_url -> "invalid_location"
    "A recorded page location was malformed (a capture hiccup). Re-crawl the app."

The crawl had produced 115 page states, 90 boundary crossings, 45 outcome
milestones and 70 edges. All of it was discarded over two records that described
no page at all.

Both halves of that are wrong, and this file fixes the half that is the cause:
``chrome-error://chromewebdata/`` is the browser saying it could not load
anything. It is not evidence about the application, it cannot be replayed, and
recording it corrupts a manifest that is otherwise sound.

WHY THE ASSERTIONS ARE SHAPED THIS WAY. "The record is absent" is satisfied just
as well by an emitter that writes nothing at all, so every test here pairs the
dropped record with a REAL one that must survive the same call sequence. A guard
that ate good page states would pass a test that only checked for absence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import emit


class _Clock:
    def now_ms(self) -> int:
        return 1_000


def _emitter(tmp_path: Path) -> emit.ManifestEmitter:
    return emit.ManifestEmitter(
        work_dir=str(tmp_path), crawl_id="c1", clock=_Clock(),
    )


def _record(location: str, seq: int) -> emit.PageStateRecord:
    return emit.PageStateRecord(
        sequence_index=seq, location=location, first_seen_ms=1, last_seen_ms=2,
    )


def _manifest(tmp_path: Path) -> list[dict]:
    path = emit.manifest_path(str(tmp_path), "c1")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


REAL = "https://parabank.parasoft.com/parabank/index.htm"


@pytest.mark.parametrize(
    "bad",
    [
        "chrome-error://chromewebdata/",   # the one measured live
        "about:blank",
        "chrome://new-tab-page",
        "data:text/html,<p>x</p>",
        "blob:https://example.com/9f8e",
        "",                                # no location at all
    ],
)
def test_a_browser_error_location_is_not_recorded(tmp_path, bad):
    """The failed navigation is dropped AND a real page still lands.

    The second half is the control: without it, an emitter that recorded nothing
    would satisfy this test perfectly.
    """
    em = _emitter(tmp_path)
    em.emit_page_state(_record(bad, 1))
    em.emit_page_state(_record(REAL, 2))

    rows = [r for r in _manifest(tmp_path) if r.get("type") == emit.REC_PAGE_STATE]
    locations = [r["location"] for r in rows]

    assert bad not in locations, f"a non-navigable location was recorded: {bad!r}"
    assert locations == [REAL], (
        "the real page state must still be recorded — a guard that drops "
        f"everything is not a fix. got {locations!r}"
    )


def test_the_drop_is_announced(tmp_path, caplog):
    """Silently vanishing states read later as 'the app has nothing there'."""
    em = _emitter(tmp_path)
    with caplog.at_level("WARNING"):
        em.emit_page_state(_record("chrome-error://chromewebdata/", 1))
    assert any("non_navigable_location_skipped" in r.message for r in caplog.records), (
        "the drop must be logged with the offending location, or a genuine "
        "navigation failure becomes undiagnosable"
    )


def test_the_substrate_would_have_refused_what_we_now_drop():
    """CONTROL — proves the dropped values are the ones that broke ingest.

    This pins the emitter's rule to the SUBSTRATE's rule. If they ever diverge —
    the substrate tightening, or this list drifting — a location could pass here
    and still refuse a whole crawl at ingest, which is the exact failure being
    fixed. The check is a local mirror of ``_require_http_url`` because the two
    services cannot import each other.
    """
    from urllib.parse import urlparse

    def substrate_would_refuse(value: str) -> bool:
        parsed = urlparse(value or "")
        return parsed.scheme not in ("http", "https") or not parsed.hostname

    for bad in ("chrome-error://chromewebdata/", "about:blank", "data:text/html,x", ""):
        assert substrate_would_refuse(bad), (
            f"{bad!r} is dropped by the emitter but WOULD be accepted by the "
            "substrate — the emitter is now stricter than the thing it protects"
        )
    assert not substrate_would_refuse(REAL), (
        "the substrate would refuse a normal page URL; the mirror is wrong"
    )
