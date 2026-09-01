"""Front-end route extractor → ``route`` atoms.

Covers the four route conventions the design names:

* **React Router** v6 — ``<Route path=.. element=..>`` JSX AND route objects
  (``createBrowserRouter`` / ``useRoutes`` arrays of ``{ path, element }``).
* **Next.js / Remix file-convention** — ``pages/`` + ``app/`` (Next) and
  ``app/routes/`` (Remix): the file PATH is the route; the anchoring
  ``export default`` line is the provenance.
* **Vue Router** — ``createRouter`` route objects ``{ path, component }``.
* **Angular** — ``RouterModule.forRoot/forChild`` route arrays
  ``{ path, component }``.

Every atom carries ``file:line`` + a verbatim secret-scrubbed quote and a
rule-band confidence. The pure logic runs on source text (regex/text) with
NO tree-sitter; the native AST path is additive and gated behind
``ctx.use_tree_sitter`` + grammar availability (see :func:`_native_atoms`).

Precision discipline: object-route extraction only runs in files that carry
BOTH a router-library import AND a route-container marker, so an unrelated
``{ path: '/tmp' }`` config object never masquerades as a route.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.extract.registry import (
    SOURCE_TIER_REGEX,
    SOURCE_TIER_TREESITTER,
    STACK_ANGULAR,
    STACK_NEXTJS,
    STACK_REACT_ROUTER,
    STACK_REMIX,
    STACK_VUE_ROUTER,
    Atom,
    ExtractionContext,
    get_ts_parser,
    line_at_offset,
    use_native,
)

logger = logging.getLogger("repo_intel.extract.ts_routes")

_SRC_SUFFIXES = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".vue")
_PAGE_EXT = (".tsx", ".jsx", ".ts", ".js")

# Router-library import markers.
_RE_REACT_ROUTER = re.compile(r"""from\s+['"]react-router(?:-dom)?['"]""")
_RE_VUE_ROUTER = re.compile(r"""from\s+['"]vue-router['"]""")
_RE_ANGULAR_ROUTER = re.compile(r"""from\s+['"]@angular/router['"]""")

# Route-container markers (a route ARRAY/tree is being declared here).
_RE_REACT_CONTAINER = re.compile(r"\b(createBrowserRouter|createHashRouter|createMemoryRouter|useRoutes|createRoutesFromElements)\s*\(")
_RE_VUE_CONTAINER = re.compile(r"\bcreateRouter\s*\(")
_RE_ANGULAR_CONTAINER = re.compile(r"\bRouterModule\s*\.\s*for(?:Root|Child)\s*\(")
_RE_ROUTES_DECL = re.compile(r"\b(?:const|let|var)\s+\w*[Rr]outes\b\s*(?::\s*[A-Za-z_][\w<>\[\], ]*)?\s*=")

# JSX <Route ... path="..." ... element={<Comp/>} | component={Comp} />
_RE_JSX_ROUTE = re.compile(r"<Route\b(?P<attrs>[^>]*?)/?>", re.DOTALL)
_RE_ATTR_PATH = re.compile(r"""\bpath\s*=\s*(?:(['"])(?P<v1>[^'"]*)\1|\{\s*(['"])(?P<v2>[^'"]*)\3\s*\})""")
_RE_ATTR_ELEMENT = re.compile(r"""\belement\s*=\s*\{?\s*<\s*(?P<c>[A-Z]\w*)""")
_RE_ATTR_COMPONENT = re.compile(r"""\bcomponent\s*=\s*\{?\s*(?P<c>[A-Z]\w*)""")

# Object route: path: '...'  (+ nearby component/element)
_RE_OBJ_PATH = re.compile(r"""\bpath\s*:\s*(['"])(?P<p>[^'"]*)\1""")
_RE_OBJ_COMPONENT = re.compile(r"""\b(?:component|Component)\s*:\s*(?P<c>[A-Za-z_]\w*)""")
_RE_OBJ_ELEMENT = re.compile(r"""\belement\s*:\s*<\s*(?P<c>[A-Z]\w*)""")
_RE_OBJ_LAZY = re.compile(r"""import\s*\(\s*['"](?P<m>[^'"]+)['"]\s*\)""")

# export-default / component anchor lines for file-convention routes.
_RE_ANCHOR = re.compile(r"(?m)^\s*export\s+default\b|^\s*export\s+(?:async\s+)?function\b|^\s*export\s+const\s+\w+")


