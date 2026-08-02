"""FIELD LEARNING — remember for one client, generalise for everyone.

Two stores with deliberately different shapes, because they carry deliberately
different risk:

  :func:`remember` / :func:`recall`   TENANT-PRIVATE.  The value a client actually
      supplied, envelope-encrypted exactly like a credential card and scoped by
      tenant. This is what stops the same question being asked twice.

  :func:`observe_prior` / :func:`load_priors`   CROSS-TENANT and VALUE-FREE.  Only
      that a field with a given signature turned out to be a given SEMANTIC TYPE,
      and how many independent tenants agreed. This is what makes the
      hundred-and-first client start smarter than the first.

THE RULE THIS MODULE EXISTS TO ENFORCE
    Values never leave the tenant. Only shapes do.

    The priors table has no column a value could be written to, which is the real
    enforcement — a future writer cannot leak through a column that does not
    exist. :func:`observe_prior` adds a second, redundant guard anyway: it refuses
    a semantic type outside the closed vocabulary, so nothing free-form can be
    smuggled into the one string column it does write.

Contribution to the shared table is OPT-IN per tenant and defaults to off. A
regulated client must be able to use the product without their field shapes ever
reaching a shared table, and the safe default is the one that applies before
anyone has decided.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .persona_store import Base

_logger = logging.getLogger(__name__)

#: The closed vocabulary, mirrored from the explorer's field_semantics. Duplicated
#: rather than imported because the two services deploy independently — a shared
#: import would make a prior written by one unreadable by the other after a
#: partial rollout. Any type not in this set is refused, never stored.
VOCABULARY = frozenset({
    "unknown", "given_name", "family_name", "full_name", "email", "phone",
    "date_of_birth", "age", "national_id", "street_address", "street_address_2",
    "city", "region", "postal_code", "country", "company", "job_title", "url",
    "card_number", "card_expiry", "card_cvc", "currency_amount", "percent",
    "quantity", "date", "time", "password", "one_time_code", "username",
    "free_text", "choice", "consent",
})

#: A remembered value is capped — a field memory is a form value, not a document.
MAX_VALUE_BYTES = 4096
#: Beyond this many memories per artifact the crawl is learning noise, not answers.
MAX_MEMORIES_PER_ARTIFACT = 2000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


# ── models ───────────────────────────────────────────────────────────────────

class TpFieldMemoryRow(Base):
    __tablename__ = "tp_field_memory"
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_version: Mapped[int] = mapped_column(Integer, default=1)
    semantic_type: Mapped[str] = mapped_column(String(48), default="unknown")
    field_label: Mapped[str] = mapped_column(String(300), default="")
    value_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    provenance: Mapped[str] = mapped_column(String(24), default="provided")
    accept_count: Mapped[int] = mapped_column(Integer, default=0)
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    last_outcome: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class FieldPriorRow(Base):
    """CROSS-TENANT. Note what is absent: no tenant, no artifact, no value."""
    __tablename__ = "field_priors"
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)
    semantic_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    signature_version: Mapped[int] = mapped_column(Integer, default=1)
    tenant_count: Mapped[int] = mapped_column(Integer, default=0)
    observations: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class FieldPriorContributorRow(Base):
    __tablename__ = "field_prior_contributors"
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TenantLearningConsentRow(Base):
    __tablename__ = "tenant_learning_consent"
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contribute: Mapped[bool] = mapped_column(Boolean, default=False)
    consume: Mapped[bool] = mapped_column(Boolean, default=True)
    decided_by: Mapped[str] = mapped_column(String(200), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ── consent ──────────────────────────────────────────────────────────────────

async def get_consent(session: AsyncSession, *, tenant_id: str) -> dict:
    """What this tenant has agreed to. Absent = the safe default: consume the
    shared knowledge, contribute nothing to it."""
    try:
        row = (await session.execute(select(TenantLearningConsentRow).where(
            TenantLearningConsentRow.tenant_id == tenant_id))).scalar_one_or_none()
    except Exception:
        row = None
    if row is None:
        return {"tenant_id": tenant_id, "contribute": False, "consume": True,
                "decided": False, "decided_by": ""}
    return {"tenant_id": tenant_id, "contribute": bool(row.contribute),
            "consume": bool(row.consume), "decided": True,
            "decided_by": row.decided_by or ""}


async def set_consent(session: AsyncSession, *, tenant_id: str, contribute: bool,
                      consume: bool, decided_by: str) -> dict:
    """Record a decision. Caller commits."""
    stmt = (pg_insert(TenantLearningConsentRow).values(
        tenant_id=tenant_id, contribute=bool(contribute), consume=bool(consume),
        decided_by=str(decided_by or "")[:200], decided_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TenantLearningConsentRow.tenant_id],
            set_={"contribute": bool(contribute), "consume": bool(consume),
                  "decided_by": str(decided_by or "")[:200], "decided_at": _utc_now()}))
    await session.execute(stmt)
    return {"tenant_id": tenant_id, "contribute": bool(contribute),
            "consume": bool(consume), "decided": True}


# ── P1 · tenant-private memory ───────────────────────────────────────────────

def _aad(tenant_id: str, artifact_id: str, signature: str) -> bytes:
    """Bind the ciphertext to exactly one tenant, artifact and field, so a blob
    lifted from one row cannot be decrypted in the context of another."""
    return f"fieldmem::{tenant_id}::{artifact_id}::{signature}".encode("utf-8")


async def remember(session: AsyncSession, *, envelope, tenant_id: str,
                   artifact_id: str, signature: str, value: str,
                   semantic_type: str = "unknown", field_label: str = "",
                   signature_version: int = 1) -> dict:
    """Store a value this client supplied, encrypted. Caller commits.

    Only ``provided`` values are ever remembered. A synthesized value is
    regenerated identically from the identity seed every crawl, so storing it
    would add risk (a real row holding real-looking personal data) for no benefit
    at all."""
    if envelope is None:
        raise RuntimeError("encryption unavailable — refusing to store a field value in plaintext")
    v = "" if value is None else str(value)
    if not v.strip():
        raise ValueError("refusing to remember an empty value")
    payload = v.encode("utf-8")
    if len(payload) > MAX_VALUE_BYTES:
        raise ValueError("field value too large to remember")

    count = (await session.execute(
        select(func.count()).select_from(TpFieldMemoryRow).where(
            TpFieldMemoryRow.tenant_id == tenant_id,
            TpFieldMemoryRow.artifact_id == artifact_id))).scalar() or 0
    existing = (await session.execute(select(TpFieldMemoryRow.memory_id).where(
        TpFieldMemoryRow.tenant_id == tenant_id,
        TpFieldMemoryRow.artifact_id == artifact_id,
        TpFieldMemoryRow.signature == signature))).scalar_one_or_none()
    if existing is None and count >= MAX_MEMORIES_PER_ARTIFACT:
        raise ValueError("field-memory limit reached for this application")

    blob = await envelope.encrypt(tenant_id, payload,
                                  aad=_aad(tenant_id, artifact_id, signature))
    raw = blob.to_bytes()
    stmt = (pg_insert(TpFieldMemoryRow).values(
        memory_id=existing or _new_id(), tenant_id=tenant_id, artifact_id=artifact_id,
        signature=signature, signature_version=int(signature_version or 1),
        semantic_type=_coerce_type(semantic_type), field_label=str(field_label or "")[:300],
        value_blob=raw, provenance="provided", created_at=_utc_now(), updated_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpFieldMemoryRow.tenant_id, TpFieldMemoryRow.artifact_id,
                            TpFieldMemoryRow.signature],
            # A rewritten value has NOT been proven against the app, so the accept
            # /reject history of the value it replaced must not carry over — that
            # history described a different string.
            set_={"value_blob": raw, "semantic_type": _coerce_type(semantic_type),
                  "field_label": str(field_label or "")[:300], "provenance": "provided",
                  "accept_count": 0, "reject_count": 0, "last_outcome": "",
                  "updated_at": _utc_now()}))
    await session.execute(stmt)
    return {"signature": signature, "semantic_type": _coerce_type(semantic_type),
            "remembered": True}


async def recall(session: AsyncSession, *, envelope, tenant_id: str,
                 artifact_id: str) -> dict[str, str]:
    """Every remembered value for this application, as ``{signature: value}``.

    Never raises: a crawl that cannot read its memory must still run, filling what
    it can and asking for the rest — degraded, never broken. A memory the
    application has REJECTED more often than it accepted is withheld, because a
    remembered wrong answer is worse than no answer: it looks like an answer."""
    if envelope is None:
        return {}
    try:
        rows = (await session.execute(select(TpFieldMemoryRow).where(
            TpFieldMemoryRow.tenant_id == tenant_id,
            TpFieldMemoryRow.artifact_id == artifact_id))).scalars().all()
    except Exception as exc:
        _logger.warning("test_factory.field_memory.recall_failed error=%s", str(exc)[:200])
        return {}
    out: dict[str, str] = {}
    for r in rows:
        if r.reject_count > r.accept_count and r.reject_count > 0:
            continue
        try:
            plain = await envelope.decrypt(
                tenant_id, r.value_blob, aad=_aad(tenant_id, artifact_id, r.signature))
            out[r.signature] = plain.decode("utf-8")
        except Exception:
            # A blob we cannot open is a field we must ask about again. Silently
            # skipping is right; raising would strand the whole crawl.
            continue
    return out


async def list_memories(session: AsyncSession, *, tenant_id: str,
                        artifact_id: str) -> list[dict]:
    """Non-secret inventory for the operator: what we remember, never the values."""
    try:
        rows = (await session.execute(select(TpFieldMemoryRow).where(
            TpFieldMemoryRow.tenant_id == tenant_id,
            TpFieldMemoryRow.artifact_id == artifact_id)
            .order_by(TpFieldMemoryRow.updated_at.desc()))).scalars().all()
    except Exception:
        return []
    return [{
        "signature": r.signature, "semantic_type": r.semantic_type,
        "field_label": r.field_label, "provenance": r.provenance,
        "accepted": r.accept_count, "rejected": r.reject_count,
        "last_outcome": r.last_outcome,
        "updated_at": r.updated_at.isoformat() if r.updated_at else "",
    } for r in rows]


async def forget(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                 signature: str = "") -> int:
    """Delete remembered values. A client must be able to make us forget."""
    from sqlalchemy import delete
    stmt = delete(TpFieldMemoryRow).where(
        TpFieldMemoryRow.tenant_id == tenant_id,
        TpFieldMemoryRow.artifact_id == artifact_id)
    if signature:
        stmt = stmt.where(TpFieldMemoryRow.signature == signature)
    res = await session.execute(stmt)
    return int(res.rowcount or 0)


# ── P5 · did the application actually accept it? ─────────────────────────────

async def record_outcome(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                         signature: str, accepted: bool) -> None:
    """Close the loop on the APPLICATION'S verdict, not on our optimism.

    A recalled value that the app rejects is demoted, and once it has been
    rejected more often than accepted :func:`recall` stops offering it — so the
    field returns to the residue ask instead of silently poisoning every future
    crawl. Caller commits."""
    col = TpFieldMemoryRow.accept_count if accepted else TpFieldMemoryRow.reject_count
    await session.execute(update(TpFieldMemoryRow).where(
        TpFieldMemoryRow.tenant_id == tenant_id,
        TpFieldMemoryRow.artifact_id == artifact_id,
        TpFieldMemoryRow.signature == signature,
    ).values({col: col + 1,
              TpFieldMemoryRow.last_outcome: "accepted" if accepted else "rejected",
              TpFieldMemoryRow.updated_at: _utc_now()}))


# ── P4 · cross-tenant priors ─────────────────────────────────────────────────

def _coerce_type(value: Any) -> str:
    """The cage, restated at the storage boundary.

    Whatever proposed this — a language model, a stored row, a future caller — only
    a member of the closed vocabulary is ever written. This is what stops the one
    free-form string column on the shared table from becoming a channel."""
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in VOCABULARY else "unknown"


def _tenant_hash(tenant_id: str) -> str:
    """A salted, one-way hash. The shared table needs to count DISTINCT tenants
    without being able to name one."""
    salt = os.getenv("NEXUS_PRIOR_SALT", "") or "nexus-field-priors"
    return hashlib.sha256(f"{salt}::{tenant_id}".encode("utf-8")).hexdigest()[:32]


async def observe_prior(session: AsyncSession, *, tenant_id: str, signature: str,
                        semantic_type: str, signature_version: int = 1,
                        accepted: Optional[bool] = None) -> bool:
    """Record that a field with this signature was this semantic type.

    Returns whether anything was written. NO VALUE IS ACCEPTED BY THIS FUNCTION —
    there is no parameter one could be passed through, and the only string it
    writes is forced into the closed vocabulary first. Caller commits.

    An ``unknown`` reading teaches nothing and is not stored; recording it would
    let a signature accumulate confidence in having no meaning."""
    sem = _coerce_type(semantic_type)
    if not signature or sem == "unknown":
        return False

    consent = await get_consent(session, tenant_id=tenant_id)
    if not consent["contribute"]:
        return False

    th = _tenant_hash(tenant_id)
    first_from_this_tenant = (await session.execute(
        select(FieldPriorContributorRow.tenant_hash).where(
            FieldPriorContributorRow.signature == signature,
            FieldPriorContributorRow.tenant_hash == th))).scalar_one_or_none() is None
    if first_from_this_tenant:
        await session.execute(pg_insert(FieldPriorContributorRow).values(
            signature=signature, tenant_hash=th, created_at=_utc_now()
        ).on_conflict_do_nothing())

    acc = 1 if accepted is True else 0
    rej = 1 if accepted is False else 0
    stmt = (pg_insert(FieldPriorRow).values(
        signature=signature, semantic_type=sem,
        signature_version=int(signature_version or 1),
        tenant_count=1 if first_from_this_tenant else 0,
        observations=1, accepted=acc, rejected=rej,
        first_seen=_utc_now(), last_seen=_utc_now())
        .on_conflict_do_update(
            index_elements=[FieldPriorRow.signature, FieldPriorRow.semantic_type],
            set_={
                "observations": FieldPriorRow.observations + 1,
                "tenant_count": FieldPriorRow.tenant_count + (1 if first_from_this_tenant else 0),
                "accepted": FieldPriorRow.accepted + acc,
                "rejected": FieldPriorRow.rejected + rej,
                "last_seen": _utc_now(),
            }))
    await session.execute(stmt)
    return True


def _confidence(row: FieldPriorRow) -> float:
    """How much a prior is worth believing.

    Independent TENANTS dominate: one client crawling fifty times is one opinion,
    fifty clients agreeing once each is a fact. Rejections by the application
    subtract, because the app is the only thing here that actually knows."""
    tenants = max(0, int(row.tenant_count or 0))
    obs = max(1, int(row.observations or 0))
    acc, rej = int(row.accepted or 0), int(row.rejected or 0)
    breadth = min(0.5, tenants * 0.1)
    depth = min(0.2, obs * 0.02)
    if acc + rej > 0:
        verdict = (acc - rej) / float(acc + rej) * 0.3
    else:
        verdict = 0.0
    return round(max(0.0, min(0.95, 0.3 + breadth + depth + verdict)), 3)


async def load_priors(session: AsyncSession, *, tenant_id: str,
                      signatures: Optional[Iterable[str]] = None,
                      min_confidence: float = 0.4) -> dict[str, dict]:
    """The shared knowledge this crawl may use, as ``{signature: {type, confidence}}``.

    Consuming is on by default and contributing is not: reading the pooled shapes
    costs a client nothing and leaks nothing, while writing to it is a decision
    only they can make."""
    consent = await get_consent(session, tenant_id=tenant_id)
    if not consent["consume"]:
        return {}
    try:
        q = select(FieldPriorRow)
        sigs = [s for s in (signatures or []) if s]
        if sigs:
            q = q.where(FieldPriorRow.signature.in_(sigs[:5000]))
        rows = (await session.execute(q)).scalars().all()
    except Exception as exc:
        _logger.warning("test_factory.field_priors.load_failed error=%s", str(exc)[:200])
        return {}

    best: dict[str, dict] = {}
    for r in rows:
        conf = _confidence(r)
        if conf < min_confidence:
            continue
        cur = best.get(r.signature)
        if cur is None or conf > cur["confidence"]:
            best[r.signature] = {"type": r.semantic_type, "confidence": conf,
                                 "tenants": int(r.tenant_count or 0),
                                 "observations": int(r.observations or 0)}
    return best


def priors_are_value_free(payload: Any) -> bool:
    """A guard a caller can assert on before shipping priors anywhere.

    The priors that travel to the crawler are ``{signature: {type, confidence,
    tenants, observations}}`` and nothing else. If a future change starts carrying
    a value, every consumer that checks this stops rather than distributing it."""
    if not isinstance(payload, Mapping):
        return False
    allowed = {"type", "confidence", "tenants", "observations"}
    for sig, entry in payload.items():
        if not isinstance(sig, str) or not isinstance(entry, Mapping):
            return False
        if set(entry.keys()) - allowed:
            return False
        if _coerce_type(entry.get("type")) != entry.get("type"):
            return False
    return True
