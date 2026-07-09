"""QE-Central S4 — the scenario-governance API (``/api/v1/qec/…``, "the 1%").

Thin, honest HTTP surface over the deterministic S4 services (criticality /
synthesis / coverage / approval / tier_label / touch_meter).  Every endpoint is
tenant-scoped (RLS via the qecentral/substrate session helpers) and every
mutation rides the admin|manager RBAC gate cloned from the VKPower factory
(test_factory.py:118-124).

Endpoint groups (design §3.4 API surface):
  * criticality registry            GET/PUT  /registry/criticality
  * universe + coverage             POST /apps/{id}/universe/{compute,approve} ·
                                    GET  /apps/{id}/coverage · GET/POST gaps
  * scenario lifecycle              POST /apps/{id}/scenarios/synthesize ·
                                    GET  /apps/{id}/scenarios · GET /scenarios/{id} ·
                                    POST /scenarios/{id}/review (422-no-signature) ·
                                    POST /scenarios/{id}/materialize (409-unless-approved)
  * certified invariants            POST/GET /apps/{id}/invariants
  * tier labels                     POST/GET /artifacts/{id}/tier-label
  * human-touch meter               GET /apps/{id}/autonomy · POST /touches ·
                                    POST /touches/ingest · GET /touches

ZERO LLM anywhere; deterministic + $0.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..clients import factory
from ..clients.factory import FactoryClientError
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.gov_models import CertifiedInvariantRow, CriticalityRegistryRow, ScenarioRow
from ..services import approval, coverage, criticality, synthesis, tier_label, touch_meter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Scenario Governance"])

_MUTATE = require_role("admin", "manager")

# Scenario-review actions a human may submit (carry_forward is internal-only —
# it is the machine auto-carry recorded by /synthesize, never a review action).
_SCENARIO_REVIEW_ACTIONS = frozenset({"submit", "approve", "reject", "reopen"})
_APPROVED_STATE = "approved"


def _actor(user: dict) -> str:
    """The audit actor string for a user context (sub | user_id | email)."""
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "")


def _http_status_of(exc: Exception, default: int = 422) -> int:
    """Map a typed service error to its declared HTTP status (else ``default``)."""
    return int(getattr(exc, "http_status", default) or default)


# ══════════════════════ criticality registry ══════════════════════════════

class RegistryPut(BaseModel):
    """Replace the ACTIVE criticality registry with a NEW immutable version.

    Provide ``signals`` (a full pack) to install a custom marker table, OR
    ``domain`` to install the built-in seed pack (optionally with a domain
    boost).  Exactly the DB ``pack`` shape is stored; the prior version stays
    immutable history and is merely deactivated.
    """

    signals: list[dict] | None = None
    domain: str | None = Field(default=None, max_length=64)


@router.get("/registry/criticality")
async def get_registry(user: dict = Depends(require_auth)) -> dict:
    """Return the tenant's ACTIVE criticality pack (seed fallback when unset)."""
    tenant_id = user["tenant_id"]
    signals, registry_version = await criticality.load_active_pack(tenant_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        has_row = (await session.execute(
            select(CriticalityRegistryRow.registry_version).where(
                CriticalityRegistryRow.tenant_id == tenant_id,
                CriticalityRegistryRow.active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
    return {
        "registry_version": registry_version,
        "signals": signals,
        "signal_count": len(signals),
        "is_seed_fallback": has_row is None,
        "classifier": criticality.CLASSIFIER,
    }


@router.put("/registry/criticality")
async def put_registry(
    payload: RegistryPut, user: dict = Depends(_MUTATE),
) -> dict:
    """Install a NEW immutable, ACTIVE criticality registry version.

    A PUT never mutates a prior version (verdict_events immutability): it mints a
    globally-unique ``registry_version``, deactivates every currently-active row,
    and inserts the new active row atomically.
    """
    tenant_id = user["tenant_id"]
    if payload.signals is not None:
        pack = {
            "registry_version": criticality.SEED_REGISTRY_VERSION,
            "signals": [dict(s) for s in payload.signals],
        }
    else:
        pack = criticality.seed_pack(payload.domain)
    version = criticality.new_registry_version(pack.get("registry_version") or "crit-v1")

    async with tenant_scoped_qec_session(tenant_id) as session:
        actives = (await session.execute(
            select(CriticalityRegistryRow).where(
                CriticalityRegistryRow.tenant_id == tenant_id,
                CriticalityRegistryRow.active.is_(True),
            )
        )).scalars().all()
        for row in actives:
            row.active = False
        session.add(CriticalityRegistryRow(
            registry_version=version, tenant_id=tenant_id, pack=pack,
            active=True, created_by=_actor(user)[:200],
        ))
        await session.flush()
    logger.info(
        "qec.registry.installed",
        extra={"tenant_id": tenant_id, "registry_version": version,
               "signal_count": len(pack.get("signals") or []), "actor": _actor(user)},
    )
    return {"registry_version": version, "active": True,
            "signal_count": len(pack.get("signals") or [])}


# ══════════════════════ universe + coverage ═══════════════════════════════

class AtomIn(BaseModel):
    canonical_key: str = Field(min_length=1, max_length=1000)
    kind: str = Field(min_length=1, max_length=32)
    source: str = Field(default=coverage.SOURCE_CRAWL, max_length=32)
    provenance: str = Field(default=coverage.PROV_INFERRED, max_length=32)
    evidence: dict = Field(default_factory=dict)


class UniverseCompute(BaseModel):
    """The fresh enumerated universe for one compute pass.

    ``atoms`` is the freshly-enumerated atom set (from crawl/repo/answer-key);
    when provided it is upserted and the shrinkage guard diffs it against the
    signed baseline.  When omitted, the guard runs against the STORED universe
    (``shrinkage_basis='stored'`` — no fresh deletion can be detected)."""

    atoms: list[AtomIn] | None = None


class UniverseApprove(BaseModel):
    signature: str = Field(min_length=1, max_length=200)
    signed_by: str = Field(default="", max_length=200)


@router.post("/apps/{app_id}/universe/compute")
async def compute_universe(
    app_id: str, payload: UniverseCompute, user: dict = Depends(_MUTATE),
) -> dict:
    """Upsert the fresh atom set, compute the universe, run the shrinkage guard.

    A previously-approved atom MISSING from the fresh universe raises exactly one
    P0 ``possible_deletion`` gap and flips the coverage verdict to
    ``blocked_on_p0_gaps`` (never a silent pass on a shrinking universe).
    """
    tenant_id = user["tenant_id"]
    upsert_stats = None
    if payload.atoms is not None:
        atoms = [
            coverage.AtomInput(
                canonical_key=a.canonical_key, kind=a.kind, source=a.source,
                provenance=a.provenance, evidence=a.evidence,
            )
            for a in payload.atoms
        ]
        try:
            upsert_stats = await coverage.upsert_atoms(
                tenant_id=tenant_id, app_id=app_id, atoms=atoms,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        fresh_keys = [a.canonical_key for a in payload.atoms]
        shrinkage_basis = "fresh"
    else:
        universe = await coverage.compute_universe(tenant_id=tenant_id, app_id=app_id)
        fresh_keys = universe["canonical_keys"]
        shrinkage_basis = "stored"

    guard = await coverage.shrinkage_guard(
        tenant_id=tenant_id, app_id=app_id, fresh_keys=fresh_keys,
    )
    universe = await coverage.compute_universe(tenant_id=tenant_id, app_id=app_id)
    logger.info(
        "qec.universe.computed",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "atom_count": universe["atom_count"], "verdict": guard["verdict"],
               "shrinkage_basis": shrinkage_basis},
    )
    return {
        "app_id": app_id,
        "universe": universe,
        "shrinkage": {**guard, "shrinkage_basis": shrinkage_basis},
        "atoms_upsert": upsert_stats,
    }


@router.post("/apps/{app_id}/universe/approve")
async def approve_universe(
    app_id: str, payload: UniverseApprove, user: dict = Depends(_MUTATE),
) -> dict:
    """E-sign the current universe as the approved baseline (hash-chained).

    The signed ``atoms_hash`` becomes the anchor the shrinkage guard diffs future
    universes against.  A blank signature is a 422 (an unsigned baseline has no
    honest meaning)."""
    tenant_id = user["tenant_id"]
    universe = await coverage.compute_universe(tenant_id=tenant_id, app_id=app_id)
    try:
        baseline = await approval.append_universe_baseline(
            tenant_id=tenant_id, app_id=app_id,
            atoms_hash=universe["atoms_hash"], atom_count=universe["atom_count"],
            signature=payload.signature, signed_by=payload.signed_by or _actor(user),
        )
    except approval.SignatureRequiredError as exc:
        raise HTTPException(status_code=_http_status_of(exc), detail=str(exc))
    logger.info(
        "qec.universe.approved",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "atom_count": universe["atom_count"], "actor": _actor(user)},
    )
    return {"app_id": app_id, "baseline": baseline, "universe": universe}


@router.get("/apps/{app_id}/coverage")
async def get_coverage(app_id: str, user: dict = Depends(require_auth)) -> dict:
    """The coverage scorecard: atoms + invariants + NAMED gaps + verdict.

    ``verdict`` is ``blocked_on_p0_gaps`` while any P0 blocking gap is open (or
    its waiver has expired); every blocking gap is named so the block is never
    opaque."""
    tenant_id = user["tenant_id"]
    return await coverage.compute_coverage(tenant_id=tenant_id, app_id=app_id)


@router.get("/apps/{app_id}/gaps")
async def list_gaps(
    app_id: str,
    status: str | None = Query(default=None, max_length=32),
    user: dict = Depends(require_auth),
) -> dict:
    """List the coverage gaps for an app (optionally filtered by status)."""
    tenant_id = user["tenant_id"]
    gaps = await coverage.list_gaps(tenant_id=tenant_id, app_id=app_id, status=status)
    return {"app_id": app_id, "gaps": gaps, "total": len(gaps)}


class GapWaive(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    requested_expires_at: datetime | None = None


class GapAdjudicate(BaseModel):
    note: str = Field(default="", max_length=500)


@router.post("/apps/{app_id}/gaps/{gap_id}/waive")
async def waive_gap(
    app_id: str, gap_id: str, payload: GapWaive, user: dict = Depends(_MUTATE),
) -> dict:
    """Annotate a gap with a time-bounded waiver (≤90d) — NEVER deletes it.

    Records one ``waiver_create`` human touch (band P0 — waivers only ever cover
    P0 blocking gaps)."""
    tenant_id = user["tenant_id"]
    try:
        gap = await coverage.waive_gap(
            tenant_id=tenant_id, app_id=app_id, gap_id=gap_id,
            reason=payload.reason, actor=_actor(user),
            requested_expires_at=payload.requested_expires_at,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="coverage gap not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await touch_meter.record_touch(
        tenant_id=tenant_id, touch_type=touch_meter.TOUCH_WAIVER_CREATE,
        band=coverage.BAND_P0, app_id=app_id, actor=_actor(user),
    )
    return gap


@router.post("/apps/{app_id}/gaps/{gap_id}/adjudicate")
async def adjudicate_gap(
    app_id: str, gap_id: str, payload: GapAdjudicate, user: dict = Depends(_MUTATE),
) -> dict:
    """Mark a gap adjudicated (a human decided it is acceptable / handled).

    Records one ``gap_adjudicate`` human touch (band P0)."""
    tenant_id = user["tenant_id"]
    try:
        gap = await coverage.adjudicate_gap(
            tenant_id=tenant_id, app_id=app_id, gap_id=gap_id,
            actor=_actor(user), note=payload.note,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="coverage gap not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await touch_meter.record_touch(
        tenant_id=tenant_id, touch_type=touch_meter.TOUCH_GAP_ADJUDICATE,
        band=coverage.BAND_P0, app_id=app_id, actor=_actor(user),
    )
    return gap


# ══════════════════════ scenario lifecycle ════════════════════════════════

class SynthesizeRequest(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=64)


class ReviewRequest(BaseModel):
    action: str = ""           # submit | approve | reject | reopen
    signature: str = ""        # typed full name — required to approve
    note: str = Field(default="", max_length=1000)


def _scenario_view(row: ScenarioRow) -> dict:
    """Compact scenario view for the queue (fingerprint + diff + review state)."""
    review = dict(row.review or {})
    return {
        "scenario_id": row.scenario_id,
        "app_id": row.app_id,
        "name": row.name,
        "source_artifact_id": row.source_artifact_id,
        "criticality_band": row.criticality_band,
        "criticality_evidence": row.criticality_evidence,
        "registry_version": row.registry_version,
        "fingerprint": row.fingerprint,
        "diff_state": row.diff_state,
        "review_state": str(review.get("state") or "draft"),
        "tier": row.tier,
        "materialized_artifact_id": row.materialized_artifact_id,
        "status": row.status,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _record_carry_forwards(tenant_id: str, app_id: str, result) -> int:
    """Record a ZERO-TOUCH carry_forward approval event for every UNCHANGED,
    already-approved scenario (design: UNCHANGED auto-carries approval).

    These land in ``qec_approval_events`` (audit) but are NOT human touches
    (``is_human_touch`` returns False for carry_forward) — the whole point of the
    zero-touch carry path."""
    unchanged = [
        s.scenario_id for s in result.scenarios
        if s.diff_state == synthesis.DIFF_UNCHANGED
    ]
    if not unchanged:
        return 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(ScenarioRow).where(
                ScenarioRow.tenant_id == tenant_id,
                ScenarioRow.app_id == app_id,
                ScenarioRow.scenario_id.in_(unchanged),
            )
        )).scalars().all()
    carried = 0
    for row in rows:
        if str((row.review or {}).get("state") or "") != _APPROVED_STATE:
            continue
        await approval.append_event(
            tenant_id=tenant_id, subject_kind=approval.SUBJECT_SCENARIO,
            subject_id=row.scenario_id, action=approval.ACTION_CARRY_FORWARD,
            payload={"fingerprint": row.fingerprint, "diff_state": row.diff_state},
            actor="svc-qe-central", carry_forward=True,
        )
        carried += 1
    return carried


@router.post("/apps/{app_id}/scenarios/synthesize")
async def synthesize_scenarios(
    app_id: str, payload: SynthesizeRequest, user: dict = Depends(_MUTATE),
) -> dict:
    """Synthesise deterministic journey scenarios from a crawl artifact.

    Diffs against the stored scenario set: NEW/CHANGED enter the approval queue;
    UNCHANGED auto-carry their prior approval (recorded, ZERO human touch);
    MISSING drive the shrinkage path (flagged, never deleted)."""
    tenant_id = user["tenant_id"]
    result = await synthesis.synthesize(tenant_id, app_id, payload.artifact_id)
    carried = await _record_carry_forwards(tenant_id, app_id, result)
    body = result.as_dict()
    body["carry_forwarded"] = carried
    logger.info(
        "qec.scenarios.synthesized",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "artifact_id": payload.artifact_id, "counts": result.counts,
               "carry_forwarded": carried, "actor": _actor(user)},
    )
    return body


@router.get("/apps/{app_id}/scenarios")
async def list_scenarios(
    app_id: str,
    state: str = Query(default="all", max_length=32),
    band: str | None = Query(default=None, max_length=8),
    user: dict = Depends(require_auth),
) -> dict:
    """The approval queue / scenario list with fingerprint deltas.

    ``state`` ∈ ``all`` | ``needs_approval`` | ``new`` | ``changed`` |
    ``unchanged`` | ``missing``.  ``needs_approval`` = NEW/CHANGED not yet
    approved — the human's actual work queue."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        stmt = select(ScenarioRow).where(
            ScenarioRow.tenant_id == tenant_id,
            ScenarioRow.app_id == app_id,
            ScenarioRow.status == "active",
        )
        if band:
            stmt = stmt.where(ScenarioRow.criticality_band == band.upper())
        rows = (await session.execute(
            stmt.order_by(ScenarioRow.criticality_band.asc(), ScenarioRow.updated_at.desc())
        )).scalars().all()

    views = [_scenario_view(r) for r in rows]
    if state == "needs_approval":
        views = [
            v for v in views
            if v["diff_state"] in (synthesis.DIFF_NEW, synthesis.DIFF_CHANGED)
            and v["review_state"] != _APPROVED_STATE
        ]
    elif state in (synthesis.DIFF_NEW, synthesis.DIFF_CHANGED,
                   synthesis.DIFF_UNCHANGED, synthesis.DIFF_MISSING):
        views = [v for v in views if v["diff_state"] == state]
    return {"app_id": app_id, "state": state, "band": band or "",
            "scenarios": views, "total": len(views)}


async def _require_scenario(session, tenant_id: str, scenario_id: str) -> ScenarioRow:
    row = (await session.execute(
        select(ScenarioRow).where(
            ScenarioRow.scenario_id == scenario_id,
            ScenarioRow.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return row


@router.get("/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str, user: dict = Depends(require_auth)) -> dict:
    """Fetch one scenario in full (journey + evidence + review + snapshot)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_scenario(session, tenant_id, scenario_id)
        return row_to_dict(row)


@router.post("/scenarios/{scenario_id}/review")
async def review_scenario(
    scenario_id: str, payload: ReviewRequest, user: dict = Depends(_MUTATE),
) -> dict:
    """Transition a scenario through its sign-off lifecycle (hash-chained).

    ``approve`` REQUIRES a typed e-signature (422 otherwise — VKPower
    case-review parity) and snapshots the approved journey so a later
    re-synthesis cannot silently overwrite approved content; it records ONE
    ``scenario_approve`` human touch banded at the scenario's criticality.  The
    approval event is appended to the tamper-evident chain FIRST (the authority),
    then mirrored onto the scenario row."""
    tenant_id = user["tenant_id"]
    action = (payload.action or "").strip().lower()
    if action not in _SCENARIO_REVIEW_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {sorted(_SCENARIO_REVIEW_ACTIONS)}",
        )
    actor = _actor(user)

    # Load the scenario (band + fingerprint travel into the chain payload).
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_scenario(session, tenant_id, scenario_id)
        band = row.criticality_band
        fingerprint = row.fingerprint
        journey = dict(row.journey or {})

    # Append to the tamper-evident chain FIRST (enforces the signature gate).
    try:
        event = await approval.append_event(
            tenant_id=tenant_id, subject_kind=approval.SUBJECT_SCENARIO,
            subject_id=scenario_id, action=action,
            payload={"fingerprint": fingerprint, "band": band, "note": payload.note[:500]},
            signature=payload.signature, actor=actor,
        )
    except (approval.SignatureRequiredError, approval.InvalidActionError) as exc:
        raise HTTPException(status_code=_http_status_of(exc), detail=str(exc))

    # Mirror the decision onto the scenario row's review block.
    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_scenario(session, tenant_id, scenario_id)
        review = dict(row.review or {})
        history = list(review.get("history") or [])
        if action == "submit":
            review["state"] = "in_review"
        elif action == "approve":
            review["state"] = _APPROVED_STATE
            review["approved_by"] = actor
            review["approved_email"] = str(user.get("email") or "")
            review["approved_at"] = now.isoformat()
            review["signature"] = payload.signature.strip()[:200]
            review["chain_hash"] = event["chain_hash"]
            row.approved_snapshot = {
                "journey": journey, "fingerprint": fingerprint, "band": band,
                "approved_by": actor, "approved_at": now.isoformat(),
            }
        elif action == "reject":
            review["state"] = "rejected"
            review["rejected_by"] = actor
            review["rejected_at"] = now.isoformat()
        elif action == "reopen":
            review["state"] = "draft"
            for k in ("approved_by", "approved_at", "signature", "chain_hash"):
                review.pop(k, None)
        history.append({"action": action, "by": actor, "at": now.isoformat(),
                        "chain_hash": event["chain_hash"]})
        review["history"] = history[-50:]
        row.review = review
        row.updated_at = now
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "review")
        await session.flush()
        view = _scenario_view(row)

    # A completed sign-off is ONE human touch (carry-forward is never a touch).
    touch = None
    if action == "approve":
        touch = await touch_meter.record_touch(
            tenant_id=tenant_id, touch_type=touch_meter.TOUCH_SCENARIO_APPROVE,
            band=band, app_id=view["app_id"], actor=actor,
        )
    logger.info(
        "qec.scenario.reviewed",
        extra={"tenant_id": tenant_id, "scenario_id": scenario_id,
               "action": action, "is_touch": event["is_touch"], "actor": actor},
    )
    return {"scenario": view, "event": event, "touch": touch}


