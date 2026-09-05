"""Rung 8's server side — ONE test value for ONE field, through the guarded wire.

WHY THIS SERVICE EXISTS RATHER THAN AN HTTP CALL IN THE CRAWLER. The explorer's
first LLM data agent built its own ``httpx.Client`` and called a provider
directly, which is precisely the thing T-SEC-12 exists to make impossible:

    "the guard lives at the WIRE — inside the only two functions in this service
     that talk to a model — and there is no second route. A caller that wants to
     skip it would have to write its own HTTP client."

That is what had been written. Field labels, section headings and option lists
were leaving a crawl for a third-party model with no PII scan, and the test that
forbids it lives in THIS service, so an explorer-side client sailed past it.
Routing here restores the single chokepoint: ``platform_api.complete_llm``
scans egress immediately before the request is built, mints the service JWT,
and reports token usage — none of which a crawler-side client did.

THE CONTRACT, deliberately shaped like :mod:`advance_agent`:

    {"value": str|null, "status": "answered"|"none"|"unavailable", "usage": {}}

  * ``answered``    — a value the field can take;
  * ``none``        — the agent honestly has nothing (a credential, an
                      off-list reply, an empty completion);
  * ``unavailable`` — the decision could not be made (model failure, egress
                      refusal). The crawler treats both non-answers identically:
                      the field stays residue and the crawl continues.

WHAT THE SERVER DECIDES, NOT THE CALLER. Credentials are refused here as well as
in the crawler, and an enumerable field's reply is clamped to the control's own
options here as well. Both checks exist on both sides on purpose: the crawler's
copy saves a round trip, and this copy is the one that actually binds, because a
future caller reaching this endpoint inherits it without having to remember.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..clients import platform_api

logger = logging.getLogger(__name__)

STATUS_ANSWERED = "answered"
STATUS_NONE = "none"
STATUS_UNAVAILABLE = "unavailable"

#: Semantic types that are credentials, not data. A fabricated one-time code is
#: a burned auth attempt, and a fabricated password is a failed login — neither
#: is an answer, so neither is worth a model call.
_CREDENTIAL_TYPES = frozenset({"password", "otp", "one_time_code"})

_SYSTEM = (
    "You supply ONE test value for ONE form field in a disposable test "
    "environment. Reply with the value only — no quotes, no explanation, no "
    "labels. Rules: "
    "(1) If a list of allowed options is given, reply with EXACTLY one of them, "
    "verbatim. "
    "(2) Respect every stated constraint (pattern, min, max, maxlength, date "
    "windows). "
    # NO LITERAL CARD NUMBER HERE, and it is not a style choice.
    #
    # MEASURED 2026-09-04. This instruction used to carry the classic Visa test
    # PAN as an example. That number passes Luhn BY DESIGN, so the egress guard
    # scanned this very system prompt, matched credit_card, and refused the
    # request — every field, every crawl, every application:
    #
    #     qec.egress.pii_detected site=llm:field_value patterns=['credit_card']
    #     qec.platform_api.egress_blocked
    #     Middle Name -> status=unavailable
    #
    # The safety instruction tripped the safety scanner, and the only visible
    # symptom was a crawl quietly falling back to its deterministic filler.
    # The guard was RIGHT: a Luhn-valid PAN in an outbound payload is exactly
    # what it exists to stop. So the prompt loses the literal and keeps the
    # instruction; the model does not need the digits spelled out.
    "(3) For government or financial identifiers use designated TEST ranges "
    "only: SSNs in the 900-series, and card numbers from the card networks' "
    "own published test ranges - never a real or realistic PAN. "
    "(4) Prefer a plausible, internally consistent value over a clever one; "
    "short over long. "
    "(5) If a rejection message from the application is provided, your value "
    "MUST satisfy the rule that message states. "
    "(6) Never reply with a refusal or a question — always produce a value."
)


@dataclass(frozen=True)
class ValueDecision:
    """What the agent decided, and what the completion cost."""

    status: str
    value: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)


def build_prompt(*, name: str, semantic_type: str, kind: str,
                 options: Sequence[str] = (), constraints: str = "",
                 section: str = "", page_title: str = "",
                 rejection: str = "") -> str:
    """The field, described to the model. PURE — no values, only the page's own
    wording and the rules the application itself declared."""
    parts = [f"Field: {name or '(unnamed)'}", f"Kind: {kind or 'text'}"]
    if semantic_type:
        parts.append(f"Semantic type: {semantic_type}")
    if section:
        parts.append(f"Section: {section}")
    if page_title:
        parts.append(f"Page: {page_title}")
    if constraints:
        parts.append(f"Constraints: {constraints}")
    if options:
        parts.append("Allowed options (reply with one, verbatim): "
                     + " | ".join(str(o) for o in list(options)[:40]))
    if rejection:
        parts.append("The application rejected the previous value with: "
                     + repr(rejection))
    return "\n".join(parts)


def clamp_to_options(value: str, options: Sequence[str]) -> Optional[str]:
    """An enumerable field's answer must be one the control offers.

    Returns the CONTROL'S OWN label (so "yes" commits as "Yes"), or ``None``
    when the reply is off-list — which becomes ``none``, never a committed
    impossible choice.
    """
    cleaned = (value or "").strip()
    if not options:
        return cleaned or None
    for option in options:
        if cleaned.lower() == str(option).strip().lower():
            return str(option)
    return None


async def pick_value(*, tenant_id: str, name: str, semantic_type: str = "",
                     kind: str = "", options: Sequence[str] = (),
                     constraints: str = "", section: str = "",
                     page_title: str = "", rejection: str = "", app_id: str = "",
                     crawl_id: str = "") -> ValueDecision:
    """One field, one value, through the guarded wire. Never raises."""
    if semantic_type in _CREDENTIAL_TYPES or kind == "password":
        return ValueDecision(status=STATUS_NONE)
    if not str(name or "").strip() and not options:
        return ValueDecision(status=STATUS_NONE)

    prompt = build_prompt(
        name=name, semantic_type=semantic_type, kind=kind, options=options,
        constraints=constraints, section=section, page_title=page_title,
        rejection=rejection)

    # THE CHOKEPOINT. complete_llm scans egress before building the request and
    # returns ok=False on a refusal — the same shape as a provider outage, so a
    # blocked payload degrades to residue instead of raising into a crawl.
    result = await platform_api.complete_llm(
        tenant_id=tenant_id, prompt=prompt, system=_SYSTEM,
        max_tokens=60, temperature=0.2, task="field_value")

    # Usage is telemetry, not contract — read it the way advance_agent does:
    # a result without it still yields a decision, degrading to "spend unknown".
    _usage = getattr(result, "usage", None)
    usage = (_usage.as_dict()
             if _usage is not None and getattr(_usage, "reported", False)
             else {})
    # The response relay is also metered by the explorer, but Prometheus is not
    # a ledger. Persist this individual call at the only server-side LLM wire.
    from .llm_cost import record_llm_usage
    await record_llm_usage(tenant_id=tenant_id, app_id=app_id,
                           crawl_id=crawl_id, task="field_value", usage=usage)

    if not getattr(result, "ok", False):
        logger.info("qec.value_agent.unavailable detail=%s",
                    str(getattr(result, "detail", ""))[:120])
        return ValueDecision(status=STATUS_UNAVAILABLE, usage=usage)

    raw = str(getattr(result, "text", "") or "").strip()
    raw = raw.strip('"').strip("'").split("\n")[0][:500]
    if not raw:
        return ValueDecision(status=STATUS_NONE, usage=usage)

    value = clamp_to_options(raw, options)
    if value is None:
        logger.info("qec.value_agent.off_list field=%r", str(name)[:40])
        return ValueDecision(status=STATUS_NONE, usage=usage)
    return ValueDecision(status=STATUS_ANSWERED, value=value, usage=usage)
