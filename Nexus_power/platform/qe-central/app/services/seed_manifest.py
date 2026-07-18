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

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from .coverage_ledger import build_coverage_ledger
from .dispositions import (
    FieldSignal,
    classify_manifest,
    is_action_label,
    normalize_label,
)
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
    seed_fields: Iterable[str] = (),
    opaque_surfaces: Iterable[Mapping] = (),
    today: date | None = None,
) -> dict:
    """Assemble the two-mode Seed Manifest for a crawled artifact.

    ``artifact_id`` empty (app never crawled) yields an empty-but-honest manifest so
    the portal can say "crawl first" rather than error.

    ``seed_fields`` are the crawler's own ``coverage.fields_needing_seed`` — a RICHER
    account of what blocked deeper coverage than ``form_snapshot_signals`` alone (which
    may only have captured the entry/login page). Any such field not already in the
    inventory is added; lacking a type/options, the classifier fail-closes it to ASK —
    exactly right for a deep-form field the crawl couldn't ground (e.g. a transfer's
    From Account / Payee behind a login). This keeps the manifest consistent with the
    crawl's SEEDS_NEEDED diagnosis instead of showing only the login form.
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
    # Merge the crawler-flagged seed fields the inventory missed (deep pages behind a
    # login) — deduped by normalized label. The crawler's coverage list mixes real
    # data-input fields (From Account, Payee) with UI ACTION controls (buttons/toggles
    # like "Mark ... as done", "Enable ..."); only data fields belong in a "provide a
    # value" manifest, so action-shaped labels are filtered out.
    have = {normalize_label(s.label) for s in signals}
    for f in seed_fields:
        lbl = str(f or "").strip()
        if lbl and not is_action_label(lbl) and normalize_label(lbl) not in have:
            signals.append(FieldSignal(label=lbl))
            have.add(normalize_label(lbl))

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
    # The coverage ledger — the honesty spine: a measured per-app coverage number + every
    # control we did NOT fully capture, named with a reason. Computed over what the crawl
    # actually captured (the field inventory), so "did we miss anything?" is answerable.
    manifest["coverage"] = build_coverage_ledger(inventory, opaque_surfaces=opaque_surfaces)
    return manifest
