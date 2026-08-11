"""QE-Central — client app registry (``/api/v1/qec/apps``, R-8 merged table).

POST registers a client application; credentials are envelope-encrypted
(KMS, AAD = ``app_id``) via the SAME refuse-plaintext discipline as
``auth_profiles.save_profile`` (503 — never a silent plaintext fallback).
Credentials are NEVER echoed back; responses expose ``has_credentials``
only.  Reads are open to any authenticated tenant member; mutations
require admin|manager (platform-api RBAC parity).
"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..auth import require_auth, require_role
from ..clients import platform_api, repo_intel
from ..controlplane.scheduling.admission import ADMISSION
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.controlplane_models import AppFingerprintRow
from ..db.models import ClientAppEnvironmentRow, ClientAppRow, QEExplorationRow
from ..fleet import quota
from ..services.crawl_diagnosis import diagnose as diagnose_crawl
from ..services.data_agent import (
    LLM_SYSTEM as DATA_AGENT_SYSTEM,
    build_llm_prompt,
    fill_from_items as data_agent_fill,
    parse_llm_proposal,
    propose_dispositions,
)
from ..services.pii_egress_guard import guard_inventory as pii_guard_inventory
from ..services.flow_grouping import block_cause_for_missing_primary, group_into_flows
from ..services.login_slots import credentials_from_slot_values
from ..services.seed_manifest import build_seed_manifest, library_keys_from_answer_key
from ..services.brief_compiler import (
    BRIEF_SYSTEM_INSTRUCTION, build_prompt, ground_and_assemble, parse_proposal,
)
from ..services.answer_key import _normalize_outcome
from ..services.dispositions import ASK as DISP_ASK, OBSERVE as DISP_OBSERVE
from ..services.synthesis import (
    field_inventory_for_artifact,
    known_labels_for_artifact,
    known_routes_for_artifact,
    known_value_nodes_for_artifact,
    value_candidates_for_artifact,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Apps"])

_MUTATE = require_role("admin", "manager")

# Statuses an operator may set via PATCH; 'deleted' only via DELETE.
_SETTABLE_STATUSES = frozenset({"active", "paused"})

# Attestable environment kinds; only 'disposable' may host the mutating submit
# tier. 'uat' (R2) postures as staging: crawlable, never submit-tier.
_ENV_KINDS = frozenset({"prod", "staging", "uat", "disposable"})
_SUBMIT_ENV_KIND = "disposable"


def _validated_env_kind(att: dict | None) -> dict | None:
    """422 on an UNKNOWN ``env_kind`` at WRITE time (R2 audit finding: the
    vocabulary was defined but never enforced, so a typo like 'disposible' was
    stored silently and then fail-closed-degraded to prod at READ time — the
    operator saw refusals with no clue why). A blank kind stays allowed (it
    degrades to prod honestly); only a NON-EMPTY unknown value is rejected."""
    if not att:
        return att
    kind = str((att or {}).get("env_kind") or "").strip().lower()
    if kind and kind not in _ENV_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(f"unknown env_kind {kind!r} — must be one of: "
                    f"{'|'.join(sorted(_ENV_KINDS))} (unknown kinds are treated "
                    f"as prod at enforcement time, which refuses crawls)"))
    return att


async def _bound_run_environment(session, tenant_id: str, app_id: str) -> str:
    """The app's schedule.run_environment (the profile name every cycle runs
    against), or ""."""
    app_row = (await session.execute(
        select(ClientAppRow).where(
            ClientAppRow.app_id == app_id, ClientAppRow.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if app_row is None:
        return ""
    return str(((app_row.schedule or {}).get("run_environment")) or "").strip()


#: The ONLY keys kept from a client-supplied login recording. An allowlist rather
#: than a denylist: this column is NOT encrypted, so anything unexpected — a value,
#: a cookie, a token — must be dropped by default rather than caught by name.
_LOGIN_RECORDING_KEYS = (
    "steps", "slots", "login_type_key", "domain", "login_path", "home_path",
    "federated", "slot_names",
)


def _sanitised_login_recording(raw: dict | None) -> dict:
    """Keep the choreography, drop everything else.

    A recipe is steps + slot NAMES. Credential values never belong here (they live
    in a member's encrypted card) and neither does a session (that rides the
    encrypted creds_blob). Steps are additionally stripped of any `value`, so even a
    hand-crafted payload cannot park a secret in plaintext."""
    if not isinstance(raw, dict) or not raw.get("steps"):
        return {}
    out = {k: raw[k] for k in _LOGIN_RECORDING_KEYS if k in raw}
    clean_steps = []
    for step in (out.get("steps") or []):
        if not isinstance(step, dict):
            continue
        clean_steps.append({k: v for k, v in step.items()
                            if k not in ("value", "secret", "password")})
    out["steps"] = clean_steps
    return out


class AppCreate(BaseModel):
    """Registration payload for one client application."""

    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2000)
    canonical_host: str = Field(default="", max_length=500)
    # Login credentials — envelope-encrypted at rest, never echoed.
    credentials: dict | None = None
    # A login RECORDED at onboarding: steps, slot NAMES and the reuse keys. NO
    # credential values — the session from the same recording rides in
    # `credentials.session` and is encrypted. Held on the app until the first crawl
    # mints an artifact, then materialised into a real recipe.
    login_recording: dict | None = None
    #: Values for the slots the recording OBSERVED ({slot name -> value}), mapped onto
    #: ``credentials`` by ``services/login_slots``. Lets ONE recording produce a login
    #: every future crawl can replay, instead of a session that expires. Merged UNDER
    #: any explicit ``credentials`` (an explicit field always wins).
    slot_values: dict[str, str] | None = None
    answer_key: dict = Field(default_factory=dict)
    env_attestation: dict = Field(default_factory=dict)
    fences: dict = Field(default_factory=dict)
    repo_binding: dict = Field(default_factory=dict)
    schedule: dict = Field(default_factory=dict)
    budgets: dict = Field(default_factory=dict)


class AppUpdate(BaseModel):
    """Partial update; every field optional.  ``credentials`` rotates the
    envelope blob; ``status`` may be 'active'|'paused'."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    canonical_host: str | None = Field(default=None, max_length=500)
    credentials: dict | None = None
    answer_key: dict | None = None
    env_attestation: dict | None = None
    fences: dict | None = None
    repo_binding: dict | None = None
    schedule: dict | None = None
    budgets: dict | None = None
    status: str | None = None
    # Multi-env: the NAME of the Environment Profile every cycle runs against
    # (folded into schedule.run_environment). "" clears it (single-env, unchanged);
    # a non-empty name MUST match an existing profile (validated) or the daemon
    # would fail-close every cycle.
    run_environment: str | None = None


class EnvCreate(BaseModel):
    """One named Environment Profile (dev/test/uat/prod) for an app.

    ``credentials`` folds the env's login + HTTP basic-auth + any SECRET cookies/
    headers — envelope-encrypted at rest (AAD=environment_id), never echoed.
    Non-secret routing ``cookies``/``headers`` are stored in the clear (they ARE
    the env selector, e.g. a Gloo routing cookie)."""

    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2000)
    cookies: list = Field(default_factory=list)          # [{name,value,domain,path}]
    headers: dict = Field(default_factory=dict)           # {name: value}
    credentials: dict | None = None                       # login + basic_auth + secrets
    data_overrides: dict = Field(default_factory=dict)
    fences: dict = Field(default_factory=dict)
    env_attestation: dict = Field(default_factory=dict)
    env_assertion: dict = Field(default_factory=dict)     # {selector,expect_text}|{url_pattern}


