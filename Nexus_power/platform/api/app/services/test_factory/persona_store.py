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
    Boolean, DateTime, Integer, LargeBinary, String, Text, and_, func, select, update,
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
    # Reuse-matching key (Phase 5): domain + login-page + login-FORM fingerprint,
    # computed by the caller from the observed form (login_fingerprint.login_type_key)
    # so the fleet can propose an existing recipe instead of re-recording. Empty when
    # the recipe predates the key or the form was not fingerprinted.
    login_type_key: Mapped[str] = mapped_column(String(64), default="")
    # registrable domain the recipe was recorded against. The key above is a
    # hash and cannot be reversed, so this is what lets a NEW app on a known
    # host be offered an existing recipe before any form has been seen.
    login_domain: Mapped[str] = mapped_column(String(253), default="")
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
    # WHICH login this card was provisioned against. A re-record that renames a slot
    # otherwise leaves every card looking healthy while none can authenticate.
    login_type_key: Mapped[str] = mapped_column(String(64), default="")
    recipe_version: Mapped[int] = mapped_column(Integer, default=0)
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
    # Routing the run must APPLY (F6). environment_routing copies these into the run
    # context, and the row previously had nowhere to hold them — so a cookie-selected
    # lane on a shared host silently landed on the host's default. Non-secret only:
    # the onboarding profile's sealed credentials never leave qe-central.
    cookies: Mapped[list] = mapped_column(JSONB, default=list)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    env_assertion: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Which registry this row came from, and the onboarding profile it mirrors — so
    # the panel can say plainly what it is showing instead of implying one list.
    source: Mapped[str] = mapped_column(String(24), default="studio")
    app_env_id: Mapped[str] = mapped_column(String(64), default="")
    health_status: Mapped[str] = mapped_column(String(16), default="unknown")
    health_detail: Mapped[str] = mapped_column(Text, default="")
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    require_scoped_certification: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class TpCertificationLedgerRow(Base):
    __tablename__ = "tp_certification_ledger"
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    behavior_class: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    status: Mapped[str] = mapped_column(String(16), default="certified")
    run_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


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
                      source: str = "crawl_demonstration",
                      login_type_key: str = "", login_domain: str = "") -> dict:
    """Persist a NEW recipe version (monotonic per artifact); supersede prior
    active ones. ``login_type_key`` (from login_fingerprint) keys the recipe for
    fleet reuse. Caller commits. Returns the stored recipe (non-secret)."""
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
        source=source, status="active", login_type_key=str(login_type_key or ""),
        login_domain=str(login_domain or "").strip().lower(),
        created_at=_utc_now()))
    await session.flush()
    return {"recipe_id": rid, "version": next_version, "slots": list(slots or []),
            "step_count": len(steps or []), "login_type_key": str(login_type_key or "")}


async def withdraw_card_proofs(session: AsyncSession, *, tenant_id: str,
                               artifact_id: str, required_slots: list) -> dict:
    """A new login recipe was recorded — withdraw every proof it invalidated.

    A card's ``verified`` badge is a claim about a SPECIFIC login. The moment a new
    version supersedes the old one, that claim is about a login that is no longer
    active, so it is retracted here rather than left standing until someone notices.

    Cards are also classified against the new slot set so the operator learns
    immediately which members stopped working, instead of discovering it when a run
    is blocked:

      * ``broken``  — the card no longer covers the login. Every run for this member
        is refused until it is re-provisioned. (The dispatch gate re-derives this
        independently; this is the visible half, not the enforcing one.)
      * ``unproven`` — the card still fits, but its proof was about the old version.

    Returns ``{broken, unproven, checked}`` — persona ids, no secrets. Caller commits.
    """
    req = [str(s) for s in (required_slots or []) if str(s)]
    pids = await _artifact_persona_ids(session, tenant_id=tenant_id, artifact_id=artifact_id)
    if not pids:
        return {"broken": [], "unproven": [], "checked": 0}
    try:
        rows = (await session.execute(select(TpPersonaCredentialRow).where(
            TpPersonaCredentialRow.tenant_id == tenant_id))).scalars().all()
    except Exception as exc:
        logger.warning("persona_store.withdraw_proofs_failed err=%s", str(exc)[:200])
        return {"broken": [], "unproven": [], "checked": 0}
    broken, unproven, checked = [], [], 0
    for r in rows:
        if r.persona_id not in pids:
            continue
        checked += 1
        held = set(str(s) for s in (r.slot_names or []))
        covers = bool(req) and not (set(req) - held) and not (held - set(req))
        # The proof is withdrawn either way: it was made against a login that is no
        # longer the active one, and that is true whether or not the slots still fit.
        if r.verify_status == "verified":
            r.verify_status = "unverified"
        (broken if not covers else unproven).append(r.persona_id)
    if broken:
        logger.warning(
            "persona_store.cards_broken_by_recipe artifact=%s count=%d slots=%s",
            artifact_id, len(broken), ",".join(req))
    return {"broken": broken, "unproven": unproven, "checked": checked}


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
            "login_type_key": row.login_type_key, "login_domain": row.login_domain,
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
             "source": r.source, "login_type_key": r.login_type_key,
             "verified_at": _iso(r.verified_at)} for r in rows]


