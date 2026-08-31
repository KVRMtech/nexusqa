"""QE-Central — internal explorer completion callback (design §3.2 / §1.1).

``POST /internal/crawls/{crawl_id}/complete`` is the HMAC-authenticated seam the
contained explorer calls when a crawl finishes.  It:

  1. verifies the v2 signature envelope (``X-QEC-Signature``) over the RAW body:
     a KNOWN key id, a timestamp inside the skew window, a SINGLE-USE nonce, and
     a scope bound to THIS crawl — fail-closed, so an unsigned, stale, replayed,
     re-pointed or wrong-key callback is rejected (401), never trusted
     (M0.5 T-SEC-06/T-SEC-11; see ``app/security/hmac_auth.py``);
  1b. resolves the crawl id to its OWNING tenant server-side, so the body's
     ``tenant_id``/``exploration_id`` are claims to be checked, never authority
     (M0.5 T-SEC-07);
  2. locates the pending ``qe_explorations`` row (tenant-scoped, RLS);
  3. reads the staged ``manifest.jsonl`` from the shared crawl volume (path
     DERIVED from the validated crawl_id — a client path is never trusted) and
     maps it to an :class:`ExplorationBundle` via the pure manifest mapper;
  4. creates the crawl artifact + atomically writes the §2 substrate through
     the SAME ``artifacts.creator`` / ``substrate.writer`` seam the Phase-0
     inline path uses;
  5. relays the in-memory ``storageState`` (if any) to platform-api's E3
     auth-import endpoint (best-effort — a disabled/unavailable auth-import
     never discards a successful substrate write);
  6. persists an HONEST terminal state on the exploration row
     (completed | refused | failed) — refusal is first-class, never a silently
     empty artifact.

This router lives OUTSIDE the ``/api/*`` prefix so the JWT middleware does not
apply (the explorer holds no JWT — only the fleet token).  Authentication is in
TWO layers, and both are required:

  * ``app.auth.internal_auth_middleware`` gates the whole ``/internal`` PREFIX on
    the per-fleet ``X-QEC-Token``, so an anonymous request never reaches a
    handler — including a handler added later that forgets to check anything
    (M0.5 T-SEC-02);
  * each handler below then verifies the signed, fresh, single-use, crawl-scoped
    body envelope.  The token proves WHO is calling; the signature proves WHAT
    they are calling about.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..clients import factory, platform_api
from ..config import settings
from ..services import field_agent, rule_store
from ..clients.config import SIGNATURE_HEADER, phase1_settings
from ..clients.manifest_mapper import (
    ManifestMappingError,
    map_manifest_records_to_bundle,
)
from ..clients.refusal_messages import client_refusal_message
from ..db import tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppEnvironmentRow, ClientAppRow, QEExplorationRow
from ..storage import object_store
from ..substrate.schema import CRAWL_ID_PATTERN, ExplorationBundle, RefusalError
from ..substrate.writer import (
    EXTRACTOR_VERSION_PREFIX,
    validate_extractor_version,
    write_exploration,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["QEC Internal"])

_MANIFEST_FILENAME = "manifest.jsonl"
#: Terminal states that make a repeat callback a no-op (idempotency).
_TERMINAL_STATES = frozenset({"completed", "refused", "failed"})

#: M1.7 — stop reasons that mean the crawl did NOT prove what it set out to
#: prove. Mirrors ``app.crawl_constants.FAILED_STOP_REASONS`` in the explorer.
#: The two services share no library, so this list and that one must change
#: together or a new failure reason is invented on one side and read as a
#: success on the other. The contract test asserts they match.
_FAILED_STOP_REASONS = frozenset({
    "error", "auth_failed", "inventory_failed", "resume_unrecoverable",
    "no_evidence",
})


def _disposition_of(body: "CompletionCallback") -> str:
    """The terminal disposition of a completion — the explorer's if it sent one.

    An explorer that predates M1.7 sends no ``disposition``, and for it the
    reasons that existed then map exactly as they always did. A NEWER explorer
    that sends one is believed, because it adjudicated against evidence this
    service cannot see (the inventory-failure count, the resumed-state count).

    Fail-closed on an unrecognised reason: something nobody classified must never
    read as a success.
    """
    declared = (body.disposition or "").strip().lower()
    if declared in ("completed", "failed", "incomplete"):
        return declared
    reason = (body.stop_reason or "").strip().lower()
    if reason in _FAILED_STOP_REASONS:
        return "failed"
    if reason in ("cancelled", "auth_required_no_credentials"):
        return "incomplete"
    if reason == "completed" or reason.startswith("budget_"):
        return "completed"
    return "failed" if reason else "completed"


class CompletionCallback(BaseModel):
    """The explorer's completion callback body (design §3.2).

    ``storage_state`` is the in-memory Playwright session relayed for E3 import;
    it is NEVER persisted by qe-central directly (platform-api encrypts it).
    ``error`` is the explorer's honest failure reason when no usable manifest
    was produced (login failed, crawl aborted before any state).
    """

    model_config = {"extra": "ignore"}

    tenant_id: str = Field(min_length=1, max_length=64)
    exploration_id: str = Field(min_length=1, max_length=64)
    crawl_id: str = Field(min_length=1, max_length=36)
    storage_state: dict | None = None
    auth_label: str | None = Field(default=None, max_length=200)
    stop_reason: str = Field(default="", max_length=200)
    guard_events: int = Field(default=0, ge=0)
    error: str = Field(default="", max_length=2000)
    #: Crawl coverage (P4): forms_found / fields_inferred / fields_needing_seed /
    #: submit_candidates. Carried on the exploration ``stats`` so the app UI can
    #: turn "why so shallow?" into a NAMED, seed-this-field remediation list.
    coverage: dict | None = None
    #: M1.7 — the terminal DISPOSITION the explorer ADJUDICATED from evidence
    #: (``completed`` / ``failed`` / ``incomplete``; see ``app/completion.py`` in
    #: the explorer). ``stop_reason`` says WHAT happened; this says whether the
    #: crawl may be believed. Read rather than re-derived: two services each
    #: mapping a stop-reason string onto a judgement is two mappings that drift.
    #: Absent (an older explorer) ⇒ derived locally by :func:`_disposition_of`,
    #: which is byte-identical to the pre-M1.7 behaviour for every reason that
    #: existed then.
    disposition: str = Field(default="", max_length=24)
    #: The evidence the disposition was adjudicated FROM, so a completion claim
    #: can be re-checked against counts rather than trusted.
    evidence: dict | None = None
    #: True when the explorer REFUSED a completion claim for want of evidence.
    downgraded: bool = Field(default=False)


class _UsageAccumulator:
    """Sum provider-reported token usage across the calls one request makes.

    The vision endpoints reach the model through a ``propose_fn`` closure that
    returns text, so the usage on the client's result would otherwise be dropped
    at the closure boundary — the same shape of leak T-OB-03 fixed one level up.
    A vision_medic implementation may retry internally, so this ADDS rather than
    overwrites, and it records on every attempt including the failing one.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.provider = ""
        self.model = ""

    def add(self, result) -> None:
        usage = getattr(result, "usage", None)
        if usage is None or not getattr(usage, "reported", False):
            return
        self.calls += 1
        self.prompt_tokens += int(usage.prompt_tokens or 0)
        self.completion_tokens += int(usage.completion_tokens or 0)
        self.provider = self.provider or usage.provider
        self.model = self.model or usage.model

    def as_dict(self) -> dict:
        """The relay shape, or ``{}`` when nothing was reported — never zeros
        that would read as a free call."""
        if not self.calls:
            return {}
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "provider": self.provider, "model": self.model,
            "calls": self.calls,
        }


def _extractor_version(crawl_id: str) -> str:
    """Build + validate the ONE version string for the whole write (§2.3)."""
    return validate_extractor_version(f"{EXTRACTOR_VERSION_PREFIX}{crawl_id}")


def _crawl_dir(crawl_id: str) -> Path:
    """DERIVE the per-crawl directory from the validated crawl_id.

    crawl_id is pinned to ``CRAWL_ID_PATTERN`` (no separators / traversal), so
    ``{storage_root}/{crawl_id}`` is always contained — a client-supplied path
    is never used.
    """
    return Path(phase1_settings.crawl_storage_root) / crawl_id


