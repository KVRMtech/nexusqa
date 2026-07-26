"""Persona × Environment Matrix — storage layer (P0 foundation).

RUN = Suite × Environment × Persona. This module owns the six registries
(recipes, personas, credential cards, answer sheets, value classifications,
reservations). Everything is tenant-scoped (RLS); credential cards are
envelope-encrypted exactly like ``auth_profiles`` — ciphertext at rest, never
returned by an API, AAD-bound so a card cannot be replayed elsewhere.

Keyed by ``artifact_id`` (the crawled representation of an app) — consistent
with the run/report/certification machinery, which is all artifact-scoped.

Every helper degrades safe when a table is absent (pre-migration): reads return
empty, and the back-compat shim still surfaces today's form-login as persona-0.
The ORM binds the SDK ``Base``; the tables are created out-of-band by
``scripts/apply_persona_env.sql`` (the auth_profiles pattern).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, Integer, LargeBinary, String, Text, and_, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base
from nexus_sdk.security.envelope import EnvelopeBlob

logger = logging.getLogger(__name__)

MAX_CARD_BYTES = 256 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


# ── ORM rows ─────────────────────────────────────────────────────────────────

class TpLoginRecipeRow(Base):
    __tablename__ = "tp_login_recipes"
    recipe_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    slots: Mapped[list] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(String(24), default="crawl_demonstration")
    status: Mapped[str] = mapped_column(String(16), default="active")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_env: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpPersonaRow(Base):
    __tablename__ = "tp_personas"
    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    traits: Mapped[list] = mapped_column(JSONB, default=list)
    behavior_class: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_recording_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpPersonaCredentialRow(Base):
    __tablename__ = "tp_persona_credentials"
    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    slot_names: Mapped[list] = mapped_column(JSONB, default=list)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verify_status: Mapped[str] = mapped_column(String(16), default="unverified")
    verified_epoch: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpPersonaExpectedValueRow(Base):
    __tablename__ = "tp_persona_expected_values"
    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    expected_value: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(24), default="crawl_observed")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpValueClassificationRow(Base):
    __tablename__ = "tp_value_classifications"
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    class_: Mapped[str] = mapped_column("class", String(20), default="unknown")
    evidence: Mapped[str] = mapped_column(String(24), default="unclassified")
    scenario_id: Mapped[str] = mapped_column(String(64), default="")
    step_number: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpPersonaReservationRow(Base):
    __tablename__ = "tp_persona_reservations"
    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), default="")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TpEnvironmentRow(Base):
    __tablename__ = "tp_environments"
    environment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64), default="")
    label: Mapped[str] = mapped_column(String(120), default="")
    posture: Mapped[str] = mapped_column(String(16), default="read_write")
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    write_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    base_url: Mapped[str] = mapped_column(Text, default="")
    data_epoch: Mapped[str] = mapped_column(String(64), default="")
    health_status: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[str] = mapped_column(Text, default="")
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpCaseScopeRow(Base):
    __tablename__ = "tp_case_scopes"
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    required_behavior_class: Mapped[str] = mapped_column(String(64), default="")
    evidence: Mapped[str] = mapped_column(String(24), default="structure_fork")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


# ── Recipes ──────────────────────────────────────────────────────────────────

async def save_recipe(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                      steps: list, slots: list, app_id: str = "",
                      source: str = "crawl_demonstration") -> dict:
    """Persist a NEW recipe version (monotonic per artifact); supersede prior
    active ones. Caller commits. Returns the stored recipe (non-secret)."""
    existing = list((await session.execute(
        select(TpLoginRecipeRow).where(TpLoginRecipeRow.tenant_id == tenant_id,
                                     TpLoginRecipeRow.artifact_id == artifact_id)
    )).scalars().all())
    next_version = (max((r.version for r in existing), default=0) + 1)
    for r in existing:
        if r.status == "active":
            r.status = "superseded"
    rid = _new_id()
    session.add(TpLoginRecipeRow(
        recipe_id=rid, tenant_id=tenant_id, artifact_id=artifact_id, app_id=app_id,
        version=next_version, steps=list(steps or []), slots=list(slots or []),
        source=source, status="active", created_at=_utc_now()))
    await session.flush()
    return {"recipe_id": rid, "version": next_version, "slots": list(slots or []),
            "step_count": len(steps or [])}


async def get_recipe(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                     version: int | None = None) -> dict | None:
    try:
        q = select(TpLoginRecipeRow).where(TpLoginRecipeRow.tenant_id == tenant_id,
                                         TpLoginRecipeRow.artifact_id == artifact_id)
        q = q.where(TpLoginRecipeRow.version == version) if version else \
            q.where(TpLoginRecipeRow.status == "active")
        row = (await session.execute(
            q.order_by(TpLoginRecipeRow.version.desc()).limit(1))).scalar_one_or_none()
    except Exception as exc:
        logger.debug("persona_store.get_recipe_skipped err=%s", exc)
        return None
    if row is None:
        return None
    return {"recipe_id": row.recipe_id, "version": row.version, "steps": row.steps,
            "slots": row.slots, "source": row.source, "status": row.status,
            "verified_at": _iso(row.verified_at), "verified_env": row.verified_env}


async def list_recipes(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> list[dict]:
    try:
        rows = (await session.execute(
            select(TpLoginRecipeRow).where(TpLoginRecipeRow.tenant_id == tenant_id,
                                         TpLoginRecipeRow.artifact_id == artifact_id)
            .order_by(TpLoginRecipeRow.version.desc()))).scalars().all()
    except Exception:
        return []
    return [{"recipe_id": r.recipe_id, "version": r.version, "status": r.status,
             "slots": r.slots, "step_count": len(r.steps or []),
             "verified_at": _iso(r.verified_at)} for r in rows]


async def stamp_recipe_verified(session: AsyncSession, *, tenant_id: str,
                                recipe_id: str, environment_id: str) -> None:
    await session.execute(update(TpLoginRecipeRow)
        .where(TpLoginRecipeRow.recipe_id == recipe_id, TpLoginRecipeRow.tenant_id == tenant_id)
        .values(verified_at=_utc_now(), verified_env=environment_id))


# ── Personas ─────────────────────────────────────────────────────────────────

async def save_persona(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                       name: str, description: str = "", traits: list | None = None,
                       behavior_class: str = "", app_id: str = "",
                       is_recording_baseline: bool = False,
                       persona_id: str | None = None) -> dict:
    pid = persona_id or _new_id()
    stmt = (pg_insert(TpPersonaRow).values(
        persona_id=pid, tenant_id=tenant_id, artifact_id=artifact_id, app_id=app_id,
        name=name, description=description, traits=list(traits or []),
        behavior_class=behavior_class, status="active",
        is_recording_baseline=is_recording_baseline, created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpPersonaRow.tenant_id, TpPersonaRow.artifact_id, TpPersonaRow.name],
            set_={"description": description, "traits": list(traits or []),
                  "behavior_class": behavior_class})
        # RETURN the row's ACTUAL persona_id: on a name conflict the stored row
        # keeps its original id, so the freshly minted pid would be a phantom.
        # Callers dispatch/scope by this id — it MUST be the persisted one.
        .returning(TpPersonaRow.persona_id))
    real_pid = (await session.execute(stmt)).scalar_one()
    return {"persona_id": real_pid, "name": name}


async def get_persona(session: AsyncSession, *, tenant_id: str, persona_id: str) -> dict | None:
    try:
        row = (await session.execute(select(TpPersonaRow).where(
            TpPersonaRow.persona_id == persona_id,
            TpPersonaRow.tenant_id == tenant_id))).scalar_one_or_none()
    except Exception:
        return None
    return _persona_dict(row) if row else None


def _persona_dict(r) -> dict:
    return {"persona_id": r.persona_id, "artifact_id": r.artifact_id, "name": r.name,
            "description": r.description, "traits": r.traits,
            "behavior_class": r.behavior_class, "status": r.status,
            "is_recording_baseline": r.is_recording_baseline}


async def list_personas(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                        traits: list | None = None,
                        include_retired: bool = False) -> list[dict]:
    try:
        q = select(TpPersonaRow).where(TpPersonaRow.tenant_id == tenant_id,
                                     TpPersonaRow.artifact_id == artifact_id)
        if not include_retired:
            q = q.where(TpPersonaRow.status == "active")
        rows = (await session.execute(q.order_by(TpPersonaRow.created_at))).scalars().all()
    except Exception:
        rows = []
    out = [_persona_dict(r) for r in rows]
    if traits:
        want = {t.strip().lower() for t in traits if t.strip()}
        out = [p for p in out if want.issubset({str(t).lower() for t in (p["traits"] or [])})]
    # Back-compat: surface today's form-login as persona-0 if no personas exist.
    if not any(p["is_recording_baseline"] for p in out):
        legacy = await _legacy_persona0(session, tenant_id=tenant_id, artifact_id=artifact_id)
        if legacy:
            out = [legacy] + out
    return out


async def retire_persona(session: AsyncSession, *, tenant_id: str, persona_id: str) -> None:
    await session.execute(update(TpPersonaRow)
        .where(TpPersonaRow.persona_id == persona_id, TpPersonaRow.tenant_id == tenant_id)
        .values(status="retired"))


async def _legacy_persona0(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> dict | None:
    """Represent an existing form-login auth profile as a read-only persona-0,
    so every current client gains a working 'default' persona with zero action."""
    from . import auth_profiles
    try:
        status = await auth_profiles.get_status(session, tenant_id=tenant_id, artifact_id=artifact_id)
    except Exception:
        return None
    if not status.get("present"):
        return None
    return {"persona_id": f"persona0::{artifact_id}", "artifact_id": artifact_id,
            "name": "default", "description": "Imported form-login / session (legacy).",
            "traits": [], "behavior_class": "", "status": "active",
            "is_recording_baseline": True, "legacy": True}


# ── Credential cards (envelope-encrypted) ────────────────────────────────────

def _card_aad(persona_id: str, environment_id: str) -> bytes:
    return f"personacred::{persona_id}::{environment_id}".encode("utf-8")


async def save_persona_credential(session: AsyncSession, *, envelope, tenant_id: str,
                                  persona_id: str, environment_id: str,
                                  slot_values: dict) -> dict:
    """Encrypt + persist a card. Raises if encryption unavailable (never
    plaintext). Caller commits. Returns non-secret slot names."""
    clean = {str(k): ("" if v is None else str(v)) for k, v in (slot_values or {}).items() if str(k)}
    if not clean:
        raise ValueError("a credential card needs at least one slot value")
    if envelope is None:
        raise RuntimeError("encryption unavailable — refusing to store credentials in plaintext")
    payload = json.dumps(clean).encode("utf-8")
    if len(payload) > MAX_CARD_BYTES:
        raise ValueError("credential card too large")
    blob = await envelope.encrypt(tenant_id, payload,
                                  aad=_card_aad(persona_id, environment_id))
    raw = blob.to_bytes()
    slot_names = sorted(clean.keys())
    stmt = (pg_insert(TpPersonaCredentialRow).values(
        persona_id=persona_id, environment_id=environment_id, tenant_id=tenant_id,
        blob=raw, slot_names=slot_names, verify_status="unverified", created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpPersonaCredentialRow.persona_id,
                            TpPersonaCredentialRow.environment_id,
                            TpPersonaCredentialRow.tenant_id],
            set_={"blob": raw, "slot_names": slot_names, "verify_status": "unverified",
                  "created_at": _utc_now()}))
    await session.execute(stmt)
    return {"persona_id": persona_id, "environment_id": environment_id, "slot_names": slot_names}


async def get_persona_credential(session: AsyncSession, *, envelope, tenant_id: str,
                                 persona_id: str, environment_id: str) -> dict | None:
    """Decrypt a card for a SERVER run. Never raises. Falls back to the legacy
    form-login card when the persona is the synthetic persona-0."""
    if persona_id.startswith("persona0::"):
        return await _legacy_card(session, envelope=envelope, tenant_id=tenant_id,
                                  artifact_id=persona_id.split("::", 1)[1])
    try:
        row = (await session.execute(select(TpPersonaCredentialRow).where(
            TpPersonaCredentialRow.persona_id == persona_id,
            TpPersonaCredentialRow.environment_id == environment_id,
            TpPersonaCredentialRow.tenant_id == tenant_id))).scalar_one_or_none()
    except Exception as exc:
        logger.debug("persona_store.card_skipped err=%s", exc)
        return None
    if row is None or envelope is None:
        return None
    try:
        blob = EnvelopeBlob.from_bytes(bytes(row.blob))
        plaintext = await envelope.decrypt(tenant_id, blob,
                                           expected_aad=_card_aad(persona_id, environment_id))
        return json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        logger.warning("persona_store.card_decrypt_failed persona=%s err=%s",
                       persona_id, str(exc)[:200])
        return None


async def _legacy_card(session: AsyncSession, *, envelope, tenant_id: str,
                       artifact_id: str) -> dict | None:
    """Persona-0's card IS the existing form-login profile — decrypted through
    the same envelope. Returns the slot dict (user/password) or None."""
    from . import auth_profiles
    cfg = await auth_profiles.get_form_login(session, envelope=envelope,
                                             tenant_id=tenant_id, artifact_id=artifact_id)
    if not cfg:
        return None
    # normalize the legacy form-login shape into slot values
    return {"user": cfg.get("user", ""), "password": cfg.get("password", ""),
            **{k: v for k, v in cfg.items() if k not in ("user", "password")}}


async def credential_status(session: AsyncSession, *, tenant_id: str,
                            persona_id: str, environment_id: str) -> dict:
    try:
        row = (await session.execute(select(TpPersonaCredentialRow).where(
            TpPersonaCredentialRow.persona_id == persona_id,
            TpPersonaCredentialRow.environment_id == environment_id,
            TpPersonaCredentialRow.tenant_id == tenant_id))).scalar_one_or_none()
    except Exception:
        return {"present": False}
    if row is None:
        return {"present": False}
    return {"present": True, "slot_names": row.slot_names,
            "verify_status": row.verify_status,
            "verified_epoch": getattr(row, "verified_epoch", "") or "",
            "last_verified_at": _iso(row.last_verified_at)}


async def stamp_card_verified(session: AsyncSession, *, tenant_id: str, persona_id: str,
                              environment_id: str, verified: bool, data_epoch: str = "") -> None:
    """Record a card verification outcome + the data epoch it was proven against
    (P5 staleness). A later run knows whether the card still holds for the
    environment's current data snapshot. Caller commits."""
    try:
        await session.execute(update(TpPersonaCredentialRow)
            .where(TpPersonaCredentialRow.persona_id == persona_id,
                   TpPersonaCredentialRow.environment_id == environment_id,
                   TpPersonaCredentialRow.tenant_id == tenant_id)
            .values(verify_status=("verified" if verified else "failed"),
                    last_verified_at=_utc_now(), verified_epoch=str(data_epoch or "")))
    except Exception as exc:
        logger.debug("persona_store.stamp_card_skipped err=%s", exc)


