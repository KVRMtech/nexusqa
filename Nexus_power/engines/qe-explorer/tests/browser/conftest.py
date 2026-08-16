"""Fixtures for the Browser Test Harness (M0.2).

Two execution lanes are set up here, both session-scoped so a 13-fixture suite
pays for one node install probe and ONE Chromium launch rather than 13.

The Playwright lane deliberately manages its own event loop in a *sync* fixture
instead of relying on ``pytest-asyncio``'s function-scoped loop: the browser,
the context and the production port must all live on the same loop for the whole
session, and a per-test loop would close the browser out from under the next
test.  Tests therefore call ``pw.run(coro)``.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, TypeVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H  # noqa: E402

T = TypeVar("T")


def pytest_configure(config: pytest.Config) -> None:
    for marker, desc in (
        ("browser", "Browser Test Harness (M0.2) — executes real capture JavaScript."),
        ("jsdom", "Executes the production injected JS inside jsdom."),
        ("playwright", "Executes the production injected JS inside real Chromium."),
        ("characterization", "Golden-snapshot test — fails on any behavioural change."),
        ("bug_repro", "Describes CORRECT browser behaviour that Capture does not yet implement."),
        ("proving_ground", "Crawls a real proving-ground application end to end."),
    ):
        config.addinivalue_line("markers", f"{marker}: {desc}")


# ─── Shared: the fixture origins ─────────────────────────────────────────────

@pytest.fixture(scope="session")
def fixture_server() -> Any:
    srv = H.FixtureServer().start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def snippet_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The production snippets, written verbatim to disk for the node runner."""
    out_dir = tmp_path_factory.mktemp("production_js")
    return {name: H.snippet_to_tempfile(name, out_dir)
            for name in ("INVENTORY_JS", "OPAQUE_JS", "DISPLAYED_VALUES_JS")}


# ─── Lane 1: jsdom ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def jsdom(snippet_files: dict[str, Path]) -> Any:
    ok, why = H.jsdom_available()
    if not ok:
        pytest.skip(f"jsdom lane unavailable: {why}")

    class _Lane:
        # Node process startup dominates this lane, and several tests read the
        # SAME (url, snippet) pair. Memoising is sound because the lane is
        # deterministic — a property proven independently by
        # test_jsdom_execution_is_deterministic, which calls `fresh()` to bypass
        # this cache and would fail if the assumption were false.
        def __init__(self) -> None:
            self._memo: dict[tuple[str, str], H.JsdomResult] = {}

        def fresh(self, url: str, snippet: str = "INVENTORY_JS") -> H.JsdomResult:
            return H.run_jsdom(url, snippet, snippet_files[snippet])

        def run(self, url: str, snippet: str = "INVENTORY_JS") -> H.JsdomResult:
            key = (url, snippet)
            if key not in self._memo:
                self._memo[key] = self.fresh(url, snippet)
            return self._memo[key]

    return _Lane()


# ─── Lane 2: real Chromium, through the PRODUCTION port ──────────────────────

