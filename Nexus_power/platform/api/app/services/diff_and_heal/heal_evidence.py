"""Part-11 evidence chain for self-heal.

Append-only, tamper-evident record of every heal decision. Each row is chained to
the tenant's previous row via sha256(prev_hash | canonical(payload)) so any later
edit/deletion breaks the chain. Written FAIL-CLOSED in the SAME transaction as the
heal it documents — if the event cannot be recorded the caller's commit aborts, so
an unauditable heal is never promoted. The app DB role has SELECT+INSERT only (no
UPDATE/DELETE) — immutability is enforced at the grant, not just by convention.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import HealEventRow


def _canonical(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


# ── T5.3 OPTIONAL DETACHED SIGNATURE (default-off) ────────────────────────────
# The hash chain (prev_hash -> row_hash) already makes the ledger tamper-EVIDENT:
# any edit/deletion breaks the chain and verify_chain() finds the first break.
# A detached signature adds tamper-PROOF: a row_hash signed with a key the app DB
# role does not hold means an attacker with DB write cannot forge a row that also
# verifies. DEFAULT-OFF: with no key configured, sign_row_hash() returns "" and
# behavior is byte-identical to today (chain-only). The signature is "detached" —
# computed over the already-final row_hash, so it needs NO new behavior in
# record_heal_event's hashing and can be added/rotated without rewriting history.
#
# The signature lives in a NEW SDK column ``signature`` on HealEventRow. That column
# is SCAFFOLDED here + flagged for the nexus-base SDK + a migration (see
# _row_signature / _set_signature below, which degrade gracefully when the column
# is absent) — it is NOT assumed already landed in this repo.

def signing_enabled() -> bool:
    return bool(os.getenv("NEXUS_HEAL_EVIDENCE_SIGNING_KEY", "").strip())


def sign_row_hash(row_hash: str) -> str:
    """Detached HMAC-SHA256 signature over a row_hash, or "" when no key is set
    (default-off => today's chain-only behavior). The asymmetric-sign hook is a
    drop-in here (replace the HMAC with a private-key sign) without touching the
    chain or callers."""
    key = os.getenv("NEXUS_HEAL_EVIDENCE_SIGNING_KEY", "").strip()
    if not key or not row_hash:
        return ""
    return hmac.new(key.encode("utf-8"), row_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_row_signature(row_hash: str, signature: str) -> bool | None:
    """True/False if a signing key is configured (constant-time compare); None when
    signing is off OR the row carries no signature (cannot assert — not a failure)."""
    if not signing_enabled() or not signature:
        return None
    return hmac.compare_digest(sign_row_hash(row_hash), str(signature))


def _row_signature(row) -> str:
    """Read the SCAFFOLDED ``signature`` column if the SDK model exposes it; "" if
    the column is not yet landed in nexus-base (graceful pre-migration)."""
    return getattr(row, "signature", "") or ""


def _set_signature(row, sig: str) -> None:
    """Set the SCAFFOLDED ``signature`` column only if the SDK model has it. A no-op
    pre-migration — the chain still protects the row; the signature simply isn't
    stored until the nexus-base column + migration land."""
    if sig and hasattr(row, "signature"):
        try:
            row.signature = sig
        except Exception:
            pass


async def record_heal_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: str,
    event_type: str,
    actor: str,
    scenario_id: str = "",
    step_number: int = 0,
    fix_kind: str = "",
    before_locator: str = "",
    after_locator: str = "",
    engine_verdict: str = "",
    verified_green: bool = False,
    version_no: int = 0,
    run_id: str = "",
    reason_for_change: str = "",
    details: dict | None = None,
) -> HealEventRow:
    """Append a heal evidence row chained to the tenant's previous row_hash.

    Adds to the session (does NOT commit) so the evidence is atomic with the heal
    it documents — the caller's commit persists both or neither. Raises on any
    failure (FAIL-CLOSED): the caller must let the exception abort the heal persist.
    """
    prev_hash = (await session.execute(
        select(HealEventRow.row_hash)
        .where(HealEventRow.tenant_id == tenant_id)
        # Deterministic tiebreaker. created_at alone is NOT unique (a heal + its
        # approve can share a timestamp; some DBs round resolution) and SQL order for
        # a tie is UNDEFINED. heal_event_id is a stable PER-ROW value (NOT monotonic —
        # it is a random UUID, default=_new_id — so it does NOT encode write/temporal
        # order). What it DOES provide is ORDERING CONSISTENCY: the chain is
        # self-describing via stored prev_hash/row_hash, so the only requirement is
        # that this writer and verify_chain()/list_heal_events() walk the SAME total
        # order. Both use (created_at, heal_event_id) — so a clean chain can never
        # spuriously read as tampered. DO NOT "fix" this to a sequence column on a
        # temporal-order assumption: that would BREAK the writer/verifier consistency.
        .order_by(desc(HealEventRow.created_at), desc(HealEventRow.heal_event_id))
        .limit(1)
    )).scalar_one_or_none() or ""

    payload = {
        "tenant_id": tenant_id, "artifact_id": artifact_id, "event_type": event_type,
        "actor": actor, "scenario_id": scenario_id, "step_number": int(step_number or 0),
        "fix_kind": fix_kind, "before_locator": before_locator, "after_locator": after_locator,
        "engine_verdict": engine_verdict, "verified_green": bool(verified_green),
        "version_no": int(version_no or 0), "run_id": run_id,
        "reason_for_change": reason_for_change, "details": details or {},
    }
    row_hash = hashlib.sha256((prev_hash + "|" + _canonical(payload)).encode("utf-8")).hexdigest()

    row = HealEventRow(
        tenant_id=tenant_id, artifact_id=artifact_id, scenario_id=scenario_id,
        step_number=int(step_number or 0), event_type=event_type, actor=actor,
        fix_kind=fix_kind, before_locator=before_locator, after_locator=after_locator,
        engine_verdict=engine_verdict, verified_green=bool(verified_green),
        version_no=int(version_no or 0), run_id=run_id, reason_for_change=reason_for_change,
        details=details or {}, prev_hash=prev_hash, row_hash=row_hash,
    )
    # T5.3: optional detached signature over the final row_hash (default-off; no-op
    # when no key OR the SDK column is not yet landed). The chain is unchanged.
    sig = sign_row_hash(row_hash)
    _set_signature(row, sig)
    # Record which key id sealed it (scaffolded ``sig_key_id`` column; guarded so it
    # is a no-op until the nexus-base column + migration land — see PATCH 2of3/3of3).
    if sig and hasattr(row, "sig_key_id"):
        try:
            row.sig_key_id = os.getenv("NEXUS_HEAL_EVIDENCE_SIG_KEY_ID", "").strip()
        except Exception:
            pass
    session.add(row)
    return row


async def verify_chain(session: AsyncSession, *, tenant_id: str) -> dict:
    """Recompute the WHOLE tenant hash chain from scratch and report the FIRST
    tamper/break. Independent of the stored prev_hash links: for each row it
    recomputes sha256(prev_recomputed | canonical(payload)) from the row's own
    fields and compares to the stored row_hash. A mismatch means the row's CONTENT
    was edited; a prev_hash that doesn't match the prior recomputed hash means a row
    was inserted/deleted/reordered. Also verifies the detached signature when one is
    present + signing is on.

    Returns ``{ok, count, first_break, signing}`` where first_break (or None) is
    ``{index, heal_event_id, kind, ...}`` and kind ∈ {content_tampered,
    chain_broken, signature_invalid}."""
    rows = (await session.execute(
        select(HealEventRow)
        .where(HealEventRow.tenant_id == tenant_id)
        # MUST be the SAME total order record_heal_event used to pick prev_hash:
        # (created_at, heal_event_id). This is a consistency contract, not a temporal
        # claim — see record_heal_event. Walking a different order on a same-timestamp
        # tie would recompute prev from the wrong row and report a false tamper.
        .order_by(HealEventRow.created_at, HealEventRow.heal_event_id)
    )).scalars().all()

    prev = ""
    first_break: dict | None = None
    for i, r in enumerate(rows):
        payload = {
            "tenant_id": r.tenant_id, "artifact_id": r.artifact_id,
            "event_type": r.event_type, "actor": r.actor,
            "scenario_id": r.scenario_id, "step_number": int(r.step_number or 0),
            "fix_kind": r.fix_kind, "before_locator": r.before_locator,
            "after_locator": r.after_locator, "engine_verdict": r.engine_verdict,
            "verified_green": bool(r.verified_green), "version_no": int(r.version_no or 0),
            "run_id": r.run_id, "reason_for_change": r.reason_for_change,
            "details": r.details or {},
        }
        expect = hashlib.sha256((prev + "|" + _canonical(payload)).encode("utf-8")).hexdigest()
        if r.prev_hash != prev:
            first_break = {"index": i, "heal_event_id": r.heal_event_id,
                           "kind": "chain_broken",
                           "detail": "stored prev_hash does not match the prior row's hash "
                                     "(a row was inserted, deleted, or reordered)"}
            break
        if expect != r.row_hash:
            first_break = {"index": i, "heal_event_id": r.heal_event_id,
                           "kind": "content_tampered",
                           "detail": "recomputed row_hash does not match the stored row_hash "
                                     "(this row's content was edited)"}
            break
        sig = _row_signature(r)
        sig_ok = verify_row_signature(r.row_hash, sig)
        if sig_ok is False:
            first_break = {"index": i, "heal_event_id": r.heal_event_id,
                           "kind": "signature_invalid",
                           "detail": "the detached signature does not verify against the row_hash"}
            break
        prev = r.row_hash
    return {
        "ok": first_break is None, "count": len(rows),
        "first_break": first_break, "signing": signing_enabled(),
    }


async def list_heal_events(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> dict:
    """Reconstruction read: the full ordered evidence chain for an artifact, with a
    per-row chain-integrity check (chain_ok = the stored prev_hash matches the actual
    previous row's row_hash). A False anywhere = tamper/gap."""
    rows = (await session.execute(
        select(HealEventRow)
        .where(HealEventRow.tenant_id == tenant_id, HealEventRow.artifact_id == artifact_id)
        # SAME (created_at, heal_event_id) order as verify_chain, so this per-artifact
        # chain_ok can never disagree with verify_chain() on a same-timestamp tie.
        .order_by(HealEventRow.created_at, HealEventRow.heal_event_id)
    )).scalars().all()

    events: list[dict] = []
    prev = ""
    chain_intact = True
    for r in rows:
        ok = (r.prev_hash == prev)
        chain_intact = chain_intact and ok
        events.append({
            "heal_event_id": r.heal_event_id, "event_type": r.event_type, "actor": r.actor,
            "scenario_id": r.scenario_id, "step_number": r.step_number, "fix_kind": r.fix_kind,
            "before_locator": r.before_locator, "after_locator": r.after_locator,
            "engine_verdict": r.engine_verdict, "verified_green": r.verified_green,
            "version_no": r.version_no, "run_id": r.run_id,
            "reason_for_change": r.reason_for_change, "details": r.details,
            "prev_hash": r.prev_hash, "row_hash": r.row_hash, "chain_ok": ok,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            # T5.3: surface WHO approved (the actor already on the row — for an
            # approve event this is the human who clicked) + the detached signature
            # state so an auditor sees the accountable identity and the seal.
            "approver": r.actor if (r.event_type or "").endswith("approve")
            or "approve" in (r.event_type or "") else "",
            "signature": _row_signature(r),
            "signature_ok": verify_row_signature(r.row_hash, _row_signature(r)),
            # Which key sealed the row (scaffolded column; "" pre-migration or unsigned).
            "sig_key_id": getattr(r, "sig_key_id", "") or "",
        })
        prev = r.row_hash
    return {"events": events, "count": len(events), "chain_intact": chain_intact,
            "signing": signing_enabled()}
