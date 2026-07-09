"""QE-Central S4 — coverage model, shrinkage guard, waiver lifecycle.

Coverage is modelled as **enumerable atoms** (routes / API endpoints / form
fields / journey edges / validators, enumerated from crawl + repo + answer-key)
measured against **human-certified invariants** (the non-enumerable half —
executed and certified, never auto-discovered).

The **universe-shrinkage guard** is the honesty spine: a previously-approved,
covered atom that DISAPPEARS from a fresh universe raises exactly one P0
``possible_deletion`` gap and flips the coverage verdict to
``blocked_on_p0_gaps`` until the gap is adjudicated or waived — a silent pass on
a shrinking universe is impossible.  The approved universe is anchored by the
e-signed ``atoms_hash`` in the LATEST ``qec_universe_baselines`` row
(:mod:`app.services.approval`); its membership is reconstructed from the
UPSERT-only ``qec_coverage_atoms`` table, and the reconstruction is
hash-verified against the signed baseline (mismatch ⇒ honest fail-up, never a
silent green).

**Waiver lifecycle** clones the ``verdict_events`` ``WaiverRow`` semantics
(verdict_events.py:263-341): a waiver ANNOTATES a gap (owner + reason + expiry),
it NEVER deletes it, and it stops applying automatically once expired — with a
hard 90-day ceiling on the requested expiry.

All pure primitives (hashing, diffing, gap construction, the verdict rule, the
waiver clamp) are unit-tested without a database; the ``async`` helpers persist
through the tenant-scoped qecentral session (RLS on every table).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import approval
from ..db import tenant_scoped_qec_session
from ..db.gov_models import (
    CaseTierRow,
    CertifiedInvariantRow,
    CoverageAtomRow,
    CoverageGapRow,
)

logger = logging.getLogger(__name__)

MODEL_VERSION = "qec-coverage-v1"

# ── Atom vocabulary (qec_coverage_atoms columns) ──────────────────────────
ATOM_ROUTE = "route"
ATOM_API_ENDPOINT = "api_endpoint"
ATOM_FORM_FIELD = "form_field"
ATOM_JOURNEY_EDGE = "journey_edge"
ATOM_VALIDATOR = "validator"
ATOM_KINDS = frozenset(
    {ATOM_ROUTE, ATOM_API_ENDPOINT, ATOM_FORM_FIELD, ATOM_JOURNEY_EDGE, ATOM_VALIDATOR}
)

SOURCE_CRAWL = "crawl"
SOURCE_REPO = "repo"
SOURCE_ANSWER_KEY = "answer_key"
ATOM_SOURCES = frozenset({SOURCE_CRAWL, SOURCE_REPO, SOURCE_ANSWER_KEY})

PROV_DETERMINISTIC = "G_DETERMINISTIC"
PROV_LIVE_CONFIRMED = "G_LIVE_CONFIRMED"
PROV_INFERRED = "G_INFERRED"
PROVENANCES = frozenset({PROV_DETERMINISTIC, PROV_LIVE_CONFIRMED, PROV_INFERRED})

# ── Criticality bands (local mirror; criticality.py owns the registry) ────
BAND_P0 = "P0"
BAND_P1 = "P1"
BAND_P2 = "P2"
BAND_P3 = "P3"

# ── Gap vocabulary (qec_coverage_gaps columns) ────────────────────────────
GAP_POSSIBLE_DELETION = "possible_deletion"
GAP_UNCOVERED_ATOM = "uncovered_atom"
GAP_TIER_GAP = "tier_gap"
GAP_UNCLASSIFIED = "unclassified_fail_up"
GAP_KINDS = frozenset(
    {GAP_POSSIBLE_DELETION, GAP_UNCOVERED_ATOM, GAP_TIER_GAP, GAP_UNCLASSIFIED}
)
# The gap kinds that BLOCK the all-green verdict while unresolved. Both are
# always P0: a vanished approved atom and an uncomputable universe fail UP.
_BLOCKING_KINDS = frozenset({GAP_POSSIBLE_DELETION, GAP_UNCLASSIFIED})

GAP_OPEN = "open"
GAP_ADJUDICATED = "adjudicated"
GAP_WAIVED = "waived"

# ── Coverage verdicts ─────────────────────────────────────────────────────
VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked_on_p0_gaps"

# ── Waiver ceiling ─────────────────────────────────────────────────────────
MAX_WAIVER_DAYS = 90

# Stable uuid5 namespaces — deterministic ids make atoms/gaps idempotent.
_ATOM_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "qec.coverage.atom.nexus")
_GAP_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "qec.coverage.gap.nexus")


@dataclass(frozen=True)
class AtomInput:
    """One enumerated atom to upsert into the universe."""

    canonical_key: str
    kind: str
    source: str = SOURCE_CRAWL
    provenance: str = PROV_INFERRED
    evidence: Mapping | None = None


@dataclass(frozen=True)
class UniverseDiff:
    """A fresh-vs-baseline set diff (all tuples are sorted, de-duplicated)."""

    missing: tuple[str, ...]   # in baseline, absent from fresh → possible deletions
    added: tuple[str, ...]     # in fresh, absent from baseline → growth
    retained: tuple[str, ...]  # in both


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | str | None) -> datetime | None:
    """Coerce an ISO string / naive datetime to a tz-aware UTC datetime."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Pure primitives: keys, hashing, ids ───────────────────────────────────

