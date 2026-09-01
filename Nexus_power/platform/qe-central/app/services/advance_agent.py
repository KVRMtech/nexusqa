"""Crawl-time agent: which control advances a multi-step flow?

Called mid-crawl in E2E mode when the deterministic regex
(_WIZARD_ADVANCE_RE / _WIZARD_COMMIT_RE) cannot identify the advance
control.  Sends only SHAPE — control names and kinds — never values,
never tenant data, never full URLs.  The LLM picks the obvious
"next step" button the way a human user would.

SAFETY (the commit boundary, defense in depth): the explorer filters
commit-labeled controls out of its candidate set before calling, but this
service must be safe for ANY caller — commit-labeled, danger, disabled and
nameless controls are dropped here too, BEFORE the prompt is built, and the
returned index is re-mapped to the caller's original list.  A commit pick
can therefore never leave this module, whoever calls it.

HONESTY (three-state outcome): "the LLM said nothing advances" (``none``)
and "the decision could not be made" (``unavailable`` — LLM failure or an
unreadable reply) are DIFFERENT facts.  The crawler ends a walk as covered
on the first and as NOT covered on the second; conflating them would report
an infrastructure failure as a finished journey.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from ..clients import platform_api
from . import advance_vocab

logger = logging.getLogger(__name__)

SYSTEM = (
    "You decide which control on a web page advances a multi-step flow "
    "to its next step.  You receive a numbered list of clickable controls.  "
    "Reply with ONLY the number of the control that a human user would "
    "click to move forward to the next step in the flow.  "
    "Controls that submit, pay, purchase, sign, finalize, or otherwise "
    "commit the transaction are never a valid answer: if the only way "
    "forward would commit, the flow is at its boundary — reply 0.  "
    "If none of the controls advance the flow (dead end, confirmation "
    "page, or the flow is already complete), reply 0.  "
    "Your ENTIRE reply must be a single number — no explanation, no "
    "preamble, no punctuation."
)

#: Consultation outcomes (the wire contract of ``/internal/pick-advance``).
STATUS_PICKED = "picked"
STATUS_NONE = "none"
STATUS_UNAVAILABLE = "unavailable"

#: Commit-boundary vocabulary — union-compiled from the language packs in
#: :mod:`app.services.advance_vocab`, which MUST stay data-identical to the
#: explorer's ``app/vocab.py`` (the two services share no library). Parity is
#: pinned by mirrored tests in BOTH suites:
#:   qe-central/tests/test_advance_agent.py::test_commit_vocabulary_parity_pin
#:   qe-explorer/tests/test_e2e_advance.py::test_commit_vocabulary_parity_pin
_COMMIT_VETO_RE = advance_vocab.COMMIT_RE

_NUM_RE = re.compile(r"\d+")
_WORD_RE = re.compile(r"[a-z]+")
_WS_RE = re.compile(r"\s+")
#: A digit-free reply that still EXPLICITLY says "nothing advances" — models
#: write "None" / "None of these" despite the reply-0 instruction. That is an
#: honest none, not an unreadable reply; conflating them turned a covered
#: journey into oracle_unavailable (observed live on VKPower quote/start).
_NONE_RE = re.compile(r"\b(none|no)\b", re.I)


@dataclass(frozen=True)
class AdvanceDecision:
    """One consultation outcome.

    ``index`` is 0-based into the CALLER's original control list (set only
    when ``status == picked``).  ``signature`` is the value-free
    decision-point signature the pick was made under — the key a PROVEN pick
    is later remembered by."""

    status: str
    index: int | None = None
    signature: str = ""
    #: Provider-REPORTED token usage for the LLM call this decision cost, or an
    #: empty dict when the decision was made without one (a recall hit, a
    #: filtered-out control set) or the provider reported nothing.  Relayed to
    #: the explorer so the tokens are attributed to the CRAWL that spent them —
    #: the per-crawl half of the accounting Prometheus deliberately cannot hold.
    usage: dict = field(default_factory=dict)


def eligible_controls(
    controls: list[dict],
) -> list[tuple[int, dict]]:
    """(original_index, control) pairs the agent may choose between.

    Dropped, in every case: disabled controls, danger controls (refuse-pack
    irreversibility), commit-labeled controls (the submit boundary is
    something a crawl STOPS at, never something an LLM may choose), and
    nameless controls (no name = no commit signal to veto on = a blind
    click; fail closed)."""
    out: list[tuple[int, dict]] = []
    for i, c in enumerate(controls):
        if not isinstance(c, dict):
            continue
        if c.get("disabled") or c.get("danger"):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        if _COMMIT_VETO_RE.search(name):
            continue
        out.append((i, c))
    return out


def compute_signature(
    eligible: list[tuple[int, dict]], page_title: str,
) -> str:
    """Value-free decision-point signature.

    Basis: the eligible controls' normalized names + kinds (sorted, so DOM
    order does not matter) plus the page title's word shape.  NO URLs, NO
    hosts, NO user values — safe to persist as tenant memory and to echo
    into crawl evidence."""
    names = sorted(
        f"{str(c.get('kind') or '')}::{str(c.get('name') or '').strip().lower()}"
        for _, c in eligible
    )
    title_shape = " ".join(_WORD_RE.findall(str(page_title or "").lower()))[:120]
    basis = "\n".join(names) + "\n#title::" + title_shape
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _build_prompt(
    controls: list[dict], page_title: str, page_url: str,
) -> str:
    lines = [f"Page: {page_title}"]
    base = page_url.split("?")[0] if page_url else ""
    if base:
        lines.append(f"URL path: {base}")
    lines.append("")
    lines.append("Controls:")
    for i, c in enumerate(controls, 1):
        kind = c.get("kind", "button")
        name = c.get("name") or "(unnamed)"
        flags = []
        if c.get("disabled"):
            flags.append("disabled")
        if c.get("danger"):
            flags.append("irreversible")
        extra = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {i}. \"{name}\" ({kind}){extra}")
    lines.append("")
    lines.append("Which number advances to the next step?")
    return "\n".join(lines)


async def pick_advance(
    *, tenant_id: str, controls: list[dict],
    page_title: str, page_url: str,
) -> AdvanceDecision:
    """Ask the agent which control advances the flow.

    Never raises.  ``picked`` carries an index into the caller's ORIGINAL
    list; ``none`` is the agent's honest "nothing here advances"; every
    failure to decide — LLM error, unreadable reply, out-of-range pick — is
    ``unavailable``, because a reply we could not read is not a reply that
    said stop."""
    if not controls:
        return AdvanceDecision(status=STATUS_UNAVAILABLE)

    eligible = eligible_controls(controls)
    if not eligible:
        # Everything the caller sent is commit/danger/disabled/nameless —
        # nothing here ADVANCES.  (Whether it is the submit boundary is the
        # crawler's classification, made from the full inventory.)
        return AdvanceDecision(status=STATUS_NONE)

    signature = compute_signature(eligible, page_title)

    # RECALL (tenant-private): a decision point this tenant already PROVED
    # answers instantly — no LLM call, no token spend.
    from . import advance_memory

    def _by_label(label_norm: str) -> AdvanceDecision | None:
        for original_index, c in eligible:
            if advance_memory.normalize_label(str(c.get("name") or "")) == label_norm:
                return AdvanceDecision(
                    status=STATUS_PICKED, index=original_index,
                    signature=signature)
        return None

    remembered = await advance_memory.recall(tenant_id, signature)
    if remembered:
        hit = _by_label(remembered)
        if hit is not None:
            logger.warning(
                "qec.advance_agent.recall_hit source=memory tenant=%s", tenant_id)
            return hit

    # TIER 2.5 (pooled, value-free): a candidate whose label the fleet has
    # proven advances — thresholds from settings, contribution consent-gated.
    prior = await advance_memory.recall_prior(
        tenant_id, {str(c.get("name") or "") for _, c in eligible})
    if prior:
        hit = _by_label(prior)
        if hit is not None:
            logger.warning(
                "qec.advance_agent.recall_hit source=prior tenant=%s", tenant_id)
            return hit

    prompt = _build_prompt([c for _, c in eligible], page_title, page_url)
    result = await platform_api.complete_llm(
        tenant_id=tenant_id, prompt=prompt, system=SYSTEM,
        task="pick_advance", max_tokens=60, temperature=0.0,
    )
    # From here on EVERY outcome carries the call's token cost. A consultation
    # that the model answered unreadably cost exactly what a clean pick cost, so
    # attaching usage only to the happy path would understate spend precisely on
    # the crawls that are going wrong.
    # Read the telemetry field DEFENSIVELY. Usage is an observability enrichment
    # on the client's result, not part of the decision contract — a result object
    # without it must still yield a decision, degrading to "spend unknown" rather
    # than to a failed consultation.
    _usage = getattr(result, "usage", None)
    usage = (_usage.as_dict()
             if _usage is not None and getattr(_usage, "reported", False)
             else {})

    def _decided(status: str, index: int | None = None) -> AdvanceDecision:
        return AdvanceDecision(status=status, index=index, signature=signature,
                               usage=usage)

    if not result.ok:
        logger.warning(
            "qec.advance_agent.llm_failed",
            extra={"tenant_id": tenant_id, "detail": (result.detail or "")[:200]},
        )
        return _decided(STATUS_UNAVAILABLE)

    reply = (result.text or "").strip()
    # The LAST number wins: a model that ignores the numbers-only instruction
    # writes preamble first and its conclusion last ("...so control 2"), and
    # for a bare "2" first==last anyway.
    numbers = _NUM_RE.findall(reply)
    match = numbers[-1] if numbers else None
    if not match:
        # Digit-free replies still carry honest answers. Models answer with
        # the CONTROL'S LABEL ("See My Quote") or with an explicit "none"
        # despite the reply-with-a-number instruction — both observed live.
        # Order matters: a label match wins (a label may contain the word
        # "no"), then none-words; only then is the reply truly unreadable —
        # and that is LOGGED in the message body (formatter-proof), never
        # silent.
        normalized_reply = _WS_RE.sub(" ", reply.strip().strip('"\'' ).lower())
        for original_index, c in eligible:
            name_norm = _WS_RE.sub(
                " ", str(c.get("name") or "").strip().lower())
            if name_norm and normalized_reply == name_norm:
                return _decided(STATUS_PICKED, original_index)
        if _NONE_RE.search(reply):
            return _decided(STATUS_NONE)
        logger.warning(
            "qec.advance_agent.unreadable_reply tenant=%s reply=%r",
            tenant_id, reply[:80],
        )
        return _decided(STATUS_UNAVAILABLE)
    picked = int(match)
    if picked == 0:
        return _decided(STATUS_NONE)
    idx = picked - 1
    if idx < 0 or idx >= len(eligible):
        return _decided(STATUS_UNAVAILABLE)
    return _decided(STATUS_PICKED, eligible[idx][0])
