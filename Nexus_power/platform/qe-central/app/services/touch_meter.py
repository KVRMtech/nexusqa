"""QE-Central S4 — the human-touch meter (per-band autonomy KPI).

Every human intervention in the governance loop is a TYPED touch event; the
autonomy story of the product ("99% autonomous, 1% human on the things that
matter") is only credible if that 1% is COUNTED, per criticality band, from real
events — never a self-reported single number.

Two sources feed ``qec_touch_events`` (both RLS, tenant-scoped):

  * ``qec_direct``   — a QE-Central governance action recorded inline by the
    scenario_gov router (a scenario approval, an invariant authoring, a gap
    adjudication/waiver, a direct credential/answer-key touch).
  * ``vkpower_audit``— a HUMAN mutation on the UNCHANGED VKPower factory,
    ingested read-only from ``audit_log`` (a case review sign-off, a heal/version
    approval).  Deduped on ``audit_log.log_id`` (the partial unique index
    ``uq_qec_touch_events_source_ref``) so re-ingest is idempotent.

TWO honesty rules make the KPI trustworthy:
  1. A machine mutation is NEVER a human touch — audit rows whose actor is the
     QE-Central service principal (``svc-qe-central``) are SKIPPED, so the
     factory calls S4 itself makes (``/generate``, autonomous heals) can never
     deflate autonomy.  Carry-forward of an UNCHANGED scenario is recorded as an
     APPROVAL EVENT (``qec_approval_events``), not a touch — so every row in
     ``qec_touch_events`` is, by construction, a real human touch.
  2. The KPI REFUSES to emit a single averaged number.  Output is keyed PER
     band, each with its own raw numerator/denominator, so a P0 that needed a
     human can never be averaged away by a sea of autonomous P3s.

ZERO LLM, deterministic, $0.  The pure classifier (:func:`classify_audit_action`)
and the KPI math are unit-tested with no database.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from nexus_sdk.db.models import AuditLogRow

from ..db import (
    new_id,
    tenant_scoped_qec_session,
    tenant_scoped_substrate_session,
    utc_now,
)
from ..db.gov_models import ScenarioRow, TouchEventRow

logger = logging.getLogger(__name__)

MODEL_VERSION = "qec-touch-v1"

# ── Touch-type vocabulary (qec_touch_events.touch_type) ───────────────────
TOUCH_SCENARIO_APPROVE = "scenario_approve"
TOUCH_INVARIANT_AUTHOR = "invariant_author"
TOUCH_GAP_ADJUDICATE = "gap_adjudicate"
TOUCH_WAIVER_CREATE = "waiver_create"
TOUCH_CREDENTIAL_PROVISION = "credential_provision"
TOUCH_ANSWER_KEY_EDIT = "answer_key_edit"
TOUCH_HEAL_APPROVE = "heal_approve"
TOUCH_CASE_REVIEW = "case_review"
TOUCH_DEFECT_CONFIRM = "defect_confirm"
#: P5 fallback ladder — a human recorded a hard widget once (record-once rung),
#: or a human had to resolve a decision the ladder could neither automate nor
#: record. Counted so per-app coverage reports how much needed a person.
TOUCH_WIDGET_RECORD = "widget_record"
TOUCH_WIDGET_RESOLVE = "widget_resolve"
TOUCH_TYPES = frozenset({
    TOUCH_SCENARIO_APPROVE, TOUCH_INVARIANT_AUTHOR, TOUCH_GAP_ADJUDICATE,
    TOUCH_WAIVER_CREATE, TOUCH_CREDENTIAL_PROVISION, TOUCH_ANSWER_KEY_EDIT,
    TOUCH_HEAL_APPROVE, TOUCH_CASE_REVIEW, TOUCH_DEFECT_CONFIRM,
    TOUCH_WIDGET_RECORD, TOUCH_WIDGET_RESOLVE,
})

# ── Sources ────────────────────────────────────────────────────────────────
SOURCE_QEC_DIRECT = "qec_direct"
SOURCE_VKPOWER_AUDIT = "vkpower_audit"

# ── Bands (mirror; criticality.py owns the registry) ──────────────────────
BAND_P0 = "P0"
BAND_P1 = "P1"
BAND_P2 = "P2"
BAND_P3 = "P3"
BANDS: tuple[str, ...] = (BAND_P0, BAND_P1, BAND_P2, BAND_P3)
#: touches whose band could not be attributed (ingested audit rows have no
#: criticality band) land here — counted, never silently dropped or misfiled.
BAND_UNATTRIBUTED = "unattributed"

#: Actors that are the QE-Central service principal, NOT a human (service_token
#: mints ``sub='svc-qe-central'``).  Their audited mutations are autonomous.
SERVICE_ACTORS = frozenset({"svc-qe-central", "qe-central@service"})


class InvalidTouchTypeError(ValueError):
    """Raised for a touch_type outside :data:`TOUCH_TYPES`."""

    http_status = 422


# ── The deterministic audit → touch classifier (marker table, $0) ─────────

def classify_audit_action(action: str, user_id: str) -> str | None:
    """Map ONE ``audit_log`` row to a human ``touch_type`` — or ``None`` to skip.

    Deterministic marker table over the VKPower mutation surface
    (test_factory.py mutating endpoints).  Returns ``None`` (SKIP) when the row
    is not a metered human touch, in two cases that protect the autonomy KPI:

      * the actor is the QE-Central SERVICE principal (a machine mutation — e.g.
        ``/generate`` or an autonomous heal — never a human touch);
      * the action is not one of the recognised human sign-off endpoints (a
        plain read-through, a run trigger like ``/auto-heal/run-config``, etc.).

    Recognised human touches on the VKPower side:
      * ``…/test-cases/{id}/review``           → ``case_review``
      * ``…/scripts/{id}/versions/{n}/approve`` → ``heal_approve``
      * ``…/steps/{sid}/{n}/heal`` (manual)    → ``heal_approve``
    """
    if str(user_id or "").strip() in SERVICE_ACTORS:
        return None
    act = str(action or "").lower()
    # Strip a leading HTTP method so we classify on the path only.
    path = act.split(" ", 1)[1] if " " in act else act
    path = path.rstrip("/")

    if path.endswith("/review") and "/test-cases/" in path:
        return TOUCH_CASE_REVIEW
    if "/versions/" in path and path.endswith("/approve"):
        return TOUCH_HEAL_APPROVE
    # A manual single-step heal APPLY (NOT the autonomous run-config trigger,
    # which ends in '/run-config' and is a run, not an approval).
    if path.endswith("/heal"):
        return TOUCH_HEAL_APPROVE
    return None


def _validate_touch_type(touch_type: str) -> str:
    tt = str(touch_type or "").strip()
    if tt not in TOUCH_TYPES:
        raise InvalidTouchTypeError(
            f"touch_type must be one of {sorted(TOUCH_TYPES)} (got {touch_type!r})"
        )
    return tt


def _norm_band(band: str) -> str:
    b = str(band or "").strip().upper()
    return b if b in BANDS else ""


# ── Direct touch recording (qecentral DB) ─────────────────────────────────

async def record_touch(
    *,
    tenant_id: str,
    touch_type: str,
    band: str = "",
    app_id: str = "",
    cycle_id: str = "",
    actor: str = "",
    source: str = SOURCE_QEC_DIRECT,
    source_ref: str | None = None,
) -> dict:
    """Record ONE typed human touch and return its record.

    Validates ``touch_type`` BEFORE any write (a bad type is a 422, not a silent
    drop).  For an ingested ``vkpower_audit`` touch the caller passes
    ``source_ref=log_id``; the partial unique index dedupes a re-ingest — a
    duplicate is swallowed and reported as ``deduped=True`` (never a 500).
    """
    tt = _validate_touch_type(touch_type)
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required to record a touch")
    touch = TouchEventRow(
        touch_id=new_id(),
        tenant_id=tenant_id,
        app_id=(app_id or "")[:64],
        touch_type=tt,
        band=_norm_band(band),
        cycle_id=(cycle_id or "")[:64],
        source=source,
        source_ref=source_ref,
        actor=(actor or "")[:200],
        created_at=utc_now(),
    )
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            session.add(touch)
            await session.flush()
    except IntegrityError:
        # Dedupe collision on (tenant_id, source, source_ref) — already counted.
        logger.info(
            "qec.touch.deduped",
            extra={"tenant_id": tenant_id, "touch_type": tt, "source_ref": source_ref},
        )
        return {"touch_id": "", "touch_type": tt, "deduped": True}
    logger.info(
        "qec.touch.recorded",
        extra={"tenant_id": tenant_id, "touch_type": tt, "band": touch.band,
               "app_id": touch.app_id, "source": source},
    )
    return {
        "touch_id": touch.touch_id,
        "touch_type": tt,
        "band": touch.band,
        "app_id": touch.app_id,
        "cycle_id": touch.cycle_id,
        "source": source,
        "source_ref": source_ref,
        "deduped": False,
        "created_at": touch.created_at.isoformat(),
    }


# ── audit_log ingest (nexus DB, read-only; qecentral DB, write) ───────────

async def _existing_source_refs(
    tenant_id: str, refs: Sequence[str],
) -> set[str]:
    """The subset of ``refs`` already ingested (dedupe pre-check)."""
    if not refs:
        return set()
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(TouchEventRow.source_ref).where(
                TouchEventRow.tenant_id == tenant_id,
                TouchEventRow.source == SOURCE_VKPOWER_AUDIT,
                TouchEventRow.source_ref.in_(list(refs)),
            )
        )).scalars().all()
    return {r for r in rows if r}


async def ingest_audit_log(
    *,
    tenant_id: str,
    app_id: str = "",
    since: datetime | None = None,
    limit: int = 500,
) -> dict:
    """Ingest recent HUMAN mutations from ``audit_log`` as ``vkpower_audit`` touches.

    Reads ``audit_log`` on the nexus DB (role ``qec_substrate`` has SELECT on it,
    R-7) newest-first, classifies each row via :func:`classify_audit_action`
    (service-actor + non-touch rows skipped), and records the survivors deduped
    on ``log_id``.  Ingested touches carry ``band=""`` (an audit row has no
    criticality band) — they surface in the KPI's ``unattributed`` bucket, never
    misattributed to a real band.

    Returns ``{scanned, classified, ingested, duplicates, skipped}``.
    """
    limit = max(1, min(5000, int(limit)))
    async with tenant_scoped_substrate_session(tenant_id) as session:
        stmt = select(AuditLogRow).where(AuditLogRow.tenant_id == tenant_id)
        if since is not None:
            stmt = stmt.where(AuditLogRow.created_at >= since)
        stmt = stmt.order_by(AuditLogRow.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

    scanned = len(rows)
    candidates: list[tuple[str, str, str]] = []  # (log_id, touch_type, actor)
    for r in rows:
        actor = str(r.user_id or "")
        touch_type = classify_audit_action(r.action, actor)
        if touch_type is None:
            continue
        email = str((r.details or {}).get("email") or "")
        candidates.append((r.log_id, touch_type, actor or email))

    already = await _existing_source_refs(tenant_id, [c[0] for c in candidates])
    ingested = duplicates = 0
    for log_id, touch_type, actor in candidates:
        if log_id in already:
            duplicates += 1
            continue
        result = await record_touch(
            tenant_id=tenant_id, touch_type=touch_type, band="", app_id=app_id,
            actor=actor, source=SOURCE_VKPOWER_AUDIT, source_ref=log_id,
        )
        if result.get("deduped"):
            duplicates += 1
        else:
            ingested += 1

    logger.info(
        "qec.touch.ingested",
        extra={"tenant_id": tenant_id, "scanned": scanned,
               "classified": len(candidates), "ingested": ingested,
               "duplicates": duplicates},
    )
    return {
        "scanned": scanned,
        "classified": len(candidates),
        "ingested": ingested,
        "duplicates": duplicates,
        "skipped": scanned - len(candidates),
    }


# ── The per-band autonomy KPI (NEVER a single averaged number) ────────────

async def _scenarios_by_band(tenant_id: str, app_id: str) -> dict[str, int]:
    """Count of ACTIVE governed scenarios per band — the autonomy denominator."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(ScenarioRow.criticality_band).where(
                ScenarioRow.tenant_id == tenant_id,
                ScenarioRow.app_id == app_id,
                ScenarioRow.status == "active",
            )
        )).scalars().all()
    out: dict[str, int] = {}
    for band in rows:
        b = _norm_band(band) or BAND_UNATTRIBUTED
        out[b] = out.get(b, 0) + 1
    return out


