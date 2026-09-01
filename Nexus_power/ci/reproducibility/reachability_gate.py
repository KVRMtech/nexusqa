#!/usr/bin/env python3
"""Reproducibility gate — reachability of shipped code from the real entrypoint.

Problem this closes
-------------------
A module can sit in the repo, compile, and even pass tests, yet be wired into
*nothing* the running service actually imports — so a clean build from git omits
it (or a hand-patched server runs code git doesn't). That drift is invisible to a
parse-only CI. This gate makes it a hard, generic failure.

What it does
------------
For a service (a source root + one or more entrypoint modules), it statically
follows intra-service imports from the entrypoint and computes the set of modules
that are actually reachable. Any ``.py`` under the service that is NOT reachable is
reported as an orphan (shipped-but-unwired). Exit code is non-zero when a gated
service has orphans.

Design constraints honored
--------------------------
* Pure standard library, static (``ast``) — no code is imported or executed, so it
  is deterministic and safe to run in CI or offline.
* Nothing about this repository is hardcoded: services are auto-discovered from
  their Dockerfiles, entrypoints from the Docker ``CMD``/``ENTRYPOINT``. Exclusions
  are path *conventions* (tests, migrations, …), never module names.
* Over-approximates reachability (a ``from pkg import name`` credits both ``pkg``
  and ``pkg.name``) so a flagged module has genuinely zero inbound references — no
  false orphan from an ambiguous import.

Usage
-----
Single service::

    reachability_gate.py --source-root platform/qe-central --root app.main

Whole repo (auto-discover every Dockerfile-backed Python service)::

    reachability_gate.py --discover .                 # report every service
    reachability_gate.py --discover . --gate 'platform/**' --gate 'engines/qe-explorer'

``--gate`` globs (repeatable) select which services are *blocking*; services that
match none are reported but do not fail the build. With no ``--gate`` every
discovered service is blocking.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field

# Directory *segments* that are never part of the running import graph (test
# suites, migration trees, vendored/build dirs). Convention-based, so it holds for
# any Python service without naming a single business module.
_EXCLUDE_DIR_SEGMENTS: frozenset[str] = frozenset({
    "tests", "test", "migrations", "node_modules", "__pycache__",
    ".venv", "venv", "build", "dist", ".git", "htmlcov",
})
# Segment *prefixes* (e.g. an ``alembic`` / ``alembic_qec`` migration package).
_EXCLUDE_DIR_PREFIXES: tuple[str, ...] = ("alembic",)
# Non-application file basenames (entrypoint-adjacent / tooling / test files).
_EXCLUDE_BASENAMES: frozenset[str] = frozenset({"conftest.py", "setup.py", "__main__.py"})


# A pytest test module — detected by CONTENT, never by name. A live module whose
# name merely starts with ``test_`` (e.g. a ``test_factory`` / ``test_runs`` router
# where "test" is the DOMAIN, not a pytest prefix) must NOT be excluded, or every
# module it imports is falsely orphaned. A real co-located test defines a ``test_``
# function or imports pytest.
_PYTEST_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+test_|^\s*import\s+pytest\b|^\s*from\s+pytest\b", re.M)


def _looks_like_pytest(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return bool(_PYTEST_RE.search(fh.read()))
    except OSError:
        return False


def _is_excluded(rel_path: str, extra_globs: tuple[str, ...]) -> bool:
    """True when ``rel_path`` (relative to the service root, ``/``-separated) is
    conventionally not part of the running application import graph. Test files are
    excluded by DIRECTORY convention here (``tests/``); a ``test_``-named file that
    is NOT under a tests dir is left for the content check in _iter_py to classify,
    so misnamed live code is analysed rather than silently dropped."""
    parts = rel_path.split("/")
    segs, base = parts[:-1], parts[-1]
    if any(s in _EXCLUDE_DIR_SEGMENTS for s in segs):
        return True
    if any(s.startswith(p) for s in segs for p in _EXCLUDE_DIR_PREFIXES):
        return True
    if base in _EXCLUDE_BASENAMES:
        return True
    return any(fnmatch.fnmatch(rel_path, g) for g in extra_globs)


# Kept for the CLI signature; extra user globs flow through ``_is_excluded``.
DEFAULT_EXCLUDES: tuple[str, ...] = ()

# ─────────────────────────── module discovery ───────────────────────────


def _module_of(source_root: str, path: str) -> str:
    """Dotted module name of ``path`` as importable from ``source_root``."""
    rel = os.path.relpath(path, source_root).replace(os.sep, "/")
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = rel.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _iter_py(source_root: str, extra_globs: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for base, dirs, files in os.walk(source_root):
        # prune excluded dirs early (keeps the walk cheap and consistent with
        # _is_excluded's segment rules).
        dirs[:] = [d for d in dirs
                   if d not in _EXCLUDE_DIR_SEGMENTS
                   and not any(d.startswith(p) for p in _EXCLUDE_DIR_PREFIXES)]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, source_root).replace(os.sep, "/")
            if _is_excluded(rel, extra_globs):
                continue
            # A real pytest module (detected by content, not name) is not part of
            # the running import graph. This is what lets a ``test_``-named LIVE
            # module through while still excluding genuine co-located tests.
            if _looks_like_pytest(p):
                continue
            out.append(p)
    return out


def _package_of(module: str, is_pkg: bool) -> str:
    """The package a module lives in (for resolving relative imports)."""
    if is_pkg:
        return module
    return module.rsplit(".", 1)[0] if "." in module else ""


# ─────────────────────────── import extraction ───────────────────────────


def _intra_imports(source: str, module: str, is_pkg: bool, known: set[str]) -> set[str]:
    """Every in-service module ``module`` imports (absolute + relative)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    pkg = _package_of(module, is_pkg)
    targets: set[str] = set()

    def _credit(dotted: str) -> None:
        # credit the module and, for a `from pkg import name`, its submodule too;
        # also every ancestor package (its __init__ runs on import).
        if not dotted:
            return
        for cand in (dotted,):
            if cand in known:
                targets.add(cand)
        # ancestors
        cur = dotted
        while "." in cur:
            cur = cur.rsplit(".", 1)[0]
            if cur in known:
                targets.add(cur)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _credit(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                base_parts = pkg.split(".") if pkg else []
                up = node.level - 1
                base_parts = base_parts[: len(base_parts) - up] if up else base_parts
                base = ".".join(base_parts)
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            _credit(mod)
            # from X import a, b  → credit X.a / X.b when they are modules
            for alias in node.names:
                if mod:
                    _credit(f"{mod}.{alias.name}")
    return targets


# ─────────────────────────── the gate ───────────────────────────


@dataclass
class ServiceReport:
    label: str
    source_root: str
    roots: list[str]
    total: int
    reachable: int
    orphans: list[str] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)
    gated: bool = True