class EnvUpdate(BaseModel):
    """Partial update of an Environment Profile; every field optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2000)
    cookies: list | None = None
    headers: dict | None = None
    credentials: dict | None = None
    data_overrides: dict | None = None
    fences: dict | None = None
    env_attestation: dict | None = None
    env_assertion: dict | None = None
    status: str | None = None


def _validated_base_url(base_url: str) -> str:
    """Require an absolute http(s) URL; return it normalised (stripped)."""
    url = (base_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=422, detail="base_url must be an absolute http(s) URL",
        )
    return url


def _derive_canonical_host(base_url: str, explicit: str) -> str:
    """Use the explicit canonical_host when given, else the URL hostname."""
    host = (explicit or "").strip().lower()
    if host:
        return host[:500]
    return (urlparse(base_url).hostname or "").lower()[:500]


async def _encrypt_credentials(
    request: Request, tenant_id: str, app_id: str, credentials: dict,
    *, aad_id: str | None = None,
) -> bytes:
    """Envelope-encrypt a credentials dict (AAD = ``aad_id`` or ``app_id``).

    REFUSES with 503 when the envelope service is unavailable — we never
    store credentials in plaintext (auth_profiles.py:71-72 rule).  ``aad_id``
    lets an Environment Profile bind its blob to ``environment_id`` instead of
    ``app_id`` (defaults to ``app_id`` → every existing caller unchanged).
    """
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — refusing to store credentials in plaintext",
        )
    plaintext = json.dumps(credentials, sort_keys=True).encode("utf-8")
    _aad = (aad_id or app_id).encode("utf-8")
    try:
        blob = await envelope.encrypt(tenant_id, plaintext, aad=_aad)
    except Exception as exc:
        logger.error(
            "qec.apps.creds_encrypt_failed",
            extra={"app_id": app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="credential encryption failed")
    return blob.to_bytes()


async def _decrypt_credentials_for_merge(
    request: Request, tenant_id: str, row: ClientAppRow,
) -> dict:
    """The app's current credentials, so a partial update can MERGE onto them.

    Fail-closed on every unhappy path. Returning ``{}`` when a blob exists but
    cannot be read would re-encrypt an app's credentials as "just the new session"
    and destroy a working username/password — so an unreadable blob is a 503, not
    an empty dict. No blob at all is the one honest ``{}``.
    """
    if not row.creds_blob:
        return {}
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — cannot merge into existing credentials",
        )
    from nexus_sdk.security.envelope import EnvelopeBlob

    try:
        blob = EnvelopeBlob.from_bytes(row.creds_blob)
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=row.app_id.encode("utf-8"),
        )
        creds = json.loads(plaintext)
    except Exception as exc:
        logger.error(
            "qec.apps.creds_decrypt_failed",
            extra={"app_id": row.app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(
            status_code=503,
            detail="could not read the app's existing credentials — refusing to overwrite them",
        )
    return creds if isinstance(creds, dict) else {}


async def _seal_webhook_secret(
    request: Request, tenant_id: str, app_id: str, repo_binding: dict | None,
) -> dict:
    """Return ``repo_binding`` with any plaintext ``webhook_secret`` envelope-
    encrypted in place (AAD = app_id) as base64 ``webhook_secret_enc``.

    The webhook shared secret was stored in plaintext JSONB; both GitLab
    (X-Gitlab-Token) and GitHub (HMAC) need the cleartext at verify time, so we
    encrypt at rest and decrypt in the handler.  REFUSES (503) when a secret is
    present but encryption is unavailable — never store it in plaintext.
    """
    rb = dict(repo_binding or {})
    secret = str(rb.pop("webhook_secret", "") or "").strip()
    if not secret:
        rb.pop("webhook_secret_enc", None)
        return rb
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — refusing to store a webhook secret in plaintext",
        )
    try:
        blob = await envelope.encrypt(tenant_id, secret.encode("utf-8"), aad=app_id.encode("utf-8"))
    except Exception as exc:
        logger.error(
            "qec.apps.webhook_secret_encrypt_failed",
            extra={"app_id": app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="webhook secret encryption failed")
    rb["webhook_secret_enc"] = base64.b64encode(blob.to_bytes()).decode("ascii")
    return rb


_PROVIDER_BASE = {"gitlab": "https://gitlab.com", "github": "https://github.com"}


async def _prepare_repo_binding(
    request: Request, tenant_id: str, app_id: str, repo_binding: dict | None,
) -> dict:
    """Seal the webhook secret AND provision a repo-intel connection.

    When ``repo_binding`` carries a repo ``token`` (+ project + provider), relay it
    ONCE to repo-intel which KMS-seals it in its own store; persist only the
    returned ``connection_id`` and STRIP the raw token from qe-central.  Fail-open
    on intelligence: if repo-intel is unreachable the app is still created, marked
    ``repo_status='needs_reauth'`` (never a silent failure), and no token is kept.
    """
    rb = await _seal_webhook_secret(request, tenant_id, app_id, repo_binding)
    token = str(rb.pop("token", "") or "").strip()
    project = str(rb.get("project_path") or rb.get("project") or "").strip()
    provider = str(rb.get("provider") or "gitlab").strip().lower()
    base_url = str(rb.get("base_url") or _PROVIDER_BASE.get(provider, "")).strip()
    if token and project and base_url and provider in ("gitlab", "github", "generic_git"):
        try:
            conn = await repo_intel.create_connection(
                tenant_id=tenant_id, provider=provider, base_url=base_url,
                project_path=project, token=token, app_id=app_id,
                default_branch=str(rb.get("default_branch") or "main"),
                label=str(rb.get("label") or "")[:200],
            )
            rb["connection_id"] = conn.connection_id
            rb.pop("repo_status", None)
        except repo_intel.RepoIntelError as exc:
            logger.warning(
                "qec.apps.repo_connection_failed",
                extra={"app_id": app_id, "status": exc.status_code, "detail": exc.detail},
            )
            rb["repo_status"] = "needs_reauth"
    return rb


def _public_view(row: ClientAppRow) -> dict:
    """Serialise a row WITHOUT any secret material; expose has_* flags only."""
    from ..security import prod_guard
    d = row_to_dict(row)
    d.pop("creds_blob", None)
    d["has_credentials"] = bool(row.creds_blob)
    # Never echo the webhook secret (plaintext or ciphertext) — expose a flag.
    rb = dict(d.get("repo_binding") or {})
    has_webhook_secret = bool(rb.pop("webhook_secret", None)) or bool(rb.pop("webhook_secret_enc", None))
    d["repo_binding"] = rb
    d["has_webhook_secret"] = has_webhook_secret
    # Multi-env: surface the bound run environment (the daemon reads schedule.run_environment).
    d["run_environment"] = str((row.schedule or {}).get("run_environment") or "")
    # Onboarding legibility (crawl gate): surface the DERIVED status (draft/attested/
    # live) + the EXACT unmet requirements + attestation expiry, so the app UI can show
    # WHY an app can't crawl and offer a one-click (re-)attest — instead of a raw PATCH.
    d["onboarding_status"] = prod_guard.onboarding_status(row)
    _ready, _reasons = prod_guard.onboarding_ready(row)
    d["onboarding_ready"] = _ready
    d["onboarding_reasons"] = _reasons
    d["attestation_expires_at"] = str((row.env_attestation or {}).get("expires_at") or "")
    return d


def _finalize_attestation(att: dict | None, user: dict) -> dict:
    """Bind the accountable human to the AUTHENTICATED identity: when the operator
    leaves ``attested_by`` / ``rules_of_engagement.signed_by`` blank, stamp them from
    the JWT subject (``user['sub']``, the same identity used for audit logging) so the
    attestation is attributable rather than retyped free text. Never overrides an
    explicitly-provided value; a no-op when the attestation is empty."""
    a = dict(att or {})
    if not a:
        return a
    who = str(user.get("sub") or user.get("email") or "").strip()
    if not who:
        return a
    if not str(a.get("attested_by") or "").strip():
        a["attested_by"] = who
    roe = dict(a.get("rules_of_engagement") or {})
    if roe.get("signed") and not str(roe.get("signed_by") or "").strip():
        roe["signed_by"] = who
        a["rules_of_engagement"] = roe
    # The AUTHORIZATION signature ("we own / may test this URL") is a legal liability
    # gate — bind authorized_by to the AUTHENTICATED operator and OVERWRITE any client
    # value (unlike attested_by which preserves an explicit one), so a refusal/allow is
    # always attributable to a real identity, never unverifiable free text.
    authz = dict(a.get("authorization") or {})
    if str(authz.get("authorized")).strip().lower() in ("true", "1", "yes"):
        authz["authorized_by"] = who
        a["authorization"] = authz
    return a


async def _validated_run_environment(session, tenant_id: str, app_id: str, name: str | None) -> str:
    """Validate the app's bound run environment. ``""`` clears it (cycles run against
    the app base_url, unchanged); a non-empty name MUST match an existing Environment
    Profile for this app (else the daemon fail-closes EVERY cycle) → 422 otherwise."""
    nm = (name or "").strip()
    if not nm:
        return ""
    exists = (await session.execute(
        select(ClientAppEnvironmentRow.environment_id).where(
            ClientAppEnvironmentRow.tenant_id == tenant_id,
            ClientAppEnvironmentRow.app_id == app_id,
            ClientAppEnvironmentRow.name == nm,
        ).limit(1)
    )).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=422,
            detail=f"run_environment {nm!r} does not match any Environment Profile for this app",
        )
    return nm


async def _require_app(session, tenant_id: str, app_id: str) -> ClientAppRow:
    """Fetch one tenant-owned app row or 404 (mirrors ``_require_artifact``)."""
    row = (
        await session.execute(
            select(ClientAppRow).where(
                ClientAppRow.app_id == app_id,
                ClientAppRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="app not found")
    return row


def _validated_env_assertion(env_assertion: dict | None) -> dict:
    """Validate an env_assertion at WRITE time. It MUST be able to produce a usable
    HARD env-pin — a ``url_pattern`` OR both ``selector`` + ``expect_text`` — else the
    profile would silently run UN-pinned (a green-wash the buyer wouldn't see). Empty
    ``{}`` is allowed (no pin requested); a partial/mis-keyed one is rejected."""
    ea = dict(env_assertion or {})
    if not ea:
        return ea
    has_url = bool(str(ea.get("url_pattern") or "").strip())
    has_dom = bool(str(ea.get("selector") or "").strip()) and bool(str(ea.get("expect_text") or "").strip())
    if not (has_url or has_dom):
        raise HTTPException(
            status_code=422,
            detail="env_assertion must set url_pattern OR both selector+expect_text "
                   "(a partial assertion would run un-pinned)",
        )
    return ea


def _env_public_view(row: ClientAppEnvironmentRow) -> dict:
    """Serialise an Environment Profile WITHOUT secret material (creds_blob popped;
    ``has_credentials`` flag only) — mirrors :func:`_public_view`."""
    d = row_to_dict(row)
    d.pop("creds_blob", None)
    d["has_credentials"] = bool(row.creds_blob)
    return d


async def _require_env(
    session, tenant_id: str, app_id: str, env_id: str,
) -> ClientAppEnvironmentRow:
    """Fetch one tenant-owned Environment Profile row (scoped to its app) or 404."""
    row = (
        await session.execute(
            select(ClientAppEnvironmentRow).where(
                ClientAppEnvironmentRow.environment_id == env_id,
                ClientAppEnvironmentRow.app_id == app_id,
                ClientAppEnvironmentRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="environment not found")
    return row


@router.post("/apps", status_code=201)
async def create_app(
    payload: AppCreate, request: Request, user: dict = Depends(_MUTATE),
) -> dict:
    """Register a client app; encrypt creds; store answer_key (design §3.1)."""
    tenant_id = user["tenant_id"]
    base_url = _validated_base_url(payload.base_url)

    # Phase-7 fleet quota (fail-closed, OPT-IN): refuse a new app when the tenant's
    # plan caps ``max_apps`` and it is already at the cap.  The default plan leaves
    # ``max_apps`` unlimited, so this resolves the plan and returns immediately
    # (opening no session, running no query) — today's behaviour is unchanged.
    # Checked BEFORE credential encryption so a denied request never spends a KMS
    # envelope call.
    try:
        await quota.enforce_app_registration_quota(tenant_id)
    except quota.QuotaExceeded as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())

    app_id = new_id()

    # A RECORDING now carries the values for the slots it observed, so one recording
    # yields a login every future crawl can replay. Explicit `credentials` always win —
    # the slot values only fill what the operator did not type directly.
    credentials = dict(payload.credentials or {})
    if payload.slot_values:
        derived = credentials_from_slot_values(
            (payload.login_recording or {}).get("slots")
            or (payload.login_recording or {}).get("slot_names"),
            payload.slot_values,
        )
        for key, value in derived.items():
            if not credentials.get(key):
                credentials[key] = value

    creds_blob: bytes | None = None
    if credentials:
        creds_blob = await _encrypt_credentials(
            request, tenant_id, app_id, credentials,
        )
    repo_binding = await _prepare_repo_binding(request, tenant_id, app_id, payload.repo_binding)

    row = ClientAppRow(
        app_id=app_id,
        tenant_id=tenant_id,
        name=payload.name.strip()[:200],
        base_url=base_url,
        canonical_host=_derive_canonical_host(base_url, payload.canonical_host),
        creds_blob=creds_blob,
        # Sanitised deliberately: only the shape a recipe needs is stored, so a
        # caller cannot smuggle a credential value into an unencrypted column.
        login_recording=_sanitised_login_recording(payload.login_recording),
        answer_key=payload.answer_key or {},
        env_attestation=_finalize_attestation(_validated_env_kind(payload.env_attestation), user),
        fences=payload.fences or {},
        repo_binding=repo_binding,
        schedule=payload.schedule or {},
        budgets=payload.budgets or {},
        status="active",
    )
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(row)
        await session.flush()
        result = _public_view(row)

    logger.info(
        "qec.apps.created",
        extra={
            "tenant_id": tenant_id,
            "app_id": app_id,
            "canonical_host": row.canonical_host,
            "has_credentials": bool(creds_blob),
            "actor": user.get("sub", ""),
        },
    )
    return result


@router.get("/apps")
async def list_apps(user: dict = Depends(require_auth)) -> dict:
    """List the tenant's registered apps (creds never included)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(ClientAppRow)
                .where(ClientAppRow.tenant_id == tenant_id)
                .order_by(ClientAppRow.created_at.desc())
            )
        ).scalars().all()
        return {"apps": [_public_view(r) for r in rows], "total": len(rows)}


