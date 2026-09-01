"""SCIM filter parser (RFC 7644 §3.4.2.2).

Supports the subset that SCIM clients actually use:

    attribute eq "value"
    attribute ne "value"
    attribute co "value"            (contains, case-insensitive)
    attribute sw "value"            (starts with)
    attribute pr                     (present)
    attribute1 eq "x" and attribute2 eq "y"
    attribute1 eq "x" or attribute2 eq "y"

Parentheses are not required for our use cases. The parser is
deliberately strict: anything it doesn't recognise raises a
``SCIMError`` with ``scimType='invalidFilter'`` so the client gets
RFC-compliant feedback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .errors import SCIMError


_OPERATORS = {"eq", "ne", "co", "sw", "pr"}
_CONNECTIVES = {"and", "or"}


@dataclass(frozen=True)
class _Clause:
    attribute: str
    operator: str
    value: Optional[str]


@dataclass(frozen=True)
class SCIMFilter:
    clauses: tuple[_Clause, ...]
    connective: str  # "and" | "or"

    def matches(self, row: dict[str, Any]) -> bool:
        """In-Python evaluation — used by tests and for non-DB filters."""
        results = [_evaluate(c, row) for c in self.clauses]
        if not results:
            return True
        if self.connective == "or":
            return any(results)
        return all(results)


_TOKEN_RE = re.compile(
    r"""(?x)
    \"((?:[^\"\\]|\\.)*)\"      # quoted string
    | ([A-Za-z][A-Za-z0-9_:.\-]*)  # identifier (attribute, op, connective)
    """
)


def parse_scim_filter(expr: Optional[str]) -> SCIMFilter:
    if not expr or not expr.strip():
        return SCIMFilter(clauses=(), connective="and")
    tokens = _tokenize(expr)
    clauses: list[_Clause] = []
    connective: Optional[str] = None
    i = 0
    while i < len(tokens):
        attr = tokens[i]
        if attr in _CONNECTIVES:
            raise SCIMError(
                status=400,
                detail=f"unexpected connective at start: {attr!r}",
                scim_type="invalidFilter",
            )
        if i + 1 >= len(tokens):
            raise SCIMError(
                status=400,
                detail=f"filter clause missing operator after {attr!r}",
                scim_type="invalidFilter",
            )
        op = tokens[i + 1].lower()
        if op not in _OPERATORS:
            raise SCIMError(
                status=400,
                detail=f"unsupported operator: {op!r}",
                scim_type="invalidFilter",
            )
        if op == "pr":
            clauses.append(_Clause(attribute=attr, operator=op, value=None))
            i += 2
        else:
            if i + 2 >= len(tokens):
                raise SCIMError(
                    status=400,
                    detail=f"filter clause missing value after {attr!r} {op}",
                    scim_type="invalidFilter",
                )
            value = tokens[i + 2]
            clauses.append(_Clause(attribute=attr, operator=op, value=value))
            i += 3
        # Optional connective.
        if i < len(tokens):
            conn = tokens[i].lower()
            if conn not in _CONNECTIVES:
                raise SCIMError(
                    status=400,
                    detail=f"expected 'and'/'or', got {conn!r}",
                    scim_type="invalidFilter",
                )
            if connective is None:
                connective = conn
            elif connective != conn:
                # Mixed and/or — we don't model precedence; refuse.
                raise SCIMError(
                    status=400,
                    detail="filter mixes 'and' and 'or'; please split",
                    scim_type="invalidFilter",
                )
            i += 1
    return SCIMFilter(
        clauses=tuple(clauses), connective=connective or "and"
    )


# ── Internals ──────────────────────────────────────────────────


def _tokenize(expr: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    length = len(expr)
    while pos < length:
        c = expr[pos]
        if c.isspace() or c == "(" or c == ")":
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if m is None:
            raise SCIMError(
                status=400,
                detail=f"unexpected character at position {pos}",
                scim_type="invalidFilter",
            )
        quoted, ident = m.groups()
        if quoted is not None:
            # Unescape \\ and \" per RFC 7644 §3.4.2.2.
            tokens.append(
                quoted.replace('\\\\', '\\').replace('\\"', '"')
            )
        else:
            tokens.append(ident)
        pos = m.end()
    return tokens


def _evaluate(clause: _Clause, row: dict[str, Any]) -> bool:
    attribute = _extract(row, clause.attribute)
    if clause.operator == "pr":
        return attribute is not None and attribute != ""
    if attribute is None:
        return False
    text = str(attribute)
    value = clause.value or ""
    if clause.operator == "eq":
        return text == value
    if clause.operator == "ne":
        return text != value
    if clause.operator == "co":
        return value.lower() in text.lower()
    if clause.operator == "sw":
        return text.lower().startswith(value.lower())
    return False


def _extract(row: dict[str, Any], attribute: str) -> Any:
    """Resolve dotted SCIM paths against a flat or shallow-nested dict.

    Examples:
        ``userName``                → row["userName"]
        ``emails.value``            → first row["emails"][i]["value"]
        ``urn:...:User:userName``   → row["userName"]  (URN prefix stripped)
    """
    if not attribute:
        return None
    # Drop SCIM URN prefix when present.
    bare = attribute.split(":")[-1] if ":" in attribute else attribute
    parts = bare.split(".")
    node: Any = row
    for part in parts:
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and part in item:
                    node = item[part]
                    break
            else:
                return None
        elif isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        else:
            return None
    return node