async def stamp_recipe_verified(session: AsyncSession, *, tenant_id: str,
                                recipe_id: str, environment_id: str) -> None:
    await session.execute(update(TpLoginRecipeRow)
        .where(TpLoginRecipeRow.recipe_id == recipe_id, TpLoginRecipeRow.tenant_id == tenant_id)
        .values(verified_at=_utc_now(), verified_env=environment_id))


async def find_recipes_by_login_type(session: AsyncSession, *, tenant_id: str,
                                     login_type_key: str) -> list[dict]:
    """Active recipes across the TENANT that share a login type — the fleet-wide
    reuse lookup (Phase 5). An empty key returns nothing (an unfingerprinted recipe
    is never a reuse candidate). RLS + the explicit tenant filter keep it scoped."""
    key = str(login_type_key or "").strip()
    if not key:
        return []
    try:
        rows = (await session.execute(
            select(TpLoginRecipeRow).where(
                TpLoginRecipeRow.tenant_id == tenant_id,
                TpLoginRecipeRow.login_type_key == key,
                TpLoginRecipeRow.status == "active")
            .order_by(TpLoginRecipeRow.created_at.desc()))).scalars().all()
    except Exception as exc:
        logger.debug("persona_store.find_by_login_type_skipped err=%s", exc)
        return []
    return [{"recipe_id": r.recipe_id, "artifact_id": r.artifact_id,
             "app_id": r.app_id, "version": r.version, "slots": r.slots,
             "login_type_key": r.login_type_key,
             "verified_at": _iso(r.verified_at)} for r in rows]


def _recipe_from_form_login(cfg: dict) -> tuple[list, list]:
    """Derive a v1 login RECIPE (steps + slots) from a stored form-login profile.

    A form-login already IS the login choreography — the login path, the labelled
    fields, and the submit control. Turning it into a recipe means a named persona
    fills the SAME choreography with its own card instead of anyone hand-authoring
    it. Slot names come from each field's ``value`` (``user``/``password``), so a
    persona card keyed by those slots drops straight in. Emits steps in the exact
    shape the compiler's recipe interpreter replays (goto/fill/click/wait)."""
    login_path = str(cfg.get("login_path") or "/")
    fields = cfg.get("fields") or [{"label": "Email", "value": "user"},
                                   {"label": "Password", "value": "password"}]
    submit = str(cfg.get("submit_label") or "Sign in")
    steps: list = [{"action": "goto", "path": login_path}]
    slots: list = []
    seen: set = set()
    for f in fields:
        slot = str((f or {}).get("value") or "").strip() or "user"
        label = str((f or {}).get("label") or slot).strip()
        steps.append({"action": "fill", "slot": slot, "label": label})
        if slot not in seen:
            slots.append({"name": slot, "type": "secret"})
            seen.add(slot)
    steps.append({"action": "click", "name": submit})
    steps.append({"action": "wait", "state": "networkidle"})
    return steps, slots


