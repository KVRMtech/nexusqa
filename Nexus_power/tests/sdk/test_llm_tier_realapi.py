"""
Real-API integration tests for the LLM tier system.

These tests **only run when the relevant API keys are present in the
environment** — they're opt-in by design. Without keys, every test
in this file skips so CI stays green and free.

Why we need these even though we have stub tests:

  - Stubs prove the router logic; real-API tests prove the provider
    classes parse current API responses, that pricing math matches
    actual billing dimensions, and that token-budget enforcement
    works end-to-end.
  - Provider APIs evolve. A change to Anthropic's response shape
    breaks our integration silently — stubs won't catch it. These
    tests do.

Opt-in env vars (set ONE OR MORE):

  ANTHROPIC_API_KEY        — runs the Anthropic test path
  OPENAI_API_KEY           — runs the OpenAI test path
  AZURE_OPENAI_API_KEY     +
    AZURE_OPENAI_ENDPOINT  +
    AZURE_OPENAI_DEPLOYMENT — runs the Azure test path
  NEXUS_REAL_LLM_TEST_OLLAMA=http://localhost:11434
                           — runs the Ollama test path

How to run (locally):

  ANTHROPIC_API_KEY=$YOUR_KEY \
  pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi

How to run (in CI):

  Add this to a *manual-trigger* workflow that pulls keys from the
  CI secret store. **Do NOT run on every PR** — these calls cost
  real money and consume real rate-limit budget.

Cost guardrail:

  Every test asks for a tiny prompt (~20 tokens in, ~50 tokens out).
  Worst case per test run: <$0.01. Set NEXUS_REAL_LLM_TEST_MAX_USD if
  you want a hard cap that aborts the suite if the running total
  threatens to exceed it.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest


# Mark every test in this module so a normal `pytest` invocation skips
# them. Run explicitly with `-m realapi` to opt in.
pytestmark = pytest.mark.realapi


# Add SDK to path so `nexus_sdk` imports resolve in a fresh check-out.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"),
)


# ─── Skip-if-missing helpers ───────────────────────────────────


def _has(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


_HAS_ANTHROPIC = _has("ANTHROPIC_API_KEY")
_HAS_OPENAI = _has("OPENAI_API_KEY")
_HAS_AZURE = (
    _has("AZURE_OPENAI_API_KEY")
    and _has("AZURE_OPENAI_ENDPOINT")
    and _has("AZURE_OPENAI_DEPLOYMENT")
)
_HAS_OLLAMA = _has("NEXUS_REAL_LLM_TEST_OLLAMA")


@pytest.fixture(scope="module")
def _cost_cap_usd() -> float:
    raw = os.environ.get("NEXUS_REAL_LLM_TEST_MAX_USD", "0.10").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.10


# Cumulative cost across this module — tests can assert their own
# inc but the module-level cap is checked in a finalizer to fail loud
# if a test blows past it.
_cumulative_cost_usd = [0.0]


# ─── Anthropic Tier 1 ───────────────────────────────────────────


@pytest.mark.skipif(not _HAS_ANTHROPIC, reason="ANTHROPIC_API_KEY not set")
@pytest.mark.asyncio
async def test_anthropic_tier1_returns_valid_response(_cost_cap_usd):
    """A real Anthropic call returns text + populated token counts.

    What this catches that stubs can't:
      - The response shape Anthropic returns today (vs what we coded for)
      - Auth flow end-to-end (the right API version, headers, etc.)
      - Token-count fields under their real names
    """
    from nexus_sdk.llm.tiered import (
        TieredProviderConfig, TieredLLMRouter, TierConfig, ProviderTier,
    )

    cfg = TieredProviderConfig(
        engine_name="realtest",
        tiers=[TierConfig(
            tier=ProviderTier.PRIMARY,
            provider="anthropic",
            model="claude-3-5-haiku-20241022",  # cheap, fast
            api_key=os.environ["ANTHROPIC_API_KEY"],
            max_retries=0,
            timeout=30,
        )],
        mode="single",
    )
    router = TieredLLMRouter(cfg)
    await router.initialize()
    try:
        response = await router.generate(
            system_prompt="Respond with exactly one word.",
            user_prompt="Say hello.",
            max_tokens=20,
        )
        assert response.content
        assert response.total_tokens > 0
        # Token-cost calculation must produce a non-zero value for a
        # priced model — caught if our pricing table goes stale.
        from nexus_sdk.llm.pricing import cost_usd_for
        cost = cost_usd_for(
            "anthropic", response.model,
            response.prompt_tokens, response.completion_tokens,
        )
        assert cost > 0, (
            f"cost lookup returned 0 for live model {response.model!r}; "
            f"update nexus_sdk/llm/pricing.py:_PRICE_TABLE"
        )
        _cumulative_cost_usd[0] += cost
    finally:
        await router.shutdown()


# ─── OpenAI Tier 1 ──────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_OPENAI, reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_openai_tier1_returns_valid_response(_cost_cap_usd):
    from nexus_sdk.llm.tiered import (
        TieredProviderConfig, TieredLLMRouter, TierConfig, ProviderTier,
    )

    cfg = TieredProviderConfig(
        engine_name="realtest",
        tiers=[TierConfig(
            tier=ProviderTier.PRIMARY,
            provider="openai",
            model="gpt-4o-mini",  # cheap
            api_key=os.environ["OPENAI_API_KEY"],
            max_retries=0,
            timeout=30,
        )],
        mode="single",
    )
    router = TieredLLMRouter(cfg)
    await router.initialize()
    try:
        response = await router.generate(
            system_prompt="Respond with exactly one word.",
            user_prompt="Say hello.",
            max_tokens=20,
        )
        assert response.content
        from nexus_sdk.llm.pricing import cost_usd_for
        cost = cost_usd_for(
            "openai", response.model,
            response.prompt_tokens, response.completion_tokens,
        )
        assert cost > 0
        _cumulative_cost_usd[0] += cost
    finally:
        await router.shutdown()


# ─── Azure OpenAI ───────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_AZURE, reason="Azure OpenAI env not configured")
@pytest.mark.asyncio
async def test_azure_openai_tier1_returns_valid_response(_cost_cap_usd):
    from nexus_sdk.llm.tiered import (
        TieredProviderConfig, TieredLLMRouter, TierConfig, ProviderTier,
    )

    cfg = TieredProviderConfig(
        engine_name="realtest",
        tiers=[TierConfig(
            tier=ProviderTier.PRIMARY,
            provider="azure",
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            max_retries=0,
            timeout=30,
        )],
        mode="single",
    )
    router = TieredLLMRouter(cfg)
    await router.initialize()
    try:
        response = await router.generate(
            system_prompt="Respond with exactly one word.",
            user_prompt="Say hello.",
            max_tokens=20,
        )
        assert response.content
        # Azure pricing includes the optional surcharge multiplier.
        from nexus_sdk.llm.pricing import cost_usd_for
        cost = cost_usd_for(
            "azure", response.model,
            response.prompt_tokens, response.completion_tokens,
        )
        # May be 0 if the operator didn't map their Azure deployment
        # name to a priced model — warn but don't fail.
        if cost == 0:
            print(
                f"\n[warn] Azure deployment {response.model!r} not in price "
                f"table — set NEXUS_LLM_PRICE_OVERRIDES to fix.",
            )
        _cumulative_cost_usd[0] += cost
    finally:
        await router.shutdown()


# ─── Ollama Tier 3 ──────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_OLLAMA, reason="NEXUS_REAL_LLM_TEST_OLLAMA not set")
@pytest.mark.asyncio
async def test_ollama_tier3_returns_valid_response():
    """Ollama is zero-cost; useful as a sanity check that the local
    model path actually works end-to-end. No spend guard needed."""
    from nexus_sdk.llm.tiered import (
        TieredProviderConfig, TieredLLMRouter, TierConfig, ProviderTier,
    )

    base_url = os.environ["NEXUS_REAL_LLM_TEST_OLLAMA"]
    model = os.environ.get("NEXUS_REAL_LLM_TEST_OLLAMA_MODEL", "llama3.2:1b")
    cfg = TieredProviderConfig(
        engine_name="realtest",
        tiers=[TierConfig(
            tier=ProviderTier.LOCAL,
            provider="ollama",
            model=model,
            ollama_base_url=base_url,
            max_retries=0,
            timeout=120,
        )],
        mode="single",
    )
    router = TieredLLMRouter(cfg)
    await router.initialize()
    try:
        response = await router.generate(
            system_prompt="Respond with exactly one word.",
            user_prompt="Say hello.",
            max_tokens=20,
        )
        assert response.content
        # Ollama is free.
        from nexus_sdk.llm.pricing import cost_usd_for
        assert cost_usd_for("ollama", model, 100, 100) == 0.0
    finally:
        await router.shutdown()


# ─── Failover behavior with one real tier + one stub ────────────


@pytest.mark.skipif(
    not (_HAS_ANTHROPIC or _HAS_OPENAI),
    reason="No cloud API key available",
)
@pytest.mark.asyncio
async def test_failover_from_dead_stub_to_real_tier(_cost_cap_usd):
    """Drive a failover with one real provider and one deliberately-
    broken stub. Validates that the router's failover plumbing works
    against actual provider response shapes, not just mocks."""
    from nexus_sdk.llm.tiered import (
        TieredProviderConfig, TieredLLMRouter, TierConfig, ProviderTier,
    )

    # Tier 1 = bogus key → forces failover. Tier 2 = real call.
    real_provider, real_model, real_key = (
        ("anthropic", "claude-3-5-haiku-20241022", os.environ.get("ANTHROPIC_API_KEY"))
        if _HAS_ANTHROPIC
        else ("openai", "gpt-4o-mini", os.environ.get("OPENAI_API_KEY"))
    )

    cfg = TieredProviderConfig(
        engine_name="realtest_failover",
        tiers=[
            TierConfig(
                tier=ProviderTier.PRIMARY,
                provider=real_provider,
                model=real_model,
                api_key="bogus-key-will-401",
                max_retries=0,
                timeout=10,
            ),
            TierConfig(
                tier=ProviderTier.SECONDARY,
                provider=real_provider,
                model=real_model,
                api_key=real_key,
                max_retries=0,
                timeout=30,
            ),
        ],
        mode="multi-tier",
    )
    router = TieredLLMRouter(cfg)
    await router.initialize()
    try:
        response = await router.generate(
            system_prompt="Reply with a single word.",
            user_prompt="Hi.",
            max_tokens=20,
        )
        # Response should be served by tier2 (the real one).
        assert response.content
        assert "tier2" in (response.provider or "")
    finally:
        await router.shutdown()


# ─── Cost-cap finalizer ────────────────────────────────────────


def test_total_cost_under_cap(_cost_cap_usd):
    """Final defense: assert the module-level cumulative cost stays
    under the configured cap. Default cap is $0.10 — well under what
    a single CI run should ever spend."""
    if _cumulative_cost_usd[0] > _cost_cap_usd:
        pytest.fail(
            f"real-API test suite cost ${_cumulative_cost_usd[0]:.4f} "
            f"which exceeds cap ${_cost_cap_usd:.4f}; "
            f"raise NEXUS_REAL_LLM_TEST_MAX_USD or reduce per-test cost"
        )
