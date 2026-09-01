# LLM Tier — Real-API Validation Procedure

When and how to run [tests/sdk/test_llm_tier_realapi.py](Nexus_power/tests/sdk/test_llm_tier_realapi.py) against actual providers.

---

## When to run

| Trigger | Tests | Owner |
|---|---|---|
| Before first deploy to a new cloud region | All | Platform |
| When you rotate any tier API key | The provider whose key changed | Operator |
| After upgrading a provider model in [plans.py](Nexus_power/sdk/nexus-sdk/nexus_sdk/workflows/plans.py) | The affected provider | Platform |
| Monthly canary (model APIs evolve) | All | Platform |
| Provider returns 4xx/5xx in production that stubs didn't predict | The affected provider | On-call |

**Do NOT** add real-API tests to the normal CI pull-request gate. They cost real money and consume real rate-limit budget; they're a deploy-gate / canary tool, not a development tool.

---

## How to run

### Locally

```bash
# Tier 1 — Anthropic
ANTHROPIC_API_KEY=sk-ant-xxxx \
pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi

# Tier 1 — OpenAI
OPENAI_API_KEY=sk-xxxx \
pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi

# Azure
AZURE_OPENAI_API_KEY=... \
AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com \
AZURE_OPENAI_DEPLOYMENT=gpt-4o \
pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi

# Tier 3 — Ollama (free)
NEXUS_REAL_LLM_TEST_OLLAMA=http://localhost:11434 \
NEXUS_REAL_LLM_TEST_OLLAMA_MODEL=llama3.2:1b \
pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi

# All four together
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \
AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_DEPLOYMENT=... \
NEXUS_REAL_LLM_TEST_OLLAMA=http://localhost:11434 \
pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi
```

Cost guardrail — each test asks for a ~20-token prompt and ~50-token completion. Worst case for a full run with all four providers: < $0.01. The suite enforces an aggregate cap (default $0.10) via `NEXUS_REAL_LLM_TEST_MAX_USD`.

### In CI (manual-trigger only)

Add to `.github/workflows/llm-real-api.yml`:

```yaml
name: llm-real-api-validation
on:
  workflow_dispatch:           # MANUAL only — do NOT add `pull_request`
  schedule:
    - cron: "0 8 1 * *"        # monthly canary, 1st of each month at 08:00 UTC

jobs:
  validate:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install pytest pytest-asyncio httpx
      - run: pytest tests/sdk/test_llm_tier_realapi.py -v -m realapi
        env:
          PYTHONPATH: ${{ github.workspace }}/sdk/nexus-sdk
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          NEXUS_REAL_LLM_TEST_MAX_USD: "0.50"
```

---

## What the tests prove

| Test | What stubs CAN'T catch |
|---|---|
| `test_anthropic_tier1_returns_valid_response` | Anthropic's current response shape, today's `prompt_tokens` / `completion_tokens` field names, the model returns text we can parse |
| `test_openai_tier1_returns_valid_response` | OpenAI's `choices[0].message.content` shape; the `usage` block; that gpt-4o-mini is still GA |
| `test_azure_openai_tier1_returns_valid_response` | Azure's deployment-name routing (different from OpenAI direct); the `api-version` header |
| `test_ollama_tier3_returns_valid_response` | The local Ollama is reachable, model is pulled, `/api/chat` accepts our payload |
| `test_failover_from_dead_stub_to_real_tier` | The failover plumbing works against an actual 401 response — not just a Python exception we raised |
| `test_total_cost_under_cap` | The pricing table produces non-zero numbers for live model IDs (catches stale model defaults) |

---

## What to do when a test fails

| Failure | Likely cause | Fix |
|---|---|---|
| `assert cost > 0` in any provider test | Model returned today is missing from [`pricing.py:_PRICE_TABLE`](Nexus_power/sdk/nexus-sdk/nexus_sdk/llm/pricing.py) | Add the model prefix + list price to the table; submit a PR |
| `assert "tier2" in response.provider` in failover test | Router failover broke or tier1 unexpectedly succeeded | Inspect logs — was the bogus key actually rejected? |
| Anthropic / OpenAI auth error | Key was rotated / scoped | Update the secret in your CI / local env |
| Azure `404 Resource not found` | Deployment name changed | Update `AZURE_OPENAI_DEPLOYMENT` |
| Ollama `connection refused` | Local Ollama not running | `docker compose up -d ollama` |
| Test suite exceeds `NEXUS_REAL_LLM_TEST_MAX_USD` cap | Cost guardrail | Reduce per-test cost; raise the cap deliberately if needed |

---

## Privacy

These tests send the literal strings `"Say hello."` and `"Hi."` as prompts. No customer data is ever sent. The validation suite must never be modified to use real customer transcripts.

If you need to validate that the PII guard is intercepting a specific pattern, use the **stub-based** tests in [test_tiered.py](Nexus_power/tests/sdk/test_tiered.py), not the real-API harness.

---

## Sign-off

The real-API harness is a **deploy gate**, not a development gate. The flow:

1. Engineering merges PR (stub tests required to pass).
2. Operator triggers the manual `llm-real-api-validation` CI workflow.
3. If green → deploy.
4. If red → fix forward (provider API change, pricing table stale, etc.) before deploy.

The same gate runs monthly to catch provider-side drift.