@router.post("/scenarios/{scenario_id}/materialize")
async def materialize_scenario(
    scenario_id: str, user: dict = Depends(_MUTATE),
) -> dict:
    """Materialize an APPROVED scenario into runnable cases (409 unless approved).

    The scenario is a governance overlay on a REAL crawl artifact; materializing
    binds it to that grounded ``source_artifact_id`` and triggers the UNCHANGED
    VKPower ``POST /generate`` over a service JWT so cases compile from the same
    table-loaded substrate a human would use.  A scenario that is not approved is
    a 409 — the gate is never bypassed.

    DEVIATION (noted): the design sketches a per-scenario carved artifact; that
    substrate-carving contract is UNVERIFIED/deferred (§3.4), so we generate on
    the grounded source artifact — honest and value-backed, never a stub."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_scenario(session, tenant_id, scenario_id)
        review_state = str((row.review or {}).get("state") or "draft")
        source_artifact_id = row.source_artifact_id
    if review_state != _APPROVED_STATE:
        raise HTTPException(
            status_code=409,
            detail=f"scenario is not approved (review_state={review_state}) — "
                   "approve it with an e-signature before materializing",
        )
    if not source_artifact_id:
        raise HTTPException(
            status_code=409,
            detail="scenario has no source artifact to materialize from",
        )

    try:
        generate = await factory.generate(tenant_id=tenant_id, artifact_id=source_artifact_id)
    except FactoryClientError as exc:
        raise HTTPException(
            status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
            detail=f"VKPower /generate failed: {exc.detail}",
        )

    now = utc_now()
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_scenario(session, tenant_id, scenario_id)
        row.materialized_artifact_id = source_artifact_id
        row.status = "materialized"
        row.updated_at = now
        await session.flush()
    logger.info(
        "qec.scenario.materialized",
        extra={"tenant_id": tenant_id, "scenario_id": scenario_id,
               "artifact_id": source_artifact_id, "actor": _actor(user)},
    )
    return {"scenario_id": scenario_id, "materialized_artifact_id": source_artifact_id,
            "generate": generate}


# ══════════════════════ certified invariants ══════════════════════════════

class InvariantCreate(BaseModel):
    """Author + CERTIFY one human P0 invariant (requires an e-signature)."""

    statement: str = Field(min_length=1, max_length=4000)
    signature: str = Field(min_length=1, max_length=200)
    criticality_band: str = Field(default="P0", max_length=8)
    requires_disposable_env: bool = True
    linked_scenario_ids: list[str] = Field(default_factory=list)


@router.post("/apps/{app_id}/invariants", status_code=201)
async def create_invariant(
    app_id: str, payload: InvariantCreate, user: dict = Depends(_MUTATE),
) -> dict:
    """Author + e-sign a certified invariant (the non-enumerable half of coverage).

    A blank signature is a 422 — an invariant with no human certification has no
    honest meaning.  Records ONE ``invariant_author`` human touch and appends the
    certification to the tamper-evident approval chain."""
    tenant_id = user["tenant_id"]
    signature = (payload.signature or "").strip()
    if not signature:
        raise HTTPException(
            status_code=422,
            detail="An e-signature (your full name) is required to certify an invariant",
        )
    band = (payload.criticality_band or "P0").strip().upper()
    if band not in criticality.BANDS:
        band = criticality.BAND_P0
    invariant_id = new_id()
    now = utc_now()
    actor = _actor(user)

    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(CertifiedInvariantRow(
            invariant_id=invariant_id, tenant_id=tenant_id, app_id=app_id,
            statement=payload.statement.strip(), criticality_band=band,
            signature=signature[:200], signed_by=actor[:200], signed_at=now,
            requires_disposable_env=bool(payload.requires_disposable_env),
            linked_scenario_ids=list(payload.linked_scenario_ids or []),
            status="certified",
        ))
        await session.flush()

    await approval.append_event(
        tenant_id=tenant_id, subject_kind=approval.SUBJECT_INVARIANT,
        subject_id=invariant_id, action=approval.ACTION_APPROVE,
        payload={"statement": payload.statement.strip()[:500], "band": band},
        signature=signature, actor=actor,
    )
    touch = await touch_meter.record_touch(
        tenant_id=tenant_id, touch_type=touch_meter.TOUCH_INVARIANT_AUTHOR,
        band=band, app_id=app_id, actor=actor,
    )
    logger.info(
        "qec.invariant.certified",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "invariant_id": invariant_id, "band": band, "actor": actor},
    )
    return {"invariant_id": invariant_id, "app_id": app_id, "band": band,
            "status": "certified", "touch": touch}


@router.get("/apps/{app_id}/invariants")
async def list_invariants(app_id: str, user: dict = Depends(require_auth)) -> dict:
    """List the app's certified invariants."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CertifiedInvariantRow)
            .where(
                CertifiedInvariantRow.tenant_id == tenant_id,
                CertifiedInvariantRow.app_id == app_id,
            )
            .order_by(CertifiedInvariantRow.created_at.desc())
        )).scalars().all()
    return {"app_id": app_id, "invariants": [row_to_dict(r) for r in rows],
            "total": len(rows)}