def normalize_keys(keys: Iterable[str]) -> list[str]:
    """Sorted, de-duplicated, non-empty canonical keys — the ONE ordering used
    everywhere a universe is hashed or diffed."""
    return sorted({str(k).strip() for k in keys if str(k).strip()})


def compute_atoms_hash(keys: Iterable[str]) -> str:
    """sha256 over the newline-joined normalized canonical keys.

    This is the ``atoms_hash`` stamped into a signed baseline and the anchor the
    shrinkage guard reconstructs against — so normalization MUST be identical on
    both sides (it is: both call :func:`normalize_keys`).
    """
    import hashlib

    normalized = normalize_keys(keys)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def atom_id_for(tenant_id: str, app_id: str, canonical_key: str) -> str:
    """Deterministic atom id (uuid5) — idempotent across recomputes."""
    return uuid.uuid5(_ATOM_NS, f"{tenant_id}|{app_id}|{canonical_key}").hex


def possible_deletion_gap_id(
    tenant_id: str, app_id: str, baseline_id: str, canonical_key: str,
) -> str:
    """Deterministic gap id for a vanished atom — one gap per (baseline, key)
    so re-running the guard NEVER duplicates a gap (and never clobbers a waiver
    already applied to it)."""
    return uuid.uuid5(
        _GAP_NS,
        f"{tenant_id}|{app_id}|{GAP_POSSIBLE_DELETION}|{baseline_id}|{canonical_key}",
    ).hex


def unclassified_gap_id(tenant_id: str, app_id: str, baseline_id: str) -> str:
    """Deterministic gap id for the one honest fail-up gap per baseline."""
    return uuid.uuid5(
        _GAP_NS, f"{tenant_id}|{app_id}|{GAP_UNCLASSIFIED}|{baseline_id}",
    ).hex


def diff_universe(
    baseline_keys: Iterable[str], fresh_keys: Iterable[str],
) -> UniverseDiff:
    """Diff a fresh universe against a baseline universe (pure).

    ``missing`` (baseline − fresh) are the possible deletions the guard acts on.
    """
    base = set(normalize_keys(baseline_keys))
    fresh = set(normalize_keys(fresh_keys))
    return UniverseDiff(
        missing=tuple(sorted(base - fresh)),
        added=tuple(sorted(fresh - base)),
        retained=tuple(sorted(base & fresh)),
    )


# ── Pure primitives: gap construction ─────────────────────────────────────

