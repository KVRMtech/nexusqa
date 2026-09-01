"""THE INTELLIGENT FILL ENGINE — understand → generate → validate → repair.

The engine this package replaces filled CONTROLS.  It did not complete FORMS.
One linear pass — generate, fill, advance — with no way back: a value the
application rejected ended the page, because nothing ever read the rejection.
Every defect that mattered was a consequence of that shape.

    * a stale page banner marked every later fill on the page as errored,
      because validity was a property of the PAGE and not of a CONTROL;
    * money fields answered "100" — a constant, so a coverage amount and an
      annual income were the same number and neither agreed with the applicant;
    * a split date of birth received the YEAR in all three of month, day and
      year, because the option picker was handed ``date_of_birth[:4]`` whatever
      the part asked for;
    * "Beneficiary Name", "Spouse Name" and "Employer Name" all resolved to the
      applicant, because a classifier that reads only a semantic TYPE cannot see
      that a field belongs to somebody else;
    * radio groups and portal-rendered dropdowns were skipped unless an operator
      changed posture, so the most common widget in enterprise software was the
      one the engine did not answer;
    * ``pattern`` was captured, used to CLASSIFY, and never used to GENERATE.

The subsystems here are ordered the way a value flows, and each is importable,
pure and testable on its own:

    :mod:`persona`     one coherent household — the applicant, their spouse,
                       children, beneficiary and employer, with income that fits
                       the occupation and an age that matches the birth date.
    :mod:`roles`       WHOSE field is this?  Possessor-aware resolution, so a
                       beneficiary's name comes from the beneficiary.
    :mod:`constraints` what the application itself demands of the value.
    :mod:`patterns`    a bounded, deterministic satisfier for the regexes an
                       application declares.
    :mod:`generator`   a value that is semantically right AND constraint-legal
                       on the first attempt.
    :mod:`widgets`     what KIND of widget this is and how it is driven, so a
                       radio group and a Radix combobox are ordinary cases.
    :mod:`validation`  control-scoped validity.  A cookie banner is not a
                       verdict on the field you just typed into.
    :mod:`repair`      a bounded loop that retries only what an observed
                       rejection told it to change, and says why every time.
    :mod:`learning`    the key a remembered value is stored under, so learning
                       outlives the crawl that produced it.

DISCIPLINE, unchanged from the engine it replaces and the reason it is worth
replacing:  nothing here reads a clock, a network or a random source; the same
seed always yields the same person and the same values, so a run recorded months
ago replays exactly; and every value carries the provenance and the rationale
that produced it, so a reader can always ask WHY this field holds this.
"""
from __future__ import annotations

from .persona import Persona, Person, Employment, Money, derive_persona
from .roles import Possessor, resolve_possessor
from .constraints import Constraints, extract as extract_constraints, violations
from .generator import Candidate, generate
from .widgets import WidgetClass, classify_widget
from .validation import ValidationSignal, PageAlertFilter, signals_for_control
from .repair import RepairAttempt, RepairOutcome, RepairBudget, repair_loop
from .learning import memory_scope, MEMORY_SCOPE_VERSION

__all__ = [
    "Persona", "Person", "Employment", "Money", "derive_persona",
    "Possessor", "resolve_possessor",
    "Constraints", "extract_constraints", "violations",
    "Candidate", "generate",
    "WidgetClass", "classify_widget",
    "ValidationSignal", "PageAlertFilter", "signals_for_control",
    "RepairAttempt", "RepairOutcome", "RepairBudget", "repair_loop",
    "memory_scope", "MEMORY_SCOPE_VERSION",
]
