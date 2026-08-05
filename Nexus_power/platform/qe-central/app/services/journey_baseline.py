"""O0 — Journey baseline lifecycle (captured → approved → validated → drifted).

A captured outcome is an OBSERVATION; it becomes an EXPECTATION only when a
human who knows the business approves it.  This module owns every transition:

  captured  → approved   SME clicked Approve (e-signature required)
  approved  → validated  a later crawl/run matched the approved baseline
  approved  → drifted    a later crawl/run DIFFERS — awaiting adjudication
  validated → validated  a later crawl/run still matches
  validated → drifted    a later crawl/run DIFFERS
  drifted   → approved   adjudicator rules "intended change" (re-approve)
  (any)     → captured   adjudicator rules "defect" on a drifted journey
                         (baseline revoked — reverts to unverified)

Never auto-absorb drift; never auto-fail on it.  Everything unapproved
wears UNVERIFIED openly.

The pure functions (``outcome_hash``, ``outcomes_match``, ``build_snapshot``,
``diff_outcomes``) carry all the comparison logic and are unit-testable with
no database.  The async helpers persist through the tenant-scoped session.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import JourneyRow, JourneyTraversalRow
from .approval import (
    SUBJECT_JOURNEY_BASELINE,
    append_event as append_approval_event,
    list_events as list_approval_events,
)

logger = logging.getLogger(__name__)

# ── Baseline lifecycle states ────────────────────────────────────────────────
BASELINE_CAPTURED = "captured"
BASELINE_APPROVED = "approved"
BASELINE_VALIDATED = "validated"
BASELINE_DRIFTED = "drifted"
BASELINE_STATES = frozenset(
    {BASELINE_CAPTURED, BASELINE_APPROVED, BASELINE_VALIDATED, BASELINE_DRIFTED}
)

# ── Adjudication verdicts ────────────────────────────────────────────────────
ADJUDICATION_INTENDED_CHANGE = "intended_change"
ADJUDICATION_DEFECT = "defect"
ADJUDICATION_VERDICTS = frozenset(
    {ADJUDICATION_INTENDED_CHANGE, ADJUDICATION_DEFECT}
)


# ── Pure comparison primitives (no DB) ───────────────────────────────────────

def _canonical_outcomes(outcome_values: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic canonical JSON for outcome_values, sorted by label."""
    normalized = []
    for v in (outcome_values or []):
        if not isinstance(v, Mapping):
            continue
        normalized.append({
            "label": str(v.get("label") or "").strip().lower(),
            "value": str(v.get("value") or "").strip(),
            "value_type": str(v.get("value_type") or "").strip().lower(),
        })
    normalized.sort(key=lambda x: (x["label"], x["value_type"]))
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def outcome_hash(outcome_values: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the canonical outcome_values — the drift-detection anchor."""
    return hashlib.sha256(
        _canonical_outcomes(outcome_values).encode("utf-8")
    ).hexdigest()[:64]


def outcomes_match(
    a: Sequence[Mapping[str, Any]],
    b: Sequence[Mapping[str, Any]],
) -> bool:
    """True iff two outcome_values lists are semantically identical."""
    return outcome_hash(a) == outcome_hash(b)


def diff_outcomes(
    approved: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Per-label diff between approved and observed outcomes.

    Returns ``[{label, approved_value, observed_value, changed}]`` — the
    adjudicator's diff view.  Labels present in only one side are marked
    ``added`` or ``removed``.
    """
    def _by_label(vals: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for v in (vals or []):
            if not isinstance(v, Mapping):
                continue
            label = str(v.get("label") or "").strip()
            if label:
                out[label] = {
                    "value": str(v.get("value") or ""),
                    "value_type": str(v.get("value_type") or ""),
                }
        return out

    approved_map = _by_label(approved)
    observed_map = _by_label(observed)
    all_labels = sorted(set(approved_map) | set(observed_map))
    diffs: list[dict[str, Any]] = []
    for label in all_labels:
        a_val = approved_map.get(label)
        o_val = observed_map.get(label)
        if a_val is None:
            diffs.append({"label": label, "approved_value": None,
                          "observed_value": o_val["value"],
                          "change": "added"})
        elif o_val is None:
            diffs.append({"label": label, "approved_value": a_val["value"],
                          "observed_value": None,
                          "change": "removed"})
        elif a_val["value"] != o_val["value"]:
            diffs.append({"label": label,
                          "approved_value": a_val["value"],
                          "observed_value": o_val["value"],
                          "change": "changed"})
        else:
            diffs.append({"label": label,
                          "approved_value": a_val["value"],
                          "observed_value": o_val["value"],
                          "change": "unchanged"})
    return diffs


def build_snapshot(
    traversal: JourneyTraversalRow, *, steps: list[dict] | None = None,
) -> dict:
    """The immutable snapshot locked at approval time — the adjudication
    diff source and the evidence chain anchor."""
    return {
        "traversal_id": traversal.traversal_id,
        "exploration_id": traversal.exploration_id,
        "path_fps": list(traversal.path_fps or []),
        "path_hash": traversal.path_hash,
        "outcome_values": list(traversal.outcome_values or []),
        "outcome_hash": outcome_hash(traversal.outcome_values or []),
        "identity_ref": traversal.identity_ref,
        "env_ref": traversal.env_ref,
        "terminal": traversal.terminal,
        "completed": traversal.completed,
        "steps": steps or [],
        "captured_at": (traversal.created_at.isoformat()
                        if traversal.created_at else None),
    }


# ── Async persistence ───────────────────────────────────────────────────────

async def approve_baseline(
    *,
    tenant_id: str,
    app_id: str,
    journey_id: str,
    traversal_id: str,
    signature: str,
    actor: str = "",
) -> dict[str, Any]:
    """Approve a traversal's outcomes as the journey's expected baseline.

    The traversal must exist and be completed. The e-signature is required
    (422 if blank — enforced by the approval chain). The approval event is
    hash-chained for tamper evidence.

    Transitions: captured→approved, validated→approved (re-baseline),
    drifted→approved (adjudication: intended_change).
    """
    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            raise ValueError("journey not found")

        traversal = (await session.execute(
            select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant_id,
                JourneyTraversalRow.app_id == app_id,
                JourneyTraversalRow.traversal_id == traversal_id,
            ))).scalar_one_or_none()
        if traversal is None:
            raise ValueError("traversal not found")
        if not traversal.completed:
            raise ValueError(
                "only a completed traversal can be approved as a baseline "
                "— a truncated walk has no proven outcome to confirm")

        snapshot = build_snapshot(traversal)
        o_hash = snapshot["outcome_hash"]

        journey.baseline_status = BASELINE_APPROVED
        journey.baseline_traversal_id = traversal_id
        journey.baseline_outcome_hash = o_hash
        journey.baseline_snapshot = snapshot
        journey.baseline_approved_at = now
        journey.baseline_approved_by = str(actor or "")[:200]
        journey.drift_traversal_id = ""
        journey.drift_detected_at = None

    event = await append_approval_event(
        tenant_id=tenant_id,
        subject_kind=SUBJECT_JOURNEY_BASELINE,
        subject_id=journey_id,
        action="approve",
        payload={
            "traversal_id": traversal_id,
            "outcome_hash": o_hash,
            "outcome_values": snapshot["outcome_values"],
        },
        signature=signature,
        actor=actor,
    )

    logger.warning(
        "qec.journey_baseline.approved tenant=%s journey=%s traversal=%s "
        "outcome_hash=%s", tenant_id, journey_id, traversal_id, o_hash)
    return {
        "journey_id": journey_id,
        "baseline_status": BASELINE_APPROVED,
        "traversal_id": traversal_id,
        "outcome_hash": o_hash,
        "approval_event": event,
    }


async def detect_drift(
    *,
    tenant_id: str,
    app_id: str,
    journey_id: str,
    new_traversal_id: str,
    new_outcome_values: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Called by the fold when a new completed traversal lands.

    Compares the new outcomes against the approved baseline:
      - match → validated (or stays validated)
      - differ → drifted (awaiting adjudication)
      - no approved baseline → no-op (stays captured)

    Returns ``{action, baseline_status}`` describing what happened.
    """
    now = utc_now()
    new_hash = outcome_hash(new_outcome_values)

    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            return {"action": "no_journey", "baseline_status": ""}

        if journey.baseline_status == BASELINE_CAPTURED:
            return {"action": "no_baseline_yet",
                    "baseline_status": BASELINE_CAPTURED}

        if journey.baseline_status == BASELINE_DRIFTED:
            return {"action": "already_drifted",
                    "baseline_status": BASELINE_DRIFTED}

        approved_hash = journey.baseline_outcome_hash
        if new_hash == approved_hash:
            journey.baseline_status = BASELINE_VALIDATED
            journey.drift_traversal_id = ""
            journey.drift_detected_at = None
            logger.info(
                "qec.journey_baseline.validated tenant=%s journey=%s",
                tenant_id, journey_id)
            return {"action": "validated",
                    "baseline_status": BASELINE_VALIDATED}
        else:
            journey.baseline_status = BASELINE_DRIFTED
            journey.drift_traversal_id = new_traversal_id
            journey.drift_detected_at = now
            logger.warning(
                "qec.journey_baseline.drifted tenant=%s journey=%s "
                "approved_hash=%s observed_hash=%s",
                tenant_id, journey_id, approved_hash, new_hash)
            return {"action": "drifted",
                    "baseline_status": BASELINE_DRIFTED,
                    "approved_hash": approved_hash,
                    "observed_hash": new_hash}


async def adjudicate_drift(
    *,
    tenant_id: str,
    app_id: str,
    journey_id: str,
    verdict: str,
    signature: str,
    actor: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """A human rules on a drifted journey: defect or intended change.

    ``intended_change``: the drift IS the new expected behavior — the baseline
    moves to the drift traversal's outcomes. A new approval event is chained.

    ``defect``: the drift is a regression — the baseline is REVOKED (journey
    returns to ``captured``). The defect should be raised externally; this
    module only records the adjudication decision.

    Both verdicts require an e-signature for audit.
    """
    if verdict not in ADJUDICATION_VERDICTS:
        raise ValueError(
            f"verdict must be one of {sorted(ADJUDICATION_VERDICTS)} "
            f"(got {verdict!r})")

    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            raise ValueError("journey not found")
        if journey.baseline_status != BASELINE_DRIFTED:
            raise ValueError(
                f"journey baseline_status is {journey.baseline_status!r}, "
                "not 'drifted' — only a drifted journey can be adjudicated")

        drift_tid = journey.drift_traversal_id

        if verdict == ADJUDICATION_INTENDED_CHANGE:
            traversal = (await session.execute(
                select(JourneyTraversalRow).where(
                    JourneyTraversalRow.tenant_id == tenant_id,
                    JourneyTraversalRow.traversal_id == drift_tid,
                ))).scalar_one_or_none()
            if traversal is None:
                raise ValueError(
                    "the drift traversal no longer exists — cannot re-baseline")

            snapshot = build_snapshot(traversal)
            o_hash = snapshot["outcome_hash"]
            journey.baseline_status = BASELINE_APPROVED
            journey.baseline_traversal_id = drift_tid
            journey.baseline_outcome_hash = o_hash
            journey.baseline_snapshot = snapshot
            journey.baseline_approved_at = now
            journey.baseline_approved_by = str(actor or "")[:200]
            journey.drift_traversal_id = ""
            journey.drift_detected_at = None
            new_status = BASELINE_APPROVED

        else:
            journey.baseline_status = BASELINE_CAPTURED
            journey.baseline_traversal_id = ""
            journey.baseline_outcome_hash = ""
            journey.baseline_snapshot = None
            journey.baseline_approved_at = None
            journey.baseline_approved_by = ""
            journey.drift_traversal_id = ""
            journey.drift_detected_at = None
            new_status = BASELINE_CAPTURED

    action = "approve" if verdict == ADJUDICATION_INTENDED_CHANGE else "reject"
    event = await append_approval_event(
        tenant_id=tenant_id,
        subject_kind=SUBJECT_JOURNEY_BASELINE,
        subject_id=journey_id,
        action=action,
        payload={
            "verdict": verdict,
            "drift_traversal_id": drift_tid,
            "reason": str(reason or "")[:500],
        },
        signature=signature,
        actor=actor,
    )

    logger.warning(
        "qec.journey_baseline.adjudicated tenant=%s journey=%s verdict=%s",
        tenant_id, journey_id, verdict)
    return {
        "journey_id": journey_id,
        "verdict": verdict,
        "baseline_status": new_status,
        "approval_event": event,
    }


async def baseline_view(
    *, tenant_id: str, app_id: str, journey_id: str,
) -> dict[str, Any]:
    """The journey's baseline state — for the API response."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            return {}

        result: dict[str, Any] = {
            "baseline_status": journey.baseline_status,
            "baseline_traversal_id": journey.baseline_traversal_id,
            "baseline_outcome_hash": journey.baseline_outcome_hash,
            "baseline_approved_at": (journey.baseline_approved_at.isoformat()
                                     if journey.baseline_approved_at else None),
            "baseline_approved_by": journey.baseline_approved_by,
        }

        if journey.baseline_status == BASELINE_DRIFTED and journey.drift_traversal_id:
            drift_traversal = (await session.execute(
                select(JourneyTraversalRow).where(
                    JourneyTraversalRow.tenant_id == tenant_id,
                    JourneyTraversalRow.traversal_id == journey.drift_traversal_id,
                ))).scalar_one_or_none()
            if drift_traversal is not None:
                approved_outcomes = (journey.baseline_snapshot or {}).get(
                    "outcome_values", [])
                result["drift"] = {
                    "traversal_id": journey.drift_traversal_id,
                    "detected_at": (journey.drift_detected_at.isoformat()
                                    if journey.drift_detected_at else None),
                    "observed_outcomes": list(
                        drift_traversal.outcome_values or []),
                    "diff": diff_outcomes(approved_outcomes,
                                          drift_traversal.outcome_values or []),
                }

    return result


async def auto_validate_from_rules(
    *,
    tenant_id: str,
    app_id: str,
    journey_id: str,
    traversal_id: str,
    outcome_values: Sequence[Mapping[str, Any]],
    rule_results: list,
    rule_summary: dict[str, Any],
) -> dict[str, Any]:
    """O1 — auto-approve a baseline when client rules confirm the outcomes.

    Called by the fold for journeys still in ``captured`` status.  The
    rule_results come from ``rule_oracle.evaluate_rules`` — this function
    only acts when ``all_applicable_pass`` is True (every applicable
    rule confirmed, at least one rule applicable).

    The approval event is recorded with ``action="rule_auto_approve"``
    so it is auditable and distinguishable from human approval.  The
    actor is ``"rule_oracle"`` and the signature is
    ``"rule:auto:<source>"`` — a machine signature, not a human one.
    """
    if not rule_summary.get("all_applicable_pass"):
        return {"action": "rules_not_confirmed",
                "confirmed": rule_summary.get("confirmed", 0),
                "unconfirmed": rule_summary.get("unconfirmed", 0)}

    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            return {"action": "no_journey"}

        if journey.baseline_status != BASELINE_CAPTURED:
            return {"action": "already_has_baseline",
                    "baseline_status": journey.baseline_status}

        traversal = (await session.execute(
            select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant_id,
                JourneyTraversalRow.traversal_id == traversal_id,
            ))).scalar_one_or_none()
        if traversal is None:
            return {"action": "traversal_not_found"}
        if not traversal.completed:
            return {"action": "traversal_not_completed"}

        snapshot = build_snapshot(traversal)
        o_hash = snapshot["outcome_hash"]

        sources = sorted({
            r.rule.source for r in rule_results
            if r.status == "confirmed" and r.rule.source
        })
        source_label = sources[0] if len(sources) == 1 else ",".join(sources[:3])
        signature = f"rule:auto:{source_label}" if source_label else "rule:auto"

        journey.baseline_status = BASELINE_APPROVED
        journey.baseline_traversal_id = traversal_id
        journey.baseline_outcome_hash = o_hash
        journey.baseline_snapshot = snapshot
        journey.baseline_approved_at = now
        journey.baseline_approved_by = "rule_oracle"
        journey.drift_traversal_id = ""
        journey.drift_detected_at = None

    event = await append_approval_event(
        tenant_id=tenant_id,
        subject_kind=SUBJECT_JOURNEY_BASELINE,
        subject_id=journey_id,
        action="rule_auto_approve",
        payload={
            "traversal_id": traversal_id,
            "outcome_hash": o_hash,
            "outcome_values": snapshot["outcome_values"],
            "rule_summary": rule_summary,
        },
        signature=signature,
        actor="rule_oracle",
    )

    logger.warning(
        "qec.journey_baseline.rule_auto_approved tenant=%s journey=%s "
        "traversal=%s confirmed=%d source=%s",
        tenant_id, journey_id, traversal_id,
        rule_summary.get("confirmed", 0), source_label)
    return {
        "action": "rule_auto_approved",
        "journey_id": journey_id,
        "baseline_status": BASELINE_APPROVED,
        "traversal_id": traversal_id,
        "outcome_hash": o_hash,
        "confirmed_rules": rule_summary.get("confirmed", 0),
        "approval_event": event,
    }


async def approval_history(
    *, tenant_id: str, journey_id: str, limit: int = 100,
) -> list[dict]:
    """The hash-chained approval events for this journey's baseline."""
    return await list_approval_events(
        tenant_id=tenant_id,
        subject_kind=SUBJECT_JOURNEY_BASELINE,
        subject_id=journey_id,
        limit=limit,
    )
