"""Transient store for failure-state a11y captures (TrueFix re-anchor, P-B).

The gated ``afterEach`` capture fixture (compiler `_HEAL_CAPTURE_AFTEREACH`) posts
the live accessibility tree at the moment a step failed; the re-anchor resolver
reads it back within seconds (same heal flow). In-memory + bounded so it needs no
migration and can't grow unbounded. Per-worker (not shared across workers) — fine:
the capture re-run and the resolve happen in the same heal request flow.
"""

from __future__ import annotations

from collections import OrderedDict

_MAX_ENTRIES = 256

# key: (tenant_id, artifact_id, scenario_id) -> {"nodes": [...], "status": str}
_store: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()


def put(*, tenant_id: str, artifact_id: str, scenario_id: str,
        nodes: list[dict], status: str = "") -> None:
    key = (tenant_id or "", artifact_id or "", scenario_id or "")
    _store[key] = {"nodes": list(nodes or []), "status": status or ""}
    _store.move_to_end(key)
    while len(_store) > _MAX_ENTRIES:
        _store.popitem(last=False)


def get(*, tenant_id: str, artifact_id: str, scenario_id: str) -> dict | None:
    return _store.get((tenant_id or "", artifact_id or "", scenario_id or ""))


def clear(*, tenant_id: str, artifact_id: str, scenario_id: str) -> None:
    _store.pop((tenant_id or "", artifact_id or "", scenario_id or ""), None)


__all__ = ["put", "get", "clear"]
