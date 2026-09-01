"""Slack inbound webhooks: events + interactivity.

The route is intentionally thin — verify signature, parse the payload,
hand off to the orchestrator. Slack expects a 200 within 3 seconds, so
the heavy work is dispatched as a background task and we ACK
immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from ..classifier import SenderContext
from ..orchestrator import EchoInput, EchoOrchestrator
from ..slack import (
    ParsedInteraction,
    ParsedSlackEvent,
    SlackEventKind,
    SlackInstallationError,
    SlackInstallationLoader,
    SlackSignatureError,
    SlackSignatureInvalid,
    SlackSignatureMissing,
    SlackSignatureReplay,
    parse_block_actions,
    parse_slack_event,
    verify_slack_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Slack"], prefix="/webhook/slack")


def _orchestrator(request: Request) -> EchoOrchestrator:
    svc = getattr(request.app.state, "orchestrator", None)
    if svc is None:
        raise HTTPException(503, "echo_orchestrator_not_initialised")
    return svc


def _installs(request: Request) -> SlackInstallationLoader:
    svc = getattr(request.app.state, "slack_installs", None)
    if svc is None:
        raise HTTPException(503, "slack_install_loader_not_initialised")
    return svc


def _max_age(request: Request) -> int:
    cfg = getattr(request.app.state, "echo_config", None)
    return getattr(cfg, "slack_request_max_age_seconds", 300) if cfg else 300


# ── Events API ──────────────────────────────────────────────────


@router.post("/events")
async def slack_events(request: Request) -> JSONResponse:
    body = await request.body()
    headers = request.headers

    # Parse without verification first so we can short-circuit url_verification.
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_json")

    parsed = parse_slack_event(payload if isinstance(payload, dict) else {})

    if parsed.kind == SlackEventKind.URL_VERIFICATION:
        # url_verification still needs to be signed by Slack to count.
        team_id = None
    else:
        team_id = parsed.team_id

    # Look up the installation to get the signing secret.
    if team_id is None:
        # For url_verification we don't yet have an installation; the
        # signing secret must be sourced from the env. We do not allow
        # this for any other path.
        if parsed.kind != SlackEventKind.URL_VERIFICATION:
            raise HTTPException(
                status_code=400, detail="missing_team_id"
            )
        signing_secret = _env_signing_secret(request)
        if not signing_secret:
            raise HTTPException(
                status_code=503, detail="signing_secret_not_configured"
            )
    else:
        try:
            install = await _installs(request).for_team_id(team_id)
        except SlackInstallationError as exc:
            logger.info("slack.events.unknown_team team_id=%s err=%s", team_id, exc)
            raise HTTPException(status_code=404, detail="team_not_installed")
        signing_secret = install.signing_secret

    _verify_or_raise(
        signing_secret=signing_secret,
        timestamp=headers.get("x-slack-request-timestamp"),
        signature=headers.get("x-slack-signature"),
        body=body,
        max_age=_max_age(request),
    )

    if parsed.kind == SlackEventKind.URL_VERIFICATION:
        return JSONResponse({"challenge": parsed.challenge or ""})

    # Filter to actionable kinds; everything else ACK silently.
    if parsed.kind not in (SlackEventKind.APP_MENTION, SlackEventKind.MESSAGE_IM):
        return JSONResponse({"ok": True, "ignored": parsed.kind.value})

    # Hand off to orchestrator in the background. Slack expects us to
    # return within 3 seconds; the orchestrator has its own timeout.
    orch = _orchestrator(request)
    install = await _installs(request).for_team_id(team_id)  # already cached
    asyncio.create_task(
        _process_event_in_background(
            orch=orch,
            tenant_id=install.tenant_id,
            parsed=parsed,
        )
    )
    return JSONResponse({"ok": True})


# ── Interactivity (button clicks) ──────────────────────────────


@router.post("/interactions")
async def slack_interactions(request: Request) -> JSONResponse:
    body = await request.body()
    headers = request.headers
    # Slack sends interactivity as application/x-www-form-urlencoded
    # with a single ``payload`` form field carrying JSON.
    form = await request.form()
    payload_field = form.get("payload")
    if not isinstance(payload_field, str):
        raise HTTPException(status_code=400, detail="missing_payload")
    parsed = parse_block_actions(payload_field)
    if parsed.kind != "block_actions" or parsed.team_id is None:
        raise HTTPException(status_code=400, detail="unsupported_interaction")

    try:
        install = await _installs(request).for_team_id(parsed.team_id)
    except SlackInstallationError:
        raise HTTPException(status_code=404, detail="team_not_installed")

    _verify_or_raise(
        signing_secret=install.signing_secret,
        timestamp=headers.get("x-slack-request-timestamp"),
        signature=headers.get("x-slack-signature"),
        body=body,
        max_age=_max_age(request),
    )

    # Action IDs follow the convention "<prefix>:<signal>" or are the
    # ask-SME id. The dispatch_id is in the action ``value``.
    signal = _signal_from_action_id(parsed.action_id)
    if signal is None or not parsed.action_value:
        return JSONResponse({"ok": True, "ignored": True})

    repo = getattr(request.app.state, "dispatch_repo", None)
    if repo is None:
        raise HTTPException(503, "dispatch_repo_not_initialised")
    await repo.record_feedback(
        tenant_id=install.tenant_id,
        dispatch_id=parsed.action_value,
        user_id_ext=parsed.user_id,
        signal=signal,
        metadata={
            "team_id": parsed.team_id,
            "channel_id": parsed.channel_id,
            "message_ts": parsed.message_ts,
        },
    )

    # Outcome → circuit breaker. Thumbs_down is a failure signal.
    flags = getattr(request.app.state, "feature_flags", None)
    feature_key = getattr(request.app.state.echo_config, "feature_key", None)
    if flags is not None and feature_key:
        from nexus_sdk.feature_flags import Outcome

        outcome = (
            Outcome.THUMBS_UP
            if signal == "thumbs_up"
            else Outcome.THUMBS_DOWN
            if signal == "thumbs_down"
            else None
        )
        if outcome is not None:
            try:
                await flags.record_outcome(install.tenant_id, feature_key, outcome)
            except Exception as exc:
                logger.warning(
                    "slack.interactions.outcome_failed: %s", exc
                )

    return JSONResponse({"ok": True})


# ── Helpers ────────────────────────────────────────────────────


def _verify_or_raise(
    *,
    signing_secret: str,
    timestamp: Any,
    signature: Any,
    body: bytes,
    max_age: int,
) -> None:
    try:
        verify_slack_signature(
            signing_secret=signing_secret,
            timestamp=timestamp,
            received_signature=signature,
            body=body,
            max_age_seconds=max_age,
        )
    except SlackSignatureMissing as exc:
        raise HTTPException(status_code=401, detail=f"missing_signature: {exc}")
    except SlackSignatureReplay as exc:
        raise HTTPException(status_code=401, detail=f"replay_rejected: {exc}")
    except SlackSignatureInvalid as exc:
        raise HTTPException(status_code=401, detail=f"signature_invalid: {exc}")
    except SlackSignatureError as exc:
        raise HTTPException(status_code=401, detail=f"signature_error: {exc}")


async def _process_event_in_background(
    *,
    orch: EchoOrchestrator,
    tenant_id: str,
    parsed: ParsedSlackEvent,
) -> None:
    try:
        await orch.process(
            EchoInput(
                tenant_id=tenant_id,
                trigger_surface="slack",
                trigger_plugin_event_id=parsed.event_id,
                user_id_ext=parsed.user_id,
                channel_id_ext=parsed.channel_id,
                text=parsed.text,
                sender=SenderContext(surface="slack"),
                thread_ts=parsed.thread_ts,
                trace_id=parsed.event_id,
            )
        )
    except Exception as exc:
        logger.exception(
            "slack.background_process_failed event_id=%s err=%s",
            parsed.event_id,
            exc,
        )


def _signal_from_action_id(action_id: Any) -> Any:
    if not isinstance(action_id, str):
        return None
    if action_id.startswith("echo_feedback:thumbs_up"):
        return "thumbs_up"
    if action_id.startswith("echo_feedback:thumbs_down"):
        return "thumbs_down"
    if action_id.startswith("echo_ask_sme"):
        return "asked_sme"
    return None


def _env_signing_secret(request: Request) -> str:
    import os

    cfg = getattr(request.app.state, "echo_config", None)
    env_var = (
        getattr(cfg, "slack_signing_secret_env", "SLACK_SIGNING_SECRET")
        if cfg
        else "SLACK_SIGNING_SECRET"
    )
    return os.environ.get(env_var, "") or ""