def _assert_home_step(home: dict | None) -> dict | None:
    """Build the terminal ``assert_home`` step — the Home-reached ORACLE — from a
    landing signal observed after login. A signal is any of ``url_pattern`` /
    ``selector`` / ``expect_text`` (+ optional ``timeout`` ms). Returns ``None`` when
    no usable signal is present, so the recipe falls back to the unchanged
    steps-completed success (never a bare, always-passing assert)."""
    if not home:
        return None
    step: dict = {"action": "assert_home"}
    has_signal = False
    for key in ("url_pattern", "selector", "expect_text"):
        val = str((home or {}).get(key) or "").strip()
        if val:
            step[key] = val
            has_signal = True
    # refuse a signal-less assert_home — an oracle that asserts nothing is a lie.
    # A timeout alone is NOT a signal (it would just wait, then pass vacuously).
    if not has_signal:
        return None
    if home.get("timeout"):
        try:
            step["timeout"] = int(home["timeout"])
        except (TypeError, ValueError):
            pass
    return step


def _verify_document_steps(recorded: list | None) -> list:
    """Mark recorded verify-documents interstitial steps OPTIONAL, so a member who
    hits the document handles it and a member who does not skips it — both converge
    at Home. Only actionable ``fill``/``click`` steps become optional; ``goto``/
    ``wait`` stay unconditional. Unknown/missing actions pass through untouched."""
    out: list = []
    for raw in (recorded or []):
        step = dict(raw or {})
        if step.get("action") in ("fill", "click"):
            step["optional"] = True
        out.append(step)
    return out


def build_login_recipe(cfg: dict, *, verify_documents: list | None = None,
                       home: dict | None = None) -> tuple[list, list]:
    """Assemble a full login recipe in the exact shape the compiler's interpreter
    replays, following the canonical login sequence:

        Member (form-login)  ->  [Verify-documents, OPTIONAL]  ->  [assert_home ORACLE]

    ``cfg`` is the form-login choreography (see ``_recipe_from_form_login``);
    ``verify_documents`` are recorded interstitial steps (marked optional here);
    ``home`` is the post-login landing signal for the Home-reached oracle.

    Backward-compatible: with neither ``verify_documents`` nor ``home`` this returns
    exactly ``_recipe_from_form_login(cfg)``."""
    steps, slots = _recipe_from_form_login(cfg)
    vdoc = _verify_document_steps(verify_documents)
    if vdoc:
        steps = steps + vdoc
    home_step = _assert_home_step(home)
    if home_step:
        steps = steps + [home_step]
    return steps, slots


async def ensure_baseline_from_form_login(session: AsyncSession, *, tenant_id: str,
                                          artifact_id: str, form_login_cfg: dict | None
                                          ) -> dict:
    """Materialize a baseline login recipe from a configured form-login, so a
    crawl/auth-setup YIELDS a recipe on file — no hand-authoring. Idempotent: a
    no-op when a recipe already exists; honest skip when there is no form-login.
    Caller commits.

    The synthetic persona-0 (the recording baseline) keeps working via the legacy
    form path; this recipe is the shared choreography a NAMED persona fills with
    its own credential card."""
    try:
        existing = await get_recipe(session, tenant_id=tenant_id, artifact_id=artifact_id)
    except Exception:
        existing = None
    if existing:
        return {"materialized": False, "reason": "recipe_already_exists",
                "recipe_id": existing.get("recipe_id")}
    cfg = form_login_cfg or {}
    if not (cfg.get("user") and cfg.get("password")):
        return {"materialized": False, "reason": "no_form_login_to_derive_from"}
    steps, slots = _recipe_from_form_login(cfg)
    out = await save_recipe(session, tenant_id=tenant_id, artifact_id=artifact_id,
                            steps=steps, slots=slots, source="derived_from_form_login")
    return {"materialized": True, "recipe_id": out["recipe_id"], "version": out["version"],
            "step_count": len(steps), "slots": [s["name"] for s in slots]}


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
                                  slot_values: dict, login_type_key: str = "",
                                  recipe_version: int = 0) -> dict:
    """Encrypt + persist a card. Raises if encryption unavailable (never
    plaintext). Caller commits. Returns non-secret slot names.

    ``login_type_key``/``recipe_version`` record WHICH login the card was checked
    against, so a later re-record that renames a slot is a detectable break rather
    than a card that silently stops authenticating."""
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
    key = str(login_type_key or "")
    version = int(recipe_version or 0)
    stmt = (pg_insert(TpPersonaCredentialRow).values(
        persona_id=persona_id, environment_id=environment_id, tenant_id=tenant_id,
        blob=raw, slot_names=slot_names, verify_status="unverified",
        login_type_key=key, recipe_version=version, created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpPersonaCredentialRow.persona_id,
                            TpPersonaCredentialRow.environment_id,
                            TpPersonaCredentialRow.tenant_id],
            # verify_status resets on every rewrite: a card whose secret just changed
            # has not been proven, and carrying the old 'verified' forward would make
            # the badge a claim about a value that is no longer stored (F3).
            set_={"blob": raw, "slot_names": slot_names, "verify_status": "unverified",
                  "login_type_key": key, "recipe_version": version,
                  "created_at": _utc_now()}))
    await session.execute(stmt)
    return {"persona_id": persona_id, "environment_id": environment_id,
            "slot_names": slot_names, "login_type_key": key, "recipe_version": version}


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
            "login_type_key": getattr(row, "login_type_key", "") or "",
            "recipe_version": int(getattr(row, "recipe_version", 0) or 0),
            "last_verified_at": _iso(row.last_verified_at)}


