"""
Heart Engine — Autonomous Flow Exploration Module.

Given a single demonstrated flow, uses LLM reasoning to discover
ALL possible paths through the system (alternate paths, error
handling, edge cases in data, concurrent access, etc.).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Prompt Templates ──────────────────────────────────────────

EXPLORE_FLOWS_SYSTEM = """You are an expert at discovering all possible paths through a software system.
An SME showed you ONE flow. Your job is to think about EVERY other possible flow.

For each UI element, ask:
- What if the user clicks something DIFFERENT?
- What if validation fails?
- What if the data is in a different state?
- What if permissions are different?
- What about error handling paths?
- What about concurrent access?
- What about edge cases in the data?

Think systematically about:
1. Every decision point → what are ALL the branches?
2. Every input field → what are ALL valid and invalid values?
3. Every state transition → what are ALL possible states?
4. Every integration → what if the external system is down/slow/returns errors?

Return valid JSON only."""

EXPLORE_FLOWS_USER = """Demonstrated Flow:
---
{demonstrated_flow}
---

Known UI Screens:
{ui_screens}

Known Business Rules:
{known_rules}

Explore ALL possible flows. Return as JSON with keys: explored_flows, new_paths_found, questions"""


# ─── Flow Explorer ─────────────────────────────────────────────

class FlowExplorer:
    """
    Autonomously explores all possible flows from a demonstrated path.

    Parameters
    ----------
    llm : object
        HeartLLM instance (or any object with async ``generate(system, user)``).
    prompt_overrides : dict | None
        Optional prompt template overrides from plugins.
    """

    def __init__(
        self,
        llm,
        prompt_overrides: Optional[dict[str, str]] = None,
    ):
        self.llm = llm
        self._overrides = prompt_overrides or {}

    def _get_prompt(self, prompt_id: str, fallback: str) -> str:
        return self._overrides.get(prompt_id, fallback)

    async def explore(
        self,
        demonstrated_flow: dict,
        ui_screens: list[dict] | None = None,
        known_rules: list[str] | None = None,
    ) -> dict:
        """
        Explore all possible flows from a single demonstrated flow.

        Returns
        -------
        dict
            Keys: ``explored_flows``, ``new_paths_found``, ``questions``.
        """
        system_prompt = self._get_prompt(
            "explore_flows", EXPLORE_FLOWS_SYSTEM
        )
        user_prompt_template = self._get_prompt(
            "explore_flows_user", EXPLORE_FLOWS_USER
        )

        response = await self.llm.generate(
            system_prompt,
            user_prompt_template.format(
                demonstrated_flow=json.dumps(demonstrated_flow, indent=2),
                ui_screens=(
                    json.dumps(ui_screens, indent=2)
                    if ui_screens
                    else "None provided"
                ),
                known_rules=(
                    "\n".join(known_rules)
                    if known_rules
                    else "None provided"
                ),
            ),
        )

        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            logger.warning("heart.generators: failed to parse explore-flows LLM response")
            parsed = {"explored_flows": [], "new_paths_found": 0, "questions": []}

        return {
            "explored_flows": parsed.get("explored_flows", []),
            "new_paths_found": parsed.get("new_paths_found", 0),
            "questions": parsed.get("questions", []),
        }