async def all_credential_status(session: AsyncSession, *, tenant_id: str,
                                artifact_id: str) -> list[dict]:
    """Every card's NON-SECRET status for an artifact (persona×env grid) — feeds
    the provisioning manifest and the staleness report. Ciphertext never leaves."""
    try:
        # personas for this artifact bound to their cards (any environment)
        prows = (await session.execute(select(TpPersonaRow.persona_id).where(
            TpPersonaRow.tenant_id == tenant_id,
            TpPersonaRow.artifact_id == artifact_id))).scalars().all()
        pids = set(prows)
        rows = (await session.execute(select(TpPersonaCredentialRow).where(
            TpPersonaCredentialRow.tenant_id == tenant_id))).scalars().all()
    except Exception:
        return []
    out = []
    for r in rows:
        if r.persona_id not in pids:
            continue
        out.append({"persona_id": r.persona_id, "environment_id": r.environment_id,
                    "slot_names": r.slot_names, "verify_status": r.verify_status,
                    "verified_epoch": getattr(r, "verified_epoch", "") or "",
                    "last_verified_at": _iso(r.last_verified_at), "present": True})
    return out


# ── Answer sheets ────────────────────────────────────────────────────────────

async def set_expected_value(session: AsyncSession, *, tenant_id: str, persona_id: str,
                             environment_id: str, value_key: str, expected_value: str,
                             source: str = "client_supplied") -> None:
    stmt = (pg_insert(TpPersonaExpectedValueRow).values(
        persona_id=persona_id, environment_id=environment_id, tenant_id=tenant_id,
        value_key=value_key, expected_value=str(expected_value), source=source,
        updated_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpPersonaExpectedValueRow.persona_id,
                            TpPersonaExpectedValueRow.environment_id,
                            TpPersonaExpectedValueRow.tenant_id,
                            TpPersonaExpectedValueRow.value_key],
            set_={"expected_value": str(expected_value), "source": source,
                  "updated_at": _utc_now()}))
    await session.execute(stmt)