def build_possible_deletion_gap(
    *, tenant_id: str, app_id: str, baseline_id: str, canonical_key: str,
    evidence: Mapping | None = None,
) -> dict:
    """A P0 ``possible_deletion`` gap for one vanished approved atom (pure).

    ALWAYS band P0 — a previously-approved+covered atom disappearing is the
    highest-severity signal and blocks the all-green verdict.
    """
    detail = {
        "canonical_key": canonical_key,
        "baseline_id": baseline_id,
        "reason": (
            "a previously-approved, covered atom is MISSING from the fresh "
            "universe — possible deletion; blocks 'all green' until adjudicated "
            "or waived"
        ),
    }
    if evidence:
        detail["evidence"] = dict(evidence)
    return {
        "gap_id": possible_deletion_gap_id(tenant_id, app_id, baseline_id, canonical_key),
        "tenant_id": tenant_id,
        "app_id": app_id,
        "kind": GAP_POSSIBLE_DELETION,
        "band": BAND_P0,
        "detail": detail,
        "status": GAP_OPEN,
        "waiver": None,
        "waiver_expires_at": None,
    }


def build_unclassified_gap(
    *, tenant_id: str, app_id: str, baseline_id: str, reason: str,
) -> dict:
    """A P0 ``unclassified_fail_up`` gap — the honest fail-up when the approved
    universe cannot be trusted (reconstruction hash mismatch)."""
    return {
        "gap_id": unclassified_gap_id(tenant_id, app_id, baseline_id),
        "tenant_id": tenant_id,
        "app_id": app_id,
        "kind": GAP_UNCLASSIFIED,
        "band": BAND_P0,
        "detail": {"baseline_id": baseline_id, "reason": reason},
        "status": GAP_OPEN,
        "waiver": None,
        "waiver_expires_at": None,
    }


def gaps_for_shrinkage(
    *, tenant_id: str, app_id: str, baseline_id: str, missing_keys: Iterable[str],
) -> list[dict]:
    """One P0 ``possible_deletion`` gap per missing key (pure, deterministic)."""
    return [
        build_possible_deletion_gap(
            tenant_id=tenant_id, app_id=app_id, baseline_id=baseline_id,
            canonical_key=key,
        )
        for key in normalize_keys(missing_keys)
    ]


# ── Pure primitives: verdict + waiver lifecycle ───────────────────────────

def waiver_active(
    waiver: Mapping | None,
    *,
    waiver_expires_at: datetime | str | None = None,
    now: datetime | None = None,
) -> bool:
    """True iff a waiver exists and has NOT expired.

    Prefers the explicit ``waiver_expires_at`` column value; falls back to the
    waiver payload's ``expires_at``. A waiver with no parseable expiry is
    treated as INACTIVE (fail-up).
    """
    if not waiver:
        return False
    now = now or _utc_now()
    expires = _as_aware(waiver_expires_at) or _as_aware(
        waiver.get("expires_at") if isinstance(waiver, Mapping) else None
    )
    if expires is None:
        return False
    return expires > now


def is_gap_blocking(gap: Mapping, *, now: datetime | None = None) -> bool:
    """True iff this gap currently BLOCKS the all-green verdict.

    A gap blocks iff it is a P0 blocking-kind gap that is neither adjudicated
    nor covered by an ACTIVE (unexpired) waiver. An expired waiver stops
    applying automatically, so the gap blocks again — never a silent green.
    """
    if str(gap.get("band") or "") != BAND_P0:
        return False
    if str(gap.get("kind") or "") not in _BLOCKING_KINDS:
        return False
    status = str(gap.get("status") or GAP_OPEN)
    if status == GAP_ADJUDICATED:
        return False
    if status == GAP_WAIVED:
        return not waiver_active(
            gap.get("waiver"),
            waiver_expires_at=gap.get("waiver_expires_at"),
            now=now,
        )
    return True  # open


def coverage_verdict(gaps: Iterable[Mapping], *, now: datetime | None = None) -> str:
    """``blocked_on_p0_gaps`` if ANY gap is currently blocking, else ``ok``."""
    now = now or _utc_now()
    return VERDICT_BLOCKED if any(is_gap_blocking(g, now=now) for g in gaps) else VERDICT_OK


def clamp_waiver_expiry(
    *, now: datetime, requested: datetime | None = None,
) -> datetime:
    """Return a valid waiver expiry: at most ``now + 90d``, strictly future.

    ``None`` requested ⇒ the 90-day ceiling. A past/now expiry is rejected
    (a waiver that is already expired has no honest meaning).
    """
    ceiling = now + timedelta(days=MAX_WAIVER_DAYS)
    if requested is None:
        return ceiling
    requested = _as_aware(requested)
    if requested is None or requested <= now:
        raise ValueError("waiver expiry must be a future timestamp")
    return min(requested, ceiling)


