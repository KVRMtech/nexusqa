"""R1 Interaction Ladder — per-archetype ordered mechanic rungs.

Each archetype (radio, select, slider, date, text) has an ordered list of
mechanic specifications tried until R0 ``verify_intent`` says True.  The
ladder is PURE DATA — no browser, no Playwright — so it is unit-testable
and auditable.  The live adapter in ``main.py`` executes each rung through
the existing ``_act`` primitive.

Design invariants:
  * The NATIVE mechanism is always rung 0 (fastest, most precise).
  * Subsequent rungs degrade gracefully toward more universal gestures.
  * A rung that errors or returns ``intent_met=False`` is skipped silently;
    ``intent_met=None`` (unverifiable) is treated as success (conservative).
  * The ladder NEVER includes a commit/danger/submit control — those are
    guarded upstream by the submit boundary.  This module operates FORM
    CONTROLS only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Rung:
    """One mechanic to try for a given archetype.

    ``kind`` is the Playwright gesture (``checked``, ``click``, ``fill``,
    ``select``, ``press``).  ``variant`` names the specific sub-strategy
    so the observation record says *which* rung won (e.g. ``click_label``,
    ``focus_space``).
    """
    kind: str
    variant: str


RADIO_LADDER: tuple[Rung, ...] = (
    Rung("checked", "native_set_checked"),
    Rung("click", "click_element"),
    Rung("press", "focus_space"),
)

CHECKBOX_LADDER: tuple[Rung, ...] = (
    Rung("checked", "native_set_checked"),
    Rung("click", "click_element"),
    Rung("press", "focus_space"),
)

SELECT_LADDER: tuple[Rung, ...] = (
    Rung("select", "native_select_option"),
    Rung("click_option", "open_click_option"),
)

SLIDER_LADDER: tuple[Rung, ...] = (
    Rung("fill", "native_fill"),
    Rung("press", "focus_arrow"),
)

DATE_LADDER: tuple[Rung, ...] = (
    Rung("fill", "native_fill"),
    Rung("press", "type_chars"),
)

TEXT_LADDER: tuple[Rung, ...] = (
    Rung("fill", "native_fill"),
    Rung("press", "type_chars"),
)

_LADDERS: dict[str, tuple[Rung, ...]] = {
    "radio": RADIO_LADDER,
    "checkbox": CHECKBOX_LADDER,
    "toggle": CHECKBOX_LADDER,
    "select": SELECT_LADDER,
    "slider": SLIDER_LADDER,
    "color": SLIDER_LADDER,
    "date": DATE_LADDER,
    "text": TEXT_LADDER,
}


def ladder_for(kind: str) -> tuple[Rung, ...]:
    """Return the mechanic ladder for a control archetype, or empty if none."""
    return _LADDERS.get(kind.strip().lower(), ())


def archetype_has_ladder(kind: str) -> bool:
    return kind.strip().lower() in _LADDERS