def _read_manifest(path: Path) -> list[dict]:
    """Read all COMPLETE JSONL records; a trailing partial line is discarded
    (crash-mid-write leaves a durable, resumable prefix — emit.py contract)."""
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "qec.internal.partial_manifest_line_discarded",
                    extra={"path": str(path)},
                )
                break
    return records


def _make_screenshot_loader(crawl_dir: Path) -> Callable[[str], bytes]:
    """A path→bytes loader hardened against traversal outside the crawl dir."""
    base = crawl_dir.resolve()

    def _load(rel_path: str) -> bytes:
        candidate = (base / rel_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise ManifestMappingError(
                f"screenshot path {rel_path!r} escapes the crawl directory",
                reason="screenshot_missing_data",
            ) from exc
        if not candidate.is_file():
            raise FileNotFoundError(rel_path)
        return candidate.read_bytes()

    return _load


async def _mark(
    tenant_id: str, exploration_id: str, *, status: str, **fields,
) -> None:
    """Persist a status transition on the exploration row (own transaction, so
    an honest terminal state survives even when the write txn rolled back)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover — verified present moments earlier
            logger.error(
                "qec.internal.mark_lost_row",
                extra={"exploration_id": exploration_id, "status": status},
            )
            return
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()


async def _recorded_session(request, tenant_id: str, app_id: str) -> dict | None:
    """The session captured when the operator RECORDED the login, if any.

    Lives in the app's encrypted creds_blob (AAD=app_id), same envelope the
    dispatcher reads. Returns None on anything unexpected — a run without an auth
    profile fails HONESTLY on a logged-out page, which is far better than a
    half-decrypted state being written as if it were a real session."""
    if not app_id:
        return None
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        return None
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (await session.execute(
                select(ClientAppRow).where(
                    ClientAppRow.app_id == app_id,
                    ClientAppRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
        if row is None or not row.creds_blob:
            return None
        from nexus_sdk.security.envelope import EnvelopeBlob

        blob = EnvelopeBlob.from_bytes(row.creds_blob)
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=row.app_id.encode("utf-8"),
        )
        creds = json.loads(plaintext)
        state = (creds or {}).get("session") if isinstance(creds, dict) else None
        if isinstance(state, dict) and (state.get("cookies") or state.get("origins")):
            return state
    except Exception as exc:
        logger.warning(
            "qec.internal.recorded_session_unavailable",
            extra={"app_id": app_id, "error": str(exc)[:200]},
        )
    return None


async def _promote_latest_artifact(tenant_id: str, app_id: str, artifact_id: str) -> None:
    """Promote a freshly-recorded crawl artifact onto its registered app so a
    cycle can run against it (own transaction; best-effort — a promote failure
    NEVER fails the crawl, which already produced a valid, evidence-passing
    artifact). Without this the app keeps ``latest_artifact_id=''`` and every
    cycle 409s with 'register a crawl/exploration first'."""
    if not (app_id and artifact_id):
        return
    pending_recording: dict = {}
    pending_envs: list = []
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(ClientAppRow).where(
                        ClientAppRow.app_id == app_id,
                        ClientAppRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None and row.status != "deleted":
                row.latest_artifact_id = artifact_id
                row.updated_at = utc_now()
                # A login recorded at onboarding waits here for an artifact to exist.
                # KEPT, not consumed: one app produces many artifacts over time and
                # each needs its own recipe (they are artifact-scoped). Clearing it on
                # first use left later crawls — including the one carrying the
                # generated suite — with no way to log in as another member.
                # Re-minting is prevented by materialise_login_recipe skipping an
                # artifact that already has a recipe, not by discarding the source.
                pending_recording = dict(row.login_recording or {})
            # F6 — the Environment Profiles collected at onboarding and the runner's
            # governance registry were two disjoint lists. The routing a run must
            # APPLY (base_url, cookies, headers, env assertion) lived only here, and
            # the runner-side row had nowhere to hold it — so a cookie-selected lane
            # on a shared host silently landed on the host's default.
            pending_envs = [
                {"environment_id": e.environment_id, "name": e.name,
                 "base_url": e.base_url, "cookies": list(e.cookies or []),
                 "headers": dict(e.headers or {}),
                 "env_assertion": dict(e.env_assertion or {})}
                for e in (await session.execute(
                    select(ClientAppEnvironmentRow).where(
                        ClientAppEnvironmentRow.app_id == app_id,
                        ClientAppEnvironmentRow.tenant_id == tenant_id,
                        ClientAppEnvironmentRow.status == "active",
                    )
                )).scalars().all()
            ]
    except Exception as exc:  # pragma: no cover — promotion is best-effort
        logger.warning(
            "qec.internal.promote_failed",
            extra={"app_id": app_id, "artifact_id": artifact_id, "error": str(exc)[:300]},
        )
        return

    # Materialise AFTER the promote transaction commits: platform-api requires the
    # artifact to exist, and this must never hold a DB transaction open across an
    # HTTP call. Strictly best-effort — the crawl already produced a valid artifact
    # and its own recorded session already got it in; a recipe failure only means
    # OTHER members cannot replay the login yet.
    if pending_recording:
        try:
            await platform_api.materialise_login_recipe(
                tenant_id=tenant_id, artifact_id=artifact_id,
                recording=pending_recording,
            )
        except Exception as exc:  # pragma: no cover — never fail a good crawl
            logger.warning(
                "qec.internal.recipe_materialise_error",
                extra={"app_id": app_id, "artifact_id": artifact_id,
                       "error": str(exc)[:300]},
            )

    # Mirror each Environment Profile's NON-SECRET routing onto the artifact, so the
    # governance registry and the profiles behave as one list. Ownership stays here;
    # posture is deliberately NOT asserted, because overwriting it from onboarding
    # would silently re-open a production environment somebody had locked. Sealed
    # credentials never leave. Best-effort, exactly like the recipe above.
    for _env in pending_envs:
        try:
            await platform_api.mirror_environment_profile(
                tenant_id=tenant_id, artifact_id=artifact_id, environment=_env,
            )
        except Exception as exc:  # pragma: no cover — never fail a good crawl
            logger.warning(
                "qec.internal.env_mirror_error",
                extra={"app_id": app_id, "artifact_id": artifact_id,
                       "environment_id": _env.get("environment_id"),
                       "error": str(exc)[:300]},
            )


async def _app_answer_key(tenant_id: str, app_id: str) -> dict:
    """The app's value-oracle contract (``{outcomes, rules}``) for auto-generate,
    or ``{}``.  Best-effort — a read failure just means a body-less generate (still
    materialises the demonstrated cases from the substrate)."""
    if not app_id:
        return {}
    try:
        from ..services.answer_key import value_oracle_contract
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(ClientAppRow).where(
                        ClientAppRow.app_id == app_id,
                        ClientAppRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return {}
            return value_oracle_contract(row.answer_key)
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning("qec.internal.answer_key_read_failed",
                       extra={"app_id": app_id, "error": str(exc)[:200]})
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# M0.5 T-SEC-07 — tenant + crawl binding for the internal seam
# ═══════════════════════════════════════════════════════════════════════════
#
# THE HOLE
# ========
# Every mid-crawl endpoint below read ``tenant_id`` straight out of the request
# BODY and handed it to a service that then queried, spent LLM budget against,
# and returned data for that tenant.  A correctly-signed caller could therefore
# name ANY tenant it liked: the signature proved membership of the fleet, never
# whose crawl this was.  ``/complete`` had the same shape — it located the
# exploration by (exploration_id, body.tenant_id).
#
# THE FIX
# =======
# The crawl id is the identity.  ``_bind_crawl`` resolves it SERVER-SIDE, under
# the claimed tenant's RLS scope, and returns the row's OWN tenant/exploration/
# app.  Because the lookup runs inside the tenant GUC, a row that comes back is
# PROOF that this crawl belongs to that tenant — a mismatched claim finds
# nothing and is refused.  Everything downstream then uses the values off the
# ROW, never the ones off the body.
#
# The signature is scope-bound to the same crawl id, so a captured envelope for
# crawl A cannot be replayed against crawl B either.


class CrawlBinding:
    """The server's own answer to "whose crawl is this?"."""

    __slots__ = ("tenant_id", "exploration_id", "app_id", "status")

    def __init__(self, tenant_id: str, exploration_id: str, app_id: str,
                 status: str) -> None:
        self.tenant_id = tenant_id
        self.exploration_id = exploration_id
        self.app_id = app_id
        self.status = status


async def _bind_crawl(crawl_id: str, claimed_tenant: str) -> CrawlBinding | None:
    """Resolve ``crawl_id`` to its OWNING tenant, or ``None``.

    The lookup runs in ``claimed_tenant``'s RLS scope on purpose: a row that is
    returned is proof the claim is true, and a false claim simply finds nothing.
    That makes the check a real server-side binding rather than a comparison
    against another attacker-supplied value.
    """
    crawl_id = str(crawl_id or "").strip()
    claimed = str(claimed_tenant or "").strip()
    if not crawl_id or not claimed or not CRAWL_ID_PATTERN.match(crawl_id):
        return None
    try:
        async with tenant_scoped_qec_session(claimed) as session:
            row = (await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.tenant_id == claimed,
                    QEExplorationRow.extractor_version
                    == f"{EXTRACTOR_VERSION_PREFIX}{crawl_id}",
                ).limit(1)
            )).scalar_one_or_none()
    except Exception as exc:  # a lookup failure must never fail OPEN
        logger.error("qec.internal.crawl_binding_error crawl_id=%s error=%s",
                     crawl_id, str(exc)[:200])
        return None
    if row is None:
        return None
    return CrawlBinding(
        tenant_id=row.tenant_id, exploration_id=row.exploration_id,
        app_id=row.app_id or "", status=row.status or "",
    )


