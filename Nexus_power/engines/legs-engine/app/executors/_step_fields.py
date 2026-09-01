"""Reading a step's EXPECTED text, whatever shape it arrived in.

Both executors read `step.expected_output`. `TestStep` (nexus_sdk.models) does
not declare that field — it declares `expected` and `expected_result` — so the
attribute exists only when a caller happened to pass it as an extra
(`model_config = ConfigDict(extra="allow")`). For every step built from the
declared fields the access raised

    AttributeError: 'TestStep' object has no attribute 'expected_output'

in the middle of executing a test, which is the worst place to discover it.

Reading through one accessor makes the executors tolerant of all three shapes
and keeps the precedence in a single documented place: the DECLARED fields win,
and the undeclared legacy key is honoured last so older payloads still run.
"""
from __future__ import annotations

from typing import Any


def expected_text(step: Any) -> str:
    """The step's expected-outcome text, or "" when it declares none."""
    for name in ("expected_result", "expected", "expected_output"):
        value = getattr(step, name, None)
        if value:
            return str(value)
    # Pydantic keeps undeclared keys in model_extra; a dict-shaped step may carry
    # them directly. Neither is guaranteed, so both are probed defensively.
    extra = getattr(step, "model_extra", None) or {}
    for name in ("expected_result", "expected", "expected_output"):
        if extra.get(name):
            return str(extra[name])
    if isinstance(step, dict):
        for name in ("expected_result", "expected", "expected_output"):
            if step.get(name):
                return str(step[name])
    return ""
