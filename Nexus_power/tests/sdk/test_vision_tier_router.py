"""Tests for the vision tier router and provider abstractions.

Real provider HTTP calls are mocked; the focus is on the contract:

  - Config parses standard env vars correctly
  - Each provider class can be instantiated from a tier spec
  - Anthropic / OpenAI providers refuse to construct without an API key
  - The router prefers tier1 → tier2 → tier3, retries within a tier, and
    falls back to the next tier on retriable errors
  - Non-retriable errors poison the failing tier for the rest of the run
  - The base ``analyze()`` retry/timing wrapper records latency and stats
  - The to_eyes_dict() shim preserves the eyes-engine contract
"""
from __future__ import annotations

import os
import pytest

from nexus_sdk.vision import (
    VisionAnalysisRequest,
    VisionAnalysisResponse,
    VisionConfig,
    VisionProvider,
    VisionProviderError,
    VisionTierSpec,
    VisionUIElement,
    build_vision_router,
    register_vision_provider,
)
from nexus_sdk.vision.providers.anthropic import AnthropicVisionProvider
from nexus_sdk.vision.providers.ollama import OllamaVisionProvider
from nexus_sdk.vision.providers.openai import OpenAIVisionProvider


# ── Config tests ─────────────────────────────────────────────────────────────

def test_vision_config_from_env_picks_up_tier_specs(monkeypatch):
    """Standard env var layout maps to a configured tier spec."""
    env = {
        "EYES_VISION_TIER1_PROVIDER": "anthropic",
        "EYES_VISION_TIER1_MODEL": "claude-sonnet-4-6",
        "EYES_VISION_TIER1_API_KEY": "sk-ant-test",
        "EYES_VISION_TIER1_TIMEOUT": "60",
        "EYES_VISION_TIER1_RETRIES": "2",
        "EYES_VISION_TIER3_PROVIDER": "ollama",
        "EYES_VISION_TIER3_MODEL": "llama3.2-vision:11b",
        "EYES_VISION_TIER3_BASE_URL": "http://ollama:11434",
    }
    cfg = VisionConfig.from_env(env)
    tiers = cfg.ordered_tiers()
    assert [t.tier for t in tiers] == [1, 3]  # tier 2 absent
    t1 = tiers[0]
    assert t1.provider == "anthropic"
    assert t1.model == "claude-sonnet-4-6"
    assert t1.api_key == "sk-ant-test"
    assert t1.timeout_seconds == 60.0
    assert t1.retries == 2
    t3 = tiers[1]
    assert t3.provider == "ollama"
    assert t3.base_url == "http://ollama:11434"


def test_vision_config_handles_custom_tier_order(monkeypatch):
    env = {
        "EYES_VISION_TIER1_PROVIDER": "anthropic",
        "EYES_VISION_TIER1_MODEL": "claude-sonnet-4-6",
        "EYES_VISION_TIER1_API_KEY": "sk-ant-test",
        "EYES_VISION_TIER3_PROVIDER": "ollama",
        "EYES_VISION_TIER3_MODEL": "llava:7b",
        "EYES_VISION_TIER_ORDER": "3,1",  # prefer local
    }
    cfg = VisionConfig.from_env(env)
    tiers = cfg.ordered_tiers()
    assert [t.tier for t in tiers] == [3, 1]


def test_vision_config_unconfigured_returns_no_tiers():
    cfg = VisionConfig.from_env({})
    assert cfg.ordered_tiers() == []


def test_vision_config_invalid_timeout_falls_back_to_default():
    env = {
        "EYES_VISION_TIER1_PROVIDER": "ollama",
        "EYES_VISION_TIER1_MODEL": "llava:7b",
        "EYES_VISION_TIER1_TIMEOUT": "not-a-number",
    }
    cfg = VisionConfig.from_env(env)
    assert cfg.ordered_tiers()[0].timeout_seconds == 120.0


# ── Provider construction tests ──────────────────────────────────────────────

def test_anthropic_provider_refuses_without_api_key():
    spec = VisionTierSpec(tier=1, provider="anthropic", model="claude-sonnet-4-6")
    with pytest.raises(VisionProviderError) as exc_info:
        AnthropicVisionProvider(spec)
    assert "missing api_key" in str(exc_info.value).lower()
    assert exc_info.value.retriable is False