def _audit_refusal(endpoint: str, *, reason: str, **fields) -> None:
    """Structured audit for an internal-seam refusal (M0.5 §19).

    Records WHAT was refused and WHY, with the ids needed to correlate.  Never
    a token, a signature, a nonce or a body.
    """
    logger.warning(
        "qec.security.internal_refused endpoint=%s reason=%s %s",
        endpoint, reason,
        " ".join(f"{k}={v}" for k, v in sorted(fields.items())),
    )


async def _authenticate_internal(
    request: Request, endpoint: str,
) -> tuple[dict, CrawlBinding]:
    """Verify signature + resolve the crawl binding, or RAISE.

    ONE function for every mid-crawl endpoint, so a new endpoint cannot be added
    with a weaker check by accident:

      1. the v2 signature verifies over THIS body, under a live key, within the
         clock-skew window, with a nonce that has not been used, and scoped to
         ``{endpoint}:{crawl_id}``;
      2. ``crawl_id`` resolves — server-side, under the claimed tenant's RLS
         scope — to a real exploration owned by that tenant;
      3. the body's ``tenant_id`` matches the ROW's tenant.

    Returns ``(body, binding)``.  Callers use ``binding.tenant_id`` — the body's
    value is never authoritative again.
    """
    raw = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    crawl_id = str(body.get("crawl_id") or "").strip()

    # The scope is derived from the BODY's crawl id, and the signature covers
    # the body — so a caller cannot rewrite the crawl id without invalidating
    # its own signature, and cannot reuse a signature made for another crawl.
    if not phase1_settings.verify_signature(
        raw, signature, scope=f"{endpoint}:{crawl_id}",
    ):
        _audit_refusal(endpoint, reason="bad_or_replayed_signature",
                       crawl_id=crawl_id or "(absent)",
                       has_signature=bool(signature))
        raise HTTPException(status_code=401, detail="invalid or missing signature")

    if not crawl_id:
        _audit_refusal(endpoint, reason="missing_crawl_id")
        raise HTTPException(status_code=400, detail="crawl_id is required")

    claimed_tenant = str(body.get("tenant_id") or "").strip()
    binding = await _bind_crawl(crawl_id, claimed_tenant)
    if binding is None:
        _audit_refusal(endpoint, reason="crawl_not_owned_by_claimed_tenant",
                       crawl_id=crawl_id, claimed_tenant=claimed_tenant or "(absent)")
        # Deliberately indistinguishable from "no such crawl": this must not
        # become an oracle for which crawl ids exist in other tenants.
        raise HTTPException(status_code=404, detail="unknown crawl")
    if binding.tenant_id != claimed_tenant:  # pragma: no cover — RLS guarantees it
        _audit_refusal(endpoint, reason="tenant_mismatch", crawl_id=crawl_id)
        raise HTTPException(status_code=403, detail="crawl belongs to another tenant")
    return body, binding


@router.post("/pick-advance")
async def pick_advance(request: Request) -> dict:
    """Agent-assisted wizard advance: which control moves the flow forward?

    Called mid-crawl in E2E mode when the deterministic regex cannot
    identify the next-step control.  HMAC-authenticated (same shared secret
    as the completion callback).

    Three-state contract — ``{"control_index": int|null, "status":
    "picked"|"none"|"unavailable", "signature": str}``:

      * ``picked`` — index into the caller's ORIGINAL control list;
      * ``none`` — the agent's honest "nothing here advances" (the crawler
        may end the walk as covered);
      * ``unavailable`` — the decision could NOT be made (LLM failure,
        unreadable reply); the crawler must end the walk as NOT covered.

    Commit-labeled, danger, disabled and nameless controls are dropped
    server-side before the agent sees anything — a commit pick can never be
    returned to any caller.  ``unavailable`` is still HTTP 200 (best-effort
    contract): only a bad signature is an error status.
    """
    body, binding = await _authenticate_internal(request, "pick-advance")

    from ..services import advance_agent

    unavailable = {"control_index": None,
                   "status": advance_agent.STATUS_UNAVAILABLE, "signature": ""}

    controls = body.get("controls") or []
    if not controls:
        return unavailable

    decision = await advance_agent.pick_advance(
        # binding.tenant_id — the SERVER's answer, not the body's claim.
        tenant_id=binding.tenant_id,
        controls=controls,
        page_title=body.get("page_title", ""),
        page_url=body.get("page_url", ""),
    )
    # ``usage`` relays the provider-REPORTED token cost of this consultation back
    # to the explorer, which folds it into the crawl's spend record (M0.6 /
    # T-OB-03). Empty when the decision cost no LLM call (a recall hit) or the
    # provider reported nothing — never an estimate.
    return {"control_index": decision.index, "status": decision.status,
            "signature": decision.signature, "usage": decision.usage}


@router.post("/pick-value")
async def pick_value(request: Request) -> dict:
    """Rung 8 of the data ladder: ONE test value for ONE field.

    Called mid-crawl when every deterministic value rung produced nothing.
    HMAC-authenticated (same shared secret as pick-advance), and the model is
    reached ONLY through ``platform_api.complete_llm`` — the guarded wire that
    PII-scans every outbound payload. This endpoint exists precisely so the
    explorer never needs its own model client (T-SEC-12: "no second route").

    Three-state contract, mirroring pick-advance:

        {"value": str|null, "status": "answered"|"none"|"unavailable",
         "usage": {...}}

    ``none`` and ``unavailable`` are both HTTP 200 (best-effort contract): the
    field stays residue and the crawl continues. Only a bad signature errors.
    """
    body, binding = await _authenticate_internal(request, "pick-value")

    from ..services import value_agent

    decision = await value_agent.pick_value(
        # binding.tenant_id — the SERVER's answer, not the body's claim.
        tenant_id=binding.tenant_id,
        name=str(body.get("name") or ""),
        semantic_type=str(body.get("semantic_type") or ""),
        kind=str(body.get("kind") or ""),
        options=[str(o) for o in (body.get("options") or [])][:60],
        constraints=str(body.get("constraints") or "")[:400],
        section=str(body.get("section") or "")[:200],
        page_title=str(body.get("page_title") or "")[:200],
        rejection=str(body.get("rejection") or "")[:400],
    )
    return {"value": decision.value, "status": decision.status,
            "usage": decision.usage}


