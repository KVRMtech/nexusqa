"""R3 — Crawl Medic: caged agent for genuinely novel widgets.

Fires ONLY after the deterministic R1 ladder is exhausted and R0 still
reports intent-unmet.  The medic receives the control's SHAPE (never values,
never raw HTML) plus a summary of what was tried, and proposes an action from
a CLOSED, ENUMERATED vocabulary.  The explorer executes the proposal through
existing primitives; R0 verifies the result; an unverified proposal is refused
and the control becomes named residue (R2 diagnostician).

Vocabulary (the entire set of things a medic may propose):

  * ``click``          — plain click on the control element.
  * ``press:Space``    — focus + Space keystroke.
  * ``press:Enter``    — focus + Enter keystroke.
  * ``press:ArrowDown``— focus + ArrowDown (custom select/dropdown).
  * ``open_then_pick`` — hover/click to open, then pick the first option.
  * ``display_only``   — the control is read-only / display-only — skip it.
  * ``unavailable``    — the medic cannot determine an action.

No other action may leave this module.  The vocabulary is the safety
contract: every term maps 1:1 to an existing crawler primitive, and the
R0 verifier gates the result, so an LLM hallucination is never executed
unchecked.

NEVER raises — ``unavailable`` on any failure (LLM error, unreadable reply,
out-of-vocabulary pick).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..clients import platform_api

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are a web-UI interaction specialist. A test crawler tried to "
    "operate a form control but its deterministic interaction methods all "
    "failed. You will receive the control's shape (role, tag, name, kind, "
    "attributes) and what each interaction attempt observed.\n\n"
    "Your job: propose ONE action from EXACTLY this vocabulary:\n"
    "  click          — plain click on the control\n"
    "  press:Space    — focus the control then press Space\n"
    "  press:Enter    — focus the control then press Enter\n"
    "  press:ArrowDown — focus then press ArrowDown (custom dropdown)\n"
    "  open_then_pick — hover/click to open a menu, then pick the first option\n"
    "  display_only   — the control is read-only/display-only, skip it\n"
    "  unavailable    — you cannot determine an action\n\n"
    "Reply with ONLY the action string from the vocabulary above. "
    "No explanation, no preamble, no punctuation. "
    "If you cannot determine an action, reply: unavailable"
)

STATUS_PROPOSED = "proposed"
STATUS_DISPLAY_ONLY = "display_only"
STATUS_UNAVAILABLE = "unavailable"

VOCABULARY = frozenset({
    "click",
    "press:Space",
    "press:Enter",
    "press:ArrowDown",
    "open_then_pick",
    "display_only",
    "unavailable",
})

_VOCAB_NORM = {v.lower(): v for v in VOCABULARY}


@dataclass(frozen=True)
class MedicDecision:
    """One medic consultation outcome.

    ``action`` is the vocabulary term the medic proposed (only meaningful
    when ``status == proposed``).  ``unavailable`` covers every way the
    decision could not be made.
    """
    status: str
    action: str = ""
    #: Provider-REPORTED token usage for the LLM call this decision cost, or an
    #: empty dict when it cost none.  Relayed to the explorer so the tokens are
    #: attributed to the CRAWL that spent them (M0.6 / T-OB-03).
    usage: dict = field(default_factory=dict)


def _build_prompt(
    *, control: dict, intent: str, ladder_results: list[dict],
    page_context: dict,
) -> str:
    lines = ["Control that could not be operated:"]
    lines.append(f"  Name: {control.get('name', '(unnamed)')}")
    lines.append(f"  Kind: {control.get('kind', 'unknown')}")
    lines.append(f"  Role: {control.get('role', '')}")
    lines.append(f"  Tag: {control.get('tag', '')}")
    if control.get("css_hint"):
        lines.append(f"  CSS hint: {control['css_hint']}")
    attrs = control.get("attributes", {})
    if attrs:
        for k, v in list(attrs.items())[:10]:
            lines.append(f"  Attr {k}: {v}")
    lines.append(f"  Declared intent: {intent}")
    lines.append("")
    if ladder_results:
        lines.append("Interaction attempts tried (all failed):")
        for lr in ladder_results[:8]:
            rung = lr.get("rung", "?")
            obs = lr.get("observation", "")
            lines.append(f"  {rung}: {obs}")
        lines.append("")
    ctx = page_context or {}
    if ctx.get("title"):
        lines.append(f"Page: {ctx['title']}")
    if ctx.get("url"):
        url_base = str(ctx["url"]).split("?")[0]
        lines.append(f"URL: {url_base}")
    if ctx.get("siblings"):
        lines.append("Nearby controls:")
        for sib in ctx["siblings"][:5]:
            lines.append(f"  {sib.get('name', '?')} ({sib.get('kind', '?')})")
    if ctx.get("errors"):
        lines.append(f"Visible page errors: {ctx['errors'][:200]}")
    lines.append("")
    lines.append("Which action from the vocabulary should operate this control?")
    return "\n".join(lines)


def _parse_reply(text: str) -> MedicDecision:
    cleaned = text.strip().lower()
    cleaned = re.sub(r"[^a-z0-9: _]", "", cleaned)
    cleaned = cleaned.strip()
    canon = _VOCAB_NORM.get(cleaned)
    if canon:
        if canon == "unavailable":
            return MedicDecision(status=STATUS_UNAVAILABLE)
        if canon == "display_only":
            return MedicDecision(status=STATUS_DISPLAY_ONLY, action=canon)
        return MedicDecision(status=STATUS_PROPOSED, action=canon)
    for vocab_lower, vocab_canon in _VOCAB_NORM.items():
        if vocab_lower in cleaned:
            if vocab_canon == "unavailable":
                return MedicDecision(status=STATUS_UNAVAILABLE)
            if vocab_canon == "display_only":
                return MedicDecision(status=STATUS_DISPLAY_ONLY, action=vocab_canon)
            return MedicDecision(status=STATUS_PROPOSED, action=vocab_canon)
    return MedicDecision(status=STATUS_UNAVAILABLE)


async def consult_medic(
    *, tenant_id: str, control: dict, intent: str,
    ladder_results: list[dict], page_context: dict,
) -> MedicDecision:
    """Ask the medic which action might operate a stuck control.

    Never raises.  ``proposed`` carries the vocabulary action term;
    ``display_only`` is the honest "skip this control"; ``unavailable``
    covers every failure mode.
    """
    if not control:
        return MedicDecision(status=STATUS_UNAVAILABLE)
    name = str(control.get("name") or "").strip()
    if not name:
        return MedicDecision(status=STATUS_UNAVAILABLE)
    if control.get("disabled"):
        return MedicDecision(status=STATUS_DISPLAY_ONLY, action="display_only")
    if control.get("danger"):
        return MedicDecision(status=STATUS_UNAVAILABLE)

    prompt = _build_prompt(
        control=control, intent=intent,
        ladder_results=ladder_results, page_context=page_context,
    )
    try:
        result = await platform_api.complete_llm(
            tenant_id=tenant_id, prompt=prompt, system=SYSTEM,
            task="crawl_medic", max_tokens=30, temperature=0.0,
        )
        # Read the telemetry field DEFENSIVELY — usage is an enrichment on the
        # client's result, not part of the decision contract, so a result object
        # without it still yields a decision.
        _usage = getattr(result, "usage", None)
        usage = (_usage.as_dict()
                 if _usage is not None and getattr(_usage, "reported", False)
                 else {})
        if not result.ok:
            logger.warning("qec.crawl_medic.llm_failed tenant=%s detail=%s",
                           tenant_id, result.detail[:200])
            # A failed consultation that the provider billed for is still spend.
            return MedicDecision(status=STATUS_UNAVAILABLE, usage=usage)
        decision = _parse_reply(result.text)
        return MedicDecision(status=decision.status, action=decision.action,
                             usage=usage)
    except Exception as exc:
        logger.warning("qec.crawl_medic.consult_failed tenant=%s error=%s",
                       tenant_id, str(exc)[:200])
        return MedicDecision(status=STATUS_UNAVAILABLE)
