"""
Nexus Workflow Context — State management and value resolution.

The context is the shared "memory" of a running workflow.
Every stage reads from and writes to this context.

Structure:
    workflow:
        workflow_id, chain_id, tenant_id, session_id
        input: { ... original input ... }
    stages:
        <stage_id>:
            status: "completed" | "failed" | "skipped"
            output: { ... engine response ... }
    temp:
        item: <current for_each item>
        item_index: <current for_each index>
"""

from __future__ import annotations

import re
import copy
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WorkflowContext:
    """
    Manages workflow execution state and provides value resolution.

    Resolution syntax:
        $workflow.tenant_id              → literal value (preserves type)
        $stages.shield.output.safe_text  → nested dict access
        $workflow.input.documents.0      → list index access
        $temp.item                       → current for_each item

    String interpolation:
        "Report for ${workflow.session_id}"  → string with embedded values
    """

    def __init__(
        self,
        workflow_id: str,
        chain_id: str,
        tenant_id: str,
        session_id: str,
        input_data: dict,
    ):
        self._data: dict[str, Any] = {
            "workflow": {
                "workflow_id": workflow_id,
                "chain_id": chain_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "input": input_data or {},
            },
            "stages": {},
            "temp": {},
        }

    # ── Accessors ──────────────────────────────────────────────

    @property
    def data(self) -> dict:
        return self._data

    def set_stage_output(self, stage_id: str, output: Any):
        self._data["stages"].setdefault(stage_id, {})
        self._data["stages"][stage_id]["output"] = output

    def set_stage_status(self, stage_id: str, status: str):
        self._data["stages"].setdefault(stage_id, {})
        self._data["stages"][stage_id]["status"] = status

    def get_stage_output(self, stage_id: str) -> Any:
        return self._data.get("stages", {}).get(stage_id, {}).get("output")

    def get_stage_status(self, stage_id: str) -> Optional[str]:
        return self._data.get("stages", {}).get(stage_id, {}).get("status")

    def set_temp(self, key: str, value: Any):
        self._data["temp"][key] = value

    def clear_temp(self):
        self._data["temp"] = {}

    # ── Resolution ─────────────────────────────────────────────

    def resolve(self, path: str) -> Any:
        """
        Resolve a $-prefixed context path to its value.

        Returns the original value (preserving type) or None if not found.

        Examples:
            $workflow.tenant_id              → "tenant-123"
            $stages.shield.output.safe_text  → "redacted text..."
            $workflow.input.documents.0      → first document
            $temp.item                       → current for_each item
        """
        if not isinstance(path, str) or not path.startswith("$"):
            return path  # literal — return unchanged

        segments = path[1:].split(".")
        current: Any = self._data

        for segment in segments:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(segment)
            elif isinstance(current, (list, tuple)):
                try:
                    current = current[int(segment)]
                except (ValueError, IndexError):
                    return None
            else:
                current = getattr(current, segment, None)

        return current

    def resolve_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """
        Recursively resolve all $-prefixed values in a mapping dict.
        Non-$-prefixed values pass through as literals.
        """
        return self._resolve_value(mapping)

    def _resolve_value(self, value: Any) -> Any:
        """Recursively resolve a single value."""
        if isinstance(value, str):
            if value.startswith("$") and "${" not in value:
                # Pipe-fallback: "$path.a|$path.b" tries path.a first,
                # falls back to path.b when the first resolves to None.
                if "|" in value:
                    for alt in value.split("|"):
                        resolved = self.resolve(alt.strip())
                        if resolved is not None:
                            return resolved
                    return None
                # Full replacement — preserves original type
                return self.resolve(value)
            elif "${" in value:
                # String interpolation — always returns str
                return self._interpolate(value)
        elif isinstance(value, dict):
            return {k: self._resolve_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._resolve_value(item) for item in value]
        return value

    def _interpolate(self, template: str) -> str:
        """Replace ${path.to.value} tokens in a template string."""

        def _replacer(match: re.Match) -> str:
            path = "$" + match.group(1)
            val = self.resolve(path)
            return str(val) if val is not None else ""

        return re.sub(r"\$\{([\w.]+)}", _replacer, template)

    # ── Condition evaluation ───────────────────────────────────

    def evaluate_condition(self, condition: str) -> bool:
        """
        Evaluate a condition expression against the context.

        Simple truthiness (most common):
            "$workflow.input.audio_file_id"

        Comparison:
            "len($stages.rule_extraction.output.rules) > 0"
            "$workflow.input.skip_execution == false"

        Uses restricted eval with no builtins for safety.
        Chain definitions are admin-created, so eval is acceptable
        (same approach as Apache Airflow, Prefect, etc.).
        """
        if not condition or not condition.strip():
            return True

        stripped = condition.strip()

        # Fast path: simple context-path truthiness check
        if stripped.startswith("$") and " " not in stripped and "${" not in stripped:
            value = self.resolve(stripped)
            return bool(value)

        # Complex expression: resolve all $-tokens then eval
        try:
            resolved = self._resolve_condition_tokens(condition)

            safe_globals: dict[str, Any] = {
                "__builtins__": {},
                "len": len,
                "bool": bool,
                "int": int,
                "float": float,
                "str": str,
                "list": list,
                "dict": dict,
                "set": set,
                "abs": abs,
                "min": min,
                "max": max,
                "any": any,
                "all": all,
                "True": True,
                "False": False,
                "None": None,
                "true": True,
                "false": False,
                "null": None,
            }
            return bool(eval(resolved, safe_globals))  # noqa: S307
        except Exception as exc:
            import os
            fail_open = os.getenv("NEXUS_CONDITION_FAIL_OPEN", "false").lower() in ("true", "1", "yes")
            logger.warning(
                "Condition evaluation failed for '%s': %s — defaulting to %s",
                condition,
                exc,
                "True (fail-open)" if fail_open else "False (fail-closed)",
            )
            return fail_open  # Configurable: set NEXUS_CONDITION_FAIL_OPEN=false for strict mode

    def _resolve_condition_tokens(self, condition: str) -> str:
        """Replace $-prefixed tokens in condition strings with their repr()."""

        def _replacer(match: re.Match) -> str:
            token = match.group(0)
            value = self.resolve(token)
            return repr(value)

        return re.sub(r"\$[\w.]+", _replacer, condition)

    # ── Concurrency-safe copy for for_each ─────────────────────

    def with_temp(self, temp_data: dict) -> "WorkflowContext":
        """
        Create a lightweight copy that shares workflow/stages data
        but has isolated temp data — safe for concurrent for_each.

        Workflow and stages dicts are READ-ONLY during for_each execution
        (only the engine writes to them after all iterations complete),
        so sharing the reference is safe and avoids deep-copy overhead.
        """
        child = WorkflowContext.__new__(WorkflowContext)
        child._data = {
            "workflow": self._data["workflow"],
            "stages": self._data["stages"],
            "temp": dict(temp_data),
        }
        return child

    # ── Persistence ────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a deep copy of context data for Redis persistence."""
        return copy.deepcopy(self._data)

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "WorkflowContext":
        """Reconstruct a context from a persisted snapshot."""
        ctx = cls.__new__(cls)
        ctx._data = copy.deepcopy(snapshot)
        return ctx
