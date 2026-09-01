"""Per-call LLM usage is written to the durable tenant cost ledger."""
from unittest.mock import AsyncMock

import pytest

from app.controlplane.cost import meter
from app.services import llm_cost


@pytest.mark.asyncio
async def test_reported_prompt_and_completion_are_one_ledger_entry(monkeypatch):
    record = AsyncMock()
    monkeypatch.setattr(meter, "record_cost", record)

    await llm_cost.record_llm_usage(
        tenant_id="tenant-a", app_id="app-a", crawl_id="crawl-a",
        task="pick_advance",
        usage={"prompt_tokens": 17, "completion_tokens": 5,
               "cache_read_tokens": 17},
    )

    record.assert_awaited_once_with(
        tenant_id="tenant-a", app_id="app-a",
        units={meter.UNIT_LLM_TOKENS: 22},
        source_ref="crawl:crawl-a:llm:pick_advance",
    )


@pytest.mark.asyncio
async def test_unreported_usage_does_not_invent_a_free_or_zero_row(monkeypatch):
    record = AsyncMock()
    monkeypatch.setattr(meter, "record_cost", record)

    await llm_cost.record_llm_usage(
        tenant_id="tenant-a", task="field_value", usage={})

    record.assert_not_awaited()