def test_openai_provider_refuses_without_api_key():
    spec = VisionTierSpec(tier=2, provider="openai", model="gpt-4o")
    with pytest.raises(VisionProviderError):
        OpenAIVisionProvider(spec)


def test_ollama_provider_constructs_without_api_key():
    spec = VisionTierSpec(tier=3, provider="ollama", model="llava:7b")
    provider = OllamaVisionProvider(spec)
    assert provider.name == "ollama:llava:7b"
    assert provider.model == "llava:7b"
    assert provider.retries == 1


# ── Response shape tests ─────────────────────────────────────────────────────

def test_vision_analysis_response_to_eyes_dict_preserves_shape():
    response = VisionAnalysisResponse(
        description="Birthdate form",
        ui_elements=[
            VisionUIElement(
                element_type="text_field",
                text="Year",
                confidence=0.92,
                properties={"placeholder": "YYYY"},
            ),
            VisionUIElement(element_type="button", text="Continue", confidence=0.99),
        ],
        tables=[[["A", "B"], ["1", "2"]]],
        page_title="Personal Information — Birthdate",
    )
    d = response.to_eyes_dict()
    assert d["description"] == "Birthdate form"
    assert d["page_title"] == "Personal Information — Birthdate"
    assert isinstance(d["ui_elements"], list) and len(d["ui_elements"]) == 2
    assert d["ui_elements"][0]["element_type"] == "text_field"
    assert d["ui_elements"][0]["properties"]["placeholder"] == "YYYY"
    assert d["tables"] == [[["A", "B"], ["1", "2"]]]


# ── Tier router behavior ─────────────────────────────────────────────────────

class _RecordingStubProvider(VisionProvider):
    """A scriptable stub for router behavior tests.

    ``script`` is a list of behaviours to apply to successive analyze
    calls.  Each behaviour is one of:

      "ok"                       — return a successful response
      "retry-once-then-ok"       — first call raises retriable, second OK
      VisionProviderError(...)   — raise on every call (retriable or not)
    """

    def __init__(self, *, name: str, script: list, retries: int = 1):
        super().__init__(name=name, model=f"stub-{name}", retries=retries)
        self._script = list(script)
        self.call_count = 0

    async def initialize(self) -> None:
        self._initialized = True

    async def health(self) -> dict:
        return {"healthy": self._initialized, "provider": self.name}

    async def _do_analyze(self, request: VisionAnalysisRequest) -> VisionAnalysisResponse:
        self.call_count += 1
        if not self._script:
            return VisionAnalysisResponse(description=f"{self.name}_fallthrough_ok")
        action = self._script.pop(0)
        if isinstance(action, VisionProviderError):
            raise action
        if action == "ok":
            return VisionAnalysisResponse(description=f"{self.name}_ok")
        if action == "retry-once-then-ok":
            self._script.insert(0, "ok")
            raise VisionProviderError("transient", provider=self.name, retriable=True)
        raise AssertionError(f"unknown script action: {action!r}")


@pytest.mark.asyncio
async def test_router_uses_tier1_when_healthy():
    from nexus_sdk.vision.tiered import VisionTierRouter

    p1 = _RecordingStubProvider(name="t1", script=["ok"])
    p2 = _RecordingStubProvider(name="t2", script=["ok"])
    router = VisionTierRouter(providers=[p1, p2])
    await router.initialize()
    response = await router.analyze_frame(VisionAnalysisRequest(frame_path="dummy.png"))
    assert response.description == "t1_ok"
    assert p1.call_count == 1
    assert p2.call_count == 0


@pytest.mark.asyncio
async def test_router_falls_back_on_retriable_failure():
    from nexus_sdk.vision.tiered import VisionTierRouter

    p1 = _RecordingStubProvider(
        name="t1",
        script=[VisionProviderError("flaky", provider="t1", retriable=True)],
        retries=0,  # no per-tier retry to keep test fast
    )
    p2 = _RecordingStubProvider(name="t2", script=["ok"])
    router = VisionTierRouter(providers=[p1, p2])
    await router.initialize()
    response = await router.analyze_frame(VisionAnalysisRequest(frame_path="dummy.png"))
    assert response.description == "t2_ok"
    assert p1.call_count == 1
    assert p2.call_count == 1


