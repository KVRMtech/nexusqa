"""QE-Central — explorations router: THE substrate-write seam (design §3.1).

``POST /api/v1/qec/explorations`` has TWO mutually-exclusive shapes:

  * **Phase-0 (inline bundle)** — the caller posts an ``ExplorationBundle``
    (a deterministic fixture or a pre-built bundle); qe-central creates the
    session+artifact rows and atomically writes the §2 substrate through
    ``substrate.writer``.  Unchanged from Phase-0.
  * **Phase-1 (explorer dispatch)** — the caller posts an ``app_id`` (no
    bundle); qe-central mints a crawl, populates the egress allowlist, and
    dispatches the contained explorer, which later calls back
    ``POST /internal/crawls/{crawl_id}/complete`` (``app/routers/internal.py``)
    with the manifest → the SAME writer runs on the mapped bundle.

Both paths persist an HONEST terminal state on the ``qe_explorations`` row and
never green-wash a broken crawl.  Status lifecycle (first-class):
    pending (dispatched) → writing → completed | failed | refused

Dependency contract (implemented in ``app.substrate`` / ``app.artifacts`` /
``app.clients``):
  * ``ExplorationBundle`` — pydantic model (``crawl_id``, ``target_url``,
    ``explorer_version``, ``config_fingerprint``, ``frame_count``).
  * ``RefusalError`` — raised on a broken evidence rule; ``str(exc)`` is honest.
  * ``write_exploration(...) -> WriteStats`` (``.model_dump()``).
  * ``create_crawl_artifact(...) -> CreatedArtifact`` (``.artifact_id`` /
    ``.session_id``).
  * ``explorer_client.dispatch_crawl(ExploreDispatchRequest) -> DispatchResult``.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..auth import require_auth, require_role
from ..clients import explorer_client
from ..clients import platform_api
from ..clients.config import phase1_settings
from ..clients.explorer_client import ExploreDispatchRequest, ExplorerDispatchError
from ..clients.refusal_messages import client_refusal_message
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppRow, QEExplorationRow
from ..fleet.lifecycle import TenantNotOperational
from ..fleet.provisioning import assert_tenant_operational_db
from ..security import prod_guard
from ..services.answer_key import explorer_fill_contract
from ..services.crawl_diagnosis import diagnose as diagnose_crawl
from ..services.exploration_planner import build_exploration_plan
from ..substrate.schema import CRAWL_ID_PATTERN, ExplorationBundle, RefusalError
from ..substrate.writer import write_exploration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Explorations"])

# page_visits.extractor_version is String(50) (034_page_visits.py:135):
# 'qec_live_v1@' (12) + uuid4 (36) = 48 — enforce the ceiling honestly.
_EXTRACTOR_VERSION_PREFIX = "qec_live_v1@"
_EXTRACTOR_VERSION_MAX = 50

# A bounded FIRST-PASS crawl budget, applied ONLY when an app has no explicit
# per-app budget configured. The explorer's own defaults are a DEEP crawl
# (200 states / 30 min wall / 5000 requests) — right for a scheduled deep pass,
# but far too long for the interactive "crawl this site and show me tests" flow:
# an unbounded ~30-min crawl reads as "broken" because Test Studio stays empty
# the whole time it runs. This ceiling returns a useful first pass in minutes;
# an app that wants a deep crawl sets its own budgets, which WIN (this only fills
# the empty default). The 5-min wall is the predictable ceiling on ANY site — a
# slow/huge site stops at the wall with a partial-but-useful artifact rather than
# grinding for half an hour.
_FIRST_PASS_BUDGET = {
    "max_states": 40,
    "max_depth": 4,
    "max_wall_ms": 300_000,  # 5 minutes
    "max_requests": 1500,
}


class ExplorationCreateRequest(BaseModel):
    """POST body — EXACTLY ONE of ``bundle`` (Phase-0) or ``app_id`` (Phase-1).

    A request carrying both, or neither, is a 422 (an ambiguous write intent
    must never silently pick a path).
    """

    # Phase-0: the bundle travels inline (R-1 direct-write seam).
    bundle: ExplorationBundle | None = None
    # Phase-1: dispatch the contained explorer for this registered app.
    app_id: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "ExplorationCreateRequest":
        has_bundle = self.bundle is not None
        has_app = bool((self.app_id or "").strip())
        if has_bundle == has_app:
            raise ValueError(
                "provide EXACTLY ONE of 'bundle' (Phase-0 inline write) or "
                "'app_id' (Phase-1 explorer dispatch)"
            )
        return self


def _extractor_version(crawl_id: str) -> str:
    """Build the ONE version string for the whole atomic write (§2.3)."""
    version = f"{_EXTRACTOR_VERSION_PREFIX}{crawl_id}"
    if len(version) > _EXTRACTOR_VERSION_MAX:
        raise HTTPException(
            status_code=422,
            detail=(
                f"crawl_id too long: extractor_version '{version[:60]}' exceeds "
                f"{_EXTRACTOR_VERSION_MAX} chars (page_visits.extractor_version cap)"
            ),
        )
    return version


async def _mark(
    tenant_id: str, exploration_id: str, *, status: str, **fields,
) -> None:
    """Persist a status transition on the exploration row (own transaction,
    so an honest terminal state survives even when the write txn rolled back)."""
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (
            await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover — row created moments earlier
            logger.error(
                "qec.explorations.mark_lost_row",
                extra={"exploration_id": exploration_id, "status": status},
            )
            return
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = utc_now()


# ─── Phase-0: inline bundle write ────────────────────────────────────────────


async def _write_inline_bundle(
    *, tenant_id: str, exploration_id: str, extractor_version: str,
    bundle: ExplorationBundle, app_id: str,
) -> dict:
    """Create the artifact + atomically write the §2 substrate (Phase-0)."""
    try:
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
            },
        )
        stats = await write_exploration(
            bundle,
            tenant_id=tenant_id,
            artifact_id=created.artifact_id,
            session_id=created.session_id,
            extractor_version=extractor_version,
        )
    except RefusalError as exc:
        reason = str(exc)[:2000]                        # technical (logs / support / stats)
        friendly = client_refusal_message(exc.reason)   # plain-English, actionable (Fix B)
        await _mark(
            tenant_id, exploration_id,
            status="refused", error=friendly,           # the operator READS this (portal shows row.error)
            stats={"refusal_code": exc.reason, "refusal_technical": reason},
            finished_at=utc_now(),
        )
        logger.warning(
            "qec.explorations.refused",
            extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                   "reason": reason[:300]},
        )
        raise HTTPException(
            status_code=422,
            detail={"refused": True, "reason": reason, "message": friendly,
                    "reason_code": exc.reason, "exploration_id": exploration_id},
        )
    except HTTPException:
        await _mark(
            tenant_id, exploration_id,
            status="failed", error="upstream HTTP error during substrate write",
            finished_at=utc_now(),
        )
        raise
    except Exception as exc:
        message = str(exc)[:2000]
        await _mark(
            tenant_id, exploration_id,
            status="failed", error=message, finished_at=utc_now(),
        )
        logger.error(
            "qec.explorations.write_failed",
            extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                   "error": message[:300]},
        )
        raise HTTPException(
            status_code=500,
            detail=f"substrate write failed: {message[:500]}",
        )

    stats_dict = stats.model_dump()
    await _mark(
        tenant_id, exploration_id,
        status="completed",
        artifact_id=created.artifact_id,
        session_id=created.session_id,
        stats=stats_dict,
        finished_at=utc_now(),
    )
    logger.info(
        "qec.explorations.completed",
        extra={
            "exploration_id": exploration_id,
            "tenant_id": tenant_id,
            "artifact_id": created.artifact_id,
            "extractor_version": extractor_version,
        },
    )
    return {
        "exploration_id": exploration_id,
        "artifact_id": created.artifact_id,
        "session_id": created.session_id,
        "extractor_version": extractor_version,
        "stats": stats_dict,
    }


# ─── Phase-1: explorer dispatch ──────────────────────────────────────────────


def _idp_domains(fences: dict) -> list[str]:
    """The operator-declared federated-login IdP domains (``fences.idp_domains``),
    cleaned.  Empty ⇒ no SSO crossing (fail-closed) — never guessed."""
    return [str(d).strip() for d in (fences.get("idp_domains") or []) if str(d).strip()]


def _allowlist_domains(base_url: str, fences: dict) -> list[str]:
    """Resolve the egress allowlist for a crawl (operator fences win).

    Uses the operator-declared ``fences.allowed_hosts`` verbatim (e.g.
    ``['.acmelife.example']``); falls back to the base_url hostname when none
    are declared.  No public-suffix guessing — the allowlist is explicit data.

    Federated login (#7): the DECLARED ``fences.idp_domains`` are appended so the
    browser can egress to the IdP during the SSO redirect (the guard separately
    requires that POST be AUTH-phase + to a declared IdP).
    """
    declared = [str(h).strip() for h in (fences.get("allowed_hosts") or []) if str(h).strip()]
    if declared:
        base = list(declared)
    else:
        host = (urlparse(base_url).hostname or "").strip().lower()
        base = [host] if host else []
    for d in _idp_domains(fences):
        if d not in base:
            base.append(d)
    return base


def _write_egress_allowlist(domains: list[str], allowlist_path: str) -> None:
    """Populate a WORKER's squid allowlist file BEFORE dispatching to that worker
    (fail-closed).

    Writes one destination domain per line to ``allowlist_path`` — the file that
    the chosen worker's squid re-reads. Each worker has its OWN file (per-worker
    egress isolation); a shared file would be raced by concurrent crawls and break
    the fence. A write failure is FATAL to the dispatch (503) — never launch a
    browser that can only reach a stale/empty allowlist, and never proceed silently.
    """
    if not domains:
        raise HTTPException(
            status_code=422,
            detail="cannot dispatch: no allowed_hosts fence and no resolvable base_url host",
        )
    from pathlib import Path

    path = Path(allowlist_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "# populated by qe-central at dispatch (fail-closed)\n" + "\n".join(domains) + "\n"
        path.write_text(body, encoding="utf-8")
    except Exception as exc:
        logger.error(
            "qec.explorations.egress_allowlist_write_failed",
            extra={"path": str(path), "error": str(exc)[:300]},
        )
        raise HTTPException(
            status_code=503,
            detail="egress allowlist unavailable — refusing to dispatch a crawl "
                   "that could not be network-fenced",
        )


async def _decrypt_credentials(request: Request, tenant_id: str, row: ClientAppRow) -> dict | None:
    """Decrypt a registered app's credentials for in-memory relay to the explorer.

    Symmetric with ``routers/apps.py::_encrypt_credentials`` (AAD=app_id).
    503 when encryption is unavailable but creds exist — never silently drop
    the login (which would produce an unauthenticated crawl masquerading as
    authenticated).
    """
    if not row.creds_blob:
        return None
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — cannot decrypt app credentials for dispatch",
        )
    from nexus_sdk.security.envelope import EnvelopeBlob

    try:
        blob = EnvelopeBlob.from_bytes(row.creds_blob)
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=row.app_id.encode("utf-8"),
        )
        creds = json.loads(plaintext)
        return creds if isinstance(creds, dict) else None
    except Exception as exc:
        logger.error(
            "qec.explorations.creds_decrypt_failed",
            extra={"app_id": row.app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="credential decryption failed")


#: Cap on a resolved session payload (mirrors the run-side auth-profile cap).
_MAX_SESSION_BYTES = 2 * 1024 * 1024

#: Host literals never fetched (SSRF): loopback + the cloud metadata endpoint.
_BLOCKED_HOOK_HOSTS = frozenset({"localhost", "metadata", "metadata.google.internal"})


def _is_safe_public_hook(url: str) -> tuple[bool, str]:
    """SSRF guard for the auth-hook URL: True only for an ``https`` URL whose host
    resolves ENTIRELY to public/global IPs.

    Fail-closed at every step. Blocks non-https, ``localhost`` / ``*.internal`` /
    ``*.local`` / the metadata hostname, and any address that is private, loopback,
    link-local (incl. 169.254.169.254 metadata), reserved, multicast, or otherwise
    non-global — for BOTH an IP-literal host and every DNS-resolved address. Pure
    apart from a DNS lookup, so the cheap scheme/literal checks are unit-testable
    with no network.  (TOCTOU/DNS-rebinding pinning is a deeper follow-up; the hook
    is operator-configured, so the resolve-and-check materially reduces the risk.)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "unparseable url"
    if parsed.scheme != "https":
        return False, "not https"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "no host"
    if host in _BLOCKED_HOOK_HOSTS or host.endswith(".internal") or host.endswith(".local"):
        return False, f"blocked host {host!r}"

    # Gather candidate IPs: an IP-literal host directly, else every DNS answer.
    candidates: list = []
    try:
        candidates.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
        except Exception as exc:
            return False, f"dns resolution failed ({str(exc)[:60]})"
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return False, "unparseable resolved address"
    if not candidates:
        return False, "no addresses"
    for ip in candidates:
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified or not ip.is_global):
            return False, f"non-public address {ip}"
    return True, "ok"


