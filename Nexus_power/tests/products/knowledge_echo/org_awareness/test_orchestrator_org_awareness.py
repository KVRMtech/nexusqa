"""Orchestrator integration with Phase 6 hooks — policy gate + gap recorder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from app.classifier import ClassifierOutput, SenderContext
from app.gaps import GapRecordInput, GapRecordResult
from app.matcher import MatchCandidate, MatchResult
from app.orchestrator import EchoInput, EchoOrchestrator
from app.policy import PolicyContext, PolicyDecision
from app.surfaces import (
    ComposedPayload,
    DispatchOutcome,
    SurfaceHandler,
    SurfaceRegistry,
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

    async def claim_or_get_existing(
        self, *, tenant_id, dedup_key, dispatch_id, ttl_seconds  # noqa: ARG002
    ):
        return None


@dataclass
class _FakePolicy:
    decision: PolicyDecision
    calls: list[PolicyContext] = field(default_factory=list)

    async def evaluate(self, ctx):
        self.calls.append(ctx)
        return self.decision


@dataclass
class _FakeGapRecorder:
    calls: list[GapRecordInput] = field(default_factory=list)
    raise_with: Optional[Exception] = None

    async def record(self, rec):
        if self.raise_with:
            raise self.raise_with
        self.calls.append(rec)
        return GapRecordResult(gap_id="g-1", is_new=True, question_count=1)


class _RecordingDispatcher:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "as_dm": as_dm,
                "tenant_id": tenant_id,
                "channel_id_ext": channel_id_ext,
            }
        )
        return DispatchOutcome(
            decision="posted_dm" if as_dm else "posted_channel",
            message_ref="ref-1",
            raw={},
        )


class _RecordingComposer:
    def compose(self, *, dispatch_id, question_text, match):  # noqa: ARG002
        return ComposedPayload(
            surface="slack",
            text="echo",
            payload={"text": "echo"},
            payload_hash=f"h-{dispatch_id}",
            similarity_pct=int(round(match.top_similarity * 100)),
            primary_candidate=match.candidates[0].to_audit_dict(),
        )


# ── Helpers ─────────────────────────────────────────────────────


def _flag_state(*, mode=Mode.LIVE, enabled=True) -> FlagState:
    return FlagState(
        tenant_id="t1",
        feature_key="knowledge_echo",
        enabled=enabled,
        mode=mode,
        config={},
        version=1,
        circuit_state=CircuitState.CLOSED,
    )


def _classifier_yes(*, products=()) -> ClassifierOutput:
    return ClassifierOutput(
        is_question=True,
        confidence=0.95,
        question_type="policy",
        rationale_short="",
        product_hints=list(products),
    )


def _candidate(similarity: float) -> MatchCandidate:
    return MatchCandidate(
        node_id="n1",
        node_type="TranscriptSegment",
        similarity=similarity,
        text="CA cigar lookback is 24 months.",
        speaker_id="priya",
        speaker_role="underwriting",
        session_id="sess",
        artifact_id="art",
        start_ms=0,
        end_ms=1,
        ordinal=0,
        product_ids=("lt5",),
        raw={},
    )


def _match(sim: float) -> MatchResult:
    if sim <= 0:
        return MatchResult(
            candidates=[], top_similarity=0.0, confidence_band="none"
        )
    band = "high" if sim >= 0.85 else "medium" if sim >= 0.65 else "low"
    return MatchResult(
        candidates=[_candidate(sim)],
        top_similarity=sim,
        confidence_band=band,  # type: ignore[arg-type]
    )


def _registry(dispatcher) -> SurfaceRegistry:
    return SurfaceRegistry(
        [
            SurfaceHandler(
                surface="slack",
                composer=_RecordingComposer(),
                dispatcher=dispatcher,
            )
        ]
    )


def _build(
    *,
    flags: _FakeFlags,
    matcher: _FakeMatcher,
    repo: _FakeRepo,
    dispatcher,
    policy: Optional[_FakePolicy] = None,
    gap_recorder: Optional[_FakeGapRecorder] = None,
) -> EchoOrchestrator:
    return EchoOrchestrator(
        feature_flags=flags,
        feature_key="knowledge_echo",
        classifier=_FakeClassifier(_classifier_yes(products=["lt5"])),
        matcher=matcher,
        dispatches=repo,
        dedup_window_seconds=3600,
        max_match_candidates=5,
        min_confidence_high=0.85,
        min_confidence_medium=0.65,
        end_to_end_timeout_seconds=10.0,
        surfaces=_registry(dispatcher),
        channel_policy=policy,
        gap_recorder=gap_recorder,
    )


def _input(*, channel="C1") -> EchoInput:
    return EchoInput(
        tenant_id="t1",
        trigger_surface="slack",
        trigger_plugin_event_id="Ev1",
        user_id_ext="U1",
        channel_id_ext=channel,
        text="What is the CA tobacco lookback?",
        sender=SenderContext(surface="slack"),
    )


# ── Policy gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_blocks_suppression() -> None:
    dispatcher = _RecordingDispatcher()
    policy = _FakePolicy(
        decision=PolicyDecision(allow=False, reason="channel_muted")
    )
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=dispatcher,
        policy=policy,
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_disabled"
    assert "channel_muted" in result.decision_reason
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_policy_forces_dm_only() -> None:
    dispatcher = _RecordingDispatcher()
    policy = _FakePolicy(
        decision=PolicyDecision(
            allow=True, forced_mode="dm_only", reason="policy_applied"
        )
    )
    orch = _build(
        flags=_FakeFlags(state=_flag_state(mode=Mode.LIVE)),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=dispatcher,
        policy=policy,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_dm"
    assert dispatcher.calls[0]["as_dm"] is True


@pytest.mark.asyncio
async def test_policy_min_confidence_blocks_low_match() -> None:
    """Policy raises floor to 0.99; the 0.95 match falls below → suppressed."""
    dispatcher = _RecordingDispatcher()
    policy = _FakePolicy(
        decision=PolicyDecision(
            allow=True,
            min_confidence_override=0.99,
            reason="policy_applied",
        )
    )
    gap = _FakeGapRecorder()
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=dispatcher,
        policy=policy,
        gap_recorder=gap,
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"
    assert dispatcher.calls == []
    # Gap recorder still fires.
    assert len(gap.calls) == 1


@pytest.mark.asyncio
async def test_policy_pass_through_when_decision_allows_default_mode() -> None:
    dispatcher = _RecordingDispatcher()
    policy = _FakePolicy(
        decision=PolicyDecision(allow=True, reason="no_policy_row")
    )
    orch = _build(
        flags=_FakeFlags(state=_flag_state(mode=Mode.LIVE)),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=dispatcher,
        policy=policy,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_channel"


# ── Gap recorder ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gap_recorded_on_no_match() -> None:
    dispatcher = _RecordingDispatcher()
    gap = _FakeGapRecorder()
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0)),
        repo=_FakeRepo(),
        dispatcher=dispatcher,
        gap_recorder=gap,
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"
    assert len(gap.calls) == 1
    assert gap.calls[0].asker_user_id_ext == "U1"
    assert "no_match" in gap.calls[0].reason


@pytest.mark.asyncio
async def test_gap_recorded_on_low_confidence() -> None:
    gap = _FakeGapRecorder()
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.4)),
        repo=_FakeRepo(),
        dispatcher=_RecordingDispatcher(),
        gap_recorder=gap,
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"
    assert len(gap.calls) == 1
    assert "low_conf" in gap.calls[0].reason
    assert gap.calls[0].product_ids == ("lt5",)


@pytest.mark.asyncio
async def test_gap_not_recorded_on_successful_match() -> None:
    gap = _FakeGapRecorder()
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=_RecordingDispatcher(),
        gap_recorder=gap,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_channel"
    assert gap.calls == []


@pytest.mark.asyncio
async def test_gap_record_failure_does_not_break_orchestrator() -> None:
    gap = _FakeGapRecorder(raise_with=RuntimeError("db down"))
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.0)),
        repo=_FakeRepo(),
        dispatcher=_RecordingDispatcher(),
        gap_recorder=gap,
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"


@pytest.mark.asyncio
async def test_orchestrator_works_without_phase6_hooks() -> None:
    """Phase 6 hooks are optional; without them the orchestrator
    keeps Phase 2-4 behaviour."""
    orch = _build(
        flags=_FakeFlags(state=_flag_state()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        dispatcher=_RecordingDispatcher(),
        policy=None,
        gap_recorder=None,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_channel"
