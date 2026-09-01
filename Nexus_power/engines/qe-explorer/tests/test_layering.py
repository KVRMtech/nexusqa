"""LAYER ISOLATION GATE (M0.3 / T-DE-05).

The browser layer must be importable WITHOUT the HTTP layer.  Before the
decomposition, ``from app.main import PlaywrightBrowserPort`` pulled in 158
HTTP-layer modules and constructed a live FastAPI application as an import
side effect: the only way to reach the browser adapter was through the web
server that consumes it.

These tests are the ratchet that keeps the arrow pointing the right way.  They
run the import in a SUBPROCESS because ``sys.modules`` is process-global — an
in-process check would pass simply because some earlier test already imported
FastAPI.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SERVICE_ROOT = str(Path(__file__).resolve().parent.parent)


def _probe(body: str) -> str:
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_SERVICE_ROOT!r})
        {body}
    """)
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


def test_browser_layer_imports_without_fastapi():
    """The headline invariant: no web framework is loaded by the browser layer."""
    out = _probe("""
        from app.playwright_port import PlaywrightBrowserPort
        http = [m for m in sys.modules
                if m.split(".")[0] in {"fastapi", "starlette", "pydantic", "httpx"}]
        print(",".join(sorted(http)) or "NONE")
    """)
    assert out == "NONE", f"browser layer dragged in HTTP modules: {out}"


def test_crawler_imports_without_fastapi():
    """The crawler is a browser-layer consumer too — same invariant."""
    out = _probe("""
        import app.crawler  # noqa
        http = [m for m in sys.modules if m.split(".")[0] in {"fastapi", "starlette"}]
        print(",".join(sorted(http)) or "NONE")
    """)
    assert out == "NONE", f"crawler dragged in HTTP modules: {out}"


def test_extracted_modules_import_standalone():
    """Every extracted module must stand on its own feet."""
    out = _probe("""
        import importlib
        mods = ["app.protocols", "app.budget", "app.frontier", "app.playwright_port"]
        for m in mods:
            importlib.import_module(m)
        print("OK")
    """)
    assert out == "OK"


def test_protocols_module_has_no_runtime_app_dependencies():
    """protocols.py declares contracts; importing it must cost nothing, so it
    can never participate in an import cycle."""
    out = _probe("""
        import app.protocols  # noqa
        pulled = sorted(m for m in sys.modules
                        if m.startswith("app.") and m != "app.protocols")
        print(",".join(pulled) or "NONE")
    """)
    assert out == "NONE", f"protocols.py pulled in app modules: {out}"


def test_http_layer_still_consumes_the_browser_layer():
    """The arrow must EXIST, just point the right way: main.py imports the port."""
    out = _probe("""
        import app.main as m
        from app.playwright_port import PlaywrightBrowserPort
        print(m.PlaywrightBrowserPort is PlaywrightBrowserPort)
    """)
    assert out == "True"
