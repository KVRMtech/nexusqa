"""Durable accounting for a single crawl-time LLM call.

Prometheus is deliberately aggregate-only; this helper writes the matching raw
token count to the tenant-scoped ``cost_ledger``.  It is best-effort with
respect to the oracle decision: a transient ledger outage must not turn a
usable answer into an unavailable crawl step, but it is always loud in logs.
"""
from __future__ import annotations

import logging
from typing import Any

from ..controlplane.cost import meter

logger = logging.getLogger(__name__)


async def record_llm_usage(*, tenant_id: str, app_id: str = "",
                           crawl_id: str = "", task: str,
                           usage: dict[str, Any]) -> None:
    """Append the provider-reported prompt+completion token count once.

    Cache-token fields are intentionally excluded: providers report them as a
    subdivision of prompt use, so adding them would double-count spend.
    """
    try:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return
        total = max(0, int(prompt or 0)) + max(0, int(completion or 0))
        await meter.record_cost(
            tenant_id=tenant_id,
            app_id=app_id,
            units={meter.UNIT_LLM_TOKENS: total},
            source_ref=f"crawl:{crawl_id or 'unknown'}:llm:{task}",
        )
    except Exception:
        logger.exception("qec.llm_cost.record_failed task=%s crawl_id=%s",
                         task, crawl_id)