class PlaywrightLane:
    """A session-lived Chromium plus the production ``PlaywrightBrowserPort``."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        # Same reasoning as the jsdom lane: a full production goto + settle is
        # the expensive step and several tests read the same page. Determinism is
        # proven independently by test_chromium_execution_is_deterministic, which
        # uses `collect_fresh()` to bypass this cache.
        self._memo: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def run(self, coro: Awaitable[T]) -> T:
        return self._loop.run_until_complete(coro)

    def start(self) -> None:
        from playwright.async_api import async_playwright

        async def _boot():
            self._pw = await async_playwright().start()
            # Mirrors the production launch in app.main._run_job: headless
            # Chromium with service workers blocked. No egress proxy — the
            # fixture origins are local, and the guard under test here is the
            # capture path, not the network gate.
            self._browser = await self._pw.chromium.launch(headless=True)
            self.context = await self._browser.new_context(service_workers="block")
            self.context.set_default_timeout(15000)
            self.page = await self.context.new_page()

        self.run(_boot())

    def collect_fresh(self, url: str, what: str = "controls") -> list[dict[str, Any]]:
        return self.run(H.collect_via_production_port(
            self.page, self.context, url, what=what))

    def collect(self, url: str, what: str = "controls") -> list[dict[str, Any]]:
        key = (url, what)
        if key not in self._memo:
            self._memo[key] = self.collect_fresh(url, what)
        return self._memo[key]

    def stop(self) -> None:
        async def _shutdown():
            for closer in (self.context, self._browser):
                if closer is not None:
                    try:
                        await closer.close()
                    except Exception:
                        pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass

        try:
            self.run(_shutdown())
        finally:
            self._loop.close()


@pytest.fixture(scope="session")
def pw() -> Any:
    ok, why = H.playwright_available()
    if not ok:
        pytest.skip(f"playwright lane unavailable: {why}")
    lane = PlaywrightLane()
    lane.start()
    yield lane
    lane.stop()


# ─── Parametrisation helpers ─────────────────────────────────────────────────

def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise any test taking ``fixture_name`` over the whole library."""
    if "fixture_name" in metafunc.fixturenames:
        names = H.fixture_names()
        metafunc.parametrize("fixture_name", names, ids=names)


# ─── M0.1 — an execution LANE may never silently skip in CI ──────────────────
# The same failure this suite's DB-side sibling closes with QEC_REQUIRE_DB
# (platform/qe-central/tests/conftest.py), applied to the two browser lanes:
# node is missing (or `npm ci` never ran) and the jsdom lane skips; Chromium was
# never installed and the Playwright lane skips — and the job goes GREEN having
# executed none of the production capture JavaScript it exists to prove.
#
# On a laptop those skips are the right answer, so the gate is two-state and OFF
# by default. With QEC_REQUIRE_BROWSER_LANES=1 (CI sets it) a lane that could not
# start FAILS the session instead, with the reason named.

def _lanes_required() -> bool:
    return os.environ.get(
        "QEC_REQUIRE_BROWSER_LANES", "").strip().lower() in ("1", "true", "yes", "on")


#: Substrings that identify a skip as "the lane could not start", as opposed to a
#: legitimate skip (a fixture that declares itself out of scope for one lane).
#: These are the exact reasons the `jsdom` / `pw` fixtures above raise.
_LANE_SKIP_SIGNATURES = ("jsdom lane unavailable", "playwright lane unavailable")

_lane_skips: list[tuple[str, str]] = []


def pytest_runtest_logreport(report: Any) -> None:
    """Record every skip whose reason says a lane could not start."""
    if report.when != "setup" or report.outcome != "skipped":
        return
    if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
        reason = str(report.longrepr[2])
    else:                                    # pragma: no cover — defensive
        reason = str(report.longrepr)
    if any(sig in reason for sig in _LANE_SKIP_SIGNATURES):
        _lane_skips.append((report.nodeid, reason.replace("Skipped: ", "", 1)))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    """Fail the session when a required execution lane never ran."""
    if not _lanes_required() or not _lane_skips:
        return
    # One line per lane, not per test: a missing Chromium skips every
    # Playwright-lane test, and 300 identical entries buries the one fact.
    by_reason: dict[str, int] = {}
    for _nodeid, reason in _lane_skips:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    lines = "\n".join(f"    {n} test(s) — {reason}" for reason, n in sorted(by_reason.items()))
    print(
        "\n"
        "═══════════════════════════════════════════════════════════════════\n"
        f"QEC_REQUIRE_BROWSER_LANES is set, but {len(_lane_skips)} test(s) SKIPPED\n"
        "because an execution lane could not start. A lane that never ran has\n"
        "proven nothing about the production capture JavaScript, so this is a\n"
        "FAILURE:\n"
        f"{lines}\n"
        "═══════════════════════════════════════════════════════════════════",
        file=sys.stderr,
    )
    session.exitstatus = 1
