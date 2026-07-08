"""QE-Central VKPower pin suite — extension E2 (composer live_crawl guard).

Pins the §4-E2 contract in ``services/storyboard/composer.py``:

* T8 (table-driven) — for ``canonical_artifacts.source_type`` in
  (None, '', 'video', 'upload', 'LIVE_CRAWL'): the effective vision flags equal
  the resolved surface flags EXACTLY (byte-identical decision path for every
  video/upload artifact; the guard keys on the lowercase ``'live_crawl'``
  literal only).
* ``'live_crawl'`` — both ``storyboard_on`` and ``pages_forms_on`` forced
  False regardless of the resolved surfaces (anti-clobber Belt-2), with the
  ``composer.live_crawl_vision_skipped`` log event emitted.
* SELECT raises — flags untouched (fail-open: any read error must leave video
  behavior byte-identical).
* Wiring pin — ``_maybe_derive`` invokes the guard immediately after surface
  resolution and before the first vision-extractor gate, so no extractor can
  run ahead of the guard.

No DB / no LLM: the session double answers (or fails) the single
``source_type`` SELECT. Run from Nexus_power/platform/api:
    python -m pytest tests/test_qec_contract_e2_composer.py -q
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys

import pytest

_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SDK_DIR = os.path.abspath(os.path.join(_API_DIR, "..", "..", "sdk", "nexus-sdk"))
for _p in (_API_DIR, _SDK_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from app.services.storyboard import composer  # noqa: E402

_ARTIFACT = "art-e2"
_TENANT = "t-contract"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    """Answers the guard's single source_type SELECT (or raises)."""

    def __init__(self, source_type=None, raise_exc: Exception | None = None):
        self.source_type = source_type
        self.raise_exc = raise_exc
        self.calls = 0

    async def execute(self, stmt, params=None):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResult(self.source_type)


def _run_guard(session, storyboard_on: bool, pages_forms_on: bool) -> tuple[bool, bool]:
    return asyncio.run(composer._live_crawl_vision_guard(
        session,
        artifact_id=_ARTIFACT,
        tenant_id=_TENANT,
        storyboard_on=storyboard_on,
        pages_forms_on=pages_forms_on,
    ))


# ── T8: non-live_crawl source types leave the resolved surfaces untouched ────


@pytest.mark.parametrize("source_type", [None, "", "video", "upload", "LIVE_CRAWL"])
@pytest.mark.parametrize("flags", [(True, True), (True, False), (False, True), (False, False)])
def test_t8_non_live_crawl_flags_equal_resolved_surfaces(source_type, flags):
    session = _FakeSession(source_type=source_type)
    out = _run_guard(session, *flags)
    assert out == flags, (
        f"source_type={source_type!r} must leave the resolved surfaces "
        f"byte-identical (got {out}, expected {flags})")
    assert session.calls == 1


# ── T8: 'live_crawl' forces both vision surfaces OFF ─────────────────────────


@pytest.mark.parametrize("flags", [(True, True), (True, False), (False, True), (False, False)])
def test_t8_live_crawl_forces_both_vision_surfaces_off(flags, caplog):
    session = _FakeSession(source_type="live_crawl")
    with caplog.at_level(logging.INFO, logger=composer.logger.name):
        out = _run_guard(session, *flags)
    assert out == (False, False)
    assert any(r.getMessage() == "composer.live_crawl_vision_skipped"
               for r in caplog.records), \
        "the guard must log composer.live_crawl_vision_skipped when it fires"


# ── T8: SELECT raises → flags untouched (fail-open) ──────────────────────────


@pytest.mark.parametrize("exc", [RuntimeError("db down"), ValueError("bad row"),
                                 TimeoutError("query timeout")])
@pytest.mark.parametrize("flags", [(True, True), (False, True)])
def test_t8_select_failure_leaves_flags_untouched(exc, flags):
    session = _FakeSession(raise_exc=exc)
    out = _run_guard(session, *flags)
    assert out == flags, "any exception reading source_type must fail OPEN"


# ── Wiring pin: the guard runs right after surface resolution ────────────────


def test_guard_wired_into_maybe_derive_before_vision_gates():
    src = inspect.getsource(composer.StoryboardComposer._maybe_derive)
    assert "_live_crawl_vision_guard(" in src, \
        "_maybe_derive must invoke the E2 guard"
    resolution = src.index('pages_forms_on = sf.get("pages_forms", True)')
    guard = src.index("_live_crawl_vision_guard(")
    first_vision_gate = src.index("_needs_page_visit_extractor(")
    assert resolution < guard < first_vision_gate, (
        "the guard must run immediately after surface resolution and before "
        "the first vision-extractor freshness gate")
    # Both flags must be reassigned from the guard (not merely consulted).
    assert "storyboard_on, pages_forms_on = await _live_crawl_vision_guard(" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
