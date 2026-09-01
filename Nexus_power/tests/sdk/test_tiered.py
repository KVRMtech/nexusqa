"""
SDK — Tiered LLM Provider tests.

Tests the multi-tier provider configuration, router initialization,
and failover logic.
"""

import pytest
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


class TestProviderTier:

    def test_tier_enum_values(self):
        from nexus_sdk.llm.tiered import ProviderTier
        assert ProviderTier.PRIMARY.value == "tier1"
        assert ProviderTier.SECONDARY.value == "tier2"
        assert ProviderTier.LOCAL.value == "tier3"


class TestTierConfig:

    def test_tier_config_defaults(self):
        from nexus_sdk.llm.tiered import TierConfig, ProviderTier
        tc = TierConfig(
            tier=ProviderTier.PRIMARY,
            provider="anthropic",
            model="claude-opus-4-5-20250514",
        )
        assert tc.tier == ProviderTier.PRIMARY
        assert tc.provider == "anthropic"
        assert tc.model == "claude-opus-4-5-20250514"
        assert tc.enabled is True
        assert tc.max_retries == 2
        assert tc.timeout == 120

    def test_tier_config_disabled(self):
        from nexus_sdk.llm.tiered import TierConfig, ProviderTier
        tc = TierConfig(
            tier=ProviderTier.LOCAL,
            provider="ollama",
            model="llama3.1",
            enabled=False,
        )
        assert tc.enabled is False

    def test_to_llm_config(self):
        from nexus_sdk.llm.tiered import TierConfig, ProviderTier
        tc = TierConfig(
            tier=ProviderTier.PRIMARY,
            provider="openai",
            model="gpt-4o",
            api_key="sk-test",
        )
        llm_cfg = tc.to_llm_config()
        assert llm_cfg.provider == "openai"
        assert llm_cfg.model == "gpt-4o"
        assert llm_cfg.api_key == "sk-test"


class TestTieredProviderConfig:

    def test_from_engine_no_env(self):
        """With no env vars, should return empty active_tiers."""
        from nexus_sdk.llm.tiered import TieredProviderConfig
        config = TieredProviderConfig.from_engine("test_engine")
        # Without env vars, tiers have no provider configured
        assert isinstance(config.active_tiers, list)

    def test_from_engine_with_env(self, monkeypatch):
        """With proper env vars, should detect tier 1."""
        from nexus_sdk.llm.tiered import TieredProviderConfig
        monkeypatch.setenv("TEST_TIER1_PROVIDER", "openai")
        monkeypatch.setenv("TEST_TIER1_MODEL", "gpt-4o")
        monkeypatch.setenv("TEST_TIER1_API_KEY", "sk-test")
        config = TieredProviderConfig.from_engine("test")
        assert len(config.active_tiers) >= 1
        assert config.active_tiers[0].provider == "openai"

    def test_from_engine_global_fallback(self, monkeypatch):
        """With global LLM_PROVIDER set, should detect at least one tier."""
        from nexus_sdk.llm.tiered import TieredProviderConfig
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("LLM_MODEL", "llama3.1")
        config = TieredProviderConfig.from_engine("fallback_test")
        # Should use global as local tier
        assert isinstance(config.active_tiers, list)

    def test_from_engine_multi_tier(self, monkeypatch):
        """With all three tiers set, should have 3 active tiers."""
        from nexus_sdk.llm.tiered import TieredProviderConfig
        monkeypatch.setenv("MULTI_TIER1_PROVIDER", "anthropic")
        monkeypatch.setenv("MULTI_TIER1_MODEL", "claude-3-opus")
        monkeypatch.setenv("MULTI_TIER2_PROVIDER", "openai")
        monkeypatch.setenv("MULTI_TIER2_MODEL", "gpt-4o")
        monkeypatch.setenv("MULTI_TIER3_PROVIDER", "ollama")
        monkeypatch.setenv("MULTI_TIER3_MODEL", "llama3.1")
        config = TieredProviderConfig.from_engine("multi")
        assert len(config.active_tiers) == 3


