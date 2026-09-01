"""Tests for the deploy-verification gate (content-identical, line-ending safe)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_deployment as vd  # noqa: E402


def _w(root: str, rel: str, data: bytes) -> None:
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)


def test_identical_is_pass(tmp_path):
    g = str(tmp_path / "git"); d = str(tmp_path / "dep")
    _w(g, "app/main.py", b"x = 1\n"); _w(d, "app/main.py", b"x = 1\n")
    assert vd.compare(g, d, (".py",)) == ([], [], [])


def test_line_endings_only_is_pass(tmp_path):
    # the exact trap: git checkout is CRLF, container is LF — must NOT be flagged.
    g = str(tmp_path / "git"); d = str(tmp_path / "dep")
    _w(g, "app/a.py", b"line1\r\nline2\r\n")
    _w(d, "app/a.py", b"line1\nline2\n")
    only_g, only_d, differ = vd.compare(g, d, (".py",))
    assert (only_g, only_d, differ) == ([], [], [])


def test_real_content_diff_flagged(tmp_path):
    g = str(tmp_path / "git"); d = str(tmp_path / "dep")
    _w(g, "app/a.py", b"VALUE = 1\n")
    _w(d, "app/a.py", b"VALUE = 2\n")          # genuinely different
    only_g, only_d, differ = vd.compare(g, d, (".py",))
    assert differ == ["app/a.py"] and not only_g and not only_d


def test_missing_and_extra_flagged(tmp_path):
    g = str(tmp_path / "git"); d = str(tmp_path / "dep")
    _w(g, "app/only_git.py", b"a = 1\n")
    _w(d, "app/only_dep.py", b"b = 1\n")       # code that runs but isn't in git
    only_g, only_d, differ = vd.compare(g, d, (".py",))
    assert only_g == ["app/only_git.py"]
    assert only_d == ["app/only_dep.py"]


def test_pycache_ignored(tmp_path):
    g = str(tmp_path / "git"); d = str(tmp_path / "dep")
    _w(g, "app/a.py", b"a = 1\n")
    _w(d, "app/a.py", b"a = 1\n")
    _w(d, "app/__pycache__/a.cpython-311.pyc", b"\x00\x01garbage")
    assert vd.compare(g, d, (".py",)) == ([], [], [])