@pytest.mark.asyncio
async def test_router_poisons_tier_on_non_retriable_failure():
    from nexus_sdk.vision.tiered import VisionTierRouter

    p1 = _RecordingStubProvider(
        name="t1",
        script=[
            VisionProviderError("auth", provider="t1", retriable=False),
            "ok",  # would succeed if it were retried, but it's poisoned
        ],
        retries=0,
    )
    p2 = _RecordingStubProvider(name="t2", script=["ok", "ok"])
    router = VisionTierRouter(providers=[p1, p2])
    await router.initialize()
    # First call: t1 fails non-retriably, t2 succeeds
    r1 = await router.analyze_frame(VisionAnalysisRequest(frame_path="dummy.png"))
    assert r1.description == "t2_ok"
    # Second call: t1 is poisoned, router skips straight to t2
    r2 = await router.analyze_frame(VisionAnalysisRequest(frame_path="dummy.png"))
    assert r2.description == "t2_ok"
    assert p1.call_count == 1  # only the first attempt
    assert p2.call_count == 2
    assert router.healthy_tier_count == 1


@pytest.mark.asyncio
async def test_router_raises_when_all_tiers_fail():
    from nexus_sdk.vision.tiered import VisionTierRouter

    p1 = _RecordingStubProvider(
        name="t1",
        script=[VisionProviderError("flaky", provider="t1", retriable=True)],
        retries=0,
    )
    p2 = _RecordingStubProvider(
        name="t2",
        script=[VisionProviderError("flaky", provider="t2", retriable=True)],
        retries=0,
    )
    router = VisionTierRouter(providers=[p1, p2])
    await router.initialize()
    with pytest.raises(VisionProviderError) as exc_info:
        await router.analyze_frame(VisionAnalysisRequest(frame_path="dummy.png"))
    assert "all configured vision tiers failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_router_initialize_raises_when_no_tiers_configured():
    from nexus_sdk.vision.tiered import VisionTierRouter

    router = VisionTierRouter(providers=[])
    with pytest.raises(VisionProviderError):
        await router.initialize()


@pytest.mark.asyncio
async def test_provider_base_retry_counts_failures():
    """The base analyze() wraps _do_analyze with retry; failure counts surface in stats."""
    p = _RecordingStubProvider(
        name="metrics",
        script=["retry-once-then-ok"],
        retries=2,
    )
    await p.initialize()
    response = await p.analyze(VisionAnalysisRequest(frame_path="dummy.png"))
    assert response.description == "metrics_ok"
    stats = p.stats()
    assert stats["total_calls"] == 1
    assert stats["total_failures"] == 1
    assert stats["average_latency_ms"] >= 0.0


# ── Registry tests ──────────────────────────────────────────────────────────

def test_register_custom_provider_lets_router_pick_it_up(monkeypatch):
    from nexus_sdk.vision import REGISTERED_VISION_PROVIDERS

    sentinel: dict[str, object] = {}

    class CustomVision(VisionProvider):
        def __init__(self, spec: VisionTierSpec):
            super().__init__(name="custom", model=spec.model)
            sentinel["spec"] = spec

        async def initialize(self):
            self._initialized = True

        async def health(self):
            return {"healthy": True, "provider": self.name}

        async def _do_analyze(self, request):
            return VisionAnalysisResponse(description="from-custom")

    register_vision_provider("customx", lambda spec: CustomVision(spec))
    try:
        cfg = VisionConfig(tiers=[VisionTierSpec(
            tier=1, provider="customx", model="m1",
        )])
        router = build_vision_router(cfg)
        assert router.configured_tier_count == 1
        assert sentinel["spec"].provider == "customx"
    finally:
        REGISTERED_VISION_PROVIDERS.pop("customx", None)


def test_build_vision_router_skips_unconfigured_tiers_without_failing():
    cfg = VisionConfig(tiers=[
        VisionTierSpec(tier=1, provider="anthropic", model="claude-sonnet-4-6"),  # no api_key
        VisionTierSpec(tier=3, provider="ollama", model="llava:7b"),
    ])
    router = build_vision_router(cfg)
    # Tier 1 missing api_key surfaces during construction inside the registry,
    # so it should be skipped and tier 3 should remain.
    assert router.configured_tier_count == 1