# ══════════════════════ tier labels ═══════════════════════════════════════

@router.post("/artifacts/{artifact_id}/tier-label")
async def compute_tier_label(
    artifact_id: str, user: dict = Depends(_MUTATE),
) -> dict:
    """Label every case of an artifact RENDERS vs BEHAVES from ``/rtm``.

    Fetches the read-only VKPower ``/rtm`` matrix over a service JWT, labels each
    case (BEHAVES iff ≥1 grounded navigation/outcome-region oracle), persists to
    ``qec_case_tiers``, and returns the MIN suite tier — so a fill-only suite can
    never read as behavioral even at a green delivery score."""
    tenant_id = user["tenant_id"]
    try:
        rtm = await factory.get_rtm(tenant_id=tenant_id, artifact_id=artifact_id)
    except FactoryClientError as exc:
        raise HTTPException(
            status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
            detail=f"VKPower /rtm failed: {exc.detail}",
        )
    result = await tier_label.label_and_persist(tenant_id, artifact_id, rtm)
    logger.info(
        "qec.tier_label.computed",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "suite_tier": result["suite_tier"], "actor": _actor(user)},
    )
    return result


@router.get("/artifacts/{artifact_id}/tier-label")
async def get_tier_label(artifact_id: str, user: dict = Depends(require_auth)) -> dict:
    """Read the persisted tier labels + MIN suite tier for an artifact."""
    tenant_id = user["tenant_id"]
    return await tier_label.read_case_tiers(tenant_id, artifact_id)