class TypeScriptRoutesExtractor:
    """Route atoms from front-end router configs + file conventions."""

    name = "ts_routes"
    supported_stacks = frozenset({
        STACK_REACT_ROUTER, STACK_NEXTJS, STACK_REMIX, STACK_VUE_ROUTER, STACK_ANGULAR,
    })
    ceiling_band: Dict[str, Dict[str, float]] = {
        # JSX / config / file-convention extraction is lossy vs a formal spec.
        "route": {"floor": 0.75, "ceiling": 0.92},
    }

    def extract(self, repo_path: Path, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        atoms.extend(self._file_convention_routes(ctx))
        native_on = use_native(ctx)
        for path in ctx.iter_files(_SRC_SUFFIXES):
            source = ctx.read_text(path)
            if not source:
                continue
            file_atoms = self._routes_in_source(source, path, ctx)
            if native_on:
                # Additive-only: native can add routes regex missed, never
                # remove them. Any native failure is swallowed (fallback wins).
                try:
                    file_atoms = _dedup(file_atoms + _native_atoms(source, path, ctx, self.name))
                except Exception as exc:  # pragma: no cover - native optional
                    logger.debug("native ts parse failed for %s: %s", ctx.rel(path), exc)
            atoms.extend(file_atoms)
        return _dedup(atoms)

    # -- config / JSX routes ----------------------------------------------
    def _routes_in_source(self, source: str, path: Path, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        is_react = bool(_RE_REACT_ROUTER.search(source))
        is_vue = bool(_RE_VUE_ROUTER.search(source))
        is_angular = bool(_RE_ANGULAR_ROUTER.search(source))

        # JSX <Route> — the token itself is the marker (react-router).
        if "<Route" in source and (is_react or _RE_REACT_CONTAINER.search(source)):
            atoms.extend(self._jsx_routes(source, path, ctx))

        # Object routes: require BOTH a router import AND a route container.
        has_container = bool(
            _RE_REACT_CONTAINER.search(source) or _RE_VUE_CONTAINER.search(source)
            or _RE_ANGULAR_CONTAINER.search(source) or _RE_ROUTES_DECL.search(source)
        )
        if has_container:
            router = (STACK_ANGULAR if is_angular else STACK_VUE_ROUTER if is_vue
                      else STACK_REACT_ROUTER if is_react else "")
            if router:
                atoms.extend(self._object_routes(source, path, ctx, router))
        return atoms

    def _jsx_routes(self, source: str, path: Path, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        for m in _RE_JSX_ROUTE.finditer(source):
            attrs = m.group("attrs") or ""
            pm = _RE_ATTR_PATH.search(attrs)
            if not pm:
                continue  # pathless index route — not a distinct URL
            path_pattern = pm.group("v1") if pm.group("v1") is not None else pm.group("v2")
            if path_pattern is None:
                continue
            comp = ""
            em = _RE_ATTR_ELEMENT.search(attrs) or _RE_ATTR_COMPONENT.search(attrs)
            if em:
                comp = em.group("c")
            line = line_at_offset(source, m.start())
            atoms.append(self._route_atom(ctx, path, line, source, path_pattern, comp,
                                          STACK_REACT_ROUTER, "jsx", 0.85))
        return atoms

    def _object_routes(self, source: str, path: Path, ctx: ExtractionContext,
                       router: str) -> List[Atom]:
        atoms: List[Atom] = []
        matches = list(_RE_OBJ_PATH.finditer(source))
        for i, m in enumerate(matches):
            raw = m.group("p")
            path_pattern = raw if raw != "" else "/"
            line = line_at_offset(source, m.start())
            # Look ahead for a component, bounded by the NEXT path: match so
            # we never attribute a sibling route's component to this one.
            window_end = matches[i + 1].start() if i + 1 < len(matches) else min(len(source), m.end() + 300)
            window = source[m.end():window_end]
            comp = ""
            cm = _RE_OBJ_COMPONENT.search(window) or _RE_OBJ_ELEMENT.search(window)
            if cm:
                comp = cm.group("c")
            elif _RE_OBJ_LAZY.search(window):
                comp = _RE_OBJ_LAZY.search(window).group("m")
            atoms.append(self._route_atom(ctx, path, line, source, path_pattern, comp,
                                          router, "config", 0.82))
        return atoms

    # -- file-convention routes -------------------------------------------
    def _file_convention_routes(self, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        remix = STACK_REMIX in ctx.stacks or self._has_remix_marker(ctx)
        seen_dirs_next: List[Path] = []
        for path in ctx.iter_files(_PAGE_EXT):
            rel = ctx.rel(path)
            parts = rel.split("/")
            # Next.js pages router
            page = _next_pages_route(parts, path.name)
            if page is not None:
                atoms.append(self._file_route_atom(ctx, path, page, "nextjs-pages", 0.88))
                continue
            # Next.js app router (page.* files)
            app_route = _next_app_route(parts, path.name)
            if app_route is not None:
                atoms.append(self._file_route_atom(ctx, path, app_route, "nextjs-app", 0.88))
                continue
            # Remix routes (gated on a remix marker to protect precision)
            if remix:
                rr = _remix_route(parts, path.name)
                if rr is not None:
                    atoms.append(self._file_route_atom(ctx, path, rr, "remix", 0.82))
        return atoms

    @staticmethod
    def _has_remix_marker(ctx: ExtractionContext) -> bool:
        for name in ("remix.config.js", "remix.config.mjs", "remix.config.ts"):
            if (ctx.repo_path / name).exists():
                return True
        return False

    # -- atom builders -----------------------------------------------------
    def _route_atom(self, ctx: ExtractionContext, path: Path, line: int, source: str,
                    path_pattern: str, component: str, router: str, mode: str,
                    confidence: float) -> Atom:
        value = {"path_pattern": path_pattern, "component": component,
                 "router": router, "mode": mode, "http": None}
        return ctx.make_atom(
            kind="route", value=value, path=path, line=line,
            quote_source=_line_text(source, line),
            extractor=self.name, confidence=confidence, source_tier=SOURCE_TIER_REGEX,
        )

    def _file_route_atom(self, ctx: ExtractionContext, path: Path, path_pattern: str,
                        router: str, confidence: float) -> Atom:
        source = ctx.read_text(path)
        line = _anchor_line(source)
        value = {"path_pattern": path_pattern, "component": path.stem,
                 "router": router, "mode": "file-convention", "http": None}
        return ctx.make_atom(
            kind="route", value=value, path=path, line=line,
            quote_source=_line_text(source, line),
            extractor=self.name, confidence=confidence, source_tier=SOURCE_TIER_REGEX,
        )


# ── pure helpers (unit-testable without any parser) ───────────────────────


def _line_text(source: str, line_no: int) -> str:
    lines = source.split("\n")
    return lines[line_no - 1] if 1 <= line_no <= len(lines) else ""


def _anchor_line(source: str) -> int:
    """Provenance line for a file-convention route: the ``export default`` /
    component-declaration line, else the first non-empty line."""
    m = _RE_ANCHOR.search(source)
    if m:
        return line_at_offset(source, m.start())
    for idx, line in enumerate(source.split("\n"), start=1):
        if line.strip():
            return idx
    return 1


def _dedup(atoms: List[Atom]) -> List[Atom]:
    """Collapse identical evidence: same normalized route in the same file."""
    seen = set()
    out: List[Atom] = []
    for a in atoms:
        key = (a.canonical_key(), a.provenance_path)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _strip_page_dir(parts: List[str], dir_names: Tuple[str, ...]) -> Optional[List[str]]:
    """Return the path segments AFTER the last matching router dir, or None."""
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in dir_names:
            return parts[i + 1:]
    return None


def _seg_to_pattern(seg: str) -> str:
    """Map a filename/dir segment to a URL segment (dynamic → :param / *)."""
    seg = re.sub(r"\[\.\.\.[^\]]+\]", "*", seg)     # [...slug] catch-all
    seg = re.sub(r"\[\[\.\.\.[^\]]+\]\]", "*", seg)  # [[...slug]] optional catch-all
    seg = re.sub(r"\[([^\]]+)\]", r":\1", seg)      # [id] → :id
    return seg


def _next_pages_route(parts: List[str], filename: str) -> Optional[str]:
    after = _strip_page_dir(parts, ("pages",))
    if after is None:
        return None
    # API routes are endpoints, not UI routes — out of scope here.
    if after and after[0] == "api":
        return None
    stem = Path(filename).stem
    if stem.startswith("_"):       # _app, _document, _error
        return None
    segs = [s for s in after[:-1]]  # dir segments
    if stem != "index":
        segs.append(stem)
    segs = [_seg_to_pattern(s) for s in segs if s and not (s.startswith("(") and s.endswith(")"))]
    return "/" + "/".join(segs) if segs else "/"


def _next_app_route(parts: List[str], filename: str) -> Optional[str]:
    stem = Path(filename).stem
    if stem != "page":
        return None
    after = _strip_page_dir(parts, ("app",))
    if after is None:
        return None
    if after and after[0] == "api":
        return None
    segs = after[:-1]  # drop 'page.*'
    out = []
    for s in segs:
        if s.startswith("(") and s.endswith(")"):
            continue  # route group — no URL segment
        if s.startswith("@"):
            continue  # parallel-route slot
        out.append(_seg_to_pattern(s))
    return "/" + "/".join(out) if out else "/"


def _remix_route(parts: List[str], filename: str) -> Optional[str]:
    after = _strip_page_dir(parts, ("routes",))
    if after is None:
        return None
    # Only files directly describing a route (skip nested non-route dirs is
    # handled by the flat/dotted mapping below).
    stem = Path(filename).stem
    if stem.startswith("_index") or stem == "index":
        # index at this level
        dirsegs = after[:-1]
        segs = [_remix_seg(s) for s in dirsegs]
        segs = [s for s in segs if s]
        return "/" + "/".join(segs) if segs else "/"
    # Combine directory segments (v1 nesting) with dotted flat segments (v2).
    dirsegs = list(after[:-1])
    flat = [p for p in stem.split(".") if p and p != "_index" and not p.startswith("_")]
    segs = [_remix_seg(s) for s in (dirsegs + flat)]
    segs = [s for s in segs if s]
    return "/" + "/".join(segs) if segs else "/"


def _remix_seg(seg: str) -> str:
    if seg.startswith("$"):
        return "*" if seg == "$" else ":" + seg[1:]
    seg = re.sub(r"\$([A-Za-z0-9_]+)", r":\1", seg)
    return seg


# ── native (tree-sitter) — additive, gated OFF by default ─────────────────


def _native_atoms(source: str, path: Path, ctx: ExtractionContext, extractor_name: str) -> List[Atom]:
    """Additive tree-sitter pass (JSX + object routes).

    Runs only when ``ctx.use_tree_sitter`` is set AND compiled grammars are
    importable. Returns atoms the AST can see; the caller UNIONs them with the
    regex result so this path can only add recall, never reduce it. Covered by
    the ``@skipif`` native tests. Any failure raises → swallowed by the caller.
    """
    lang = "tsx" if path.suffix.lower() in (".tsx", ".jsx") else "typescript"
    parser = get_ts_parser(lang) or get_ts_parser("javascript")
    if parser is None:
        return []
    tree = parser.parse(source.encode("utf-8"))
    atoms: List[Atom] = []
    src_bytes = source.encode("utf-8")

    def text(node) -> str:
        return src_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def walk(node):
        # JSX <Route path=.. />
        if node.type in ("jsx_opening_element", "jsx_self_closing_element"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and text(name_node) == "Route":
                path_val = _native_jsx_attr(node, "path", text)
                if path_val is not None:
                    comp = _native_jsx_attr(node, "element", text) or _native_jsx_attr(node, "component", text) or ""
                    line = node.start_point[0] + 1
                    atoms.append(ctx.make_atom(
                        kind="route",
                        value={"path_pattern": path_val, "component": _clean_comp(comp),
                               "router": STACK_REACT_ROUTER, "mode": "jsx", "http": None},
                        path=path, line=line, quote_source=_line_text(source, line),
                        extractor=extractor_name, confidence=0.85,
                        source_tier=SOURCE_TIER_TREESITTER,
                    ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return atoms


def _native_jsx_attr(element_node, attr: str, text) -> Optional[str]:
    for child in element_node.children:
        if child.type != "jsx_attribute":
            continue
        name = child.child_by_field_name("name")
        if name is None or text(name) != attr:
            continue
        raw = text(child)
        m = _RE_ATTR_PATH.search(raw) if attr == "path" else re.search(r"[A-Z]\w*", raw)
        if attr == "path" and m:
            return m.group("v1") if m.group("v1") is not None else m.group("v2")
        if attr != "path" and m:
            return m.group(0)
    return None


def _clean_comp(comp: str) -> str:
    m = re.search(r"[A-Z]\w*", comp or "")
    return m.group(0) if m else ""
