"""Seed Manifest builder (Phase 1) — joins real substrate signals → a two-mode manifest.

Reads what the crawl ACTUALLY observed — the per-field inventory
(``field_inventory_for_artifact``), the OBSERVE oracle candidates
(``value_candidates_for_artifact``), and the app's already-provided values (the
answer_key ``fill`` keys, which stand in for the Phase-4 client data library as the
CARRY source) — and runs the pure six-disposition classifier over them.

Everything grounded and value-free: labels/types/options only from the substrate, no
LLM in the path (Phase 3 adds that as a refinement). The result is the two-mode
manifest the portal renders: ``recommended`` (only ASK + APPROVE) and ``full`` (every
field, pre-filled + editable), plus the ``prefill`` projection for the crawl's fill.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .dispositions import FieldSignal, classify_manifest
from .synthesis import field_inventory_for_artifact, value_candidates_for_artifact


def library_keys_from_answer_key(answer_key: Mapping[str, Any] | None) -> list[str]:
    """The CARRY vocabulary: labels the client has already provided a value for.

    Sourced from the app answer_key's ``fill`` map today; Phase 4 swaps in the
    encrypted per-client data library slots without changing this contract.
    """
    ak = answer_key if isinstance(answer_key, Mapping) else {}
    fill = ak.get("fill") if isinstance(ak.get("fill"), Mapping) else {}
    return [str(k) for k in fill.keys() if str(k).strip()]


async def build_seed_manifest(
    tenant_id: str,
    artifact_id: str,
    *,
    answer_key: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> dict:
    """Assemble the two-mode Seed Manifest for a crawled artifact.

    ``artifact_id`` empty (app never crawled) yields an empty-but-honest manifest so
    the portal can say "crawl first" rather than error.
    """
    if not artifact_id:
        return {
            "artifact_id": "", "recommended": [], "full": [], "prefill": {},
            "counts": {}, "ask_count": 0, "approve_count": 0, "autonomous_count": 0,
            "status": "no_crawl",
        }

    inventory = await field_inventory_for_artifact(tenant_id, artifact_id)
    candidates = await value_candidates_for_artifact(tenant_id, artifact_id)

    signals = [
        FieldSignal(
            label=f["label"], type=f.get("type", "text"),
            options=tuple(f.get("options") or ()), required=bool(f.get("required")),
        )
        for f in inventory
    ]
    library_keys = library_keys_from_answer_key(answer_key)
    observe_labels = [str(c.get("label") or "") for c in candidates if c.get("label")]

    manifest = classify_manifest(
        signals,
        library_keys=library_keys,
        observe_labels=observe_labels,
        observe_targets=candidates,
        today=today,
    )
    manifest["artifact_id"] = artifact_id
    manifest["status"] = "ready" if signals else "no_fields"
    return manifest
