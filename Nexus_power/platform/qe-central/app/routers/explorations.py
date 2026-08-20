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
from datetime import timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..artifacts.creator import create_crawl_artifact
from ..auth import require_auth, require_role
from ..clients import explorer_client
from ..clients import platform_api
from ..clients.config import phase1_settings
from ..controlplane.scheduling import queue_store, worker_registry
from ..clients.explorer_client import ExploreDispatchRequest, ExplorerDispatchError
from ..clients.refusal_messages import client_refusal_message
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppRow, QEExplorationRow
from ..fleet import quota
from ..fleet.lifecycle import TenantNotOperational
from ..fleet.provisioning import assert_tenant_operational_db
from ..security import prod_guard
from ..security.host_policy import HostPolicyError, validate_allowed_hosts
from ..services.answer_key import explorer_fill_contract
from ..services.crawl_diagnosis import diagnose as diagnose_crawl
from ..services.exploration_planner import build_exploration_plan
from ..substrate.schema import CRAWL_ID_PATTERN, ExplorationBundle, RefusalError
from ..substrate.writer import write_exploration
from nexus_sdk.session import session_has_substance

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

# END-TO-END is a DIFFERENT PROMISE. When the client asks for an E2E crawl they
# are asking for the whole application catalogued — every journey, to its end —
# and a ceiling tuned for "show me something in five minutes" silently breaks
# that promise. Observed live: an app explicitly configured crawl_mode=e2e still
# inherited the 40-state / depth-4 / 5-minute first-pass ceiling because it had
# no per-app budget, and reported a "complete" crawl of a funnel it had only
# seen the first page of.
#
# These are DELIBERATELY non-binding. The real terminator for an E2E crawl is
# the frontier running out (stop_reason=completed) or the branch backlog
# emptying — an honest "there is nothing left to walk", not an arbitrary number.
# They remain as a runaway backstop only, and an explicit per-app budget still
# wins so an operator can cap a hostile or enormous site.
#
# NOTE what is NOT relaxed here: the refuse pack (never click Delete / Pay /
# Cancel policy), the fail-closed egress allowlist, submit approvals, the auth
# window, and PII redaction. Those are not coverage limits — they are what stops
# an exhaustive crawl doing real damage to a client's real data.
#
# ``max_wall_ms`` is PER CRAWL, not per sweep. An E2E sweep is long because
# crawls CHAIN — each completion plans the next option — not because any single
# crawl runs for hours; in practice each takes about a minute. Worse, the stale
# reaper grants an in-flight crawl its whole stamped wall before declaring it
# dead, so an over-generous wall is exactly how long a crawl whose explorer died
# holds the app's one-active-crawl slot with the Crawl button disabled. 45
# minutes is far beyond any single observed crawl and still bounds that window.
_E2E_BUDGET = {
    "max_states": 5_000,
    "max_depth": 50,
    "max_wall_ms": 2_700_000,  # 45 min PER CRAWL (the sweep chains many)
    "max_requests": 100_000,
}


def _resolve_crawl_mode(row: Any, scope_paths: list, walk_plan: Any) -> str:
    """explore | target | e2e — the ONE place the mode is decided.

    A planned branch walk IS an e2e walk by definition. Otherwise the app's own
    setting wins; absent that it degrades to the pre-mode behaviour (a confined
    crawl is Target, an unconfined one Explore)."""
    if walk_plan:
        return "e2e"
    declared = str((row.schedule or {}).get("crawl_mode") or "").strip().lower()
    if declared in ("explore", "target", "e2e"):
        return declared
    return "target" if scope_paths else "explore"


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


@router.get("/fleet/crawl-funnel")
async def fleet_crawl_funnel(
    days: int = 14, user: dict = Depends(require_auth),
) -> dict:
    """C4 — WHERE CRAWLS DIE, aggregated across the tenant's fleet.

    Every crawl already writes a precise diagnosis; nothing ever aggregated them,
    so a fleet-wide collapse (weekly yield 86% -> 16%, four apps consuming 80% of
    capacity, 270 crawls generating nothing) surfaced as a founder escalation two
    months later instead of as a number that moved.

    Pure aggregation: every field read here was written by the crawl that
    produced it. A telemetry layer that derives its own numbers can disagree with
    the evidence, and then nobody trusts either.

    ``worst_stage`` is the measure-first loop in one field — rather than arguing
    about priorities, read which stage drops the most and fix that one.
    """
    from ..services import fleet_funnel

    window = max(1, min(int(days or 14), 90))
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(QEExplorationRow)
            .where(QEExplorationRow.tenant_id == tenant_id)
            .order_by(QEExplorationRow.created_at.desc())
            .limit(2000)
        )).scalars().all()

    cutoff = utc_now() - timedelta(days=window)
    recent = [
        {"app_id": r.app_id, "status": r.status, "stats": r.stats}
        for r in rows
        if r.created_at is not None and (
            r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        ) >= cutoff
    ]
    summary = fleet_funnel.summarize(recent)
    summary["window_days"] = window
    summary["worst_stage"] = fleet_funnel.worst_stage(summary)
    return summary