# Exploration statuses that mean a crawl is ACTIVE (not yet terminal). The UI
# reads these to show a "crawl in progress" state on load — so an empty Test
# Studio during a long crawl is never mistaken for a broken/completed one.
_ACTIVE_CRAWL_STATUSES = frozenset({"pending", "writing", "running", "dispatched"})


async def _latest_crawl(session, app_id: str) -> dict:
    """The most recent crawl's live status for this app, so the app view can show
    'Crawling…' on load (server truth, not ephemeral client state) and never present
    an empty Test Studio as 'done' while a crawl is still running."""
    exp = (await session.execute(
        select(QEExplorationRow)
        .where(QEExplorationRow.app_id == app_id)
        .order_by(QEExplorationRow.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if exp is None:
        # A never-crawled app still gets a typed diagnosis, so the panel says
        # "No crawl yet — start a crawl" rather than rendering nothing at all.
        return {
            "status": "none", "active": False,
            "diagnosis": diagnose_crawl(status="none", error="", stats={}),
        }
    stats = exp.stats if isinstance(exp.stats, dict) else {}
    status = (exp.status or "unknown").strip().lower()
    active = status in _ACTIVE_CRAWL_STATUSES
    # SAFETY VALVE: a crawl that has been active FAR past its wall budget is stalled
    # (a crashed worker or a lost completion callback) — never leave the UI's
    # "Crawling…" banner spinning forever. Stale-after = the dispatched wall budget +
    # a generous buffer for post-crawl substrate write + generation. Falls back to the
    # 30-min deep-crawl ceiling for older rows that never stamped a budget.
    stalled = False
    if active and exp.started_at is not None:
        try:
            wall_ms = int(stats.get("budget_wall_ms") or 0)
            stale_after_s = (wall_ms / 1000.0 if wall_ms > 0 else 1_800.0) + 180.0
            started = exp.started_at
            if started.tzinfo is None:  # defensive: treat a naive stamp as UTC
                started = started.replace(tzinfo=timezone.utc)
            elapsed_s = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed_s > stale_after_s:
                active = False
                stalled = True
        except (TypeError, ValueError, AttributeError):
            pass  # never let a timestamp edge-case break get_app; keep active as-is
    effective_status = "stalled" if stalled else status
    return {
        "exploration_id": exp.exploration_id,
        "status": effective_status,
        "active": active,
        "started_at": str(exp.started_at or ""),
        "finished_at": str(exp.finished_at or ""),
        "artifact_id": exp.artifact_id or "",
        # Pages captured (populated on completion today; a live heartbeat can fill it
        # during the crawl later). Best-effort — 0 while a fresh crawl is mid-flight.
        "pages": int(stats.get("visits") or 0),
        # Typed, durable diagnosis so the app panel always states WHY a crawl ended
        # and WHAT to do next — never a blank Test Studio. Survives reload (read-time,
        # not client session state). Uses the stall-valve-adjusted status.
        "diagnosis": diagnose_crawl(
            status=effective_status, error=exp.error or "", stats=stats,
        ),
    }


async def _auth_capabilities(request: Request, tenant_id: str, row: ClientAppRow) -> dict:
    """What this app can ACTUALLY do to authenticate — non-secret booleans only.

    ``has_credentials`` is only "a credential blob exists", and a blob holding nothing
    but a recorded SESSION satisfied it — so an app that cannot sign itself in reported
    the same flag as one that can, and the UI told the operator "this app can sign
    itself in, nothing to do" while every crawl walked the logged-out product. These
    three flags separate the two, so the product can state the truth and offer the real
    choice:

      * ``can_sign_in``  — a username+password (or an auth hook) the crawl can REPLAY.
        Durable: it works on every future crawl.
      * ``has_session``  — a recorded session. A snapshot: it expires, and an app whose
        login lives in client-side state cannot restore it at all.
      * ``has_mfa``      — a second factor the crawl can compute.

    Fail-SAFE: any decryption problem yields all-False rather than an error, and the
    caller degrades to the old flag — a credential page must never 500.
    """
    caps = {"can_sign_in": False, "has_session": False, "has_mfa": False}
    if not row.creds_blob:
        return caps
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        return caps
    try:
        from nexus_sdk.security.envelope import EnvelopeBlob

        blob = EnvelopeBlob.from_bytes(row.creds_blob)
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=row.app_id.encode("utf-8"),
        )
        creds = json.loads(plaintext)
    except Exception:
        return caps
    if not isinstance(creds, dict):
        return caps
    username = str(creds.get("username") or "").strip()
    password = str(creds.get("password") or "")
    auth_hook = str(creds.get("auth_hook") or "").strip()
    caps["can_sign_in"] = bool((username and password) or auth_hook)
    caps["has_session"] = bool(creds.get("session"))
    caps["has_mfa"] = bool(creds.get("mfa"))
    return caps


@router.get("/apps/{app_id}")
async def get_app(
    app_id: str, request: Request, user: dict = Depends(require_auth),
) -> dict:
    """Fetch one app (404 when absent or foreign-tenant — RLS + WHERE)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        view = _public_view(row)
        # The TRUTH about how this app authenticates (see _auth_capabilities): whether
        # it can sign ITSELF in, versus merely holding a session that may not replay.
        view.update(await _auth_capabilities(request, tenant_id, row))
        # Live crawl status so the UI shows "Crawling…" instead of an empty Studio.
        view["crawl"] = await _latest_crawl(session, app_id)
        return view


@router.post("/apps/{app_id}/data-agent/propose")
async def data_agent_propose(app_id: str, user: dict = Depends(_MUTATE)) -> dict:
    """Data Agent (Phase 3): classify every observed field and shrink the human ask.

    Floor-first + fail-closed: the deterministic six-disposition floor always runs; the
    LLM refines it ONLY when the PII egress guard clears the value-free payload AND a
    provider is configured. The grounding gate + hard-line re-validate every proposal,
    so a fabricated SSN/policy value can never enter the fill. The response reports the
    LLM's measured delta and its honest availability (no silent 'full manual').
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        answer_key = row.answer_key if isinstance(row.answer_key, dict) else {}
        artifact_id = row.latest_artifact_id or ""
    if not artifact_id:
        return {"status": "no_crawl", "items": [], "recommended": [], "prefill": {},
                "llm_used": False, "llm_ok": False, "egress_safe": True,
                "llm_error": "crawl the app first"}

    inventory = await field_inventory_for_artifact(tenant_id, artifact_id)
    candidates = await value_candidates_for_artifact(tenant_id, artifact_id)
    library_keys = library_keys_from_answer_key(answer_key)
    observe_labels = [str(c.get("label") or "") for c in candidates if c.get("label")]

    guard = pii_guard_inventory(inventory)
    llm_proposal = None
    llm_ok = False
    llm_error = ""
    if not guard["safe"]:
        llm_error = "PII egress guard blocked the LLM: " + guard["reason"]
    else:
        res = await platform_api.complete_llm(
            tenant_id=tenant_id, prompt=build_llm_prompt(inventory),
            system=DATA_AGENT_SYSTEM, task="field_disposition",
        )
        llm_ok = bool(res.ok)
        if res.ok:
            llm_proposal = parse_llm_proposal(res.text)
        else:
            llm_error = res.detail or "no LLM provider configured on-prem"

    out = propose_dispositions(
        inventory, llm_proposal=llm_proposal, library_keys=library_keys,
        observe_labels=observe_labels, today=datetime.now(timezone.utc).date(),
    )
    items = out["items"]
    out["recommended"] = [i for i in items if i["disposition"] in ("ASK", "APPROVE")]
    out["prefill"] = data_agent_fill(items)
    out["egress_safe"] = guard["safe"]
    out["llm_ok"] = llm_ok
    out["llm_error"] = llm_error
    out["status"] = "ready"
    return out


class DataAgentSaveIn(BaseModel):
    """Human-confirmed seed values to persist into ``answer_key.fill``.

    ``fills`` maps an OBSERVED field LABEL → the value to seed (typically the ASK
    fields the human must supply, or an override of a grounded default). The
    grounded auto-fillable defaults are persisted automatically — this body only
    carries the values a human owns."""

    fills: dict[str, str | float | int] = Field(default_factory=dict)


@router.post("/apps/{app_id}/data-agent/save")
async def data_agent_save(
    app_id: str, body: DataAgentSaveIn, user: dict = Depends(_MUTATE),
) -> dict:
    """Persist the Data Agent's grounded proposal (+ human ASK values) into
    ``answer_key.fill`` so the NEXT crawl actually seeds them — closing the gap
    (audit P1) where a grounded, PII-guarded proposal never reached a crawl
    without a human re-typing it by hand.

    Fail-closed, never green-wash:
      * grounded auto-fillable defaults (``data_agent_fill``: SYNTHESIZE/PICK/CARRY,
        already hard-lined) are persisted; OBSERVE (oracle) and ASK (never invented)
        are EXCLUDED, so no fabricated value can reach the crawl;
      * a human ``fills`` entry is accepted ONLY for a real OBSERVED field that is
        NOT an OBSERVE oracle — an unknown/hallucinated label, or an attempt to seed
        an oracle field, is REFUSED and reported (never silently dropped);
      * the deterministic FLOOR (no LLM) drives persistence, so what is saved is
        reproducible and independent of any LLM call;
      * the response names what is STILL MISSING (ASK fields with no value) so the
        UI can never show a false 'ready'.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        if row.status == "deleted":
            raise HTTPException(status_code=409, detail="app is deleted")
        answer_key = dict(row.answer_key or {})
        artifact_id = row.latest_artifact_id or ""
        if not artifact_id:
            raise HTTPException(
                status_code=409,
                detail="no crawl artifact yet — crawl the app before saving seeds",
            )

        inventory = await field_inventory_for_artifact(tenant_id, artifact_id)
        candidates = await value_candidates_for_artifact(tenant_id, artifact_id)
        library_keys = library_keys_from_answer_key(answer_key)
        observe_labels = [str(c.get("label") or "") for c in candidates if c.get("label")]
        items = propose_dispositions(
            inventory, llm_proposal=None, library_keys=library_keys,
            observe_labels=observe_labels, today=datetime.now(timezone.utc).date(),
        )["items"]

        disp_by_label = {str(i.get("label")): str(i.get("disposition") or "") for i in items}
        oracle_labels = {lbl for lbl, d in disp_by_label.items() if d == DISP_OBSERVE}
        grounded = data_agent_fill(items)   # SYNTHESIZE/PICK/CARRY defaults only

        accepted: dict[str, str] = {}
        rejected: list[dict] = []
        for raw_label, raw_val in (body.fills or {}).items():
            label = str(raw_label).strip()
            val = str(raw_val).strip()
            if not label or not val:
                rejected.append({"label": raw_label, "reason": "empty label or value"})
                continue
            if label not in disp_by_label:
                rejected.append({"label": label,
                                 "reason": "not an observed field on this app's crawl"})
                continue
            if label in oracle_labels:
                rejected.append({"label": label,
                                 "reason": "OBSERVE oracle field — proven, never seeded as a form fill"})
                continue
            accepted[label] = val

        existing_fill = answer_key.get("fill") if isinstance(answer_key.get("fill"), dict) else {}
        # Preserve prior fills; refresh grounded defaults; human-confirmed values WIN.
        new_fill = {**existing_fill, **grounded, **accepted}
        answer_key["fill"] = new_fill
        row.answer_key = answer_key    # reassign for JSONB change detection
        row.updated_at = utc_now()

        still_missing = sorted(
            lbl for lbl, d in disp_by_label.items()
            if d == DISP_ASK and not str(new_fill.get(lbl) or "").strip()
        )

    logger.info(
        "qec.apps.data_agent_saved",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "grounded": len(grounded), "human": len(accepted),
               "rejected": len(rejected), "still_missing_ask": len(still_missing)},
    )
    return {
        "app_id": app_id,
        "status": "ok",
        "persisted_fill_count": len(new_fill),
        "auto_grounded": len(grounded),
        "human_provided": len(accepted),
        "rejected": rejected,
        "still_missing_ask": still_missing,
    }


@router.get("/apps/{app_id}/seed-manifest")
async def get_seed_manifest(
    app_id: str,
    request: Request,
    mode: str = "recommended",
    user: dict = Depends(require_auth),
) -> dict:
    """The discovery-first Seed Manifest (Phase 1) for an app's latest crawl.

    Classifies every observed field into one of the six dispositions and returns BOTH
    views so the portal can toggle without a round-trip: ``recommended`` (only the ASK
    + APPROVE human-1%) and ``full`` (every field, grounded default, editable). ``mode``
    selects which list the ``items`` convenience field mirrors (default recommended).
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        answer_key = row.answer_key if isinstance(row.answer_key, dict) else {}
        artifact_id = row.latest_artifact_id or ""
        base_url = row.base_url or ""
        has_credentials = row.creds_blob is not None
        # Can the crawl sign ITSELF in (username+password / auth hook), or does it only
        # hold a session? Decides whether the honest remediation is "re-record" or
        # "add a username and password" — see block_cause_for_missing_primary.
        _caps = await _auth_capabilities(request, tenant_id, row)
        # The crawler's own account of what blocked deeper coverage — a richer source
        # than form_snapshot_signals (which may only hold the entry/login page). Merged
        # into the manifest so deep-form fields (a transfer's From Account / Payee behind
        # a login) surface, matching the crawl's SEEDS_NEEDED diagnosis.
        exps = (await session.execute(
            select(QEExplorationRow)
            .where(QEExplorationRow.app_id == app_id)
            .order_by(QEExplorationRow.created_at.desc())
            .limit(10)
        )).scalars().all()
        # The NEWEST crawl's own account of whether it was BLOCKED at a login (no
        # credentials / expired session) vs merely didn't reach the entry — captured
        # once (rows are created_at desc) so the manifest reports the truthful
        # remediation instead of a blanket "re-crawl".
        _newest_cov: dict = {}
        if exps:
            _nstats = exps[0].stats if isinstance(exps[0].stats, dict) else {}
            _newest_cov = (_nstats.get("coverage")
                           if isinstance(_nstats.get("coverage"), dict) else {})
        # Union across recent crawls: "fields needing seed" is a stable property of the
        # app's forms, so a shallow re-crawl (that only reached login) must not hide what
        # a deeper earlier crawl found (a transfer's From Account / Payee). Deduped,
        # order-preserving.
        seed_fields: list[str] = []
        _seen_seed: set[str] = set()
        # Per-field PAGE url {label: url} the crawler now emits — grounds flow grouping in
        # the actual page a field was on instead of a keyword guess. First non-empty wins.
        seed_field_urls: dict[str, str] = {}
        # DOM-unreadable surfaces the crawl detected → the ledger's OPAQUE rows. Unioned
        # across crawls, deduped by (kind,label).
        opaque_surfaces: list[dict] = []
        _seen_opaque: set[str] = set()
        unhandled_controls: list[dict] = []
        _seen_unhandled: set[str] = set()
        # The submit the crawl FOUND but is not allowed to press. Read here so the
        # manifest can offer an APPROVE row for it — without this the product asks for
        # an approval it provides no way to grant.
        submit_candidates: list[str] = []
        _seen_submit: set[str] = set()
        for _e in exps:
            _st = _e.stats if isinstance(_e.stats, dict) else {}
            _cov = _st.get("coverage") if isinstance(_st.get("coverage"), dict) else {}
            for _f in (_cov.get("fields_needing_seed") or []):
                _s = str(_f).strip()
                if _s and _s.lower() not in _seen_seed:
                    _seen_seed.add(_s.lower())
                    seed_fields.append(_s)
            for _d in (_cov.get("fields_needing_seed_detail") or []):
                _lbl = str((_d or {}).get("label") or "").strip()
                _url = str((_d or {}).get("url") or "").strip()
                if _lbl and _url and _lbl.lower() not in {k.lower() for k in seed_field_urls}:
                    seed_field_urls[_lbl] = _url
            for _o in (_cov.get("opaque_surfaces") or []):
                _ok = f"{(_o or {}).get('kind')}|{(_o or {}).get('label')}"
                if _ok not in _seen_opaque:
                    _seen_opaque.add(_ok)
                    opaque_surfaces.append(dict(_o))
            for _sc in (_cov.get("submit_candidates") or []):
                _s2 = str(_sc).strip()
                if _s2 and _s2.lower() not in _seen_submit:
                    _seen_submit.add(_s2.lower())
                    submit_candidates.append(_s2)
            for _u in (_cov.get("unhandled_controls") or []):
                _uk = str((_u or {}).get("label") or "").strip().lower()
                if _uk and _uk not in _seen_unhandled:
                    _seen_unhandled.add(_uk)
                    unhandled_controls.append(dict(_u))
    manifest = await build_seed_manifest(
        tenant_id, artifact_id, answer_key=answer_key, seed_fields=seed_fields,
        opaque_surfaces=opaque_surfaces, unhandled_controls=unhandled_controls,
        submit_candidates=submit_candidates,
        today=datetime.now(timezone.utc).date(),
    )
    # Group the fields into flows so the portal can lead with the flow the app was
    # onboarded for ("to test Transfer, provide these") instead of a flat, undifferentiated
    # list. Values already in the answer key drive per-flow progress; stored credentials
    # mark the login group satisfied so we never re-ask for it.
    fill = answer_key.get("fill") if isinstance(answer_key.get("fill"), dict) else {}
    provided_labels = [str(k) for k in fill.keys()]
    # The page routes the crawl actually reached — ground truth for "was the onboarded
    # entry flow captured?" (an auto-only field must not hint-fake the primary as present).
    captured_paths = await known_routes_for_artifact(tenant_id, artifact_id) if artifact_id else []
    grouping = group_into_flows(
        manifest.get("full") or [],
        base_url=base_url,
        provided_labels=provided_labels,
        auth_satisfied=has_credentials,
        field_urls=seed_field_urls,
        captured_paths=captured_paths,
    )
    manifest.update(grouping)
    manifest["has_credentials"] = has_credentials
    # HONEST "why wasn't the entry captured": tell a benign non-reach (budget/depth —
    # "re-crawl") apart from an AUTH BLOCK (the entry is behind a login the crawl could
    # not pass — re-crawling alone loops forever). Prefer the crawler's explicit signal
    # (coverage.auth_blocked); fall back to "no credentials AND a login flow was seen"
    # so the truth still surfaces on crawls recorded before that flag shipped. Never
    # marks a block when credentials exist and the crawl reported no auth trouble.
    mp = manifest.get("missing_primary")
    if isinstance(mp, dict):
        block = block_cause_for_missing_primary(
            has_credentials=has_credentials,
            login_flow_present=manifest.get("auth") is not None,
            coverage=_newest_cov,
            can_sign_in=bool(_caps.get("can_sign_in")),
        )
        if block:
            mp.update(block)
    mode = mode if mode in ("recommended", "full") else "recommended"
    manifest["mode"] = mode
    manifest["items"] = manifest["full"] if mode == "full" else manifest["recommended"]
    return manifest


#: Generic step-advance labels. Approving one of these as a Phase-B SUBMIT
#: control silently disables every wizard step that uses the same label — and
#: "Continue"/"Next"/"Submit" are the same word on step 2 as on step 5.
#:
#: Observed live: an app configured submit_approvals=["Continue"], and its
#: five-step quote funnel was recorded as five ONE-STEP journeys. No error was
#: raised anywhere; the catalogue simply described a product that does not
#: exist. The approval itself is a legitimate operator action — the defect is
#: that it was accepted without saying what it would cost.
_ADVANCE_SHADOW_WORDS = frozenset({
    "continue", "next", "proceed", "forward", "go", "start", "begin", "resume",
})


def _reject_advance_shadowing_approvals(fences: Any) -> None:
    """Refuse a submit approval that would make the app's funnel unwalkable.

    Fail LOUD, not closed: the operator is told exactly which label collides and
    what to approve instead (the FINAL submit control, whose label is usually
    distinct — "See My Quote", "Place Order", "Submit Application").
    """
    if not isinstance(fences, Mapping):
        return
    approvals = fences.get("submit_approvals")
    if not isinstance(approvals, (list, tuple)):
        return
    clashing = sorted({
        str(a).strip() for a in approvals
        if str(a).strip().lower() in _ADVANCE_SHADOW_WORDS
    })
    if clashing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"submit_approvals contains generic step-advance label(s) "
                f"{clashing}. That label also advances every earlier step of the "
                f"flow, so approving it for submit makes the whole journey "
                f"unwalkable and the catalogue records one-step journeys. "
                f"Approve the FINAL submit control instead (e.g. 'See My Quote', "
                f"'Submit Application')."
            ),
        )


