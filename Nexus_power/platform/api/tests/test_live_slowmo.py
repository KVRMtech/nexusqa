"""A live run exists to be WATCHED — it must not finish faster than a human can look.

MEASURED on a real client run: the headed suite drew on the display for about five
seconds and then went black. Per-test timings from that run were 1.1s, 969ms, 614ms.
By the time the operator clicked a test, opened /live/vnc.html and noVNC completed
its handshake, the browser had exited — and x11vnc kept serving the now-empty
desktop, which is indistinguishable from a broken stream.

Framebuffer evidence (RFB read straight off :5900):

    before run        1 colour              blank
    t=5s  during run  30 colours, 10 chrome THE BROWSER IS DRAWING
    t=10s             1 colour,  0 chrome   blank

The headless verdict path must keep its exact timing: slowing a client-facing run
would change the very thing the product certifies.
"""
from app.services.script_factory.compiler import _playwright_config_param


def test_a_headed_live_run_is_slowed_enough_to_watch():
    cfg = _playwright_config_param(["chromium"], headed=True, workers=1)
    assert "slowMo" in cfg
    assert "250" in cfg
    assert "headless: false" in cfg


def test_a_headless_verdict_run_is_NOT_slowed():
    """The client-facing result must be timed exactly as before — a slowed verdict
    run would change what the product certifies."""
    cfg = _playwright_config_param(["chromium"], headed=False)
    assert "slowMo: Number(process.env.NEXUS_SLOWMO || 0)" in cfg
    assert "headless: true" in cfg


def test_the_operator_can_override_the_pace_per_run():
    cfg = _playwright_config_param(["chromium"], headed=True)
    assert "process.env.NEXUS_SLOWMO" in cfg


def test_no_placeholder_survives_into_the_generated_config():
    """An unreplaced __SLOWMO__ would be a TypeScript syntax error and every run
    would die at config load."""
    for headed in (True, False):
        cfg = _playwright_config_param(["chromium"], headed=headed, workers=1, retries=2)
        assert "__" not in cfg, cfg[cfg.index("__"):cfg.index("__") + 40]


def test_the_config_still_parses_as_the_shape_playwright_expects():
    cfg = _playwright_config_param(["chromium", "firefox"], headed=True, workers=1)
    assert "launchOptions: {" in cfg
    # Exactly ONE top-level use block (each browser project has its own inline
    # `use: { ...devices[...] }`, which is normal and must not be miscounted).
    assert cfg.count("use: {\n") == 1
    # slowMo lives in launchOptions inside that top-level block, before `projects`
    i_use, i_slow, i_proj = cfg.index("use: {\n"), cfg.index("slowMo"), cfg.index("projects: [")
    assert i_use < i_slow < i_proj
