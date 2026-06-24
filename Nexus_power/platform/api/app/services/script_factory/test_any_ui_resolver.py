"""Standalone never-green-wash tests for the any-UI tier (TIER 3 + L6/L7).

Run inside the platform-api container:
    cd /app/service && python -c "import app.services.script_factory.test_any_ui_resolver as t; \
        [getattr(t,n)() for n in dir(t) if n.startswith('test_')]; print('any-UI OK')"
"""
from . import any_ui_resolver as a


def test_detect_routing():
    assert a.detect_any_ui({"label": "X"}, "Cannot pierce closed shadow root")["kind"] == "open_shadow_shim"
    v = a.detect_any_ui({"label": "Brush", "value": "Red"}, "target is a <canvas> with empty a11y tree")
    assert v and v["kind"] == "visual_propose"
    assert a.detect_any_ui({"label": "X"}, "locator.fill timeout") is None


def test_vlm_default_off():
    # No NEXUS_VLM_GROUND_URL -> tier inert -> no candidates.
    assert a.vlm_enabled() is False
    assert a.propose_candidates(b"fake-png", "do X") == []


def test_visual_propose_refuses_without_vlm():
    """The cardinal guarantee: with no VLM/candidates, a non-DOM heal REFUSES (throws),
    it NEVER clicks a blind coordinate and NEVER green-washes."""
    js = "\n".join(a.emit_any_ui_lines({"label": "Brush", "value": "Red"},
                                       {"kind": "visual_propose", "label": "Brush", "value": "Red"}))
    assert "throw new Error" in js
    assert "dispatchMouseEvent" not in js  # no blind click
    assert "escalat" in js.lower()


def test_open_shadow_preamble():
    sh = "\n".join(a.emit_open_shadow_preamble())
    assert "addInitScript" in sh and "mode: 'open'" in sh and "attachShadow" in sh


def test_unknown_kind_byte_identical():
    assert a.emit_any_ui_lines({"label": "X"}, {"kind": "nonsense"}) == []
