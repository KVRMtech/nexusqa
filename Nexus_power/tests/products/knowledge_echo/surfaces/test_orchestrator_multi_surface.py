"""Orchestrator routing via SurfaceRegistry — multi-surface scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from app.classifier import ClassifierOutput, SenderContext
from app.matcher import MatchCandidate, MatchResult
from app.orchestrator import EchoInput, EchoOrchestrator
from app.surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceError,
    SurfaceHandler,
    SurfaceRegistry,
    SurfaceUnavailable,
)

from nexus_sdk.feature_flags import (
    CircuitState,
    FlagState,
    Mode,
    Outcome,
)


# ── Doubles ─────────────────────────────────────────────────────


@dataclass
class _FakeFlags:
    state: FlagState
    outcomes: list[Outcome] = field(default_factory=list)

    async def get(self, tenant_id, key):  # noqa: ARG002
        return self.state

    async def record_outcome(self, tenant_id, key, outcome):  # noqa: ARG002
        self.outcomes.append(outcome)


@dataclass
class _FakeClassifier:
    output: ClassifierOutput

    async def classify(self, *, text, sender):  # noqa: ARG002
        return self.output


@dataclass
class _FakeMatcher:
    result: MatchResult

    async def match(self, *, tenant_id, trace_id, query, limit):  # noqa: ARG002
        return self.result


@dataclass
class _FakeRepo:
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    dedup_collide_with: Optional[str] = None

    async def create(self, **kwargs):
        from dataclasses import dataclass as _dc

        @_dc(frozen=True)
        class _Rec:
            dispatch_id: str
            tenant_id: str
            trace_id: str
            decision: str
            confidence_band: Optional[str]
            top_similarity: Optional[float]
            posted_message_ref: Optional[str]

        dispatch_id = f"d-{len(self.dispatches)+1}"
        self.dispatches.append({"dispatch_id": dispatch_id, **kwargs})
        return _Rec(
            dispatch_id=dispatch_id,
            tenant_id=kwargs["tenant_id"],
            trace_id=kwargs["trace_id"],
            decision=kwargs["decision"],
            confidence_band=kwargs.get("confidence_band"),
            top_similarity=kwargs.get("top_similarity"),
            posted_message_ref=kwargs.get("posted_message_ref"),
        )

    async def claim_or_get_existing(self, *, tenant_id, dedup_key, dispatch_id, ttl_seconds):  # noqa: ARG002
        if self.dedup_collide_with is None:
            return None
        from app.dispatches import DedupHit
        from datetime import datetime, timedelta, timezone

        return DedupHit(
            existing_dispatch_id=self.dedup_collide_with,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )


class _RecordingComposer:
    def __init__(self, surface: str):
        self.surface = surface
        self.calls: list[dict[str, Any]] = []

    def compose(self, *, dispatch_id, question_text, match):
        self.calls.append(
            {"dispatch_id": dispatch_id, "question": question_text}
        )
        return ComposedPayload(
            surface=self.surface,
            text=f"[{self.surface}] echo",
            payload={"surface": self.surface, "dispatch_id": dispatch_id},
            payload_hash=f"hash-{self.surface}-{dispatch_id}",
            similarity_pct=int(round(match.top_similarity * 100)),
            primary_candidate=match.candidates[0].to_audit_dict(),
        )


class _RecordingDispatcher:
    def __init__(self, *, decision_on_channel: str = "posted_channel", raise_with: Optional[Exception] = None):
        self.calls: list[dict[str, Any]] = []
        self._decision = decision_on_channel
        self._raise = raise_with

    async def dispatch(
        self,
        *,
        tenant_id,
        payload,
        as_dm,
        is_live,
        user_id_ext,
        channel_id_ext,
        thread_ts,
    ):
        if self._raise:
            raise self._raise
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "as_dm": as_dm,
                "is_live": is_live,
                "user_id_ext": user_id_ext,
                "channel_id_ext": channel_id_ext,
                "surface": payload.surface,
            }
        )
        if as_dm:
            return DispatchOutcome(
                decision="posted_dm",
                message_ref=f"dm-{payload.surface}",
                raw={},
            )
        return DispatchOutcome(
            decision=self._decision,
            message_ref=f"ch-{payload.surface}",
            raw={},
        )


# ── Helpers ─────────────────────────────────────────────────────


def _flag_state(*, mode=Mode.LIVE, enabled=True, circuit=CircuitState.CLOSED, config=None) -> FlagState:
    return FlagState(
        tenant_id="t1",
        feature_key="knowledge_echo",
        enabled=enabled,
        mode=mode,
        config=config or {},
        version=1,
        circuit_state=circuit,
    )


def _classifier_yes() -> ClassifierOutput:
    return ClassifierOutput(
        is_question=True,
        confidence=0.95,
        question_type="policy",
        rationale_short="",
    )


def _candidate(similarity: float = 0.95) -> MatchCandidate:
    return MatchCandidate(
        node_id="n1",
        node_type="TranscriptSegment",
        similarity=similarity,
        text="CA cigar lookback is 24 months.",
        speaker_id="priya",
        speaker_role="underwriting",
        session_id="sess",
        artifact_id="art",
        start_ms=1000,
        end_ms=5000,
        ordinal=0,
        product_ids=("lt5",),
        raw={},
    )


def _match(sim: float) -> MatchResult:
    band = "high" if sim >= 0.85 else "medium" if sim >= 0.65 else "low"
    if sim <= 0:
        return MatchResult(candidates=[], top_similarity=0.0, confidence_band="none")
    return MatchResult(
        candidates=[_candidate(sim)], top_similarity=sim, confidence_band=band  # type: ignore[arg-type]
    )


def _build_orch(
    *,
    surfaces: SurfaceRegistry,
    flags: _FakeFlags,
    matcher: _FakeMatcher,
    repo: _FakeRepo,
) -> EchoOrchestrator:
    return EchoOrchestrator(
        feature_flags=flags,
        feature_key="knowledge_echo",
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=matcher,
        dispatches=repo,
        dedup_window_seconds=3600,
        max_match_candidates=5,
        min_confidence_high=0.85,
        min_confidence_medium=0.65,
        end_to_end_timeout_seconds=10.0,
        surfaces=surfaces,
    )


def _input(*, surface="slack", channel="C1") -> EchoInput:
    return EchoInput(
        tenant_id="t1",
        trigger_surface=surface,
        trigger_plugin_event_id="Ev1",
        user_id_ext="U1",
        channel_id_ext=channel,
        text="What is the CA tobacco lookback?",
        sender=SenderContext(surface=surface),
    )


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_dispatches_to_matching_surface() -> None:
    slack_disp = _RecordingDispatcher()
    teams_disp = _RecordingDispatcher()
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="slack",
                composer=_RecordingComposer("slack"),
                dispatcher=slack_disp,
            ),
            SurfaceHandler(
                surface="teams",
                composer=_RecordingComposer("teams"),
                dispatcher=teams_disp,
            ),
        ]
    )
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        surfaces=surfaces,
        flags=flags,
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
    )

    r1 = await orch.process(_input(surface="slack"))
    assert r1.decision == "posted_channel"
    assert r1.surface == "slack"
    assert len(slack_disp.calls) == 1 and len(teams_disp.calls) == 0

    r2 = await orch.process(_input(surface="teams"))
    assert r2.decision == "posted_channel"
    assert r2.surface == "teams"
    assert len(teams_disp.calls) == 1


@pytest.mark.asyncio
async def test_orchestrator_suppresses_for_unknown_surface() -> None:
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="slack",
                composer=_RecordingComposer("slack"),
                dispatcher=_RecordingDispatcher(),
            ),
        ]
    )
    repo = _FakeRepo()
    orch = _build_orch(
        surfaces=surfaces,
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
    )
    result = await orch.process(_input(surface="discord"))
    assert result.decision == "suppressed_error"
    assert "surface_unregistered" in result.decision_reason


@pytest.mark.asyncio
async def test_orchestrator_handles_surface_unavailable() -> None:
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="email",
                composer=_RecordingComposer("email"),
                dispatcher=_RecordingDispatcher(
                    raise_with=SurfaceUnavailable("no install")
                ),
            ),
        ]
    )
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        surfaces=surfaces,
        flags=flags,
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
    )
    result = await orch.process(_input(surface="email"))
    assert result.decision == "suppressed_error"
    assert "surface_install_error" in result.decision_reason
    assert flags.outcomes == [Outcome.FAILURE]


@pytest.mark.asyncio
async def test_orchestrator_handles_surface_post_error() -> None:
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="webhook",
                composer=_RecordingComposer("webhook"),
                dispatcher=_RecordingDispatcher(
                    raise_with=SurfaceError("upstream rejected")
                ),
            ),
        ]
    )
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        surfaces=surfaces,
        flags=flags,
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
    )
    result = await orch.process(_input(surface="webhook"))
    assert result.decision == "suppressed_error"
    assert "surface_post_error" in result.decision_reason


@pytest.mark.asyncio
async def test_orchestrator_handler_override_routes_one_request() -> None:
    primary = _RecordingDispatcher()
    override = _RecordingDispatcher()
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="email",
                composer=_RecordingComposer("email"),
                dispatcher=primary,
            ),
        ]
    )
    override_handler = SurfaceHandler(
        surface="email",
        composer=_RecordingComposer("email"),
        dispatcher=override,
    )
    orch = _build_orch(
        surfaces=surfaces,
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
    )
    await orch.process(
        _input(surface="email"),
        surface_handler_override=override_handler,
    )
    assert len(override.calls) == 1
    assert len(primary.calls) == 0


@pytest.mark.asyncio
async def test_orchestrator_uses_shadow_mode_via_registry() -> None:
    disp = _RecordingDispatcher()
    surfaces = SurfaceRegistry(
        [
            SurfaceHandler(
                surface="teams",
                composer=_RecordingComposer("teams"),
                dispatcher=disp,
            ),
        ]
    )
    orch = _build_orch(
        surfaces=surfaces,
        flags=_FakeFlags(state=_flag_state(mode=Mode.SHADOW)),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
    )
    result = await orch.process(_input(surface="teams"))
    assert result.decision == "shadow_logged"
    assert disp.calls == []