async def _touches_by_band(
    tenant_id: str, app_id: str, cycle_id: str | None,
) -> dict[str, dict[str, int]]:
    """``{band: {touch_type: count}}`` of human touches (optionally one cycle)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        stmt = select(TouchEventRow.band, TouchEventRow.touch_type).where(
            TouchEventRow.tenant_id == tenant_id,
            TouchEventRow.app_id == app_id,
        )
        if cycle_id:
            stmt = stmt.where(TouchEventRow.cycle_id == cycle_id)
        rows = (await session.execute(stmt)).all()
    out: dict[str, dict[str, int]] = {}
    for band, touch_type in rows:
        b = _norm_band(band) or BAND_UNATTRIBUTED
        bucket = out.setdefault(b, {})
        bucket[touch_type] = bucket.get(touch_type, 0) + 1
    return out


def _autonomy_pct(governed: int, human_touches: int) -> float | None:
    """Autonomy % for one band, clamped to [0, 100]; ``None`` when there is no
    governed denominator (an honest "not computable", never a misleading 0).

    Autonomy = the fraction of governed units that proceeded WITHOUT a human
    touch.  ``human_touches`` is clamped to ``governed`` so a band can never read
    as negatively autonomous; the RAW counts travel alongside so the % is fully
    auditable and never stands alone.
    """
    if governed <= 0:
        return None
    touched = min(human_touches, governed)
    return round(100.0 * (governed - touched) / governed, 1)


async def autonomy_by_band(
    *, tenant_id: str, app_id: str, cycle_id: str | None = None,
) -> dict:
    """The autonomy KPI — PER criticality band, with raw numerator/denominator.

    DELIBERATELY refuses to emit a single averaged autonomy number: the return
    is keyed ``by_band``, each entry carrying ``governed`` (active scenarios in
    the band), ``human_touches`` (metered touches in the band), ``autonomy_pct``
    (``None`` when there is no denominator), and a ``by_type`` breakdown.  A
    separate ``unattributed`` bucket holds ingested audit touches with no band —
    counted, never averaged into a real band.

    Returns ``{model_version, app_id, cycle_id, by_band, unattributed,
    totals, note}``.
    """
    scen = await _scenarios_by_band(tenant_id, app_id)
    touches = await _touches_by_band(tenant_id, app_id, cycle_id)

    by_band: dict[str, dict] = {}
    for band in BANDS:
        governed = scen.get(band, 0)
        by_type = touches.get(band, {})
        human = sum(by_type.values())
        if governed == 0 and human == 0:
            continue  # nothing governed and nothing touched in this band — omit
        by_band[band] = {
            "governed_scenarios": governed,
            "human_touches": human,
            "autonomy_pct": _autonomy_pct(governed, human),
            "by_type": dict(sorted(by_type.items())),
        }

    unattributed = touches.get(BAND_UNATTRIBUTED, {})
    total_touches = sum(sum(v.values()) for v in touches.values())
    return {
        "model_version": MODEL_VERSION,
        "app_id": app_id,
        "cycle_id": cycle_id or "",
        "by_band": by_band,
        "unattributed": {
            "human_touches": sum(unattributed.values()),
            "by_type": dict(sorted(unattributed.items())),
            "note": "ingested audit touches with no criticality band — counted, "
                    "never folded into a real band",
        },
        "totals": {
            "governed_scenarios": sum(scen.values()),
            "human_touches": total_touches,
        },
        # The contract of this KPI: no single averaged autonomy number exists.
        "note": "autonomy is reported PER band only — there is deliberately no "
                "aggregate averaged autonomy figure (a P0 touch must never be "
                "averaged away by autonomous P3s)",
    }


# ── The per-band autonomy TREND across cycles (NEVER a single number) ─────

#: Hard ceiling on the trend window (a report, not an unbounded scan).
MAX_TREND_CYCLES = 200


async def _recent_cycles(
    tenant_id: str, app_id: str, cycles: int,
) -> list[dict]:
    """The last ``cycles`` ``app_cycles`` for an app, OLDEST→NEWEST (chronological).

    The authoritative cycle window comes from ``app_cycles`` — NOT from the touch
    rows — so a fully-autonomous cycle (zero touches, the 99% path) is still
    present in the trend instead of being silently dropped.
    """
    from ..db.controlplane_models import AppCycleRow  # S5 table, read-only

    n = max(1, min(MAX_TREND_CYCLES, int(cycles)))
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(
                AppCycleRow.cycle_id, AppCycleRow.state,
                AppCycleRow.trigger, AppCycleRow.created_at,
            ).where(
                AppCycleRow.tenant_id == tenant_id,
                AppCycleRow.app_id == app_id,
            ).order_by(AppCycleRow.created_at.desc()).limit(n)
        )).all()
    out = [
        {
            "cycle_id": cid,
            "state": state,
            "trigger": trigger,
            "created_at": created_at.isoformat() if created_at else None,
        }
        for (cid, state, trigger, created_at) in rows
    ]
    out.reverse()  # chronological (oldest first) for a readable trend
    return out


async def _touches_by_cycle_band(
    tenant_id: str, app_id: str, cycle_ids: Sequence[str],
) -> dict[str, dict[str, int]]:
    """``{cycle_id: {band_or_unattributed: count}}`` for the given cycles.

    Ingested audit touches (no band) land under :data:`BAND_UNATTRIBUTED`, never
    folded into a real band.
    """
    if not cycle_ids:
        return {}
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(TouchEventRow.cycle_id, TouchEventRow.band).where(
                TouchEventRow.tenant_id == tenant_id,
                TouchEventRow.app_id == app_id,
                TouchEventRow.cycle_id.in_(list(cycle_ids)),
            )
        )).all()
    out: dict[str, dict[str, int]] = {}
    for cycle_id, band in rows:
        b = _norm_band(band) or BAND_UNATTRIBUTED
        bucket = out.setdefault(cycle_id, {})
        bucket[b] = bucket.get(b, 0) + 1
    return out


def _assemble_trend(
    *,
    app_id: str,
    cycles_meta: Sequence[dict],
    governed_by_band: Mapping[str, int],
    touches_by_cycle_band: Mapping[str, Mapping[str, int]],
) -> dict:
    """Assemble the per-band autonomy series across an ORDERED cycle window (pure).

    DELIBERATELY refuses to emit a single averaged number: the result is keyed
    ``per_band``, each a LIST of one entry per cycle (oldest→newest) carrying its
    own governed denominator, human-touch numerator, and autonomy %.

    Honesty rules:
      * ``autonomy_pct`` is ``None`` (null) for any cycle where the band has NO
        governed denominator — a band with no cases is null, NEVER 100%.
      * a band with ``governed > 0`` and ZERO touches in a cycle reads a genuine
        100% (fully autonomous that cycle) — distinct from the null above.
      * ``unattributed`` (ingested audit touches with no band) is reported in its
        OWN per-cycle series — never folded into a real band's numerator.
      * a band that is neither governed nor touched anywhere in the window is
        omitted (noise), never emitted as an all-null row.
    """
    cycle_ids = [c["cycle_id"] for c in cycles_meta]
    touched_bands: set[str] = set()
    for cid in cycle_ids:
        touched_bands.update(touches_by_cycle_band.get(cid, {}).keys())

    per_band: dict[str, list[dict]] = {}
    for band in BANDS:
        governed = int(governed_by_band.get(band, 0) or 0)
        if governed <= 0 and band not in touched_bands:
            continue  # nothing governed and nothing touched in this band — omit
        series: list[dict] = []
        for cid in cycle_ids:
            human = int(touches_by_cycle_band.get(cid, {}).get(band, 0) or 0)
            series.append({
                "cycle_id": cid,
                "governed_scenarios": governed,
                "human_touches": human,
                "autonomy_pct": _autonomy_pct(governed, human),  # None when governed==0
            })
        per_band[band] = series

    unattributed: list[dict] = []
    if any(BAND_UNATTRIBUTED in touches_by_cycle_band.get(c, {}) for c in cycle_ids):
        unattributed = [
            {
                "cycle_id": cid,
                "human_touches": int(
                    touches_by_cycle_band.get(cid, {}).get(BAND_UNATTRIBUTED, 0) or 0
                ),
            }
            for cid in cycle_ids
        ]

    return {
        "model_version": MODEL_VERSION,
        "app_id": app_id,
        "window": len(cycle_ids),
        "cycles": list(cycles_meta),
        "per_band": per_band,
        "unattributed": unattributed,
        # The contract of this KPI: no single averaged autonomy number exists.
        "note": (
            "autonomy is reported PER band PER cycle — there is deliberately no "
            "single averaged autonomy figure; a band with no governed scenarios "
            "in a cycle is null, NEVER 100% (a P0 that needed a human must never "
            "be averaged away by autonomous P3s or by an empty band)"
        ),
    }


async def autonomy_trend(
    *, tenant_id: str, app_id: str, cycles: int = 10,
) -> dict:
    """The autonomy TREND — per criticality band across the last ``cycles`` cycles.

    Reads the authoritative cycle window from ``app_cycles`` (so zero-touch,
    fully-autonomous cycles are present, not dropped), the CURRENT governed-
    scenario denominator per band (:func:`_scenarios_by_band`), and the touches
    recorded against each cycle_id, then assembles a per-band per-cycle series via
    :func:`_assemble_trend`.  NEVER emits a single averaged number.

    Returns ``{model_version, app_id, window, cycles, per_band, unattributed,
    note}``.
    """
    cycles_meta = await _recent_cycles(tenant_id, app_id, cycles)
    governed = await _scenarios_by_band(tenant_id, app_id)
    cycle_ids = [c["cycle_id"] for c in cycles_meta]
    touches = await _touches_by_cycle_band(tenant_id, app_id, cycle_ids)
    return _assemble_trend(
        app_id=app_id, cycles_meta=cycles_meta,
        governed_by_band=governed, touches_by_cycle_band=touches,
    )


async def list_touches(
    *, tenant_id: str, app_id: str = "", limit: int = 500,
) -> list[dict]:
    """Recent touches for ``(tenant[, app])`` newest-first (audit view)."""
    limit = max(1, min(5000, int(limit)))
    async with tenant_scoped_qec_session(tenant_id) as session:
        stmt = select(TouchEventRow).where(TouchEventRow.tenant_id == tenant_id)
        if app_id:
            stmt = stmt.where(TouchEventRow.app_id == app_id)
        rows = (await session.execute(
            stmt.order_by(TouchEventRow.created_at.desc()).limit(limit)
        )).scalars().all()
    return [{
        "touch_id": r.touch_id,
        "touch_type": r.touch_type,
        "band": r.band,
        "app_id": r.app_id,
        "cycle_id": r.cycle_id,
        "source": r.source,
        "source_ref": r.source_ref,
        "actor": r.actor,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


__all__ = [
    "MODEL_VERSION",
    "TOUCH_SCENARIO_APPROVE", "TOUCH_INVARIANT_AUTHOR", "TOUCH_GAP_ADJUDICATE",
    "TOUCH_WAIVER_CREATE", "TOUCH_CREDENTIAL_PROVISION", "TOUCH_ANSWER_KEY_EDIT",
    "TOUCH_HEAL_APPROVE", "TOUCH_CASE_REVIEW", "TOUCH_DEFECT_CONFIRM",
    "TOUCH_TYPES",
    "SOURCE_QEC_DIRECT", "SOURCE_VKPOWER_AUDIT",
    "BANDS", "BAND_UNATTRIBUTED", "SERVICE_ACTORS",
    "MAX_TREND_CYCLES",
    "InvalidTouchTypeError",
    "classify_audit_action",
    "record_touch",
    "ingest_audit_log",
    "autonomy_by_band",
    "autonomy_trend",
    "list_touches",
]