async def _resolve_session(credentials: dict | None) -> dict | None:
    """Resolve a tier-4 START-authenticated session for a login the crawler can't
    script (captcha / SSO / hardware token).  Two sources, in order:

      1. a STATIC client-provided ``session`` (a Playwright storageState dict);
      2. a client ``auth_hook`` URL — GET a FRESH storageState (https-only,
         bounded, timed-out) so an expiring session can be re-minted per crawl.

    Honest ``None`` on any problem (empty/unusable/unreachable) — the crawl then
    proceeds COLD rather than failing.  NOTE: the hook is an operator-configured
    URL; a full SSRF guard (block internal/metadata hosts) is a hardening
    follow-up — today it is https-only + size/time bounded.
    """
    if not isinstance(credentials, dict):
        return None
    static = credentials.get("session")
    if isinstance(static, dict) and (static.get("cookies") or static.get("origins")):
        return static
    hook = str(credentials.get("auth_hook") or "").strip()
    if not hook:
        return None
    safe, reason = _is_safe_public_hook(hook)
    if not safe:
        logger.warning("qec.explorations.auth_hook_rejected reason=%s", reason)
        return None
    try:
        import httpx

        # follow_redirects=False: a redirect could bounce to an internal host,
        # bypassing the SSRF check applied to the original URL.
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(hook)
        if resp.status_code == 200 and len(resp.content) <= _MAX_SESSION_BYTES:
            data = resp.json()
            if isinstance(data, dict) and (data.get("cookies") or data.get("origins")):
                logger.info("qec.explorations.auth_hook_session_resolved")
                return data
        logger.warning("qec.explorations.auth_hook_unusable status=%s", resp.status_code)
    except Exception as exc:
        logger.warning("qec.explorations.auth_hook_failed error=%s", str(exc)[:200])
    return None


