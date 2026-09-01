"""CIRCULAR-DEPENDENCY GATE (M0.3).

Decomposing a god object is exactly the manoeuvre that introduces import
cycles: module B is carved out of A, then needs a name that stayed in A.  The
usual "fix" — a function-local import — hides the cycle from the reader while
leaving the design tangled.

This gate reads the RUNTIME import graph of the ``app`` package straight from
the AST (``if TYPE_CHECKING:`` blocks excluded, since those never execute) and
fails on any cycle.
"""
from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent / "app"


def _runtime_imports(path: Path) -> set[str]:
    """Sibling ``app`` modules imported at RUNTIME by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Drop `if TYPE_CHECKING:` bodies — they are not executed.
    class _Strip(ast.NodeTransformer):
        def visit_If(self, node: ast.If):
            test = node.test
            name = (getattr(test, "id", None)
                    or getattr(getattr(test, "attr", None), "__str__", lambda: None)()
                    or getattr(test, "attr", None))
            if name == "TYPE_CHECKING":
                return node.orelse or None
            return self.generic_visit(node)

    tree = _Strip().visit(tree)

    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:                       # from .x import y
                out.add(node.module.split(".")[0])
            else:                                 # from . import x, y
                out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("app."):
                    out.add(a.name.split(".")[1])
    return out


def _graph() -> dict[str, set[str]]:
    mods = {p.stem for p in _APP.glob("*.py") if p.stem != "__init__"}
    return {m: _runtime_imports(_APP / f"{m}.py") & mods for m in sorted(mods)}


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {m: WHITE for m in graph}
    stack: list[str] = []

    def visit(node: str):
        colour[node] = GREY
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if colour.get(dep) == GREY:
                return stack[stack.index(dep):] + [dep]
            if colour.get(dep) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for m in sorted(graph):
        if colour[m] == WHITE:
            found = visit(m)
            if found:
                return found
    return None


#: EMPTY, and it must stay empty.
#:
#: One cycle predated M0.3: ``forms.py`` reached back into ``crawler.py`` for
#: ``_displayed_values`` through a function-local import, with a comment naming
#: the cycle it was dodging.  A lazy import does not remove a cycle, it only
#: hides it from the reader.  T-DE-06 rehomed that helper into
#: ``state_identity.py``, which both modules now import DOWNWARD, so the cycle
#: is gone rather than deferred.
KNOWN_CYCLES: set[tuple[str, ...]] = set()


def _cycle_key(cycle: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(cycle)))


def test_app_package_has_no_import_cycles():
    graph = _graph()
    cycle = _find_cycle(graph)
    if cycle is None:
        return
    assert _cycle_key(cycle) in KNOWN_CYCLES, (
        "runtime import cycle: " + " -> ".join(cycle))


def test_extracted_modules_do_not_import_crawler():
    """The extracted leaves must not depend back on the module they left.

    This is the ratchet that keeps the decomposition a decomposition: if
    ``budget`` or ``frontier`` ever needs something from ``crawler``, that
    something belongs in the leaf, not behind a back-reference.
    """
    graph = _graph()
    for leaf in ("budget", "frontier", "protocols", "playwright_port"):
        if leaf in graph:
            assert "crawler" not in graph[leaf], (
                f"{leaf}.py imports crawler.py — the extraction leaked")


def test_browser_layer_does_not_import_http_layer():
    graph = _graph()
    assert "main" not in graph.get("playwright_port", set()), \
        "playwright_port.py imports main.py — the layering inversion is back"
    assert "main" not in graph.get("crawler", set()), \
        "crawler.py imports main.py — the layering inversion is back"
