"""Echo orchestrator — surface-agnostic policy layer.

Pipeline (unchanged semantically from Phase 2; refactored in Phase 4
to dispatch through the surface registry instead of a hard-coded
Slack path):

    1. Feature flag — bail when disabled or circuit-open.
    2. Question classifier — skip non-questions.
    3. Matcher — apply per-tenant confidence overrides.
    4. Dedup — claim or detect duplicate.
    5. Compose payload via the surface's ``SurfaceComposer``.
    6. Dispatch through the surface's ``SurfaceDispatcher`` according
       to effective mode (shadow / DM / live).
    7. Persist outcome to ``echo_dispatches``; record outcome to the
       feature flag service so the circuit breaker observes failures.

The orchestrator carries no surface-specific knowledge beyond the
``trigger_surface`` field on the input. Surfaces are added by
registering a ``SurfaceHandler`` with the ``SurfaceRegistry``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from nexus_sdk.feature_flags import (
    FeatureFlagService,
    Mode,
    Outcome,
)

from .classifier import (
    ClassifierOutput,
    QuestionClassifier,
    SenderContext,
)
from .dispatches import (
    DispatchRepository,
    compute_dedup_key,
    compute_text_hash,
)
from .gaps import GapRecordInput, KnowledgeGapRecorder
from .matcher import Matcher, MatchResult
from .policy import ChannelPolicyService, PolicyContext, PolicyDecision
from .slack import (
    EchoCardComposer,
    SlackClient,
    SlackInstallationLoader,
)
from .surfaces import (
    ComposedPayload,
    SurfaceError,
    SurfaceHandler,
    SurfaceRegistry,
    SurfaceUnavailable,
)
from .surfaces.slack_adapter import build_slack_handler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EchoInput:
    """Parsed surface-agnostic representation of an inbound message."""

    tenant_id: str
    trigger_surface: str
    trigger_plugin_event_id: Optional[str]
    user_id_ext: Optional[str]
    channel_id_ext: Optional[str]
    text: str
    sender: SenderContext
    thread_ts: Optional[str] = None
    trace_id: Optional[str] = None
    surface_payload_hints: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class EchoResult:
    dispatch_id: Optional[str]
    decision: str
    decision_reason: str
    effective_mode: str
    posted_message_ref: Optional[str] = None
    surface: Optional[str] = None


class EchoOrchestrator:
    def __init__(
        self,
        *,
        feature_flags: FeatureFlagService,
        feature_key: str,
        classifier: QuestionClassifier,
        matcher: Matcher,
        dispatches: DispatchRepository,
        dedup_window_seconds: int,
        max_match_candidates: int,
        min_confidence_high: float,
        min_confidence_medium: float,
        end_to_end_timeout_seconds: float,
        # Phase 4: register handlers via a SurfaceRegistry.
        surfaces: Optional[SurfaceRegistry] = None,
        # Phase 2 backwards-compat: when ``surfaces`` is not given,
        # build a registry containing only the legacy Slack handler.
        composer: Optional[EchoCardComposer] = None,
        slack: Optional[SlackClient] = None,
        installs: Optional[SlackInstallationLoader] = None,
        # Phase 6 org-awareness hooks (all optional).
        channel_policy: Optional[ChannelPolicyService] = None,
        gap_recorder: Optional[KnowledgeGapRecorder] = None,
    ) -> None:
        self._flags = feature_flags
        self._feature_key = feature_key
        self._classifier = classifier
        self._matcher = matcher
        self._dispatches = dispatches
        self._dedup_window = dedup_window_seconds
        self._limit = max_match_candidates
        self._high = min_confidence_high
        self._medium = min_confidence_medium
        self._timeout = end_to_end_timeout_seconds
        self._channel_policy = channel_policy
        self._gap_recorder = gap_recorder

        if surfaces is not None:
            self._surfaces = surfaces
        else:
            if slack is None or installs is None:
                raise ValueError(
                    "EchoOrchestrator requires either 'surfaces' or "
                    "the legacy slack+installs pair"
                )
            handler = build_slack_handler(
                slack=slack,
                installs=installs,
                composer=composer,
            )
            self._surfaces = SurfaceRegistry([handler])

    # ── Entry point ────────────────────────────────────────────

    async def process(
        self,
        inp: EchoInput,
        *,
        surface_handler_override: Optional[SurfaceHandler] = None,
    ) -> EchoResult:
        try:
            return await asyncio.wait_for(
                self._process(inp, surface_handler_override),
                self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "echo.timeout tenant=%s surface=%s text_len=%d",
                inp.tenant_id,
                inp.trigger_surface,
                len(inp.text or ""),
            )
            await self._record_outcome_safe(
                inp.tenant_id, Outcome.TIMEOUT
            )
            return EchoResult(
                dispatch_id=None,
                decision="suppressed_error",
                decision_reason="orchestrator_timeout",
                effective_mode="shadow",
                surface=inp.trigger_surface,
            )

    # ── Pipeline ───────────────────────────────────────────────

    async def _process(
        self,
        inp: EchoInput,
        surface_handler_override: Optional[SurfaceHandler],
    ) -> EchoResult:
        trace_id = inp.trace_id or uuid.uuid4().hex

        # Resolve the surface handler up front so we suppress with a
        # clear reason when a surface has nothing registered.
        try:
            handler = (
                surface_handler_override
                if surface_handler_override is not None
                else self._surfaces.get(inp.trigger_surface)
            )
        except SurfaceUnavailable as exc:
            return await self._suppress(
                inp,
                trace_id,
                decision="suppressed_error",
                reason=f"surface_unregistered: {exc}",
                effective_mode="shadow",
            )

        # 1. Feature flag.
        flag_state = await self._flags.get(inp.tenant_id, self._feature_key)
        effective_mode = flag_state.effective_mode
        if not flag_state.enabled:
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_disabled",
                reason="feature_disabled",
                effective_mode=effective_mode.value,
            )
        if flag_state.circuit_state.value == "open":
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_circuit",
                reason="circuit_open",
                effective_mode=effective_mode.value,
            )

        thresholds = self._resolve_thresholds(flag_state.config)

        # 2. Classifier.
        text = (inp.text or "").strip()
        if not text:
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_classifier",
                reason="empty_text",
                effective_mode=effective_mode.value,
            )

        classifier_out = await self._classifier.classify(
            text=text, sender=inp.sender
        )
        if not classifier_out.is_question or classifier_out.confidence < 0.5:
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_classifier",
                reason=f"not_a_question conf={classifier_out.confidence:.2f}",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
            )

        # 2b. Channel policy gate (Phase 6).
        policy_decision: Optional[PolicyDecision] = None
        if self._channel_policy is not None:
            policy_decision = await self._channel_policy.evaluate(
                PolicyContext(
                    tenant_id=inp.tenant_id,
                    surface=inp.trigger_surface,
                    channel_id_ext=inp.channel_id_ext,
                    product_ids=tuple(classifier_out.product_hints or ()),
                    topic_hints=tuple(classifier_out.domain_hints or ()),
                )
            )
            if not policy_decision.allow:
                return await self._suppress(
                    inp, trace_id,
                    decision="suppressed_disabled",
                    reason=f"channel_policy:{policy_decision.reason}",
                    effective_mode="muted",
                    classifier_output=classifier_out.model_dump(),
                )
            if policy_decision.forced_mode:
                try:
                    effective_mode = Mode(policy_decision.forced_mode)
                except ValueError:
                    pass
            if policy_decision.min_confidence_override is not None:
                # Policy can only RAISE the threshold, never lower it.
                thresholds = _raise_thresholds(
                    thresholds,
                    policy_decision.min_confidence_override,
                )

        # 3. Dedup key (claim after compose).
        text_hash = compute_text_hash(text)
        dedup_key = compute_dedup_key(
            channel_id=inp.channel_id_ext, text_hash=text_hash
        )

        # 4. Match.
        match = await self._matcher.match(
            tenant_id=inp.tenant_id,
            trace_id=trace_id,
            query=text,
            limit=self._limit,
        )
        if match.is_empty or match.top_similarity < thresholds.medium:
            await self._record_gap_safe(
                tenant_id=inp.tenant_id,
                question_text=text,
                inp=inp,
                product_hints=classifier_out.product_hints,
                similarity=match.top_similarity,
                reason=(
                    "no_match"
                    if match.is_empty
                    else f"low_conf:{match.top_similarity:.2f}"
                ),
            )
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_low_conf",
                reason=f"top_sim={match.top_similarity:.2f}",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
                match=match,
                text_hash=text_hash,
            )

        # 5. Compose via surface handler.
        dispatch_id = uuid.uuid4().hex
        payload = handler.composer.compose(
            dispatch_id=dispatch_id,
            question_text=text,
            match=match,
        )
        if payload is None:
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_low_conf",
                reason="composer_returned_none",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
                match=match,
                text_hash=text_hash,
            )

        # Surface-specific routing hints (e.g. teams service_url) may
        # arrive on the input; merge them into the primary_candidate
        # so dispatchers that need them can read them.
        if inp.surface_payload_hints:
            payload = self._merge_hints(payload, inp.surface_payload_hints)

        # 6. Claim dedup slot.
        existing = await self._dispatches.claim_or_get_existing(
            tenant_id=inp.tenant_id,
            dedup_key=dedup_key,
            dispatch_id=dispatch_id,
            ttl_seconds=self._dedup_window,
        )
        if existing is not None:
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_dup",
                reason=f"existing_dispatch_id={existing.existing_dispatch_id}",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
                match=match,
                text_hash=text_hash,
            )

        # 7. Dispatch by mode.
        if effective_mode == Mode.SHADOW:
            await self._persist_dispatch(
                dispatch_id=dispatch_id,
                inp=inp,
                trace_id=trace_id,
                text_hash=text_hash,
                classifier_output=classifier_out,
                match=match,
                decision="shadow_logged",
                decision_reason="shadow_mode",
                effective_mode=effective_mode.value,
                rendered_payload_hash=payload.payload_hash,
                posted_message_ref=None,
                posted_at=None,
            )
            await self._record_outcome_safe(inp.tenant_id, Outcome.SUCCESS)
            return EchoResult(
                dispatch_id=dispatch_id,
                decision="shadow_logged",
                decision_reason="shadow_mode",
                effective_mode=effective_mode.value,
                surface=inp.trigger_surface,
            )

        is_live = effective_mode == Mode.LIVE
        as_dm = (
            effective_mode == Mode.DM_ONLY
            or match.top_similarity < thresholds.high
        )

        try:
            outcome = await handler.dispatcher.dispatch(
                tenant_id=inp.tenant_id,
                payload=payload,
                as_dm=as_dm,
                is_live=is_live,
                user_id_ext=inp.user_id_ext,
                channel_id_ext=inp.channel_id_ext,
                thread_ts=inp.thread_ts,
            )
        except SurfaceUnavailable as exc:
            await self._record_outcome_safe(inp.tenant_id, Outcome.FAILURE)
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_error",
                reason=f"surface_install_error: {exc}",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
                match=match,
                text_hash=text_hash,
            )
        except SurfaceError as exc:
            await self._record_outcome_safe(inp.tenant_id, Outcome.FAILURE)
            return await self._suppress(
                inp, trace_id,
                decision="suppressed_error",
                reason=f"surface_post_error: {exc}",
                effective_mode=effective_mode.value,
                classifier_output=classifier_out.model_dump(),
                match=match,
                text_hash=text_hash,
            )

        posted_message_ref = outcome.message_ref
        await self._persist_dispatch(
            dispatch_id=dispatch_id,
            inp=inp,
            trace_id=trace_id,
            text_hash=text_hash,
            classifier_output=classifier_out,
            match=match,
            decision=outcome.decision,
            decision_reason=f"sim={match.top_similarity:.2f}",
            effective_mode=effective_mode.value,
            rendered_payload_hash=payload.payload_hash,
            posted_message_ref=posted_message_ref,
            posted_at=datetime.now(timezone.utc),
        )
        await self._record_outcome_safe(inp.tenant_id, Outcome.SUCCESS)
        return EchoResult(
            dispatch_id=dispatch_id,
            decision=outcome.decision,
            decision_reason=f"sim={match.top_similarity:.2f}",
            effective_mode=effective_mode.value,
            posted_message_ref=posted_message_ref,
            surface=inp.trigger_surface,
        )

    # ── Persistence helpers ────────────────────────────────────

    async def _suppress(
        self,
        inp: EchoInput,
        trace_id: str,
        *,
        decision: str,
        reason: str,
        effective_mode: str,
        classifier_output: Optional[dict[str, Any]] = None,
        match: Optional[MatchResult] = None,
        text_hash: Optional[str] = None,
    ) -> EchoResult:
        await self._persist_dispatch(
            dispatch_id=uuid.uuid4().hex,
            inp=inp,
            trace_id=trace_id,
            text_hash=text_hash or compute_text_hash(inp.text or ""),
            classifier_output=_classifier_output_to_obj(classifier_output),
            match=match,
            decision=decision,
            decision_reason=reason,
            effective_mode=effective_mode,
            rendered_payload_hash=None,
            posted_message_ref=None,
            posted_at=None,
        )
        return EchoResult(
            dispatch_id=None,
            decision=decision,
            decision_reason=reason,
            effective_mode=effective_mode,
            surface=inp.trigger_surface,
        )

    async def _persist_dispatch(
        self,
        *,
        dispatch_id: str,
        inp: EchoInput,
        trace_id: str,
        text_hash: str,
        classifier_output: Optional[ClassifierOutput],
        match: Optional[MatchResult],
        decision: str,
        decision_reason: str,
        effective_mode: str,
        rendered_payload_hash: Optional[str],
        posted_message_ref: Optional[str],
        posted_at: Optional[datetime],
    ) -> None:
        match_dump: Optional[list[dict[str, Any]]] = None
        top_sim: Optional[float] = None
        band: Optional[str] = None
        if match is not None and not match.is_empty:
            match_dump = [c.to_audit_dict() for c in match.candidates]
            top_sim = match.top_similarity
            band = match.confidence_band
        elif match is not None:
            top_sim = 0.0
            band = "none"

        classifier_dump = (
            classifier_output.model_dump()
            if classifier_output is not None
            else None
        )

        await self._dispatches.create(
            tenant_id=inp.tenant_id,
            trace_id=trace_id,
            trigger_surface=inp.trigger_surface,
            trigger_plugin_event_id=inp.trigger_plugin_event_id,
            trigger_user_id_ext=inp.user_id_ext,
            trigger_channel_ext=inp.channel_id_ext,
            trigger_text_hash=text_hash,
            classifier_output=classifier_dump,
            match_candidates=match_dump,
            top_similarity=top_sim,
            confidence_band=band,
            decision=decision,
            decision_reason=decision_reason,
            effective_mode=effective_mode,
            rendered_payload_hash=rendered_payload_hash,
            posted_at=posted_at,
            posted_message_ref=posted_message_ref,
        )

    async def _record_outcome_safe(
        self, tenant_id: str, outcome: Outcome
    ) -> None:
        try:
            await self._flags.record_outcome(
                tenant_id, self._feature_key, outcome
            )
        except Exception as exc:
            logger.warning(
                "echo.outcome_record_failed tenant=%s err=%s",
                tenant_id, exc,
            )

    async def _record_gap_safe(
        self,
        *,
        tenant_id: str,
        question_text: str,
        inp: EchoInput,
        product_hints: Optional[list[str]],
        similarity: float,
        reason: str,
    ) -> None:
        if self._gap_recorder is None:
            return
        try:
            await self._gap_recorder.record(
                GapRecordInput(
                    tenant_id=tenant_id,
                    question_text=question_text,
                    asker_user_id_ext=inp.user_id_ext,
                    product_ids=tuple(product_hints or ()),
                    similarity=similarity,
                    reason=reason,
                )
            )
        except Exception as exc:
            logger.warning(
                "echo.gap_record_failed tenant=%s err=%s",
                tenant_id, exc,
            )

    # ── Threshold resolution + payload helpers ────────────────

    def _resolve_thresholds(
        self, flag_config: dict[str, Any]
    ) -> "_Thresholds":
        high = self._high
        medium = self._medium
        if isinstance(flag_config, dict):
            try:
                cfg_high = float(flag_config.get("min_confidence_high"))
                if 0.0 < cfg_high <= 1.0:
                    high = cfg_high
            except (TypeError, ValueError):
                pass
            try:
                cfg_med = float(flag_config.get("min_confidence_medium"))
                if 0.0 < cfg_med <= 1.0:
                    medium = cfg_med
            except (TypeError, ValueError):
                pass
        if medium > high:
            medium = high
        return _Thresholds(high=high, medium=medium)

    @staticmethod
    def _merge_hints(
        payload: ComposedPayload, hints: dict[str, Any]
    ) -> ComposedPayload:
        merged_primary = dict(payload.primary_candidate)
        merged_primary.update(hints)
        return ComposedPayload(
            surface=payload.surface,
            text=payload.text,
            payload=payload.payload,
            payload_hash=payload.payload_hash,
            similarity_pct=payload.similarity_pct,
            primary_candidate=merged_primary,
        )


# ── Helpers ────────────────────────────────────────────────────


def _classifier_output_to_obj(
    raw: Optional[dict[str, Any]]
) -> Optional[ClassifierOutput]:
    if raw is None:
        return None
    try:
        return ClassifierOutput.model_validate(raw)
    except Exception:
        return None


# Backwards-compat alias kept for callers importing the old helper name.
classifier_output_to_obj = _classifier_output_to_obj


@dataclass(frozen=True)
class _Thresholds:
    high: float
    medium: float


def _raise_thresholds(
    current: "_Thresholds", floor: float
) -> "_Thresholds":
    """Raise thresholds so neither is below ``floor``.

    Used by the channel-policy gate to enforce a per-channel
    ``min_confidence_override`` without ever lowering the tenant's
    configured floor.
    """
    new_medium = max(current.medium, float(floor))
    new_high = max(current.high, new_medium)
    return _Thresholds(high=new_high, medium=new_medium)