async def stamp_card_verified(session: AsyncSession, *, tenant_id: str, persona_id: str,
                              environment_id: str, verified: bool, data_epoch: str = "",
                              recipe_version: int | None = None) -> None:
    """Record a card verification outcome, the data epoch it was proven against
    (P5 staleness), and WHICH login version it proved.

    ``recipe_version`` is what makes a proof re-attachable. Without it the column
    only ever held the version the card was PROVISIONED against, so after any
    re-record a card was permanently ``proof_superseded``: proving it again changed
    nothing, and the state it needed to leave was the state it could never leave.
    Every pre-contract and bulk-imported card (version 0) was stuck the same way.
    Pass it whenever the caller knows which recipe the login actually replayed.

    Caller commits."""
    values = {"verify_status": ("verified" if verified else "failed"),
              "last_verified_at": _utc_now(), "verified_epoch": str(data_epoch or "")}
    # Only on a PROOF: a failed attempt says nothing about which login the card
    # belongs to, and re-pointing it at the active version on failure would quietly
    # adopt a recipe it was never checked against.
    if verified and recipe_version is not None:
        values["recipe_version"] = int(recipe_version or 0)
    try:
        await session.execute(update(TpPersonaCredentialRow)
            .where(TpPersonaCredentialRow.persona_id == persona_id,
                   TpPersonaCredentialRow.environment_id == environment_id,
                   TpPersonaCredentialRow.tenant_id == tenant_id)
            .values(**values))
    except Exception as exc:
        # WARNING, not debug: the platform suppresses INFO, and a silently skipped
        # stamp means the next run re-reads a proof state that no longer reflects
        # what just happened.
        logger.warning("persona_store.stamp_card_failed persona=%s err=%s",
                       persona_id, str(exc)[:200])


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
                    "login_type_key": getattr(r, "login_type_key", "") or "",
                    "recipe_version": int(getattr(r, "recipe_version", 0) or 0),
                    "last_verified_at": _iso(r.last_verified_at), "present": True})
    return out


async def _artifact_persona_ids(session: AsyncSession, *, tenant_id: str,
                                artifact_id: str) -> set[str]:
    rows = (await session.execute(select(TpPersonaRow.persona_id).where(
        TpPersonaRow.tenant_id == tenant_id,
        TpPersonaRow.artifact_id == artifact_id))).scalars().all()
    return set(rows)