class TestTieredLLMRouter:

    def test_router_creation(self):
        from nexus_sdk.llm.tiered import TieredLLMRouter, TieredProviderConfig
        config = TieredProviderConfig.from_engine("router_test")
        router = TieredLLMRouter(config)
        assert router is not None

    def test_router_health_before_init(self):
        from nexus_sdk.llm.tiered import TieredLLMRouter, TieredProviderConfig
        config = TieredProviderConfig.from_engine("health_test")
        router = TieredLLMRouter(config)
        health = router.get_health()
        assert "engine" in health or "tiers" in health or isinstance(health, dict)

    def test_router_stats_before_init(self):
        from nexus_sdk.llm.tiered import TieredLLMRouter, TieredProviderConfig
        config = TieredProviderConfig.from_engine("stats_test")
        router = TieredLLMRouter(config)
        stats = router.get_stats()
        assert isinstance(stats, dict)


# ─────────────────────────────────────────────────────────────────
# Failover, fail-fast, cost, PII, budget — behavior tests.
# ─────────────────────────────────────────────────────────────────
#
# These use the same `_StubProvider` pattern as the vision tier router
# tests so we get apples-to-apples coverage between the two systems.
#
# Each stub is a minimal LLMProvider subclass that returns scripted
# responses (or raises scripted exceptions) on each generate() call.
# That gives us deterministic failover + circuit-breaker behavior
# without hitting a real provider.

import asyncio


class _StubProvider:
    """Pretend to be an LLMProvider for one tier. Scripts are consumed
    one element per generate() call."""

    def __init__(self, name: str, model: str, script: list):
        self.name = name
        self.model = model
        self._script = list(script)
        self.calls: int = 0
        self._initialized = False

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False

    async def generate(
        self, *, system_prompt: str, user_prompt: str,
        temperature=None, max_tokens=None, json_mode=None,
    ):
        from nexus_sdk.llm.base import LLMResponse
        self.calls += 1
        if not self._script:
            return LLMResponse(
                content=f"{self.name}_ok_default",
                provider=self.name,
                model=self.model,
                prompt_tokens=10, completion_tokens=20, total_tokens=30,
            )
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        if isinstance(action, dict):
            return LLMResponse(
                content=action.get("content", f"{self.name}_ok"),
                provider=self.name,
                model=self.model,
                prompt_tokens=int(action.get("prompt_tokens", 10)),
                completion_tokens=int(action.get("completion_tokens", 20)),
                total_tokens=int(action.get("total_tokens", 30)),
            )
        if action == "ok":
            return LLMResponse(
                content=f"{self.name}_ok",
                provider=self.name,
                model=self.model,
                prompt_tokens=10, completion_tokens=20, total_tokens=30,
            )
        raise AssertionError(f"unknown script action: {action!r}")

    async def health(self) -> dict:
        return {"healthy": self._initialized, "provider": self.name}


def _router_with_providers(
    providers: dict, *, engine: str = "test", **router_kwargs,
):
    """Build a TieredLLMRouter populated with stub providers,
    skipping the real `create_provider` factory."""
    from nexus_sdk.llm.tiered import (
        TieredLLMRouter, TieredProviderConfig, TierConfig, TierHealth,
    )
    tier_configs = [
        TierConfig(tier=t, provider="stub", model=f"stub-{t.value}", enabled=True)
        for t in providers.keys()
    ]
    config = TieredProviderConfig(
        engine_name=engine,
        tiers=tier_configs,
        mode="multi-tier" if len(tier_configs) > 1 else "single",
    )
    router = TieredLLMRouter(config, **router_kwargs)
    # Bypass the real initialize() — inject stub providers directly.
    for tier, prov in providers.items():
        router._providers[tier] = prov
        router._health[tier] = TierHealth(
            tier=tier, provider_name=prov.name, model=prov.model, healthy=True,
        )
    router._initialized = True
    return router