@router.post("/operate-control")
async def operate_control(request: Request) -> dict:
    """R3 Crawl Medic: which action might operate a stuck control?

    Called mid-crawl when the deterministic ladder is exhausted and R0 still
    reports intent-unmet.  HMAC-authenticated (same secret as pick-advance).

    Contract — ``{"action": str, "status": "proposed"|"display_only"|
    "unavailable"}``:

      * ``proposed`` — the action is from the closed vocabulary and the
        explorer should execute it through existing primitives, then R0-verify;
      * ``display_only`` — the control is read-only, skip it honestly;
      * ``unavailable`` — the medic could not determine an action.

    Every outcome is HTTP 200 (best-effort); only a bad signature is 401.
    """
    body, binding = await _authenticate_internal(request, "operate-control")

    from ..services import crawl_medic

    unavailable = {"action": "", "status": crawl_medic.STATUS_UNAVAILABLE}

    control = body.get("control") or {}
    if not control:
        return unavailable

    decision = await crawl_medic.consult_medic(
        tenant_id=binding.tenant_id,
        control=control,
        intent=body.get("intent", ""),
        ladder_results=body.get("ladder_results", []),
        page_context=body.get("page_context", {}),
    )
    # ``usage`` relays this consultation's provider-reported token cost so the
    # explorer attributes it to the crawl that spent it (M0.6 / T-OB-03).
    return {"action": decision.action, "status": decision.status,
            "usage": decision.usage}


@router.post("/vision-operate")
async def vision_operate(request: Request) -> dict:
    """R5 Vision Medic: where to click on a DOM-opaque control?

    Called mid-crawl when both the deterministic ladder AND the text medic
    are exhausted, the control is classified as DOM-opaque (canvas, unlabeled,
    iframe), and the ``QEC_CRAWL_VISION_ENABLED`` flag is ON.

    HMAC-authenticated (same secret as pick-advance / operate-control) AND
    server-side flag-gated. The flag was previously enforced ONLY at dispatch
    time — qe-central decided per crawl whether to hand the explorer a vision
    oracle — so this endpoint honoured a decision made by its CALLER and would
    answer any correctly-signed request even with vision switched off on the
    server. The signature proves WHO is asking, never WHAT they are permitted
    to spend: an explorer built from a stale image, or a crawl dispatched
    before the flag was turned off, kept billing vision calls that the operator
    had disabled. ``perceive-controls`` already gated here for exactly this
    reason; this closes the asymmetry it named.

    The explorer sends a page screenshot (base64 PNG) + the element's
    bounding box + the control's shape.

    Contract — ``{"action": str, "status": "proposed"|"display_only"|
    "unavailable", "click_x": int, "click_y": int, "reason": str}``.
    Coordinates are relative to the element bbox.

    Every outcome is HTTP 200 (best-effort); only a bad signature is 401.
    """
    body, binding = await _authenticate_internal(request, "vision-operate")

    from ..clients import platform_api
    from ..services import vision_medic

    unavailable = {"action": "", "status": vision_medic.STATUS_UNAVAILABLE,
                   "click_x": 0, "click_y": 0, "reason": ""}
    if not getattr(settings, "crawl_vision_enabled", False):
        # Fail CLOSED and say so — an unavailable medic is a documented outcome
        # the crawler already handles (it falls back to the deterministic
        # ladder), never an error and never a silent empty.
        return {**unavailable, "reason": "vision disabled"}

    control = body.get("control") or {}
    if not control:
        return unavailable

    tenant_id = binding.tenant_id
    screenshot_b64 = body.get("screenshot_b64", "")

    # The propose closure returns only text, so the call's token cost is
    # captured HERE, in a holder the closure writes on EVERY attempt — including
    # the one that then raises, because a vision call the provider billed for is
    # spend whether or not it produced a usable answer (M0.6 / T-OB-03).
    spend = _UsageAccumulator()

    # M3.1 / T-VIS-03 — the system prompt comes from the ONE authoritative table,
    # keyed by the SAME task string the spend is billed under, so the prompt and
    # the cost attribution cannot drift apart.
    task = vision_medic.TASK_VISION_MEDIC
    # M3.1 / T-VIS-05 — the redaction receipt travels beside the image and is
    # ENFORCED at the wire (``platform_api._assert_image_egress_clean``). Absent
    # or mismatched ⇒ the call is refused there, and this endpoint reports the
    # medic as unavailable, which is an outcome the crawler already handles.
    redaction = body.get("pixel_redaction") or {}

    async def _propose(prompt: str, image_b64: str) -> str:
        res = await platform_api.complete_vision(
            tenant_id=tenant_id, prompt=prompt,
            screenshot_b64=image_b64,
            system=vision_medic.system_prompt_for(task),
            task=task, redaction=redaction,
        )
        spend.add(res)
        if not res.ok:
            raise RuntimeError(res.detail or "vision LLM unavailable")
        return res.text

    decision = await vision_medic.consult_vision(
        tenant_id=tenant_id,
        control=control,
        screenshot_b64=screenshot_b64,
        element_bbox=body.get("element_bbox", {}),
        page_context=body.get("page_context", {}),
        propose_fn=_propose,
    )
    return {
        "action": decision.action,
        "status": decision.status,
        "click_x": decision.click_x,
        "click_y": decision.click_y,
        "reason": decision.reason,
        "usage": spend.as_dict(),
        # WHICH prompt this call actually ran under (T-VIS-03). Deterministic and
        # inspectable from the response, so nobody has to read a deploy to find
        # out which contract the model was given.
        "prompt": vision_medic.effective_prompt(task),
    }


@router.post("/perceive-controls")
async def perceive_controls_endpoint(request: Request) -> dict:
    """U2 Perceiver: enumerate the interactive controls + displayed outcome values
    on a DOM-opaque page (canvas / Flutter Web) from its SCREENSHOT.

    Called mid-crawl when the DOM inventory is sparse on a visibly-interactive
    page. HMAC-authenticated (same secret as vision-operate) AND flag-gated: returns
    empty unless ``QEC_CRAWL_VISION_ENABLED`` is on — the server-side enforcement
    the map flagged as missing on vision-operate. Every outcome is HTTP 200
    (best-effort); only a bad signature is 401.

    Contract — ``{"controls": [...], "displayed_values": [...]}``.
    """
    body, binding = await _authenticate_internal(request, "perceive-controls")

    empty = {"controls": [], "displayed_values": []}
    if not getattr(settings, "crawl_vision_enabled", False):
        return {**empty, "reason": "vision disabled"}

    from ..clients import platform_api
    from ..services import vision_medic

    tenant_id = binding.tenant_id
    screenshot_b64 = body.get("screenshot_b64", "")

    spend = _UsageAccumulator()

    # M3.1 / T-VIS-03 — THE CONTRADICTION THAT LIVED ON THIS LINE.
    # This endpoint sent ``system=vision_medic.SYSTEM``, the click-region prompt
    # demanding ``{"action":"click_region","x","y"}``, while
    # ``perceive_controls`` built its own ``{"controls":[…]}`` contract into the
    # user prompt. The model received two mutually exclusive output contracts on
    # every call, and ``parse_perceived`` returned empty lists for any reply that
    # obeyed the system one — so a misconfiguration presented as "vision found
    # nothing". The prompt is now selected by task from one authoritative table.
    task = vision_medic.TASK_VISION_PERCEIVE
    # M3.1 / T-VIS-05 — proof that the explorer masked the sensitive regions of
    # THESE bytes before encoding them. Enforced at the wire; absent or
    # mismatched ⇒ blocked there, and this returns honest empties.
    redaction = body.get("pixel_redaction") or {}

    async def _propose(prompt: str, image_b64: str) -> str:
        res = await platform_api.complete_vision(
            tenant_id=tenant_id, prompt=prompt,
            screenshot_b64=image_b64,
            system=vision_medic.system_prompt_for(task),
            task=task, redaction=redaction,
        )
        spend.add(res)
        if not res.ok:
            raise RuntimeError(res.detail or "vision LLM unavailable")
        return res.text

    perceived = await vision_medic.perceive_controls(
        tenant_id=tenant_id, screenshot_b64=screenshot_b64,
        page_context=body.get("page_context", {}), propose_fn=_propose)
    return {**(perceived or {}), "usage": spend.as_dict(),
            "prompt": vision_medic.effective_prompt(task)}


async def _autowalk_convergence(tenant_id: str, app_id: str) -> dict:
    """Has this app's sweep stopped learning? (see :mod:`services.convergence`)

    Reads the app's recent COMPLETED crawls and asks whether the last few each
    produced nothing beyond what was already known. Best-effort by construction:
    a failure here must never brake a sweep that is genuinely making progress, so
    an unreadable history returns not-stuck and the loop proceeds as before.
    """
    from ..services import convergence

    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            rows = (await session.execute(
                select(QEExplorationRow)
                .where(
                    QEExplorationRow.tenant_id == tenant_id,
                    QEExplorationRow.app_id == app_id,
                    QEExplorationRow.status == "completed",
                )
                .order_by(QEExplorationRow.created_at.desc())
                .limit(convergence.DEFAULT_LOOKBACK)
            )).scalars().all()
        return convergence.assess([r.stats for r in rows])
    except Exception as exc:                      # never brake on our own error
        logger.warning("qec.internal.convergence_check_failed app=%s err=%s",
                       app_id, str(exc)[:200])
        return {"stuck": False, "barren_streak": 0, "reason": "", "best": {}}