@router.patch("/apps/{app_id}")
async def update_app(
    app_id: str,
    payload: AppUpdate,
    request: Request,
    user: dict = Depends(_MUTATE),
) -> dict:
    """Partial update (pause/resume, fences/budgets, credential rotation)."""
    tenant_id = user["tenant_id"]

    if payload.status is not None and payload.status not in _SETTABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {'|'.join(sorted(_SETTABLE_STATUSES))}",
        )

    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        if row.status == "deleted":
            raise HTTPException(status_code=409, detail="app is deleted")

        if payload.base_url is not None:
            row.base_url = _validated_base_url(payload.base_url)
            row.canonical_host = _derive_canonical_host(
                row.base_url, payload.canonical_host or row.canonical_host,
            )
        elif payload.canonical_host is not None:
            row.canonical_host = _derive_canonical_host(row.base_url, payload.canonical_host)

        if payload.name is not None:
            row.name = payload.name.strip()[:200]
        for field in ("answer_key", "fences", "schedule", "budgets"):
            value = getattr(payload, field)
            if value is not None:
                if field == "fences":
                    _reject_advance_shadowing_approvals(value)
                setattr(row, field, value)
        if payload.env_attestation is not None:
            # (Re-)attest binds the accountable human to the authenticated identity.
            row.env_attestation = _finalize_attestation(_validated_env_kind(payload.env_attestation), user)
        if payload.repo_binding is not None:
            # Seal the webhook secret + (re)provision the repo-intel connection.
            row.repo_binding = await _prepare_repo_binding(
                request, tenant_id, app_id, payload.repo_binding,
            )
        if payload.status is not None:
            row.status = payload.status
        if payload.run_environment is not None:
            # Fold the validated selector into schedule.run_environment (the key the
            # cycle daemon reads); merges onto any schedule set above in this request.
            name = await _validated_run_environment(session, tenant_id, app_id, payload.run_environment)
            sched = dict(row.schedule or {})
            if name:
                sched["run_environment"] = name
            else:
                sched.pop("run_environment", None)
            row.schedule = sched
        if payload.credentials is not None:
            row.creds_blob = await _encrypt_credentials(
                request, tenant_id, app_id, payload.credentials,
            )
        row.updated_at = utc_now()
        await session.flush()
        result = _public_view(row)

    logger.info(
        "qec.apps.updated",
        extra={"tenant_id": tenant_id, "app_id": app_id, "actor": user.get("sub", "")},
    )
    return result


