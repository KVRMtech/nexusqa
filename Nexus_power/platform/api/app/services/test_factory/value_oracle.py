"""Business-value oracle — compile a client's expected OUTCOMES into GROUNDED,
deterministic Playwright assertions (ANSWERS P1, design DATA_ANSWERS §1.2).

This is the differentiator: proving the app's OUTPUT is correct (a computed
``$28.40`` premium, a ``UW-17`` decline code) — not just that it still *behaves*.

DOCTRINE — never green-wash (mirrors ``oracle_scorecard`` PROVEN-vs-INFERRED):
  * An assertion is emitted ONLY when it can be GROUNDED in a real DOM node — the
    client's ``source_hint`` locator (primary), or a captured field label
    (fallback).  An outcome we cannot ground yields an honest ``// UNVERIFIED``
    comment — NEVER a silent pass.
  * A grounded value MISMATCH compiles to a HARD assertion (``toContainText`` or a
    numeric throw carrying the phrase "unexpected value"), which the FROZEN verdict
    reducer (``test_runs.outcome_contradicted_from_error``) already classifies as
    PROVEN ``real_regression`` — this module adds NO new verdict logic.
  * A node that does not resolve at run time throws "not found" → the reducer
    buckets it INFERRED/needs-review (honest), never a pass.

Deterministic at run time: pure string/number comparison over the live DOM. No
per-assertion LLM (LLM is authoring-time only — Phase 2).  The input is the CLEAN
value-oracle contract qe-central already projected
(``{field, when, expected, tolerance, source_hint, match}``).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

#: Injected once per spec that uses a numeric value oracle. Reads the grounded
#: node's text, extracts a single number (tolerant of $,%, commas, whitespace) and
#: THROWS on a > tolerance miss — the throw text says "unexpected value" so the
#: frozen verdict reducer classifies a real miss as PROVEN. A node that is absent
#: makes ``innerText()`` throw "not found" → honest INFERRED, never a pass.
NXNUM_JS = (
    r"""async function __nxNum(loc, expected, tol){"""
    r"""const t = await loc.first().innerText();"""
    r"""const m = String(t).replace(/[,\s$%€£]/g,'').match(/-?\d+(?:\.\d+)?/);"""
    r"""if(!m) throw new Error('value oracle: no number found in \"'+t+'\" (unverifiable)');"""
    r"""const got = parseFloat(m[0]);"""
    r"""if(Math.abs(got-expected) > tol) throw new Error("""
    r"""'value oracle: unexpected value — expected '+expected+' ±'+tol+' but received '+got);"""
    r"""}"""
)

#: Injected once per spec that uses a numeric INVARIANT (P3 rule oracle). Reads the
#: grounded node's number and throws "unexpected value" when the bound is violated
#: — so the frozen reducer classifies a real breach PROVEN. Absent node → not found
#: → INFERRED. Same honest contract as ``__nxNum``.
NXCMP_JS = (
    r"""async function __nxCmp(loc, op, bound){"""
    r"""const t = await loc.first().innerText();"""
    r"""const m = String(t).replace(/[,\s$%€£]/g,'').match(/-?\d+(?:\.\d+)?/);"""
    r"""if(!m) throw new Error('invariant: no number found in \"'+t+'\" (unverifiable)');"""
    r"""const got = parseFloat(m[0]);"""
    r"""const ok = op==='<='?got<=bound:op==='>='?got>=bound:op==='<'?got<bound"""
    r""":op==='>'?got>bound:op==='=='?got===bound:op==='!='?got!==bound:null;"""
    r"""if(ok===null) throw new Error('invariant: unknown operator '+op);"""
    r"""if(!ok) throw new Error('invariant violated: unexpected value '+got+' is not '+op+' '+bound);"""
    r"""}"""
)


def _js_str(value: Any) -> str:
    """Escape a value for embedding inside a single-quoted JS string literal."""
    return (
        str("" if value is None else value)
        .replace("\\", "\\\\").replace("'", "\\'")
        .replace("\n", "\\n").replace("\r", "")
    )


def _regex_token(value: str) -> str:
    """A regex-safe token for a tolerant ``toContainText(/tok/i)`` — the first
    alphanumeric run of length ≥2, escaped.  '' when the value has no stable token
    (caller then falls back to a non-empty/visible check, never a brittle mirror)."""
    m = re.search(r"[A-Za-z0-9]{2,}", value or "")
    return re.escape(m.group(0)) if m else ""


def _locator_expr(outcome: Mapping[str, Any], field_meta: Mapping[str, Any] | None) -> str | None:
    """The JS Playwright locator expression grounding this outcome, or ``None``.

    Primary: the client's ``source_hint`` (a Playwright locator string — CSS like
    ``#premium`` or an engine selector).  Fallback: a captured field label matching
    the outcome ``field`` → ``getByLabel`` (the readonly-input/committed-value case
    the crawler already captures).  ``None`` ⇒ ungroundable ⇒ UNVERIFIED.
    """
    hint = str(outcome.get("source_hint") or "").strip()
    if hint:
        return f"page.locator('{_js_str(hint)}')"
    field = str(outcome.get("field") or "").strip()
    labels = (field_meta or {}).get("labels") if isinstance(field_meta, Mapping) else None
    if field and isinstance(labels, (list, tuple, set)) and field in labels:
        return f"page.getByLabel('{_js_str(field)}')"
    return None


def value_assertion_lines(
    outcomes: Sequence[Mapping[str, Any]],
    *, field_meta: Mapping[str, Any] | None = None, indent: str = "  ",
) -> tuple[list[str], bool]:
    """Compile value expectations → grounded Playwright assertion lines.

    Returns ``(lines, uses_nxnum)`` — ``uses_nxnum`` tells the compiler whether to
    inject :data:`NXNUM_JS`.  Each outcome becomes exactly ONE of:

      * a HARD numeric throw ``await __nxNum(loc, expected, tol);``  (match=numeric)
      * a HARD ``await expect(loc).toContainText(/tok/i);``          (exact/contains)
      * an honest ``// UNVERIFIED value oracle: 'field' …`` comment  (ungroundable)

    Pure + deterministic; safe against a malformed outcome (skipped with a comment).
    """
    lines: list[str] = []
    uses_nxnum = False
    for oc in outcomes or ():
        if not isinstance(oc, Mapping):
            continue
        field = str(oc.get("field") or "").strip() or "(unnamed)"
        loc = _locator_expr(oc, field_meta)
        if loc is None:
            lines.append(
                f"{indent}// UNVERIFIED value oracle: '{_js_str(field)}' — no source_hint "
                "and no captured node to ground against (author a source_hint)."
            )
            continue
        match = str(oc.get("match") or "").strip().lower()
        expected = oc.get("expected")
        if match == "numeric":
            try:
                exp = float(expected)
            except (TypeError, ValueError):
                lines.append(
                    f"{indent}// UNVERIFIED value oracle: '{_js_str(field)}' — expected "
                    "value is not numeric.")
                continue
            tol = oc.get("tolerance")
            try:
                tol = float(tol) if tol is not None else 0.0
            except (TypeError, ValueError):
                tol = 0.0
            uses_nxnum = True
            lines.append(
                f"{indent}await __nxNum({loc}, {exp!r}, {tol!r}); "
                f"// PROVEN value oracle: '{_js_str(field)}' == {exp} ±{tol}")
        else:
            tok = _regex_token(str(expected))
            if not tok:
                lines.append(
                    f"{indent}await expect({loc}).not.toBeEmpty(); "
                    f"// value oracle: '{_js_str(field)}' present (no stable token to match)")
                continue
            lines.append(
                f"{indent}await expect({loc}).toContainText(/{tok}/i); "
                f"// PROVEN value oracle: '{_js_str(field)}' contains {expected!r}")
    return lines, uses_nxnum


#: Operators a bound invariant may use (client aliases → JS operator).
_OP_ALIASES = {
    "<=": "<=", "le": "<=", "lte": "<=", "max": "<=", "at_most": "<=",
    ">=": ">=", "ge": ">=", "gte": ">=", "min": ">=", "at_least": ">=",
    "<": "<", "lt": "<", "below": "<",
    ">": ">", "gt": ">", "above": ">",
    "==": "==", "eq": "==", "equals": "==", "=": "==",
    "!=": "!=", "ne": "!=", "not": "!=",
}


def rule_assertion_lines(
    rules: Sequence[Mapping[str, Any]],
    *, field_meta: Mapping[str, Any] | None = None, indent: str = "  ",
) -> tuple[list[str], bool]:
    """Compile business INVARIANTS (P3) → grounded Playwright assertions.

    Supports the SINGLE-RUN, groundable subset — a numeric BOUND on one displayed
    value (``coverage ≤ $2,000,000``, ``premium > 0``, ``discount ≤ 100``): the
    kind of guardrail that catches a whole class of regressions from ONE run.

    A rule shape: ``{kind:'bound', field, op, limit, source_hint}`` where ``op`` is
    ``<=|>=|<|>|==|!=`` (or an alias like ``max``/``min``).  Returns ``(lines,
    uses_nxcmp)``.  RELATIONAL / cross-persona invariants (``smoker premium ≥ 40%
    higher``, ``monotonic in coverage``) need a multi-persona run harness and are
    emitted as an honest ``// UNVERIFIED invariant`` here — never a silent pass.
    """
    lines: list[str] = []
    uses_nxcmp = False
    for rule in rules or ():
        if not isinstance(rule, Mapping):
            continue
        kind = str(rule.get("kind") or rule.get("type") or "").strip().lower()
        field = str(rule.get("field") or rule.get("name") or "").strip() or "(invariant)"
        stmt = str(rule.get("statement") or rule.get("description") or "").strip()
        desc = stmt or field
        if kind not in ("bound", "range", "limit", "cap", "floor", ""):
            lines.append(
                f"{indent}// UNVERIFIED invariant: {_js_str(desc)} — kind '{_js_str(kind)}' "
                "needs a multi-persona run harness (relational/cross-run), not yet grounded.")
            continue
        op = _OP_ALIASES.get(str(rule.get("op") or rule.get("operator") or "").strip().lower())
        limit = rule.get("limit")
        if limit is None:
            limit = rule.get("max", rule.get("min", rule.get("value", rule.get("bound"))))
            if op is None and rule.get("max") is not None:
                op = "<="
            elif op is None and rule.get("min") is not None:
                op = ">="
        try:
            bound = float(limit)
        except (TypeError, ValueError):
            bound = float("nan")
        if bound != bound:  # NaN (incl. float("NaN")) — a nonsensical bound, never emit
            lines.append(
                f"{indent}// UNVERIFIED invariant: {_js_str(desc)} — no numeric bound to compare.")
            continue
        if op is None:
            lines.append(
                f"{indent}// UNVERIFIED invariant: {_js_str(desc)} — no comparison operator.")
            continue
        loc = _locator_expr(rule, field_meta)
        if loc is None:
            lines.append(
                f"{indent}// UNVERIFIED invariant: {_js_str(desc)} — no source_hint / grounded node "
                f"for '{_js_str(field)}'.")
            continue
        uses_nxcmp = True
        lines.append(
            f"{indent}await __nxCmp({loc}, '{op}', {bound!r}); "
            f"// PROVEN invariant: '{_js_str(field)}' {op} {bound}")
    return lines, uses_nxcmp


__all__ = ["value_assertion_lines", "rule_assertion_lines", "NXNUM_JS", "NXCMP_JS"]
