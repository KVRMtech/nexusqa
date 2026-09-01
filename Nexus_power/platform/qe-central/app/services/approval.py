"""QE-Central S4 — the approval gate (append-only, hash-chained).

Two append-only, hash-chained event logs, both using byte-for-byte the
``verdict_events.py:66-124`` recipe:

  * ``qec_approval_events`` — scenario / invariant / gap sign-off, chained per
    ``(tenant_id, subject_kind, subject_id)``.  ``approve`` REQUIRES a typed
    e-signature (the router maps a missing one to HTTP 422 — parity with the
    VKPower case-review lifecycle, test_factory.py:4265-4272).  ``carry_forward``
    (the UNCHANGED-scenario auto-carry) is recorded for audit but is NOT a
    human touch.

  * ``qec_universe_baselines`` — the e-signed, hash-chained approved-universe
    snapshot, chained per ``(tenant_id, app_id)``; ``atoms_hash`` is the anchor
    the coverage shrinkage guard diffs fresh universes against.

Design invariants (never green-wash):
  * ``chain_hash = sha256(prev_hash + canonical_json(payload))`` where
    ``canonical_json`` is ``json.dumps(sort_keys=True, separators=(',',':'),
    default=str)`` — deterministic and replayable.
  * The chain is TAMPER-EVIDENT: mutating any prior event's canonical fields
    (or its stored hashes) makes :func:`verify_chain` report the exact break.
  * Appends are IMMUTABLE — rows are only ever inserted, never updated.

The pure primitives (``canonical_json``, ``compute_chain_hash``,
``build_approval_event``, ``build_baseline_event``, ``verify_chain``,
``require_signature``, ``is_human_touch``) carry all the security logic and are
unit-tested with no database.  The ``async`` helpers persist through the
tenant-scoped qecentral session and are exercised behind ``QEC_TEST_DATABASE_URL``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session
from ..db.gov_models import ApprovalEventRow, UniverseBaselineRow

logger = logging.getLogger(__name__)

# Registry version stamped alongside every chain so a replay knows the rules.
REGISTRY_VERSION = "qec-approval-v1"

# ── Subject kinds (what a chain is ABOUT) ─────────────────────────────────
SUBJECT_SCENARIO = "scenario"
SUBJECT_UNIVERSE = "universe"
SUBJECT_INVARIANT = "invariant"
SUBJECT_COVERAGE_GAP = "coverage_gap"
SUBJECT_JOURNEY_BASELINE = "journey_baseline"
SUBJECT_KINDS = frozenset(
    {SUBJECT_SCENARIO, SUBJECT_UNIVERSE, SUBJECT_INVARIANT,
     SUBJECT_COVERAGE_GAP, SUBJECT_JOURNEY_BASELINE}
)

# ── Actions (the qec_approval_events.action vocabulary) ───────────────────
ACTION_SUBMIT = "submit"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_REOPEN = "reopen"
ACTION_CARRY_FORWARD = "carry_forward"
VALID_ACTIONS = frozenset(
    {ACTION_SUBMIT, ACTION_APPROVE, ACTION_REJECT, ACTION_REOPEN, ACTION_CARRY_FORWARD}
)

# Only an explicit human decision carrying an e-signature may approve.
SIGNATURE_REQUIRED_ACTIONS = frozenset({ACTION_APPROVE})

# Actions that constitute a HUMAN touch for the autonomy KPI. A carry-forward
# is machine-driven (auto-carry of an UNCHANGED scenario) and never counts even
# when its action is 'approve' — see :func:`is_human_touch`.
HUMAN_TOUCH_ACTIONS = frozenset({ACTION_APPROVE, ACTION_REJECT, ACTION_REOPEN})

SIGNATURE_MAX = 200
ACTOR_MAX = 200

# The canonical fields hashed into an approval event's chain — tampering with
# ANY of these breaks the chain. ``action`` and ``signature`` are load-bearing
# (they prove WHAT was decided and WHO signed), so both are inside the hash.
_APPROVAL_CANONICAL_FIELDS = (
    "tenant_id", "subject_kind", "subject_id", "action",
    "payload", "signature", "actor", "carry_forward", "registry_version",
)
# The canonical fields hashed into a universe-baseline event.
_BASELINE_CANONICAL_FIELDS = (
    "tenant_id", "app_id", "atoms_hash", "atom_count",
    "signature", "signed_by", "registry_version",
)


class SignatureRequiredError(ValueError):
    """Raised when a signature-gated action arrives without a typed signature.

    Carries ``http_status = 422`` so the router can map it to the same
    Unprocessable-Entity response the VKPower case-review endpoint returns
    (test_factory.py:4267).
    """

    http_status = 422

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or "An e-signature (your full name) is required to approve"
        )


class InvalidActionError(ValueError):
    """Raised when an approval action is not in :data:`VALID_ACTIONS`."""

    http_status = 422


@dataclass(frozen=True)
class ChainVerification:
    """Result of a hash-chain integrity check.

    ``ok`` is True only when every event links to its predecessor AND every
    stored ``chain_hash`` reproduces from ``prev_hash + canonical_json``.
    ``break_index`` is the 0-based index of the FIRST inconsistent event
    (``None`` when intact); ``reason`` names the failure class.
    """

    ok: bool
    length: int
    break_index: int | None = None
    reason: str = ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Pure chain primitives (the security core; no DB) ──────────────────────

def canonical_json(payload: Mapping[str, Any]) -> str:
    """Deterministic canonical JSON (verdict_events.py:66-71 recipe).

    Sorted keys, tight separators, ``default=str`` so datetimes/UUIDs/Decimals
    serialise stably. The SAME text must be produced on write and on replay.
    """
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), default=str
    )


def compute_chain_hash(prev_hash: str, canonical: str) -> str:
    """``sha256(prev_hash + canonical)`` as hex (verdict_events.py:112-113)."""
    return hashlib.sha256((str(prev_hash or "") + canonical).encode("utf-8")).hexdigest()


def _project(event: Mapping[str, Any], fields: Sequence[str]) -> dict:
    """Project ``event`` onto ``fields`` with normalised scalar types so the
    canonical payload is byte-identical whether built in Python or reloaded
    from the database."""
    out: dict[str, Any] = {}
    for key in fields:
        val = event.get(key)
        if key == "carry_forward":
            val = bool(val)
        elif key == "atom_count":
            val = int(val or 0)
        elif key == "payload":
            val = dict(val or {})
        elif key == "registry_version":
            val = str(val or REGISTRY_VERSION)
        else:
            val = "" if val is None else str(val)
        out[key] = val
    return out


def approval_canonical(event: Mapping[str, Any]) -> str:
    """Canonical JSON for an approval event's chain hash."""
    return canonical_json(_project(event, _APPROVAL_CANONICAL_FIELDS))