async def _settle_worker_accounting(
    tenant_id: str, exploration_id: str, crawl_id: str,
) -> None:
    """TEAM A / PHASE A — hand the WORKER's accounting back at completion.

    A completion callback means the worker's browser for this crawl is torn
    down (the explorer fires it strictly after its ``finally``). Two pieces of
    fleet state must not stay taken: the registry slot acquired at dispatch
    (``worker_id`` stamped on the row's stats) and the crawl's per-crawl egress
    fence (the generated squid ACL would otherwise keep authorising this crawl
    id's egress until the age GC). Callers invoke this only on the FIRST
    callback for a crawl — duplicates return idempotently before it — so a
    double release cannot manufacture capacity. Best-effort throughout:
    heartbeats reconcile ``in_flight`` from the worker's own count, and the
    fence age-GC is the backstop.
    """
    try:
        wid = ""
        async with tenant_scoped_qec_session(tenant_id) as session:
            stats = (await session.execute(
                select(QEExplorationRow.stats).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                ))).scalar_one_or_none()
        if isinstance(stats, dict):
            wid = str(stats.get("worker_id") or "")
        if wid:
            from ..controlplane.scheduling import worker_registry
            await worker_registry.release_slot(worker_id=wid)
        from ..controlplane.scheduling import egress_fence
        await egress_fence.release_crawl_fence_everywhere(crawl_id)
    except Exception:  # noqa: BLE001 — settlement must never fail a callback
        logger.warning("qec.internal.worker_settle_failed",
                       extra={"crawl_id": crawl_id}, exc_info=True)