def _posture_shortfall_cause(env_attestation: dict, traversal: str) -> str:
    """WHY the posture fell short — the specific, actionable sentence.

    "attestation expired 2026-07-12" is fixable in thirty seconds; "traversal is
    probe" sends an operator into the code. The refusal is only worth issuing if
    it names the cause.
    """
    att = env_attestation if isinstance(env_attestation, dict) else {}
    kind = str(att.get("env_kind") or "").strip().lower()
    attested_by = str(att.get("attested_by") or "").strip()
    expires_raw = att.get("expires_at")

    if traversal == prod_guard.TRAVERSAL_OBSERVE:
        return ("The environment is attested as production, which is catalogued "
                "read-only and never driven.")
    if not att or not kind:
        return "This app carries no environment attestation."
    if not attested_by:
        return ("The environment attestation names no attester, so it is not a "
                "claim anyone has taken responsibility for.")
    expires = prod_guard._parse_iso_utc(expires_raw)
    if expires is None:
        return (f"The environment attestation has no readable expiry "
                f"({expires_raw!r}).")
    if expires <= prod_guard._utcnow():
        return (f"The environment attestation EXPIRED on "
                f"{expires.date().isoformat()}.")
    return (f"The attested env_kind {kind!r} is not one of the non-production "
            f"kinds that permit a full-traversal crawl.")