async def rotate_cards(session: AsyncSession, *, envelope, tenant_id: str,
                       artifact_id: str) -> dict:
    """Secret rotation (P5 ops): re-encrypt every card of an artifact's personas
    so the CURRENT envelope key wraps them. Decrypt-then-encrypt in place; the
    plaintext is never returned or logged. Caller commits. Idempotent (re-wrapping
    identical plaintext is safe)."""
    if envelope is None:
        raise RuntimeError("encryption unavailable — refusing to rotate credentials")
    pids = await _artifact_persona_ids(session, tenant_id=tenant_id, artifact_id=artifact_id)
    rotated = 0
    failed = 0
    rows = (await session.execute(select(TpPersonaCredentialRow).where(
        TpPersonaCredentialRow.tenant_id == tenant_id))).scalars().all()
    for r in rows:
        if r.persona_id not in pids:
            continue
        aad = _card_aad(r.persona_id, r.environment_id)
        try:
            blob = EnvelopeBlob.from_bytes(bytes(r.blob))
            plaintext = await envelope.decrypt(tenant_id, blob, expected_aad=aad)
            fresh = await envelope.encrypt(tenant_id, plaintext, aad=aad)
            r.blob = fresh.to_bytes()
            rotated += 1
        except Exception as exc:
            failed += 1
            logger.warning("persona_store.rotate_card_failed persona=%s env=%s err=%s",
                           r.persona_id, r.environment_id, str(exc)[:200])
    return {"rotated": rotated, "failed": failed}


async def flag_stale_cards(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                           environment_id: str, current_epoch: str) -> dict:
    """Staleness sweep (P5 ops): when an environment's data epoch rolls, mark every
    card that was verified against an OLDER epoch back to ``unverified`` so the next
    run must re-prove it before it is trusted. The honest 'env refresh ⇒ cards
    stale' half of the scheduler; re-validation itself happens at the next run's
    preflight (which re-stamps the card). Caller commits."""
    epoch = str(current_epoch or "")
    pids = await _artifact_persona_ids(session, tenant_id=tenant_id, artifact_id=artifact_id)
    rows = (await session.execute(select(TpPersonaCredentialRow).where(
        TpPersonaCredentialRow.tenant_id == tenant_id,
        TpPersonaCredentialRow.environment_id == environment_id))).scalars().all()
    flagged = []
    for r in rows:
        if r.persona_id not in pids:
            continue
        prev = getattr(r, "verified_epoch", "") or ""
        # No `and prev` guard: a blank verified_epoch means the card was proven while
        # the environment had no epoch label, so nothing ties that proof to the
        # snapshot running now. Exempting blanks left every pre-P5 card permanently
        # un-flaggable — and disagreed with the gate that actually stops runs.
        if r.verify_status == "verified" and epoch and prev != epoch:
            r.verify_status = "unverified"
            flagged.append(r.persona_id)
    return {"flagged": flagged, "flagged_count": len(flagged),
            "environment_id": environment_id, "current_epoch": epoch}


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


# ── Cardinality (per-persona repeat counts) ──────────────────────────────────
# A repeated block (beneficiaries, dependents) runs as many times as THIS
# persona's data demands. Stored on the answer sheet under a reserved value_key
# so no migration is needed; consumed by the run dispatch (NEXUS_REPETITION) and
# surfaced in the report.

_CARD_PREFIX = "__cardinality__::"


async def set_persona_cardinality(session: AsyncSession, *, tenant_id: str, persona_id: str,
                                  environment_id: str, counts: dict) -> dict:
    """Store {block: count} for a persona/environment. Caller commits."""
    clean: dict[str, int] = {}
    for block, n in (counts or {}).items():
        b = str(block).strip()
        try:
            c = int(n)
        except (TypeError, ValueError):
            continue
        if b and c > 0:
            clean[b] = c
            await set_expected_value(
                session, tenant_id=tenant_id, persona_id=persona_id,
                environment_id=environment_id, value_key=f"{_CARD_PREFIX}{b}",
                expected_value=str(c), source="cardinality")
    return clean


async def get_persona_cardinality(session: AsyncSession, *, tenant_id: str, persona_id: str,
                                  environment_id: str) -> dict:
    """{block: count} for a persona/environment (empty when none set)."""
    vals = await get_expected_values(
        session, tenant_id=tenant_id, persona_id=persona_id, environment_id=environment_id)
    out: dict[str, int] = {}
    for key, v in vals.items():
        if key.startswith(_CARD_PREFIX):
            try:
                out[key[len(_CARD_PREFIX):]] = int(v.get("expected_value") or 0)
            except (TypeError, ValueError):
                continue
    return {b: c for b, c in out.items() if c > 0}


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