class LoginRecordingIn(BaseModel):
    """A login re-recorded for an app that already exists.

    Same two halves the onboarding wizard produces: the ``session`` that gets a
    CRAWL past the login, and the ``login_recording`` choreography that becomes a
    run-side recipe. Either may be sent alone — a recording with no usable session
    is still worth keeping, and a session refresh needs no new choreography.
    """

    login_recording: dict | None = None
    session: dict | None = None
    # The credentials the crawler RE-DRIVES the login with when it meets an auth
    # wall mid-journey. A captured session cannot do this: it is a snapshot that
    # expires, whereas a username/password re-authenticates forever with no human.
    # Settable ONLY here and at onboarding — an app that lost its session had no
    # way to be given credentials without being re-registered, which stranded its
    # whole catalogue under a new app_id.
    username: str | None = Field(default=None, max_length=400)
    password: str | None = Field(default=None, max_length=400)
    #: The values for the slots the RECORDING observed this app ask for
    #: (``{slot name -> value}``). Recording is value-free by construction, so without
    #: these a recording can only replay a session — a snapshot that expires and that
    #: some apps never restore. Mapped onto {username, password, mfa} by
    #: ``services/login_slots`` using the recipe's own slots, so a member#+PIN login and
    #: a 3-slot MFA login both work with no app-specific knowledge.
    slot_values: dict[str, str] | None = None


