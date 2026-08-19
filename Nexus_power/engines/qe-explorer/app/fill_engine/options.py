"""THE CANONICAL PLACEHOLDER RULE — is this entry an answer, or a prompt?

Moved here from :mod:`app.field_values`, verbatim in behaviour, because the
engine's lowest layers now need it too and a function-local import would hide
the dependency rather than remove it.  :mod:`app.field_values` re-exports both
names, so every existing caller — including
``forms._is_placeholder_option is field_values.is_placeholder_option``, which a
test pins — keeps working and there is still exactly ONE rule.

WHY ONE RULE MATTERS, in the words of the incident that produced it.  There
used to be two lists.  Fixing one still left the other choosing "Select coverage
amount…", so the quote funnel stayed shut behind a field the ledger reported as
filled: the option's underlying value is ``""``, the field is EMPTY, the
application's own validation never enables Continue, and the crawl stalls on a
page it believes it completed.  Observed live, twice, in two different modules.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = ["is_placeholder_option", "enumerate_real", "normalize_option"]

#: Option text that is a prompt, never a real choice.
_PLACEHOLDER = frozenset({
    "", "select", "choose", "please select", "select one", "select an option",
    "--", "---", "-- select --", "none", "choose one", "pick one", "select...",
})

#: An exact-phrase set cannot survive real applications: they write "Select
#: coverage amount...", "Choose your state", "-- Select term length --".  Those
#: are the SAME thing — the option whose underlying value is "".
_PLACEHOLDER_LEAD_VERBS = ("select", "choose", "pick")


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def is_placeholder_option(label: Any, *, first: bool = False) -> bool:
    """Is this the "nothing chosen yet" entry rather than a real answer?

    Deliberately conservative: the leading-verb rule applies only when ``first``
    (where placeholders conventionally live) or when the text trails off in an
    ellipsis, so a genuine product named "Choose Life Term 20" further down a
    list is still selectable.  A false positive silently discards a real
    business path, which is worse than occasionally keeping a placeholder.
    """
    text = _norm(label)
    if not text or text in _PLACEHOLDER:
        return True
    stripped = text.strip("-–—_ .·:…")
    if not stripped:
        return True
    lead = stripped.split()[0]
    trails_off = text.endswith(("...", "…"))
    if trails_off and lead in _PLACEHOLDER_LEAD_VERBS:
        return True
    # A FIRST option that trails off — "Feet...", "Inches...", "Year..." — is the
    # same "nothing chosen yet" entry wearing the field's own name instead of a
    # verb.  Observed live: the health step's height dropdowns were answered
    # "Feet..." / "Inches...", so the field stayed empty and the funnel stopped
    # one page short of the quote.  A real answer almost never trails off, and
    # restricting this to the first option keeps it safe.
    if first and trails_off:
        return True
    return bool(first) and lead in _PLACEHOLDER_LEAD_VERBS


def enumerate_real(raw: Any) -> list[str]:
    """Every option that is a real ANSWER, in order — placeholders dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(o).strip() for i, o in enumerate(raw)
            if str(o).strip() and not is_placeholder_option(o, first=(i == 0))]


def normalize_option(label: Any) -> str:
    """One normalization for every option/choice comparison: lowercased,
    whitespace-collapsed, bounded."""
    return re.sub(r"\s+", " ", str(label or "").strip().lower())[:80]