async def live_runs_for_persona(session: AsyncSession, *, tenant_id: str,
                                persona_id: str) -> list[str]:
    """Run ids currently holding this member. Used when a member is retired
    mid-flight: those runs are authenticating as an account that has just been
    decommissioned, and finishing quietly would file evidence under it."""
    try:
        rows = (await session.execute(select(TpPersonaReservationRow).where(
            TpPersonaReservationRow.tenant_id == tenant_id,
            TpPersonaReservationRow.persona_id == persona_id,
            TpPersonaReservationRow.released_at.is_(None)))).scalars().all()
    except Exception as exc:
        logger.warning("persona_store.live_runs_failed persona=%s err=%s",
                       persona_id, str(exc)[:200])
        return []
    return [r.run_id for r in rows if r.run_id]


async def release_persona_reservations(session: AsyncSession, *, tenant_id: str,
                                       persona_id: str) -> int:
    """Release every live hold on a member. Without this, retiring a member leaves
    its reservation held until the TTL expires — the member is gone from the picker
    and still counts against the concurrency cap. Caller commits."""
    res = await session.execute(update(TpPersonaReservationRow).where(
        TpPersonaReservationRow.tenant_id == tenant_id,
        TpPersonaReservationRow.persona_id == persona_id,
        TpPersonaReservationRow.released_at.is_(None)).values(released_at=_utc_now()))
    return res.rowcount or 0


async def expire_stale_reservations(session: AsyncSession, *, tenant_id: str) -> int:
    res = await session.execute(update(TpPersonaReservationRow).where(
        TpPersonaReservationRow.tenant_id == tenant_id,
        TpPersonaReservationRow.released_at.is_(None),
        TpPersonaReservationRow.expires_at < _utc_now()).values(released_at=_utc_now()))
    return res.rowcount or 0


async def count_live_reservations(session: AsyncSession, *, tenant_id: str) -> int:
    """Live (unreleased) reservations for a tenant, after expiring stale holds —
    the P5 per-tenant concurrency cap reads this at dispatch."""
    await expire_stale_reservations(session, tenant_id=tenant_id)
    try:
        n = (await session.execute(select(func.count()).select_from(TpPersonaReservationRow).where(
            TpPersonaReservationRow.tenant_id == tenant_id,
            TpPersonaReservationRow.released_at.is_(None)))).scalar_one()
    except Exception:
        return 0
    return int(n or 0)


# ── Environments (P4 governance) ─────────────────────────────────────────────

def _env_dict(r) -> dict:
    return {"environment_id": r.environment_id, "artifact_id": r.artifact_id,
            "app_id": r.app_id, "label": r.label, "posture": r.posture,
            "is_production": r.is_production, "write_authorized": r.write_authorized,
            "base_url": r.base_url, "data_epoch": r.data_epoch,
            "health_status": r.health_status, "health_detail": r.health_detail,
            "require_scoped_certification": getattr(r, "require_scoped_certification", False),
            # F6 routing — what environment_routing carries into the run.
            "cookies": list(getattr(r, "cookies", None) or []),
            "headers": dict(getattr(r, "headers", None) or {}),
            "env_assertion": dict(getattr(r, "env_assertion", None) or {}),
            "source": getattr(r, "source", "") or "studio",
            "app_env_id": getattr(r, "app_env_id", "") or "",
            "last_health_at": _iso(r.last_health_at)}