@router.post("/apps/{app_id}/login-recording")
async def replace_login_recording(
    app_id: str,
    payload: LoginRecordingIn,
    request: Request,
    user: dict = Depends(_MUTATE),
) -> dict:
    """Re-record the login for an EXISTING app.

    Sessions expire on the application's own schedule, so every app eventually
    needs its login re-recorded. Until this existed the recorder lived only in the
    onboarding wizard, and the only way to restore authenticated crawling was to
    register the app again — which strands its catalogue under a new app_id and
    does not scale past a handful of applications.

    MERGES into the credential blob rather than replacing it. ``PATCH /apps`` sets
    ``credentials`` wholesale (a deliberate rotation), so reusing it here would
    silently destroy a stored username/password every time somebody refreshed a
    session. Only the ``session`` key is touched.
    """
    username = (payload.username or "").strip()
    password = payload.password or ""
    # Values captured for the slots the recording OBSERVED. They map onto the same
    # {username, password, mfa} shape as the typed fields, so "record once" finally
    # yields a login every FUTURE crawl can replay — not just this one session.
    slot_creds: dict = {}
    if payload.slot_values:
        slot_creds = credentials_from_slot_values(
            (payload.login_recording or {}).get("slots")
            or (payload.login_recording or {}).get("slot_names"),
            payload.slot_values,
        )
        username = username or str(slot_creds.get("username") or "").strip()
        password = password or str(slot_creds.get("password") or "")
    if (payload.login_recording is None and payload.session is None
            and not username and not password and not payload.slot_values):
        raise HTTPException(
            status_code=422,
            detail="send login_recording, session, credentials, or any combination — nothing to record",
        )
    if bool(username) != bool(password):
        raise HTTPException(
            status_code=422,
            detail="a username needs a password (and vice versa) — a half credential cannot log in",
        )

    recording = _sanitised_login_recording(payload.login_recording)
    if payload.login_recording is not None and not recording:
        raise HTTPException(
            status_code=422,
            detail=(
                "the recording carries no steps — log in inside the recorder "
                "before saving, or send only the session"
            ),
        )

    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        if row.status == "deleted":
            raise HTTPException(status_code=409, detail="app is deleted")

        # Slot values can arrive WITHOUT a fresh recording (filling in the login this
        # app already recorded) — map them against the recipe stored on the row.
        if payload.slot_values and not username:
            slot_creds = credentials_from_slot_values(
                (row.login_recording or {}).get("slots")
                or (row.login_recording or {}).get("slot_names"),
                payload.slot_values,
            )
            username = str(slot_creds.get("username") or "").strip()
            password = str(slot_creds.get("password") or "")
            if bool(username) != bool(password):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "fill both the identifier and the secret this login asks for — "
                        "a half credential cannot sign in"
                    ),
                )

        if payload.session is not None or username or slot_creds.get("mfa"):
            creds = await _decrypt_credentials_for_merge(request, tenant_id, row)
            if payload.session is not None:
                creds["session"] = payload.session
            if username:
                creds["username"], creds["password"] = username, password
            # A second factor the crawl can COMPUTE — without it an MFA login stalls on
            # the code screen no matter how correct the username and password are.
            if slot_creds.get("mfa"):
                creds["mfa"] = slot_creds["mfa"]
            row.creds_blob = await _encrypt_credentials(
                request, tenant_id, app_id, creds,
            )
        if recording:
            row.login_recording = recording
        row.updated_at = utc_now()
        await session.flush()
        result = _public_view(row)

    logger.info(
        "qec.apps.login_recording_replaced",
        extra={
            "tenant_id": tenant_id, "app_id": app_id,
            "actor": user.get("sub", ""),
            "steps": len(recording.get("steps") or ()),
            "session_refreshed": payload.session is not None,
            # The flag, never the value.
            "credentials_set": bool(username),
        },
    )
    return result