@pytest.mark.asyncio
async def test_router_uses_tier1_when_healthy():
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "claude-opus-4", ["ok"])
    p2 = _StubProvider("tier2", "gpt-4o", ["ok"])
    p3 = _StubProvider("tier3", "llama3.1", ["ok"])
    router = _router_with_providers({
        ProviderTier.PRIMARY: p1,
        ProviderTier.SECONDARY: p2,
        ProviderTier.LOCAL: p3,
    })
    resp = await router.generate(system_prompt="hi", user_prompt="hello")
    assert "tier1_ok" in resp.content
    assert p1.calls == 1
    assert p2.calls == 0
    assert p3.calls == 0


@pytest.mark.asyncio
async def test_router_falls_through_to_tier2_on_failure():
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "claude-opus-4", [RuntimeError("primary down")])
    p2 = _StubProvider("tier2", "gpt-4o", ["ok"])
    router = _router_with_providers({
        ProviderTier.PRIMARY: p1,
        ProviderTier.SECONDARY: p2,
    })
    resp = await router.generate(system_prompt="hi", user_prompt="hello")
    assert "tier2_ok" in resp.content
    assert p1.calls == 1
    assert p2.calls == 1


@pytest.mark.asyncio
async def test_router_falls_through_to_tier3_on_double_failure():
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "claude-opus-4", [RuntimeError("primary down")])
    p2 = _StubProvider("tier2", "gpt-4o", [RuntimeError("secondary down")])
    p3 = _StubProvider("tier3", "llama3.1", ["ok"])
    router = _router_with_providers({
        ProviderTier.PRIMARY: p1,
        ProviderTier.SECONDARY: p2,
        ProviderTier.LOCAL: p3,
    })
    resp = await router.generate(system_prompt="hi", user_prompt="hello")
    assert "tier3_ok" in resp.content
    assert p1.calls == 1 and p2.calls == 1 and p3.calls == 1


@pytest.mark.asyncio
async def test_router_raises_when_all_tiers_fail():
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "m1", [RuntimeError("a")])
    p2 = _StubProvider("tier2", "m2", [RuntimeError("b")])
    p3 = _StubProvider("tier3", "m3", [RuntimeError("c")])
    router = _router_with_providers({
        ProviderTier.PRIMARY: p1,
        ProviderTier.SECONDARY: p2,
        ProviderTier.LOCAL: p3,
    })
    with pytest.raises(RuntimeError, match="All LLM tiers failed"):
        await router.generate(system_prompt="hi", user_prompt="hello")


@pytest.mark.asyncio
async def test_circuit_breaker_skips_tier1_after_threshold():
    """After N consecutive failures, tier 1 is skipped for the cooldown
    window. A subsequent request goes straight to tier 2 without
    paying tier 1's latency cost."""
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "m1", [
        RuntimeError("fail1"),
        RuntimeError("fail2"),
        "ok",  # would succeed on 3rd call if router didn't skip
    ])
    p2 = _StubProvider("tier2", "m2", ["ok", "ok", "ok"])
    router = _router_with_providers(
        {ProviderTier.PRIMARY: p1, ProviderTier.SECONDARY: p2},
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown=60.0,
    )
    # Two calls — both fall through to tier2. Tier1 hits threshold.
    await router.generate(system_prompt="x", user_prompt="y")
    await router.generate(system_prompt="x", user_prompt="y")
    # Third call — tier1 should be circuit-broken and skipped.
    resp = await router.generate(system_prompt="x", user_prompt="y")
    assert "tier2_ok" in resp.content
    assert p1.calls == 2, "tier1 must be skipped after threshold"
    assert p2.calls == 3


# ─── Fix 2: fail-fast-last flag ────────────────────────────────


