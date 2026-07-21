"""Tests for the reproducibility reachability gate.

These build throwaway package trees on disk and assert the gate's judgments, so
the gate that guards the codebase is itself guarded — no fixtures reach into the
real repo, nothing is hardcoded about it.
"""
from __future__ import annotations

import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reachability_gate as rg  # noqa: E402


def _write(root: str, rel: str, body: str = "") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(body))


def test_flags_orphan_keeps_wired(tmp_path):
    root = str(tmp_path)
    _write(root, "main.py", "from app import wired\n")
    _write(root, "app/__init__.py")
    _write(root, "app/wired.py", "VALUE = 1\n")
    _write(root, "app/orphan.py", "VALUE = 2\n")          # imported by nobody

    rep = rg.analyze(root, ["main"], extra_globs=(), label="svc")

    assert "app.orphan" in rep.orphans
    assert "app.wired" not in rep.orphans
    assert "main" not in rep.orphans          # the entrypoint is never an orphan
    assert not rep.missing_roots


def test_transitive_and_relative_imports_are_reachable(tmp_path):
    root = str(tmp_path)
    _write(root, "app/__init__.py")
    _write(root, "app/main.py", "from .a import thing\n")
    _write(root, "app/a.py", "from .b import helper\nthing = 1\n")   # relative
    _write(root, "app/b.py", "helper = 2\n")
    _write(root, "app/never.py", "x = 3\n")

    rep = rg.analyze(root, ["app.main"], extra_globs=(), label="svc")

    assert rep.orphans == ["app.never"]       # a + b reachable transitively


def test_real_tests_and_migrations_are_excluded(tmp_path):
    root = str(tmp_path)
    _write(root, "main.py", "from app import wired\n")
    _write(root, "app/__init__.py")
    _write(root, "app/wired.py", "V = 1\n")
    _write(root, "app/tests/test_wired.py", "def test_x(): pass\n")     # test by DIR
    _write(root, "app/services/foo_test.py", "import pytest\n")         # test by CONTENT
    _write(root, "alembic_x/versions/0001_init.py", "def up(): pass\n")  # migration

    rep = rg.analyze(root, ["main"], extra_globs=(), label="svc")

    assert rep.orphans == []                  # every real test / migration excluded


def test_test_prefixed_live_module_is_not_falsely_orphaned(tmp_path):
    """Regression: a LIVE module whose name starts with ``test_`` (e.g. a
    test-factory / test-runs router, where "test" is the DOMAIN, not a pytest
    prefix) must be analysed as real code, not silently dropped. Dropping it
    orphaned everything it imported and inflated the orphan count."""
    root = str(tmp_path)
    _write(root, "main.py", "from app.routers import test_factory\n")
    _write(root, "app/__init__.py")
    _write(root, "app/routers/__init__.py")
    # a live router named test_* — NO pytest markers — wired from main
    _write(root, "app/routers/test_factory.py", "from app.services import engine\nrouter = 1\n")
    _write(root, "app/services/__init__.py")
    _write(root, "app/services/engine.py", "E = 1\n")
    # a genuinely dead test_*-named live module (no pytest, imported by nobody)
    _write(root, "app/routers/test_orphan.py", "router = 2\n")

    rep = rg.analyze(root, ["main"], extra_globs=(), label="svc")

    # the wired router and its transitive import are reachable, not orphaned…
    assert "app.routers.test_factory" not in rep.orphans
    assert "app.services.engine" not in rep.orphans
    # …but a truly-unwired test_*-named live module IS correctly flagged
    assert "app.routers.test_orphan" in rep.orphans


def test_missing_entrypoint_is_reported(tmp_path):
    root = str(tmp_path)
    _write(root, "app/__init__.py")
    _write(root, "app/thing.py", "V = 1\n")

    rep = rg.analyze(root, ["app.nonexistent"], extra_globs=(), label="svc")

    assert rep.missing_roots == ["app.nonexistent"]


def test_entrypoint_extraction_from_dockerfile(tmp_path):
    df1 = tmp_path / "Dockerfile.a"
    df1.write_text('CMD ["python", "-m", "app.main"]\n', encoding="utf-8")
    df2 = tmp_path / "Dockerfile.b"
    df2.write_text('CMD ["python", "main.py"]\n', encoding="utf-8")
    df3 = tmp_path / "Dockerfile.c"
    df3.write_text('CMD ["node", "server.js"]\n', encoding="utf-8")   # not python

    assert rg._entrypoint_from_dockerfile(str(df1)) == ["app.main"]
    assert rg._entrypoint_from_dockerfile(str(df2)) == ["main"]
    assert rg._entrypoint_from_dockerfile(str(df3)) == []