@router.delete("/apps/{app_id}")
async def delete_app(app_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """Soft-delete: zero the credential ciphertext + mark status='deleted'.

    Mirrors the repo_connections revoke discipline (ciphertext zeroed, row
    retained for audit).  Admin-only — destructive-adjacent.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        connection_id = str((row.repo_binding or {}).get("connection_id") or "").strip()
        row.creds_blob = None
        row.status = "deleted"
        row.updated_at = utc_now()

    # Revoke the sealed repo-intel connection (wipes its token + workdir). Best
    # effort — a repo-intel outage never blocks the delete.
    if connection_id:
        await repo_intel.revoke_connection(tenant_id=tenant_id, connection_id=connection_id)

    logger.info(
        "qec.apps.deleted",
        extra={"tenant_id": tenant_id, "app_id": app_id, "actor": user.get("sub", "")},
    )
    return {"app_id": app_id, "status": "deleted", "credentials_zeroed": True}


class CompileBriefIn(BaseModel):
    notes: str = Field(default="", max_length=20_000)    # seed-data brief (Data tab)
    answers: str = Field(default="", max_length=20_000)  # expected outcomes/invariants


@router.post("/apps/{app_id}/compile-brief")
async def compile_brief(
    app_id: str, body: CompileBriefIn, user: dict = Depends(_MUTATE),
) -> dict:
    """ANSWERS P2 — compile a plain-English brief into a GROUNDED answer_key PROPOSAL.

    The LLM only proposes; :func:`ground_and_assemble` re-validates every item against
    the app's REAL captured field labels, so a hallucinated field can NEVER enter the
    active contract — it is flagged ``needs_confirmation`` in the review list. On any
    LLM failure the endpoint degrades honestly (empty proposal + ``llm_error``), never
    a fabricated contract. This is a pure PROPOSE step: the operator reviews the
    result and saves the confirmed answer_key via ``PATCH /apps/{app_id}``.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        artifact_id = row.latest_artifact_id or ""

    known_labels = await known_labels_for_artifact(tenant_id, artifact_id)
    known_value_nodes = await known_value_nodes_for_artifact(tenant_id, artifact_id)  # P1.B
    prompt = build_prompt(
        notes=body.notes, answers=body.answers,
        known_labels=known_labels, known_value_nodes=known_value_nodes)
    llm = await platform_api.complete_llm(
        tenant_id=tenant_id, prompt=prompt, system=BRIEF_SYSTEM_INSTRUCTION, task="brief_compile")
    proposal = parse_proposal(llm.text) if llm.ok else {}
    result = ground_and_assemble(
        proposal, known_labels=known_labels, known_value_nodes=known_value_nodes)
    result["llm_ok"] = llm.ok
    result["llm_error"] = "" if llm.ok else (llm.detail or "LLM unavailable — author the answer_key manually")
    result["known_label_count"] = len(known_labels)
    result["known_value_node_count"] = len(known_value_nodes)
    logger.info(
        "qec.apps.compile_brief",
        extra={"tenant_id": tenant_id, "app_id": app_id, "llm_ok": llm.ok,
               "labels": len(known_labels), "grounded": result.get("grounded"),
               "ungrounded": result.get("ungrounded")},
    )
    return result


# ─────────────── #2 Value-oracle proving wiring (candidate → confirmed) ──────
# The crawl CLASSIFIES rendered value nodes as candidate expected outcomes
# (value_infer: a premium/total/decision). These endpoints close the loop:
# surface the candidates for review, and let a human CONFIRM one — writing a
# grounded {field, expected, source_hint} into answer_key.outcomes. Everything
# downstream is UNCHANGED, FROZEN machinery: value_oracle_contract → factory
# generate (value_assertions) → compiler → value_oracle.py PROVEN assertion at
# the captured selector → the frozen verdict reducer classifies a miss.


def _outcomes_as_list(answer_key: dict) -> list[dict]:
    """The stored ``answer_key.outcomes`` READ as a list of structured records
    (the flat-map form ``{field: expected}`` is projected; the ``_raw`` free-text
    sentinel and non-dict entries are skipped — ungroundable for matching)."""
    src = (answer_key or {}).get("outcomes")
    if isinstance(src, dict):
        return [{"field": k, "expected": v} for k, v in src.items()
                if str(k).strip() and str(k).strip() != "_raw"]
    if isinstance(src, (list, tuple)):
        return [dict(o) for o in src if isinstance(o, dict)]
    return []


def _outcomes_preserving_upgrade(answer_key: dict) -> list:
    """The stored ``outcomes`` upgraded to LIST form for a WRITE, preserving
    everything verbatim that this endpoint did not author: the ``_raw`` free-text
    sentinel survives as ``{"_raw": ...}`` (the contract projector already skips
    it) and unknown/non-dict entries ride through untouched — a confirm must
    never silently prune another author's data."""
    src = (answer_key or {}).get("outcomes")
    if isinstance(src, dict):
        out: list = [{"field": k, "expected": v} for k, v in src.items()
                     if str(k).strip() and str(k).strip() != "_raw"]
        if "_raw" in src:
            out.append({"_raw": src["_raw"]})
        return out
    if isinstance(src, (list, tuple)):
        return list(src)
    return []


@router.get("/apps/{app_id}/value-candidates")
async def list_value_candidates(
    app_id: str, user: dict = Depends(require_auth),
) -> dict:
    """#2 — the crawl-classified CANDIDATE expected values, for review.

    Each candidate carries the captured selector (``source_hint``), the rendered
    ``text`` (a runtime observation — shown as a pre-fill hint, never auto-
    asserted), the inferred ``value_type`` and confidence, plus ``confirmed``
    when an outcome already pins that selector."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        artifact_id = row.latest_artifact_id or ""
        answer_key = dict(row.answer_key or {})
    candidates = await value_candidates_for_artifact(tenant_id, artifact_id)
    confirmed_hints = {
        str(o.get("source_hint") or "").strip()
        for o in _outcomes_as_list(answer_key)
    } - {""}
    for c in candidates:
        c["confirmed"] = c["source_hint"] in confirmed_hints
    return {"app_id": app_id, "artifact_id": artifact_id,
            "candidates": candidates, "count": len(candidates)}


class ValueCandidateConfirmIn(BaseModel):
    """A HUMAN-CONFIRMED expected value for a crawl-captured candidate node."""

    field: str = Field(min_length=1, max_length=200)
    #: the AUTHORED expected value (the captured text is only a pre-fill hint).
    expected: str | float | int
    #: the captured node selector — must match a crawl-captured candidate.
    source_hint: str = Field(min_length=1, max_length=300)
    when: dict = Field(default_factory=dict)
    match: str = Field(default="", max_length=20)      # ''|numeric|exact|contains
    tolerance: float | None = None


@router.post("/apps/{app_id}/value-candidates/confirm")
async def confirm_value_candidate(
    app_id: str, body: ValueCandidateConfirmIn, user: dict = Depends(_MUTATE),
) -> dict:
    """#2 — CONFIRM one candidate into ``answer_key.outcomes`` (grounded, fail-closed).

    Anti-fabrication gates:
      * the ``source_hint`` MUST be one of the crawl-captured candidate selectors
        for the app's current artifact (a hand-typed/hallucinated selector is
        refused — the assertion must point at evidence);
      * the expected value is normalized by the SAME frozen
        :func:`_normalize_outcome` the run contract uses — an ungroundable
        expectation is refused, never green-washed.
    One outcome per (field, source_hint): re-confirming replaces the entry."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        if row.status == "deleted":
            raise HTTPException(status_code=409, detail="app is deleted")
        artifact_id = row.latest_artifact_id or ""
        if not artifact_id:
            raise HTTPException(
                status_code=409,
                detail="no crawl artifact yet — crawl the app before confirming values",
            )

        candidates = await value_candidates_for_artifact(tenant_id, artifact_id)
        hint = body.source_hint.strip()
        if hint not in {c["source_hint"] for c in candidates}:
            raise HTTPException(
                status_code=422,
                detail="ungrounded source_hint — it must be one of the crawl-captured "
                       "candidate selectors (GET /apps/{app_id}/value-candidates)",
            )

        if len(json.dumps(body.when, default=str)) > 2000:
            raise HTTPException(status_code=422, detail="'when' condition too large")

        rec = _normalize_outcome(body.field, body.expected, {
            "source_hint": hint, "when": body.when,
            "match": body.match, "tolerance": body.tolerance,
        })
        if rec is None:
            raise HTTPException(
                status_code=422,
                detail="ungroundable expectation — supply a non-empty field and a "
                       "scalar expected value",
            )

        answer_key = dict(row.answer_key or {})
        outcomes = _outcomes_preserving_upgrade(answer_key)
        _key = (rec["field"].strip().lower(), rec["source_hint"])
        outcomes = [o for o in outcomes
                    if not (isinstance(o, dict)
                            and (str(o.get("field") or "").strip().lower(),
                                 str(o.get("source_hint") or "").strip()) == _key)]
        outcomes.append(rec)
        answer_key["outcomes"] = outcomes
        row.answer_key = answer_key   # reassign: JSONB change detection
        row.updated_at = utc_now()

    logger.info(
        "qec.apps.value_candidate_confirmed",
        extra={"tenant_id": tenant_id, "app_id": app_id,
               "field": rec["field"], "source_hint": rec["source_hint"],
               "match": rec["match"], "outcomes": len(outcomes)},
    )
    return {"app_id": app_id, "confirmed": rec, "outcomes_count": len(outcomes)}