@router.post("/crawls/{crawl_id}/complete")
async def complete_crawl(crawl_id: str, request: Request) -> dict:
    """Ingest a finished crawl: verify HMAC → map manifest → write substrate.

    Returns ``{status, exploration_id, artifact_id, extractor_version, stats,
    auth_import}`` on success; 401 on a bad signature; 404 for an unknown
    exploration; 422 on a refused (dishonest) manifest; 500 on infrastructure
    failure.  Every outcome is persisted on the ``qe_explorations`` row.
    """
    # ── 1) SIGNATURE: authentic, fresh, un-replayed, and bound to THIS crawl ─
    # M0.5 T-SEC-06.  The old check was HMAC(body) alone: it proved the sender
    # held the fleet secret and nothing else, so a captured ``/complete`` body +
    # signature could be POSTed again indefinitely (re-running the substrate
    # write, the auto-generate and the autowalk cascade), and a signature made
    # for one crawl authenticated a call about any other. The v2 envelope adds a
    # key id, a timestamp inside a skew window, a single-use nonce, and a scope
    # bound to this crawl id.
    raw = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not phase1_settings.verify_signature(
        raw, signature, scope=f"complete:{crawl_id}",
    ):
        _audit_refusal("complete", reason="bad_stale_or_replayed_signature",
                       crawl_id=crawl_id, has_signature=bool(signature))
        raise HTTPException(status_code=401, detail="invalid or missing callback signature")

    # ── 2) Parse + cross-check the identifiers ────────────────────────────
    try:
        body = CompletionCallback.model_validate(json.loads(raw or b"{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=f"malformed callback body: {str(exc)[:300]}")
    if body.crawl_id != crawl_id:
        raise HTTPException(status_code=400, detail="crawl_id path/body mismatch")
    if not CRAWL_ID_PATTERN.match(crawl_id):
        raise HTTPException(status_code=400, detail="invalid crawl_id")

    # ── 2b) SERVER-SIDE CRAWL BINDING (T-SEC-07) ──────────────────────────
    # The body's tenant_id and exploration_id are CLAIMS. Resolve the crawl id
    # to its owning row first; every value used from here on comes off that row.
    binding = await _bind_crawl(crawl_id, body.tenant_id)
    if binding is None:
        _audit_refusal("complete", reason="crawl_not_owned_by_claimed_tenant",
                       crawl_id=crawl_id,
                       claimed_tenant=body.tenant_id or "(absent)")
        raise HTTPException(status_code=404, detail="exploration not found")
    if binding.exploration_id != body.exploration_id:
        _audit_refusal("complete", reason="exploration_crawl_mismatch",
                       crawl_id=crawl_id, tenant=binding.tenant_id)
        raise HTTPException(
            status_code=409,
            detail="exploration_id does not belong to this crawl",
        )

    tenant_id = binding.tenant_id          # server-derived, never the body's
    exploration_id = binding.exploration_id

    # ── 3) Locate the pending exploration (tenant-scoped, RLS) ────────────
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="exploration not found")
        current_status = row.status
        row_extractor_version = row.extractor_version
        app_id = row.app_id
        # Cross-check: the version stamped at dispatch encodes the crawl_id.
        expected_crawl_id = (row_extractor_version or "").removeprefix(EXTRACTOR_VERSION_PREFIX)
        artifact_id_existing = row.artifact_id
        session_id_existing = row.session_id
        # The app's prior GOOD capture — the clobber-guard preserves it against a flaky
        # login re-crawl that would otherwise replace it with a login-only page.
        prior_artifact_id = ""
        if app_id:
            prior_artifact_id = (await session.execute(
                select(ClientAppRow.latest_artifact_id).where(
                    ClientAppRow.app_id == app_id, ClientAppRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none() or ""

    if expected_crawl_id and expected_crawl_id != crawl_id:
        raise HTTPException(
            status_code=409,
            detail="crawl_id does not match the dispatched exploration",
        )

    # Idempotency: a repeat callback on a finished crawl is a no-op.
    if current_status in _TERMINAL_STATES:
        logger.info(
            "qec.internal.duplicate_callback_ignored",
            extra={"exploration_id": exploration_id, "status": current_status},
        )
        return {
            "status": current_status,
            "exploration_id": exploration_id,
            "artifact_id": artifact_id_existing,
            "session_id": session_id_existing,
            "extractor_version": row_extractor_version,
            "idempotent": True,
        }

    # ── TEAM A / PHASE A — settle the worker's accounting exactly once ────
    # Past the idempotency gate above, so a duplicate callback can never
    # double-release a slot another crawl now holds. The worker is done with
    # this crawl whatever the verdict below turns out to be.
    await _settle_worker_accounting(tenant_id, exploration_id, crawl_id)

    # ── 3b) M1.7 · AN ADJUDICATED FAILURE IS A FAILURE ────────────────────
    # Checked BEFORE the manifest is opened, and that ordering is the whole
    # point. A crawl that could not READ a page still wrote a manifest — the
    # records it managed before the failure are real — so a manifest-exists test
    # says nothing about whether the crawl may be believed. Mapping the bundle
    # here would write a partial capture over a good prior artifact and mark the
    # row ``completed``, which is precisely the shape of green-wash this
    # milestone exists to remove: the evidence is genuine, the CLAIM about it is
    # not.
    #
    # The row is failed with the explorer's own diagnosis. The manifest is left
    # in place, untouched: it is a durable, resumable prefix, and T-GW-03 exists
    # so the crawl can be CONTINUED once the cause is fixed rather than restarted.
    if _disposition_of(body) == "failed":
        reason = (body.error
                  or f"crawl did not complete honestly (stop_reason={body.stop_reason})")
        await _mark(tenant_id, exploration_id, status="failed",
                    error=reason[:2000],
                    stats={"coverage": body.coverage or {}, "crawl_id": crawl_id,
                           "stop_reason": body.stop_reason,
                           "evidence": body.evidence or {},
                           "downgraded": bool(body.downgraded)},
                    finished_at=utc_now())
        logger.warning(
            "qec.internal.adjudicated_failure",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id,
                   "stop_reason": body.stop_reason, "downgraded": bool(body.downgraded),
                   "evidence": body.evidence or {}, "reason": reason[:300]},
        )
        return {"status": "failed", "exploration_id": exploration_id,
                "error": reason[:500], "stop_reason": body.stop_reason,
                "downgraded": bool(body.downgraded)}

    # ── 4) The explorer reported an honest failure with no usable manifest ─
    # ── M3.3 / T-FL-03 · MATERIALISE THE EVIDENCE BEFORE READING IT ──────
    # The producer (explorer) and this consumer are DIFFERENT PODS in K8s, and
    # `/work` is a pod-local emptyDir on each — so the manifest this line looks
    # for was never on this node's disk, and a completed crawl was recorded as
    # "no manifest produced". `ensure_local` is read-through: when the manifest
    # IS already local (single node, or a shared volume) it returns immediately
    # and costs nothing; otherwise it fetches from object storage.
    try:
        crawl_dir = await object_store.ensure_local(
            tenant_id, crawl_id, _crawl_dir(crawl_id))
    except object_store.EvidenceStoreError as exc:
        # A CONFIGURED store that cannot be reached must not degrade to "no
        # manifest" — that would discard a real crawl's evidence and blame the
        # crawl for an infrastructure fault.
        logger.error("qec.internal.evidence_store_unavailable",
                     extra={"crawl_id": crawl_id, "error": str(exc)[:300]})
        raise HTTPException(
            status_code=503,
            detail="crawl evidence store unavailable — refusing to record this "
                   "crawl as failed for an infrastructure fault",
        ) from exc
    manifest_file = crawl_dir / _MANIFEST_FILENAME
    if not manifest_file.is_file():
        reason = body.error or f"no manifest produced (stop_reason={body.stop_reason or 'unknown'})"
        await _mark(tenant_id, exploration_id, status="failed", error=reason[:2000], finished_at=utc_now())
        logger.warning(
            "qec.internal.no_manifest",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "reason": reason[:300]},
        )
        return {"status": "failed", "exploration_id": exploration_id, "error": reason[:500]}

    # ── 4b) CLOBBER-GUARD: a login-blocked / empty crawl must NOT overwrite a good prior
    # artifact. field_inventory reads the NEWEST version, so a flaky-login re-crawl (which
    # reaches only the login page) would replace a rich capture with login fields. When this
    # crawl captured nothing useful (auth_failed OR 0 forms) AND the app already has a good
    # artifact, record the exploration honestly (its stats still diagnose LOGIN_FAILED) but
    # SKIP the substrate write, so the prior capture survives.
    _cov = body.coverage if isinstance(body.coverage, dict) else {}
    _forms = int(_cov.get("forms_found") or 0)
    _nfields = len(_cov.get("fields_inferred") or []) + len(_cov.get("fields_needing_seed") or [])
    # Login-blocked, OR captured nothing at all (no forms AND no fields) — never a rich
    # capture. A merely form-less content page (0 forms but has fields/content) is NOT guarded.
    _degraded = (body.stop_reason or "").strip().lower() in (
        "auth_failed", "auth_required_no_credentials",
    ) or (_forms == 0 and _nfields == 0)
    if _degraded and prior_artifact_id:
        await _mark(
            tenant_id, exploration_id, status="completed",
            stats={"coverage": _cov, "stop_reason": body.stop_reason, "clobber_guarded": True},
            finished_at=utc_now(),
        )
        logger.info(
            "qec.internal.clobber_guarded",
            extra={"exploration_id": exploration_id, "app_id": app_id,
                   "stop_reason": body.stop_reason, "forms_found": _forms,
                   "preserved_artifact": prior_artifact_id},
        )
        return {
            "status": "completed", "exploration_id": exploration_id,
            "artifact_id": prior_artifact_id, "clobber_guarded": True,
            "note": "degraded crawl (login-blocked/empty) — prior good artifact preserved",
        }

    # ── 5) Map manifest → bundle → substrate (the §2 write) ───────────────
    extractor_version = _extractor_version(crawl_id)
    try:
        records = _read_manifest(manifest_file)
        loader = _make_screenshot_loader(crawl_dir)
        bundle: ExplorationBundle = map_manifest_records_to_bundle(
            records, screenshot_loader=loader,
        )
        if bundle.crawl_id != crawl_id:
            raise ManifestMappingError(
                f"manifest crawl_id {bundle.crawl_id!r} != callback crawl_id {crawl_id!r}"
            )
        created = await create_crawl_artifact(
            tenant_id=tenant_id,
            target_url=bundle.target_url,
            crawl_id=bundle.crawl_id,
            config_fingerprint=bundle.config_fingerprint,
            frame_count=int(bundle.frame_count),
            meta={
                "exploration_id": exploration_id,
                "app_id": app_id or "",
                "explorer_version": bundle.explorer_version or "",
                "created_by": "svc-qe-explorer",
                "stop_reason": body.stop_reason or "",
                "guard_events": body.guard_events,
            },
        )
        stats = await write_exploration(
            bundle,
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            session_id=created.session_id,
            extractor_version=extractor_version,
        )
    except RefusalError as exc:  # includes ManifestMappingError
        reason = str(exc)[:2000]                        # technical (logs / support / stats)
        friendly = client_refusal_message(exc.reason)   # plain-English, actionable (Fix B)
        await _mark(
            tenant_id, exploration_id, status="refused",
            error=friendly,                             # the operator READS this (portal shows row.error)
            stats={"refusal_code": exc.reason, "refusal_technical": reason},
            finished_at=utc_now(),
        )
        logger.warning(
            "qec.internal.refused",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "reason": reason[:300]},
        )
        raise HTTPException(
            status_code=422,
            detail={"refused": True, "reason": reason, "message": friendly,
                    "reason_code": exc.reason, "exploration_id": exploration_id},
        )
    except Exception as exc:
        message = str(exc)[:2000]
        await _mark(tenant_id, exploration_id, status="failed", error=message, finished_at=utc_now())
        logger.error(
            "qec.internal.write_failed",
            extra={"exploration_id": exploration_id, "crawl_id": crawl_id, "error": message[:300]},
        )
        raise HTTPException(status_code=500, detail=f"substrate write failed: {message[:500]}")

    # ── 6) Relay storageState to E3 (best-effort — never discards the write) ─
    #
    # A CRAWL and a RUN authenticate through different doors: the crawl is handed
    # `credentials.session` at dispatch, while a run reads the ARTIFACT's auth
    # profile. So a login recorded at onboarding got the crawl in and left every
    # run logged out — they failed on a nav link that only exists once signed in,
    # which reads as an application fault rather than a missing session.
    #
    # The crawler only returns a storageState when it captured one itself. When it
    # does not, fall back to the session the operator RECORDED, which is already
    # stored (encrypted) on the app. Same session, both doors.
    auth_state = body.storage_state
    auth_label = body.auth_label
    if not auth_state:
        recorded = await _recorded_session(request, tenant_id, app_id)
        if recorded:
            auth_state = recorded
            auth_label = "recorded at onboarding"

    auth_import = {"attempted": False}
    if auth_state:
        result = await platform_api.import_auth_profile(
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            storage_state=auth_state,
            label=auth_label,
        )
        auth_import = {"attempted": True, "ok": result.ok,
                       "status_code": result.status_code, "detail": result.detail,
                       "source": "crawler" if body.storage_state else "recording"}

    # ── 7) Honest terminal state ──────────────────────────────────────────
    stats_dict = stats.model_dump()
    stats_dict["auth_import"] = auth_import
    if body.coverage:  # P4: named seed-remediation surface for the app UI
        stats_dict["coverage"] = body.coverage

    # ── FIELD LEARNING (P3/P4) — what did this crawl teach us? ────────────
    # The crawl's residue is every field nothing deterministic could name. A
    # language model reads their SHAPES ONLY — label words, input type, declared
    # constraints, option count — and returns a semantic TYPE from a closed
    # vocabulary. It never sees a value and never produces one; a generator does
    # that, from a fictional identity, so every value stays reproducible and
    # provably not a real person's.
    #
    # What is learned is stored as a SHAPE against the field's signature, so the
    # next crawl of ANY application with the same field starts knowing what it is.
    # Contribution is opt-in per tenant and refused server-side when it is off.
    #
    # Entirely best-effort: this runs after the substrate is already durable, and a
    # classifier hiccup must never cost a client a valid crawl.
    learning = {"attempted": False}
    try:
        ledger = ((body.coverage or {}).get("field_ledger") or []) if body.coverage else []
        residue = [f for f in ledger
                   if isinstance(f, dict)
                   and str(f.get("semantic_type") or "unknown") == "unknown"
                   and f.get("signature")]
        if residue:
            learning["attempted"] = True
            verdicts = await field_agent.classify_fields(tenant_id, residue)
            applied = await platform_api.contribute_field_shapes(
                tenant_id=tenant_id, artifact_id=created.artifact_id,
                verdicts=verdicts,
            )
            learning.update(unnamed=len(residue), classified=len(verdicts),
                            contributed=applied)
        else:
            learning.update(unnamed=0, classified=0, contributed=0)
    except Exception as exc:                       # never fail a durable crawl
        learning = {"attempted": True, "error": str(exc)[:200]}
    stats_dict["field_learning"] = learning

    # ── ADVANCE MEMORY (Release B P4 / M2.6) — harvest PROVEN advances ───
    # EVERY advance the walk actually confirmed (real effect + new unseen
    # state) carries evidence, whichever tier decided it; folding them into
    # tenant memory makes the next crawl of the same decision point free. This
    # used to admit tier-3 only, so an application whose forward controls are
    # named "Next" contributed nothing. Consent gates contribution to the
    # shared pool; everything is best-effort after the durable write.
    try:
        from ..services import advance_memory
        stats_dict["advance_memory"] = await advance_memory.harvest_completion(
            tenant_id=tenant_id, app_id=app_id, coverage=body.coverage)
    except Exception as exc:                       # never fail a durable crawl
        stats_dict["advance_memory"] = {"error": str(exc)[:200]}

    # ── R4 MECHANIC MEMORY — harvest PROVEN interaction mechanics ─────────
    # A field-ledger entry with ``mechanic`` set means the explorer's R0 intent
    # contract verified a non-native ladder rung operated the control. Folding
    # the proven mechanic into tenant memory makes the next crawl of that
    # control instant (skip the ladder). Same consent doctrine as advance_memory.
    try:
        from ..services import mechanic_memory
        stats_dict["mechanic_memory"] = await mechanic_memory.harvest_mechanics(
            tenant_id=tenant_id, app_id=app_id, coverage=body.coverage)
    except Exception as exc:                       # never fail a durable crawl
        stats_dict["mechanic_memory"] = {"error": str(exc)[:200]}

    # ── M1.7 / T-GW-04 · THE LEARNING SURVIVES THE CRAWL ───────────────────
    # The rules this crawl proved become durable, versioned, tenant-scoped
    # knowledge that the NEXT dispatch of this app hands back to the explorer.
    # Until this call, every crawl re-ran the same experiment against the same
    # checkbox to re-derive the same fact.
    #
    # ── M2.2 · WHY THIS RUNS BEFORE THE FOLD, NOT AFTER IT ─────────────────
    # It used to sit below, just before the exploration row was marked complete,
    # which was fine while the store had exactly one reader — the NEXT dispatch.
    # T-BR-01 gave it a second: the fold builds the Master Catalog and writes
    # ``catalog_questions``, joining each question to the rule an experiment
    # proved about it. Persisting after the fold meant the crawl that PROVED a
    # rule catalogued it as UNVERIFIED and only the crawl after that could see
    # it — the durable catalogue permanently one crawl behind the evidence, and
    # an application crawled once (which is most of them, at first) showing no
    # business rules at all despite having proved several. Ordering is the whole
    # fix: nothing else changes, and the fold reads the store it now writes to.
    #
    # Best-effort, and only here: a rule store that is unreachable costs a future
    # crawl one repeated experiment, never this crawl its evidence. That is the
    # opposite of the discipline everywhere else in this milestone, and the
    # asymmetry is deliberate — a missing OPTIMISATION is not a false claim.
    rules_persisted = 0
    reuse = rule_store.reuse_metrics(body.coverage)
    try:
        rules_persisted = await rule_store.persist_rules(
            tenant_id, app_id,
            (body.coverage or {}).get("discovered_rules") or [],
            crawl_id=crawl_id,
        )
    except Exception as exc:                       # pragma: no cover - defensive
        logger.warning(
            "qec.internal.rule_persist_failed",
            extra={"exploration_id": exploration_id, "error": str(exc)[:300]},
        )
    stats_dict["rules"] = {"persisted": rules_persisted, **reuse}

    # ── JOURNEY GRAPH (Release C1/C2/C4/C5) ───────────────────────────────
    # Fold flows into the graph (idempotent upserts; unwalked options become
    # first-class ``discovered`` branch rows), name new journeys in business
    # language, reconcile a planned branch walk's outcome (planned → walked |
    # blocked, never silent), and — ONLY under the fail-closed double flags —
    # schedule the next branch walks. Best-effort throughout: the manifest
    # stays the source of truth and any crawl can be re-folded later.
    try:
        from ..services import branch_planner, journey_fold, journey_naming
        walk_plan: dict = {}
        async with tenant_scoped_qec_session(tenant_id) as session:
            row_stats = (await session.execute(
                select(QEExplorationRow.stats).where(
                    QEExplorationRow.tenant_id == tenant_id,
                    QEExplorationRow.exploration_id == exploration_id,
                ))).scalar_one_or_none()
            if isinstance(row_stats, dict):
                wp = row_stats.get("walk_plan")
                walk_plan = wp if isinstance(wp, dict) else {}
        stats_dict["journey_fold"] = await journey_fold.fold_crawl(
            tenant_id=tenant_id, app_id=app_id,
            exploration_id=exploration_id, coverage=body.coverage,
            identity_ref=str(walk_plan.get("identity_ref") or ""))
        if walk_plan:
            stats_dict["walk_plan"] = walk_plan
            stats_dict["branch_reconcile"] = await branch_planner.reconcile_completion(
                tenant_id=tenant_id, app_id=app_id, walk_plan=walk_plan,
                terminal_reason=body.stop_reason or "completed")
        stats_dict["journey_naming"] = await journey_naming.name_unnamed_journeys(
            tenant_id=tenant_id, app_id=app_id)
        # Runnable Journeys (Release D0/D1): re-derive the journey⇄case links
        # for THIS crawl's artifact — the auto-generate above just refreshed
        # the case set. Deterministic URL-path matching, no LLM.
        from ..services import journey_case_linker
        stats_dict["journey_cases"] = await journey_case_linker.link_app_journeys(
            tenant_id=tenant_id, app_id=app_id,
            artifact_id=created.artifact_id)
        # E1 AUTOWALK — the loop with explicit stop conditions: no discovered
        # branches (plan_walks returns none), the per-cycle cap (inside
        # plan_walks), flags off, or depth exceeded.  Branch walks that
        # reveal NEW decision points trigger further autowalks (recursive
        # HLQ explosion) up to ``autowalk_max_depth``.
        walk_depth = int((walk_plan or {}).get("walk_depth") or 0)
        max_depth = settings.autowalk_max_depth
        # Retire the options of any decision that has now been SHOWN to be a data
        # variation rather than a business fork, BEFORE planning the next cycle —
        # otherwise the planner keeps queueing US states and height values that
        # cannot exercise new behaviour, and the sweep never converges.
        stats_dict["branch_equivalence"] = (
            await branch_planner.classify_equivalent_options(
                tenant_id=tenant_id, app_id=app_id))
        flags = await branch_planner.autonomy_flags(tenant_id)
        # CONVERGENCE BRAKE. Live: four apps accounted for 450 of 563 crawls in
        # 18 days — one was crawled 203 times, almost all producing no new pages
        # and no new tests, and the sweep planned the next cycle every time. That
        # saturated the single-flight explorer, so REAL client crawls were refused
        # with "explorer busy" while the fleet re-discovered what it already knew.
        # An autonomous loop with no brake is not autonomy, it is a spin.
        stuck = await _autowalk_convergence(tenant_id, app_id)
        if stuck["stuck"]:
            stats_dict["journey_autowalk_stuck"] = stuck
            logger.warning(
                "qec.internal.autowalk_braked tenant=%s app=%s streak=%d — %s",
                tenant_id, app_id, stuck["barren_streak"], stuck["reason"])
        if flags["autowalk"] and walk_depth < max_depth and not stuck["stuck"]:
            from fastapi import Response as _Response
            from .explorations import _dispatch_explorer
            plans = await branch_planner.plan_walks(
                tenant_id=tenant_id, app_id=app_id)
            autowalked = []
            for plan in plans:
                plan["walk_depth"] = walk_depth + 1
                await branch_planner.mark_planned(
                    tenant_id=tenant_id, branch_ids=plan["branch_ids"])
                try:
                    dispatch = await _dispatch_explorer(
                        tenant_id=tenant_id, app_id=app_id, request=request,
                        response=_Response(), walk_plan=plan)
                    autowalked.append({
                        "journey_id": plan["journey_id"],
                        "exploration_id": dispatch.get("exploration_id")})
                except Exception as exc:
                    # BUSY (409) is back-pressure, not a finding. The explorer
                    # pool is single-flight by default, so a multi-plan cycle
                    # WILL be refused after the first dispatch; retiring those
                    # options as `blocked` would silently cap coverage at one
                    # option per cycle for every app with more than one worker's
                    # worth of work. Put them back on the backlog instead.
                    if getattr(exc, "status_code", 0) == 409:
                        await branch_planner.unmark_planned(
                            tenant_id=tenant_id, branch_ids=plan["branch_ids"])
                        autowalked.append({"journey_id": plan["journey_id"],
                                           "requeued": "explorer_busy"})
                        continue
                    await branch_planner.reconcile_completion(
                        tenant_id=tenant_id, app_id=app_id, walk_plan=plan,
                        terminal_reason="dispatch_failed")
                    autowalked.append({"journey_id": plan["journey_id"],
                                       "error": str(exc)[:200]})
            stats_dict["journey_autowalk"] = autowalked
            if walk_depth >= max_depth:
                stats_dict["journey_autowalk_depth_capped"] = True
    except Exception as exc:                       # never fail a durable crawl
        stats_dict["journey_fold"] = {"error": str(exc)[:200]}

    # ── O1 RULE AUTO-VALIDATION ─────────────────────────────────────────────
    # After the fold, journeys still in ``captured`` status have no baseline.
    # If the app's answer_key carries ``outcome_rule`` rules, evaluate them
    # against each captured journey's latest completed traversal. Rules that
    # confirm ALL applicable outcomes auto-approve the baseline — zero human
    # touch, fully auditable (action="rule_auto_approve" in the event chain).
    try:
        from ..services import rule_oracle
        from ..services.answer_key import value_oracle_contract
        from ..services.journey_baseline import auto_validate_from_rules
        rule_ak = await _app_answer_key(tenant_id, app_id)
        raw_rules = rule_ak.get("rules") if rule_ak else []
        rules = rule_oracle.normalize_rules(raw_rules)
        if rules:
            async with tenant_scoped_qec_session(tenant_id) as session:
                from ..db.journey_models import JourneyRow, JourneyTraversalRow
                captured_journeys = (await session.execute(
                    select(JourneyRow).where(
                        JourneyRow.tenant_id == tenant_id,
                        JourneyRow.app_id == app_id,
                        JourneyRow.baseline_status == "captured",
                    ))).scalars().all()
                rule_auto_results = []
                for journey in captured_journeys:
                    latest_traversal = (await session.execute(
                        select(JourneyTraversalRow).where(
                            JourneyTraversalRow.tenant_id == tenant_id,
                            JourneyTraversalRow.app_id == app_id,
                            JourneyTraversalRow.journey_id == journey.journey_id,
                            JourneyTraversalRow.completed == True,  # noqa: E712
                        ).order_by(JourneyTraversalRow.created_at.desc())
                    )).scalars().first()
                    if latest_traversal is None:
                        continue
                    results = rule_oracle.evaluate_rules(
                        rules, latest_traversal.outcome_values or [])
                    summary = rule_oracle.summarize_evaluation(results)
                    if summary["all_applicable_pass"]:
                        r = await auto_validate_from_rules(
                            tenant_id=tenant_id, app_id=app_id,
                            journey_id=journey.journey_id,
                            traversal_id=latest_traversal.traversal_id,
                            outcome_values=latest_traversal.outcome_values or [],
                            rule_results=results, rule_summary=summary)
                        rule_auto_results.append(r)
                stats_dict["rule_auto_validation"] = {
                    "rules_count": len(rules),
                    "captured_journeys": len(captured_journeys),
                    "auto_approved": len([r for r in rule_auto_results
                                          if r.get("action") == "rule_auto_approved"]),
                }
        else:
            stats_dict["rule_auto_validation"] = {"rules_count": 0}
    except Exception as exc:
        stats_dict["rule_auto_validation"] = {"error": str(exc)[:200]}

    # Promote the recorded artifact onto the app so a cycle / the portal reads it.
    await _promote_latest_artifact(tenant_id, app_id, created.artifact_id)

    # AUTO-GENERATE: a completed crawl must YIELD test cases. Without this the
    # substrate is written but factory_test_cases stays 0, so the portal's Test
    # Studio / Command Center (which gate on cases existing) look empty and the
    # crawl appears to have "done nothing" — a showstopper for the bare-crawl path
    # (only the full cycle-driver used to trigger generate). Runs BEFORE the row is
    # marked completed so its summary is persisted in ``stats.generate`` for the UI.
    # Best-effort + NON-FATAL: the substrate write above is already durable, so a
    # generate hiccup is recorded (never a lost crawl); the row still completes. The
    # app's answer_key seeds the value oracle; re-generate upserts (cycle-safe).
    generate_result: dict = {"attempted": False}
    try:
        answer_key = await _app_answer_key(tenant_id, app_id)
        summary = await factory.generate(
            tenant_id=tenant_id, artifact_id=created.artifact_id,
            answer_key=answer_key or None,
        )
        n = int(summary.get("generated") or 0)
        reason = str(summary.get("no_cases_reason") or "").strip()
        # THREE DISTINCT OUTCOMES, never one blurred one. 270 crawls recorded
        # generated: 0 with an EMPTY reason — a factory call that returned
        # nothing and was absorbed, indistinguishable on the row from an honest
        # "this crawl had no coherent flow to build from". They need opposite
        # responses: one is our bug, the other is a finding about the app.
        if n > 0:
            outcome = "generated"
        elif reason:
            outcome = "no_cases"          # honest, explained
        else:
            outcome = "unexplained"       # zero tests and nobody said why
        generate_result = {
            "attempted": True, "ok": bool(summary.get("success")),
            "outcome": outcome,
            "generated": n,
            "no_cases_reason": reason,
        }
        if outcome == "unexplained":
            generate_result["ok"] = False
            generate_result["no_cases_reason"] = (
                "the generator returned no test cases and no reason — this is a "
                "gap in the generator, not a finding about your application"
            )
            logger.warning(
                "qec.internal.generate_unexplained",
                extra={"exploration_id": exploration_id,
                       "artifact_id": created.artifact_id,
                       "summary_keys": sorted(summary.keys())[:12]},
            )
    except Exception as exc:  # never fail the callback over generation
        generate_result = {"attempted": True, "ok": False, "outcome": "error",
                           "error": str(exc)[:300]}
        logger.warning(
            "qec.internal.autogenerate_failed",
            extra={"exploration_id": exploration_id, "artifact_id": created.artifact_id,
                   "error": str(exc)[:300]},
        )
    stats_dict["generate"] = generate_result

    await _mark(
        tenant_id, exploration_id,
        status="completed",
        artifact_id=created.artifact_id,
        session_id=created.session_id,
        explorer_version=(bundle.explorer_version or "")[:100],
        stats=stats_dict,
        finished_at=utc_now(),
    )

    logger.info(
        "qec.internal.completed",
        extra={"exploration_id": exploration_id, "crawl_id": crawl_id,
               "artifact_id": created.artifact_id, "extractor_version": extractor_version,
               "auth_import_ok": auth_import.get("ok"),
               "generated": generate_result.get("generated"),
               "generate_ok": generate_result.get("ok"),
               "rules_persisted": rules_persisted,
               "rules_reused": reuse.get("rules_reused"),
               "rule_reuse_rate": reuse.get("rule_reuse_rate")},
    )
    return {
        "status": "completed",
        "exploration_id": exploration_id,
        "artifact_id": created.artifact_id,
        "session_id": created.session_id,
        "extractor_version": extractor_version,
        "stats": stats_dict,
        "auth_import": auth_import,
        "generate": generate_result,
    }