def _explorer_attestation(att: dict | None) -> dict | None:
    """Map the STORED ``env_attestation`` JSONB onto the explorer's STRICT
    :class:`Attestation` shape (``extra='forbid'``): keep ONLY ``attested_by``,
    ``env_kind``, ``reset_procedure`` and ``expires_at_ms``. The stored dict carries
    extra keys (``attested_at``, ``rules_of_engagement``, ``preflight``,
    ``submit_approvals``) and an ISO ``expires_at`` — passing it raw makes the
    explorer's ``Attestation.model_validate`` RAISE (the 'bad_attestation' rejection
    that leaves the guard attestation None and blocks every submit). Here we keep the
    subset the model accepts and convert ``expires_at`` ISO → epoch millis. Returns
    ``None`` when there is no ``attested_by``. Only the FORMAT is transformed — every
    value is read verbatim from the row (no hardcoding)."""
    att = att or {}
    attested_by = str(att.get("attested_by") or "").strip()
    if not attested_by:
        return None
    out: dict = {
        "attested_by": attested_by,
        "env_kind": str(att.get("env_kind") or "").strip(),
        "reset_procedure": str(att.get("reset_procedure") or "").strip(),
    }
    _dt = prod_guard._parse_iso_utc(att.get("expires_at"))
    if _dt is not None:
        out["expires_at_ms"] = int(_dt.timestamp() * 1000)
    return out


