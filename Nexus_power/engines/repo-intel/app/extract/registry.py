"""Extractor registry — the Atom shape, the Extractor Protocol, the
degraded-aware runner, and the shared provenance / tree-sitter helpers.

This module is the FOUNDATION every extractor and every downstream
consumer (model/store, manifest/seed, drift/report, lens) imports. It is
deliberately dependency-light (stdlib only) so the pure parse-to-atoms
logic is unit-testable WITHOUT a compiled tree-sitter grammar, an engine
runtime, or the SDK.

Key exports (the contract other agents import):

* :class:`Atom` — one provenance-tagged fact; ``to_row()`` maps 1:1 to the
  ``app_model_atoms`` columns (qec_001).
* :class:`ExtractionContext` — repo-rooted file access + secret scrub +
  detected stacks + config, injected into every ``extract`` call.
* :class:`Extractor` — the Protocol every plugin satisfies.
* :class:`ExtractorDegraded` — raised by a plugin to signal a HONEST
  partial (keeps atoms produced so far, marks the universe degraded).
* :func:`run_extractors` — runs applicable plugins in isolation and
  returns a :class:`RegistryResult` with per-plugin verdicts.
* provenance helpers :func:`line_at_offset`, :func:`source_line`,
  :func:`build_quote`; route normaliser :func:`normalize_route_pattern`;
  tree-sitter gate :func:`tree_sitter_available` / :func:`get_ts_parser`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from app.security.secret_scrub import scrub as _default_scrub

logger = logging.getLogger("repo_intel.extract")

# Max verbatim quote length — mirrors app_model_atoms.quote String(500).
MAX_QUOTE_LEN = 500

# source_tier values (the parse method that produced the atom).
SOURCE_TIER_SPEC = "static_spec"
SOURCE_TIER_TREESITTER = "static_treesitter"
SOURCE_TIER_REGEX = "static_regex"

# Stack identifiers used by supported_stacks + detect/stack.py fingerprints.
STACK_OPENAPI = "openapi"
STACK_REACT_ROUTER = "react-router"
STACK_NEXTJS = "nextjs"
STACK_REMIX = "remix"
STACK_VUE_ROUTER = "vue-router"
STACK_ANGULAR = "angular"
STACK_EXPRESS = "express"
STACK_NESTJS = "nestjs"
STACK_SPRING = "spring"

# Directories that never contain first-party source worth extracting.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "out", "vendor", "target",
    "__pycache__", ".venv", "venv", ".next", ".nuxt", "coverage", ".idea",
    ".gradle", "bin", "obj",
})


# ─────────────────────────────── Atom ────────────────────────────────────


@dataclass(frozen=True)
class Atom:
    """One provenance-tagged fact extracted from source.

    Field set matches the extractor-owned columns of ``app_model_atoms``
    (qec_001). ``atom_id``/``tenant_id``/``universe_id``/``created_at`` are
    assigned by the store, NOT the extractor.
    """

    kind: str                    # route | api_endpoint | form | validator_rule | ...
    value: dict                  # kind-normalized JSON payload
    provenance_path: str         # repo-relative path (posix separators)
    provenance_line: int         # 1-based line of the anchoring construct
    provenance_sha: str          # sha256 of the file's bytes (or "" if unknown)
    quote: str                   # verbatim <=500, secret-scrubbed
    extractor: str               # producing extractor.name
    confidence: float            # rule-band float — NEVER an LLM score
    source_tier: str             # SOURCE_TIER_*

    def canonical_key(self) -> Tuple:
        """Stable identity for dedup + answer-key grading.

        Kind-aware so re-runs and re-crawls are idempotent and so recall /
        precision are computed against a normalized key rather than a raw
        (path-sensitive, param-name-sensitive) string.
        """
        v = self.value
        if self.kind == "route":
            return ("route", normalize_route_pattern(v.get("path_pattern", "")))
        if self.kind == "api_endpoint":
            return ("api_endpoint", str(v.get("method", "")).upper(),
                    normalize_route_pattern(v.get("path", "")))
        if self.kind == "validator_rule":
            return ("validator_rule", str(v.get("field", "")).lower(),
                    str(v.get("rule", "")).lower())
        return (self.kind, str(sorted(v.items())))

    def to_row(self) -> dict:
        """Map to the extractor-owned ``app_model_atoms`` columns."""
        return {
            "kind": self.kind,
            "value": self.value,
            "provenance_path": self.provenance_path,
            "provenance_line": self.provenance_line,
            "provenance_sha": self.provenance_sha,
            "quote": self.quote,
            "extractor": self.extractor,
            "confidence": self.confidence,
            "source_tier": self.source_tier,
        }


# ───────────────────────── ExtractionContext ─────────────────────────────


@dataclass
class ExtractionContext:
    """Repo-rooted, read-only access + config injected into every extractor.

    Extractors NEVER touch the filesystem directly nor the network — they
    go through this context so the pure logic is testable against a fixture
    directory and so newline/secret handling is uniform.
    """

    repo_path: Path
    deployed_sha: str = ""
    # Detected stacks (from detect/stack.py). EMPTY ⇒ "unknown": the runner
    # then attempts every extractor (fail-open, self-gated by file presence).
    stacks: frozenset = frozenset()
    scrub: Callable[[str], str] = _default_scrub
    max_quote_len: int = MAX_QUOTE_LEN
    # When True AND tree-sitter is importable, extractors use the native AST
    # path; otherwise the documented regex/text fallback. Default OFF so the
    # graded, verified path is the fallback until native is validated in a
    # tree-sitter-equipped CI (env: REPO_INTEL_USE_TREE_SITTER).
    use_tree_sitter: bool = field(
        default_factory=lambda: _env_flag("REPO_INTEL_USE_TREE_SITTER")
    )
    # Cap total bytes read per file (defensive against pathological blobs).
    max_file_bytes: int = 4_000_000

    _text_cache: Dict[str, str] = field(default_factory=dict, repr=False)
    _sha_cache: Dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.repo_path = Path(self.repo_path)

    # -- file access -------------------------------------------------------
    def rel(self, path: Path) -> str:
        """Return the posix repo-relative path for provenance."""
        try:
            return path.resolve().relative_to(self.repo_path.resolve()).as_posix()
        except Exception:
            return path.as_posix()

    def read_text(self, path: Path) -> str:
        """Read + cache a file as text with newlines normalised to ``\\n``.

        Non-existent / oversize / binary files return ``""`` (fail-safe).
        """
        rel = self.rel(path)
        if rel in self._text_cache:
            return self._text_cache[rel]
        text = ""
        try:
            raw = path.read_bytes()
            if len(raw) <= self.max_file_bytes and b"\x00" not in raw[:4096]:
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, ValueError) as exc:  # pragma: no cover - defensive
            logger.debug("read_text failed for %s: %s", rel, exc)
        self._text_cache[rel] = text
        return text

    def file_sha(self, path: Path) -> str:
        """sha256 hexdigest of the file's raw bytes (provenance anchor)."""
        rel = self.rel(path)
        if rel in self._sha_cache:
            return self._sha_cache[rel]
        sha = ""
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:  # pragma: no cover - defensive
            pass
        self._sha_cache[rel] = sha
        return sha

    def iter_files(self, suffixes: Sequence[str]) -> Iterator[Path]:
        """Yield first-party source files with any of ``suffixes`` (skips
        vendored / build dirs). Deterministic (sorted) order."""
        wanted = {s.lower() if s.startswith(".") else "." + s.lower() for s in suffixes}
        root = self.repo_path
        results: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in filenames:
                if Path(name).suffix.lower() in wanted:
                    results.append(Path(dirpath) / name)
        results.sort(key=lambda p: self.rel(p))
        return iter(results)

    # -- atom construction -------------------------------------------------
    def make_atom(
        self,
        *,
        kind: str,
        value: dict,
        path: Path,
        line: int,
        quote_source: str,
        extractor: str,
        confidence: float,
        source_tier: str,
    ) -> Atom:
        """Build a fully-provenanced, secret-scrubbed Atom.

        ``quote_source`` is the raw text the quote is derived from (a single
        source line or a compact construct). It is stripped, secret-scrubbed
        and truncated to ``max_quote_len``. ``line`` is 1-based.
        """
        quote = self.scrub(quote_source.strip())[: self.max_quote_len]
        return Atom(
            kind=kind,
            value=value,
            provenance_path=self.rel(path),
            provenance_line=int(line),
            provenance_sha=self.file_sha(path),
            quote=quote,
            extractor=extractor,
            confidence=float(confidence),
            source_tier=source_tier,
        )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ────────────────────────── Extractor Protocol ───────────────────────────


