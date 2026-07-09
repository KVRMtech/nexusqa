"""OpenAPI / Swagger spec extractor → ``api_endpoint`` atoms.

Discovers OpenAPI (v3) / Swagger (v2) documents anywhere in the repo — by
filename heuristic AND by a content sniff for a top-level ``openapi:`` /
``swagger:`` key — and turns every ``paths.<path>.<method>`` operation into
a provenanced ``api_endpoint`` atom (method + path + params) with the real
``file:line`` of the method (falling back to the path-item line).

Honesty / robustness:

* A formal contract ⇒ the highest published ceiling band of any extractor.
* A malformed spec does NOT crash the extractor and does NOT silently drop:
  good atoms from other specs are retained and the extractor raises
  :class:`ExtractorDegraded` (universe → ``degraded``).
* No specs found ⇒ empty result with ``ok`` status (absence ≠ failure).

Parsing uses :mod:`yaml` (``safe_load`` — a JSON document is valid YAML) for
structure and a raw-text scan for line numbers, so it needs no tree-sitter.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from app.extract.registry import (
    SOURCE_TIER_SPEC,
    STACK_OPENAPI,
    Atom,
    ExtractionContext,
    ExtractorDegraded,
    line_at_offset,
)

logger = logging.getLogger("repo_intel.extract.openapi")

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SPEC_SUFFIXES = (".json", ".yaml", ".yml")
# Filenames that are almost certainly a spec (fast path before content sniff).
_NAME_HINTS = ("openapi", "swagger", "api-docs", "apispec", "api_spec")


class OpenAPIExtractor:
    """Extractor plugin for OpenAPI/Swagger documents."""

    name = "openapi_spec"
    # Stack-agnostic: a spec can accompany ANY stack (self-gated by content).
    supported_stacks = frozenset()
    ceiling_band: Dict[str, Dict[str, float]] = {
        # A formal contract: recall and precision ceilings are high.
        "api_endpoint": {"floor": 0.90, "ceiling": 0.99},
    }

    def extract(self, repo_path: Path, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        malformed: List[str] = []
        found_any = False

        for path in ctx.iter_files(_SPEC_SUFFIXES):
            source = ctx.read_text(path)
            if not source or not self._looks_like_spec(path, source):
                continue
            found_any = True
            try:
                doc = yaml.safe_load(source)
            except yaml.YAMLError as exc:
                logger.warning("malformed spec %s: %s", ctx.rel(path), exc)
                malformed.append(ctx.rel(path))
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
                malformed.append(ctx.rel(path))
                continue
            try:
                atoms.extend(self._atoms_from_doc(doc, source, path, ctx))
            except Exception as exc:  # a single bad doc must not kill the rest
                logger.warning("failed parsing spec %s: %s", ctx.rel(path), exc)
                malformed.append(ctx.rel(path))

        if malformed:
            # Honest partial: keep the good atoms, mark the universe degraded.
            raise ExtractorDegraded(
                reason=f"{len(malformed)} malformed/unsupported spec(s): "
                       + ", ".join(sorted(malformed)[:5]),
                atoms=atoms,
            )
        if not found_any:
            logger.info("openapi_spec: no OpenAPI/Swagger documents found")
        return atoms

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def _looks_like_spec(path: Path, source: str) -> bool:
        stem = path.stem.lower()
        if any(h in stem for h in _NAME_HINTS):
            return True
        head = source[:4000]
        # Top-level version key (YAML or JSON) is the definitive marker.
        return bool(
            _RE_OPENAPI_KEY.search(head) or _RE_SWAGGER_KEY.search(head)
        )

    # -- parsing -----------------------------------------------------------
    def _atoms_from_doc(self, doc: dict, source: str, path: Path,
                        ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        base = _base_path(doc)
        paths = doc.get("paths", {})
        for raw_path, item in paths.items():
            if not isinstance(item, dict):
                continue
            full_path = _join(base, str(raw_path))
            path_line = _find_path_line(source, str(raw_path))
            shared_params = _params(item.get("parameters"))
            for method in _HTTP_METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                method_line = _find_method_line(source, str(raw_path), method, path_line)
                params = shared_params + _params(op.get("parameters"))
                params += _request_body_fields(op.get("requestBody"))
                value = {
                    "method": method.upper(),
                    "path": full_path,
                    "operation_id": str(op.get("operationId", "")),
                    "summary": str(op.get("summary", ""))[:200],
                    "params": params,
                    "framework": "openapi",
                    "spec_version": _spec_version(doc),
                }
                atoms.append(ctx.make_atom(
                    kind="api_endpoint",
                    value=value,
                    path=path,
                    line=method_line,
                    quote_source=_line_text(source, method_line),
                    extractor=self.name,
                    confidence=0.95,
                    source_tier=SOURCE_TIER_SPEC,
                ))
        return atoms


# ── module-level helpers (pure — trivially unit-testable) ─────────────────

import re  # noqa: E402  (kept local to this module's helpers)

_RE_OPENAPI_KEY = re.compile(r'(?m)^\s*["\']?openapi["\']?\s*:')
_RE_SWAGGER_KEY = re.compile(r'(?m)^\s*["\']?swagger["\']?\s*:')


def _spec_version(doc: dict) -> str:
    return str(doc.get("openapi") or doc.get("swagger") or "")


def _base_path(doc: dict) -> str:
    """v3 servers[0].url path OR v2 basePath — best-effort prefix."""
    servers = doc.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = str(servers[0].get("url", ""))
        m = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]+", "", url)
        # Only keep a leading path (ignore templated hosts).
        if m and not m.startswith("{"):
            return m.rstrip("/")
    base = doc.get("basePath")
    return str(base).rstrip("/") if isinstance(base, str) else ""


def _join(base: str, path: str) -> str:
    if not base:
        return path if path.startswith("/") else "/" + path
    if not path.startswith("/"):
        path = "/" + path
    return (base + path) or "/"


def _params(raw) -> List[dict]:
    """Normalise an OpenAPI parameter list to ``[{name,in,required,type}]``."""
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for p in raw:
        if not isinstance(p, dict) or "name" not in p:
            continue
        schema = p.get("schema") if isinstance(p.get("schema"), dict) else {}
        out.append({
            "name": str(p.get("name")),
            "in": str(p.get("in", "")),
            "required": bool(p.get("required", False)),
            "type": str(schema.get("type", p.get("type", ""))),
        })
    return out


def _request_body_fields(raw) -> List[dict]:
    """Top-level required request-body properties → param-shaped dicts."""
    if not isinstance(raw, dict):
        return []
    content = raw.get("content")
    if not isinstance(content, dict):
        return []
    for media in content.values():
        schema = media.get("schema") if isinstance(media, dict) else None
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties")
        required = set(schema.get("required", []) if isinstance(schema.get("required"), list) else [])
        if isinstance(props, dict):
            out: List[dict] = []
            for name, spec in props.items():
                t = spec.get("type", "") if isinstance(spec, dict) else ""
                out.append({"name": str(name), "in": "body",
                            "required": name in required, "type": str(t)})
            return out
    return []


def _line_text(source: str, line_no: int) -> str:
    lines = source.split("\n")
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1]
    return ""


def _find_path_line(source: str, raw_path: str) -> int:
    """Line of the path-item key (``/foo:`` in YAML or ``"/foo":`` in JSON)."""
    # Match the path used as a key, at some indentation, quoted or not.
    esc = re.escape(raw_path)
    for pat in (rf'(?m)^\s*["\']?{esc}["\']?\s*:',):
        m = re.search(pat, source)
        if m:
            return line_at_offset(source, m.start())
    return 1


def _find_method_line(source: str, raw_path: str, method: str, path_line: int) -> int:
    """Line of the ``method:`` key beneath its path item.

    Scans forward from the path-item line for the first ``<method>:`` key at a
    deeper indentation before the next same-or-shallower top-level path key.
    Falls back to the path line when structure can't be resolved (still a real
    ``file:line`` for the endpoint).
    """
    lines = source.split("\n")
    start = max(path_line, 1)
    method_re = re.compile(rf'(?i)^\s*["\']?{re.escape(method)}["\']?\s*:')
    next_path_re = re.compile(r'(?m)^\s*["\']?/')
    for idx in range(start, len(lines) + 1):
        line = lines[idx - 1]
        if idx > start and next_path_re.match(line) and not method_re.match(line):
            # Reached the next path item without finding the method.
            break
        if method_re.match(line):
            return idx
    return path_line