def build_waiver(
    *, reason: str, actor: str, now: datetime | None = None,
    requested_expires_at: datetime | None = None,
) -> dict:
    """Build a waiver annotation (owner + reason + clamped expiry).

    Requires a non-empty ``reason`` and ``actor`` — a waiver is a governed
    exception, never anonymous.
    """
    now = now or _utc_now()
    reason = (reason or "").strip()
    actor = (actor or "").strip()
    if not reason:
        raise ValueError("a waiver requires a reason")
    if not actor:
        raise ValueError("a waiver requires an actor")
    expires = clamp_waiver_expiry(now=now, requested=requested_expires_at)
    return {
        "reason": reason[:500],
        "actor": actor[:200],
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "max_days": MAX_WAIVER_DAYS,
    }


def apply_waiver_to_gap(gap: Mapping, waiver: Mapping) -> dict:
    """Return an ANNOTATED copy of ``gap``: status→waived, waiver + expiry set.

    The gap's ``detail`` (which atom, the evidence) is preserved untouched — a
    waiver annotates, it never deletes the finding.
    """
    annotated = dict(gap)
    annotated["status"] = GAP_WAIVED
    annotated["waiver"] = dict(waiver)
    annotated["waiver_expires_at"] = waiver.get("expires_at")
    return annotated


# ── Async persistence (qecentral DB, tenant-scoped) ───────────────────────

def _atom_row_to_dict(row: CoverageAtomRow) -> dict:
    return {
        "atom_id": row.atom_id,
        "canonical_key": row.canonical_key,
        "kind": row.kind,
        "source": row.source,
        "provenance": row.provenance,
        "evidence": dict(row.evidence or {}),
        "first_seen": row.first_seen.isoformat() if row.first_seen else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
    }