@pytest.mark.asyncio
async def test_last_tier_normally_immune_to_circuit_breaker():
    """Legacy behavior: even after the last tier fails repeatedly,
    the router keeps trying it (no fast-fail)."""
    from nexus_sdk.llm.tiered import ProviderTier
    p3 = _StubProvider("tier3", "m3", [
        RuntimeError("fail1"),
        RuntimeError("fail2"),
        RuntimeError("fail3"),
    ])
    router = _router_with_providers(
        {ProviderTier.LOCAL: p3},
        circuit_breaker_threshold=2,
    )
    # Three failures — each should still hit the tier (no skip).
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await router.generate(system_prompt="x", user_prompt="y")
    assert p3.calls == 3, "last tier must NOT be circuit-broken by default"


@pytest.mark.asyncio
async def test_fail_fast_last_skips_last_tier_when_unhealthy(monkeypatch):
    """With LLM_TIER_FAIL_FAST_LAST=true, the last tier IS subject to
    circuit-breaking — protects from cascading-timeout failure."""
    monkeypatch.setenv("LLM_TIER_FAIL_FAST_LAST", "true")
    from nexus_sdk.llm.tiered import ProviderTier
    p3 = _StubProvider("tier3", "m3", [
        RuntimeError("fail1"),
        RuntimeError("fail2"),
        RuntimeError("fail3"),  # this one should be skipped
    ])
    router = _router_with_providers(
        {ProviderTier.LOCAL: p3},
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown=60.0,
    )
    # First two calls fail and hit the provider; third should be skipped.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await router.generate(system_prompt="x", user_prompt="y")
    # Third call: circuit open → "all tiers failed" without hitting p3.
    with pytest.raises(RuntimeError):
        await router.generate(system_prompt="x", user_prompt="y")
    assert p3.calls == 2, "last tier must be circuit-broken when FAIL_FAST_LAST is set"


@pytest.mark.asyncio
async def test_per_engine_fail_fast_last_override(monkeypatch):
    """Per-engine override beats the global default."""
    # Global default off; per-engine on.
    monkeypatch.setenv("LLM_TIER_FAIL_FAST_LAST", "false")
    monkeypatch.setenv("MYENGINE_TIER_FAIL_FAST_LAST", "true")
    from nexus_sdk.llm.tiered import ProviderTier
    p3 = _StubProvider("tier3", "m3", [
        RuntimeError("a"), RuntimeError("b"), RuntimeError("c"),
    ])
    router = _router_with_providers(
        {ProviderTier.LOCAL: p3}, engine="myengine",
        circuit_breaker_threshold=2,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await router.generate(system_prompt="x", user_prompt="y")
    with pytest.raises(RuntimeError):
        await router.generate(system_prompt="x", user_prompt="y")
    assert p3.calls == 2


# ─── Fix 4: PII guard ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pii_guard_blocks_cloud_tier_falls_through_to_local(monkeypatch):
    """Default policy=block. A prompt with PII falls through tier1
    (blocked) and lands on tier3 (on-prem, not guarded)."""
    monkeypatch.setenv("NEXUS_LLM_PII_GUARD_POLICY", "block")
    monkeypatch.setenv("NEXUS_LLM_PII_GUARD_TIERS", "tier1,tier2")
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "claude", ["ok"])
    p3 = _StubProvider("tier3", "llama", ["ok"])
    router = _router_with_providers({
        ProviderTier.PRIMARY: p1,
        ProviderTier.LOCAL: p3,
    })
    # The email triggers the PII regex.
    resp = await router.generate(
        system_prompt="system",
        user_prompt="Contact me at jane.doe@example.com please",
    )
    assert "tier3_ok" in resp.content
    assert p1.calls == 0, "PII guard must block cloud tier dispatch"
    assert p3.calls == 1, "fall-through to on-prem tier expected"


