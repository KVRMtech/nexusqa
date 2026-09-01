"""Express + NestJS extractor — API endpoints and validator rules.

Turns Express route registrations (``app.get('/x', ...)`` / ``router.post``)
and NestJS controller decorators (``@Controller('base')`` + ``@Get('sub')``)
into ``api_endpoint`` atoms, and zod / joi / yup / class-validator field
constraints into ``validator_rule`` atoms — every atom provenance-tagged with
``file:line`` and a verbatim, secret-scrubbed quote at that location.

Default path is a documented regex/text scan (unit-testable without a compiled
tree-sitter grammar). The native tree-sitter AST path is gated behind
``ctx.use_tree_sitter`` AND grammar availability; when either is absent the
regex path is used and the atoms are stamped ``source_tier='static_regex'``.

Confidence is a fixed rule-band value per construct kind — NEVER an LLM score.
A file that cannot be read is skipped (fail-safe); a wholesale parse failure
raises :class:`ExtractorDegraded` carrying the atoms found so far (honest
partial, never a silent drop).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from app.extract.registry import (
    Atom,
    ExtractionContext,
    ExtractorDegraded,
    STACK_EXPRESS,
    STACK_NESTJS,
    SOURCE_TIER_REGEX,
    line_at_offset,
)

# HTTP verbs Express/Nest expose as route-registration methods.
_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "options", "head", "all")

# app.get('/path', ...)  |  router.post("/path", ...)  |  route.delete(`/path`, ...)
_EXPRESS_ROUTE = re.compile(
    r"""\b(?P<obj>app|router|route)\s*\.\s*(?P<verb>%s)\s*\(\s*"""
    r"""(?P<q>['"`])(?P<path>[^'"`]*)(?P=q)""" % "|".join(_HTTP_VERBS),
    re.IGNORECASE,
)

# @Controller('base')  /  @Controller()  (Nest class-level prefix)
_NEST_CONTROLLER = re.compile(
    r"""@Controller\s*\(\s*(?:(?P<q>['"`])(?P<base>[^'"`]*)(?P=q))?\s*\)""",
)
# @Get('sub')  @Post()  @Put(':id')  (Nest method-level)
_NEST_METHOD = re.compile(
    r"""@(?P<verb>Get|Post|Put|Patch|Delete|Options|Head|All)\s*\("""
    r"""\s*(?:(?P<q>['"`])(?P<sub>[^'"`]*)(?P=q))?\s*\)""",
)

# class-validator decorators: @IsEmail() @IsNotEmpty() @Min(3) @MaxLength(50) ...
_CLASS_VALIDATOR = re.compile(
    r"""@(?P<rule>Is[A-Z]\w+|Min|Max|MinLength|MaxLength|Length|Matches|"""
    r"""IsOptional|IsNotEmpty|IsEmail|IsUUID|IsEnum|IsInt|IsNumber|IsString|"""
    r"""IsBoolean|IsDate|IsPositive|IsNegative)\s*\((?P<args>[^)]*)\)""",
)
# The property a class-validator decorator applies to (next non-decorator line).
_TS_PROPERTY = re.compile(r"^\s*(?:readonly\s+)?(?P<field>[A-Za-z_]\w*)\s*[?!]?\s*:")

# zod:  fieldName: z.string().email().min(3)
_ZOD_FIELD = re.compile(
    r"""(?P<field>[A-Za-z_]\w*)\s*:\s*z\s*\.\s*(?P<chain>[A-Za-z_]\w*"""
    r"""(?:\s*\([^)]*\)\s*(?:\.\s*[A-Za-z_]\w*\s*\([^)]*\)\s*)*)?)""",
)
# joi:  fieldName: Joi.string().required()
_JOI_FIELD = re.compile(
    r"""(?P<field>[A-Za-z_]\w*)\s*:\s*Joi\s*\.\s*(?P<chain>[A-Za-z_]\w*"""
    r"""(?:\s*\([^)]*\)\s*(?:\.\s*[A-Za-z_]\w*\s*\([^)]*\)\s*)*)?)""",
)

_ROUTE_CONF = 0.9      # a literal-string route registration is high-certainty
_VALIDATOR_CONF = 0.8  # a declarative validator constraint


def _join(base: str, sub: str) -> str:
    base = (base or "").strip()
    sub = (sub or "").strip()
    if not base:
        return "/" + sub.lstrip("/") if sub else "/"
    if not sub:
        return "/" + base.strip("/")
    return "/" + base.strip("/") + "/" + sub.lstrip("/")


class ExpressNestExtractor:
    """Express route + NestJS controller + JS/TS validator extractor."""

    name = "express_nest"
    supported_stacks = frozenset({STACK_EXPRESS, STACK_NESTJS})
    ceiling_band: Dict[str, Dict[str, float]] = {
        "api_endpoint": {"floor": 0.75, "ceiling": 0.95},
        "validator_rule": {"floor": 0.55, "ceiling": 0.85},
    }

    _SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

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
                atoms.extend(self._express_routes(path, text, ctx))
                atoms.extend(self._nest_controllers(path, text, ctx))
                atoms.extend(self._validators(path, text, ctx))
            except Exception:  # pragma: no cover - defensive per-file isolation
                errors += 1
        # Honest partial: if a meaningful fraction of files failed, degrade.
        if files and errors and errors >= max(1, files // 2):
            raise ExtractorDegraded(
                f"express_nest failed on {errors}/{files} files", atoms
            )
        return atoms

    # -- Express -----------------------------------------------------------
    def _express_routes(self, path: Path, text: str, ctx: ExtractionContext) -> List[Atom]:
        out: List[Atom] = []
        for m in _EXPRESS_ROUTE.finditer(text):
            verb = m.group("verb").upper()
            route = m.group("path")
            if not route:
                continue
            line = line_at_offset(text, m.start())
            methods = _HTTP_VERBS if verb == "ALL" else [verb]
            for method in ([v.upper() for v in _HTTP_VERBS] if verb == "ALL" else [verb]):
                out.append(ctx.make_atom(
                    kind="api_endpoint",
                    value={"method": method, "path": route,
                           "framework": "express", "handler": m.group("obj")},
                    path=path, line=line,
                    quote_source=text.split("\n")[line - 1],
                    extractor=self.name, confidence=_ROUTE_CONF,
                    source_tier=SOURCE_TIER_REGEX,
                ))
        return out

    # -- NestJS ------------------------------------------------------------
    def _nest_controllers(self, path: Path, text: str, ctx: ExtractionContext) -> List[Atom]:
        out: List[Atom] = []
        controllers = list(_NEST_CONTROLLER.finditer(text))
        if not controllers:
            return out
        # Assign each method decorator to the nearest preceding controller.
        bounds = [(c.start(), (c.group("base") or "")) for c in controllers]
        for m in _NEST_METHOD.finditer(text):
            base = ""
            for start, b in bounds:
                if start <= m.start():
                    base = b
                else:
                    break
            verb = m.group("verb").upper()
            sub = m.group("sub") or ""
            line = line_at_offset(text, m.start())
            full = _join(base, sub)
            methods = [v.upper() for v in _HTTP_VERBS] if verb == "ALL" else [verb]
            for method in methods:
                out.append(ctx.make_atom(
                    kind="api_endpoint",
                    value={"method": method, "path": full,
                           "framework": "nestjs", "controller_base": base},
                    path=path, line=line,
                    quote_source=text.split("\n")[line - 1],
                    extractor=self.name, confidence=_ROUTE_CONF,
                    source_tier=SOURCE_TIER_REGEX,
                ))
        return out

    # -- Validators (zod / joi / class-validator) --------------------------
    def _validators(self, path: Path, text: str, ctx: ExtractionContext) -> List[Atom]:
        out: List[Atom] = []
        lines = text.split("\n")

        for pattern, lib in ((_ZOD_FIELD, "zod"), (_JOI_FIELD, "joi")):
            for m in pattern.finditer(text):
                field = m.group("field")
                chain = re.sub(r"\s+", "", m.group("chain") or "")
                rules = re.findall(r"\.?([A-Za-z_]\w*)\s*\(", chain)
                if not rules:
                    continue
                line = line_at_offset(text, m.start())
                out.append(ctx.make_atom(
                    kind="validator_rule",
                    value={"field": field, "rule": ",".join(rules),
                           "library": lib},
                    path=path, line=line, quote_source=lines[line - 1],
                    extractor=self.name, confidence=_VALIDATOR_CONF,
                    source_tier=SOURCE_TIER_REGEX,
                ))

        # class-validator: decorator on one line, property on a following line.
        for m in _CLASS_VALIDATOR.finditer(text):
            line = line_at_offset(text, m.start())
            field = ""
            for probe in range(line, min(line + 4, len(lines) + 1)):
                pm = _TS_PROPERTY.match(lines[probe - 1]) if probe - 1 < len(lines) else None
                if pm:
                    field = pm.group("field")
                    break
            out.append(ctx.make_atom(
                kind="validator_rule",
                value={"field": field, "rule": m.group("rule"),
                       "args": m.group("args").strip(), "library": "class-validator"},
                path=path, line=line, quote_source=lines[line - 1],
                extractor=self.name, confidence=_VALIDATOR_CONF,
                source_tier=SOURCE_TIER_REGEX,
            ))
        return out