async def _dispatch_explorer(
    *, tenant_id: str, app_id: str, request: Request, response: Response,
) -> dict:
    """Mint a crawl, fence egress, and dispatch the contained explorer (Phase-1)."""
    if not phase1_settings.dispatch_enabled:
        raise HTTPException(
            status_code=503,
            detail="explorer dispatch is disabled (QEC_EXPLORER_DISPATCH_ENABLED unset) — "
                   "enable it to run live crawls, or POST an inline bundle (Phase-0)",
        )

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
            raise HTTPException(status_code=404, detail="app not found")
        if row.status != "active":
            raise HTTPException(status_code=409, detail=f"app is not active (status={row.status})")
        # Phase-6 SAFETY SPINE — fail-closed onboarding gate on the REAL-APP crawl
        # path (this Phase-1 dispatch only; the Phase-0 inline-bundle harness path
        # never reaches here).  Even a read-only EXPLORE crawl requires a non-prod
        # attestation; refuse (409/422) unless the app is onboarding-'live'.
        try:
            prod_guard.assert_crawlable(row, phase=prod_guard.PHASE_EXPLORE)
        except prod_guard.OnboardingRefused as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
        # Phase-7 FLEET lifecycle gate — a SUSPENDED / offboarding tenant may not
        # dispatch a crawl (fail-closed).  A tenant with no control record is
        # operational (today's behavior).  Uses the open tenant-scoped session.
        try:
            await assert_tenant_operational_db(session, tenant_id, operation="crawl")
        except TenantNotOperational as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())
        base_url = row.base_url
        prior_artifact_id = row.latest_artifact_id or ""   # the LAST completed crawl
        fences = dict(row.fences or {})
        # TARGET MODE (R3 Mode 2): operator-declared path prefixes the crawl is
        # CONFINED to (schedule.scope_paths, e.g. ["/quote"]). Only well-formed
        # absolute paths pass; empty ⇒ classic whole-app Explore mode.
        scope_paths = [
            str(p).strip() for p in ((row.schedule or {}).get("scope_paths") or [])
            if str(p).strip().startswith("/")
        ][:20]
        # REFUSE A CONFIGURATION THAT CANNOT CRAWL ANYTHING. Target mode confines the
        # crawl to these prefixes, and the crawl STARTS at base_url — so if the entry
        # path is not inside the scope, the very first URL is out_of_scope at depth 0
        # and the crawl "completes" having captured nothing. That reads as success and
        # sends the operator to check whether their URL is reachable, which it is.
        # Refusing up front, naming both values, is the honest failure.
        if scope_paths:
            # STRICT: the entry must be INSIDE a scope prefix. A parent is not
            # enough — the crawler skips an out-of-scope URL at depth 0 rather than
            # walking down from it (observed: `out_of_scope depth=0` then states=0),
            # so entering above the scope captures nothing either. A "parent counts"
            # rule also makes '/' a parent of every scope and voids this guard.
            _entry = urlparse(base_url).path or "/"
            if not any(_entry.startswith(sp) for sp in scope_paths):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "the crawl scope excludes the entry point",
                        "base_url_path": _entry,
                        "scope_paths": scope_paths,
                        "reason": (
                            f"Target mode confines this crawl to {scope_paths}, but the "
                            f"Base URL enters at '{_entry}' — which is outside that scope, "
                            "so the crawl would start out-of-scope and capture nothing. "
                            f"Point the Base URL at {scope_paths[0]}, or clear the target "
                            "scope to crawl the whole app. This is a configuration "
                            "conflict, NOT an unreachable URL."
                        ),
                    },
                )
        # Project the canonical answer_key onto the explorer's {exact, semantic,
        # regex_rules} FILL contract — without this, a wizard-shaped key
        # ({fill|notes|outcomes}) resolves to empty and the crawler fills nothing.
        answer_key = explorer_fill_contract(row.answer_key)
        # Bound the FIRST-PASS crawl (no per-app budget configured) so tests appear
        # in minutes instead of after a 30-min deep default — the "new site looks
        # broken" complaint. An explicit per-app budget always wins.
        budgets = dict(row.budgets or {}) or dict(_FIRST_PASS_BUDGET)
        env_attestation = dict(row.env_attestation or {})
        # Phase-B ATTESTED SUBMIT enablement: the operator-approved flow names, from
        # the app's stored config, gated fail-closed (allow_submit + a DISPOSABLE,
        # unexpired attestation + a non-empty per-flow list). [] for an explore-only
        # app → the crawl stays at the Phase-A boundary, exactly as before.
        submit_approvals = prod_guard.submit_approvals(row)

    credentials = await _decrypt_credentials(request, tenant_id, row)
    # Tier-4: resolve a start-authenticated session (static client session or a
    # fetched auth-hook) for a login the crawler cannot script. NOTE: named
    # `auth_session` — NOT `session` — to avoid shadowing by the DB
    # `async with tenant_scoped_qec_session(...) as session` block below.
    auth_session = await _resolve_session(credentials)

    crawl_id = uuid.uuid4().hex  # 32 hex chars — matches CRAWL_ID_PATTERN, fits String(50)
    if not CRAWL_ID_PATTERN.match(crawl_id):  # pragma: no cover — uuid hex is always valid
        raise HTTPException(status_code=500, detail="generated crawl_id failed validation")
    extractor_version = _extractor_version(crawl_id)
    exploration_id = new_id()

    # Persist the pending row BEFORE dispatch so a lost callback still leaves an
    # honest, queryable record (never a silent orphan crawl). Stamp the crawl's wall
    # budget so the UI can tell a still-running crawl from a STALLED one (crashed
    # worker / lost callback) and never spin the "Crawling…" banner forever.
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(
            QEExplorationRow(
                exploration_id=exploration_id,
                tenant_id=tenant_id,
                app_id=app_id[:64],
                status="pending",
                extractor_version=extractor_version,
                started_at=utc_now(),
                stats={"budget_wall_ms": int(budgets.get("max_wall_ms") or 1_800_000)},
            )
        )

    allowed_hosts = _allowlist_domains(base_url, fences)
    # CAGED PLANNER (agent inside the cage): an LLM proposes grounded frontier-
    # priority patterns for a RE-crawl. Runs HERE (qe-central), never in the
    # quarantined explorer; the plan is pure ordering DATA the explorer applies as
    # frontier priority only. Fail-open: no prior artifact / LLM down / ungrounded
    # ⇒ empty plan ⇒ byte-identical crawl.
    plan: dict = {"priority_patterns": []}
    if phase1_settings.planner_enabled:
        plan = await build_exploration_plan(
            tenant_id, app_id, prior_artifact_id, base_url,
        )
    # FIELD LEARNING (P1/P4). What this client already told us, plus the pooled
    # value-free knowledge of what a field with a given signature is FOR. Fetched
    # HERE, never in the quarantined explorer, and fail-open: no memory means the
    # crawl fills what it can and asks for the rest, exactly as it always did.
    resolution = await platform_api.fetch_field_resolution(
        tenant_id=tenant_id, artifact_id=prior_artifact_id,
    )
    dispatch_request = ExploreDispatchRequest(
        crawl_id=crawl_id,
        tenant_id=tenant_id,
        exploration_id=exploration_id,
        target_url=base_url,
        credentials=credentials,
        answer_key=answer_key,
        budgets=budgets,
        allowed_hosts=allowed_hosts,
        idp_domains=_idp_domains(fences),
        plan=plan,
        phase="explore",
        attestation=_explorer_attestation(env_attestation),
        submit_approvals=submit_approvals,
        session=auth_session,
        scope_path_prefixes=scope_paths,
        recalled_values=resolution["recalled_values"],
        field_priors=resolution["field_priors"],
        identity_seed=resolution["identity_seed"] or f"{tenant_id}::{app_id}",
        # The operator's DATA dial, from the app row. Absent ⇒ "user", which is the
        # behaviour that existed before field learning — an unset app must never be
        # silently upgraded into letting an agent choose its business paths.
        data_mode=str((row.schedule or {}).get("data_mode") or "user").strip().lower(),
        # Absent ⇒ derived from the scope, which is exactly how mode worked before
        # this key existed: a confined crawl is Target, an unconfined one Explore.
        # Only an explicit "e2e" opts into the deeper walk.
        crawl_mode=(lambda m: m if m in ("explore", "target", "e2e")
                    else ("target" if scope_paths else "explore"))(
            str((row.schedule or {}).get("crawl_mode") or "").strip().lower()),
    )
    # Dispatch to an available WORKER in the pool. For EACH worker we fence egress
    # into THAT worker's OWN allowlist file (fail-closed) BEFORE dispatching to it —
    # per-worker isolation, so concurrent crawls never race a shared allowlist. A
    # busy (409) or unreachable (502) worker → try the next; a deterministic error
    # (config/reject) stops immediately; all-workers-unavailable is an honest,
    # retryable failure. With the default single-worker pool this is byte-identical
    # to the pre-pool path.
    workers = phase1_settings.workers()
    result = None
    last_exc: ExplorerDispatchError | None = None
    for worker in workers:
        _write_egress_allowlist(allowed_hosts, worker["allowlist_path"])
        try:
            result = await explorer_client.dispatch_crawl(
                dispatch_request, explorer_url=worker["url"],
            )
            last_exc = None
            break
        except ExplorerDispatchError as exc:
            last_exc = exc
            if exc.status_code in (409, 502):
                continue  # this worker busy/unreachable → try the next
            break  # deterministic error (token unset / bad request) — same for all
    if result is None:
        detail = str(last_exc)[:500] if last_exc else "no explorer worker available"
        await _mark(
            tenant_id, exploration_id,
            status="failed", error=detail[:2000], finished_at=utc_now(),
        )
        raise HTTPException(
            status_code=(last_exc.status_code if last_exc else 503) or 502,
            detail=detail,
        )

    response.status_code = 202
    logger.info(
        "qec.explorations.dispatched",
        extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
               "app_id": app_id, "crawl_id": crawl_id},
    )
    return {
        "exploration_id": exploration_id,
        "app_id": app_id,
        "crawl_id": crawl_id,
        "extractor_version": extractor_version,
        "status": "dispatched",
        "accepted": result.accepted,
    }


