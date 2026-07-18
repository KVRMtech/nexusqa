"""Coverage Ledger — the honesty spine that answers "did we miss anything on this app?"
with a MEASURED number instead of a surprise.

For every captured control it assigns one disposition and a plain reason:

  FULLY     — captured and usable: a value field, a boolean, or a choice with its options.
  PARTIAL   — present but under-captured: a choice whose options weren't read, a dependent
              captured for one branch only, a slider/color we don't value yet, a radio whose
              group isn't assembled.
  UNHANDLED — a control kind we don't handle at all (named, never hidden).
  OPAQUE    — a surface the DOM can't read (canvas / closed-shadow / cross-origin). NOTE:
              detecting these requires the crawler's opaque-surface emitter (roadmap P0.2);
              until that ships this ledger measures only what reached the substrate, and
              says so — it never claims an opaque surface was covered.

The per-app coverage number is FULLY / (total non-opaque controls), reported ALONGSIDE the
explicit partial / unhandled / opaque counts — never a single rolled-up green number. Every
non-FULLY control surfaces as a named line-item with the reason, so the product can always
say exactly what it reached and what it did not. This is a pure function over the captured
field inventory — no green-wash: a control is FULLY only when its facts were actually read.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

FULLY = "FULLY"
PARTIAL = "PARTIAL"
UNHANDLED = "UNHANDLED"
OPAQUE = "OPAQUE"

_CHOICE_TYPES = frozenset({"select", "combobox", "listbox"})
_VALUE_TYPES = frozenset({"text", "textarea", "email", "number", "date", "tel", "url", "password"})
_BOOL_TYPES = frozenset({"checkbox", "toggle", "switch"})
# Controls we recognise but do not yet fully capture — honest PARTIAL, not silent.
_PARTIAL_TYPES = frozenset({"slider", "color", "range"})


def classify_control(sig: Mapping) -> tuple[str, str]:
    """Disposition + plain reason for one ``form_snapshot_signals`` entry {type, options,
    required, depends_on}."""
    ftype = str(sig.get("type") or "").strip().lower()
    options = list(sig.get("options") or [])
    depends_on = str(sig.get("depends_on") or "").strip()

    if ftype in _CHOICE_TYPES:
        if not options:
            return PARTIAL, "a choice whose options weren't captured — re-crawl to read them"
        if depends_on:
            return PARTIAL, f"conditional on “{depends_on}” — options captured for one branch only"
        return FULLY, "a choice with its options captured"
    if ftype == "radio":
        return PARTIAL, "a radio option whose group isn't assembled yet (roadmap P1.1)"
    if ftype in _BOOL_TYPES:
        return FULLY, "a checkbox/toggle the crawl can set"
    if ftype in _PARTIAL_TYPES:
        return PARTIAL, f"a {ftype} — value not captured yet (roadmap RANGE-SET-READ)"
    if ftype in _VALUE_TYPES or not ftype:
        if depends_on:
            return PARTIAL, f"a field revealed only by “{depends_on}” — captured conditionally"
        return FULLY, "a value field captured"
    # A recognised label but an unmodelled control kind.
    return UNHANDLED, f"control kind “{ftype}” not handled yet — named, on the roadmap"


def build_coverage_ledger(
    inventory: Iterable[Mapping], *, opaque_surfaces: Iterable[Mapping] = (),
) -> dict:
    """Compute the coverage ledger over a captured field inventory
    (``[{label, type, options, required, depends_on}]``) plus the crawler-detected
    ``opaque_surfaces`` (``[{kind, label, reason}]`` — DOM-unreadable surfaces).

    Returns ``{controls, fully, partial, unhandled, opaque, total, coverage_pct, gaps,
    measures_captured_only}``. ``coverage_pct`` = FULLY / non-opaque total (0 when empty).
    ``gaps`` is the named non-FULLY line-items (the exact "here's what we did/didn't reach").
    """
    controls: list[dict] = []
    counts = {FULLY: 0, PARTIAL: 0, UNHANDLED: 0, OPAQUE: 0}
    gaps: list[dict] = []
    for f in inventory:
        label = str(f.get("label") or "").strip()
        if not label:
            continue
        disp, reason = classify_control(f)
        counts[disp] += 1
        row = {"label": label, "type": str(f.get("type") or ""), "disposition": disp, "reason": reason}
        if f.get("depends_on"):
            row["depends_on"] = str(f.get("depends_on"))
        controls.append(row)
        if disp != FULLY:
            gaps.append(row)

    # OPAQUE surfaces the DOM couldn't read — named blind spots, NEVER counted as covered.
    for o in opaque_surfaces:
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        counts[OPAQUE] += 1
        row = {"label": label, "type": str(o.get("kind") or "opaque"),
               "disposition": OPAQUE, "reason": str(o.get("reason") or "a surface the DOM can't read")}
        controls.append(row)
        gaps.append(row)

    # Total EXCLUDES opaque (they were never readable); coverage is over what could be read.
    total = len(controls)
    non_opaque = total - counts[OPAQUE]
    coverage_pct = round(100 * counts[FULLY] / non_opaque) if non_opaque else 0
    return {
        "controls": controls,
        "fully": counts[FULLY],
        "partial": counts[PARTIAL],
        "unhandled": counts[UNHANDLED],
        "opaque": counts[OPAQUE],
        "total": total,
        "coverage_pct": coverage_pct,
        "gaps": gaps,
        # Honest scope caveat: an opaque/skipped surface the crawler couldn't SEE isn't in
        # the inventory, so it can't be counted here yet — the crawler's opaque-surface
        # emitter (roadmap) closes that. This ledger measures what reached the substrate.
        "measures_captured_only": True,
    }