def baseline_canonical(event: Mapping[str, Any]) -> str:
    """Canonical JSON for a universe-baseline event's chain hash."""
    return canonical_json(_project(event, _BASELINE_CANONICAL_FIELDS))


def require_signature(action: str, signature: str) -> str:
    """Return the trimmed signature, or raise for a signature-gated action.

    Raises :class:`SignatureRequiredError` (422) when ``action`` is in
    :data:`SIGNATURE_REQUIRED_ACTIONS` and ``signature`` is blank. For every
    other action a blank signature is allowed and the trimmed value returned.
    """
    sig = (signature or "").strip()
    if action in SIGNATURE_REQUIRED_ACTIONS and not sig:
        raise SignatureRequiredError()
    return sig[:SIGNATURE_MAX]


def is_human_touch(action: str, carry_forward: bool) -> bool:
    """True iff this event is a HUMAN touch for the autonomy KPI.

    A carry-forward is machine-driven (auto-carry of an UNCHANGED scenario) and
    is NEVER a human touch, even if its action is ``approve`` — that is the
    whole point of the zero-touch carry path.
    """
    if carry_forward:
        return False
    return action in HUMAN_TOUCH_ACTIONS


def build_approval_event(
    *,
    prev_hash: str,
    tenant_id: str,
    subject_kind: str,
    subject_id: str,
    action: str,
    payload: Mapping[str, Any] | None = None,
    signature: str = "",
    actor: str = "",
    carry_forward: bool = False,
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    """Build ONE fully-formed approval event (pure — no DB).

    Validates the action, enforces the signature gate, and computes the chain
    hash from ``prev_hash``. This is the single writer of chain hashes so the
    async appender and the unit tests use identical logic.
    """
    act = (action or "").strip().lower()
    if act not in VALID_ACTIONS:
        raise InvalidActionError(
            f"action must be one of {sorted(VALID_ACTIONS)} (got {action!r})"
        )
    sig = require_signature(act, signature)
    event = {
        "event_id": event_id or uuid.uuid4().hex,
        "tenant_id": str(tenant_id or ""),
        "subject_kind": str(subject_kind or ""),
        "subject_id": str(subject_id or ""),
        "action": act,
        "payload": dict(payload or {}),
        "signature": sig,
        "actor": str(actor or "")[:ACTOR_MAX],
        "carry_forward": bool(carry_forward),
        "registry_version": REGISTRY_VERSION,
        "prev_hash": str(prev_hash or ""),
        "created_at": created_at or _utc_now(),
    }
    event["chain_hash"] = compute_chain_hash(event["prev_hash"], approval_canonical(event))
    event["is_touch"] = is_human_touch(act, event["carry_forward"])
    return event


def build_baseline_event(
    *,
    prev_hash: str,
    tenant_id: str,
    app_id: str,
    atoms_hash: str,
    atom_count: int,
    signature: str,
    signed_by: str = "",
    baseline_id: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    """Build ONE fully-formed universe-baseline event (pure — no DB).

    A baseline is an approval by definition, so a blank signature is refused
    with :class:`SignatureRequiredError` (422).
    """
    sig = (signature or "").strip()
    if not sig:
        raise SignatureRequiredError(
            "An e-signature is required to approve a universe baseline"
        )
    event = {
        "baseline_id": baseline_id or uuid.uuid4().hex,
        "tenant_id": str(tenant_id or ""),
        "app_id": str(app_id or ""),
        "atoms_hash": str(atoms_hash or ""),
        "atom_count": int(atom_count or 0),
        "signature": sig[:SIGNATURE_MAX],
        "signed_by": str(signed_by or "")[:ACTOR_MAX],
        "registry_version": REGISTRY_VERSION,
        "prev_hash": str(prev_hash or ""),
        "created_at": created_at or _utc_now(),
    }
    event["chain_hash"] = compute_chain_hash(event["prev_hash"], baseline_canonical(event))
    return event


def verify_chain(
    events: Sequence[Mapping[str, Any]],
    *,
    canonical: Callable[[Mapping[str, Any]], str] = approval_canonical,
) -> ChainVerification:
    """Verify a hash chain given oldest-first ``events``.

    Two independent checks per event:
      1. LINKAGE: ``event.prev_hash`` equals the previous event's
         ``chain_hash`` (the first event must link to ``""``).
      2. INTEGRITY: ``event.chain_hash`` reproduces from
         ``sha256(prev_hash + canonical(event))``.

    Any tamper — editing a prior payload, action, signature, or a stored hash —
    surfaces as the earliest inconsistent index. ``canonical`` selects the
    field projection (approval vs baseline).
    """
    prev = ""
    for idx, event in enumerate(events):
        stored_prev = str(event.get("prev_hash") or "")
        stored_chain = str(event.get("chain_hash") or "")
        if stored_prev != prev:
            return ChainVerification(
                ok=False, length=len(events), break_index=idx,
                reason="linkage_broken",
            )
        recomputed = compute_chain_hash(stored_prev, canonical(event))
        if recomputed != stored_chain:
            return ChainVerification(
                ok=False, length=len(events), break_index=idx,
                reason="hash_mismatch",
            )
        prev = stored_chain
    return ChainVerification(ok=True, length=len(events))


def verify_approval_chain(events: Sequence[Mapping[str, Any]]) -> ChainVerification:
    """:func:`verify_chain` with the approval-event projection."""
    return verify_chain(events, canonical=approval_canonical)


def verify_baseline_chain(events: Sequence[Mapping[str, Any]]) -> ChainVerification:
    """:func:`verify_chain` with the universe-baseline projection."""
    return verify_chain(events, canonical=baseline_canonical)


# ── Async persistence (qecentral DB, tenant-scoped) ───────────────────────

def _approval_row_to_event(row: ApprovalEventRow) -> dict:
    """Project an ORM row onto the canonical-field dict for verification."""
    return {
        "event_id": row.event_id,
        "tenant_id": row.tenant_id,
        "subject_kind": row.subject_kind,
        "subject_id": row.subject_id,
        "action": row.action,
        "payload": dict(row.payload or {}),
        "signature": row.signature,
        "actor": row.actor,
        "carry_forward": bool(row.carry_forward),
        "registry_version": REGISTRY_VERSION,
        "prev_hash": row.prev_hash,
        "chain_hash": row.chain_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "is_touch": is_human_touch(row.action, bool(row.carry_forward)),
    }


def _baseline_row_to_event(row: UniverseBaselineRow) -> dict:
    return {
        "baseline_id": row.baseline_id,
        "tenant_id": row.tenant_id,
        "app_id": row.app_id,
        "atoms_hash": row.atoms_hash,
        "atom_count": int(row.atom_count or 0),
        "signature": row.signature,
        "signed_by": row.signed_by,
        "registry_version": REGISTRY_VERSION,
        "prev_hash": row.prev_hash,
        "chain_hash": row.chain_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def append_event(
    *,
    tenant_id: str,
    subject_kind: str,
    subject_id: str,
    action: str,
    payload: Mapping[str, Any] | None = None,
    signature: str = "",
    actor: str = "",
    carry_forward: bool = False,
) -> dict:
    """Append one hash-chained approval event and return its record.

    Reads the latest ``chain_hash`` for ``(tenant_id, subject_kind,
    subject_id)`` (newest ``created_at``), builds the event (enforcing the
    signature gate — :class:`SignatureRequiredError` propagates so the router
    can 422), inserts it, commits, and returns
    ``{event_id, action, prev_hash, chain_hash, is_touch, carry_forward,
    created_at}``.

    NOTE: signature/action validation happens BEFORE any DB write, so a refused
    approve leaves no row.  Errors other than the typed signature/action
    failures propagate to the caller (never silently swallowed — an approval
    that appears to succeed but did not persist would be a green-wash).
    """
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required for an approval event")
    # Fail fast on the signature/action gate before opening a transaction.
    act = (action or "").strip().lower()
    if act not in VALID_ACTIONS:
        raise InvalidActionError(
            f"action must be one of {sorted(VALID_ACTIONS)} (got {action!r})"
        )
    require_signature(act, signature)

    async with tenant_scoped_qec_session(tenant_id) as session:
        prev = (await session.execute(
            select(ApprovalEventRow.chain_hash)
            .where(
                ApprovalEventRow.tenant_id == tenant_id,
                ApprovalEventRow.subject_kind == subject_kind,
                ApprovalEventRow.subject_id == subject_id,
            )
            .order_by(ApprovalEventRow.created_at.desc())
            .limit(1)
        )).scalar() or ""
        event = build_approval_event(
            prev_hash=prev, tenant_id=tenant_id, subject_kind=subject_kind,
            subject_id=subject_id, action=act, payload=payload,
            signature=signature, actor=actor, carry_forward=carry_forward,
        )
        session.add(ApprovalEventRow(
            event_id=event["event_id"],
            tenant_id=event["tenant_id"],
            subject_kind=event["subject_kind"],
            subject_id=event["subject_id"],
            action=event["action"],
            payload=event["payload"],
            signature=event["signature"],
            actor=event["actor"],
            carry_forward=event["carry_forward"],
            prev_hash=event["prev_hash"],
            chain_hash=event["chain_hash"],
            created_at=event["created_at"],
        ))
        await session.commit()
    logger.info(
        "qec.approval.appended",
        extra={"subject_kind": subject_kind, "subject_id": subject_id,
               "action": act, "is_touch": event["is_touch"]},
    )
    return {
        "event_id": event["event_id"],
        "action": event["action"],
        "prev_hash": event["prev_hash"],
        "chain_hash": event["chain_hash"],
        "is_touch": event["is_touch"],
        "carry_forward": event["carry_forward"],
        "created_at": event["created_at"].isoformat(),
    }


async def list_events(
    *,
    tenant_id: str,
    subject_kind: str,
    subject_id: str,
    limit: int = 500,
) -> list[dict]:
    """Return the approval events for a subject OLDEST-first.

    Oldest-first is the order :func:`verify_approval_chain` expects.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(ApprovalEventRow)
            .where(
                ApprovalEventRow.tenant_id == tenant_id,
                ApprovalEventRow.subject_kind == subject_kind,
                ApprovalEventRow.subject_id == subject_id,
            )
            .order_by(ApprovalEventRow.created_at.asc())
            .limit(max(1, min(5000, limit)))
        )).scalars().all()
    return [_approval_row_to_event(r) for r in rows]


async def append_universe_baseline(
    *,
    tenant_id: str,
    app_id: str,
    atoms_hash: str,
    atom_count: int,
    signature: str,
    signed_by: str = "",
) -> dict:
    """Append one e-signed, hash-chained universe baseline.

    Chained per ``(tenant_id, app_id)``.  A blank ``signature`` is refused with
    :class:`SignatureRequiredError` (422) BEFORE any DB write.
    """
    if not str(tenant_id or "").strip():
        raise ValueError("tenant_id is required for a universe baseline")
    if not (signature or "").strip():
        raise SignatureRequiredError(
            "An e-signature is required to approve a universe baseline"
        )
    async with tenant_scoped_qec_session(tenant_id) as session:
        prev = (await session.execute(
            select(UniverseBaselineRow.chain_hash)
            .where(
                UniverseBaselineRow.tenant_id == tenant_id,
                UniverseBaselineRow.app_id == app_id,
            )
            .order_by(UniverseBaselineRow.created_at.desc())
            .limit(1)
        )).scalar() or ""
        event = build_baseline_event(
            prev_hash=prev, tenant_id=tenant_id, app_id=app_id,
            atoms_hash=atoms_hash, atom_count=atom_count,
            signature=signature, signed_by=signed_by,
        )
        session.add(UniverseBaselineRow(
            baseline_id=event["baseline_id"],
            tenant_id=event["tenant_id"],
            app_id=event["app_id"],
            atoms_hash=event["atoms_hash"],
            atom_count=event["atom_count"],
            signature=event["signature"],
            signed_by=event["signed_by"],
            prev_hash=event["prev_hash"],
            chain_hash=event["chain_hash"],
            created_at=event["created_at"],
        ))
        await session.commit()
    logger.info(
        "qec.universe_baseline.appended",
        extra={"app_id": app_id, "atom_count": event["atom_count"]},
    )
    return {
        "baseline_id": event["baseline_id"],
        "atoms_hash": event["atoms_hash"],
        "atom_count": event["atom_count"],
        "prev_hash": event["prev_hash"],
        "chain_hash": event["chain_hash"],
        "created_at": event["created_at"].isoformat(),
    }


async def latest_universe_baseline(*, tenant_id: str, app_id: str) -> dict | None:
    """Return the most-recent baseline for ``(tenant, app)``, or ``None``."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(UniverseBaselineRow)
            .where(
                UniverseBaselineRow.tenant_id == tenant_id,
                UniverseBaselineRow.app_id == app_id,
            )
            .order_by(UniverseBaselineRow.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    return _baseline_row_to_event(row) if row is not None else None


async def list_universe_baselines(
    *, tenant_id: str, app_id: str, limit: int = 500,
) -> list[dict]:
    """Return the baselines for ``(tenant, app)`` OLDEST-first (chain order)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(UniverseBaselineRow)
            .where(
                UniverseBaselineRow.tenant_id == tenant_id,
                UniverseBaselineRow.app_id == app_id,
            )
            .order_by(UniverseBaselineRow.created_at.asc())
            .limit(max(1, min(5000, limit)))
        )).scalars().all()
    return [_baseline_row_to_event(r) for r in rows]