# ─────────────────────── Environment Profiles (multi-env) ───────────────────
# An app is crawled ONCE against its reference env → one flow + one baseline.
# Environment Profiles are named run-time REBINDS of that flow (dev/test/uat/prod):
# base_url + routing cookies/headers + per-env fences + an env_assertion. Secrets
# (login/basic-auth/secret cookies/headers) are KMS-sealed (AAD=environment_id).

@router.post("/apps/{app_id}/environments", status_code=201)
async def create_environment(
    app_id: str, payload: EnvCreate, request: Request, user: dict = Depends(_MUTATE),
) -> dict:
    """Register an Environment Profile for an app (multi-env, crawl-once/run-many)."""
    tenant_id = user["tenant_id"]
    env_id = new_id()
    creds_blob: bytes | None = None
    if payload.credentials:
        creds_blob = await _encrypt_credentials(
            request, tenant_id, app_id, payload.credentials, aad_id=env_id)
    # Validate base_url the SAME way create_app does (absolute http(s) — SSRF/scheme
    # guard) when provided; empty ⇒ inherits the app's base_url at resolve time.
    base_url = (payload.base_url or "").strip()
    if base_url:
        base_url = _validated_base_url(base_url)
    env_assertion = _validated_env_assertion(payload.env_assertion)
    async with tenant_scoped_qec_session(tenant_id) as session:
        await _require_app(session, tenant_id, app_id)   # 404 if app absent/foreign
        row = ClientAppEnvironmentRow(
            environment_id=env_id,
            tenant_id=tenant_id,
            app_id=app_id,
            name=payload.name.strip()[:200],
            base_url=base_url,
            canonical_host=_derive_canonical_host(base_url, "") if base_url else "",
            cookies=list(payload.cookies or []),
            headers=dict(payload.headers or {}),
            data_overrides=dict(payload.data_overrides or {}),
            fences=dict(payload.fences or {}),
            env_attestation=dict(_validated_env_kind(payload.env_attestation) or {}),
            env_assertion=env_assertion,
            creds_blob=creds_blob,
            status="active",
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="an environment with that name already exists")
        result = _env_public_view(row)
    logger.info("qec.apps.env_created",
                extra={"tenant_id": tenant_id, "app_id": app_id, "environment_id": env_id,
                       "env_name": row.name, "has_credentials": bool(creds_blob),
                       "actor": user.get("sub", "")})
    return result


@router.get("/apps/{app_id}/environments")
async def list_environments(app_id: str, user: dict = Depends(require_auth)) -> dict:
    """List an app's Environment Profiles (no secret material)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        await _require_app(session, tenant_id, app_id)
        rows = (await session.execute(
            select(ClientAppEnvironmentRow)
            .where(ClientAppEnvironmentRow.app_id == app_id,
                   ClientAppEnvironmentRow.tenant_id == tenant_id)
            .order_by(ClientAppEnvironmentRow.created_at.asc())
        )).scalars().all()
        return {"app_id": app_id, "environments": [_env_public_view(r) for r in rows]}


@router.get("/apps/{app_id}/environments/{env_id}")
async def get_environment(app_id: str, env_id: str, user: dict = Depends(require_auth)) -> dict:
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        return _env_public_view(await _require_env(session, tenant_id, app_id, env_id))


@router.patch("/apps/{app_id}/environments/{env_id}")
async def update_environment(
    app_id: str, env_id: str, payload: EnvUpdate, request: Request,
    user: dict = Depends(_MUTATE),
) -> dict:
    """Partial update; ``credentials`` rotates the sealed blob (AAD=environment_id)."""
    tenant_id = user["tenant_id"]
    creds_blob: bytes | None = None
    if payload.credentials is not None:
        creds_blob = await _encrypt_credentials(
            request, tenant_id, app_id, payload.credentials, aad_id=env_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_env(session, tenant_id, app_id, env_id)
        if payload.name is not None and payload.name.strip() != row.name:
            # R2 guard: renaming the profile the app's cycles are BOUND to would
            # fail-close every subsequent cycle (the daemon resolves by name).
            _bound = await _bound_run_environment(session, tenant_id, app_id)
            if _bound and _bound == row.name:
                raise HTTPException(
                    status_code=409,
                    detail=(f"environment {row.name!r} is the app's bound "
                            f"run_environment — re-bind (PATCH app.run_environment) "
                            f"before renaming"))
            row.name = payload.name.strip()[:200]
        elif payload.name is not None:
            row.name = payload.name.strip()[:200]
        if payload.base_url is not None:
            _bu = payload.base_url.strip()
            row.base_url = _validated_base_url(_bu) if _bu else ""
            row.canonical_host = _derive_canonical_host(row.base_url, "") if row.base_url else ""
        if payload.cookies is not None:
            row.cookies = list(payload.cookies)
        if payload.headers is not None:
            row.headers = dict(payload.headers)
        if payload.data_overrides is not None:
            row.data_overrides = dict(payload.data_overrides)
        if payload.fences is not None:
            row.fences = dict(payload.fences)
        if payload.env_attestation is not None:
            row.env_attestation = dict(_validated_env_kind(payload.env_attestation) or {})
        if payload.env_assertion is not None:
            row.env_assertion = _validated_env_assertion(payload.env_assertion)
        if payload.credentials is not None:
            row.creds_blob = creds_blob
        if payload.status is not None:
            row.status = payload.status.strip()[:32]
        try:
            await session.flush()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="an environment with that name already exists")
        result = _env_public_view(row)
    logger.info("qec.apps.env_updated",
                extra={"tenant_id": tenant_id, "app_id": app_id, "environment_id": env_id,
                       "actor": user.get("sub", "")})
    return result


@router.delete("/apps/{app_id}/environments/{env_id}")
async def delete_environment(
    app_id: str, env_id: str, user: dict = Depends(_MUTATE),
) -> dict:
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_env(session, tenant_id, app_id, env_id)
        # R2 guard: deleting the profile the app's cycles are BOUND to would
        # fail-close every subsequent cycle. Re-bind first.
        _bound = await _bound_run_environment(session, tenant_id, app_id)
        if _bound and _bound == row.name:
            raise HTTPException(
                status_code=409,
                detail=(f"environment {row.name!r} is the app's bound "
                        f"run_environment — re-bind (PATCH app.run_environment) "
                        f"before deleting"))
        await session.delete(row)
    logger.info("qec.apps.env_deleted",
                extra={"tenant_id": tenant_id, "app_id": app_id, "environment_id": env_id,
                       "actor": user.get("sub", "")})
    return {"app_id": app_id, "environment_id": env_id, "status": "deleted"}
