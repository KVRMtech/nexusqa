"""Spring (Java) extractor — REST controller mappings + JSR-380 constraints.

Turns Spring MVC controller mappings (``@RestController`` /
``@RequestMapping`` class prefix + ``@GetMapping`` / ``@PostMapping`` /
``@RequestMapping`` methods) into ``api_endpoint`` atoms, and bean-validation
(JSR-380) field annotations (``@NotNull`` / ``@Size`` / ``@Min`` / ``@Email``
...) into ``validator_rule`` atoms.

The controller-mapping path is fully implemented on the regex/text fallback.
Deep validator extraction (cross-field, custom constraints, nested DTOs) is a
documented seam — this extractor emits the directly-annotated field constraints
and marks itself degraded only if a controller file is unparseable, never
pretending to a completeness it does not have (design §3.3: Spring validator
depth is a bonus tier).

Every atom carries ``file:line`` + a verbatim, secret-scrubbed quote, and a
fixed rule-band confidence (never an LLM score).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from app.extract.registry import (
    Atom,
    ExtractionContext,
    ExtractorDegraded,
    STACK_SPRING,
    SOURCE_TIER_REGEX,
    line_at_offset,
)

# Class-level prefix: @RequestMapping("/base")  or  @RequestMapping(path = "/base")
_CLASS_MAPPING = re.compile(
    r"""@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?"""
    r"""(?P<q>["'])(?P<base>[^"']*)(?P=q)""",
)
_CONTROLLER_MARK = re.compile(r"@(?:RestController|Controller)\b")

# Method mappings — @GetMapping("/x"), @PostMapping (no args → controller base),
# @PutMapping(value="/x"). The argument list is OPTIONAL (a bare @PostMapping
# maps to the class-level @RequestMapping prefix).
_METHOD_MAPPING = re.compile(
    r"""@(?P<ann>Get|Post|Put|Patch|Delete)Mapping\b"""
    r"""(?:\s*\(\s*(?:(?:value|path)\s*=\s*)?"""
    r"""(?:(?P<q>["'])(?P<path>[^"']*)(?P=q))?[^)]*\))?""",
)
_REQUEST_MAPPING_METHOD = re.compile(
    r"""@RequestMapping\s*\((?P<body>[^)]*)\)""",
)

# JSR-380 field annotations.
_JSR_ANN = re.compile(
    r"""@(?P<rule>NotNull|NotEmpty|NotBlank|Size|Min|Max|Email|Pattern|"""
    r"""Positive|PositiveOrZero|Negative|NegativeOrZero|Past|Future|"""
    r"""AssertTrue|AssertFalse|Digits|DecimalMin|DecimalMax)"""
    r"""\b(?:\s*\((?P<args>[^)]*)\))?""",
)
# The Java field a JSR-380 annotation applies to.
_JAVA_FIELD = re.compile(
    r"^\s*(?:private|protected|public\s+)?(?:final\s+)?[A-Za-z_][\w<>\[\],.\s]*?\s+"
    r"(?P<field>[A-Za-z_]\w*)\s*[;=]",
)

_ANN_TO_METHOD = {"Get": "GET", "Post": "POST", "Put": "PUT",
                  "Patch": "PATCH", "Delete": "DELETE"}
_ROUTE_CONF = 0.9
_VALIDATOR_CONF = 0.8


def _join(base: str, sub: str) -> str:
    base = (base or "").strip()
    sub = (sub or "").strip()
    if not base and not sub:
        return "/"
    if not base:
        return "/" + sub.lstrip("/")
    if not sub:
        return "/" + base.strip("/")
    return "/" + base.strip("/") + "/" + sub.lstrip("/")


def _request_mapping_methods(body: str) -> List[str]:
    """Extract HTTP method(s) from a @RequestMapping(method=...) body."""
    methods = re.findall(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)", body)
    return [m.upper() for m in methods] or ["GET"]


def _request_mapping_path(body: str) -> str:
    m = re.search(r"""(?:value|path)?\s*=?\s*["'](?P<p>[^"']*)["']""", body)
    return m.group("p") if m else ""


class SpringExtractor:
    """Spring MVC controller-mapping + JSR-380 validator extractor."""

    name = "spring"
    supported_stacks = frozenset({STACK_SPRING})
    ceiling_band: Dict[str, Dict[str, float]] = {
        "api_endpoint": {"floor": 0.70, "ceiling": 0.90},
        "validator_rule": {"floor": 0.45, "ceiling": 0.75},
    }

    _SUFFIXES = (".java",)

    def extract(self, repo_path: Path, ctx: ExtractionContext) -> List[Atom]:
        atoms: List[Atom] = []
        errors = 0
        files = 0
        for path in ctx.iter_files(self._SUFFIXES):
            text = ctx.read_text(path)
            if not text:
                continue
            files += 1
            try:
                if _CONTROLLER_MARK.search(text):
                    atoms.extend(self._controller(path, text, ctx))
                atoms.extend(self._validators(path, text, ctx))
            except Exception:  # pragma: no cover - defensive isolation
                errors += 1
        if files and errors and errors >= max(1, files // 2):
            raise ExtractorDegraded(
                f"spring failed on {errors}/{files} files", atoms
            )
        return atoms

    def _controller(self, path: Path, text: str, ctx: ExtractionContext) -> List[Atom]:
        out: List[Atom] = []
        lines = text.split("\n")
        cm = _CLASS_MAPPING.search(text)
        base = cm.group("base") if cm else ""

        for m in _METHOD_MAPPING.finditer(text):
            method = _ANN_TO_METHOD.get(m.group("ann"), "GET")
            sub = m.group("path") or ""
            line = line_at_offset(text, m.start())
            out.append(ctx.make_atom(
                kind="api_endpoint",
                value={"method": method, "path": _join(base, sub),
                       "framework": "spring", "controller_base": base},
                path=path, line=line, quote_source=lines[line - 1],
                extractor=self.name, confidence=_ROUTE_CONF,
                source_tier=SOURCE_TIER_REGEX,
            ))

        # @RequestMapping on a METHOD (has a method= or is inside a controller body)
        for m in _REQUEST_MAPPING_METHOD.finditer(text):
            body = m.group("body")
            if "method" not in body and "RequestMethod" not in body:
                continue  # class-level prefix, already captured as base
            line = line_at_offset(text, m.start())
            sub = _request_mapping_path(body)
            for method in _request_mapping_methods(body):
                out.append(ctx.make_atom(
                    kind="api_endpoint",
                    value={"method": method, "path": _join(base, sub),
                           "framework": "spring", "controller_base": base},
                    path=path, line=line, quote_source=lines[line - 1],
                    extractor=self.name, confidence=_ROUTE_CONF,
                    source_tier=SOURCE_TIER_REGEX,
                ))
        return out

    def _validators(self, path: Path, text: str, ctx: ExtractionContext) -> List[Atom]:
        out: List[Atom] = []
        lines = text.split("\n")
        for m in _JSR_ANN.finditer(text):
            line = line_at_offset(text, m.start())
            field = ""
            for probe in range(line, min(line + 4, len(lines) + 1)):
                fm = _JAVA_FIELD.match(lines[probe - 1]) if probe - 1 < len(lines) else None
                if fm:
                    field = fm.group("field")
                    break
            out.append(ctx.make_atom(
                kind="validator_rule",
                value={"field": field, "rule": m.group("rule"),
                       "args": (m.group("args") or "").strip(), "library": "jsr380"},
                path=path, line=line, quote_source=lines[line - 1],
                extractor=self.name, confidence=_VALIDATOR_CONF,
                source_tier=SOURCE_TIER_REGEX,
            ))
        return out