def _gap_row_to_dict(row: CoverageGapRow) -> dict:
    return {
        "gap_id": row.gap_id,
        "tenant_id": row.tenant_id,
        "app_id": row.app_id,
        "kind": row.kind,
        "band": row.band,
        "detail": dict(row.detail or {}),
        "status": row.status,
        "waiver": dict(row.waiver) if row.waiver else None,
        "waiver_expires_at": (
            row.waiver_expires_at.isoformat() if row.waiver_expires_at else None
        ),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def upsert_atoms(
    *, tenant_id: str, app_id: str, atoms: Sequence[AtomInput],
) -> dict:
    """Idempotently upsert enumerated atoms into the universe.

    Insert on first sight (stamping ``first_seen``); on a re-sight bump
    ``last_seen`` and refresh source/provenance/evidence. Atoms are NEVER
    hard-deleted here — an atom's absence from a fresh crawl is expressed by
    its ``last_seen`` NOT being bumped, which is exactly what lets the shrinkage
    guard reconstruct an approved baseline's membership.
    """
    now = _utc_now()
    inserted = updated = skipped = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        for atom in atoms:
            key = str(atom.canonical_key or "").strip()
            if not key:
                skipped += 1
                continue
            if atom.kind not in ATOM_KINDS:
                raise ValueError(f"unknown atom kind: {atom.kind!r}")
            atom_id = atom_id_for(tenant_id, app_id, key)
            row = (await session.execute(
                select(CoverageAtomRow).where(
                    CoverageAtomRow.atom_id == atom_id,
                    CoverageAtomRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if row is None:
                session.add(CoverageAtomRow(
                    atom_id=atom_id, tenant_id=tenant_id, app_id=app_id,
                    canonical_key=key, kind=atom.kind, source=atom.source,
                    provenance=atom.provenance,
                    evidence=dict(atom.evidence or {}),
                    first_seen=now, last_seen=now,
                ))
                inserted += 1
            else:
                row.last_seen = now
                row.source = atom.source
                row.provenance = atom.provenance
                if atom.evidence:
                    row.evidence = dict(atom.evidence)
                updated += 1
        await session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": inserted + updated}


async def _read_atoms(
    *, tenant_id: str, app_id: str, first_seen_at_or_before: datetime | None = None,
) -> list[CoverageAtomRow]:
    stmt = select(CoverageAtomRow).where(
        CoverageAtomRow.tenant_id == tenant_id,
        CoverageAtomRow.app_id == app_id,
    )
    if first_seen_at_or_before is not None:
        stmt = stmt.where(CoverageAtomRow.first_seen <= first_seen_at_or_before)
    async with tenant_scoped_qec_session(tenant_id) as session:
        return list((await session.execute(stmt)).scalars().all())


async def compute_universe(*, tenant_id: str, app_id: str) -> dict:
    """Compute the current universe from ``qec_coverage_atoms``.

    Returns the sorted ``canonical_keys``, the ``atoms_hash`` (the exact value
    to sign into a baseline via :func:`approval.append_universe_baseline`), the
    ``atom_count``, and per-kind / per-source / per-provenance breakdowns.
    """
    rows = await _read_atoms(tenant_id=tenant_id, app_id=app_id)
    keys = normalize_keys(r.canonical_key for r in rows)
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_prov: dict[str, int] = {}
    for r in rows:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        by_source[r.source] = by_source.get(r.source, 0) + 1
        by_prov[r.provenance] = by_prov.get(r.provenance, 0) + 1
    return {
        "model_version": MODEL_VERSION,
        "app_id": app_id,
        "canonical_keys": keys,
        "atoms_hash": compute_atoms_hash(keys),
        "atom_count": len(keys),
        "by_kind": by_kind,
        "by_source": by_source,
        "by_provenance": by_prov,
    }


async def _reconstruct_approved_keys(
    *, tenant_id: str, app_id: str, cutoff: datetime,
) -> list[str]:
    """The canonical keys that existed at/before a baseline's ``created_at``.

    Relies on the UPSERT-only atom invariant: an atom present when the baseline
    was signed still exists (with its original ``first_seen``), so the approved
    membership is reconstructable.
    """
    rows = await _read_atoms(
        tenant_id=tenant_id, app_id=app_id, first_seen_at_or_before=cutoff,
    )
    return normalize_keys(r.canonical_key for r in rows)


async def _persist_gaps(
    *, tenant_id: str, app_id: str, gaps: Sequence[Mapping], now: datetime,
) -> int:
    """Insert gaps idempotently (by deterministic ``gap_id``).

    An existing gap row is left UNTOUCHED so a re-run never clobbers a waiver or
    adjudication already applied to it. Returns the number newly inserted.
    """
    if not gaps:
        return 0
    inserted = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        for gap in gaps:
            exists = (await session.execute(
                select(CoverageGapRow.gap_id).where(
                    CoverageGapRow.gap_id == gap["gap_id"],
                    CoverageGapRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if exists is not None:
                continue
            session.add(CoverageGapRow(
                gap_id=gap["gap_id"], tenant_id=tenant_id, app_id=app_id,
                kind=gap["kind"], band=gap["band"], detail=dict(gap["detail"]),
                status=gap.get("status", GAP_OPEN),
                waiver=gap.get("waiver"),
                waiver_expires_at=_as_aware(gap.get("waiver_expires_at")),
                created_at=now, updated_at=now,
            ))
            inserted += 1
        await session.commit()
    return inserted


async def list_gaps(
    *, tenant_id: str, app_id: str, status: str | None = None,
) -> list[dict]:
    """All coverage gaps for ``(tenant, app)`` (optionally filtered by status)."""
    stmt = select(CoverageGapRow).where(
        CoverageGapRow.tenant_id == tenant_id,
        CoverageGapRow.app_id == app_id,
    )
    if status:
        stmt = stmt.where(CoverageGapRow.status == status)
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            stmt.order_by(CoverageGapRow.created_at.asc())
        )).scalars().all()
    return [_gap_row_to_dict(r) for r in rows]


async def shrinkage_guard(
    *, tenant_id: str, app_id: str, fresh_keys: Iterable[str],
    baseline: Mapping | None = None, now: datetime | None = None,
) -> dict:
    """Diff a fresh universe against the LATEST signed baseline; raise P0 gaps.

    ``fresh_keys`` are the canonical keys the current crawl/repo/answer-key
    enumerated (the caller passes them explicitly — an unknown fresh universe
    must never be inferred as "unchanged").

    Steps:
      1. Load the latest baseline (none ⇒ nothing to shrink below → ``ok``).
      2. Reconstruct the approved membership and HASH-VERIFY it against the
         signed ``atoms_hash``. A mismatch is an honest fail-up: one P0
         ``unclassified_fail_up`` gap and a blocked verdict.
      3. For every approved key MISSING from the fresh universe, raise exactly
         one idempotent P0 ``possible_deletion`` gap.
      4. Recompute the app-wide verdict over ALL open gaps.

    Returns ``{verdict, missing, added, gaps_created, hash_verified,
    baseline_id, approved_count, fresh_count, reason}``.
    """
    now = now or _utc_now()
    if baseline is None:
        baseline = await approval.latest_universe_baseline(
            tenant_id=tenant_id, app_id=app_id,
        )
    fresh = normalize_keys(fresh_keys)
    if baseline is None:
        return {
            "verdict": VERDICT_OK, "reason": "no_baseline",
            "missing": [], "added": [], "gaps_created": 0,
            "hash_verified": None, "baseline_id": "",
            "approved_count": 0, "fresh_count": len(fresh),
        }

    baseline_id = str(baseline.get("baseline_id") or "")
    cutoff = _as_aware(baseline.get("created_at")) or now
    approved = await _reconstruct_approved_keys(
        tenant_id=tenant_id, app_id=app_id, cutoff=cutoff,
    )
    hash_verified = compute_atoms_hash(approved) == str(baseline.get("atoms_hash") or "")

    gaps: list[dict] = []
    if not hash_verified:
        logger.warning(
            "qec.coverage.baseline_hash_mismatch",
            extra={"app_id": app_id, "baseline_id": baseline_id,
                   "reconstructed": len(approved),
                   "baseline_count": baseline.get("atom_count")},
        )
        gaps.append(build_unclassified_gap(
            tenant_id=tenant_id, app_id=app_id, baseline_id=baseline_id,
            reason=(
                "approved-universe reconstruction hash does NOT match the signed "
                "baseline atoms_hash — atom-table integrity uncertain; failing up"
            ),
        ))

    diff = diff_universe(approved, fresh)
    gaps.extend(gaps_for_shrinkage(
        tenant_id=tenant_id, app_id=app_id, baseline_id=baseline_id,
        missing_keys=diff.missing,
    ))
    gaps_created = await _persist_gaps(
        tenant_id=tenant_id, app_id=app_id, gaps=gaps, now=now,
    )

    open_gaps = await list_gaps(tenant_id=tenant_id, app_id=app_id)
    verdict = coverage_verdict(open_gaps, now=now)
    logger.info(
        "qec.coverage.shrinkage_guard",
        extra={"app_id": app_id, "baseline_id": baseline_id,
               "missing": len(diff.missing), "gaps_created": gaps_created,
               "verdict": verdict, "hash_verified": hash_verified},
    )
    return {
        "verdict": verdict,
        "missing": list(diff.missing),
        "added": list(diff.added),
        "gaps_created": gaps_created,
        "hash_verified": hash_verified,
        "baseline_id": baseline_id,
        "approved_count": len(approved),
        "fresh_count": len(fresh),
        "reason": "" if hash_verified else "baseline_hash_mismatch",
    }


async def waive_gap(
    *, tenant_id: str, app_id: str, gap_id: str, reason: str, actor: str,
    requested_expires_at: datetime | None = None, now: datetime | None = None,
) -> dict:
    """Annotate a gap with a waiver (never delete). Clamps expiry to ≤ 90d.

    Raises ``KeyError`` if the gap does not exist and ``ValueError`` on a blank
    reason/actor or a non-future expiry.
    """
    now = now or _utc_now()
    waiver = build_waiver(
        reason=reason, actor=actor, now=now, requested_expires_at=requested_expires_at,
    )
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(CoverageGapRow).where(
                CoverageGapRow.gap_id == gap_id,
                CoverageGapRow.tenant_id == tenant_id,
                CoverageGapRow.app_id == app_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise KeyError(f"coverage gap {gap_id} not found")
        row.status = GAP_WAIVED
        row.waiver = waiver
        row.waiver_expires_at = _as_aware(waiver["expires_at"])
        row.updated_at = now
        await session.commit()
        result = _gap_row_to_dict(row)
    logger.info(
        "qec.coverage.gap_waived",
        extra={"app_id": app_id, "gap_id": gap_id, "actor": actor[:64]},
    )
    return result


async def adjudicate_gap(
    *, tenant_id: str, app_id: str, gap_id: str, actor: str, note: str = "",
    now: datetime | None = None,
) -> dict:
    """Mark a gap adjudicated (a human decided it is acceptable / handled).

    Annotates ``detail`` with the adjudication record; never deletes the row.
    """
    now = now or _utc_now()
    actor = (actor or "").strip()
    if not actor:
        raise ValueError("adjudication requires an actor")
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(CoverageGapRow).where(
                CoverageGapRow.gap_id == gap_id,
                CoverageGapRow.tenant_id == tenant_id,
                CoverageGapRow.app_id == app_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise KeyError(f"coverage gap {gap_id} not found")
        detail = dict(row.detail or {})
        detail["adjudication"] = {
            "actor": actor[:200], "note": (note or "")[:500], "at": now.isoformat(),
        }
        row.detail = detail
        row.status = GAP_ADJUDICATED
        row.updated_at = now
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "detail")
        await session.commit()
        result = _gap_row_to_dict(row)
    return result


async def _invariant_summary(*, tenant_id: str, app_id: str) -> dict:
    """Certified-invariant coverage counts (the non-enumerable half)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CertifiedInvariantRow).where(
                CertifiedInvariantRow.tenant_id == tenant_id,
                CertifiedInvariantRow.app_id == app_id,
            )
        )).scalars().all()
    by_status: dict[str, int] = {}
    linked = 0
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.linked_scenario_ids:
            linked += 1
    return {"total": len(rows), "by_status": by_status, "linked_to_scenarios": linked}


async def _tier_distribution(*, tenant_id: str, app_id: str) -> dict:
    """RENDERS-vs-BEHAVES distribution over the app's labelled cases.

    Best-effort read of ``qec_case_tiers`` (owned by tier_label.py). Cases are
    keyed by (tenant, artifact, test); a nexus artifact is not app-scoped in
    that table, so this reports the tenant-wide tier mix as a coarse signal.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CaseTierRow.tier).where(CaseTierRow.tenant_id == tenant_id)
        )).scalars().all()
    dist: dict[str, int] = {}
    for tier in rows:
        dist[tier] = dist.get(tier, 0) + 1
    return dist


async def compute_coverage(
    *, tenant_id: str, app_id: str, now: datetime | None = None,
) -> dict:
    """The coverage scorecard: atoms + invariants + NAMED gaps + verdict.

    ``verdict`` is ``blocked_on_p0_gaps`` while any P0 blocking gap is open (or
    its waiver has expired), else ``ok``. Every blocking gap is named in
    ``gaps`` so the block is never opaque.
    """
    now = now or _utc_now()
    universe = await compute_universe(tenant_id=tenant_id, app_id=app_id)
    invariants = await _invariant_summary(tenant_id=tenant_id, app_id=app_id)
    tiers = await _tier_distribution(tenant_id=tenant_id, app_id=app_id)
    gaps = await list_gaps(tenant_id=tenant_id, app_id=app_id)
    for g in gaps:
        g["blocking"] = is_gap_blocking(g, now=now)
    verdict = coverage_verdict(gaps, now=now)
    return {
        "model_version": MODEL_VERSION,
        "app_id": app_id,
        "generated_at": now.isoformat(),
        "verdict": verdict,
        "atoms": {
            "count": universe["atom_count"],
            "by_kind": universe["by_kind"],
            "by_source": universe["by_source"],
            "by_provenance": universe["by_provenance"],
            "atoms_hash": universe["atoms_hash"],
        },
        "invariants": invariants,
        "tier_distribution": tiers,
        "gaps": gaps,
        "open_gaps": sum(1 for g in gaps if g["status"] == GAP_OPEN),
        "blocking_gaps": sum(1 for g in gaps if g["blocking"]),
    }
