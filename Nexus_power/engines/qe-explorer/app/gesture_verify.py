"""U3 — verification read-backs for non-DOM gesture/coordinate actions (Δ2).

``browser.verify_intent`` has no vocabulary for gesture outcomes, so a drag /
canvas draw / custom slider degrades to ``intent_met=None`` and earns NO proven
credit — the difference between "we tested it" and "we clicked and hoped". These
pure predicates give each gesture a read-back:

  * ``True``  — PROVEN it registered (order changed / canvas inked / value moved);
  * ``False`` — refuted (nothing changed);
  * ``None``  — honestly unverifiable (no signal to read) → coverage stays
    ``G_INFERRED`` and the ladder descends.

The browser supplies the before/after signals (VM side); this module DECIDES, so
it unit-tests without a browser.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

GESTURE_DRAG = "drag"
GESTURE_DRAW = "draw"
GESTURE_SLIDER = "slider"


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def drag_registered(
    before_order: Optional[Sequence[Any]], after_order: Optional[Sequence[Any]]
) -> Optional[bool]:
    """A drag/reorder is PROVEN when the ordered item sequence changed while its
    MEMBERSHIP stayed the same; refuted when identical; ``None`` when the order was
    unreadable or membership changed (can't attribute that to the drag)."""
    if not before_order or not after_order:
        return None
    b, a = list(before_order), list(after_order)
    if sorted(map(str, b)) != sorted(map(str, a)):
        return None                      # items added/removed — not a clean reorder
    return b != a


def draw_registered(before_ink: Any, after_ink: Any) -> Optional[bool]:
    """A canvas draw is PROVEN when the surface went from empty to marked — the
    ink/pixel signal is truthy afterwards AND differs from before. ``before/after``
    may be a bool ("has ink") or a coarse hash. ``None`` when either is unknown."""
    if before_ink is None or after_ink is None:
        return None
    return bool(after_ink) and (after_ink != before_ink)


def slider_registered(
    before_value: Any, after_value: Any, target: Any = None
) -> Optional[bool]:
    """A custom slider is PROVEN when ``aria-valuenow`` moved (and, if a target is
    given, not further from it); refuted when unchanged; ``None`` when valuenow was
    unreadable."""
    b, a = _num(before_value), _num(after_value)
    if b is None or a is None:
        return None
    if b == a:
        return False
    t = _num(target)
    if t is not None:
        return abs(a - t) <= abs(b - t)
    return True


def verify_gesture(
    kind: str, before: Any, after: Any, target: Any = None
) -> Optional[bool]:
    """Dispatch a gesture read-back by kind. Unknown kind → ``None`` (unverifiable,
    honest — never a false PROVEN)."""
    if kind == GESTURE_DRAG:
        return drag_registered(before, after)
    if kind == GESTURE_DRAW:
        return draw_registered(before, after)
    if kind == GESTURE_SLIDER:
        return slider_registered(before, after, target)
    return None
