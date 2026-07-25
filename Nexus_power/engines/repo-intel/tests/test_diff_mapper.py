"""CODE P4 — changed-files → changed-atoms mapper (pure, no repo/clone/DB).

The safety property under test: never-green-wash. A changed file that maps to NO
atom sets fail_safe_to_full (the caller must run the FULL suite), so a config /
template / dependency edit can never masquerade as "nothing changed".
"""
from __future__ import annotations

import sys
from pathlib import Path

# Same bootstrap as every sibling suite (test_pipeline_logic.py:9): the repo-intel
# `app` package is imported by path, not pip-installed — without this line the file
# only collects when pytest happens to run from engines/repo-intel/ (why it passed
# locally and died in qec-ci, which runs from the Nexus_power root).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.diff.mapper import map_changed_atoms  # noqa: E402


def _atom(atom_id, kind, path, value=None):
    return {"atom_id": atom_id, "kind": kind, "provenance_path": path, "value": value or {}}


ATOMS = [
    _atom("a1", "route", "src/routes/quote.py", {"path_pattern": "/quote"}),
    _atom("a2", "api_endpoint", "src/api/bind.py", {"path": "/api/bind"}),
    _atom("a3", "form_field", "src/routes/quote.py", {"name": "age"}),
    _atom("a4", "route", "src/routes/login.py", {"path_pattern": "/login"}),
]


def test_changed_file_maps_to_its_atoms():
    out = map_changed_atoms(["src/routes/quote.py"], ATOMS)
    ids = {a["atom_id"] for a in out["changed_atoms"]}
    assert ids == {"a1", "a3"}                     # both atoms from quote.py
    assert out["affected_routes"] == ["/quote"]    # only the surface atom
    assert out["fail_safe_to_full"] is False       # every changed file mapped


def test_multiple_files_union_of_atoms_and_routes():
    out = map_changed_atoms(["src/routes/quote.py", "src/api/bind.py"], ATOMS)
    assert {a["atom_id"] for a in out["changed_atoms"]} == {"a1", "a3", "a2"}
    assert out["affected_routes"] == ["/api/bind", "/quote"]


def test_unmapped_file_forces_full_suite():
    # a changed file with NO atom (e.g. a config) must fail-safe to full
    out = map_changed_atoms(["deploy/nginx.conf", "src/routes/quote.py"], ATOMS)
    assert out["unmapped_files"] == ["deploy/nginx.conf"]
    assert out["fail_safe_to_full"] is True
    assert {a["atom_id"] for a in out["changed_atoms"]} == {"a1", "a3"}  # quote atoms still surfaced


def test_empty_diff_is_not_fail_safe():
    out = map_changed_atoms([], ATOMS)
    assert out["changed_atoms"] == [] and out["affected_routes"] == []
    assert out["fail_safe_to_full"] is False  # genuinely nothing changed
    assert out["unmapped_files"] == []


def test_unchanged_file_atoms_excluded():
    out = map_changed_atoms(["src/routes/quote.py"], ATOMS)
    assert "a4" not in {a["atom_id"] for a in out["changed_atoms"]}  # login.py untouched


def test_path_normalization():
    out = map_changed_atoms(["./src/routes/quote.py", "src\\api\\bind.py"], ATOMS)
    assert {a["atom_id"] for a in out["changed_atoms"]} == {"a1", "a3", "a2"}
    assert out["fail_safe_to_full"] is False


def test_works_with_object_atoms_not_just_dicts():
    class A:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    objs = [A(atom_id="x1", kind="route", provenance_path="app/main.py",
              value={"path_pattern": "/home"})]
    out = map_changed_atoms(["app/main.py"], objs)
    assert out["changed_atoms"][0]["atom_id"] == "x1"
    assert out["affected_routes"] == ["/home"]


def test_stack_supported_always_true_from_the_mapper():
    # the mapper itself always supports the stack; stack_supported=false is the
    # CALLER's fail-safe when a diff cannot be produced at all (upstream).
    assert map_changed_atoms(["x"], [])["stack_supported"] is True