def analyze(source_root: str, roots: list[str], extra_globs: tuple[str, ...],
            label: str) -> ServiceReport:
    files = _iter_py(source_root, extra_globs)
    mod_by_name: dict[str, str] = {}          # module → path
    is_pkg: dict[str, bool] = {}
    for p in files:
        name = _module_of(source_root, p)
        if not name:                          # a .py sitting at the source root itself
            continue
        mod_by_name[name] = p
        is_pkg[name] = os.path.basename(p) == "__init__.py"
    known = set(mod_by_name)

    graph: dict[str, set[str]] = {}
    for name, path in mod_by_name.items():
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            src = ""
        graph[name] = _intra_imports(src, name, is_pkg[name], known)

    missing = [r for r in roots if r not in known]
    seen: set[str] = set()
    stack = [r for r in roots if r in known]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, ()))

    orphans = sorted(n for n in known if n not in seen and n not in roots)
    return ServiceReport(
        label=label, source_root=source_root, roots=roots,
        total=len(known), reachable=len(seen), orphans=orphans,
        missing_roots=missing,
    )


# ─────────────────────────── service auto-discovery ───────────────────────────

_CMD_RE = re.compile(r"^\s*(?:CMD|ENTRYPOINT)\s+(.*)$", re.IGNORECASE)