async def get_expected_values(session: AsyncSession, *, tenant_id: str, persona_id: str,
                              environment_id: str) -> dict:
    try:
        rows = (await session.execute(select(TpPersonaExpectedValueRow).where(
            TpPersonaExpectedValueRow.persona_id == persona_id,
            TpPersonaExpectedValueRow.environment_id == environment_id,
            TpPersonaExpectedValueRow.tenant_id == tenant_id))).scalars().all()
    except Exception:
        return {}
    return {r.value_key: {"expected_value": r.expected_value, "source": r.source}
            for r in rows}


# ── Value classifications ────────────────────────────────────────────────────

async def save_classification(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                              value_key: str, class_: str, evidence: str,
                              scenario_id: str = "", step_number: int = 0,
                              detail: dict | None = None) -> None:
    stmt = (pg_insert(TpValueClassificationRow).values(
        artifact_id=artifact_id, tenant_id=tenant_id, value_key=value_key,
        scenario_id=scenario_id, step_number=int(step_number or 0),
        detail=dict(detail or {}), updated_at=_utc_now(),
        **{"class": class_, "evidence": evidence})
        .on_conflict_do_update(
            index_elements=[TpValueClassificationRow.artifact_id,
                            TpValueClassificationRow.tenant_id,
                            TpValueClassificationRow.value_key],
            set_={"class": class_, "evidence": evidence, "scenario_id": scenario_id,
                  "step_number": int(step_number or 0), "detail": dict(detail or {}),
                  "updated_at": _utc_now()}))
    await session.execute(stmt)