@runtime_checkable
class Extractor(Protocol):
    """The plugin contract.

    * ``name`` — stable extractor id (also stamped on every Atom).
    * ``supported_stacks`` — stacks this extractor applies to. EMPTY means
      "stack-agnostic" (e.g. OpenAPI specs can live in any repo): always
      attempted, self-gated by file discovery.
    * ``ceiling_band`` — PUBLISHED honesty: ``{atom_kind: {"floor": f,
      "ceiling": c}}``. Rules never claim recall above the ceiling; CI
      grades measured recall against the floor.
    * ``extract(repo_path, ctx)`` — return the atoms; raise
      :class:`ExtractorDegraded` for an honest partial, any other exception
      to be caught by the runner and reported as degraded.
    """

    name: str
    supported_stacks: frozenset
    ceiling_band: Dict[str, Dict[str, float]]

    def extract(self, repo_path: Path, ctx: ExtractionContext) -> List[Atom]:
        ...


class ExtractorDegraded(Exception):
    """Raised by an extractor to signal an HONEST partial extraction.

    Carries the atoms produced before degradation so the runner keeps them
    while marking the universe ``degraded`` (never a silent partial).
    """

    def __init__(self, reason: str, atoms: Optional[List[Atom]] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.atoms: List[Atom] = list(atoms or [])


# ─────────────────────────── Runner + result ─────────────────────────────

# Per-extractor verdict.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_SKIPPED = "skipped"


@dataclass
class ExtractorRun:
    name: str
    status: str
    atoms: List[Atom] = field(default_factory=list)
    reason: str = ""
    error: str = ""


@dataclass
class RegistryResult:
    runs: List[ExtractorRun]

    @property
    def atoms(self) -> List[Atom]:
        out: List[Atom] = []
        for r in self.runs:
            out.extend(r.atoms)
        return out

    @property
    def degraded_extractors(self) -> List[str]:
        return [r.name for r in self.runs if r.status == STATUS_DEGRADED]

    @property
    def ran_extractors(self) -> List[str]:
        return [r.name for r in self.runs if r.status in (STATUS_OK, STATUS_DEGRADED)]

    @property
    def universe_status(self) -> str:
        """``degraded`` if ANY applicable extractor failed/partialed, else
        ``ready``. Absence of applicable extractors (all skipped) is still
        ``ready`` — nothing to do is not a failure."""
        return STATUS_DEGRADED if self.degraded_extractors else "ready"

    @property
    def ceiling_bands(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        """Merged published bands keyed by extractor name — feeds
        ``app_model_universes.ceiling_bands``."""
        return {r.name: dict(_EXTRACTOR_BANDS.get(r.name, {})) for r in self.runs
                if r.status in (STATUS_OK, STATUS_DEGRADED)}


# Populated as extractors register their bands (see build_default_registry).
_EXTRACTOR_BANDS: Dict[str, Dict[str, Dict[str, float]]] = {}


def _applies(extractor: Extractor, ctx: ExtractionContext) -> bool:
    supported = getattr(extractor, "supported_stacks", frozenset()) or frozenset()
    if not supported:
        return True  # stack-agnostic
    if not ctx.stacks:
        return True  # unknown stack ⇒ attempt (fail-open, self-gated)
    return bool(supported & set(ctx.stacks))


def run_extractors(
    repo_path: Path,
    ctx: Optional[ExtractionContext] = None,
    extractors: Optional[Iterable[Extractor]] = None,
) -> RegistryResult:
    """Run every applicable extractor in isolation and collect verdicts.

    A plugin raising :class:`ExtractorDegraded` ⇒ ``degraded`` (its partial
    atoms retained). A plugin raising any other exception ⇒ ``degraded``
    (zero atoms, scrubbed error). Neither ever aborts the run of the other
    plugins — the failure is contained and made honest, never silent.
    """
    repo_path = Path(repo_path)
    if ctx is None:
        ctx = ExtractionContext(repo_path=repo_path)
    plugins = list(extractors) if extractors is not None else build_default_registry()
    runs: List[ExtractorRun] = []
    for ex in plugins:
        _EXTRACTOR_BANDS[ex.name] = getattr(ex, "ceiling_band", {}) or {}
        if not _applies(ex, ctx):
            runs.append(ExtractorRun(name=ex.name, status=STATUS_SKIPPED,
                                     reason="stack not present"))
            continue
        try:
            atoms = list(ex.extract(repo_path, ctx))
            runs.append(ExtractorRun(name=ex.name, status=STATUS_OK, atoms=atoms))
        except ExtractorDegraded as deg:
            logger.warning("extractor %s degraded: %s", ex.name, deg.reason)
            runs.append(ExtractorRun(name=ex.name, status=STATUS_DEGRADED,
                                     atoms=deg.atoms, reason=deg.reason))
        except Exception as exc:  # isolation — one plugin never kills another
            # Scrub the message: it may embed source fragments / paths.
            msg = ctx.scrub(f"{type(exc).__name__}: {exc}")[:500]
            logger.exception("extractor %s crashed", ex.name)
            runs.append(ExtractorRun(name=ex.name, status=STATUS_DEGRADED,
                                     reason="extractor raised", error=msg))
    return RegistryResult(runs=runs)


def build_default_registry() -> List[Extractor]:
    """Instantiate the shipped extractors. Imported lazily to avoid a
    circular import (extractors import this module)."""
    from app.extract.openapi_spec import OpenAPIExtractor
    from app.extract.ts_routes import TypeScriptRoutesExtractor
    from app.extract.express_nest import ExpressNestExtractor
    from app.extract.spring import SpringExtractor

    return [
        OpenAPIExtractor(),
        TypeScriptRoutesExtractor(),
        ExpressNestExtractor(),
        SpringExtractor(),
    ]


# ───────────────────── provenance / text helpers ─────────────────────────


def line_at_offset(source: str, offset: int) -> int:
    """1-based line number of ``offset`` within ``source`` (``\\n`` newlines)."""
    if offset < 0:
        offset = 0
    return source.count("\n", 0, offset) + 1


def source_line(source: str, line_no: int) -> str:
    """Return the raw content of 1-based ``line_no`` (``""`` if OOB)."""
    if line_no < 1:
        return ""
    lines = source.split("\n")
    if line_no > len(lines):
        return ""
    return lines[line_no - 1]


def build_quote(source: str, line_no: int, scrub: Callable[[str], str] = _default_scrub,
                max_len: int = MAX_QUOTE_LEN) -> str:
    """Convenience: secret-scrubbed, stripped, truncated quote for a line."""
    return scrub(source_line(source, line_no).strip())[:max_len]


def normalize_route_pattern(pattern: str) -> str:
    """Canonicalise a route/endpoint path for matching + drift.

    ``:id`` / ``{id}`` / ``[id]`` → ``:param``; ``[...slug]`` / ``*`` → ``*``;
    trailing slash trimmed (except root); collapses duplicate slashes.
    """
    if not pattern:
        return ""
    p = pattern.strip()
    # Strip protocol+host if a full URL slipped in.
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+", "", p)
    # Catch-all params → *
    p = re.sub(r"\[\.\.\.[^\]]+\]", "*", p)          # [...slug]
    p = re.sub(r"\{\*[^}]*\}", "*", p)               # {*rest}
    # Named single params → :param
    p = re.sub(r"\{[^}/]+\}", ":param", p)           # {id}
    p = re.sub(r"\[[^\]/]+\]", ":param", p)          # [id]
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", ":param", p)  # :id
    p = re.sub(r"/{2,}", "/", p)                     # dup slashes
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    if not p.startswith("/") and p not in ("", "*"):
        p = "/" + p
    return p or "/"


# Concrete dynamic segments a LIVE url carries where code has a :param.
_NUMERIC_SEG = re.compile(r"^\d+$")
_UUID_SEG = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}$")
_HEX_SEG = re.compile(r"^[0-9a-fA-F]{16,}$")


def normalize_reached_path(path: str) -> str:
    """Normalise a LIVE (crawl-reached) url_path so it compares equal to the
    code route PATTERN it was served by.

    A code route is a pattern (``/orders/:id`` → ``/orders/:param``); a live
    url is concrete (``/orders/42``). This collapses concrete dynamic segments
    (pure-numeric, uuid, long-hex) to ``:param`` BEFORE the shared
    normalisation, so ``/orders/42`` and ``/orders/:id`` match. Static
    segments (``/orders/summary``) are left intact.
    """
    if not path:
        return ""
    # Drop protocol+host if a full URL slipped in, and query/fragment.
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+", "", path.strip())
    p = p.split("?", 1)[0].split("#", 1)[0]
    segs = [s for s in p.split("/")]
    out = []
    for s in segs:
        if s and (_NUMERIC_SEG.match(s) or _UUID_SEG.match(s) or _HEX_SEG.match(s)):
            out.append(":param")
        else:
            out.append(s)
    return normalize_route_pattern("/".join(out))


# ──────────────────────── tree-sitter gating ─────────────────────────────

_TS_PARSER_CACHE: Dict[str, object] = {}


def tree_sitter_available() -> bool:
    """True iff ``tree_sitter_languages`` (compiled grammars) is importable.

    Grammars may NOT be installable on every dev box; the native AST path is
    gated behind this AND ``ctx.use_tree_sitter`` so the default, graded path
    is always the pure regex/text fallback.
    """
    try:
        import tree_sitter_languages  # noqa: F401
        return True
    except Exception:
        return False


def get_ts_parser(language: str):
    """Return a cached tree-sitter parser for ``language`` or ``None``.

    ``language`` ∈ {"typescript", "tsx", "javascript", "java"}. Any import /
    grammar failure returns ``None`` so callers fall back to regex.
    """
    if language in _TS_PARSER_CACHE:
        return _TS_PARSER_CACHE[language]
    parser = None
    try:  # pragma: no cover - exercised only where grammars are installed
        from tree_sitter_languages import get_parser
        parser = get_parser(language)
    except Exception as exc:
        logger.debug("tree-sitter parser for %s unavailable: %s", language, exc)
        parser = None
    _TS_PARSER_CACHE[language] = parser
    return parser


def use_native(ctx: ExtractionContext) -> bool:
    """Whether to use the native tree-sitter path for this run."""
    return bool(ctx.use_tree_sitter) and tree_sitter_available()