@router.post("/explorations", status_code=201)
async def create_exploration(
    payload: ExplorationCreateRequest,
    request: Request,
    response: Response,
    user: dict = Depends(require_role("admin", "manager")),
) -> dict:
    """Create session+artifact and write the crawl substrate (Phase-0), OR
    dispatch the contained explorer for a registered app (Phase-1).

    Phase-0 → 201 with ``{exploration_id, artifact_id, session_id,
    extractor_version, stats}``; Phase-1 → 202 with ``{exploration_id, app_id,
    crawl_id, extractor_version, status:'dispatched'}``.  A broken evidence
    rule is an honest 422 with the refusal reason persisted on the row.
    """
    tenant_id = user["tenant_id"]

    # Phase-1: explorer dispatch.
    if payload.bundle is None:
        return await _dispatch_explorer(
            tenant_id=tenant_id, app_id=payload.app_id.strip(),
            request=request, response=response,
        )

    # Phase-0: inline bundle write.
    bundle = payload.bundle
    exploration_id = new_id()
    extractor_version = _extractor_version(bundle.crawl_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(
            QEExplorationRow(
                exploration_id=exploration_id,
                tenant_id=tenant_id,
                app_id=(payload.app_id or "")[:64],
                status="writing",
                explorer_version=(bundle.explorer_version or "")[:100],
                extractor_version=extractor_version,
                started_at=utc_now(),
            )
        )
    return await _write_inline_bundle(
        tenant_id=tenant_id, exploration_id=exploration_id,
        extractor_version=extractor_version, bundle=bundle, app_id=payload.app_id,
    )


@router.get("/explorations/{exploration_id}")
async def get_exploration(
    exploration_id: str, user: dict = Depends(require_auth),
) -> dict:
    """Status + stats + honest error/refusal reason for one exploration."""
    tenant_id = user["tenant_id"]
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
        payload = row_to_dict(row)
        # Typed diagnosis alongside the raw row so a deep-link / poll shows the same
        # honest "what happened + what to do" the app panel does (never green-wash).
        payload["diagnosis"] = diagnose_crawl(
            status=row.status, error=row.error or "", stats=row.stats,
        )
        return payload