async def get_classifications(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> dict:
    try:
        rows = (await session.execute(select(TpValueClassificationRow).where(
            TpValueClassificationRow.artifact_id == artifact_id,
            TpValueClassificationRow.tenant_id == tenant_id))).scalars().all()
    except Exception:
        return {}
    return {r.value_key: {"class": r.class_, "evidence": r.evidence,
                          "scenario_id": r.scenario_id, "step_number": r.step_number}
            for r in rows}


# ── Reservations ─────────────────────────────────────────────────────────────

async def acquire_reservation(session: AsyncSession, *, tenant_id: str, persona_id: str,
                              environment_id: str, run_id: str, ttl_seconds: int) -> str | None:
    """Atomic acquire against the partial-unique live index. Returns the id, or
    None when the persona is already held. Expires stale holds first."""
    await expire_stale_reservations(session, tenant_id=tenant_id)
    rid = _new_id()
    stmt = (pg_insert(TpPersonaReservationRow).values(
        reservation_id=rid, persona_id=persona_id, environment_id=environment_id,
        tenant_id=tenant_id, run_id=run_id, acquired_at=_utc_now(),
        expires_at=_utc_now() + timedelta(seconds=max(60, int(ttl_seconds))))
        .on_conflict_do_nothing(index_elements=[
            TpPersonaReservationRow.persona_id, TpPersonaReservationRow.environment_id,
            TpPersonaReservationRow.tenant_id],
            index_where=TpPersonaReservationRow.released_at.is_(None)))
    res = await session.execute(stmt)
    return rid if (res.rowcount or 0) > 0 else None


async def release_reservation(session: AsyncSession, *, tenant_id: str,
                              run_id: str = "", reservation_id: str = "") -> int:
    cond = [TpPersonaReservationRow.tenant_id == tenant_id,
            TpPersonaReservationRow.released_at.is_(None)]
    if reservation_id:
        cond.append(TpPersonaReservationRow.reservation_id == reservation_id)
    elif run_id:
        cond.append(TpPersonaReservationRow.run_id == run_id)
    else:
        return 0
    res = await session.execute(update(TpPersonaReservationRow).where(and_(*cond))
                                .values(released_at=_utc_now()))
    return res.rowcount or 0


async def expire_stale_reservations(session: AsyncSession, *, tenant_id: str) -> int:
    res = await session.execute(update(TpPersonaReservationRow).where(
        TpPersonaReservationRow.tenant_id == tenant_id,
        TpPersonaReservationRow.released_at.is_(None),
        TpPersonaReservationRow.expires_at < _utc_now()).values(released_at=_utc_now()))
    return res.rowcount or 0


# ── Environments (P4 governance) ─────────────────────────────────────────────

def _env_dict(r) -> dict:
    return {"environment_id": r.environment_id, "artifact_id": r.artifact_id,
            "app_id": r.app_id, "label": r.label, "posture": r.posture,
            "is_production": r.is_production, "write_authorized": r.write_authorized,
            "base_url": r.base_url, "data_epoch": r.data_epoch,
            "health_status": r.health_status, "health_detail": r.health_detail,
            "last_health_at": _iso(r.last_health_at)}


async def save_environment(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                           environment_id: str, label: str = "", posture: str = "read_write",
                           is_production: bool = False, write_authorized: bool = False,
                           base_url: str = "", data_epoch: str = "", app_id: str = "") -> dict:
    """Upsert an environment's governance record. Caller commits."""
    from .persona_governance import normalize_posture
    posture = normalize_posture(posture)
    stmt = (pg_insert(TpEnvironmentRow).values(
        environment_id=environment_id, artifact_id=artifact_id, tenant_id=tenant_id,
        app_id=app_id, label=label, posture=posture, is_production=bool(is_production),
        write_authorized=bool(write_authorized), base_url=base_url,
        data_epoch=data_epoch, created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpEnvironmentRow.environment_id, TpEnvironmentRow.artifact_id,
                            TpEnvironmentRow.tenant_id],
            set_={"label": label, "posture": posture, "is_production": bool(is_production),
                  "write_authorized": bool(write_authorized), "base_url": base_url,
                  "data_epoch": data_epoch}))
    await session.execute(stmt)
    return {"environment_id": environment_id, "posture": posture,
            "is_production": bool(is_production), "write_authorized": bool(write_authorized)}


