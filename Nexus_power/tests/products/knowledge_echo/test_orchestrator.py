"""Echo orchestrator — branch coverage with focused doubles.

These tests use in-memory doubles for every collaborator. The goal is
to assert that *policy* (modes, dedup, confidence thresholds, suppression
decisions) is correct, independent of any external system.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from app.classifier import ClassifierOutput, SenderContext
from app.matcher import MatchCandidate, MatchResult
from app.orchestrator import EchoInput, EchoOrchestrator
from app.slack.composer import EchoCard, EchoCardComposer
from app.slack.installation import SlackInstallation, SlackInstallationError
from app.slack.client import SlackClientError, SlackPostResult

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
class _FakeInstalls:
    install: Optional[SlackInstallation] = None
    error: Optional[Exception] = None

    async def for_tenant(self, tenant_id):  # noqa: ARG002
        if self.error:
            raise self.error
        assert self.install is not None
        return self.install


@dataclass
class _FakeSlack:
    raise_on_send: Optional[Exception] = None
    sent_calls: list[dict[str, Any]] = field(default_factory=list)

    async def post_message(self, **kwargs):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent_calls.append({"kind": "channel", **kwargs})
        return SlackPostResult(
            ok=True,
            channel=kwargs.get("channel"),
            ts="1620000000.0001",
            raw={"ok": True},
        )

    async def post_dm(self, **kwargs):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent_calls.append({"kind": "dm", **kwargs})
        return SlackPostResult(
            ok=True,
            channel="D-fake",
            ts="1620000000.0002",
            raw={"ok": True},
        )

    async def post_ephemeral(self, **kwargs):
        self.sent_calls.append({"kind": "ephemeral", **kwargs})
        return SlackPostResult(
            ok=True,
            channel=kwargs.get("channel"),
            ts=None,
            raw={"ok": True},
        )


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

    async def claim_or_get_existing(
        self, *, tenant_id, dedup_key, dispatch_id, ttl_seconds  # noqa: ARG002
    ):
        if self.dedup_collide_with is None:
            return None
        from app.dispatches import DedupHit

        return DedupHit(
            existing_dispatch_id=self.dedup_collide_with,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=ttl_seconds),
        )

    async def record_feedback(self, **kwargs):  # noqa: ARG002
        return True

    async def recent_decisions(self, tenant_id, *, limit=50):  # noqa: ARG002
        return []


# ── Helpers ─────────────────────────────────────────────────────


def _flag_state(
    *, enabled=True, mode=Mode.LIVE, circuit=CircuitState.CLOSED, config=None
) -> FlagState:
    return FlagState(
        tenant_id="t1",
        feature_key="knowledge_echo",
        enabled=enabled,
        mode=mode,
        config=config or {},
        version=1,
        circuit_state=circuit,
    )


def _classifier_yes(confidence: float = 0.9) -> ClassifierOutput:
    return ClassifierOutput(
        is_question=True,
        confidence=confidence,
        question_type="policy",
        rationale_short="",
    )


def _classifier_no() -> ClassifierOutput:
    return ClassifierOutput(
        is_question=False,
        confidence=0.9,
        question_type="other",
        rationale_short="",
    )


def _candidate(similarity: float = 0.92) -> MatchCandidate:
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


def _match(similarity: float) -> MatchResult:
    if similarity <= 0:
        return MatchResult(
            candidates=[], top_similarity=0.0, confidence_band="none"
        )
    band = (
        "high" if similarity >= 0.85
        else "medium" if similarity >= 0.65
        else "low"
    )
    return MatchResult(
        candidates=[_candidate(similarity)],
        top_similarity=similarity,
        confidence_band=band,  # type: ignore[arg-type]
    )


def _install() -> SlackInstallation:
    return SlackInstallation(
        tenant_id="t1",
        installation_id="inst-1",
        team_id="T01",
        bot_token="xoxb-test",
        signing_secret="sec",
        default_channel=None,
        status="connected",
    )


def _build_orch(
    *,
    flags: _FakeFlags,
    classifier: _FakeClassifier,
    matcher: _FakeMatcher,
    repo: _FakeRepo,
    installs: _FakeInstalls,
    slack: _FakeSlack,
    high: float = 0.85,
    medium: float = 0.65,
) -> EchoOrchestrator:
    return EchoOrchestrator(
        feature_flags=flags,
        feature_key="knowledge_echo",
        classifier=classifier,
        matcher=matcher,
        composer=EchoCardComposer(),
        slack=slack,
        installs=installs,
        dispatches=repo,
        dedup_window_seconds=3600,
        max_match_candidates=5,
        min_confidence_high=high,
        min_confidence_medium=medium,
        end_to_end_timeout_seconds=10.0,
    )


def _input(*, text: str = "What is the CA tobacco lookback?", channel="C1") -> EchoInput:
    return EchoInput(
        tenant_id="t1",
        trigger_surface="slack",
        trigger_plugin_event_id="Ev1",
        user_id_ext="U1",
        channel_id_ext=channel,
        text=text,
        sender=SenderContext(surface="slack"),
    )


# ── Disabled / circuit ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_flag_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state(enabled=False))
    repo = _FakeRepo()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
        installs=_FakeInstalls(),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_disabled"
    assert repo.dispatches[-1]["decision"] == "suppressed_disabled"


@pytest.mark.asyncio
async def test_open_circuit_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state(circuit=CircuitState.OPEN))
    repo = _FakeRepo()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
        installs=_FakeInstalls(),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_circuit"


# ── Classifier ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_a_question_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state())
    repo = _FakeRepo()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_no()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
        installs=_FakeInstalls(),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input(text="closing the loop"))
    assert result.decision == "suppressed_classifier"


@pytest.mark.asyncio
async def test_empty_text_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input(text="   "))
    assert result.decision == "suppressed_classifier"


# ── Match thresholds ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_confidence_match_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.4)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"


@pytest.mark.asyncio
async def test_no_match_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state())
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_low_conf"


# ── Dedup ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_hit_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state())
    repo = _FakeRepo(dedup_collide_with="d-prior")
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
        installs=_FakeInstalls(install=_install()),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_dup"


# ── Mode-specific dispatch ─────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_mode_does_not_send() -> None:
    flags = _FakeFlags(state=_flag_state(mode=Mode.SHADOW))
    slack = _FakeSlack()
    repo = _FakeRepo()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=repo,
        installs=_FakeInstalls(install=_install()),
        slack=slack,
    )
    result = await orch.process(_input())
    assert result.decision == "shadow_logged"
    assert slack.sent_calls == []
    assert flags.outcomes == [Outcome.SUCCESS]


@pytest.mark.asyncio
async def test_dm_only_mode_uses_dm() -> None:
    flags = _FakeFlags(state=_flag_state(mode=Mode.DM_ONLY))
    slack = _FakeSlack()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=slack,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_dm"
    assert slack.sent_calls and slack.sent_calls[0]["kind"] == "dm"


@pytest.mark.asyncio
async def test_live_high_confidence_posts_to_channel() -> None:
    flags = _FakeFlags(state=_flag_state(mode=Mode.LIVE))
    slack = _FakeSlack()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=slack,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_channel"
    assert slack.sent_calls and slack.sent_calls[0]["kind"] == "channel"
    assert result.posted_message_ref is not None


@pytest.mark.asyncio
async def test_live_medium_confidence_degrades_to_dm() -> None:
    """0.75 is above the medium threshold (0.65) but below high (0.85)."""
    flags = _FakeFlags(state=_flag_state(mode=Mode.LIVE))
    slack = _FakeSlack()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.75)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=slack,
    )
    result = await orch.process(_input())
    assert result.decision == "posted_dm"


# ── Slack failures ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slack_install_missing_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state(mode=Mode.LIVE))
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(error=SlackInstallationError("no install")),
        slack=_FakeSlack(),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_error"
    assert flags.outcomes == [Outcome.FAILURE]


@pytest.mark.asyncio
async def test_slack_post_error_suppresses() -> None:
    flags = _FakeFlags(state=_flag_state(mode=Mode.LIVE))
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=_FakeSlack(raise_on_send=SlackClientError("rate_limited")),
    )
    result = await orch.process(_input())
    assert result.decision == "suppressed_error"
    assert flags.outcomes == [Outcome.FAILURE]


# ── Tenant config overrides ──────────────────────────────────


@pytest.mark.asyncio
async def test_per_tenant_high_threshold_override() -> None:
    """Tenant config can raise the channel-post threshold."""
    flags = _FakeFlags(
        state=_flag_state(
            mode=Mode.LIVE,
            config={"min_confidence_high": 0.99},
        )
    )
    slack = _FakeSlack()
    orch = _build_orch(
        flags=flags,
        classifier=_FakeClassifier(_classifier_yes()),
        matcher=_FakeMatcher(_match(0.95)),
        repo=_FakeRepo(),
        installs=_FakeInstalls(install=_install()),
        slack=slack,
    )
    result = await orch.process(_input())
    # 0.95 is below the tenant-raised 0.99 threshold; we degrade to DM.
    assert result.decision == "posted_dm"