def test_build_vision_router_rejects_unknown_provider():
    cfg = VisionConfig(tiers=[
        VisionTierSpec(tier=1, provider="not-a-real-provider", model="m"),
    ])
    router = build_vision_router(cfg)
    # Unknown provider is logged and skipped; if no other tier is set the
    # router has zero tiers and initialize() will raise.
    assert router.configured_tier_count == 0


# ── Transition (two-image) router behavior ──────────────────────────────────


class _RecordingTransitionStub(VisionProvider):
    """Like _RecordingStubProvider but for the transition (two-image)
    path. Tracks calls separately so tests can assert per-method usage.
    """

    def __init__(self, *, name: str, script: list, retries: int = 1):
        super().__init__(name=name, model=f"stub-{name}", retries=retries)
        self._script = list(script)
        self.transition_calls = 0

    async def initialize(self) -> None:
        self._initialized = True

    async def health(self) -> dict:
        return {"healthy": self._initialized, "provider": self.name}

    async def _do_analyze(self, request):
        # Not exercised in transition tests, but required by ABC.
        return VisionAnalysisResponse(description=f"{self.name}_frame_ok")

    async def _do_analyze_transition(self, request):
        from nexus_sdk.vision.base import VisionTransitionResponse
        self.transition_calls += 1
        if not self._script:
            return VisionTransitionResponse(
                kind="click_cta",
                action_label=f"{self.name}_default",
                confidence=0.7,
            )
        action = self._script.pop(0)
        if isinstance(action, VisionProviderError):
            raise action
        if action == "ok":
            return VisionTransitionResponse(
                kind="navigate",
                action_label=f"{self.name}_navigated",
                target_element_label="Continue",
                confidence=0.85,
                evidence_text="URL changed",
            )
        raise AssertionError(f"unknown action: {action}")


@pytest.mark.asyncio
async def test_router_transition_uses_tier1_when_healthy():
    from nexus_sdk.vision.tiered import VisionTierRouter
    from nexus_sdk.vision import VisionTransitionRequest

    t1 = _RecordingTransitionStub(name="t1", script=["ok"])
    t2 = _RecordingTransitionStub(name="t2", script=["ok"])
    router = VisionTierRouter(providers=[t1, t2])
    await router.initialize()
    response = await router.analyze_transition(VisionTransitionRequest(
        before_frame_path="b.png", after_frame_path="a.png",
    ))
    assert response.kind == "navigate"
    assert response.action_label == "t1_navigated"
    assert t1.transition_calls == 1
    assert t2.transition_calls == 0


@pytest.mark.asyncio
async def test_router_transition_falls_back_on_retriable():
    from nexus_sdk.vision.tiered import VisionTierRouter
    from nexus_sdk.vision import VisionTransitionRequest

    t1 = _RecordingTransitionStub(
        name="t1",
        script=[VisionProviderError("flaky", provider="t1", retriable=True)],
        retries=0,
    )
    t2 = _RecordingTransitionStub(name="t2", script=["ok"])
    router = VisionTierRouter(providers=[t1, t2])
    await router.initialize()
    response = await router.analyze_transition(VisionTransitionRequest(
        before_frame_path="b.png", after_frame_path="a.png",
    ))
    assert response.action_label == "t2_navigated"
    assert t1.transition_calls == 1
    assert t2.transition_calls == 1


@pytest.mark.asyncio
async def test_router_transition_poisons_independent_of_frame_path():
    """Transition poisoning must not disable single-frame calls on the
    same provider — different model, different API capability."""
    from nexus_sdk.vision.tiered import VisionTierRouter
    from nexus_sdk.vision import VisionTransitionRequest, VisionAnalysisRequest

    t1 = _RecordingTransitionStub(
        name="t1",
        script=[
            VisionProviderError("auth", provider="t1", retriable=False),
            "ok", "ok",  # would succeed if not poisoned (transition path only)
        ],
    )
    t2 = _RecordingTransitionStub(name="t2", script=["ok", "ok"])
    router = VisionTierRouter(providers=[t1, t2])
    await router.initialize()

    # First transition: t1 fails non-retriably → poisoned for transitions.
    await router.analyze_transition(VisionTransitionRequest(
        before_frame_path="b.png", after_frame_path="a.png",
    ))
    # Second transition: t1 skipped (poisoned for transitions); t2 serves.
    r2 = await router.analyze_transition(VisionTransitionRequest(
        before_frame_path="b.png", after_frame_path="a.png",
    ))
    assert r2.action_label == "t2_navigated"
    assert t1.transition_calls == 1
    assert t2.transition_calls == 2

    # CRITICAL: single-frame analyze on t1 must still work — transition
    # poisoning is path-specific.
    frame_response = await router.analyze_frame(VisionAnalysisRequest(
        frame_path="dummy.png",
    ))
    assert frame_response.description == "t1_frame_ok"