async def save_environment(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                           environment_id: str, label: str = "", posture: str = "read_write",
                           is_production: bool = False, write_authorized: bool = False,
                           base_url: str = "", data_epoch: str = "", app_id: str = "",
                           require_scoped_certification: bool = False,
                           cookies: list | None = None, headers: dict | None = None,
                           env_assertion: dict | None = None,
                           source: str = "", app_env_id: str = "") -> dict:
    """Upsert an environment's governance record + the routing a run must apply.

    ``cookies``/``headers``/``env_assertion`` are what ``environment_routing`` carries
    into the run context; they are NON-SECRET routing only. When this row mirrors an
    onboarding Environment Profile, ``source='onboarding'`` and ``app_env_id`` links
    back to it — the profile remains the owner and its sealed credentials never leave
    qe-central. Omitted routing keeps whatever is stored, so a governance-only edit
    from Studio cannot silently blank a lane selector pushed by onboarding.

    Caller commits."""
    from .persona_governance import normalize_posture
    posture = normalize_posture(posture)
    values = dict(
        environment_id=environment_id, artifact_id=artifact_id, tenant_id=tenant_id,
        app_id=app_id, label=label, posture=posture, is_production=bool(is_production),
        write_authorized=bool(write_authorized), base_url=base_url,
        data_epoch=data_epoch,
        require_scoped_certification=bool(require_scoped_certification),
        cookies=list(cookies or []), headers=dict(headers or {}),
        env_assertion=dict(env_assertion or {}),
        source=str(source or "studio"), app_env_id=str(app_env_id or ""),
        created_at=_utc_now())
    updates = {"label": label, "posture": posture, "is_production": bool(is_production),
               "write_authorized": bool(write_authorized), "base_url": base_url,
               "data_epoch": data_epoch,
               "require_scoped_certification": bool(require_scoped_certification)}
    # Only overwrite routing that was actually supplied.
    if cookies is not None:
        updates["cookies"] = list(cookies)
    if headers is not None:
        updates["headers"] = dict(headers)
    if env_assertion is not None:
        updates["env_assertion"] = dict(env_assertion)
    if source:
        updates["source"] = str(source)
    if app_env_id:
        updates["app_env_id"] = str(app_env_id)
    stmt = (pg_insert(TpEnvironmentRow).values(**values)
            .on_conflict_do_update(
                index_elements=[TpEnvironmentRow.environment_id, TpEnvironmentRow.artifact_id,
                                TpEnvironmentRow.tenant_id],
                set_=updates))
    await session.execute(stmt)
    return {"environment_id": environment_id, "posture": posture,
            "is_production": bool(is_production), "write_authorized": bool(write_authorized),
            "require_scoped_certification": bool(require_scoped_certification),
            "source": str(source or "studio"), "app_env_id": str(app_env_id or "")}


# ── Scoped certification ledger (R3) ─────────────────────────────────────────

async def record_certification(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                               scenario_id: str, environment_id: str, behavior_class: str,
                               status: str = "certified", run_id: str = "",
                               detail: dict | None = None) -> None:
    """Record a certification outcome for one (scenario, environment, behavior_class)
    cell. 'cert on env A does not grant trust on env B' — each cell is proven on its
    own. Caller commits."""
    stmt = (pg_insert(TpCertificationLedgerRow).values(
        artifact_id=artifact_id, tenant_id=tenant_id, scenario_id=scenario_id,
        environment_id=environment_id, behavior_class=(behavior_class or ""),
        status=status, run_id=run_id, detail=dict(detail or {}), updated_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[TpCertificationLedgerRow.artifact_id, TpCertificationLedgerRow.tenant_id,
                            TpCertificationLedgerRow.scenario_id, TpCertificationLedgerRow.environment_id,
                            TpCertificationLedgerRow.behavior_class],
            set_={"status": status, "run_id": run_id, "detail": dict(detail or {}),
                  "updated_at": _utc_now()}))
    await session.execute(stmt)


async def get_certification_ledger(session: AsyncSession, *, tenant_id: str,
                                   artifact_id: str) -> list[dict]:
    try:
        rows = (await session.execute(select(TpCertificationLedgerRow).where(
            TpCertificationLedgerRow.tenant_id == tenant_id,
            TpCertificationLedgerRow.artifact_id == artifact_id))).scalars().all()
    except Exception:
        return []
    return [{"scenario_id": r.scenario_id, "environment_id": r.environment_id,
             "behavior_class": r.behavior_class, "status": r.status,
             "run_id": r.run_id, "at": _iso(r.updated_at)} for r in rows]


def certification_matrix(ledger: list[dict]) -> dict:
    """Build the (environment × behavior_class) proof grid from the ledger:
    {environment: {behavior_class: {certified, failed, scenarios}}}. The honest
    answer to 'how much is proven for THIS env and THIS member class' — a cell with
    no rows is simply not certified there (never assumed from another cell)."""
    grid: dict = {}
    for row in (ledger or []):
        env = row.get("environment_id") or ""
        cls = row.get("behavior_class") or "(any)"
        cell = grid.setdefault(env, {}).setdefault(cls, {"certified": 0, "failed": 0, "scenarios": 0})
        cell["scenarios"] += 1
        if row.get("status") == "certified":
            cell["certified"] += 1
        else:
            cell["failed"] += 1
    return grid