async def get_environment(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                          environment_id: str) -> dict | None:
    """The governance record for one environment, or None (unregistered ⇒ the
    caller treats it as a default read_write non-production target)."""
    try:
        row = (await session.execute(select(TpEnvironmentRow).where(
            TpEnvironmentRow.environment_id == environment_id,
            TpEnvironmentRow.artifact_id == artifact_id,
            TpEnvironmentRow.tenant_id == tenant_id))).scalar_one_or_none()
    except Exception as exc:
        logger.debug("persona_store.get_environment_skipped err=%s", exc)
        return None
    return _env_dict(row) if row else None


async def list_environments(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> list[dict]:
    try:
        rows = (await session.execute(select(TpEnvironmentRow).where(
            TpEnvironmentRow.tenant_id == tenant_id,
            TpEnvironmentRow.artifact_id == artifact_id)
            .order_by(TpEnvironmentRow.created_at))).scalars().all()
    except Exception:
        return []
    return [_env_dict(r) for r in rows]


async def stamp_env_health(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                           environment_id: str, health_status: str, health_detail: str = "") -> None:
    """Record the outcome of a health probe (NOT a re-crawl). Upserts a shell row
    when the environment was never registered, so a probe result is never lost."""
    stmt = (pg_insert(TpEnvironmentRow).values(
        environment_id=environment_id, artifact_id=artifact_id, tenant_id=tenant_id,
        health_status=health_status, health_detail=(health_detail or "")[:2000],
        last_health_at=_utc_now(), created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpEnvironmentRow.environment_id, TpEnvironmentRow.artifact_id,
                            TpEnvironmentRow.tenant_id],
            set_={"health_status": health_status,
                  "health_detail": (health_detail or "")[:2000],
                  "last_health_at": _utc_now()}))
    await session.execute(stmt)