@pytest.mark.asyncio
async def test_router_transition_raises_when_all_tiers_fail():
    from nexus_sdk.vision.tiered import VisionTierRouter
    from nexus_sdk.vision import VisionTransitionRequest

    t1 = _RecordingTransitionStub(
        name="t1",
        script=[VisionProviderError("a", provider="t1", retriable=True)],
        retries=0,
    )
    t2 = _RecordingTransitionStub(
        name="t2",
        script=[VisionProviderError("b", provider="t2", retriable=True)],
        retries=0,
    )
    router = VisionTierRouter(providers=[t1, t2])
    await router.initialize()
    with pytest.raises(VisionProviderError) as exc_info:
        await router.analyze_transition(VisionTransitionRequest(
            before_frame_path="b.png", after_frame_path="a.png",
        ))
    assert "transition" in str(exc_info.value).lower()


def test_transition_response_to_eyes_dict_matches_legacy_shape():
    """The dict produced by VisionTransitionResponse.to_eyes_dict() must
    match the keys that eyes-engine's _analyze_scene_transitions_llm
    consumes. Breaking this contract regresses every video workflow."""
    from nexus_sdk.vision import VisionTransitionResponse
    response = VisionTransitionResponse(
        kind="click_cta",
        action_label="Clicked Continue",
        target_element_label="Continue button",
        observed_value="",
        confidence=0.91,
        evidence_text="New URL visible after click",
    )
    d = response.to_eyes_dict()
    # Required legacy keys — preserve binary compatibility with the
    # canonical processing pipeline's transition consumer.
    assert d["action_kind"] == "click_cta"
    assert d["action_label"] == "Clicked Continue"
    assert d["target_element_label"] == "Continue button"
    assert d["observed_value"] == ""
    assert d["confidence"] == 0.91
    assert d["evidence_text"] == "New URL visible after click"


def test_transition_parser_clamps_invalid_action_kind():
    from nexus_sdk.vision.providers._transition_shared import parse_transition_response
    # Made-up kind → collapses to "unknown"
    out = parse_transition_response(
        content_str='{"action_kind":"hyperclick","confidence":0.9}',
        raw={}, model="test",
    )
    assert out.kind == "unknown"
    # Confidence still preserved
    assert out.confidence == 0.9


def test_transition_parser_clamps_confidence_to_unit_interval():
    from nexus_sdk.vision.providers._transition_shared import parse_transition_response
    out_high = parse_transition_response(
        content_str='{"action_kind":"navigate","confidence":2.5}',
        raw={}, model="test",
    )
    out_neg = parse_transition_response(
        content_str='{"action_kind":"navigate","confidence":-0.3}',
        raw={}, model="test",
    )
    assert out_high.confidence == 1.0
    assert out_neg.confidence == 0.0


def test_transition_default_implementation_raises_unsupported():
    """Providers that don't override _do_analyze_transition must surface
    that as a non-retriable error so the router skips them and tries the
    next tier. This is the safety net for partially-implemented providers."""
    from nexus_sdk.vision.base import VisionProvider, VisionTransitionRequest

    class _NoTransitionImpl(VisionProvider):
        def __init__(self):
            super().__init__(name="no-trans", model="m")
            self._initialized = True
        async def initialize(self): self._initialized = True
        async def health(self): return {"healthy": True}
        async def _do_analyze(self, request):
            return VisionAnalysisResponse(description="frame")

    provider = _NoTransitionImpl()
    import asyncio
    with pytest.raises(VisionProviderError) as exc_info:
        asyncio.run(provider.analyze_transition(VisionTransitionRequest(
            before_frame_path="b.png", after_frame_path="a.png",
        )))
    assert "does not implement" in str(exc_info.value).lower()
    assert exc_info.value.retriable is False
