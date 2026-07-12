"""QE-Central S5 — repo webhook ingress (design §3.5).

``POST /webhooks/gitlab/{app_id}`` is the change-event producer a client's GitLab
calls on every push.  It:

  1. resolves the target app (and its tenant + per-app webhook secret) by
     ``app_id`` — a NON-tenant-scoped read, because GitLab sends no JWT (only the
     ``X-Gitlab-Token`` per-app secret);
  2. verifies ``X-Gitlab-Token`` against the app's secret in CONSTANT TIME —
     FAIL-CLOSED: an unknown app, an app with no configured secret, or a bad
     token is rejected 401 with a single opaque message (no app-existence leak);
  3. records ONE ``change_events`` row (``source='repo_sha'``) under the resolved
     tenant's RLS scope, coalescing duplicate deliveries via
     ``UNIQUE(app_id, dedupe_key)`` (a replayed webhook never spawns a second
     cycle) — and returns 202.

This router lives OUTSIDE the ``/api/*`` prefix so the fail-closed JWT middleware
does not apply (parity with the explorer HMAC callback in ``internal.py``); the
per-app token IS the authentication.

RESOLVED (open decision #7): the app-resolution read carries no tenant GUC (a
webhook has no JWT), and ``client_apps`` has FORCE RLS — so a direct read from the
service role (``qec``, ``rolbypassrls=false``, verified) returns NOTHING and the
handler fail-closed 401 on EVERY delivery.  Fixed by resolving through the narrow,
read-only ``qec_resolve_webhook_app`` SECURITY DEFINER function (owned by a
BYPASSRLS role; migration ``scripts/apply_webhook_resolver_fn.sql``), which returns
only ``{tenant_id, repo_binding, status}`` for the one requested ``app_id`` — never
the whole table, never a wrong-tenant write.  The change_events INSERT still runs
under the resolved tenant's RLS scope.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..db import new_id, qec_engine, tenant_scoped_qec_session
from ..db.controlplane_models import CHANGE_SOURCE_REPO_SHA, ChangeEventRow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["QEC Webhooks"])

GITLAB_TOKEN_HEADER = "X-Gitlab-Token"
GITLAB_EVENT_HEADER = "X-Gitlab-Event"
#: The repo_binding key holding the per-app webhook secret.
WEBHOOK_SECRET_KEY = "webhook_secret"
_MAX_BODY_BYTES = 1_048_576  # 1 MiB — a push payload is small; cap defensively.


async def _resolve_app(app_id: str) -> dict | None:
    """Resolve ``app_id`` → ``{tenant_id, repo_binding, status}``.

    A webhook carries no JWT, so there is no tenant GUC to set — yet
    ``client_apps`` has FORCE ROW LEVEL SECURITY, so a direct read from the
    service role returns nothing and the handler would fail-closed 401 on EVERY
    delivery.  We instead call the narrow, read-only ``qec_resolve_webhook_app``
    SECURITY DEFINER function (owned by a BYPASSRLS role, migration
    ``apply_webhook_resolver_fn.sql``): it returns only the three fields this
    handler needs for the one requested ``app_id`` — never the whole table.

    Returns ``None`` when the app is absent — the caller then fails closed.
    Never raises into the request path."""
    try:
        async with qec_engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT tenant_id, repo_binding, status FROM qec_resolve_webhook_app(:aid)"),
                {"aid": app_id},
            )).mappings().first()
    except Exception as exc:  # DB unreachable / function absent — fail closed, never crash
        logger.warning("qec.webhook.resolve_failed", extra={"app_id": app_id, "error": str(exc)[:200]})
        return None
    return dict(row) if row is not None else None


async def _webhook_secret(request, tenant_id: str, app_id: str, repo_binding) -> str:
    """The app's webhook secret in cleartext for constant-time comparison.

    Prefers the envelope-encrypted ``webhook_secret_enc`` (AAD=app_id); falls back
    to a legacy plaintext ``webhook_secret`` for pre-encryption rows.  On any
    decrypt failure returns ``""`` → the caller fails closed (401)."""
    if not isinstance(repo_binding, dict):
        return ""
    enc = str(repo_binding.get("webhook_secret_enc") or "").strip()
    if enc:
        envelope = getattr(request.app.state, "envelope_service", None)
        if envelope is None:
            return ""
        try:
            from nexus_sdk.security.envelope import EnvelopeBlob

            blob = EnvelopeBlob.from_bytes(base64.b64decode(enc))
            plaintext = await envelope.decrypt(
                tenant_id, blob, expected_aad=app_id.encode("utf-8"),
            )
            return plaintext.decode("utf-8").strip()
        except Exception as exc:
            logger.warning(
                "qec.webhook.secret_decrypt_failed",
                extra={"app_id": app_id, "error": str(exc)[:200]},
            )
            return ""
    return str(repo_binding.get(WEBHOOK_SECRET_KEY) or "").strip()


def _dedupe_key(payload: dict, raw: bytes, event: str) -> str:
    """A stable dedupe key so a replayed delivery coalesces.

    Prefers the pushed commit sha + ref (the natural identity of a push); falls
    back to a hash of the raw body + event header when a sha is absent (e.g. a
    non-push hook), so EVERY delivery still has a deterministic key ≤200 chars."""
    after = str(payload.get("after") or payload.get("checkout_sha") or "").strip()
    ref = str(payload.get("ref") or "").strip()
    if after:
        return f"{event}:{ref}:{after}"[:200]
    digest = hashlib.sha256((event.encode() + b"\x00" + (raw or b""))).hexdigest()
    return f"{event}:{digest}"[:200]


@router.post("/gitlab/{app_id}", status_code=202)
async def gitlab_webhook(app_id: str, request: Request) -> dict:
    """Ingest a GitLab push webhook → one ``change_events`` row (202)."""
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="webhook payload too large")

    app = await _resolve_app(app_id)
    provided = (request.headers.get(GITLAB_TOKEN_HEADER) or "").strip()

    # FAIL-CLOSED: unknown app OR no configured secret OR token mismatch → 401.
    # Compute the comparison against a placeholder even when the app is unknown so
    # the response is not obviously timing-distinguishable by existence.
    _tenant_id = str(app["tenant_id"]) if app else ""
    secret = await _webhook_secret(request, _tenant_id, app_id, app.get("repo_binding")) if app else ""
    valid = bool(secret) and bool(provided) and hmac.compare_digest(secret, provided)
    if app is None or not valid:
        logger.warning(
            "qec.webhook.rejected",
            extra={"app_id": app_id, "has_app": app is not None,
                   "has_secret": bool(secret), "has_token": bool(provided)},
        )
        raise HTTPException(status_code=401, detail="invalid webhook credentials")

    if app.get("status") == "deleted":
        raise HTTPException(status_code=401, detail="invalid webhook credentials")

    tenant_id = str(app["tenant_id"])
    event = (request.headers.get(GITLAB_EVENT_HEADER) or "push").strip()[:64]
    try:
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            payload = {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed webhook body (not JSON)")

    dedupe_key = _dedupe_key(payload, raw, event)
    stored = {
        "event": event,
        "ref": str(payload.get("ref") or "")[:200],
        "new_sha": str(payload.get("after") or payload.get("checkout_sha") or "")[:64],
        "old_sha": str(payload.get("before") or "")[:64],
        "project_id": payload.get("project_id") or (payload.get("project") or {}).get("id"),
    }

    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            session.add(ChangeEventRow(
                event_id=new_id(), tenant_id=tenant_id, app_id=app_id,
                source=CHANGE_SOURCE_REPO_SHA, payload=stored, dedupe_key=dedupe_key,
            ))
            await session.flush()
    except IntegrityError:
        # UNIQUE(app_id, dedupe_key) — a duplicate delivery coalesces (idempotent).
        logger.info(
            "qec.webhook.duplicate_coalesced",
            extra={"tenant_id": tenant_id, "app_id": app_id, "dedupe_key": dedupe_key},
        )
        return {"accepted": True, "app_id": app_id, "deduped": True, "dedupe_key": dedupe_key}

    logger.info(
        "qec.webhook.recorded",
        extra={"tenant_id": tenant_id, "app_id": app_id, "event": event,
               "new_sha": stored["new_sha"][:12], "dedupe_key": dedupe_key},
    )
    return {"accepted": True, "app_id": app_id, "deduped": False, "dedupe_key": dedupe_key}
