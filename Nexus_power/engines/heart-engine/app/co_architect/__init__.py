"""Co-Architect — AI assistant constrained to the visual evidence graph (P4).

Public surface:
    build_graph_context(graph)           → str
    build_system_prompt(graph, propose)  → str
    encode_conversation(messages)        → str
    parse_chat_response(text, propose)   → ChatTurnResponse
    SYSTEM_PROMPT_BASE                   → str (constant rules)
"""
from .context import build_graph_context
from .prompts import build_system_prompt, SYSTEM_PROMPT_BASE
from .parsing import (
    ChatTurnResponse,
    ProposedScenarioStep,
    ProposedScenario,
    encode_conversation,
    parse_chat_response,
)

__all__ = [
    "build_graph_context",
    "build_system_prompt",
    "encode_conversation",
    "parse_chat_response",
    "ChatTurnResponse",
    "ProposedScenarioStep",
    "ProposedScenario",
    "SYSTEM_PROMPT_BASE",
]