# ── Case scopes (behavior-class binding) ─────────────────────────────────────

async def bind_case_scope(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                          scenario_id: str, required_behavior_class: str,
                          evidence: str = "structure_fork", detail: dict | None = None) -> None:
    """Bind a case to the behavior class a structure fork proved it belongs to.
    An out-of-class persona is later refused at dispatch (test_scope). Caller
    commits."""
    stmt = (pg_insert(TpCaseScopeRow).values(
        artifact_id=artifact_id, tenant_id=tenant_id, scenario_id=scenario_id,
        required_behavior_class=required_behavior_class, evidence=evidence,
        detail=dict(detail or {}), updated_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpCaseScopeRow.artifact_id, TpCaseScopeRow.tenant_id,
                            TpCaseScopeRow.scenario_id],
            set_={"required_behavior_class": required_behavior_class,
                  "evidence": evidence, "detail": dict(detail or {}),
                  "updated_at": _utc_now()}))
    await session.execute(stmt)


async def get_case_scopes(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> dict[str, str]:
    """scenario_id → required behavior class ('' = applies to any persona)."""
    try:
        rows = (await session.execute(select(TpCaseScopeRow).where(
            TpCaseScopeRow.artifact_id == artifact_id,
            TpCaseScopeRow.tenant_id == tenant_id))).scalars().all()
    except Exception:
        return {}
    return {r.scenario_id: r.required_behavior_class for r in rows}


# ── Bundle: recipe + card → (auth_config, login_env) ─────────────────────────

def build_persona_bundle(recipe: dict | None, slot_values: dict | None) -> tuple[dict | None, dict]:
    """Generalization of ``auth_profiles.build_form_login_bundle``.

    Emits a ``strategy:"recipe"`` config (non-secret: steps + slot metadata) and
    the run env carrying each secret under ``NEXUS_LOGIN_<SLOT>``. Secrets ride
    the env, never the bundle. Returns (None, {}) when there is no recipe."""
    if not recipe or not (recipe.get("steps")):
        return None, {}
    slots = recipe.get("slots") or []
    login_env: dict = {}
    out_slots = []
    for sl in slots:
        name = str(sl.get("name") or "")
        if not name:
            continue
        env_key = f"NEXUS_LOGIN_{name.upper()}"
        out_slots.append({"name": name, "type": sl.get("type") or "secret",
                          "env": env_key})
        if slot_values and name in slot_values:
            login_env[env_key] = str(slot_values[name])
    auth_config = {
        "strategy": "recipe",
        "loginPath": recipe.get("login_path") or "/",
        "steps": recipe.get("steps"),
        "slots": out_slots,
    }
    return auth_config, login_env


__all__ = [
    "TpLoginRecipeRow", "TpPersonaRow", "TpPersonaCredentialRow", "TpPersonaExpectedValueRow",
    "TpValueClassificationRow", "TpPersonaReservationRow", "TpEnvironmentRow", "TpCaseScopeRow",
    "save_recipe", "get_recipe", "list_recipes", "stamp_recipe_verified",
    "save_persona", "get_persona", "list_personas", "retire_persona",
    "save_persona_credential", "get_persona_credential", "credential_status",
    "stamp_card_verified", "all_credential_status",
    "set_expected_value", "get_expected_values",
    "save_classification", "get_classifications",
    "acquire_reservation", "release_reservation", "expire_stale_reservations",
    "save_environment", "get_environment", "list_environments", "stamp_env_health",
    "bind_case_scope", "get_case_scopes",
    "build_persona_bundle",
]
