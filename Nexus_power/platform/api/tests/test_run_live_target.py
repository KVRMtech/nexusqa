"""A live run must know WHERE to go.

Clicking a test case in Test Studio opened the noVNC viewer on a blank screen. The
plumbing was fine — the browser's WebSocket got 101 and ~74KB of framebuffer — and
the headed browser really did launch. It just had nowhere to navigate:

    POST .../playwright/run-live  ->  {"status":"running","scripts":12,"target":""}

`playwright_run_live` took base_url from the request body / env_context and, alone
among the dispatch paths, had NO fallback to the origin the crawl recorded. With
NEXUS_BASE_URL empty, the compiled spec's relative `page.goto('/portal/claims/new')`
has no origin to resolve against: Playwright launches the headed browser, fails the
first navigation, and relaunches — a browser flickering behind an empty display.
"""
import re

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT_DEF = chr(10) + "async def "


def _live_handler() -> str:
    """The playwright_run_live function body, located by NAME — the route string
    also appears on the /run-live/status route and in the returned live_url."""
    i = _ROUTER.index("async def playwright_run_live(")
    nxt = _ROUTER.find(_NEXT_DEF, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


def test_the_live_run_falls_back_to_the_recorded_origin():
    seg = _live_handler()
    assert "_recorded_origin(visits)" in seg, \
        "run-live has no origin fallback — NEXUS_BASE_URL will be empty"


def test_the_fallback_runs_AFTER_the_explicit_sources_so_precedence_is_kept():
    """A caller-supplied address, and an environment profile's address, must both
    still win — the fallback is a last resort, not an override."""
    seg = _live_handler()
    i_body = seg.index('base_url = (body.base_url or "").strip()')
    i_env = seg.index('body.env_context.get("base_url")')
    i_fallback = seg.index("_recorded_origin(visits)")
    assert i_body < i_env < i_fallback


def test_the_fallback_only_fires_when_nothing_else_supplied_one():
    seg = _live_handler()
    m = re.search(r"if not base_url:\s*\n\s*base_url = _recorded_origin\(visits\)", seg)
    assert m, "the fallback must be guarded by `if not base_url:`"


def test_visits_are_loaded_before_the_fallback_uses_them():
    seg = _live_handler()
    assert seg.index("_load_current_pages_and_actions") < seg.index("_recorded_origin(visits)")


def test_every_other_dispatch_path_already_had_this_fallback():
    """Pins that run-live was the odd one out — so a future path that forgets it is
    visibly inconsistent with the rest."""
    assert _ROUTER.count("_recorded_origin(visits)") >= 4


def test_the_target_reported_back_is_the_resolved_one():
    """The response's `target` is what an operator reads to see where a run went; it
    must be the resolved URL, not the raw request field."""
    seg = _live_handler()
    assert '"target": base_url' in seg