def _entrypoint_from_dockerfile(dockerfile: str) -> list[str]:
    """Root module(s) from the Docker CMD/ENTRYPOINT, e.g. ``python -m app.main``
    → ``app.main``; ``python main.py`` → ``main``. Empty when not a python start."""
    try:
        with open(dockerfile, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    cmd = ""
    for ln in lines:
        m = _CMD_RE.match(ln)
        if m:
            cmd = m.group(1).strip()
    if not cmd:
        return []
    # JSON-exec form ["python","-m","app.main"] or shell form
    toks: list[str]
    try:
        parsed = json.loads(cmd)
        toks = [str(t) for t in parsed] if isinstance(parsed, list) else cmd.split()
    except json.JSONDecodeError:
        toks = cmd.split()
    if not toks or "python" not in toks[0]:
        return []
    if "-m" in toks:
        return [toks[toks.index("-m") + 1]]
    for t in toks[1:]:
        if t.endswith(".py"):
            return [t[:-3].replace("/", ".")]
    return []


def discover(repo_root: str) -> list[tuple[str, list[str]]]:
    """Every Dockerfile-backed python service → (source_root, [root modules])."""
    found: list[tuple[str, list[str]]] = []
    for base, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules",
                                                "__pycache__", ".venv"}]
        for fn in files:
            if not fn.lower().startswith("dockerfile"):
                continue
            roots = _entrypoint_from_dockerfile(os.path.join(base, fn))
            if roots:
                found.append((base, roots))
    return found


# ─────────────────────────── cli ───────────────────────────


def _print_report(r: ServiceReport) -> None:
    tag = "GATED " if r.gated else "report"
    head = f"[{tag}] {r.label}  ({r.reachable}/{r.total} modules reachable from {', '.join(r.roots)})"
    print(head)
    if r.missing_roots:
        print(f"    ! entrypoint not found: {', '.join(r.missing_roots)}")
    if r.orphans:
        print(f"    ORPHANED (shipped but unreachable — {len(r.orphans)}):")
        for o in r.orphans:
            print(f"      - {o}")
    elif not r.missing_roots:
        print("    ok — no orphaned modules")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-root", help="analyze a single service rooted here")
    ap.add_argument("--root", action="append", default=[],
                    help="entrypoint module(s), e.g. app.main (repeatable)")
    ap.add_argument("--discover", metavar="REPO_ROOT",
                    help="auto-discover every Dockerfile-backed python service")
    ap.add_argument("--gate", action="append", default=[],
                    help="path glob(s) whose services are BLOCKING (repeatable); "
                         "default: every discovered service is blocking")
    ap.add_argument("--exclude", action="append", default=[],
                    help="extra path-glob exclusions (repeatable)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)

    excludes = DEFAULT_EXCLUDES + tuple(args.exclude)
    reports: list[ServiceReport] = []

    if args.source_root:
        if not args.root:
            ap.error("--source-root requires at least one --root")
        reports.append(analyze(os.path.abspath(args.source_root), args.root,
                               excludes, label=args.source_root))
    elif args.discover:
        repo = os.path.abspath(args.discover)
        for src, roots in sorted(discover(repo)):
            label = os.path.relpath(src, repo).replace(os.sep, "/")
            rep = analyze(src, roots, excludes, label=label)
            rep.gated = (not args.gate) or any(
                fnmatch.fnmatch(label, g) or fnmatch.fnmatch(label + "/", g)
                or label.startswith(g.rstrip("*/")) for g in args.gate)
            reports.append(rep)
    else:
        ap.error("supply --source-root or --discover")

    if args.format == "json":
        print(json.dumps([r.__dict__ for r in reports], indent=2))
    else:
        for r in reports:
            _print_report(r)
            print()

    failed = [r for r in reports if r.gated and (r.orphans or r.missing_roots)]
    if failed:
        print(f"REPRODUCIBILITY GATE FAILED: {len(failed)} gated service(s) have "
              f"orphaned modules or a missing entrypoint.", file=sys.stderr)
        return 1
    print("reproducibility gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
