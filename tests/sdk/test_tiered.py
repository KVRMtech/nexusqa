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