# ══════════════════════ human-touch meter ═════════════════════════════════

class TouchCreate(BaseModel):
    touch_type: str = Field(min_length=1, max_length=40)
    band: str = Field(default="", max_length=8)
    app_id: str = Field(default="", max_length=64)
    cycle_id: str = Field(default="", max_length=64)


class IngestRequest(BaseModel):
    app_id: str = Field(default="", max_length=64)
    limit: int = Field(default=500, ge=1, le=5000)
    since: datetime | None = None


@router.get("/apps/{app_id}/autonomy")
async def get_autonomy(
    app_id: str,
    cycle_id: str | None = Query(default=None, max_length=64),
    user: dict = Depends(require_auth),
) -> dict:
    """The autonomy KPI — PER criticality band (deliberately NEVER averaged).

    Each band carries its own governed-scenario denominator, human-touch
    numerator, and autonomy %, so a P0 that needed a human can never be averaged
    away by autonomous P3s."""
    tenant_id = user["tenant_id"]
    return await touch_meter.autonomy_by_band(
        tenant_id=tenant_id, app_id=app_id, cycle_id=cycle_id,
    )


@router.post("/touches", status_code=201)
async def create_touch(payload: TouchCreate, user: dict = Depends(_MUTATE)) -> dict:
    """Record ONE typed human touch (direct governance action)."""
    tenant_id = user["tenant_id"]
    try:
        return await touch_meter.record_touch(
            tenant_id=tenant_id, touch_type=payload.touch_type, band=payload.band,
            app_id=payload.app_id, cycle_id=payload.cycle_id, actor=_actor(user),
        )
    except touch_meter.InvalidTouchTypeError as exc:
        raise HTTPException(status_code=_http_status_of(exc), detail=str(exc))


@router.post("/touches/ingest")
async def ingest_touches(payload: IngestRequest, user: dict = Depends(_MUTATE)) -> dict:
    """Ingest recent HUMAN mutations from ``audit_log`` as ``vkpower_audit`` touches.

    Service-actor rows (the factory calls S4 itself makes) and non-sign-off
    actions are skipped; survivors are deduped on ``audit_log.log_id``."""
    tenant_id = user["tenant_id"]
    return await touch_meter.ingest_audit_log(
        tenant_id=tenant_id, app_id=payload.app_id,
        since=payload.since, limit=payload.limit,
    )


@router.get("/touches")
async def get_touches(
    app_id: str = Query(default="", max_length=64),
    limit: int = Query(default=500, ge=1, le=5000),
    user: dict = Depends(require_auth),
) -> dict:
    """Recent touches for the tenant (optionally one app), newest-first."""
    tenant_id = user["tenant_id"]
    touches = await touch_meter.list_touches(
        tenant_id=tenant_id, app_id=app_id, limit=limit,
    )
    return {"app_id": app_id, "touches": touches, "total": len(touches)}