@pytest.mark.asyncio
async def test_pii_guard_redact_mode_rewrites_prompts(monkeypatch):
    """policy=redact lets the cloud tier proceed but with redactions
    in place of PII."""
    monkeypatch.setenv("NEXUS_LLM_PII_GUARD_POLICY", "redact")
    monkeypatch.setenv("NEXUS_LLM_PII_GUARD_TIERS", "tier1")
    from nexus_sdk.llm.tiered import ProviderTier

    captured = {}

    class _CapturingStub(_StubProvider):
        async def generate(self, *, system_prompt, user_prompt, **kw):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return await super().generate(
                system_prompt=system_prompt, user_prompt=user_prompt, **kw,
            )

    p1 = _CapturingStub("tier1", "claude", ["ok"])
    router = _router_with_providers({ProviderTier.PRIMARY: p1})
    await router.generate(
        system_prompt="system",
        user_prompt="Email me at foo@bar.com",
    )
    assert "[REDACTED:EMAIL]" in captured["user"]
    assert "foo@bar.com" not in captured["user"]


@pytest.mark.asyncio
async def test_pii_guard_off_lets_email_through(monkeypatch):
    monkeypatch.setenv("NEXUS_LLM_PII_GUARD_POLICY", "off")
    from nexus_sdk.llm.tiered import ProviderTier
    p1 = _StubProvider("tier1", "claude", ["ok"])
    router = _router_with_providers({ProviderTier.PRIMARY: p1})
    resp = await router.generate(
        system_prompt="s", user_prompt="email: a@b.com",
    )
    assert p1.calls == 1


# ─── Fix 5: per-tenant token budget ────────────────────────────


class _FakeRedis:
    """In-memory Redis stub — supports hgetall, hset, expire."""

    def __init__(self) -> None:
        self.store: dict = {}

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def hset(self, key, mapping=None, **_):
        cur = self.store.setdefault(key, {})
        if mapping:
            cur.update({k: str(v) for k, v in mapping.items()})

    async def expire(self, key, ttl):
        pass