async def cell_certified_scenarios(session: AsyncSession, *, tenant_id: str, artifact_id: str,
                                   environment_id: str, behavior_class: str) -> set[str]:
    """Scenario ids CERTIFIED for a specific (environment, behavior_class) cell."""
    try:
        rows = (await session.execute(select(TpCertificationLedgerRow.scenario_id).where(
            TpCertificationLedgerRow.tenant_id == tenant_id,
            TpCertificationLedgerRow.artifact_id == artifact_id,
            TpCertificationLedgerRow.environment_id == environment_id,
            TpCertificationLedgerRow.behavior_class == (behavior_class or ""),
            TpCertificationLedgerRow.status == "certified"))).scalars().all()
    except Exception:
        return set()
    return set(rows)


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
    the env, never the bundle. Returns (None, {}) when there is no recipe.

    The emitted slot list is every slot the recipe's STEPS fill, union the declared
    list — not the declaration alone. The interpreter's missing-credential guard
    only inspects the slots this config declares, so a step that fills a slot absent
    from the declaration used to produce no env key AND no warning: the field was
    filled with the empty string and the login failed as though the application were
    broken. Declaring it means the guard sees it."""
    if not recipe or not (recipe.get("steps")):
        return None, {}
    from . import card_contract
    declared = {str((sl or {}).get("name") or ""): (sl or {})
                for sl in (recipe.get("slots") or [])}
    names = list(card_contract.required_slots(recipe))
    for name in declared:                      # declared-but-never-filled stays declared
        if name and name not in names:
            names.append(name)
    login_env: dict = {}
    out_slots = []
    for name in names:
        if not name:
            continue
        env_key = f"NEXUS_LOGIN_{name.upper()}"
        # Default 'secret', never 'plain': the guard EXEMPTS plain slots, so an
        # unclassified slot typed plain would let a run with no value proceed and
        # report green.
        out_slots.append({"name": name,
                          "type": declared.get(name, {}).get("type") or "secret",
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
    "ensure_baseline_from_form_login", "build_login_recipe", "find_recipes_by_login_type",
    "save_persona", "get_persona", "list_personas", "retire_persona",
    "save_persona_credential", "get_persona_credential", "credential_status",
    "stamp_card_verified", "all_credential_status", "rotate_cards", "flag_stale_cards",
    "set_expected_value", "get_expected_values",
    "set_persona_cardinality", "get_persona_cardinality",
    "save_classification", "get_classifications",
    "acquire_reservation", "release_reservation", "expire_stale_reservations",
    "count_live_reservations",
    "save_environment", "get_environment", "list_environments", "stamp_env_health",
    "bind_case_scope", "get_case_scopes",
    "record_certification", "get_certification_ledger", "certification_matrix",
    "cell_certified_scenarios",
    "build_persona_bundle",
]


async def find_recipes_by_domain(session: AsyncSession, *, tenant_id: str,
                                 login_domain: str) -> list[dict]:
    """Active recipes this tenant recorded against a domain.

    Used at onboarding, where only the base URL is known: the login_type_key is a
    hash of the form shape and no form has been observed yet, so the domain is the
    only handle available. An empty domain matches nothing — a recipe recorded
    before this column existed is simply not proposed, rather than proposed wrongly.
    """
    domain = str(login_domain or "").strip().lower()
    if not domain:
        return []
    try:
        rows = (await session.execute(
            select(TpLoginRecipeRow).where(
                TpLoginRecipeRow.tenant_id == tenant_id,
                TpLoginRecipeRow.login_domain == domain,
                TpLoginRecipeRow.status == "active")
            .order_by(TpLoginRecipeRow.created_at.desc()))).scalars().all()
    except Exception as exc:
        logger.debug("persona_store.find_by_domain_skipped err=%s", exc)
        return []
    return [{"recipe_id": r.recipe_id, "artifact_id": r.artifact_id,
             "app_id": r.app_id, "version": r.version, "slots": r.slots,
             "login_type_key": r.login_type_key, "login_domain": r.login_domain,
             "verified_at": _iso(r.verified_at)} for r in rows]
