"""Consented export channel for the flywheel (Phase 2 foundation) — the safety
floor of the bet-the-company moat.

The federated design: a customer's CORRECTIONS make the system smarter for every
other customer WITHOUT raw data leaving the building ("the teacher shares the
lesson, never the students' files"). This module is that channel + its floor. It
reads ONLY consented, already-de-identified label rows (``ledger``), aggregates
them to COUNTS, DROPS any group below a k-anonymity floor, applies (stub)
differential-privacy noise, and seals the result with the tenant's envelope. No
raw row / value / name / selector can appear in the output — enforced by the
schema (the ledger has no raw column) AND by this count-only aggregation.

DEFAULT OFF (``NEXUS_FLYWHEEL_EXPORT``). DEFERRED (need N consented tenants + a
privacy-engineering build, per the Phase 2 design): the cross-tenant federation
loop and REAL differential privacy / secure aggregation. What's here is the
per-tenant export unit + k-anonymity + the seal + the DP interface stub — the
architecture, present from day one so the channel exists before the data flows.
"""

from __future__ import annotations

import json
import os
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .ledger import FlywheelLabelRow

DEFAULT_K = 20  # k-anonymity floor: never export a group seen fewer than k times.

# Coarse de-identified columns that form an export GROUP KEY. Deliberately EXCLUDES
# label_token_hash (per-tenant; could narrow a group) and all id/timestamp columns.
_GROUP_COLS = (
    "decision_point", "verb_enum", "emitted_method_enum", "observed_kind_enum",
    "has_grounded_select", "options_count_bucket", "value_shape_sig",
    "selector_drifted", "bbox_drifted", "is_flaky", "error_pattern_class",
    "similarity_bucket", "ambiguity_bucket", "diff_shape_enum",
    "engine_verdict_enum", "human_decision_enum", "vertical_tag",
)


def export_enabled() -> bool:
    """Master export gate — DEFAULT OFF. Capturing a label never means it may
    leave the building; export is a separate, explicit, per-deployment opt-in."""
    return os.getenv("NEXUS_FLYWHEEL_EXPORT", "").strip().lower() in {"1", "true", "yes", "on"}


def _group_key(row) -> tuple:
    return tuple(getattr(row, c, None) for c in _GROUP_COLS)


def aggregate_labels(rows, *, k: int = DEFAULT_K) -> list[dict]:
    """Group de-identified rows by their coarse feature tuple, COUNT, and DROP any
    group with count < k (k-anonymity — a group too small to be non-identifying
    never leaves). Returns [{...enum/bucket group features..., count,
    verified_green_count}]. Pure; the raw label NEVER appears — only enum/bucket
    group keys + counts (which are the "lesson")."""
    counts = Counter(_group_key(r) for r in rows)
    by_key: dict[tuple, list] = {}
    for r in rows:
        by_key.setdefault(_group_key(r), []).append(r)
    out: list[dict] = []
    for key, n in counts.items():
        if n < k:
            continue  # k-anonymity drop — too small to be safely non-identifying
        grp = by_key[key]
        d = dict(zip(_GROUP_COLS, key))
        d["count"] = n
        # The OUTCOME signal within the group (the actual lesson): how often the
        # correction proved green vs was later contradicted.
        d["verified_green_count"] = sum(1 for r in grp if r.verified_green is True)
        d["later_contradicted_count"] = sum(1 for r in grp if r.later_contradicted is True)
        out.append(d)
    return out


def add_dp_noise(aggregates: list[dict], *, epsilon: float = 1.0) -> list[dict]:
    """STUB — differential privacy interface. The REAL mechanism (Laplace/Gaussian
    per count with epsilon-accounting and a per-group sensitivity bound given k) is
    DEFERRED to a privacy-engineering build. This marked pass-through exists so the
    export SHAPE + interface are present from day one; the output is flagged
    ``_dp_applied=False`` so it can NEVER be mistaken for actually-private until the
    real mechanism lands."""
    # TODO(phase2-federated): replace with calibrated DP noise + epsilon accounting.
    return [{**a, "_dp_applied": False, "_epsilon": epsilon} for a in aggregates]


async def build_tenant_export(
    session: AsyncSession, *, tenant_id: str, vertical: str = "",
    k: int = DEFAULT_K, epsilon: float = 1.0,
) -> dict:
    """Build ONE tenant's consented, k-anonymized, (stub-)DP aggregate — the ONLY
    thing that may cross the boundary. Reads ONLY ``consented=true`` rows within
    the caller's ``tenant_scoped_session`` (RLS-isolated). Returns counts only;
    NEVER raw rows. OFF (empty) unless ``export_enabled()``."""
    if not export_enabled():
        return {"enabled": False, "groups": [], "raw_rows_exported": 0}
    q = select(FlywheelLabelRow).where(FlywheelLabelRow.consented.is_(True))
    if vertical:
        q = q.where(FlywheelLabelRow.vertical_tag == vertical)
    rows = (await session.execute(q)).scalars().all()
    aggregates = add_dp_noise(aggregate_labels(rows, k=k), epsilon=epsilon)
    return {
        "enabled": True, "tenant_id": tenant_id, "vertical": vertical or "all",
        "k": k, "epsilon": epsilon, "consented_rows_seen": len(rows),
        "group_count": len(aggregates), "groups": aggregates,
        "raw_rows_exported": 0,  # invariant: rows never leave — only counts
    }


async def seal_export(envelope, *, tenant_id: str, payload: dict,
                      aad: bytes = b"flywheel-export") -> bytes | None:
    """Seal an export payload with the tenant's envelope (AES-GCM via
    EnvelopeService) so it leaves the building ENCRYPTED. Fail-closed: returns None
    when no envelope is configured (never export plaintext)."""
    if envelope is None:
        return None
    blob = await envelope.encrypt(
        tenant_id, json.dumps(payload, sort_keys=True).encode("utf-8"), aad=aad,
    )
    return blob.to_bytes()


__all__ = [
    "export_enabled", "aggregate_labels", "add_dp_noise",
    "build_tenant_export", "seal_export", "DEFAULT_K",
]