async def _mark_if_status(
    tenant_id: str, exploration_id: str, *, expect: str, status: str,
) -> str:
    """Compare-and-set the exploration status; return the status now in the row.

    Used for the dispatch transition, where an unconditional write could race a
    fast crawl's completion callback and regress a finished crawl to an earlier
    state. Only advances the row when it is still ``expect``; otherwise reports
    what it actually holds, so the caller never claims a state the crawl left.
    """
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
            return status
        if (row.status or "") == expect:
            row.status = status
            row.updated_at = utc_now()
            return status
        return row.status or status


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

    Uses the operator-declared ``fences.allowed_hosts`` (e.g.
    ``['.acmelife.example']``); falls back to the base_url hostname when none
    are declared.  No public-suffix guessing — the allowlist is explicit data.

    Federated login (#7): the DECLARED ``fences.idp_domains`` are appended so the
    browser can egress to the IdP during the SSO redirect (the guard separately
    requires that POST be AUTH-phase + to a declared IdP).

    M0.5 T-SEC-04 — DEFENCE IN DEPTH.  The write boundary
    (``routers/apps._validated_fences``) is where an unsafe host is REFUSED, so
    nothing dangerous should reach here.  We re-validate anyway, because rows
    written before that gate existed are still in the database and this is the
    last point before the value becomes squid's configuration.  A row that fails
    now is refused (422), never silently trimmed — a partially-honoured fence is
    not a fence, and an operator must be told which entry is unsafe.
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
    try:
        return validate_allowed_hosts(base, field="fences.allowed_hosts")
    except HostPolicyError as exc:
        logger.error(
            "qec.explorations.unsafe_allowlist_refused entry=%s reason=%s",
            exc.entry, exc.reason)
        raise HTTPException(
            status_code=422,
            detail={
                "refused": True,
                "reason": "unsafe_egress_allowlist",
                "entry": exc.entry,
                "message": (
                    f"This app's egress allowlist entry {exc.entry!r} is unsafe: "
                    f"{exc.reason}. Fix fences.allowed_hosts on the app before "
                    "crawling — dispatching would hand the browser a fence that "
                    "does not fence."
                ),
            },
        )


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


#: The process ``EnvelopeService``, shared with the QUEUE DRAINER (M3.3 /
#: T-FL-01). A queued crawl is dispatched by a background daemon, which has no
#: ``Request`` to read ``app.state`` from — the same seam the cycle driver
#: already uses (``driver.set_control_plane_envelope``). Unset ⇒ a queued crawl
#: that needs credentials fails honestly with the existing 503 rather than
#: dispatching an UNAUTHENTICATED crawl that would masquerade as a logged-in one.
_DISPATCH_ENVELOPE = None


def set_dispatch_envelope(envelope) -> None:
    """Share the process EnvelopeService with the queue drainer (called once
    from main's lifespan)."""
    global _DISPATCH_ENVELOPE
    _DISPATCH_ENVELOPE = envelope


def _envelope_for(request: "Request | None"):
    """The envelope service for this dispatch.

    Prefers the live request's ``app.state`` (the interactive path, unchanged);
    falls back to the process-shared instance for a daemon-driven dispatch,
    which has no request at all.
    """
    if request is not None:
        found = getattr(request.app.state, "envelope_service", None)
        if found is not None:
            return found
    return _DISPATCH_ENVELOPE


async def _decrypt_credentials(envelope, tenant_id: str, row: ClientAppRow) -> dict | None:
    """Decrypt a registered app's credentials for in-memory relay to the explorer.

    Symmetric with ``routers/apps.py::_encrypt_credentials`` (AAD=app_id).
    503 when encryption is unavailable but creds exist — never silently drop
    the login (which would produce an unauthenticated crawl masquerading as
    authenticated).
    """
    if not row.creds_blob:
        return None
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
    if session_has_substance(static):
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
            # Canonical substance rule — this branch previously omitted
            # __nx_session_storage and so discarded a freshly hook-fetched
            # sessionStorage-only session (the same bug fixed elsewhere).
            if session_has_substance(data):
                logger.info("qec.explorations.auth_hook_session_resolved")
                return data
        logger.warning("qec.explorations.auth_hook_unusable status=%s", resp.status_code)
    except Exception as exc:
        logger.warning("qec.explorations.auth_hook_failed error=%s", str(exc)[:200])
    return None


def resolve_crawl_observe_only(
    env_attestation: dict | None, fences: dict | None,
) -> tuple[bool, str]:
    """The crawl path's OWN observe-only decision (M0.5 T-SEC-05).

    Returns ``(observe_only, env_kind)``.

    WHY IT LIVES HERE
    =================
    ``security.prod_guard.resolve_effective_fences`` already forces
    ``observe_only`` for a production environment — but ONLY on the multi-env
    ``env_resolver`` path, which a plain single-env crawl never travels.  So the
    invariant "a non-disposable environment is never mutated" was enforced by a
    configuration-resolution helper that the actual dispatch did not call: the
    dispatch read ``fences.get("observe_only")`` and nothing else.  An app whose
    fences simply lacked the key was dispatched free to type, fill, submit and
    advance against whatever it was pointed at.

    The rule, fail-closed:
      * an explicit ``fences.observe_only`` is honoured (a floor, never lowered);
      * mutation is permitted ONLY when the attested ``env_kind`` is
        ``disposable`` — the same environment class that already gates SUBMIT;
      * an absent, blank or unrecognised ``env_kind`` is treated as production.

    The explorer re-derives this independently from the attestation it receives
    (``main.resolve_observe_only``), so a manipulated dispatch cannot lower it.
    """
    att = dict(env_attestation or {})
    fen = dict(fences or {})
    env_kind = str(att.get("env_kind") or "").strip().lower()
    if bool(fen.get("observe_only")):
        return True, env_kind
    return env_kind != prod_guard.ENV_KIND_DISPOSABLE, env_kind


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


async def _release_registry_slot(worker_id: str) -> None:
    """Hand a registry slot back, best-effort.

    NEVER raises into the dispatch path: a metrics/accounting failure must not
    convert a recoverable dispatch error into a 500. A slot that leaks despite
    this is reconciled by the worker's next heartbeat, which reports its OWN
    in-flight count and is the authority on what it is actually running.
    """
    if not worker_id:
        return
    try:
        await worker_registry.release_slot(worker_id=worker_id)
    except Exception as exc:  # pragma: no cover - accounting must never raise
        logger.warning("qec.explorations.slot_release_failed",
                       extra={"worker_id": worker_id, "error": str(exc)[:200]})


async def _dispatch_explorer(
    *, tenant_id: str, app_id: str,
    request: Request | None = None, response: Response | None = None,
    walk_plan: dict | None = None, resume_from: dict | None = None,
    from_queue: bool = False,
) -> dict:
    """Mint a crawl, fence egress, and dispatch the contained explorer (Phase-1).

    ``walk_plan`` (Journey Graph C4) is a PLANNED BRANCH WALK: ``{journey_id,
    branch_ids, choice_overrides {signature → option label}, identity_ref}``.
    When present the dispatch forces ``crawl_mode='e2e'``, threads the choice
    overrides to the explorer's fill (enumerable options only, ``planned``
    provenance), and stamps the plan onto the pending exploration row's stats
    so the completion fold can attribute the traversal and reconcile branch
    statuses. Every safety gate is identical to an ordinary crawl."""
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
        # M3.4 / T-RS-03 — THE PER-TENANT QUOTA, ENFORCED ON THE DISPATCH PATH.
        # Quota enforcement was real but wired only into ``run_cycle``, so the
        # SCHEDULED door was capped and this one — the direct crawl dispatch —
        # was not. A tenant sitting at its monthly browser-second ceiling could
        # still saturate the fleet by POSTing crawls, because no cycle row was
        # ever created for the cap to see.
        #
        # THIS IS THE CHOKE POINT, and that is the whole point of putting it
        # here rather than on the two routes above: ``create_exploration`` and
        # ``resume_exploration`` both funnel through this function, and a crawl
        # cannot reach a worker without passing it. A future route that dispatches
        # a crawl inherits the cap by construction instead of by review.
        #
        # BEFORE the worker is reserved and BEFORE the egress fence is written:
        # a refused tenant must not consume a fleet slot or leave an allowlist
        # file behind on a worker it was never allowed to use.
        try:
            await quota.enforce_crawl_quota(tenant_id, session=session)
        except quota.QuotaExceeded as exc:
            raise HTTPException(status_code=exc.status_code,
                                detail=exc.as_http_detail())
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
        # E2E means "catalogue the whole application", so it must NOT inherit the
        # interactive first-pass ceiling. An explicit per-app budget still wins
        # over both.
        crawl_mode = _resolve_crawl_mode(row, scope_paths, walk_plan)
        budgets = dict(row.budgets or {}) or dict(
            _E2E_BUDGET if crawl_mode == "e2e" else _FIRST_PASS_BUDGET)
        env_attestation = dict(row.env_attestation or {})
        # Phase-B ATTESTED SUBMIT enablement: the operator-approved flow names, from
        # the app's stored config, gated fail-closed (allow_submit + a DISPOSABLE,
        # unexpired attestation + a non-empty per-flow list). [] for an explore-only
        # app → the crawl stays at the Phase-A boundary, exactly as before.
        submit_approvals = prod_guard.submit_approvals(row)
        # A4.3 / T-AC-02 — the PER-CONTROL approval seam, carried through
        # unchanged from the app's stored config. This is the grant the explorer
        # needs to cross an irreversible boundary at least privilege; without it
        # the only route was `submit_approvals == ["*"]`, which authorises every
        # submit the application offers. prod_guard applies the same fail-closed
        # gate as the label list (signed RoE + a valid attestation), so an app
        # that may not submit at all still gets [].
        boundary_approvals = prod_guard.boundary_approvals(row)
        # TRAVERSAL POSTURE — how far this crawl may walk a business journey.
        # Derived from the attestation the operator already signed, so a test
        # environment does not need a second dial set by hand. Never a safety
        # dial: the refuse pack, the danger gate and the disposable-only submit
        # tier are unchanged and re-checked at click time by the explorer.
        traversal = prod_guard.traversal_posture(row)
        # DATA MODE — who answers a question the client never answered for us.
        #
        # "user" leaves every semantic CHOICE (a radio group, a select) unanswered
        # and files it as residue for a human to supply, after which the crawl has
        # to be RUN AGAIN. On an attested test environment that default is
        # backwards: it turns a missing value — the most ordinary thing a crawl
        # meets — into a stop-and-ask-and-restart cycle, which is not what an
        # agentic platform should do with a form it can honestly answer.
        #
        # So an attested test environment answers by default. An operator who has
        # explicitly chosen "user" still gets "user"; nothing is overridden, and a
        # posture the operator never attested is untouched. Honesty is preserved
        # by PROVENANCE, not by refusing to answer: every generated value is
        # recorded as synthesized in the field ledger, so a journey completed with
        # invented data is a valid traversal and a clearly-labelled one.
        declared_data_mode = str((row.schedule or {}).get("data_mode") or "").strip().lower()
        data_mode = declared_data_mode or (
            "agent" if traversal == prod_guard.TRAVERSAL_FULL else "user")

        # E2E DOES NOT SILENTLY DEGRADE.
        #
        # Posture is derived from the attestation, so an expired or downgraded
        # one resolves traversal full → probe. Everything downstream then
        # quietly follows: agent data-fill turns off, the wizard budget drops to
        # a six-step probe, and the deeper advance tiers never run — while the
        # crawl still reports "completed" and the operator, who explicitly asked
        # for END-TO-END, has no way to see they were given a shallow walk.
        #
        # That is the silent-degradation failure this product exists to refuse.
        # A ten-second refusal naming the cause is worth more than a 45-minute
        # crawl that answers a question nobody asked. The operator can re-attest
        # and re-run; they cannot un-believe a green "completed".
        #
        # Scoped to an EXPLICIT e2e request: a planned branch walk (which forces
        # e2e by definition) and an app that never asked for it are untouched.
        if (crawl_mode == "e2e" and not walk_plan
                and traversal != prod_guard.TRAVERSAL_FULL):
            cause = _posture_shortfall_cause(env_attestation, traversal)
            logger.warning(
                "qec.explorations.e2e_refused_degraded tenant=%s app=%s "
                "traversal=%s cause=%s", tenant_id, app_id, traversal, cause)
            raise HTTPException(
                status_code=422,
                detail={
                    "refused": True,
                    "reason": "e2e_posture_unavailable",
                    "requested": {"crawl_mode": "e2e", "traversal": "full"},
                    "actual": {"traversal": traversal},
                    "message": (
                        f"End-to-end was requested but this app resolves to "
                        f"'{traversal}' traversal, which walks a sampled probe "
                        f"rather than whole journeys and disables agent "
                        f"data-fill. {cause} Re-attest the environment and run "
                        f"again — running now would report 'completed' for a "
                        f"crawl that never attempted what you asked for."
                    ),
                },
            )

    # M0.5 T-SEC-05 — resolve the mutation posture BEFORE anything is dispatched.
    # Non-disposable ⇒ observation only: no typing, no filling, no submitting, no
    # commit advance. Recorded on the pending row so a client reading "completed"
    # can see WHY the crawl catalogued rather than walked.
    observe_only, crawl_env_kind = resolve_crawl_observe_only(env_attestation, fences)
    if observe_only:
        logger.info(
            "qec.explorations.observe_only tenant=%s app=%s env_kind=%s",
            tenant_id, app_id, crawl_env_kind or "(unattested)")

    credentials = await _decrypt_credentials(
        _envelope_for(request), tenant_id, row)
    # Tier-4: resolve a start-authenticated session (static client session or a
    # fetched auth-hook) for a login the crawler cannot script. NOTE: named
    # `auth_session` — NOT `session` — to avoid shadowing by the DB
    # `async with tenant_scoped_qec_session(...) as session` block below.
    auth_session = await _resolve_session(credentials)

    # ── M1.7 / T-GW-03 · A RESUME IS NOT A NEW CRAWL ──────────────────────
    # THE REASON RESUME WAS UNREACHABLE. Every dispatch minted a fresh
    # ``uuid.uuid4().hex``, and the explorer derives its work directory — and
    # therefore its durable manifest — from the crawl id. So a "resumed" crawl
    # looked for a manifest under an id that had never been written, found
    # nothing, and walked the application from zero. The recovery machinery in
    # the engine was real and simply could not be addressed.
    #
    # A resume therefore reuses BOTH ids: the crawl id (so the worker opens the
    # manifest it is meant to continue) and the exploration id (so the evidence
    # is not split across two rows that each hold half a crawl). The row
    # transitions back to ``dispatched``; nothing is deleted, and the manifest is
    # appended to, never truncated.
    resuming = bool(resume_from)
    if resuming:
        crawl_id = str((resume_from or {}).get("crawl_id") or "")
        exploration_id = str((resume_from or {}).get("exploration_id") or "")
        if not (crawl_id and exploration_id):   # pragma: no cover — caller-validated
            raise HTTPException(status_code=500, detail="resume target is incomplete")
    else:
        crawl_id = uuid.uuid4().hex  # 32 hex chars — matches CRAWL_ID_PATTERN, fits String(50)
        exploration_id = new_id()
    if not CRAWL_ID_PATTERN.match(crawl_id):  # pragma: no cover — uuid hex is always valid
        raise HTTPException(status_code=500, detail="generated crawl_id failed validation")
    extractor_version = _extractor_version(crawl_id)

    # Persist the pending row BEFORE dispatch so a lost callback still leaves an
    # honest, queryable record (never a silent orphan crawl). Stamp the crawl's wall
    # budget so the UI can tell a still-running crawl from a STALLED one (crashed
    # worker / lost callback) and never spin the "Crawling…" banner forever.
    pending_stats: dict = {
        "budget_wall_ms": int(budgets.get("max_wall_ms") or 1_800_000),
        # The worker-side job id. Persisted so the stale reaper can ASK the
        # explorer whether this crawl is still alive instead of waiting out its
        # whole wall budget: the explorer is single-flight, so a crawl whose
        # worker died holds the slot for EVERY app, not just this one, and the
        # portal's Crawl button stays disabled fleet-wide until it clears.
        "crawl_id": crawl_id,
        # T-SEC-05 evidence: the posture this crawl actually ran under.
        "observe_only": observe_only,
        "env_kind": crawl_env_kind,
        # M3.3 / T-FL-01 — the host this crawl occupies, so the per-host
        # concurrency cap can COUNT what is actually in flight. Without it the
        # queue's politeness cap has nothing to count and silently never fires.
        "target_host": (urlparse(base_url).hostname or "").strip().lower(),
    }
    # B2 — WHAT WAS ASKED FOR vs WHAT WILL ACTUALLY RUN, recorded at dispatch.
    # B1 refuses the worst case (an explicit e2e that cannot run at full
    # posture), but softer downgrades remain legitimate and must still be
    # visible: an app that never asked for e2e still resolves a posture and a
    # data mode, and a client reading "completed" has no way to know the crawl
    # sampled rather than walked. Absent when nothing was downgraded, so its
    # PRESENCE is the signal — a consumer never has to interpret an empty value.
    degraded: dict[str, Any] = {}
    if traversal != prod_guard.TRAVERSAL_FULL:
        degraded["traversal"] = {
            "requested": "full" if crawl_mode == "e2e" else "(app default)",
            "actual": traversal,
            "cause": _posture_shortfall_cause(env_attestation, traversal),
        }
    if data_mode != "agent":
        degraded["data_mode"] = {
            "requested": declared_data_mode or "(unset)",
            "actual": data_mode,
            "cause": (
                "an operator set data_mode=user, so semantic choices are left "
                "for a human to answer"
                if declared_data_mode == "user" else
                "agent fill is enabled only on an attested non-production "
                "environment"
            ),
        }
    if degraded:
        pending_stats["degraded"] = degraded
        logger.warning(
            "qec.explorations.degraded tenant=%s app=%s degraded=%s",
            tenant_id, app_id, sorted(degraded.keys()))
    if walk_plan:
        # The plan is evidence: WHY this crawl exists, WHICH branches it was
        # sent to walk, and AS WHOM — read back by the completion fold
        # (traversal identity_ref) and the branch reconciler (planned →
        # walked | blocked, never silent).
        pending_stats["walk_plan"] = {
            "journey_id": str(walk_plan.get("journey_id") or "")[:64],
            "branch_ids": [str(b)[:64] for b in (walk_plan.get("branch_ids") or [])][:32],
            "choice_overrides": {
                str(k)[:64]: str(v)[:80]
                for k, v in (walk_plan.get("choice_overrides") or {}).items()},
            "identity_ref": str(walk_plan.get("identity_ref") or "")[:200],
            # How deep in the autowalk cascade this crawl already is. MUST be
            # persisted: the completion handler reads it back to decide whether
            # to recurse, so dropping it here made every branch walk look like a
            # fresh depth-0 crawl and the loop never terminated — observed live,
            # re-planning every ~2.5 minutes and burning the branch backlog.
            "walk_depth": int(walk_plan.get("walk_depth") or 0),
        }
    if resuming:
        # RE-ARM the existing row rather than inserting a second one. Inserting
        # would leave two rows describing one crawl id, and every consumer that
        # asks "what happened to this exploration" would get two different
        # answers depending on which it found first.
        #
        # ``_mark`` REPLACES ``stats`` wholesale (it setattrs the field), so the
        # replacement must be complete: ``pending_stats`` is rebuilt above with
        # the wall budget, the crawl id the reaper's liveness probe reads, and
        # the posture — and the resume counter is carried forward explicitly
        # below rather than relying on a merge that does not happen.
        prior_resumes = 0
        async with tenant_scoped_qec_session(tenant_id) as session:
            existing = (await session.execute(
                select(QEExplorationRow).where(
                    QEExplorationRow.exploration_id == exploration_id,
                    QEExplorationRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                prior_resumes = int((existing.stats or {}).get("resumes") or 0)
        pending_stats["resumes"] = prior_resumes + 1
        pending_stats["resumed_at"] = utc_now().isoformat()
        await _mark(tenant_id, exploration_id, status="pending",
                    stats=pending_stats, error="", finished_at=None,
                    started_at=utc_now())
        logger.warning(
            "qec.explorations.resuming tenant=%s app=%s crawl_id=%s attempt=%d — "
            "the worker CONTINUES this crawl id; it does not start a new crawl",
            tenant_id, app_id, crawl_id, prior_resumes + 1)
    else:
        async with tenant_scoped_qec_session(tenant_id) as session:
            session.add(
                QEExplorationRow(
                    exploration_id=exploration_id,
                    tenant_id=tenant_id,
                    app_id=app_id[:64],
                    status="pending",
                    extractor_version=extractor_version,
                    started_at=utc_now(),
                    stats=pending_stats,
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
        tenant_id=tenant_id, artifact_id=prior_artifact_id, app_id=app_id,
    )
    # ── M1.7 / T-GW-04 · WHAT EARLIER CRAWLS OF THIS APP PROVED ───────────
    # Business rules the engine discovered by experiment and, until this
    # milestone, threw away — so every crawl re-ran the identical experiment to
    # re-derive them. Fetched HERE, never in the quarantined explorer, exactly as
    # field memory and mechanic memory are. Fail-open: no rules ⇒ every blocked
    # advance runs the full experiment, which is byte-identical to the behaviour
    # before this existed.
    from ..services import rule_store
    known_rules = await rule_store.fetch_rules(tenant_id, app_id)
    if known_rules:
        logger.info(
            "qec.explorations.rules_recalled tenant=%s app=%s rules=%d",
            tenant_id, app_id, len(known_rules))

    # R4 MECHANIC MEMORY — proven ladder rungs for this tenant's controls.
    # Fail-open: no memory → full ladder walk, exactly as before.
    from ..services import mechanic_memory
    proven_mechanics = await mechanic_memory.recall_all(tenant_id, app_id)
    # GAP-2 fix: supplement with cross-tenant pooled priors (value-free).
    # Tenant-specific mechanics always win on collision (setdefault).
    pooled_mechanics = await mechanic_memory.recall_all_priors()
    for sig, mech in pooled_mechanics.items():
        proven_mechanics.setdefault(sig, mech)
    # U2 — vision autonomy for this tenant (fail-closed double-gate: env
    # QEC_CRAWL_VISION_ENABLED AND the tenant's vision_enabled flag). Default OFF.
    try:
        from ..services import branch_planner as _bp
        _vision_enabled = bool((await _bp.autonomy_flags(tenant_id)).get("vision"))
    except Exception:
        _vision_enabled = False
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
        boundary_approvals=boundary_approvals,
        session=auth_session,
        scope_path_prefixes=scope_paths,
        recalled_values=resolution["recalled_values"],
        field_priors=resolution["field_priors"],
        # T-FE-04 · STABLE ACROSS CRAWLS.  The resolution now returns an
        # APPLICATION-scoped seed; the fallback was already app-scoped, which is
        # why this line looked correct while the value it usually received —
        # "tenant::artifact" — changed the applicant on every single re-crawl.
        identity_seed=resolution["identity_seed"] or f"{tenant_id}::{app_id}",
        # The operator's DATA dial, from the app row. Absent ⇒ "user", which is the
        # behaviour that existed before field learning — an unset app must never be
        # silently upgraded into letting an agent choose its business paths.
        data_mode=data_mode,
        # Absent ⇒ derived from the scope, which is exactly how mode worked before
        # this key existed: a confined crawl is Target, an unconfined one Explore.
        # Only an explicit "e2e" opts into the deeper walk — and a planned
        # branch walk IS an e2e walk by definition.
        crawl_mode=crawl_mode,
        traversal=traversal,
        # M1.7 — continue an existing crawl (T-GW-03) and consume what earlier
        # crawls of this app proved (T-GW-04).
        resume=resuming,
        known_rules=known_rules,
        vision_enabled=_vision_enabled,
        choice_overrides=(dict((walk_plan or {}).get("choice_overrides") or {})
                          if walk_plan else {}),
        proven_mechanics=proven_mechanics,
        # M0.5 T-SEC-05 — decided HERE, in the crawl path, not inherited from an
        # unrelated config resolver. ``env_kind`` travels with it so the explorer
        # can reach the same verdict independently and refuse to be talked down.
        observe_only=observe_only,
        env_kind=crawl_env_kind,
    )
    # Dispatch to an available WORKER in the pool. For EACH worker we fence egress
    # into THAT worker's OWN allowlist file (fail-closed) BEFORE dispatching to it —
    # per-worker isolation, so concurrent crawls never race a shared allowlist. A
    # busy (409) or unreachable (502) worker → try the next; a deterministic error
    # (config/reject) stops immediately; all-workers-unavailable is an honest,
    # retryable failure. With the default single-worker pool this is byte-identical
    # to the pre-pool path.
    # ── M3.3 / T-FL-01 · PRE-ADMISSION (declared concurrency caps) ───────
    # Before touching a worker, ask the PURE admission core whether this tenant
    # is already at its concurrency cap (or at its per-host politeness cap). A
    # crawl over cap is QUEUED, never rejected — and when no cap is configured
    # the verdict is always ADMIT, so an un-provisioned tenant behaves exactly
    # as it did before this milestone. This is what stops one tenant flooding
    # 20 crawls from consuming the whole fleet before another tenant's single
    # crawl is even considered.
    _verdict, _cap_reason = await queue_store.admission_verdict(
        tenant_id=tenant_id, host=(urlparse(base_url).hostname or "").lower())
    if _verdict == queue_store.QUEUE:
        _pos = await queue_store.enqueue(
            tenant_id=tenant_id, exploration_id=exploration_id,
            reason=_cap_reason,
            detail=f"tenant is at its configured concurrency cap ({_cap_reason})",
        )
        try:
            from ..observability import metrics as _metrics
            _metrics.record_crawl_queued(reason=_cap_reason)
        except Exception:  # pragma: no cover — metrics never break dispatch
            pass
        logger.warning(
            "qec.explorations.queued_at_cap",
            extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                   "reason": _cap_reason, "position": _pos})
        if response is not None:
            response.status_code = 202
        return {
            "exploration_id": exploration_id, "app_id": app_id,
            "crawl_id": crawl_id, "extractor_version": extractor_version,
            "status": queue_store.STATUS_QUEUED, "queue_position": _pos,
            "queued_reason": _cap_reason, "accepted": False,
        }

    # M3.3 / T-FL-02 — the WORKER REGISTRY replaces static-pool order. Workers
    # are tried LEAST-LOADED-ELIGIBLE first instead of in fixed array order, so
    # worker[0] no longer absorbs every dispatch attempt and a stale worker is
    # never offered work at all. An empty/unreadable registry falls back to the
    # static QEC_EXPLORER_POOL, which is byte-identical to the previous path.
    workers, worker_source = await worker_registry.schedulable_workers(
        tenant_id=tenant_id)
    _now = worker_registry.utc_now()
    _ttl = worker_registry.heartbeat_ttl_seconds()
    ranked: list[dict] = []
    _pool = list(workers)
    while True:
        pick = worker_registry.choose_worker(
            _pool, tenant_id=tenant_id, now=_now, ttl_s=_ttl)
        if pick is None:
            break
        ranked.append(pick)
        _pool = [w for w in _pool if w.get("worker_id") != pick.get("worker_id")]
    if not ranked:
        # Nothing eligible: record WHY (busy vs dead vs parked vs foreign) so the
        # queued crawl carries a real cause instead of "unavailable".
        no_worker_reason = worker_registry.explain_unavailable(
            workers, tenant_id=tenant_id, now=_now, ttl_s=_ttl)
    else:
        no_worker_reason = ""
    result = None
    last_exc: ExplorerDispatchError | None = None
    claimed_worker_id = ""
    for worker in ranked:
        # ── M0.5 T-SEC-03: RESERVE FIRST, FENCE SECOND ────────────────────
        # 1. authenticate (done: require_role on the route)
        # 2. resolve tenant  (done: user["tenant_id"])
        # 3. resolve crawl   (done: the pending exploration row above)
        # 4. reserve the worker ATOMICALLY  ← must precede the fence write
        # 5. ownership established (the worker records THIS tenant)
        # 6. write THIS worker's own allowlist
        # 7. dispatch
        #
        # The old order was 6 → 7 → discover-it-was-busy, which let a second
        # tenant rewrite the egress fence of a worker already crawling for a
        # first one. A busy worker now refuses at step 4 and its allowlist file
        # is never opened.
        try:
            reserved = await explorer_client.reserve_worker(
                explorer_url=worker["url"], crawl_id=crawl_id, tenant_id=tenant_id,
            )
        except ExplorerDispatchError as exc:
            last_exc = exc
            if exc.status_code in (409, 502):
                continue          # busy/unreachable → next worker, fence untouched
            break                 # deterministic (token unset / old image)
        if not reserved:
            last_exc = ExplorerDispatchError(
                "explorer is busy (single-flight job lock) — try again later",
                status_code=409,
            )
            continue              # busy → next worker, fence untouched

        # T-FL-02 — take the registry slot ONLY after the worker itself agreed.
        # The worker's own reservation is the authority on whether it can run
        # this crawl; the registry slot is qe-central's accounting of that fact.
        # Taking it first would leak a slot every time a worker refused.
        _wid = str(worker.get("worker_id") or "")
        if _wid and worker_source == "registry":
            if not await worker_registry.acquire_slot(worker_id=_wid):
                # Another replica took the last slot between ranking and here.
                # Hand the worker's own reservation back so it is not wedged,
                # then try the next-least-loaded worker.
                await explorer_client.release_worker(
                    explorer_url=worker["url"], crawl_id=crawl_id,
                    tenant_id=tenant_id)
                last_exc = ExplorerDispatchError(
                    "worker reached capacity between selection and dispatch",
                    status_code=409)
                continue
            claimed_worker_id = _wid

        try:
            _write_egress_allowlist(allowed_hosts, worker["allowlist_path"])
            result = await explorer_client.dispatch_crawl(
                dispatch_request, explorer_url=worker["url"],
            )
            last_exc = None
            break
        except ExplorerDispatchError as exc:
            last_exc = exc
            await explorer_client.release_worker(
                explorer_url=worker["url"], crawl_id=crawl_id, tenant_id=tenant_id)
            await _release_registry_slot(claimed_worker_id)
            claimed_worker_id = ""
            if exc.status_code in (409, 502):
                continue  # this worker busy/unreachable → try the next
            break  # deterministic error (token unset / bad request) — same for all
        except Exception:
            # An allowlist write failure (503) or anything else: hand the slot
            # back so a failed dispatch never leaves a worker wedged.
            await explorer_client.release_worker(
                explorer_url=worker["url"], crawl_id=crawl_id, tenant_id=tenant_id)
            await _release_registry_slot(claimed_worker_id)
            claimed_worker_id = ""
            raise
    if result is None:
        # ── M3.3 / T-FL-01 · A BUSY FLEET IS NOT A FAILED CRAWL ───────────
        # This block used to mark the row `failed` and raise. A crawl was
        # therefore recorded as FAILED because someone else's crawl got to the
        # worker first — indistinguishable, in the row and in the UI, from a
        # crawl that failed because the customer's application is broken. And
        # the work was simply LOST: no retry, no record of intent.
        #
        # Now a CAPACITY condition (409 busy / 502 unreachable / nothing
        # eligible) ENQUEUES the crawl durably and answers 202 `queued` with a
        # real position in the fair drain order. A DETERMINISTIC error (missing
        # fleet token, rejected request, misconfiguration) still fails
        # immediately and loudly — it will fail identically on every worker and
        # at every future moment, so queueing it would trade an actionable error
        # for an hour of silence and a timeout naming the wrong cause.
        detail = (str(last_exc)[:500] if last_exc
                  else (no_worker_reason or "no explorer worker available"))
        capacity_bound = (
            last_exc is None or queue_store.queue_verdict_is_capacity(
                last_exc.status_code)
        )
        if capacity_bound:
            try:
                position = await queue_store.enqueue(
                    tenant_id=tenant_id, exploration_id=exploration_id,
                    reason="fleet_at_capacity", detail=detail,
                )
            except Exception as exc:
                # The queue itself is unavailable. Fail honestly rather than
                # answer 202 for a crawl that was never durably recorded — a
                # fabricated "queued" is worse than an honest 503.
                logger.error("qec.explorations.enqueue_failed",
                             extra={"exploration_id": exploration_id,
                                    "error": str(exc)[:300]})
                await _mark(tenant_id, exploration_id, status="failed",
                            error=f"could not queue: {str(exc)[:300]}",
                            finished_at=utc_now())
                raise HTTPException(
                    status_code=503,
                    detail="the fleet is busy and the crawl queue is "
                           "unavailable — nothing was started",
                ) from exc
            try:
                from ..observability import metrics as _metrics
                _metrics.record_crawl_queued(reason="fleet_at_capacity")
            except Exception:  # pragma: no cover — metrics never break dispatch
                pass
            logger.warning(
                "qec.explorations.queued",
                extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
                       "app_id": app_id, "crawl_id": crawl_id,
                       "position": position, "reason": detail[:200]})
            if response is not None:
                response.status_code = 202
            return {
                "exploration_id": exploration_id, "app_id": app_id,
                "crawl_id": crawl_id, "extractor_version": extractor_version,
                "status": queue_store.STATUS_QUEUED,
                "queue_position": position,
                # WHY it is waiting — busy fleet, dead workers, or parked
                # workers are three different incidents and must not read alike.
                "queued_reason": detail,
                "accepted": False,
            }
        await _mark(
            tenant_id, exploration_id,
            status="failed", error=detail[:2000], finished_at=utc_now(),
        )
        raise HTTPException(
            status_code=(last_exc.status_code if last_exc else 503) or 502,
            detail=detail,
        )

    # PERSIST the status this response reports. 'dispatched' was returned in the
    # body and never written, so the row said 'pending' while the API said
    # 'dispatched' — four ACTIVE-status sets carried the value defensively for a
    # state that could not occur, and an operator reading the row saw a crawl
    # still queued when a worker had accepted it.
    #
    # Compare-and-set on 'pending': a fast crawl can call back /complete before
    # this line runs, and an unconditional write would regress a finished crawl
    # to 'dispatched' — inventing a state the crawl had already left.
    status = await _mark_if_status(
        tenant_id, exploration_id, expect="pending", status="dispatched",
    )
    if response is not None:
        response.status_code = 202
    logger.info(
        "qec.explorations.dispatched",
        extra={"exploration_id": exploration_id, "tenant_id": tenant_id,
               "app_id": app_id, "crawl_id": crawl_id, "status": status},
    )
    return {
        "exploration_id": exploration_id,
        "app_id": app_id,
        "crawl_id": crawl_id,
        "extractor_version": extractor_version,
        # The row's ACTUAL status — 'dispatched' normally, or whatever the crawl
        # has already advanced to if it beat us here.
        "status": status,
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


#: Statuses a crawl may be resumed FROM. ``stalled`` is the reaper's terminal
#: state for a crawl whose worker died or whose callback was lost; ``failed`` is
#: included because an adjudicated failure (T-GW-01: an inventory read the crawl
#: could not recover) leaves a perfectly valid durable prefix, and the whole
#: point of resume is that such a crawl is CONTINUED once the cause is fixed
#: rather than restarted from nothing.
_RESUMABLE_STATUSES = frozenset({"stalled", "failed"})


@router.post("/explorations/{exploration_id}/resume", status_code=202)
async def resume_exploration(
    exploration_id: str,
    request: Request,
    response: Response,
    user: dict = Depends(require_role("admin", "manager")),
) -> dict:
    """CONTINUE an interrupted crawl under its ORIGINAL crawl id (T-GW-03).

    The operator-facing half of durable resume.  Everything the crawl already
    proved — its page states, its actions, its screenshots, its walk mutations —
    stays exactly where it is; the worker re-opens the same manifest, restores
    the frontier from the last checkpoint, and continues past the durable prefix.

    WHY THIS IS NOT "CRAWL AGAIN".  A fresh dispatch mints a new crawl id, so it
    writes a new manifest, discovers the same states over again, and the earlier
    evidence is orphaned under an id nothing will ever complete.  For a crawl
    that died forty minutes into a fifty-minute walk, that is the difference
    between losing ten minutes and losing forty.

    REFUSED, LOUDLY, when the row is not resumable: a crawl still running must
    not be double-dispatched (the explorer is single-flight and the second
    dispatch would be refused anyway, but refusing here says why), and a
    ``completed`` crawl has nothing to continue.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = (await session.execute(
            select(QEExplorationRow).where(
                QEExplorationRow.exploration_id == exploration_id,
                QEExplorationRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="exploration not found")
        status_now = row.status
        app_id = row.app_id
        crawl_id = str((row.stats or {}).get("crawl_id") or "")

    if status_now not in _RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(f"exploration is '{status_now}' — only "
                    f"{sorted(_RESUMABLE_STATUSES)} may be resumed"),
        )
    if not crawl_id:
        # A row from before the crawl id was stamped on stats. There is no way to
        # find its manifest, so there is nothing to continue — and saying so is
        # the honest answer. Silently starting a fresh crawl here would be this
        # milestone's own failure mode wearing a resume label.
        raise HTTPException(
            status_code=409,
            detail=("this exploration has no recorded crawl_id, so its durable "
                    "evidence cannot be located — start a new crawl instead"),
        )
    if not app_id:
        raise HTTPException(
            status_code=409,
            detail="this exploration is not bound to an app and cannot be resumed",
        )

    return await _dispatch_explorer(
        tenant_id=tenant_id, app_id=app_id, request=request, response=response,
        resume_from={"exploration_id": exploration_id, "crawl_id": crawl_id},
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
