"""THE OPERATOR'S DATA POSTURE — three portal modes, one resolved decision.

The portal offers three choices, and this is the one place that turns them into
what the explorer is actually told:

    agent     LEGACY: the explorer's own dial, set directly before the portal
              offered modes. Means "the generator answers choices", model off.
    llm       the data agent answers everything it safely can; a field is asked
              of the client only when no rung could honestly produce a value
    user      the client's own data only; anything unanswered becomes the ask,
              which is the behaviour every crawl had before rung 8 existed
    user_llm  the client's data wins wherever it exists, and the model fills the
              rest — so only true secrets are ever asked

WHY A SERVICE AND NOT AN `if` IN THE ROUTER. The explorer takes TWO dials, not
one: ``data_mode`` (whether the deterministic generator answers semantic
choices) and ``data_llm`` (whether rung 8 is consulted at all). Three portal
modes across two dials is exactly the shape that drifts when it lives inline —
one caller sets a mode and forgets the flag, and a crawl silently runs with the
model off while its report says "LLM". Resolving both here, together, is what
makes the report's claim and the crawl's behaviour the same fact.

THE ATTESTATION STILL DECIDES WHAT IS ALLOWED. This resolves what the operator
ASKED for; ``prod_guard`` decides what the environment PERMITS, and a posture
the operator never attested is never widened here. An operator's explicit
choice is honoured, and the absence of one falls back to the attested default —
neither of which this module may quietly override.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The portal's three choices.
MODE_LLM = "llm"
MODE_USER = "user"
MODE_USER_LLM = "user_llm"
PORTAL_MODES = (MODE_LLM, MODE_USER, MODE_USER_LLM)

#: What the explorer understands for its own generator dial.
EXPLORER_AGENT = "agent"
EXPLORER_USER = "user"


@dataclass(frozen=True)
class DataPosture:
    """What the explorer is told, and how to say it to a human."""

    #: The portal mode this came from ("" when the operator declared none).
    declared: str
    #: The explorer's generator dial.
    data_mode: str
    #: Whether rung 8 (the LLM data agent) is consulted.
    data_llm: bool

    @property
    def summary(self) -> str:
        if self.data_llm and self.data_mode == EXPLORER_AGENT:
            return ("the data agent answers what it safely can; only fields no "
                    "rung could honestly produce are asked of you")
        if self.data_llm:
            return ("your own data wins wherever you supplied it, and the model "
                    "fills the rest; only true secrets are asked")
        return ("your own data only; anything unanswered is reported back as a "
                "request rather than invented")


def resolve(declared_mode: str, *, attested_default_agent: bool) -> DataPosture:
    """Turn the operator's portal choice into the explorer's two dials.

    ``attested_default_agent`` is what the ENVIRONMENT permits by default — an
    attested test environment answers, anything else does not. It applies only
    when the operator declared nothing; an explicit choice is never overridden
    in either direction.
    """
    declared = (declared_mode or "").strip().lower()

    if declared == MODE_LLM:
        return DataPosture(declared, EXPLORER_AGENT, True)
    if declared == MODE_USER_LLM:
        # The generator still declines semantic choices — the client's data is
        # meant to win — and rung 8 fills what is left. That combination is the
        # whole point of the middle mode.
        return DataPosture(declared, EXPLORER_USER, True)
    if declared == MODE_USER:
        return DataPosture(declared, EXPLORER_USER, False)
    if declared == EXPLORER_AGENT:
        # THE LEGACY DIAL, PASSED THROUGH UNCHANGED. Before the portal offered
        # three modes, an operator set the explorer's own value directly and
        # schedules still carry it. "agent" has always meant "the deterministic
        # generator answers semantic choices" and nothing about a model, so it
        # maps to exactly that — dropping it here would silently downgrade a
        # crawl an operator had explicitly configured, which
        # test_an_operator_who_chose_agent_keeps_agent_without_an_attestation
        # exists to prevent.
        return DataPosture(declared, EXPLORER_AGENT, False)

    # Nothing declared: the attestation decides, and the model stays OFF. A
    # crawl must never start consulting a third-party model because nobody
    # chose anything — that is a decision, and an unmade decision is not one.
    return DataPosture(
        "", EXPLORER_AGENT if attested_default_agent else EXPLORER_USER, False)
