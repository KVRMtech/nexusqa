"""Regression — server-side run base URL uses the FULL host, never the
registrable domain.

The founder-visible 404 (2026-07-25): certification / diagnosis / auto-heal
runs derived their base URL from ``canonical_host`` — the registrable domain
'sslip.io' for host 'vkpowerlife.35-186-147-245.sslip.io' — producing
``https://sslip.io`` → nginx 404. Every such run navigated to a dead host and
died at the first field. A diagnosis run's 404 screenshot proved it.

``_recorded_origin`` is extracted from the router source (the module imports
heavy deps) and pinned here.

Run from Nexus_power/platform/api:
    python -m pytest tests/test_recorded_origin.py -q
"""
from __future__ import annotations

import os
import re
import types

_SRC = open(
    os.path.join(os.path.dirname(__file__), "..", "app", "routers", "test_factory.py"),
    encoding="utf-8").read()
_m = re.search(r"\ndef _recorded_origin\(.*?\n(?=\ndef |\nasync def |\nclass )", _SRC, re.S)
assert _m, "_recorded_origin not found in the router"
_ns: dict = {}
exec(compile(_m.group(0), "test_factory.py::_recorded_origin", "exec"), _ns)  # noqa: S102
_recorded_origin = _ns["_recorded_origin"]


def _v(url_host="", canonical_host=""):
    return types.SimpleNamespace(url_host=url_host, canonical_host=canonical_host)


def test_the_exact_sslip_bug_uses_full_host_not_registrable_domain():
    visits = [_v(url_host="vkpowerlife.35-186-147-245.sslip.io",
                 canonical_host="sslip.io")]
    assert _recorded_origin(visits) == "https://vkpowerlife.35-186-147-245.sslip.io"
    # the bug would have produced this dead origin:
    assert _recorded_origin(visits) != "https://sslip.io"


def test_prefers_url_host_over_canonical_host_always():
    visits = [_v(url_host="app.example.com", canonical_host="example.com")]
    assert _recorded_origin(visits) == "https://app.example.com"


def test_dotless_internal_host_is_http():
    assert _recorded_origin([_v(url_host="summitlife-app")]) == "http://summitlife-app"


def test_falls_back_to_canonical_only_when_no_url_host():
    assert _recorded_origin([_v(url_host="", canonical_host="fallback.example")]) \
        == "https://fallback.example"


def test_first_visit_with_a_host_wins():
    visits = [_v(url_host=""), _v(url_host="real.example.com")]
    assert _recorded_origin(visits) == "https://real.example.com"


def test_no_host_returns_empty():
    assert _recorded_origin([]) == ""
    assert _recorded_origin([_v()]) == ""