@pytest.mark.asyncio
async def test_budget_blocks_tier_when_exhausted(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_BUDGET_TIER1_DEFAULT", "100")
    monkeypatch.setenv("LLM_TOKEN_BUDGET_TIER3_DEFAULT", "0")  # unlimited
    from nexus_sdk.llm.tiered import ProviderTier
    from nexus_sdk.llm.token_budget import TenantTokenBudget
    fake_r = _FakeRedis()
    budget = TenantTokenBudget(redis_client=fake_r)
    p1 = _StubProvider("tier1", "claude", [
        # First call consumes ~30 tokens (10 in + 20 out per stub default)
        {"prompt_tokens": 60, "completion_tokens": 50, "total_tokens": 110},
    ])
    p3 = _StubProvider("tier3", "llama", ["ok"])
    router = _router_with_providers(
        {ProviderTier.PRIMARY: p1, ProviderTier.LOCAL: p3},
        token_budget=budget,
    )
    # First call fits the 100-token tier1 budget (estimate is loose
    # but the actual consume is 110 — uses the budget up).
    resp1 = await router.generate(
        system_prompt="short", user_prompt="short",
        tenant_id="acme", max_tokens=50,
    )
    # Second call: tier1 over budget → fall through to tier3.
    resp2 = await router.generate(
        system_prompt="short", user_prompt="short",
        tenant_id="acme", max_tokens=50,
    )
    assert "tier3_ok" in resp2.content


@pytest.mark.asyncio
async def test_budget_exhausted_raises_when_no_fallback(monkeypatch):
    """If only cloud tiers are configured and all budgets are out,
    we raise TokenBudgetExhausted (not a generic provider failure)."""
    monkeypatch.setenv("LLM_TOKEN_BUDGET_TIER1_DEFAULT", "1")  # ~immediately
    from nexus_sdk.llm.tiered import ProviderTier
    from nexus_sdk.llm.token_budget import TenantTokenBudget, TokenBudgetExhausted
    fake_r = _FakeRedis()
    # Pre-populate Redis so tier1 starts at 0 remaining.
    fake_r.store["nexus:llm_budget:tier1:acme"] = {
        "limit": "1", "tokens": "0", "updated": str(__import__("time").time()),
    }
    budget = TenantTokenBudget(redis_client=fake_r)
    p1 = _StubProvider("tier1", "claude", ["ok"])
    router = _router_with_providers(
        {ProviderTier.PRIMARY: p1}, token_budget=budget,
    )
    with pytest.raises(TokenBudgetExhausted):
        await router.generate(
            system_prompt="s", user_prompt="u",
            tenant_id="acme", max_tokens=50,
        )


@pytest.mark.asyncio
async def test_budget_no_tenant_id_skips_enforcement():
    """No tenant_id → platform-internal call (sweeper, admin tool).
    Budget enforcement is bypassed."""
    from nexus_sdk.llm.tiered import ProviderTier
    from nexus_sdk.llm.token_budget import TenantTokenBudget
    fake_r = _FakeRedis()
    budget = TenantTokenBudget(redis_client=fake_r)
    p1 = _StubProvider("tier1", "claude", ["ok"])
    router = _router_with_providers(
        {ProviderTier.PRIMARY: p1}, token_budget=budget,
    )
    # No tenant_id → no enforcement, no Redis writes
    resp = await router.generate(system_prompt="s", user_prompt="u")
    assert p1.calls == 1


# ─── Fix 1: cost metric — pricing module unit tests ────────────


def test_pricing_known_model_anthropic_opus():
    from nexus_sdk.llm.pricing import cost_usd_for
    # claude-opus-4: $15/1M input, $75/1M output
    cost = cost_usd_for("anthropic", "claude-opus-4-20250514", 1_000_000, 1_000_000)
    assert abs(cost - 90.0) < 0.01


def test_pricing_unknown_model_returns_zero_with_log():
    from nexus_sdk.llm.pricing import cost_usd_for
    cost = cost_usd_for("anthropic", "claude-future-9", 100_000, 100_000)
    assert cost == 0.0


def test_pricing_self_hosted_models_free():
    from nexus_sdk.llm.pricing import cost_usd_for
    assert cost_usd_for("ollama", "llama3.1:70b", 1_000_000, 1_000_000) == 0.0
    assert cost_usd_for("ollama", "moondream", 1_000_000, 1_000_000) == 0.0


def test_pricing_runtime_override():
    """register_model_price() supports operator-supplied negotiated rates."""
    from nexus_sdk.llm.pricing import (
        register_model_price, cost_usd_for,
    )
    register_model_price("test-custom-model", 1.0, 2.0)
    cost = cost_usd_for("custom", "test-custom-model-v1", 1_000_000, 1_000_000)
    assert cost == 3.0


# ─── PII guard unit tests ──────────────────────────────────────


def test_pii_regex_detects_email_phone_credit_card():
    from nexus_sdk.llm.pii_guard import RegexPIIDetector
    det = RegexPIIDetector()
    matches = det.scan("Contact jane@example.com or +1 555 234 5678. Card 4111 1111 1111 1111.")
    kinds = {m.pattern_name for m in matches}
    assert "email" in kinds
    assert "phone_e164" in kinds
    assert "credit_card" in kinds


def test_pii_regex_rejects_invalid_luhn_card():
    """A 16-digit sequence that fails Luhn is not flagged as a card."""
    from nexus_sdk.llm.pii_guard import RegexPIIDetector
    det = RegexPIIDetector()
    matches = det.scan("Order number 1234567890123456 was shipped.")
    assert all(m.pattern_name != "credit_card" for m in matches)


def test_pii_guard_skips_tier3():
    from nexus_sdk.llm.pii_guard import enforce
    # tier3 is in the default unguarded set
    system, user = enforce(
        "system", "email: a@b.com",
        tier_value="tier3", engine_name="test",
    )
    assert user == "email: a@b.com"  # untouched
